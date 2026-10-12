"""SLP2 focused regression suite. Zero Ollama calls, zero inference, zero
external network, zero real Sleep. Every test operates against a FRESH
SYNTHETIC anaxi_provenance.db built by
provenance_schema.create_provenance_db() in a disposable temp
directory -- never the live file, never production data.

Run from repository root: python3 -B anaxi_final/test_sleep_timing_knock.py
(also pytest-compatible: bare def test_*() functions).
"""
import inspect
import os
import sqlite3
import sys
import tempfile
import threading
import time
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

from provenance_schema import create_provenance_db
from migrate_historical_data import build_pipeline_map, seed_reference_data
import hir1_schema_migration
from hir1_registration import register_canonical_human
import slp2_schema_migration
import sleep_timing_knock as slp2
import conversation_direction as cd

TEST_DIR = tempfile.mkdtemp(prefix="slp2_test_")
_MANIFEST = {"pipelines": {
    "llama": {"routing_constant_value": "nate"},
    "claude": {"routing_constant_value": "nate"},
}}
_COUNTER = [0]


def _new_env(name):
    """Fresh synthetic data_dir with the full canonical schema, SLP2's
    additive migration, and reference-data (pipelines + clark_agent +
    host_system actors) seeded -- no human registered yet."""
    data_dir = os.path.join(TEST_DIR, name)
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    create_provenance_db(db_path).close()
    slp2_schema_migration.apply_additive_migration(db_path)
    hir1_schema_migration.apply_additive_migration(db_path)
    pipeline_map = build_pipeline_map(_MANIFEST)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    seed_reference_data(conn, pipeline_map, int(time.time()))
    conn.close()
    return data_dir


def _register_owner(data_dir, suffix):
    """Registers a genuine synthetic human owner via the REAL
    production writer (hir1_registration.py). Returns the actor_id.
    Never fabricates a human row directly."""
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    result, failure = register_canonical_human(conn, {
        "registration_request_id": f"slp2-test-reg-{suffix}",
        "aab_actor_id": f"actor-slp2-test-owner-{suffix}",
        "display_label": f"SLP2 Test Owner {suffix}",
        "source": "local_operator_provisioning",
    })
    conn.close()
    assert failure is None, failure
    return result["actor_id"]


def _next_id():
    _COUNTER[0] += 1
    return f"sess-{_COUNTER[0]}-{slp2.generate_sleep_event_id()}"


def _now():
    return int(time.time())


def _table_rows(data_dir, table):
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------- migration --


def test_migration_fresh_and_idempotent():
    data_dir = _new_env("migration_fresh")
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    state = slp2_schema_migration.verify_migration_state(db_path)
    assert all(state.values()), state
    result = slp2_schema_migration.apply_additive_migration(db_path)
    assert result["new_tables_created"] == [], "re-applying must create nothing new"
    state_2 = slp2_schema_migration.verify_migration_state(db_path)
    assert state_2 == state


def test_migration_upgrade_preserves_existing_rows():
    data_dir = _new_env("migration_upgrade")
    session_id = _next_id()
    r = slp2.record_sleep_request(data_dir, event_id=slp2.generate_sleep_event_id(), session_id=session_id,
                                   session_started_at=_now(), occurred_at=_now())
    before = _table_rows(data_dir, "sleep_requests")
    slp2_schema_migration.apply_additive_migration(os.path.join(data_dir, "anaxi_provenance.db"))
    after = _table_rows(data_dir, "sleep_requests")
    assert before == after
    assert slp2.fetch_sleep_request(data_dir, r["request_id"])["state"] == "PENDING"


# -------------------------------------------------------- request lifecycle


def test_request_creates_pending_canonical_event_surviving_fresh_read():
    data_dir = _new_env("req_pending")
    session_id = _next_id()
    occurred_at = _now()
    event_id = slp2.generate_sleep_event_id()
    created = slp2.record_sleep_request(
        data_dir, event_id=event_id, session_id=session_id, session_started_at=occurred_at, occurred_at=occurred_at,
    )
    assert created["state"] == slp2.STATE_PENDING
    assert created["request_id"] == event_id
    fresh = slp2.fetch_sleep_request(data_dir, event_id)
    assert fresh["state"] == "PENDING"
    assert fresh["requested_at"] == occurred_at
    assert fresh["knock_count"] == 0
    assert fresh["actor_id"] == slp2._clark_actor_id()
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    row = conn.execute("SELECT event_type FROM events WHERE event_id = ?", (event_id,)).fetchone()
    conn.close()
    assert row[0] == "clark_sleep_request"


def test_request_idempotent_replay_returns_same_bundle():
    data_dir = _new_env("req_idempotent")
    session_id = _next_id()
    occurred_at = _now()
    event_id = slp2.generate_sleep_event_id()
    r1 = slp2.record_sleep_request(data_dir, event_id=event_id, session_id=session_id,
                                    session_started_at=occurred_at, occurred_at=occurred_at)
    r2 = slp2.record_sleep_request(data_dir, event_id=event_id, session_id=session_id,
                                    session_started_at=occurred_at, occurred_at=occurred_at)
    assert r1["request_id"] == r2["request_id"] == event_id
    assert len(_table_rows(data_dir, "sleep_requests")) == 1
    assert len(_table_rows(data_dir, "events")) == len(set(r["request_id"] for r in [r1]))  # sanity: no crash


def test_duplicate_request_while_unresolved_creates_zero_rows():
    data_dir = _new_env("req_duplicate")
    session_id = _next_id()
    occurred_at = _now()
    slp2.record_sleep_request(data_dir, event_id=slp2.generate_sleep_event_id(), session_id=session_id,
                               session_started_at=occurred_at, occurred_at=occurred_at)
    events_before = len(_table_rows(data_dir, "events"))
    requests_before = len(_table_rows(data_dir, "sleep_requests"))
    try:
        slp2.record_sleep_request(data_dir, event_id=slp2.generate_sleep_event_id(), session_id=session_id,
                                   session_started_at=occurred_at, occurred_at=occurred_at)
        assert False, "duplicate REQUEST while unresolved must be rejected"
    except slp2.RequestRejected:
        pass
    assert len(_table_rows(data_dir, "events")) == events_before
    assert len(_table_rows(data_dir, "sleep_requests")) == requests_before


def test_ordinary_prose_never_creates_a_formal_request():
    """There is no function anywhere in this module that accepts
    free-form conversational text and derives a request/knock/
    withdrawal from it -- every write entrypoint's signature carries
    only structural identifiers/timestamps, never a prose/content
    parameter. This is a structural, not merely behavioral, guarantee:
    prose like 'I think I may be tired' has no code path into this
    module at all."""
    for fn in (slp2.record_sleep_request, slp2.record_sleep_knock, slp2.record_sleep_withdrawal):
        params = set(inspect.signature(fn).parameters)
        for forbidden in ("content", "text", "prose", "message", "expression"):
            assert forbidden not in params, f"{fn.__name__} must not accept free-form text ({forbidden})"


def test_triggering_human_event_id_optional_and_validated():
    data_dir = _new_env("triggering_h")
    session_id = _next_id()
    occurred_at = _now()

    # No H at all: lawful, exactly as OC0's own "no preceding H required".
    r_no_h = slp2.record_sleep_request(data_dir, event_id=slp2.generate_sleep_event_id(), session_id=session_id,
                                        session_started_at=occurred_at, occurred_at=occurred_at)
    assert r_no_h["triggering_human_event_id"] is None
    slp2.record_sleep_withdrawal(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=r_no_h["request_id"],
                                  session_id=session_id, occurred_at=occurred_at)

    # A genuine, canonically-earlier human_waking_input event: accepted.
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    conn.execute("PRAGMA foreign_keys = ON;")
    h_event_id = "H-" + slp2.generate_sleep_event_id()
    h_time = occurred_at - 10
    conn.execute(
        "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
        "VALUES ('slp2-test-auth', ?, 'unknown', ?)", (session_id, h_time),
    ) if not conn.execute("SELECT 1 FROM auth_contexts WHERE auth_context_id='slp2-test-auth'").fetchone() else None
    conn.execute(
        "INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES (?, NULL, ?) "
        "ON CONFLICT(session_id) DO NOTHING", (session_id, occurred_at),
    )
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        "auth_context_id, input_source_ref, occurred_at, record_created_at) "
        "VALUES (?, 'human_waking_input', NULL, 'unknown', NULL, 'slp2-test-auth', NULL, ?, ?)",
        (h_event_id, h_time, int(time.time())),
    )
    conn.commit()
    conn.close()

    session_id_2 = _next_id()
    later_event = slp2.generate_sleep_event_id()
    r_with_h = slp2.record_sleep_request(
        data_dir, event_id=later_event, session_id=session_id_2, session_started_at=occurred_at + 1,
        occurred_at=occurred_at + 1, triggering_human_event_id=h_event_id,
    )
    assert r_with_h["triggering_human_event_id"] == h_event_id
    slp2.record_sleep_withdrawal(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=r_with_h["request_id"],
                                  session_id=session_id_2, occurred_at=occurred_at + 1)

    # A nonexistent H reference: rejected, nothing created.
    session_id_3 = _next_id()
    events_before = len(_table_rows(data_dir, "events"))
    try:
        slp2.record_sleep_request(
            data_dir, event_id=slp2.generate_sleep_event_id(), session_id=session_id_3,
            session_started_at=occurred_at + 2, occurred_at=occurred_at + 2,
            triggering_human_event_id="does-not-exist",
        )
        assert False, "nonexistent triggering_human_event_id must be rejected"
    except Exception:
        pass
    assert len(_table_rows(data_dir, "events")) == events_before


# ------------------------------------------------------------------- knock -


def test_knock_with_no_unresolved_request_rejected():
    data_dir = _new_env("knock_none")
    try:
        slp2.record_sleep_knock(data_dir, event_id=slp2.generate_sleep_event_id(), request_id="nonexistent",
                                 session_id=_next_id(), occurred_at=_now())
        assert False
    except slp2.RequestRejected:
        pass


def _make_pending(data_dir, suffix=""):
    session_id = _next_id() + suffix
    occurred_at = _now()
    r = slp2.record_sleep_request(data_dir, event_id=slp2.generate_sleep_event_id(), session_id=session_id,
                                   session_started_at=occurred_at, occurred_at=occurred_at)
    return r["request_id"], session_id


def test_knock_while_pending_creates_distinct_canonical_knock():
    data_dir = _new_env("knock_pending")
    request_id, session_id = _make_pending(data_dir)
    k1 = slp2.record_sleep_knock(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                  session_id=session_id, occurred_at=_now())
    k2 = slp2.record_sleep_knock(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                  session_id=session_id, occurred_at=_now())
    assert k1["knock_id"] != k2["knock_id"]
    assert (k1["knock_sequence"], k2["knock_sequence"]) == (1, 2)
    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert status["knock_count"] == 2
    assert status["state"] == "PENDING"


def test_knock_while_deferred_lawful():
    data_dir = _new_env("knock_deferred")
    owner = _register_owner(data_dir, "a")
    request_id, session_id = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="DEFER", occurred_at=_now())
    k = slp2.record_sleep_knock(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                 session_id=session_id, occurred_at=_now())
    assert k["knock_sequence"] == 1
    assert slp2.fetch_sleep_request(data_dir, request_id)["state"] == "DEFERRED"


def test_knock_after_authorized_rejected():
    data_dir = _new_env("knock_authorized")
    owner = _register_owner(data_dir, "a")
    request_id, session_id = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
    try:
        slp2.record_sleep_knock(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                 session_id=session_id, occurred_at=_now())
        assert False
    except slp2.RequestRejected:
        pass


def test_knock_after_declined_rejected():
    data_dir = _new_env("knock_declined")
    owner = _register_owner(data_dir, "a")
    request_id, session_id = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="DECLINE", occurred_at=_now())
    try:
        slp2.record_sleep_knock(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                 session_id=session_id, occurred_at=_now())
        assert False
    except slp2.RequestRejected:
        pass


def test_knock_after_withdrawn_rejected():
    data_dir = _new_env("knock_withdrawn")
    request_id, session_id = _make_pending(data_dir)
    slp2.record_sleep_withdrawal(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                  session_id=session_id, occurred_at=_now())
    try:
        slp2.record_sleep_knock(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                 session_id=session_id, occurred_at=_now())
        assert False
    except slp2.RequestRejected:
        pass


# -------------------------------------------------------------- withdrawal -


def test_withdrawal_while_pending_or_deferred_lawful_preserves_history():
    data_dir = _new_env("withdraw_lawful")
    request_id, session_id = _make_pending(data_dir)
    slp2.record_sleep_knock(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                             session_id=session_id, occurred_at=_now())
    slp2.record_sleep_withdrawal(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                  session_id=session_id, occurred_at=_now())
    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert status["state"] == "WITHDRAWN"
    assert status["knock_count"] == 1, "withdrawal must never erase prior Knocks"
    assert len(status["transitions"]) == 3  # REQUEST, KNOCK, WITHDRAW
    assert slp2.fetch_open_request_for_actor(data_dir) is None


def test_withdrawal_after_terminal_owner_outcome_rejected():
    data_dir = _new_env("withdraw_after_terminal")
    owner = _register_owner(data_dir, "a")
    request_id, session_id = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
    try:
        slp2.record_sleep_withdrawal(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                      session_id=session_id, occurred_at=_now())
        assert False
    except slp2.RequestRejected:
        pass
    assert slp2.fetch_sleep_request(data_dir, request_id)["state"] == "AUTHORIZED"


def test_new_request_allowed_after_prior_withdrawal():
    data_dir = _new_env("withdraw_then_new")
    request_id, session_id = _make_pending(data_dir)
    slp2.record_sleep_withdrawal(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                  session_id=session_id, occurred_at=_now())
    r2 = slp2.record_sleep_request(data_dir, event_id=slp2.generate_sleep_event_id(), session_id=session_id,
                                    session_started_at=_now(), occurred_at=_now())
    assert r2["request_id"] != request_id
    assert r2["state"] == "PENDING"


# ---------------------------------------------------------- owner response -


def test_owner_authorize_nonexistent_request_fails_closed():
    data_dir = _new_env("owner_bad_request")
    owner = _register_owner(data_dir, "a")
    try:
        slp2.apply_owner_transition(data_dir, request_id="does-not-exist", owner_actor_id=owner,
                                     action="AUTHORIZE", occurred_at=_now())
        assert False
    except slp2.RequestRejected:
        pass


def test_owner_transition_by_unregistered_actor_fails_closed():
    data_dir = _new_env("owner_unregistered")
    request_id, _ = _make_pending(data_dir)
    try:
        slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id="actor-not-registered-at-all",
                                     action="AUTHORIZE", occurred_at=_now())
        assert False
    except slp2.RequestRejected:
        pass
    assert slp2.fetch_sleep_request(data_dir, request_id)["state"] == "PENDING"


def test_arbitrary_h_text_does_not_mechanically_authorize():
    """'yes, go sleep' typed as free H text has no code path here at
    all -- apply_owner_transition() only accepts one of the three
    literal action strings, structurally, never parsed prose."""
    data_dir = _new_env("owner_no_prose")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    try:
        slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner,
                                     action="yes, go sleep", occurred_at=_now())
        assert False
    except slp2.SleepTimingError:
        pass
    assert slp2.fetch_sleep_request(data_dir, request_id)["state"] == "PENDING"


def test_defer_transition_and_no_automatic_reknock():
    data_dir = _new_env("defer_no_auto")
    owner = _register_owner(data_dir, "a")
    request_id, session_id = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="DEFER", occurred_at=_now())
    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert status["state"] == "DEFERRED"
    assert status["knock_count"] == 0, "deferral must never itself create a Knock"


def test_repeated_identical_owner_transition_creates_no_new_chronology():
    data_dir = _new_env("owner_repeat")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
    transitions_before = len(slp2.fetch_sleep_request(data_dir, request_id)["transitions"])
    try:
        slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
        assert False
    except slp2.RequestRejected:
        pass
    transitions_after = len(slp2.fetch_sleep_request(data_dir, request_id)["transitions"])
    assert transitions_before == transitions_after


def test_cache_and_ledger_are_atomically_paired_ledger_insert_failure():
    """SLP2-CORRECTION-1 post-build preflight, made permanent: injects
    a trigger that aborts ONLY the sleep_request_transitions insert,
    forcing a failure AFTER sleep_requests' own CAS UPDATE has already
    executed in program order within the same transaction. Proves the
    whole transaction rolls back -- sleep_requests.state (the
    consequential cache) is never left AUTHORIZED while the ledger
    disagrees, and the ledger gains zero rows either."""
    data_dir = _new_env("atomicity_ledger_failure")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")

    inj = sqlite3.connect(db_path)
    inj.execute(
        "CREATE TRIGGER injected_ledger_failure BEFORE INSERT ON sleep_request_transitions "
        "BEGIN SELECT RAISE(ABORT, 'INJECTED: ledger insert failure'); END;"
    )
    inj.commit()
    inj.close()

    before_state = sqlite3.connect(db_path).execute(
        "SELECT state, updated_at FROM sleep_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    before_transitions = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM sleep_request_transitions").fetchone()[0]

    try:
        slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
        assert False, "injected ledger failure must propagate, not be silently absorbed"
    except Exception:
        pass

    after_state = sqlite3.connect(db_path).execute(
        "SELECT state, updated_at FROM sleep_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    after_transitions = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM sleep_request_transitions").fetchone()[0]
    after_open = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM sleep_open_requests").fetchone()[0]
    assert before_state == after_state, "cache must remain unchanged when the paired ledger write fails"
    assert before_transitions == after_transitions, "no ledger row on a rolled-back transaction"
    assert after_open == 1, "the open-request row must not be deleted by a rolled-back AUTHORIZE"


def test_cache_and_ledger_are_atomically_paired_cache_update_failure():
    """The reverse direction: injects a trigger that aborts the
    sleep_requests CAS UPDATE itself (the write that happens FIRST in
    program order), proving the ledger insert -- which would happen
    after it -- is never reached and never partially applied either."""
    data_dir = _new_env("atomicity_cache_failure")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")

    inj = sqlite3.connect(db_path)
    inj.execute(
        "CREATE TRIGGER injected_cache_failure BEFORE UPDATE ON sleep_requests "
        "WHEN NEW.state = 'AUTHORIZED' "
        "BEGIN SELECT RAISE(ABORT, 'INJECTED: cache update failure'); END;"
    )
    inj.commit()
    inj.close()

    before_transitions = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM sleep_request_transitions").fetchone()[0]

    try:
        slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
        assert False, "injected cache failure must propagate, not be silently absorbed"
    except Exception:
        pass

    after_state = sqlite3.connect(db_path).execute(
        "SELECT state FROM sleep_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    after_transitions = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM sleep_request_transitions").fetchone()[0]
    after_open = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM sleep_open_requests").fetchone()[0]
    assert after_state == ("PENDING",), "cache must remain at its pre-transition value"
    assert before_transitions == after_transitions, "no ledger row when the cache write itself fails"
    assert after_open == 1


def test_authorization_does_not_itself_claim_sleep_success():
    data_dir = _new_env("authorize_not_success")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    record = slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
    assert record["state"] == "AUTHORIZED"
    assert record["execution_attempts"] == []


def test_request_or_knock_never_invokes_sleep_automatically():
    """Structural: the only place run_fn() is ever called is inside
    record_sleep_execution_attempt() itself -- record_sleep_request,
    record_sleep_knock, record_sleep_withdrawal, and
    apply_owner_transition contain no such call, and this module never
    imports the Sleep engine at all."""
    import ast
    source = open(os.path.join(ANAXI_FINAL, "sleep_timing_knock.py")).read()
    for line in source.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import sleep_cycle"), "must never import the Sleep engine itself"
        assert not stripped.startswith("from sleep_cycle"), "must never import the Sleep engine itself"
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            calls_run_fn = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "run_fn"
                for n in ast.walk(node)
            )
            if calls_run_fn:
                assert node.name == "record_sleep_execution_attempt", (
                    f"unexpected run_fn() call inside {node.name}"
                )


def test_manual_sleep_unrelated_not_auto_linked_as_fulfillment():
    """There is no function anywhere that scans for an unrelated,
    already-run Sleep cycle and retroactively links it to a request --
    the only way sleep_execution_attempts gains a row is the explicit
    record_sleep_execution_attempt() call naming an exact request_id."""
    params = set(inspect.signature(slp2.record_sleep_execution_attempt).parameters)
    assert "request_id" in params
    # No function accepts a bare Sleep-cycle result without a request_id.
    for name, fn in inspect.getmembers(slp2, inspect.isfunction):
        if name.startswith("_"):
            continue
        sig_params = set(inspect.signature(fn).parameters)
        if "run_fn" in sig_params:
            assert "request_id" in sig_params


# ---------------------------------------------------------------- execution


def test_execution_requires_authorized_state():
    data_dir = _new_env("exec_requires_authorized")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    try:
        slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                             attempted_at=_now(), run_fn=lambda: {"status": "completed"})
        assert False
    except slp2.RequestRejected:
        pass


def test_execution_success_and_failure_and_unestablished_leave_state_authorized():
    data_dir = _new_env("exec_outcomes")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())

    ok = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                              attempted_at=_now(), run_fn=lambda: {"status": "completed"})
    assert ok["outcome"] == "succeeded"
    assert slp2.fetch_sleep_request(data_dir, request_id)["state"] == "AUTHORIZED"

    def boom():
        raise RuntimeError("synthetic Sleep engine failure")
    failed = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                                  attempted_at=_now(), run_fn=boom)
    assert failed["outcome"] == "failed"
    assert slp2.fetch_sleep_request(data_dir, request_id)["state"] == "AUTHORIZED"

    unestablished = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                                          attempted_at=_now(), run_fn=lambda: {"status": "no_work"})
    assert unestablished["outcome"] == "not_established"
    assert slp2.fetch_sleep_request(data_dir, request_id)["state"] == "AUTHORIZED"

    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert [e["attempt_sequence"] for e in status["execution_attempts"]] == [1, 2, 3]


def test_execution_unrecognized_result_shape_is_not_established_never_success():
    data_dir = _new_env("exec_unrecognized")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
    outcome = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                                    attempted_at=_now(), run_fn=lambda: "not a dict at all")
    assert outcome["outcome"] == "not_established"


# --------------------------------------- SLP2-CORRECTION-1: repeated execution


def test_execution_operator_actor_id_must_resolve_to_registered_human():
    data_dir = _new_env("exec_operator_unregistered")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
    try:
        slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id="actor-not-registered",
                                             attempted_at=_now(), run_fn=lambda: {"status": "completed"})
        assert False
    except slp2.RequestRejected:
        pass
    assert slp2.fetch_sleep_request(data_dir, request_id)["execution_attempts"] == []


def test_correction1_item1_authorized_request_can_have_zero_attempts():
    data_dir = _new_env("c1_zero_attempts")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert status["state"] == "AUTHORIZED"
    assert status["execution_attempts"] == []


def test_correction1_item2_first_explicit_execute_creates_exactly_one_attempt():
    data_dir = _new_env("c1_first_execute")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())

    call_count = [0]
    def counting_run_fn():
        call_count[0] += 1
        return {"status": "completed"}

    outcome = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                                    attempted_at=_now(), run_fn=counting_run_fn)
    assert call_count[0] == 1, "run_fn must be invoked exactly once for one explicit execute"
    assert outcome["attempt_sequence"] == 1
    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert len(status["execution_attempts"]) == 1


def test_correction1_item3_failed_first_attempt_leaves_authorized_no_automatic_retry():
    data_dir = _new_env("c1_failed_no_retry")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())

    call_count = [0]
    def failing_run_fn():
        call_count[0] += 1
        raise RuntimeError("synthetic failure")

    outcome = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                                    attempted_at=_now(), run_fn=failing_run_fn)
    assert outcome["outcome"] == "failed"
    assert call_count[0] == 1, "no automatic retry -- run_fn called exactly once despite failure"
    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert status["state"] == "AUTHORIZED"
    assert len(status["execution_attempts"]) == 1


def test_correction1_item4_second_explicit_execute_creates_distinct_second_attempt():
    data_dir = _new_env("c1_second_execute")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())

    call_count = [0]
    def counting_run_fn():
        call_count[0] += 1
        return {"status": "completed"}

    first = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                                  attempted_at=_now(), run_fn=counting_run_fn)
    assert call_count[0] == 1
    second = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                                   attempted_at=_now(), run_fn=counting_run_fn)
    assert call_count[0] == 2, "the second explicit invocation calls run_fn exactly once more"
    assert first["execution_attempt_id"] != second["execution_attempt_id"]
    assert (first["attempt_sequence"], second["attempt_sequence"]) == (1, 2)
    assert slp2.fetch_sleep_request(data_dir, request_id)["state"] == "AUTHORIZED"


def test_correction1_item5_without_second_invocation_second_attempt_count_stays_zero():
    data_dir = _new_env("c1_no_second_invocation")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
    slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                         attempted_at=_now(), run_fn=lambda: {"status": "completed"})
    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert len(status["execution_attempts"]) == 1, "no operator invocation happened a second time"


def test_correction1_item6_malformed_result_no_automatic_retry_authorized_intact():
    data_dir = _new_env("c1_malformed_no_retry")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())

    call_count = [0]
    def malformed_run_fn():
        call_count[0] += 1
        return {"unexpected": "shape"}

    outcome = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                                    attempted_at=_now(), run_fn=malformed_run_fn)
    assert outcome["outcome"] == "not_established"
    assert call_count[0] == 1
    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert status["state"] == "AUTHORIZED"
    assert len(status["execution_attempts"]) == 1


def test_correction1_item7_multiple_attempts_have_distinct_provenance():
    data_dir = _new_env("c1_distinct_provenance")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())

    t1, t2, t3 = _now(), _now() + 10, _now() + 20
    a1 = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                               attempted_at=t1, run_fn=lambda: {"status": "completed"})
    a2 = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                               attempted_at=t2, run_fn=lambda: {"status": "no_work"})
    a3 = slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                               attempted_at=t3, run_fn=lambda: {"status": "completed"})
    ids = {a1["execution_attempt_id"], a2["execution_attempt_id"], a3["execution_attempt_id"]}
    assert len(ids) == 3, "every attempt has a distinct execution_attempt_id"
    assert [a["attempt_sequence"] for a in (a1, a2, a3)] == [1, 2, 3]
    assert [a["attempted_at"] for a in (a1, a2, a3)] == [t1, t2, t3]
    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert all(e["operator_actor_id"] == owner for e in status["execution_attempts"])


def test_correction1_item8_request_or_knock_alone_never_causes_an_attempt():
    data_dir = _new_env("c1_request_knock_no_attempt")
    request_id, session_id = _make_pending(data_dir)
    slp2.record_sleep_knock(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                             session_id=session_id, occurred_at=_now())
    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert status["execution_attempts"] == []


def test_correction1_item9_no_loop_around_the_sleep_runner():
    data_dir = _new_env("c1_no_loop")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
    call_count = [0]
    def counting_run_fn():
        call_count[0] += 1
        return {"status": "completed"}
    slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                         attempted_at=_now(), run_fn=counting_run_fn)
    assert call_count[0] == 1, "exactly one run_fn() call per record_sleep_execution_attempt() invocation"


def test_correction1_item10_repeated_execution_impossible_from_non_authorized_states():
    owner_registered = {}

    def attempt_execution_from(state_setup_fn, label):
        data_dir = _new_env(f"c1_non_authorized_{label}")
        owner = _register_owner(data_dir, label)
        request_id, session_id = _make_pending(data_dir)
        state_setup_fn(data_dir, request_id, session_id, owner)
        try:
            slp2.record_sleep_execution_attempt(data_dir, request_id=request_id, operator_actor_id=owner,
                                                 attempted_at=_now(), run_fn=lambda: {"status": "completed"})
            assert False, f"execution must be refused from state reached via {label}"
        except slp2.RequestRejected:
            pass

    attempt_execution_from(lambda d, r, s, o: None, "pending")
    attempt_execution_from(
        lambda d, r, s, o: slp2.apply_owner_transition(d, request_id=r, owner_actor_id=o, action="DEFER", occurred_at=_now()),
        "deferred",
    )
    attempt_execution_from(
        lambda d, r, s, o: slp2.apply_owner_transition(d, request_id=r, owner_actor_id=o, action="DECLINE", occurred_at=_now()),
        "declined",
    )
    attempt_execution_from(
        lambda d, r, s, o: slp2.record_sleep_withdrawal(
            d, event_id=slp2.generate_sleep_event_id(), request_id=r, session_id=s, occurred_at=_now(),
        ),
        "withdrawn",
    )


def test_correction1_migration_upgrade_from_pre_correction1_schema_preserves_legacy_rows():
    """Reproduces the exact pre-Correction-1 SLP2 schema (as historically
    BUILT at 5f375b25), inserts a legacy execution_attempts row the old
    way (no operator_actor_id column existed), then applies the current
    (Correction-1) migration and proves: the legacy row's original
    columns are byte-identical, its new operator_actor_id is NULL (not
    fabricated), the new column/trigger now exist, and reapplication is
    idempotent."""
    data_dir = os.path.join(TEST_DIR, "c1_migration_upgrade")
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    create_provenance_db(db_path).close()

    # The exact pre-Correction-1 DDL, reconstructed inline (not imported
    # from git history at test time, to keep this suite hermetic) --
    # matches slp2_schema_migration.py's CREATE TABLE for
    # sleep_execution_attempts exactly as it shipped in the historical
    # SLP2 BUILT commit (5f375b25), before this correction's additive
    # column/trigger.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sleep_requests (
        request_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, state TEXT NOT NULL,
        triggering_human_event_id TEXT, requested_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sleep_execution_attempts (
        execution_attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
        attempt_sequence INTEGER NOT NULL, attempted_at INTEGER NOT NULL,
        outcome TEXT NOT NULL, detail TEXT, created_at INTEGER NOT NULL
    );
    """)
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES ('s1', NULL, 1000)")
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) VALUES ('a1', 's1', 'unknown', 1000)")
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        "auth_context_id, input_source_ref, occurred_at, record_created_at) VALUES "
        "('req1', 'clark_sleep_request', NULL, 'unknown', NULL, 'a1', NULL, 1000, 1000)"
    )
    conn.execute("INSERT INTO sleep_requests VALUES ('req1', 'actor-x', 'AUTHORIZED', NULL, 1000, 1000)")
    conn.execute(
        "INSERT INTO sleep_execution_attempts VALUES ('exec1', 'req1', 1, 1000, 'succeeded', 'legacy row', 1000)"
    )
    conn.commit()
    conn.close()

    before = sqlite3.connect(db_path).execute("SELECT * FROM sleep_execution_attempts").fetchall()

    slp2_schema_migration.apply_additive_migration(db_path)
    after = sqlite3.connect(db_path).execute("SELECT * FROM sleep_execution_attempts").fetchall()
    assert after[0][:7] == before[0][:7], "legacy row's original columns must be byte-identical"
    assert after[0][7] is None, "legacy row must never have a fabricated operator_actor_id"

    state = slp2_schema_migration.verify_migration_state(db_path)
    assert state["has_sleep_execution_attempts_operator_actor_id"]
    assert state["has_sleep_execution_attempts_operator_required_trigger"]

    slp2_schema_migration.apply_additive_migration(db_path)  # idempotent reapplication
    after_2 = sqlite3.connect(db_path).execute("SELECT * FROM sleep_execution_attempts").fetchall()
    assert after == after_2

    # And the NEW trigger correctly enforces operator_actor_id on any
    # NEW insert going forward -- it does not retroactively touch the
    # legacy row above, but it must block a fresh insert missing it.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute(
            "INSERT INTO sleep_execution_attempts (execution_attempt_id, request_id, attempt_sequence, "
            "attempted_at, outcome, detail, created_at) VALUES ('exec2', 'req1', 2, 2000, 'succeeded', 'x', 2000)"
        )
        assert False, "a new insert with no operator_actor_id must be rejected"
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


# ------------------------------------------------------------- concurrency -


def test_concurrent_duplicate_requests_at_most_one_survives():
    data_dir = _new_env("race_duplicate_request")
    session_id = _next_id()
    occurred_at = _now()
    barrier = threading.Barrier(2)
    results = [None, None]

    def attempt(i):
        barrier.wait()
        try:
            r = slp2.record_sleep_request(data_dir, event_id=slp2.generate_sleep_event_id(), session_id=session_id,
                                           session_started_at=occurred_at, occurred_at=occurred_at)
            results[i] = ("ok", r["request_id"])
        except slp2.RequestRejected as exc:
            results[i] = ("rejected", str(exc))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()

    outcomes = sorted(r[0] for r in results)
    assert outcomes == ["ok", "rejected"], results
    assert len(_table_rows(data_dir, "sleep_open_requests")) == 1


def test_owner_transition_race_authorize_vs_decline_exactly_one_winner():
    data_dir = _new_env("race_authorize_decline")
    owner = _register_owner(data_dir, "a")
    request_id, _ = _make_pending(data_dir)
    barrier = threading.Barrier(2)
    results = {}

    def attempt(action):
        barrier.wait()
        try:
            slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action=action, occurred_at=_now())
            results[action] = "ok"
        except slp2.RequestRejected:
            results[action] = "rejected"

    threads = [threading.Thread(target=attempt, args=(a,)) for a in ("AUTHORIZE", "DECLINE")]
    for t in threads: t.start()
    for t in threads: t.join()

    assert sorted(results.values()) == ["ok", "rejected"], results
    final_state = slp2.fetch_sleep_request(data_dir, request_id)["state"]
    assert final_state in ("AUTHORIZED", "DECLINED")
    winner = "AUTHORIZE" if results["AUTHORIZE"] == "ok" else "DECLINE"
    assert (winner == "AUTHORIZE") == (final_state == "AUTHORIZED")
    owner_transitions = [t for t in slp2.fetch_sleep_request(data_dir, request_id)["transitions"] if t["actor_role"] == "owner"]
    assert len(owner_transitions) == 1, "the loser must append zero transition rows"


def test_authorize_vs_withdraw_race_one_lawful_terminal_result():
    data_dir = _new_env("race_authorize_withdraw")
    owner = _register_owner(data_dir, "a")
    request_id, session_id = _make_pending(data_dir)
    barrier = threading.Barrier(2)
    results = {}

    def try_authorize():
        barrier.wait()
        try:
            slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="AUTHORIZE", occurred_at=_now())
            results["authorize"] = "ok"
        except slp2.RequestRejected:
            results["authorize"] = "rejected"

    def try_withdraw():
        barrier.wait()
        try:
            slp2.record_sleep_withdrawal(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                          session_id=session_id, occurred_at=_now())
            results["withdraw"] = "ok"
        except slp2.RequestRejected:
            results["withdraw"] = "rejected"

    threads = [threading.Thread(target=try_authorize), threading.Thread(target=try_withdraw)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert sorted(results.values()) == ["ok", "rejected"], results
    final_state = slp2.fetch_sleep_request(data_dir, request_id)["state"]
    assert final_state in ("AUTHORIZED", "WITHDRAWN")


def test_knock_vs_decline_race_no_corruption():
    data_dir = _new_env("race_knock_decline")
    owner = _register_owner(data_dir, "a")
    request_id, session_id = _make_pending(data_dir)
    barrier = threading.Barrier(2)
    results = {}

    def try_knock():
        barrier.wait()
        try:
            slp2.record_sleep_knock(data_dir, event_id=slp2.generate_sleep_event_id(), request_id=request_id,
                                     session_id=session_id, occurred_at=_now())
            results["knock"] = "ok"
        except slp2.RequestRejected:
            results["knock"] = "rejected"

    def try_decline():
        barrier.wait()
        try:
            slp2.apply_owner_transition(data_dir, request_id=request_id, owner_actor_id=owner, action="DECLINE", occurred_at=_now())
            results["decline"] = "ok"
        except slp2.RequestRejected:
            results["decline"] = "rejected"

    threads = [threading.Thread(target=try_knock), threading.Thread(target=try_decline)]
    for t in threads: t.start()
    for t in threads: t.join()

    status = slp2.fetch_sleep_request(data_dir, request_id)
    assert status["state"] == "DECLINED"
    assert results["decline"] == "ok"
    assert status["knock_count"] == (1 if results["knock"] == "ok" else 0)


# ----------------------------------------------------------------- misc ---


def test_module_never_imports_private_space():
    for path in ("sleep_timing_knock.py", "slp2_schema_migration.py", "slp2_cli.py"):
        source = open(os.path.join(ANAXI_FINAL, path)).read()
        for line in source.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("import workspace_private"), path
            assert not stripped.startswith("from workspace_private"), path


def test_fetch_sleep_request_reports_only_mechanical_facts():
    data_dir = _new_env("no_urgency")
    request_id, _ = _make_pending(data_dir)
    status = slp2.fetch_sleep_request(data_dir, request_id)
    forbidden_terms = ("urgent", "urgency", "impatien", "desperat", "mood", "tired", "feel", "want", "need")
    dumped = str(status).lower()
    for term in forbidden_terms:
        assert term not in dumped, f"{term!r} must never appear in a mechanical status receipt: {status}"


# ------------------------------------------------- conversation_direction --


def test_sleep_timing_request_field_additive_default_none():
    raw = {"act": "develop_current", "thread": "t", "direction_request": "none", "relinquish_direction": False}
    validated, failure = cd.validate_pass1_conversation_act(raw)
    assert failure is None
    assert validated["sleep_timing_request"] == "none"


def test_sleep_timing_request_field_accepts_valid_values():
    for value in ("request_sleep", "knock_sleep_request", "withdraw_sleep_request", "none"):
        raw = {"act": "develop_current", "thread": "t", "direction_request": "none",
               "relinquish_direction": False, "sleep_timing_request": value}
        validated, failure = cd.validate_pass1_conversation_act(raw)
        assert failure is None, (value, failure)
        assert validated["sleep_timing_request"] == value


def test_sleep_timing_request_field_rejects_invalid_value():
    raw = {"act": "develop_current", "thread": "t", "direction_request": "none",
           "relinquish_direction": False, "sleep_timing_request": "bogus_action"}
    validated, failure = cd.validate_pass1_conversation_act(raw)
    assert validated is None
    assert failure == cd.DirectionFailure.INVALID_SLEEP_TIMING_REQUEST


def test_sleep_timing_request_independent_of_other_fields():
    base = {"act": "ask_human", "thread": "t", "direction_request": "request_clark", "relinquish_direction": False}
    v_none, _ = cd.validate_pass1_conversation_act(base)
    v_sleep, _ = cd.validate_pass1_conversation_act(dict(base, sleep_timing_request="request_sleep"))
    assert v_none["act"] == v_sleep["act"] == "ask_human"
    assert v_none["direction_request"] == v_sleep["direction_request"] == "request_clark"
    assert v_none["sleep_timing_request"] == "none"
    assert v_sleep["sleep_timing_request"] == "request_sleep"


def test_pass1_schema_includes_sleep_timing_request_as_optional():
    assert "sleep_timing_request" in cd.PASS1_SCHEMA["properties"]
    assert "sleep_timing_request" not in cd.PASS1_SCHEMA["required"], "must stay optional, never required"


# --------------------------------------- SLP2-CORRECTION-1: model-visible affordance


def test_affordance_text_names_all_four_enum_values():
    text = cd.SLEEP_TIMING_AFFORDANCE_TEXT
    for value in cd.VALID_SLEEP_TIMING_REQUESTS:
        assert value in text, f"{value!r} must be nameable from the affordance text"


def test_affordance_text_states_optionality_and_zero_action_validity():
    text = cd.SLEEP_TIMING_AFFORDANCE_TEXT.lower()
    assert "optional" in text
    assert "always valid" in text or "no action" in text


def test_affordance_text_states_ordinary_prose_is_not_the_action():
    text = cd.SLEEP_TIMING_AFFORDANCE_TEXT.lower()
    assert "prose" in text and "never this action" in text


def test_affordance_text_avoids_pressure_language():
    text = cd.SLEEP_TIMING_AFFORDANCE_TEXT.lower()
    for forbidden in ("you should", "consider whether", "remember to", "every turn", "must choose"):
        assert forbidden not in text, f"{forbidden!r} is pressure language and must not appear"


def test_affordance_text_absent_from_calibrated_pass1_task_instruction():
    """Locks in the calibration-safety property this correction relies
    on: the affordance text (and the bare field name) must never be
    folded into PASS1_TASK_INSTRUCTION, which is part of the
    fingerprint-calibrated Pass-1 scaffold hash -- any future edit that
    accidentally moves this text back into PASS1_TASK_INSTRUCTION would
    reintroduce the exact BUDGET_EXCEEDED regression this correction
    fixed (see SLEEP_TIMING_AFFORDANCE_TEXT's own module comment)."""
    assert "sleep_timing_request" not in cd.PASS1_TASK_INSTRUCTION
    assert cd.SLEEP_TIMING_AFFORDANCE_TEXT not in cd.PASS1_TASK_INSTRUCTION
    for value in cd.VALID_SLEEP_TIMING_REQUESTS:
        if value == "none":
            continue  # "none" already appears in PASS1_TASK_INSTRUCTION for unrelated fields
        assert value not in cd.PASS1_TASK_INSTRUCTION


def test_affordance_text_is_small_and_real_byte_cost_fits_pass1_budget():
    """Static/synthetic proof (no live inference): using the REAL
    context_budget module, a representative hard-contribution set --
    the calibrated scaffold cost (693, unaffected by this correction),
    a representative human message, and the mechanical-state
    contribution WITH the affordance text appended -- fits well within
    PASS1_MAX_PROMPT_BUDGET. This mirrors the exact arithmetic
    llama_anaxi.py performs; it does not import llama_anaxi.py itself
    (unavailable in this sandbox without the ollama/numpy dependency
    chain), only the dependency-free context_budget module."""
    import context_budget as cb
    CALIBRATED_SCAFFOLD_COST = 693
    mechanical_state_text = (
        "Current working state: active_thread=None, active_thread_origin=None, "
        "open_threads=[], direction_owner='unknown'\n\n"
        "Current date/time: 2026-09-13T21:00:00Z.\n\n" + cd.SLEEP_TIMING_AFFORDANCE_TEXT
    )
    hard_core = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "x", hard=True)
    hard_core.cost = CALIBRATED_SCAFFOLD_COST
    hard_human = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, "So what have you been thinking about lately?", hard=True)
    hard_mechanical = cb.Contribution(cb.MECHANICAL_STATE, mechanical_state_text, hard=True)
    result = cb.compose_within_budget([hard_core, hard_human, hard_mechanical], cb.PASS1_MAX_PROMPT_BUDGET)
    assert result.fits, (
        f"hard cost {hard_core.cost + hard_human.cost + hard_mechanical.cost} "
        f"must fit within budget {cb.PASS1_MAX_PROMPT_BUDGET}"
    )


# --------------------------------------- Correction-2 storage/lifecycle ---


def _c2_connect(data_dir, fk=1, recursive=0):
    conn = sqlite3.connect(os.path.join(data_dir, 'anaxi_provenance.db'))
    conn.execute(f'PRAGMA foreign_keys={fk}')
    conn.execute(f'PRAGMA recursive_triggers={recursive}')
    return conn


def _c2_reject(conn, sql, args=()):
    try:
        conn.execute(sql, args)
    except sqlite3.IntegrityError:
        conn.rollback()
    else:
        conn.rollback()
        raise AssertionError(f'identity/provenance mutation accepted: {sql!r}, {args!r}')


def _c2_attempt(conn, req, actor, seq=1):
    conn.execute(
        'INSERT INTO sleep_execution_attempts '
        '(execution_attempt_id,request_id,attempt_sequence,attempted_at,outcome,created_at,operator_actor_id) '
        'VALUES (?,?,?,1000,\'not_established\',1000,?)',
        (f'c2-attempt-{seq}', req, seq, actor))


def _c2_authorized(name):
    data_dir = _new_env(name)
    human = _register_owner(data_dir, name)
    req, _ = _make_pending(data_dir)
    slp2.apply_owner_transition(data_dir, request_id=req, owner_actor_id=human,
                               action='AUTHORIZE', occurred_at=_now())
    return data_dir, human, req


def test_correction2_direct_storage_operator_matrix():
    """Attack the real table, never the API, in all four pragma modes."""
    for fk in (0, 1):
        for recursive in (0, 1):
            d, human, req = _c2_authorized(f'c2_operators_{fk}_{recursive}')
            c = _c2_connect(d, fk, recursive)
            host = c.execute("SELECT actor_id FROM actors WHERE actor_type='host_system'").fetchone()[0]
            # Partial registration is not a human-person registration.
            c.execute("INSERT INTO actors VALUES ('bare-human','human_person','bare-human',NULL,1,NULL)")
            c.execute("INSERT INTO actors VALUES ('dangling-human','human_person','dangling-human',NULL,1,NULL)")
            c.commit()
            c.execute('PRAGMA foreign_keys=OFF')
            c.execute("INSERT INTO actor_human_person VALUES ('dangling-human','missing-person',NULL,1)")
            c.commit()
            c.execute(f'PRAGMA foreign_keys={fk}')
            invalid = (None, '', ' ', '\t', '\n', 'unknown', slp2._clark_actor_id(), host,
                       'bare-human', 'dangling-human', b'malformed', b'\x00', 123)
            for actor in invalid:
                try:
                    _c2_attempt(c, req, actor)
                except sqlite3.IntegrityError:
                    c.rollback()
                else:
                    raise AssertionError(f'accepted invalid operator {actor!r}, FK={fk}, recursive={recursive}')
                with _c2_connect(d) as fresh:
                    assert fresh.execute('SELECT COUNT(*) FROM sleep_execution_attempts').fetchone()[0] == 0
            _c2_attempt(c, req, human)
            c.commit()
            c.close()
            # Storage registration is not an invented current-owner binding:
            # another canonically registered person is also valid provenance.
            other = _register_owner(d, f'c2_other_operator_{fk}_{recursive}')
            with _c2_connect(d, fk, recursive) as c:
                _c2_attempt(c, req, other, 2)
            status = slp2.fetch_sleep_request(d, req)
            assert [r['operator_actor_id'] for r in status['execution_attempts']] == [human, other]
            assert status['state'] == 'AUTHORIZED'


def test_correction2_application_registration_and_explicit_policy():
    d, human, req = _c2_authorized('c2_app_registration')
    with _c2_connect(d) as c:
        c.execute("INSERT INTO actors VALUES ('bare-human','human_person','bare-human',NULL,1,NULL)")
        host = c.execute("SELECT actor_id FROM actors WHERE actor_type='host_system'").fetchone()[0]
    calls = []
    def runner():
        calls.append(1)
        return {'status': 'no_work'}
    for invalid in (None, '', ' ', '\t', 'unknown', slp2._clark_actor_id(), host, 'bare-human', b'bad', 123):
        try:
            slp2.record_sleep_execution_attempt(d, request_id=req, operator_actor_id=invalid,
                                               attempted_at=1000, run_fn=runner)
        except slp2.SleepTimingError:
            pass
        else:
            raise AssertionError(f'application accepted {invalid!r}')
        assert calls == []
        assert slp2.fetch_sleep_request(d, req)['execution_attempts'] == []
    for seq in (1, 2):
        receipt = slp2.record_sleep_execution_attempt(d, request_id=req, operator_actor_id=human,
                                                     attempted_at=1000+seq, run_fn=runner)
        assert receipt['operator_actor_id'] == human
        assert receipt['attempt_sequence'] == seq
        assert receipt['outcome'] == 'not_established'
        assert len(calls) == seq
        # Repeated status reads are not a second explicit invocation/retry.
        for _ in range(3):
            status = slp2.fetch_sleep_request(d, req)
            assert len(status['execution_attempts']) == seq
            assert status['state'] == 'AUTHORIZED'
    assert [r['attempted_at'] for r in status['execution_attempts']] == [1001, 1002]
    assert len({r['execution_attempt_id'] for r in status['execution_attempts']}) == 2
    assert {r['operator_actor_id'] for r in status['execution_attempts']} == {human}


def test_correction2_identity_lifecycle_all_write_forms_preserve_receipt():
    """Real registration + mocked execution, then conflicting SQL, then fresh status.
    Rowid/unique-key collisions matter as much as the obvious primary key.
    No trigger is removed and no pragma is assumed by any attack.
    """
    for fk in (0, 1):
        for recursive in (0, 1):
            d, human, req = _c2_authorized(f'c2_identity_{fk}_{recursive}')
            second = _register_owner(d, f'c2_second_{fk}_{recursive}')
            calls = []
            def runner():
                calls.append(1)
                return {'status': 'completed'}
            receipt = slp2.record_sleep_execution_attempt(d, request_id=req, operator_actor_id=human,
                                                         attempted_at=1000, run_fn=runner)
            c = _c2_connect(d, fk, recursive)
            person = c.execute('SELECT person_id FROM actor_human_person WHERE actor_id=?', (human,)).fetchone()[0]
            actor_rowid = c.execute('SELECT rowid FROM actors WHERE actor_id=?', (human,)).fetchone()[0]
            link_rowid = c.execute('SELECT rowid FROM actor_human_person WHERE actor_id=?', (human,)).fetchone()[0]
            person_rowid = c.execute('SELECT rowid FROM persons WHERE person_id=?', (person,)).fetchone()[0]
            original = {table: c.execute(f'SELECT rowid,* FROM {table} ORDER BY rowid').fetchall()
                        for table in ('actors', 'actor_human_person', 'persons', 'sleep_execution_attempts')}
            # Ordinary and UPDATE OR ... forms, including hidden rowid aliases.
            for modifier in ('', 'OR REPLACE', 'OR IGNORE', 'OR FAIL', 'OR ABORT', 'OR ROLLBACK'):
                for sql, args in (
                    (f'UPDATE {modifier} actors SET actor_id=? WHERE actor_id=?', ('renamed', human)),
                    (f"UPDATE {modifier} actors SET actor_type='host_system' WHERE actor_id=?", (human,)),
                    (f'UPDATE {modifier} actor_human_person SET person_id=? WHERE actor_id=?', ('other', human)),
                    (f'UPDATE {modifier} persons SET person_id=? WHERE person_id=?', ('other', person)),
                ):
                    _c2_reject(c, sql, args)
                for alias in ('rowid', '_rowid_', 'oid'):
                    _c2_reject(c, f'UPDATE {modifier} actors SET {alias}=? WHERE actor_id=?', (actor_rowid, second))
            for table, key, val in (('actors','actor_id',human), ('actor_human_person','actor_id',human),
                                    ('persons','person_id',person)):
                _c2_reject(c, f'DELETE FROM {table} WHERE {key}=?', (val,))
            actors = c.execute('SELECT actor_id,actor_type,stable_key FROM actors').fetchall()
            for actor, _, _ in actors:
                _c2_reject(c, 'UPDATE actors SET actor_id=? WHERE actor_id=?', ('renamed', actor))
            for verb in ('INSERT', 'INSERT OR REPLACE', 'REPLACE', 'INSERT OR IGNORE', 'INSERT OR FAIL',
                         'INSERT OR ABORT', 'INSERT OR ROLLBACK'):
                for actor, kind, stable in actors:
                    changed = 'host_system' if kind == 'human_person' else 'human_person'
                    _c2_reject(c, f'{verb} INTO actors(actor_id,actor_type,stable_key,created_at) VALUES (?,?,?,1)',
                               (actor, changed, stable))
                # Different primary key colliding with stable_key/person_id must not evict its owner.
                _c2_reject(c, f"{verb} INTO actors(actor_id,actor_type,stable_key,created_at) VALUES ('new','human_person',?,1)", (human,))
                _c2_reject(c, f'{verb} INTO actor_human_person(actor_id,person_id,relationship_established_at) VALUES (?,?,1)', (human, 'other'))
                _c2_reject(c, f'{verb} INTO actor_human_person(actor_id,person_id,relationship_established_at) VALUES (?,?,1)', (second, person))
                _c2_reject(c, f'{verb} INTO actors(rowid,actor_id,actor_type,stable_key,created_at) VALUES (?,\'new\',\'human_person\',\'new\',1)', (actor_rowid,))
                _c2_reject(c, f'{verb} INTO actor_human_person(rowid,actor_id,person_id,relationship_established_at) VALUES (?,?,?,1)', (link_rowid, second, 'other'))
                _c2_reject(c, f"{verb} INTO persons(rowid,person_id,created_at) VALUES (?,'new-person',1)", (person_rowid,))
                _c2_reject(c, f'{verb} INTO persons(person_id,created_at) VALUES (?,1)', (person,))
            # UPSERT using an identical proposed insert, then conflicting DO UPDATE.
            for target, change in (('actor_id', "actor_type='host_system'"),
                                   ('stable_key', "actor_id='renamed'")):
                _c2_reject(c, 'INSERT INTO actors SELECT * FROM actors WHERE actor_id=? '
                           f'ON CONFLICT({target}) DO UPDATE SET {change}', (human,))
            _c2_reject(c, 'INSERT INTO actor_human_person SELECT * FROM actor_human_person WHERE actor_id=? '
                       "ON CONFLICT(actor_id) DO UPDATE SET person_id='other'", (human,))
            _c2_reject(c, 'INSERT INTO persons SELECT * FROM persons WHERE person_id=? '
                       "ON CONFLICT(person_id) DO UPDATE SET person_id='other'", (person,))
            for table, rows in original.items():
                assert c.execute(f'SELECT rowid,* FROM {table} ORDER BY rowid').fetchall() == rows
            # Semantic no-op UPDATE is allowed; existing retirement rules still apply.
            c.execute('UPDATE actors SET actor_id=actor_id WHERE actor_id=?', (human,))
            created = c.execute('SELECT created_at FROM actors WHERE actor_id=?', (human,)).fetchone()[0]
            c.execute('UPDATE actors SET retired_at=? WHERE actor_id=?', (created+1, human))
            c.commit()
            _c2_reject(c, 'UPDATE actors SET retired_at=NULL WHERE actor_id=?', (human,))
            c.close()
            with _c2_connect(d, fk, recursive) as fresh:
                resolution = fresh.execute('SELECT a.actor_type,h.person_id FROM actors a '
                    'JOIN actor_human_person h ON h.actor_id=a.actor_id JOIN persons p ON p.person_id=h.person_id '
                    'WHERE a.actor_id=?', (human,)).fetchone()
                assert resolution == ('human_person', person)
                _c2_reject(fresh, 'UPDATE actors SET actor_id=? WHERE actor_id=?', ('renamed', human))
            status = slp2.fetch_sleep_request(d, req)
            assert status['execution_attempts'][0]['operator_actor_id'] == human
            assert status['execution_attempts'][0]['execution_attempt_id'] == receipt['execution_attempt_id']
            assert status['execution_attempts'][0]['attempted_at'] == 1000
            assert len(status['execution_attempts']) == len(calls) == 1


def test_correction2_bound_cli_execution_uses_mocked_boundary_once_per_invocation():
    import contextlib
    import io
    import types
    from unittest.mock import patch
    import slp2_cli
    d, human, req = _c2_authorized('c2_cli_bound')
    calls = []
    fake_cycle = types.ModuleType('sleep_cycle')
    def run():
        calls.append(1)
        return types.SimpleNamespace(status='completed')
    fake_cycle.run_sleep_cycle_with_production_defaults = run
    args = types.SimpleNamespace(db=os.path.join(d, 'anaxi_provenance.db'),
                                 request_id=req, confirm_real_sleep=True)
    with patch.dict(sys.modules, {'sleep_cycle': fake_cycle}), contextlib.redirect_stdout(io.StringIO()):
        with patch.dict(os.environ, {'ANAXI_BOUND_HUMAN_ACTOR_ID': human}):
            assert slp2_cli.cmd_execute_sleep(args) == 0
            assert len(calls) == 1
            assert slp2_cli.cmd_status(args) == 0
            assert len(calls) == 1
            assert slp2_cli.cmd_execute_sleep(args) == 0
            assert len(calls) == 2
        with patch.dict(os.environ, {'ANAXI_BOUND_HUMAN_ACTOR_ID': slp2._clark_actor_id()}):
            assert slp2_cli.cmd_execute_sleep(args) == 1
            assert len(calls) == 2
    assert [a['operator_actor_id'] for a in slp2.fetch_sleep_request(d, req)['execution_attempts']] == [human, human]


def test_correction2_seeding_registration_and_guard_install_are_idempotent():
    import provenance_schema as ps
    d = _new_env('c2_idempotent_registration')
    first = _register_owner(d, 'c2_idempotent_human')
    with _c2_connect(d) as c:
        before = list(c.iterdump())
        seed_reference_data(c, build_pipeline_map(_MANIFEST), _now()+100)
        ps.install_identity_guards(c)
        ps.install_identity_guards(c)
        assert list(c.iterdump()) == before
    assert _register_owner(d, 'c2_idempotent_human') == first
    # New canonical identity creation remains lawful after reapplication.
    assert _register_owner(d, 'c2_another_human') != first


def _c2_historical_env(name, stage):
    """Frozen canonical DDL + unchanged original SLP2 DDL, no C2 guards.
    C1 is original SLP2 plus its real guarded column/NULL-only trigger.
    Does not depend on git or a production database at test time.
    """
    import provenance_schema as ps
    d = os.path.join(TEST_DIR, name)
    os.makedirs(d)
    path = os.path.join(d, 'anaxi_provenance.db')
    with sqlite3.connect(path) as c:
        for _, ddl in ps.ALL_DDL_BLOCKS:
            c.executescript(ddl)
        seed_reference_data(c, build_pipeline_map(_MANIFEST), _now())
    hir1_schema_migration.apply_additive_migration(path)
    human = _register_owner(d, name)
    if stage != 'pre_slp2':
        with _c2_connect(d) as c:
            c.executescript(slp2_schema_migration.NEW_TABLES_DDL)
        # Historical fixture writes use the historical column shape. The
        # modern application reader requires operator_actor_id already.
        req = 'historical-request'
        with _c2_connect(d) as c:
            c.execute('INSERT INTO events(event_id,event_type,pipeline_provenance_status,occurred_at,record_created_at) '
                      "VALUES (?,'clark_sleep_request','unknown',1,1)", (req,))
            c.execute("INSERT INTO sleep_requests VALUES (?,?,'AUTHORIZED',NULL,1,1)", (req, slp2._clark_actor_id()))
            c.execute("INSERT INTO sleep_execution_attempts VALUES ('historical',?,1,1,'not_established','unknown operator',1)", (req,))
            if stage == 'correction1':
                c.execute('ALTER TABLE sleep_execution_attempts ADD COLUMN operator_actor_id TEXT')
                c.execute(slp2_schema_migration._OPERATOR_REQUIRED_TRIGGER_DDL)
                _c2_attempt(c, req, 'historically-unregistered', 2)
    else:
        req = None
    return d, human, req


def test_correction2_migration_all_predecessors_preserve_history():
    import oc0_schema_migration as oc_schema
    import wtr0_schema_migration as wtr_schema
    import sleep_c_schema as sleep_schema
    for stage in ('pre_slp2', 'original_slp2', 'correction1'):
        d, human, req = _c2_historical_env('c2_upgrade_'+stage, stage)
        path = os.path.join(d, 'anaxi_provenance.db')
        # Real neighboring schemas, synthetic persistent rows. No Sleep engine.
        with _c2_connect(d) as c:
            for ddl in (oc_schema.NEW_TABLES_DDL, wtr_schema.NEW_TABLES_DDL, sleep_schema.NEW_TABLES_DDL):
                c.executescript(ddl)
            for event, kind in (('old-H','human_waking_input'), ('old-X','waking_turn'), ('old-O','clark_outward_act')):
                c.execute('INSERT INTO events(event_id,event_type,pipeline_provenance_status,occurred_at,record_created_at) VALUES (?,?,\'unknown\',1,1)', (event, kind))
            for event, actor in (('old-H', human), ('old-X', slp2._clark_actor_id())):
                c.execute('INSERT INTO event_components(event_id,sequence,creator_actor_id,component_kind,authorship_resolution,component_text,content_sha256) '
                          "VALUES (?,1,?,'conversational_prose','resolved','historical synthetic content','synthetic-hash')", (event, actor))
            c.execute("INSERT INTO outward_projection_attempts VALUES ('old-projection','old-O',1,'hash','not_established',1,1,'synthetic',1)")
            c.execute("INSERT INTO waking_failure_evidence VALUES ('old-failure','old-H','synthetic',0,'synthetic',NULL,1,0,1)")
            c.execute("INSERT INTO wtr0_recovery(recovery_id,human_input_event_id,failure_evidence_id,eligibility_decision,eligibility_basis,reserved_at,updated_at) VALUES ('old-recovery','old-H','old-failure','synthetic','synthetic',1,1)")
            c.execute("INSERT INTO sleep_cycles VALUES ('old-cycle',1,1,0,0,NULL,1,2)")
        with _c2_connect(d) as c:
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            before = {table: c.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
            old_triggers = dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
        for _ in range(2):
            slp2_schema_migration.apply_additive_migration(path)
            assert all(slp2_schema_migration.verify_migration_state(path).values())
            with _c2_connect(d) as c:
                for table, rows in before.items():
                    after = c.execute(f'SELECT * FROM {table}').fetchall()
                    if table == 'sleep_execution_attempts' and stage == 'original_slp2':
                        assert after == [r+(None,) for r in rows]
                    else:
                        assert after == rows, table
                for name, ddl in old_triggers.items():
                    assert c.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0] == ddl
                c.execute('PRAGMA foreign_keys=OFF')
                c.execute('PRAGMA recursive_triggers=OFF')
                _c2_reject(c, 'UPDATE actors SET actor_id=? WHERE actor_id=?', ('renamed', human))
                _c2_reject(c, 'INSERT OR REPLACE INTO actors(actor_id,actor_type,stable_key,created_at) '
                           "VALUES (?,'host_system',?,1)", (human, human))
                _c2_reject(c, 'REPLACE INTO actor_human_person(actor_id,person_id,relationship_established_at) '
                           "VALUES (?,'other',1)", (human,))
        if req is None:
            req, _ = _make_pending(d)
            slp2.apply_owner_transition(d, request_id=req, owner_actor_id=human, action='AUTHORIZE', occurred_at=_now())
        status = slp2.fetch_sleep_request(d, req)
        expected = [] if stage == 'pre_slp2' else [None]
        if stage == 'correction1':
            expected += ['historically-unregistered']
        assert [a['operator_actor_id'] for a in status['execution_attempts']] == expected
        with _c2_connect(d, 0) as c:
            try:
                _c2_attempt(c, req, 'unknown', 10)
            except sqlite3.IntegrityError:
                c.rollback()
            else:
                raise AssertionError('upgraded storage accepted unknown operator')
            _c2_attempt(c, req, human, 10)
        assert slp2.fetch_sleep_request(d, req)['execution_attempts'][-1]['operator_actor_id'] == human


def test_correction2_migration_failure_rolls_back_tables_columns_and_guards():
    from unittest.mock import patch
    # Failure after the identity guards have been installed, before COMMIT.
    for stage in ('pre_slp2', 'original_slp2', 'correction1'):
        d, _, _ = _c2_historical_env('c2_rollback_'+stage, stage)
        path = os.path.join(d, 'anaxi_provenance.db')
        with _c2_connect(d) as c:
            before = list(c.iterdump())
        with patch.object(slp2_schema_migration, '_OPERATOR_HUMAN_TRIGGER_DDL', 'INVALID SQL'):
            try:
                slp2_schema_migration.apply_additive_migration(path)
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError('injected migration failure not raised')
        with _c2_connect(d) as c:
            assert list(c.iterdump()) == before
        slp2_schema_migration.apply_additive_migration(path)
        assert all(slp2_schema_migration.verify_migration_state(path).values())


# --------------------------------------------------------------- runner ---

ALL_TESTS = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def main():
    failed = 0
    for test in ALL_TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(ALL_TESTS)} tests, {len(ALL_TESTS) - failed} passed, {failed} failed")
    import shutil
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
