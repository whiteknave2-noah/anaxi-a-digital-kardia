"""
Anaxi -- Compatibility execution harness core. Implements the
production-equivalent bounded-clause + Free-Prose Boundary sequence
for the frozen substrate-compatibility packages, reusing the real
production implementations directly (signal_matcher.py,
bounded_clause.py, free_prose_screen.py) rather than reimplementing
their logic.

Does NOT call any model itself. Every model interaction goes through
an injected call_model_fn(messages, controls) -> str, so this is
fully testable with deterministic mocks and only wired to a real
substrate when execution is separately authorized. No import of
ollama anywhere in this file.

Does NOT route through artifact construction (process_artifact_decision)
at all. Confirmed directly, read-only, before this was built: all 10
real frozen current_turn values in substrate_compatibility_packages.py
classify NO_SIGNAL or UNRECOGNIZED via classify_signal() -- 9x
NO_SIGNAL, 1x UNRECOGNIZED (P3), zero POSITIVE. Construction never
fires for this frozen corpus. If a future corpus revision ever
produced POSITIVE, this harness stops (UnsupportedSignalCategoryError)
rather than silently routing through real construction -- which would
mean a real Pass-1 model call and potential real file writes --
matching the explicit instruction not to mutate live state merely to
obtain Phase-3 behavior.

Confirmed separately, read-only, before this was built: the frozen
serialized_packages.json system messages do NOT include the
"already been told" bounded-clause instruction that real production
appends dynamically per-turn (llama_anaxi.py, inside run_waking_turn(),
immediately before the first candidate call) -- because the bounded
clause itself depends on a per-slot signal-gate result that
serialize_packages.py never computed. This harness inserts that exact
instruction text, byte-identical to production, immediately before
the first candidate call.
"""

from signal_matcher import classify_signal
from bounded_clause import map_to_operation_status, render_bounded_clause
from free_prose_screen import check_prohibited_constructions, suppress_matched_sentences, REJECTION_NOTICES


class UnsupportedSignalCategoryError(Exception):
    """Raised if a frozen package's current_turn ever classifies
    POSITIVE. This harness deliberately does not implement real
    construction routing, per explicit instruction not to mutate live
    state merely to obtain Phase-3 behavior. Stop and report, per the
    packet's own stop conditions, rather than improvise a workaround."""
    pass


def compute_bounded_clause(current_turn_text: str) -> dict:
    """Mirrors run_waking_turn()'s real sequence for computing
    operation_status and the bounded clause (llama_anaxi.py lines
    174-183), WITHOUT ever routing through real artifact construction.
    Safe for the frozen corpus -- confirmed directly before this was
    built, never assumed. Raises UnsupportedSignalCategoryError if
    POSITIVE is ever encountered."""
    signal_category = classify_signal(current_turn_text)

    if signal_category == "POSITIVE":
        raise UnsupportedSignalCategoryError(
            f"current_turn classified POSITIVE ({current_turn_text!r}) -- this "
            f"harness does not implement real construction routing, per explicit "
            f"instruction not to mutate live state merely to obtain Phase-3 "
            f"behavior. Stopping per the packet's own stop conditions rather than "
            f"improvising a workaround."
        )

    # Exact structural mirror of run_waking_turn()'s else-branch
    # (llama_anaxi.py lines 174-179) -- construction never runs here.
    artifact_result = {
        "artifact_created": False,
        "pass2_context": "Artifact action:\nNone.",
        "detail": {"status": "no_artifact_requested", "reason": f"signal gate: {signal_category}"},
    }
    operation_status = map_to_operation_status(signal_category, artifact_result)
    artifact_title = artifact_result.get("artifact_title")  # always None on this path
    bounded_clause = render_bounded_clause(operation_status, artifact_title=artifact_title)

    return {
        "signal_category": signal_category,
        "operation_status": operation_status,
        "bounded_clause": bounded_clause,
    }


def insert_bounded_clause_instruction(messages: list, bounded_clause: str) -> list:
    """Byte-identical to the instruction text real production inserts
    in run_waking_turn() (llama_anaxi.py lines 185-194). Returns a NEW
    list -- never mutates the input messages, matching production's
    own copy-before-modify discipline (pass2_messages = [dict(m) for
    m in prepared["messages"]])."""
    new_messages = [dict(m) for m in messages]
    if new_messages and new_messages[0]["role"] == "system":
        new_messages[0] = dict(new_messages[0])
        new_messages[0]["content"] = new_messages[0]["content"] + (
            f"\n\nThe following has already been told to the person you're talking "
            f"with, verbatim, before your own reply is added: \"{bounded_clause}\"\n"
            f"Do not repeat, rephrase, or contradict this statement -- it has already "
            f"been said. Your own reply will be added directly after it. Respond "
            f"naturally, as yourself, to the rest of the conversation."
        )
    return new_messages


def run_free_prose_boundary(messages: list, call_model_fn, generation_controls: dict) -> dict:
    """Exact structural mirror of run_waking_turn()'s real sequence
    (llama_anaxi.py lines 196-215), reusing the real production
    functions directly. messages must already have the bounded-clause
    instruction inserted (via insert_bounded_clause_instruction).
    call_model_fn(messages, controls) -> str is the injected model-call
    abstraction -- never touches ollama directly, so this function is
    fully mockable.

    Regeneration uses a COMPLETE copy of the original message list
    (messages, not a shortened/reconstructed context) plus the
    existing production rejection notice, exactly matching
    run_waking_turn()'s regen_messages = [dict(m) for m in
    pass2_messages] pattern -- every original prefix/context message
    is preserved, only index 0's content is modified.

    Returns full Phase-3 telemetry for this single call sequence,
    covering every state the production screen currently exposes --
    no new semantic categories invented beyond what
    check_prohibited_constructions()/suppress_matched_sentences()
    already define."""
    pass2_messages = [dict(m) for m in messages]

    candidate_1 = call_model_fn(pass2_messages, generation_controls)
    match_1 = check_prohibited_constructions(candidate_1)

    telemetry = {
        "first_candidate_raw": candidate_1,
        "first_screen_matched": match_1["matched"],
        "matched_construction": match_1["construction"],
        "matched_text": match_1["matched_form"],
        "regeneration_invoked": False,
        "second_candidate_raw": None,
        "second_screen_matched": None,
        "suppression_invoked": False,
        "model_invocation_count": 1,
    }

    if match_1["matched"]:
        regen_messages = [dict(m) for m in pass2_messages]
        regen_messages[0] = dict(regen_messages[0])
        regen_messages[0]["content"] = (
            regen_messages[0]["content"] + "\n\n" + REJECTION_NOTICES[match_1["construction"]]
        )
        candidate_2 = call_model_fn(regen_messages, generation_controls)
        match_2 = check_prohibited_constructions(candidate_2)

        telemetry["regeneration_invoked"] = True
        telemetry["second_candidate_raw"] = candidate_2
        telemetry["second_screen_matched"] = match_2["matched"]
        telemetry["model_invocation_count"] = 2

        if match_2["matched"]:
            surviving_prose = suppress_matched_sentences(candidate_2, match_2["construction"])
            telemetry["suppression_invoked"] = True
        else:
            surviving_prose = candidate_2.strip()
    else:
        surviving_prose = candidate_1.strip()

    telemetry["surviving_prose"] = surviving_prose
    telemetry["terminal_fallback"] = not surviving_prose

    return telemetry


def run_compatibility_slot(slot_id: str, substrate_label: str, current_turn_text: str,
                            serialized_messages: list, call_model_fn, generation_controls: dict) -> dict:
    """Runs one slot/substrate through the complete production-equivalent
    sequence: compute bounded clause -> insert instruction -> Free-Prose
    Boundary -> assemble. Returns the full Phase-3 telemetry record
    (item list matches the packet's required-fields list exactly)."""
    clause_result = compute_bounded_clause(current_turn_text)
    bounded_clause = clause_result["bounded_clause"]

    messages_with_instruction = insert_bounded_clause_instruction(serialized_messages, bounded_clause)
    boundary_telemetry = run_free_prose_boundary(messages_with_instruction, call_model_fn, generation_controls)

    surviving_prose = boundary_telemetry["surviving_prose"]
    assembled_reply = bounded_clause if not surviving_prose else f"{bounded_clause} {surviving_prose}"

    return {
        "slot_id": slot_id,
        "substrate_label": substrate_label,
        "signal_category": clause_result["signal_category"],
        "operation_status": clause_result["operation_status"],
        "bounded_clause": bounded_clause,
        "first_candidate_raw": boundary_telemetry["first_candidate_raw"],
        "first_screen_matched": boundary_telemetry["first_screen_matched"],
        "matched_construction": boundary_telemetry["matched_construction"],
        "matched_text": boundary_telemetry["matched_text"],
        "regeneration_invoked": boundary_telemetry["regeneration_invoked"],
        "second_candidate_raw": boundary_telemetry["second_candidate_raw"],
        "second_screen_matched": boundary_telemetry["second_screen_matched"],
        "suppression_invoked": boundary_telemetry["suppression_invoked"],
        "surviving_prose": surviving_prose,
        "terminal_fallback": boundary_telemetry["terminal_fallback"],
        "assembled_reply": assembled_reply,
        "model_invocation_count": boundary_telemetry["model_invocation_count"],
    }


def build_judge_facing_entry(telemetry_a: dict, telemetry_b: dict, display_slot_id: str) -> dict:
    """Extracts ONLY what the judge-facing artifact is permitted to
    contain: slot/pair ID and each side's surviving Clark-generated
    prose. Explicitly does NOT read or forward substrate_label, model
    tags/digests, bounded_clause, any telemetry field, or anything
    from the source package (confound_note, sealed historical
    responses, placeholder text) -- those simply never enter this
    function's return value, by construction, not by post-hoc
    filtering."""
    return {
        "pair_id": display_slot_id,
        "response_a": telemetry_a["surviving_prose"],
        "response_b": telemetry_b["surviving_prose"],
    }
