import json
from pathlib import Path


def test_owner_inventory_holds_the_25_mandatory_surfaces_plus_represented_surface_additions():
    """The 25 owner-mandated surfaces are never removed or renumbered. Any
    further capability the production architecture actually represents is an
    ADDITION (26+, carrying its ``source``) -- the denominator can grow to match
    production, never shrink below the owner's directive."""
    path = Path(__file__).with_name("owner_completion_inventory.json")
    inventory = json.loads(path.read_text(encoding="utf-8"))
    capabilities = inventory["capabilities"]
    assert len(capabilities) >= 25
    expected_prefixes = [f"{index:02d}_" for index in range(1, len(capabilities) + 1)]
    assert [entry["id"][:3] for entry in capabilities] == expected_prefixes
    assert len({entry["id"] for entry in capabilities}) == len(capabilities)
    for entry in capabilities[25:]:
        assert entry.get("source"), f"{entry['id']}: an addition must state the production surface it represents"
    for entry in capabilities:
        assert entry["scenarios"]
        assert len(entry["scenarios"]) == len(set(entry["scenarios"]))
        assert isinstance(entry["live_only_requirements"], list)
        assert len(entry["live_only_requirements"]) == len(set(entry["live_only_requirements"]))


def test_inventory_is_bound_to_the_owner_directive_and_evidence_law_hashes():
    inventory = json.loads(
        Path(__file__).with_name("owner_completion_inventory.json").read_text(encoding="utf-8")
    )
    assert inventory["owner_directive_sha256"] == (
        "9db995ba5ac28710739cc344e209cfaf1ebc592b85e3e817962733302a213318"
    )
    assert inventory["evidence_law_addendum_sha256"] == (
        "c2ace753b115bbe4b938ec620e10e5552c9039137150d70d6fcb040d1c88eea1"
    )
