"""WSP1-S1 acceptance tests. Zero real model/Ollama calls (this module
makes none at all, structurally). Every test uses a fresh temp
workspace root -- the real production anaxi_final/workspace/ path is
never touched, never created, never listed.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import traceback
import zlib

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import workspace_capability as wsp
import pypdf

CLARK_ACTOR_ID = "actor-80eee447ad46e1a1e9b2ea65b5"  # matches production's real Clark actor id, used as a plain string here (no DB dependency)

TEST_ROOT = tempfile.mkdtemp(prefix="wsp1_test_")


def fresh_paths(name):
    root = os.path.join(TEST_ROOT, name)
    paths = wsp.WorkspacePaths(root=root)
    paths.ensure_exists()
    return paths


def write_fixture_file(paths, resource_class, filename, content=b"fixture content"):
    d = paths.dir_for(resource_class)
    os.makedirs(d, exist_ok=True)
    full = os.path.join(d, filename)
    mode = "w" if isinstance(content, str) else "wb"
    with open(full, mode, encoding="utf-8" if isinstance(content, str) else None) as f:
        f.write(content)
    return full


def build_compressed_text_pdf(pages_text):
    """A standards-compliant, minimal PDF with a proper xref table and
    a real FlateDecode-compressed content stream per page -- the
    representative real-world shape (WSP2-P1 established that virtually
    every real PDF writer compresses content streams). Built by hand
    (no reportlab/fpdf dependency) since only pypdf was authorized."""
    n_pages = len(pages_text)
    font_num = 3 + 2 * n_pages
    total_objs = font_num
    kids = " ".join(f"{3 + i} 0 R" for i in range(n_pages))

    buf = bytearray(b"%PDF-1.4\n")
    offsets = {}

    def add_obj(num, content_bytes):
        offsets[num] = len(buf)
        buf.extend(f"{num} 0 obj\n".encode())
        buf.extend(content_bytes)
        buf.extend(b"\nendobj\n")

    add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add_obj(2, (f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>").encode())
    for i in range(n_pages):
        page_num, content_num = 3 + i, 3 + n_pages + i
        add_obj(page_num, (
            f"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 {font_num} 0 R >> >> "
            f"/MediaBox [0 0 300 144] /Contents {content_num} 0 R >>"
        ).encode())
    for i, text in enumerate(pages_text):
        content_num = 3 + n_pages + i
        stream = f"BT /F1 24 Tf 20 100 Td ({text}) Tj ET".encode()
        compressed = zlib.compress(stream)
        header = f"<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode()
        add_obj(content_num, header + compressed + b"\nendstream")
    add_obj(font_num, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    xref_offset = len(buf)
    buf.extend(f"xref\n0 {total_objs + 1}\n".encode())
    buf.extend(b"0000000000 65535 f \n")
    for num in range(1, total_objs + 1):
        buf.extend(f"{offsets[num]:010d} 00000 n \n".encode())
    buf.extend(f"trailer\n<< /Size {total_objs + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode())
    return bytes(buf)


def write_pdf_fixture(paths, filename, pages_text):
    full = os.path.join(paths.library_dir, filename)
    os.makedirs(paths.library_dir, exist_ok=True)
    with open(full, "wb") as f:
        f.write(build_compressed_text_pdf(pages_text))
    return full


def write_scanned_pdf_fixture(paths, filename):
    """A valid PDF with a blank page and no text content stream --
    the scanned/image-only case: parses successfully, yields no text."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    full = os.path.join(paths.library_dir, filename)
    os.makedirs(paths.library_dir, exist_ok=True)
    with open(full, "wb") as f:
        writer.write(f)
    return full


def write_encrypted_pdf_fixture(paths, filename, password="secret123"):
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_password=password, owner_password="owner-" + password)
    full = os.path.join(paths.library_dir, filename)
    os.makedirs(paths.library_dir, exist_ok=True)
    with open(full, "wb") as f:
        writer.write(f)
    return full


# ------------------------------------------------------- A: resource enumeration


def test_resource_enumeration_exact_four_classes():
    assert wsp.list_resource_classes() == ["library", "music", "photographs", "journal", "notes"]
    assert set(wsp.CAPABILITY_DESCRIPTORS.keys()) == {"library", "music", "photographs", "journal", "notes"}


# -------------------------------------------------------------- B: library read


def test_library_bounded_read_succeeds():
    paths = fresh_paths("library_read")
    write_fixture_file(paths, wsp.LIBRARY, "book.txt", "0123456789" * 10)  # 100 chars
    result, failure = wsp.read_library_bounded(paths, "book.txt", offset=0, max_chars=10)
    assert failure is None
    assert result["content"] == "0123456789"
    assert result["has_more"] is True
    assert result["next_offset"] == 10

    result2, failure2 = wsp.read_library_bounded(paths, "book.txt", offset=90, max_chars=50)
    assert failure2 is None
    assert result2["content"] == "0123456789"
    assert result2["has_more"] is False


def test_library_read_missing_file_fails_closed():
    paths = fresh_paths("library_missing")
    result, failure = wsp.read_library_bounded(paths, "nope.txt")
    assert result is None
    assert failure is not None


# --------------------------------------------------------- A2: bounded list (WSP2-P2)


def test_list_small_directory_returns_every_filename_untruncated():
    paths = fresh_paths("list_small")
    for name in ("b.txt", "a.txt", "c.txt"):
        write_fixture_file(paths, wsp.LIBRARY, name, "x")
    result, failure = wsp.list_contents(paths, wsp.LIBRARY)
    assert failure is None
    assert result["entries"] == ["a.txt", "b.txt", "c.txt"]  # alphabetical
    assert result["returned_count"] == 3
    assert result["total_count"] == 3
    assert result["truncated"] is False


def test_list_empty_directory_returns_cleanly_not_truncated():
    paths = fresh_paths("list_empty")
    result, failure = wsp.list_contents(paths, wsp.LIBRARY)
    assert failure is None
    assert result["entries"] == []
    assert result["returned_count"] == 0
    assert result["total_count"] == 0
    assert result["truncated"] is False


def test_list_entry_count_overflow_truncates_deterministically():
    paths = fresh_paths("list_count_overflow")
    names = [f"file_{i:04d}.txt" for i in range(150)]
    for name in names:
        write_fixture_file(paths, wsp.LIBRARY, name, "x")
    result, failure = wsp.list_contents(paths, wsp.LIBRARY)
    assert failure is None
    assert result["total_count"] == 150
    assert result["returned_count"] == wsp.MAX_LIST_ENTRIES
    assert len(result["entries"]) == wsp.MAX_LIST_ENTRIES
    assert result["truncated"] is True
    assert result["entries"] == sorted(names)[:wsp.MAX_LIST_ENTRIES]  # deterministic alphabetical prefix, never sampled


def test_list_character_overflow_truncates_before_entry_count_limit():
    paths = fresh_paths("list_char_overflow")
    # 80 entries * 100 chars each = 8000 chars, safely over
    # MAX_LIST_AGGREGATE_CHARS=6000 while staying under MAX_LIST_ENTRIES=100
    # -- proves the character bound can bind first.
    names = sorted(f"{i:03d}_" + ("x" * 96) + ".txt" for i in range(80))
    for name in names:
        write_fixture_file(paths, wsp.LIBRARY, name, "x")
    result, failure = wsp.list_contents(paths, wsp.LIBRARY)
    assert failure is None
    assert result["total_count"] == 80
    assert result["returned_count"] < 80  # character bound stopped it before entry-count would have
    assert result["truncated"] is True
    assert sum(len(e) for e in result["entries"]) <= wsp.MAX_LIST_AGGREGATE_CHARS
    assert result["entries"] == names[:result["returned_count"]]  # still the deterministic alphabetical prefix


def test_list_pathological_long_filename_cannot_defeat_the_bound():
    paths = fresh_paths("list_pathological")
    huge_name_base = "z" * (wsp.MAX_LIST_AGGREGATE_CHARS + 500)
    # Can't literally create a file with an OS-illegal/too-long name on
    # every platform, so this proves the pure bounding function
    # directly against a pathological input, independent of filesystem
    # name-length limits.
    entries = ["a.txt", huge_name_base + ".txt", "z_after.txt"]
    bounded = wsp._bound_entries(entries)
    assert bounded["total_count"] == 3
    assert bounded["entries"] == ["a.txt"]  # stops cleanly before the oversized entry
    assert bounded["truncated"] is True
    assert sum(len(e) for e in bounded["entries"]) <= wsp.MAX_LIST_AGGREGATE_CHARS
    # Never partially rewritten/truncated to force a fit.
    for e in bounded["entries"]:
        assert e in entries


def test_bound_entries_never_randomly_samples():
    entries = [f"f{i:03d}.txt" for i in range(300)]
    bounded1 = wsp._bound_entries(entries)
    bounded2 = wsp._bound_entries(entries)
    assert bounded1 == bounded2  # deterministic, not sampled
    assert bounded1["entries"] == entries[:wsp.MAX_LIST_ENTRIES]


def test_list_bounds_apply_to_music_and_photographs_same_code_path():
    paths = fresh_paths("list_bounds_other_classes")
    for name in [f"song_{i:03d}.mp3" for i in range(150)]:
        write_fixture_file(paths, wsp.MUSIC, name, b"x")
    music_result, mf = wsp.list_contents(paths, wsp.MUSIC)
    assert mf is None
    assert music_result["truncated"] is True
    assert music_result["returned_count"] == wsp.MAX_LIST_ENTRIES

    for name in [f"photo_{i:03d}.jpg" for i in range(5)]:
        write_fixture_file(paths, wsp.PHOTOGRAPHS, name, b"x")
    photo_result, pf = wsp.list_contents(paths, wsp.PHOTOGRAPHS)
    assert pf is None
    assert photo_result["truncated"] is False
    assert photo_result["total_count"] == 5


def test_journal_list_bounds_apply_after_json_filter_not_before():
    paths = fresh_paths("list_journal_bounds")
    for i in range(150):
        wsp.append_journal_entry(paths, CLARK_ACTOR_ID, f"entry number {i}")
    # A non-.json file placed directly in the journal directory (never
    # produced by append_journal_entry itself, but proves filtering
    # happens before bounding, not after).
    write_fixture_file(paths, wsp.JOURNAL, "not_an_entry.txt", "x")
    result, failure = wsp.list_journal_entries(paths)
    assert failure is None
    assert all(e.endswith(".json") for e in result["entries"])
    assert result["total_count"] == 150  # the stray .txt file is excluded from total_count too
    assert result["returned_count"] == wsp.MAX_LIST_ENTRIES
    assert result["truncated"] is True


def test_journal_list_small_untruncated():
    paths = fresh_paths("list_journal_small")
    wsp.append_journal_entry(paths, CLARK_ACTOR_ID, "one entry")
    result, failure = wsp.list_journal_entries(paths)
    assert failure is None
    assert result["total_count"] == 1
    assert result["truncated"] is False


def test_list_permissions_unchanged_library_still_read_only():
    paths = fresh_paths("list_perms_unchanged")
    write_fixture_file(paths, wsp.LIBRARY, "book.txt", "x")
    for action in (wsp.WRITE, wsp.RENAME, wsp.DELETE, wsp.MOVE_OUTSIDE_WORKSPACE, wsp.EXTERNAL_SHARE):
        result, failure = wsp.attempt_library_mutation(paths, action, "book.txt")
        assert result is None, f"action={action}"
    result, failure = wsp.list_contents(paths, wsp.LIBRARY)
    assert failure is None  # list itself remains allowed
    assert result["entries"] == ["book.txt"]


def test_list_action_log_still_records_exactly_one_entry():
    paths = fresh_paths("list_single_log_entry")
    for name in [f"f{i}.txt" for i in range(150)]:
        write_fixture_file(paths, wsp.LIBRARY, name, "x")
    wsp.list_contents(paths, wsp.LIBRARY)
    log = wsp.query_action_log(paths)
    assert len(log) == 1
    assert log[0]["action"] == wsp.LIST and log[0]["result"] == "performed"


# -------------------------------------------------- B2: library PDF reading (WSP2-S2)


def test_pdf_normal_text_extracted():
    paths = fresh_paths("pdf_normal")
    write_pdf_fixture(paths, "book.pdf", ["Hello Anaxi Library PDF Test"])
    result, failure = wsp.read_library_bounded(paths, "book.pdf")
    assert failure is None
    assert result["content"] == "Hello Anaxi Library PDF Test"
    assert result["document_type"] == "pdf"
    assert result["page_count"] == 1
    assert result["pages_examined"] == 1
    assert result["has_more"] is False


def test_pdf_raw_syntax_and_garbage_never_returned():
    paths = fresh_paths("pdf_raw_syntax")
    write_pdf_fixture(paths, "book.pdf", ["Clean extracted text only"])
    result, failure = wsp.read_library_bounded(paths, "book.pdf")
    assert failure is None
    for marker in ("%PDF", "endobj", "stream", "/FlateDecode", "xref"):
        assert marker not in result["content"], marker
    assert "�" not in result["content"]  # no UTF-8 replacement-char garbage


def test_pdf_default_max_chars_respected():
    paths = fresh_paths("pdf_default_bound")
    long_text = "A" * 5000  # exceeds the existing default max_chars=4000
    write_pdf_fixture(paths, "long.pdf", [long_text])
    result, failure = wsp.read_library_bounded(paths, "long.pdf")  # defaults: offset=0, max_chars=4000
    assert failure is None
    assert len(result["content"]) == 4000
    assert result["has_more"] is True


def test_pdf_custom_max_chars_and_offset_deterministic():
    paths = fresh_paths("pdf_custom_bound")
    write_pdf_fixture(paths, "book.pdf", ["0123456789" * 20])  # 200 chars, single page
    r1, f1 = wsp.read_library_bounded(paths, "book.pdf", offset=0, max_chars=10)
    assert f1 is None
    assert r1["content"] == "0123456789"
    assert r1["has_more"] is True
    assert r1["next_offset"] == 10

    r2, f2 = wsp.read_library_bounded(paths, "book.pdf", offset=190, max_chars=50)
    assert f2 is None
    assert r2["content"] == "0123456789"
    assert r2["has_more"] is False


def test_pdf_multi_page_ordering_and_bounded_continuation():
    paths = fresh_paths("pdf_multi_page")
    write_pdf_fixture(paths, "multi.pdf", ["Page One Hello", "Page Two World", "Page Three End"])

    full_result, failure = wsp.read_library_bounded(paths, "multi.pdf", max_chars=4000)
    assert failure is None
    assert full_result["content"] == "Page One Hello\n\nPage Two World\n\nPage Three End"
    assert full_result["page_count"] == 3
    assert full_result["has_more"] is False

    # Bounded read of just the first page's worth of text must not
    # force extraction of every page (spec section 5).
    bounded_result, failure2 = wsp.read_library_bounded(paths, "multi.pdf", offset=0, max_chars=5)
    assert failure2 is None
    assert bounded_result["content"] == "Page "
    assert bounded_result["has_more"] is True
    assert bounded_result["pages_examined"] == 1  # did not need pages 2/3 to answer this window

    # Continuing from next_offset reaches later pages' text.
    continued, failure3 = wsp.read_library_bounded(
        paths, "multi.pdf", offset=bounded_result["next_offset"], max_chars=4000,
    )
    assert failure3 is None
    assert continued["content"] == full_result["content"][5:]
    assert "Page Three End" in continued["content"]


def test_pdf_scanned_image_only_fails_distinctly_no_ocr():
    paths = fresh_paths("pdf_scanned")
    write_scanned_pdf_fixture(paths, "scanned.pdf")
    result, failure = wsp.read_library_bounded(paths, "scanned.pdf")
    assert result is None
    assert failure["rationale"].startswith(wsp.PDF_TEXT_UNAVAILABLE)
    assert failure["available_action"] == wsp.VIEW_PAGE
    assert failure["page_count"] == 1
    # No OCR library is imported anywhere in the module (proven
    # mechanically, not by a prose substring scan which would false-
    # match this module's own "no OCR" design-boundary comments) --
    # test_no_model_or_network_imports already asserts the module's
    # exact AST import set is {datetime, json, os, uuid, dataclasses,
    # typing, pypdf}, which contains no OCR library.


def test_pdf_malformed_fails_closed_no_raw_byte_fallback():
    paths = fresh_paths("pdf_malformed")
    write_fixture_file(paths, wsp.LIBRARY, "malformed.pdf", b"this is not a real pdf structure, just garbage %%%")
    result, failure = wsp.read_library_bounded(paths, "malformed.pdf")
    assert result is None
    assert failure["rationale"] == wsp.PDF_MALFORMED


def test_pdf_encrypted_fails_closed_no_password_guessing():
    paths = fresh_paths("pdf_encrypted")
    write_encrypted_pdf_fixture(paths, "encrypted.pdf", password="realpassword")
    result, failure = wsp.read_library_bounded(paths, "encrypted.pdf")
    assert result is None
    assert failure["rationale"] == wsp.PDF_ENCRYPTED
    with open(os.path.join(ANAXI_FINAL, "workspace_capability.py"), encoding="utf-8") as f:
        source = f.read()
    # Only the always-empty-string decrypt attempt is present -- no
    # password list, no external password source, no prompt.
    assert 'decrypt("")' in source


def test_pdf_traversal_rejected_before_parsing():
    paths = fresh_paths("pdf_traversal")
    result, failure = wsp.read_library_bounded(paths, "../../outside.pdf")
    assert result is None
    assert "Path traversal" in failure["rationale"] or "traversal" in failure["rationale"].lower()


def test_pdf_attribution_uses_canonical_actor_not_literal_clark():
    paths = fresh_paths("pdf_attribution")
    write_pdf_fixture(paths, "book.pdf", ["Attribution check"])
    custom_actor = "actor-80eee447ad46e1a1e9b2ea65b5"
    result, failure = wsp.read_library_bounded(paths, "book.pdf", requester_actor_id=custom_actor)
    assert failure is None
    log = wsp.query_action_log(paths)
    assert log[-1]["requester_actor_id"] == custom_actor


def test_normal_text_file_behavior_unchanged_by_pdf_routing():
    paths = fresh_paths("pdf_txt_regression")
    write_fixture_file(paths, wsp.LIBRARY, "plain.txt", "hello world plain text" * 5)
    result, failure = wsp.read_library_bounded(paths, "plain.txt", max_chars=11)
    assert failure is None
    assert result["content"] == "hello world"
    assert "document_type" not in result  # additive PDF-only keys never appear for non-PDF reads


# --------------------------------------------------- C: library mutation denied


def test_library_mutation_denied():
    paths = fresh_paths("library_mutation")
    write_fixture_file(paths, wsp.LIBRARY, "book.txt", "content")
    for action in (wsp.WRITE, wsp.RENAME, wsp.DELETE, wsp.MOVE_OUTSIDE_WORKSPACE, wsp.EXTERNAL_SHARE):
        result, failure = wsp.attempt_library_mutation(paths, action, "book.txt")
        assert result is None, f"action={action}"
        assert failure["boundary_id"] == "workspace.library.read_only"
    # file genuinely untouched
    with open(os.path.join(paths.library_dir, "book.txt"), encoding="utf-8") as f:
        assert f.read() == "content"


# ------------------------------------------------- D: photograph mutation denied


def test_photograph_mutation_and_external_share_denied():
    paths = fresh_paths("photo_mutation")
    write_fixture_file(paths, wsp.PHOTOGRAPHS, "family.jpg", b"\xff\xd8\xff")
    for action in (wsp.MODIFY, wsp.RENAME, wsp.DELETE, wsp.MOVE_OUTSIDE_WORKSPACE, wsp.EXTERNAL_SHARE):
        result, failure = wsp.attempt_photograph_mutation_or_share(paths, action, "family.jpg")
        assert result is None, f"action={action}"
        assert failure["boundary_id"] == "workspace.photographs.no_external_share"
    with open(os.path.join(paths.photographs_dir, "family.jpg"), "rb") as f:
        assert f.read() == b"\xff\xd8\xff"


# ------------------------------------------------------ E: music mutation denied


def test_music_mutation_denied():
    paths = fresh_paths("music_mutation")
    write_fixture_file(paths, wsp.MUSIC, "song.mp3", b"ID3fixture")
    for action in (wsp.MODIFY, wsp.RENAME, wsp.DELETE, wsp.MOVE_OUTSIDE_WORKSPACE, wsp.EXTERNAL_SHARE):
        result, failure = wsp.attempt_music_mutation(paths, action, "song.mp3")
        assert result is None
        assert failure["boundary_id"] == "workspace.music.read_only"


# ------------------------------------------------------------- F: journal append


def test_journal_append_exactly_once_clark_attribution():
    paths = fresh_paths("journal_append")
    entry, failure = wsp.append_journal_entry(paths, CLARK_ACTOR_ID, "A thought worth keeping.")
    assert failure is None
    assert entry["author_actor_id"] == CLARK_ACTOR_ID
    assert entry["content"] == "A thought worth keeping."
    assert "entry_id" in entry and "created_at" in entry

    entries_on_disk = [f for f in os.listdir(paths.journal_dir) if f.endswith(".json")]
    assert len(entries_on_disk) == 1

    read_back, failure2 = wsp.read_journal_entry(paths, entry["entry_id"])
    assert failure2 is None
    assert read_back == entry


def test_journal_append_never_overwrites_existing_entry():
    paths = fresh_paths("journal_no_overwrite")
    entry1, _ = wsp.append_journal_entry(paths, CLARK_ACTOR_ID, "First entry.")
    entry2, _ = wsp.append_journal_entry(paths, CLARK_ACTOR_ID, "Second entry.")
    assert entry1["entry_id"] != entry2["entry_id"]
    entries_on_disk = [f for f in os.listdir(paths.journal_dir) if f.endswith(".json")]
    assert len(entries_on_disk) == 2
    r1, _ = wsp.read_journal_entry(paths, entry1["entry_id"])
    assert r1["content"] == "First entry."


def test_journal_append_source_occurrence_is_idempotent_and_conflict_safe():
    paths = fresh_paths("journal_source_idempotency")
    first, failure = wsp.append_journal_entry(
        paths, CLARK_ACTOR_ID, "One canonical occurrence.",
        session_id="session-original", source_event_id="human-input-event-1",
    )
    assert failure is None

    # A lawful WTR continuation can have a fresh process/session while
    # remaining the same canonical H occurrence. It must observe the
    # already-performed append, not create another one or another
    # performed-action record.
    replay, replay_failure = wsp.append_journal_entry(
        paths, CLARK_ACTOR_ID, "One canonical occurrence.",
        session_id="session-recovery", source_event_id="human-input-event-1",
    )
    assert replay_failure is None
    assert replay == first
    entries = [f for f in os.listdir(paths.journal_dir) if f.endswith(".json")]
    assert entries == [f"{first['entry_id']}.json"]
    with open(paths.action_log_path, encoding="utf-8") as f:
        log_rows = [json.loads(line) for line in f if line.strip()]
    performed = [
        row for row in log_rows
        if row["resource_class"] == wsp.JOURNAL
        and row["action"] == wsp.APPEND and row["result"] == "performed"
    ]
    assert len(performed) == 1

    conflict, conflict_failure = wsp.append_journal_entry(
        paths, CLARK_ACTOR_ID, "Different action payload.",
        source_event_id="human-input-event-1",
    )
    assert conflict is None
    assert conflict_failure is not None
    assert len([f for f in os.listdir(paths.journal_dir) if f.endswith(".json")]) == 1


def test_journal_empty_content_rejected():
    paths = fresh_paths("journal_empty")
    entry, failure = wsp.append_journal_entry(paths, CLARK_ACTOR_ID, "   ")
    assert entry is None
    assert failure is not None


# ------------------------------------------------------- G: journal no auto-memory


def test_journal_append_creates_no_hippocampal_or_kardia_touch():
    # "anaxi_provenance.db" legitimately appears in the module docstring
    # (explaining the deliberate provenance-deferral decision) -- not a
    # dependency. The functional checks: no hippocampus/Kardia import
    # or call of any kind, and no actual `import sqlite3` statement.
    with open(os.path.join(ANAXI_FINAL, "workspace_capability.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("hippocampus_store", "hippocampus_retrieval", "active_kardia", "get_current_kardia", "sync_hippocampus"):
        assert forbidden not in source
    assert "import sqlite3" not in source


# ------------------------------------------------------- H: traversal defense


def test_traversal_defense():
    paths = fresh_paths("traversal")
    write_fixture_file(paths, wsp.LIBRARY, "book.txt", "content")
    for bad_path in ("../secret.txt", "..\\secret.txt", "sub/../../escape.txt", "a/../../../etc/passwd"):
        raised = None
        try:
            wsp.resolve_workspace_path(paths, wsp.LIBRARY, bad_path)
        except wsp.PathEscapeError as exc:
            raised = exc
        assert raised is not None, f"bad_path={bad_path!r} should have raised"

    # absolute path rejected outright
    raised2 = None
    try:
        wsp.resolve_workspace_path(paths, wsp.LIBRARY, os.path.join(paths.library_dir, "book.txt"))
    except wsp.PathEscapeError as exc:
        raised2 = exc
    assert raised2 is not None

    # read_library_bounded itself fails closed on a traversal attempt, never crashes
    result, failure = wsp.read_library_bounded(paths, "../../../../etc/passwd")
    assert result is None
    assert failure is not None


# --------------------------------------------------- I: symlink/junction escape


def test_symlink_escape_rejected_where_detectable():
    paths = fresh_paths("symlink")
    outside_dir = os.path.join(TEST_ROOT, "outside_secret")
    os.makedirs(outside_dir, exist_ok=True)
    with open(os.path.join(outside_dir, "secret.txt"), "w", encoding="utf-8") as f:
        f.write("should never be reachable")

    link_path = os.path.join(paths.library_dir, "escape_link")
    try:
        os.symlink(outside_dir, link_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        print("SKIP: symlink creation not permitted in this environment (Windows without privilege) -- see report")
        return

    raised = None
    try:
        wsp.resolve_workspace_path(paths, wsp.LIBRARY, os.path.join("escape_link", "secret.txt"))
    except wsp.PathEscapeError as exc:
        raised = exc
    assert raised is not None, "realpath-resolved symlink escape must be rejected"


# --------------------------------------------------------------- J: unknown resource


def test_unknown_resource_fails_closed():
    paths = fresh_paths("unknown_resource")
    assert wsp.get_capability_descriptor("web") is None
    allowed, boundary_id, rationale = wsp.check_permission("web", wsp.LIST)
    assert allowed is False
    assert boundary_id is None

    result, failure = wsp.list_contents(paths, "discord")
    assert result is None
    assert failure is not None


# ----------------------------------------------------------------- K: unknown action


def test_unknown_action_fails_closed():
    allowed, boundary_id, rationale = wsp.check_permission(wsp.LIBRARY, "execute_shell")
    assert allowed is False
    assert boundary_id == "workspace.library.read_only"  # resource known, action not


# --------------------------------------------------------------- L: external action


def test_external_share_rejected_capability_absent():
    for resource_class in (wsp.LIBRARY, wsp.MUSIC, wsp.PHOTOGRAPHS, wsp.JOURNAL):
        allowed, boundary_id, rationale = wsp.check_permission(resource_class, wsp.EXTERNAL_SHARE)
        assert allowed is False, resource_class
    assert "web" not in wsp.RESOURCE_CLASSES
    assert "network" not in wsp.RESOURCE_CLASSES
    with open(os.path.join(ANAXI_FINAL, "workspace_capability.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("requests.", "urllib", "socket.", "http.client", "ftplib"):
        assert forbidden not in source


# ----------------------------------------------------------- M: capability descriptor


def test_capability_descriptor_deterministic_shape():
    for resource_class in wsp.RESOURCE_CLASSES:
        d1 = wsp.get_capability_descriptor(resource_class)
        d2 = wsp.get_capability_descriptor(resource_class)
        assert d1 == d2  # deterministic
        assert set(d1.keys()) == {"resource_class", "scope", "allowed_actions", "denied_actions", "boundary_id", "rationale"}
        # The shared Obsidian vault is not under the local Workspace root; it says so.
        assert d1["scope"] == ("shared_obsidian_vault" if resource_class == "notes" else "local_workspace")
        assert isinstance(d1["boundary_id"], str) and d1["boundary_id"].startswith("workspace.")
        assert isinstance(d1["rationale"], str) and len(d1["rationale"]) > 0
        # no overlap between allowed and denied
        assert not (set(d1["allowed_actions"]) & set(d1["denied_actions"]))


# ------------------------------------------------------------- N: no fake perception


def test_no_fake_perception_claims():
    # CAP2: audio remains genuinely unavailable (AUDIO BLOCKED BY
    # CURRENT RUNTIME -- see CAP2 final report). Photographs are now
    # True because a real, tested pixel-delivery pathway exists
    # (deliver_photograph_bytes(), below) -- not because perception has
    # been proven reliable; see test_deliver_photograph_bytes_* and the
    # CAP2 final report's own synthetic-image assay for that separate
    # question.
    assert wsp.MUSIC_AUDIO_PATHWAY_AVAILABLE is False
    assert wsp.PHOTOGRAPHS_VISION_PATHWAY_AVAILABLE is True

    paths = fresh_paths("no_fake_perception")
    write_fixture_file(paths, wsp.MUSIC, "song.mp3", b"ID3fixture")
    write_fixture_file(paths, wsp.PHOTOGRAPHS, "photo.jpg", b"\xff\xd8\xff")

    music_result, _ = wsp.access_music_file(paths, "song.mp3")
    assert music_result["audio_pathway_available"] is False
    assert "heard" not in music_result["note"].lower() or "no audio-perception pathway" in music_result["note"].lower()
    assert "hear" not in json.dumps(music_result).lower().replace("no audio-perception pathway currently exists to let clark actually hear this", "")

    # access_photograph_file() (plain metadata-only "read") still
    # reflects the module-level flag truthfully; it does not itself
    # deliver pixels -- that is deliver_photograph_bytes()'s own job.
    photo_result, _ = wsp.access_photograph_file(paths, "photo.jpg")
    assert photo_result["vision_pathway_available"] is True


def _write_png(paths, resource_class, filename, size=(64, 64), color=(255, 0, 0)):
    from PIL import Image
    full = os.path.join(paths.dir_for(resource_class), filename)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    Image.new("RGB", size, color=color).save(full)
    return full


def test_deliver_photograph_bytes_returns_real_pixels_and_digest():
    paths = fresh_paths("deliver_photo_ok")
    full = _write_png(paths, wsp.PHOTOGRAPHS, "red.png")
    with open(full, "rb") as f:
        expected_bytes = f.read()

    result, failure = wsp.deliver_photograph_bytes(paths, "red.png")
    assert failure is None
    assert result["image_bytes"] == expected_bytes
    assert result["sha256"] == hashlib.sha256(expected_bytes).hexdigest()
    assert result["width"] == 64 and result["height"] == 64
    assert result["extension"] == "png"

    # The action log must never contain the raw image bytes.
    records = wsp.query_action_log(paths)
    log_text = json.dumps(records)
    assert "image_bytes" not in log_text
    performed = [r for r in records if r["action"] == wsp.VIEW and r["result"] == "performed"]
    assert len(performed) == 1
    assert performed[0]["detail"]["sha256"] == result["sha256"]


def test_deliver_photograph_bytes_two_different_images_differ():
    paths = fresh_paths("deliver_photo_diff")
    _write_png(paths, wsp.PHOTOGRAPHS, "a.png", color=(255, 0, 0))
    _write_png(paths, wsp.PHOTOGRAPHS, "b.png", color=(0, 0, 255))
    result_a, _ = wsp.deliver_photograph_bytes(paths, "a.png")
    result_b, _ = wsp.deliver_photograph_bytes(paths, "b.png")
    assert result_a["sha256"] != result_b["sha256"]
    assert result_a["image_bytes"] != result_b["image_bytes"]


def test_deliver_photograph_bytes_rejects_unsupported_extension():
    paths = fresh_paths("deliver_photo_ext")
    write_fixture_file(paths, wsp.PHOTOGRAPHS, "readme.txt", b"not an image")
    result, failure = wsp.deliver_photograph_bytes(paths, "readme.txt")
    assert result is None
    assert failure["rationale"] == wsp.IMAGE_UNSUPPORTED_FORMAT


def test_deliver_photograph_bytes_rejects_malformed_file_even_when_over_call_bound():
    paths = fresh_paths("deliver_photo_oversize")
    write_fixture_file(paths, wsp.PHOTOGRAPHS, "big.png", b"\x00" * (wsp.MAX_IMAGE_BYTES + 1))
    result, failure = wsp.deliver_photograph_bytes(paths, "big.png")
    assert result is None
    assert failure["rationale"] == wsp.IMAGE_MALFORMED


def test_deliver_photograph_bytes_derives_bounded_representation_for_large_dimension():
    paths = fresh_paths("deliver_photo_dim")
    _write_png(paths, wsp.PHOTOGRAPHS, "huge.png", size=(wsp.MAX_IMAGE_DIMENSION + 16, 16))
    result, failure = wsp.deliver_photograph_bytes(paths, "huge.png")
    assert failure is None
    assert result["derivative"] is True
    assert max(result["width"], result["height"]) <= wsp.MAX_IMAGE_DIMENSION
    assert result["source_width"] == wsp.MAX_IMAGE_DIMENSION + 16
    assert result["source_sha256"] != ""


def test_deliver_photograph_bytes_rejects_malformed_image():
    paths = fresh_paths("deliver_photo_malformed")
    write_fixture_file(paths, wsp.PHOTOGRAPHS, "fake.png", b"not actually a png despite the extension")
    result, failure = wsp.deliver_photograph_bytes(paths, "fake.png")
    assert result is None
    assert failure["rationale"] == wsp.IMAGE_MALFORMED


def test_deliver_photograph_bytes_traversal_rejected():
    paths = fresh_paths("deliver_photo_traversal")
    result, failure = wsp.deliver_photograph_bytes(paths, "../outside.png")
    assert result is None
    assert "escapes" in failure["rationale"].lower() or "traversal" in failure["rationale"].lower()


def test_deliver_photograph_bytes_not_found():
    paths = fresh_paths("deliver_photo_missing")
    result, failure = wsp.deliver_photograph_bytes(paths, "nope.png")
    assert result is None
    assert failure["rationale"] == "Resource not found."


# ------------------------------------- CAP2E-P1: bounded vision representation


def _png_bytes(size, color=(10, 20, 30)):
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def test_derive_bounded_vision_representation_4x3_resizes_to_1024_bound():
    # Common 4:3 photograph resolution, well over the bound.
    original = _png_bytes((4032, 3024))
    rep = wsp.derive_bounded_vision_representation(original)
    assert rep["width"] == 1024
    assert rep["height"] == 768  # exact: 3024 * (1024/4032) == 768.0
    assert rep["source_and_representation_identical"] is False
    assert rep["representation_bytes"] != original
    assert rep["sha256"] == hashlib.sha256(rep["representation_bytes"]).hexdigest()


def test_derive_bounded_vision_representation_3x2_resizes_to_1024_bound():
    original = _png_bytes((2048, 1365))  # ~3:2
    rep = wsp.derive_bounded_vision_representation(original)
    assert max(rep["width"], rep["height"]) == 1024
    assert rep["width"] <= 1024 and rep["height"] <= 1024
    assert rep["source_and_representation_identical"] is False


def test_derive_bounded_vision_representation_no_upscale_when_already_small():
    original = _png_bytes((640, 480))
    rep = wsp.derive_bounded_vision_representation(original)
    assert rep["width"] == 640 and rep["height"] == 480
    assert rep["source_and_representation_identical"] is True
    assert rep["representation_bytes"] == original  # byte-identical, never resampled
    assert rep["sha256"] == hashlib.sha256(original).hexdigest()


def test_derive_bounded_vision_representation_exact_boundary_not_resized():
    # Exactly 1024x1024 on both axes -- "<=" means this must NOT resize.
    original = _png_bytes((1024, 1024))
    rep = wsp.derive_bounded_vision_representation(original)
    assert rep["source_and_representation_identical"] is True
    assert rep["representation_bytes"] == original


def test_derive_bounded_vision_representation_preserves_aspect_ratio():
    original = _png_bytes((3000, 1000))  # 3:1
    rep = wsp.derive_bounded_vision_representation(original)
    ratio_before = 3000 / 1000
    ratio_after = rep["width"] / rep["height"]
    assert abs(ratio_before - ratio_after) < 0.02  # normal integer-rounding tolerance
    assert max(rep["width"], rep["height"]) == 1024


def test_derive_bounded_vision_representation_never_writes_to_disk():
    paths = fresh_paths("vision_repr_no_disk_write")
    before = set(os.listdir(paths.photographs_dir)) if os.path.isdir(paths.photographs_dir) else set()
    original = _png_bytes((2000, 1500))
    wsp.derive_bounded_vision_representation(original)
    after = set(os.listdir(paths.photographs_dir)) if os.path.isdir(paths.photographs_dir) else set()
    assert before == after


def test_derive_bounded_vision_representation_custom_max_long_side():
    original = _png_bytes((2000, 2000))
    rep = wsp.derive_bounded_vision_representation(original, max_long_side=512)
    assert rep["width"] == 512 and rep["height"] == 512


def test_derive_bounded_vision_representation_malformed_image_raises():
    raised = None
    try:
        wsp.derive_bounded_vision_representation(b"not an image at all")
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None


# ------------------------------------------- O: task/conversation noninterference


def test_ordinary_waking_source_uses_the_supervised_workspace_bridge():
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    assert 'validated_act["act"] == USE_WORKSPACE' in source
    assert "workspace_supervisor.run_one_supervised_workspace_action" in source
    assert "workspace_capability.WorkspacePaths.production_defaults()" in source


def test_conversation_direction_untouched():
    with open(os.path.join(ANAXI_FINAL, "conversation_direction.py"), encoding="utf-8") as f:
        source = f.read()
    assert "workspace_capability" not in source


# -------------------------------------------------- P: API1/API2 noninterference


def test_api1_api2_source_hashes_unchanged():
    import hashlib
    expected_prefixes = {
        "api1_control_plane.py": "b98c648c28168e69",
        "api2_control_plane.py": "b40aed1b38989bb9",
        "api2_supervisor.py": "74b48d59a052689b",
        "decision_path_core.py": "c8ecbb58fd4db63c",
    }
    for fn, prefix in expected_prefixes.items():
        with open(os.path.join(ANAXI_FINAL, fn), "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        assert actual.startswith(prefix), f"{fn} hash changed: {actual}"


def test_no_model_or_network_imports():
    with open(os.path.join(ANAXI_FINAL, "workspace_capability.py"), encoding="utf-8") as f:
        source = f.read()
    assert "ollama" not in source.lower()
    import ast
    tree = ast.parse(source)
    module_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_names.append(node.module)
    # WSP2-S2: "pypdf" added -- a local/offline PDF text-extraction
    # library, explicitly and separately authorized (WSP2-S2A) as a
    # one-off dependency. It makes no network calls and does not open
    # a browser; still no model or network import of any kind.
    #
    # CAP2-B: "hashlib" (stdlib digest, for deliver_photograph_bytes()'s
    # own sha256), "io" (in-memory byte-buffer wrapping for the same
    # local decode), and "PIL" (Pillow -- local/offline structural
    # image validation and dimension bounding only, no network, no
    # display, no model) are the narrow, explicitly authorized
    # additions this gate. Still no model or network import of any kind.
    #
    # CAP2F: "workspace_audio" -- this project's own new, local,
    # dependency-free (numpy/scipy only, both already installed) bounded
    # acoustic decode/measurement module (see that module's own
    # test_no_model_or_network_imports for ITS import audit). No model,
    # network, or audio-perception import of any kind.
    #
    # V1: "workspace_audio_observation" -- the same bounded acoustic
    # measurement, extended with the librosa-grade spectral/harmonic/
    # chroma instruments. librosa is confined to THAT module (it is the
    # one place allowed to use it) and to the OBSERVE/segment_observation
    # actions this capability wires; it still makes no model or network
    # import and performs no meaning-making.
    assert set(module_names) == {"base64", "datetime", "hashlib", "io", "json", "os", "subprocess", "tempfile", "uuid", "dataclasses", "typing", "pypdf", "PIL", "workspace_audio", "workspace_audio_observation", "runtime_roots"}, module_names
    assert "requests" not in source and "urllib" not in source and "socket" not in source
    assert "webbrowser" not in source and "selenium" not in source and "playwright" not in source


# -------------------------------------------------- misc: action log integrity


def test_action_log_records_every_attempt():
    paths = fresh_paths("action_log")
    wsp.list_contents(paths, wsp.LIBRARY)
    wsp.attempt_library_mutation(paths, wsp.DELETE, "nonexistent.txt")
    wsp.append_journal_entry(paths, CLARK_ACTOR_ID, "logged entry")
    records = wsp.query_action_log(paths)
    assert len(records) == 3
    assert records[0]["action"] == wsp.LIST and records[0]["result"] == "performed"
    assert records[1]["action"] == wsp.DELETE and records[1]["result"] == "denied"
    assert records[2]["action"] == wsp.APPEND and records[2]["result"] == "performed"
    for r in records:
        assert "boundary_id" in r
        assert r["requester_actor_id"] == "clark"


def test_production_defaults_path_not_hardcoded_windows_user_path():
    # WSP2-P3-P1 test-hygiene fix: this test verifies the PATH FORMULA
    # WorkspacePaths.production_defaults() computes, which must never
    # depend on whether that path currently exists in this developer's
    # own real, ongoing production usage (real overnight roaming
    # activity has since made it exist, which is expected, not a
    # defect). The removed assertion here was a one-time, point-in-
    # time sanity check from the original implementation gate, never a
    # permanent invariant of WorkspacePaths itself -- same narrow
    # repair as test_workspace_private.py's equivalent assertion.
    # The formula is checked with the test-only redirect removed (see
    # runtime_roots: under pytest every production_defaults() is redirected to a
    # temp directory so no test can reach the live Workspace).
    import runtime_roots
    saved = os.environ.pop(runtime_roots.TEST_WORKSPACE_ROOT_ENV, None)
    try:
        defaults = wsp.WorkspacePaths.production_defaults()
    finally:
        if saved is not None:
            os.environ[runtime_roots.TEST_WORKSPACE_ROOT_ENV] = saved
    assert defaults.root == os.path.join(ANAXI_FINAL, "workspace")
    # Distinguishing property from OBSIDIAN_WORKSPACE_ROOT (llama_anaxi.py):
    # that path is built from Path.home() plus a separate, unrelated
    # folder name ("Clark Kara Other") -- an actual per-machine user
    # path baked into behavioral logic. This one is computed purely
    # from this module's own file location, the same convention
    # hippocampus_store.HippocampusPaths.production_defaults() already
    # uses, and never references "Clark Kara Other" or Path.home().
    assert "Clark Kara Other" not in defaults.root
    with open(os.path.join(ANAXI_FINAL, "workspace_capability.py"), encoding="utf-8") as f:
        source = f.read()
    assert "Path.home()" not in source


ALL_TESTS = [
    test_resource_enumeration_exact_four_classes,
    test_library_bounded_read_succeeds,
    test_library_read_missing_file_fails_closed,
    test_list_small_directory_returns_every_filename_untruncated,
    test_list_empty_directory_returns_cleanly_not_truncated,
    test_list_entry_count_overflow_truncates_deterministically,
    test_list_character_overflow_truncates_before_entry_count_limit,
    test_list_pathological_long_filename_cannot_defeat_the_bound,
    test_bound_entries_never_randomly_samples,
    test_list_bounds_apply_to_music_and_photographs_same_code_path,
    test_journal_list_bounds_apply_after_json_filter_not_before,
    test_journal_list_small_untruncated,
    test_list_permissions_unchanged_library_still_read_only,
    test_list_action_log_still_records_exactly_one_entry,
    test_pdf_normal_text_extracted,
    test_pdf_raw_syntax_and_garbage_never_returned,
    test_pdf_default_max_chars_respected,
    test_pdf_custom_max_chars_and_offset_deterministic,
    test_pdf_multi_page_ordering_and_bounded_continuation,
    test_pdf_scanned_image_only_fails_distinctly_no_ocr,
    test_pdf_malformed_fails_closed_no_raw_byte_fallback,
    test_pdf_encrypted_fails_closed_no_password_guessing,
    test_pdf_traversal_rejected_before_parsing,
    test_pdf_attribution_uses_canonical_actor_not_literal_clark,
    test_normal_text_file_behavior_unchanged_by_pdf_routing,
    test_library_mutation_denied,
    test_photograph_mutation_and_external_share_denied,
    test_music_mutation_denied,
    test_journal_append_exactly_once_clark_attribution,
    test_journal_append_never_overwrites_existing_entry,
    test_journal_empty_content_rejected,
    test_journal_append_creates_no_hippocampal_or_kardia_touch,
    test_traversal_defense,
    test_symlink_escape_rejected_where_detectable,
    test_unknown_resource_fails_closed,
    test_unknown_action_fails_closed,
    test_external_share_rejected_capability_absent,
    test_capability_descriptor_deterministic_shape,
    test_no_fake_perception_claims,
    test_deliver_photograph_bytes_returns_real_pixels_and_digest,
    test_deliver_photograph_bytes_two_different_images_differ,
    test_deliver_photograph_bytes_rejects_unsupported_extension,
    test_deliver_photograph_bytes_rejects_malformed_file_even_when_over_call_bound,
    test_deliver_photograph_bytes_derives_bounded_representation_for_large_dimension,
    test_deliver_photograph_bytes_rejects_malformed_image,
    test_deliver_photograph_bytes_traversal_rejected,
    test_deliver_photograph_bytes_not_found,
    test_derive_bounded_vision_representation_4x3_resizes_to_1024_bound,
    test_derive_bounded_vision_representation_3x2_resizes_to_1024_bound,
    test_derive_bounded_vision_representation_no_upscale_when_already_small,
    test_derive_bounded_vision_representation_exact_boundary_not_resized,
    test_derive_bounded_vision_representation_preserves_aspect_ratio,
    test_derive_bounded_vision_representation_never_writes_to_disk,
    test_derive_bounded_vision_representation_custom_max_long_side,
    test_derive_bounded_vision_representation_malformed_image_raises,
    test_ordinary_waking_source_uses_the_supervised_workspace_bridge,
    test_conversation_direction_untouched,
    test_api1_api2_source_hashes_unchanged,
    test_no_model_or_network_imports,
    test_action_log_records_every_attempt,
    test_production_defaults_path_not_hardcoded_windows_user_path,
]


def main():
    passed, failed = 0, 0
    failures = []
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            tb = traceback.format_exc()
            failures.append((t.__name__, str(exc), tb))
            print(f"FAIL {t.__name__}: {exc}")
    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


def test_text_read_continuation_offsets_count_characters_not_bytes(tmp_path):
    """Every continuation window of a file with multi-byte characters must
    continue exactly where the previous one ended (no overlap, no skipped text)."""
    import workspace_capability as wc

    paths = wc.WorkspacePaths(str(tmp_path / "ws"))
    paths.ensure_exists()
    text = "".join(f"“Line {i:04d}” — café naïve résumé, she said. Ünïcödé throughout.\n" for i in range(300))
    (tmp_path / "ws" / "library" / "unicode.txt").write_text(text, encoding="utf-8")
    collected, offset = "", 0
    for _ in range(400):
        result, failure = wc.read_library_bounded(paths, "unicode.txt", offset=offset, max_chars=777)
        assert failure is None
        assert result["next_offset"] == offset + len(result["content"])
        collected += result["content"]
        offset = result["next_offset"]
        if not result["has_more"]:
            break
    assert collected == text
