"""Command line: ``python -m claim_harness {claims,run,show}``.

Exit codes for ``run``:
  0  ledger written; no reproducible claim failed (NOT_ATTEMPTED, LIVE_ONLY,
     DISPOSITION and WITHDRAWN do not fail the run unless the inventory's
     policy says NOT_ATTEMPTED should)
  1  ledger written; at least one reproducible claim FAILed (or policy failure)
  2  accounting invalid: inventory, bindings or evidence cannot be accounted for;
     no ledger is written
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bindings import load_bindings
from .evidence import load_junit
from .inventory import AccountingError, load_inventory, load_json
from .ledger import EXIT_INVALID, STATES, build_ledger


def _load(args):
    root = Path(args.project_root)
    inventory = load_inventory(args.inventory)
    bindings = load_bindings(args.bindings, inventory, project_root=root)
    return root, inventory, bindings


def _print_invalid(exc: AccountingError) -> int:
    print("ACCOUNTING INVALID - no ledger written:", file=sys.stderr)
    for problem in exc.problems:
        print(f"  - {problem}", file=sys.stderr)
    return EXIT_INVALID


def cmd_claims(args) -> int:
    try:
        _, inventory, bindings = _load(args)
    except AccountingError as exc:
        return _print_invalid(exc)
    print(f"project: {inventory.project}  ({len(inventory.claims)} claims; inventory and bindings valid)")
    for claim in inventory.claims:
        kind = claim.evidence + (" (withdrawn)" if claim.withdrawn else "")
        question = f"  [{claim.question}]" if claim.question else ""
        print(f"\n{claim.id}  {kind}{question}\n  {claim.statement}")
        for node in bindings.required(claim.id):
            print(f"    requires {node.raw}")
    return 0


def _print_summary(ledger: dict, output) -> None:
    print(f"project: {ledger['project']}")
    width = max(len(c["id"]) for c in ledger["claims"])
    for claim in ledger["claims"]:
        counts = f" {claim['passed']}/{claim['required']}" if "required" in claim else ""
        print(f"  {claim['status']:<13} {claim['id']:<{width}}{counts:>6}  {claim['reason']}")
    summary = "  ".join(f"{state}={ledger['summary'][state]}" for state in STATES)
    print(f"summary: {summary}")
    code = ledger["code_state"] or {}
    head = code.get("head") or code.get("reason")
    print(f"code state: {code.get('status')} {head}" + (f" dirty={code['dirty']}" if code.get("dirty") is not None else ""))
    guard = sorted({run["properties"].get("claim_harness.network_guard", "not recorded")
                    for run in ledger["inputs"]["runs"]})
    print(f"network guard during evidence run: {', '.join(guard) or 'no runs'}")
    if output:
        print(f"ledger: {output}")
    verdict = ledger["verdict"]
    print(f"exit {verdict['exit_code']}: " + ("; ".join(verdict["reasons"]) or "no reproducible claim failed"))


def cmd_run(args) -> int:
    try:
        root, inventory, bindings = _load(args)
        runs = [load_junit(path) for path in args.junit]
        ledger = build_ledger(inventory, bindings, runs, project_root=root)
    except AccountingError as exc:
        return _print_invalid(exc)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    _print_summary(ledger, args.output)
    return ledger["verdict"]["exit_code"]


def cmd_show(args) -> int:
    ledger = load_json(args.ledger)
    for claim in ledger["claims"]:
        print(f"{claim['id']}  {claim['status']}  ({claim['evidence_kind']})")
        print(f"  claim:  {claim['statement']}")
        print(f"  reason: {claim['reason']}")
        for item in claim.get("evidence", []) + claim.get("observed_evidence", []):
            message = f" - {item['message']}" if item["message"] else ""
            print(f"    {item['outcome']:<12} {item['id']}{message}")
        for key in ("live_record", "disposition", "withdrawn"):
            if key in claim:
                print(f"  {key}: " + "; ".join(f"{k}={v}" for k, v in claim[key].items()))
        print()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="claim_harness", description="Bind claims to executed evidence.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--inventory", required=True, help="claim inventory JSON")
        p.add_argument("--bindings", required=True, help="claim -> pytest node ID bindings JSON")
        p.add_argument("--project-root", default=".",
                       help="pytest rootdir the node IDs are relative to (default: current directory)")

    p_claims = sub.add_parser("claims", help="validate and list claims with their required evidence")
    common(p_claims)
    p_claims.set_defaults(func=cmd_claims)

    p_run = sub.add_parser("run", help="build a ledger from executed evidence")
    common(p_run)
    p_run.add_argument("--junit", action="append", default=[], metavar="PATH",
                       help="pytest --junitxml output; repeat for several runs")
    p_run.add_argument("--output", help="where to write the ledger JSON")
    p_run.set_defaults(func=cmd_run)

    p_show = sub.add_parser("show", help="print a written ledger claim by claim")
    p_show.add_argument("ledger")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
