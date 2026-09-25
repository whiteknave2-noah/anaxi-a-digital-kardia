"""Regression: a page image / photograph delivered to the vision model must not be lost to hidden reasoning.

Live scanned-PDF recheck (H 01M2XS6DS3KVBKNMBA8T4FAJ7V): the page-view action ran and real pixels were
delivered, then the vision model (qwen3-vl:4b) spent 6722 characters / 1468 tokens in hidden reasoning --
think=False is not honored by this build -- exhausting the 4096 window (prompt 2628): done_reason=length,
ZERO content, no X, and a generic "control could not be validated" message. The call had no num_predict
and no completion check, so the truthful cause was also lost.
"""
import json
import os
from pathlib import Path

import pytest
from PIL import Image

import context_budget
from test_wsp1_production_hard_floor import PRODUCTION_HUMAN_BYTES, _build, _message

VIEW = {"resource_class": "photographs", "action": "view", "relative_path": "p.png", "content": ""}


def _vision_turn(monkeypatch, tmp_path, answer):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action=VIEW)
    Image.new("RGB", (64, 48), (20, 90, 200)).save(Path(h.paths.photographs_dir, "p.png"))
    seen = []
    inner = h.la.ollama.chat

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        if any(m.get("images") for m in messages):
            seen.append({"model": model, "options": dict(options or {})})
            return dict(answer, prompt_eval_count=2628)
        return inner(model, messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat
    return h, seen


def _run(h):
    return h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation")


def test_vision_call_runs_in_an_enlarged_bounded_window_and_long_reasoning_is_not_a_failure(monkeypatch, tmp_path):
    good = {"message": {"content": "A blue field.", "thinking": "r" * 9000},
            "done": True, "done_reason": "stop", "eval_count": 2200}       # far above the 1024 visible-reply reserve
    h, seen = _vision_turn(monkeypatch, tmp_path, good)

    result = _run(h)

    assert result["reply"] == "A blue field."
    assert seen and seen[0]["model"] == "qwen3-vl:4b"
    assert seen[0]["options"]["num_ctx"] == context_budget.QWEN_VISION_RUNTIME_CONTEXT == 10240
    assert seen[0]["options"]["num_predict"] == context_budget.QWEN_VISION_RUNTIME_MAX_GENERATION == 6144
    # the admitted prompt plus the whole generation always fits the runtime window
    assert context_budget.QWEN_VISION_MAX_PROMPT_BUDGET + context_budget.QWEN_VISION_RUNTIME_MAX_GENERATION < context_budget.QWEN_VISION_RUNTIME_CONTEXT
    # prompt admission is unchanged: still against the 4096 ceiling minus the reserve
    assert h.budget_results["wsp1_pass2"].max_prompt_budget == context_budget.QWEN_VISION_MAX_PROMPT_BUDGET


def test_reasoning_only_length_exhaustion_is_a_truthful_generation_failure(monkeypatch, tmp_path):
    """The live shape: done_reason=length, empty content, thinking only."""
    dead = {"message": {"content": "", "thinking": "r" * 6722}, "done": True, "done_reason": "length", "eval_count": 1468}
    h, _seen = _vision_turn(monkeypatch, tmp_path, dead)
    import conversation_direction as cd

    with pytest.raises(cd.ConversationDirectionFailure) as caught:
        _run(h)

    assert caught.value.stage == "workspace_pass2" and caught.value.failure_code == "COMPLETION_LIMIT_REACHED"
    trace = h.la.get_last_conversation_direction_trace()
    assert trace["pass2_status"] == "COMPLETION_LIMIT_REACHED"          # not the misleading INCOMPLETE_EXPRESSION_BOUNDARY


def test_a_text_model_workspace_pass2_is_unchanged(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    seen = []
    inner = h.la.ollama.chat

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        if format is None and not (options and options.get("num_predict") == 1):
            seen.append(dict(options or {}))
        return inner(model, messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat
    _run(h)
    assert seen and all("num_ctx" not in o and "num_predict" not in o for o in seen)


def test_call_llama_runtime_options_are_whitelisted():
    import test_owc5_s2_integration as fixture

    la, *_ = fixture.fresh_llama_anaxi(pass1_value=None, pass2_value=None)
    with pytest.raises(ValueError):
        la.call_llama([{"role": "user", "content": "x"}], {"temperature": 0.4, "top_p": 0.85},
                      runtime_options={"num_ctx": 8192, "keep_alive": 1})


# ---- live next-page turn (H 01M2YBSSD2EYP0WJNAYVS94ZFG): marker handshake + recovery classification --------

def _classify(exc):
    # The fixtures reload conversation_direction/llama_anaxi; classify with a capture module bound to
    # the SAME (current) exception classes, exactly as production has one set of classes.
    import importlib
    import sys

    sys.modules.pop("waking_turn_failure_capture", None)
    wtfc = importlib.import_module("waking_turn_failure_capture")
    # ...and the workspace_direction module the failing supervisor actually raised from (other suites
    # re-import it; production has exactly one).
    supervisor = sys.modules.get("workspace_supervisor")
    saved = sys.modules.get("workspace_direction")
    if supervisor is not None:
        sys.modules["workspace_direction"] = supervisor.wd
    try:
        return wtfc._classify_waking_failure(exc, has_x=False)
    finally:
        if saved is not None:
            sys.modules["workspace_direction"] = saved


@pytest.mark.parametrize("text", ["Page two continues the argument.", "Page two continues the argument.\n\n",
                                  "  Page two continues the argument."])
def test_a_normally_stopped_vision_reply_is_the_reply_with_nothing_typed_back(monkeypatch, tmp_path, text):
    """Shared expression seam: the live next-page turn (H 01M2YBSSD2EYP0WJNAYVS94ZFG) died because the
    model put a voluntary marker on the same line; no protocol string is required any more -- the
    provider's normal stop within the vision limits is the completion evidence."""
    h, _seen = _vision_turn(monkeypatch, tmp_path, {
        "message": {"content": text}, "done": True, "done_reason": "stop", "eval_count": 900})
    assert _run(h)["reply"] == "Page two continues the argument."


def test_a_blank_vision_completion_fails_closed_as_expression_not_established(monkeypatch, tmp_path):
    h, _seen = _vision_turn(monkeypatch, tmp_path, {
        "message": {"content": "  \n"}, "done": True, "done_reason": "stop", "eval_count": 3})
    import conversation_direction as cd

    with pytest.raises(cd.ConversationDirectionFailure) as caught:
        _run(h)
    assert caught.value.failure_code == "EMPTY_EXPRESSION" and caught.value.stage == "workspace_pass2"


def test_a_generation_failure_after_a_read_only_action_is_retry_safe_for_recovery(monkeypatch, tmp_path):
    """The live H was not offered for recovery: workspace_pass2 failures were always UNCLASSIFIED because the
    action had run. After a READ-ONLY action nothing durable exists to duplicate."""
    h, _seen = _vision_turn(monkeypatch, tmp_path, {
        "message": {"content": "", "thinking": "r" * 500}, "done": True, "done_reason": "length", "eval_count": 6144})
    import conversation_direction as cd

    with pytest.raises(cd.ConversationDirectionFailure) as caught:
        _run(h)

    assert caught.value.stage == "workspace_pass2"
    assert caught.value.__cause__.executed_action == ("photographs", "view")
    failure_class, basis = _classify(caught.value)
    assert failure_class == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED" and "READ-ONLY" in basis


def test_a_state_changing_or_forged_workspace_failure_is_not_retry_safe():
    import conversation_direction as cd
    import workspace_direction as wd

    def failure(cause):
        exc = cd.ConversationDirectionFailure("workspace_pass2", "INCOMPLETE_EXPRESSION_BOUNDARY")
        exc.__cause__ = cause
        return _classify(exc)[0]

    assert failure(wd.WorkspaceDirectionFailure("pass2", "INCOMPLETE_EXPRESSION_BOUNDARY", ("journal", "append"))) == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert failure(wd.WorkspaceDirectionFailure("pass2", "INCOMPLETE_EXPRESSION_BOUNDARY", ("organization", "rename"))) == "UNCLASSIFIED_WAKING_EXCEPTION"
    assert failure(wd.WorkspaceDirectionFailure("pass2", "INCOMPLETE_EXPRESSION_BOUNDARY", None)) == "UNCLASSIFIED_WAKING_EXCEPTION"

    class Forged(Exception):
        executed_action = ("library", "read")

    assert failure(Forged()) == "UNCLASSIFIED_WAKING_EXCEPTION"                 # not the genuine class
    assert failure(None) == "UNCLASSIFIED_WAKING_EXCEPTION"
    # a deterministic budget failure is still not replayable, even after a read-only action
    budget = cd.ConversationDirectionFailure("workspace_pass2", "BUDGET_EXCEEDED")
    budget.__cause__ = wd.WorkspaceDirectionFailure("pass2", "BUDGET_EXCEEDED", ("library", "read"))
    assert _classify(budget)[0] == "UNCLASSIFIED_WAKING_EXCEPTION"
