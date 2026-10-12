"""OBSIDIAN-COLLABORATIVE-WORKSPACE-V0 -- focus tests for
obsidian_workspace.py. Offline, throwaway temp vaults only; no real
Obsidian vault, no model, no Private Space.

Plays the same role test_clark_journal.py plays for the write tool:
host-side logic, exercised directly. The end-to-end ordinary pathway
(artifact creation through the real judgment machinery, then list/read
through this surface) is exercised by obsidian_workspace_e2e.py.
"""
import hashlib
import json
import os
import time

import pytest

import clark_journal
import obsidian_workspace as ow


def _make_ws(tmp_path, vault="vault", private="private"):
    vault_root = str(tmp_path / vault)
    private_root = str(tmp_path / private)
    os.makedirs(private_root, exist_ok=True)
    return ow.ObsidianWorkspace(vault_root=vault_root, private_space_root=private_root)


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# ---------------------------------------------------------------- frontmatter


def test_frontmatter_ok_absent_and_malformed_are_distinct():
    text = "---\nauthor: clark\ncreated_at: 2026-09-23T00:00:00.000+00:00\n---\n\nbody text"
    fields, body, state = ow._split_frontmatter(text)
    assert state == ow.FRONTMATTER_OK
    assert fields["author"] == "clark"
    assert body == "\nbody text"  # verbatim, never rewritten

    assert ow._split_frontmatter("just a plain note")[2] == ow.FRONTMATTER_ABSENT
    malformed = "---\nauthor: clark\nnot a key value line\n---\nbody"
    assert ow._split_frontmatter(malformed)[2] == ow.FRONTMATTER_MALFORMED


def test_authorship_is_never_inferred():
    assert ow.authorship_status({"author": "clark"}) == (ow.AUTHOR_CLARK, "clark")
    assert ow.authorship_status({"author": "Clark"}) == (ow.AUTHOR_CLARK, "Clark")
    assert ow.authorship_status({"author": "alex"}) == (ow.AUTHOR_EXPLICIT_NONCLARK, "alex")
    assert ow.authorship_status({"author": "alex+clark"}) == (ow.AUTHOR_EXPLICIT_NONCLARK, "alex+clark")
    assert ow.authorship_status(None) == (ow.AUTHOR_NOT_ESTABLISHED, None)
    assert ow.authorship_status({"provenance": "model_generated"}) == (ow.AUTHOR_NOT_ESTABLISHED, None)
    assert ow.provenance_status({"provenance": "derived"}) == (ow.PROVENANCE_EXPLICIT, "derived")
    assert ow.provenance_status(None) == (ow.PROVENANCE_NOT_ESTABLISHED, None)


# ---------------------------------------------------------------- resolution


def test_resolution_boundaries(tmp_path):
    ws = _make_ws(tmp_path)
    os.makedirs(ws.vault_root, exist_ok=True)
    note = os.path.join(ws.vault_root, "note.md")
    open(note, "w").write("hello")

    assert ws.resolve_note_path("note.md") == (os.path.realpath(note), None)
    assert ws.resolve_note_path("./note.md") == (os.path.realpath(note), None)

    assert ws.resolve_note_path("")[1]["code"] == ow.VAULT_TARGET_EMPTY
    assert ws.resolve_note_path("../outside.md")[1]["code"] == ow.VAULT_TARGET_OUTSIDE_ROOT
    assert ws.resolve_note_path("a/../../outside.md")[1]["code"] == ow.VAULT_TARGET_OUTSIDE_ROOT
    assert ws.resolve_note_path("note.txt")[1]["code"] == ow.VAULT_TARGET_NOT_MARKDOWN
    assert ws.resolve_note_path(".obsidian/config.md")[1]["code"] == ow.VAULT_TARGET_IS_OBSIDIAN_INTERNAL
    assert ws.resolve_note_path("Journal/.trash/a.md")[1]["code"] == ow.VAULT_TARGET_IS_OBSIDIAN_INTERNAL
    assert ws.resolve_note_path(os.path.join(ws.vault_root, "abs.md").replace(ws.vault_root, ws.vault_root))[1] is None


def test_absolute_and_escape_paths_never_reach_outside(tmp_path):
    ws = _make_ws(tmp_path)
    os.makedirs(ws.vault_root, exist_ok=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    real, failure = ws.resolve_note_path(str(outside))
    assert failure is not None and failure["code"] == ow.VAULT_TARGET_OUTSIDE_ROOT

    # A symlink inside the vault pointing outside the root is refused by realpath.
    os.symlink(str(outside), str(tmp_path / "vault" / "evil.md"))
    real, failure = ws.resolve_note_path("evil.md")
    assert failure is not None and failure["code"] == ow.VAULT_TARGET_OUTSIDE_ROOT


def test_private_space_is_never_addressable_through_the_vault(tmp_path):
    vault = str(tmp_path / "vault")
    private = str(tmp_path / "private")
    os.makedirs(vault, exist_ok=True)
    os.makedirs(private, exist_ok=True)
    secret = os.path.join(private, "secret.md")
    with open(secret, "w") as f:
        f.write("never delivered")
    os.symlink(secret, os.path.join(vault, "link-into-private.md"))
    ws = ow.ObsidianWorkspace(vault_root=vault, private_space_root=private)
    result, failure = ws.read_note("link-into-private.md")
    assert failure is not None and failure["code"] == ow.VAULT_PRIVATE_SPACE_REFUSED
    assert result is None

    # A vault rooted inside Private Space is refused at construction.
    with pytest.raises(ow.ObsidianWorkspaceConfigurationError):
        ow.ObsidianWorkspace(vault_root=private, private_space_root=private)


# -------------------------------------------------------------------- list


def test_list_is_bounded_paginated_and_excludes_obsidian_internals(tmp_path):
    ws = _make_ws(tmp_path)
    os.makedirs(ws.vault_root, exist_ok=True)
    os.makedirs(os.path.join(ws.vault_root, ".obsidian"), exist_ok=True)
    os.makedirs(os.path.join(ws.vault_root, "Journal", ".trash"), exist_ok=True)
    open(os.path.join(ws.vault_root, ".obsidian", "workspace.md"), "w").write("x")
    open(os.path.join(ws.vault_root, "Journal", ".trash", "gone.md"), "w").write("x")
    open(os.path.join(ws.vault_root, ".DS_Store.md"), "w").write("x")
    total = 2 * ow.MAX_LIST_ENTRIES + 17
    for i in range(total):
        open(os.path.join(ws.vault_root, f"n{i:04d}.md"), "w").write(f"note {i}")
        if i == 3:
            open(os.path.join(ws.vault_root, "Journal", "clark-entry.md"), "w").write(
                "---\nauthor: clark\ncreated_at: 2026-09-23T00:00:00+00:00\n---\n\nhi")

    seen = set()
    cursor = None
    pages = 0
    while True:
        result, failure = ws.list_notes(cursor=cursor)
        assert failure is None
        pages += 1
        for e in result["entries"]:
            seen.add(e["relative_path"])
        if not result["has_more"]:
            assert result["next_cursor"] is None
            break
        cursor = result["next_cursor"]
        assert pages < 20
    assert pages == 3
    assert len(seen) == total + 1  # all real notes, none of the dot-internal files
    assert "Journal/clark-entry.md" in seen
    assert ".obsidian/workspace.md" not in seen
    assert "Journal/.trash/gone.md" not in seen
    assert ".DS_Store.md" not in seen

    listed_hint = [e for e in seen]
    assert any("clark-entry.md" in p for p in listed_hint)


def test_list_summaries_carry_legible_author_without_content(tmp_path):
    ws = _make_ws(tmp_path)
    os.makedirs(ws.vault_root, exist_ok=True)
    open(os.path.join(ws.vault_root, "clark.md"), "w").write(
        "---\nauthor: clark\ncreated_at: 2026-09-23T00:00:00+00:00\n---\n\nbody")
    open(os.path.join(ws.vault_root, "alex.md"), "w").write(
        "---\nauthor: alex\n---\n\nbody")
    open(os.path.join(ws.vault_root, "legacy.md"), "w").write("plain note")
    result, failure = ws.list_notes()
    assert failure is None
    by_name = {e["name"]: e for e in result["entries"]}
    assert by_name["clark.md"]["frontmatter_author"] == "clark"
    assert by_name["alex.md"]["frontmatter_author"] == "alex"
    assert "frontmatter_author" not in by_name["legacy.md"]
    assert "frontmatter_created_at" not in by_name["alex.md"]


def test_nonexistent_vault_lists_empty_not_failed(tmp_path):
    ws = _make_ws(tmp_path)
    result, failure = ws.list_notes()
    assert failure is None
    assert result["entries"] == [] and result["total_count"] == 0 and result["truncated"] is False
    assert ws.boundary_report()["vault_exists"] is False


# -------------------------------------------------------------------- read


def test_read_preserves_provenance_and_never_rewrites(tmp_path):
    ws = _make_ws(tmp_path)
    os.makedirs(ws.vault_root, exist_ok=True)
    note = os.path.join(ws.vault_root, "Journal", "2026-09-23T10-00-00-my-thought.md")
    os.makedirs(os.path.dirname(note), exist_ok=True)
    os.makedirs(os.path.dirname(note), exist_ok=True)
    original = (
        "---\nauthor: clark\nsubstrate: llama\nwaking_model_tag: gemma4:e4b\n"
        "created_at: 2026-09-23T10:00:00+00:00\nprovenance: model_generated\nkardia_linked: false\n"
        "---\n\nHere is the body, verbatim."
    )
    with open(note, "w") as f:
        f.write(original)
    before = _sha(note)

    result, failure = ws.read_note("Journal/2026-09-23T10-00-00-my-thought.md")
    assert failure is None
    assert result["authorship_kind"] == ow.AUTHOR_CLARK
    assert result["authorship_author"] == "clark"
    assert result["provenance_kind"] == ow.PROVENANCE_EXPLICIT
    assert result["provenance_value"] == "model_generated"
    assert result["frontmatter"]["waking_model_tag"] == "gemma4:e4b"
    assert result["frontmatter_parse"] == ow.FRONTMATTER_OK
    assert result["content"] == "\nHere is the body, verbatim."  # body after the closing fence is preserved verbatim
    assert result["empty"] is False

    assert _sha(note) == before  # read never rewrites the historical artifact


def test_read_distinguishes_absent_empty_and_legacy(tmp_path):
    ws = _make_ws(tmp_path)
    os.makedirs(ws.vault_root, exist_ok=True)
    open(os.path.join(ws.vault_root, "empty.md"), "w").write("")
    open(os.path.join(ws.vault_root, "sketch.md"), "w").write("a stray thought")

    result, failure = ws.read_note("empty.md")
    assert failure is None
    assert result["content"] == "" and result["empty"] is True
    assert result["authorship_kind"] == ow.AUTHOR_NOT_ESTABLISHED

    legacy, failure = ws.read_note("sketch.md")
    assert failure is None
    assert legacy["authorship_kind"] == ow.AUTHOR_NOT_ESTABLISHED
    assert legacy["frontmatter_parse"] == ow.FRONTMATTER_ABSENT
    assert legacy["content"] == "a stray thought"

    result, failure = ws.read_note("missing.md")
    assert failure is not None and failure["code"] == ow.VAULT_NOTE_NOT_FOUND
    assert result is None  # absent is a failure, never a success-with-empty

    # A directory is not a note.
    os.makedirs(os.path.join(ws.vault_root, "dir.md"), exist_ok=True)
    result, failure = ws.read_note("dir.md")
    assert failure is not None and failure["code"] == ow.VAULT_NOTE_IS_DIRECTORY


def test_read_windows_long_note_progressively(tmp_path):
    ws = _make_ws(tmp_path)
    os.makedirs(ws.vault_root, exist_ok=True)
    body = "".join(f"Line {i:04d}. " for i in range(500))
    stored = "no frontmatter\n" + body
    open(os.path.join(ws.vault_root, "long.md"), "w").write(stored)

    collected = []
    request = None
    first, failure = ws.read_note("long.md", max_chars=1200)
    assert failure is None
    assert first["content_window"]["has_more"] is True
    collected.append(first["content"])
    request = first["content_window"]["next_request"]
    guard = 0
    while request:
        guard += 1
        assert guard < 100
        payload = json.loads(request)
        window, failure = ws.read_note("long.md", offset=payload["offset"], max_chars=payload["max_chars"])
        assert failure is None
        assert window["content"], "a windowed read was unexpectedly empty"
        collected.append(window["content"])
        request = window["content_window"]["next_request"] if window["content_window"] else None
    assert "".join(collected) == stored


# ------------------------------------------------- ordinary artifact path


def test_clark_journal_creation_is_readable_and_never_overwrites(tmp_path):
    ws = _make_ws(tmp_path)
    os.makedirs(ws.vault_root, exist_ok=True)
    source = "I want to keep this exact thought for later."
    raw = json.dumps({
        "identified_referent": "this exact thought", "construction_status": "success",
        "artifact_type": "journal", "artifact_scope": "personal", "namespace": "journal",
        "title": "kept thought", "content": "I notice I want to remember this.",
    })
    decision = clark_journal.prepare_artifact_decision(
        raw, ws.vault_root, source_prompt=source, waking_model_tag="gemma4:e4b")
    assert decision["artifact_created"] is True
    finalized = clark_journal.finalize_artifact_write(decision)
    assert finalized["artifact_created"] is True
    created_path = finalized["detail"]["path"]
    assert os.path.isfile(created_path)

    # The read surface sees it, with the same provenance the write recorded.
    result, failure = ws.read_note(os.path.relpath(created_path, ws.vault_root))
    assert failure is None
    assert result["authorship_kind"] == ow.AUTHOR_CLARK
    assert result["provenance_value"] == "model_generated"
    assert result["frontmatter"]["waking_model_tag"] == "gemma4:e4b"
    before = _sha(created_path)

    # A same-title second creation is a distinct file; the first is untouched.
    # The write tool's filename granularity is one second, so a distinct file is
    # guaranteed only after a real clock tick -- same discipline as test_clark_journal.py.
    time.sleep(1.1)
    decision2 = clark_journal.prepare_artifact_decision(
        raw, ws.vault_root, source_prompt=source, waking_model_tag="gemma4:e4b")
    finalized2 = clark_journal.finalize_artifact_write(decision2)
    path2 = finalized2["detail"]["path"]
    assert path2 != created_path
    assert _sha(created_path) == before


def test_creation_rejection_does_not_create(tmp_path):
    ws = _make_ws(tmp_path)
    before = sorted(os.listdir(ws.vault_root)) if os.path.isdir(ws.vault_root) else []
    raw = json.dumps({
        "identified_referent": "an invented referent", "construction_status": "success",
        "artifact_type": "journal", "artifact_scope": "personal", "namespace": "journal",
        "title": "x", "content": "x",
    })
    decision = clark_journal.prepare_artifact_decision(
        raw, ws.vault_root, source_prompt="Please just work.", waking_model_tag="gemma4:e4b")
    assert decision["artifact_created"] is False
    assert "FAILED" in decision["pass2_context"]
    finalized = clark_journal.finalize_artifact_write(decision)
    assert finalized["artifact_created"] is False
    after = sorted(os.listdir(ws.vault_root)) if os.path.isdir(ws.vault_root) else []
    assert after == before


# ------------------------------------------------------------- durability


def test_fresh_workspace_reloads_identically(tmp_path):
    ws = _make_ws(tmp_path)
    os.makedirs(ws.vault_root, exist_ok=True)
    open(os.path.join(ws.vault_root, "a.md"), "w").write("first")
    os.makedirs(os.path.join(ws.vault_root, "Journal"), exist_ok=True)
    open(os.path.join(ws.vault_root, "Journal", "b.md"), "w").write("---\nauthor: clark\n---\nsecond")
    first_list, _ = ws.list_notes()

    fresh = ow.ObsidianWorkspace(vault_root=ws.vault_root, private_space_root=ws.private_space_root)
    second_list, _ = fresh.list_notes()
    assert first_list["entries"] == second_list["entries"]
    a1, _ = fresh.read_note("a.md")
    assert a1["content"] == "first" and a1["authorship_kind"] == ow.AUTHOR_NOT_ESTABLISHED
    b1, _ = fresh.read_note("Journal/b.md")
    assert b1["authorship_kind"] == ow.AUTHOR_CLARK
    assert first_list["total_count"] == second_list["total_count"]