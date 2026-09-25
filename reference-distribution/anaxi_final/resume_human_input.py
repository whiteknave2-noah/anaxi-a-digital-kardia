"""Explicit operator continuation of one unanswered canonical H. No auto-retry.

WTR0-CORRECTION-1: this is a thin legacy-named front end over the SAME
authoritative WTR0 recovery gate `wtr0_cli.py recover` already uses --
`wtr0_waking_recovery.execute_recovery()`. It is NOT a second retry
counter and does NOT implement its own eligibility policy: the one
authoritative one-attempt ceiling and eligibility law lives entirely in
`wtr0_waking_recovery.py` (durable `wtr0_recovery` UNIQUE-constrained
reservation, re-checked fresh immediately before every reservation).
This file only supplies the legacy operator surface (argument parsing,
the pre-existing backend-must-be-stopped guard, and the read-only
metadata check) around that one gate.

Historically this script called `llama_anaxi.run_waking_turn(...,
existing_human_input_event_id=...)` directly, gated only on "does H
already have a canonical X" -- which meant a WTR0 recovery already
reserved/consumed for H (successfully or not) did not stop a second,
legacy-triggered generation for the same H: the one-attempt ceiling was
CLI-local to wtr0_cli.py, not system-authoritative. `resume_human_input.py`'s
own entire purpose -- explicit operator continuation of an unanswered H
-- IS the same waking-recovery act WTR0 governs; there is no other,
unrelated legitimate use of this script's `--resume` flag to preserve
unchanged. Routing it through `execute_recovery()` closes that bypass:
a legacy resume attempt now requires the same qualifying retry-safe
failure evidence, is reserved through the same UNIQUE-constrained
durable row, and is refused (`RecoveryDenied`) under exactly the same
conditions `wtr0_cli.py recover` already refuses under -- no canonical
X alone was never sufficient authorization, and it still isn't; an
unanswered H with no qualifying failure evidence can no longer use this
script as a back door around WTR0 eligibility.

Stop the running backend first. Without --resume this reads only H metadata.
With --resume, WTR0 reserves the one recovery attempt (if and only if
currently ELIGIBLE under a fresh re-check), cold-resets the waking
inference path, and -- only if reset positively succeeds -- retries the
SAME canonical H once through the existing lawful
llama_anaxi.run_waking_turn(..., existing_human_input_event_id=...)
pathway. No new H is minted, and this can succeed at most once per H,
regardless of which entrypoint (this script or wtr0_cli.py) reserves it.
"""
import argparse
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import socket


def read_unanswered_input(path, event_id, actor_id):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        row = conn.execute(
            "SELECT ec.component_text FROM events e JOIN event_components ec ON ec.event_id=e.event_id "
            "JOIN auth_contexts a ON a.auth_context_id=e.auth_context_id "
            "WHERE e.event_id=? AND e.event_type='human_waking_input' AND ec.sequence=0 "
            "AND ec.component_kind='human_conversational_input' AND ec.creator_actor_id=? "
            "AND a.auth_state='authenticated' AND a.authenticated_actor_id=ec.creator_actor_id",
            (event_id, actor_id),
        ).fetchone()
        if row is None:
            raise ValueError('H does not resolve to authenticated input from the configured human')
        if conn.execute(
            "SELECT 1 FROM event_components c JOIN events e ON e.event_id=c.event_id "
            "WHERE c.component_kind='human_input_event_id' AND c.component_text=? "
            "AND e.event_type='waking_turn'", (event_id,),
        ).fetchone():
            raise ValueError('H already has a canonical X; refusing another continuation')
        return row[0]


def resume_via_wtr0_gate(
    db_path, event_id, actor_id, *, waking, binding, orchestrator_cls, ollama_module,
    recovery_module, cold_reset_fn,
):
    """The one consequential same-H generation path this script may
    ever take -- extracted from main() so it is directly, mechanically
    testable with fake `waking`/`orchestrator_cls`/`ollama_module`
    modules (no real llama_anaxi/orchestration/numpy import required)
    while remaining the EXACT function main() itself calls for a real
    `--resume` invocation. `recovery_module` is `wtr0_waking_recovery`
    (passed explicitly, matching this repository's existing test-
    injection convention, e.g. run_waking_turn_capturing_failure()'s
    own `run_waking_turn_fn` parameter) -- reservation, the fresh
    fail-closed eligibility re-check, and the durable one-attempt
    UNIQUE constraint all live there, unchanged and unduplicated; this
    function supplies only the reset/generation closures around it,
    exactly like wtr0_cli.py's own `cmd_recover` does.

    Returns the `execute_recovery()` outcome dict on any consequential
    or non-consequential terminal state. Raises `recovery_module.
    RecoveryDenied` unchanged if reservation itself was refused (H
    already answered, no qualifying retry-safe evidence, or a WTR0
    recovery -- reserved through EITHER this script or wtr0_cli.py --
    already exists for this H) -- no consequential action of any kind
    was taken in that case, by `execute_recovery()`'s own contract."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        authority = binding.bind_session_to_registered_human(
            conn, session_id=waking.get_current_session_id(),
            session_started_at=waking.get_current_session_started_at(),
            pipeline_key=waking.PIPELINE_KEY, actor_id=actor_id,
        )

    def reset_fn():
        return cold_reset_fn(ollama_module, waking.MODEL)

    orch = orchestrator_cls(waking.DB_PATH)
    try:
        def generation_fn(canonical_prompt):
            return waking.run_waking_turn(
                orch, canonical_prompt, interaction_mode=waking.CONVERSATION_MODE,
                human_input_authority=authority, existing_human_input_event_id=event_id,
            )

        return recovery_module.execute_recovery(
            str(db_path), event_id, actor_id, reset_fn=reset_fn, generation_fn=generation_fn,
        )
    finally:
        orch.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event-id', required=True)
    parser.add_argument('--resume', action='store_true', help='Explicitly attempt one WTR0-gated recovery generation')
    args = parser.parse_args()
    base = Path(__file__).resolve().parent
    actor_id = os.environ.get('ANAXI_BOUND_HUMAN_ACTOR_ID')
    if not actor_id:
        parser.error('ANAXI_BOUND_HUMAN_ACTOR_ID must identify the registered human')
    db_path = base / 'anaxi_provenance.db'
    prompt = read_unanswered_input(db_path, args.event_id, actor_id)
    print(f'Unanswered authenticated H: {args.event_id}; exact input length: {len(prompt)}')
    if not args.resume:
        print('Read-only check complete. No inference or canonical writes performed.')
        return
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', 7860)) == 0:
            parser.error('Stop the existing Desktop backend before resuming H; closing its browser tab is insufficient')
    import llama_anaxi as waking
    import human_session_binding as binding
    from orchestration import AnaxiOrchestrator
    import ollama
    import wtr0_waking_recovery as recovery
    from wtr0_cold_reset import cold_reset_waking_inference_path

    try:
        outcome = resume_via_wtr0_gate(
            db_path, args.event_id, actor_id, waking=waking, binding=binding,
            orchestrator_cls=AnaxiOrchestrator, ollama_module=ollama, recovery_module=recovery,
            cold_reset_fn=cold_reset_waking_inference_path,
        )
    except recovery.RecoveryDenied as denied:
        print(json.dumps({"terminal_state": "DENIED", "receipt": denied.receipt}, indent=2))
        raise SystemExit(1)

    print(json.dumps(outcome, indent=2))
    if outcome.get('terminal_state') == 'SUCCESS':
        print(f"Canonical X: {outcome['canonical_x_event_id']}")
    else:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
