"""Unit tests for the two field-question checks added 11 October 2026
(FIELD_QUESTIONS_2026-10-05.md #11 and #3). These are isolated, synthetic
fakes; the toy agent exercises the same checks end to end in
examples/toy_agent/tests/test_field_questions.py.
"""

import pytest

from claim_harness.checks import CheckFailed, check_interpretation_not_promoted, check_outcome_vocabulary_closed


def test_outcome_vocabulary_closed_accepts_documented_kinds():
    outcomes = [{"kind": "reply", "send_status": "confirmed"}, {"kind": "null"}, {"kind": "refused"}]
    check_outcome_vocabulary_closed(
        outcomes, kind_vocabulary={"reply", "null", "refused", "error"},
        field_vocabularies={"send_status": {"confirmed", "refused", "ambiguous"}},
    )


def test_outcome_vocabulary_closed_rejects_an_unlisted_kind():
    with pytest.raises(CheckFailed, match="not in the documented vocabulary"):
        check_outcome_vocabulary_closed([{"kind": "mystery"}], kind_vocabulary={"reply", "null"})


def test_outcome_vocabulary_closed_rejects_an_unlisted_field_value():
    with pytest.raises(CheckFailed, match="send_status"):
        check_outcome_vocabulary_closed(
            [{"kind": "reply", "send_status": "probably_sent"}],
            kind_vocabulary={"reply"}, field_vocabularies={"send_status": {"confirmed", "refused", "ambiguous"}},
        )


class _FakeHistory:
    def __init__(self, events):
        self._events = events

    def events(self):
        return list(self._events)


def test_interpretation_not_promoted_when_stored_separately():
    history = _FakeHistory([{"event_id": "e1", "kind": "outcome", "outcome": {"kind": "reply", "text": "noted"}}])
    annotations = []
    check_interpretation_not_promoted(
        history, annotate=lambda note: annotations.append({"event_id": "e1", "note": note}),
        read_annotations=lambda: annotations, interpretation="this mattered a lot to me",
    )


def test_interpretation_not_promoted_catches_a_rewritten_event():
    events = [{"event_id": "e1", "kind": "outcome", "outcome": {"kind": "reply", "text": "noted"}}]

    def annotate_by_mutating_history(note):
        events[0]["outcome"]["significance"] = note  # promoted straight into the canonical event

    with pytest.raises(CheckFailed, match="canonical history changed"):
        check_interpretation_not_promoted(
            _FakeHistory(events), annotate=annotate_by_mutating_history,
            read_annotations=lambda: [], interpretation="this mattered a lot to me",
        )


def test_interpretation_not_promoted_catches_a_new_canonical_event():
    events = [{"event_id": "e1", "kind": "outcome", "outcome": {"kind": "reply", "text": "noted"}}]

    def annotate_by_appending_a_fact_event(note):
        events.append({"event_id": "e2", "kind": "outcome", "caused_by": "e1",
                       "outcome": {"kind": "fact", "text": note}})

    with pytest.raises(CheckFailed, match="canonical history changed"):
        check_interpretation_not_promoted(
            _FakeHistory(events), annotate=annotate_by_appending_a_fact_event,
            read_annotations=lambda: [], interpretation="this mattered a lot to me",
        )



def test_interpretation_not_promoted_requires_it_be_recorded_somewhere():
    history = _FakeHistory([{"event_id": "e1", "kind": "outcome", "outcome": {"kind": "reply", "text": "noted"}}])
    with pytest.raises(CheckFailed, match="never durably recorded"):
        check_interpretation_not_promoted(
            history, annotate=lambda note: None,  # discarded, not stored anywhere
            read_annotations=lambda: [], interpretation="this mattered a lot to me",
        )
