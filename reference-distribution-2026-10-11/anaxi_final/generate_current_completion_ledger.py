"""Generate the conservative whole-system ledger from owner inventory.

Scenario claims remain NOT_ATTEMPTED until a checked-in evidence binding names
them and the bound tests run green (scenario_evidence.py).  Resource collection records may be imported from the exhaustive assay.
This deliberately prefers an understated ledger to a prose-based upgrade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import completion_evidence


LIVE_ESTABLISHED = "established"
# Owner dispositions (2026-09-24 owner decisions): they settle a live-only requirement for completion but
# are never live evidence, and the ledger reports them apart from live_only_passed.
DISPOSITION_OWNER_ACCEPTED_OFFLINE = "owner_accepted_offline_no_live_occasion"
DISPOSITION_OPTIONAL_USE_NOT_OBSERVED = "optional_use_not_observed"
# A capability used live and then repaired, whose post-repair live invocation was not observed (Clark chose
# not to invoke it again); accepted by the owner, never labelled live-established.
DISPOSITION_OWNER_ACCEPTED_INVOCATION_NOT_OBSERVED = "owner_accepted_post_repair_invocation_not_observed"
OWNER_DISPOSITIONS = frozenset({DISPOSITION_OWNER_ACCEPTED_OFFLINE, DISPOSITION_OPTIONAL_USE_NOT_OBSERVED,
                                DISPOSITION_OWNER_ACCEPTED_INVOCATION_NOT_OBSERVED})


def load_release_contract(path, inventory) -> dict:
    """{capability_id: withdrawal record} for capabilities the owner withdrew from the current production
    release contract.  Each must name a real inventory capability and cite the owner decision and its
    failure evidence; the historical record is never altered by this."""
    if path is None:
        return {}
    known = {c["id"] for c in inventory["capabilities"]}
    withdrawn = {}
    for entry in json.loads(Path(path).read_text(encoding="utf-8"))["withdrawn"]:
        cap = entry["capability_id"]
        if cap not in known or not entry.get("owner_decision") or not entry.get("failure_evidence"):
            raise ValueError(f"release-contract withdrawal {cap!r} is not a documented owner decision")
        withdrawn[cap] = entry
    return withdrawn


def build_release_contract(records, withdrawn) -> dict:
    """The current production release contract: every capability record except owner-withdrawn ones."""
    in_release = [r for r in records if r["capability_id"] not in withdrawn]
    nonpassing = sorted(r["capability_id"] for r in in_release if r["status"] != "COMPLETE")
    return {
        "status": "COMPLETE" if in_release and not nonpassing else "NOT_COMPLETE",
        "capability_record_total": len(in_release),
        "passing_capability_total": len(in_release) - len(nonpassing),
        "nonpassing_capabilities": nonpassing,
        "withdrawn_from_release": [
            {"capability_id": cap, "historical_status": entry["historical_status"],
             "owner_decision": entry["owner_decision"], "failure_evidence": entry["failure_evidence"]}
            for cap, entry in sorted(withdrawn.items())],
    }


def _live_records(paths, inventory):
    allowed = {c["id"]: set(c["live_only_requirements"]) for c in inventory["capabilities"]}
    for path in paths or ():
        for record in json.loads(Path(path).read_text(encoding="utf-8"))["records"]:
            cap, req = record["capability_id"], record["requirement"]
            if req not in allowed.get(cap, set()):
                raise ValueError(f"live record names {req!r}, not a live-only requirement of {cap!r}")
            if record.get("result") in OWNER_DISPOSITIONS:
                if not record.get("checks") or not record.get("owner_decision"):
                    raise ValueError(f"owner disposition {cap}/{req} cites no owner decision")
            elif not record.get("checks") or not (record.get("canonical_event_ids") or cap.startswith("22_")):
                raise ValueError(f"live record {cap}/{req} cites no canonical evidence")
            yield cap, req, record


def load_live_evidence(paths, inventory) -> dict:
    """capability id -> set of live-only requirements established by genuine live records.  A record that
    names an unknown requirement, the wrong capability, or cites no canonical event and no observed launcher
    fact is rejected (ValueError) -- never silently counted.  Owner dispositions are never counted here."""
    passed = {}
    for cap, req, record in _live_records(paths, inventory):
        if record.get("result") == LIVE_ESTABLISHED:
            passed.setdefault(cap, set()).add(req)
    return passed


def load_owner_dispositions(paths, inventory) -> dict:
    """capability id -> {requirement: disposition} for requirements settled by a recorded owner decision
    rather than live evidence.  A requirement that is also live-established keeps the live result."""
    passed = load_live_evidence(paths, inventory)
    dispositions = {}
    for cap, req, record in _live_records(paths, inventory):
        if record.get("result") in OWNER_DISPOSITIONS and req not in passed.get(cap, set()):
            dispositions.setdefault(cap, {})[req] = record["result"]
    return dispositions


def generate(
    inventory_path: Path,
    resource_report_path: Path | None = None,
    regression_report_path: Path | None = None,
    scenario_report_path: Path | None = None,
    live_evidence_paths=None,
    release_contract_path: Path | None = None,
) -> dict:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    live_passed = load_live_evidence(live_evidence_paths, inventory)
    live_dispositions = load_owner_dispositions(live_evidence_paths, inventory)
    scenario_results: dict = {}
    scenario_node_outcomes: dict = {}
    if scenario_report_path is not None:
        scenario_report = json.loads(scenario_report_path.read_text(encoding="utf-8"))
        scenario_results = scenario_report["item_results"]
        scenario_node_outcomes = scenario_report["node_outcomes"]
    records = []
    for capability in inventory["capabilities"]:
        results = scenario_results.get(capability["id"], {})
        # A scenario is "discovered in production" only when every bound test
        # node was actually collected; a binding to a vanished test is missing.
        discovered = [
            scenario for scenario, result in results.items()
            if all(
                scenario_node_outcomes.get(node, {}).get("outcome") != "not_collected"
                for node in result["evidence"]["tests"]
            )
        ]
        records.append(completion_evidence.build_completion_record(
            capability_id=capability["id"],
            authoritative_items=capability["scenarios"],
            production_discovered_items=discovered, item_results=results,
            live_only_requirements=capability["live_only_requirements"],
            live_only_passed=sorted(live_passed.get(capability["id"], ())), nonempty_expected=True,
            live_only_dispositions=live_dispositions.get(capability["id"], {}),
        ))
    if resource_report_path is not None:
        resource_report = json.loads(resource_report_path.read_text(encoding="utf-8"))
        artifact_sha256 = hashlib.sha256(resource_report_path.read_bytes()).hexdigest()
        for detailed_record in resource_report["records"]:
            summary = {
                key: value for key, value in detailed_record.items()
                if key != "item_results"
            }
            summary["detailed_evidence_artifact"] = str(resource_report_path)
            summary["detailed_evidence_sha256"] = artifact_sha256
            records.append(summary)
    ledger = {
        "ledger_version": 1,
        "inventory_version": inventory["inventory_version"],
        "owner_directive_sha256": inventory["owner_directive_sha256"],
        "evidence_law_addendum_sha256": inventory["evidence_law_addendum_sha256"],
        "records": records,
        # The historical 2026-09-24 acceptance campaign over the whole owner inventory (unchanged; a
        # withdrawn capability keeps its failed status here).
        "whole_system": completion_evidence.build_whole_system_record(records),
        # The current production release contract: the same records minus owner-withdrawn capabilities.
        "release_contract": build_release_contract(records, load_release_contract(release_contract_path, inventory)),
        "notice": (
            "Conservative ledger: an owner scenario is PASS only where a checked-in scenario "
            "binding (completion_evidence/scenario_bindings.json) ran green in this generation; "
            "prose, aggregate counts, and unbound tests never mark a scenario PASS, and offline "
            "bindings never satisfy live_only_requirements. A recorded owner disposition (no lawful live "
            "occasion accepted offline; optional use not observed) settles a live-only requirement but is "
            "reported under live_only_owner_dispositions, never as live evidence."
        ),
    }
    if scenario_report_path is not None:
        ledger["scenario_evidence"] = {
            "artifact": str(scenario_report_path),
            "artifact_sha256": hashlib.sha256(scenario_report_path.read_bytes()).hexdigest(),
            "code_head": scenario_report.get("code_head"),
            "code_dirty_outside_evidence": scenario_report.get("code_dirty_outside_evidence"),
            "bindings_sha256": scenario_report.get("bindings_sha256"),
        }
    blocked_path = inventory_path.parent / "completion_evidence" / "blocked_items.json"
    if blocked_path.is_file():
        ledger["blocked_items"] = json.loads(blocked_path.read_text(encoding="utf-8"))["items"]
    if regression_report_path is not None:
        regression_report = json.loads(regression_report_path.read_text(encoding="utf-8"))
        ledger["checkpoint_evidence"] = {
            "offline_regression": regression_report,
            "artifact": str(regression_report_path),
            "artifact_sha256": hashlib.sha256(regression_report_path.read_bytes()).hexdigest(),
            "scenario_status_effect": (
                "Recorded as checkpoint evidence only; it does not upgrade owner scenarios "
                "without scenario-specific evidence bindings or satisfy live-only requirements."
            ),
        }
    return ledger


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--resource-report", type=Path)
    parser.add_argument("--regression-report", type=Path)
    parser.add_argument("--scenario-report", type=Path)
    parser.add_argument("--live-evidence", type=Path, action="append")
    parser.add_argument("--release-contract", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    ledger = generate(
        args.inventory, args.resource_report, args.regression_report, args.scenario_report,
        live_evidence_paths=args.live_evidence, release_contract_path=args.release_contract,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if ledger["whole_system"]["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
