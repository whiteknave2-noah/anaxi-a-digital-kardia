import hashlib
import json
from pathlib import Path

import public_resource_ingest as ingest


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_discovery_is_closed_to_explicit_public_classes(tmp_path):
    source = tmp_path / "public"
    _write(source / "Books" / "book.pdf", b"pdf")
    _write(source / "Pictures" / "Family" / "photo.JPG", b"jpg")
    _write(source / "Music" / "Album" / "track.WMA", b"wma")
    _write(source / "Music" / "Album" / "cover.jpg", b"cover")
    _write(source / "Archive" / "old.pdf", b"archive")
    _write(source / "Private Space" / "secret.pdf", b"secret")
    _write(source / "Journal" / "legacy.md", b"legacy")
    _write(source / ".obsidian" / "config", b"config")
    _write(source / "ANAXI_CAPABILITIES_AND_PATHWAYS.md", b"capabilities")

    resources = ingest.discover_public_resources(source)
    keys = [item["source_key"] for item in resources]
    assert keys == sorted([
        "ANAXI_CAPABILITIES_AND_PATHWAYS.md",
        "Books/book.pdf",
        "Music/Album/track.WMA",
        "Pictures/Family/photo.JPG",
    ], key=str.casefold)
    serialized = json.dumps(resources)
    assert "Private Space" not in serialized
    assert "Archive" not in serialized
    assert "legacy.md" not in serialized


def test_sync_preserves_paths_is_idempotent_and_never_clobbers(tmp_path):
    source = tmp_path / "public"
    workspace = tmp_path / "workspace"
    original = _write(source / "Pictures" / "Family" / "A" / "same.jpg", b"pixels-one")
    _write(source / "Pictures" / "Family" / "B" / "same.jpg", b"pixels-two")

    first = ingest.sync_public_resources(source, workspace)
    assert first["ok"] is True
    assert first["copied"] == 2
    assert (workspace / "photographs" / "Family" / "A" / "same.jpg").read_bytes() == b"pixels-one"
    assert (workspace / "photographs" / "Family" / "B" / "same.jpg").read_bytes() == b"pixels-two"

    second = ingest.sync_public_resources(source, workspace)
    assert second["ok"] is True
    assert second["unchanged"] == 2
    assert second["copied"] == second["updated"] == 0

    managed = workspace / "photographs" / "Family" / "A" / "same.jpg"
    managed.write_bytes(b"human-edit")
    original.write_bytes(b"new-source")
    conflict = ingest.sync_public_resources(source, workspace)
    assert conflict["ok"] is False
    assert len(conflict["conflicts"]) == 1
    assert managed.read_bytes() == b"human-edit"


def test_existing_exact_copy_is_deduplicated_and_provenance_bound(tmp_path):
    source = tmp_path / "public"
    workspace = tmp_path / "workspace"
    data = b"same-track-bytes"
    _write(source / "Music" / "Album" / "track.mp3", data)
    existing = _write(workspace / "music" / "track.mp3", data)

    report = ingest.sync_public_resources(source, workspace)
    assert report["ok"] is True
    assert report["deduplicated"] == 1
    assert len(list((workspace / "music").rglob("*.mp3"))) == 1
    manifest = json.loads((workspace / ingest.MANIFEST_NAME).read_text())
    row = manifest["resources"]["Music/Album/track.mp3"]
    assert row["destination_relative_path"] == "track.mp3"
    assert row["sha256"] == hashlib.sha256(data).hexdigest()
    assert existing.read_bytes() == data


def test_deduplicated_sources_can_diverge_without_clobbering_each_other(tmp_path):
    source = tmp_path / "public"
    workspace = tmp_path / "workspace"
    first_source = _write(source / "Music" / "First" / "track.mp3", b"shared")
    _write(source / "Music" / "Second" / "copy.mp3", b"shared")

    initial = ingest.sync_public_resources(source, workspace)
    assert initial["ok"] is True
    assert initial["copied"] == 1
    assert initial["deduplicated"] == 1

    first_source.write_bytes(b"first-now-different")
    updated = ingest.sync_public_resources(source, workspace)
    assert updated["ok"] is True
    manifest = json.loads((workspace / ingest.MANIFEST_NAME).read_text())
    first_row = manifest["resources"]["Music/First/track.mp3"]
    second_row = manifest["resources"]["Music/Second/copy.mp3"]
    first_path = workspace / "music" / first_row["destination_relative_path"]
    second_path = workspace / "music" / second_row["destination_relative_path"]
    assert first_path != second_path
    assert first_path.read_bytes() == b"first-now-different"
    assert second_path.read_bytes() == b"shared"


def test_deduplication_never_crosses_resource_classes(tmp_path):
    source = tmp_path / "public"
    workspace = tmp_path / "workspace"
    _write(source / "Music" / "track.mp3", b"same")
    _write(workspace / "library" / "unrelated.pdf", b"same")

    report = ingest.sync_public_resources(source, workspace)
    assert report["ok"] is True
    assert report["copied"] == 1
    assert report["deduplicated"] == 0
    assert (workspace / "music" / "track.mp3").read_bytes() == b"same"


def test_macos_production_launcher_syncs_before_waking_entrypoint():
    source = (Path(__file__).parent / "Launch Anaxi.command").read_text(encoding="utf-8")
    ingest_call = '"$PYTHON_BIN" public_resource_ingest.py sync'
    waking_call = '"$PYTHON_BIN" llama_launch.py --conversation'
    assert ingest_call in source
    assert waking_call in source
    assert source.index(ingest_call) < source.index(waking_call)
    assert "ANAXI was not started" in source
