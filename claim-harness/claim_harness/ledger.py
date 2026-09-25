"""The claim ledger: each claim's truthful disposition, derived mechanically.

Claim states
------------
PASS           executable; every one of its N required bindings ran and passed (M == N).
FAIL           executable; at least one required binding ran and failed or errored.
NOT_ATTEMPTED  executable; nothing failed, but some required binding was skipped,
               deselected or absent from the executed evidence (M < N).
LIVE_ONLY      observed in a live setting only; recorded, not reproduced here.
DISPOSITION    settled by an owner/project decision; not execution evidence.
WITHDRAWN      no longer claimed; never counts as PASS, whatever its old tests do.

Only PASS is a positive result, and only executed evidence can produce it.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Iterable, Optional

from .bindings import Bindings
from .codestate import code_state as _code_state
from .evidence import ERROR, FAILED, PASSED, SKIPPED, EvidenceRun, sha256_file
from .inventory import DISPOSITION, EXECUTABLE, LIVE_ONLY, AccountingError, Inventory

LEDGER_SCHEMA = "claim-harness.ledger/v0"

PASS = "PASS"
FAIL = "FAIL"
NOT_ATTEMPTED = "NOT_ATTEMPTED"
LIVE_ONLY_STATUS = "LIVE_ONLY"
DISPOSITION_STATUS = "DISPOSITION"
WITHDRAWN = "WITHDRAWN"
STATES = (PASS, FAIL, NOT_ATTEMPTED, LIVE_ONLY_STATUS, DISPOSITION_STATUS, WITHDRAWN)

NOT_EXECUTED = "not_executed"

EXIT_OK = 0
EXIT_CLAIMS_FAILED = 1
EXIT_INVALID = 2


def _locate(node, runs: list) -> tuple:
    """Find a node's executed result across runs -> (outcome, message, run_id, seconds)."""
    key = node.junit_key
    hits = [(run, run.results[key]) for run in runs if key in run.results]
    if len(hits) > 1 or any(key in run.duplicates for run in runs):
        raise AccountingError([f"evidence {node.raw!r} appears more than once in the executed results; "
                               "its outcome is ambiguous"])
    if hits:
        run, result = hits[0]
        return result.outcome, result.message, run.run_id, result.seconds
    for run in runs:
        if node.module_key in run.collection_errors:
            return ERROR, f"test module failed to collect: {run.collection_errors[node.module_key]}", run.run_id, None
    return NOT_EXECUTED, "absent from the executed evidence (not collected, deselected or not run)", None, None


def _executable_record(nodes: tuple, runs: list, root) -> dict:
    evidence = []
    for node in nodes:
        outcome, message, run_id, seconds = _locate(node, runs)
        evidence.append({
            "id": node.raw,
            "file": node.path,
            "file_sha256": sha256_file(root / node.path) if root is not None else None,
            "outcome": outcome,
            "message": message,
            "run_id": run_id,
            "seconds": seconds,
        })
    required = len(nodes)
    passed = sum(1 for e in evidence if e["outcome"] == PASSED)
    failed = [e["id"] for e in evidence if e["outcome"] in (FAILED, ERROR)]
    missing = [e["id"] for e in evidence if e["outcome"] in (SKIPPED, NOT_EXECUTED)]
    assert passed + len(failed) + len(missing) == required
    if failed:
        status = FAIL
        reason = f"{len(failed)} of {required} required evidence failed: " + ", ".join(failed)
    elif passed == required:
        status = PASS
        reason = f"all {required} required evidence ran and passed"
    else:
        status = NOT_ATTEMPTED
        reason = (f"{passed} of {required} required evidence passed; not executed: " + ", ".join(missing))
    return {"status": status, "reason": reason, "required": required, "passed": passed,
            "failed": failed, "not_executed": missing, "evidence": evidence}


def build_ledger(inventory: Inventory, bindings: Bindings, runs: Iterable[EvidenceRun] = (),
                 project_root=None, code_state: Optional[dict] = None, now: Optional[str] = None) -> dict:
    runs = list(runs)
    root = Path(project_root).resolve() if project_root is not None else None
    claims = []
    for claim in inventory.claims:
        base = {"id": claim.id, "statement": claim.statement, "question": claim.question,
                "evidence_kind": claim.evidence}
        nodes = bindings.required(claim.id)
        if claim.withdrawn is not None:
            record = {"status": WITHDRAWN, "reason": f"withdrawn: {claim.withdrawn['reason']}",
                      "withdrawn": dict(claim.withdrawn)}
            if nodes:
                observed = _executable_record(nodes, runs, root)
                record["observed_evidence"] = observed["evidence"]
                record["observed_evidence_note"] = ("recorded for history only; a withdrawn claim does not "
                                                    "become PASS or FAIL from its old evidence")
        elif claim.evidence == EXECUTABLE:
            record = _executable_record(nodes, runs, root)
        elif claim.evidence == LIVE_ONLY:
            record = {"status": LIVE_ONLY_STATUS,
                      "reason": "live observation recorded; not reproducible offline and not re-run here",
                      "live_record": dict(claim.live_record)}
        elif claim.evidence == DISPOSITION:
            record = {"status": DISPOSITION_STATUS,
                      "reason": f"disposition by {claim.disposition['decided_by']}: "
                                f"{claim.disposition['decision']}",
                      "disposition": dict(claim.disposition)}
        else:  # parse_inventory prevents this
            raise AccountingError([f"claim {claim.id}: unknown evidence kind {claim.evidence!r}"])
        claims.append({**base, **record})

    summary = {state: sum(1 for c in claims if c["status"] == state) for state in STATES}
    reasons = []
    failing = [c["id"] for c in claims if c["status"] == FAIL]
    if failing:
        reasons.append("reproducible claims FAIL: " + ", ".join(failing))
    not_attempted = [c["id"] for c in claims if c["status"] == NOT_ATTEMPTED]
    if not_attempted and inventory.not_attempted_fails_run:
        reasons.append("policy not_attempted_fails_run: NOT_ATTEMPTED claims: " + ", ".join(not_attempted))

    if code_state is None and root is not None:
        files = sorted({e["file"] for c in claims for e in c.get("evidence", c.get("observed_evidence", []))})
        code_state = _code_state(root, files)

    return {
        "schema": LEDGER_SCHEMA,
        "project": inventory.project,
        "generated_at": now or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "inventory": {"path": inventory.source,
                          "sha256": sha256_file(inventory.source) if inventory.source else None},
            "bindings": {"path": bindings.source,
                         "sha256": sha256_file(bindings.source) if bindings.source else None},
            "runs": [run.describe() for run in runs],
        },
        "policy": {"not_attempted_fails_run": inventory.not_attempted_fails_run},
        "code_state": code_state,
        "summary": summary,
        "verdict": {"exit_code": EXIT_CLAIMS_FAILED if reasons else EXIT_OK, "reasons": reasons},
        "claims": claims,
    }
