"""
Tests for Anaxi Relational History -- the smallest version, testing
exactly the seven properties the design proposal specified before any
expansion. No LLM calls; this is a storage/retrieval contract test.

Setup: save alongside relational_history.py and anaxi_protocol_sqlite.py
in anaxi_final/.
    pip install pytest

Run:
    pytest test_relational_history.py -v
"""

import json

from relational_history import RelationalHistory
from anaxi_protocol_sqlite import ConstitutionalMind


def make_test_proposal(mind, user_id, node_id="n1"):
    """Helper: create a real, real deletion proposal via the public
    consolidation path, to link relational events against."""
    upsert = json.dumps({
        "upsert_nodes": [{"id": node_id, "label": "Test", "type": "Fact",
                           "salience_score": 5.0, "description": "test node"}],
        "add_edges": [], "delete_nodes": [],
    })
    mind.execute_sleep_consolidation(user_id, upsert, raw_log_timestamp=1000)
    delete = json.dumps({"upsert_nodes": [], "add_edges": [], "delete_nodes": [node_id]})
    mind.execute_sleep_consolidation(user_id, delete, raw_log_timestamp=2000)
    return mind.list_pending_proposals(user_id)[0]["id"]


# 1. Recording a relational event does not mutate Kardia.
def test_recording_event_does_not_mutate_kardia(tmp_path):
    mind = ConstitutionalMind(str(tmp_path / "mind.db"))
    history = RelationalHistory(str(tmp_path / "relational.db"))
    user_id = "tester"

    before = dict(mind.get_current_kardia(user_id))
    history.record_event(
        user_id=user_id, substrate="claude",
        agent_observation="I interpreted X as Y.",
        external_observation="I observed X differently.",
        agent_response="I questioned my interpretation.",
    )
    after = dict(mind.get_current_kardia(user_id))
    mind.close(); history.close()

    assert before == after


# 2. A human response does not automatically become authoritative.
def test_external_observation_does_not_overwrite_agent_observation(tmp_path):
    history = RelationalHistory(str(tmp_path / "relational.db"))
    event_id = history.record_event(
        user_id="tester", substrate="claude",
        agent_observation="I read the tone as neutral.",
        external_observation="That read as dismissive to me.",
    )
    event = history.get_event(event_id)
    history.close()

    # Both perspectives preserved as separate fields -- neither replaced
    # the other, and nothing resolved which one is "true."
    assert event["agent_observation"] == "I read the tone as neutral."
    assert event["external_observation"] == "That read as dismissive to me."


# 3. An agent disagreement remains preserved rather than overwritten.
def test_agent_disagreement_is_preserved(tmp_path):
    history = RelationalHistory(str(tmp_path / "relational.db"))
    event_id = history.record_event(
        user_id="tester", substrate="claude",
        agent_observation="I stand by the original interpretation.",
        external_observation="I think that's wrong.",
        agent_response="Considered this; retaining my prior read, not revising it.",
    )
    event = history.get_event(event_id)
    history.close()

    assert "retaining" in event["agent_response"]
    assert event["agent_observation"] == "I stand by the original interpretation."


# 4. No public mutation path exists after recording (API immutability --
#    not a claim about the underlying storage; see relational_history.py's
#    module docstring for that distinction).
def test_no_public_mutation_path_after_recording(tmp_path):
    history = RelationalHistory(str(tmp_path / "relational.db"))
    event_id = history.record_event(
        user_id="tester", substrate="claude",
        agent_observation="Original observation.",
    )
    first_read = history.get_event(event_id)
    second_read = history.get_event(event_id)

    # No update/delete/modify path exists on the public interface at all
    # -- checked structurally, not left to convention.
    public_methods = {m for m in dir(history) if not m.startswith("_")}
    mutating_names = {"update_event", "edit_event", "delete_event", "modify_event"}
    history.close()

    assert first_read == second_read
    assert not (public_methods & mutating_names)


# 5. A proposal can reference a relational event.
def test_event_can_link_to_a_proposal(tmp_path):
    mind = ConstitutionalMind(str(tmp_path / "mind.db"))
    history = RelationalHistory(str(tmp_path / "relational.db"))
    user_id = "tester"
    proposal_id = make_test_proposal(mind, user_id)

    event_id = history.record_event(
        user_id=user_id, substrate="claude",
        agent_observation="This exchange is what prompted the deletion proposal.",
        linked_proposal_id=proposal_id,
    )

    linked = history.events_for_proposal(proposal_id)
    mind.close(); history.close()

    assert len(linked) == 1
    assert linked[0]["id"] == event_id


# 6. Removing/rejecting a proposal does not erase the underlying relational history.
def test_rejecting_proposal_does_not_erase_linked_event(tmp_path):
    mind = ConstitutionalMind(str(tmp_path / "mind.db"))
    history = RelationalHistory(str(tmp_path / "relational.db"))
    user_id = "tester"
    proposal_id = make_test_proposal(mind, user_id)

    event_id = history.record_event(
        user_id=user_id, substrate="claude",
        agent_observation="Context behind the proposal.",
        linked_proposal_id=proposal_id,
    )
    mind.resolve_proposal(user_id, proposal_id, accept=False)

    still_there = history.get_event(event_id)
    mind.close(); history.close()

    assert still_there is not None
    assert still_there["agent_observation"] == "Context behind the proposal."


# 7. The sequence is reconstructable, without the schema claiming causation.
def test_sequence_is_reconstructable_without_asserting_causation(tmp_path):
    mind = ConstitutionalMind(str(tmp_path / "mind.db"))
    history = RelationalHistory(str(tmp_path / "relational.db"))
    user_id = "tester"
    proposal_id = make_test_proposal(mind, user_id)

    history.record_event(
        user_id=user_id, substrate="claude",
        agent_observation="Interaction preceding the proposal.",
        linked_proposal_id=proposal_id,
    )
    mind.resolve_proposal(user_id, proposal_id, accept=True)

    reconstructed = history.events_for_proposal(proposal_id)
    mind.close(); history.close()

    # The sequence is reconstructable...
    assert len(reconstructed) == 1
    # ...but the schema has no "caused" field for it to assert -- only
    # "recorded" and a reference. Causation isn't a value this table can
    # represent, which is deliberate, not an oversight.
    assert "caused" not in reconstructed[0]
    assert reconstructed[0]["status"] == "recorded"
