"""WITNESS GATE V0: a decision point at a mechanically identifiable consequential boundary.

When a consequential action the subject chose (a workspace/journal mutation, an outward send) is about to be
executed, the host shows a compact witness record of facts ANAXI already knows -- the occasion, the exact action,
the exact target/destination and payload, whether the target exists, the authority scope, the load-bearing
canonical state -- and asks for ONE structured decision: proceed | cancel | inspect.  `inspect` is offered only
when an applicable read-only source exists; it performs exactly one lawful read, delivers the exact result, and
re-enters the SAME gate once, where only proceed | cancel remain.  No inspection chain.

What the gate never does: recommend a decision, call one safer or expected, state what the facts mean, or run
anywhere but at the boundary.  It is silent when no consequential action is about to execute.  It forces
witnessing, never belief: the host executes exactly what the subject decides.

Records: one content-free operational line per gate (occasion kind, action family, target-exists, decisions,
inspected) -- never canonical history and never a second truth store; the executed action's own canonical
record and receipts remain the only authority about what happened."""
import json
import os
import time

import context_budget

PROCEED, CANCEL, INSPECT = "proceed", "cancel", "inspect"
LOG_NAME = "witness_gate_log.jsonl"
DECISION_FIELD = "decision"
WITNESS_HEAD = "Witness gate (host mechanics, before a consequential action executes; not anyone's words):"
INSPECTION_HEAD = "Result of the inspect you chose at this witness gate (host-delivered, read-only; not anyone's words):"


def decision_schema(options):
    return {"type": "object", "properties": {DECISION_FIELD: {"type": "string", "enum": list(options)}},
            "required": [DECISION_FIELD], "additionalProperties": False}


def occasion_lines(*, pulse_kind=None, caret=False):
    """Who or what initiated this occasion -- facts only."""
    if pulse_kind == "pulse":
        return ["occasion: a Pulse (timer only)",
                "no human message initiated this occasion",
                "no separately authorized external event initiated this occasion"]
    if pulse_kind == "action_receipt":
        return ["occasion: the receipt of an earlier outward act (no human message initiated this occasion)"]
    if caret:
        return ["occasion: an inbound Caret message (a correspondent's words, not the human's)"]
    return ["occasion: a human message (the human's words are in this conversation)"]


def render_witness(occasion, action_lines, inspect_description=None):
    options = [PROCEED, CANCEL] + ([INSPECT] if inspect_description else [])
    lines = [WITNESS_HEAD] + [f"- {line}" for line in occasion + action_lines]
    lines.append(f"Decide exactly one: proceed = execute exactly this action | cancel = do not execute it"
                 + (f" | inspect = {inspect_description}, then decide proceed or cancel" if inspect_description else "")
                 + ".")
    return "\n".join(lines), options


def _decide(la, *, system_text, dialogue_units, current_input, current_input_role, evidence_texts, witness_text,
            options, occasion_envelope=None):
    instruction = ('Answer with exactly this JSON and nothing else: {"decision": "<' + " | ".join(options) + '>"}')
    hard_core = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, system_text + "\n\n" + instruction,
                                            hard=True)
    hard_human = context_budget.Contribution(context_budget.CURRENT_HUMAN_MESSAGE, current_input, hard=True)
    hard_witness = context_budget.Contribution(
        context_budget.ACTION_RECORD_RESULT, "\n\n".join(list(evidence_texts) + [witness_text]), hard=True)
    from session_dialogue_window import dialogue_contribution, dialogue_messages
    dialogue = dialogue_contribution(list(dialogue_units))
    contributions = [hard_core, hard_human, hard_witness, dialogue]
    la.recost_when_shedding(contributions, context_budget.PASS1_MAX_PROMPT_BUDGET)
    result = context_budget.compose_within_budget(contributions, context_budget.PASS1_MAX_PROMPT_BUDGET)
    if not result.fits:
        # The exact words and the occasion's receipts echo what Clark authored this occasion, beside the
        # admitted human input: both are carried up to the turn reserve on the waking serving context.
        result = la._compose_with_turn_reserve(contributions, context_budget.PASS1_MAX_PROMPT_BUDGET) or result
    if not result.fits:
        return None
    messages = ([{"role": "system", "content": hard_core.rendered_text}] + dialogue_messages(dialogue)
                + [{"role": current_input_role, "content": current_input}]
                + [la._host_evidence_message(t, occasion_envelope) for t in evidence_texts]
                + [la._host_evidence_message(witness_text, occasion_envelope)])
    raw = la.ask_llama_for_json(messages, structured_schema=decision_schema(options),
                                generation_reserve=context_budget.PASS1_GENERATION_RESERVE)
    try:
        decision = (json.loads(raw) if isinstance(raw, str) else raw).get(DECISION_FIELD)
    except (TypeError, ValueError, AttributeError):
        return None
    return decision if decision in options else None


def run_gate(la, *, family, occasion, action_lines, system_text, dialogue_units, current_input, current_input_role,
             occasion_envelope=None,
             evidence_texts=(), inspect_description=None, inspect_fn=None, facts=None, log_dir=None):
    """One gate.  Returns {"decision": proceed|cancel, "decisions": [...], "inspected": bool, "witness": text}.
    A decision the provider could not deliver (budget/parse) is never treated as consent: nothing executes."""
    witness, options = render_witness(occasion, action_lines, inspect_description if inspect_fn else None)
    common = dict(system_text=system_text, dialogue_units=dialogue_units, current_input=current_input,
                  current_input_role=current_input_role, evidence_texts=list(evidence_texts),
                  occasion_envelope=occasion_envelope)
    first = _decide(la, witness_text=witness, options=options, **common)
    decisions, inspected = [first], False
    final = first
    if first == INSPECT:
        inspected = True
        inspection = inspect_fn()
        witness_after, options_after = render_witness(occasion, action_lines, None)
        final = _decide(la, witness_text=INSPECTION_HEAD + "\n" + inspection + "\n\n" + witness_after,
                        options=options_after, **common)
        decisions.append(final)
    outcome = {"family": family, "decision": PROCEED if final == PROCEED else CANCEL, "decisions": decisions,
               "decision_unavailable": final is None, "inspected": inspected, "witness": witness,
               "facts": dict(facts or {})}
    _log(log_dir, {"ts": int(time.time()), "family": family, "occasion": occasion[0], "facts": dict(facts or {}),
                   "decisions": decisions, "inspected": inspected})
    return outcome


def _log(log_dir, record):
    if not log_dir:
        return
    try:
        with open(os.path.join(log_dir, LOG_NAME), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass


def cancelled_note(family, target):
    """The host fact delivered to the reply pass after a cancel (what did not happen; no evaluation)."""
    return f"At the witness gate you chose cancel: the {family} ({target}) was not executed; nothing changed."
