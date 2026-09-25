"""WSP1-S2 mechanical acceptance tests. Zero real Ollama/model calls --
ollama faked at the sys.modules boundary, exactly matching
test_owc5_s2_integration.py's established convention. No real
workspace or live database touched.
"""
import json
import os
import shutil
import sys
import tempfile
import traceback
import types

import pypdf
import pytest

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import context_budget
import hippocampus_retrieval
import ui_turn_diagnostics as utd
import workspace_capability as wc
import workspace_direction as wd

CLARK_ACTOR_ID = "actor-80eee447ad46e1a1e9b2ea65b5"
TEST_ROOT = tempfile.mkdtemp(prefix="wsp1s2_test_")


def fresh_paths(name):
    root = os.path.join(TEST_ROOT, name)
    paths = wc.WorkspacePaths(root=root)
    paths.ensure_exists()
    return paths


def counting_callable(return_value_or_fn):
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if callable(return_value_or_fn):
            return return_value_or_fn()
        return return_value_or_fn

    _fn.calls = calls
    return _fn


# ------------------------------------------------- pure-pathway tests (A/H/J)


def test_pass1_call_count_one_on_success():
    paths = fresh_paths("call_count")
    pass1 = counting_callable({"resource_class": "library", "action": "list", "relative_path": "", "content": ""})
    pass2 = counting_callable({"expression": "I checked the library."})
    result, failure, boundary, p1, p2 = wd.run_typed_workspace_turn(paths, CLARK_ACTOR_ID, pass1, pass2)
    assert failure is None
    assert p1 == 1 and p2 == 1
    assert pass1.calls["n"] == 1
    assert pass2.calls["n"] == 1


def test_pass1_malformed_no_execution_no_pass2():
    paths = fresh_paths("pass1_malformed")
    pass1 = counting_callable({"resource_class": "library", "action": "delete_everything", "relative_path": "", "content": ""})
    pass2 = counting_callable({"expression": "unreachable"})
    result, failure, boundary, p1, p2 = wd.run_typed_workspace_turn(paths, CLARK_ACTOR_ID, pass1, pass2)
    assert result is None
    assert failure == wd.DirectionFailure.NOT_IN_ALLOWED_SURFACE
    assert boundary is None
    assert p1 == 1 and p2 == 0
    assert pass2.calls["n"] == 0
    assert wc.query_action_log(paths) == []  # no execution attempted at all


def test_pass2_malformed_action_result_remains_logged_no_retry():
    paths = fresh_paths("pass2_malformed")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("hello world")
    pass1 = counting_callable({"resource_class": "library", "action": "list", "relative_path": "", "content": ""})
    pass2 = counting_callable({"wrong_key": "oops"})
    result, failure, boundary, p1, p2 = wd.run_typed_workspace_turn(paths, CLARK_ACTOR_ID, pass1, pass2)
    assert result is None
    assert failure == wd.DirectionFailure.MALFORMED_EXPRESSION
    assert boundary is not None
    assert boundary["result"]["entries"] == ["book.txt"]  # the list action's result IS observable
    assert boundary["result"]["truncated"] is False
    assert p1 == 1 and p2 == 1
    assert pass1.calls["n"] == 1  # no reselection
    log = wc.query_action_log(paths)
    assert len(log) == 1
    assert log[0]["action"] == "list" and log[0]["result"] == "performed"


# ----------------------------------------------------------------- B: library list


def test_library_list_executes_once():
    paths = fresh_paths("lib_list")
    for name in ("a.txt", "b.txt"):
        with open(os.path.join(paths.library_dir, name), "w", encoding="utf-8") as f:
            f.write("x")
    action = {"resource_class": "library", "action": "list", "relative_path": "", "content": ""}
    validated, failure = wd.validate_pass1_workspace_action(action)
    assert failure is None
    boundary, performed = wd.execute_workspace_action(paths, validated, CLARK_ACTOR_ID)
    assert performed is True
    assert boundary["result"]["entries"] == ["a.txt", "b.txt"]
    assert boundary["result"]["total_count"] == 2
    assert boundary["result"]["truncated"] is False
    assert boundary["boundary"] == "workspace.library.read_only"
    assert boundary["consequence"] == "Read permitted; source remains unchanged."


# ------------------------------------------------------------- C: library read


def test_library_bounded_read_exact_content():
    paths = fresh_paths("lib_read")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("Chapter one begins here.")
    action = {"resource_class": "library", "action": "read", "relative_path": "book.txt", "content": ""}
    validated, _ = wd.validate_pass1_workspace_action(action)
    boundary, performed = wd.execute_workspace_action(paths, validated, CLARK_ACTOR_ID)
    assert performed is True
    assert boundary["result"]["content"] == "Chapter one begins here."
    assert boundary["result"]["has_more"] is False


# ------------------------------------------------------------- D: journal append


def test_journal_append_exact_content_once():
    paths = fresh_paths("journal_append")
    action = {"resource_class": "journal", "action": "append", "relative_path": "", "content": "A private thought."}
    validated, _ = wd.validate_pass1_workspace_action(action)
    boundary, performed = wd.execute_workspace_action(paths, validated, CLARK_ACTOR_ID)
    assert performed is True
    assert boundary["result"]["content"] == "A private thought."
    assert boundary["result"]["author_actor_id"] == CLARK_ACTOR_ID
    entries = [f for f in os.listdir(paths.journal_dir) if f.endswith(".json")]
    assert len(entries) == 1


# ---------------------------------------------------------------- E: denied action


def test_denied_action_correct_boundary_consequence_rationale_no_mutation():
    paths = fresh_paths("denied")
    with open(os.path.join(paths.photographs_dir, "photo.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff")
    result, failure = wc.attempt_photograph_mutation_or_share(paths, wc.EXTERNAL_SHARE, "photo.jpg")
    assert result is None
    assert failure["boundary_id"] == "workspace.photographs.no_external_share"
    with open(os.path.join(paths.photographs_dir, "photo.jpg"), "rb") as f:
        assert f.read() == b"\xff\xd8\xff"


# --------------------------------------------------------------- F: unknown action


def test_unknown_action_and_resource_fail_closed():
    validated, failure = wd.validate_pass1_workspace_action(
        {"resource_class": "library", "action": "execute_shell", "relative_path": "", "content": ""}
    )
    assert validated is None and failure == wd.DirectionFailure.NOT_IN_ALLOWED_SURFACE

    validated2, failure2 = wd.validate_pass1_workspace_action(
        {"resource_class": "web", "action": "list", "relative_path": "", "content": ""}
    )
    assert validated2 is None and failure2 == wd.DirectionFailure.UNKNOWN_RESOURCE_CLASS


# ----------------------------------------------------------- G: traversal denied


def test_traversal_attempt_fails_closed():
    paths = fresh_paths("traversal")
    action = {"resource_class": "library", "action": "read", "relative_path": "../../../../etc/passwd", "content": ""}
    validated, _ = wd.validate_pass1_workspace_action(action)
    boundary, performed = wd.execute_workspace_action(paths, validated, CLARK_ACTOR_ID)
    assert performed is False
    assert boundary["result"] is None


# ----------------------------------------------------------- I: host-exec failure


def test_host_execution_failure_not_fabricated_success():
    paths = fresh_paths("exec_failure")
    action = {"resource_class": "library", "action": "read", "relative_path": "nonexistent.txt", "content": ""}
    validated, _ = wd.validate_pass1_workspace_action(action)
    boundary, performed = wd.execute_workspace_action(paths, validated, CLARK_ACTOR_ID)
    assert performed is False
    assert boundary["consequence"] == "Action blocked; source remains unchanged."
    assert boundary["result"] is None


# -------------------------------------------------------------- N: no hippocampus


def test_no_hippocampal_or_kardia_touch_in_pathway_module():
    with open(os.path.join(ANAXI_FINAL, "workspace_direction.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("hippocampus_store", "hippocampus_retrieval", "active_kardia", "get_current_kardia", "sync_hippocampus"):
        assert forbidden not in source


# -------------------------------------------------------------- O: API1/API2


def test_api1_api2_source_hashes_unchanged():
    import hashlib
    expected_prefixes = {
        "api1_control_plane.py": "b98c648c28168e69",
        "api2_control_plane.py": "b40aed1b38989bb9",
        "api2_supervisor.py": "74b48d59a052689b",
        "decision_path_core.py": "c8ecbb58fd4db63c",
    }
    for fn, prefix in expected_prefixes.items():
        with open(os.path.join(ANAXI_FINAL, fn), "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        assert actual.startswith(prefix), fn


# -------------------------------------------------------------- P: no network


def test_no_network_imports_or_calls():
    for fn in ("workspace_direction.py", "workspace_supervisor.py"):
        with open(os.path.join(ANAXI_FINAL, fn), encoding="utf-8") as f:
            source = f.read()
        for forbidden in ("requests.", "urllib", "socket.", "http.client", "ftplib", "smtplib"):
            assert forbidden not in source, f"{fn}: {forbidden}"


# ----------------------------------------------------------- Q: no perception


def test_no_audio_vision_perception_claims():
    paths = fresh_paths("perception")
    with open(os.path.join(paths.music_dir, "song.mp3"), "wb") as f:
        f.write(b"ID3")
    action = {"resource_class": "music", "action": "inspect_metadata", "relative_path": "song.mp3", "content": ""}
    validated, _ = wd.validate_pass1_workspace_action(action)
    boundary, performed = wd.execute_workspace_action(paths, validated, CLARK_ACTOR_ID)
    assert performed is True
    assert "audio_pathway_available" not in boundary["result"]  # inspect_metadata doesn't claim perception at all
    # music READ is not in the live allowed surface for this gate
    read_action = {"resource_class": "music", "action": "read", "relative_path": "song.mp3", "content": ""}
    validated2, failure2 = wd.validate_pass1_workspace_action(read_action)
    assert validated2 is None
    assert failure2 == wd.DirectionFailure.NOT_IN_ALLOWED_SURFACE


# ---------------------------------------------------------- R: no absolute paths


def test_persistent_identifiers_no_absolute_windows_path():
    paths = fresh_paths("relative_identity")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("content")
    action = {"resource_class": "library", "action": "read", "relative_path": "book.txt", "content": ""}
    validated, _ = wd.validate_pass1_workspace_action(action)
    wd.execute_workspace_action(paths, validated, CLARK_ACTOR_ID)
    log = wc.query_action_log(paths)
    assert len(log) == 1
    assert log[0]["relative_path"] == "book.txt"
    assert ":" not in log[0]["relative_path"]  # no drive letter
    assert TEST_ROOT not in json.dumps(log[0])  # no absolute host path leaked into the durable record


# ------------------------------------------------------- live-integration (L/M)


def build_fake_ollama(call_log, pass1_value_holder, pass2_value_holder):
    fake = types.ModuleType("ollama")

    def fake_chat(model, messages, format=None, options=None, think=None):
        import sleep_decision_test_support as _sdts
        if _sdts.is_sleep_decision(format):
            return _sdts.sleep_decision_response()
        call_log.append({"model": model, "format": format, "messages": [dict(m) for m in messages]})
        value = pass1_value_holder["value"] if format == "json" else pass2_value_holder["value"]
        content = value if isinstance(value, str) else json.dumps(value)
        return {"message": {"content": content}}

    fake.chat = fake_chat
    return fake


def install_fake_heavy_dependencies(core_system_text=None, legacy_retrieval_text=None, hippocampal_retrieval_result=None):
    """OWC9-P3 Part B: the three optional kwargs are ADDITIVE, off by
    default (None) -- every existing call site (none of which pass
    them) gets the exact original 4-key prepare_context() return,
    unaffected. Only the new WSP1 budget-repair tests below pass them."""
    fake_orch_module = types.ModuleType("orchestration")

    class FakeOrchestrator:
        def prepare_context(self, user_id, prompt):
            prepared = {
                "messages": [
                    {"role": "system", "content": "Your current stance:\n- Aesthetic directive: Respond with extreme brevity and precision. Prefer short sentences. Avoid filler."},
                    {"role": "user", "content": prompt},
                ],
                "controls": {"temperature": 0.4, "top_p": 0.85, "style_instruction": "Respond with extreme brevity and precision. Prefer short sentences. Avoid filler."},
                "kardia": {}, "memory_context": "",
            }
            if core_system_text is not None:
                prepared["core_system_text"] = core_system_text
            if legacy_retrieval_text is not None:
                prepared["legacy_retrieval_text"] = legacy_retrieval_text
            if hippocampal_retrieval_result is not None:
                prepared["hippocampal_retrieval_result"] = hippocampal_retrieval_result
            return prepared

        def record_turn_generation_controls(self, user_id, controls, *, model_revision_id, pipeline_id, event_id, timestamp=None):
            pass

    fake_orch_module.AnaxiOrchestrator = FakeOrchestrator
    sys.modules["orchestration"] = fake_orch_module

    fake_relational_module = types.ModuleType("relational_history")

    class FakeRelationalHistory:
        def __init__(self, path):
            pass

        def record_event(self, **kwargs):
            pass

        def close(self):
            pass

    fake_relational_module.RelationalHistory = FakeRelationalHistory
    sys.modules["relational_history"] = fake_relational_module

    # SLP0: llama_anaxi.py does not import sleep_receipts at all, directly
    # or transitively (confirmed by source audit) -- faking it here was
    # dead defensive code whose only observed real effect was a
    # permanent, unrestored sys.modules["sleep_receipts"] substitution
    # that corrupted later same-process tests (e.g. test_sleep_receipts.
    # py's own inspect.getsource() call). Removed rather than wrapped in
    # save/restore machinery, since the injection itself was unnecessary.

    fake_npw_module = types.ModuleType("native_provenance_writer")
    _n = {"count": 0}
    persistence_log = []

    def _fake_generate_native_ulid():
        _n["count"] += 1
        return f"FAKEULID{_n['count']:018d}"

    def _fake_stage_and_record_native_waking_turn(data_dir, staging_path, *, session_id, session_started_at, user_id, prompt, bounded_clause, clark_prose, kardia, controls, waking_model_tag, pipeline_key, artifact_pass_ran, occurred_at, delivered_episode_run_id=None, interaction_mode=None, delivered_active_workspace_event_ids=None, human_input_event_id=None):
        _n["count"] += 1
        persistence_log.append({"prompt": prompt, "clark_prose": clark_prose, "bounded_clause": bounded_clause})
        return {
            "event_id": f"fake-event-{_n['count']}", "session_id": session_id,
            "staging_id": f"fake-staging-{_n['count']}",
            "auth_context_id": f"fake-auth-{_n['count']}", "pipeline_id": f"fake-pipeline-{pipeline_key}",
            "model_participations": [{"model_revision_id": "fake-rev-pass2", "tag": waking_model_tag}],
            "reassembled_reply": clark_prose, "occurred_at": occurred_at,
        }

    fake_npw_module.generate_native_ulid = _fake_generate_native_ulid
    fake_npw_module.stage_and_record_native_waking_turn = _fake_stage_and_record_native_waking_turn
    sys.modules["native_provenance_writer"] = fake_npw_module
    return persistence_log


def fresh_modules(pass1_value=None, pass2_value=None, core_system_text=None,
                   legacy_retrieval_text=None, hippocampal_retrieval_result=None):
    call_log = []
    pass1_holder = {"value": pass1_value}
    pass2_holder = {"value": pass2_value}
    sys.modules["ollama"] = build_fake_ollama(call_log, pass1_holder, pass2_holder)
    persistence_log = install_fake_heavy_dependencies(
        core_system_text=core_system_text, legacy_retrieval_text=legacy_retrieval_text,
        hippocampal_retrieval_result=hippocampal_retrieval_result,
    )
    for mod in ("llama_anaxi", "workspace_supervisor", "workspace_direction", "conversation_direction"):
        if mod in sys.modules:
            del sys.modules[mod]
    import llama_anaxi as la
    test_dir = tempfile.mkdtemp(prefix="wsp1s2_live_")
    la.PROVENANCE_DB_DIR = test_dir
    la.STAGING_PATH = os.path.join(test_dir, "native_turn_staging.jsonl")
    import workspace_supervisor as ws
    return la, ws, call_log, persistence_log


# --------------------------------------------------- L: task-mode unaffected


def test_supervisor_refuses_outside_conversation_mode():
    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
        pass2_value={"expression": "unreachable"},
    )
    # task mode (default) -- never explicitly selected conversation
    paths = fresh_paths("refuse_task_mode")
    raised = None
    try:
        ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), "hi", "clark-actor", paths)
    except ws.SupervisorRefused as exc:
        raised = exc
    assert raised is not None
    assert call_log == []  # zero model calls
    assert persistence == []


# ------------------------------------------------- M: OWC noninterference (source)


def test_owc_sources_untouched_by_workspace_modules():
    # Final integration intentionally adds the one ordinary-waking
    # delegation seam in llama_anaxi; the lower-level OWC contracts must
    # remain independent of workspace internals.
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        waking_source = f.read()
    assert "import workspace_supervisor" in waking_source
    for fn in (
        "conversation_direction.py",
        "interaction_mode.py",
        "session_dialogue_window.py",
    ):
        with open(os.path.join(ANAXI_FINAL, fn), encoding="utf-8") as f:
            source = f.read()
        assert "import workspace" not in source, f"{fn} unexpectedly imports a workspace module"
        assert "from workspace" not in source, f"{fn} unexpectedly imports a workspace module"


def test_music_listen_and_inspect_audio_reachable_through_live_typed_pathway():
    """CAP2F: model-free proof that LISTEN/INSPECT_AUDIO are genuinely
    reachable through the SAME live typed-action pathway every other
    resource class uses -- typed action -> host validation -> real
    decode -> bounded representation -> observable state."""
    import numpy as np
    from scipy.io import wavfile

    paths = fresh_paths("music_live_pathway")
    os.makedirs(paths.music_dir, exist_ok=True)
    sr = 22050
    t = np.linspace(0, 2.0, int(sr * 2.0), endpoint=False)
    tone = (0.5 * np.sin(2 * np.pi * 220 * t)).astype("float32")
    wavfile.write(os.path.join(paths.music_dir, "tone.wav"), sr, tone)

    assert wc.LISTEN in wd.LIVE_ALLOWED_SURFACE[wc.MUSIC]
    assert wc.INSPECT_AUDIO in wd.LIVE_ALLOWED_SURFACE[wc.MUSIC]

    raw_listen = {"resource_class": "music", "action": wc.LISTEN, "relative_path": "tone.wav", "content": ""}
    validated_listen, failure = wd.validate_pass1_workspace_action(raw_listen)
    assert failure is None
    boundary_listen, performed = wd.execute_workspace_action(paths, validated_listen, "clark-actor")
    assert performed is True
    assert boundary_listen["result"]["renderer"] == "AUDIO_SOURCE_V1"
    assert boundary_listen["result"]["channel_count"] == 1
    assert "waveform_envelope" in boundary_listen["result"]["available_views"]
    for forbidden in ("mood", "genre", "emotion"):
        assert forbidden not in json.dumps(boundary_listen["result"]).lower()

    raw_inspect = {
        "resource_class": "music", "action": wc.INSPECT_AUDIO, "relative_path": "tone.wav",
        "content": json.dumps({"view": "dynamics"}),
    }
    validated_inspect, failure = wd.validate_pass1_workspace_action(raw_inspect)
    assert failure is None
    boundary_inspect, performed2 = wd.execute_workspace_action(paths, validated_inspect, "clark-actor")
    assert performed2 is True
    assert boundary_inspect["result"]["renderer"] == "AUDIO_VIEW_V1"
    assert boundary_inspect["result"]["view"] == "dynamics"
    assert "rms_amplitude" in boundary_inspect["result"]

    # music remains read-only -- no new mutation path introduced.
    assert wc.check_permission(wc.MUSIC, wc.MODIFY)[0] is False
    assert wc.check_permission(wc.MUSIC, wc.DELETE)[0] is False


def test_inspect_audio_malformed_payload_fails_closed_via_typed_pathway():
    paths = fresh_paths("music_inspect_malformed")
    os.makedirs(paths.music_dir, exist_ok=True)
    with open(os.path.join(paths.music_dir, "tone.wav"), "wb") as f:
        f.write(b"not a wav")
    raw = {
        "resource_class": "music", "action": wc.INSPECT_AUDIO, "relative_path": "tone.wav",
        "content": "please show me the spectrum",
    }
    validated, failure = wd.validate_pass1_workspace_action(raw)
    assert failure is None  # structurally legal at the envelope layer
    boundary, performed = wd.execute_workspace_action(paths, validated, "clark-actor")
    assert performed is False
    assert boundary["result"] is None


def test_organization_reachable_through_live_typed_action_pathway():
    """CAP2A-A: model-free end-to-end proof that Clark's organization
    overlay is genuinely reachable through the SAME live typed-action
    pathway every other resource class uses -- raw model-shaped JSON ->
    validate_pass1_workspace_action() (host structural validation) ->
    execute_workspace_action() (the SAME dispatcher workspace_
    supervisor.py and workspace_roaming.py both call) -> observable
    organization state -- with zero model/network calls, and proves
    deletion/removal never touches the underlying resource."""
    import hashlib

    paths = fresh_paths("org_live_pathway")
    library_full = os.path.join(paths.library_dir, "book.txt")
    os.makedirs(paths.library_dir, exist_ok=True)
    with open(library_full, "wb") as f:
        f.write(b"once upon a time")
    original_digest = hashlib.sha256(open(library_full, "rb").read()).hexdigest()

    # 1. organization is a legal resource_class in the live surface --
    # the actual reachability claim CAP2 failed to establish.
    assert "organization" in wd.LIVE_ALLOWED_SURFACE
    assert wd.ORG_CREATE_COLLECTION in wd.LIVE_ALLOWED_SURFACE["organization"]

    # 2. typed Clark action -> host validation -> operation -> state:
    # create a container entirely through the raw JSON shape a real
    # Pass-1 response would take.
    raw_create = {"resource_class": "organization", "action": wd.ORG_CREATE_COLLECTION,
                  "relative_path": "", "content": "My Shelf"}
    validated_create, failure = wd.validate_pass1_workspace_action(raw_create)
    assert failure is None
    boundary_create, performed = wd.execute_workspace_action(paths, validated_create, "clark-actor")
    assert performed is True
    assert boundary_create["boundary"] == wd.ORGANIZATION_BOUNDARY_ID
    container_id = boundary_create["result"]["container_id"]
    assert boundary_create["result"]["container_type"] == "collection"
    assert boundary_create["result"]["name"] == "My Shelf"

    # 3. add a reference to the real library file, through the same pathway.
    raw_add = {"resource_class": "organization", "action": wd.ORG_ADD_REFERENCE_LIBRARY,
               "relative_path": container_id, "content": "book.txt"}
    validated_add, failure = wd.validate_pass1_workspace_action(raw_add)
    assert failure is None
    boundary_add, performed = wd.execute_workspace_action(paths, validated_add, "clark-actor")
    assert performed is True
    assert len(boundary_add["result"]["resource_references"]) == 1
    assert boundary_add["result"]["resource_references"][0]["resource_class"] == "library"
    assert boundary_add["result"]["resource_references"][0]["relative_path"] == "book.txt"

    # 4. inspect reflects the resulting organization state.
    raw_inspect = {"resource_class": "organization", "action": wd.ORG_INSPECT,
                   "relative_path": container_id, "content": ""}
    validated_inspect, _ = wd.validate_pass1_workspace_action(raw_inspect)
    boundary_inspect, performed = wd.execute_workspace_action(paths, validated_inspect, "clark-actor")
    assert performed is True
    assert boundary_inspect["result"]["resource_references"][0]["relative_path"] == "book.txt"

    # 5. remove the reference -- the source file must survive untouched.
    raw_remove = {"resource_class": "organization", "action": wd.ORG_REMOVE_REFERENCE_LIBRARY,
                  "relative_path": container_id, "content": "book.txt"}
    validated_remove, _ = wd.validate_pass1_workspace_action(raw_remove)
    boundary_remove, performed = wd.execute_workspace_action(paths, validated_remove, "clark-actor")
    assert performed is True
    assert boundary_remove["result"]["resource_references"] == []
    assert os.path.isfile(library_full)
    assert hashlib.sha256(open(library_full, "rb").read()).hexdigest() == original_digest

    # 6. delete the container -- source resource still untouched, and
    # the container itself is genuinely gone.
    raw_delete = {"resource_class": "organization", "action": wd.ORG_DELETE_CONTAINER,
                  "relative_path": container_id, "content": ""}
    validated_delete, _ = wd.validate_pass1_workspace_action(raw_delete)
    boundary_delete, performed = wd.execute_workspace_action(paths, validated_delete, "clark-actor")
    assert performed is True
    assert os.path.isfile(library_full)
    assert hashlib.sha256(open(library_full, "rb").read()).hexdigest() == original_digest
    raw_inspect_gone = {"resource_class": "organization", "action": wd.ORG_INSPECT,
                         "relative_path": container_id, "content": ""}
    validated_gone, _ = wd.validate_pass1_workspace_action(raw_inspect_gone)
    boundary_gone, performed_gone = wd.execute_workspace_action(paths, validated_gone, "clark-actor")
    assert performed_gone is False
    assert boundary_gone["result"] is None

    # 7. journal/library/music/photographs boundaries stay exactly as
    # they were -- organization introduces no new mutation path onto
    # any of them (still library=read-only, journal=append-only).
    assert wc.check_permission(wc.LIBRARY, wc.WRITE)[0] is False
    assert wc.check_permission(wc.JOURNAL, wc.WRITE)[0] is False


def test_organization_unknown_action_fails_closed_via_typed_pathway():
    raw = {"resource_class": "organization", "action": "delete_everything",
           "relative_path": "", "content": ""}
    validated, failure = wd.validate_pass1_workspace_action(raw)
    assert validated is None
    assert failure == wd.DirectionFailure.NOT_IN_ALLOWED_SURFACE


def test_organization_no_private_pathway_involvement():
    with open(os.path.join(ANAXI_FINAL, "workspace_organization.py"), encoding="utf-8") as f:
        source = f.read()
    assert "workspace_private" not in source
    assert "private" not in source.lower()


def test_supervisor_music_listen_records_audio_encounter_provenance():
    """CAP2F full-pipeline integration (fake ollama, real decode/
    provenance code): a LISTEN action's bounded neutral orientation
    must reach the final Pass-2 text, stay on the ordinary gemma4:e4b
    model (no vision-style routing for audio -- no model ever receives
    audio bytes), and record a genuine, source-grounded resource-
    encounter fact distinguishing the delivered representation's own
    digest from the original audio file's digest."""
    import numpy as np
    from scipy.io import wavfile
    import hashlib
    import sqlite3
    import provenance_schema
    import workspace_episode_provenance as wep

    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "music", "action": "listen", "relative_path": "tone.wav", "content": ""},
        pass2_value={"expression": "I encountered a music resource."},
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("supervisor_music_listen")
    os.makedirs(paths.music_dir, exist_ok=True)
    full = os.path.join(paths.music_dir, "tone.wav")
    sr = 22050
    t = np.linspace(0, 2.0, int(sr * 2.0), endpoint=False)
    tone = (0.5 * np.sin(2 * np.pi * 220 * t)).astype("float32")
    wavfile.write(full, sr, tone)
    with open(full, "rb") as f:
        original_bytes = f.read()
    original_sha256 = hashlib.sha256(original_bytes).hexdigest()

    db_path = os.path.join(la.PROVENANCE_DB_DIR, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-test-1', ?, 'llama', 'test')", (la.PIPELINE_KEY,),
    )
    host_actor_id = wep.host_actor_id()
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
        "VALUES (?, 'host_system', 'bounded_clause_renderer', 1000)", (host_actor_id,),
    )
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    conn.execute(
        "INSERT INTO model_revisions (model_revision_id, tag, identity_confidence, first_observed_at) "
        "VALUES ('fake-rev-pass2', 'fake-tag', 'tag_only_degraded', 1000)"
    )
    conn.commit()
    conn.close()

    result = ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), "Explore some music.", "clark-actor", paths)
    assert result["reply"] == "I encountered a music resource."

    pass2_call = [c for c in call_log if c["format"] != "json" and c["messages"][0]["content"] != "You are a helpful assistant."][0]
    assert pass2_call["model"] == "gemma4:e4b"  # no vision-style routing for audio
    assert "images" not in pass2_call["messages"][-1]

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT c.component_text FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type = 'workspace_resource_encounter' AND c.component_kind = 'resource_encounter_fact'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    fact = json.loads(rows[0][0])
    assert fact["modality"] == "audio"
    assert fact["resource_class"] == "music"
    assert fact["genuinely_delivered"] is True
    assert fact["target_model_pathway"] == "gemma4:e4b"
    assert fact["source_content_sha256"] == original_sha256
    assert fact["content_sha256"] != fact["source_content_sha256"]  # rendered dict digest, not raw file digest
    for forbidden in ("liked", "understood", "remembered", "valued"):
        assert forbidden not in json.dumps(fact).lower()


def test_supervisor_full_success_live_integration():
    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "journal", "action": "append", "relative_path": "", "content": "Testing the workspace."},
        pass2_value={"expression": "I wrote a short note in my journal."},
    )
    la._interaction_mode_state["mode"] = "conversation"  # simulate an active conversation-mode session
    paths = fresh_paths("supervisor_success")
    result = ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), "Go ahead and explore.", "clark-actor", paths)
    assert result["reply"] == "I wrote a short note in my journal."
    pass1_calls = sum(1 for c in call_log if c["format"] == "json")
    pass2_calls = sum(1 for c in call_log if c["format"] != "json")
    assert pass1_calls == 1 and pass2_calls == 1
    # CAP2E: a non-image action must remain on the ordinary waking model.
    pass2_call = [c for c in call_log if c["format"] != "json" and c["messages"][0]["content"] != "You are a helpful assistant."][0]
    assert pass2_call["model"] == "gemma4:e4b"
    assert len(persistence) == 1
    assert persistence[0]["clark_prose"] == "I wrote a short note in my journal."
    assert persistence[0]["prompt"] == "Go ahead and explore."
    entries = [f for f in os.listdir(paths.journal_dir) if f.endswith(".json")]
    assert len(entries) == 1
    trace = ws.wd.get_last_workspace_direction_trace()
    assert trace["action"] == "append"
    assert trace["resource_class"] == "journal"


def test_post_action_staging_failure_is_not_exposed_as_retry_safe_staging_error():
    from native_turn_staging import StagingDurabilityError

    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "journal", "action": "append", "relative_path": "", "content": "One occurrence."},
        pass2_value={"expression": "I wrote one occurrence."},
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("post_action_staging_failure")

    def fail_after_action(*args, **kwargs):
        raise StagingDurabilityError("synthetic post-action staging failure")

    ws.native_provenance_writer.stage_and_record_native_waking_turn = fail_after_action
    raised = None
    try:
        ws.run_one_supervised_workspace_action(
            la.AnaxiOrchestrator(), "Write this once.", "clark-actor", paths,
            human_input_event_id="canonical-h-for-post-action-failure",
        )
    except Exception as exc:  # exact type asserted below
        raised = exc
    assert type(raised) is ws.WorkspacePostActionPersistenceFailure
    assert isinstance(raised.__cause__, StagingDurabilityError)
    assert len([f for f in os.listdir(paths.journal_dir) if f.endswith(".json")]) == 1
    assert persistence == []


def test_supervisor_photograph_view_delivers_real_pixels_and_records_encounter():
    """CAP2-B/E full-pipeline integration (fake ollama, real budget/
    provenance code): a VIEW action's actual image bytes must reach the
    final Pass-2 message sent to the model, must never appear in the
    boundary_result/trace, and a genuine resource-encounter fact must
    be recorded only after that call succeeds."""
    from PIL import Image
    import hashlib
    import sqlite3
    import provenance_schema
    import workspace_episode_provenance as wep

    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "photographs", "action": "view", "relative_path": "photo.png", "content": ""},
        pass2_value={"expression": "I looked at a photograph."},
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("supervisor_photo_view")
    full = os.path.join(paths.photographs_dir, "photo.png")
    os.makedirs(paths.photographs_dir, exist_ok=True)
    Image.new("RGB", (32, 32), color=(10, 20, 30)).save(full)
    with open(full, "rb") as f:
        expected_bytes = f.read()
    expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()

    # provenance DB must exist with real schema + a resolvable pipeline
    # before a real resource-encounter write -- mirrors
    # test_workspace_episode_provenance.py's own fresh_db() pattern.
    db_path = os.path.join(la.PROVENANCE_DB_DIR, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-test-1', ?, 'llama', 'test')",
        (la.PIPELINE_KEY,),
    )
    host_actor_id = wep.host_actor_id()
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
        "VALUES (?, 'host_system', 'bounded_clause_renderer', 1000)",
        (host_actor_id,),
    )
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    # matches install_fake_heavy_dependencies()'s fixed fake model_revision_id
    conn.execute(
        "INSERT INTO model_revisions (model_revision_id, tag, identity_confidence, first_observed_at) "
        "VALUES ('fake-rev-pass2', 'fake-tag', 'tag_only_degraded', 1000)"
    )
    conn.commit()
    conn.close()

    result = ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), "Look at a photo.", "clark-actor", paths)
    assert result["reply"] == "I looked at a photograph."

    pass2_call = [c for c in call_log if c["format"] != "json" and c["messages"][0]["content"] != "You are a helpful assistant."][0]
    last_message = pass2_call["messages"][-1]
    assert last_message.get("images") == [expected_bytes]
    # CAP2E: a genuine admitted photograph must route this expression
    # call to the vision substrate, not the ordinary waking model.
    assert pass2_call["model"] == "qwen3-vl:4b"

    trace = ws.wd.get_last_workspace_direction_trace()
    assert trace["action"] == "view" and trace["resource_class"] == "photographs"
    assert "image_bytes" not in json.dumps(trace["boundary_result"])

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT c.component_text FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type = 'workspace_resource_encounter' AND c.component_kind = 'resource_encounter_fact'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    fact = json.loads(rows[0][0])
    assert fact["modality"] == "image"
    assert fact["content_sha256"] == expected_sha256
    assert fact["genuinely_delivered"] is True
    assert fact["resource_class"] == "photographs"
    assert fact["relative_path"] == "photo.png"
    assert fact["target_model_pathway"] == "qwen3-vl:4b"
    # CAP2E-P2 provenance semantics audit: a small (32x32, well under
    # the 1024px bound) photograph is never resized, so representation
    # == source and both digests are numerically EQUAL -- but they
    # remain two SEPARATE fields with distinct documented meanings
    # (content_sha256 = delivered; source_content_sha256 = original
    # resource identity), never collapsed into one even when equal.
    assert fact["source_content_sha256"] == expected_sha256
    assert fact["content_sha256"] == fact["source_content_sha256"]


@pytest.mark.skipif(sys.platform != "darwin", reason="production page renderer uses macOS PDFKit")
def test_supervisor_pdf_page_delivers_pixels_to_established_vision_pathway():
    """A scanned-page selection crosses the ordinary supervisor boundary.

    The renderer test covers PDFKit itself; this test proves that its genuine
    pixels reach the vision model call and durable encounter provenance rather
    than stopping at a direct helper or metadata-only result.
    """
    import hashlib
    import sqlite3
    import provenance_schema
    import workspace_episode_provenance as wep

    la, ws, call_log, persistence = fresh_modules(
        pass1_value={
            "resource_class": "library", "action": "view_page",
            "relative_path": "scanned.pdf", "content": '{"page":1}',
        },
        pass2_value={"expression": "I viewed the scanned page."},
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("supervisor_pdf_page_view")
    pdf_path = os.path.join(paths.library_dir, "scanned.pdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with open(pdf_path, "wb") as handle:
        writer.write(handle)
    with open(pdf_path, "rb") as handle:
        source_sha256 = hashlib.sha256(handle.read()).hexdigest()

    db_path = os.path.join(la.PROVENANCE_DB_DIR, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-test-pdf', ?, 'llama', 'test')",
        (la.PIPELINE_KEY,),
    )
    host_actor_id = wep.host_actor_id()
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
        "VALUES (?, 'host_system', 'bounded_clause_renderer', 1000)",
        (host_actor_id,),
    )
    conn.execute(
        "INSERT INTO actor_host_system (actor_id, subsystem_key) "
        "VALUES (?, 'bounded_clause_renderer')", (host_actor_id,),
    )
    conn.execute(
        "INSERT INTO model_revisions (model_revision_id, tag, identity_confidence, first_observed_at) "
        "VALUES ('fake-rev-pass2', 'fake-tag', 'tag_only_degraded', 1000)"
    )
    conn.commit()
    conn.close()

    result = ws.run_one_supervised_workspace_action(
        la.AnaxiOrchestrator(), "View the first page.", "clark-actor", paths,
    )
    assert result["reply"] == "I viewed the scanned page."
    pass2_call = [call for call in call_log if call["format"] != "json"][0]
    delivered = pass2_call["messages"][-1]["images"][0]
    assert delivered.startswith(b"\x89PNG")
    assert pass2_call["model"] == "qwen3-vl:4b"

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT c.component_text FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type = 'workspace_resource_encounter' "
            "AND c.component_kind = 'resource_encounter_fact'"
        ).fetchone()
    finally:
        conn.close()
    fact = json.loads(row[0])
    assert fact["resource_class"] == "library"
    assert fact["modality"] == "image"
    assert fact["source_content_sha256"] == source_sha256
    assert fact["content_sha256"] == hashlib.sha256(delivered).hexdigest()
    assert fact["genuinely_delivered"] is True
    assert fact["target_model_pathway"] == "qwen3-vl:4b"
    assert "PDF page 1 of 1" in fact["delivered_portion"]


def test_supervisor_photograph_view_large_photo_resized_before_qwen_delivery():
    """CAP2E-P1 full-pipeline integration: a large (2048x2048) admitted
    photograph must be resized to <=1024 on both axes BEFORE being
    attached to the final Qwen request and BEFORE its cost is computed
    -- the bytes actually sent must differ from the original file, and
    provenance must record BOTH the delivered representation's own
    digest (content_sha256) and the original source's own, separate
    digest (source_content_sha256)."""
    from PIL import Image
    import hashlib
    import sqlite3
    import provenance_schema
    import workspace_episode_provenance as wep

    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "photographs", "action": "view", "relative_path": "big.png", "content": ""},
        pass2_value={"expression": "I see a large photograph."},
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("supervisor_photo_resize")
    full = os.path.join(paths.photographs_dir, "big.png")
    os.makedirs(paths.photographs_dir, exist_ok=True)
    Image.new("RGB", (2048, 2048), color=(9, 9, 9)).save(full)
    with open(full, "rb") as f:
        original_bytes = f.read()
    original_sha256 = hashlib.sha256(original_bytes).hexdigest()

    db_path = os.path.join(la.PROVENANCE_DB_DIR, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-test-1', ?, 'llama', 'test')", (la.PIPELINE_KEY,),
    )
    host_actor_id = wep.host_actor_id()
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
        "VALUES (?, 'host_system', 'bounded_clause_renderer', 1000)", (host_actor_id,),
    )
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    conn.execute(
        "INSERT INTO model_revisions (model_revision_id, tag, identity_confidence, first_observed_at) "
        "VALUES ('fake-rev-pass2', 'fake-tag', 'tag_only_degraded', 1000)"
    )
    conn.commit()
    conn.close()

    result = ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), "Look at a photo.", "clark-actor", paths)
    assert result["reply"] == "I see a large photograph."

    pass2_call = [c for c in call_log if c["format"] != "json" and c["messages"][0]["content"] != "You are a helpful assistant."][0]
    delivered_bytes = pass2_call["messages"][-1]["images"][0]
    assert delivered_bytes != original_bytes  # genuinely resized, not the original
    from PIL import Image as PILImage
    import io
    with PILImage.open(io.BytesIO(delivered_bytes)) as img:
        assert img.size == (1024, 1024)

    # Original source file on disk must be completely untouched.
    with open(full, "rb") as f:
        assert f.read() == original_bytes

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT c.component_text FROM events e JOIN event_components c ON c.event_id = e.event_id "
            "WHERE e.event_type = 'workspace_resource_encounter' AND c.component_kind = 'resource_encounter_fact'"
        ).fetchall()
    finally:
        conn.close()
    fact = json.loads(rows[0][0])
    assert fact["content_sha256"] == hashlib.sha256(delivered_bytes).hexdigest()
    assert fact["source_content_sha256"] == original_sha256
    assert fact["content_sha256"] != fact["source_content_sha256"]
    assert "1024x1024" in fact["delivered_portion"]


def test_supervisor_corrupt_photo_fails_closed_no_encounter_no_inference():
    """CAP2E-P1 section 5F: a corrupt/invalid image is DENIED by
    wc.deliver_photograph_bytes() itself, before derive_bounded_
    vision_representation() or any vision model call is ever reached.
    Pass-2 still runs (narrating the denial, exactly like any other
    denied WSP1 action) but on the ordinary gemma4:e4b model, with zero
    images attached, and zero resource-encounter fact recorded --
    _performed is False, and the encounter recorder's own precondition
    (`... and _performed`) never fires for a denied action."""
    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "photographs", "action": "view", "relative_path": "corrupt.png", "content": ""},
        pass2_value={"expression": "That image could not be read."},
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("qwen_corrupt_image")
    os.makedirs(paths.photographs_dir, exist_ok=True)
    with open(os.path.join(paths.photographs_dir, "corrupt.png"), "wb") as f:
        f.write(b"not actually a png despite the extension")

    result = ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), "Look at a photo.", "clark-actor", paths)
    assert result["reply"] == "That image could not be read."

    # Denied -> Pass-2 narrates it on the ORDINARY model, never Qwen,
    # since no genuine image was ever admitted.
    pass2_calls = [c for c in call_log if c["format"] != "json" and c["messages"][0]["content"] != "You are a helpful assistant."]
    assert len(pass2_calls) == 1
    assert pass2_calls[0]["model"] == "gemma4:e4b"
    assert "images" not in pass2_calls[0]["messages"][-1]

    trace = ws.wd.get_last_workspace_direction_trace()
    assert trace["boundary_result"]["result"] is None  # denied, nothing delivered
    assert trace["pass1_status"] == "ok" and trace["pass2_status"] == "ok"


def test_qwen_route_hard_overflow_zero_inference_no_gemma_fallback():
    """CAP2E/CAP2E-P1 failure containment: a turn whose HARD cost
    (resized-image admission + oversized human message) exceeds
    QWEN_VISION_MAX_PROMPT_BUDGET must fail the whole turn closed --
    zero pass-2 model calls at all, on EITHER substrate -- never a
    silent downgrade to gemma4:e4b pretending vision occurred.

    CAP2E-P1 note: a 2048x2048 (or any) source image ALONE can no
    longer trigger this by itself -- derive_bounded_vision_
    representation() always resizes down to <=1024 on both axes before
    costing, and 1024x1024's own admission cost (the measured plateau,
    ~1100) comfortably fits QWEN_VISION_MAX_PROMPT_BUDGET on its own.
    This is the INTENDED effect of that patch (an ordinary large
    photograph must no longer be rejected merely for being large), so
    this test instead combines a real, resized-and-admitted photograph
    with an oversized human message to genuinely exceed the budget --
    still proving the same containment property (hard overflow -> zero
    inference, no fallback), just via a combination CAP2E-P1 did not
    eliminate and should never eliminate (an oversized message is a
    real overflow condition on any substrate)."""
    from PIL import Image

    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "photographs", "action": "view", "relative_path": "big.png", "content": ""},
        pass2_value={"expression": "unreachable"},
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("qwen_overflow")
    os.makedirs(paths.photographs_dir, exist_ok=True)
    Image.new("RGB", (2048, 2048), color=(1, 2, 3)).save(os.path.join(paths.photographs_dir, "big.png"))

    # Sized to fit Pass-1's own budget (no image cost there) but tip
    # Pass-2's QWEN_VISION_MAX_PROMPT_BUDGET over once the resized
    # image's own ~1100-token admission cost is added on top --
    # verified empirically against this exact fake harness's own fixed
    # core/task/framing text sizes. (Shared-expression-seam repair: the
    # workspace narration instruction lost its 160-byte retired-marker
    # sentence and the Pass-1 menu grew, so this harness no longer has a
    # message size that overflows only the vision Pass 2; the vision budget
    # is tightened by exactly those 160 bytes instead, which is the same
    # hard-overflow condition the containment law is about.)
    oversized_prompt = "x" * 410
    import context_budget as _cb
    _saved_vision_budget = _cb.QWEN_VISION_MAX_PROMPT_BUDGET
    _cb.QWEN_VISION_MAX_PROMPT_BUDGET = _saved_vision_budget - 160
    raised = None
    try:
        ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), oversized_prompt, "clark-actor", paths)
    except ws.wd.WorkspaceDirectionFailure as exc:
        raised = exc
    finally:
        _cb.QWEN_VISION_MAX_PROMPT_BUDGET = _saved_vision_budget
    assert raised is not None
    assert raised.stage == "pass2"
    assert raised.failure_code == __import__("context_budget").BUDGET_EXCEEDED
    # No GENERATION on any substrate. The bounded num_predict=1 cost probes
    # ordinary waking already uses to recover an overcounted human message are
    # measurements whose output is discarded, not inference.
    pass2_calls = [
        c for c in call_log
        if c["format"] != "json"
        and c["messages"][0]["content"] != la._REAL_TEXT_COST_PROBE_BASELINE
    ]
    assert pass2_calls == []
    assert persistence == []  # no persisted turn, no false encounter


def test_qwen_unavailable_fails_closed_no_silent_gemma_fallback():
    """CAP2E failure containment: if the vision substrate itself raises
    (unavailable/errored), that failure must propagate uncaught -- never
    silently retried against gemma4:e4b as a fallback that would falsely
    imply vision occurred."""
    from PIL import Image

    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "photographs", "action": "view", "relative_path": "photo.png", "content": ""},
        pass2_value={"expression": "unreachable"},
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("qwen_unavailable")
    os.makedirs(paths.photographs_dir, exist_ok=True)
    Image.new("RGB", (32, 32), color=(4, 5, 6)).save(os.path.join(paths.photographs_dir, "photo.png"))

    real_chat = sys.modules["ollama"].chat

    def raising_chat(model, messages, format=None, options=None, think=None):
        if model == "qwen3-vl:4b":
            raise RuntimeError("vision substrate unavailable (simulated)")
        return real_chat(model, messages, format=format, options=options, think=think)

    sys.modules["ollama"].chat = raising_chat

    raised = None
    try:
        ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), "Look at a photo.", "clark-actor", paths)
    except RuntimeError as exc:
        raised = exc
    assert raised is not None
    assert "vision substrate unavailable" in str(raised)
    # The qwen3-vl:4b call raised before ever reaching call_log's own
    # append point -- no successful pass-2 call was logged on ANY
    # model, proving there was no fallback retry against gemma4:e4b.
    pass2_models = [
        c["model"] for c in call_log
        if c["format"] != "json" and c["messages"][0]["content"] != "You are a helpful assistant."  # not cost probes
    ]
    assert pass2_models == []
    assert persistence == []


# ================================= WSP2-MA1: ordinary action legal-shape matrix


def test_ma1_minimal_legal_shapes_accepted_per_action():
    cases = [
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
        {"resource_class": "library", "action": "inspect_metadata", "relative_path": "book.pdf", "content": ""},
        {"resource_class": "library", "action": "read", "relative_path": "book.pdf", "content": ""},
        {"resource_class": "journal", "action": "list", "relative_path": "", "content": ""},
        {"resource_class": "journal", "action": "read", "relative_path": "e1", "content": ""},
        {"resource_class": "journal", "action": "append", "relative_path": "", "content": "text"},
        {"resource_class": "music", "action": "list", "relative_path": "", "content": ""},
        {"resource_class": "music", "action": "inspect_metadata", "relative_path": "song.mp3", "content": ""},
        {"resource_class": "photographs", "action": "list", "relative_path": "", "content": ""},
        {"resource_class": "photographs", "action": "inspect_metadata", "relative_path": "p.jpg", "content": ""},
        {"resource_class": "photographs", "action": "view", "relative_path": "p.jpg", "content": ""},
    ]
    for raw in cases:
        validated, failure = wd.validate_pass1_workspace_action(raw)
        assert failure is None, raw
        assert validated == raw


def test_ma1_all_four_fields_required():
    base = {"resource_class": "library", "action": "list", "relative_path": "", "content": ""}
    for missing in ("resource_class", "action", "relative_path"):
        raw = dict(base)
        del raw[missing]
        _, failure = wd.validate_pass1_workspace_action(raw)
        assert failure == wd.DirectionFailure.MALFORMED_ACTION, missing
    # ``content`` alone may be absent, and only where it carries nothing (real-model finding,
    # 2026-09-21: a read/list naming its target in relative_path omits it). An append needs text.
    for action_name in ("list", "read", "inspect_metadata"):
        validated, failure = wd.validate_pass1_workspace_action(
            {"resource_class": "library", "action": action_name, "relative_path": "x"})
        assert failure is None and validated["content"] == ""
    # Content-bearing actions never gain validity from an absent ``content``.
    for resource_class, action_name in (
        ("journal", "append"), ("library", "view_page"), ("music", "inspect_audio"),
        ("organization", "create_collection"), ("organization", "rename"),
    ):
        _, failure = wd.validate_pass1_workspace_action(
            {"resource_class": resource_class, "action": action_name, "relative_path": ""})
        assert failure == wd.DirectionFailure.MALFORMED_ACTION, action_name


def test_ma1_forbidden_extra_field_rejected():
    raw = {"resource_class": "library", "action": "list", "relative_path": "", "content": "", "destination_relative_path": ""}
    _, failure = wd.validate_pass1_workspace_action(raw)
    assert failure == wd.DirectionFailure.PROTOCOL_LEAKAGE


def test_ma1_wrong_types_rejected():
    base = {"resource_class": "library", "action": "list", "relative_path": "", "content": ""}
    for field, bad_value, expected in (
        ("relative_path", 5, wd.DirectionFailure.INVALID_RELATIVE_PATH),
        ("relative_path", None, wd.DirectionFailure.INVALID_RELATIVE_PATH),
        ("content", 5, wd.DirectionFailure.INVALID_CONTENT),
        ("content", None, wd.DirectionFailure.INVALID_CONTENT),
    ):
        raw = dict(base)
        raw[field] = bad_value
        _, failure = wd.validate_pass1_workspace_action(raw)
        assert failure == expected, (field, bad_value)


def test_ma1_invalid_discriminator_rejected():
    _, failure = wd.validate_pass1_workspace_action(
        {"resource_class": "not_a_real_class", "action": "list", "relative_path": "", "content": ""}
    )
    assert failure == wd.DirectionFailure.UNKNOWN_RESOURCE_CLASS

    _, failure = wd.validate_pass1_workspace_action(
        {"resource_class": "library", "action": "delete", "relative_path": "", "content": ""}
    )
    assert failure == wd.DirectionFailure.NOT_IN_ALLOWED_SURFACE


def test_ma1_null_vs_absent_both_rejected_matching_written_contract():
    # The written contract (PASS1_TASK_INSTRUCTION, post-repair) says
    # relative_path/content are REQUIRED KEYS, empty string when
    # inapplicable -- never null, never absent. Both violations are
    # rejected, via the correct distinct codes.
    absent = {"resource_class": "library", "action": "list", "content": ""}  # relative_path absent
    _, failure = wd.validate_pass1_workspace_action(absent)
    assert failure == wd.DirectionFailure.MALFORMED_ACTION

    null_value = {"resource_class": "library", "action": "list", "relative_path": None, "content": ""}
    _, failure = wd.validate_pass1_workspace_action(null_value)
    assert failure == wd.DirectionFailure.INVALID_RELATIVE_PATH


def test_ma1_model_facing_instruction_states_all_fields_required_and_empty_string_rule():
    # Tests the ACTUAL rendered task_instruction content, not merely
    # that some words appear anywhere in the repository.
    text = wd.PASS1_TASK_INSTRUCTION
    assert "resource_class, action, relative_path, and content are all" in text
    assert "required fields" in text
    assert "empty string" in text
    assert "does not need it" in text


def test_ma1_model_facing_instruction_mirrors_private_contract_clause_pattern():
    import workspace_private as wpriv
    # Both sibling control contracts now state the identical structural
    # rule ("all required fields ... empty string" when inapplicable),
    # closing the asymmetry this gate's forensic identified.
    assert "required fields" in wd.PASS1_TASK_INSTRUCTION
    assert "required fields" in wpriv.PRIVATE_TASK_INSTRUCTION
    assert "empty string" in wd.PASS1_TASK_INSTRUCTION
    assert "empty string" in wpriv.PRIVATE_TASK_INSTRUCTION


def test_ma1_rendered_roaming_action_prompt_includes_the_clarified_instruction():
    # The instruction change must actually reach the rendered roaming
    # control-call prompt, not merely exist as an unused constant.
    import workspace_roaming as wr
    messages = wr.build_workspace_action_messages()
    task_text = messages[-1]["content"]
    assert "required fields" in task_text
    assert "empty string" in task_text


# ==================================== WSP2-MA2: Stage-2 structural schema


def test_ma2_schema_shape_object_with_exact_required_keys():
    schema = wd.STAGE2_ACTION_SCHEMA
    assert schema["type"] == "object"
    assert set(schema["required"]) == wd.RAW_ACTION_ALLOWED_FIELDS
    assert set(schema["properties"].keys()) == wd.RAW_ACTION_ALLOWED_FIELDS


def test_ma2_schema_all_values_typed_string():
    schema = wd.STAGE2_ACTION_SCHEMA
    for field, spec in schema["properties"].items():
        assert spec == {"type": "string"}, field


def test_ma2_schema_forbids_additional_properties():
    assert wd.STAGE2_ACTION_SCHEMA["additionalProperties"] is False


def test_ma2_schema_no_semantic_policy_encoded():
    # Section 5: resource_class/action legality, path containment, and
    # journal-only content semantics stay OUT of the schema -- only
    # mechanical object shape.
    schema = wd.STAGE2_ACTION_SCHEMA
    for spec in schema["properties"].values():
        assert set(spec.keys()) == {"type"}  # no enum, no pattern, no format


def test_ma2_schema_built_from_same_constant_as_validator_never_drifts():
    assert set(wd.STAGE2_ACTION_SCHEMA["required"]) == wd.RAW_ACTION_ALLOWED_FIELDS


def test_ma2_host_validator_still_authoritative_legal_object():
    validated, failure = wd.validate_pass1_workspace_action(
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""}
    )
    assert failure is None


def test_ma2_host_validator_still_authoritative_semantically_illegal_object():
    # Structurally legal under the schema (all 4 string keys present),
    # but semantically illegal per the validator's own allowed-surface
    # check.
    validated, failure = wd.validate_pass1_workspace_action(
        {"resource_class": "library", "action": "delete", "relative_path": "", "content": ""}
    )
    assert failure == wd.DirectionFailure.NOT_IN_ALLOWED_SURFACE


def test_ma2_no_normalization_source_audit():
    with open(os.path.join(ANAXI_FINAL, "workspace_direction.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("setdefault(", "coerce", "or \"\""):
        assert forbidden not in source


# ==================================== OWC9-P3 Part B: WSP1 aggregate budget


def _fake_hippocampal_item(i, content, occurred_at=1700000000):
    return hippocampus_retrieval.RetrievedMemory(
        item_id=f"item-{i}", event_id=f"hippo-ev-{i}", occurred_at=occurred_at + i,
        source_store="hippocampus", source_locator=f"loc-{i}", memory_kind="episodic",
        attribution_status="attributed", authentication_status="verified",
        creator_actor_id=None, creator_actor_type=None, pipeline_id=None,
        session_id=None, session_resolution="unresolved", component_kind=None,
        content=content, content_truncated=False, retrieval_method="fts",
        bm25_score=-1.0 - i, query_terms=("test",),
    )


def test_wsp1_no_retrieval_normal_path():
    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "journal", "action": "list", "relative_path": "", "content": ""},
        pass2_value={"expression": "Nothing new in the journal."},
        legacy_retrieval_text="",
        hippocampal_retrieval_result=hippocampus_retrieval.RetrievalResult(query_terms=(), items=()),
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("wsp1_no_retrieval")
    result = ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), "Anything new in your journal?", CLARK_ACTOR_ID, paths)
    assert result["reply"] == "Nothing new in the journal."
    for call in call_log:
        sent_text = json.dumps(call["messages"])
        assert "LEGACY_MEMORY_CONTEXT_V1" not in sent_text
        assert hippocampus_retrieval.CONTEXT_HEADING not in sent_text


def test_wsp1_huge_retrieval_trimmed_reserve_preserved():
    huge_legacy = "z" * 11000
    hippocampal_items = tuple(_fake_hippocampal_item(i, "modest hippocampal content " * 5) for i in range(4))
    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "journal", "action": "list", "relative_path": "", "content": ""},
        pass2_value={"expression": "Nothing new in the journal."},
        legacy_retrieval_text=huge_legacy,
        hippocampal_retrieval_result=hippocampus_retrieval.RetrievalResult(query_terms=("test",), items=hippocampal_items),
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("wsp1_huge_retrieval")
    utd.start_model_call_tracking()
    result = ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), "Anything new in your journal?", CLARK_ACTOR_ID, paths)
    budget_results = utd.get_and_clear_context_budget_results()
    assert result["reply"] == "Nothing new in the journal."

    for pass_name, budget in (("wsp1_pass1", context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET),
                               ("wsp1_pass2", context_budget.WSP1_PASS2_MAX_PROMPT_BUDGET)):
        diag = next(r for r in budget_results if r["pass"] == pass_name)
        assert diag["fits"] is True
        assert diag["final_prompt_cost"] <= budget
        assert context_budget.CORE_SYSTEM_CONTROL in diag["kinds_included"]
        assert context_budget.CURRENT_HUMAN_MESSAGE in diag["kinds_included"]

    for call in call_log:
        sent_text = json.dumps(call["messages"])
        assert huge_legacy not in sent_text  # never sent unbounded/verbatim


def test_wsp1_hard_overflow_budget_exceeded_zero_model_calls():
    huge_core = "q" * 5000  # alone exceeds WSP1_PASS1_MAX_PROMPT_BUDGET
    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "journal", "action": "list", "relative_path": "", "content": ""},
        pass2_value={"expression": "unreachable"},
        core_system_text=huge_core,
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("wsp1_hard_overflow")
    raised = None
    try:
        ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), "Go ahead and explore.", CLARK_ACTOR_ID, paths)
    except ws.wd.WorkspaceDirectionFailure as exc:  # ws.wd: the SAME reloaded module fresh_modules() gave workspace_supervisor.py itself
        raised = exc
    assert raised is not None
    assert raised.stage == "pass1"
    assert raised.failure_code == context_budget.BUDGET_EXCEEDED
    assert call_log == []  # zero model calls of any kind
    assert persistence == []


def test_wsp1_hippocampal_disclaimer_present_when_survives_absent_when_trimmed():
    one_item = (_fake_hippocampal_item(0, "a short, genuine memory item"),)
    la, ws, call_log, persistence = fresh_modules(
        pass1_value={"resource_class": "journal", "action": "list", "relative_path": "", "content": ""},
        pass2_value={"expression": "ok"},
        hippocampal_retrieval_result=hippocampus_retrieval.RetrievalResult(query_terms=("x",), items=one_item),
    )
    la._interaction_mode_state["mode"] = "conversation"
    paths = fresh_paths("wsp1_disclaimer_present")
    ws.run_one_supervised_workspace_action(la.AnaxiOrchestrator(), "Anything new?", CLARK_ACTOR_ID, paths)
    sent_text = json.dumps(call_log)
    assert hippocampus_retrieval.CONTEXT_HEADING in sent_text
    assert "a short, genuine memory item" in sent_text

    huge_legacy = "w" * 11000
    many_items = tuple(_fake_hippocampal_item(i, "hippocampal filler content " * 20) for i in range(6))
    la2, ws2, call_log2, persistence2 = fresh_modules(
        pass1_value={"resource_class": "journal", "action": "list", "relative_path": "", "content": ""},
        pass2_value={"expression": "ok"},
        legacy_retrieval_text=huge_legacy,
        hippocampal_retrieval_result=hippocampus_retrieval.RetrievalResult(query_terms=("x",), items=many_items),
    )
    la2._interaction_mode_state["mode"] = "conversation"
    paths2 = fresh_paths("wsp1_disclaimer_trimmed")
    utd.start_model_call_tracking()
    ws2.run_one_supervised_workspace_action(la2.AnaxiOrchestrator(), "Anything new?", CLARK_ACTOR_ID, paths2)
    budget_results2 = utd.get_and_clear_context_budget_results()
    # call_log2[0] is Pass-1's own call (format="json"), call_log2[1] is
    # Pass-2's -- each pass composes independently, so a kind dropped
    # under Pass-1's tighter budget may still survive Pass-2's own
    # (potentially different) composition; check each pass against its
    # OWN sent messages, never the two calls conflated together.
    pass1_call2 = next(c for c in call_log2 if c["format"] == "json")
    pass2_call2 = next(c for c in call_log2 if c["format"] != "json")
    diag1_2 = next(r for r in budget_results2 if r["pass"] == "wsp1_pass1")
    diag2_2 = next(r for r in budget_results2 if r["pass"] == "wsp1_pass2")
    if context_budget.RETRIEVED_HISTORY not in diag1_2["kinds_included"]:
        assert hippocampus_retrieval.CONTEXT_HEADING not in json.dumps(pass1_call2["messages"])
    if context_budget.RETRIEVED_HISTORY not in diag2_2["kinds_included"]:
        assert hippocampus_retrieval.CONTEXT_HEADING not in json.dumps(pass2_call2["messages"])


ALL_TESTS = [
    test_pass1_call_count_one_on_success,
    test_pass1_malformed_no_execution_no_pass2,
    test_pass2_malformed_action_result_remains_logged_no_retry,
    test_library_list_executes_once,
    test_library_bounded_read_exact_content,
    test_journal_append_exact_content_once,
    test_denied_action_correct_boundary_consequence_rationale_no_mutation,
    test_unknown_action_and_resource_fail_closed,
    test_traversal_attempt_fails_closed,
    test_host_execution_failure_not_fabricated_success,
    test_no_hippocampal_or_kardia_touch_in_pathway_module,
    test_api1_api2_source_hashes_unchanged,
    test_no_network_imports_or_calls,
    test_no_audio_vision_perception_claims,
    test_persistent_identifiers_no_absolute_windows_path,
    test_supervisor_refuses_outside_conversation_mode,
    test_owc_sources_untouched_by_workspace_modules,
    test_music_listen_and_inspect_audio_reachable_through_live_typed_pathway,
    test_inspect_audio_malformed_payload_fails_closed_via_typed_pathway,
    test_organization_reachable_through_live_typed_action_pathway,
    test_organization_unknown_action_fails_closed_via_typed_pathway,
    test_organization_no_private_pathway_involvement,
    test_supervisor_music_listen_records_audio_encounter_provenance,
    test_supervisor_full_success_live_integration,
    test_post_action_staging_failure_is_not_exposed_as_retry_safe_staging_error,
    test_supervisor_photograph_view_delivers_real_pixels_and_records_encounter,
    test_supervisor_photograph_view_large_photo_resized_before_qwen_delivery,
    test_supervisor_corrupt_photo_fails_closed_no_encounter_no_inference,
    test_qwen_route_hard_overflow_zero_inference_no_gemma_fallback,
    test_qwen_unavailable_fails_closed_no_silent_gemma_fallback,
    # OWC9-P3 Part B
    test_wsp1_no_retrieval_normal_path,
    test_wsp1_huge_retrieval_trimmed_reserve_preserved,
    test_wsp1_hard_overflow_budget_exceeded_zero_model_calls,
    test_wsp1_hippocampal_disclaimer_present_when_survives_absent_when_trimmed,
    # WSP2-MA1
    test_ma1_minimal_legal_shapes_accepted_per_action,
    test_ma1_all_four_fields_required,
    test_ma1_forbidden_extra_field_rejected,
    test_ma1_wrong_types_rejected,
    test_ma1_invalid_discriminator_rejected,
    test_ma1_null_vs_absent_both_rejected_matching_written_contract,
    test_ma1_model_facing_instruction_states_all_fields_required_and_empty_string_rule,
    test_ma1_model_facing_instruction_mirrors_private_contract_clause_pattern,
    test_ma1_rendered_roaming_action_prompt_includes_the_clarified_instruction,
    # WSP2-MA2
    test_ma2_schema_shape_object_with_exact_required_keys,
    test_ma2_schema_all_values_typed_string,
    test_ma2_schema_forbids_additional_properties,
    test_ma2_schema_no_semantic_policy_encoded,
    test_ma2_schema_built_from_same_constant_as_validator_never_drifts,
    test_ma2_host_validator_still_authoritative_legal_object,
    test_ma2_host_validator_still_authoritative_semantically_illegal_object,
    test_ma2_no_normalization_source_audit,
]


def main():
    passed, failed = 0, 0
    failures = []
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            tb = traceback.format_exc()
            failures.append((t.__name__, str(exc), tb))
            print(f"FAIL {t.__name__}: {exc}")
    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
