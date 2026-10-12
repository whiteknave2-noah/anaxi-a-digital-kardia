"""Scenario-specific machine evidence for the owner completion inventory.

A scenario is PASS only when it is explicitly bound (in
``completion_evidence/scenario_bindings.json``) to one or more offline pytest
node ids and *every* bound node id was collected and passed in this run.  A
scenario with no binding is NOT_ATTEMPTED; a binding to a node that does not
exist, was skipped, errored, or failed is a FAIL that keeps the scenario and
node identity.  No prose, aggregate count, or unbound test can pass a scenario.

Bindings never satisfy ``live_only_requirements``; those are recorded only by
genuine live evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import completion_evidence
import network_guard

HERE = Path(__file__).resolve().parent
INVENTORY_PATH = HERE / "owner_completion_inventory.json"
BINDINGS_PATH = HERE / "completion_evidence" / "scenario_bindings.json"


def load_inventory(path: Path = INVENTORY_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_bindings(path: Path = BINDINGS_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["bindings"]


def validate_bindings(inventory: dict, bindings: dict) -> list[str]:
    """Structural errors: unknown capability/scenario, empty test list, duplicate node."""
    errors = []
    scenarios_by_capability = {c["id"]: set(c["scenarios"]) for c in inventory["capabilities"]}
    for capability_id, scenario_map in bindings.items():
        if capability_id not in scenarios_by_capability:
            errors.append(f"binding for unknown capability {capability_id!r}")
            continue
        for scenario, entry in scenario_map.items():
            if scenario not in scenarios_by_capability[capability_id]:
                errors.append(f"{capability_id}: binding for non-inventory scenario {scenario!r}")
            tests = entry.get("tests", [])
            artifacts = entry.get("artifacts", [])
            if not tests and not artifacts:
                errors.append(f"{capability_id}/{scenario}: needs tests and/or artifacts")
            if not all(isinstance(t, str) and "::" in t for t in tests):
                errors.append(f"{capability_id}/{scenario}: tests must be node ids")
            elif len(tests) != len(set(tests)):
                errors.append(f"{capability_id}/{scenario}: duplicate node ids")
            for artifact in artifacts:
                if not {"path", "record_id"} <= set(artifact) and artifact.get("kind") != "eligibility_receipt":
                    errors.append(f"{capability_id}/{scenario}: artifact needs path and record_id")
                if artifact.get("kind") == "eligibility_receipt" and not {"path", "event_id"} <= set(artifact):
                    errors.append(f"{capability_id}/{scenario}: eligibility receipt needs path and event_id")
            if not entry.get("rationale"):
                errors.append(f"{capability_id}/{scenario}: rationale required")
    return errors


def all_bound_nodes(bindings: dict) -> list[str]:
    nodes = []
    for scenario_map in bindings.values():
        for entry in scenario_map.values():
            nodes.extend(entry.get("tests", []))
    return sorted(set(nodes))


def _node_key(classname: str, name: str) -> str:
    """JUnit classname/name -> ``file.py::name`` (or ``file.py::Class::name``)."""
    parts = classname.split(".")
    # module path is the leading dotted parts that form a test file
    for split in range(1, len(parts) + 1):
        module = "/".join(parts[:split]) + ".py"
        if (HERE / module).is_file():
            rest = parts[split:]
            return "::".join([module, *rest, name])
    return f"{classname}::{name}"


def run_nodes(nodes: list[str], extra_args: list[str] | None = None) -> dict[str, dict]:
    """Run node ids once; return {node_id: {"outcome": passed|failed|skipped|error|not_collected, ...}}."""
    results = {node: {"outcome": "not_collected", "detail": "no such test node was collected"} for node in nodes}
    if not nodes:
        return results
    env = dict(os.environ)
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("TRANSFORMERS_OFFLINE", "1")
    env = network_guard.guarded_env(env)
    with tempfile.TemporaryDirectory(prefix="anaxi-scenario-evidence-") as tmp:
        junit = Path(tmp) / "junit.xml"
        command = [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            f"--junitxml={junit}", *(extra_args or []), *nodes,
        ]
        subprocess.run(command, cwd=HERE, env=env, capture_output=True, text=True)
        if not junit.is_file():
            return results
        root = ET.parse(junit).getroot()
        for case in root.iter("testcase"):
            key = _node_key(case.get("classname", ""), case.get("name", ""))
            # parametrized/id suffixes: attribute to the requested base node
            matched = [n for n in nodes if key == n or key.startswith(n + "[")]
            if not matched:
                continue
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            if failure is not None:
                outcome, detail = "failed", (failure.get("message") or "")[:300]
            elif error is not None:
                outcome, detail = "error", (error.get("message") or "")[:300]
            elif skipped is not None:
                outcome, detail = "skipped", (skipped.get("message") or "")[:300]
            else:
                outcome, detail = "passed", ""
            for node in matched:
                previous = results[node]
                # a parametrized node passes only if every case passed
                if previous["outcome"] in {"failed", "error", "skipped"} and outcome == "passed":
                    continue
                if previous["outcome"] == "passed" and outcome != "passed":
                    results[node] = {"outcome": outcome, "detail": detail}
                elif previous["outcome"] == "not_collected":
                    results[node] = {"outcome": outcome, "detail": detail}
    return results


def _file_sha256(node: str) -> str:
    path = HERE / node.split("::", 1)[0]
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "MISSING"


DECISIONS = {"ELIGIBLE", "INELIGIBLE", "NOT_ESTABLISHED"}


def receipt_outcome(artifact: dict) -> dict:
    """A recorded read-only WTR0 assessment of one named H.  Any of the three
    decisions is a valid *assessment*; PASS requires that it was genuinely made:
    right H, closed decision vocabulary, read-only, no text exposed, nothing
    performed, and internally consistent with the decision table."""
    path = HERE / artifact["path"]
    try:
        raw = path.read_bytes()
        doc = json.loads(raw)
        receipt = doc["receipt"]
    except (OSError, ValueError, KeyError) as exc:
        return {"outcome": "failed", "detail": f"receipt unreadable: {type(exc).__name__}"}
    problems = []
    if doc.get("receipt_type") != "wtr0_eligibility_assessment":
        problems.append("wrong receipt type")
    if receipt.get("human_input_event_id") != artifact["event_id"]:
        problems.append("receipt is for a different H")
    if receipt.get("decision") not in DECISIONS:
        problems.append("decision outside the closed vocabulary")
    if not str(doc.get("db_open_mode", "")).startswith("read-only"):
        problems.append("not recorded as read-only")
    if doc.get("h_text_exposed") is not False:
        problems.append("H text exposure not ruled out")
    if any(doc.get(k) is not False for k in ("generation_or_reset_performed", "recovery_reserved")):
        problems.append("assessment performed a consequential act")
    decision, basis = receipt.get("decision"), receipt.get("basis")
    if decision == "INELIGIBLE" and basis == "FAILURE_NOT_RETRY_SAFE" and not (
            receipt.get("failure_evidence_id") and receipt.get("canonical_x_event_id") is None):
        problems.append("INELIGIBLE/FAILURE_NOT_RETRY_SAFE without failure evidence or with a linked X")
    if decision == "ELIGIBLE" and not receipt.get("failure_evidence_id"):
        problems.append("ELIGIBLE without qualifying failure evidence")
    if decision == "NOT_ESTABLISHED" and basis != "NO_FAILURE_EVIDENCE":
        problems.append("NOT_ESTABLISHED with an unexpected basis")
    return {
        "outcome": "failed" if problems else "passed", "detail": "; ".join(problems),
        "artifact_sha256": hashlib.sha256(raw).hexdigest(), "decision": decision, "basis": basis,
        "failure_class": receipt.get("failure_class"),
    }


def artifact_outcome(artifact: dict) -> dict:
    if artifact.get("kind") == "eligibility_receipt":
        return receipt_outcome(artifact)
    """A COMPLETE record in a checked-in machine artifact, exact N == M, non-empty."""
    path = HERE / artifact["path"]
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
        record = next(r for r in document["records"] if r["capability_id"] == artifact["record_id"])
    except (OSError, ValueError, StopIteration, KeyError) as exc:
        return {"outcome": "failed", "detail": f"artifact unreadable or record absent: {type(exc).__name__}"}
    sha = hashlib.sha256(raw).hexdigest()
    exact = (
        record.get("status") == "COMPLETE"
        and record.get("authoritative_expected_total", 0) > 0
        and record.get("authoritative_expected_total") == record.get("passed_total") == record.get("attempted_total")
        and not record.get("failed_total") and not record.get("missing_from_production")
    )
    return {
        "outcome": "passed" if exact else "failed",
        "detail": "" if exact else f"record status {record.get('status')!r} not exact-complete",
        "artifact_sha256": sha, "passed_total": record.get("passed_total"),
        "expected_total": record.get("authoritative_expected_total"),
    }


def scenario_item_results(bindings: dict, node_results: dict[str, dict]) -> dict[str, dict]:
    """{capability_id: {scenario: {status, reason?, evidence}}} for bound scenarios only."""
    out: dict[str, dict] = {}
    for capability_id, scenario_map in bindings.items():
        for scenario, entry in scenario_map.items():
            nodes = entry.get("tests", [])
            outcomes = {n: node_results.get(n, {"outcome": "not_collected", "detail": "not run"}) for n in nodes}
            artifact_outcomes = {
                f"{a['path']}#{a.get('record_id') or a.get('event_id')}": artifact_outcome(a)
                for a in entry.get("artifacts", [])
            }
            bad = {n: o for n, o in {**outcomes, **artifact_outcomes}.items() if o["outcome"] != "passed"}
            evidence = {
                "route": entry.get("route"),
                "rationale": entry["rationale"],
                "tests": {n: {"outcome": o["outcome"], "file_sha256": _file_sha256(n)} for n, o in outcomes.items()},
                "artifacts": {
                    k: {key: v for key, v in o.items() if key != "detail"} for k, o in artifact_outcomes.items()
                },
            }
            if bad:
                reason = "; ".join(f"{n}: {o['outcome']} {o['detail']}".strip() for n, o in sorted(bad.items()))
                out.setdefault(capability_id, {})[scenario] = {
                    "status": completion_evidence.FAIL, "reason": reason, "evidence": evidence,
                }
            else:
                out.setdefault(capability_id, {})[scenario] = {
                    "status": completion_evidence.PASS, "evidence": evidence,
                }
    return out


def _code_state() -> dict:
    """Exact code identity the evidence was produced against.

    Evidence files (completion_evidence/) and the continuation artifact may differ
    from HEAD without invalidating the run; any other tracked or untracked change
    is recorded so the report cannot silently describe uncommitted code."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True).stdout
    head = git("rev-parse", "HEAD").strip()
    dirty = []
    # "normal", never "all": an untracked DIRECTORY is reported as one entry, so
    # production runtime trees (the live Workspace, including Private Space) are
    # never enumerated into evidence files, transcripts or logs.
    for line in git("status", "--porcelain", "--untracked-files=normal").splitlines():
        path = line[3:].strip().strip('"')
        if "completion_evidence/" in path or path.endswith("WHOLE_SYSTEM_CONTINUATION.md"):
            continue
        dirty.append(line.strip())
    return {"code_head": head, "code_dirty_outside_evidence": dirty}


def build_report(inventory_path: Path = INVENTORY_PATH, bindings_path: Path = BINDINGS_PATH) -> dict:
    inventory = load_inventory(inventory_path)
    bindings = load_bindings(bindings_path)
    errors = validate_bindings(inventory, bindings)
    if errors:
        raise ValueError("invalid scenario bindings:\n" + "\n".join(errors))
    node_results = run_nodes(all_bound_nodes(bindings))
    return {
        "report": "scenario_evidence_v1",
        **_code_state(),
        "inventory_version": inventory["inventory_version"],
        "bindings_sha256": hashlib.sha256(bindings_path.read_bytes()).hexdigest(),
        "node_outcomes": node_results,
        "item_results": scenario_item_results(bindings, node_results),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failed = sum(1 for caps in report["item_results"].values() for r in caps.values() if r["status"] != "PASS")
    total = sum(len(caps) for caps in report["item_results"].values())
    print(f"scenario bindings: {total - failed}/{total} pass")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
