"""Evidence for a claim the toy agent does NOT satisfy: !digest is recorded but never delivered.

The first test passes (the backend has the digest).  The second fails, because
nothing reached the subject.  The claim therefore FAILs, and for that reason:
backend success is not delivery.
"""

from claim_harness.checks import check_delivered
from toy_agent import item, open_demo


def test_digest_is_recorded_in_history(tmp_path):
    agent = open_demo(tmp_path / "state")
    agent.submit(item("i1", "!note meet at noon"))
    outcome = agent.submit(item("i2", "!digest"))
    assert outcome == {"kind": "reply", "text": "digest: 1 note(s); latest: meet at noon"}
    assert agent.events()[-1]["outcome"] == outcome


def test_digest_reaches_the_subject(tmp_path):
    agent = open_demo(tmp_path / "state")
    agent.submit(item("i1", "!note meet at noon"))
    check_delivered(agent, agent.channel, item("i2", "!digest"), subject="owner")
