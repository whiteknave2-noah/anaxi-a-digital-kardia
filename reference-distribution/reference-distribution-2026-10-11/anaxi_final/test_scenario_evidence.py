"""Fail-closed properties of the scenario-evidence binding layer."""

import json
import subprocess
import sys
from pathlib import Path

import completion_evidence as ce
import generate_current_completion_ledger as gen
import network_guard
import scenario_evidence as se

HERE = Path(__file__).resolve().parent
INVENTORY = se.load_inventory()
BINDINGS = se.load_bindings()
CAPS = {c["id"]: c for c in INVENTORY["capabilities"]}


def test_bindings_are_structurally_valid_against_the_owner_inventory():
    assert se.validate_bindings(INVENTORY, BINDINGS) == []


def test_every_bound_node_resolves_to_a_collected_test():
    """The collected list only VALIDATES bindings; it never defines the owner surface."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=HERE, env=network_guard.guarded_env(__import__("os").environ.copy()),
        capture_output=True, text=True, timeout=600,
    )
    collected = {line.strip() for line in completed.stdout.splitlines() if "::" in line}
    collected_base = {c.split("[")[0] for c in collected}
    missing = [n for n in se.all_bound_nodes(BINDINGS) if n not in collected and n.split("[")[0] not in collected_base]
    assert not missing, f"bound nodes that do not resolve to a real test: {missing}"


def test_unknown_or_extra_scenarios_are_rejected():
    bad = {"02_conversation_direction": {"invented_scenario": {"tests": ["x.py::y"], "rationale": "r"}},
           "99_unknown": {"s": {"tests": ["x.py::y"], "rationale": "r"}}}
    errors = se.validate_bindings(INVENTORY, bad)
    assert any("non-inventory scenario" in e for e in errors)
    assert any("unknown capability" in e for e in errors)


def test_empty_or_rationale_free_bindings_are_rejected():
    bad = {"02_conversation_direction": {"alex_leads": {"tests": [], "rationale": ""}}}
    errors = se.validate_bindings(INVENTORY, bad)
    assert any("needs tests and/or artifacts" in e for e in errors)
    assert any("rationale required" in e for e in errors)


def test_a_single_failed_skipped_or_uncollected_node_fails_the_scenario_with_identity():
    bindings = {"c": {"s": {"tests": ["a.py::one", "a.py::two", "a.py::three"], "rationale": "r"}}}
    for bad_outcome in ("failed", "skipped", "error", "not_collected"):
        results = se.scenario_item_results(bindings, {
            "a.py::one": {"outcome": "passed", "detail": ""},
            "a.py::two": {"outcome": bad_outcome, "detail": "boom"},
            "a.py::three": {"outcome": "passed", "detail": ""},
        })
        record = results["c"]["s"]
        assert record["status"] == ce.FAIL
        assert "a.py::two" in record["reason"] and bad_outcome in record["reason"]
        assert record["evidence"]["tests"]["a.py::two"]["outcome"] == bad_outcome


def test_unbound_inventory_scenarios_stay_not_attempted_and_block_completion(tmp_path):
    report = {"item_results": {}, "node_outcomes": {}}
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    ledger = gen.generate(se.INVENTORY_PATH, scenario_report_path=path)
    for record in ledger["records"]:
        assert record["status"] == "NOT_COMPLETE"
        assert record["passed_total"] == 0
        assert record["skipped_or_unattempted"] == CAPS[record["capability_id"]]["scenarios"]
    assert ledger["whole_system"]["status"] == "NOT_COMPLETE"


def test_offline_bindings_can_never_satisfy_a_live_only_requirement(tmp_path):
    capability = next(c for c in INVENTORY["capabilities"] if c["live_only_requirements"])
    item_results = {
        capability["id"]: {
            scenario: {"status": ce.PASS, "evidence": {"tests": {}, "artifacts": {}}}
            for scenario in capability["scenarios"]
        }
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"item_results": item_results, "node_outcomes": {}}), encoding="utf-8")
    ledger = gen.generate(se.INVENTORY_PATH, scenario_report_path=path)
    record = next(r for r in ledger["records"] if r["capability_id"] == capability["id"])
    assert record["passed_total"] == len(capability["scenarios"])
    assert record["status"] == "NOT_COMPLETE"
    assert record["live_only_passed"] == []
    assert set(capability["live_only_requirements"]) <= set(record["remaining_not_established"])


def test_a_binding_to_a_vanished_test_is_missing_not_passing(tmp_path):
    capability = CAPS["02_conversation_direction"]
    scenario = capability["scenarios"][0]
    item_results = {capability["id"]: {scenario: {
        "status": ce.FAIL, "reason": "a.py::gone: not_collected",
        "evidence": {"tests": {"a.py::gone": {"outcome": "not_collected"}}, "artifacts": {}},
    }}}
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({
        "item_results": item_results, "node_outcomes": {"a.py::gone": {"outcome": "not_collected"}},
    }), encoding="utf-8")
    record = next(r for r in gen.generate(se.INVENTORY_PATH, scenario_report_path=path)["records"]
                  if r["capability_id"] == capability["id"])
    assert scenario in record["missing_from_production"]
    assert any(f["item_or_scenario"] == scenario for f in record["failed_items_or_scenarios"])


def test_artifact_binding_requires_exact_nonempty_complete_records():
    ok = se.artifact_outcome({"path": "completion_evidence/synthetic_public_resources.json",
                              "record_id": "public_resources.music"})
    assert ok["outcome"] == "passed" and ok["passed_total"] == ok["expected_total"] > 0
    absent = se.artifact_outcome({"path": "completion_evidence/synthetic_public_resources.json", "record_id": "nope"})
    assert absent["outcome"] == "failed"
    missing_file = se.artifact_outcome({"path": "completion_evidence/nope.json", "record_id": "x"})
    assert missing_file["outcome"] == "failed"


def test_code_state_never_enumerates_untracked_runtime_trees():
    """Evidence must not name files inside untracked production trees (the live
    Workspace, including Private Space): an untracked directory is one entry."""
    import inspect
    source = inspect.getsource(se._code_state)
    assert '"--untracked-files=normal"' in source and "--untracked-files=all" not in source
    dirty = se._code_state()["code_dirty_outside_evidence"]
    import re
    # The untracked workspace directory is a single entry; nothing BELOW it appears.  Only UNTRACKED
    # entries ("?? ") can name runtime-tree contents: a modified TRACKED source file whose name merely
    # contains "private" (e.g. workspace_private.py) is code, not Private Space, and must not trip this.
    untracked = [entry for entry in dirty if entry.startswith("??")]
    assert not any(re.search(r"workspace/.+", entry) or re.search(r"/private(/|$)", entry)
                   for entry in untracked), len(untracked)
