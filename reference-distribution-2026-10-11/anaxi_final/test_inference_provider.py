"""THIN-INFERENCE-PROVIDER-BOUNDARY-V0: focused, network-free tests for
inference_provider.py -- the seam that lets llama_anaxi.py's two real
generation call sites (call_llama()/ask_llama_for_json()) dispatch to
either Ollama (default, unchanged) or mlx-serve (qualified alternate),
explicitly, statelessly, with no automatic fallback.

Covers, against a synthetic ollama.Client stand-in (never a real network
call): provider selection (default/explicit/env/unknown), statelessness
(each call self-contained, no session id used), sole-residency guarding
before and after every mlx-serve request, honest result classification
(success/truncation/provider-error/malformed-response/residency-not-
established), and num_ctx's confirmed-ignored status never being reported
as honored.
"""
import os

import pytest

import inference_provider as ip


# ============================================== provider selection

def test_no_explicit_provider_and_no_env_resolves_to_ollama(monkeypatch):
    monkeypatch.delenv(ip.PROVIDER_ENV_VAR, raising=False)
    assert ip.resolve_provider(None) == ip.PROVIDER_OLLAMA


def test_explicit_ollama_resolves_to_ollama(monkeypatch):
    monkeypatch.setenv(ip.PROVIDER_ENV_VAR, ip.PROVIDER_MLX_SERVE)
    assert ip.resolve_provider(ip.PROVIDER_OLLAMA) == ip.PROVIDER_OLLAMA


def test_explicit_mlxserve_resolves_to_mlxserve(monkeypatch):
    monkeypatch.delenv(ip.PROVIDER_ENV_VAR, raising=False)
    assert ip.resolve_provider(ip.PROVIDER_MLX_SERVE) == ip.PROVIDER_MLX_SERVE


def test_env_var_selects_mlxserve_when_no_explicit_argument(monkeypatch):
    monkeypatch.setenv(ip.PROVIDER_ENV_VAR, ip.PROVIDER_MLX_SERVE)
    assert ip.resolve_provider(None) == ip.PROVIDER_MLX_SERVE


def test_unknown_explicit_provider_fails_clearly(monkeypatch):
    monkeypatch.delenv(ip.PROVIDER_ENV_VAR, raising=False)
    with pytest.raises(ValueError):
        ip.resolve_provider("gpt-nonsense")


def test_unknown_env_provider_fails_clearly(monkeypatch):
    monkeypatch.setenv(ip.PROVIDER_ENV_VAR, "definitely-not-a-provider")
    with pytest.raises(ValueError):
        ip.resolve_provider(None)


def test_dispatch_does_not_handle_ollama_directly():
    """The Ollama path stays inline in llama_anaxi.py's own wrapper
    functions (byte-identical to before this module existed) -- dispatch()
    is only ever used for mlx-serve. A caller that mistakenly routes
    Ollama through dispatch() gets a clear error, not silent Ollama
    behavior duplicated in a second place."""
    with pytest.raises(ValueError):
        ip.dispatch(ip.PROVIDER_OLLAMA, model="whatever", messages=[])


# ============================================== synthetic mlx-serve client
#
# ollama.Client's real network I/O is never exercised here -- _mlxserve_chat()
# constructs `ollama.Client(host=base_url)` itself, so these tests patch
# `ollama.Client` (imported by inference_provider as `ollama`) with a
# factory returning a synthetic stand-in exposing the same `.ps()`/`.chat()`
# surface, matching this repo's existing established pattern for faking the
# ollama client (see test_waking_turn_recovery.py's own FakeOllama/_ollama_module).

class _FakeMlxClient:
    def __init__(self, host, ps_sequence, chat_response=None, chat_raises=None,
                 record=None):
        self.host = host
        self._ps_sequence = list(ps_sequence)
        self._chat_response = chat_response
        self._chat_raises = chat_raises
        self._record = record if record is not None else []

    def ps(self):
        self._record.append(("ps",))
        return self._ps_sequence.pop(0)

    def chat(self, model, messages, format=None, options=None, **kw):
        self._record.append(("chat", model, messages, format, options))
        if self._chat_raises is not None:
            raise self._chat_raises
        return self._chat_response


def _patch_client(monkeypatch, ps_sequence, chat_response=None, chat_raises=None):
    record = []

    def factory(host):
        return _FakeMlxClient(host, ps_sequence, chat_response, chat_raises, record)

    monkeypatch.setattr(ip.ollama, "Client", factory)
    return record


_MODEL = ip.QUALIFIED_MLXSERVE_MODEL
_SOLE_RESIDENT = {"models": [{"model": _MODEL}]}
_EMPTY_RESIDENT = {"models": []}
_MULTI_RESIDENT = {"models": [{"model": _MODEL}, {"model": "other:latest"}]}
_OTHER_RESIDENT = {"models": [{"model": "other:latest"}]}


def _ok_chat_response(model_self_reported=_MODEL, done_reason="stop", content="hi"):
    return {
        "model": model_self_reported,
        "message": {"content": content},
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": 12,
        "eval_count": 4,
    }


# ============================================== success / statelessness

def test_mlxserve_success_checks_residency_before_and_after_and_returns_raw(monkeypatch):
    record = _patch_client(monkeypatch, [_SOLE_RESIDENT, _SOLE_RESIDENT],
                            chat_response=_ok_chat_response())
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL,
                          messages=[{"role": "user", "content": "hello"}],
                          options={"temperature": 0.2})
    assert result.status == ip.SUCCESS
    assert result.provider == ip.PROVIDER_MLX_SERVE
    assert result.model_requested == _MODEL
    assert result.model_dispatched == _MODEL
    assert result.model_self_reported == _MODEL
    assert result.raw["message"]["content"] == "hi"
    assert result.unsupported_options == ()
    # residency checked before AND after the single chat call, in order.
    assert [c[0] for c in record] == ["ps", "chat", "ps"]


def test_production_logical_model_maps_to_exact_qualified_alias(monkeypatch):
    record = _patch_client(monkeypatch, [_SOLE_RESIDENT, _SOLE_RESIDENT],
                           chat_response=_ok_chat_response())
    result = ip.dispatch(
        ip.PROVIDER_MLX_SERVE, model=ip.ANAXI_DEFAULT_OLLAMA_MODEL,
        messages=[{"role": "user", "content": "hello"}],
    )
    chat_call = next(call for call in record if call[0] == "chat")
    assert chat_call[1] == ip.QUALIFIED_MLXSERVE_MODEL
    assert result.model_requested == ip.ANAXI_DEFAULT_OLLAMA_MODEL
    assert result.model_dispatched == ip.QUALIFIED_MLXSERVE_MODEL
    assert result.status == ip.SUCCESS


def test_unqualified_model_and_image_path_fail_before_provider_request(monkeypatch):
    record = _patch_client(monkeypatch, [], chat_response=_ok_chat_response())
    wrong_model = ip.dispatch(
        ip.PROVIDER_MLX_SERVE, model="qwen3-vl:4b", messages=[])
    image_request = ip.dispatch(
        ip.PROVIDER_MLX_SERVE, model=_MODEL,
        messages=[{"role": "user", "content": "look", "images": [b"pixels"]}],
    )
    assert wrong_model.status == ip.UNSUPPORTED_BEHAVIOR
    assert image_request.status == ip.UNSUPPORTED_BEHAVIOR
    assert record == []


def test_each_mlxserve_call_is_self_contained_no_session_id(monkeypatch):
    """P3/statelessness: the messages list sent is exactly what the caller
    passed, nothing added (no previous_response_id, no session/context
    token array), and two independent calls never share state."""
    record = _patch_client(monkeypatch, [_SOLE_RESIDENT, _SOLE_RESIDENT,
                                          _SOLE_RESIDENT, _SOLE_RESIDENT],
                            chat_response=_ok_chat_response())
    messages_a = [{"role": "user", "content": "turn one"}]
    messages_b = [{"role": "user", "content": "turn two, unrelated"}]
    ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=messages_a)
    ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=messages_b)
    chat_calls = [c for c in record if c[0] == "chat"]
    assert chat_calls[0][2] == messages_a
    assert chat_calls[1][2] == messages_b
    for call in chat_calls:
        assert "context" not in (call[4] or {})
        assert not hasattr(call, "previous_response_id")


def test_truncated_completion_is_distinct_from_normal_success(monkeypatch):
    _patch_client(monkeypatch, [_SOLE_RESIDENT, _SOLE_RESIDENT],
                   chat_response=_ok_chat_response(done_reason="length"))
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert result.status == ip.TRUNCATED
    assert result.status != ip.SUCCESS


# ============================================== residency guard (P8)

def test_prechat_multi_resident_blocks_the_request_entirely(monkeypatch):
    record = _patch_client(monkeypatch, [_MULTI_RESIDENT], chat_response=_ok_chat_response())
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert result.status == ip.RESIDENCY_NOT_ESTABLISHED
    # the chat call must never have been attempted -- residency failed closed.
    assert [c[0] for c in record] == ["ps"]


def test_prechat_wrong_resident_model_blocks_the_request(monkeypatch):
    record = _patch_client(monkeypatch, [_OTHER_RESIDENT], chat_response=_ok_chat_response())
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert result.status == ip.RESIDENCY_NOT_ESTABLISHED
    assert [c[0] for c in record] == ["ps"]


def test_prechat_zero_resident_blocks_the_request(monkeypatch):
    record = _patch_client(monkeypatch, [_EMPTY_RESIDENT], chat_response=_ok_chat_response())
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert result.status == ip.RESIDENCY_NOT_ESTABLISHED
    assert [c[0] for c in record] == ["ps"]


def test_postchat_residency_lost_marks_not_established_even_on_apparent_success(monkeypatch):
    """A request that raced with a load/unload -- sole residency held
    before dispatch but not after -- must never be reported as a trusted
    success, per the qualified Stage A/B guard this reproduces."""
    _patch_client(monkeypatch, [_SOLE_RESIDENT, _MULTI_RESIDENT],
                   chat_response=_ok_chat_response())
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert result.status == ip.RESIDENCY_NOT_ESTABLISHED
    # raw content is still attached for diagnostics -- never discarded.
    assert result.raw is not None


def test_wrong_or_ambiguous_residency_never_reported_as_success(monkeypatch):
    for ps_sequence in ([_MULTI_RESIDENT], [_OTHER_RESIDENT], [_EMPTY_RESIDENT]):
        _patch_client(monkeypatch, list(ps_sequence), chat_response=_ok_chat_response())
        result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
        assert result.status not in (ip.SUCCESS, ip.TRUNCATED)


# ============================================== error / malformed handling (P6)

def test_provider_exception_is_reported_as_provider_error_not_success(monkeypatch):
    _patch_client(monkeypatch, [_SOLE_RESIDENT, _SOLE_RESIDENT],
                   chat_raises=RuntimeError("connection refused"))
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert result.status == ip.PROVIDER_ERROR
    assert "RuntimeError" in result.error
    assert isinstance(result.diagnostics["request_exception"], RuntimeError)


def test_request_exception_plus_postcheck_race_reports_unestablished_and_keeps_both(monkeypatch):
    _patch_client(monkeypatch, [_SOLE_RESIDENT, _MULTI_RESIDENT],
                  chat_raises=RuntimeError("connection reset"))
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert result.status == ip.RESIDENCY_NOT_ESTABLISHED
    assert result.diagnostics["residency_after"]["status"] == ip.ResidencyAuthority.CONFLICTING
    assert isinstance(result.diagnostics["request_exception"], RuntimeError)


def test_malformed_response_missing_content_is_never_success(monkeypatch):
    _patch_client(monkeypatch, [_SOLE_RESIDENT, _SOLE_RESIDENT],
                   chat_response={"message": {}, "done": True})
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert result.status == ip.MALFORMED_RESPONSE


def test_missing_completion_accounting_is_malformed_not_success(monkeypatch):
    _patch_client(
        monkeypatch, [_SOLE_RESIDENT, _SOLE_RESIDENT],
        chat_response={"model": _MODEL, "message": {"content": "hi"}},
    )
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert result.status == ip.MALFORMED_RESPONSE


def test_conflicting_response_model_self_report_is_not_success(monkeypatch):
    _patch_client(
        monkeypatch, [_SOLE_RESIDENT, _SOLE_RESIDENT],
        chat_response=_ok_chat_response(model_self_reported="wrong:latest"),
    )
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert result.status == ip.MALFORMED_RESPONSE
    assert result.model_dispatched == _MODEL
    assert result.model_self_reported == "wrong:latest"


def test_provider_error_exception_carries_the_full_result():
    result = ip.ProviderResult(provider=ip.PROVIDER_MLX_SERVE, status=ip.PROVIDER_ERROR,
                                model_requested="x", error="boom")
    exc = ip.ProviderError(result)
    assert exc.result is result
    assert "boom" in str(exc)


# ============================================== provider-specific options (P9)

def test_num_ctx_is_reported_unsupported_never_honored(monkeypatch):
    record = _patch_client(monkeypatch, [_SOLE_RESIDENT, _SOLE_RESIDENT],
                           chat_response=_ok_chat_response())
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[],
                          options={"temperature": 0.2, "num_ctx": 4096})
    assert "num_ctx" in result.unsupported_options
    # still succeeds -- unsupported/ignored, not rejected outright -- but
    # the caller can see it was never claimed as honored.
    assert result.status == ip.SUCCESS
    chat_call = next(call for call in record if call[0] == "chat")
    assert chat_call[4] == {"temperature": 0.2}


def test_unknown_option_is_rejected_before_provider_request(monkeypatch):
    record = _patch_client(monkeypatch, [], chat_response=_ok_chat_response())
    result = ip.dispatch(
        ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[], options={"seed": 7})
    assert result.status == ip.UNSUPPORTED_BEHAVIOR
    assert result.unsupported_options == ("seed",)
    assert record == []


def test_shared_supported_option_passes_through_unremarked(monkeypatch):
    record = _patch_client(monkeypatch, [_SOLE_RESIDENT, _SOLE_RESIDENT],
                            chat_response=_ok_chat_response())
    ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[],
                options={"temperature": 0.2})
    chat_call = next(c for c in record if c[0] == "chat")
    assert chat_call[4] == {"temperature": 0.2}


# ============================================== diagnostics (P7)

def test_raw_diagnostics_and_residency_evidence_remain_available_on_failure(monkeypatch):
    _patch_client(monkeypatch, [_MULTI_RESIDENT], chat_response=_ok_chat_response())
    result = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert "residency_before" in result.diagnostics
    assert result.diagnostics["residency_before"]["status"] == ip.ResidencyAuthority.CONFLICTING


def test_provider_identity_is_retained_on_every_result_shape(monkeypatch):
    _patch_client(monkeypatch, [_SOLE_RESIDENT, _SOLE_RESIDENT], chat_response=_ok_chat_response())
    success = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert success.provider == ip.PROVIDER_MLX_SERVE

    _patch_client(monkeypatch, [_EMPTY_RESIDENT])
    failure = ip.dispatch(ip.PROVIDER_MLX_SERVE, model=_MODEL, messages=[])
    assert failure.provider == ip.PROVIDER_MLX_SERVE
