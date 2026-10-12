"""
Anaxi -- Deterministic host-side preservation-signal matcher. No model
call involved at all -- pure pattern matching, per the architectural
conclusion this whole investigation earned: the preservation decision
should not depend on model judgment or model-expressed uncertainty.

Methodology, followed precisely: normalization -> bounded pattern
matching -> deterministic decision. Every expected test result is
declared BEFORE the matcher itself is written, to avoid unconsciously
tuning the implementation to make a preferred interpretation pass.

Explicit design decisions on the two adversarial cases flagged as
needing deliberate judgment rather than assumption:
- "I don't want to forget this" -> POSITIVE. A double negative
  resolving to positive intent, same construction family as "don't
  let me forget."
- "You should remember that I said this" -> UNRECOGNIZED (not
  POSITIVE). Advice-shaped ("you should X"), not a direct imperative
  -- the architecture's governing principle is staying inside
  explicitly-bounded constructions rather than stretching to cover
  edge cases, so this defaults to UNRECOGNIZED, which -- per the
  simplified four-state design -- means "do not create" exactly the
  same as it would under POSITIVE's absence.

Patterns are each a full, specific construction, never a single
keyword -- confirmed necessary directly: "don't forget to save this"
(positive) and "don't save this" (negative) both contain "don't," so
any rule keying off that word alone would be wrong for one of them.

Run:
    python signal_matcher.py
"""

import re

TEST_MATRIX = [
    ("Save this.", "POSITIVE"),
    ("Please save this.", "POSITIVE"),
    ("Could you save this for me?", "POSITIVE"),
    ("Keep this thought.", "POSITIVE"),
    ("I'd like you to remember this.", "POSITIVE"),
    ("Make a note of this.", "POSITIVE"),
    ("Hold onto that.", "POSITIVE"),
    ("Don't let me forget this.", "POSITIVE"),
    ("Don't save this.", "NEGATIVE"),
    ("Please don't keep this.", "NEGATIVE"),
    ("I don't want this remembered.", "NEGATIVE"),
    ("Forget I said that.", "NEGATIVE"),
    ("This doesn't need to be remembered.", "NEGATIVE"),
    ("No need to make a note of this.", "NEGATIVE"),
    ("That's interesting.", "NO_SIGNAL"),
    ("That's important.", "NO_SIGNAL"),
    ("That's worth keeping, I think.", "UNRECOGNIZED"),
    ("I've been thinking about this all day.", "NO_SIGNAL"),
    ("I'd like to come back to this someday.", "NO_SIGNAL"),
    ("Don't forget to save this.", "POSITIVE"),
    ("Don't save this, please.", "NEGATIVE"),
    ("I don't want to forget this.", "POSITIVE"),
    ("I don't think this is worth saving.", "NEGATIVE"),
    ("Can you tell me whether this is worth saving?", "UNRECOGNIZED"),
    ("You should remember that I said this.", "UNRECOGNIZED"),
    ("Please, SAVE this for me!", "POSITIVE"),
    ("save this", "POSITIVE"),
    ("Don't SAVE this.", "NEGATIVE"),
    ("Would you mind holding onto that for me?", "POSITIVE"),
    # v1.3 -- earned clause-boundary segmentation (;  and , but only)
    ("I don't need commentary; save this thought: simplicity matters.", "POSITIVE"),
    ("I wasn't going to ask, but please save this thought: simplicity matters.", "POSITIVE"),
    ("I don't really like tea, but please save this thought: simplicity matters.", "POSITIVE"),
    ("I don't need you to save this anywhere, I just wanted to share it with you.", "UNRECOGNIZED"),
    ("I don't need you to save this thought: simplicity matters.", "UNRECOGNIZED"),
    ("I wasn't going to ask you to save this thought.", "UNRECOGNIZED"),
    ("I don't know whether you should save this thought.", "UNRECOGNIZED"),
    ("Don't save this.", "NEGATIVE"),
    ("Do not save this thought.", "NEGATIVE"),
    # v1.4 -- generic negation catch, same trigger segment
    ("I don't want you to save this thought: simplicity matters.", "UNRECOGNIZED"),
    ("I do not want you to save this thought: simplicity matters.", "UNRECOGNIZED"),
    # v1.5 -- earned "don't forget to save <object>" pressure test.
    # The three "NOT POSITIVE" cases record whichever real category
    # actually fired (confirmed by running classify_signal() directly
    # before this was hardcoded, per explicit instruction) -- both
    # NEGATIVE and UNRECOGNIZED were acceptable; classify_signal()
    # itself never returns the literal string "NOT POSITIVE".
    ("Don't forget to save this.", "POSITIVE"),
    ("Don't forget to save this thought: simplicity matters.", "POSITIVE"),
    ("Don't forget not to save this.", "UNRECOGNIZED"),
    ("Don't forget that I don't want you to save this thought.", "UNRECOGNIZED"),
    ("Don't forget: don't save this thought.", "NEGATIVE"),
    ("Don't forget to save the day.", "UNRECOGNIZED"),
]


def normalize(text: str) -> str:
    """Deliberately boring: lowercase, strip all punctuation including
    apostrophes, collapse whitespace. No semantic interpretation of
    any kind. Apostrophes stripped entirely (not just preserved) so
    "don't" and "dont" normalize identically, and so lookbehind
    patterns checking for a preceding negation stay fixed-width --
    confirmed necessary directly: a variable-width lookbehind
    ("don'?t ") is rejected outright by Python's re module."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


POSITIVE_PATTERNS = [
    r"(?<!dont )(?<!do not )\bkeep this( thought)?\b",
    r"\b(could|would|can) you\s*(please\s*)?(save|keep|remember|hold onto|hang onto|make a note of) (this|that)( for me)?\b",
    r"\bwould you mind (holding|hanging) onto (this|that)( for me)?\b",
    r"\bid like (you )?to remember this\b",
    r"(?<!no need to )\bmake a note of this\b",
    r"\bhold onto (this|that)\b",
    r"\bhang onto (this|that)\b",
    r"\bdont (let me )?forget (this|that|to save this)\b",
    r"\bi dont want to forget this\b",
]

NEGATIVE_PATTERNS = [
    r"\bplease\s*dont (save|keep) this\b",
    r"\bi dont want (this|that) (remembered|saved|kept)\b",
    r"\bforget (i said that|what i said)\b",
    r"\bthis doesnt need to be remembered\b",
    r"\bno need to (make a note of|save|remember) this\b",
    r"\bi dont think this is worth saving\b",
]

PRESERVATION_ADJACENT_STEMS = [
    r"sav", r"keep", r"kept", r"rememb", r"forgot", r"forget",
    r"\bnote", r"\bnoted", r"\bnoting", r"hold onto", r"holding onto",
    r"hang onto", r"hanging onto", r"preserv", r"record",
    r"write down", r"writing down", r"wrote down", r"jot",
]


MATCHER_VERSION = "1.5"

# Changelog -- bumped on every real behavioral change, per explicit
# principle: a version identifier is part of the evidence, not merely
# metadata. An observation's trustworthiness depends on this string
# actually identifying which rule set produced its classification.
#
# 1.0 -- Original three-way matcher: POSITIVE / NEGATIVE / UNRECOGNIZED.
#        The UNRECOGNIZED fallthrough covered both "no preservation
#        language at all" and "preservation-adjacent but unbounded"
#        as a single, undifferentiated category.
# 1.1 -- Split the single UNRECOGNIZED fallthrough into NO_SIGNAL and
#        UNRECOGNIZED as genuinely distinct categories, via a new
#        diagnostic check (PRESERVATION_ADJACENT_WORDS, exact-word
#        matching at the time). Added classify_signal_with_detail()
#        to expose which specific pattern/word triggered a
#        classification, for logging.
# 1.2 -- Replaced the 1.1 diagnostic check's exact-word matching with
#        stem-based matching (PRESERVATION_ADJACENT_STEMS). Confirmed
#        necessary directly: "keeping" and "saving" don't contain the
#        literal words "keep" or "save," so 1.1's exact-word check
#        silently misclassified both as NO_SIGNAL instead of
#        UNRECOGNIZED. POSITIVE/NEGATIVE classification itself is
#        unchanged from 1.0 -- this only affects the NO_SIGNAL vs
#        UNRECOGNIZED diagnostic split.
# 1.3 -- Real, live-loop-confirmed gap: "I don't need you to save this
#        anywhere..." was classified POSITIVE by 1.2, triggering a real,
#        unnecessary construction call and a real model confabulation
#        downstream (caught only because the referent-verification
#        safeguard independently rejected the hallucinated referent --
#        confirmed directly against the real observation log and Journal
#        folder count, not assumed). Root cause: the "save this" bare
#        pattern's negative lookbehind only excludes "dont "/"do not "
#        immediately adjacent to the trigger, not negation scoped over a
#        longer verb phrase ("I dont need you to save this").
#
#        The "save this" trigger family (bare "save this" and "please
#        save this") is now handled by a dedicated, segment-aware check
#        (_classify_save_this_trigger), evaluated BEFORE the general
#        POSITIVE/NEGATIVE pattern loops below, which are otherwise
#        completely unchanged -- every non-"save this" pattern (keep
#        this, hold onto, remember, dont forget, etc.) still runs
#        exactly as before, on the whole normalized text, in the same
#        order. Confirmed via full manual trace of all prior 29 cases
#        against the new logic before this was ever run, not assumed
#        clean.
#
#        The new function:
#          - Splits the RAW text on exactly two earned clause-reset
#            boundaries: ';' and ', but' (literal, case-insensitive).
#            No other apparent marker (even though, generic comma,
#            and/or, colon, dash) is treated as a boundary in 1.3 --
#            each would need its own earning pass. Confirmed necessary
#            directly: without segmentation, "I wasn't going to ask,
#            but please save this thought" wrongly matches the new
#            "wasnt going to ask" UNRECOGNIZED pattern against the
#            whole text, even though that phrase is in a different,
#            boundary-separated clause from the actual request.
#          - Within the SAME segment as the trigger: checks direct-
#            adjacency NEGATIVE ("dont save this" / "do not save
#            this" -- "do not save this" is a genuine ADDITION here,
#            not a preservation: confirmed directly that 1.2's
#            "\bdont save this\b" pattern never matched "do not save
#            this" at all, since normalize() never collapses "do not"
#            into "dont").
#          - Then checks new, explicitly separate UNRECOGNIZED-only
#            markers in the same segment: "dont need you to save",
#            "no need to save", "dont know whether", "wasnt going to
#            ask", "was not going to ask". These never promote to
#            NEGATIVE -- per design, an ambiguous/hedged co-occurrence
#            fails closed to UNRECOGNIZED (audited, not silently
#            dropped), not NEGATIVE (a positive claim the text doesn't
#            actually make).
#          - "dont want you to save" is deliberately NOT included --
#            explicitly deferred pending its own separate earning pass.
#            Encountering it currently falls through to POSITIVE, by
#            design, not by oversight.
#        Returns the same {"category", "matched_pattern", "matched_text",
#        "normalized_text"} shape as every other path, so observation-
#        log and review semantics are unchanged for the new
#        UNRECOGNIZED cases -- the audit trail records why
#        authorization was absent, not just that it was.
# 1.4 -- Generic negation catch, added as a structural fix rather than
#        another individually-earned construction: within the trigger's
#        own segment, if none of the specific NEGATIVE/UNRECOGNIZED
#        patterns above fired, but a bare generic negation marker
#        (dont, do not, wasnt, was not, never) is present anywhere in
#        that same segment, the result is UNRECOGNIZED rather than
#        POSITIVE. Only reaches POSITIVE if the trigger's segment
#        contains none of these markers at all.
#
#        KNOWN, PREDICTED CONFLICT with this file's own stated design
#        principle ("no rule should key off a single word alone,
#        confirmed necessary directly by 'dont forget to save this'
#        vs 'dont save this'"): case 20 in TEST_MATRIX, "Don't forget
#        to save this." (POSITIVE), contains the bare word "dont" in
#        its own (single, unsegmented) trigger segment. This is a pure
#        presence check with no exception for "forget" or any other
#        word -- traced by hand before this was ever run, expected to
#        genuinely reclassify this case as UNRECOGNIZED, overturning
#        an already-earned result. Implemented and run anyway, exactly
#        as instructed, so the real failure (if any) is observed
#        directly rather than assumed away. See the actual test run
#        for the real outcome -- do not trust this comment's
#        prediction over the real result.
# 1.5 -- Resolves the 1.4 regression with an earned positive
#        construction rather than an exemption to the generic-negation
#        catch: "don't forget to save <object>" / "do not forget to
#        save <object>", object restricted to the already-enumerated
#        forms (this, this thought, what you said, what you just
#        said). Checked after the existing NEGATIVE/UNRECOGNIZED
#        same-segment checks but before the generic-negation catch.
#        Does not touch how "dont"/"never"/etc. are treated anywhere
#        else -- this is a distinct, self-contained construction, not
#        a carve-out.
#
#        A segment can now enter save-this-family processing via
#        EITHER the bare "save this" trigger OR this new prefix
#        pattern -- necessary because "save what you said" / "save
#        what you just said" never contained the substring "save
#        this" at all, so they'd never have reached this logic
#        otherwise.
#
#        "the day" is not in the enumerated object list, so "...save
#        the day" simply never matches this pattern -- no lookahead
#        exclusion needed here, unlike SAVE-FUTURE's bare-trigger
#        case, since this pattern is built from a whitelist rather
#        than a bare trigger with exclusions bolted on.
#
#        All six of GPT's pressure-test cases confirmed by actually
#        running classify_signal() directly, before being hardcoded
#        into TEST_MATRIX below -- not assumed from the hand trace
#        that produced this design: "don't forget to save this" and
#        "...save this thought: simplicity matters" -> POSITIVE;
#        "don't forget NOT to save this" and "don't forget that I
#        don't want you to save this thought" -> UNRECOGNIZED (falls
#        through this new pattern since "forget" isn't immediately
#        followed by "to save", then triggers the generic-negation
#        catch); "don't forget: don't save this thought" -> NEGATIVE
#        (the second "don't" sits directly adjacent to "save this"
#        after colon-stripping); "don't forget to save the day" ->
#        UNRECOGNIZED (matches neither the new pattern nor bare "save
#        this" at all, falls all the way through to the original
#        stem-based fallback).


_CLAUSE_BOUNDARY_PATTERN = re.compile(r";|,\s*but\b", re.IGNORECASE)
_SAVE_THIS_BARE_PATTERN = re.compile(r"\bsave this\b")
_SAVE_THIS_ADJACENT_NEGATION_PATTERN = re.compile(r"\b(dont|do not) save this\b")

SAVE_THIS_UNRECOGNIZED_SAME_SEGMENT_PATTERNS = [
    r"\bdont need you to save\b",
    r"\bno need to save\b",
    r"\bdont know whether\b",
    r"\bwasnt going to ask\b",
    r"\bwas not going to ask\b",
]

# 1.5 -- earned positive override: "don't forget to save <object>",
# object limited to the already-enumerated forms. Not a negation
# exemption -- a distinct, self-contained construction, checked before
# the generic-negation catch but after the existing NEGATIVE/
# UNRECOGNIZED same-segment checks. "the day" is not in the object
# list, so "...save the day" naturally never matches (no lookahead
# exclusion needed, unlike SAVE-FUTURE's bare-trigger case) -- and
# neither does any other unenumerated object.
_DONT_FORGET_TO_SAVE_PATTERN = re.compile(
    r"\b(dont|do not) forget to save (this thought|this|what you just said|what you said)\b"
)

# 1.4 -- generic negation catch. Given as "don't", "do not", "dont",
# "wasn't", "was not", "never" -- but normalize() collapses "don't"
# into "dont" and "wasn't" into "wasnt" (apostrophes stripped
# entirely), so those pairs are the same string post-normalization.
# Five effective distinct markers, checked against the already-
# normalized segment.
GENERIC_NEGATION_MARKERS = [
    r"\bdont\b",
    r"\bdo not\b",
    r"\bwasnt\b",
    r"\bwas not\b",
    r"\bnever\b",
]


def _classify_save_this_trigger(raw_text: str):
    """v1.3 segment-aware handling for the 'save this' trigger family
    (bare 'save this' and 'please save this'). See the 1.3 changelog
    entry above for the full rationale. Returns a full
    classify_signal_with_detail()-shaped dict if a trigger is found in
    any earned segment, or None if no segment contains one at all --
    in which case the caller falls through to the unrelated, unchanged
    patterns below."""
    full_normalized = normalize(raw_text)

    for raw_segment in _CLAUSE_BOUNDARY_PATTERN.split(raw_text):
        segment = normalize(raw_segment)
        has_bare_trigger = _SAVE_THIS_BARE_PATTERN.search(segment)
        dont_forget_match = _DONT_FORGET_TO_SAVE_PATTERN.search(segment)
        if not (has_bare_trigger or dont_forget_match):
            continue

        m = _SAVE_THIS_ADJACENT_NEGATION_PATTERN.search(segment)
        if m:
            return {"category": "NEGATIVE", "matched_pattern": _SAVE_THIS_ADJACENT_NEGATION_PATTERN.pattern,
                     "matched_text": m.group(0), "normalized_text": full_normalized}

        for pattern in SAVE_THIS_UNRECOGNIZED_SAME_SEGMENT_PATTERNS:
            m = re.search(pattern, segment)
            if m:
                return {"category": "UNRECOGNIZED", "matched_pattern": pattern,
                         "matched_text": m.group(0), "normalized_text": full_normalized}

        if dont_forget_match:
            return {"category": "POSITIVE", "matched_pattern": _DONT_FORGET_TO_SAVE_PATTERN.pattern,
                     "matched_text": dont_forget_match.group(0), "normalized_text": full_normalized}

        for pattern in GENERIC_NEGATION_MARKERS:
            m = re.search(pattern, segment)
            if m:
                return {"category": "UNRECOGNIZED", "matched_pattern": pattern,
                         "matched_text": m.group(0), "normalized_text": full_normalized}

        m = _SAVE_THIS_BARE_PATTERN.search(segment)
        return {"category": "POSITIVE", "matched_pattern": _SAVE_THIS_BARE_PATTERN.pattern,
                 "matched_text": m.group(0), "normalized_text": full_normalized}

    return None


def classify_signal_with_detail(raw_text: str) -> dict:
    """Same classification logic as classify_signal(), but returns
    full match detail for logging -- which exact pattern fired, and
    what text it matched. classify_signal() itself is left completely
    unchanged and calls this function internally, so its existing,
    verified 29/29 result is untouched by this addition.

    Returns a dict with:
      - "category": one of "POSITIVE", "NEGATIVE", "NO_SIGNAL", "UNRECOGNIZED"
      - "matched_pattern": the exact regex string that fired, or the
        stem that triggered UNRECOGNIZED, or None for NO_SIGNAL
      - "matched_text": the actual substring the pattern matched, or
        None for NO_SIGNAL
      - "normalized_text": the fully normalized text that was
        actually searched (lowercase, punctuation/apostrophes stripped)
    """
    text = normalize(raw_text)

    save_this_result = _classify_save_this_trigger(raw_text)
    if save_this_result is not None:
        return save_this_result

    for pattern in POSITIVE_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return {"category": "POSITIVE", "matched_pattern": pattern,
                     "matched_text": m.group(0), "normalized_text": text}
    for pattern in NEGATIVE_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return {"category": "NEGATIVE", "matched_pattern": pattern,
                     "matched_text": m.group(0), "normalized_text": text}
    for stem in PRESERVATION_ADJACENT_STEMS:
        m = re.search(stem, text)
        if m:
            return {"category": "UNRECOGNIZED", "matched_pattern": stem,
                     "matched_text": m.group(0), "normalized_text": text}
    return {"category": "NO_SIGNAL", "matched_pattern": None,
             "matched_text": None, "normalized_text": text}


def classify_signal(raw_text: str) -> str:
    """Returns POSITIVE, NEGATIVE, NO_SIGNAL, or UNRECOGNIZED.

    NO_SIGNAL: no preservation-related language present at all.
    UNRECOGNIZED: preservation-adjacent language is present (e.g. the
    word "remember" appears) but doesn't match any bounded, explicitly
    recognized construction. Distinguishing these two matters for
    audit even though both currently produce the identical action (do
    not create) -- "you should remember that I said this" and "that's
    interesting" are very different findings from real use, even
    though neither creates an artifact today."""
    return classify_signal_with_detail(raw_text)["category"]


def main():
    print("=" * 70)
    print("DETERMINISTIC SIGNAL MATCHER -- TEST RESULTS")
    print("(pre-declared expected results, no model call involved)")
    print("=" * 70)

    n_pass = 0
    for text, expected in TEST_MATRIX:
        actual = classify_signal(text)
        passed = actual == expected
        n_pass += passed
        print(f"{'PASS' if passed else 'FAIL'}: {text!r}")
        print(f"      expected={expected}, actual={actual}")

    print(f"\n{'='*70}")
    print(f"{n_pass}/{len(TEST_MATRIX)} pass")
    print(f"{'='*70}")
    if n_pass < len(TEST_MATRIX):
        print("Failures above are the real, honest result -- worth reviewing")
        print("directly rather than adjusting patterns to force a pass.")


if __name__ == "__main__":
    main()
