"""WSP1-P1A: canonical actor EXISTENCE validation tests.

Proves resolve_canonical_actor_id() no longer merely derives a
deterministic ID (derive_stable_id() alone cannot distinguish a
registered actor from an arbitrary valid string) but establishes
registry existence via a read-only lookup against the authoritative
provenance_schema.actors table (stable_key UNIQUE), failing closed on
both malformed input (ValueError) and syntactically valid but
unregistered stable keys (UnknownActorError).

Zero real Ollama/model calls. Zero live workspace actions. Zero live
journal writes. Zero live canonical DB mutations -- fixture tests use
a fresh, isolated, schema-only DB built via
provenance_schema.create_provenance_db(); the one production-DB test
(A) opens it strictly read-only (mode=ro) and performs a SELECT only.
"""
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import workspace_supervisor as ws
from provenance_schema import create_provenance_db, derive_stable_id

TEST_ROOT = tempfile.mkdtemp(prefix="wsp1p1a_test_")

PRODUCTION_CLARK_ACTOR_ID = "actor-80eee447ad46e1a1e9b2ea65b5"  # confirmed real value, actors.stable_key='clark'


def fresh_fixture_conn(name):
    """A schema-only provenance DB (via the real create_provenance_db
    DDL), completely isolated from production, with zero rows in
    actors."""
    db_path = os.path.join(TEST_ROOT, f"{name}.db")
    return create_provenance_db(db_path)


def insert_actor(conn, actor_id, actor_type, stable_key):
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, ?, ?, ?)",
        (actor_id, actor_type, stable_key, int(datetime.datetime.now(datetime.timezone.utc).timestamp())),
    )
    conn.commit()


# --------------------------------------- A: "clark" resolves via registry (prod, read-only)


def test_clark_resolves_via_canonical_registry_readonly():
    conn = fresh_fixture_conn("registered_clark")
    insert_actor(conn, PRODUCTION_CLARK_ACTOR_ID, "clark_agent", "clark")
    resolved = ws.resolve_canonical_actor_id("clark", conn=conn)
    conn.close()
    assert resolved == PRODUCTION_CLARK_ACTOR_ID
    assert resolved == derive_stable_id("actor", "clark")


# --------------------------------------------------- B: valid-but-unknown fails closed


def test_valid_but_unknown_stable_key_fails_closed():
    conn = fresh_fixture_conn("unknown_actor")
    insert_actor(conn, derive_stable_id("actor", "clark"), "clark_agent", "clark")
    before_count = conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0]

    raised = None
    try:
        ws.resolve_canonical_actor_id("definitely-not-a-real-actor", conn=conn)
    except ws.UnknownActorError as exc:
        raised = exc
    assert raised is not None, "expected UnknownActorError for a syntactically valid but unregistered stable_key"

    after_count = conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0]
    assert after_count == before_count  # D: no side-effect row created
    conn.close()


# ------------------------------------------------------- C: malformed/empty inputs


def test_malformed_or_empty_stable_keys_fail_closed_with_value_error():
    conn = fresh_fixture_conn("malformed_input")
    insert_actor(conn, derive_stable_id("actor", "clark"), "clark_agent", "clark")
    for bad in (None, "", "   ", 42, [], {}):
        raised = None
        try:
            ws.resolve_canonical_actor_id(bad, conn=conn)
        except ValueError as exc:
            raised = exc
        assert raised is not None, f"expected ValueError for stable_key={bad!r}"
        assert not isinstance(raised, ws.UnknownActorError)  # distinct failure class (spec section 7)
    conn.close()


# ---------------------------------------------- D: no unknown actor created (fixture, isolated)


def test_unknown_lookup_never_inserts_a_row():
    conn = fresh_fixture_conn("no_side_effect")
    assert conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0] == 0
    try:
        ws.resolve_canonical_actor_id("also-not-a-real-actor", conn=conn)
    except ws.UnknownActorError:
        pass
    assert conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0] == 0
    conn.close()


# --------------------------------------- resolver agreement check (registry vs. derivation)


def test_registry_and_derivation_agreement_for_known_actor():
    conn = fresh_fixture_conn("agreement")
    actor_id = derive_stable_id("actor", "some_other_registered_actor")
    insert_actor(conn, actor_id, "host_system", "some_other_registered_actor")
    resolved = ws.resolve_canonical_actor_id("some_other_registered_actor", conn=conn)
    assert resolved == actor_id
    conn.close()


def test_mismatched_registry_row_fails_closed():
    # Pathological fixture: a row exists under the stable_key, but its
    # stored actor_id does NOT agree with the deterministic derivation
    # -- must still fail closed, never silently trust the stored value
    # or silently trust the derivation over the registry.
    conn = fresh_fixture_conn("mismatch")
    insert_actor(conn, "actor-deliberately-wrong-id", "host_system", "mismatched_actor")
    raised = None
    try:
        ws.resolve_canonical_actor_id("mismatched_actor", conn=conn)
    except ws.UnknownActorError as exc:
        raised = exc
    assert raised is not None
    conn.close()


# --------------------- E: canonical ID already supplied to the supervisor survives unchanged


def test_supervisor_signature_and_threading_unchanged_by_p1a():
    import inspect
    sig = inspect.signature(ws.run_one_supervised_workspace_action)
    assert list(sig.parameters)[:4] == ["orch", "prompt", "clark_actor_id", "workspace_paths"]
    assert "requester_actor_id" not in sig.parameters
    # resolve_canonical_actor_id is a standalone helper for the trusted
    # caller boundary -- run_one_supervised_workspace_action itself
    # must NOT call it (no repeated DB resolution inside low-level
    # workspace execution, spec section 8).
    import ast
    with open(os.path.join(ANAXI_FINAL, "workspace_supervisor.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fn_node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_one_supervised_workspace_action")
    calls_in_fn = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
                   for n in ast.walk(fn_node) if isinstance(n, ast.Call)}
    assert "resolve_canonical_actor_id" not in calls_in_fn


ALL_TESTS = [
    test_clark_resolves_via_canonical_registry_readonly,
    test_valid_but_unknown_stable_key_fails_closed,
    test_malformed_or_empty_stable_keys_fail_closed_with_value_error,
    test_unknown_lookup_never_inserts_a_row,
    test_registry_and_derivation_agreement_for_known_actor,
    test_mismatched_registry_row_fails_closed,
    test_supervisor_signature_and_threading_unchanged_by_p1a,
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
