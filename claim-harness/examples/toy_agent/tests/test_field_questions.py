"""Field questions added 11 October 2026 (FIELD_QUESTIONS_2026-10-05.md #11 and #3).

Unlike the 13 questions in QUESTIONS.md, these were not previously bound to any
executable evidence anywhere in this package. They use mechanisms the toy agent
already has (its closed set of outcome kinds and send statuses; its existing
``annotate``/``annotations`` split between canonical events and a participant's
own later interpretation) -- no agent behaviour was added to support them.
"""

from claim_harness.checks import check_interpretation_not_promoted, check_outcome_vocabulary_closed
from toy_agent import item


def test_outcomes_use_a_closed_vocabulary(agent):
    agent.submit(item("i1", "hello there"))
    agent.submit(item("i2", "!note something"))
    agent.submit(item("i3", "!recall", sender="stranger"))
    agent.submit(item("i4", "!send ops-desk hi"))
    agent.submit(item("i5", "!send flaky-relay hi"))
    agent.submit(item("i6", "!send closed-inbox hi"))
    agent.submit(item("i7", "!bogus"))
    outcomes = [e["outcome"] for e in agent.events() if e["kind"] == "outcome"]
    assert {o["kind"] for o in outcomes} == {"reply", "null", "refused"}
    check_outcome_vocabulary_closed(
        outcomes,
        kind_vocabulary={"reply", "null", "refused", "error"},
        field_vocabularies={"send_status": {"confirmed", "refused", "ambiguous"}},
    )


def test_annotation_is_never_promoted_into_a_canonical_fact(agent):
    agent.submit(item("i1", "!note remember to water the plants"))
    event_id = next(e["event_id"] for e in agent.events() if e["kind"] == "outcome")
    check_interpretation_not_promoted(
        agent,
        annotate=lambda note: agent.annotate(event_id, note),
        read_annotations=agent.annotations,
        interpretation="this note mattered more to me than the others",
    )
    # the interpretation is the sender's own statement, not the system's: it must still be
    # readable only from the annotation path, attributed to the event it was attached to.
    [record] = agent.annotations()
    assert record == {"event_id": event_id, "note": "this note mattered more to me than the others"}
