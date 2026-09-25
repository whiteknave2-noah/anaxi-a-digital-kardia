"""
ANAXI SLP1-B -- dormant Sleep Selection.

Takes complete, lawful Waking Material Units (as already assembled by
waking_material_unit.assemble_wmus() from the closed A-series
architecture) and asks Clark, on the llama3.2:3b substrate, which whole
WMUs -- if any -- should receive further processing in a later
(not-yet-built) Transformation stage.

This module is dormant: nothing here is called from any production
waking or Sleep entry point. Sleep/REM remains disabled.

Hard boundaries (see SLP1-B mandate for the full 18-rule architecture):
  - Selects whole wmu_id values only -- never component/segment ids,
    support facts, hashes, run ids, or backlinks.
  - A set, not a ranking. Zero selections is a valid, non-retried result.
  - Never constructs WMUs from raw history, never reopens a source
    (no DB access, no Qwen, no audio reanalysis, no web fetch -- this
    module touches nothing but the WMU dicts it is handed).
  - Every model call is admitted through the existing central OWC9
    budget authority (context_budget.compose_within_budget), reusing
    the already-approved, already-budgeted llama_sleep.ask_llama_for_json
    wrapper call site -- no new physical ollama.chat() call site.
  - Persists nothing. Returns an in-memory SelectionResult only.
"""

import json

import context_budget
from llama_sleep import ask_llama_for_json as _selection_chat

MAX_SELECTED_WMUS_PER_CALL = 8

MODE_INITIAL = "initial"
MODE_REDUCTION = "reduction"

_SOURCE_ROLE_LABELS = {
    "human_expression": "authenticated human expression",
    "clark_expression": "Clark expression",
    "delivered_resource_trace": "delivered resource trace",
}

_NEUTRAL_INSTRUCTIONS = (
    "From the waking-material units offered here, choose which items, "
    "if any, should receive further processing.\n\n"
    "Choosing none is valid.\n\n"
    "Do not rank, summarize, explain, or justify the choices."
)

def _schema_instructions(max_select):
    return (
        f"HARD LIMIT: select AT MOST {max_select} item(s) in total. Never "
        f"more than {max_select}, no matter how many are offered below. "
        "Selecting 0 is allowed and valid.\n\n"
        'Respond with ONLY a JSON object of exactly this shape, nothing else:\n'
        '{"selected_wmu_ids": ["wmu-...", ...]}\n\n'
        "Rules:\n"
        "- Only choose ids from the wmu_id values shown below.\n"
        f"- The \"selected_wmu_ids\" list must contain between 0 and "
        f"{max_select} items, inclusive -- never more than {max_select}.\n"
        "- Do not invent ids. Do not include any field other than "
        '"selected_wmu_ids".'
    )


def _cardinality_reminder(max_select):
    return f"Reminder: select AT MOST {max_select} item(s) in total, out of the items listed above."


class SleepSelectionError(Exception):
    """Base class for every SLP1-B failure. B always fails closed and
    atomically -- no partial union is ever returned as a success."""


class OversizeCandidateError(SleepSelectionError):
    """A single lawful WMU cannot fit alone within the Selection budget."""


class SelectionValidationError(SleepSelectionError):
    """A Selection call produced invalid output twice (initial + one retry)."""


class NonConvergenceError(SleepSelectionError):
    """A reduction round failed to shrink the candidate count."""


class SelectionResult:
    """Ephemeral, in-memory only. Never written to any store."""

    def __init__(self, selected_wmu_ids, batch_count, reduction_rounds, attempt_count):
        self.selected_wmu_ids = list(selected_wmu_ids)
        self.batch_count = batch_count
        self.reduction_rounds = reduction_rounds
        self.attempt_count = attempt_count

    def __repr__(self):
        return (
            f"SelectionResult(selected_wmu_ids={self.selected_wmu_ids!r}, "
            f"batch_count={self.batch_count}, "
            f"reduction_rounds={self.reduction_rounds}, "
            f"attempt_count={self.attempt_count})"
        )


def _render_candidate(wmu):
    lines = [f"WMU {wmu['wmu_id']}:"]
    for entry in wmu["selection_bearing"]:
        label = _SOURCE_ROLE_LABELS.get(entry["memory_kind"], "waking expression")
        lines.append(f"  [{label}] {entry['expression']!r}")
    return "\n".join(lines)


def _render_prompt(candidate_texts, max_select, mode):
    parts = [
        f"(Selection mode: {mode})",
        _NEUTRAL_INSTRUCTIONS,
        _schema_instructions(max_select),
        "\n\n".join(candidate_texts),
        _cardinality_reminder(max_select),
    ]
    return "\n\n".join(parts)


def _build_messages(candidate_texts, max_select, mode):
    return [{"role": "user", "content": _render_prompt(candidate_texts, max_select, mode)}]


def _compose_selection_budget(messages, measure=None):
    contribution = context_budget.Contribution(
        context_budget.CURRENT_HUMAN_MESSAGE,
        "".join(message["content"] for message in messages),
        hard=True,
    )
    result = context_budget.compose_within_budget(
        [contribution], context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET
    )
    return context_budget.admit_by_measurement(
        result, messages, context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET, measure,
    )


def _fits(wmus, mode, measure=None):
    """Whole-batch/whole-group preflight fit check. Uses len(wmus) itself
    as a safe upper-bound placeholder for max_select's text -- the real
    max_select used for the eventual call is always <= this, so the
    rendered text used for the real call is never longer than what was
    just proven to fit here."""
    texts = [_render_candidate(w) for w in wmus]
    messages = _build_messages(texts, len(wmus), mode)
    messages.append({"role": "user", "content": _retry_correction(min(MAX_SELECTED_WMUS_PER_CALL, len(wmus)))})
    return _compose_selection_budget(messages, measure).fits


def _pack_whole_candidates(wmus, mode, measure=None):
    """Deterministic, non-semantic packing: pack consecutive whole WMUs
    (in the order already given) until the next one would overflow the
    admitted Selection input budget, then start a new group. Raises
    OversizeCandidateError if any single WMU cannot fit alone -- never
    truncates, never skips, never continues with a partial group."""
    groups = []
    current = []
    for wmu in wmus:
        trial = current + [wmu]
        if _fits(trial, mode, measure):
            current = trial
            continue
        if not current:
            raise OversizeCandidateError(
                f"WMU {wmu['wmu_id']!r} cannot fit within the Selection "
                "budget even alone -- failing the complete B operation."
            )
        groups.append(current)
        if not _fits([wmu], mode, measure):
            raise OversizeCandidateError(
                f"WMU {wmu['wmu_id']!r} cannot fit within the Selection "
                "budget even alone -- failing the complete B operation."
            )
        current = [wmu]
    if current:
        groups.append(current)
    return groups


def _validate_selection_response(raw_text, offered_ids, max_select):
    """Returns a deduplicated list of valid ids (model's own order), or
    None if the response is invalid in any way. Never fuzzy-matches,
    never infers intended ids, never repairs semantic meaning."""
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if set(parsed.keys()) != {"selected_wmu_ids"}:
        return None
    value = parsed["selected_wmu_ids"]
    if not isinstance(value, list):
        return None
    if not all(isinstance(v, str) for v in value):
        return None
    offered = set(offered_ids)
    if not all(v in offered for v in value):
        return None
    deduped = []
    seen = set()
    for v in value:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    if len(deduped) > max_select:
        return None
    return deduped


def _retry_correction(max_select):
    return (
        "Your previous response was not valid. Respond again with ONLY a "
        'JSON object of exactly this shape: {"selected_wmu_ids": ["wmu-...", '
        '...]}. Choose only from the ids already shown above. HARD LIMIT: '
        f"at most {max_select} item(s) in total -- never more than "
        f"{max_select}. No other text, no other field."
    )


def _call_selection_once(candidates, max_select, mode, measure=None, chat=None):
    """One Selection opportunity over one already-packed group. Exactly
    one retry on invalid output; a valid response (including a valid
    empty selection) is never rerolled. Returns
    (deduplicated_selected_ids, attempt_count)."""
    offered_ids = [w["wmu_id"] for w in candidates]
    candidate_texts = [_render_candidate(w) for w in candidates]
    messages = _build_messages(candidate_texts, max_select, mode)

    chat = chat or _selection_chat
    budget = _compose_selection_budget(messages, measure)
    if not budget.fits:
        raise OversizeCandidateError(
            "Packed group unexpectedly exceeds the Selection budget at "
            f"call time (ids={offered_ids!r})."
        )

    raw = chat(messages)
    result = _validate_selection_response(raw, offered_ids, max_select)
    if result is not None:
        return result, 1

    retry_messages = messages + [
        {"role": "user", "content": _retry_correction(max_select)}
    ]
    if not _compose_selection_budget(retry_messages, measure).fits:
        raise OversizeCandidateError("Selection correction exceeds the retry budget.")
    raw2 = chat(retry_messages)
    result2 = _validate_selection_response(raw2, offered_ids, max_select)
    if result2 is not None:
        return result2, 2

    raise SelectionValidationError(
        f"Selection produced invalid output twice for candidates {offered_ids!r}."
    )


def _canonicalize(ids, order_index):
    return sorted(set(ids), key=lambda wid: order_index[wid])


def select_wmus(wmus, *, measure=None, chat=None):
    """The single entry point. `wmus` is a list of already-assembled,
    lawful WMU dicts (waking_material_unit.assemble_wmus() output).

    Presentation order is the explicit fallback from the SLP1-B mandate:
    ascending primary_event_id (ULIDs, so this is also chronological).

    Returns a SelectionResult. Raises SleepSelectionError (or a
    subclass) on any failure -- the whole operation fails atomically,
    never a partial union.
    """
    ordered = sorted(wmus, key=lambda w: w["primary_event_id"])
    if not ordered:
        return SelectionResult([], batch_count=0, reduction_rounds=0, attempt_count=0)

    order_index = {w["wmu_id"]: i for i, w in enumerate(ordered)}
    id_to_wmu = {w["wmu_id"]: w for w in ordered}

    batches = _pack_whole_candidates(ordered, MODE_INITIAL, measure)

    union_ids = []
    total_attempts = 0
    for batch in batches:
        max_select = min(MAX_SELECTED_WMUS_PER_CALL, len(batch))
        selected_ids, attempts = _call_selection_once(batch, max_select, MODE_INITIAL, measure, chat)
        total_attempts += attempts
        union_ids.extend(selected_ids)

    current_ids = _canonicalize(union_ids, order_index)

    reduction_rounds = 0
    while len(current_ids) > MAX_SELECTED_WMUS_PER_CALL:
        reduction_rounds += 1
        current_wmus = [id_to_wmu[wid] for wid in current_ids]
        groups = _pack_whole_candidates(current_wmus, MODE_REDUCTION, measure)

        new_ids = []
        for group in groups:
            m = len(group)
            max_select = 1 if m == 1 else max(1, m // 2)
            max_select = min(max_select, MAX_SELECTED_WMUS_PER_CALL)
            selected_ids, attempts = _call_selection_once(group, max_select, MODE_REDUCTION, measure, chat)
            total_attempts += attempts
            new_ids.extend(selected_ids)

        new_ids = _canonicalize(new_ids, order_index)
        if len(new_ids) >= len(current_ids):
            raise NonConvergenceError(
                f"Reduction round {reduction_rounds} did not decrease the "
                f"candidate count ({len(current_ids)} -> {len(new_ids)})."
            )
        current_ids = new_ids

    return SelectionResult(
        current_ids,
        batch_count=len(batches),
        reduction_rounds=reduction_rounds,
        attempt_count=total_attempts,
    )
