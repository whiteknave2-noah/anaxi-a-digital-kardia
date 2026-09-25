"""
Anaxi -- Free-Prose Boundary v1. Deterministic screening of Clark's
generated prose against three earned, frozen surface-form
constructions, per the fully-designed and explicitly approved
contract (design record, this session).

Purely lexical/substring matching -- NO semantic interpretation, NO
coreference resolution, NO judgment of truthfulness. Matches exact,
enumerated surface forms only. Anything not on this list is not
detected, by design -- this is deliberately, explicitly narrow
coverage, not an attempt at comprehensive protection. See the
standing design record for the full rationale and accepted residual
risk (semantically equivalent paraphrases that don't match these
exact forms are NOT caught by this layer).

Three constructions:
  REMEMBER-BARE  -- bare first-person future memory commitments
  SAVE-FUTURE    -- first-person future save commitments (with one
                    literal exclusion: "...that the day", to avoid
                    colliding with the "save the day" idiom)
  RETRIEVE-PAST  -- first-person past retrieval claims ("found" and
                    "pulled up that..." explicitly excluded as
                    ambiguous, per design review)

SAVE-PAST was pressure-tested and explicitly dropped -- not included.

BOUNDED-CLAUSE-REPEAT -- a fourth, separately-scoped deterministic
check (not one of the three frozen constructions above): Clark's
generated prose must not reproduce the host-rendered bounded clause
itself, which is prepended separately by the host and which Clark's
own system message already tells him not to repeat. Demonstrated
necessary by the first native waking turn (2026-08-30), where pass-2
prose opened with the bounded clause verbatim despite that
instruction, producing a doubled clause in the assembled reply. Like
the three constructions above, this is purely lexical -- normalizes
case and whitespace only, never attempts paraphrase/semantic
detection.
"""

import re

REMEMBER_BARE_FORMS = [
    "i'll remember that",
    "i will remember that",
    "i'll remember what you said",
    "i will remember what you said",
    "i'll remember what you just said",
    "i will remember what you just said",
]

SAVE_FUTURE_FORMS_UNCONDITIONAL = [
    "i'll save this",
    "i will save this",
    "i'll save what you said",
    "i will save what you said",
    "i'll save what you just said",
    "i will save what you just said",
]

SAVE_FUTURE_THAT_FORMS = [
    "i'll save that",
    "i will save that",
]
SAVE_FUTURE_THAT_EXCLUSION = "the day"

RETRIEVE_PAST_FORMS = [
    "i retrieved that",
    "i retrieved this",
    "i retrieved what you said",
    "i pulled up this",
    "i pulled up what you said",
]


def check_prohibited_constructions(text: str) -> dict:
    """Checks Clark's generated text against the three frozen
    constructions. Returns {"matched": bool, "construction": str or
    None, "matched_form": str or None} -- never raises, never
    attempts semantic judgment. Case-insensitive substring matching
    only."""
    text_lower = text.lower()

    for form in REMEMBER_BARE_FORMS:
        if form in text_lower:
            return {"matched": True, "construction": "REMEMBER-BARE", "matched_form": form}

    for form in SAVE_FUTURE_FORMS_UNCONDITIONAL:
        if form in text_lower:
            return {"matched": True, "construction": "SAVE-FUTURE", "matched_form": form}

    for form in SAVE_FUTURE_THAT_FORMS:
        idx = text_lower.find(form)
        if idx != -1:
            after = text_lower[idx + len(form):idx + len(form) + 1 + len(SAVE_FUTURE_THAT_EXCLUSION)]
            if not after.strip().startswith(SAVE_FUTURE_THAT_EXCLUSION):
                return {"matched": True, "construction": "SAVE-FUTURE", "matched_form": form}

    for form in RETRIEVE_PAST_FORMS:
        if form in text_lower:
            return {"matched": True, "construction": "RETRIEVE-PAST", "matched_form": form}

    return {"matched": False, "construction": None, "matched_form": None}


def _normalize_for_comparison(text: str) -> str:
    """Deterministic normalization for exact bounded-clause repetition
    comparison: lowercase, collapse any run of whitespace to a single
    space, strip leading/trailing whitespace. Tolerates only case and
    ordinary whitespace variation -- no punctuation stripping, no
    stemming, no paraphrase handling, matching this module's existing
    narrow-coverage discipline."""
    return re.sub(r"\s+", " ", text.strip().lower())


def check_bounded_clause_repeat(text: str, bounded_clause: str) -> dict:
    """Deterministic screen for the demonstrated first-native-turn
    failure mode: Clark's generated prose reproducing the host-
    rendered bounded clause itself, despite the pass-2 system message
    already telling him it was said separately and not to repeat it.
    Purely lexical exact-substring containment after normalization --
    never raises, never attempts semantic/paraphrase detection. Same
    {"matched", "construction", "matched_form"} shape as
    check_prohibited_constructions() so both drive the same
    regeneration seam."""
    if not bounded_clause:
        return {"matched": False, "construction": None, "matched_form": None}
    normalized_clause = _normalize_for_comparison(bounded_clause)
    if not normalized_clause:
        return {"matched": False, "construction": None, "matched_form": None}
    if normalized_clause in _normalize_for_comparison(text):
        return {"matched": True, "construction": "BOUNDED-CLAUSE-REPEAT", "matched_form": normalized_clause}
    return {"matched": False, "construction": None, "matched_form": None}


REJECTION_NOTICES = {
    "REMEMBER-BARE": (
        "Your previous response contained a reserved future-memory commitment "
        "(\"I'll remember...\"). Respond again to the user's message normally, "
        "without promising future memory or persistence."
    ),
    "SAVE-FUTURE": (
        "Your previous response contained a reserved save commitment (\"I'll "
        "save...\"). Respond again to the user's message normally, without "
        "claiming that content has been or will be saved."
    ),
    "RETRIEVE-PAST": (
        "Your previous response contained a reserved retrieval claim (\"I "
        "retrieved...\"/\"I pulled up...\"). Respond again to the user's "
        "message normally, without claiming you retrieved something."
    ),
    "BOUNDED-CLAUSE-REPEAT": (
        "Your previous response repeated the statement that was already told "
        "to the person separately, before your reply was added. Respond again "
        "to the user's message normally, without repeating that statement -- "
        "simply continue naturally."
    ),
}


_SUBORDINATOR_PATTERN = re.compile(
    r"^\s*(because|although|but|and|so)\b", re.IGNORECASE
)


def suppress_matched_sentences(text: str, construction: str, bounded_clause: str = None) -> str:
    """Second-match fallback: removes every complete sentence
    containing a match for the given construction, then checks the
    remainder for a leading subordinator. Returns the salvaged
    remainder, or an empty string if nothing safely survives.

    `bounded_clause` is required only for construction ==
    "BOUNDED-CLAUSE-REPEAT" (ignored otherwise) -- that construction's
    "forms" are not a fixed list like the other three, they're
    whatever the current turn's rendered clause happens to be, so the
    caller must supply it, same as check_bounded_clause_repeat().

    Explicitly does NOT attempt antecedent/coreference resolution --
    per design review, that risk is accepted residual risk, not
    solved here."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())

    if construction == "BOUNDED-CLAUSE-REPEAT":
        normalized_clause = _normalize_for_comparison(bounded_clause) if bounded_clause else ""
        surviving = [
            s for s in sentences
            if not (normalized_clause and normalized_clause in _normalize_for_comparison(s))
        ]
    else:
        forms = {
            "REMEMBER-BARE": REMEMBER_BARE_FORMS,
            "SAVE-FUTURE": SAVE_FUTURE_FORMS_UNCONDITIONAL + SAVE_FUTURE_THAT_FORMS,
            "RETRIEVE-PAST": RETRIEVE_PAST_FORMS,
        }.get(construction, [])

        surviving = []
        for s in sentences:
            s_lower = s.lower()
            if not any(f in s_lower for f in forms):
                surviving.append(s)

    remainder = " ".join(surviving).strip()

    if not remainder:
        return ""
    if _SUBORDINATOR_PATTERN.match(remainder):
        return ""
    return remainder
