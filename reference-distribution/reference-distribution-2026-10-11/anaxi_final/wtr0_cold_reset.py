"""WTR0 mechanical model-runtime residency reset.

Correction-5 requires a complete recognized residency observation with usable,
consistent identities before positive absence can authorize recovery generation.
The local Correction-4 dependency trace records Ollama 0.6.2 ProcessResponse.Model
as declaring separate optional model/name strings, both defaulting to None;
it establishes no alias precedence or whitespace normalization. Exact types
remain authoritative. Digest is not a model name. A trusted target match is
sufficient for presence even when another entry is unidentified; absence needs
all entries to be trustworthy.

Trace (see the WTR0 final builder report for the full write-up): the
provider request itself (llama_anaxi.ask_llama_for_json() / call_llama(),
both `ollama.chat(model=..., messages=..., ...)`) is ALREADY genuinely
stateless at the wire level -- the complete message list is sent fresh
on every call, no server-side conversation/session id is ever passed,
and neither function passes Ollama's own `context` token-array field
(the one mechanism Ollama offers for carrying decoder state across
calls). There is no per-call provider session object to "reset" at
that layer because none is ever created or reused.

The one genuine piece of mechanical state that DOES persist across
calls at this layer is the Ollama RUNTIME itself keeping the model's
weights resident in memory/VRAM between requests (Ollama's own
`keep_alive` behavior, default ~5 minutes idle before automatic
unload). That residency is real, mechanical, and inspectable via
Ollama's own `ps` endpoint -- so THAT is what this module truthfully
resets: it explicitly requests the model be unloaded
(`keep_alive=0`) and then positively confirms, via `ollama.ps()`, that
the model is no longer resident before recovery generation may
proceed. The next generation call must then have the runtime reload
the model fresh rather than reusing an already-warm loaded instance.

This module does NOT claim to reset "Clark," conversation memory, or
canonical history -- none of that is provider/runtime state in the
first place (it lives in anaxi_provenance.db and is read fresh, via
the ordinary dialogue-window construction, on every call regardless).
It does not describe this as Clark sleeping, waking, forgetting, or
restarting. It is a mechanical model-runtime residency reset only.

`ollama_module` is always passed explicitly (never imported at module
scope) so tests can supply a fully synthetic/mocked client and this
module never triggers real model inference or a real network request
on its own.
"""
import time


class ColdResetOutcome:
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


_UNRECOGNIZED = object()


def _recognized_models_collection(loaded, ollama_module):
    """Returns the residency-entries collection (a list) if `loaded` is
    one of the two locally-justified `ps()` response shapes with its
    `models` collection positively present and of a recognized
    container type; returns `_UNRECOGNIZED` otherwise (missing field,
    wrong type, or an object shape this module does not positively
    recognize). Never returns an empty list for a shape that did not
    actually, positively carry an empty collection -- "missing" and
    "empty" are kept strictly distinct throughout.

    WTR0-CORRECTION-4's own builder-side adversarial preflight (see
    this correction's contract) found that recognizing the
    `ollama.ProcessResponse` shape via `isinstance()` reopens the exact
    `__class__`-forgery bypass WTR0-CORRECTION-3 closed for exception
    classification: an unrelated object overriding `__class__` as a
    property returning `ProcessResponse` is accepted by `isinstance()`
    even though its actual runtime type -- and therefore its `.models`
    data -- is wholly attacker/caller-controlled. `type(loaded) is
    process_response_cls` is used instead, exactly matching
    WTR0-CORRECTION-3's own exact-type-identity policy; there is no
    known legitimate subclass of `ollama.ProcessResponse` anywhere in
    this repository or its pinned dependency."""
    if type(loaded) is dict:
        if "models" not in loaded:
            return _UNRECOGNIZED
        models = loaded["models"]
        return list(models) if type(models) is list else _UNRECOGNIZED

    process_response_cls = getattr(ollama_module, "ProcessResponse", None)
    if process_response_cls is not None and type(loaded) is process_response_cls:
        models = getattr(loaded, "models", None)
        return list(models) if type(models) in (list, tuple) else _UNRECOGNIZED

    return _UNRECOGNIZED


def _usable_identity(value):
    # No trimming, coercion, fuzzy matching, or invisible/control characters.
    return (type(value) is str and bool(value) and value.isprintable()
            and not any(char.isspace() for char in value))


def _entry_model_identity(entry, ollama_module):
    """Return one consistent identity set or _UNRECOGNIZED.

    Plain dictionary fields, if present, must be usable (including rejecting
    explicit null). Typed Model optional None defaults mean unpopulated fields,
    per the locally recorded client type contract. At least one populated field
    is required; every populated field must be usable and all must agree.
    No canonical precedence between model and name is established locally.
    """
    if type(entry) is dict:
        values = [entry[key] for key in ("model", "name") if key in entry]
    else:
        response_cls = getattr(ollama_module, "ProcessResponse", None)
        model_cls = getattr(response_cls, "Model", None) if response_cls else None
        if model_cls is None or type(entry) is not model_cls:
            return _UNRECOGNIZED
        values = [getattr(entry, key, None) for key in ("model", "name")]
        values = [value for value in values if value is not None]
    if not values or not all(_usable_identity(value) for value in values):
        return _UNRECOGNIZED
    identities = set(values)
    return identities if len(identities) == 1 else _UNRECOGNIZED


def cold_reset_waking_inference_path(ollama_module, model_tag):
    """Requests unload of `model_tag` from the given ollama-like
    client/module (must expose `.chat(...)` and `.ps()`, matching the
    real `ollama` package's module-level API) and positively confirms
    the model is no longer resident afterward.

    Returns a dict:
        {"status": SUCCEEDED|FAILED|UNKNOWN, "basis": str, "checked_at": int}

    SUCCEEDED is the ONLY status under which a caller may proceed to
    recovery generation (see wtr0_waking_recovery.execute_recovery()).
    FAILED and UNKNOWN both mean generation must NOT be attempted --
    this module makes no attempt to distinguish "reset call failed"
    from "reset call succeeded but confirmation is unavailable"
    beyond the basis text; both are equally disqualifying per the
    WTR0 frozen semantics (reset must be POSITIVELY established)."""
    checked_at = int(time.time())
    if not _usable_identity(model_tag):
        return {"status": ColdResetOutcome.UNKNOWN,
                "basis": "configured model identity is unusable -- absence cannot be established",
                "checked_at": checked_at}
    try:
        ollama_module.chat(model=model_tag, messages=[], keep_alive=0)
    except Exception as exc:
        return {
            "status": ColdResetOutcome.FAILED,
            "basis": f"unload request (keep_alive=0) raised {type(exc).__name__}",
            "checked_at": checked_at,
        }

    try:
        loaded = ollama_module.ps()
    except Exception as exc:
        return {
            "status": ColdResetOutcome.UNKNOWN,
            "basis": f"unload requested, but residency confirmation via ps() raised "
                     f"{type(exc).__name__} -- reset cannot be positively established",
            "checked_at": checked_at,
        }

    try:
        models = _recognized_models_collection(loaded, ollama_module)
    except Exception:
        models = _UNRECOGNIZED
    if models is _UNRECOGNIZED:
        return {
            "status": ColdResetOutcome.UNKNOWN,
            "basis": "unload requested, but residency response is unrecognized, malformed, or "
                     "unreadable -- absence cannot be positively established",
            "checked_at": checked_at,
        }

    incomplete = False
    for entry in models:
        try:
            identity = _entry_model_identity(entry, ollama_module)
        except Exception:
            identity = _UNRECOGNIZED
        if identity is _UNRECOGNIZED:
            incomplete = True
        elif model_tag in identity:
            return {
                "status": ColdResetOutcome.FAILED,
                "basis": f"unload requested (keep_alive=0) but {model_tag!r} is still reported resident by ps()",
                "checked_at": checked_at,
            }
    if incomplete:
        return {
            "status": ColdResetOutcome.UNKNOWN,
            "basis": "unload requested, but at least one residency entry has an unidentified, "
                     "unusable, conflicting, or unreadable identity -- absence cannot be positively established",
            "checked_at": checked_at,
        }
    return {
        "status": ColdResetOutcome.SUCCEEDED,
        "basis": f"keep_alive=0 unload requested for {model_tag!r} and positively confirmed absent via "
                 f"a recognized, complete ps() observation with usable consistent model identities",
        "checked_at": checked_at,
    }
