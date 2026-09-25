import pytest

from claim_harness.inventory import AccountingError, parse_inventory
from helpers import claim, inventory


def problems(data):
    with pytest.raises(AccountingError) as info:
        parse_inventory(data)
    return info.value.problems


def test_valid_inventory_with_every_evidence_kind():
    inv = parse_inventory(inventory(
        claim("A"),
        claim("B", "live_only", live_record={"observed": "2026-01-01", "summary": "seen once"}),
        claim("C", "disposition", disposition={"decided_by": "owner", "decision": "accepted",
                                               "rationale": "because"}),
        claim("D", withdrawn={"reason": "superseded"}),
        not_attempted_fails_run=True))
    assert [c.id for c in inv.claims] == ["A", "B", "C", "D"]
    assert inv.not_attempted_fails_run is True
    assert inv.get("D").withdrawn == {"reason": "superseded"}


def test_schema_must_match():
    data = inventory(claim("A"))
    data["schema"] = "something/else"
    assert any("'schema'" in p for p in problems(data))


def test_duplicate_claim_ids_rejected():
    assert any("duplicate claim id" in p for p in problems(inventory(claim("A"), claim("A"))))


def test_every_problem_is_reported():
    found = problems(inventory(
        {"id": "X", "evidence": "executable"},
        claim("Y", "vibes"),
        claim("Z", "live_only"),
        claim("W", "disposition", disposition={"decided_by": "owner"}),
        claim("V", withdrawn={}),
        claim("U", live_record={"observed": "x", "summary": "y"}),
        claim("T", colour="blue")))
    joined = "\n".join(found)
    for expected in ("claim X: 'statement'", "claim Y: 'evidence' must be one of",
                     "claim Z: 'live_record' must be an object", "'disposition.decision'",
                     "'withdrawn.reason'", "claim U: 'live_record' is only valid", "claim T: unknown key 'colour'"):
        assert expected in joined


def test_claim_id_without_whitespace():
    assert any("'id'" in p for p in problems(inventory(claim("has space"))))


def test_empty_claims_rejected():
    assert any("'claims'" in p for p in problems(inventory()))
