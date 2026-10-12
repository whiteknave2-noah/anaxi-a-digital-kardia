"""Anaxi -- thin production inference-provider boundary (V0).

Lets the two real generation call sites in llama_anaxi.py (call_llama(),
ask_llama_for_json()) dispatch to either of the two qualified providers --
Ollama (incumbent/default) or mlx-serve (qualified alternate, see
d0a5945a4bf97afb9619d0e13a824ff0fdd6b04a's accepted Stage A/B qualification)
-- without either call site knowing which one actually ran.

This module transports inference only. It carries no conversation history,
memory, provenance, or Clark identity -- every request must arrive already
fully composed (messages list, model, options) by the caller, and every
response is handed straight back for the SAME caller to interpret and
persist exactly as it already does for Ollama today. There is no
provider-managed session/history of any kind: each call is a fresh,
self-contained request (P3 -- statelessness).

Provider selection is explicit and bounded: an optional `provider=`
argument, else the ANAXI_INFERENCE_PROVIDER environment variable, else
Ollama. An unrecognized value fails loudly (ValueError) rather than
silently falling back. There is no automatic failover between providers
anywhere in this module -- a failed mlx-serve request is reported as a
failure, never silently retried against Ollama.

mlx-serve is dispatched through `ollama.Client(host=...)`, exactly as the
accepted Stage A/B qualification's own run script did (mlx-serve's HTTP
API is Ollama-wire-compatible for /api/chat and /api/ps) -- so no second
message-format transform is needed, and this module stays two small
adapters, not a framework. What mlx-serve does NOT share with Ollama is
kept explicit rather than smoothed over:
  - production's logical Ollama model name (gemma4:e4b) is mapped to the
    exact alias qualified in Stage A/B; no other logical/physical model is
    accepted by this V0 adapter;
  - request-level `num_ctx` is confirmed IGNORED by mlx-serve v26.9.2 (the
    qualification's own finding) -- reported via `unsupported_options`,
    never claimed as honored;
  - `think` (Ollama's hidden-reasoning toggle) was never exercised against
    mlx-serve during qualification and is not forwarded to it;
  - the qualified sole-residency guard (originally
    inference_portability_stage_a.establish_actual_served_artifact(),
    qualification-harness-only) is reproduced here, unmodified in its
    identity logic, reusing wtr0_cold_reset's own real residency-parsing
    primitives -- never trusting mlx-serve's own /api/chat `model` field,
    which the qualification found can echo a bogus requested name back.
    A pre- and post-call residency check brackets every mlx-serve request;
    either check failing means the caller receives
    RESIDENCY_NOT_ESTABLISHED, never a fabricated success.
"""

import os

import ollama

import wtr0_cold_reset as _wtr0

PROVIDER_OLLAMA = "ollama"
PROVIDER_MLX_SERVE = "mlx-serve"
VALID_PROVIDERS = frozenset({PROVIDER_OLLAMA, PROVIDER_MLX_SERVE})

PROVIDER_ENV_VAR = "ANAXI_INFERENCE_PROVIDER"
MLXSERVE_URL_ENV_VAR = "ANAXI_MLXSERVE_URL"
DEFAULT_MLXSERVE_URL = "http://127.0.0.1:11234"

# This V0 is deliberately pinned to the one mlx-serve artifact Stage A/B
# qualified. Production's incumbent model name is a logical request at the
# provider seam; the mlx adapter translates only that one request to the
# independently-qualified physical alias. Direct bounded smoke callers may
# also name the qualified alias itself. Vision and hypothetical models fail
# explicitly instead of being sent to an unqualified substrate.
ANAXI_DEFAULT_OLLAMA_MODEL = "gemma4:e4b"
QUALIFIED_MLXSERVE_MODEL = "llama-3.2-3b-instruct-mlx-bf16:latest"
MLXSERVE_MODEL_MAP = {
    ANAXI_DEFAULT_OLLAMA_MODEL: QUALIFIED_MLXSERVE_MODEL,
    QUALIFIED_MLXSERVE_MODEL: QUALIFIED_MLXSERVE_MODEL,
}

# Confirmed IGNORED by mlx-serve v26.9.2 for the pinned qualified artifact
# (accepted Stage A/B finding) -- never claimed honored for this provider.
MLXSERVE_IGNORED_OPTIONS = frozenset({"num_ctx"})
MLXSERVE_SUPPORTED_OPTIONS = frozenset({"temperature", "top_p", "num_predict"})


def resolve_provider(explicit=None):
    """P1/provider-selection law: explicit argument wins; otherwise the
    environment variable; otherwise Ollama. Unknown value fails clearly."""
    value = explicit if explicit is not None else os.environ.get(PROVIDER_ENV_VAR, PROVIDER_OLLAMA)
    if value not in VALID_PROVIDERS:
        raise ValueError(
            f"unknown ANAXI inference provider {value!r}; must be one of {sorted(VALID_PROVIDERS)}")
    return value


# ============================================== Result vocabulary (P6)

SUCCESS = "SUCCESS"
TRUNCATED = "TRUNCATED"
PROVIDER_ERROR = "PROVIDER_ERROR"
MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
RESIDENCY_NOT_ESTABLISHED = "RESIDENCY_NOT_ESTABLISHED"
UNSUPPORTED_BEHAVIOR = "UNSUPPORTED_BEHAVIOR"


class ProviderResult:
    """The smallest normalized result callers need, plus the raw
    provider-shaped response untouched (P6/P7) -- callers that already
    know how to read an Ollama-shaped chat response (response["message"]
    ["content"], response["done_reason"], ...) keep working unmodified
    against `raw`, regardless of which provider actually produced it,
    since mlx-serve's response is Ollama-wire-shaped by construction."""

    def __init__(self, provider, status, model_requested, model_dispatched=None,
                 model_self_reported=None,
                 raw=None, error=None, unsupported_options=(), diagnostics=None):
        self.provider = provider
        self.status = status
        self.model_requested = model_requested
        self.model_dispatched = model_dispatched
        self.model_self_reported = model_self_reported
        self.raw = raw
        self.error = error
        self.unsupported_options = tuple(unsupported_options)
        self.diagnostics = diagnostics or {}


class ProviderError(Exception):
    """Raised by dispatch() callers that want a single exception rather
    than branching on ProviderResult.status themselves. Carries the full
    ProviderResult (never just a message) so nothing observed is lost."""

    def __init__(self, result: ProviderResult):
        self.result = result
        super().__init__(f"{result.provider} inference {result.status}: {result.error}")


# ============================================== mlx-serve sole-residency guard
#
# Production analogue of the accepted Stage A/B qualification's own
# establish_actual_served_artifact() -- same identity logic, reusing
# wtr0_cold_reset's real, unmodified residency-parsing primitives
# (_recognized_models_collection/_entry_model_identity), never trusting a
# /api/chat response's own `model` field for identity. Kept here, not
# imported from the qualification-harness module (inference_portability_
# stage_a.py), since that module is qualification-only by its own
# docstring and must not become a production import.

class ResidencyAuthority:
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CONFLICTING = "CONFLICTING"


def _establish_sole_resident(ps_fn, expected_model_name):
    try:
        loaded = ps_fn()
    except Exception as exc:
        return {"status": ResidencyAuthority.UNKNOWN,
                "basis": f"residency observation raised {type(exc).__name__}"}

    try:
        models = _wtr0._recognized_models_collection(loaded, ollama)
    except Exception:
        models = _wtr0._UNRECOGNIZED
    if models is _wtr0._UNRECOGNIZED:
        return {"status": ResidencyAuthority.UNKNOWN,
                "basis": "residency response unrecognized/malformed"}

    models = list(models)
    if len(models) == 0:
        return {"status": ResidencyAuthority.FAILED,
                "basis": f"{expected_model_name!r} not resident (zero resident models)"}

    identities = []
    any_unrecognized = False
    for entry in models:
        try:
            identity = _wtr0._entry_model_identity(entry, ollama)
        except Exception:
            identity = _wtr0._UNRECOGNIZED
        if identity is _wtr0._UNRECOGNIZED:
            any_unrecognized = True
        else:
            identities.append(identity)

    if any_unrecognized:
        return {"status": ResidencyAuthority.UNKNOWN,
                "basis": "an unidentified/unusable/conflicting resident entry"}
    if len(models) > 1:
        return {"status": ResidencyAuthority.CONFLICTING,
                "basis": f"{len(models)} models simultaneously resident"}

    sole_identity = identities[0]
    if expected_model_name not in sole_identity:
        return {"status": ResidencyAuthority.FAILED,
                "basis": f"sole resident identity {sole_identity!r} does not include "
                         f"expected {expected_model_name!r}"}
    return {"status": ResidencyAuthority.SUCCEEDED,
            "basis": f"{expected_model_name!r} positively confirmed as the sole resident model"}


# ============================================== Provider adapters

def _duck_get(obj, key, default=None):
    """Best-effort dict-style read -- works for a plain JSON dict AND for
    ollama.Client.chat()'s real typed SubscriptableBaseModel response
    (both support .get(key, default)), matching ui_turn_diagnostics.
    _duck_get()'s own established contract (never isinstance-checked
    against a dict, since a real chat response from this client is NOT
    a plain dict)."""
    if obj is None:
        return default
    try:
        return obj.get(key, default)
    except Exception:
        return default


def _mlxserve_chat(model, messages, options, format, base_url):
    client = ollama.Client(host=base_url)

    dispatched_model = MLXSERVE_MODEL_MAP.get(model)
    if dispatched_model is None:
        return ProviderResult(
            provider=PROVIDER_MLX_SERVE, status=UNSUPPORTED_BEHAVIOR,
            model_requested=model,
            error=(f"mlx-serve V0 supports only logical model "
                   f"{ANAXI_DEFAULT_OLLAMA_MODEL!r} / qualified alias "
                   f"{QUALIFIED_MLXSERVE_MODEL!r}"),
        )

    # Stage A/B did not qualify image-bearing messages. Refuse the existing
    # vision pathway rather than silently treating text and vision as portable.
    if any(type(message) is dict and message.get("images") for message in messages):
        return ProviderResult(
            provider=PROVIDER_MLX_SERVE, status=UNSUPPORTED_BEHAVIOR,
            model_requested=model, model_dispatched=dispatched_model,
            error="mlx-serve V0 does not support the unqualified image pathway",
        )

    option_names = set(options or {})
    unsupported = sorted(MLXSERVE_IGNORED_OPTIONS & option_names)
    unknown = sorted(option_names - MLXSERVE_SUPPORTED_OPTIONS - MLXSERVE_IGNORED_OPTIONS)
    if unknown:
        return ProviderResult(
            provider=PROVIDER_MLX_SERVE, status=UNSUPPORTED_BEHAVIOR,
            model_requested=model, model_dispatched=dispatched_model,
            error=f"mlx-serve V0 options were not qualified: {unknown!r}",
            unsupported_options=unknown,
        )

    # num_ctx is known ignored by mlx-serve v26.9.2. Keep it observable in
    # ProviderResult, but do not send an actuator known to have no effect.
    forwarded_options = {
        key: value for key, value in (options or {}).items()
        if key in MLXSERVE_SUPPORTED_OPTIONS
    }

    before = _establish_sole_resident(client.ps, dispatched_model)
    if before["status"] != ResidencyAuthority.SUCCEEDED:
        return ProviderResult(
            provider=PROVIDER_MLX_SERVE, status=RESIDENCY_NOT_ESTABLISHED,
            model_requested=model, model_dispatched=dispatched_model,
            error=f"pre-call residency not established: {before['basis']}",
            unsupported_options=unsupported, diagnostics={"residency_before": before})

    exc = None
    raw = None
    try:
        raw = client.chat(
            model=dispatched_model, messages=messages, format=format,
            options=forwarded_options,
        )
    except Exception as e:
        exc = e

    after = _establish_sole_resident(client.ps, dispatched_model)

    diagnostics = {"residency_before": before, "residency_after": after}
    if exc is not None:
        diagnostics["request_exception"] = exc

    # A failed post-check outranks the request outcome: the qualified request
    # window was not established. Preserve any simultaneous raw exception in
    # diagnostics so the second fact is not erased.
    if after["status"] != ResidencyAuthority.SUCCEEDED:
        return ProviderResult(
            provider=PROVIDER_MLX_SERVE, status=RESIDENCY_NOT_ESTABLISHED,
            model_requested=model, model_dispatched=dispatched_model, raw=raw,
            error=(f"post-call residency not established: {after['basis']}"
                   + (f"; request also raised {type(exc).__name__}: {exc}"
                      if exc is not None else "")),
            unsupported_options=unsupported, diagnostics=diagnostics)

    if exc is not None:
        return ProviderResult(
            provider=PROVIDER_MLX_SERVE, status=PROVIDER_ERROR,
            model_requested=model, model_dispatched=dispatched_model,
            error=f"{type(exc).__name__}: {exc}", unsupported_options=unsupported,
            diagnostics=diagnostics)

    content = _duck_get(_duck_get(raw, "message"), "content")
    if not isinstance(content, str):
        return ProviderResult(
            provider=PROVIDER_MLX_SERVE, status=MALFORMED_RESPONSE,
            model_requested=model, model_dispatched=dispatched_model,
            raw=raw, error="response missing a usable message.content string",
            unsupported_options=unsupported,
            diagnostics=diagnostics)

    self_reported_model = _duck_get(raw, "model")
    if self_reported_model != dispatched_model:
        return ProviderResult(
            provider=PROVIDER_MLX_SERVE, status=MALFORMED_RESPONSE,
            model_requested=model, model_dispatched=dispatched_model,
            model_self_reported=self_reported_model, raw=raw,
            error=("response model self-report conflicts with the qualified "
                   "requested/resident alias"),
            unsupported_options=unsupported, diagnostics=diagnostics)

    done = _duck_get(raw, "done")
    done_reason = _duck_get(raw, "done_reason")
    prompt_eval_count = _duck_get(raw, "prompt_eval_count")
    eval_count = _duck_get(raw, "eval_count")
    if done is True and done_reason == "length":
        status = TRUNCATED
    elif (done is not True or done_reason != "stop"
          or type(prompt_eval_count) is not int or prompt_eval_count < 0
          or type(eval_count) is not int or eval_count < 0):
        return ProviderResult(
            provider=PROVIDER_MLX_SERVE, status=MALFORMED_RESPONSE,
            model_requested=model, model_dispatched=dispatched_model,
            model_self_reported=self_reported_model, raw=raw,
            error="response lacks qualified normal-completion/accounting fields",
            unsupported_options=unsupported, diagnostics=diagnostics)
    else:
        status = SUCCESS

    return ProviderResult(
        provider=PROVIDER_MLX_SERVE, status=status, model_requested=model,
        model_dispatched=dispatched_model, model_self_reported=self_reported_model,
        raw=raw, unsupported_options=unsupported, diagnostics=diagnostics)


def dispatch(provider, model, messages, options=None, format=None, base_url=None):
    """Send one self-contained inference request through the resolved
    provider and return a ProviderResult. Never called for the Ollama
    default path today -- llama_anaxi.py's call_llama()/ask_llama_for_json()
    keep calling ollama.chat(...) directly for that path, unchanged, so
    existing Ollama production behavior carries zero risk from this
    module's existence (P5). This function exists for the mlx-serve path
    and for any future caller that wants one explicit entry point for
    both providers."""
    if provider == PROVIDER_MLX_SERVE:
        resolved_base_url = base_url or os.environ.get(MLXSERVE_URL_ENV_VAR, DEFAULT_MLXSERVE_URL)
        return _mlxserve_chat(model, messages, options or {}, format, resolved_base_url)
    raise ValueError(f"dispatch() does not handle provider {provider!r} directly -- "
                      f"the Ollama path stays inline in its existing wrapper functions")
