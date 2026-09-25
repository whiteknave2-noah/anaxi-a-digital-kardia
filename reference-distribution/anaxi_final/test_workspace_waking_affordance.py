"""Reference-stabilization coverage for Clark's ordinary waking workspace bridge."""
import json
import importlib
import os
from pathlib import Path
import sqlite3
import sys
import types

import pytest

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
if ANAXI_FINAL not in sys.path:
    sys.path.insert(0, ANAXI_FINAL)


def _install_optional_workspace_stubs():
    if "pypdf" not in sys.modules:
        module = types.ModuleType("pypdf")
        module.PdfReader = object
        sys.modules["pypdf"] = module
    if "PIL" not in sys.modules:
        pil = types.ModuleType("PIL")
        pil.Image = object
        sys.modules["PIL"] = pil
    if "soundfile" not in sys.modules:
        sys.modules["soundfile"] = types.ModuleType("soundfile")
    if "scipy" not in sys.modules:
        scipy = types.ModuleType("scipy")
        scipy.fft = types.SimpleNamespace()
        signal = types.ModuleType("scipy.signal")
        signal.find_peaks = lambda *args, **kwargs: ([], {})
        sys.modules["scipy"] = scipy
        sys.modules["scipy.signal"] = signal


def test_ordinary_waking_can_select_and_receive_substantive_library_content(monkeypatch, tmp_path):
    import test_owc5_s2_integration as fixture

    la, calls, _p1, _p2, persistence = fixture.fresh_llama_anaxi(
        pass1_value=None, pass2_value=None,
    )
    _install_optional_workspace_stubs()
    import workspace_capability as wc
    import workspace_supervisor
    workspace_supervisor.la = la
    workspace_supervisor.native_provenance_writer = sys.modules["native_provenance_writer"]

    workspace_root = str(tmp_path / "workspace")
    paths = wc.WorkspacePaths(workspace_root)
    paths.ensure_exists()
    Path(paths.library_dir, "plain-language-title.txt").write_text(
        "Substantive library content reached Clark through waking.", encoding="utf-8"
    )
    monkeypatch.setattr(
        wc.WorkspacePaths, "production_defaults", classmethod(lambda cls: paths),
    )
    # This test isolates the waking->WSP1 bridge. The fixture deliberately
    # supplies a fake native writer and no canonical database, so keep the
    # two unrelated durable-observation boundaries in memory here.
    monkeypatch.setattr(la, "record_observation", lambda *args, **kwargs: None)
    import workspace_episode_provenance
    encounters = []
    monkeypatch.setattr(
        workspace_episode_provenance, "record_resource_encounter",
        lambda *args, **kwargs: encounters.append(kwargs),
    )

    initial = {
        "act": "use_workspace", "thread": "library", "direction_request": "request_human",
        "relinquish_direction": False,
    }
    workspace_action = {
        "resource_class": "library", "action": "read",
        "relative_path": "plain-language-title.txt", "content": "",
    }
    expression = {"expression": "I read the substantive library content."}

    def fake_chat(model, messages, format=None, options=None, think=None, **kwargs):
        import sleep_decision_test_support as _sdts
        if _sdts.is_sleep_decision(format):
            return _sdts.sleep_decision_response()
        calls.append({"model": model, "format": format, "messages": [dict(m) for m in messages]})
        if isinstance(format, dict) and "act" in format.get("properties", {}):
            value = initial
        elif format == "json":
            value = workspace_action
        else:
            value = expression
        return {
            "message": {"content": json.dumps(value)},
            "prompt_eval_count": sum(len(m.get("content", "").encode("utf-8")) for m in messages),
            "done": True, "done_reason": "stop", "eval_count": 20,
        }

    la.ollama.chat = fake_chat
    result = la.run_waking_turn(
        la.AnaxiOrchestrator(), "Choose something useful from your workspace.",
        interaction_mode="conversation",
    )

    assert result["reply"] == expression["expression"]
    assert result["boundary_result"]["result"]["content"].startswith("Substantive library content")
    assert result["_diagnostic_typed_act"] == "use_workspace"
    direction_trace = la.get_last_conversation_direction_trace()
    assert direction_trace["act"] == "use_workspace"
    assert direction_trace["direction_request"] == "request_human"
    assert direction_trace["direction_owner_before"] == "unknown"
    assert direction_trace["direction_owner_after"] == "unknown"
    assert direction_trace["pass1_status"] == direction_trace["pass2_status"] == "ok"
    assert len(persistence) == 1
    assert persistence[0]["clark_prose"] == expression["expression"]
    assert [c["format"] for c in calls][0] == la.PASS1_SCHEMA
    assert any(
        "act=use_workspace" in message["content"]
        for message in calls[0]["messages"]
    )
    assert calls[1]["format"] == "json"  # existing WSP1 resource selector
    workspace_selector_text = "\n".join(m["content"] for m in calls[1]["messages"])
    assert "library" in workspace_selector_text
    assert "organization" not in workspace_selector_text
    assert encounters and encounters[0]["resource_class"] == "library"


def test_workspace_direction_request_full_path_persists_canonical_x(monkeypatch, tmp_path):
    """Owner checkout regression: lawful direction request + library use.

    This crosses the real ordinary ``run_waking_turn`` integration, real
    supervised Workspace dispatcher, real native canonical writer, and a
    fresh synthetic provenance database. Only model calls are faked.
    """
    from migrate_historical_data import build_pipeline_map, seed_reference_data
    from provenance_schema import create_provenance_db
    import test_owc5_s2_integration as fixture

    la, calls, _p1, _p2, _fake_persistence = fixture.fresh_llama_anaxi(
        pass1_value=None, pass2_value=None,
    )
    # The fixture deliberately replaces this module with an in-memory writer.
    # Re-import the production implementation only after that fixture setup.
    sys.modules.pop("native_provenance_writer", None)
    real_npw = importlib.import_module("native_provenance_writer")
    _install_optional_workspace_stubs()
    import workspace_capability as wc
    import workspace_episode_provenance
    import workspace_supervisor

    la.native_provenance_writer = real_npw
    # ``fresh_llama_anaxi`` installs a fresh llama_anaxi module on every
    # invocation, while the Workspace supervisor is intentionally a normal
    # cached import. Keep the already-imported supervisor on this test's
    # process-local model boundary.
    workspace_supervisor.la = la
    workspace_supervisor.native_provenance_writer = real_npw
    db_path = os.path.join(la.PROVENANCE_DB_DIR, "anaxi_provenance.db")
    create_provenance_db(db_path).close()
    conn = sqlite3.connect(db_path)
    manifest = {"pipelines": {
        key: {"routing_constant_value": "synthetic_user"}
        for key in ("llama", "claude")
    }}
    seed_reference_data(conn, build_pipeline_map(manifest), 1000)
    conn.close()

    paths = wc.WorkspacePaths(str(tmp_path / "canonical-workspace"))
    paths.ensure_exists()
    source_text = "Substantive canonical library payload for the coexistence regression."
    Path(paths.library_dir, "chosen.txt").write_text(source_text, encoding="utf-8")
    monkeypatch.setattr(wc.WorkspacePaths, "production_defaults", classmethod(lambda cls: paths))
    monkeypatch.setattr(la, "record_observation", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        workspace_episode_provenance, "record_resource_encounter",
        lambda *args, **kwargs: None,
    )

    conversation_choice = {
        "act": "use_workspace", "thread": "library",
        "direction_request": "request_human", "relinquish_direction": False,
    }
    workspace_choice = {
        "resource_class": "library", "action": "read",
        "relative_path": "chosen.txt", "content": "",
    }
    expression = {"expression": "I received substantive text from chosen.txt."}

    def fake_chat(model, messages, format=None, options=None, think=None, **kwargs):
        import sleep_decision_test_support as _sdts
        if _sdts.is_sleep_decision(format):
            return _sdts.sleep_decision_response()
        calls.append({"model": model, "format": format, "messages": [dict(item) for item in messages]})
        if isinstance(format, dict) and "act" in format.get("properties", {}):
            value = conversation_choice
        elif format == "json":
            value = workspace_choice
        else:
            value = expression
        return {
            "message": {"content": json.dumps(value)},
            "prompt_eval_count": sum(len(item.get("content", "").encode()) for item in messages),
            "done": True, "done_reason": "stop", "eval_count": 20,
        }

    la.ollama.chat = fake_chat
    result = la.run_waking_turn(
        la.AnaxiOrchestrator(), "Choose and read something from the library.",
        interaction_mode="conversation",
    )

    assert result["boundary_result"]["result"]["content"] == source_text
    assert result["reply"] == expression["expression"]
    assert len(calls) == 3  # conversation Pass 1, Workspace selector, Workspace Pass 2
    workspace_pass2_text = "\n".join(
        item.get("content", "") for item in calls[-1]["messages"]
    )
    assert source_text in workspace_pass2_text

    direction_trace = la.get_last_conversation_direction_trace()
    assert direction_trace["direction_request"] == "request_human"
    assert direction_trace["direction_owner_before"] == "unknown"
    assert direction_trace["direction_owner_after"] == "unknown"
    assert direction_trace["pass1_status"] == direction_trace["pass2_status"] == "ok"

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        event = conn.execute(
            "SELECT event_type FROM events WHERE event_id=?", (result["native_event_id"],)
        ).fetchone()
        prose = conn.execute(
            "SELECT component_text FROM event_components WHERE event_id=? "
            "AND component_kind='conversational_prose'", (result["native_event_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert event == ("waking_turn",)
    assert prose == (expression["expression"],)


@pytest.mark.parametrize("resource_class", ["library", "journal", "photographs", "music"])
def test_direction_request_reaches_every_public_workspace_class(monkeypatch, tmp_path, resource_class):
    """The repaired seam precedes resource selection and is class-neutral."""
    import test_owc5_s2_integration as fixture

    la, calls, _p1, _p2, persistence = fixture.fresh_llama_anaxi(
        pass1_value=None, pass2_value=None,
    )
    _install_optional_workspace_stubs()
    import workspace_capability as wc
    import workspace_episode_provenance
    import workspace_supervisor

    workspace_supervisor.la = la
    workspace_supervisor.native_provenance_writer = sys.modules["native_provenance_writer"]

    paths = wc.WorkspacePaths(str(tmp_path / resource_class))
    paths.ensure_exists()
    monkeypatch.setattr(wc.WorkspacePaths, "production_defaults", classmethod(lambda cls: paths))
    monkeypatch.setattr(la, "record_observation", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        workspace_episode_provenance, "record_resource_encounter",
        lambda *args, **kwargs: None,
    )

    conversation_choice = {
        "act": "use_workspace", "thread": resource_class,
        "direction_request": "request_clark", "relinquish_direction": False,
    }
    workspace_choice = {
        "resource_class": resource_class, "action": "list",
        "relative_path": "", "content": "",
    }
    expression = {"expression": f"I listed the {resource_class} resources."}

    def fake_chat(model, messages, format=None, options=None, think=None, **kwargs):
        import sleep_decision_test_support as _sdts
        if _sdts.is_sleep_decision(format):
            return _sdts.sleep_decision_response()
        calls.append({"format": format, "messages": [dict(item) for item in messages]})
        if isinstance(format, dict) and "act" in format.get("properties", {}):
            value = conversation_choice
        elif format == "json":
            value = workspace_choice
        else:
            value = expression
        return {"message": {"content": json.dumps(value)}, "done": True,
                "done_reason": "stop", "eval_count": 20}

    la.ollama.chat = fake_chat
    result = la.run_waking_turn(
        la.AnaxiOrchestrator(), f"Browse {resource_class}.", interaction_mode="conversation",
    )
    assert result["reply"] == expression["expression"]
    assert result["boundary_result"]["action"] == "list"
    assert result["boundary_result"]["scope"] == f"local_workspace/{resource_class}"
    assert result["boundary_result"]["result"] is not None
    assert len(persistence) == 1
    assert la.get_last_conversation_direction_trace()["pass1_status"] == "ok"


def test_a_withdrawn_boundary_inquiry_is_never_dispatched_nor_a_competing_action():
    # Owner decision 2026-09-24: boundary inquiry is withdrawn from the production release contract. A
    # boundary_inquiry_request (the grammar still admits it, see PASS1_SCHEMA) is treated as none.
    import conversation_direction as cd
    raw = {"act": cd.USE_WORKSPACE, "thread": "library", "direction_request": cd.REQUEST_HUMAN,
           "relinquish_direction": False, "boundary_inquiry_request": "capability:library"}
    validated, failure = cd.validate_pass1_conversation_act(raw)
    assert failure is None and validated["boundary_inquiry_request"] is None
    assert cd.cross_validate_workspace_coexistence(validated) is None
    assert cd.BOUNDARY_INQUIRY_IN_RELEASE is False


@pytest.mark.parametrize("extra", [
    {"background_activity_request": "resume_own_pause"},
    {"sleep_timing_request": "request_sleep"},
    {"operative_directive_request": "set_directive", "operative_directive_text": "Keep this synthetic directive."},
    {"external_info_request": "web_search", "external_info_target": "synthetic query"},
    {"discord_correspondence_request": "send_message", "discord_destination_id": "synthetic-destination", "discord_message_text": "synthetic text"},
])
def test_workspace_still_rejects_competing_substantive_actions(extra):
    import conversation_direction as cd

    raw = {
        "act": cd.USE_WORKSPACE, "thread": "library",
        "direction_request": cd.REQUEST_HUMAN, "relinquish_direction": False,
        **extra,
    }
    validated, failure = cd.validate_pass1_conversation_act(raw)
    assert failure is None
    assert cd.cross_validate_direction_control(validated, cd.DIRECTION_UNKNOWN) is None
    assert cd.cross_validate_workspace_coexistence(validated) == cd.DirectionFailure.WORKSPACE_ACTION_CONFLICT


def test_workspace_relinquishment_requires_existing_clark_ownership_but_then_coexists():
    import conversation_direction as cd

    raw = {
        "act": cd.USE_WORKSPACE, "thread": "library",
        "direction_request": cd.REQUEST_HUMAN, "relinquish_direction": True,
    }
    validated, failure = cd.validate_pass1_conversation_act(raw)
    assert failure is None
    assert cd.cross_validate_direction_control(validated, cd.DIRECTION_UNKNOWN) == cd.DirectionFailure.UNAUTHORIZED_RELINQUISH
    assert cd.cross_validate_direction_control(validated, cd.DIRECTION_CLARK) is None
    assert cd.cross_validate_workspace_coexistence(validated) is None


def test_workspace_availability_is_owner_private_after_fs1():
    import test_owc5_s2_integration as fixture
    la, *_ = fixture.fresh_llama_anaxi(pass1_value=None, pass2_value=None)

    owner_private = {
        "principal_actor_id": "owner", "owner_actor_id": "owner",
        "visibility_scope": "principal_private",
    }
    owner_shared = {**owner_private, "visibility_scope": "family_shared"}
    member_private = {**owner_private, "principal_actor_id": "member"}

    assert la._workspace_action_available(False, None)
    assert la._workspace_action_available(True, owner_private)
    assert not la._workspace_action_available(True, owner_shared)
    assert not la._workspace_action_available(True, member_private)
    assert not la._workspace_action_available(True, None)

    # Discord correspondence carries the same private owner authority
    # boundary once multiple principals exist.
    assert la._discord_correspondence_available(False, None)
    assert la._discord_correspondence_available(True, owner_private)
    assert not la._discord_correspondence_available(True, owner_shared)
    assert not la._discord_correspondence_available(True, member_private)
    assert not la._discord_correspondence_available(True, None)

    # A scoped turn cannot mutate a singleton directive whose source
    # occurrence is hidden from it.
    private_current = {"active_event_id": "private-directive"}
    assert la._operative_directive_action_available(False, private_current, None)
    assert la._operative_directive_action_available(True, None, None)
    assert la._operative_directive_action_available(True, private_current, private_current)
    assert not la._operative_directive_action_available(True, private_current, None)
