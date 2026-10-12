"""WSP1-P1: canonical Clark workspace attribution correction tests.

Proves the fix to workspace_supervisor.py's dead clark_actor_id
parameter / wrongly-defaulted requester_actor_id="clark" bug (spec
section 2 root cause). Zero real Ollama/model calls, zero live
workspace actions, zero new journal entries -- every test below uses
either a pure function call or a temp workspace via fresh_paths(),
never anaxi_final/workspace/.
"""
import os
import shutil
import sys
import tempfile
import traceback
import sqlite3

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import workspace_capability as wc
import workspace_direction as wd
import workspace_supervisor as ws
from provenance_schema import create_provenance_db, derive_stable_id

TEST_ROOT = tempfile.mkdtemp(prefix="wsp1p1_test_")

PRODUCTION_CLARK_ACTOR_ID = "actor-80eee447ad46e1a1e9b2ea65b5"  # confirmed real value, actors.stable_key='clark'
ARBITRARY_OPAQUE_ID = "actor-ffffffffffffffffffffffffff-not-clark-not-production"


def fresh_paths(name):
    root = os.path.join(TEST_ROOT, name)
    paths = wc.WorkspacePaths(root=root)
    paths.ensure_exists()
    return paths


# --------------------------------------------------------- A: canonical resolver


def test_resolver_returns_production_clark_actor_id():
    path = os.path.join(TEST_ROOT, "resolver.db")
    conn = create_provenance_db(path)
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
        "VALUES (?, 'clark_agent', 'clark', 1000)",
        (PRODUCTION_CLARK_ACTOR_ID,),
    )
    conn.commit()
    resolved = ws.resolve_canonical_actor_id("clark", conn=conn)
    conn.close()
    assert resolved == derive_stable_id("actor", "clark")
    assert resolved == PRODUCTION_CLARK_ACTOR_ID


# ---------------------------------------------- B: future action log opaque ID


def test_future_action_log_uses_opaque_canonical_actor_id():
    paths = fresh_paths("future_action_log")
    action = {"resource_class": "library", "action": "list", "relative_path": "", "content": ""}
    validated, _ = wd.validate_pass1_workspace_action(action)
    wd.execute_workspace_action(paths, validated, PRODUCTION_CLARK_ACTOR_ID)
    log = wc.query_action_log(paths)
    assert len(log) == 1
    assert log[0]["requester_actor_id"] == PRODUCTION_CLARK_ACTOR_ID


# ------------------------------------------------ C: future journal append


def test_future_journal_append_uses_opaque_canonical_actor_id():
    paths = fresh_paths("future_journal")
    action = {"resource_class": "journal", "action": "append", "relative_path": "", "content": "A test note."}
    validated, _ = wd.validate_pass1_workspace_action(action)
    boundary, performed = wd.execute_workspace_action(paths, validated, PRODUCTION_CLARK_ACTOR_ID)
    assert performed is True
    assert boundary["result"]["author_actor_id"] == PRODUCTION_CLARK_ACTOR_ID


# ------------------------------------------------------- D: no literal "clark"


def test_literal_clark_never_written_to_attribution_fields():
    paths = fresh_paths("no_literal_clark")
    action = {"resource_class": "journal", "action": "append", "relative_path": "", "content": "Another note."}
    validated, _ = wd.validate_pass1_workspace_action(action)
    boundary, _performed = wd.execute_workspace_action(paths, validated, PRODUCTION_CLARK_ACTOR_ID)
    assert boundary["result"]["author_actor_id"] != "clark"
    log = wc.query_action_log(paths)
    assert all(rec["requester_actor_id"] != "clark" for rec in log)


# --------------------------------------------------- E: arbitrary opaque ID


def test_arbitrary_opaque_actor_id_survives_unchanged():
    paths = fresh_paths("arbitrary_opaque")
    action = {"resource_class": "journal", "action": "append", "relative_path": "", "content": "Third note."}
    validated, _ = wd.validate_pass1_workspace_action(action)
    boundary, performed = wd.execute_workspace_action(paths, validated, ARBITRARY_OPAQUE_ID)
    assert performed is True
    assert boundary["result"]["author_actor_id"] == ARBITRARY_OPAQUE_ID
    log = wc.query_action_log(paths)
    assert log[-1]["requester_actor_id"] == ARBITRARY_OPAQUE_ID
    # code must not special-case one production ID -- a second, distinct
    # REGISTERED stable_key (the host actor) resolves through the same
    # mechanism as "clark" does (WSP1-P1A: resolve_canonical_actor_id now
    # requires registry existence, so this must be a real registered
    # actor, not an arbitrary string -- see test_workspace_actor_resolution.py
    # for the dedicated unknown-actor/malformed-input coverage).
    db_path = os.path.join(TEST_ROOT, "host_resolver.db")
    conn = create_provenance_db(db_path)
    host_id = derive_stable_id("actor", "bounded_clause_renderer")
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
        "VALUES (?, 'host_system', 'bounded_clause_renderer', 1000)",
        (host_id,),
    )
    conn.commit()
    assert ws.resolve_canonical_actor_id("bounded_clause_renderer", conn=conn) == host_id
    conn.close()


# ------------------------------------------------------- F: fails closed


def test_unresolvable_actor_fails_closed():
    for bad in (None, "", "   ", 42, [], {}):
        raised = None
        try:
            ws.resolve_canonical_actor_id(bad)
        except ValueError as exc:
            raised = exc
        assert raised is not None, f"expected ValueError for stable_key={bad!r}"


# --------------------------------------------- G: no new workspace action executed
# (structural -- every test above targets fresh_paths()/TEST_ROOT, never the
# real anaxi_final/workspace/ tree; asserted directly here.)


def test_no_test_touches_real_workspace_root():
    real_workspace_root = os.path.join(ANAXI_FINAL, "workspace")
    assert not TEST_ROOT.startswith(real_workspace_root)
    real_paths = wc.WorkspacePaths.production_defaults()
    assert real_paths.root != TEST_ROOT
    assert not real_paths.root.startswith(TEST_ROOT)


# -------------------------------------------- H: supervisor signature regression


def test_supervisor_signature_has_no_redundant_requester_param():
    import inspect
    sig = inspect.signature(ws.run_one_supervised_workspace_action)
    params = list(sig.parameters)
    assert params[:4] == ["orch", "prompt", "clark_actor_id", "workspace_paths"]
    assert "requester_actor_id" not in sig.parameters


def test_execute_workspace_action_unchanged_dispatch_behavior():
    # WSP1-S1/S2 capability/action-selection behavior itself is untouched:
    # same allowed surface, same boundary/consequence/rationale shape.
    paths = fresh_paths("regression_dispatch")
    action = {"resource_class": "library", "action": "delete_everything", "relative_path": "", "content": ""}
    validated, failure = wd.validate_pass1_workspace_action(action)
    assert validated is None
    assert failure == wd.DirectionFailure.NOT_IN_ALLOWED_SURFACE


# ----------------------------------------------- I: API1/API2 noninterference


def test_api1_api2_sources_unchanged_by_this_patch():
    for fn in ("api1_control_plane.py", "api2_supervisor.py"):
        path = os.path.join(ANAXI_FINAL, fn)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "workspace_supervisor" not in source
        assert "workspace_direction" not in source
        assert "workspace_capability" not in source


ALL_TESTS = [
    test_resolver_returns_production_clark_actor_id,
    test_future_action_log_uses_opaque_canonical_actor_id,
    test_future_journal_append_uses_opaque_canonical_actor_id,
    test_literal_clark_never_written_to_attribution_fields,
    test_arbitrary_opaque_actor_id_survives_unchanged,
    test_unresolvable_actor_fails_closed,
    test_no_test_touches_real_workspace_root,
    test_supervisor_signature_has_no_redundant_requester_param,
    test_execute_workspace_action_unchanged_dispatch_behavior,
    test_api1_api2_sources_unchanged_by_this_patch,
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
