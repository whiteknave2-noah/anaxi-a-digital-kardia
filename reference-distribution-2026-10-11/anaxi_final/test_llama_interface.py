"""Collection-safe structural tests for the GUI interface layer.

The original module imported ``llama_gui`` at collection time (which reads
``sys.argv`` and exits under pytest arguments) and two of its tests made real
Ollama model calls against a legacy ``nate`` relational identity.  This module
keeps the still-valid structural invariants and inspects ``llama_gui.py`` and
``llama_anaxi.py`` through ``ast`` so collection has no import side effects,
opens no database, and touches no model.

Disposition of the retired model-calling tests (recorded in
``completion_evidence/legacy_test_dispositions.json``): the GUI/CLI
relational-event parity and cross-thread respond() checks required a live
model and the legacy ``nate`` identity.  Their modern, offline replacements are
in ``test_llama_gui_conversation_mode.py`` (respond() through the faked model
boundary, shared backend, contained failures) and
``test_native_waking_turn_end_to_end.py``.  Genuine live model behaviour is a
live-only requirement in the ledger, not a collected test.
"""

import ast
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _function(module_file: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((HERE / module_file).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{module_file} has no top-level function {name!r}")


def _called_names(function: ast.FunctionDef) -> set[str]:
    names = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def test_gui_respond_has_no_constitutional_logic_of_its_own():
    respond = _function("llama_gui.py", "respond")
    calls = _called_names(respond)
    assert "run_owner_conversation_turn" in calls, (
        "respond() must delegate to the shared owner-conversation service"
    )
    runtime = _function("llama_gui.py", "_owner_conversation_runtime")
    referenced = {node.id for node in ast.walk(runtime) if isinstance(node, ast.Name)}
    assert "run_waking_turn" in referenced
    for forbidden in (
        "prepare_context", "call_llama", "record_event", "resolve_proposal",
        "run_sleep_cycle",
    ):
        assert forbidden not in calls, (
            f"respond() calls {forbidden!r} directly; that logic belongs in "
            "run_waking_turn(), not in the interface"
        )


def test_gui_history_parameter_is_observability_only_never_a_conversation_source():
    """Gradio's visible history may be measured by diagnostics but must never
    reach the waking pipeline: it would be a second conversation source."""
    respond = _function("llama_gui.py", "respond")
    assert "history" in [argument.arg for argument in respond.args.args]

    history_uses = [
        node for node in ast.walk(respond)
        if isinstance(node, ast.Name) and node.id == "history"
        and isinstance(node.ctx, ast.Load)
    ]
    assert history_uses, "diagnostics wrapper is expected to receive history"

    service_calls = [
        node for node in ast.walk(respond)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_owner_conversation_turn"
    ]
    assert len(service_calls) == 1
    service_call = service_calls[0]
    allowed_ids = {id(node) for node in ast.walk(service_call.args[1])}
    for use in history_uses:
        assert id(use) in allowed_ids, (
            "history is used somewhere other than the shared service's diagnostics input"
        )
    service_core = _function("owner_conversation_service.py", "_execute_under_waking_lock")
    history_uses = [
        node for node in ast.walk(service_core)
        if isinstance(node, ast.Name) and node.id == "history" and isinstance(node.ctx, ast.Load)
    ]
    assert len(history_uses) == 1
    wrapper_call = next(
        node for node in ast.walk(service_core)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "diagnostics_wrapper"
    )
    assert id(history_uses[0]) in {id(node) for node in ast.walk(wrapper_call)}


def test_respond_opens_its_orchestrator_per_call_not_at_import_time():
    """The cross-thread defect this replaces: a connection opened at import
    time broke when Gradio called respond() from a worker thread."""
    respond = _function("llama_gui.py", "respond")
    assert "_owner_conversation_runtime" in _called_names(respond)
    runtime = _function("llama_gui.py", "_owner_conversation_runtime")
    assert "AnaxiOrchestrator" in _called_names(runtime)
    tree = ast.parse((HERE / "llama_gui.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            assert not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "AnaxiOrchestrator"
            ), "AnaxiOrchestrator must not be constructed at module scope"


def test_gui_and_cli_share_one_waking_pipeline_entrypoint():
    """The CLI entrypoint and the GUI must reach the same run_waking_turn."""
    run_waking_turn = _function("llama_anaxi.py", "run_waking_turn")
    assert run_waking_turn.name == "run_waking_turn"
    gui_tree = ast.parse((HERE / "llama_gui.py").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in gui_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "llama_anaxi"
        for alias in node.names
    }
    assert "run_waking_turn" in imported, (
        "the GUI must import the shared pipeline, not reimplement it"
    )


def test_configured_porch_seam_uses_server_authority_and_exact_admission():
    porch = _function("llama_gui.py", "submit_porch_owner_conversation")
    referenced = {node.id for node in ast.walk(porch) if isinstance(node, ast.Name)}
    assert "BOUND_HUMAN_AUTHORITY" in referenced
    assert "admit_human_input_at_ingress" in referenced
    assert "PROVENANCE_DB_DIR" in referenced and "PIPELINE_KEY" in referenced
    assert "_apply_owner_conversation_post_success" in _called_names(porch)
    assert not ({"actor_id", "visibility_scope", "auth_context_id"} & referenced)
