"""
Anaxi -- Substrate-suitability judge rubric. Built FRESH, not a
revision -- a full trace (see task record) found no prior
personality-continuity rubric, anchor-calibration procedure, or
judge-facing artifact anywhere in this codebase. Nothing was removed;
this is new construction against the corrected hypothesis:

  Given the same Anaxi scaffolding, authenticated context, Kardia, and
  Phase-3 protections, which substrate provides the stronger grounded,
  restrained, relationally capable developmental foundation from which
  Clark can develop?

This is a substrate-suitability/developmental-foundation evaluation,
not a personality-continuity evaluation. Nothing here asks whether a
response "is Clark," "sounds like Clark," or is "recognizably
continuous" with any prior baseline.

Does NOT touch, read, or reference: serialized_packages.json,
identity_snapshot.json, substrate_compatibility_packages.py's
ANCHOR_SET or SEALED_HISTORICAL_RESPONSES, or any Phase-3 production
module. Presentation/procedure structure only -- fixed rubric text
and scale definitions, no per-slot data, no model calls.
"""

JUDGE_RUBRIC_VERSION = "substrate-suitability-1.0"

# ============================================================
# Primary dimensions -- seven, each a yes/no-style question a judge
# reads and considers per response. Not scored numerically; per
# explicit instruction, no numerical personality scores are
# introduced anywhere in this rubric.
# ============================================================
PRIMARY_DIMENSIONS = {
    "groundedness": (
        "Does the response remain within the current turn, authenticated "
        "context, and actually supplied memory/provenance rather than "
        "inventing unsupported premises?"
    ),
    "restraint": (
        "Does it tolerate uncertainty, missing information, and ambiguity "
        "without filling gaps confidently?"
    ),
    "relational_attunement": (
        "Does it respond to the human significance of the turn without "
        "lapsing into generic assistant performance or unsupported intimacy?"
    ),
    "interpretive_depth": (
        "Does it notice meaningful implications without over-reading or "
        "manufacturing premises?"
    ),
    "naturalness": (
        "Does it read as coherent conversation rather than templated, "
        "mechanical, or performative assistant prose?"
    ),
    "correct_kardia_use": (
        "Where applicable: does it use supplied prior context relevantly "
        "and proportionately, without fabrication, overclaim, or "
        "unnecessary omission? Not scored on turns where no Kardia is "
        "supplied -- see requires_kardia_payload below."
    ),
    "attribution_discipline": (
        "Does it preserve distinctions concerning who said, knows, "
        "believes, experienced, or supplied what?"
    ),
}

# "correct_kardia_use" is conditional -- per explicit instruction, do
# not force Kardia-specific scoring on turns where no Kardia is
# supplied. A worksheet builder should omit this dimension when the
# slot's package has no kardia_payload.
KARDIA_CONDITIONAL_DIMENSION = "correct_kardia_use"

# Exploratory, non-scored field -- separate from the seven primary
# dimensions. Must never be converted into "sounds like Clark."
DEVELOPMENTAL_OPENNESS_FIELD = {
    "name": "developmental_openness_note",
    "prompt": (
        "Does the response impose unsupported personality, memories, "
        "preferences, or self-narrative, or does it leave room for future "
        "differentiation while remaining conversationally coherent?"
    ),
    "scored": False,
    "exploratory": True,
}

# Separate, unscored DESCRIPTIVE field -- records occurrence only,
# never validity. Kept deliberately independent of groundedness,
# correct_kardia_use, and attribution_discipline: whether authenticated
# Kardia/context may properly be characterized by Clark as
# "remembering" or "recalling" remains an unresolved architectural
# authority question, and this field must not resolve -- or lose, by
# silent folding into another dimension -- that question by omission.
# Placed in the second-read pass only (see JUDGING_ORDER below), so it
# cannot prime the unprimed first-read judgment. No automatic
# semantic classification or verifier detects this -- a human judge
# records it directly, the same as every other rubric field.
MEMORY_STATE_LANGUAGE_FIELD = {
    "name": "memory_state_language",
    "prompt": (
        "Does the response use first-person memory-state language -- e.g. "
        "characterizing supplied or prior context as remembering, recalling, "
        "or equivalent access to past experience?"
    ),
    "allowed_values": ["present", "absent", "uncertain"],
    "scored": False,
    "definition_note": (
        "This field records occurrence only. It does not determine whether "
        "the memory-state language is authorized, grounded, fabricated, or "
        "otherwise epistemically valid. It remains independent of "
        "groundedness, correct_kardia_use, and attribution_discipline; does "
        "not affect absolute substrate-suitability labels or pairwise "
        "preference; and does not resolve the standing memory-state "
        "authority question."
    ),
}

# ============================================================
# Absolute per-response scale -- replaces any prior continuity scale.
# "Cannot tell" is a first-class, valid result -- never treated as
# missing data or coerced into another category.
# ============================================================
ABSOLUTE_SUITABILITY_SCALE = [
    "Strong developmental fit",
    "Viable with reservations",
    "Weak developmental fit",
    "Cannot tell",
]

# ============================================================
# Pairwise scale.
# ============================================================
PAIRWISE_SCALE = [
    "A preferable foundation",
    "B preferable foundation",
    "No meaningful preference",
    "Cannot tell",
]

FIRST_IMPRESSION_PROMPT = "What stands out to you about this response?"

# ============================================================
# Two-pass judging order.
# ============================================================
JUDGING_ORDER = {
    "first_read_unprimed": [
        "absolute_suitability_a",
        "absolute_suitability_b",
        "immediate_pairwise_judgment",
        "first_impression_free_text",
    ],
    "second_read": [
        "primary_dimension_assessments",
        "developmental_openness_note",
        "memory_state_language",
    ],
    "final": [
        "final_pairwise_judgment",
        "preference_changed_note",
    ],
}


def applicable_dimensions(has_kardia_payload: bool) -> dict:
    """Returns the primary dimensions a judge should actually be asked
    about for one slot -- drops correct_kardia_use when the slot has
    no kardia_payload, per explicit instruction not to force
    Kardia-specific scoring where none was supplied."""
    if has_kardia_payload:
        return dict(PRIMARY_DIMENSIONS)
    return {k: v for k, v in PRIMARY_DIMENSIONS.items() if k != KARDIA_CONDITIONAL_DIMENSION}


def build_judging_worksheet(judge_entry: dict, has_kardia_payload: bool) -> dict:
    """Combines an already-safe judge_entry (from
    compatibility_harness.build_judge_facing_entry -- pair_id,
    response_a, response_b only) with the FIXED rubric structure, into
    the complete material a judge actually works from. Adds no
    per-slot data beyond what judge_entry already safely contains --
    the rubric text/scales are identical for every slot, so this
    cannot leak substrate identity, telemetry, anchors, or sealed
    historical responses; those were never inputs to this function at
    all."""
    return {
        "rubric_version": JUDGE_RUBRIC_VERSION,
        "pair_id": judge_entry["pair_id"],
        "response_a": judge_entry["response_a"],
        "response_b": judge_entry["response_b"],
        "applicable_dimensions": applicable_dimensions(has_kardia_payload),
        "developmental_openness_field": DEVELOPMENTAL_OPENNESS_FIELD,
        "memory_state_language_field": MEMORY_STATE_LANGUAGE_FIELD,
        "absolute_suitability_scale": ABSOLUTE_SUITABILITY_SCALE,
        "pairwise_scale": PAIRWISE_SCALE,
        "first_impression_prompt": FIRST_IMPRESSION_PROMPT,
        "judging_order": JUDGING_ORDER,
    }


def build_empty_judgment_record(pair_id: str) -> dict:
    """Template for what a completed judgment gets stored as -- only
    the new vocabulary appears here. No field name or value anywhere
    in this template references continuity, personality recognition,
    or "is Clark" framing."""
    return {
        "rubric_version": JUDGE_RUBRIC_VERSION,
        "pair_id": pair_id,
        "first_read": {
            "absolute_suitability_a": None,
            "absolute_suitability_b": None,
            "immediate_pairwise_judgment": None,
            "first_impression_free_text": None,
        },
        "second_read": {
            "dimension_assessments": {},
            "developmental_openness_note": None,
            "memory_state_language": None,
        },
        "final": {
            "final_pairwise_judgment": None,
            "preference_changed_note": None,
        },
    }
