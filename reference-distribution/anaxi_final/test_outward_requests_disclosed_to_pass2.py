"""Regression: the subject's own optional outward requests (search / fetch /
Discord / Sleep / directive / boundary report) must be disclosed to the
subject's Pass 2 as host facts.

Live finding with the real model (2026-09-18): a search request was recorded
correctly, but Pass 2 was never told the subject had made one and answered
"I don't have the capability to search the web" -- a false statement to the
owner about a capability the subject had just used. The requests are carried
out after the reply, so Pass 2 must also be told the outcome is not yet known.
"""
import inspect

import pytest

import conversation_direction as cd
import test_pass1_budget_bugfix_v0 as env


def _act(**overrides):
    base = {
        "act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False,
        "background_activity_request": "none", "sleep_timing_request": "none", "boundary_inquiry_request": None,
        "operative_directive_request": "none", "operative_directive_text": "",
        "external_info_request": "none", "external_info_target": "",
        "discord_correspondence_request": "none", "discord_destination_id": "", "discord_message_text": "",
    }
    base.update(overrides)
    return base


def test_no_outward_request_adds_nothing():
    assert cd.describe_outward_requests(_act()) == ""


@pytest.mark.parametrize("overrides,expected", [
    ({"external_info_request": "web_search", "external_info_target": "capital of Australia"},
     ["web_search", "capital of Australia", "after your reply", "later turn", "do not have it yet"]),
    ({"external_info_request": "fetch_url", "external_info_target": "https://example.com/"},
     ["fetch_url", "https://example.com/", "read-only"]),
    ({"discord_correspondence_request": "send_message", "discord_destination_id": "discord_destination-x",
      "discord_message_text": "Running late tonight."},
     ["discord_destination-x", "'Running late tonight.'", "has not been sent yet"]),
    ({"sleep_timing_request": "request_sleep"}, ["Sleep request", "owner decides"]),
    ({"operative_directive_request": "set_directive", "operative_directive_text": "Ask before advising."},
     ["operative directive", "'Ask before advising.'", "records it after your reply"]),
    ({"operative_directive_request": "withdraw_directive"}, ["withdraw your operative directive"]),
    ({"boundary_inquiry_request": {"query_kind": "capability", "query_target": "library"}},
     ["capability:library", "not your conclusion"]),
])
def test_each_request_is_described_with_only_mechanical_facts(overrides, expected):
    text = cd.describe_outward_requests(_act(**overrides))
    for fragment in expected:
        assert fragment in text, (fragment, text)


def test_long_subject_text_is_shown_bounded_and_says_so():
    text = cd.describe_outward_requests(_act(
        discord_correspondence_request="send_message", discord_destination_id="d", discord_message_text="x" * 1500,
    ))
    assert "500 more characters" in text and len(text) < 1400


def test_real_pass2_prompt_carries_the_disclosure_and_only_when_a_request_was_made():
    la, call_log, _p1, _p2, _persistence, _destination = env._fresh_fully_capable_environment(
        pass1_value={
            "act": "develop_current", "thread": "weather", "direction_request": "none",
            "relinquish_direction": False, "external_info_request": "web_search",
            "external_info_target": "capital of Australia",
        },
    )
    writer = la.native_provenance_writer
    original = writer.stage_and_record_native_waking_turn
    accepted = set(inspect.signature(original).parameters)
    writer.stage_and_record_native_waking_turn = lambda *a, **k: original(*a, **{x: y for x, y in k.items() if x in accepted})

    la.run_waking_turn(la.AnaxiOrchestrator(), "Could you look that up?", interaction_mode="conversation")

    pass2 = [c for c in call_log if not (isinstance(c["format"], dict) and "act" in c["format"].get("properties", {}))][-1]
    text = "\n".join(m["content"] for m in pass2["messages"])
    assert "You requested web_search (capital of Australia)" in text
    # No canonical human input exists in this unbound fixture, so nothing durable can anchor a
    # pre-reply dispatch: the request is honestly deferred to the after-commit path.
    assert "you do not have it yet" in text
    assert "performed this read-only request just now" not in text


@pytest.mark.parametrize("overrides,name", [
    ({"external_info_request": "fetch_url"}, "external_info_request"),
    ({"external_info_request": "web_search", "external_info_target": "  "}, "external_info_request"),
    ({"operative_directive_request": "set_directive"}, "operative_directive_request"),
    ({"discord_correspondence_request": "send_message", "discord_destination_id": "d"}, "discord_correspondence_request"),
    ({"external_info_target": "the photograph"}, "external_info_request"),        # words with no request
    ({"external_info_request": "web_search", "external_info_target": "x" * 5000}, "external_info_request"),
])
def test_an_incomplete_outward_request_is_not_made_and_the_turn_still_completes(overrides, name):
    """Real model: a fetch with no URL / set_directive with no text killed the turn
    at Pass 1 -- an unanswered, unrecoverable H. The incomplete request is dropped,
    nothing is dispatched, and the subject is told."""
    la, call_log, _p1, _p2, _persistence, _destination = env._fresh_fully_capable_environment(
        pass1_value={"act": "develop_current", "thread": "t", "direction_request": "none",
                     "relinquish_direction": False, **overrides},
    )
    writer = la.native_provenance_writer
    original = writer.stage_and_record_native_waking_turn
    accepted = set(inspect.signature(original).parameters)
    seen = {}

    def stage(*args, **kwargs):
        seen.update(kwargs)
        return original(*args, **{k: v for k, v in kwargs.items() if k in accepted})

    writer.stage_and_record_native_waking_turn = stage
    result = la.run_waking_turn(la.AnaxiOrchestrator(), "Please do that.", interaction_mode="conversation")

    assert result["reply"] == "An ordinary reply."
    for field in ("external_info_request", "operative_directive_request", "discord_correspondence_request"):
        assert seen.get(field, "none") == "none", field         # nothing recorded, nothing to dispatch
    pass2 = [c for c in call_log if not (isinstance(c["format"], dict) and "act" in c["format"].get("properties", {}))][-1]
    assert f"Your {name} choice was incomplete or unusable" in "\n".join(m["content"] for m in pass2["messages"])


def test_complete_requests_and_other_failures_are_untouched():
    complete = {"act": "develop_current", "thread": "t", "direction_request": "none", "relinquish_direction": False,
                "external_info_request": "web_search", "external_info_target": "x"}
    assert cd.drop_incomplete_outward_requests(complete) == (None, [])
    withdraw = dict(complete, external_info_request="none", external_info_target="",
                    operative_directive_request="withdraw_directive")
    assert cd.drop_incomplete_outward_requests(withdraw) == (None, [])
    assert cd.drop_incomplete_outward_requests("not json") == (None, [])
