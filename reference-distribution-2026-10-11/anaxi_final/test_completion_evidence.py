import pytest

import completion_evidence as evidence


def _pass(item):
    return {"status": evidence.PASS, "evidence": f"receipt:{item}"}


def test_zero_over_zero_fails_closed_when_nonempty_is_expected():
    record = evidence.build_completion_record(
        capability_id="books", authoritative_items=[],
        production_discovered_items=[], item_results={}, nonempty_expected=True,
    )
    assert record["status"] == "AUTHORITATIVE_REQUIRED_SET_EMPTY_OR_NOT_ESTABLISHED"
    assert record["authoritative_required_set_established"] is False


def test_unavailable_authoritative_set_fails_closed():
    record = evidence.build_completion_record(
        capability_id="photos", authoritative_items=None,
        production_discovered_items=["implementation-only.jpg"], item_results={},
    )
    assert record["status"] == "AUTHORITATIVE_REQUIRED_SET_EMPTY_OR_NOT_ESTABLISHED"
    assert record["unexpected_in_production"] == ["implementation-only.jpg"]


def test_silent_subset_cannot_pass_even_when_every_discovered_item_passes():
    record = evidence.build_completion_record(
        capability_id="music", authoritative_items=["a", "b", "c"],
        production_discovered_items=["a", "b"],
        item_results={"a": _pass("a"), "b": _pass("b")},
    )
    assert record["status"] == "NOT_COMPLETE"
    assert record["missing_from_production"] == ["c"]
    assert record["skipped_or_unattempted"] == ["c"]
    assert record["passed_total"] == 2


def test_failure_requires_reason_and_is_exposed():
    with pytest.raises(ValueError, match="requires a reason"):
        evidence.build_completion_record(
            capability_id="direction", authoritative_items=["invalid_rejected"],
            production_discovered_items=["invalid_rejected"],
            item_results={"invalid_rejected": {"status": evidence.FAIL}},
        )
    record = evidence.build_completion_record(
        capability_id="direction", authoritative_items=["invalid_rejected"],
        production_discovered_items=["invalid_rejected"],
        item_results={"invalid_rejected": {"status": evidence.FAIL, "reason": "accepted"}},
    )
    assert record["failed_total"] == 1
    assert record["failed_items_or_scenarios"][0]["reason"] == "accepted"


def test_offline_pass_cannot_upgrade_missing_live_requirement():
    record = evidence.build_completion_record(
        capability_id="sleep", authoritative_items=["atomic_cycle"],
        production_discovered_items=["atomic_cycle"],
        item_results={"atomic_cycle": _pass("atomic_cycle")},
        live_only_requirements=["genuine_subject_chosen_cycle"],
    )
    assert record["passed_total"] == 1
    assert record["status"] == "NOT_COMPLETE"
    assert record["remaining_not_established"] == ["genuine_subject_chosen_cycle"]


def test_complete_requires_exact_sets_all_passed_and_all_live_passed():
    record = evidence.build_completion_record(
        capability_id="web", authoritative_items=["search", "fetch"],
        production_discovered_items=["fetch", "search"],
        item_results={"search": _pass("search"), "fetch": _pass("fetch")},
        live_only_requirements=["ordinary_live_search"],
        live_only_passed=["ordinary_live_search"],
    )
    assert record["status"] == "COMPLETE"
    assert record["attempted_total"] == record["passed_total"] == 2


def test_whole_system_is_not_complete_when_any_capability_is_not_complete():
    whole = evidence.build_whole_system_record([
        {"capability_id": "a", "status": "COMPLETE"},
        {"capability_id": "b", "status": "NOT_COMPLETE"},
    ])
    assert whole == {
        "status": "NOT_COMPLETE", "capability_record_total": 2,
        "passing_capability_total": 1, "nonpassing_capabilities": ["b"],
    }
