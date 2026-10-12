"""Journal capability completion checks (offline, temp workspace only).

Complements test_workspace_capability.py: complete-collection listing across
pages, restart/replay durability through a fresh WorkspacePaths, and the
guarantee that legacy markdown journals are never blindly merged into the
structured Journal or modified.
"""

import hashlib
import json
from pathlib import Path

import workspace_capability as wc

CLARK = "actor-80eee447ad46e1a1e9b2ea65b5"


def _paths(root: Path):
    paths = wc.WorkspacePaths(str(root))
    paths.ensure_exists()
    return paths


def test_journal_listing_reaches_every_entry_across_pages(tmp_path):
    paths = _paths(tmp_path / "ws")
    written = set()
    for index in range(2 * wc.MAX_LIST_ENTRIES + 37):
        entry, failure = wc.append_journal_entry(paths, CLARK, f"entry {index}")
        assert failure is None
        written.add(entry["entry_id"] + ".json")

    seen = []
    cursor = None
    pages = 0
    while True:
        result, failure = wc.list_journal_entries(paths, cursor=cursor)
        assert failure is None
        pages += 1
        seen.extend(result["entries"])
        if not result["has_more"]:
            assert result["next_cursor"] is None
            break
        cursor = result["next_cursor"]
        assert pages < 20
    assert pages == 3
    assert len(seen) == len(set(seen)) == len(written)
    assert set(seen) == written


def test_journal_entries_survive_a_fresh_process_and_replay_is_exactly_once(tmp_path):
    root = tmp_path / "ws"
    first = _paths(root)
    entry, failure = wc.append_journal_entry(
        first, CLARK, "durable thought", session_id="session-1", source_event_id="H-source-1")
    assert failure is None

    restarted = _paths(root)  # a fresh WorkspacePaths over the same durable directory
    read_back, failure = wc.read_journal_entry(restarted, entry["entry_id"])
    assert failure is None
    assert read_back["content"] == "durable thought"
    assert read_back["author_actor_id"] == CLARK
    assert read_back["source_event_id"] == "H-source-1"

    replay, failure = wc.append_journal_entry(
        restarted, CLARK, "durable thought", session_id="session-1", source_event_id="H-source-1")
    assert failure is None
    assert replay["entry_id"] == entry["entry_id"]
    assert len(list(Path(restarted.journal_dir).glob("*.json"))) == 1
    listed, failure = wc.list_journal_entries(restarted)
    assert failure is None and listed["entries"] == [entry["entry_id"] + ".json"]


def test_legacy_markdown_journal_is_never_listed_read_merged_or_modified(tmp_path):
    paths = _paths(tmp_path / "ws")
    legacy_dir = Path(paths.journal_dir)
    legacy_files = {
        "2024-01-01-old-entry.md": "# legacy\nAlex wrote this long before ANAXI.\n",
        "notes.md": "unstructured legacy note\n",
    }
    before = {}
    for name, text in legacy_files.items():
        (legacy_dir / name).write_text(text, encoding="utf-8")
        before[name] = hashlib.sha256((legacy_dir / name).read_bytes()).hexdigest()
    entry, failure = wc.append_journal_entry(paths, CLARK, "a structured entry")
    assert failure is None

    listed, failure = wc.list_journal_entries(paths)
    assert failure is None
    assert listed["entries"] == [entry["entry_id"] + ".json"]
    assert listed["total_count"] == 1

    for name in legacy_files:
        stem = name[:-3]
        result, failure = wc.read_journal_entry(paths, stem)
        assert result is None and failure is not None, "legacy markdown must not be readable as an entry"
    for name in legacy_files:
        assert hashlib.sha256((legacy_dir / name).read_bytes()).hexdigest() == before[name]
    structured = json.loads((legacy_dir / (entry["entry_id"] + ".json")).read_text(encoding="utf-8"))
    assert "legacy" not in json.dumps(structured)
