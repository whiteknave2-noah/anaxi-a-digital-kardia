"""The delivery assay must catch host-success/subject-no-result, and must pass
a collection whose every item genuinely reaches the subject. Synthetic public
trees under tmp_path only: no production resource or Private Space is touched."""
import math
import struct
import wave
from pathlib import Path

import PIL.Image
import pytest

import public_resource_delivery_assay as delivery
import public_resource_ingest as ingest


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _wav(path, seconds=2, freq=440):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / 8000))) for i in range(8000 * seconds)
        ))


@pytest.fixture
def synthetic(tmp_path):
    source, workspace = tmp_path / "public", tmp_path / "workspace"
    long_book = "".join(f"Chapter line {i:05d}: the prose of a book long enough to need many windows.\n" for i in range(700))
    _write(source / "Books" / "long_book.txt", long_book.encode())
    _write(source / "Books" / "sub" / "nested.md", b"# Nested\n" + b"nested text. " * 60)
    (source / "Pictures").mkdir(parents=True)
    PIL.Image.new("RGB", (800, 600), (5, 6, 7)).save(source / "Pictures" / "photo.png")
    for i in range(151):   # the production music collection's size class
        _wav(source / "Music" / "Album" / f"Artist {i:03d} - A Reasonably Long Track Title {i:03d}.wav", seconds=1, freq=200 + i)
    _write(source / "ANAXI_CAPABILITIES_AND_PATHWAYS.md", b"capabilities " * 400)
    ingest.sync_public_resources(source, workspace)
    return source, workspace, long_book


def test_every_item_of_a_synthetic_collection_reaches_the_subject(synthetic):
    source, workspace, long_book = synthetic
    report = delivery.run_assay(source, workspace)
    assert report["collection_walk_errors"] == {}
    assert report["collection_walk_counts"] == {"library": 3, "photographs": 1, "music": 151}
    by_id = {r["capability_id"]: r for r in report["records"]}
    for record in by_id.values():
        assert record["failed_total"] == 0, record["failed_items_or_scenarios"]
        assert record["attempted_total"] == record["authoritative_expected_total"] > 0
    assert report["whole_resource_result"]["status"] == "COMPLETE"
    book = by_id["public_resource_delivery.library"]["item_results"][delivery.opaque_id("library", "Books/long_book.txt")]["evidence"]
    assert book["reached_end"] is True and book["delivered_chars"] == len(long_book)
    assert book["windows"] > 10          # a real multi-window read, not a single dropped page


def test_the_assay_fails_an_item_whose_result_would_not_reach_the_subject(synthetic, monkeypatch):
    """Simulates the historical defect: results larger than the room withheld."""
    source, workspace, _book = synthetic
    monkeypatch.setattr(delivery, "ROOM", 40)   # nothing but trivia fits
    report = delivery.run_assay(source, workspace)
    assert report["whole_resource_result"]["status"] != "COMPLETE"
    failures = [r for r in report["records"] if r["failed_total"]]
    assert failures, "an undeliverable result must be a failure, never a silent pass"


def test_the_assay_fails_when_the_authoritative_source_cannot_be_established(tmp_path):
    report = delivery.run_assay(tmp_path / "missing", tmp_path / "workspace")
    assert report["authoritative_discovery_error"] is not None
    assert report["whole_resource_result"]["status"] != "COMPLETE"


def test_evidence_records_no_resource_names(synthetic):
    import json as _json
    source, workspace, _book = synthetic
    rendered = _json.dumps(delivery.run_assay(source, workspace))
    for name in ("long_book", "nested.md", "photo.png", "Artist 000", "Album", "Books/"):
        assert name not in rendered, name
