"""Ordinary owner control boundary for Clark-originated Sleep requests."""

from __future__ import annotations

import time
from typing import Callable

import sleep_timing_knock as sleep_timing


def list_requests(data_dir: str) -> list[dict]:
    return sleep_timing.list_owner_actionable_sleep_requests(data_dir)


def apply_owner_action(data_dir: str, *, request_id: str, owner_actor_id: str,
                       action: str, occurred_at: int | None = None) -> dict:
    if action not in {"AUTHORIZE", "DEFER", "DECLINE"}:
        raise sleep_timing.RequestRejected(f"unsupported owner Sleep action {action!r}")
    return sleep_timing.apply_owner_transition(
        data_dir, request_id=request_id, owner_actor_id=owner_actor_id,
        action=action, occurred_at=int(time.time()) if occurred_at is None else occurred_at,
        note=None,
    )


def execute_authorized_request(
    data_dir: str, *, request_id: str, owner_actor_id: str,
    confirmation: bool, background_activity_running: bool,
    run_sleep_cycle: Callable[[], object], attempted_at: int | None = None,
) -> dict:
    """Perform exactly one explicit attempt after all owner/UI safety gates."""
    if confirmation is not True:
        raise sleep_timing.RequestRejected("explicit real-Sleep confirmation is required")
    if background_activity_running:
        raise sleep_timing.RequestRejected("pause background Space activity before executing Sleep")

    def run_fn():
        result = run_sleep_cycle()
        return {"status": result.status}

    return sleep_timing.record_sleep_execution_attempt(
        data_dir, request_id=request_id, operator_actor_id=owner_actor_id,
        attempted_at=int(time.time()) if attempted_at is None else attempted_at,
        run_fn=run_fn,
    )
