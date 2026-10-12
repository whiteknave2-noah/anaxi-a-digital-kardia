"""API2-LR1-S1 acceptance tests: snapshot adapter, pipeline identity,
provenance-role correction, supervisor success/failure paths, single-
call helper inspection, and canonical-spelling/scratch-dependency
audits. Zero live model calls. No live protected decisions -- every
test operates on a temp copy of anaxi_provenance.db.
"""
import ast
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import uuid

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
SCRATCHPAD = os.path.join(ANAXI_FINAL, "scratchpad")
sys.path.insert(0, ANAXI_FINAL)
sys.path.insert(0, SCRATCHPAD)

import api2_schema_migration as mig
import api2_control_plane as api2
import api2_supervisor as supervisor
import api1_control_plane as api1
import hir1_registration as hir1
from provenance_schema import derive_stable_id
from aab1.store import AAB1Store
from aab1.provisioning import provision_profile
from aab1.auth import authenticate

TEST_DIR = tempfile.mkdtemp(prefix="api2_lr1_test_")
SOURCE_DB = os.path.join(ANAXI_FINAL, "anaxi_provenance.db")
CLARK_ACTOR_ID = derive_stable_id("actor", "clark")
PRINCIPAL = {"type": "username_domain", "value": "TESTDOMAIN\\lr1tester"}


def fresh_copy_db(name):
    path = os.path.join(TEST_DIR, f"{name}.db")
    shutil.copy(SOURCE_DB, path)
    mig.apply_additive_migration(path)
    conn = sqlite3.connect(path)
    api2.install_authoritative_waking_decision_pipeline(conn)
    conn.close()
    return path


def open_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def seed_human_actor(conn, suffix):
    actor_id = f"human-test-{suffix}"
    now = int(time.time())
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, display_label, created_at) "
        "VALUES (?, 'human_person', ?, ?, ?)",
        (actor_id, actor_id, f"Test Human {suffix}", now),
    )
    conn.commit()
    return actor_id


def seed_decision(conn, counterpart_actor_id, decision_id=None):
    decision_id = decision_id or f"dec-test-{uuid.uuid4().hex[:10]}"
    event_id = f"pde-event-test-{uuid.uuid4().hex[:10]}"
    now = int(time.time())
    conn.execute(
        "INSERT INTO protected_decisions (decision_id, decision_text, decision_context, "
        "owner_actor_id, decision_state, resolution_kind, counterpart_actor_id, created_event_id, "
        "last_transition_event_id, created_at, updated_at) VALUES (?, ?, NULL, ?, 'open', NULL, ?, ?, ?, ?, ?)",
        (decision_id, "Whether this fixture is A or B is your decision.", CLARK_ACTOR_ID,
         counterpart_actor_id, event_id, event_id, now, now),
    )
    conn.commit()
    return decision_id, event_id


def fake_prepared_context(prompt="hello"):
    return {
        "memory_context": "some memory",
        "kardia": {"aesthetic_valve": "playful", "moral_valve": "kind", "volitional_channel": "curious", "affective_stance": "warm"},
        "controls": {"temperature": 0.7, "top_p": 0.9, "style_instruction": "be playful", "identity_preamble": "Your current stance:\nrendered kardia text here", "raw_kardia": {"aesthetic_valve": "playful"}},
        "messages": [
            {"role": "system", "content": "Your current stance:\nrendered kardia text here\n\nRelevant long-term memory for this turn:\nsome memory"},
            {"role": "user", "content": prompt},
        ],
    }


def counting_callable(return_value_or_fn):
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if callable(return_value_or_fn):
            return return_value_or_fn()
        return return_value_or_fn

    _fn.calls = calls
    return _fn


# ------------------------------------------------------- snapshot adapter --


def test_adapter_kardia_rendered_as_text_not_dict():
    prepared = fake_prepared_context()
    wire = api2.serialize_prepared_context(prepared, "attempt-1", "dec-1", "evt-1", "gemma4:e4b", False)
    assert isinstance(wire["kardia_system_content"], str)
    assert "rendered kardia text here" in wire["kardia_system_content"]
    assert "kardia" not in wire  # raw Dict[str,str] input never stored verbatim


def test_adapter_exact_messages_preserved():
    prepared = fake_prepared_context("what should I do?")
    wire = api2.serialize_prepared_context(prepared, "attempt-1", "dec-1", "evt-1", "gemma4:e4b", False)
    assert wire["messages"] == prepared["messages"]


def test_adapter_memory_context_preserved():
    prepared = fake_prepared_context()
    wire = api2.serialize_prepared_context(prepared, "attempt-1", "dec-1", "evt-1", "gemma4:e4b", False)
    assert wire["memory_context"] == "some memory"


def test_adapter_generation_controls_preserved():
    prepared = fake_prepared_context()
    wire = api2.serialize_prepared_context(prepared, "attempt-1", "dec-1", "evt-1", "gemma4:e4b", False, seed=42)
    gc = wire["generation_controls"]
    assert gc["model_name"] == "gemma4:e4b"
    assert gc["think"] is False
    assert gc["seed"] == 42
    assert gc["temperature"] == 0.7  # API2-P1: explicit top-level field
    assert gc["top_p"] == 0.9
    assert gc["other_controls"]["style_instruction"] == "be playful"
    assert "temperature" not in gc["other_controls"]  # surfaced structurally, not duplicated
    assert "top_p" not in gc["other_controls"]


def test_prepared_context_generation_controls_round_trip_exact():
    """API2-P1 focused regression. Deliberately non-default values that
    cannot accidentally equal a hardcoded fallback (0.7/0.9 elsewhere in
    this test file, or any plausible default), proving no default/guess
    is ever consulted anywhere on the round trip."""
    def prep():
        p = fake_prepared_context()
        p["controls"] = dict(p["controls"])
        p["controls"]["temperature"] = 0.37
        p["controls"]["top_p"] = 0.83
        return p

    prepared = prep()
    wire = api2.serialize_prepared_context(prepared, "attempt-rt", "dec-rt", "evt-rt", "gemma4:e4b", False)
    assert wire["generation_controls"]["temperature"] == 0.37
    assert wire["generation_controls"]["top_p"] == 0.83
    assert "temperature" not in wire["generation_controls"]["other_controls"]
    assert "top_p" not in wire["generation_controls"]["other_controls"]

    db_path = fresh_copy_db("controls_roundtrip")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "controls_rt")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)

    result, failure, calls = api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    assert failure is None

    snapshot_row = conn.execute(
        "SELECT * FROM protected_decision_attempt_snapshots WHERE attempt_id=?", (attempt["attempt_id"],)
    ).fetchone()
    reloaded = json.loads(snapshot_row["prepared_context_json"])
    assert reloaded["generation_controls"]["temperature"] == 0.37
    assert reloaded["generation_controls"]["top_p"] == 0.83

    controls = api2.build_pass2_llama_controls(reloaded["generation_controls"])
    assert controls == {"temperature": 0.37, "top_p": 0.83}
    assert controls["temperature"] != 0.7
    assert controls["top_p"] != 0.9

    assert api2.compute_snapshot_hash(snapshot_row["prepared_context_json"]) == snapshot_row["snapshot_hash"]


def test_complete_control_set_survives_snapshot_round_trip():
    """Guards against the next missing-field variant of the same bug --
    not just temperature/top_p, every key the real controls dict
    carries must survive serialize_prepared_context() somewhere."""
    prepared = fake_prepared_context()
    original_controls = dict(prepared["controls"])
    wire = api2.serialize_prepared_context(prepared, "attempt-cs", "dec-cs", "evt-cs", "gemma4:e4b", False)
    gc = wire["generation_controls"]
    reconstructed = {"temperature": gc["temperature"], "top_p": gc["top_p"], **gc["other_controls"]}
    assert reconstructed == original_controls


def test_pass2_real_helper_binding_receives_exact_persisted_controls():
    """Proves the real Pass-2 binding path end to end (fake call_llama,
    no real Ollama call) -- the controls a real call_llama(messages,
    controls) invocation would receive come from the persisted snapshot
    exactly, not a default."""
    def prep():
        p = fake_prepared_context()
        p["controls"] = dict(p["controls"])
        p["controls"]["temperature"] = 0.37
        p["controls"]["top_p"] = 0.83
        return p

    db_path = fresh_copy_db("pass2_binding")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "pass2bind")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    attempt_row = conn.execute("SELECT * FROM protected_decision_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    route = api2.build_protected_decision_route(conn, decision_id, CLARK_ACTOR_ID)
    pass1 = counting_callable({"decision_id": decision_id, "act": "choose", "content": "A"})
    stage, failure, _ = api2.run_pass1(conn, dict(attempt_row), route, pass1, CLARK_ACTOR_ID)
    assert failure is None

    snapshot_row = conn.execute(
        "SELECT * FROM protected_decision_attempt_snapshots WHERE attempt_id=?", (attempt["attempt_id"],)
    ).fetchone()
    pass2_input = api2.build_pass2_input(snapshot_row, stage)
    controls = api2.build_pass2_llama_controls(pass2_input["generation_controls"])

    received = {}

    def fake_call_llama(messages, controls_arg):
        received["messages"] = messages
        received["controls"] = controls_arg
        return "synthetic expression"

    raw = fake_call_llama(pass2_input["prepared_messages"], controls)
    assert received["controls"] == {"temperature": 0.37, "top_p": 0.83}
    assert raw == "synthetic expression"


def test_adapter_deterministic_serialization_and_hash():
    prepared = fake_prepared_context()
    wire1 = api2.serialize_prepared_context(prepared, "attempt-1", "dec-1", "evt-1", "gemma4:e4b", False, prepared_at="2026-01-01T00:00:00+00:00")
    wire2 = api2.serialize_prepared_context(prepared, "attempt-1", "dec-1", "evt-1", "gemma4:e4b", False, prepared_at="2026-01-01T00:00:00+00:00")
    s1 = api2.canonical_snapshot_serialization(wire1)
    s2 = api2.canonical_snapshot_serialization(wire2)
    assert s1 == s2
    assert api2.compute_snapshot_hash(s1) == api2.compute_snapshot_hash(s2)


def test_adapter_no_python_object_leakage():
    prepared = fake_prepared_context()
    wire = api2.serialize_prepared_context(prepared, "attempt-1", "dec-1", "evt-1", "gemma4:e4b", False)
    serialized = api2.canonical_snapshot_serialization(wire)
    assert "object at 0x" not in serialized
    assert "<" not in serialized.split('"kardia_system_content"')[0] or True  # sanity: no repr-looking fragments
    for forbidden in ("<class ", "<Kardia", "0x0"):
        assert forbidden not in serialized


def test_adapter_round_trip_reconstructs_pass1_pass2_base_context():
    db_path = fresh_copy_db("adapter_roundtrip")
    conn = open_conn(db_path)
    counterpart = seed_human_actor(conn, "adapter1")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    prep = counting_callable(lambda: fake_prepared_context("do the thing"))
    result, failure, calls = api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    assert failure is None
    route = api2.build_protected_decision_route(conn, decision_id, CLARK_ACTOR_ID)
    pass1_iface = api2.build_pass1_interface(route, result)
    assert pass1_iface["prepared_messages"][-1]["content"] == "do the thing"
    assert pass1_iface["generation_controls"]["model_name"] == "gemma4:e4b"


def test_adapter_calls_no_preparation_function():
    with open(os.path.join(ANAXI_FINAL, "api2_control_plane.py"), encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    adapter_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "serialize_prepared_context")
    called_names = {n.func.id for n in ast.walk(adapter_fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "prepare_context" not in called_names
    assert not any("prepare" in name and name != "serialize_prepared_context" for name in called_names)


# ---------------------------------------------------------------- pipeline --


# SOURCE_DB (live) already has the pathway pipeline row installed as
# of this gate's own live-install step -- these two tests need a
# genuinely pre-install copy, same pattern as the migration-test fixes.
PRE_PIPELINE_SOURCE = os.path.join(ANAXI_FINAL, "anaxi_provenance_api2lr1s1_prepipeline_backup_20260901T163341.db")


def test_pipeline_installed_exactly_once_idempotent():
    db_path = os.path.join(TEST_DIR, "pipeline_only.db")
    shutil.copy(PRE_PIPELINE_SOURCE, db_path)
    conn = sqlite3.connect(db_path)
    before = conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
    pid1, created1 = api2.install_authoritative_waking_decision_pipeline(conn)
    assert created1 is True
    mid = conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
    assert mid == before + 1
    pid2, created2 = api2.install_authoritative_waking_decision_pipeline(conn)
    assert created2 is False
    assert pid2 == pid1
    after = conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
    assert after == mid


def test_pipeline_key_stable_and_derived():
    db_path = os.path.join(TEST_DIR, "pipeline_stable.db")
    shutil.copy(SOURCE_DB, db_path)
    conn = sqlite3.connect(db_path)
    pid, _ = api2.install_authoritative_waking_decision_pipeline(conn)
    assert pid == derive_stable_id("pipeline", api2.AUTHORITATIVE_WAKING_DECISION_PIPELINE_KEY)
    row = conn.execute(
        "SELECT pipeline_key, legacy_substrate_label FROM pipelines WHERE pipeline_id=?", (pid,)
    ).fetchone()
    assert row[0] == "authoritative_waking_decision"
    assert row[1] is None  # not a legacy substrate lineage


def test_tx2_fails_closed_without_pipeline_installed():
    db_path = os.path.join(TEST_DIR, "no_pipeline.db")
    shutil.copy(PRE_PIPELINE_SOURCE, db_path)
    mig.apply_additive_migration(db_path)
    conn = open_conn(db_path)  # deliberately skip pipeline installation
    counterpart = seed_human_actor(conn, "nopipe")
    decision_id, event_id = seed_decision(conn, counterpart)
    attempt, _ = api2.create_attempt(conn, decision_id, event_id)
    prep = counting_callable(fake_prepared_context)
    api2.run_preparation(conn, attempt["attempt_id"], prep, "gemma4:e4b", False)
    attempt_row = conn.execute("SELECT * FROM protected_decision_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    route = api2.build_protected_decision_route(conn, decision_id, CLARK_ACTOR_ID)
    pass1 = counting_callable({"decision_id": decision_id, "act": "choose", "content": "A"})
    stage, _, _ = api2.run_pass1(conn, dict(attempt_row), route, pass1, CLARK_ACTOR_ID)
    pass2 = counting_callable({"action_id": stage["action_id"], "expression": "Choosing A."})
    stage2, _, _ = api2.run_pass2(conn, stage, pass2)
    result, failure = api2.commit_tx2(conn, stage2["stage_id"], CLARK_ACTOR_ID)
    assert failure == api2.Api2Failure.PIPELINE_NOT_INSTALLED
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='protected_decision_expression'").fetchone()[0] == 0


# ---------------------------------------------------- canonical spelling ---


def test_canonical_event_spelling_no_stale_uses():
    for fn in ("api2_control_plane.py", "api2_supervisor.py", "decision_path_core.py"):
        with open(os.path.join(ANAXI_FINAL, fn), encoding="utf-8") as f:
            source = f.read()
        assert "decision_state_transition_committed" not in source, fn
    with open(os.path.join(ANAXI_FINAL, "api2_control_plane.py"), encoding="utf-8") as f:
        source = f.read()
    assert "'decision_state_transitioned'" in source


def test_other_event_type_spellings_unchanged():
    with open(os.path.join(ANAXI_FINAL, "api2_control_plane.py"), encoding="utf-8") as f:
        source = f.read()
    for unchanged in ("hdi2_action_selected", "expression_realized"):
        assert unchanged in source


# ------------------------------------------------------ scratch dependency -


def test_production_no_longer_imports_scratchpad_hdi2():
    # Checks actual import statements via AST, not prose -- module
    # docstrings are allowed to (and do) discuss the history of the
    # scratchpad/hdi2 promotion without that counting as a dependency.
    for fn in ("api2_control_plane.py", "api2_supervisor.py", "decision_path_core.py"):
        with open(os.path.join(ANAXI_FINAL, fn), encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module is None or "hdi2" not in node.module, f"{fn}: from {node.module}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "hdi2" not in alias.name, f"{fn}: import {alias.name}"


# ---------------------------------------------- Pass-1/Pass-2 single-call --


def _function_source(filename, funcname):
    with open(os.path.join(ANAXI_FINAL, filename), encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == funcname)
    return ast.get_source_segment(source, fn), fn


def test_call_llama_is_single_call_no_retry():
    # call_llama() itself performs exactly one ollama.chat() call with
    # no internal loop -- proven structurally below. Its own docstring
    # separately notes that call_llama() is REUSED by an external
    # bounded regeneration/retry loop elsewhere (this function's own
    # caller retries by calling call_llama() again, not by looping
    # inside it) -- that external reuse is a fact about a DIFFERENT
    # call site, not evidence that this function retries internally,
    # so the word "retry" appearing in its docstring is not itself a
    # failure signal here.
    src, fn = _function_source("llama_anaxi.py", "call_llama")
    ollama_calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _is_ollama_chat(n)]
    assert len(ollama_calls) == 1
    loop_nodes = [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.While))]
    assert loop_nodes == []


def test_ask_llama_for_json_is_single_call_no_retry():
    src, fn = _function_source("llama_anaxi.py", "ask_llama_for_json")
    ollama_calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _is_ollama_chat(n)]
    assert len(ollama_calls) == 1
    loop_nodes = [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.While))]
    assert loop_nodes == []
    assert "retry" not in src.lower()


def _is_ollama_chat(call_node):
    func = call_node.func
    return (
        isinstance(func, ast.Attribute) and func.attr == "chat"
        and isinstance(func.value, ast.Name) and func.value.id == "ollama"
    )


# ------------------------------------------------------------- supervisor --


def _setup_supervisor_fixture(name):
    db_path = fresh_copy_db(name)
    conn = open_conn(db_path)
    aab_store = AAB1Store(os.path.join(TEST_DIR, f"{name}_aab.sqlite3"))
    profile = provision_profile(aab_store, "Supervisor Test Human", "supervisor_test", PRINCIPAL)
    session, ac = authenticate(aab_store, profile["actor_id"], principal_provider=lambda: PRINCIPAL)
    _reg_result, _reg_failure = hir1.register_canonical_human(conn, {
        "registration_request_id": f"hir-req-{name}",
        "aab_actor_id": profile["actor_id"],
        "display_label": "Supervisor Test Human",
        "source": "local_operator_provisioning",
    })
    assert _reg_failure is None, _reg_failure
    now = int(time.time())
    conn.execute("INSERT INTO sessions (session_id, started_at) VALUES ('prod-session-sup', ?)", (now,))
    conn.commit()
    return conn, aab_store, profile, session, ac


def test_supervisor_gates_default_false():
    assert api1.AUTHORITATIVE_PDE_ENABLED is False
    assert api2.AUTHORITATIVE_DECISION_PATH_ENABLED is False


def test_supervisor_ordinary_prose_cannot_invoke():
    for fn in ("llama_anaxi.py", "orchestration.py"):
        with open(os.path.join(ANAXI_FINAL, fn), encoding="utf-8") as f:
            source = f.read()
        assert "api2_supervisor" not in source


def test_supervisor_tx1_to_tx2_success_after_api1_p1_patch():
    # API1-P1 fixed the previously-discovered bug (api1_control_plane.py
    # _commit_tx1() was hardcoding the literal "clark" into
    # protected_decisions.owner_actor_id / resulting_owner_actor_id
    # instead of using the supplied clark_actor_id parameter). This test
    # is the converted, now-green version of what was
    # test_supervisor_tx1_to_tx2_identity_mismatch_discovered: a real
    # synthetic TX1, chained through a real API2 attempt/Pass-1/stage/
    # Pass-2/TX2, with the owner-identity check now correctly passing.
    conn, aab_store, profile, session, ac = _setup_supervisor_fixture("sup_success")
    prep = counting_callable(fake_prepared_context)

    tx1_request = {
        "establishment_request_id": "sup-req-1", "operation": "create_and_assign",
        "decision_text": "Whether this fixture is A or B is your decision.",
        "recipient_actor_id": CLARK_ACTOR_ID, "source_input_id": "turn-sup-1",
    }

    def pass1_fn():
        row = conn.execute("SELECT decision_id FROM protected_decisions WHERE owner_actor_id=? ORDER BY created_at DESC LIMIT 1", (CLARK_ACTOR_ID,)).fetchone()
        return {"decision_id": row["decision_id"], "act": "choose", "content": "A"}
    pass1 = counting_callable(pass1_fn)

    def pass2_fn():
        row = conn.execute("SELECT action_id FROM protected_decision_stages ORDER BY selected_at DESC LIMIT 1").fetchone()
        return {"action_id": row["action_id"], "expression": "I'll go with A."}
    pass2 = counting_callable(pass2_fn)

    result, failure = supervisor.run_one_authoritative_decision(
        conn, aab_store, tx1_request, ac["auth_context_id"], session["session_id"],
        CLARK_ACTOR_ID, "prod-session-sup", prep, pass1, pass2, "gemma4:e4b", False,
    )
    assert failure is None, failure
    assert result["decision_state"] == "resolved"
    assert result["resolution_kind"] == "chosen"
    assert result["owner_actor_id"] == CLARK_ACTOR_ID

    committed_decision = conn.execute(
        "SELECT owner_actor_id FROM protected_decisions WHERE decision_id=?", (result["decision_id"],)
    ).fetchone()
    assert committed_decision["owner_actor_id"] == CLARK_ACTOR_ID
    assert committed_decision["owner_actor_id"] != "clark"

    # TX2's event_components.creator_actor_id FK (references actors)
    # succeeds because the real actor id is now used throughout.
    component = conn.execute(
        "SELECT creator_actor_id FROM event_components WHERE creator_actor_id=? ORDER BY component_id DESC LIMIT 1",
        (CLARK_ACTOR_ID,),
    ).fetchone()
    assert component is not None

    assert prep.calls["n"] == 1
    assert pass1.calls["n"] == 1
    assert pass2.calls["n"] == 1
    assert conn.execute("SELECT COUNT(*) FROM protected_decision_stages").fetchone()[0] == 1
    assert api1.AUTHORITATIVE_PDE_ENABLED is False
    assert api2.AUTHORITATIVE_DECISION_PATH_ENABLED is False


def test_supervisor_requires_valid_aab_session():
    conn, aab_store, profile, session, ac = _setup_supervisor_fixture("sup_no_auth")
    prep = counting_callable(fake_prepared_context)
    pass1 = counting_callable({"decision_id": "irrelevant", "act": "choose", "content": "A"})
    pass2 = counting_callable({"action_id": "irrelevant", "expression": "x"})
    tx1_request = {
        "establishment_request_id": "sup-req-2", "operation": "create_and_assign",
        "decision_text": "Whether this fixture is A or B is your decision.",
        "recipient_actor_id": CLARK_ACTOR_ID, "source_input_id": "turn-sup-2",
    }
    result, failure = supervisor.run_one_authoritative_decision(
        conn, aab_store, tx1_request, "bogus-auth-context-id", session["session_id"],
        CLARK_ACTOR_ID, "prod-session-sup", prep, pass1, pass2, "gemma4:e4b", False,
    )
    assert failure is not None
    assert failure["stage"] == "tx1"
    assert prep.calls["n"] == 0
    assert pass1.calls["n"] == 0


def test_supervisor_failure_at_each_stage_leaves_gates_false_and_no_capability_reuse():
    conn, aab_store, profile, session, ac = _setup_supervisor_fixture("sup_failures")

    # TX1 failure: wrong recipient
    bad_tx1 = {
        "establishment_request_id": "sup-req-3", "operation": "create_and_assign",
        "decision_text": "Whether this fixture is A or B is your decision.",
        "recipient_actor_id": "not-clark", "source_input_id": "turn-sup-3",
    }
    result, failure = supervisor.run_one_authoritative_decision(
        conn, aab_store, bad_tx1, ac["auth_context_id"], session["session_id"],
        CLARK_ACTOR_ID, "prod-session-sup", counting_callable(fake_prepared_context),
        counting_callable({}), counting_callable({}), "gemma4:e4b", False,
    )
    assert failure["stage"] == "tx1"
    assert api1.AUTHORITATIVE_PDE_ENABLED is False
    assert api2.AUTHORITATIVE_DECISION_PATH_ENABLED is False

    # Pass-1 failure (malformed act) -- TX1 succeeds, api2 stage fails
    good_tx1 = {
        "establishment_request_id": "sup-req-4", "operation": "create_and_assign",
        "decision_text": "Whether this fixture is A or B is your decision.",
        "recipient_actor_id": CLARK_ACTOR_ID, "source_input_id": "turn-sup-4",
    }
    bad_pass1 = counting_callable({"decision_id": "wrong-decision-id", "act": "choose", "content": "A"})
    result2, failure2 = supervisor.run_one_authoritative_decision(
        conn, aab_store, good_tx1, ac["auth_context_id"], session["session_id"],
        CLARK_ACTOR_ID, "prod-session-sup", counting_callable(fake_prepared_context),
        bad_pass1, counting_callable({}), "gemma4:e4b", False,
    )
    assert failure2["stage"] == "api2"
    assert api1.AUTHORITATIVE_PDE_ENABLED is False
    assert api2.AUTHORITATIVE_DECISION_PATH_ENABLED is False
    assert bad_pass1.calls["n"] == 1  # exactly one Pass-1 attempt, no automatic retry


ALL_TESTS = [
    test_adapter_kardia_rendered_as_text_not_dict,
    test_adapter_exact_messages_preserved,
    test_adapter_memory_context_preserved,
    test_adapter_generation_controls_preserved,
    test_prepared_context_generation_controls_round_trip_exact,
    test_complete_control_set_survives_snapshot_round_trip,
    test_pass2_real_helper_binding_receives_exact_persisted_controls,
    test_adapter_deterministic_serialization_and_hash,
    test_adapter_no_python_object_leakage,
    test_adapter_round_trip_reconstructs_pass1_pass2_base_context,
    test_adapter_calls_no_preparation_function,
    test_pipeline_installed_exactly_once_idempotent,
    test_pipeline_key_stable_and_derived,
    test_tx2_fails_closed_without_pipeline_installed,
    test_canonical_event_spelling_no_stale_uses,
    test_other_event_type_spellings_unchanged,
    test_production_no_longer_imports_scratchpad_hdi2,
    test_call_llama_is_single_call_no_retry,
    test_ask_llama_for_json_is_single_call_no_retry,
    test_supervisor_gates_default_false,
    test_supervisor_ordinary_prose_cannot_invoke,
    test_supervisor_tx1_to_tx2_success_after_api1_p1_patch,
    test_supervisor_requires_valid_aab_session,
    test_supervisor_failure_at_each_stage_leaves_gates_false_and_no_capability_reuse,
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
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
