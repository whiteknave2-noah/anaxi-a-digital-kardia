"""WTR0: explicit, operator-invoked Waking Turn Recovery command.

This is the ONLY independently-triggered mechanical surface for WTR0
eligibility assessment and recovery execution. It is a plain CLI
(argparse, four subcommands), not a daemon, not a scheduler, not a
background poller -- nothing here runs unless an operator invokes it,
and each invocation does exactly one bounded thing and exits.

Requires the canonical human_waking_input event id explicitly
(`--event-id`) -- never accepts arbitrary free-form recovery text as
the recovery's identity. `ANAXI_BOUND_HUMAN_ACTOR_ID` (same environment
convention as resume_human_input.py) names the registered human actor
whose input is being recovered.

Subcommands:
  eligibility --event-id ID   Read-only. Prints the eligibility
                               receipt (see wtr0_waking_recovery.
                               assess_eligibility()). No write of any
                               kind.
  recover --event-id ID       Reserves the one recovery attempt (if
                               and only if currently ELIGIBLE under a
                               fresh re-check), cold-resets the waking
                               inference path, and -- only if reset
                               positively succeeds -- retries the SAME
                               canonical H once through the existing
                               lawful llama_anaxi.run_waking_turn(...,
                               existing_human_input_event_id=...)
                               pathway, exactly as resume_human_input.
                               py's own --resume flow already does.
                               Same backend-must-be-stopped guard as
                               resume_human_input.py, for the same
                               reason (this performs real generation).
  status --event-id ID        Read-only. Prints the durable
                               wtr0_recovery row for this H, if any.
  reassess-workspace-conflict
         --event-id ID        Evidence-bound correction for an older
                               conservatively classified pre-action
                               WORKSPACE_ACTION_CONFLICT. Appends a
                               qualifying row only when canonical state,
                               the adjacent UI diagnostic, and adjacent
                               direction trace all agree; never edits old
                               evidence and never performs generation.

Clark stays down, Sleep stays disabled: nothing here starts a GUI,
schedules Sleep, or performs any action beyond one bounded waking
retry for one explicitly-named H per invocation.
"""
import argparse
import json
import os
import socket
import sqlite3
from contextlib import closing
from pathlib import Path

import wtr0_waking_recovery as recovery
from wtr0_cold_reset import cold_reset_waking_inference_path
from wtr0_workspace_conflict_reassessment import (
    ReassessmentDenied,
    reassess_workspace_conflict,
)


def _require_actor_id():
    actor_id = os.environ.get("ANAXI_BOUND_HUMAN_ACTOR_ID")
    if not actor_id:
        raise SystemExit("ANAXI_BOUND_HUMAN_ACTOR_ID must identify the registered human")
    return actor_id


def cmd_eligibility(args):
    actor_id = _require_actor_id()
    with closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)) as conn:
        receipt = recovery.assess_eligibility(conn, args.event_id, actor_id)
    print(json.dumps(receipt, indent=2))
    return 0


def cmd_status(args):
    with closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)) as conn:
        row = recovery.recovery_status(conn, args.event_id)
    print(json.dumps(row, indent=2))
    return 0


def cmd_reassess_workspace_conflict(args):
    actor_id = _require_actor_id()
    try:
        reassessment = reassess_workspace_conflict(args.db, args.event_id, actor_id)
    except ReassessmentDenied as denied:
        print(json.dumps({"decision": "DENIED", "basis": denied.code}, indent=2))
        return 1
    with closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)) as conn:
        eligibility = recovery.assess_eligibility(conn, args.event_id, actor_id)
    print(json.dumps({
        "decision": "RECORDED" if reassessment["created"] else "UNCHANGED",
        "reassessment": reassessment,
        "eligibility": eligibility,
    }, indent=2))
    return 0 if eligibility["decision"] == recovery.EligibilityDecision.ELIGIBLE else 1


def cmd_recover(args):
    actor_id = _require_actor_id()

    # Same production-safety guard as resume_human_input.py: this
    # command performs real generation, so a live backend on the
    # ordinary waking port must not be running concurrently.
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", 7860)) == 0:
            raise SystemExit(
                "Stop the existing Desktop backend before running WTR0 recovery; "
                "closing its browser tab is insufficient"
            )

    import llama_anaxi as waking
    import human_session_binding as binding
    from orchestration import AnaxiOrchestrator
    import ollama

    with closing(sqlite3.connect(args.db)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            recovery_scope = recovery.canonical_human_input_visibility_scope(
                conn, args.event_id, actor_id,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        authority = binding.bind_session_to_registered_human(
            conn, session_id=waking.get_current_session_id(),
            session_started_at=waking.get_current_session_started_at(),
            pipeline_key=waking.PIPELINE_KEY, actor_id=actor_id,
            visibility_scope=recovery_scope,
        )

    def reset_fn():
        return cold_reset_waking_inference_path(ollama, waking.MODEL)

    orch = AnaxiOrchestrator(waking.DB_PATH)
    try:
        def generation_fn(prompt):
            return waking.run_waking_turn(
                orch, prompt, interaction_mode=waking.CONVERSATION_MODE,
                human_input_authority=authority, existing_human_input_event_id=args.event_id,
            )

        try:
            outcome = recovery.execute_recovery(
                args.db, args.event_id, actor_id, reset_fn=reset_fn, generation_fn=generation_fn,
            )
        except recovery.RecoveryDenied as denied:
            print(json.dumps({"terminal_state": "DENIED", "receipt": denied.receipt}, indent=2))
            return 1
    finally:
        orch.close()

    print(json.dumps(outcome, indent=2))
    return 0 if outcome.get("terminal_state") == "SUCCESS" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=str(Path(__file__).resolve().parent / "anaxi_provenance.db"),
        help="Path to anaxi_provenance.db (defaults to the adjacent production path; "
             "ALWAYS pass --db explicitly for a synthetic/temp database)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_elig = sub.add_parser("eligibility", help="Read-only eligibility assessment for one H")
    p_elig.add_argument("--event-id", required=True)
    p_elig.set_defaults(func=cmd_eligibility)

    p_rec = sub.add_parser("recover", help="Reserve and execute one recovery attempt for one H")
    p_rec.add_argument("--event-id", required=True)
    p_rec.set_defaults(func=cmd_recover)

    p_status = sub.add_parser("status", help="Read-only recovery status for one H")
    p_status.add_argument("--event-id", required=True)
    p_status.set_defaults(func=cmd_status)

    p_reassess = sub.add_parser(
        "reassess-workspace-conflict",
        help="Append retry-safe evidence only for a fully corroborated pre-action conflict",
    )
    p_reassess.add_argument("--event-id", required=True)
    p_reassess.set_defaults(func=cmd_reassess_workspace_conflict)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
