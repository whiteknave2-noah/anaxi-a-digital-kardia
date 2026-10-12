"""SLP2: explicit, operator-invoked owner response + Sleep-execution
command for a Clark-originated Sleep Request.

This is the ONLY independently-triggered mechanical surface for the
owner's response (authorize/defer/decline) and for executing Sleep
against an already-AUTHORIZED request. It is a plain CLI (argparse),
not a daemon, not a scheduler, not a background poller -- nothing here
runs unless an operator invokes it, and each invocation does exactly
one bounded thing and exits. Clark's own REQUEST/KNOCK/WITHDRAW acts
are never issued from this CLI -- those originate only through his own
typed Pass-1 choice inside an actual waking turn (see
conversation_direction.py's sleep_timing_request field and
llama_anaxi.py's post-commit dispatch); this file has no command that
creates one on his behalf.

`ANAXI_BOUND_HUMAN_ACTOR_ID` (same environment convention as
resume_human_input.py and wtr0_cli.py) names the registered human
actor recording the owner response -- this codebase has always been a
single-registered-human system, so "the registered human" and "the
owner" are the same actor; sleep_timing_knock.apply_owner_transition()
itself re-validates this actor_id resolves to a genuine registered
human_person at call time regardless of what this CLI assumes.

Subcommands:
  status --request-id ID                 Read-only. Prints the
                                          complete mechanical status
                                          receipt (see
                                          sleep_timing_knock.
                                          fetch_sleep_request()). No
                                          write of any kind.
  open                                    Read-only. Prints the
                                          request_id of Clark's own
                                          currently unresolved
                                          request, or null.
  defer --request-id ID [--note TEXT]    Owner explicitly defers.
  authorize --request-id ID [--note TEXT]
                                          Owner explicitly authorizes.
                                          Never itself runs Sleep.
  decline --request-id ID [--note TEXT]  Owner explicitly declines.
  execute-sleep --request-id ID          Explicit, separately-invoked
                                          attempt to run Sleep for an
                                          ALREADY-AUTHORIZED request
                                          through the existing manual
                                          Sleep v1 orchestrator
                                          (sleep_cycle.
                                          run_sleep_cycle_with_
                                          production_defaults). Refuses
                                          outright if the request is
                                          not currently AUTHORIZED.
                                          Scheduled Sleep stays
                                          disabled; this performs one
                                          bounded, explicit Sleep
                                          cycle attempt and nothing
                                          else. Requires --confirm-real-
                                          sleep to guard against an
                                          accidental invocation, since
                                          this is the one command in
                                          this file capable of a real
                                          side effect beyond this
                                          database.

Clark stays down: nothing here starts a GUI, wakes Clark, or performs
any waking-turn action of any kind.
"""
import argparse
import json
import os
from contextlib import closing

import sleep_timing_knock as slp2


def _require_actor_id():
    actor_id = os.environ.get("ANAXI_BOUND_HUMAN_ACTOR_ID")
    if not actor_id:
        raise SystemExit("ANAXI_BOUND_HUMAN_ACTOR_ID must identify the registered human recording this response")
    return actor_id


def _data_dir(args):
    # sleep_timing_knock.py takes a data_dir (directory containing
    # anaxi_provenance.db), matching outward_communication.py's own
    # convention -- not a bare db file path.
    return os.path.dirname(os.path.abspath(args.db))


def cmd_status(args):
    receipt = slp2.fetch_sleep_request(_data_dir(args), args.request_id)
    print(json.dumps(receipt, indent=2))
    return 0 if receipt is not None else 1


def cmd_open(args):
    request_id = slp2.fetch_open_request_for_actor(_data_dir(args))
    print(json.dumps({"open_request_id": request_id}, indent=2))
    return 0


def _owner_action(args, action):
    import time
    actor_id = _require_actor_id()
    try:
        record = slp2.apply_owner_transition(
            _data_dir(args), request_id=args.request_id, owner_actor_id=actor_id,
            action=action, occurred_at=int(time.time()), note=args.note,
        )
    except slp2.RequestRejected as denied:
        print(json.dumps({"status": "rejected", "detail": str(denied)}, indent=2))
        return 1
    print(json.dumps(record, indent=2))
    return 0


def cmd_defer(args):
    return _owner_action(args, "DEFER")


def cmd_authorize(args):
    return _owner_action(args, "AUTHORIZE")


def cmd_decline(args):
    return _owner_action(args, "DECLINE")


def cmd_execute_sleep(args):
    import time
    actor_id = _require_actor_id()
    if not args.confirm_real_sleep:
        raise SystemExit("--confirm-real-sleep is required: this command can perform a real Sleep cycle")

    # Deliberately imported only here, never at module scope: this is
    # the one command in this file with a real side effect beyond this
    # database, and every other subcommand must remain importable and
    # runnable (status/open/defer/authorize/decline) without pulling in
    # the Sleep v1 orchestrator or its own dependency graph at all.
    import sleep_cycle

    def run_fn():
        result = sleep_cycle.run_sleep_cycle_with_production_defaults()
        return {"status": result.status}

    # SLP2-CORRECTION-1: AUTHORIZATION IS NOT CONSUMED BY A SLEEP
    # ATTEMPT (owner-frozen policy) -- this command may be invoked any
    # number of times against the same AUTHORIZED request, each a
    # separate, explicit operator action, each independently
    # attributed to actor_id and independently provenance-bearing. No
    # automatic retry exists anywhere in this file or in
    # record_sleep_execution_attempt() itself -- exactly one run_fn()
    # call happens per invocation of this command.
    try:
        outcome = slp2.record_sleep_execution_attempt(
            _data_dir(args), request_id=args.request_id, operator_actor_id=actor_id,
            attempted_at=int(time.time()), run_fn=run_fn,
        )
    except slp2.RequestRejected as denied:
        print(json.dumps({"status": "rejected", "detail": str(denied)}, indent=2))
        return 1
    print(json.dumps(outcome, indent=2))
    return 0 if outcome.get("outcome") == "succeeded" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "anaxi_provenance.db"),
        help="Path to anaxi_provenance.db (defaults to the adjacent production path; "
             "ALWAYS pass --db explicitly for a synthetic/temp database)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Read-only mechanical status for one Sleep request")
    p_status.add_argument("--request-id", required=True)
    p_status.set_defaults(func=cmd_status)

    p_open = sub.add_parser("open", help="Read-only: Clark's own currently unresolved request, if any")
    p_open.set_defaults(func=cmd_open)

    p_defer = sub.add_parser("defer", help="Owner explicitly defers a request")
    p_defer.add_argument("--request-id", required=True)
    p_defer.add_argument("--note", default=None)
    p_defer.set_defaults(func=cmd_defer)

    p_auth = sub.add_parser("authorize", help="Owner explicitly authorizes a request")
    p_auth.add_argument("--request-id", required=True)
    p_auth.add_argument("--note", default=None)
    p_auth.set_defaults(func=cmd_authorize)

    p_decl = sub.add_parser("decline", help="Owner explicitly declines a request")
    p_decl.add_argument("--request-id", required=True)
    p_decl.add_argument("--note", default=None)
    p_decl.set_defaults(func=cmd_decline)

    p_exec = sub.add_parser("execute-sleep", help="Execute Sleep for an already-AUTHORIZED request")
    p_exec.add_argument("--request-id", required=True)
    p_exec.add_argument("--confirm-real-sleep", action="store_true")
    p_exec.set_defaults(func=cmd_execute_sleep)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
