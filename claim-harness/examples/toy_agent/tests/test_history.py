"""Provenance, identity of the producer, and history versus reinterpretation."""

from claim_harness.checks import check_history_not_rewritten, check_producer_recorded, check_provenance
from toy_agent import item


def test_every_outcome_traces_to_its_input(agent):
    agent.submit(item("i1", "!note water the plants"))
    agent.submit(item("i2", "hello there"))
    agent.submit(item("i3", "!send ops-desk status ok"))
    check_provenance(agent, derived_kinds=["outcome"])
    outcome = agent.events()[-1]
    assert outcome["caused_by"] == next(e["event_id"] for e in agent.events() if e.get("input_id") == "i3")


def test_outcomes_record_the_responder(agent):
    agent.submit(item("i1", "!note first"))
    swapped = agent.reopen(responder="rules/2")
    swapped.submit(item("i2", "!note second"))
    check_producer_recorded(swapped, produced_kinds=["outcome"])
    producers = [e["produced_by"] for e in swapped.events() if e["kind"] == "outcome"]
    assert producers == ["rules/1", "rules/2"]


def test_agent_identity_is_not_the_responder(agent):
    swapped = agent.reopen(responder="rules/2")
    assert swapped.agent_id == agent.agent_id
    assert swapped.agent_id not in ("rules/1", "rules/2")


def test_annotation_does_not_rewrite_history(agent):
    agent.submit(item("i1", "!note buy milk"))
    target = agent.events()[0]["event_id"]
    check_history_not_rewritten(agent, reinterpret=lambda: agent.annotate(target, "actually meant oat milk"),
                                reopen=agent.reopen)
    assert agent.annotations() == [{"event_id": target, "note": "actually meant oat milk"}]


def test_history_reader_returns_copies(agent):
    agent.submit(item("i1", "!note original"))
    agent.events()[0]["text"] = "tampered"
    assert agent.events()[0]["text"] == "!note original"


def test_recall_lists_notes_oldest_first(agent):
    agent.submit(item("i1", "!note one"))
    agent.submit(item("i2", "!note two"))
    assert agent.submit(item("i3", "!recall"))["text"] == "notes: one; two"
