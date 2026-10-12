"""Observability-only per-turn diagnostic instrumentation for the
ordinary Gradio conversation callback path.

Purpose: the NEXT natural conversational failure (recurrent turn-
accumulation UI failure -- response latency increases over several
exchanges, then Gradio shows a generic red Error) should leave a
durable, bounded, host-side record precise enough to locate its
failure boundary automatically, without Alex needing to race DevTools
or screenshot the instant of failure.

This module changes NO conversational behavior. Every write is
best-effort: a failure inside this module's own logging never
propagates and never turns a successful turn into a failed one (see
`_append_record`'s bare except). The one thing this module is
explicitly designed to observe -- and never swallow -- is a real
exception escaping the wrapped waking-turn call; see
`wrap_conversation_callback()`, which always re-raises exactly what it
caught, unchanged, after recording it. This is unchanged by OWC7-P2 --
containment of ConversationDirectionFailure (deciding what a human
sees so Gradio's own callback survives) happens one layer up, in
llama_gui.py, entirely outside this module; wrap_conversation_callback()
itself still always re-raises, and its own `callback_success` field
keeps its original, narrower meaning (see `waking_turn_success` /
`control_failure_contained` below for the fields that distinguish a
contained control failure from a genuinely completed turn).

Frozen exclusions (spec section 15):
  - append-only, never overwritten;
  - completely separate from canonical provenance, Clark's memory,
    hippocampal retrieval, Sleep/REM, and WSP3/private -- this module
    imports nothing from any of those, by construction (see the
    import list below: stdlib only);
  - no hidden chain-of-thought, no model-generated summaries, no
    private-space content;
  - no complete prompts/responses -- counts, sizes, and timings only.
"""
import json
import os
import threading
import time
import traceback

try:
    import resource  # POSIX only; this project's actual production
    # platform is Windows, so this import will normally fail and the
    # resource snapshot below degrades to thread-count only -- no new
    # heavy dependency (e.g. psutil) was added merely for this.
except ImportError:
    resource = None

DEFAULT_LOG_PATH = "logs/ui_turn_diagnostics.jsonl"

# Bounded -- a captured traceback is diagnostic evidence, not a
# license to dump unlimited text; long enough to show the actual
# failure frame, never the full prompt/response content that frame
# might be holding in local variables (only the traceback's own
# formatted text is captured, never locals()).
MAX_TRACEBACK_CHARS = 4000
MAX_EXCEPTION_MESSAGE_CHARS = 2000

# OWC7-P2: bounded, failure-only preview of Pass-1's own raw output
# when that output fails structural/protocol validation -- the
# already-returned-to-host text, never re-sent to a model, never
# captured for a successful Pass-1 (see attach_pass1_failure_preview()
# below; there is deliberately no equivalent for the success path --
# spec section 14, "no unnecessary enlargement of observability").
MAX_RAW_PASS1_FAILURE_PREVIEW_CHARS = 500

_lock = threading.Lock()
_turn_counter = {"n": 0}
_model_call_local = threading.local()


def _next_turn_ordinal():
    """Monotonic per-process counter -- 'turn ordinal within current
    live GUI session' (spec section 7). Resets only on process
    restart, exactly matching what 'current live GUI session' means
    for this single-process application."""
    with _lock:
        _turn_counter["n"] += 1
        return _turn_counter["n"]


def reset_turn_counter():
    """Test-only. Never called from the ordinary waking/GUI path."""
    with _lock:
        _turn_counter["n"] = 0


def _now_ms():
    return time.monotonic() * 1000.0


# ============================================= V0 relational-history binding


def record_pass2_native_history(**facts):
    """Mechanical facts about the native retrieved-history projection for one pass2 turn:
    the byte bound the compositor reserved, the byte total delivered, whether a historical
    conversation region was projected, how many turn groups / record carriers were dropped to
    fit, and how many items were withheld by reconstruction. NO content -- not prose, not
    an event id, not a component id -- matching this module's established no-private-content
    rule. A no-op if tracking was never started for this thread/turn."""
    entries = getattr(_model_call_local, "native_history", None)
    if entries is None:
        return
    entries.append(dict(facts))


def record_pass2_native_history_error(error_type, detail):
    """A projection defect that was contained (the turn continued without retrieved history).
    Recorded by type and a short message so a recurring defect is visible in the trace without
    ever putting the offending text in the trace itself."""
    entries = getattr(_model_call_local, "native_history", None)
    if entries is None:
        return
    entries.append({"error": error_type, "detail": detail[:200]})


def get_and_clear_native_history():
    entries = getattr(_model_call_local, "native_history", [])
    _model_call_local.native_history = []
    return entries


# ======================================================= model-call timing


def start_model_call_tracking():
    """Begins a fresh per-thread accumulator for this turn's model
    calls. Gradio runs each callback in its own worker thread (see
    llama_gui.py's own respond() docstring), so thread-local storage
    is the correct, already-established isolation boundary -- no
    cross-turn contamination between concurrent callbacks."""
    _model_call_local.calls = []
    _model_call_local.tracking_started_ms = _now_ms()
    _model_call_local.local_token_counts = {"count": 0, "duration_ms": 0.0}
    _model_call_local.human_input_event_id = None
    _model_call_local.pass1_completion = None  # OWC5-P4: see record_pass1_completion_metadata() below
    _model_call_local.pass2_completion = None  # OWC8-S1: see record_pass2_completion_metadata() below
    _model_call_local.context_budget_results = []  # OWC9: see record_context_budget_result() below
    _model_call_local.native_history = []  # V0 binding: see record_pass2_native_history() below


def record_human_input_event_id(event_id):
    """Exact canonical linkage for mechanical failure correlation; no input text."""
    _model_call_local.human_input_event_id = event_id


# The provider's own mechanical counters/completion facts of one call
# (numbers, booleans, bounded enum/tag strings only; never content).
PROVIDER_COUNTER_KEYS = (
    "prompt_eval_count", "prompt_eval_cached_count", "eval_count",
    "prompt_eval_duration", "eval_duration", "load_duration", "total_duration",
)


def provider_counters(response):
    """{counter: int, model: tag} read from one provider response (a dict or the client's response object);
    {} for anything else.  Never raises."""
    if response is None or isinstance(response, (str, bytes, list, tuple)):
        return {}
    out = {}
    for key in PROVIDER_COUNTER_KEYS + ("model", "done", "done_reason"):
        try:
            value = response.get(key) if hasattr(response, "get") else getattr(response, key, None)
        except Exception:
            value = None
        if ((key in PROVIDER_COUNTER_KEYS and type(value) is int)
                or (key == "model" and isinstance(value, str) and len(value) <= 128)
                or (key == "done" and type(value) is bool)
                or (key == "done_reason" and isinstance(value, str) and len(value) <= 64)):
            out[key] = value
    prompt_ns = out.get("prompt_eval_duration")
    prompt_count = out.get("prompt_eval_count")
    eval_ns = out.get("eval_duration")
    eval_count = out.get("eval_count")
    if type(prompt_ns) is int and prompt_ns > 0 and type(prompt_count) is int:
        out["prompt_tokens_per_second"] = round(prompt_count / (prompt_ns / 1e9), 2)
    if type(eval_ns) is int and eval_ns > 0 and type(eval_count) is int:
        out["generation_tokens_per_second"] = round(eval_count / (eval_ns / 1e9), 2)
    return out


def note_provider_response(response):
    """A model call whose wrapper returns only the content (e.g. ask_llama_for_json) notes its provider
    response here, so the enclosing timed_model_call can record that call's counters."""
    noted = getattr(_model_call_local, "noted_counters", None) or {}
    noted.update(provider_counters(response))
    _model_call_local.noted_counters = noted


def note_provider_request(*, model, num_ctx=None, num_predict=None, format_mode=None):
    """Attach content-free request mechanics to the timed call in progress.

    The request text and schema are deliberately excluded.  This is diagnostic
    attribution only: it cannot affect provider arguments or model-visible data.
    """
    calls = getattr(_model_call_local, "calls", None)
    if calls is None:
        return
    noted = getattr(_model_call_local, "noted_counters", None) or {}
    if isinstance(model, str) and len(model) <= 128:
        noted["requested_model"] = model
    if type(num_ctx) is int:
        noted["num_ctx"] = num_ctx
    if type(num_predict) is int:
        noted["num_predict"] = num_predict
    if format_mode in ("plain", "json", "json_schema"):
        noted["format_mode"] = format_mode
    _model_call_local.noted_counters = noted


def record_local_token_count(duration_ms):
    """One exact local prompt-token count (no model call): counted and timed in aggregate per turn."""
    counts = getattr(_model_call_local, "local_token_counts", None)
    if counts is not None:
        counts["count"] += 1
        counts["duration_ms"] += duration_ms


def get_local_token_counts():
    counts = getattr(_model_call_local, "local_token_counts", None) or {"count": 0, "duration_ms": 0.0}
    return {"count": counts["count"], "duration_ms": round(counts["duration_ms"], 2)}


def record_model_call(purpose, duration_ms, success, *, start_ms=None, counters=None):
    calls = getattr(_model_call_local, "calls", None)
    if calls is None:
        return  # tracking was never started for this thread/turn -- no-op, never raises
    entry = {"ordinal": len(calls) + 1, "purpose": purpose,
             "duration_ms": round(duration_ms, 2), "success": success}
    started = getattr(_model_call_local, "tracking_started_ms", None)
    if start_ms is not None and started is not None:
        entry["start_offset_ms"] = round(start_ms - started, 2)
    if counters:
        entry.update(counters)
    calls.append(entry)


def get_and_clear_model_calls():
    calls = getattr(_model_call_local, "calls", [])
    _model_call_local.calls = []
    return calls


# ================================================== OWC9: context budget


def record_context_budget_result(pass_name, result):
    """OWC9 (spec section 15): bounded, generic diagnostics proving
    compositor behavior -- mechanical facts only (kind names, counts,
    token estimates, the fixed budget constants), NEVER the actual
    rendered content of any contribution (no private content, no
    resource text, no human/Clark prose -- existing privacy rules
    unchanged). A no-op if tracking was never started for this thread/
    turn, matching record_model_call()'s own established convention.
    `result` is a context_budget.CompositionResult; this function has
    no import of context_budget itself (stdlib-only diagnostics module,
    matching this file's own established dependency-free design) --
    it only reads plain attributes, duck-typed."""
    results = getattr(_model_call_local, "context_budget_results", None)
    if results is None:
        return
    per_kind = {}
    for c in result.included:
        per_kind[c.kind] = {
            "cost": c.cost,
            "unit_count": len(c.droppable_units) if c.droppable_units is not None else None,
        }
    results.append({
        "pass": pass_name,
        "context_ceiling": result.max_prompt_budget,  # the PASS-SPECIFIC max prompt budget, not the raw ceiling
        "final_prompt_cost": result.final_prompt_cost,
        "fits": result.fits,
        "kinds_included": sorted(per_kind.keys()),
        "per_kind_cost": per_kind,
        "kinds_dropped": list(result.dropped_kinds),
        "kinds_trimmed": list(result.trimmed_kinds),
        "estimator_method": "utf8_byte_count_upper_bound",
    })


def record_payload_preflight(*, field, outcome, method, prompt_tokens, allowance, attempt=1):
    """The capacity preflight of one atomic Pass-1 payload call (2026-10-08), beside the turn's other
    context-budget results: field name, outcome (FITS_ATOMIC_WINDOW / ATOMIC_WINDOW_INSUFFICIENT), how
    the prompt was counted, and the token figures -- mechanical facts only, never any content."""
    results = getattr(_model_call_local, "context_budget_results", None)
    if results is None:
        return
    results.append({
        "pass": "pass1_payload_preflight", "field": field, "outcome": outcome, "prompt_count_method": method,
        "prompt_tokens": prompt_tokens, "allowance": allowance, "attempt": attempt,
    })


def record_payload_result(*, field, outcome, termination, eval_count, allowance, attempt=1):
    """The mechanical outcome of one atomic payload generation (COMPLETED, PAYLOAD_OUTPUT_LIMIT_REACHED,
    INCOMPLETE_MODEL_COMPLETION, PAYLOAD_MALFORMED, ...) with the provider's termination and token
    figures -- never any words.  Counting outcomes measures observed demand; it proves nothing."""
    results = getattr(_model_call_local, "context_budget_results", None)
    if results is None:
        return
    results.append({
        "pass": "pass1_payload_result", "field": field, "outcome": outcome, "termination": termination,
        "eval_count": eval_count, "allowance": allowance, "attempt": attempt,
    })


def record_waymark_event(**facts):
    """WAYMARK V0 mechanical evidence beside the turn's other budget/payload results (bounded facts built by
    llama_anaxi._record_waymark_event; never canonical history, never continuity material)."""
    results = getattr(_model_call_local, "context_budget_results", None)
    if results is None:
        return
    results.append(dict({"pass": "waymark"}, **facts))


def get_and_clear_context_budget_results():
    results = getattr(_model_call_local, "context_budget_results", [])
    _model_call_local.context_budget_results = []
    return results


def timed_model_call(purpose, fn, *args, **kwargs):
    """Wraps exactly one model-call invocation with host-side
    monotonic duration capture, recorded into this thread's current-
    turn accumulator. `purpose` is an explicit, caller-supplied label
    (e.g. "pass1", "pass2", "artifact_judgment", "pass2_regen") --
    never guessed after the fact from call order or exception type.

    Always records first, on success OR failure, then returns/raises
    EXACTLY what the wrapped call itself would have -- this function
    observes; it never changes what happens."""
    start = _now_ms()
    _model_call_local.noted_counters = None
    try:
        result = fn(*args, **kwargs)
    except Exception:
        record_model_call(purpose, _now_ms() - start, False, start_ms=start,
                          counters=getattr(_model_call_local, "noted_counters", None))
        raise
    counters = dict(getattr(_model_call_local, "noted_counters", None) or {})
    # A wrapper may return the provider object directly (payload calls) or
    # return only content after note_provider_response().  Merge both shapes
    # so request mechanics are not discarded by a direct response.
    counters.update(provider_counters(result))
    record_model_call(purpose, _now_ms() - start, True, start_ms=start, counters=counters)
    return result


def timed_stage(purpose, fn, *args, **kwargs):
    """Same contract as timed_model_call(), for a non-model host stage
    (e.g. canonical persistence) that should also appear in the
    per-turn record's stage breakdown, kept in the SAME accumulator so
    'total model-call duration' vs 'everything else' can be computed
    correctly (persistence is explicitly excluded from the model-call
    total in wrap_conversation_callback() below by its purpose label)."""
    start = _now_ms()
    try:
        result = fn(*args, **kwargs)
    except Exception:
        record_model_call(purpose, _now_ms() - start, False)
        raise
    record_model_call(purpose, _now_ms() - start, True)
    return result


_NON_MODEL_STAGE_PURPOSES = {
    "context_prepare", "pass1_context_composition", "pass2_context_composition",
    "workspace_pass1_context_composition", "workspace_pass2_context_composition",
    "historical_search_or_read", "workspace_resource_action",
    "workspace_claim_witness_read", "persistence",
}
_PERSISTENCE_STAGE_PURPOSES = {"persistence"}

# OWC5-P4: field names for the observational Pass-1 completion-
# metadata block, in one place so wrap_conversation_callback()'s two
# branches and the "nothing captured" default stay in sync by
# construction rather than by two independently-maintained literal
# dicts.
_PASS1_COMPLETION_FIELDS = (
    "pass1_response_content_chars",
    "pass1_done",
    "pass1_done_reason",
    "pass1_eval_count",
    "pass1_prompt_eval_count",
    "pass1_total_duration",
    "pass1_load_duration",
    "pass1_eval_duration",
    "pass1_thinking_chars",
)


def _duck_get(obj, key, default=None):
    """Best-effort dict-style read -- works for a plain dict AND for
    the installed ollama client's SubscriptableBaseModel response
    objects (both support .get(key, default)), without this module
    importing ollama or any typed client class (stdlib-only contract
    preserved)."""
    try:
        return obj.get(key, default)
    except Exception:
        return default


def record_pass1_completion_metadata(response):
    """OWC5-P4 (spec section A4): observation-only capture of Ollama's
    own completion metadata for a Pass-1 (or the shared
    ask_llama_for_json() artifact-judgment) call -- NEVER content,
    NEVER used for validation/control. `response` is duck-typed, never
    imported/type-checked against ollama's own classes.

    pass1_thinking_chars is a LENGTH ONLY, computed from
    message['thinking'] -- the hidden-reasoning TEXT ITSELF is read
    only long enough to call len() on it and is never stored, logged,
    or returned anywhere.

    A no-op if start_model_call_tracking() was never called for this
    thread (matches record_model_call()'s existing convention). If
    ask_llama_for_json() is called more than once in a single turn
    (e.g. the separate artifact-judgment pass, which always runs
    BEFORE the OWC5 typed-act Pass-1 call within the same turn when
    both occur), the LAST call's metadata is what's recorded -- by
    construction that is always the real Pass-1 call whenever both
    run in the same turn. Never raises -- observation must never
    affect a real turn."""
    calls = getattr(_model_call_local, "calls", None)
    if calls is None:
        return  # tracking was never started for this thread/turn -- no-op
    try:
        message = _duck_get(response, "message", {}) or {}
        content = _duck_get(message, "content")
        thinking = _duck_get(message, "thinking")
        _model_call_local.pass1_completion = {
            "pass1_response_content_chars": len(content) if isinstance(content, str) else None,
            "pass1_done": _duck_get(response, "done"),
            "pass1_done_reason": _duck_get(response, "done_reason"),
            "pass1_eval_count": _duck_get(response, "eval_count"),
            "pass1_prompt_eval_count": _duck_get(response, "prompt_eval_count"),
            "pass1_total_duration": _duck_get(response, "total_duration"),
            "pass1_load_duration": _duck_get(response, "load_duration"),
            "pass1_eval_duration": _duck_get(response, "eval_duration"),
            "pass1_thinking_chars": len(thinking) if isinstance(thinking, str) else None,
        }
    except Exception:
        pass


def get_and_clear_pass1_completion_metadata():
    metadata = getattr(_model_call_local, "pass1_completion", None)
    _model_call_local.pass1_completion = None
    return metadata


# OWC8-S1: Pass-2 counterpart of _PASS1_COMPLETION_FIELDS above. Added
# because the live OWC/WSP2-P3 forensic had no equivalent for Pass-2
# at all and had to fall back to reading Ollama's own server log by
# hand to prove the two MALFORMED_EXPRESSION failures were caused by
# context-window truncation at Pass-2, not a token-limit-independent
# malformed expression.
_PASS2_COMPLETION_FIELDS = (
    "pass2_response_content_chars",
    "pass2_done",
    "pass2_done_reason",
    "pass2_eval_count",
    "pass2_prompt_eval_count",
    "pass2_total_duration",
    "pass2_load_duration",
    "pass2_eval_duration",
    "pass2_thinking_chars",
)


def record_pass2_completion_metadata(response):
    """OWC8-S1: observation-only capture of Ollama's own completion
    metadata for a Pass-2 (call_llama()) invocation -- NEVER content,
    NEVER used for validation/control. Exact same contract as
    record_pass1_completion_metadata() above, mirrored field-for-field
    with a `pass2_` prefix.

    pass2_thinking_chars is a LENGTH ONLY, computed from
    message['thinking'] -- the hidden-reasoning TEXT ITSELF is read
    only long enough to call len() on it and is never stored, logged,
    or returned anywhere. pass2_response_content_chars is likewise a
    LENGTH ONLY -- the actual Clark expression text is never captured
    by this function.

    A no-op if start_model_call_tracking() was never called for this
    thread. call_llama() is shared by the real OWC5 Pass-2 expression
    call and by TASK-mode's pass2_task_mode/pass2_regen calls -- if
    more than one runs in a single turn, the LAST call's metadata is
    what's recorded, exactly mirroring record_pass1_completion_
    metadata()'s own documented multi-call convention. Never raises --
    observation must never affect a real turn."""
    calls = getattr(_model_call_local, "calls", None)
    if calls is None:
        return  # tracking was never started for this thread/turn -- no-op
    try:
        message = _duck_get(response, "message", {}) or {}
        content = _duck_get(message, "content")
        thinking = _duck_get(message, "thinking")
        _model_call_local.pass2_completion = {
            "pass2_response_content_chars": len(content) if isinstance(content, str) else None,
            "pass2_done": _duck_get(response, "done"),
            "pass2_done_reason": _duck_get(response, "done_reason"),
            "pass2_eval_count": _duck_get(response, "eval_count"),
            "pass2_prompt_eval_count": _duck_get(response, "prompt_eval_count"),
            "pass2_total_duration": _duck_get(response, "total_duration"),
            "pass2_load_duration": _duck_get(response, "load_duration"),
            "pass2_eval_duration": _duck_get(response, "eval_duration"),
            "pass2_thinking_chars": len(thinking) if isinstance(thinking, str) else None,
        }
    except Exception:
        pass


def get_and_clear_pass2_completion_metadata():
    metadata = getattr(_model_call_local, "pass2_completion", None)
    _model_call_local.pass2_completion = None
    return metadata


# ================================================================ helpers


def _json_serializable(value):
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError, RecursionError):
        return False


def _resource_snapshot():
    """Best-effort, cheap, one-shot -- never polled continuously, never
    a new heavy dependency. RSS via the stdlib `resource` module where
    available (POSIX); always includes thread count, which is free and
    portable."""
    snapshot = {"thread_count": threading.active_count()}
    if resource is not None:
        try:
            snapshot["rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except Exception:
            pass
    return snapshot


def _bounded_traceback(exc):
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(tb) > MAX_TRACEBACK_CHARS:
        tb = tb[:MAX_TRACEBACK_CHARS] + "...[truncated]"
    return tb


def _classify_stage(exc):
    """Best-effort and honest: 'unknown' rather than a guessed stage
    when nothing recognized matches. Classifies purely from the
    exception object already raised to this callback boundary --
    never by reaching back into run_waking_turn()'s own internals,
    which this module does not import or modify beyond the explicit,
    additive timed_model_call()/timed_stage() call sites."""
    name = type(exc).__name__
    if name == "ConversationDirectionFailure":
        return getattr(exc, "stage", "conversation_direction_unknown")
    if name in ("StagingDurabilityError", "StagingIndeterminateStateError"):
        return "persistence"
    if name == "StaleSchemaError":
        return "artifact_construction"
    message = str(exc)
    if message.startswith("post-canonical-commit artifact write failed"):
        return "post_commit_artifact_write"
    return "unknown"


def bounded_pass1_failure_preview(raw_output):
    """Bounds `raw_output` (Pass-1's own raw return value -- a str, or
    whatever ask_llama_for_json() returned) to
    MAX_RAW_PASS1_FAILURE_PREVIEW_CHARS. No content-aware redaction is
    attempted -- deterministic sanitization cannot safely distinguish
    an obvious verbatim prompt echo from ordinary malformed model
    prose without risking either a false-positive (corrupting genuine
    diagnostic evidence) or a false-negative (a redaction rule
    confident enough to be wrong); per spec section 13, truncation
    alone is the mandatory, honestly-reported fallback here. Never
    raises -- returns a safe fallback preview on any internal error.
    Returns (preview, total_chars, truncated)."""
    try:
        if isinstance(raw_output, str):
            text = raw_output
        else:
            try:
                text = json.dumps(raw_output)
            except Exception:
                text = repr(raw_output)
    except Exception:
        text = ""
    total_chars = len(text)
    preview = text[:MAX_RAW_PASS1_FAILURE_PREVIEW_CHARS]
    truncated = total_chars > MAX_RAW_PASS1_FAILURE_PREVIEW_CHARS
    return preview, total_chars, truncated


def attach_pass1_failure_preview(exc, raw_output):
    """Attaches a bounded, failure-only raw-Pass-1-output preview to an
    already-constructed ConversationDirectionFailure instance, as
    plain diagnostic attributes -- called by the caller (llama_anaxi.py)
    ONLY on the structural-validation-failure path, before raising,
    exactly like `.stage` is already attached by that exception's own
    constructor. Does not change what the exception IS, does not
    change conversation_direction.py, and never itself raises --
    annotating diagnostics must never block a real failure from
    propagating unchanged."""
    try:
        preview, total_chars, truncated = bounded_pass1_failure_preview(raw_output)
        exc.raw_pass1_output_preview = preview
        exc.raw_pass1_output_total_chars = total_chars
        exc.raw_pass1_output_truncated = truncated
    except Exception:
        pass


def _append_record(record, log_path):
    try:
        parent = os.path.dirname(os.path.abspath(log_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # observability must never itself become a failure source


# ==================================================== the callback wrapper


def get_last_callback_display_metadata():
    """Same-thread mechanical timestamp already recorded by the callback.

    No transcript, disk scan, new clock read, or canonical event is introduced.
    """
    return dict(getattr(_model_call_local, 'callback_display_metadata', {}))


def wrap_conversation_callback(message, history, run_fn, *, log_path=DEFAULT_LOG_PATH, session_id_fn=None,
                               recovery_id=None):
    """The ONE function that wraps the outermost ordinary Gradio
    conversation callback boundary (spec section 8/9). `run_fn` is a
    zero-argument closure over whatever the caller's own
    run_waking_turn() call actually is -- this module has no
    dependency on ANAXI's internal call signature, only on timing it
    and observing what it returns or raises.

    Never swallows a failure: if run_fn() raises, this function
    records the failure and then RE-RAISES the exact same exception
    object, completely unchanged -- Gradio's existing behavior
    (converting an uncaught exception into its own generic UI Error)
    is fully preserved. Instrumentation observes; it does not repair.
    """
    turn_ordinal = _next_turn_ordinal()
    start_wall = time.time()
    start_mono = _now_ms()
    _model_call_local.callback_display_metadata = {}
    resource_start = _resource_snapshot()
    start_model_call_tracking()

    history_item_count = len(history) if history is not None else None
    history_json_serializable = _json_serializable(history)
    history_char_count = None
    if history_json_serializable:
        try:
            history_char_count = len(json.dumps(history))
        except Exception:
            history_char_count = None

    session_id = None
    if session_id_fn is not None:
        try:
            session_id = session_id_fn()
        except Exception:
            session_id = None

    record = {
        "timestamp_start": start_wall,
        "turn_ordinal": turn_ordinal,
        "session_id": session_id,
        "incoming_message_char_count": len(message) if isinstance(message, str) else None,
        "visible_history_item_count": history_item_count,
        "visible_history_char_count": history_char_count,
        "visible_history_json_serializable": history_json_serializable,
        "resource_start": resource_start,
    }
    if recovery_id is not None:
        record["recovery_id"] = recovery_id

    try:
        result = run_fn()
    except Exception as exc:
        duration_ms = _now_ms() - start_mono
        stages = get_and_clear_model_calls()
        # OWC7-P2 (spec section 15, diagnostic honesty freeze):
        # callback_success keeps its exact pre-existing meaning --
        # whether run_fn() raised past this wrapper -- unchanged, so no
        # existing reader/test of this field is misled. waking_turn_
        # success and control_failure_contained are new, explicit,
        # additive fields so a contained ConversationDirectionFailure
        # (identified the same way _classify_stage() already does --
        # by class name, never by importing conversation_direction.py
        # into this stdlib-only module) can never be mistaken for a
        # completed Clark turn, regardless of whether the OUTER Gradio
        # boundary (llama_gui.py) subsequently contains it gracefully
        # or lets it propagate.
        is_control_failure = type(exc).__name__ == "ConversationDirectionFailure"
        pass1_completion = get_and_clear_pass1_completion_metadata()
        pass2_completion = get_and_clear_pass2_completion_metadata()
        context_budget_results = get_and_clear_context_budget_results()
        record.update({
            "timestamp_end": time.time(),
            "callback_duration_ms": round(duration_ms, 2),
            "callback_success": False,
            "waking_turn_success": False,
            "control_failure_contained": is_control_failure,
            "exception_type": type(exc).__name__,
            "exception_module": type(exc).__module__,
            "exception_message": str(exc)[:MAX_EXCEPTION_MESSAGE_CHARS],
            "exception_traceback": _bounded_traceback(exc),
            "pipeline_stage": _classify_stage(exc),
            **{field: (pass1_completion or {}).get(field) for field in _PASS1_COMPLETION_FIELDS},
            **{field: (pass2_completion or {}).get(field) for field in _PASS2_COMPLETION_FIELDS},
            # OWC7-P2 (spec section 12): only populated when
            # llama_anaxi.py's structural-validation-failure branch
            # called attach_pass1_failure_preview() on this exact
            # exception object before raising it; None otherwise
            # (e.g. a non-control exception, or a cross-validation
            # failure like UNAUTHORIZED_RELINQUISH, which never has a
            # raw preview attached -- its act already parsed cleanly).
            "raw_pass1_output_preview": getattr(exc, "raw_pass1_output_preview", None),
            "raw_pass1_output_total_chars": getattr(exc, "raw_pass1_output_total_chars", None),
            "raw_pass1_output_truncated": getattr(exc, "raw_pass1_output_truncated", None),
            # Content-free terminal-marker facts for a rejected plain Pass 2
            # (see conversation_direction.describe_pass2_marker_shape).
            "pass2_marker_shape": getattr(exc, "pass2_marker_shape", None),
            "stages": stages,
            "local_token_counts": get_local_token_counts(),
            "context_budget_results": context_budget_results,
            "resource_end": _resource_snapshot(),
        })
        record['human_input_event_id'] = getattr(_model_call_local, 'human_input_event_id', None)
        _append_record(record, log_path)
        _model_call_local.callback_display_metadata = {
            'timestamp_end': record['timestamp_end'],
            # Exact per-attempt H identity, established by run_waking_turn()
            # before model-adjacent work.  The GUI uses it only for a fresh,
            # read-only WTR0 eligibility receipt; it is not a second truth
            # store and is never inferred from "latest unanswered" state.
            'human_input_event_id': record['human_input_event_id'],
        }
        raise

    duration_ms = _now_ms() - start_mono
    stages = get_and_clear_model_calls()
    model_stage_durations = [s["duration_ms"] for s in stages if s["purpose"] not in _NON_MODEL_STAGE_PURPOSES]
    total_model_ms = sum(model_stage_durations)
    persistence_durations = [s["duration_ms"] for s in stages if s["purpose"] in _PERSISTENCE_STAGE_PURPOSES]
    total_persistence_ms = sum(persistence_durations)

    response_str = result.get("reply") if isinstance(result, dict) else result
    typed_act = result.get("_diagnostic_typed_act") if isinstance(result, dict) else None
    pass1_completion = get_and_clear_pass1_completion_metadata()
    pass2_completion = get_and_clear_pass2_completion_metadata()
    context_budget_results = get_and_clear_context_budget_results()

    record.update({
        "timestamp_end": time.time(),
        "callback_duration_ms": round(duration_ms, 2),
        "callback_success": True,
        "waking_turn_success": True,
        "control_failure_contained": False,
        **{field: (pass1_completion or {}).get(field) for field in _PASS1_COMPLETION_FIELDS},
        **{field: (pass2_completion or {}).get(field) for field in _PASS2_COMPLETION_FIELDS},
        "context_budget_results": context_budget_results,
        "stages": stages,
        "local_token_counts": get_local_token_counts(),
        "total_model_call_duration_ms": round(total_model_ms, 2),
        "total_persistence_duration_ms": round(total_persistence_ms, 2),
        "host_transition_duration_ms": round(duration_ms - total_model_ms - total_persistence_ms, 2),
        "typed_act": typed_act,
        "response_type": type(result).__name__,
        "response_char_count": len(response_str) if isinstance(response_str, str) else None,
        "response_json_serializable": _json_serializable(result),
        "resource_end": _resource_snapshot(),
    })
    record['human_input_event_id'] = getattr(_model_call_local, 'human_input_event_id', None)
    _append_record(record, log_path)
    _model_call_local.callback_display_metadata = {
        'timestamp_end': record['timestamp_end'],
        'human_input_event_id': record['human_input_event_id'],
    }
    return result
