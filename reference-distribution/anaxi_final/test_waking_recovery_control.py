"""Owner/UI boundary regressions for the ordinary WTR0 recovery surface."""

from test_waking_turn_recovery import Env
import waking_recovery_control as control
import wtr0_waking_recovery as recovery
from wtr0_cold_reset import ColdResetOutcome


def _eligible(env, message="Recover this exact synthetic input."):
    human_input_event_id, _ = env.make_h(message)
    env.record_evidence(
        human_input_event_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED",
    )
    return human_input_event_id


def test_owner_list_enumerates_every_evidenced_h_without_top_n_sampling():
    env = Env("owner_control_enumerates_all")
    expected = {_eligible(env, f"Input {index}") for index in range(25)}
    receipts = control.list_candidates(env.db_path, env.actor_id)
    assert len(receipts) == len(expected) == 25
    assert {item["human_input_event_id"] for item in receipts} == expected
    assert all(item["decision"] == recovery.EligibilityDecision.ELIGIBLE for item in receipts)


def test_confirmation_and_background_gates_have_zero_consequential_calls_or_writes():
    env = Env("owner_control_gates")
    human_input_event_id = _eligible(env)
    calls = []

    def reset_fn():
        calls.append("reset")
        return {"status": ColdResetOutcome.SUCCEEDED, "basis": "synthetic"}

    def generation_fn(prompt):
        calls.append(("generation", prompt))
        return {}

    for confirmation, running, expected_basis in (
        (False, False, "EXPLICIT_CONFIRMATION_REQUIRED"),
        (True, True, "BACKGROUND_ACTIVITY_MUST_BE_PAUSED"),
    ):
        try:
            control.execute_candidate(
                env.db_path, human_input_event_id=human_input_event_id,
                actor_id=env.actor_id, confirmation=confirmation,
                background_activity_running=running,
                expected_visibility_scope=None, reset_fn=reset_fn,
                generation_fn=generation_fn,
            )
        except recovery.RecoveryDenied as denied:
            assert denied.receipt["basis"] == expected_basis
        else:
            raise AssertionError("owner recovery safety gate did not refuse")

    assert calls == []
    with env.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM wtr0_recovery").fetchone()[0] == 0


def test_scope_mismatch_refuses_before_reset_or_reservation():
    env = Env("owner_control_scope")
    human_input_event_id = _eligible(env)
    calls = []
    try:
        control.execute_candidate(
            env.db_path, human_input_event_id=human_input_event_id,
            actor_id=env.actor_id, confirmation=True,
            background_activity_running=False,
            expected_visibility_scope="family_shared",
            reset_fn=lambda: calls.append("reset"),
            generation_fn=lambda prompt: calls.append("generation"),
        )
    except recovery.RecoveryDenied as denied:
        assert denied.receipt["basis"] == "CURRENT_BOUND_SCOPE_DOES_NOT_MATCH_H"
    else:
        raise AssertionError("scope mismatch was accepted")
    assert calls == []
    with env.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM wtr0_recovery").fetchone()[0] == 0


def test_confirmed_matching_request_executes_exactly_one_attempt():
    env = Env("owner_control_one_attempt")
    human_input_event_id = _eligible(env, "Use my exact durable text.")
    resets = []
    generations = []

    def reset_fn():
        resets.append(True)
        return {"status": ColdResetOutcome.FAILED, "basis": "bounded synthetic failure"}

    outcome = control.execute_candidate(
        env.db_path, human_input_event_id=human_input_event_id,
        actor_id=env.actor_id, confirmation=True,
        background_activity_running=False,
        expected_visibility_scope=None, reset_fn=reset_fn,
        generation_fn=lambda prompt: generations.append(prompt),
    )
    assert outcome["terminal_state"] == "RESET_FAILED"
    assert resets == [True]
    assert generations == []
    with env.conn() as conn:
        row = conn.execute(
            "SELECT terminal_state FROM wtr0_recovery WHERE human_input_event_id=?",
            (human_input_event_id,),
        ).fetchone()
    assert row == ("RESET_FAILED",)
