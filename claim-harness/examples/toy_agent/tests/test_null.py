"""Lawful null is typed and distinct from failure."""

from claim_harness.checks import check_lawful_null
from toy_agent import item


def test_plain_chat_is_a_typed_null(agent):
    check_lawful_null(agent, item("i1", "good morning"))


def test_empty_recall_is_a_typed_null(agent):
    check_lawful_null(agent, item("i1", "!recall", scope="beta"))


def test_null_is_recorded_in_history(agent):
    agent.submit(item("i1", "good morning"))
    assert agent.events()[-1]["outcome"] == {"kind": "null", "reason": "no_action_requested"}


def test_internal_failure_is_an_error_not_a_null(agent, monkeypatch):
    def broken(event):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(agent, "_decide", broken)
    outcome = agent.submit(item("i1", "good morning"))
    assert outcome["kind"] == "error" and "disk on fire" in outcome["reason"]
