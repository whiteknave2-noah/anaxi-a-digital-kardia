"""Windowed delivery of a supervised workspace result to the subject.

Owner law: a capability may use bounded operations, but a result the host
obtained must not silently disappear before the subject can use it, and a
resource must not become unusable merely because it is larger than one
prompt. When the executed action's result does not fit the room Pass 2 has
left, the result is delivered as a truthful WINDOW -- a real prefix of the
same result, with its continuation fields rewritten to describe exactly what
was delivered -- instead of being dropped whole. The subject continues with
the same ordinary actions (`next_request`) it already uses.

Pure functions. No model call, no I/O, no permission decision: this never
changes WHAT the host read or WHETHER an action was permitted, only how much
of the already-obtained result rides in the one prompt that carries it.
"""
import json

import context_budget
import workspace_capability as wc

DELIVERY_WINDOW_KEY = "delivery_window"
WITHHELD_KEY = "delivery_withheld"


SOURCE_TEXT_HEADER = ("Source text of {label} (the exact stored words, delimited below; they are the "
                      "document's own words as written by its author -- not words of the person you are talking with):")


def render_observation(boundary_result):
    """The exact text Pass 2 carries for a result (single source of truth).

    ROLE BINDING (live finding 2026-09-24): a read document's text used to arrive as an escaped string inside
    the result dict, in the same user-role message as host framing that addresses the subject as "you", with
    nothing marking it as the document's own words.  Real model, documents addressed to their reader (3
    documents, different third parties, 30 replies): the reader's "you" was re-bound to the live interlocutor
    in 25/30; with the stored text delimited as source under this provenance header, 7/30; a control document
    addressed to the interlocutor by name resolved correctly either way.  Every workspace text result (library,
    vault note, journal entry) is rendered this way -- a provenance fact about the text, never a statement of
    whom it addresses."""
    result = boundary_result["result"]
    if isinstance(result, dict) and isinstance(result.get("content"), str) and result["content"]:
        meta = {k: v for k, v in result.items() if k != "content"}
        label = result.get("relative_path") or result.get("entry_id") or "the item read"
        return (f"Result: {meta}\n\n" + SOURCE_TEXT_HEADER.format(label=label)
                + f"\n<<<\n{result['content']}\n>>>")
    return f"Result: {result}"


def _cost(text):
    return context_budget.estimate_tokens(text)


def _resource_class(boundary_result):
    scope = boundary_result.get("scope") or ""
    return scope.rsplit("/", 1)[-1] if scope else None


def _largest_fitting(count, fits):
    """Largest k in [0, count] with ``fits(k)``; ``fits`` is monotone."""
    low, high = 0, count
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    return low


def _window_observation(result, k):
    """A bounded acoustic observation (workspace_audio_observation's
    own AUDIO_OBSERVATION_V1 / AUDIO_SEGMENT_OBSERVATION_V1 shapes): the
    temporal map is a list of WHOLE segments -- the first thing the room
    can shed -- so a window keeps the source/profile/provenance/epistemic
    fields intact and drops only whole map rows, truthfully; the
    profile itself is never char-truncated into ambiguity."""
    entries = list(result["temporal_map"])
    windowed = dict(result)
    windowed["temporal_map"] = entries[:k]
    windowed["has_more"] = True
    if isinstance(windowed.get("sub_segment_count"), int):
        windowed["sub_segment_count"] = k
    windowed["temporal_map_window"] = {
        "delivered_segments": k,
        "total_segments": len(entries),
        "reason": "prompt_room",
        # The natural continuation is host-factual: a new inspect_audio
        # action on any interval (segment_observation is always available).
        "next_request": "inspect any interval with view=segment_observation to continue",
    }
    windowed[DELIVERY_WINDOW_KEY] = {
        "reason": "prompt_room", "delivered_segments": k, "total_segments": len(entries),
    }
    return windowed


def _window_list(result, resource_class, k):
    entries = list(result["entries"])
    start = result.get("start_index", 0)
    windowed = dict(result)
    windowed["entries"] = entries[:k]
    if isinstance(result.get("entry_folders"), dict):   # only the delivered entries' filing folders
        delivered = {name for name in entries[:k]}
        windowed["entry_folders"] = {n: f for n, f in result["entry_folders"].items() if n in delivered}
        if not windowed["entry_folders"]:
            del windowed["entry_folders"]
    if isinstance(result.get("entry_summaries"), dict):  # only the delivered entries' own openings
        delivered = set(entries[:k])
        windowed["entry_summaries"] = {n: v for n, v in result["entry_summaries"].items() if n in delivered}
        if not windowed["entry_summaries"]:
            del windowed["entry_summaries"]
    windowed["returned_count"] = k
    windowed["truncated"] = True
    windowed["has_more"] = True
    next_index = start + k
    windowed["next_cursor"] = wc._encode_list_cursor(
        resource_class, result.get("directory", ""), result.get("query"), next_index,
    )
    payload = {"cursor": windowed["next_cursor"]}
    if result.get("query") is not None:
        payload["query"] = result["query"]
    windowed["next_request"] = json.dumps(payload, separators=(",", ":"))
    windowed[DELIVERY_WINDOW_KEY] = {
        "reason": "prompt_room", "delivered_entries": k, "entries_in_page": len(entries),
    }
    return windowed


def _window_text(result, k):
    content = result["content"]
    start = result["next_offset"] - len(content)
    windowed = dict(result)
    windowed["content"] = content[:k]
    windowed["next_offset"] = start + k
    windowed["has_more"] = True
    # The original request's max_chars is not always recoverable (it is None
    # once a read reached the end), so continue at the largest lawful size.
    max_chars = 4000
    previous = result.get("next_request")
    if previous:
        try:
            max_chars = json.loads(previous).get("max_chars", max_chars)
        except (TypeError, ValueError):
            pass
    windowed["next_request"] = json.dumps({"offset": start + k, "max_chars": max_chars}, separators=(",", ":"))
    windowed[DELIVERY_WINDOW_KEY] = {
        "reason": "prompt_room", "delivered_chars": k, "chars_in_read": len(content),
    }
    return windowed


def _window_journal_entry(result, k):
    """A journal entry read: ``content`` is windowed; the entry's own
    identity/authorship fields are always delivered whole."""
    window = result.get("content_window") or {}
    content = result["content"]
    start = window.get("offset", 0)
    total = window.get("total_chars", start + len(content))
    windowed = dict(result)
    if "thread" in windowed:
        # The thread's other contributions ride only when the whole entry does; a windowed
        # entry keeps the thread's who/when but not their text, and says so.
        windowed["thread"] = [
            {key: value for key, value in item.items() if key not in ("content", "content_truncated")}
            for item in windowed["thread"]
        ]
        windowed["thread_content_omitted"] = "prompt_room"
    windowed["content"] = content[:k]
    windowed["content_window"] = {
        "offset": start, "chars": k, "total_chars": total,
        "has_more": True,
        "next_request": json.dumps({"offset": start + k, "max_chars": 4000}, separators=(",", ":")),
        "reason": "prompt_room",
    }
    return windowed


MEASUREMENT_SAFETY_TOKENS = 16   # join/template slack around a probed observation
MEASUREMENT_ATTEMPTS = 4


def _grow_by_measurement(count, windower, with_result, room, measure_fn, admissible, render=render_observation):
    """Largest window whose REAL provider-measured cost fits ``room``.

    The byte estimator is a guaranteed upper bound that over-prices prose
    ~4x, so windows chosen by it alone deliver a fraction of what the prompt
    can really carry. ``measure_fn(text) -> int | None`` is the same bounded
    real-token probe ordinary waking uses to recover an overcounted human
    message (``None`` = could not be safely measured). Only a window whose
    measured cost fits is accepted; any failure returns None and the caller
    keeps its always-safe estimator-sized window.

    Returns ``(k, measured_cost)`` or None."""
    k = _largest_fitting(count, lambda n: admissible(render(with_result(windower(n)))))
    for _ in range(MEASUREMENT_ATTEMPTS):
        if k <= 0:
            return None
        measured = measure_fn(render(with_result(windower(k))))
        if type(measured) is not int or measured <= 0:
            return None
        if measured + MEASUREMENT_SAFETY_TOKENS <= room:
            return k, measured
        k = min(k - 1, int(k * (room - MEASUREMENT_SAFETY_TOKENS) / measured * 0.97))
    return None


def fit_boundary_result(boundary_result, room, cost_fn=None, measure_fn=None, measurable_limit=None, render=None):
    """Return ``(boundary_result, info)``; the returned result's rendered
    observation costs at most ``room`` (in the compositor's cost units)
    whenever any non-empty window of it can.

    ``info`` = {"delivery": "whole"|"windowed"|"withheld"|"none", ...}. A
    result that already fits is returned untouched (same object). A result
    of unknown shape that cannot fit is replaced by an explicit, truthful
    withheld marker -- never silently omitted.

    ``cost_fn(text) -> int`` defaults to the conservative byte estimator.
    """
    cost_fn = cost_fn or _cost
    render_observation_text = render or render_observation   # what the prompt REALLY carries
    result = boundary_result.get("result")
    if result is None:
        return boundary_result, {"delivery": "none"}
    if cost_fn(render_observation_text(boundary_result)) <= room:
        return boundary_result, {"delivery": "whole"}

    resource_class = _resource_class(boundary_result)

    def with_result(new_result):
        replaced = dict(boundary_result)
        replaced["result"] = new_result
        return replaced

    windower, count = None, 0
    if isinstance(result, dict) and isinstance(result.get("entries"), list) and resource_class:
        windower, count = (lambda k: _window_list(result, resource_class, k)), len(result["entries"])
    elif isinstance(result, dict) and isinstance(result.get("content"), str) and "next_offset" in result:
        windower, count = (lambda k: _window_text(result, k)), len(result["content"])
    elif isinstance(result, dict) and isinstance(result.get("content"), str) and "entry_id" in result:
        windower, count = (lambda k: _window_journal_entry(result, k)), len(result["content"])
    elif isinstance(result, dict) and isinstance(result.get("temporal_map"), list):
        windower, count = (lambda k: _window_observation(result, k)), len(result["temporal_map"])

    if windower is not None:
        if measure_fn is not None and measurable_limit is not None:
            def admissible(text):
                return cost_fn(text) <= measurable_limit

            def whole_or_window(n):
                return windower(n) if n < count else result

            grown = _grow_by_measurement(count, whole_or_window, with_result, room, measure_fn, admissible, render_observation_text)
            if grown is not None:
                k, measured = grown
                delivered = with_result(whole_or_window(k))
                info = {"delivery": "whole" if k >= count else "windowed", "delivered_units": k,
                        "available_units": count, "measured_cost": measured}
                return delivered, info
        k = _largest_fitting(count, lambda n: cost_fn(render_observation_text(with_result(windower(n)))) <= room)
        if k > 0:
            fitted = with_result(windower(k))
            return fitted, {"delivery": "windowed", "delivered_units": k, "available_units": count}

    withheld = {
        WITHHELD_KEY: "The result exceeds the room available in this prompt and could not be windowed.",
        "result_keys": sorted(result) if isinstance(result, dict) else None,
    }
    fitted = with_result(withheld)
    if cost_fn(render_observation_text(fitted)) > room:
        return with_result({WITHHELD_KEY: "exceeds prompt room"}), {"delivery": "withheld"}
    return fitted, {"delivery": "withheld"}
