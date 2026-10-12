"""
ANAXI SLP1-C -- dormant Sleep Transformation.

Takes the whole WMUs Clark already selected (through closed B) and asks
Clark, on the llama3.2:3b substrate, to form new TENTATIVE, SOURCE-
LINKED relationships among them -- associations, tensions, recurrences,
contrasts, possible relationships, tentative generalizations.

This module is dormant: nothing here is called from any production
waking or Sleep entry point. Sleep/REM remains disabled.

Every derivation this module returns is a bounded, validated OCCURRENCE
record: "Clark derived this text, citing these wmu_ids, during this
Transformation call." It is NEVER converted to truth, belief, value,
identity, or instruction anywhere in this module -- that conversion is
permanently out of scope (see the SLP1-C mandate's "NO TRUTH / IDENTITY
INSTALLATION" section). Persistence of these occurrences as canonical
provenance is the caller's (sleep_cycle.py's) job, not this module's.

Hard boundaries:
  - Genuinely generative: the model may create NEW tentative
    relationships, not merely extract/copy existing text.
  - Contradictory derivations may coexist -- this module implements no
    contradiction detection or resolution of any kind.
  - Zero derivations is a valid, non-retried result.
  - Every derivation cites only wmu_ids offered in that SAME
    Transformation call -- never a WMU from a different batch, never a
    fabricated id.
  - Whole-WMU atomicity: never truncates, never summarizes, never
    "best-excerpts" a WMU to fit.
  - Every Transformation call is admitted through the existing central
    OWC9 budget authority, reusing the already-approved, already-
    budgeted llama_sleep.ask_llama_for_json wrapper call site -- no new
    physical ollama.chat() call site.
  - Touches no database, no retrieval/memory store, no legacy memory
    graph, no identity writer. Persists nothing itself -- returns
    in-memory Derivation objects only.
"""

import json

import context_budget
from llama_sleep import ask_llama_for_json as _transformation_chat
from sleep_selection import _render_candidate

# The mandate's own default is 4; reduced to 2 here because the actual
# OWC9 Transformation output envelope does not mechanically fit 4 --
# see context_budget.SLEEP_TRANSFORMATION_GENERATION_RESERVE's own
# comment for the exact byte-level reasoning. MAX_DERIVATION_CHARS is
# unchanged from the mandate's default.
MAX_DERIVATIONS_PER_CALL = 2
MAX_DERIVATION_CHARS = 600

_NEUTRAL_INSTRUCTIONS = (
    "You are performing a bounded associative transformation over waking "
    "material that was already genuinely encountered and selected for "
    "further processing.\n\n"
    "You may form new tentative associations, tensions, recurrences, "
    "contrasts, or possible relationships within or among the offered "
    "material.\n\n"
    "Every derivation must cite one or more offered wmu_ids.\n\n"
    "Contradictory derivations may coexist. Do not resolve contradictions.\n\n"
    "You may return no derivations.\n\n"
    "Do not rank the material.\n"
    "Do not assign importance.\n"
    "Do not install beliefs, values, commitments, personality, or identity.\n"
    "Do not describe what any particular person is like, feels, wants, fears, or tends to do; relate only "
    "what was said or happened, and who said it.\n"
    "Do not issue instructions.\n\n"
    "Return only the required JSON object."
)


def _offered_ids_rule(offered_ids):
    """A host-established mechanical fact stated at the point the model needs it.

    Measured with the real llama3.2:3b on production-shaped exchanges: with only
    "cite values from the WMUs shown below" the model invented a wmu_id that was
    not in the call in about 1 of 6 attempts (a single-WMU batch citing a
    non-existent neighbour), and two invalid attempts in a row fail the whole
    cycle; naming the exact valid ids removed the failures (12/12 valid)."""
    if not offered_ids:
        return ""
    return "- The ONLY valid wmu_id values in this call are: " + ", ".join(offered_ids) + ".\n"


def _schema_instructions(max_derivations, offered_ids=None):
    return (
        f"HARD LIMITS: return AT MOST {max_derivations} derivation(s). Each "
        f"derivation's \"derived_text\" must be AT MOST {MAX_DERIVATION_CHARS} "
        "characters. Returning zero derivations is valid.\n\n"
        'Respond with ONLY a JSON object of exactly this shape, nothing else:\n'
        '{"derivations": [{"source_wmu_ids": ["wmu-...", "..."], '
        '"derived_text": "..."}]}\n\n'
        "Rules:\n"
        + _offered_ids_rule(offered_ids)
        + "- Only cite wmu_id values from the WMUs shown below.\n"
        "- Each derivation's \"source_wmu_ids\" must be a non-empty list.\n"
        "- Each derivation's \"derived_text\" must be a non-empty string.\n"
        f"- At most {max_derivations} derivations total, never more.\n"
        "- Do not include any field other than \"source_wmu_ids\" and "
        '"derived_text".'
    )


class SleepTransformationError(Exception):
    """Base class for every SLP1-C Transformation failure. Fails closed
    and atomically -- no partial batch result is ever returned as a
    success."""


class OversizeWmuError(SleepTransformationError):
    """A single selected WMU cannot fit alone within the Transformation
    budget."""


class TransformationValidationError(SleepTransformationError):
    """A Transformation call produced invalid output twice (initial +
    one retry)."""


class Derivation:
    """One validated, in-memory derivation OCCURRENCE. Not truth, not
    belief -- see module docstring. `source_wmu_ids` is already
    deduplicated and restored to canonical (offered) order."""

    def __init__(self, source_wmu_ids, derived_text, batch_index, item_index):
        self.source_wmu_ids = list(source_wmu_ids)
        self.derived_text = derived_text
        self.batch_index = batch_index
        self.item_index = item_index

    def canonical_source_key(self):
        """A hashable, order-independent identity for this derivation's
        source set -- used only for exact-duplicate-tuple detection
        (dedupe_derivations()), never for semantic comparison."""
        return (self.derived_text, tuple(sorted(set(self.source_wmu_ids))))

    def __repr__(self):
        return (
            f"Derivation(source_wmu_ids={self.source_wmu_ids!r}, "
            f"derived_text={self.derived_text!r}, "
            f"batch_index={self.batch_index}, item_index={self.item_index})"
        )


def _render_prompt(candidate_texts, max_derivations, offered_ids=None):
    parts = [
        _NEUTRAL_INSTRUCTIONS,
        _schema_instructions(max_derivations, offered_ids),
        "\n\n".join(candidate_texts),
    ]
    return "\n\n".join(parts)


def _build_messages(candidate_texts, max_derivations, offered_ids=None):
    return [{"role": "user", "content": _render_prompt(candidate_texts, max_derivations, offered_ids)}]


def _compose_transformation_budget(messages, measure=None):
    contribution = context_budget.Contribution(
        context_budget.CURRENT_HUMAN_MESSAGE,
        "".join(message["content"] for message in messages),
        hard=True,
    )
    result = context_budget.compose_within_budget(
        [contribution], context_budget.SLEEP_TRANSFORMATION_MAX_PROMPT_BUDGET
    )
    return context_budget.admit_by_measurement(
        result, messages, context_budget.SLEEP_TRANSFORMATION_MAX_PROMPT_BUDGET, measure,
    )


def _fits(wmus, measure=None):
    texts = [_render_candidate(w) for w in wmus]
    messages = _build_messages(texts, MAX_DERIVATIONS_PER_CALL, [w["wmu_id"] for w in wmus])
    messages.append({"role": "user", "content": _retry_correction(MAX_DERIVATIONS_PER_CALL)})
    return _compose_transformation_budget(messages, measure).fits


def pack_whole_wmus(wmus, measure=None):
    """Deterministic, non-semantic packing: pack consecutive whole
    selected WMUs (already in canonical order) until the next one would
    overflow the admitted Transformation input budget, then start a new
    batch. Raises OversizeWmuError if any single WMU cannot fit alone --
    never truncates, never skips, never continues with a partial batch."""
    batches = []
    current = []
    for wmu in wmus:
        trial = current + [wmu]
        if _fits(trial, measure):
            current = trial
            continue
        if not current:
            raise OversizeWmuError(
                f"WMU {wmu['wmu_id']!r} cannot fit within the Transformation "
                "budget even alone -- failing the complete cycle."
            )
        batches.append(current)
        if not _fits([wmu], measure):
            raise OversizeWmuError(
                f"WMU {wmu['wmu_id']!r} cannot fit within the Transformation "
                "budget even alone -- failing the complete cycle."
            )
        current = [wmu]
    if current:
        batches.append(current)
    return batches


def _validate_transformation_response(raw_text, offered_ids, max_derivations):
    """Returns a list of (source_wmu_ids, derived_text) tuples (source
    ids deduplicated and restored to offered-canonical order), or None
    if the response is invalid in any way. Never fuzzy-matches, never
    infers intended ids, never repairs semantic meaning, never
    semantically deduplicates similar wording."""
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if set(parsed.keys()) != {"derivations"}:
        return None
    derivations_raw = parsed["derivations"]
    if not isinstance(derivations_raw, list):
        return None
    if len(derivations_raw) > max_derivations:
        return None

    offered = set(offered_ids)
    order_index = {wid: i for i, wid in enumerate(offered_ids)}
    result = []
    for item in derivations_raw:
        if not isinstance(item, dict):
            return None
        if set(item.keys()) != {"source_wmu_ids", "derived_text"}:
            return None
        source_ids = item["source_wmu_ids"]
        derived_text = item["derived_text"]
        if not isinstance(source_ids, list) or len(source_ids) == 0:
            return None
        if not all(isinstance(sid, str) for sid in source_ids):
            return None
        if not all(sid in offered for sid in source_ids):
            return None
        if not isinstance(derived_text, str) or derived_text == "":
            return None
        if len(derived_text) > MAX_DERIVATION_CHARS:
            return None
        deduped_sources = sorted(set(source_ids), key=lambda sid: order_index[sid])
        result.append((deduped_sources, derived_text))
    return result


def _retry_correction(max_derivations):
    return (
        "Your previous response was not valid. Respond again with ONLY a "
        'JSON object of exactly this shape: {"derivations": '
        '[{"source_wmu_ids": ["wmu-...", "..."], "derived_text": "..."}]}. '
        "Cite only wmu_ids already shown above. HARD LIMITS: at most "
        f"{max_derivations} derivation(s), each derived_text at most "
        f"{MAX_DERIVATION_CHARS} characters. No other text, no other field."
    )


def _call_transformation_once(batch, batch_index, measure=None, chat=None):
    """One Transformation opportunity over one already-packed whole-WMU
    batch. Exactly one retry on invalid output; a valid response
    (including zero derivations) is never rerolled. Returns a list of
    Derivation objects."""
    offered_ids = [w["wmu_id"] for w in batch]
    candidate_texts = [_render_candidate(w) for w in batch]
    messages = _build_messages(candidate_texts, MAX_DERIVATIONS_PER_CALL, offered_ids)

    chat = chat or _transformation_chat
    budget = _compose_transformation_budget(messages, measure)
    if not budget.fits:
        raise OversizeWmuError(
            f"Packed batch unexpectedly exceeds the Transformation budget "
            f"at call time (ids={offered_ids!r})."
        )

    raw = chat(messages)
    parsed = _validate_transformation_response(raw, offered_ids, MAX_DERIVATIONS_PER_CALL)
    if parsed is None:
        retry_messages = messages + [
            {"role": "user", "content": _retry_correction(MAX_DERIVATIONS_PER_CALL)}
        ]
        if not _compose_transformation_budget(retry_messages, measure).fits:
            raise OversizeWmuError("Transformation correction exceeds the retry budget.")
        raw2 = chat(retry_messages)
        parsed = _validate_transformation_response(raw2, offered_ids, MAX_DERIVATIONS_PER_CALL)
        if parsed is None:
            raise TransformationValidationError(
                f"Transformation produced invalid output twice for batch {offered_ids!r}."
            )

    return [
        Derivation(source_ids, derived_text, batch_index=batch_index, item_index=item_index)
        for item_index, (source_ids, derived_text) in enumerate(parsed)
    ]


def transform(selected_wmus, *, measure=None, chat=None):
    """The single entry point. `selected_wmus` is the list of already-
    selected (through closed B), already-canonically-ordered whole WMU
    dicts for one Transformation cycle -- resolved by the caller
    against its own exact in-memory lawful WMU set, never re-fetched
    from any richer source.

    Returns a flat list of Derivation objects (possibly empty) spanning
    every batch. Raises SleepTransformationError (or a subclass) on any
    failure -- the whole Transformation stage fails atomically; the
    caller must discard any in-memory result and must not persist or
    advance the watermark.
    """
    if not selected_wmus:
        return []

    batches = pack_whole_wmus(selected_wmus, measure)
    derivations = []
    for batch_index, batch in enumerate(batches):
        derivations.extend(_call_transformation_once(batch, batch_index, measure, chat))
    return derivations


def dedupe_derivations(derivations):
    """Mechanical, cycle-level deduplication: exact duplicate
    (derived_text, canonical source_wmu_id set) tuples collapse to one,
    with no added weight. Never semantically deduplicates similar
    wording, never merges conflicting derivations -- only byte-exact
    text plus exact source-set duplicates are affected. Order-
    preserving: the FIRST occurrence of each unique tuple is kept."""
    seen = set()
    result = []
    for d in derivations:
        key = d.canonical_source_key()
        if key in seen:
            continue
        seen.add(key)
        result.append(d)
    return result
