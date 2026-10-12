"""Live-only requirements are satisfied only by genuine live records (never by offline bindings), and a
record that names the wrong requirement or cites no canonical evidence is rejected, never counted."""
import json
from pathlib import Path

import pytest

import generate_current_completion_ledger as g

HERE = Path(__file__).resolve().parent
INV = json.loads((HERE / "owner_completion_inventory.json").read_text(encoding="utf-8"))


def _write(tmp_path, records):
    p = tmp_path / "live.json"
    p.write_text(json.dumps({"records": records}), encoding="utf-8")
    return p


def test_the_checked_in_live_records_are_valid_and_counted():
    passed = g.load_live_evidence([HERE / "completion_evidence" / "live_acceptance_2026-09-24.json"], INV)
    assert "live_vault_checkout" in passed["34_collaborative_obsidian_workspace"]
    assert "live_second_correspondent_checkout" in passed["32_external_correspondence_scoped_continuity"]


@pytest.mark.parametrize("record", [
    {"capability_id": "34_collaborative_obsidian_workspace", "requirement": "live_something_else", "canonical_event_ids": ["E"], "checks": ["c"], "result": "established"},
    {"capability_id": "07_journal", "requirement": "live_vault_checkout", "canonical_event_ids": ["E"], "checks": ["c"], "result": "established"},
    {"capability_id": "34_collaborative_obsidian_workspace", "requirement": "live_vault_checkout", "canonical_event_ids": [], "checks": ["c"], "result": "established"},
    {"capability_id": "34_collaborative_obsidian_workspace", "requirement": "live_vault_checkout", "canonical_event_ids": ["E"], "checks": [], "result": "established"},
], ids=["unknown_requirement", "wrong_capability", "no_events", "no_checks"])
def test_a_record_without_genuine_standing_is_rejected(tmp_path, record):
    with pytest.raises(ValueError):
        g.load_live_evidence([_write(tmp_path, [record])], INV)


def test_without_live_records_no_live_requirement_is_satisfied():
    assert g.load_live_evidence(None, INV) == {}


def test_an_owner_disposition_settles_a_requirement_but_is_never_live_evidence(tmp_path):
    import completion_evidence
    path = _write(tmp_path, [
        {"capability_id": "15_recovery_wtr0", "requirement": "live_safe_recovery_checkout_where_lawful",
         "canonical_event_ids": [], "checks": ["no lawful live occasion"], "owner_decision": "accepted offline",
         "result": g.DISPOSITION_OWNER_ACCEPTED_OFFLINE}])
    assert g.load_live_evidence([path], INV) == {}                          # never live evidence
    assert g.load_owner_dispositions([path], INV) == {
        "15_recovery_wtr0": {"live_safe_recovery_checkout_where_lawful": g.DISPOSITION_OWNER_ACCEPTED_OFFLINE}}
    record = completion_evidence.build_completion_record(
        capability_id="x", authoritative_items=["a"], production_discovered_items=["a"],
        item_results={"a": {"status": "PASS"}}, live_only_requirements=["r"], live_only_passed=[],
        live_only_dispositions={"r": g.DISPOSITION_OWNER_ACCEPTED_OFFLINE})
    assert record["status"] == "COMPLETE" and record["live_only_passed"] == []
    assert record["live_only_owner_dispositions"] == {"r": g.DISPOSITION_OWNER_ACCEPTED_OFFLINE}


def test_a_disposition_without_an_owner_decision_is_rejected(tmp_path):
    path = _write(tmp_path, [
        {"capability_id": "15_recovery_wtr0", "requirement": "live_safe_recovery_checkout_where_lawful",
         "canonical_event_ids": [], "checks": ["c"], "result": g.DISPOSITION_OPTIONAL_USE_NOT_OBSERVED}])
    with pytest.raises(ValueError):
        g.load_owner_dispositions([path], INV)


def test_a_withdrawn_capability_stays_failed_historically_and_leaves_only_the_release_contract(tmp_path):
    records = [{"capability_id": "10_read_only_external_information", "status": "COMPLETE"},
               {"capability_id": "27_boundary_inspection", "status": "NOT_COMPLETE"}]
    path = tmp_path / "rc.json"
    path.write_text(json.dumps({"withdrawn": [{"capability_id": "27_boundary_inspection",
                                               "historical_status": "NOT_ESTABLISHED", "owner_decision": "withdrawn",
                                               "failure_evidence": ["x.json"]}]}), encoding="utf-8")
    contract = g.build_release_contract(records, g.load_release_contract(path, INV))
    assert contract["status"] == "COMPLETE" and contract["capability_record_total"] == 1
    assert contract["withdrawn_from_release"][0]["capability_id"] == "27_boundary_inspection"
    assert records[1]["status"] == "NOT_COMPLETE"                      # the historical record is untouched
    path.write_text(json.dumps({"withdrawn": [{"capability_id": "27_boundary_inspection"}]}), encoding="utf-8")
    with pytest.raises(ValueError):                                    # no owner decision / evidence: refused
        g.load_release_contract(path, INV)


def test_the_checked_in_release_contract_withdraws_only_boundary_inspection():
    withdrawn = g.load_release_contract(HERE / "completion_evidence" / "release_contract_2026-09-24.json", INV)
    assert set(withdrawn) == {"27_boundary_inspection"}
