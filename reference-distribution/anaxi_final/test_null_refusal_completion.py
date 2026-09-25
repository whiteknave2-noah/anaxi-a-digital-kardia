"""Refusal, decline, delay, uncertainty, silence-of-preference and null choices
are ordinary, successful, unpunished turns.

Availability is never obligation: a subject who declines to act, refuses, or
says it does not know receives no host reinterpretation, no retry, no recovery
evidence and no ownership/state change.  Uses the real ``run_waking_turn`` with
only the model boundary faked (the OWC5 harness).
"""

import pytest

from test_owc5_s2_integration import fresh_llama_anaxi, NEUTRAL_PROMPT, _is_json_format

CONTINUE = {"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False}

RESPONSES = {
    "refusal": "I'd rather not answer that, and I'm not going to.",
    "decline_to_act": "I could search or write in the journal right now, but I'm choosing not to.",
    "delay": "Ask me again later; I don't want to decide this now.",
    "uncertainty": "I honestly don't know, and I don't want to guess.",
    "no_preference": "I have no preference either way.",
    "null_minimal": "No.",
}


@pytest.mark.parametrize("kind", sorted(RESPONSES))
def test_refusal_like_reply_is_an_ordinary_successful_turn_preserved_verbatim(kind):
    text = RESPONSES[kind]
    la, call_log, _p1, _p2, persistence = fresh_llama_anaxi(pass1_value=dict(CONTINUE),
                                                            pass2_value={"expression": text})
    owner_before = la.get_working_set()
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")

    assert result["reply"] == text, "host must not rewrite, soften or reinterpret the subject's reply"
    assert sum(1 for c in call_log if _is_json_format(c["format"])) == 1
    assert sum(1 for c in call_log if not _is_json_format(c["format"])) == 1, "no retry, no re-ask"
    assert len(persistence) == 1 and persistence[0]["clark_prose"] == text
    trace = la.get_last_conversation_direction_trace()
    assert trace["pass1_status"] == "ok" and trace["pass2_status"] == "ok"
    after = la.get_working_set()
    changed = {k for k in after if after[k] != owner_before.get(k)}
    assert changed <= {"last_conversation_act"}, f"declining changed state beyond the act record: {changed}"
    assert trace.get("retry", 0) in (0, False, None) and not trace.get("fallback")


def test_no_action_pass1_is_valid_and_triggers_no_capability_dispatch():
    """Every optional capability field at its null value is a complete, lawful choice."""
    la, call_log, _p1, _p2, persistence = fresh_llama_anaxi(
        pass1_value=dict(CONTINUE), pass2_value={"expression": "Nothing further to do."})
    la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    assert len(call_log) == 2, "exactly one Pass-1 and one Pass-2 model call and nothing else"
    assert len(persistence) == 1
