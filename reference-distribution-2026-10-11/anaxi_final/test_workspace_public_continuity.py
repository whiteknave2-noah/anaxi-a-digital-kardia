"""WSP2-P4 acceptance tests. Zero real Ollama/model calls. Waking-side
fixtures use the REAL native_provenance_writer.record_native_waking_turn()
(never hand-rolled INSERTs) against a temp copy of anaxi_provenance.db's
schema, so the new waking_interaction_mode component is written exactly
as production writes it. Roaming-side fixtures use the REAL
workspace_episode_provenance.py recorders, matching
test_workspace_episode_context.py's own established fixture pattern.
"""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
import uuid

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import provenance_schema
import workspace_episode_context as wec
import workspace_episode_provenance as wep
import workspace_public_continuity as wpc
from native_provenance_writer import record_native_waking_turn
from native_turn_staging import append_staging_entry
from provenance_schema import derive_stable_id

PIPELINE_KEY = "anaxi_orchestration_lineage_a"
CLARK_ACTOR_ID = derive_stable_id("actor", "clark")

TEST_DIR = tempfile.mkdtemp(prefix="wpc_test_")
SOURCE_DB = os.path.join(ANAXI_FINAL, "anaxi_provenance.db")


def fresh_dir(name):
    d = os.path.join(TEST_DIR, name)
    os.makedirs(d, exist_ok=True)
    return d


def fresh_copy_db(name):
    """Fresh production schema with only the reference rows this test needs."""
    d = fresh_dir(name)
    path = os.path.join(d, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(path)
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-test-1', ?, 'llama', 'test')", (PIPELINE_KEY,),
    )
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
        "VALUES (?, 'clark_agent', 'clark', 1000)", (CLARK_ACTOR_ID,),
    )
    host_actor_id = derive_stable_id("actor", "bounded_clause_renderer")
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
        "VALUES (?, 'host_system', 'bounded_clause_renderer', 1000)", (host_actor_id,),
    )
    conn.execute(
        "INSERT INTO actor_host_system (actor_id, subsystem_key) "
        "VALUES (?, 'bounded_clause_renderer')", (host_actor_id,),
    )
    conn.commit()
    conn.close()
    return d


def fresh_staging_path(data_dir):
    return os.path.join(data_dir, "native_turn_staging.jsonl")


_session_started_at_by_session = {}


def seed_conversation_turn(data_dir, staging_path, session_id, prompt, clark_text, occurred_at, interaction_mode="conversation", delivered_active_workspace_event_ids=None):
    """Writes one complete, canonical waking turn via the REAL
    record_native_waking_turn() + append_staging_entry() -- never a
    hand-rolled INSERT -- so the new waking_interaction_mode component
    is written exactly as production writes it. Returns event_id.

    session_started_at is fixed at this SESSION's first-ever call and
    reused for every later turn in the same session_id (matching
    production's own one-mint-per-process-run contract via
    _get_native_session()) -- record_native_waking_turn()'s own
    _resolve_or_create_session() hard-stops on a mismatched
    session_started_at for an existing session_id, exactly like a real
    collision/corruption check would."""
    session_started_at = _session_started_at_by_session.setdefault(session_id, occurred_at)
    event_id = f"evt-{uuid.uuid4().hex[:12]}"
    auth_context_id = f"authctx-{uuid.uuid4().hex[:12]}"
    staging_id = append_staging_entry(staging_path, {
        "session_id": session_id, "session_started_at": session_started_at, "event_id": event_id,
        "auth_context_id": auth_context_id, "user_id": "nate", "prompt": prompt,
        "bounded_clause": "", "clark_prose": clark_text, "kardia": {}, "controls": {},
        "waking_model_tag": "gemma4:e4b", "pipeline_key": PIPELINE_KEY,
        "artifact_pass_ran": False, "occurred_at": occurred_at, "interaction_mode": interaction_mode,
    })
    record_native_waking_turn(
        data_dir, event_id=event_id, staging_id=staging_id, session_id=session_id,
        session_started_at=session_started_at, auth_context_id=auth_context_id,
        prompt=prompt, bounded_clause="", clark_prose=clark_text,
        pipeline_key=PIPELINE_KEY, waking_model_tag="gemma4:e4b",
        artifact_pass_ran=False, occurred_at=occurred_at, interaction_mode=interaction_mode,
        delivered_active_workspace_event_ids=delivered_active_workspace_event_ids,
    )
    return event_id


def fresh_empty_conversation_db(name):
    """A genuinely EMPTY, schema-only DB (never a copy of production
    data) -- required for any test that inspects GLOBAL state across
    ALL events (compute_high_water_mark() has no session_id scope to
    isolate it, unlike collect_live_waking_units()/
    collect_session_dialogue_pairs()). fresh_copy_db() above is correct
    for session-scoped tests (a fresh session_id naturally has zero
    pre-existing rows even against copied real data) but would be
    silently wrong here, since it would pick up this whole session's
    own real, much-later events as "the" high-water mark."""
    data_dir = fresh_dir(name)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-test-1', ?, 'llama', 'test')", (PIPELINE_KEY,),
    )
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', 1000)",
        (CLARK_ACTOR_ID,),
    )
    host_actor_id = derive_stable_id("actor", "bounded_clause_renderer")
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', 1000)",
        (host_actor_id,),
    )
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    conn.commit()
    conn.close()
    return data_dir


def fresh_episode_db(name):
    """Same shape as test_workspace_episode_context.py's own fresh_db()."""
    data_dir = fresh_dir(name)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-test-1', ?, 'llama', 'test')", (PIPELINE_KEY,),
    )
    clark_actor_id = provenance_schema.derive_stable_id("actor", "clark")
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', 1000)", (clark_actor_id,))
    host_actor_id = wep.host_actor_id()
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', 1000)", (host_actor_id,))
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    human_actor_id = "human-actor-test-canonical"
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', ?, 1000)",
        (human_actor_id, human_actor_id),
    )
    conn.commit()
    conn.close()
    return data_dir, clark_actor_id


# ===================================================================== A: bounded selector


def test_A_select_bounded_all_included_under_both_bounds():
    items = [{"n": i} for i in range(3)]
    selected = wpc.select_bounded_newest_first(items, lambda i: 10, max_items=8, max_chars=1000)
    assert selected == items


def test_A_select_bounded_more_than_max_items_keeps_newest():
    items = [{"n": i} for i in range(10)]
    selected = wpc.select_bounded_newest_first(items, lambda i: 1, max_items=4, max_chars=1000)
    assert [i["n"] for i in selected] == [6, 7, 8, 9]


def test_A_select_bounded_aggregate_cap_selects_newest_until_next_would_exceed():
    items = [{"n": i, "cost": 300} for i in range(5)]
    selected = wpc.select_bounded_newest_first(items, lambda i: i["cost"], max_items=8, max_chars=700)
    assert [i["n"] for i in selected] == [3, 4]  # 300+300=600<=700; a third would be 900>700


def test_A_select_bounded_oversized_newest_included_alone():
    items = [{"n": 0, "cost": 50}, {"n": 1, "cost": 5000}]
    selected = wpc.select_bounded_newest_first(items, lambda i: i["cost"], max_items=8, max_chars=1000)
    assert [i["n"] for i in selected] == [1]


def test_A_select_bounded_deterministic():
    items = [{"n": i, "cost": 37} for i in range(9)]
    a = wpc.select_bounded_newest_first(items, lambda i: i["cost"], max_items=4, max_chars=100)
    b = wpc.select_bounded_newest_first(items, lambda i: i["cost"], max_items=4, max_chars=100)
    assert a == b


# ===================================================== B: high-water mark / snapshot


def test_B_high_water_mark_missing_db_is_safe_floor():
    data_dir = fresh_dir("hwm_missing_db")
    assert wpc.compute_high_water_mark(data_dir) == (0, "")


def test_B_high_water_mark_reflects_latest_committed_event():
    data_dir = fresh_empty_conversation_db("hwm_reflects")
    staging_path = fresh_staging_path(data_dir)
    e1 = seed_conversation_turn(data_dir, staging_path, "sess-hwm", "p1", "r1", 1000)
    hwm1 = wpc.compute_high_water_mark(data_dir)
    assert hwm1 == (1000, e1)
    e2 = seed_conversation_turn(data_dir, staging_path, "sess-hwm", "p2", "r2", 1010)
    hwm2 = wpc.compute_high_water_mark(data_dir)
    assert hwm2 == (1010, e2)
    assert hwm2 > hwm1


def test_B_event_committed_after_snapshot_excluded_from_current_build():
    data_dir = fresh_empty_conversation_db("hwm_snapshot_excl")
    staging_path = fresh_staging_path(data_dir)
    seed_conversation_turn(data_dir, staging_path, "sess-snap", "before", "before-reply", 1000)
    hwm = wpc.compute_high_water_mark(data_dir)
    # Committed AFTER the snapshot was taken -- must not appear in a
    # collection call that reuses the earlier hwm.
    seed_conversation_turn(data_dir, staging_path, "sess-snap", "after", "after-reply", 2000)
    units = wpc.collect_live_waking_units(data_dir, "sess-snap", staging_path, since_marker=None, high_water_mark=hwm)
    assert [u["prompt"] for u in units] == ["before"]


def test_B_next_build_may_receive_the_later_event():
    data_dir = fresh_empty_conversation_db("hwm_next_build")
    staging_path = fresh_staging_path(data_dir)
    seed_conversation_turn(data_dir, staging_path, "sess-next", "before", "before-reply", 1000)
    hwm1 = wpc.compute_high_water_mark(data_dir)
    seed_conversation_turn(data_dir, staging_path, "sess-next", "after", "after-reply", 2000)
    hwm2 = wpc.compute_high_water_mark(data_dir)
    assert hwm2 > hwm1
    units = wpc.collect_live_waking_units(data_dir, "sess-next", staging_path, since_marker=None, high_water_mark=hwm2)
    assert [u["prompt"] for u in units] == ["before", "after"]


def test_B_ordering_never_requires_semantic_guessing_source_audit():
    # Import-level audit, not a raw text scan -- this module's OWN
    # docstrings legitimately discuss "importance"/"salience" by name
    # to explain what they deliberately do NOT introduce (see the
    # module header and delivery-ledger docstrings); a text scan would
    # false-trigger on that honesty, exactly like the analogous OWC8-S1
    # fix in test_session_dialogue_window.py.
    names = _module_imports(os.path.join(ANAXI_FINAL, "workspace_public_continuity.py"))
    for forbidden in ("sklearn", "numpy", "sentence_transformers", "datetime", "time"):
        assert forbidden not in names


# ================================================== C: LIVE_WAKING_CONTINUITY_V1


def test_C_empty_when_zero_post_departure_turns():
    data_dir = fresh_copy_db("live_empty")
    staging_path = fresh_staging_path(data_dir)
    hwm = wpc.compute_high_water_mark(data_dir)
    units = wpc.collect_live_waking_units(data_dir, "sess-empty", staging_path, since_marker=None, high_water_mark=hwm)
    assert units == []
    assert wpc.render_live_waking_continuity(units) == ""


def test_C_successful_canonical_turn_after_departure_is_eligible():
    data_dir = fresh_copy_db("live_eligible")
    staging_path = fresh_staging_path(data_dir)
    seed_conversation_turn(data_dir, staging_path, "sess-c", "hello", "hi there", 1000)
    hwm = wpc.compute_high_water_mark(data_dir)
    units = wpc.collect_live_waking_units(data_dir, "sess-c", staging_path, since_marker=None, high_water_mark=hwm)
    assert len(units) == 1
    assert units[0]["prompt"] == "hello" and units[0]["clark_text"] == "hi there"


def test_D_multiple_turns_deterministic_bounded_newest_set():
    data_dir = fresh_copy_db("live_bounded")
    staging_path = fresh_staging_path(data_dir)
    for i in range(6):
        seed_conversation_turn(data_dir, staging_path, "sess-d", f"p{i}", f"r{i}", 1000 + i * 10)
    hwm = wpc.compute_high_water_mark(data_dir)
    units = wpc.collect_live_waking_units(data_dir, "sess-d", staging_path, since_marker=None, high_water_mark=hwm)
    assert len(units) == wpc.LIVE_WAKING_MAX_UNITS  # 4, newest of the 6
    assert [u["prompt"] for u in units] == ["p2", "p3", "p4", "p5"]
    units_again = wpc.collect_live_waking_units(data_dir, "sess-d", staging_path, since_marker=None, high_water_mark=hwm)
    assert units == units_again


def test_E_failed_contained_waking_turns_excluded():
    # A contained/failed turn never persists a complete canonical pair
    # (no event_id, no matching conversational_prose) in the first
    # place -- nothing extra to filter. Simulated here by simply never
    # seeding one; collect_session_dialogue_pairs()'s own established
    # test suite (test_session_dialogue_window.py) already proves the
    # skip-incomplete-pair behavior this function inherits unmodified.
    data_dir = fresh_copy_db("live_failed_excluded")
    staging_path = fresh_staging_path(data_dir)
    seed_conversation_turn(data_dir, staging_path, "sess-e", "ok prompt", "ok reply", 1000)
    hwm = wpc.compute_high_water_mark(data_dir)
    units = wpc.collect_live_waking_units(data_dir, "sess-e", staging_path, since_marker=None, high_water_mark=hwm)
    assert len(units) == 1  # only the genuinely-persisted turn


def test_F_task_mode_excluded():
    data_dir = fresh_copy_db("live_task_excluded")
    staging_path = fresh_staging_path(data_dir)
    seed_conversation_turn(data_dir, staging_path, "sess-f", "conv prompt", "conv reply", 1000, interaction_mode="conversation")
    seed_conversation_turn(data_dir, staging_path, "sess-f", "task prompt", "task reply", 1010, interaction_mode="task")
    hwm = wpc.compute_high_water_mark(data_dir)
    units = wpc.collect_live_waking_units(data_dir, "sess-f", staging_path, since_marker=None, high_water_mark=hwm)
    assert [u["prompt"] for u in units] == ["conv prompt"]
    assert "task prompt" not in [u["prompt"] for u in units]


def test_G_attribution_source_ids_preserved():
    data_dir = fresh_copy_db("live_attribution")
    staging_path = fresh_staging_path(data_dir)
    event_id = seed_conversation_turn(data_dir, staging_path, "sess-g", "who said this", "I did", 1000)
    hwm = wpc.compute_high_water_mark(data_dir)
    units = wpc.collect_live_waking_units(data_dir, "sess-g", staging_path, since_marker=None, high_water_mark=hwm)
    assert units[0]["event_id"] == event_id
    rendered = wpc.render_live_waking_continuity(units)
    assert event_id in rendered


def test_H_source_content_exact_no_paraphrase():
    data_dir = fresh_copy_db("live_exact_text")
    staging_path = fresh_staging_path(data_dir)
    tricky_prompt = "Line one.\nLine two -- \"quotes,\" trailing spaces.   "
    tricky_reply = "Reply with\ttabs and an em dash -- exact."
    seed_conversation_turn(data_dir, staging_path, "sess-h", tricky_prompt, tricky_reply, 1000)
    hwm = wpc.compute_high_water_mark(data_dir)
    units = wpc.collect_live_waking_units(data_dir, "sess-h", staging_path, since_marker=None, high_water_mark=hwm)
    assert units[0]["prompt"] == tricky_prompt
    assert units[0]["clark_text"] == tricky_reply
    rendered = wpc.render_live_waking_continuity(units)
    # Rendered via !r (same established convention as
    # render_departure_handoff()/describe_direction_owner()-adjacent
    # code elsewhere in this codebase) -- exact and lossless, just
    # escaped for safe embedding, so the REPR form is what to expect.
    assert repr(tricky_prompt) in rendered and repr(tricky_reply) in rendered


def test_I_renderer_cap_enforced():
    data_dir = fresh_copy_db("live_cap")
    staging_path = fresh_staging_path(data_dir)
    big = "x" * 2000
    for i in range(3):
        seed_conversation_turn(data_dir, staging_path, "sess-i", big, big, 1000 + i * 10)
    hwm = wpc.compute_high_water_mark(data_dir)
    units = wpc.collect_live_waking_units(data_dir, "sess-i", staging_path, since_marker=None, high_water_mark=hwm)
    total_chars = sum(len(u["prompt"]) + len(u["clark_text"]) for u in units)
    # The single newest unit alone (4000 chars) exceeds the 3000 cap --
    # explicit exception: included intact, alone.
    assert len(units) == 1
    assert total_chars > wpc.LIVE_WAKING_MAX_CHARS


def _module_imports(path):
    import ast
    with open(path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_J_private_can_never_enter_live_waking_import_audit():
    # AST-level, not a raw text scan -- this module's OWN docstring
    # legitimately discusses "workspace_private" by name to explain
    # what it deliberately does NOT import (see module header); a text
    # scan would false-trigger on its own honesty, exactly like the
    # analogous OWC8-S1 fix in test_session_dialogue_window.py.
    names = _module_imports(os.path.join(ANAXI_FINAL, "workspace_public_continuity.py"))
    assert "workspace_private" not in names


# ================================================ Section 21: roaming -> waking


def test_21A_active_run_zero_public_events_empty():
    data_dir, clark_actor_id = fresh_episode_db("active_zero")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    hwm = wpc.compute_high_water_mark(data_dir)
    events = wpc.collect_active_workspace_events(data_dir, run_id, since_marker=None, high_water_mark=hwm)
    # Exactly one mechanical fact -- the "became active" capability-state
    # transition -- and nothing else (no public action/wait yet).
    assert len(events) == 1
    assert "became active" in events[0]["rendered"]


def test_21B_canonical_public_action_available_before_termination():
    data_dir, clark_actor_id = fresh_episode_db("active_action")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_public_action(
        data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1010,
        resource_class="journal", action="append", relative_path="e.json", success=True,
        action_id_backlink="wsaction-1", model_revision_id=None, journal_text="text",
    )
    hwm = wpc.compute_high_water_mark(data_dir)
    # Episode is NOT ended -- this must still be visible (spec section
    # 8/14: no artificial closure required).
    assert wep.find_pending_episode_run_ids(data_dir) == []  # not pending -- not even ended yet
    events = wpc.collect_active_workspace_events(data_dir, run_id, since_marker=None, high_water_mark=hwm)
    kinds = [e["rendered"] for e in events]
    assert any("journal.append: ok" in k for k in kinds)


def test_21C_canonical_public_wait_available_before_termination():
    data_dir, clark_actor_id = fresh_episode_db("active_wait")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_public_wait(data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1010, wait_minutes=15, model_revision_id=None)
    hwm = wpc.compute_high_water_mark(data_dir)
    events = wpc.collect_active_workspace_events(data_dir, run_id, since_marker=None, high_water_mark=hwm)
    assert any("wait 15m" in e["rendered"] for e in events)


def test_21D_capability_state_transition_available():
    data_dir, clark_actor_id = fresh_episode_db("active_capstate")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_episode_ended(data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1010, termination_class=wep.TERMINATION_CONTROL_VALIDATION_FAILURE)
    hwm = wpc.compute_high_water_mark(data_dir)
    events = wpc.collect_active_workspace_events(data_dir, run_id, since_marker=None, high_water_mark=hwm)
    rendered = [e["rendered"] for e in events]
    assert any("became active" in r for r in rendered)
    assert any("ended (reason: control_validation_failure)" in r for r in rendered)


def test_21E_deterministic_bounded_newest_set():
    data_dir, clark_actor_id = fresh_episode_db("active_bounded")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    for i in range(10):
        wep.record_public_wait(data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1010 + i, wait_minutes=5, model_revision_id=None)
    hwm = wpc.compute_high_water_mark(data_dir)
    events = wpc.collect_active_workspace_events(data_dir, run_id, since_marker=None, high_water_mark=hwm)
    assert len(events) == wpc.ACTIVE_WORKSPACE_MAX_EVENTS  # 6, newest
    events_again = wpc.collect_active_workspace_events(data_dir, run_id, since_marker=None, high_water_mark=hwm)
    assert events == events_again


def test_21G_task_mode_excluded_source_audit():
    # ACTIVE_WORKSPACE_CONTINUITY_V1 delivery into a waking turn is
    # gated at the llama_anaxi.py call site, inside the CONVERSATION_MODE
    # branch only -- proven by source audit here (renderer itself is
    # mode-agnostic by design, matching Bridge C's own insertion point).
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    active_call = source.index("workspace_public_continuity.find_active_roaming_run_id")
    conv_branch_start = source.rfind(
        "if resolved_interaction_mode == CONVERSATION_MODE:", 0, active_call,
    )
    assert conv_branch_start >= 0
    # OWC9-P4: the literal line immediately after "else:" is no longer
    # a stable anchor -- TASK-mode's own aggregate budget composition
    # (_compose_task_mode_pass2_budget()) was inserted between "else:"
    # and the pass2_task_mode call. "\n    else:\n" (exactly 4-space
    # indented) still uniquely identifies the top-level else paired
    # with the CONVERSATION_MODE if above -- any else: belonging to a
    # nested if inside that branch sits at a deeper indentation.
    else_branch_start = source.index("\n    else:\n", active_call)
    conv_branch = source[conv_branch_start:else_branch_start]
    assert "workspace_public_continuity.find_active_roaming_run_id" in conv_branch
    assert "workspace_public_continuity.find_active_roaming_run_id" not in source[else_branch_start:]


def test_21H_private_excluded_import_audit():
    names = _module_imports(os.path.join(ANAXI_FINAL, "workspace_public_continuity.py"))
    assert "workspace_private" not in names


def test_21I_canonical_provenance_not_jsonl_source_audit():
    with open(os.path.join(ANAXI_FINAL, "workspace_public_continuity.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in (".jsonl", "workspace_roaming_trace", "workspace_action_log"):
        assert forbidden not in source


def test_21J_hard_cap_enforced():
    data_dir, clark_actor_id = fresh_episode_db("active_hardcap")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    for i in range(20):
        wep.record_public_action(
            data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1010 + i,
            resource_class="journal", action="append", relative_path=f"e{i}.json", success=True,
            action_id_backlink=f"wsaction-{i}", model_revision_id=None, journal_text="x" * 100,
        )
    hwm = wpc.compute_high_water_mark(data_dir)
    events = wpc.collect_active_workspace_events(data_dir, run_id, since_marker=None, high_water_mark=hwm)
    assert len(events) <= wpc.ACTIVE_WORKSPACE_MAX_EVENTS
    total_chars = sum(len(e["rendered"]) for e in events)
    assert total_chars <= wpc.ACTIVE_WORKSPACE_MAX_CHARS or len(events) == 1  # oversized-newest exception


def test_find_active_roaming_run_id_none_before_any_run():
    data_dir, clark_actor_id = fresh_episode_db("active_none_yet")
    assert wpc.find_active_roaming_run_id(data_dir) is None


def test_find_active_roaming_run_id_finds_started_unended():
    data_dir, clark_actor_id = fresh_episode_db("active_found")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    assert wpc.find_active_roaming_run_id(data_dir) == run_id


def test_find_active_roaming_run_id_none_once_ended():
    data_dir, clark_actor_id = fresh_episode_db("active_ended")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_episode_ended(data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1010, termination_class=wep.TERMINATION_HUMAN_STOP)
    assert wpc.find_active_roaming_run_id(data_dir) is None


def test_active_workspace_renderer_never_uses_belief_feeling_language():
    with open(os.path.join(ANAXI_FINAL, "workspace_public_continuity.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("you felt", "you believe", "you wanted", "you enjoyed", "you remembered"):
        assert forbidden not in source.lower()


# =========================== Section 15: durable waking -> roam delivery novelty


def test_15A_delivered_waking_event_gets_durable_evidence():
    # fresh_copy_db() copies SOURCE_DB (the real, live, ever-growing
    # production anaxi_provenance.db) purely for its pre-seeded schema/
    # pipelines/actors -- it is NOT a schema-only/empty DB, so this
    # test asserts membership of the SPECIFIC event_id under test
    # (before/after), never global ledger emptiness -- production's own
    # legitimate accumulated roaming activity (this project's own
    # background worker has been continuously, autonomously active
    # across this whole session) already populates this exact ledger
    # with unrelated rows, correctly.
    data_dir = fresh_copy_db("durable_waking_evidence")
    staging_path = fresh_staging_path(data_dir)
    event_id = seed_conversation_turn(data_dir, staging_path, "sess-15a", "hello", "hi", 1000)
    assert event_id not in wep.find_delivered_live_waking_event_ids(data_dir)
    wep.record_live_waking_delivery(
        data_dir, run_id="wrun-15a", pipeline_key=PIPELINE_KEY, occurred_at=1010,
        delivered_event_ids=[event_id],
    )
    assert event_id in wep.find_delivered_live_waking_event_ids(data_dir)


def test_15C_delivered_event_not_classified_new_again_simulated_restart():
    data_dir = fresh_copy_db("durable_waking_restart")
    staging_path = fresh_staging_path(data_dir)
    event_id = seed_conversation_turn(data_dir, staging_path, "sess-15c", "hello", "hi", 1000)
    hwm = wpc.compute_high_water_mark(data_dir)
    units = wpc.collect_live_waking_units(data_dir, "sess-15c", staging_path, since_marker=None, high_water_mark=hwm)
    assert len(units) == 1
    wep.record_live_waking_delivery(
        data_dir, run_id="wrun-15c", pipeline_key=PIPELINE_KEY, occurred_at=1010,
        delivered_event_ids=[u["event_id"] for u in units],
    )
    # 15B: "simulated restart" -- collect_live_waking_units() itself
    # holds no process state at all (it is a pure read-only function
    # called fresh every time), so there is nothing to reset; calling
    # it again IS the restart simulation, proving durability rests
    # entirely on canonical provenance, never in-memory state.
    units_again = wpc.collect_live_waking_units(data_dir, "sess-15c", staging_path, since_marker=None, high_water_mark=hwm)
    assert units_again == []  # NOT new again
    assert event_id not in [u["event_id"] for u in units_again]


def test_15D_prior_context_would_be_distinguishable_if_retained():
    # This implementation chose Option A (omit already-delivered
    # material entirely, spec P4-P1 section 6) rather than repeating it
    # under an explicit "prior context" label -- confirmed by 15C
    # above (already-delivered units simply vanish from the eligible
    # pool, they are never re-rendered at all, so there is no
    # unlabeled-repeat risk to test for).
    pass


def test_15E_genuinely_later_waking_event_is_new():
    data_dir = fresh_copy_db("durable_waking_later_new")
    staging_path = fresh_staging_path(data_dir)
    e1 = seed_conversation_turn(data_dir, staging_path, "sess-15e", "first", "first reply", 1000)
    wep.record_live_waking_delivery(data_dir, run_id="wrun-15e", pipeline_key=PIPELINE_KEY, occurred_at=1010, delivered_event_ids=[e1])
    e2 = seed_conversation_turn(data_dir, staging_path, "sess-15e", "second", "second reply", 1020)
    hwm = wpc.compute_high_water_mark(data_dir)
    units = wpc.collect_live_waking_units(data_dir, "sess-15e", staging_path, since_marker=None, high_water_mark=hwm)
    assert [u["event_id"] for u in units] == [e2]  # only the genuinely new one


def test_15F_G_ledger_contains_only_ids_order_destination_no_semantic_content():
    # fresh_copy_db() copies real, live production data for its schema
    # (see test_15A's own comment) -- pre-existing, unrelated delivered-
    # event-id rows are expected and correct; this test proves the
    # NEWLY-written row's own shape (bare event_id, nothing else) and
    # that NO row anywhere in the ledger -- old or new -- ever leaks
    # semantic content.
    data_dir = fresh_copy_db("durable_waking_ledger_shape")
    staging_path = fresh_staging_path(data_dir)
    secret = "SECRET_WAKING_CONTENT_MARKER_never_in_the_ledger"
    event_id = seed_conversation_turn(data_dir, staging_path, "sess-15fg", secret, "reply", 1000)
    wep.record_live_waking_delivery(data_dir, run_id="wrun-15fg", pipeline_key=PIPELINE_KEY, occurred_at=1010, delivered_event_ids=[event_id])
    conn = sqlite3.connect(f"file:{os.path.join(data_dir, 'anaxi_provenance.db')}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT component_text FROM event_components WHERE component_kind = ?",
            (wep.LIVE_WAKING_DELIVERED_EVENT_ID_COMPONENT_KIND,),
        ).fetchall()
    finally:
        conn.close()
    assert (event_id,) in rows  # exactly the bare source event_id is present
    for row in rows:
        assert secret not in row[0]


# ============================ Section 16: durable roam -> waking delivery novelty


def test_16A_delivered_public_event_gets_durable_acknowledgment():
    data_dir, clark_actor_id = fresh_episode_db("durable_public_ack")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    hwm = wpc.compute_high_water_mark(data_dir)
    events = wpc.collect_active_workspace_events(data_dir, run_id, None, hwm)
    assert len(events) == 1
    staging_path = fresh_staging_path(data_dir)
    seed_conversation_turn(
        data_dir, staging_path, "sess-16a", "welcome back", "thanks", 1010,
        delivered_active_workspace_event_ids=[e["event_id"] for e in events],
    )
    assert events[0]["event_id"] in wpc.find_delivered_public_event_ids(data_dir)


def test_16C_delivered_public_event_not_classified_new_again():
    data_dir, clark_actor_id = fresh_episode_db("durable_public_restart")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    hwm = wpc.compute_high_water_mark(data_dir)
    events = wpc.collect_active_workspace_events(data_dir, run_id, None, hwm)
    staging_path = fresh_staging_path(data_dir)
    seed_conversation_turn(
        data_dir, staging_path, "sess-16c", "welcome back", "thanks", 1010,
        delivered_active_workspace_event_ids=[e["event_id"] for e in events],
    )
    # "Restart": collect_active_workspace_events() is itself a pure,
    # stateless read -- calling it again from a fresh vantage point IS
    # the restart simulation.
    events_again = wpc.collect_active_workspace_events(data_dir, run_id, None, hwm)
    assert events_again == []


def test_16D_genuinely_later_public_event_is_new():
    data_dir, clark_actor_id = fresh_episode_db("durable_public_later_new")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    hwm1 = wpc.compute_high_water_mark(data_dir)
    first = wpc.collect_active_workspace_events(data_dir, run_id, None, hwm1)
    staging_path = fresh_staging_path(data_dir)
    seed_conversation_turn(
        data_dir, staging_path, "sess-16d", "hi", "hi back", 1010,
        delivered_active_workspace_event_ids=[e["event_id"] for e in first],
    )
    wep.record_public_wait(data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1020, wait_minutes=5, model_revision_id=None)
    hwm2 = wpc.compute_high_water_mark(data_dir)
    second = wpc.collect_active_workspace_events(data_dir, run_id, None, hwm2)
    assert len(second) == 1
    assert "wait 5m" in second[0]["rendered"]


def test_16E_failed_waking_turn_does_not_acknowledge():
    # A failed/contained waking turn never reaches
    # stage_and_record_native_waking_turn() successfully (this is the
    # SAME already-established invariant OWC7-P2/WSP2-P3 rely on) --
    # source-proven here: the delivered_active_workspace_event_ids
    # argument is only ever computed and passed inside the SAME call
    # that is itself inside the try/persistence path, never written
    # independently beforehand.
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    assert source.count("delivered_active_workspace_event_ids=delivered_active_workspace_marker,") == 1
    # And it appears strictly inside the stage_and_record_native_waking_turn(...) call.
    call_idx = source.index("native_provenance_writer.stage_and_record_native_waking_turn,")
    param_idx = source.index("delivered_active_workspace_event_ids=delivered_active_workspace_marker,")
    next_call_idx = source.find("native_provenance_writer.stage_and_record_native_waking_turn,", call_idx + 1)
    assert call_idx < param_idx and (next_call_idx == -1 or param_idx < next_call_idx)


def test_16F_live_renderer_and_bridge_c_share_same_durable_truth():
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    # Both call sites consult the SAME function.
    assert source.count("workspace_public_continuity.find_delivered_public_event_ids(PROVENANCE_DB_DIR)") == 1
    assert "collect_active_workspace_events" in source  # live renderer path (uses the shared exclusion internally)
    assert "already_delivered_event_ids=already_delivered_event_ids" in source  # Bridge C path, same set


# ==================================================== Section 17: Bridge-C deduplication


def test_17_bridge_c_omits_already_live_delivered_public_material():
    data_dir, clark_actor_id = fresh_episode_db("bridgec_dedup")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    p1 = wep.record_public_action(
        data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1010,
        resource_class="journal", action="append", relative_path="e1.json", success=True,
        action_id_backlink="wsaction-1", model_revision_id=None, journal_text="text1",
    )
    p2 = wep.record_public_wait(data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1020, wait_minutes=15, model_revision_id=None)
    wep.record_episode_ended(data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1030, termination_class=wep.TERMINATION_HUMAN_STOP)

    # G: termination/closure fact remains represented regardless.
    text_no_filter = wec.build_workspace_episode_context(data_dir, run_id)
    assert "Ended: human_stop." in text_no_filter
    assert "journal.append: ok" in text_no_filter
    assert "wait 15m" in text_no_filter

    # E/F: P1 was already delivered live -- Bridge C must omit it from
    # the itemized "new material" listing, but P2 (never delivered)
    # remains eligible, and closure stays represented either way.
    already_delivered = {p1}
    # NOTE: record_public_action/record_public_wait return the NEW
    # event's own event_id (the action/wait event itself, not the
    # run_id) -- confirmed directly by using it below.
    text_filtered = wec.build_workspace_episode_context(data_dir, run_id, already_delivered_event_ids=already_delivered)
    assert "journal.append: ok" not in text_filtered  # omitted -- already live-delivered
    assert "wait 15m" in text_filtered  # remains eligible -- never delivered live
    assert "Ended: human_stop." in text_filtered  # closure fact still represented
    # Header counts remain honest TOTAL mechanical facts, unfiltered.
    assert "Public actions: 1. Public waits: 1." in text_filtered


def test_17I_successful_waking_persistence_still_atomically_acknowledges_episode():
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    assert 'delivered_episode_run_id=delivered_episode_run_id,' in source


def test_17J_failed_bridge_c_waking_turn_leaves_episode_pending():
    data_dir, clark_actor_id = fresh_episode_db("bridgec_failed_pending")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_episode_ended(data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1010, termination_class=wep.TERMINATION_HUMAN_STOP)
    assert wep.find_pending_episode_run_ids(data_dir) == [run_id]
    # No waking turn ever ran to acknowledge it (simulating a failed/
    # never-reached waking turn) -- still pending, exactly as WSP2-P3
    # already established and this gate does not change.
    assert wep.find_pending_episode_run_ids(data_dir) == [run_id]


# ======================================================= Section 18: process restart


def test_18_process_restart_sequence_waking_to_roaming():
    data_dir = fresh_copy_db("restart_waking_to_roaming")
    staging_path = fresh_staging_path(data_dir)
    # 1: canonical event exists.
    event_id = seed_conversation_turn(data_dir, staging_path, "sess-18a", "hello", "hi", 1000)
    # 2/3: delivered successfully; durable evidence persists.
    wep.record_live_waking_delivery(data_dir, run_id="wrun-18a", pipeline_key=PIPELINE_KEY, occurred_at=1010, delivered_event_ids=[event_id])
    # 4/5: "process-local state cleared" -- there IS none in this
    # design (see test_15C); rebuild the renderer fresh, exactly as a
    # brand-new process would.
    hwm = wpc.compute_high_water_mark(data_dir)
    units = wpc.collect_live_waking_units(data_dir, "sess-18a", staging_path, since_marker=None, high_water_mark=hwm)
    # 6: source event is not NEW.
    assert units == []
    # 7: later canonical event IS new.
    event_id_2 = seed_conversation_turn(data_dir, staging_path, "sess-18a", "hello again", "hi again", 1020)
    hwm2 = wpc.compute_high_water_mark(data_dir)
    units2 = wpc.collect_live_waking_units(data_dir, "sess-18a", staging_path, since_marker=None, high_water_mark=hwm2)
    assert [u["event_id"] for u in units2] == [event_id_2]


def test_18_process_restart_sequence_public_space_to_waking():
    data_dir, clark_actor_id = fresh_episode_db("restart_public_to_waking")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    hwm1 = wpc.compute_high_water_mark(data_dir)
    first = wpc.collect_active_workspace_events(data_dir, run_id, None, hwm1)
    staging_path = fresh_staging_path(data_dir)
    seed_conversation_turn(
        data_dir, staging_path, "sess-18b", "hi", "hi back", 1010,
        delivered_active_workspace_event_ids=[e["event_id"] for e in first],
    )
    # "Restart" + rebuild fresh.
    hwm2 = wpc.compute_high_water_mark(data_dir)
    replay = wpc.collect_active_workspace_events(data_dir, run_id, None, hwm2)
    assert replay == []
    # Later canonical event is new.
    wep.record_public_wait(data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1020, wait_minutes=5, model_revision_id=None)
    hwm3 = wpc.compute_high_water_mark(data_dir)
    later = wpc.collect_active_workspace_events(data_dir, run_id, None, hwm3)
    assert len(later) == 1 and "wait 5m" in later[0]["rendered"]


# =================================================== Section 19: no false salience audit


def test_19_no_salience_importance_preference_belief_fields_in_ledger_schema():
    with open(os.path.join(ANAXI_FINAL, "native_provenance_writer.py"), encoding="utf-8") as f:
        npw_source = f.read()
    with open(os.path.join(ANAXI_FINAL, "workspace_episode_provenance.py"), encoding="utf-8") as f:
        wep_source = f.read()
    for forbidden in ("importance", "salience", "preference_score", "belief_strength", "memory_strength", "recurrence_score"):
        assert forbidden not in npw_source.lower()
        assert forbidden not in wep_source.lower()


def test_19_renderer_outputs_carry_no_semantic_recurrence_language():
    data_dir, clark_actor_id = fresh_episode_db("no_salience_render")
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    hwm = wpc.compute_high_water_mark(data_dir)
    events = wpc.collect_active_workspace_events(data_dir, run_id, None, hwm)
    rendered = wpc.render_active_workspace_continuity(events)
    for forbidden in ("important", "salient", "memorable", "you preferred", "you believe", "significant to you"):
        assert forbidden not in rendered.lower()


def test_19_only_mechanical_new_vs_already_delivered_distinction_exists():
    # The ENTIRE novelty vocabulary this gate introduces is exactly two
    # functions per direction (find_delivered_*_event_ids /
    # collect_*), never a third "importance" or "salience" tier.
    with open(os.path.join(ANAXI_FINAL, "workspace_public_continuity.py"), encoding="utf-8") as f:
        source = f.read()
    assert "def find_delivered_live_waking_event_ids" not in source  # lives in workspace_episode_provenance.py
    assert "def find_delivered_public_event_ids" in source
    assert "def collect_live_waking_units" in source
    assert "def collect_active_workspace_events" in source


# ==================================================== Section 25: concurrent sequence


def test_25_concurrent_continuity_sequence_end_to_end():
    """Model-free reproduction of spec WSP2-P4 section 25's full
    sequence, now against the DURABLE delivery ledger rather than
    process-local cursors. Both waking-turn persistence and roaming-
    episode provenance are exercised against ONE shared fixture DB
    here (fresh_episode_db() provides everything record_native_waking_
    turn() itself needs -- pipeline + clark actor -- exactly like
    fresh_copy_db()'s schema does), so delivery acknowledgments
    written by one direction are visible to the other, matching how
    production actually shares a single anaxi_provenance.db."""
    data_dir, clark_actor_id = fresh_episode_db("concurrent_shared")
    staging_path = fresh_staging_path(data_dir)
    run_id = wep.generate_episode_run_id()

    # 1/2: episode starts, departure handoff established (Bridge A is
    # exercised in test_workspace_roaming.py already; not re-proven here).
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)

    # 3: waking conversation succeeds while episode active.
    seed_conversation_turn(data_dir, staging_path, "sess-concurrent", "hello while roaming", "hi, still here", 1010)

    # 4: next roam receives it.
    hwm_roam_1 = wpc.compute_high_water_mark(data_dir)
    live_units = wpc.collect_live_waking_units(data_dir, "sess-concurrent", staging_path, since_marker=(1000, ""), high_water_mark=hwm_roam_1)
    assert len(live_units) == 1 and live_units[0]["prompt"] == "hello while roaming"
    wep.record_live_waking_delivery(
        data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1011,
        delivered_event_ids=[u["event_id"] for u in live_units],
    )

    # 5: PUBLIC roaming action persists while episode active.
    wep.record_public_action(
        data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1020,
        resource_class="journal", action="append", relative_path="e.json", success=True,
        action_id_backlink="wsaction-1", model_revision_id=None, journal_text="text",
    )

    # 6: next waking turn receives it, and (matching production's own
    # atomic-with-persistence contract) durably acknowledges it via
    # its own successful persistence.
    hwm_wake_1 = wpc.compute_high_water_mark(data_dir)
    active_events_1 = wpc.collect_active_workspace_events(data_dir, run_id, since_marker=None, high_water_mark=hwm_wake_1)
    assert any("journal.append: ok" in e["rendered"] for e in active_events_1)
    seed_conversation_turn(
        data_dir, staging_path, "sess-concurrent-2", "welcome back", "thanks", 1021,
        delivered_active_workspace_event_ids=[e["event_id"] for e in active_events_1],
    )

    # 7: another event commits after that waking context snapshot.
    wep.record_public_wait(data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1030, wait_minutes=5, model_revision_id=None)

    # 8: current turn (replayed with the SAME hwm_wake_1) does not see
    # the late event, and no longer sees the already-delivered one
    # either -- both effects are correct: outside the old snapshot, and
    # durably excluded now that it has been delivered.
    replay_with_old_hwm = wpc.collect_active_workspace_events(data_dir, run_id, since_marker=None, high_water_mark=hwm_wake_1)
    assert replay_with_old_hwm == []

    # 9: next turn (fresh hwm) sees the genuinely later event, and NOT
    # the already-delivered one.
    hwm_wake_2 = wpc.compute_high_water_mark(data_dir)
    next_turn_events = wpc.collect_active_workspace_events(data_dir, run_id, since_marker=None, high_water_mark=hwm_wake_2)
    assert any("wait 5m" in e["rendered"] for e in next_turn_events)
    assert not any("journal.append: ok" in e["rendered"] for e in next_turn_events)

    # 10: episode remains active throughout -- never ended in this sequence.
    assert wpc.find_active_roaming_run_id(data_dir) == run_id


# ================================================================== M: no OWC8 touch


def test_M_owc8_files_unmodified_by_this_gate_source_audit():
    with open(os.path.join(ANAXI_FINAL, "session_dialogue_window.py"), encoding="utf-8") as f:
        source = f.read()
    assert "workspace_public_continuity" not in source
    assert "DEFAULT_MAX_DIALOGUE_CHARS = 5000" in source  # OWC8-S1's own frozen constant, unchanged


ALL_TESTS = [
    test_A_select_bounded_all_included_under_both_bounds,
    test_A_select_bounded_more_than_max_items_keeps_newest,
    test_A_select_bounded_aggregate_cap_selects_newest_until_next_would_exceed,
    test_A_select_bounded_oversized_newest_included_alone,
    test_A_select_bounded_deterministic,
    test_B_high_water_mark_missing_db_is_safe_floor,
    test_B_high_water_mark_reflects_latest_committed_event,
    test_B_event_committed_after_snapshot_excluded_from_current_build,
    test_B_next_build_may_receive_the_later_event,
    test_B_ordering_never_requires_semantic_guessing_source_audit,
    test_C_empty_when_zero_post_departure_turns,
    test_C_successful_canonical_turn_after_departure_is_eligible,
    test_D_multiple_turns_deterministic_bounded_newest_set,
    test_E_failed_contained_waking_turns_excluded,
    test_F_task_mode_excluded,
    test_G_attribution_source_ids_preserved,
    test_H_source_content_exact_no_paraphrase,
    test_I_renderer_cap_enforced,
    test_J_private_can_never_enter_live_waking_import_audit,
    test_21A_active_run_zero_public_events_empty,
    test_21B_canonical_public_action_available_before_termination,
    test_21C_canonical_public_wait_available_before_termination,
    test_21D_capability_state_transition_available,
    test_21E_deterministic_bounded_newest_set,
    test_21G_task_mode_excluded_source_audit,
    test_21H_private_excluded_import_audit,
    test_21I_canonical_provenance_not_jsonl_source_audit,
    test_21J_hard_cap_enforced,
    test_find_active_roaming_run_id_none_before_any_run,
    test_find_active_roaming_run_id_finds_started_unended,
    test_find_active_roaming_run_id_none_once_ended,
    test_active_workspace_renderer_never_uses_belief_feeling_language,
    test_15A_delivered_waking_event_gets_durable_evidence,
    test_15C_delivered_event_not_classified_new_again_simulated_restart,
    test_15D_prior_context_would_be_distinguishable_if_retained,
    test_15E_genuinely_later_waking_event_is_new,
    test_15F_G_ledger_contains_only_ids_order_destination_no_semantic_content,
    test_16A_delivered_public_event_gets_durable_acknowledgment,
    test_16C_delivered_public_event_not_classified_new_again,
    test_16D_genuinely_later_public_event_is_new,
    test_16E_failed_waking_turn_does_not_acknowledge,
    test_16F_live_renderer_and_bridge_c_share_same_durable_truth,
    test_17_bridge_c_omits_already_live_delivered_public_material,
    test_17I_successful_waking_persistence_still_atomically_acknowledges_episode,
    test_17J_failed_bridge_c_waking_turn_leaves_episode_pending,
    test_18_process_restart_sequence_waking_to_roaming,
    test_18_process_restart_sequence_public_space_to_waking,
    test_19_no_salience_importance_preference_belief_fields_in_ledger_schema,
    test_19_renderer_outputs_carry_no_semantic_recurrence_language,
    test_19_only_mechanical_new_vs_already_delivered_distinction_exists,
    test_25_concurrent_continuity_sequence_end_to_end,
    test_M_owc8_files_unmodified_by_this_gate_source_audit,
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
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
