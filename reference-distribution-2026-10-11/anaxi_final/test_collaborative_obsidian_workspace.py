"""The shared Obsidian vault as a collaborative workspace Clark reaches through his ordinary interface.

Real chain where it matters: ordinary waking Pass 1 (use_workspace) -> WSP1 -> the notes dispatcher ->
obsidian_workspace -> Pass 2 -> canonical X, in isolated state.  Only the model is faked.

Countermodels named per claim:
  * "authorship by provenance" would also pass if the host trusted the author line -> a note that SAYS
    author: clark without a canonical record is reported unconfirmed, and a confirmed note whose body
    changed is reported edited; a note in a folder called "Clark" with no author has no author.
  * "create-only" would also pass if nothing were ever written -> the lawful create writes exactly one
    new file with the provenance frontmatter, AND a colliding name leaves the existing note byte-identical.
  * "absent, not empty" would also pass if the vault were silently listed as empty -> with no vault the
    collection is missing from the surface, the task text and the menu.
"""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

import clark_journal
import conversation_direction as cd
import obsidian_workspace as ow
import runtime_roots
import workspace_capability as wc
import workspace_direction as wd
import workspace_roaming
from test_workspace_target_discovery import _pass2_text, _script
from test_wsp1_production_hard_floor import PRODUCTION_WSP1_PASS1_BUDGET, _build, _message

ALEX_NOTE = "---\nauthor: Alex\ncreated_at: 2026-09-01\n---\n\nBuy lemons, soil for the lemon tree, and a new pot.\n"


def _vault(h, tmp_path):
    vault = tmp_path / "vault"
    (vault / "Shopping").mkdir(parents=True)
    (vault / "Shopping" / "Grocery list.md").write_text(ALEX_NOTE, encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "workspace.md").write_text("internal", encoding="utf-8")
    h.paths.notes_dir = str(vault)
    return vault


def _bound(h):
    from test_web_waking_seam import _bind_human
    return _bind_human(h)


def _turn(h, authority, text="Could you look at our shared notes?"):
    return h.la.run_waking_turn(h.la.AnaxiOrchestrator(), text, interaction_mode="conversation",
                                human_input_authority=authority)


def NOTES(action, relative_path="", content=""):
    return {"resource_class": "notes", "action": action, "relative_path": relative_path, "content": content}


# ------------------------------------------------------------------ through the ordinary interface

def test_clark_reads_a_shared_note_by_its_title_with_its_authorship_through_ordinary_waking(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=NOTES("read", "Shopping/Grocery list"))
    _vault(h, tmp_path)
    result = _turn(h, _bound(h))
    text = _pass2_text(h)
    assert result["boundary_result"]["scope"] == "local_workspace/notes"
    assert "Buy lemons, soil for the lemon tree" in text
    assert "attributed to Alex (the note's own claim; ANAXI has not verified it)" in text
    first = "\n".join(m["content"] for m in h.workspace_calls()[0]["messages"])
    assert "notes=shared Obsidian notes" in first and "'notes'" in first          # offered, described


def test_discovery_lists_the_vault_without_its_internal_store_and_the_subject_chooses(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=NOTES("read"))
    _vault(h, tmp_path)
    _script(h, [NOTES("read"), NOTES("read", "Shopping/Grocery list.md")])
    _turn(h, _bound(h))
    selection = "\n".join(m["content"] for m in h.workspace_calls()[1]["messages"])
    assert "Shopping/Grocery list.md" in selection and "author: Alex" in selection
    assert ".obsidian" not in selection
    assert "Buy lemons" in _pass2_text(h)


def test_clark_creates_a_note_of_his_own_bound_to_the_canonical_message(monkeypatch, tmp_path):
    body = "Lemon tree: check the curling leaf again in three days."
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True,
               action=NOTES("append", "Lemon tree follow-up", body))
    vault = _vault(h, tmp_path)
    result = _turn(h, _bound(h), "Would you jot a note for us about the lemon tree?")
    created = vault / "Lemon tree follow-up.md"
    fm, text, _ = ow._split_frontmatter(created.read_text(encoding="utf-8"))
    assert fm["author"] == "clark" and fm["provenance"] == "clark_authored_via_anaxi"
    assert fm["content_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    h_id = fm["source_event_id"]
    conn = sqlite3.connect(h.db_path)
    try:
        assert conn.execute("SELECT event_type FROM events WHERE event_id=?", (h_id,)).fetchone()[0] == "human_waking_input"
    finally:
        conn.close()
    assert result["boundary_result"]["consequence"].startswith("New note created in the shared vault")
    # read back through the same interface: ANAXI confirms it is his, unchanged
    _script(h, [NOTES("read", "Lemon tree follow-up")])
    _turn(h, _bound(h), "What did you write about the lemon tree?")
    assert f"yours: written by you through ANAXI (canonical record {h_id}); unchanged since" in _pass2_text(h)


def test_a_colliding_name_overwrites_nothing_and_is_told_truthfully(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True,
               action=NOTES("append", "Shopping/Grocery list", "my version"))
    vault = _vault(h, tmp_path)
    before = (vault / "Shopping" / "Grocery list.md").read_bytes()
    result = _turn(h, _bound(h))
    assert (vault / "Shopping" / "Grocery list.md").read_bytes() == before
    assert "VAULT_NOTE_EXISTS" in result["boundary_result"]["rationale"]
    assert "nothing was overwritten" in _pass2_text(h)


def test_the_production_sized_turn_still_fits_with_the_vault_offered(monkeypatch, tmp_path):
    """Stated plainly: on the byte estimator alone (1 byte = 1 token, no real tokenizer) the 670-byte
    turn had 30 bytes of slack and the vault's 110 bytes exceed it.  The real path never stops there:
    a would-overflow composition is re-costed by real tokenizer measurement (recost_when_shedding),
    which the realistic probe models here -- and then it fits with room to spare."""
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    _vault(h, tmp_path)
    for size in (456, 670):
        h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(size), interaction_mode="conversation")
        pass1 = h.budget_results["wsp1_pass1"]
        assert pass1.fits and pass1.final_prompt_cost < PRODUCTION_WSP1_PASS1_BUDGET


# ------------------------------------------------------------------ authorship by provenance

def _ws(tmp_path):
    root = tmp_path / "v"
    root.mkdir(exist_ok=True)
    return ow.ObsidianWorkspace(str(root), private_space_root=str(tmp_path / "private"))


def _read(ws, name, lookup):
    result, failure = ws.read_note(name, event_lookup=lookup)
    assert failure is None, failure
    return result["authorship"]


@pytest.mark.parametrize("frontmatter, expected", [
    ("author: Alex\n", ow.ATTRIBUTED),
    ("author: Alex, Clark\n", ow.ATTRIBUTED_SHARED),
    ("author: host\n", ow.MARKED_HOST),
    ("created_at: 2026-01-01\n", ow.NO_AUTHOR),
    ("author: clark\n", ow.CLARK_UNCONFIRMED),                               # a claim with no record
    ("author: clark\nevent_id: NOT-A-REAL-EVENT\ncontent_sha256: x\n", ow.CLARK_UNCONFIRMED),
], ids=["attributed", "shared", "host", "no_author", "clark_claim_only", "clark_unknown_event"])
def test_authorship_is_decided_by_provenance_never_by_the_author_line_alone(tmp_path, frontmatter, expected):
    ws = _ws(tmp_path)
    Path(ws.vault_root, "n.md").write_text(f"---\n{frontmatter}---\n\nbody", encoding="utf-8")
    assert _read(ws, "n.md", lambda e: e == "REAL-EVENT") == expected


def test_location_never_decides_authorship(tmp_path):
    ws = _ws(tmp_path)
    os.makedirs(os.path.join(ws.vault_root, "Clark"))
    Path(ws.vault_root, "Clark", "thoughts.md").write_text("no frontmatter at all", encoding="utf-8")
    assert _read(ws, "Clark/thoughts.md", lambda e: True) == ow.NO_AUTHOR


def test_a_confirmed_note_edited_since_is_reported_as_edited(tmp_path):
    ws = _ws(tmp_path)
    result, failure = ow.create_note(ws, "mine", "original words", clark_actor_id="actor-clark", source_event_id="REAL-EVENT")
    assert failure is None and result["created"]
    assert _read(ws, "mine.md", lambda e: e == "REAL-EVENT") == ow.VERIFIED_CLARK
    path = Path(ws.vault_root, "mine.md")
    path.write_text(path.read_text(encoding="utf-8").replace("original words", "changed words"), encoding="utf-8")
    assert _read(ws, "mine.md", lambda e: e == "REAL-EVENT") == ow.VERIFIED_CLARK_EDITED
    assert _read(ws, "mine.md", None) == ow.CLARK_UNCONFIRMED                 # no database: nothing confirmed


def test_an_exact_replay_creates_nothing_new_and_a_conflicting_one_writes_nothing(tmp_path):
    ws = _ws(tmp_path)
    first, _ = ow.create_note(ws, "", "same words", clark_actor_id="a", source_event_id="H-1")
    again, failure = ow.create_note(ws, "", "same words", clark_actor_id="a", source_event_id="H-1")
    assert failure is None and again["replayed"] and again["relative_path"] == first["relative_path"]
    assert len(list(Path(ws.vault_root).glob("*.md"))) == 1
    _none, failure = ow.create_note(ws, first["relative_path"], "different", clark_actor_id="a", source_event_id="H-1")
    assert failure["code"] == ow.VAULT_REPLAY_CONFLICT


# ------------------------------------------------------------------ boundaries

def test_private_space_traversal_and_internal_store_are_refused(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.md").write_text("never", encoding="utf-8")
    ws = _ws(tmp_path)
    os.symlink(private / "secret.md", Path(ws.vault_root, "link.md"))
    assert ws.read_note("link.md")[1]["code"] == ow.VAULT_PRIVATE_SPACE_REFUSED
    assert ws.read_note("../private/secret.md")[1]["code"] == ow.VAULT_TARGET_OUTSIDE_ROOT
    assert ws.read_note(".obsidian/x.md")[1]["code"] == ow.VAULT_TARGET_IS_OBSIDIAN_INTERNAL
    assert ow.create_note(ws, "../escape", "x", clark_actor_id="a")[1]["code"] == ow.VAULT_TARGET_OUTSIDE_ROOT
    assert ow.create_note(ws, ".obsidian/plugin", "x", clark_actor_id="a")[1]["code"] == ow.VAULT_TARGET_IS_OBSIDIAN_INTERNAL
    with pytest.raises(ow.ObsidianWorkspaceConfigurationError):
        ow.ObsidianWorkspace(str(private), private_space_root=str(private))


def test_the_vault_is_never_reached_unattended():
    assert wc.NOTES not in workspace_roaming.ROAMING_ALLOWED_SURFACE
    assert wc.NOTES in wd.LIVE_ALLOWED_SURFACE


def test_an_unavailable_vault_is_absent_everywhere_never_an_empty_collection(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "ws"))
    assert paths.notes_dir is None and not paths.notes_available()
    assert wc.NOTES not in wd.resource_allowed_surface(paths)
    assert "notes=" not in wd.build_pass1_interface("", [], "", wd.resource_allowed_surface(paths))["task_instruction"]
    head, fixed, tail = cd.render_pass1_action_menu_parts(workspace_available=True, notes_available=False)
    assert "Obsidian" not in tail
    head, fixed, tail = cd.render_pass1_action_menu_parts(workspace_available=True, notes_available=True)
    assert "shared Obsidian notes (read, or write a new note)" in tail
    missing = wc.WorkspacePaths(str(tmp_path / "ws"), notes_dir=str(tmp_path / "nope"))
    result, performed = wd.execute_workspace_action(missing, NOTES("list"), "actor-clark")
    assert not performed and "not available" in result["rationale"]


def test_notes_are_owner_private_only_in_family_mode():
    import llama_anaxi as la
    import family_membership as fm
    owner = {"visibility_scope": fm.SCOPE_PRINCIPAL_PRIVATE, "principal_actor_id": "o", "owner_actor_id": "o"}
    member = {"visibility_scope": fm.SCOPE_PRINCIPAL_PRIVATE, "principal_actor_id": "m", "owner_actor_id": "o"}
    shared = {"visibility_scope": fm.SCOPE_FAMILY_SHARED, "principal_actor_id": "o", "owner_actor_id": "o"}
    assert la._workspace_action_available(True, owner)
    assert not la._workspace_action_available(True, member) and not la._workspace_action_available(True, shared)


def test_tests_and_probes_can_never_reach_the_real_vault(monkeypatch, tmp_path):
    monkeypatch.setenv(runtime_roots.TEST_WORKSPACE_ROOT_ENV, str(tmp_path / "redirected"))
    monkeypatch.setenv(runtime_roots.OBSIDIAN_VAULT_ROOT_ENV, "/Volumes/Example/Real Vault")
    assert runtime_roots.obsidian_vault_root() == str(tmp_path / "redirected" / "obsidian_vault")
    assert wc.WorkspacePaths.production_defaults().notes_dir == str(tmp_path / "redirected" / "obsidian_vault")


# ------------------------------------------------------------------ Clark's artifacts in the vault

def test_artifacts_carry_canonical_identity_and_never_overwrite_a_note(tmp_path):
    root = tmp_path / "v"
    root.mkdir()
    (root / "Idea.md").write_text(ALEX_NOTE, encoding="utf-8")
    result = clark_journal.write_journal_entry(str(root), "Idea.md", "Clark's words", event_id="EV-1", pipeline_id="P-1")
    assert result["status"] == "success" and result["path"].endswith("Idea (2).md")
    assert (root / "Idea.md").read_text(encoding="utf-8") == ALEX_NOTE
    ws = ow.ObsidianWorkspace(str(root), private_space_root=str(tmp_path / "private"))
    read, _ = ws.read_note("Idea (2).md", event_lookup=lambda e: e == "EV-1")
    assert read["frontmatter"]["event_id"] == "EV-1" and read["frontmatter"]["pipeline_id"] == "P-1"
    assert read["authorship"] == ow.VERIFIED_CLARK


# ------------------------------------------------------------------ act, then stay silent (use_workspace + no_reply)

def _silent(h):
    """Clark's ordinary Pass 1 chooses use_workspace AND reply_request=no_reply."""
    inner = h.la.ollama.chat

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        out = inner(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        if isinstance(format, dict) and "act" in format.get("properties", {}) and not (options and options.get("num_predict") == 1):
            out = dict(out, message={"content": json.dumps(dict(json.loads(out["message"]["content"]), reply_request="no_reply"))})
        return out
    h.la.ollama.chat = chat


def test_a_silent_journal_write_is_performed_and_clarks_null_is_recorded_without_a_reply_pass(monkeypatch, tmp_path):
    words = "Today mattered because we took our time."
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True,
               action={"resource_class": "journal", "action": "append", "relative_path": "", "content": words})
    _silent(h)
    result = _turn(h, _bound(h), "Please write an entry in your journal about today.")
    assert result["reply"] == "" and result["reply_choice"] == "no_reply"
    (entry,) = list(Path(h.paths.journal_dir).glob("*.json"))
    assert json.loads(entry.read_text(encoding="utf-8"))["content"] == words
    offered = "\n".join(m["content"] for m in h.workspace_calls()[0]["messages"])
    assert "'journal': ['append']" in offered and "'read'" not in offered          # writes only
    assert not [c for c in h.calls if c["format"] is None and not (c["options"] or {}).get("num_predict") == 1]
    conn = sqlite3.connect(h.db_path)
    try:
        comps = dict(conn.execute("SELECT component_kind, component_text FROM event_components WHERE event_id=?",
                                  (result["native_event_id"],)).fetchall())
    finally:
        conn.close()
    assert comps.get("clark_reply_choice") == "no_reply" and not comps.get("conversational_prose")
    # Cross-law: the same shared fact (act performed, then Clark's typed null) asked of every subsystem.
    import conversation_display
    import conversation_projection as cp
    import native_provenance_writer as npw
    import wtr0_schema_migration
    import wtr0_waking_recovery as recovery
    conn = sqlite3.connect(h.db_path)
    try:
        h_id = comps["human_input_event_id"]
        notes = [r[0] for r in conn.execute(
            "SELECT participation_note FROM event_model_participation WHERE event_id=?", (result["native_event_id"],))]
    finally:
        conn.close()
    assert notes == [npw.PASS1_TYPED_CHOICE_PARTICIPATION_NOTE]             # no reply-pass model claimed
    rows = cp.project_waking_conversation(h.db_path, include_event_metadata=True)
    assert rows[-1]["reply_choice"] == "no_reply" and rows[-1]["content"] == ""
    assert conversation_display.display_messages(rows)[-1]["content"][0]["text"] == conversation_display.NO_REPLY_DISPLAY_TEXT
    wtr0_schema_migration.apply_additive_migration(h.db_path)
    conn = sqlite3.connect(h.db_path)
    try:
        receipt = recovery.assess_eligibility(conn, h_id, h.actor_id)
    finally:
        conn.close()
    assert receipt["decision"] == "INELIGIBLE" and receipt["basis"] == "X_ALREADY_LINKED"
    assert h.la.get_last_conversation_direction_trace()["pass2_status"] == "not_composed_no_reply"


def test_a_silent_turn_cannot_read_what_it_would_never_see(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)   # WSP1 answers library read
    _silent(h)
    with pytest.raises(Exception) as raised:
        _turn(h, _bound(h))
    assert "UNKNOWN_RESOURCE_CLASS" in str(raised.value)          # the library has no write: not offered at all
    assert not [r for r in wc.query_action_log(h.paths) if r["action"] == "read"]


# ------------------------------------------------------------------ live finding: a real vault must be choosable

def _big_vault(tmp_path, n=40):
    vault = tmp_path / "bigvault"
    for i in range(n):
        folder = vault / "Clark Kara Other" / f"Section {i % 5} with a fairly long descriptive folder name"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"Note {i:02d} about a topic with a long and specific title.md").write_text(
            f"---\nauthor: Alex\ncreated_at: 2026-09-{(i % 28) + 1:02d}\n---\n\nBody {i}.\n", encoding="utf-8")
    return vault


def test_a_production_sized_vault_listing_reaches_the_selection_prompt_with_real_entries(monkeypatch, tmp_path):
    """Live 2026-09-24 (production vault, 23 notes): the per-entry dict listing (~6 KB) was windowed to
    ZERO entries in the selection prompt; Clark saw an empty-looking vault, re-listed and wandered to the
    library.  Countermodel: any listing shape whose delivered window carries no note path."""
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=NOTES("read"))
    h.paths.notes_dir = str(_big_vault(tmp_path))
    import workspace_discovery
    _script(h, [NOTES("read"), NOTES(workspace_discovery.STOP_ACTION)])
    try:
        _turn(h, _bound(h))
    except Exception:
        pass                                     # the scripted stop may end the turn; the prompt is the point
    selection = "\n".join(m["content"] for m in h.workspace_calls()[1]["messages"])
    shown = selection.count("about a topic with a long and specific title.md")
    assert shown >= 8, f"only {shown} vault entries reached the selection prompt"
    assert "author: Alex" in selection


def test_a_windowed_vault_listing_continues_with_the_cursor_it_was_given(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "ws"), notes_dir=str(_big_vault(tmp_path, 12)))
    import workspace_delivery
    first, ok = wd.execute_workspace_action(paths, NOTES("list"), "actor-clark")
    assert ok and first["result"]["total_count"] == 12
    windowed, _ = workspace_delivery.fit_boundary_result(first, 250, cost_fn=lambda text: len(text) // 4)
    request = windowed["result"]["next_request"]
    assert request and windowed["result"]["returned_count"] < 12
    second, ok = wd.execute_workspace_action(paths, NOTES("list", content=request), "actor-clark")
    assert ok and second["result"]["entries"][0] == first["result"]["entries"][windowed["result"]["returned_count"]]


def test_a_long_vault_note_is_delivered_in_bounded_pieces_and_read_on_to_its_exact_end(monkeypatch, tmp_path):
    """Live 2026-09-24: a 10,027-char vault note was WITHHELD whole at delivery (no windower matched a
    vault read).  Countermodel: a read that delivers only metadata, or that cannot continue."""
    import workspace_delivery
    vault = tmp_path / "longvault"
    vault.mkdir()
    body = "".join(f"Paragraph {i:03d}: the capability pathways are described here in order. " for i in range(150))
    (vault / "Long note.md").write_text(body, encoding="utf-8")
    paths = wc.WorkspacePaths(str(tmp_path / "ws"), notes_dir=str(vault))
    collected, request = "", ""
    for _ in range(40):
        br, ok = wd.execute_workspace_action(paths, NOTES("read", "Long note", request), "actor-clark")
        assert ok
        fitted, info = workspace_delivery.fit_boundary_result(br, 900, cost_fn=lambda text: len(text) // 4)
        assert info["delivery"] in ("whole", "windowed") and fitted["result"]["content"]
        collected += fitted["result"]["content"]
        request = fitted["result"].get("next_request")
        if not request:
            break
    assert collected == body


def test_a_long_vault_note_reaches_pass2_through_ordinary_waking(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=NOTES("read", "Long note"))
    vault = tmp_path / "v2"
    vault.mkdir()
    (vault / "Long note.md").write_text("OPENING-WORDS " + "x" * 12000, encoding="utf-8")
    h.paths.notes_dir = str(vault)
    result = _turn(h, _bound(h))
    assert "OPENING-WORDS" in _pass2_text(h)
    assert "withheld" not in json.dumps(result["boundary_result"]).lower()


def test_the_next_workspace_choice_is_told_exactly_where_the_last_reading_stopped(monkeypatch, tmp_path):
    """Live 2026-09-24: 'read the next part' restarted at 0 or did nothing -- the workspace choice was never
    told where the last delivered piece ended.  The host now states item, range and the continuing request;
    the subject still chooses.  Countermodel: a second turn whose prompt lacks the continuation."""
    import workspace_supervisor
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=NOTES("read", "Folder/Long note"))
    vault = tmp_path / "v3"
    vault.mkdir()
    body = "".join(f"Sentence {i:04d} of a long shared note. " for i in range(900))
    (vault / "Folder").mkdir()
    (vault / "Folder" / "Long note.md").write_text(body, encoding="utf-8")
    h.paths.notes_dir = str(vault)
    authority = _bound(h)
    _turn(h, authority, "Please read the Long note in our shared vault.")
    session = h.la.get_current_session_id()
    reading = workspace_supervisor._open_text_read_line(session)
    assert reading and 'content "next"' in reading and "notes/Folder/Long note" in reading
    offset = json.loads(workspace_supervisor._open_text_reads[session]["next_request"])["offset"]
    assert 0 < offset < len(body)
    _script(h, [NOTES("read", "Long note.md", "next")])   # his own "next", naming the note by its file name only
    _turn(h, authority, "Please read the next part of that note.")
    prompts = ["\n".join(m["content"] for m in c["messages"]) for c in h.workspace_calls()]
    assert len(prompts) == 2                                   # no discovery: the host bound his "next"
    first_prompt = prompts[-1]
    assert "Last reading (host fact): notes/Folder/Long note" in first_prompt and "chars 0-" in first_prompt
    assert json.dumps(body[offset:offset + 80])[1:-1] in _pass2_text(h)       # the continuation was delivered


def test_the_ordinary_menu_names_an_open_reading_only_while_one_is_open():
    """Real model: without this line, 'and the next part after that' chose no workspace act 2/3 times."""
    head, fixed, tail = cd.render_pass1_action_menu_parts(workspace_available=True)
    assert "open reading" not in tail
    line = "Your open reading: notes/X.md, chars 0-3114 of 10027 delivered so far (reading on is act=use_workspace)"
    head, fixed2, tail = cd.render_pass1_action_menu_parts(workspace_available=True, open_reading=line)
    assert line in tail and fixed2 == fixed          # the calibrated fixed block is untouched


def test_a_vault_read_carries_the_note_and_only_the_facts_clark_needs(tmp_path):
    vault = tmp_path / "slim"
    vault.mkdir()
    (vault / "n.md").write_text(ALEX_NOTE, encoding="utf-8")
    paths = wc.WorkspacePaths(str(tmp_path / "ws"), notes_dir=str(vault))
    br, ok = wd.execute_workspace_action(paths, NOTES("read", "n"), "actor-clark")
    assert ok and set(br["result"]) == {"relative_path", "authorship", "frontmatter", "content", "total_chars",
                                        "next_offset", "has_more", "next_request"}
    assert br["result"]["authorship"].startswith("attributed to Alex")


def test_a_withheld_result_is_never_recorded_as_genuinely_delivered(monkeypatch, tmp_path):
    """Live record 2026-09-24 09:55: a vault read withheld at delivery was recorded genuinely_delivered=True
    with delivered_portion "journal entry, full".  Countermodel: any withheld delivery recorded as delivered."""
    import workspace_delivery
    import workspace_episode_provenance as wep
    recorded = []
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    monkeypatch.setattr(wep, "record_resource_encounter", lambda *a, **k: recorded.append(k))
    monkeypatch.setattr(workspace_delivery, "fit_boundary_result",
                        lambda br, room, **k: (dict(br, result={workspace_delivery.WITHHELD_KEY: "exceeds prompt room"}),
                                               {"delivery": "withheld"}))
    h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(200), interaction_mode="conversation")
    assert recorded and recorded[-1]["genuinely_delivered"] is False
