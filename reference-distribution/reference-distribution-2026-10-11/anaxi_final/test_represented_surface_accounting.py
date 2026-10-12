"""Every capability the production architecture REPRESENTS must be dispositioned
against the owner inventory -- the denominator is what production exposes, not
what tests happen to exist.

The represented surface is derived by introspection of the production modules
(typed acts and request vocabularies, workspace resource classes and actions,
roaming acts, every model-call path, every owner GUI control). Each element must
map to an inventory capability. A new represented behavior therefore FAILS this
test until it is added to the inventory (an inventory defect), never silently
excluded.
"""
import ast
import json
from pathlib import Path

import budget_compliance_registry as registry
import conversation_direction as cd
import workspace_direction as wd
import workspace_roaming as roaming

HERE = Path(__file__).resolve().parent
INVENTORY = json.loads((HERE / "owner_completion_inventory.json").read_text(encoding="utf-8"))
CAPABILITY_IDS = {entry["id"] for entry in INVENTORY["capabilities"]}
P = {c[:2]: c for c in CAPABILITY_IDS}      # "04" -> "04_library_books_documents"


def cap(*numbers):
    return tuple(P[n] for n in numbers)


# surface element -> the inventory capability id(s) that own it
ACTS = {
    "ask_human": cap("02"), "close_thread": cap("02"), "develop_current": cap("02"),
    "pause_thread": cap("02"), "shift_topic": cap("02"), "use_workspace": cap("02", "25"),
    "verify_action": cap("02", "25"), "yield_direction": cap("02"),
    "search_historical_continuity": cap("19", "30"),
    "read_historical_item": cap("19", "30"),
    "browse_historical_continuity": cap("19", "30"),
    "waymark": cap("35"), "change_waymark": cap("35"),
}
REQUEST_VOCABULARIES = {
    "VALID_DIRECTION_REQUESTS": cap("02"),
    "VALID_DIRECTION_OWNERS": cap("02"),
    "VALID_BACKGROUND_ACTIVITY_REQUESTS": cap("14"),
    "VALID_SLEEP_TIMING_REQUESTS": cap("13"),
    "VALID_OPERATIVE_DIRECTIVE_REQUESTS": cap("12"),
    "VALID_EXTERNAL_INFO_REQUESTS": cap("10"),
    "VALID_ACTION_VERIFICATION_REQUESTS": cap("02", "25"),
    "VALID_ACTION_VERIFICATION_KINDS": cap("02", "25"),
    "VALID_DISCORD_CORRESPONDENCE_REQUESTS": cap("11"),
    "VALID_CARET_REPLY_REQUESTS": cap("11"),   # occasion-only typed reply route: same private-Discord capability
    "VALID_REPLY_REQUESTS": cap("16"),         # lawful null: Clark's typed choice to say nothing (null/no-action)
    "VALID_CORRESPONDENT_STANDING_REQUESTS": cap("32"),   # Clark-controlled correspondent standing
    "VALID_CONSOLIDATION_REQUESTS": cap("19"),   # subject-directed consolidation: Clark's own choice
                                                 # about what from his own experience receives stronger
                                                 # continuity (waking and Sleep/REM alike)
    "WAYMARK_REQUESTS": cap("35"),
}
RESOURCE_CLASSES = {
    "library": cap("04"), "photographs": cap("05"), "music": cap("06"), "journal": cap("07"),
    "notes": cap("34"), "organization": cap("26"),
}
ROAMING_ACTS = {"act": cap("14"), "private_act": cap("29"), "stop": cap("14"), "wait": cap("14"), "wait_for_human": cap("14")}
MODEL_PATHS = {
    "conversation_pass1": cap("01"), "conversation_pass2": cap("01"),
    "pass1_payload_generation": cap("01"),   # compact Pass-1 control: the chosen act's own words, written next
    "task_pass2_task_mode": cap("24"), "task_pass2_regen": cap("24"),   # TASK-mode-only: unreachable from the conversation launcher (architecture-invariants)
    "artifact_judgment": cap("28"),
    "wsp1_pass1": cap("04", "25"), "wsp1_pass2": cap("04", "25"), "wsp1_pass2_vision": cap("05"),
    "roaming_stage1": cap("14"), "roaming_stage2_public": cap("14"), "roaming_stage2_private": cap("29"),
    "sleep_rem_consolidation": cap("13"), "sleep_identity_reflection": cap("13"),
    "sleep_selection": cap("13"), "sleep_transformation": cap("13"), "sleep_prompt_measurement_probe": cap("13", "30"),
    "real_text_cost_recovery_probe": cap("30"), "retained_dialogue_cost_recovery_probe": cap("30"),
    "wtr0_cold_reset_unload_probe": cap("15"), "mlxserve_provider_dispatch": cap("22"),
}
GUI_CONTROLS = {
    "Authorize Sleep": cap("13", "23"), "Defer Sleep": cap("13", "23"), "Decline Sleep": cap("13", "23"),
    "Execute one authorized Sleep cycle": cap("13", "23"),
    "I explicitly confirm one real Sleep cycle attempt": cap("13", "23"),
    "Clark-originated Sleep request": cap("13", "23"),
    "Take the lead": cap("02", "23"), "Let Clark lead": cap("02", "23"), "Open": cap("02", "23"),
    "Pause background activity": cap("14", "23"), "Resume background activity": cap("14", "23"),
    "Evidenced waking input recovery": cap("15", "23"), "Recover selected waking input once": cap("15", "23"),
    "I explicitly confirm one real same-input waking recovery attempt": cap("15", "23"),
    "Show older events": cap("15", "23"),          # read-only list expansion of the same recovery selector
    "Refresh Caret status": cap("11", "23"),       # read-only owner telemetry; derives from canonical Caret records
    # Owner administration of Caret correspondents (Discord author id -> existing canonical principal):
    # explicit owner acts on the owner-gated ledger; rendering is read-only. Clark never administers.
    "Map": cap("11", "23"), "Revoke": cap("11", "23"),
    # Owner doors, safety blocks and lifecycle closure for general correspondence (never standing).
    "Authorized destination": cap("32", "23"),
    "Open to correspondents who are not ANAXI principals": cap("32", "23"),
    "Clark may write first (to correspondents he has standing with)": cap("32", "23"),
    "Set door": cap("32", "23"), "Block": cap("32", "23"), "Unblock": cap("32", "23"),
    "Authorize channel": cap("32", "23"), "Revoke destination": cap("32", "23"),
    "Record identity": cap("31", "23"),
    # Owner-authored shared-vault note: canonical owner authorship + never-overwrite write (collaborative workspace).
    "Write note to shared workspace": cap("34", "23"),
    "Held inbound message": cap("33", "23"), "Close": cap("33", "23"),
    "Reopen (new retry budget)": cap("33", "23"),
    "Clark's undelivered send": cap("33", "23"), "Close (do not deliver)": cap("33", "23"),
    "Refresh": cap("11", "23"),                    # read-only re-read of the correspondents panel
    "ANAXI principal": cap("11", "23"), "Mapped Discord id": cap("11", "23"),
    "Send": cap("01", "23"),
    # Opt-in Porch device administration. Pairing/revocation is an owner act;
    # the device selector is a read-only view of the same authority registry.
    "Paired device": cap("23", "36"),
    "Create one-time pairing code": cap("23", "36"),
    "Revoke selected device": cap("23", "36"),
}


def _literal_gui_control_labels(source):
    """Return every statically named owner control, independent of formatting.

    The former regex only recognized a string immediately after ``gr.X(``.
    A multiline constructor therefore hid Porch's device selector, while a
    constant-valued label hid the recovery confirmation checkbox.  The
    represented-surface denominator must not depend on source formatting.
    """
    tree = ast.parse(source)
    string_constants = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            string_constants[node.targets[0].id] = node.value.value

    def resolve(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return string_constants.get(node.id)
        return None

    labels = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "gr"
                and node.func.attr in {"Button", "Checkbox", "Dropdown", "Radio"}):
            continue
        label_node = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "label":
                label_node = keyword.value
                break
        label = resolve(label_node)
        assert label, f"owner GUI control at line {node.lineno} has no statically accountable label"
        labels.add(label)
    return labels


def test_the_mapping_only_names_real_inventory_capabilities():
    for table in (ACTS, REQUEST_VOCABULARIES, RESOURCE_CLASSES, ROAMING_ACTS, MODEL_PATHS, GUI_CONTROLS):
        for element, owners in table.items():
            assert owners and all(o in CAPABILITY_IDS for o in owners), element


def test_every_typed_act_and_request_vocabulary_is_dispositioned():
    represented_acts = set(cd.VALIDATED_ACTS) | {cd.CHANGE_WAYMARK}
    assert represented_acts == set(ACTS), sorted(represented_acts ^ set(ACTS))
    for name in dir(cd):
        if name.startswith("VALID_") and isinstance(getattr(cd, name), (set, frozenset, tuple, list)):
            assert name in REQUEST_VOCABULARIES, f"represented request vocabulary not in the inventory: {name}"
    assert set(cd.WAYMARK_REQUESTS)
    assert "WAYMARK_REQUESTS" in REQUEST_VOCABULARIES


def test_every_workspace_resource_class_and_roaming_act_is_dispositioned():
    assert set(wd.LIVE_ALLOWED_SURFACE) == set(RESOURCE_CLASSES), sorted(set(wd.LIVE_ALLOWED_SURFACE) ^ set(RESOURCE_CLASSES))
    assert set(roaming.VALID_ROAMING_ACTS) == set(ROAMING_ACTS), sorted(set(roaming.VALID_ROAMING_ACTS) ^ set(ROAMING_ACTS))


def test_every_model_call_path_is_dispositioned():
    represented = {path["path_id"] for path in registry.ALL_PATHS}
    assert represented == set(MODEL_PATHS), sorted(represented ^ set(MODEL_PATHS))


def test_every_owner_gui_control_is_dispositioned():
    gui = HERE / "llama_gui.py"
    if not gui.is_file():
        # October 11 edition: llama_gui.py carries the private Porch integration and is
        # not published. The GUI-control surface cannot be introspected here; the
        # remaining represented surfaces (model paths, typed acts, workspace classes,
        # roaming acts) are still checked by the other tests in this module.
        import pytest
        pytest.skip("llama_gui.py is not published in this edition (private Porch integration)")
    source = gui.read_text(encoding="utf-8")
    labels = _literal_gui_control_labels(source)
    assert labels == set(GUI_CONTROLS), sorted(labels ^ set(GUI_CONTROLS))
