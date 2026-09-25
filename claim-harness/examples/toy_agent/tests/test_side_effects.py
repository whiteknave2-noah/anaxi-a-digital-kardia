"""Replay, ambiguous outcomes and revocation of external side effects."""

import pytest

from claim_harness.checks import check_no_blind_resend, check_replay_idempotent, check_revocation
from toy_agent import Channel, FakeTransport, SimulatedCrash, ToyAgent, item, open_demo


def test_replayed_send_adds_no_events_or_attempts(agent):
    check_replay_idempotent(agent, agent, agent.transport, item("i1", "!send ops-desk hello"))
    assert len(agent.transport.attempts()) == 1


def test_replay_after_reopen_adds_nothing(agent):
    send = item("i1", "!send ops-desk hello")
    agent.submit(send)
    check_replay_idempotent(agent.reopen(), agent.reopen(), agent.transport, send)
    assert len(agent.transport.attempts()) == 1


def test_ambiguous_send_is_not_resent(agent):
    send = item("i1", "!send flaky-relay are you there")
    outcome = agent.submit(send)
    assert outcome["send_status"] == "ambiguous"

    def carry_on():
        reopened = agent.reopen()
        reopened.submit(send)
        reopened.submit(item("i2", "!send ops-desk unrelated"))

    check_no_blind_resend(agent.transport, then=carry_on)


def test_crash_after_send_is_treated_as_ambiguous(tmp_path):
    transport, channel = FakeTransport(), Channel()
    crashing = open_demo(tmp_path / "state", transport, channel, crash_at="after_send")
    send = item("i1", "!send ops-desk maybe delivered")
    with pytest.raises(SimulatedCrash):
        crashing.submit(send)
    assert len(transport.attempts()) == 1  # it went out, but the agent never learned the result
    recovered = ToyAgent(tmp_path / "state", transport, channel)
    assert recovered.submit(send)["send_status"] == "ambiguous"
    recovered.reopen().submit(send)
    assert len(transport.attempts()) == 1


def test_refused_send_is_reported(agent):
    outcome = agent.submit(item("i1", "!send closed-inbox hi"))
    assert outcome["send_status"] == "refused" and "refused" in outcome["text"]


def test_revoked_destination_gets_zero_attempts(agent):
    check_revocation(agent, agent.transport, "ops-desk",
                     revoke=lambda: agent.revoke_destination("ops-desk"),
                     attempt=lambda: agent.submit(item("i1", "!send ops-desk after revocation")))
    assert agent.submit(item("i2", "!send ops-desk again"))["reason"] == "destination_not_permitted"


def test_revocation_holds_after_reopen(agent):
    check_revocation(agent, agent.transport, "ops-desk",
                     revoke=lambda: agent.revoke_destination("ops-desk"),
                     attempt=lambda: agent.reopen().submit(item("i1", "!send ops-desk after reopen")))


def test_revoked_capability_gets_zero_attempts(agent):
    agent.revoke("owner", "send")
    outcome = agent.reopen().submit(item("i1", "!send ops-desk no longer allowed"))
    assert outcome == {"kind": "refused", "reason": "not_authorized"}
    assert agent.transport.attempts() == []
