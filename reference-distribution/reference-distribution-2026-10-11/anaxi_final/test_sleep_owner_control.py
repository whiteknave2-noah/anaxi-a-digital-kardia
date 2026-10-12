"""Ordinary owner Sleep controls preserve request and execution authority."""

from types import SimpleNamespace

import pytest

import sleep_owner_control as control
import sleep_timing_knock as timing
import test_sleep_timing_knock as fixture


def _pending(name):
    data_dir = fixture._new_env(name)
    owner = fixture._register_owner(data_dir, name)
    request_id, _ = fixture._make_pending(data_dir)
    return data_dir, owner, request_id


def test_lists_every_actionable_request_not_a_sample():
    data_dir, owner, first = fixture._c2_authorized("owner_control_list")
    expected = {first}
    # More than the old UI cap proves that discovery is exhaustive, not a
    # superficially healthy newest-20 sample.
    for index in range(24):
        pending, _ = fixture._make_pending(data_dir)
        expected.add(pending)
        control.apply_owner_action(
            data_dir, request_id=pending, owner_actor_id=owner,
            action="AUTHORIZE", occurred_at=2000 + index,
        )
    receipts = control.list_requests(data_dir)
    assert len(receipts) == len(expected) == 25
    assert {r["request_id"] for r in receipts} == expected


def test_owner_actions_use_registered_human_gate():
    data_dir, owner, request_id = _pending("owner_control_action")
    result = control.apply_owner_action(
        data_dir, request_id=request_id, owner_actor_id=owner,
        action="AUTHORIZE", occurred_at=2000,
    )
    assert result["state"] == timing.STATE_AUTHORIZED
    with pytest.raises(timing.RequestRejected):
        control.apply_owner_action(
            data_dir, request_id=request_id, owner_actor_id=owner,
            action="invent", occurred_at=2001,
        )


def test_execution_requires_confirmation_and_stopped_background():
    data_dir, owner, request_id = fixture._c2_authorized("owner_control_execution_gates")
    calls = []
    run = lambda: calls.append(1) or SimpleNamespace(status="completed")
    with pytest.raises(timing.RequestRejected, match="confirmation"):
        control.execute_authorized_request(
            data_dir, request_id=request_id, owner_actor_id=owner,
            confirmation=False, background_activity_running=False,
            run_sleep_cycle=run, attempted_at=3000,
        )
    with pytest.raises(timing.RequestRejected, match="pause background"):
        control.execute_authorized_request(
            data_dir, request_id=request_id, owner_actor_id=owner,
            confirmation=True, background_activity_running=True,
            run_sleep_cycle=run, attempted_at=3001,
        )
    assert calls == []


def test_confirmed_execution_calls_cycle_once_and_records_receipt():
    data_dir, owner, request_id = fixture._c2_authorized("owner_control_execution")
    calls = []
    outcome = control.execute_authorized_request(
        data_dir, request_id=request_id, owner_actor_id=owner,
        confirmation=True, background_activity_running=False,
        run_sleep_cycle=lambda: calls.append(1) or SimpleNamespace(status="completed"),
        attempted_at=4000,
    )
    assert calls == [1]
    assert outcome["outcome"] == "succeeded"
    receipt = timing.fetch_sleep_request(data_dir, request_id)
    assert receipt["execution_attempts"][-1]["operator_actor_id"] == owner
