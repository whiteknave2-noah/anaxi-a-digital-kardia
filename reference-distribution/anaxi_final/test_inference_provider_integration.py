"""Production-helper regressions for the thin provider boundary.

Uses the repository's established isolated llama_anaxi import fixture, so the
real call_llama()/ask_llama_for_json() code runs without model, database, or
heavy retrieval dependencies.
"""

import pytest

from test_owc5_s2_integration import fresh_llama_anaxi


_MESSAGES = [{"role": "user", "content": "hello"}]
_CONTROLS = {"temperature": 0.4, "top_p": 0.85}


def _raw(content="partial", done_reason="length", eval_count=4):
    return {
        "model": "llama-3.2-3b-instruct-mlx-bf16:latest",
        "message": {"content": content},
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": 12,
        "eval_count": eval_count,
    }


def test_default_call_llama_keeps_exact_ollama_call_semantics(monkeypatch):
    monkeypatch.delenv("ANAXI_INFERENCE_PROVIDER", raising=False)
    la, call_log, _p1, _p2, _persistence = fresh_llama_anaxi(
        pass2_value="ordinary reply")

    assert la.call_llama(_MESSAGES, _CONTROLS) == "ordinary reply"
    assert call_log == [{
        "model": "gemma4:e4b",
        "format": None,
        "messages": _MESSAGES,
        "think": False,
        "options": {"temperature": 0.4, "top_p": 0.85},
    }]


def test_mlx_provider_error_propagates_without_ollama_fallback(monkeypatch):
    la, call_log, _p1, _p2, _persistence = fresh_llama_anaxi(
        pass2_value="must not be used")
    monkeypatch.setenv("ANAXI_INFERENCE_PROVIDER", "mlx-serve")
    failed = la.inference_provider.ProviderResult(
        provider="mlx-serve", status=la.inference_provider.PROVIDER_ERROR,
        model_requested=la.MODEL, error="synthetic provider failure",
        diagnostics={"raw": "kept"},
    )
    monkeypatch.setattr(la.inference_provider, "dispatch", lambda *a, **kw: failed)

    with pytest.raises(la.inference_provider.ProviderError) as captured:
        la.call_llama(_MESSAGES, _CONTROLS)
    assert captured.value.result is failed
    assert captured.value.result.diagnostics == {"raw": "kept"}
    assert call_log == []


def test_unbounded_mlx_text_call_rejects_truncation_as_assistant_content(monkeypatch):
    la, call_log, _p1, _p2, _persistence = fresh_llama_anaxi()
    monkeypatch.setenv("ANAXI_INFERENCE_PROVIDER", "mlx-serve")
    truncated = la.inference_provider.ProviderResult(
        provider="mlx-serve", status=la.inference_provider.TRUNCATED,
        model_requested=la.MODEL, raw=_raw(),
    )
    monkeypatch.setattr(la.inference_provider, "dispatch", lambda *a, **kw: truncated)

    with pytest.raises(la.inference_provider.ProviderError) as captured:
        la.call_llama(_MESSAGES, _CONTROLS)
    assert captured.value.result is truncated
    assert call_log == []


def test_bounded_mlx_text_call_uses_existing_incomplete_completion_path(monkeypatch):
    la, _call_log, _p1, _p2, _persistence = fresh_llama_anaxi()
    monkeypatch.setenv("ANAXI_INFERENCE_PROVIDER", "mlx-serve")
    truncated = la.inference_provider.ProviderResult(
        provider="mlx-serve", status=la.inference_provider.TRUNCATED,
        model_requested=la.MODEL, raw=_raw(),
    )
    monkeypatch.setattr(la.inference_provider, "dispatch", lambda *a, **kw: truncated)

    with pytest.raises(la.context_budget.IncompleteCompletionError) as captured:
        la.call_llama(
            _MESSAGES, _CONTROLS,
            generation_reserve=la.context_budget.PASS2_GENERATION_RESERVE,
        )
    assert captured.value.failure_code == "COMPLETION_LIMIT_REACHED"


def test_mlx_structured_helper_rejects_truncation_before_content_parsing(monkeypatch):
    la, call_log, _p1, _p2, _persistence = fresh_llama_anaxi()
    monkeypatch.setenv("ANAXI_INFERENCE_PROVIDER", "mlx-serve")
    # This prefix is valid JSON on purpose: syntax validity must not erase the
    # provider's explicit transport-truncation fact.
    truncated = la.inference_provider.ProviderResult(
        provider="mlx-serve", status=la.inference_provider.TRUNCATED,
        model_requested=la.MODEL, raw=_raw(content='{"act":"stop"}'),
    )
    monkeypatch.setattr(la.inference_provider, "dispatch", lambda *a, **kw: truncated)

    with pytest.raises(la.inference_provider.ProviderError) as captured:
        la.ask_llama_for_json(_MESSAGES)
    assert captured.value.result is truncated
    assert call_log == []
