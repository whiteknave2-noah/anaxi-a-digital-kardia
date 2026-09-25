"""Fail-closed machine evidence for owner-defined completion sets.

The authoritative denominator is supplied by the owner inventory, never
derived from implementation discovery or test collection.  This module does
not decide what ANAXI requires; it only makes it mechanically impossible to
turn an empty, missing, sampled, failed, skipped, or live-unchecked set into a
passing record.
"""

from __future__ import annotations

from typing import Iterable, Mapping


PASS = "PASS"
FAIL = "FAIL"
NOT_ATTEMPTED = "NOT_ATTEMPTED"
VALID_RESULTS = {PASS, FAIL, NOT_ATTEMPTED}


def _unique(values: Iterable[str], field: str) -> list[str]:
    result = list(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{field} must contain non-empty strings")
    if len(result) != len(set(result)):
        raise ValueError(f"{field} contains duplicate identities")
    return result


def build_completion_record(
    *, capability_id: str, authoritative_items: Iterable[str] | None,
    production_discovered_items: Iterable[str],
    item_results: Mapping[str, Mapping[str, object]],
    live_only_requirements: Iterable[str] = (),
    live_only_passed: Iterable[str] = (),
    nonempty_expected: bool = True,
    live_only_dispositions: Mapping[str, str] | None = None,
) -> dict:
    """Build one enumerable capability record under the evidence law.

    ``item_results`` keys are item/scenario identities and each value must
    carry ``status`` (PASS/FAIL/NOT_ATTEMPTED).  FAIL requires a non-empty
    ``reason``.  Optional ``evidence`` is preserved verbatim.
    """
    if not isinstance(capability_id, str) or not capability_id:
        raise ValueError("capability_id must be a non-empty string")
    expected_established = authoritative_items is not None
    expected = _unique(authoritative_items or (), "authoritative_items")
    discovered = _unique(production_discovered_items, "production_discovered_items")
    live_required = _unique(live_only_requirements, "live_only_requirements")
    live_passed = _unique(live_only_passed, "live_only_passed")

    expected_set = set(expected)
    discovered_set = set(discovered)
    live_required_set = set(live_required)
    unknown_live_passes = sorted(set(live_passed) - live_required_set)
    if unknown_live_passes:
        raise ValueError(f"live_only_passed contains unknown requirements: {unknown_live_passes}")
    # An owner disposition settles a live-only requirement WITHOUT being live evidence (e.g. no lawful
    # live occasion existed; an optional subject-originated use was not observed).  It is reported
    # separately from live_only_passed, never merged into it.
    dispositions = dict(live_only_dispositions or {})
    unknown_dispositions = sorted(set(dispositions) - live_required_set)
    if unknown_dispositions:
        raise ValueError(f"live_only_dispositions contains unknown requirements: {unknown_dispositions}")
    if set(dispositions) & set(live_passed):
        raise ValueError("a live-only requirement cannot be both live-established and an owner disposition")

    normalized_results: dict[str, dict] = {}
    for item_id, raw in item_results.items():
        if item_id not in expected_set:
            raise ValueError(f"result supplied for non-authoritative item {item_id!r}")
        status = raw.get("status")
        if status not in VALID_RESULTS:
            raise ValueError(f"invalid status for {item_id!r}: {status!r}")
        reason = raw.get("reason")
        if status == FAIL and (not isinstance(reason, str) or not reason):
            raise ValueError(f"failed item {item_id!r} requires a reason")
        normalized_results[item_id] = {
            "status": status,
            "reason": reason,
            "evidence": raw.get("evidence"),
        }

    missing = sorted(expected_set - discovered_set)
    unexpected = sorted(discovered_set - expected_set)
    attempted = [
        item_id for item_id in expected
        if normalized_results.get(item_id, {}).get("status") in {PASS, FAIL}
    ]
    passed = [
        item_id for item_id in expected
        if normalized_results.get(item_id, {}).get("status") == PASS
    ]
    failed = [
        {
            "item_or_scenario": item_id,
            "reason": normalized_results[item_id]["reason"],
            "evidence": normalized_results[item_id]["evidence"],
        }
        for item_id in expected
        if normalized_results.get(item_id, {}).get("status") == FAIL
    ]
    skipped = [
        item_id for item_id in expected
        if normalized_results.get(item_id, {}).get("status") not in {PASS, FAIL}
    ]
    live_remaining = sorted(live_required_set - set(live_passed) - set(dispositions))
    remaining = sorted(set(missing + skipped + [entry["item_or_scenario"] for entry in failed] + live_remaining))

    empty_failure = nonempty_expected and (not expected_established or not expected)
    complete = (
        not empty_failure
        and not missing
        and not unexpected
        and len(attempted) == len(expected)
        and len(passed) == len(expected)
        and not failed
        and not skipped
        and not live_remaining
    )
    if empty_failure:
        status = "AUTHORITATIVE_REQUIRED_SET_EMPTY_OR_NOT_ESTABLISHED"
    else:
        status = "COMPLETE" if complete else "NOT_COMPLETE"

    return {
        "capability_id": capability_id,
        "status": status,
        "authoritative_required_set_established": expected_established and bool(expected),
        "authoritative_expected_total": len(expected),
        "production_discovered_total": len(discovered),
        "attempted_total": len(attempted),
        "passed_total": len(passed),
        "failed_total": len(failed),
        "missing_from_production": missing,
        "unexpected_in_production": unexpected,
        "skipped_or_unattempted": skipped,
        "failed_items_or_scenarios": failed,
        "live_only_requirements": live_required,
        "live_only_passed": live_passed,
        "live_only_owner_dispositions": dict(sorted(dispositions.items())),
        "remaining_not_established": remaining,
        "item_results": normalized_results,
    }


def build_whole_system_record(records: Iterable[Mapping[str, object]]) -> dict:
    records = list(records)
    if not records:
        return {
            "status": "AUTHORITATIVE_REQUIRED_SET_EMPTY_OR_NOT_ESTABLISHED",
            "capability_record_total": 0,
            "passing_capability_total": 0,
            "nonpassing_capabilities": [],
        }
    nonpassing = [
        record.get("capability_id") for record in records
        if record.get("status") != "COMPLETE"
    ]
    return {
        "status": "COMPLETE" if not nonpassing else "NOT_COMPLETE",
        "capability_record_total": len(records),
        "passing_capability_total": len(records) - len(nonpassing),
        "nonpassing_capabilities": nonpassing,
    }
