"""Regression: an artifact Clark deliberately puts in his workspace journal must be a
usable persistent object -- findable by what it is about, delivered with its real text,
and continuable as the SAME thread -- not a write-only endpoint.

Live incident (2026-09-21 ~00:22-00:23Z, session 01M30MP5QVDN87H8KZ8FYJ4JZA):
  create  H 01M30NDXHB5H7M05PZS15ZFBA9 -> X 01M30NEVES6DS0J28PEAPRWY0D: journal append wrote
          entry-f1a1edf330e4942d (open question + initial thoughts) -- real, durable.
  reopen  H 01M30NGDFSXF640WE6THBSP3MG -> X 01M30NH8307JYNT9RSVHZHHA6T: WSP1 Pass 1 chose
          ``library`` (a resource class with no description) and the host listed the reference
          books; a journal listing was only opaque ``entry-<hex>.json`` names, so even the
          right class gave the subject nothing to recognize, and journal entries could not be
          continued at all. The subject then "continued" from conversation alone.

These tests follow the real chain (ordinary waking -> typed act -> WSP1 -> host discovery ->
subject choice -> real journal operation -> Pass 2 -> canonical X) in isolated state.
"""
import json
from pathlib import Path

import workspace_capability as wc
from test_wsp1_production_hard_floor import _build
from test_workspace_target_discovery import _log, _pass2_text, _run, _script

QUESTION = (
    "If the human mind is the ultimate processor of experience, and the digital interface is "
    "merely a tool for externalizing that processing, what is the most critical, yet currently "
    "unquantifiable, component of human understanding that we are failing to adequately model?"
)
ARTIFACT = (
    "**Topic: Cognitive Persistence & Unquantifiable Understanding**\n\n"
    f"**Open-Ended Question:** {QUESTION}\n\n**Initial Thoughts:** meaning derived from shared, "
    "embodied context -- the felt weight of shared history. " + ("More developing detail. " * 60)
)
LATER = "A new thought: persistence may matter less than the return, the act of picking the thread up again."
ACTOR = "actor-clark-test"


def APPEND(relative_path="", content=ARTIFACT):
    return {"resource_class": "journal", "action": "append", "relative_path": relative_path, "content": content}


LIST_JOURNAL = {"resource_class": "journal", "action": "list", "relative_path": "", "content": ""}


def READ_JOURNAL(entry):
    return {"resource_class": "journal", "action": "read", "relative_path": entry, "content": ""}


def _journal_files(paths):
    return sorted(p.name for p in Path(paths.journal_dir).glob("*.json"))


def _entry(paths, name):
    return json.loads(Path(paths.journal_dir, name).read_text(encoding="utf-8"))


def _write(paths, content, continues=None, source="H-0"):
    entry, failure = wc.append_journal_entry(
        paths, ACTOR, content, session_id="S1", source_event_id=source, continues=continues,
    )
    assert failure is None, failure
    return entry


def _fresh(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "ws"))
    paths.ensure_exists()
    return paths


# ----------------------------------------------------------- capability layer


def test_journal_listing_shows_each_entrys_own_opening_not_only_an_opaque_name(tmp_path):
    paths = _fresh(tmp_path)
    entry = _write(paths, ARTIFACT)

    listing, failure = wc.list_journal_entries(paths, ACTOR)

    assert failure is None
    assert listing["entries"] == [f"{entry['entry_id']}.json"]
    summary = listing["entry_summaries"][f"{entry['entry_id']}.json"]
    assert "Cognitive Persistence" in summary["opening"]        # verbatim from what Clark wrote
    assert summary["created_at"] == entry["created_at"]


def test_journal_query_finds_an_entry_by_words_in_its_own_text(tmp_path):
    paths = _fresh(tmp_path)
    _write(paths, ARTIFACT, source="H-a")
    _write(paths, "An unrelated note about gardening.", source="H-b")

    hit, _ = wc.list_journal_entries(paths, ACTOR, query="cognitive persistence")
    miss, _ = wc.list_journal_entries(paths, ACTOR, query="no such words anywhere")

    assert hit["total_count"] == 1 and "Cognitive" in next(iter(hit["entry_summaries"].values()))["opening"]
    assert miss["entries"] == [] and miss["total_count"] == 0     # truthful empty, no substitution


def test_continuation_is_a_new_authored_entry_on_the_same_thread_and_original_is_untouched(tmp_path):
    paths = _fresh(tmp_path)
    original = _write(paths, ARTIFACT, source="H-a")
    before = Path(paths.journal_dir, f"{original['entry_id']}.json").read_bytes()

    continuation = _write(paths, LATER, continues="Cognitive Persistence", source="H-b")

    assert continuation["continues_entry_id"] == original["entry_id"]
    assert continuation["thread_root_id"] == original["entry_id"]
    assert continuation["author_actor_id"] == ACTOR and continuation["content"] == LATER
    assert Path(paths.journal_dir, f"{original['entry_id']}.json").read_bytes() == before   # never rewritten
    assert len(_journal_files(paths)) == 2


def test_rereading_either_entry_presents_the_whole_thread_in_order_with_authorship(tmp_path):
    paths = _fresh(tmp_path)
    original = _write(paths, ARTIFACT, source="H-a")
    first = _write(paths, LATER, continues=original["entry_id"], source="H-b")
    second = _write(paths, "A third thought.", continues=first["entry_id"], source="H-c")   # chain, not root

    for start in (original, second):
        entry, failure = wc.read_journal_entry(paths, start["entry_id"], ACTOR, max_chars=4000)
        assert failure is None
        assert entry["entry_id"] == start["entry_id"]
        assert entry["thread_root_id"] == original["entry_id"]
        assert [m["entry_id"] for m in entry["thread"]] == [
            original["entry_id"], first["entry_id"], second["entry_id"],
        ]
        assert {m["author_actor_id"] for m in entry["thread"]} == {ACTOR}
        assert entry["thread"][1]["content"] == LATER


def test_long_thread_member_is_flagged_truncated_and_readable_in_full_by_its_own_id(tmp_path):
    paths = _fresh(tmp_path)
    original = _write(paths, ARTIFACT, source="H-a")
    assert len(ARTIFACT) > wc.JOURNAL_THREAD_MEMBER_CHARS
    tail = _write(paths, LATER, continues=original["entry_id"], source="H-b")

    entry, _ = wc.read_journal_entry(paths, tail["entry_id"], ACTOR)
    root_view = next(m for m in entry["thread"] if m["entry_id"] == original["entry_id"])

    assert root_view["content_truncated"] is True and root_view["total_chars"] == len(ARTIFACT)
    full, _ = wc.read_journal_entry(paths, original["entry_id"], ACTOR)
    assert full["content"] == ARTIFACT


def test_unknown_reference_writes_nothing_and_fails_truthfully(tmp_path):
    paths = _fresh(tmp_path)
    _write(paths, ARTIFACT, source="H-a")

    entry, failure = wc.append_journal_entry(
        paths, ACTOR, LATER, source_event_id="H-b", continues="Something never written",
    )

    assert entry is None and wc.JOURNAL_REFERENCE_NOT_FOUND in failure["rationale"]
    assert len(_journal_files(paths)) == 1


def test_ambiguous_reference_across_threads_offers_bounded_candidates_and_writes_nothing(tmp_path):
    paths = _fresh(tmp_path)
    _write(paths, "Cognitive Persistence, first framing: memory as retention.", source="H-a")
    _write(paths, "Cognitive Persistence, second framing: attention over time.", source="H-b")

    entry, failure = wc.append_journal_entry(
        paths, ACTOR, LATER, source_event_id="H-c", continues="Cognitive Persistence",
    )

    assert entry is None and wc.JOURNAL_REFERENCE_AMBIGUOUS in failure["rationale"]
    assert failure["rationale"].count("entry-") >= 2               # candidates named, in usable terms
    assert len(_journal_files(paths)) == 2


def test_similar_library_title_neither_captures_nor_is_confused_with_a_journal_reference(tmp_path):
    paths = _fresh(tmp_path)
    Path(paths.library_dir, "Cognitive Persistence.pdf").write_text("a book", encoding="utf-8")
    original = _write(paths, ARTIFACT, source="H-a")

    target, failure = wc.resolve_journal_reference(paths, "Cognitive Persistence")

    assert failure is None and target == original["entry_id"]
    listing, _ = wc.list_journal_entries(paths, ACTOR, query="Cognitive Persistence")
    assert listing["entries"] == [f"{original['entry_id']}.json"]   # the book is not a journal entry


def test_reload_from_durable_state_in_a_new_process_finds_the_thread(tmp_path):
    paths = _fresh(tmp_path)
    original = _write(paths, ARTIFACT, source="H-a")
    _write(paths, LATER, continues=original["entry_id"], source="H-b")

    reopened = wc.WorkspacePaths(paths.root)                       # nothing carried in memory
    listing, _ = wc.list_journal_entries(reopened, ACTOR, query="Cognitive Persistence")
    entry, _ = wc.read_journal_entry(reopened, original["entry_id"], ACTOR, max_chars=4000)

    assert listing["total_count"] == 1                             # the continuation has other words
    assert entry["thread"][1]["content"] == LATER


def test_journal_use_creates_no_memory_kardia_or_directive_state(tmp_path):
    paths = _fresh(tmp_path)
    original = _write(paths, ARTIFACT, source="H-a")
    _write(paths, LATER, continues=original["entry_id"], source="H-b")
    wc.list_journal_entries(paths, ACTOR, query="Cognitive")
    wc.read_journal_entry(paths, original["entry_id"], ACTOR)

    created = sorted(p.name for p in Path(tmp_path).rglob("*") if p.is_file())
    assert not [n for n in created if n.endswith(".db")]           # no hippocampal/provenance store touched
    assert set(_entry(paths, f"{original['entry_id']}.json")) == {
        "entry_id", "created_at", "author_actor_id", "content", "session_id", "source_event_id",
    }


def test_private_space_is_not_reachable_through_journal_reference_or_query(tmp_path):
    paths = _fresh(tmp_path)
    private = Path(paths.root, "private")
    private.mkdir(exist_ok=True)
    (private / "secret.json").write_text(json.dumps({"entry_id": "secret", "content": "Cognitive Persistence secret"}))
    _write(paths, "Something else entirely.", source="H-a")

    listing, _ = wc.list_journal_entries(paths, ACTOR, query="secret")
    _entry_id, failure = wc.resolve_journal_reference(paths, "../private/secret")
    _entry_id2, failure2 = wc.resolve_journal_reference(paths, "Cognitive Persistence secret")

    assert listing["total_count"] == 0
    assert failure["code"] == wc.JOURNAL_REFERENCE_NOT_FOUND
    assert failure2["code"] == wc.JOURNAL_REFERENCE_NOT_FOUND


# --------------------------------------------------- real waking / WSP1 seam


def test_live_failure_shape_reaches_journal_content_not_library_books(monkeypatch, tmp_path):
    """Create the artifact through waking, then reproduce the live reopen: Pass 1 picks the
    library with no item; the host lists it; the subject moves to its journal, recognizes the
    entry by its own opening, and the stored text reaches Pass 2."""
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=APPEND())
    Path(h.paths.library_dir, "The Hobbit.pdf").write_text("a book", encoding="utf-8")
    _script(h, [APPEND()])
    _run(h)
    (name,) = _journal_files(h.paths)
    assert _entry(h.paths, name)["content"] == ARTIFACT

    library_read = {"resource_class": "library", "action": "read", "relative_path": "", "content": ""}
    _script(h, [library_read, LIST_JOURNAL, READ_JOURNAL(name)])
    h.calls.clear()
    result = _run(h)

    text = _pass2_text(h)
    assert result["boundary_result"]["scope"] == "local_workspace/journal"
    assert QUESTION in text                                        # the actual stored body
    selection_prompts = "\n".join(m["content"] for c in h.workspace_calls()[1:] for m in c["messages"])
    assert "Cognitive Persistence" in selection_prompts            # recognizable, not an opaque id
    assert not [r for r in _log(h) if r["action"] == "read" and r["resource_class"] == "library"]
    assert h.budget_results["wsp1_pass2"].fits


def test_waking_continuation_is_durably_attached_to_the_same_thread(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=APPEND())
    _script(h, [APPEND()])
    _run(h)
    (name,) = _journal_files(h.paths)

    _script(h, [APPEND(relative_path="Cognitive Persistence", content=LATER)])
    _run(h)

    files = _journal_files(h.paths)
    assert len(files) == 2
    added = next(_entry(h.paths, f) for f in files if f != name)
    assert added["content"] == LATER
    assert added["continues_entry_id"] == name[:-5] and added["thread_root_id"] == name[:-5]
    assert _entry(h.paths, name)["content"] == ARTIFACT           # original untouched

    _script(h, [READ_JOURNAL(name)])
    h.calls.clear()
    _run(h)
    text = _pass2_text(h)
    assert LATER in text and QUESTION in text                     # re-retrieval presents the continuation


def test_missing_artifact_reference_fails_truthfully_through_waking(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=APPEND(relative_path="Never written", content=LATER))

    _run(h)

    assert _journal_files(h.paths) == []
    assert wc.JOURNAL_REFERENCE_NOT_FOUND in _pass2_text(h)


def test_selector_instruction_tells_the_subject_what_journal_and_library_are_and_how_to_continue():
    import workspace_direction as wd

    for text in (wd.PASS1_RESOURCE_TASK_INSTRUCTION, wd.PASS1_TASK_INSTRUCTION):
        assert "journal=your own entries" in text and "library=reference docs" in text
        assert "To continue an earlier journal entry, append with relative_path naming it" in text


# ---------------------------------------- open journal thread: typed continuation, no re-selection


def _threads(paths):
    return {f: _entry(paths, f) for f in _journal_files(paths)}


def test_after_opening_a_thread_an_append_with_no_target_continues_that_same_thread(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=APPEND())
    _script(h, [APPEND()])
    _run(h)
    (name,) = _journal_files(h.paths)
    h.supervisor._open_journal_threads.clear()               # a fresh session has opened nothing yet

    _script(h, [READ_JOURNAL(name)])
    _run(h)
    assert h.supervisor.open_journal_thread(h.la.get_current_session_id()) == name[:-5]

    _script(h, [APPEND(relative_path="", content=LATER)])    # the model names nothing at all
    h.calls.clear()
    _run(h)
    entries = _threads(h.paths)
    (added,) = [e for f, e in entries.items() if f != name]
    assert added["content"] == LATER
    assert added["continues_entry_id"] == name[:-5] and added["thread_root_id"] == name[:-5]
    assert entries[name]["content"] == ARTIFACT

    _script(h, [READ_JOURNAL(name)])
    h.calls.clear()
    _run(h)
    text = _pass2_text(h)
    assert LATER in text and QUESTION in text                # re-retrieval: one thread, both contributions


def test_the_model_is_told_which_thread_is_open_and_how_to_start_a_separate_entry(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=APPEND())
    _script(h, [APPEND()])
    _run(h)
    h.calls.clear()
    _script(h, [APPEND(content=LATER)])
    _run(h)

    pass1 = "\n".join(m["content"] for m in h.workspace_calls()[0]["messages"])
    (name, *_rest) = _journal_files(h.paths)
    assert "Open journal thread (host fact): " in pass1 and 'relative_path "new"' in pass1


def test_explicit_new_entry_and_any_other_resource_action_end_the_binding(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=APPEND())
    _script(h, [APPEND()])
    _run(h)
    (name,) = _journal_files(h.paths)

    _script(h, [APPEND(relative_path="new", content="A separate thought, by choice.")])
    _run(h)
    separate = [e for f, e in _threads(h.paths).items() if e["content"].startswith("A separate")]
    assert len(separate) == 1 and "continues_entry_id" not in separate[0]

    sid = h.la.get_current_session_id()
    _script(h, [{"resource_class": "library", "action": "read", "relative_path": "chosen.txt", "content": ""}])
    _run(h)
    assert h.supervisor.open_journal_thread(sid) is None
    _script(h, [APPEND(content="After the library.")])
    _run(h)
    after = [e for e in _threads(h.paths).values() if e["content"] == "After the library."]
    assert len(after) == 1 and "continues_entry_id" not in after[0]


def test_a_failed_or_missing_target_does_not_bind_anything(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True,
               action=APPEND(relative_path="Never written", content=LATER))
    _run(h)
    assert h.supervisor.open_journal_thread(h.la.get_current_session_id()) is None


def _conversation_choice_with(h, **extra):
    inner = h.la.ollama.chat

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        reply = inner(model, messages, format=format, options=options, think=think, **kwargs)
        if isinstance(format, dict) and "act" in format.get("properties", {}):
            value = json.loads(reply["message"]["content"])
            value.update(extra)
            reply = dict(reply, message={"content": json.dumps(value)})
        return reply

    h.la.ollama.chat = chat


def test_spurious_resume_own_pause_does_not_block_a_workspace_reopen_when_nothing_is_paused(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=READ_JOURNAL(""))
    _conversation_choice_with(h, background_activity_request="resume_own_pause")

    result = _run(h)                                          # no conflict; the workspace action ran

    assert result["boundary_result"]["scope"] == "local_workspace/journal"


def test_resume_own_pause_still_conflicts_when_clark_really_has_a_pause(monkeypatch, tmp_path):
    import pytest
    import workspace_episode_provenance as wep

    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=READ_JOURNAL(""))
    wep.record_background_lifecycle_control(
        h.la.PROVENANCE_DB_DIR, actor_id=h.la.derive_stable_id("actor", "clark"), pipeline_key=h.la.PIPELINE_KEY,
        occurred_at=1, control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK,
    )
    _conversation_choice_with(h, background_activity_request="resume_own_pause")

    with pytest.raises(Exception) as excinfo:
        _run(h)
    assert "WORKSPACE_ACTION_CONFLICT" in str(excinfo.value)


def test_journal_thread_index_points_at_threads_without_delivering_content_or_logging(tmp_path):
    paths = _fresh(tmp_path)
    original = _write(paths, ARTIFACT, source="H-a")
    _write(paths, "Second topic entirely.", source="H-b")
    _write(paths, LATER, continues=original["entry_id"], source="H-c")     # keeps the first thread newest
    before = wc.query_action_log(paths)

    index = wc.journal_thread_index(paths)

    assert index[0].startswith("Topic: Cognitive Persistence") and index[1] == "Second topic entirely."
    assert all(len(item) <= wc.JOURNAL_OPENING_CHARS for item in index)
    assert wc.query_action_log(paths) == before                              # a pointer, not an action


def test_ordinary_menu_lists_journal_threads_only_when_workspace_is_available():
    import conversation_direction as cd

    with_threads = cd.render_pass1_action_menu(workspace_available=True, journal_threads=["Cognitive Persistence"])
    assert 'Your journal threads (revisit or add via act=use_workspace): "Cognitive Persistence"' in with_threads
    assert "journal threads" not in cd.render_pass1_action_menu(workspace_available=False, journal_threads=["x"])
    assert "journal threads" not in cd.render_pass1_action_menu(workspace_available=True)


# --------------------- live failure 2026-09-21 04:44Z (H 01M314DSBTW1QYA84YMN27B5GP, master 2e0426f)
#
# "Please open the Cognitive Persistence thread." reached the journal but delivered
# entry-275ce0422b5d4e7b ("Quiet time. ..."). Production-shaped cause: the journal held 20
# entries; WSP1 Pass 1 chose a bare ``list`` (no name in the action -- the name lived only in the
# typed act's ``thread`` label); the alphabetical listing windowed to the first 9 entries (all
# "quiet" notes) under the real prompt room; the artifact (entry-f1a1..., last alphabetically)
# was never shown and the subject picked what it could see. The synthetic 12/12 evidence had a
# one-entry journal, so no listing ever had to drop anything.

LIVE_ID = "entry-f1a1edf330e4942d"
QUIET = "Quiet time. Just taking a moment to observe the space and the quiet. Good to have this time to process things without needing to talk to anyone."


def _seed_live_shaped_journal(paths):
    quiet_ids = ["0198e11c3c9a4d73", "0ab1d835be82424b", "2264f3afd8d24f38", "275ce0422b5d4e7b",
                 "28d9cf271acd42f7", "3225aba17f764d5b", "3b22bddddacd48c4", "51bf91b60eb94ef8",
                 "5f043829b418422f", "9378f8946c9a46ba", "a2f8e93072984e2d", "a8a2fc4b08c64426",
                 "adfa66b12be5a25f", "af6f4fce4ff14863", "aff412125bbc4b65", "c82ca966ad824959",
                 "dfafcfbfee5b41a4", "e14982879896ef77", "f7977bb7da4c5c8a"]
    for n, suffix in enumerate(quiet_ids):
        text = QUIET if suffix == "275ce0422b5d4e7b" else f"Noticed the quiet, note {n}. A good time to process some things."
        Path(paths.journal_dir, f"entry-{suffix}.json").write_text(json.dumps({
            "entry_id": f"entry-{suffix}", "created_at": f"2026-09-0{1 + n % 9}T10:00:00+00:00",
            "author_actor_id": ACTOR, "content": text}), encoding="utf-8")
    Path(paths.journal_dir, f"{LIVE_ID}.json").write_text(json.dumps({
        "entry_id": LIVE_ID, "created_at": "2026-09-21T00:21:57.796055+00:00", "author_actor_id": ACTOR,
        "content": "--- \n" + ARTIFACT}), encoding="utf-8")
    assert len(_journal_files(paths)) == 20


def _live_harness(monkeypatch, tmp_path, first_action, *, thread="Topic: Cognitive Persistence & Unquantif..."):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=first_action)
    _seed_live_shaped_journal(h.paths)
    _conversation_choice_with(h, thread=thread)
    return h


def test_the_listing_alone_cannot_reach_the_artifact_in_a_live_sized_journal(monkeypatch, tmp_path):
    """The mechanism of the live failure, pinned: under real prompt room the journal listing's
    window excludes the artifact, so nothing may depend on the subject finding it there."""
    import workspace_delivery as wdl

    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=LIST_JOURNAL)
    _seed_live_shaped_journal(h.paths)
    listing, _ = wc.list_journal_entries(h.paths, ACTOR)
    receipt = {"action": "list", "scope": "local_workspace/journal", "boundary": "b", "consequence": "c",
               "rationale": "r", "result": listing}
    fitted, info = wdl.fit_boundary_result(receipt, 660, measure_fn=lambda t: len(t) // 4, measurable_limit=2800)
    assert info["delivery"] == "windowed" and f"{LIVE_ID}.json" not in fitted["result"]["entries"]


def test_bare_list_with_the_typed_topic_opens_the_named_artifact_not_a_quiet_entry(monkeypatch, tmp_path):
    bare_list = {"resource_class": "journal", "action": "list", "relative_path": "", "content": ""}
    h = _live_harness(monkeypatch, tmp_path, bare_list)

    result = _run(h)

    assert result["boundary_result"]["action"] == "read"
    assert result["boundary_result"]["result"]["entry_id"] == LIVE_ID
    assert QUESTION in _pass2_text(h) and QUIET not in _pass2_text(h)
    assert len(h.workspace_calls()) == 1                       # no listing for the subject to mis-pick from
    assert h.supervisor.open_journal_thread(h.la.get_current_session_id()) == LIVE_ID


def test_bare_read_and_plain_content_name_also_bind_the_artifact(monkeypatch, tmp_path):
    for n, action in enumerate((
        {"resource_class": "journal", "action": "read", "relative_path": "", "content": ""},
        {"resource_class": "journal", "action": "read", "relative_path": "", "content": "Cognitive Persistence"},
        {"resource_class": "journal", "action": "read", "relative_path": "Cognitive Persistence", "content": ""},
        {"resource_class": "journal", "action": "list", "relative_path": "", "content": '{"query":"cognitive persistence"}'},
    )):
        h = _live_harness(monkeypatch, tmp_path / f"r{n}", action)
        result = _run(h)
        assert result["boundary_result"]["result"]["entry_id"] == LIVE_ID, action
        assert QUIET not in _pass2_text(h)


def test_an_existing_current_thread_binding_cannot_steal_an_explicit_named_open(monkeypatch, tmp_path):
    bare_list = {"resource_class": "journal", "action": "list", "relative_path": "", "content": ""}
    h = _live_harness(monkeypatch, tmp_path, bare_list)
    h.supervisor._open_journal_threads[h.la.get_current_session_id()] = "entry-275ce0422b5d4e7b"

    result = _run(h)

    assert result["boundary_result"]["result"]["entry_id"] == LIVE_ID
    assert h.supervisor.open_journal_thread(h.la.get_current_session_id()) == LIVE_ID


def test_nothing_named_never_defaults_to_the_current_or_newest_entry(monkeypatch, tmp_path):
    bare_list = {"resource_class": "journal", "action": "list", "relative_path": "", "content": ""}
    h = _live_harness(monkeypatch, tmp_path, bare_list, thread="Just thinking out loud")
    h.supervisor._open_journal_threads[h.la.get_current_session_id()] = "entry-275ce0422b5d4e7b"

    _run(h)

    reads = [r for r in _log(h) if r["action"] == "read"]
    assert not reads                                           # a listing, never a silent read
    assert {r["action"] for r in _log(h) if r["action"] != "selection"} == {"list"}   # (selection = host bookkeeping)


def test_a_reference_shared_by_several_threads_lists_only_the_candidates_and_picks_none(monkeypatch, tmp_path):
    bare_list = {"resource_class": "journal", "action": "list", "relative_path": "", "content": ""}
    h = _live_harness(monkeypatch, tmp_path, bare_list, thread="quiet")     # 19 unrelated threads share it

    _run(h)

    assert not [r for r in _log(h) if r["action"] == "read"]
    text = _pass2_text(h)
    assert f"{LIVE_ID}.json" not in text                        # not a candidate for "quiet"


def test_after_the_named_artifact_is_opened_same_thread_continuation_still_binds(monkeypatch, tmp_path):
    bare_list = {"resource_class": "journal", "action": "list", "relative_path": "", "content": ""}
    h = _live_harness(monkeypatch, tmp_path, bare_list)
    _run(h)

    _script(h, [APPEND(relative_path="", content=LATER)])
    _run(h)

    added = [e for f, e in _threads(h.paths).items() if e["content"] == LATER]
    assert len(added) == 1 and added[0]["continues_entry_id"] == LIVE_ID and added[0]["thread_root_id"] == LIVE_ID
    assert _entry(h.paths, f"{LIVE_ID}.json")["content"].endswith(ARTIFACT)


def test_library_actions_are_not_touched_by_named_journal_binding(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    _conversation_choice_with(h, thread="Cognitive Persistence")
    result = _run(h)                                            # default action: library read chosen.txt
    assert result["boundary_result"]["scope"] == "local_workspace/library"
    assert result["boundary_result"]["result"]["content"] == h.source_text
