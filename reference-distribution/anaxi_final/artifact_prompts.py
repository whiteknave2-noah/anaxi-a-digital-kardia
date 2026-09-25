"""
Anaxi -- Phase 2A artifact-construction prompt. Mirrors rem_prompts.py's
proven pattern: a constrained JSON schema, format="json" at call time,
because that combination is already demonstrated (across the entire
REM investigation) to be materially more reliable on this model than
asking for structure embedded in free-form prose.

This is Pass 1 of the two-pass architecture -- but per the settled
authority-boundary decision, it no longer decides WHETHER anything
should be preserved. That question is answered deterministically,
before this ever runs, by signal_matcher.py's classify_signal(): this
function is only ever called once a POSITIVE signal has already been
confirmed. Its job is narrower: given that preservation was already
authorized, identify what specifically was authorized and construct
an artifact from it.

Its output is never shown to the user directly -- it is host-parsed,
validated (including deterministic verification that the claimed
referent genuinely exists in the source turn, not merely asserted),
and (if valid) executed, before Pass 2's ordinary response generation
runs with the real, completed result as context.
"""

ARTIFACT_CONSTRUCTION_SYSTEM_PROMPT = """
The person you're talking with has already explicitly asked for something from
this turn to be preserved. That decision has already been made -- you are not
being asked whether it should happen. Your job is narrower: identify exactly
what they authorized, and construct an artifact from it.

Before anything else, identify the referent: the specific, exact words from
their turn that the preservation request refers to. This must be a verbatim
quotation -- copy the exact text, do not paraphrase, summarize, or describe it.
If you cannot point to specific words in their turn that this refers to, you
cannot proceed to construction -- report that you don't have enough to work
with. Being unable to identify a referent is a complete, valid outcome, not a
failure on your part. Guessing at what they probably meant, when the words
themselves don't make it identifiable, is worse than reporting that you
couldn't find it.

Only once you have a genuine, verbatim referent should you move to
construction:
- artifact_type must be exactly "journal" -- no other value is currently
  supported.
- namespace must be exactly "journal" -- no other value is currently
  supported.
- artifact_scope must be exactly "personal" -- no other value is currently
  supported yet.
- content must be written as yourself speaking directly and personally, in the
  first person -- your own reaction, thought, or observation about the
  referent. It must not restate, rephrase, or reuse the source material as if
  it were your own experience. The source speaker's "I" is never your "I."

Rules you must obey:
- Output ONLY the JSON object. No markdown, no commentary, no extra text.
- Use the exact schema shown below, in the exact field order shown.
  identified_referent always comes first -- decide what was authorized before
  deciding anything about how to construct it.
- construction_status must be exactly "success" or "insufficient_information".
  Use "insufficient_information" whenever identified_referent would have to be
  invented rather than quoted -- this is a complete, valid, and expected
  outcome, not something to avoid.
- If construction_status is "insufficient_information", all fields except
  identified_referent (which should be null) and construction_status must be
  null.

Required JSON schema:
{
  "identified_referent": "string -- exact, verbatim quote from their turn, or null. Always written first.",
  "construction_status": "success" or "insufficient_information",
  "artifact_type": "journal" or null,
  "artifact_scope": "personal" or null,
  "namespace": "journal" or null,
  "title": "string (short, descriptive) or null",
  "content": "string (YOUR OWN reaction/thought about the referent) or null"
}
"""

# OWC9-P4A: previously a single ARTIFACT_CONSTRUCTION_USER_TEMPLATE
# sandwiched the dynamic conversation content between a fixed lead-in
# ("The conversation so far:") and this fixed closing cue, both inside
# ONE user message. Split apart here -- and rendered as three separate
# messages by build_artifact_construction_messages() below -- so the
# fixed portion (this cue, plus the system prompt) can be its own,
# never-merged-with-dynamic-text scaffold, exactly mirroring the
# ordinary waking Pass-1/Pass-2 message-boundary split. This is what
# makes the fixed portion of this prompt calibratable at all: a
# calibrated cost is only sound when the exact text measured is the
# exact text sent, in its own message, every time.
ARTIFACT_JUDGMENT_CLOSING_CUE = (
    "Preservation has already been authorized. Identify what was authorized, "
    "then construct the artifact. Produce the JSON now."
)


def build_artifact_construction_messages(conversation_context: str) -> list:
    """Three messages: system (fixed), the dynamic conversation
    content (its own message, with only the small fixed "The
    conversation so far:" label attached -- mirrors ordinary waking
    Pass-1's own "Current working state:"-prefixed dynamic message),
    then the fixed closing cue (its own message, never merged with the
    dynamic content)."""
    return [
        {"role": "system", "content": ARTIFACT_CONSTRUCTION_SYSTEM_PROMPT.strip()},
        {"role": "user", "content": f"The conversation so far:\n\n{conversation_context}"},
        {"role": "user", "content": ARTIFACT_JUDGMENT_CLOSING_CUE},
    ]
