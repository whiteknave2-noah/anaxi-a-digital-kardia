"""BOUNDARY INSPECTOR v1 -- rationale-registry drift-lock suite.

Pure host-metadata checks: the PRIVATE_SPACE_PUBLIC_RULE literal stays
byte-identical to the REAL public literal in workspace_private.py, the
frozen event-model constants stay put, "not recorded" is used exactly
for the frozen WTR0/SLP2 ceilings, the closed boundary_id vocabulary is
in lockstep between the registry and conversation_direction, and the
schema-migration registration is idempotent and matches the registry's
own constants.

No inference, no Ollama, no network, no live/Private Space access.
workspace_private.py is imported HERE (a test file) to read its PUBLIC
literal -- the engine itself never imports it.

Capability lockstep checks exercise the REAL workspace_capability
descriptors; as in test_boundary_inspector.py, the pure third-party
import-time names are stubbed so the real substrate can import.

Run from repository root:
    python3 -B anaxi_final/test_boundary_rationale_registry.py
(also pytest-compatible: bare def test_*() functions).
"""
import os
import importlib
import sqlite3
import sys
import tempfile
import traceback
import types

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)


def _install_capability_substrate_stubs():
    for name in ("pypdf", "numpy", "soundfile", "PIL.Image", "scipy.fft", "scipy.signal"):
        try:
            importlib.import_module(name)
        except ImportError:
            sys.modules.setdefault(name, types.ModuleType(name))
    pil = sys.modules.setdefault("PIL", types.ModuleType("PIL"))
    pil.Image = sys.modules["PIL.Image"]
    scipy = sys.modules.setdefault("scipy", types.ModuleType("scipy"))
    scipy.fft = sys.modules["scipy.fft"]
    scipy_signal = sys.modules["scipy.signal"]
    if not hasattr(scipy_signal, "find_peaks"):
        scipy_signal.find_peaks = lambda *a, **k: ((), {})
    scipy.signal = scipy_signal


_install_capability_substrate_stubs()

import workspace_capability
import workspace_private
import boundary_rationale_registry as brr
import boundary_inspector as bi
import boundary_inspector_schema_migration as bsm
import conversation_direction as cd
import provenance_schema

NOT_RECORDED = "not recorded"
TEST_DIR = tempfile.mkdtemp(prefix="boundary_registry_test_")


def test_public_rule_literal_is_byte_identical_to_real_public_literal():
    assert brr.PRIVATE_SPACE_PUBLIC_RULE == workspace_private.PRIVATE_SYSTEM_CONTENT
    assert brr.PRIVATE_SPACE_PUBLIC_RULE_SOURCE_MODULE == "workspace_private"
    assert brr.PRIVATE_SPACE_PUBLIC_RULE_SOURCE_ATTRIBUTE == "PRIVATE_SYSTEM_CONTENT"


def test_event_model_constants_are_frozen():
    assert brr.BOUNDARY_INSPECTION_EVENT_TYPE == "clark_boundary_query"
    assert brr.BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND == "boundary_inspection_result"
    assert brr.BOUNDARY_INSPECTION_DELIVERED_COMPONENT_KIND == "boundary_inspection_delivered"
    assert brr.BOUNDARY_INSPECTION_QUERY_SPEC_COMPONENT_KIND == "boundary_query_spec"
    assert brr.BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND == "boundary_inspection_result_carriage"
    assert brr.QUERY_EVENT_COMPONENT_KINDS == (
        "boundary_query_spec", "boundary_inspection_result", "boundary_inspection_delivered"
    )
    # The carriage kind is written on a waking_turn event, never on a
    # clark_boundary_query event, so it must NOT join the query-event tuple.
    assert brr.BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND not in brr.QUERY_EVENT_COMPONENT_KINDS


def test_not_recorded_used_exactly_for_the_frozen_ceilings():
    not_recorded_ids = {
        bid for bid, entry in brr.BOUNDARY_DEFINITIONS.items()
        if entry["architectural_rationale"] == NOT_RECORDED
    }
    assert not_recorded_ids == {
        "sleep.one_unresolved_root",
        "sleep.authorize_execute_decoupling",
        "wtr0.recovery_ceiling",
    }
    for boundary_id in not_recorded_ids:
        assert brr.BOUNDARY_DEFINITIONS[boundary_id]["operational_why"]
        assert brr.BOUNDARY_DEFINITIONS[boundary_id]["what_changes_it"]


def test_every_registry_entry_is_well_shaped():
    for boundary_id, entry in brr.BOUNDARY_DEFINITIONS.items():
        assert set(entry.keys()) == {
            "boundary_type", "operational_why", "architectural_rationale",
            "what_changes_it", "evidence_sources",
        }, boundary_id
        assert entry["boundary_type"] in brr.BOUNDARY_TYPES, boundary_id
        assert entry["operational_why"], boundary_id
        assert entry["what_changes_it"], boundary_id
        assert entry["evidence_sources"], boundary_id
    assert set(brr.BOUNDARY_DEFINITIONS) == set(brr.BOUNDARY_IDS)


def test_boundary_id_vocabulary_is_in_lockstep_with_conversation_direction():
    assert set(bi.BOUNDARY_ID_TARGETS) == set(cd.BOUNDARY_BOUNDARY_ID_TARGETS)
    assert set(cd.BOUNDARY_BOUNDARY_ID_TARGETS) <= set(brr.BOUNDARY_IDS)
    for target in cd.BOUNDARY_BOUNDARY_ID_TARGETS:
        assert target in brr.BOUNDARY_DEFINITIONS, target


def test_host_rule_targets_are_covered_by_registry():
    for target in cd.BOUNDARY_HOST_RULE_TARGETS:
        boundary_id = f"host_rule.{target}"
        assert boundary_id in brr.BOUNDARY_DEFINITIONS, boundary_id
        assert brr.BOUNDARY_DEFINITIONS[boundary_id]["boundary_type"] == "conversation_participation_rule"


def test_capability_descriptor_boundary_ids_are_locked_in_registry():
    for resource_class in workspace_capability.RESOURCE_CLASSES:
        descriptor = workspace_capability.get_capability_descriptor(resource_class)
        entry = brr.BOUNDARY_DEFINITIONS[descriptor["boundary_id"]]
        assert entry["boundary_type"] == "workspace_capability"
        assert descriptor["boundary_id"] in {"workspace.library.read_only", "workspace.music.read_only",
                                              "workspace.photographs.no_external_share",
                                              "workspace.journal.append_only",
                                              "workspace.notes.shared_vault_create_only"}
    for action in sorted(workspace_capability.ALL_KNOWN_ACTIONS):
        _, boundary_id, _ = workspace_capability.check_permission("library", action)
        if boundary_id is not None:
            assert boundary_id in brr.BOUNDARY_DEFINITIONS


def test_budget_and_outward_boundary_ids_registered():
    assert "budget.exhaustion" in brr.BOUNDARY_DEFINITIONS
    assert brr.BOUNDARY_DEFINITIONS["budget.exhaustion"]["boundary_type"] == "resource_budget"
    assert "outward.projection_failure" in brr.BOUNDARY_DEFINITIONS
    assert brr.BOUNDARY_DEFINITIONS["outward.projection_failure"]["boundary_type"] == "projection_failure_boundary"


def test_schema_migration_registration_is_idempotent_and_locked_to_constants():
    db_path = os.path.join(TEST_DIR, "registry_migration.db")
    provenance_schema.create_provenance_db(db_path).close()
    first = bsm.apply_additive_migration(db_path)
    second = bsm.apply_additive_migration(db_path)
    assert first["new_tables_created"] == [bsm.REGISTRY_TABLE]
    assert second["new_tables_created"] == []
    assert all(bsm.verify_migration_state(db_path).values())
    conn = sqlite3.connect(db_path)
    try:
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute("SELECT entry_key, event_type, component_kind FROM host_boundary_registry")
        }
    finally:
        conn.close()
    assert rows == {
        brr.BOUNDARY_INSPECTION_EVENT_TYPE: ("clark_boundary_query", None),
        brr.BOUNDARY_INSPECTION_QUERY_SPEC_COMPONENT_KIND: (None, "boundary_query_spec"),
        brr.BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND: (None, "boundary_inspection_result"),
        brr.BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND: (None, "boundary_inspection_result_carriage"),
        brr.BOUNDARY_INSPECTION_DELIVERED_COMPONENT_KIND: (None, "boundary_inspection_delivered"),
    }


ALL_TESTS = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def main():
    failed = 0
    for test in ALL_TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(ALL_TESTS)} tests, {len(ALL_TESTS) - failed} passed, {failed} failed")
    import shutil
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
