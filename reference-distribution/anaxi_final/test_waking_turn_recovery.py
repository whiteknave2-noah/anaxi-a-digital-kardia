"""WTR0 permanent regression suite: "Did I break Waking Turn Recovery?"

Run: `python3 -B anaxi_final/test_waking_turn_recovery.py`
or:  `python3 -B -m pytest anaxi_final/test_waking_turn_recovery.py`

Every fixture is synthetic/temporary -- a fresh tempdir provenance DB
per Env, created via the REAL provenance_schema.create_provenance_db(),
REAL migrate_historical_data.seed_reference_data(), and REAL
hir1_registration.register_canonical_human(). No test opens, reads, or
writes the real anaxi_provenance.db. No test performs real model
inference -- every provider/model boundary used here (`ollama`-shaped
fakes, fake `waking`/`run_waking_turn_fn` callables) is synthetic. No
Sleep, no Private Space, no GUI, no network, anywhere in this file.

Fixture: `Env` -- provenance DB + registered human actor + WTR0 schema.
Used for every test in this file. Where a test needs a genuine
canonical X, it persists one through the REAL native_provenance_writer.
stage_and_record_native_waking_turn() writer directly (the same
technique test_13_success_followed_by_another_recovery_is_denied()
already uses) -- never through llama_anaxi.run_waking_turn() itself,
which would pull in numpy via llama_anaxi -> orchestration ->
anaxi_protocol_sqlite and is unavailable in this sandbox (external_
network=deny forbids installing it; see this repository's other
end-to-end suites for the same documented limitation).

WTR0-CORRECTION-1 removed this file's prior WakingEnv fixture and the
three tests that required it (they imported the numpy-dependent
llama_anaxi chain and were reported SKIPPED here, which the frozen
WTR0 test-discipline contract forbids as a *newly introduced* skip).
The claims those three tests were meant to establish --
(1) a genuine retry-safe waking failure becomes eligible through the
real run_waking_turn_capturing_failure() control flow, (2) an ordinary
successful turn through the same wrapper writes no WTR0 row, and
(3) a full failure-then-recovery lifecycle persists exactly one
canonical X and closes further recovery -- are re-established below
through synthetic/mock provider and `run_waking_turn_fn` boundaries
that exercise the REAL run_waking_turn_capturing_failure() and
execute_recovery() code, never a numpy-dependent import. The one
property genuinely specific to the real llama_anaxi.run_waking_turn()
integration (that wiring it through the capture wrapper inside an
actually-running Gradio process behaves identically) is left
NOT_ESTABLISHED in this sandbox; see this correction's builder
evidence for the exact command to establish it once numpy is
available.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import types
import uuid

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
if ANAXI_FINAL not in sys.path:
    sys.path.insert(0, ANAXI_FINAL)

from provenance_schema import create_provenance_db
from migrate_historical_data import build_pipeline_map, seed_reference_data, alter_existing_stores_schema
from hir1_registration import register_canonical_human
import hir1_schema_migration
import human_session_binding
import wtr0_schema_migration
import wtr0_waking_recovery as recovery
from wtr0_waking_recovery import EligibilityDecision, RecoveryDenied
from wtr0_cold_reset import cold_reset_waking_inference_path, ColdResetOutcome
import waking_failure_evidence as wfe
from waking_turn_failure_capture import run_waking_turn_capturing_failure
import ui_turn_diagnostics
import resume_human_input
from conversation_direction import ConversationDirectionFailure
from native_turn_staging import StagingDurabilityError, StagingIndeterminateStateError
from native_provenance_writer import stage_and_record_native_waking_turn
import context_budget

TEST_ROOT = tempfile.mkdtemp(prefix="wtr0_test_")


# =============================================================== fixtures


class Env:
    """Light fixture: provenance DB + registered human actor + WTR0
    schema. No waking-turn machinery."""

    def __init__(self, name):
        self.tmp_root = os.path.join(TEST_ROOT, name)
        os.makedirs(self.tmp_root, exist_ok=True)
        self.db_path = os.path.join(self.tmp_root, "anaxi_provenance.db")
        create_provenance_db(self.db_path).close()

        manifest = {"pipelines": {"llama": {"routing_constant_value": "nate"},
                                   "claude": {"routing_constant_value": "nate"}}}
        self.pipeline_map = build_pipeline_map(manifest)
        self.pipeline_key = "anaxi_orchestration_lineage_a"

        hir1_schema_migration.apply_additive_migration(self.db_path)

        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        seed_reference_data(conn, self.pipeline_map, int(time.time()))
        conn.row_factory = sqlite3.Row
        result, failure = register_canonical_human(conn, {
            "registration_request_id": f"req-{uuid.uuid4()}",
            "aab_actor_id": f"aab-{uuid.uuid4()}",
            "display_label": "Test Human",
            "source": "local_operator_provisioning",
        })
        assert failure is None, failure
        conn.commit()
        conn.close()
        self.actor_id = result["actor_id"]

        wtr0_schema_migration.apply_additive_migration(self.db_path)

    def make_h(self, message="Hello, this is an unanswered synthetic H."):
        """Creates a real canonical human_waking_input event via the
        REAL human_session_binding writer -- the same mechanism
        run_waking_turn() itself uses -- without exercising the rest
        of the waking pipeline."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        authority = human_session_binding.bind_session_to_registered_human(
            conn, session_id=f"sess-{uuid.uuid4()}", session_started_at=int(time.time()),
            pipeline_key=self.pipeline_key, actor_id=self.actor_id,
        )
        conn.commit()
        conn.close()
        h_id = human_session_binding.record_human_waking_input(
            self.tmp_root, pipeline_key=self.pipeline_key, authority=authority,
            message=message, occurred_at=int(time.time()),
        )
        return h_id, authority

    def conn(self):
        c = sqlite3.connect(self.db_path)
        c.execute("PRAGMA foreign_keys = ON;")
        return c

    def record_evidence(self, h_id, failure_class, basis="synthetic test evidence"):
        return wfe.record_waking_failure(
            self.db_path, human_input_event_id=h_id, failure_class=failure_class, basis=basis,
        )


# =============================================================== migration


def test_migration_fresh_install_creates_tables():
    env = Env("mig_fresh")
    state = wtr0_schema_migration.verify_migration_state(env.db_path)
    assert state == {"has_waking_failure_evidence": True, "has_wtr0_recovery": True}


def test_migration_idempotent_reapplication():
    env = Env("mig_idempotent")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
    result_1 = wtr0_schema_migration.apply_additive_migration(env.db_path)
    assert result_1 == {"new_tables_created": []}  # already created in Env.__init__
    conn = env.conn()
    rows_before = conn.execute("SELECT COUNT(*) FROM waking_failure_evidence").fetchone()[0]
    conn.close()
    wtr0_schema_migration.apply_additive_migration(env.db_path)
    conn = env.conn()
    rows_after = conn.execute("SELECT COUNT(*) FROM waking_failure_evidence").fetchone()[0]
    conn.close()
    assert rows_before == rows_after == 1


def test_migration_preserves_existing_rows_on_upgrade():
    """A DB that already carries production rows (events/event_components,
    seeded here via make_h() before wtr0 migration ever runs on it a
    second time) is byte/value-identical afterward."""
    env = Env("mig_upgrade")
    h_id, _ = env.make_h("Preserve me across the WTR0 migration.")
    conn = env.conn()
    before = conn.execute(
        "SELECT ec.component_text FROM events e JOIN event_components ec ON ec.event_id = e.event_id "
        "WHERE e.event_id = ?", (h_id,),
    ).fetchone()
    conn.close()
    wtr0_schema_migration.apply_additive_migration(env.db_path)  # reapply
    conn = env.conn()
    after = conn.execute(
        "SELECT ec.component_text FROM events e JOIN event_components ec ON ec.event_id = e.event_id "
        "WHERE e.event_id = ?", (h_id,),
    ).fetchone()
    conn.close()
    assert before == after == ("Preserve me across the WTR0 migration.",)


def test_deployment_migration_creates_truthfully_empty_prospective_state():
    """A pre-existing unanswered H is canonical history, not evidence.
    Installing WTR0 after that H creates the two tables but backfills
    neither failure evidence nor a recovery reservation."""
    root = os.path.join(TEST_ROOT, "prospective_only_migration")
    os.makedirs(root, exist_ok=True)
    db_path = os.path.join(root, "anaxi_provenance.db")
    create_provenance_db(db_path).close()
    manifest = {"pipelines": {"llama": {"routing_constant_value": "nate"},
                                "claude": {"routing_constant_value": "nate"}}}
    pipeline_map = build_pipeline_map(manifest)
    hir1_schema_migration.apply_additive_migration(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    seed_reference_data(conn, pipeline_map, int(time.time()))
    conn.row_factory = sqlite3.Row
    result, failure = register_canonical_human(conn, {
        "registration_request_id": f"req-{uuid.uuid4()}",
        "aab_actor_id": f"aab-{uuid.uuid4()}",
        "display_label": "Test Human",
        "source": "local_operator_provisioning",
    })
    assert failure is None
    authority = human_session_binding.bind_session_to_registered_human(
        conn, session_id=f"sess-{uuid.uuid4()}", session_started_at=int(time.time()),
        pipeline_key="anaxi_orchestration_lineage_a", actor_id=result["actor_id"],
    )
    conn.commit()
    conn.close()
    historical_h = human_session_binding.record_human_waking_input(
        root, pipeline_key="anaxi_orchestration_lineage_a", authority=authority,
        message="Existing unanswered input; migration must not reinterpret me.",
        occurred_at=int(time.time()),
    )

    assert wtr0_schema_migration.verify_migration_state(db_path) == {
        "has_waking_failure_evidence": False, "has_wtr0_recovery": False,
    }
    receipt = wtr0_schema_migration.apply_additive_migration(db_path)
    assert receipt["new_tables_created"] == ["waking_failure_evidence", "wtr0_recovery"]
    assert wtr0_schema_migration.bookkeeping_row_counts(db_path) == {
        "waking_failure_evidence": 0, "wtr0_recovery": 0,
    }
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM waking_failure_evidence").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM wtr0_recovery").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event_id=?", (historical_h,)).fetchone()[0] == 1


def test_deployment_cli_refuses_to_create_a_mistyped_database_path():
    missing = os.path.join(TEST_ROOT, "mistyped", "anaxi_provenance.db")
    try:
        wtr0_schema_migration.main(["--db", missing])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("deployment CLI accepted a nonexistent target")
    assert not os.path.exists(missing)


def test_new_h_may_follow_unrecoverable_h_and_only_new_failure_is_evidenced():
    """Forward continuity: an older unanswered/no-evidence H does not
    block a new authenticated H. A future exact-H failure is attributed
    only to the new attempt and becomes eligible prospectively."""
    env = Env("forward_continuity")
    old_h, _ = env.make_h("Older unanswered H with no WTR0 evidence.")
    new_h, _ = env.make_h("New ordinary waking input after the older H.")

    assert resume_human_input.read_unanswered_input(env.db_path, old_h, env.actor_id)
    assert resume_human_input.read_unanswered_input(env.db_path, new_h, env.actor_id)

    expected = ConversationDirectionFailure("pass1", context_budget.BUDGET_EXCEEDED)

    def fail_current_attempt(*args, **kwargs):
        ui_turn_diagnostics.record_human_input_event_id(new_h)
        raise expected

    try:
        run_waking_turn_capturing_failure(
            fail_current_attempt, object(), "New ordinary waking input after the older H.",
            provenance_db_path=env.db_path,
        )
    except ConversationDirectionFailure as caught:
        assert caught is expected
    else:
        raise AssertionError("synthetic future waking failure did not propagate")

    with env.conn() as conn:
        rows = conn.execute(
            "SELECT human_input_event_id, retry_safe FROM waking_failure_evidence ORDER BY rowid"
        ).fetchall()
        assert rows == [(new_h, 1)]
        old_receipt = recovery.assess_eligibility(conn, old_h, env.actor_id)
        new_receipt = recovery.assess_eligibility(conn, new_h, env.actor_id)
    assert old_receipt["decision"] == EligibilityDecision.NOT_ESTABLISHED
    assert old_receipt["basis"] == "NO_FAILURE_EVIDENCE"
    assert new_receipt["decision"] == EligibilityDecision.ELIGIBLE


# =========================================================== negative proof


def test_1_h_does_not_exist_is_ineligible():
    env = Env("neg1")
    conn = env.conn()
    receipt = recovery.assess_eligibility(conn, "event-does-not-exist", env.actor_id)
    conn.close()
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE
    assert receipt["basis"] == "H_NOT_FOUND"


def test_2_h_already_has_x_is_ineligible():
    env = Env("neg2")
    h_id, authority = env.make_h()
    # Directly link a canonical waking_turn (X) event to H via the
    # REAL native_provenance_writer, exactly as an ordinary successful
    # waking turn would -- never a fabricated/ad-hoc row.
    from native_provenance_writer import stage_and_record_native_waking_turn
    session_id, session_started_at = "sess-x", int(time.time())
    stage_and_record_native_waking_turn(
        env.tmp_root, os.path.join(env.tmp_root, "native_turn_staging.jsonl"),
        session_id=session_id, session_started_at=session_started_at, user_id="nate",
        prompt="irrelevant", bounded_clause="", clark_prose="An ordinary real reply.",
        kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=env.pipeline_key,
        artifact_pass_ran=False, occurred_at=int(time.time()), human_input_event_id=h_id,
    )
    conn = env.conn()
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE
    assert receipt["basis"] == "X_ALREADY_LINKED"


def test_3_no_failure_evidence_is_not_established():
    env = Env("neg3")
    h_id, _ = env.make_h()
    conn = env.conn()
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert receipt["decision"] == EligibilityDecision.NOT_ESTABLISHED
    assert receipt["basis"] == "NO_FAILURE_EVIDENCE"


def test_4_not_retry_safe_evidence_is_ineligible():
    env = Env("neg4")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "AMBIGUOUS_PERSISTENCE_OUTCOME")
    conn = env.conn()
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE
    assert receipt["basis"] == "FAILURE_NOT_RETRY_SAFE"


def test_5_unknown_failure_class_is_rejected_outright():
    env = Env("neg5")
    h_id, _ = env.make_h()
    raised = False
    try:
        env.record_evidence(h_id, "SOME_MADE_UP_CLASS")
    except wfe.UnknownFailureClassError:
        raised = True
    assert raised
    conn = env.conn()
    count = conn.execute("SELECT COUNT(*) FROM waking_failure_evidence").fetchone()[0]
    conn.close()
    assert count == 0


def test_6_qualifying_retry_safe_failure_is_eligible():
    env = Env("neg6")
    h_id, _ = env.make_h()
    ev_id = env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
    conn = env.conn()
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert receipt["decision"] == EligibilityDecision.ELIGIBLE
    assert receipt["failure_evidence_id"] == ev_id


def test_7_x_appearing_between_eligibility_and_reservation_denies_execution():
    env = Env("neg7")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
    conn = env.conn()
    pre_check = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert pre_check["decision"] == EligibilityDecision.ELIGIBLE

    # X appears (an independent ordinary success happened) after the
    # first eligibility read but before reservation.
    from native_provenance_writer import stage_and_record_native_waking_turn
    stage_and_record_native_waking_turn(
        env.tmp_root, os.path.join(env.tmp_root, "native_turn_staging.jsonl"),
        session_id="sess-race-x", session_started_at=int(time.time()), user_id="nate",
        prompt="irrelevant", bounded_clause="", clark_prose="Beat WTR0 to it.",
        kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=env.pipeline_key,
        artifact_pass_ran=False, occurred_at=int(time.time()), human_input_event_id=h_id,
    )

    denied = False
    try:
        recovery.reserve_recovery(env.db_path, h_id, env.actor_id)
    except RecoveryDenied as e:
        denied = True
        assert e.receipt["basis"] == "X_ALREADY_LINKED"
    assert denied
    conn = env.conn()
    count = conn.execute("SELECT COUNT(*) FROM wtr0_recovery WHERE human_input_event_id = ?", (h_id,)).fetchone()[0]
    conn.close()
    assert count == 0


def test_8_second_reservation_for_same_h_is_denied():
    env = Env("neg8")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
    recovery_id_1 = recovery.reserve_recovery(env.db_path, h_id, env.actor_id)
    assert recovery_id_1
    denied = False
    try:
        recovery.reserve_recovery(env.db_path, h_id, env.actor_id)
    except RecoveryDenied as e:
        denied = True
        assert e.receipt["basis"] == "RECOVERY_ALREADY_RESERVED_OR_CONSUMED"
    assert denied
    conn = env.conn()
    count = conn.execute("SELECT COUNT(*) FROM wtr0_recovery WHERE human_input_event_id = ?", (h_id,)).fetchone()[0]
    conn.close()
    assert count == 1


def test_8b_concurrent_reservation_race_allows_only_one_winner():
    import threading
    env = Env("neg8b")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    results = []

    def attempt():
        try:
            rid = recovery.reserve_recovery(env.db_path, h_id, env.actor_id)
            results.append(("won", rid))
        except RecoveryDenied as e:
            results.append(("denied", e.receipt["basis"]))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wins = [r for r in results if r[0] == "won"]
    assert len(wins) == 1, f"expected exactly one winner, got {results}"
    conn = env.conn()
    count = conn.execute("SELECT COUNT(*) FROM wtr0_recovery WHERE human_input_event_id = ?", (h_id,)).fetchone()[0]
    conn.close()
    assert count == 1


def test_9_reset_failure_stops_before_generation_and_consumes_attempt():
    env = Env("neg9")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
    generation_calls = []

    def reset_fn():
        return {"status": ColdResetOutcome.FAILED, "basis": "synthetic reset failure"}

    def generation_fn(prompt):
        generation_calls.append(prompt)
        return {"native_event_id": "should-never-happen"}

    outcome = recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    assert outcome["terminal_state"] == "RESET_FAILED"
    assert generation_calls == []

    denied = False
    try:
        recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    except RecoveryDenied:
        denied = True
    assert denied


def test_9b_unknown_reset_outcome_also_stops_before_generation():
    env = Env("neg9b")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
    generation_calls = []

    def reset_fn():
        return {"status": ColdResetOutcome.UNKNOWN, "basis": "ps() unavailable"}

    def generation_fn(prompt):
        generation_calls.append(prompt)
        return {"native_event_id": "should-never-happen"}

    outcome = recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    assert outcome["terminal_state"] == "RESET_FAILED"
    assert generation_calls == []


def test_10_generation_failure_after_successful_reset_consumes_attempt():
    env = Env("neg10")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    def reset_fn():
        return {"status": ColdResetOutcome.SUCCEEDED, "basis": "ok"}

    def generation_fn(prompt):
        raise RuntimeError("synthetic generation failure")

    outcome = recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    assert outcome["terminal_state"] == "GENERATION_FAILED"

    denied = False
    try:
        recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    except RecoveryDenied:
        denied = True
    assert denied
    conn = env.conn()
    x = recovery._linked_canonical_x_event_id(conn, h_id)
    conn.close()
    assert x is None


def test_11_persistence_failure_after_generation_consumes_attempt_no_fabricated_x():
    env = Env("neg11")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    def reset_fn():
        return {"status": ColdResetOutcome.SUCCEEDED, "basis": "ok"}

    def generation_fn(prompt):
        return {"reply": "a real model reply that was never persisted"}  # no native_event_id

    outcome = recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    assert outcome["terminal_state"] == "PERSISTENCE_FAILED"

    denied = False
    try:
        recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    except RecoveryDenied:
        denied = True
    assert denied
    conn = env.conn()
    x = recovery._linked_canonical_x_event_id(conn, h_id)
    conn.close()
    assert x is None, "no canonical X may ever be fabricated from an unpersisted generation"


def test_11b_linked_x_without_substantive_prose_cannot_be_success():
    """A linked row called X is not equivalent to visible delivery."""
    env = Env("neg11b_non_substantive_x")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    def reset_fn():
        return {"status": ColdResetOutcome.SUCCEEDED, "basis": "ok"}

    def generation_fn(prompt):
        result = stage_and_record_native_waking_turn(
            env.tmp_root, os.path.join(env.tmp_root, "native_turn_staging.jsonl"),
            session_id="sess-empty-recovery", session_started_at=int(time.time()), user_id="nate",
            prompt=prompt, bounded_clause="", clark_prose="",
            kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=env.pipeline_key,
            artifact_pass_ran=False, occurred_at=int(time.time()), human_input_event_id=h_id,
        )
        return {"native_event_id": result["event_id"]}

    outcome = recovery.execute_recovery(
        env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn,
    )
    assert outcome["terminal_state"] == "PERSISTENCE_UNCONFIRMED"
    assert outcome["canonical_x_event_id"]
    assert "conversational_prose" in outcome["error"]
    with env.conn() as conn:
        status = recovery.recovery_status(conn, h_id)
    assert status["persistence_status"] == "UNCONFIRMED"
    assert status["terminal_state"] == "PERSISTENCE_UNCONFIRMED"


def test_13_success_followed_by_another_recovery_is_denied():
    env = Env("neg13")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    def reset_fn():
        return {"status": ColdResetOutcome.SUCCEEDED, "basis": "ok"}

    from native_provenance_writer import stage_and_record_native_waking_turn

    def generation_fn(prompt):
        result = stage_and_record_native_waking_turn(
            env.tmp_root, os.path.join(env.tmp_root, "native_turn_staging.jsonl"),
            session_id="sess-real-recovery", session_started_at=int(time.time()), user_id="nate",
            prompt=prompt, bounded_clause="", clark_prose="A genuinely persisted recovery reply.",
            kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=env.pipeline_key,
            artifact_pass_ran=False, occurred_at=int(time.time()), human_input_event_id=h_id,
        )
        return {"native_event_id": result["event_id"]}

    outcome = recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    assert outcome["terminal_state"] == "SUCCESS"
    x_event_id = outcome["canonical_x_event_id"]

    conn = env.conn()
    linked = recovery._linked_canonical_x_event_id(conn, h_id)
    h_count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'human_waking_input'"
    ).fetchone()[0]
    conn.close()
    assert linked == x_event_id
    assert h_count == 1, "no duplicate H may ever be created by recovery"

    denied = False
    try:
        recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    except RecoveryDenied as e:
        denied = True
        assert e.receipt["basis"] in ("X_ALREADY_LINKED", "RECOVERY_ALREADY_RESERVED_OR_CONSUMED")
    assert denied


def test_14_process_restart_ceiling_survives_fresh_connection():
    env = Env("neg14")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
    recovery.reserve_recovery(env.db_path, h_id, env.actor_id)

    # Simulate a fresh process: brand-new connection, no in-memory
    # state carried over at all (module-level state in this process
    # holds nothing recovery-specific either -- everything durable
    # lives in wtr0_recovery).
    fresh_conn = sqlite3.connect(env.db_path)
    fresh_conn.execute("PRAGMA foreign_keys = ON;")
    receipt = recovery.assess_eligibility(fresh_conn, h_id, env.actor_id)
    fresh_conn.close()
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE
    assert receipt["basis"] == "RECOVERY_ALREADY_RESERVED_OR_CONSUMED"


def test_16_unrelated_h_recovery_state_is_isolated():
    env = Env("neg16")
    h1, _ = env.make_h("First unanswered input.")
    h2, _ = env.make_h("Second, unrelated unanswered input.")
    env.record_evidence(h1, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
    env.record_evidence(h2, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    recovery.reserve_recovery(env.db_path, h1, env.actor_id)

    conn = env.conn()
    r1 = recovery.assess_eligibility(conn, h1, env.actor_id)
    r2 = recovery.assess_eligibility(conn, h2, env.actor_id)
    conn.close()
    assert r1["decision"] == EligibilityDecision.INELIGIBLE
    assert r2["decision"] == EligibilityDecision.ELIGIBLE


def test_17_ordinary_unanswered_h_without_evidence_is_never_auto_promoted():
    env = Env("neg17")
    h_id, _ = env.make_h()
    conn = env.conn()
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert receipt["decision"] != EligibilityDecision.ELIGIBLE
    assert receipt["decision"] == EligibilityDecision.NOT_ESTABLISHED


def test_caller_supplied_replacement_text_is_never_used_for_recovery():
    """Recovery must load the canonical prompt from H, never trust a
    caller-supplied replacement -- execute_recovery()'s generation_fn
    is always called with the canonically-loaded prompt, not anything
    a caller might pass elsewhere."""
    env = Env("no_fabricated_text")
    canonical_text = "The one true canonical prompt text."
    h_id, _ = env.make_h(canonical_text)
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    seen = {}

    def reset_fn():
        return {"status": ColdResetOutcome.SUCCEEDED, "basis": "ok"}

    def generation_fn(prompt):
        seen["prompt"] = prompt
        return {"native_event_id": None}

    recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    assert seen["prompt"] == canonical_text


# ===================================================== real waking pipeline


def test_real_reset_boundary_ordering_and_confirmation():
    """cold_reset_waking_inference_path() against a mocked ollama-like
    client: requests keep_alive=0 unload, confirms absence via ps(),
    and only then reports SUCCEEDED. If the model is still reported
    resident, it must report FAILED, never SUCCEEDED."""
    calls = []

    class FakeOllama:
        def chat(self, model, messages, keep_alive=None, **kw):
            calls.append(("chat", model, keep_alive))
            return {"message": {"content": ""}}

        def ps(self):
            calls.append(("ps",))
            return {"models": []}

    result = cold_reset_waking_inference_path(FakeOllama(), "gemma4:e4b")
    assert result["status"] == ColdResetOutcome.SUCCEEDED
    assert calls[0] == ("chat", "gemma4:e4b", 0)
    assert calls[1] == ("ps",)

    class StillLoadedOllama:
        def chat(self, model, messages, keep_alive=None, **kw):
            return {"message": {"content": ""}}

        def ps(self):
            return {"models": [{"model": "gemma4:e4b"}]}

    result_still_loaded = cold_reset_waking_inference_path(StillLoadedOllama(), "gemma4:e4b")
    assert result_still_loaded["status"] == ColdResetOutcome.FAILED


# ============================================= WTR0-CORRECTION-4
# cold reset must positively establish model absence before SUCCEEDED
# -- missing/unrecognized/malformed ps() observation must fail closed
# to UNKNOWN, never be silently treated as an empty (= absent) list.
#
# _RealShapedProcessResponse/_RealShapedModel are minimal structural
# stand-ins for the actual, locally-installed `ollama==0.6.2` package's
# `ollama.ProcessResponse`/`ollama.ProcessResponse.Model` pydantic
# classes (traced directly from that installed package's _types.py:
# `ProcessResponse.models: Sequence[Model]`, `Model.model`/`Model.name`
# both Optional[str]) -- reproducing their SHAPE (a `.models` sequence
# of objects exposing `.model`/`.name`) without depending on `ollama`
# itself being importable in this sandbox. `_ollama_module()` builds a
# synthetic client exposing this same `ProcessResponse` class as an
# attribute, exactly as the real `ollama` module does, so
# `_recognized_models_collection()`'s `isinstance(loaded,
# ollama_module.ProcessResponse)` check is exercised against a
# genuinely structurally-faithful stand-in class, not a name-only or
# duck-typed guess.


class _RealShapedModel:
    def __init__(self, model=None, name=None):
        self.model = model
        self.name = name


class _RealShapedProcessResponse:
    Model = _RealShapedModel

    def __init__(self, models):
        self.models = models


def _ollama_module(ps_result=None, ps_raises=None, chat_raises=None):
    """Builds a synthetic ollama-like client. `ps_result` may be a
    plain value (returned as-is by `ps()`, e.g. a dict, None, or a
    `_RealShapedProcessResponse` instance) or a zero-arg callable
    (invoked to produce the value, for cases that also need to record
    call order). Exposes `ProcessResponse` as a module-level attribute,
    exactly like the real `ollama` package, so this module's
    real-response-shape recognition path can be exercised."""
    class _Module:
        ProcessResponse = _RealShapedProcessResponse

        def chat(self, model, messages, keep_alive=None, **kw):
            if chat_raises is not None:
                raise chat_raises
            return {"message": {"content": ""}}

        def ps(self):
            if ps_raises is not None:
                raise ps_raises
            return ps_result() if callable(ps_result) else ps_result

    return _Module()


def test_correction_4_defect_reproduction_missing_models_field_pre_fix_was_false_absence():
    """Verifier reproduction: run the EXACT pre-correction-4 parsing
    expression (`models = loaded.get("models", []) if isinstance(loaded,
    dict) else []`) directly against `{}` and `None` to prove it
    silently produced an empty list -- i.e. false confirmed-absence --
    for both. This is the precise mechanism the independent verifier
    reported: missing/unavailable residency data was converted into
    positive absence rather than failing closed."""
    for pre_fix_response in ({}, None):
        loaded = pre_fix_response
        models = loaded.get("models", []) if isinstance(loaded, dict) else []
        assert models == [], (
            "reproduces the exact pre-correction-4 defect: a missing/unavailable ps() "
            "observation silently became an EMPTY (= confirmed absent) list"
        )

    env = Env("correction4_repro_missing_field")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    # Post-fix: the real cold_reset_waking_inference_path() must NOT
    # reach the same false-absence conclusion for either response.
    for ps_result in ({}, None):
        result = cold_reset_waking_inference_path(_ollama_module(ps_result=ps_result), "gemma4:e4b")
        assert result["status"] == ColdResetOutcome.UNKNOWN, (
            f"post-fix, ps()->{ps_result!r} must never be treated as confirmed absence"
        )
        assert "confirmed absent" not in result["basis"]


def test_correction_4_recognized_dict_response_target_absent_succeeds():
    """Case 1: a recognized dict response with a valid residency
    collection that genuinely does not contain the target model ->
    SUCCEEDED, and the receipt's own basis text claims positive
    confirmation (never merely absence of information)."""
    result = cold_reset_waking_inference_path(
        _ollama_module(ps_result={"models": [{"model": "other-model:latest"}]}), "gemma4:e4b",
    )
    assert result["status"] == ColdResetOutcome.SUCCEEDED
    assert "confirmed absent" in result["basis"] or "positively confirmed absent" in result["basis"]


def test_correction_4_recognized_process_response_target_absent_succeeds():
    """Same as above, but through the real locally-justified
    `ollama.ProcessResponse`-shaped object (a `.models` sequence of
    objects exposing `.model`/`.name`) instead of a dict -- proving the
    fix recognizes the actual pinned client library's real response
    type, not only the pre-existing dict-based test convention."""
    response = _RealShapedProcessResponse(models=[_RealShapedModel(model="other-model:latest")])
    result = cold_reset_waking_inference_path(_ollama_module(ps_result=response), "gemma4:e4b")
    assert result["status"] == ColdResetOutcome.SUCCEEDED


def test_correction_4_recognized_process_response_target_present_blocks():
    """Case 2: a recognized ProcessResponse-shaped object that DOES
    contain the target model resident -> reset must not succeed."""
    response = _RealShapedProcessResponse(models=[_RealShapedModel(name="gemma4:e4b")])
    result = cold_reset_waking_inference_path(_ollama_module(ps_result=response), "gemma4:e4b")
    assert result["status"] == ColdResetOutcome.FAILED


def test_correction_4_none_response_is_not_established():
    """Case 3: ps() -> None must never be "confirmed absent"."""
    result = cold_reset_waking_inference_path(_ollama_module(ps_result=None), "gemma4:e4b")
    assert result["status"] == ColdResetOutcome.UNKNOWN
    assert "confirmed absent" not in result["basis"]


def test_correction_4_empty_dict_missing_models_field_is_not_established():
    """Case 4: ps() -> {} (the models field itself is absent, not an
    empty list) must fail closed -- missing collection is NOT the same
    as an empty collection."""
    result = cold_reset_waking_inference_path(_ollama_module(ps_result={}), "gemma4:e4b")
    assert result["status"] == ColdResetOutcome.UNKNOWN


def test_correction_4_explicit_empty_collection_is_positive_absence():
    """Case 5: a RECOGNIZED response with an explicit, positively-
    present empty residency collection ({"models": []}, and the
    ProcessResponse-shaped equivalent) is a genuine, locally-justified
    representation of "nothing resident" (this is what the real
    `ollama` client returns whenever no model happens to be loaded --
    its `models` field is a required, never-omitted sequence) and must
    be distinguished from case 4's MISSING field -- both must remain
    positively distinguishable, and only this one may succeed."""
    dict_result = cold_reset_waking_inference_path(_ollama_module(ps_result={"models": []}), "gemma4:e4b")
    assert dict_result["status"] == ColdResetOutcome.SUCCEEDED

    process_response_result = cold_reset_waking_inference_path(
        _ollama_module(ps_result=_RealShapedProcessResponse(models=[])), "gemma4:e4b",
    )
    assert process_response_result["status"] == ColdResetOutcome.SUCCEEDED


def test_correction_4_unrecognized_object_is_not_established():
    """Case 6: an object whose shape is not one of the two locally-
    justified supported forms (not a dict, not the recognized
    ProcessResponse class) must fail closed, even though it superficially
    carries a plausible-looking `.models` attribute."""
    class _Unrecognized:
        def __init__(self):
            self.models = []

    result = cold_reset_waking_inference_path(_ollama_module(ps_result=_Unrecognized()), "gemma4:e4b")
    assert result["status"] == ColdResetOutcome.UNKNOWN


def test_correction_4_unrecognized_object_containing_target_like_data_is_not_established():
    """Case 7 (verifier's adversarial shape): an unsupported object
    that APPEARS to positively contain the target model resident must
    still be NOT_ESTABLISHED, never ABSENT -- and, just as importantly,
    never silently treated as PRESENT either. Recognition must precede
    any inspection of apparent content."""
    class _UnrecognizedButSuggestive:
        def __init__(self):
            self.models = [{"model": "gemma4:e4b"}]

    result = cold_reset_waking_inference_path(
        _ollama_module(ps_result=_UnrecognizedButSuggestive()), "gemma4:e4b",
    )
    assert result["status"] == ColdResetOutcome.UNKNOWN
    assert "confirmed absent" not in result["basis"]


def test_correction_4_forged_class_property_process_response_is_not_established():
    """Builder-side adversarial preflight finding: recognizing the
    `ollama.ProcessResponse` shape via `isinstance()` reopens the exact
    `__class__`-forgery bypass WTR0-CORRECTION-3 closed for exception
    classification. An unrelated object overriding `__class__` as a
    property returning the genuine `ProcessResponse` class -- while its
    actual runtime type, and therefore its `.models` data, is wholly
    unrelated/attacker-controlled -- must never be recognized, whether
    it claims an empty (absent) collection or a target-present one.
    `_recognized_models_collection()` uses `type(loaded) is
    process_response_cls`, never `isinstance()`, closing this."""
    class _ForgedProcessResponse:
        def __init__(self, models):
            self._models = models

        @property
        def models(self):
            return self._models

        @property
        def __class__(self):
            return _RealShapedProcessResponse

    forged_empty = _ForgedProcessResponse(models=[])
    assert isinstance(forged_empty, _RealShapedProcessResponse), "confirms the forgery fools isinstance()"
    assert type(forged_empty) is not _RealShapedProcessResponse

    result_claims_empty = cold_reset_waking_inference_path(_ollama_module(ps_result=forged_empty), "gemma4:e4b")
    assert result_claims_empty["status"] == ColdResetOutcome.UNKNOWN
    assert "confirmed absent" not in result_claims_empty["basis"]

    forged_present = _ForgedProcessResponse(models=[_RealShapedModel(model="gemma4:e4b")])
    result_claims_present = cold_reset_waking_inference_path(_ollama_module(ps_result=forged_present), "gemma4:e4b")
    assert result_claims_present["status"] == ColdResetOutcome.UNKNOWN


def test_correction_4_forged_class_property_entry_is_not_established():
    """Same forged-`__class__` attack, applied to one residency entry
    INSIDE an otherwise genuine, exact-type `ProcessResponse`: the
    entry overrides `__class__` to present as the trusted
    `ProcessResponse.Model` type while its actual runtime type is
    unrelated. Must fail closed -- a single forged entry must not let
    the surrounding genuine response be treated as authoritative."""
    class _ForgedModel:
        def __init__(self, model):
            self._model = model

        @property
        def model(self):
            return self._model

        @property
        def name(self):
            return None

        @property
        def __class__(self):
            return _RealShapedModel

    forged_entry = _ForgedModel(model="other-model:latest")
    assert isinstance(forged_entry, _RealShapedModel), "confirms the forgery fools isinstance()"
    assert type(forged_entry) is not _RealShapedModel

    genuine_response = _RealShapedProcessResponse(models=[forged_entry])
    result = cold_reset_waking_inference_path(_ollama_module(ps_result=genuine_response), "gemma4:e4b")
    assert result["status"] == ColdResetOutcome.UNKNOWN


def test_correction_4_genuine_subclass_of_process_response_does_not_inherit_trust():
    """Documents the exact-type-only policy decision for
    `ollama.ProcessResponse`: traced directly from the locally
    installed, pinned `ollama==0.6.2` package's `Client._request()`
    (`return cls(**self._request_raw(...).json())`, where `cls` is the
    literal `ProcessResponse` class passed by `ps()` itself), the real
    client NEVER constructs a subclass -- so even a genuine Python
    subclass of `ProcessResponse` is deliberately NOT recognized here,
    matching WTR0-CORRECTION-3's own exact-type-identity precedent."""
    class _GenuineSubclass(_RealShapedProcessResponse):
        pass

    genuine_subclass_instance = _GenuineSubclass(models=[_RealShapedModel(model="other-model:latest")])
    assert isinstance(genuine_subclass_instance, _RealShapedProcessResponse)
    assert type(genuine_subclass_instance) is not _RealShapedProcessResponse

    result = cold_reset_waking_inference_path(
        _ollama_module(ps_result=genuine_subclass_instance), "gemma4:e4b",
    )
    assert result["status"] == ColdResetOutcome.UNKNOWN


def test_correction_4_malformed_residency_entry_is_not_established():
    """Case 8: a recognized OUTER dict response containing a
    nonconforming (non-dict) entry must fail closed -- a malformed
    entry is never silently skipped and the remainder treated as
    authoritative."""
    result = cold_reset_waking_inference_path(
        _ollama_module(ps_result={"models": ["not-a-dict-entry"]}), "gemma4:e4b",
    )
    assert result["status"] == ColdResetOutcome.UNKNOWN


def test_correction_4_entry_missing_model_identity_is_not_established():
    """Case 9: a recognized entry that carries no `model`/`name`
    identity field at all cannot be positively ruled out as the target
    -- fail closed rather than silently ignore it."""
    result = cold_reset_waking_inference_path(
        _ollama_module(ps_result={"models": [{"digest": "sha256:abcdef"}]}), "gemma4:e4b",
    )
    assert result["status"] == ColdResetOutcome.UNKNOWN


def test_correction_4_mixed_valid_and_malformed_entries_fails_closed():
    """Case 10: even when a VALID entry among the collection does not
    contain the target, a single malformed sibling entry must still
    force fail-closed -- the architecture here cannot positively prove
    a malformed entry is irrelevant to the target's identity."""
    result = cold_reset_waking_inference_path(
        _ollama_module(ps_result={"models": [{"model": "other-model:latest"}, "garbage"]}), "gemma4:e4b",
    )
    assert result["status"] == ColdResetOutcome.UNKNOWN


def test_correction_4_ps_raises_is_not_established():
    """Case 11: ps() itself raising must never be treated as confirmed
    absence (pre-existing behavior, unmodified by this correction --
    permanent regression coverage per the adversarial matrix)."""
    result = cold_reset_waking_inference_path(
        _ollama_module(ps_raises=ConnectionError("ollama daemon unreachable")), "gemma4:e4b",
    )
    assert result["status"] == ColdResetOutcome.UNKNOWN


def test_correction_4_unload_request_failure_blocks_reset():
    """Case 12: the unload (chat keep_alive=0) request itself raising
    must never produce reset success (pre-existing behavior, unmodified
    by this correction -- permanent regression coverage per the
    adversarial matrix)."""
    result = cold_reset_waking_inference_path(
        _ollama_module(chat_raises=RuntimeError("connection refused")), "gemma4:e4b",
    )
    assert result["status"] == ColdResetOutcome.FAILED


def test_correction_4_end_to_end_unknown_observation_blocks_generation_and_consumes_attempt():
    """Case 13: through the REAL execute_recovery() with a qualifying
    H, a durable reservation, a successful unload mock, and an
    unrecognized/unavailable ps() observation ({} and None both) --
    reset must not report SUCCEEDED, generation must never be invoked,
    and the one recovery attempt remains consumed (a second
    execute_recovery() call for the same H is denied)."""
    for ps_result in ({}, None):
        env = Env(f"correction4_e2e_unknown_{ps_result!r}")
        h_id, _ = env.make_h()
        env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
        generation_calls = []

        def reset_fn(ps_result=ps_result):
            return cold_reset_waking_inference_path(_ollama_module(ps_result=ps_result), "gemma4:e4b")

        def generation_fn(prompt):
            generation_calls.append(prompt)
            return {"native_event_id": "should-never-happen"}

        outcome = recovery.execute_recovery(
            env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn,
        )
        assert outcome["terminal_state"] == "RESET_FAILED"
        assert generation_calls == [], "generation must never be invoked when reset is not positively SUCCEEDED"

        denied = False
        try:
            recovery.execute_recovery(
                env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn,
            )
        except RecoveryDenied:
            denied = True
        assert denied, "the one recovery attempt must remain consumed after an unestablished reset"


def test_correction_4_end_to_end_positive_absence_allows_exactly_one_generation():
    """Case 14: through the REAL execute_recovery(), a recognized,
    positively-confirmed-absent ps() observation (via the real
    cold_reset_waking_inference_path(), not a synthetic reset_fn
    stand-in) allows generation to proceed, exactly once."""
    env = Env("correction4_e2e_positive_absence")
    h_id, _ = env.make_h("Recovers after a positively confirmed reset.")
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
    generation_calls = []

    def reset_fn():
        return cold_reset_waking_inference_path(
            _ollama_module(ps_result={"models": []}), "gemma4:e4b",
        )

    def generation_fn(prompt):
        generation_calls.append(prompt)
        result = stage_and_record_native_waking_turn(
            env.tmp_root, os.path.join(env.tmp_root, "native_turn_staging.jsonl"),
            session_id="sess-correction4-recovery", session_started_at=int(time.time()), user_id="nate",
            prompt=prompt, bounded_clause="", clark_prose="A genuinely persisted correction-4 recovery reply.",
            kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=env.pipeline_key,
            artifact_pass_ran=False, occurred_at=int(time.time()), human_input_event_id=h_id,
        )
        return {"native_event_id": result["event_id"]}

    outcome = recovery.execute_recovery(
        env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn,
    )
    assert outcome["terminal_state"] == "SUCCESS"
    assert len(generation_calls) == 1


# ============ WTR0-CORRECTION-1: capture wrapper (synthetic/mock boundary)


def _run_fn_raising(exc, human_input_event_id=None):
    """A synthetic `run_waking_turn_fn` matching run_waking_turn_
    capturing_failure()'s exact call contract (positional orch/prompt,
    keyword interaction_mode/human_input_authority/
    existing_human_input_event_id) that always raises `exc`.

    WTR0-CORRECTION-2: when `human_input_event_id` is given, this fake
    simulates the REAL llama_anaxi.run_waking_turn()'s own
    unconditional call to ui_turn_diagnostics.record_human_input_
    event_id() -- which always happens immediately after H is
    validated/persisted and strictly BEFORE any call that could fail
    -- so tests can exercise exact-H attribution without importing the
    numpy-dependent llama_anaxi chain. Omitting it (default None)
    simulates a failure that occurred BEFORE any H was established for
    this exact attempt (e.g. invalid/missing authority, or the
    exception happened before H persistence) -- run_waking_turn_
    capturing_failure() must then record no H-targeted evidence at
    all, regardless of what other unanswered H events happen to exist
    for the same actor."""
    def fn(orch, prompt, *, interaction_mode=None, human_input_authority=None,
           existing_human_input_event_id=None):
        if human_input_event_id is not None:
            ui_turn_diagnostics.record_human_input_event_id(human_input_event_id)
        raise exc
    return fn


def test_capture_wrapper_known_retry_safe_staging_durability_becomes_eligible():
    """StagingDurabilityError's own docstring states the pre-append
    rollback succeeded and retry-whole is genuinely safe -- the ONLY
    exception type (besides the explicit BUDGET_EXCEEDED case below)
    this module positively proves retry-safe from type alone."""
    env = Env("cap_staging_durability")
    h_id, _ = env.make_h()
    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(StagingDurabilityError("synthetic durability failure"), h_id),
            orch=None, prompt="irrelevant", provenance_db_path=env.db_path,
        )
    except StagingDurabilityError:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"
    assert evidence["retry_safe"] is True
    assert receipt["decision"] == EligibilityDecision.ELIGIBLE


def test_capture_wrapper_budget_exceeded_direction_failure_is_retry_safe():
    """ConversationDirectionFailure(failure_code=BUDGET_EXCEEDED) is the
    WTR0 contract's own named example of a deterministic scaffold/
    budget check that structurally precedes canonical X persistence --
    positively retry-safe."""
    env = Env("cap_budget_exceeded")
    h_id, _ = env.make_h()
    exc = ConversationDirectionFailure("pass1", context_budget.BUDGET_EXCEEDED)
    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(exc, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except ConversationDirectionFailure:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"
    assert evidence["retry_safe"] is True
    assert receipt["decision"] == EligibilityDecision.ELIGIBLE


def test_workspace_pass2_budget_failure_is_not_retry_safe_after_action_boundary():
    """Workspace Pass-2 runs after the selected workspace action. Even
    without canonical X, BUDGET_EXCEEDED there cannot authorize replay
    of the whole waking occurrence merely from its broad exception type."""
    env = Env("cap_workspace_pass2_budget")
    h_id, _ = env.make_h()
    exc = ConversationDirectionFailure("workspace_pass2", context_budget.BUDGET_EXCEEDED)
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(exc, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
        assert False, "the original direction failure must propagate"
    except ConversationDirectionFailure as raised:
        assert raised is exc

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert evidence["retry_safe"] is False
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE
    assert receipt["basis"] == "FAILURE_NOT_RETRY_SAFE"


def test_ordinary_completion_classes_are_retry_safe():
    """The bounded ordinary Pass-1/Pass-2 completion failures occur before
    canonical X persistence and before durable external effects. They
    preserve H and authorize at most the existing explicit WTR0 recovery;
    no automatic second generation is introduced."""
    for index, (stage, failure_code) in enumerate((
        ("pass1", "COMPLETION_LIMIT_REACHED"),
        ("pass1", "INCOMPLETE_MODEL_COMPLETION"),
        ("pass1", "COMPLETION_LIMIT_EXCEEDED"),
        ("pass2", "COMPLETION_LIMIT_REACHED"),
        ("pass2", "INCOMPLETE_MODEL_COMPLETION"),
        ("pass2", "COMPLETION_LIMIT_EXCEEDED"),
        ("pass2", "INCOMPLETE_EXPRESSION_BOUNDARY"),
    )):
        env = Env(f"cap_{stage}_incomplete_{index}")
        h_id, _ = env.make_h()
        exc = ConversationDirectionFailure(stage, failure_code)
        try:
            run_waking_turn_capturing_failure(
                _run_fn_raising(exc, h_id), orch=None, prompt="irrelevant",
                provenance_db_path=env.db_path,
            )
            assert False, "the original direction failure must propagate"
        except ConversationDirectionFailure as raised:
            assert raised is exc

        conn = env.conn()
        evidence = wfe.latest_failure_evidence(conn, h_id)
        receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
        linked_x = recovery._linked_canonical_x_event_id(conn, h_id)
        conn.close()
        assert linked_x is None
        assert evidence["failure_class"] == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"
        assert evidence["retry_safe"] is True
        assert receipt["decision"] == EligibilityDecision.ELIGIBLE


def test_capture_wrapper_non_budget_direction_failure_is_not_retry_safe():
    """A structural ConversationDirectionFailure that is NOT the
    BUDGET_EXCEEDED case (e.g. a malformed model act) is a
    nondeterministic model-output failure, not a deterministic
    scaffold check -- this module cannot positively prove it
    retry-safe, so it must not become eligible."""
    env = Env("cap_malformed_act")
    h_id, _ = env.make_h()
    exc = ConversationDirectionFailure("pass1", "MALFORMED_ACT")
    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(exc, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except ConversationDirectionFailure:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert evidence["retry_safe"] is False
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE
    assert receipt["basis"] == "FAILURE_NOT_RETRY_SAFE"


def test_capture_wrapper_unknown_exception_is_not_retry_safe():
    """Correction B proof #3: an unrecognized exception type must
    never become retry-safe -- this repository's current failure
    boundary cannot mechanically distinguish an ordinary transient
    provider hiccup from an unrelated programming bug by type alone,
    so it must fail closed rather than guess."""
    env = Env("cap_unknown_exc")
    h_id, _ = env.make_h()
    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(KeyError("some unrelated bug"), h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except KeyError:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert evidence["retry_safe"] is False
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE
    assert receipt["basis"] == "FAILURE_NOT_RETRY_SAFE"


def test_capture_wrapper_ambiguous_persistence_outcome_is_not_retry_safe():
    """StagingIndeterminateStateError's own docstring explicitly
    FORBIDS retry-whole until an operator inspects the raw staging
    file -- WTR0 must never treat this the same as a plain retry-safe
    failure."""
    env = Env("cap_ambiguous")
    h_id, _ = env.make_h()
    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(StagingIndeterminateStateError("synthetic ambiguous rollback"), h_id),
            orch=None, prompt="irrelevant", provenance_db_path=env.db_path,
        )
    except StagingIndeterminateStateError:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "AMBIGUOUS_PERSISTENCE_OUTCOME"
    assert evidence["retry_safe"] is False
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE


def test_capture_wrapper_post_persistence_exception_with_x_existing_is_not_retry_safe():
    """A fresh canonical read showing X already exists always wins
    over exception-type inference: an exception in best-effort
    post-persistence bookkeeping must never look like a retry-safe
    pre-persistence failure."""
    env = Env("cap_post_persistence")
    h_id, _ = env.make_h()
    stage_and_record_native_waking_turn(
        env.tmp_root, os.path.join(env.tmp_root, "native_turn_staging.jsonl"),
        session_id="sess-x", session_started_at=int(time.time()), user_id="nate",
        prompt="whatever", bounded_clause="", clark_prose="Already persisted reply.",
        kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=env.pipeline_key,
        artifact_pass_ran=False, occurred_at=int(time.time()), human_input_event_id=h_id,
    )

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(RuntimeError("best-effort post-persistence bookkeeping failed"), h_id),
            orch=None, prompt="irrelevant", provenance_db_path=env.db_path,
            existing_human_input_event_id=h_id,
        )
    except RuntimeError:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    conn.close()
    assert evidence["failure_class"] == "POST_PERSISTENCE_EXCEPTION_X_EXISTS"
    assert evidence["retry_safe"] is False


def test_capture_wrapper_ordinary_success_writes_no_rows():
    """Ordinary waking compatibility, re-established synthetically:
    a genuinely successful run_waking_turn_fn call through the SAME
    capture wrapper writes zero waking_failure_evidence rows and zero
    wtr0_recovery rows."""
    env = Env("cap_success")
    h_id, _ = env.make_h()

    def fn(orch, prompt, *, interaction_mode=None, human_input_authority=None,
           existing_human_input_event_id=None):
        return {"native_event_id": "whatever", "reply": "ok"}

    result = run_waking_turn_capturing_failure(
        fn, orch=None, prompt="irrelevant", provenance_db_path=env.db_path,
    )
    assert result["native_event_id"] == "whatever"

    conn = env.conn()
    ev_count = conn.execute("SELECT COUNT(*) FROM waking_failure_evidence").fetchone()[0]
    rec_count = conn.execute("SELECT COUNT(*) FROM wtr0_recovery").fetchone()[0]
    conn.close()
    assert ev_count == 0
    assert rec_count == 0


def test_capture_wrapper_caller_cannot_forge_retry_safe_via_exception_attributes():
    """Classification is derived purely from exception TYPE (plus a
    fresh DB read for has_x) -- attaching forged `retry_safe`/
    `failure_class`-looking attributes to an unrecognized exception
    instance must have zero effect."""
    env = Env("cap_forge_attempt")
    h_id, _ = env.make_h()

    class SneakyException(Exception):
        pass

    exc = SneakyException("looks urgent")
    exc.retry_safe = True  # forged; must be completely ignored
    exc.failure_class = "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"  # forged; must be completely ignored

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(exc, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except SneakyException:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    conn.close()
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert evidence["retry_safe"] is False


def test_capture_wrapper_evidence_capture_failure_does_not_conceal_original_exception():
    """If evidence capture itself raises (e.g. an unopenable provenance
    DB path), the ORIGINAL waking exception must still propagate
    unchanged -- never masked, replaced, or reclassified by a
    secondary error from evidence capture."""
    env = Env("cap_evidence_failure")
    h_id, _ = env.make_h()

    class OriginalFailure(RuntimeError):
        pass

    bad_db_path = os.path.join(env.tmp_root, "does_not_exist_dir", "anaxi_provenance.db")
    raised_type = None
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(OriginalFailure("the real waking failure"), h_id), orch=None,
            prompt="irrelevant", provenance_db_path=bad_db_path,
        )
    except Exception as exc:
        raised_type = type(exc)
    assert raised_type is OriginalFailure


def test_synthetic_full_recovery_lifecycle_via_capture_wrapper_and_execute_recovery():
    """Synthetic replacement for the (removed, numpy-gated) full real-
    llama_anaxi success-path test. Establishes the same mechanical
    claims -- a genuine waking-attempt failure captured through the
    real run_waking_turn_capturing_failure() control flow becomes
    eligible, and a subsequent execute_recovery() persists exactly one
    canonical X through the REAL native_provenance_writer lawful
    writer, closing recovery -- using a synthetic StagingDurabilityError
    and a synthetic generation_fn instead of the numpy-dependent
    llama_anaxi.run_waking_turn()."""
    env = Env("synthetic_full_lifecycle")
    h_id, _ = env.make_h("First attempt fails, then recovers.")

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(StagingDurabilityError("synthetic first-attempt staging failure"), h_id),
            orch=None, prompt="First attempt fails, then recovers.",
            provenance_db_path=env.db_path,
        )
    except StagingDurabilityError:
        raised = True
    assert raised

    conn = env.conn()
    original_text = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND sequence = 0", (h_id,),
    ).fetchone()[0]
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"
    assert evidence["retry_safe"] is True
    assert receipt["decision"] == EligibilityDecision.ELIGIBLE

    def reset_fn():
        return {"status": ColdResetOutcome.SUCCEEDED, "basis": "synthetic reset ok"}

    def generation_fn(prompt):
        assert prompt == original_text, "recovery must reuse the exact canonical H text"
        result = stage_and_record_native_waking_turn(
            env.tmp_root, os.path.join(env.tmp_root, "native_turn_staging.jsonl"),
            session_id="sess-synthetic-recovery", session_started_at=int(time.time()), user_id="nate",
            prompt=prompt, bounded_clause="", clark_prose="A genuinely persisted synthetic recovery reply.",
            kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=env.pipeline_key,
            artifact_pass_ran=False, occurred_at=int(time.time()), human_input_event_id=h_id,
        )
        return {"native_event_id": result["event_id"]}

    outcome = recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    assert outcome["terminal_state"] == "SUCCESS"
    x_event_id = outcome["canonical_x_event_id"]

    conn = env.conn()
    h_text_after = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND sequence = 0", (h_id,),
    ).fetchone()[0]
    h_count = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'human_waking_input'").fetchone()[0]
    x_rows = conn.execute(
        "SELECT COUNT(*) FROM event_components c JOIN events e ON e.event_id = c.event_id "
        "WHERE c.component_kind = 'human_input_event_id' AND c.component_text = ? AND e.event_type = 'waking_turn'",
        (h_id,),
    ).fetchone()[0]
    conn.close()

    assert h_text_after == original_text, "original H must be unchanged"
    assert h_count == 1, "no duplicate H may be created"
    assert x_rows == 1, "exactly one canonical X relationship must exist"
    assert x_event_id is not None

    conn = env.conn()
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE
    assert receipt["basis"] in ("X_ALREADY_LINKED", "RECOVERY_ALREADY_RESERVED_OR_CONSUMED")

    denied = False
    try:
        recovery.reserve_recovery(env.db_path, h_id, env.actor_id)
    except RecoveryDenied:
        denied = True
    assert denied


# ============================================= WTR0-CORRECTION-2: defect 1
# trusted retry-safe classification must not trust type(exc).__name__


def test_correction_2_defect_1_reproduction_spoofed_class_name_is_not_retry_safe():
    """Verifier reproduction A: independent verification established
    that pre-correction classification trusted `type(exc).__name__`,
    so a synthetic, wholly unrelated exception class dynamically
    constructed with the SAME NAME as the real trusted
    StagingDurabilityError was accepted as retry-safe and made an H
    ELIGIBLE. After WTR0-CORRECTION-2, classification is isinstance()
    against the actual imported class, so this spoof must be classified
    UNCLASSIFIED_WAKING_EXCEPTION / retry_safe=False, and the H must
    NOT become eligible."""
    env = Env("correction2_defect1_spoofed_name")
    h_id, _ = env.make_h()

    SpoofedStagingDurabilityError = type("StagingDurabilityError", (Exception,), {})
    assert SpoofedStagingDurabilityError is not StagingDurabilityError
    assert SpoofedStagingDurabilityError.__name__ == StagingDurabilityError.__name__

    spoofed = SpoofedStagingDurabilityError("looks exactly like the trusted class by name")
    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(spoofed, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except SpoofedStagingDurabilityError:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence is not None, "the spoof must still be recorded, never silently discarded"
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert evidence["retry_safe"] is False
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE
    assert receipt["basis"] == "FAILURE_NOT_RETRY_SAFE"


def test_correction_2_spoofed_message_text_is_not_retry_safe():
    """An unrelated exception carrying the real trusted class's own
    message text is not retry-safe either -- classification must never
    consult message text."""
    env = Env("correction2_spoofed_message")
    h_id, _ = env.make_h()

    class UnrelatedBug(Exception):
        pass

    spoofed = UnrelatedBug("its own contract states the pre-append rollback succeeded")
    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(spoofed, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except UnrelatedBug:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    conn.close()
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert evidence["retry_safe"] is False


def test_correction_2_caller_supplied_retry_safe_flag_on_trusted_looking_subclass_is_ignored():
    """A caller/attacker cannot subclass an ordinary Exception, name it
    after the trusted type, AND attach a forged retry_safe attribute --
    none of it has any effect; only genuine isinstance() of the real
    imported class can produce retry_safe=True."""
    env = Env("correction2_forged_subclass")
    h_id, _ = env.make_h()

    FakeClass = type("StagingDurabilityError", (Exception,), {})
    exc = FakeClass("forged")
    exc.retry_safe = True
    exc.failure_class = "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(exc, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except FakeClass:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    conn.close()
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert evidence["retry_safe"] is False


def test_correction_2_genuine_trusted_exception_is_still_retry_safe():
    """Permanent negative-proof companion #1: the REAL, genuinely
    imported StagingDurabilityError must still classify retry-safe --
    the fix must not have overcorrected into rejecting the legitimate
    case."""
    env = Env("correction2_genuine_trusted")
    h_id, _ = env.make_h()
    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(StagingDurabilityError("genuine"), h_id), orch=None,
            prompt="irrelevant", provenance_db_path=env.db_path,
        )
    except StagingDurabilityError:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"
    assert evidence["retry_safe"] is True
    assert receipt["decision"] == EligibilityDecision.ELIGIBLE


def test_correction_2_genuine_non_retry_safe_exception_stays_non_retry_safe():
    """Permanent negative-proof companion #2: a genuine, explicitly
    non-retry-safe trusted exception (StagingIndeterminateStateError)
    must remain non-retry-safe after the fix."""
    env = Env("correction2_genuine_non_retry_safe")
    h_id, _ = env.make_h()
    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(StagingIndeterminateStateError("genuine ambiguous"), h_id),
            orch=None, prompt="irrelevant", provenance_db_path=env.db_path,
        )
    except StagingIndeterminateStateError:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    conn.close()
    assert evidence["failure_class"] == "AMBIGUOUS_PERSISTENCE_OUTCOME"
    assert evidence["retry_safe"] is False


# ============================================= WTR0-CORRECTION-2: defect 2
# failure evidence must attach only to the exact H of the failing attempt


def test_correction_2_defect_2_reproduction_no_current_h_leaves_historical_h_untouched():
    """Verifier reproduction B: independent verification established
    that pre-correction attribution picked "the actor's latest
    unanswered H," so a NEW mocked waking call that created no H of its
    own, but raised the REAL trusted retry-safe exception, attached
    evidence to an older, wholly unrelated historical unanswered H --
    making it incorrectly ELIGIBLE. After WTR0-CORRECTION-2, a failing
    attempt that never positively established an H for itself (the
    fake run_waking_turn_fn records no human_input_event_id before
    raising, simulating a failure before H persistence/validation)
    must leave EVERY historical unanswered H completely untouched."""
    env = Env("correction2_defect2_no_current_h")
    historical_h_id, _ = env.make_h("An older, still-unanswered turn from the same actor.")

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(StagingDurabilityError("new attempt failed before creating any H")),
            orch=None, prompt="a brand new attempt", provenance_db_path=env.db_path,
        )
    except StagingDurabilityError:
        raised = True
    assert raised

    conn = env.conn()
    historical_evidence = wfe.latest_failure_evidence(conn, historical_h_id)
    historical_receipt = recovery.assess_eligibility(conn, historical_h_id, env.actor_id)
    total_evidence_rows = conn.execute("SELECT COUNT(*) FROM waking_failure_evidence").fetchone()[0]
    conn.close()
    assert historical_evidence is None, "the historical H must receive zero evidence"
    assert historical_receipt["decision"] == EligibilityDecision.NOT_ESTABLISHED
    assert total_evidence_rows == 0, "no evidence row of any kind may be written anywhere"


def test_correction_2_h1_historical_h2_current_evidence_attaches_only_to_h2():
    """H1 is an older unanswered H (no evidence). The CURRENT attempt
    positively establishes a DIFFERENT H2 (simulating run_waking_turn()
    recording H2 via ui_turn_diagnostics before failing) and fails
    retry-safely. Evidence must attach ONLY to H2; H1 must remain
    completely unchanged and NOT_ESTABLISHED."""
    env = Env("correction2_h1_h2")
    h1_id, _ = env.make_h("H1: an older unanswered turn.")
    h2_id, _ = env.make_h("H2: the current attempt's own turn.")

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(StagingDurabilityError("current attempt's own H2 fails"), h2_id),
            orch=None, prompt="H2: the current attempt's own turn.",
            provenance_db_path=env.db_path,
        )
    except StagingDurabilityError:
        raised = True
    assert raised

    conn = env.conn()
    h1_evidence = wfe.latest_failure_evidence(conn, h1_id)
    h1_receipt = recovery.assess_eligibility(conn, h1_id, env.actor_id)
    h2_evidence = wfe.latest_failure_evidence(conn, h2_id)
    h2_receipt = recovery.assess_eligibility(conn, h2_id, env.actor_id)
    total_evidence_rows = conn.execute("SELECT COUNT(*) FROM waking_failure_evidence").fetchone()[0]
    conn.close()

    assert h1_evidence is None, "H1 must be completely unaffected by H2's failure"
    assert h1_receipt["decision"] == EligibilityDecision.NOT_ESTABLISHED
    assert h2_evidence is not None
    assert h2_evidence["failure_class"] == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"
    assert h2_evidence["retry_safe"] is True
    assert h2_receipt["decision"] == EligibilityDecision.ELIGIBLE
    assert total_evidence_rows == 1, "exactly one evidence row, on H2 only"


def test_correction_2_multiple_unanswered_h_no_latest_heuristic_decides_target():
    """The actor has THREE unanswered H events. The current attempt's
    exact H is explicitly the MIDDLE one (h2), never the most recently
    created (h3) -- proving there is no "latest unanswered" heuristic
    silently deciding the target. Evidence must attach to h2 only; h1
    and h3, one older and one newer than h2, must both remain
    completely untouched."""
    env = Env("correction2_multiple_unanswered")
    h1_id, _ = env.make_h("H1: oldest unanswered turn.")
    h2_id, _ = env.make_h("H2: the current attempt's own turn (not the latest).")
    h3_id, _ = env.make_h("H3: a newer unanswered turn than H2.")

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(StagingDurabilityError("H2 is this attempt's exact H"), h2_id),
            orch=None, prompt="H2: the current attempt's own turn (not the latest).",
            provenance_db_path=env.db_path,
        )
    except StagingDurabilityError:
        raised = True
    assert raised

    conn = env.conn()
    h1_evidence = wfe.latest_failure_evidence(conn, h1_id)
    h2_evidence = wfe.latest_failure_evidence(conn, h2_id)
    h3_evidence = wfe.latest_failure_evidence(conn, h3_id)
    h1_receipt = recovery.assess_eligibility(conn, h1_id, env.actor_id)
    h3_receipt = recovery.assess_eligibility(conn, h3_id, env.actor_id)
    conn.close()

    assert h1_evidence is None
    assert h1_receipt["decision"] == EligibilityDecision.NOT_ESTABLISHED
    assert h3_evidence is None, "the newer H3 must not be mistaken for the current attempt's H"
    assert h3_receipt["decision"] == EligibilityDecision.NOT_ESTABLISHED
    assert h2_evidence is not None
    assert h2_evidence["retry_safe"] is True


def test_correction_2_thread_local_marker_does_not_leak_across_sequential_calls():
    """Directly proves the per-call reset: a first call positively
    establishes h1 and fails; a SECOND, unrelated call on the same
    thread creates no H of its own and fails -- it must not pick up
    h1's stale thread-local marker left over from the first call."""
    env = Env("correction2_no_leak_across_calls")
    h1_id, _ = env.make_h("First call's own H.")

    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(StagingDurabilityError("first call"), h1_id),
            orch=None, prompt="first", provenance_db_path=env.db_path,
        )
    except StagingDurabilityError:
        pass

    conn = env.conn()
    h1_evidence_after_first = wfe.latest_failure_evidence(conn, h1_id)
    conn.close()
    assert h1_evidence_after_first is not None

    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(RuntimeError("second, unrelated call creates no H")),
            orch=None, prompt="second", provenance_db_path=env.db_path,
        )
    except RuntimeError:
        pass

    conn = env.conn()
    h1_evidence_after_second = wfe.latest_failure_evidence(conn, h1_id)
    total_evidence_rows = conn.execute("SELECT COUNT(*) FROM waking_failure_evidence").fetchone()[0]
    conn.close()
    assert h1_evidence_after_second is not None
    assert h1_evidence_after_second["failure_evidence_id"] == h1_evidence_after_first["failure_evidence_id"], (
        "h1's evidence must be byte-identical to the first call's -- the second call must not "
        "have written a second row against h1"
    )
    assert total_evidence_rows == 1, "the second call must have written no evidence anywhere"


# ============================================= WTR0-CORRECTION-3
# retry-safe classification must derive from type(exc), not isinstance()
# (isinstance() consults an instance's __class__, which a caller can
# override with a property -- type(exc) reads the actual runtime type
# slot directly and cannot be spoofed this way)


def _forged_class_presenting_as(trusted_class):
    """Builds a wholly unrelated exception class whose *instances*
    report `__class__` as `trusted_class` via a property override --
    the verifier's exact adversarial construction against
    WTR0-CORRECTION-2. `type(exc)` on an instance of the returned class
    is still the unrelated class itself; only `exc.__class__` (and
    therefore `isinstance(exc, trusted_class)`) is spoofed."""
    def _class_property(self):
        return trusted_class

    return type("UnrelatedFailure", (Exception,), {"__class__": property(_class_property)})


def test_correction_3_defect_reproduction_forged_class_property_staging_durability_is_not_retry_safe():
    """Verifier reproduction: WTR0-CORRECTION-2 already rejected a
    spoofed class NAME (`type("StagingDurabilityError", (Exception,), {})`),
    but its classification still used `isinstance()`, which consults
    `obj.__class__` in addition to `type(obj)`. An unrelated exception
    instance that overrides `__class__` as a property returning the
    REAL, genuinely imported `StagingDurabilityError` is therefore
    accepted by `isinstance(exc, StagingDurabilityError)` even though
    `type(exc)` is the wholly unrelated forged class. Before this
    correction this was misclassified retry-safe and made an H
    ELIGIBLE with a false staging-durability/rollback-succeeded claim;
    after this correction it must be UNCLASSIFIED_WAKING_EXCEPTION /
    retry_safe=False and the H must remain INELIGIBLE."""
    env = Env("correction3_forged_class_property_staging_durability")
    h_id, _ = env.make_h()

    ForgedFailure = _forged_class_presenting_as(StagingDurabilityError)
    assert issubclass(ForgedFailure, Exception)
    assert ForgedFailure is not StagingDurabilityError
    forged = ForgedFailure("presents as StagingDurabilityError via __class__")
    # This is the exact defect: isinstance() is fooled, type() is not.
    assert isinstance(forged, StagingDurabilityError)
    assert type(forged) is not StagingDurabilityError

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(forged, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except Exception as exc:
        assert exc is forged
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence is not None, "the spoof must still be recorded, never silently discarded"
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert evidence["retry_safe"] is False
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE
    assert receipt["basis"] == "FAILURE_NOT_RETRY_SAFE"


def test_correction_3_forged_class_property_indeterminate_state_produces_no_false_trusted_receipt():
    """The same forged-`__class__` attack against
    StagingIndeterminateStateError must not produce a false trusted-type
    receipt either -- even though that category is already non-retry-
    safe, the receipt's failure_class must truthfully reflect that this
    exception's actual type was never established as the trusted
    indeterminate-state type."""
    env = Env("correction3_forged_class_property_indeterminate")
    h_id, _ = env.make_h()

    ForgedFailure = _forged_class_presenting_as(StagingIndeterminateStateError)
    forged = ForgedFailure("presents as StagingIndeterminateStateError via __class__")
    assert isinstance(forged, StagingIndeterminateStateError)
    assert type(forged) is not StagingIndeterminateStateError

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(forged, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except Exception as exc:
        assert exc is forged
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence is not None
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION", (
        "must not be misclassified as the genuine trusted AMBIGUOUS_PERSISTENCE_OUTCOME "
        "class on the strength of a forged __class__ presentation"
    )
    assert evidence["retry_safe"] is False
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE


def test_correction_3_forged_class_property_budget_exceeded_direction_failure_is_not_retry_safe():
    """The same forged-`__class__` attack against
    ConversationDirectionFailure, additionally carrying a genuine
    `failure_code=BUDGET_EXCEEDED` attribute, must not receive trusted
    budget-exceeded classification -- actual trusted type identity must
    be established BEFORE `failure_code` is ever consulted."""
    env = Env("correction3_forged_class_property_budget")
    h_id, _ = env.make_h()

    ForgedFailure = _forged_class_presenting_as(ConversationDirectionFailure)
    forged = ForgedFailure("presents as ConversationDirectionFailure via __class__")
    forged.failure_code = context_budget.BUDGET_EXCEEDED
    assert isinstance(forged, ConversationDirectionFailure)
    assert type(forged) is not ConversationDirectionFailure

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(forged, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except Exception as exc:
        assert exc is forged
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence is not None
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION", (
        "a matching failure_code attribute must never bootstrap trust in a forged type"
    )
    assert evidence["retry_safe"] is False
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE


def test_correction_3_exact_class_name_match_is_not_retry_safe():
    """Permanent companion to the CORRECTION-2 regression: a
    dynamically-constructed class literally NAMED `StagingDurabilityError`
    (no `__class__` trickery at all, just a matching `__name__`) remains
    non-retry-safe under the actual-type rule -- proving the fix does
    not depend on `__class__` forgery being the only spoofing vector
    considered."""
    env = Env("correction3_same_class_name")
    h_id, _ = env.make_h()

    SameName = type("StagingDurabilityError", (Exception,), {})
    assert SameName is not StagingDurabilityError
    exc = SameName("same name, unrelated type")

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(exc, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except Exception as caught:
        assert caught is exc
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    conn.close()
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert evidence["retry_safe"] is False


def test_correction_3_arbitrary_trusted_looking_attributes_cannot_establish_retry_safety():
    """An unrelated exception carrying arbitrary trusted-looking fields
    (a forged `retry_safe`, `failure_class`, and `failure_code` all set
    to genuine trusted values) still cannot become retry-safe without
    actual trusted type identity -- attributes are never consulted
    before `type(exc)` is checked."""
    env = Env("correction3_arbitrary_attributes")
    h_id, _ = env.make_h()

    class LooksTrusted(Exception):
        pass

    exc = LooksTrusted("carries forged trusted-looking fields")
    exc.retry_safe = True
    exc.failure_class = "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"
    exc.failure_code = context_budget.BUDGET_EXCEEDED

    raised = False
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(exc, h_id), orch=None, prompt="irrelevant",
            provenance_db_path=env.db_path,
        )
    except LooksTrusted:
        raised = True
    assert raised

    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_id)
    receipt = recovery.assess_eligibility(conn, h_id, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert evidence["retry_safe"] is False
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE


def test_correction_3_genuine_trusted_exceptions_still_classify_correctly_by_actual_type():
    """Permanent negative-proof companion: the fix must not have
    overcorrected. Each of the three REAL, genuinely imported trusted
    exception types (StagingDurabilityError, StagingIndeterminateStateError,
    and ConversationDirectionFailure with failure_code=BUDGET_EXCEEDED)
    must still classify exactly as before this correction -- the two
    retry-safe cases must still make their H ELIGIBLE, and the
    non-retry-safe case must still make its H INELIGIBLE."""
    env = Env("correction3_genuine_types_still_work")

    h_durability, _ = env.make_h("durability case")
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(StagingDurabilityError("genuine"), h_durability), orch=None,
            prompt="irrelevant", provenance_db_path=env.db_path,
        )
    except StagingDurabilityError:
        pass
    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_durability)
    receipt = recovery.assess_eligibility(conn, h_durability, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"
    assert evidence["retry_safe"] is True
    assert receipt["decision"] == EligibilityDecision.ELIGIBLE

    h_indeterminate, _ = env.make_h("indeterminate case")
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(StagingIndeterminateStateError("genuine"), h_indeterminate),
            orch=None, prompt="irrelevant", provenance_db_path=env.db_path,
        )
    except StagingIndeterminateStateError:
        pass
    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_indeterminate)
    receipt = recovery.assess_eligibility(conn, h_indeterminate, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "AMBIGUOUS_PERSISTENCE_OUTCOME"
    assert evidence["retry_safe"] is False
    assert receipt["decision"] == EligibilityDecision.INELIGIBLE

    h_budget, _ = env.make_h("budget case")
    budget_exc = ConversationDirectionFailure("budget exceeded", context_budget.BUDGET_EXCEEDED)
    try:
        run_waking_turn_capturing_failure(
            _run_fn_raising(budget_exc, h_budget), orch=None,
            prompt="irrelevant", provenance_db_path=env.db_path,
        )
    except ConversationDirectionFailure:
        pass
    conn = env.conn()
    evidence = wfe.latest_failure_evidence(conn, h_budget)
    receipt = recovery.assess_eligibility(conn, h_budget, env.actor_id)
    conn.close()
    assert evidence["failure_class"] == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"
    assert evidence["retry_safe"] is True
    assert receipt["decision"] == EligibilityDecision.ELIGIBLE


# =========== WTR0-CORRECTION-1: legacy resume / WTR0 entrypoint unification


class _FakeWakingModule:
    """A synthetic stand-in for llama_anaxi's public surface that
    resume_human_input.resume_via_wtr0_gate() (and, in production,
    wtr0_cli.py's cmd_recover) depends on -- exposing exactly the
    attributes those callers read, so the real resume_via_wtr0_gate()
    function can be exercised without importing the numpy-dependent
    llama_anaxi module chain at all. `run_waking_turn` persists a REAL
    canonical X via the REAL native_provenance_writer.
    stage_and_record_native_waking_turn() -- the actual lawful
    persistence pathway -- when `should_succeed` is True, and raises
    RuntimeError (simulating a genuine waking execution failure) when
    False."""

    def __init__(self, env, should_succeed):
        self.env = env
        self.should_succeed = should_succeed
        self.MODEL = "gemma4:e4b"
        self.DB_PATH = env.db_path
        self.PIPELINE_KEY = env.pipeline_key
        self.CONVERSATION_MODE = "conversation"

    def get_current_session_id(self):
        return f"sess-{uuid.uuid4()}"

    def get_current_session_started_at(self):
        return int(time.time())

    def run_waking_turn(self, orch, prompt, *, interaction_mode, human_input_authority,
                         existing_human_input_event_id=None):
        if not self.should_succeed:
            raise RuntimeError("synthetic legacy-resume waking execution failure")
        result = stage_and_record_native_waking_turn(
            self.env.tmp_root, os.path.join(self.env.tmp_root, "native_turn_staging.jsonl"),
            session_id="sess-legacy-resume", session_started_at=int(time.time()), user_id="nate",
            prompt=prompt, bounded_clause="", clark_prose="A genuinely persisted legacy-resume reply.",
            kardia={}, controls={}, waking_model_tag=self.MODEL, pipeline_key=self.PIPELINE_KEY,
            artifact_pass_ran=False, occurred_at=int(time.time()), human_input_event_id=existing_human_input_event_id,
        )
        return {"native_event_id": result["event_id"], "reply": "A genuinely persisted legacy-resume reply."}


class _FakeOrchestrator:
    def __init__(self, db_path):
        self.db_path = db_path

    def close(self):
        pass


class _FakeOllama:
    def chat(self, model, messages, keep_alive=None, **kw):
        return {"message": {"content": ""}}

    def ps(self):
        return {"models": []}


def _attempt_legacy_resume(env, h_id, should_succeed=True, waking_cls=_FakeWakingModule):
    fake_waking = waking_cls(env, should_succeed)
    return resume_human_input.resume_via_wtr0_gate(
        env.db_path, h_id, env.actor_id, waking=fake_waking, binding=human_session_binding,
        orchestrator_cls=_FakeOrchestrator, ollama_module=_FakeOllama(),
        recovery_module=recovery, cold_reset_fn=cold_reset_waking_inference_path,
    )


def test_legacy_resume_fails_closed_when_h_does_not_exist():
    env = Env("legacy_no_h")
    denied = False
    try:
        _attempt_legacy_resume(env, "nonexistent-h-id")
    except RecoveryDenied as e:
        denied = True
        assert e.receipt["basis"] == "H_NOT_FOUND"
    assert denied


def test_legacy_resume_fails_closed_when_x_already_exists():
    env = Env("legacy_h_has_x")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
    stage_and_record_native_waking_turn(
        env.tmp_root, os.path.join(env.tmp_root, "native_turn_staging.jsonl"),
        session_id="sess-ordinary", session_started_at=int(time.time()), user_id="nate",
        prompt="whatever", bounded_clause="", clark_prose="An ordinary independent reply.",
        kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=env.pipeline_key,
        artifact_pass_ran=False, occurred_at=int(time.time()), human_input_event_id=h_id,
    )
    denied = False
    try:
        _attempt_legacy_resume(env, h_id)
    except RecoveryDenied as e:
        denied = True
        assert e.receipt["basis"] == "X_ALREADY_LINKED"
    assert denied


def test_legacy_resume_denied_without_qualifying_failure_evidence():
    """Correction A: an unanswered H with NO recorded failure evidence
    must not be usable as a back door around WTR0 eligibility -- no
    canonical X alone was never sufficient authorization, and it still
    isn't."""
    env = Env("legacy_no_evidence")
    h_id, _ = env.make_h()
    denied = False
    try:
        _attempt_legacy_resume(env, h_id)
    except RecoveryDenied as e:
        denied = True
        assert e.receipt["decision"] == EligibilityDecision.NOT_ESTABLISHED
        assert e.receipt["basis"] == "NO_FAILURE_EVIDENCE"
    assert denied
    conn = env.conn()
    x = recovery._linked_canonical_x_event_id(conn, h_id)
    conn.close()
    assert x is None, "legacy resume must never generate without qualifying WTR0 evidence"


def test_legacy_resume_denied_when_failure_evidence_not_retry_safe():
    env = Env("legacy_not_retry_safe")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "AMBIGUOUS_PERSISTENCE_OUTCOME")
    denied = False
    try:
        _attempt_legacy_resume(env, h_id)
    except RecoveryDenied as e:
        denied = True
        assert e.receipt["basis"] == "FAILURE_NOT_RETRY_SAFE"
    assert denied


def test_wtr0_reservation_closes_legacy_resume():
    """The established defect, closed: once WTR0 (e.g. via wtr0_cli.py
    `recover`) has reserved recovery for H, legacy resume must not be
    able to reach generation for that same H."""
    env = Env("wtr0_then_legacy")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")
    recovery.reserve_recovery(env.db_path, h_id, env.actor_id)  # simulates wtr0_cli.py recover

    denied = False
    try:
        _attempt_legacy_resume(env, h_id)
    except RecoveryDenied as e:
        denied = True
        assert e.receipt["basis"] == "RECOVERY_ALREADY_RESERVED_OR_CONSUMED"
    assert denied


def test_legacy_resume_success_closes_wtr0_and_creates_exactly_one_x():
    """The mirror image: a successful legacy resume must close WTR0's
    own sibling entrypoint too -- one authoritative reservation table,
    not two independent retry counters."""
    env = Env("legacy_success")
    h_id, _ = env.make_h("Legacy resume canonical text.")
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    outcome = _attempt_legacy_resume(env, h_id)
    assert outcome["terminal_state"] == "SUCCESS"

    conn = env.conn()
    h_count = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'human_waking_input'").fetchone()[0]
    x_rows = conn.execute(
        "SELECT COUNT(*) FROM event_components c JOIN events e ON e.event_id = c.event_id "
        "WHERE c.component_kind = 'human_input_event_id' AND c.component_text = ? AND e.event_type = 'waking_turn'",
        (h_id,),
    ).fetchone()[0]
    conn.close()
    assert h_count == 1
    assert x_rows == 1

    denied = False
    try:
        recovery.reserve_recovery(env.db_path, h_id, env.actor_id)  # simulates wtr0_cli.py recover
    except RecoveryDenied as e:
        denied = True
        assert e.receipt["basis"] in ("X_ALREADY_LINKED", "RECOVERY_ALREADY_RESERVED_OR_CONSUMED")
    assert denied


def test_legacy_resume_uses_canonical_stored_text_not_caller_supplied():
    env = Env("legacy_canonical_text")
    canonical_text = "The one true canonical prompt for legacy resume."
    h_id, _ = env.make_h(canonical_text)
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    seen = {}

    class _CapturingWaking(_FakeWakingModule):
        def run_waking_turn(self, orch, prompt, *, interaction_mode, human_input_authority,
                             existing_human_input_event_id=None):
            seen["prompt"] = prompt
            return {"native_event_id": None}

    _attempt_legacy_resume(env, h_id, waking_cls=_CapturingWaking)
    assert seen["prompt"] == canonical_text


def test_race_between_wtr0_and_legacy_resume_permits_only_one_consequential_generation():
    """Correction A proof #5: concurrent/repeated invocation across
    WTR0 (wtr0_cli.py's own reserve_recovery/execute_recovery path)
    and legacy resume must still permit at most one consequential
    recovery execution -- both entrypoints funnel through the SAME
    authoritative reserve_recovery() BEGIN IMMEDIATE transaction plus
    UNIQUE constraint."""
    import threading
    env = Env("race_wtr0_legacy")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    results = []
    lock = threading.Lock()

    def wtr0_attempt():
        try:
            rid = recovery.reserve_recovery(env.db_path, h_id, env.actor_id)
            with lock:
                results.append(("wtr0_won", rid))
        except RecoveryDenied as e:
            with lock:
                results.append(("wtr0_denied", e.receipt.get("basis")))

    def legacy_attempt():
        try:
            outcome = _attempt_legacy_resume(env, h_id)
            with lock:
                results.append(("legacy_won", outcome["terminal_state"]))
        except RecoveryDenied as e:
            with lock:
                results.append(("legacy_denied", e.receipt.get("basis")))

    threads = (
        [threading.Thread(target=wtr0_attempt) for _ in range(4)]
        + [threading.Thread(target=legacy_attempt) for _ in range(4)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wins = [r for r in results if r[0].endswith("_won")]
    assert len(wins) == 1, f"expected exactly one consequential winner across entrypoints, got {results}"
    conn = env.conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM wtr0_recovery WHERE human_input_event_id = ?", (h_id,),
    ).fetchone()[0]
    conn.close()
    assert count == 1


# ============================================= WTR0-CORRECTION-5

def test_correction_5_identity_matrix():
    """Both recognized shapes; every invalid populated identity blocks absence."""
    invalid = ['', ' ', '\t\n', ' other:tag', 'other:tag ', 'oth er:tag',
               'other:\ntag', '\x00', 'other\x00tag', 0, False, [], {}, ['other:tag']]
    for value in invalid:
        for fields in ({'model': value}, {'name': value},
                       {'model': value, 'name': 'other:tag'},
                       {'model': 'other:tag', 'name': value}):
            for response in ({'models': [fields]},
                             _RealShapedProcessResponse(models=[_RealShapedModel(**fields)])):
                result = cold_reset_waking_inference_path(_ollama_module(ps_result=response), 'gemma4:e4b')
                assert result['status'] == ColdResetOutcome.UNKNOWN, (fields, result)
                assert 'confirmed absent' not in result['basis']
    for fields in ({}, {'model': None}, {'model': None, 'name': 'other:tag'},
                   {'name': None, 'model': 'other:tag'}, {'digest': 'other:tag'},
                   {'model': 'other:tag', 'name': 'different:tag'},
                   {'model': 'gemma4:e4b', 'name': 'other:tag'}):
        result = cold_reset_waking_inference_path(_ollama_module(ps_result={'models': [fields]}), 'gemma4:e4b')
        assert result['status'] == ColdResetOutcome.UNKNOWN, fields
    for fields in ({}, {'model': None}, {'model': '', 'name': ''},
                   {'model': 'other:tag', 'name': 'different:tag'}):
        response = _RealShapedProcessResponse(models=[_RealShapedModel(**fields)])
        assert cold_reset_waking_inference_path(_ollama_module(ps_result=response), 'gemma4:e4b')['status'] == ColdResetOutcome.UNKNOWN


def test_correction_5_valid_identity_fields_and_empty_collections():
    for tag, expected in [('gemma4:e4b', ColdResetOutcome.FAILED), ('other:tag', ColdResetOutcome.SUCCEEDED)]:
        for fields in ({'model': tag}, {'name': tag}, {'model': tag, 'name': tag}):
            for response in ({'models': [fields]}, _RealShapedProcessResponse(models=[_RealShapedModel(**fields)])):
                assert cold_reset_waking_inference_path(_ollama_module(ps_result=response), 'gemma4:e4b')['status'] == expected
    for response in ({'models': []}, _RealShapedProcessResponse(models=[]), _RealShapedProcessResponse(models=())):
        assert cold_reset_waking_inference_path(_ollama_module(ps_result=response), 'gemma4:e4b')['status'] == ColdResetOutcome.SUCCEEDED


def test_correction_5_presence_absence_asymmetry():
    for malformed in ({'model': ''}, {'model': ' '}, {'model': None}, {}, [], None,
                      {'model': 'one', 'name': 'two'}):
        for tag, expected in [('gemma4:e4b', ColdResetOutcome.FAILED), ('other:tag', ColdResetOutcome.UNKNOWN)]:
            for entries in ([{'model': tag}, malformed], [malformed, {'model': tag}]):
                assert cold_reset_waking_inference_path(_ollama_module(ps_result={'models': entries}), 'gemma4:e4b')['status'] == expected


def test_correction_5_outer_matrix_and_forged_identity_types():
    class ForgedString:
        @property
        def __class__(self):
            return str
    class StringSubclass(str):
        pass
    class DictSubclass(dict):
        pass
    class BadRepr:
        def __repr__(self):
            raise RuntimeError('must not format untrusted observation')
    responses = [None, {}, [], (), 'models', 0, False, BadRepr(), DictSubclass(models=[])]
    responses += [{'models': value} for value in (None, 0, False, {}, (), 'empty', iter([]))]
    responses += [{'models': [value]} for value in ([], {}, None, BadRepr(), DictSubclass(model='other'))]
    responses += [{'models': [{'model': value}]} for value in (ForgedString(), StringSubclass('other'))]
    for response in responses:
        assert cold_reset_waking_inference_path(_ollama_module(ps_result=response), 'gemma4:e4b')['status'] == ColdResetOutcome.UNKNOWN
    for target in ('', ' ', '\t\n', None, 0, ' gemma4:e4b'):
        calls=[]
        module=_ollama_module(ps_result={'models': []})
        module.chat=lambda **kwargs: calls.append(kwargs)
        assert cold_reset_waking_inference_path(module, target)['status'] == ColdResetOutcome.UNKNOWN
        assert calls == []


def test_correction_5_parsing_exceptions_fail_closed():
    class Response:
        @property
        def models(self):
            raise RuntimeError('unreadable collection')
    module=_ollama_module(ps_result=Response())
    module.ProcessResponse=Response
    assert cold_reset_waking_inference_path(module, 'gemma4:e4b')['status'] == ColdResetOutcome.UNKNOWN
    class Entry:
        @property
        def model(self):
            raise RuntimeError('unreadable identity')
    Response.Model=Entry
    module.ps=lambda: {'models': [Entry()]}
    assert cold_reset_waking_inference_path(module, 'gemma4:e4b')['status'] == ColdResetOutcome.UNKNOWN
    module.ps=lambda: {'models': [Entry(), {'model': 'gemma4:e4b'}]}
    assert cold_reset_waking_inference_path(module, 'gemma4:e4b')['status'] == ColdResetOutcome.FAILED


def test_correction_5_end_to_end_invalid_identity_consumes_attempt():
    for i, entries in enumerate(([{'model': ''}], [{'model': ' '}], [{'model': '\t\n'}],
        [{'model': None}], [{'model': False}], [{'model': []}], [{}],
        [{'model': 'other:tag'}, {'model': ''}],
        [{'model': 'other:tag', 'name': 'different:tag'}], [{'model': 'gemma4:e4b'}])):
        env=Env(f'c5_identity_e2e_{i}'); h,_=env.make_h('exact same H text')
        env.record_evidence(h, 'WAKING_EXECUTION_FAILURE_NO_X_PERSISTED')
        calls=[]
        def reset():
            with env.conn() as conn:
                assert recovery.recovery_status(conn,h)['terminal_state']=='RESERVED'
            return cold_reset_waking_inference_path(_ollama_module(ps_result={'models': entries}), 'gemma4:e4b')
        def generate(prompt):
            calls.append(prompt)
            return {}
        result=recovery.execute_recovery(env.db_path,h,env.actor_id,reset_fn=reset,generation_fn=generate)
        assert result['terminal_state']=='RESET_FAILED'
        with env.conn() as conn:
            row=recovery.recovery_status(conn,h)
            assert row['reset_status'] in ('UNKNOWN','FAILED')
            assert row['generation_status']=='not_attempted'
            assert 'confirmed absent' not in row['reset_detail']
            assert conn.execute('SELECT COUNT(*) FROM wtr0_recovery').fetchone()[0]==1
            assert recovery._load_canonical_human_input(conn,h,env.actor_id)=='exact same H text'
        # Even fresh qualifying evidence cannot reopen a consumed attempt.
        env.record_evidence(h, 'WAKING_EXECUTION_FAILURE_NO_X_PERSISTED')
        try:
            recovery.execute_recovery(env.db_path,h,env.actor_id,reset_fn=reset,generation_fn=generate)
        except RecoveryDenied:
            pass
        else:
            raise AssertionError('second attempt was permitted')
        assert calls==[]


def _c5_write_ordered_evidence(env, h, classes):
    from unittest.mock import patch
    import native_provenance_writer as writer
    # Actual ULID encoder, valid adversarial random tails; all timestamps equal.
    with patch.object(writer.time, 'time', return_value=1800000000.125), patch.object(
        writer.secrets, 'token_bytes', side_effect=[bytes([255-i*50])*10 for i in range(len(classes))]
    ):
        return [env.record_evidence(h, cls, f'durable insertion {i}') for i,cls in enumerate(classes)]


def test_correction_5_same_second_evidence_order_and_consequences():
    safe='WAKING_EXECUTION_FAILURE_NO_X_PERSISTED'; unsafe='AMBIGUOUS_PERSISTENCE_OUTCOME'
    for i, classes in enumerate(([safe,unsafe], [unsafe,safe], [safe,unsafe,unsafe], [unsafe,safe,safe])):
        env=Env(f'c5_chronology_{i}'); h,_=env.make_h()
        ids=_c5_write_ordered_evidence(env,h,classes)
        assert ids==sorted(ids,reverse=True)
        with env.conn() as conn:
            rows=conn.execute('SELECT rowid,failure_evidence_id,recorded_at FROM waking_failure_evidence ORDER BY rowid').fetchall()
            assert [r[1] for r in rows]==ids and len({r[2] for r in rows})==1
            latest=wfe.latest_failure_evidence(conn,h)
            assert latest['failure_evidence_id']==ids[-1]
            assert latest['failure_class']==classes[-1]
            receipt=recovery.assess_eligibility(conn,h,env.actor_id)
        calls=[]
        def reset():
            calls.append('reset')
            return cold_reset_waking_inference_path(_ollama_module(ps_result={'models': []}), 'gemma4:e4b')
        def generate(prompt):
            calls.append('generation')
            return {}
        if classes[-1]==unsafe:
            assert receipt['decision']==EligibilityDecision.INELIGIBLE
            try:
                recovery.execute_recovery(env.db_path,h,env.actor_id,reset_fn=reset,generation_fn=generate)
            except RecoveryDenied:
                pass
            else:
                raise AssertionError('unsafe newest evidence authorized recovery')
            assert calls==[]
            with env.conn() as conn:
                assert recovery.recovery_status(conn,h) is None
        else:
            assert receipt['decision']==EligibilityDecision.ELIGIBLE
            recovery.execute_recovery(env.db_path,h,env.actor_id,reset_fn=reset,generation_fn=generate)
            assert calls==['reset','generation']


def test_correction_5_evidence_order_survives_process_reopen():
    import subprocess
    env=Env('c5_reopen'); h,_=env.make_h()
    ids=_c5_write_ordered_evidence(env,h,['WAKING_EXECUTION_FAILURE_NO_X_PERSISTED','AMBIGUOUS_PERSISTENCE_OUTCOME'])
    code='import sys,sqlite3,json; sys.path.insert(0,sys.argv[1]); import waking_failure_evidence as w; c=sqlite3.connect(sys.argv[2]); print(json.dumps(w.latest_failure_evidence(c,sys.argv[3]))); c.close()'
    result=subprocess.run([sys.executable,'-B','-c',code,ANAXI_FINAL,env.db_path,h],capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)['failure_evidence_id']==ids[-1]


def test_correction_5_recording_order_ignores_clock_rollback_and_occurrence():
    from unittest.mock import patch
    env=Env('c5_clock'); h,_=env.make_h()
    with patch.object(wfe.time,'time',return_value=1900000000):
        env.record_evidence(h,'WAKING_EXECUTION_FAILURE_NO_X_PERSISTED')
    with patch.object(wfe.time,'time',return_value=1800000000):
        newer=wfe.record_waking_failure(env.db_path,human_input_event_id=h,
            failure_class='AMBIGUOUS_PERSISTENCE_OUTCOME',basis='recorded second',occurred_at=1)
    with env.conn() as conn:
        assert wfe.latest_failure_evidence(conn,h)['failure_evidence_id']==newer
        assert recovery.assess_eligibility(conn,h,env.actor_id)['decision']==EligibilityDecision.INELIGIBLE


def test_correction_5_concurrent_evidence_writers_are_serialized():
    import threading
    env=Env('c5_concurrent'); h,_=env.make_h()
    gate=threading.Barrier(6); ids=[]; errors=[]; lock=threading.Lock()
    def write(i):
        try:
            gate.wait()
            ident=env.record_evidence(h,'WAKING_EXECUTION_FAILURE_NO_X_PERSISTED' if i%2 else 'AMBIGUOUS_PERSISTENCE_OUTCOME')
            with lock: ids.append(ident)
        except Exception as exc:
            with lock: errors.append(repr(exc))
    threads=[threading.Thread(target=write,args=(i,)) for i in range(6)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)
    assert not errors and len(ids)==6 and not any(t.is_alive() for t in threads)
    with env.conn() as conn:
        rows=conn.execute('SELECT rowid,failure_evidence_id FROM waking_failure_evidence ORDER BY rowid').fetchall()
        assert [r[0] for r in rows]==list(range(1,7))
        assert wfe.latest_failure_evidence(conn,h)['failure_evidence_id']==rows[-1][1]


def test_correction_5_sequence_exhaustion_refuses_random_rowid_fallback():
    env=Env('c5_exhaustion'); h,_=env.make_h()
    # Insert an extreme synthetic fixture directly; never rewrite existing rows.
    with env.conn() as conn:
        conn.execute(
            "INSERT INTO waking_failure_evidence (rowid, failure_evidence_id, "
            "human_input_event_id, failure_class, retry_safe, basis, recorded_at, occurred_at_unavailable) "
            "VALUES (9223372036854775807, 'extreme-fixture', ?, "
            "'AMBIGUOUS_PERSISTENCE_OUTCOME', 0, 'synthetic ceiling', 1, 1)", (h,))
    try:
        env.record_evidence(h,'WAKING_EXECUTION_FAILURE_NO_X_PERSISTED')
    except sqlite3.DatabaseError as exc:
        assert 'sequence exhausted' in str(exc)
    else:
        raise AssertionError('random rowid fallback permitted')
    with env.conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM waking_failure_evidence').fetchone()[0]==1
        assert recovery.assess_eligibility(conn,h,env.actor_id)['decision']==EligibilityDecision.INELIGIBLE


def test_correction_5_receipt_persistence_failure_blocks_generation():
    from unittest.mock import patch
    env=Env('c5_receipt_failure'); h,_=env.make_h()
    env.record_evidence(h,'WAKING_EXECUTION_FAILURE_NO_X_PERSISTED')
    calls=[]
    with patch.object(recovery,'_update_recovery',side_effect=sqlite3.OperationalError('synthetic write failure')):
        try:
            recovery.execute_recovery(env.db_path,h,env.actor_id,
                reset_fn=lambda: {'status':'SUCCEEDED','basis':'mocked positive reset'},
                generation_fn=lambda prompt: calls.append(prompt))
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError('receipt persistence unexpectedly succeeded')
    assert calls==[]
    with env.conn() as conn:
        assert recovery.recovery_status(conn,h)['terminal_state']=='RESERVED'
        assert recovery.assess_eligibility(conn,h,env.actor_id)['decision']==EligibilityDecision.INELIGIBLE


def test_correction_5_unprintable_observation_exception_is_still_unknown():
    class UnprintableError(Exception):
        def __str__(self):
            raise RuntimeError('exception formatting failed')
    result = cold_reset_waking_inference_path(
        _ollama_module(ps_raises=UnprintableError()), 'gemma4:e4b')
    assert result['status'] == ColdResetOutcome.UNKNOWN
    result = cold_reset_waking_inference_path(
        _ollama_module(chat_raises=UnprintableError()), 'gemma4:e4b')
    assert result['status'] == ColdResetOutcome.FAILED


ALL_TESTS = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    """WTR0-CORRECTION-1: no test in this permanent suite is allowed to
    report SKIPPED any longer -- the prior numpy-environment-gated
    skip path (and the three tests that required it) was removed; see
    this module's own docstring and the correction's builder evidence
    for what replaced those claims (synthetic/mock-boundary coverage
    of the same mechanical properties) and what remains explicitly
    NOT_ESTABLISHED in this sandbox (a real llama_anaxi.run_waking_turn()
    call, which needs numpy). `failed` is the only non-zero exit
    condition; there is no skip bucket left to silently absorb one."""
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
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed} SKIPPED=0")
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
