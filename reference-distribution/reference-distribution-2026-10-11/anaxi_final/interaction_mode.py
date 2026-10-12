"""OWC3-S1: explicit interaction-mode contract for ordinary waking.

Frozen principle: INTERACTION MODE != PERSONALITY != KARDIA != OWNERSHIP
!= MEMORY. The host establishes only the interaction contract (whether
a task/service completion is pending); Clark still determines what he
says. Mode is never inferred from prompt content -- no phrase
detector, no classifier, no semantic inference of any kind. The only
two valid modes in this gate are `TASK` (existing default ordinary-
waking behavior, unchanged) and `CONVERSATION` (adds one fixed,
model-visible clause). Nothing else -- no reflection/deliberation
modes, no working-state layer, no topic ledger.

Pure module. No model call, no DB access, no Kardia/hippocampus
interaction.
"""

TASK = "task"
CONVERSATION = "conversation"
VALID_MODES = {TASK, CONVERSATION}

# Originally exact wording from the frozen spec (OWC3-S1 section 7).
# OWC6-P1: two standing anti-handoff statements ("...or return
# conversational direction to the human" and the standalone "You do
# not need to end each response with a question or hand the next
# choice back to the human.") were removed -- controlled testing
# (OWC6-S2/S3) established this exact language as a major structural
# attractor that suppressed ask_human/yield_direction typed selection
# even when the current human turn explicitly requested human
# direction. See OWC6-P1's final report for the evidence trail. An
# interaction contract, not a claim that Clark possesses
# phenomenological desires or feelings -- and not an instruction toward
# any particular phenotype (section 11): it permits initiative without
# requiring it, permits a genuine question without requiring one.
CONVERSATION_MODE_CLAUSE = (
    "Interaction mode: open conversation.\n\n"
    "No task is pending or implied by this mode. You do not need to "
    "search for a service request.\n\n"
    "You may choose and develop a topic, continue a line of thought, "
    "make an observation, follow an association available from the "
    "conversation, disagree, ask a question when genuinely useful, or "
    "allow a thought to remain open.\n\n"
    "If genuine clarification is required to understand what the "
    "human means, ask for it."
)


# OWC4-S1: mode-specific conversational aesthetic. Expression-layer
# modulation only -- never touches Kardia's durable aesthetic_valve
# field or any other persisted row. The override replaces the
# ALREADY-RENDERED aesthetic-directive substring inside the system
# message (built by prepare_context()/linguistic_pipeline.py from
# whichever AESTHETIC_PRESETS entry the live Kardia's aesthetic_valve
# matched) with this fixed conversational directive -- it never
# hardcodes an assumption about which preset was active; it replaces
# exactly the text prepare_context() actually returned
# (`controls["style_instruction"]`), whatever that was.
CONVERSATION_AESTHETIC_DIRECTIVE = (
    "Respond naturally and with enough development to carry a "
    "conversational thought forward. Brevity is welcome when it fits, "
    "but do not compress every response into a minimal answer. Allow "
    "room for explanation, association, reflection, and unfinished "
    "thought when useful. Avoid filler and needless repetition."
)


def apply_conversation_aesthetic(messages, mode, original_style_instruction):
    """Pure. `original_style_instruction` is the exact
    `prepared["controls"]["style_instruction"]` value prepare_context()
    already returned for THIS turn -- never recomputed, never guessed,
    never a second call into the Kardia/aesthetic-matching pipeline.

    mode == TASK: returns `messages` unchanged (same list object) --
    the persisted directive is used exactly as before.

    mode == CONVERSATION: if the leading system message contains the
    exact rendered substring `"Aesthetic directive: {original_style_
    instruction}"`, replaces it (once) with
    `"Aesthetic directive: {CONVERSATION_AESTHETIC_DIRECTIVE}"` --
    removing the original directive, never appending alongside it, so
    exactly one aesthetic directive is ever active. If the expected
    substring isn't found (unexpected rendering shape), returns
    `messages` unchanged rather than guessing at a partial/garbled
    substitution."""
    if mode != CONVERSATION:
        return messages
    if not messages or messages[0].get("role") != "system":
        return messages
    if not original_style_instruction:
        return messages
    system_content = messages[0]["content"]
    target = f"Aesthetic directive: {original_style_instruction}"
    if target not in system_content:
        return messages
    replacement = f"Aesthetic directive: {CONVERSATION_AESTHETIC_DIRECTIVE}"
    new_content = system_content.replace(target, replacement, 1)
    new_messages = [dict(m) for m in messages]
    new_messages[0] = dict(new_messages[0])
    new_messages[0]["content"] = new_content
    return new_messages


def apply_interaction_mode(messages, mode):
    """Pure. `mode` must already be resolved by the caller -- this
    function performs no inference of any kind from `messages` content.

    mode == TASK: returns `messages` completely unchanged (same list
    object, not even a copy) -- task-mode context stays logically and
    referentially identical to the pre-OWC3 ordinary-waking path.

    mode == CONVERSATION: returns a new list with the fixed
    CONVERSATION_MODE_CLAUSE appended to the leading system message's
    content (if any). No new message is created; role labels and every
    other message are untouched. If there is no leading system
    message, `messages` is returned unchanged (nothing to attach the
    clause to) -- this gate does not fabricate one."""
    if mode not in VALID_MODES:
        raise ValueError(f"unknown interaction_mode: {mode!r}")
    if mode != CONVERSATION:
        return messages
    if not messages or messages[0].get("role") != "system":
        return messages
    new_messages = [dict(m) for m in messages]
    new_messages[0] = dict(new_messages[0])
    new_messages[0]["content"] = new_messages[0]["content"] + "\n\n" + CONVERSATION_MODE_CLAUSE
    return new_messages
