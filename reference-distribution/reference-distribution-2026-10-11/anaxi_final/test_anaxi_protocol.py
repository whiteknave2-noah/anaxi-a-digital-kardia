"""
Tests for the Anaxi Protocol's proposal lifecycle.

These check the specific properties raised in review: that pending
proposals survive a restart, that rejected proposals stay inspectable
rather than being deleted, that acceptance produces the real state
change it claims to, that an unresolved proposal cannot silently
mutate Kardia, that AnaxiOrchestrator can actually drive this
lifecycle end to end, whether identity-shift proposals carry any
reference back to what triggered them, and that resolving a proposal
is a one-way, terminal action.

These are protocol-level tests -- no LLM calls involved. Where the code
under test decides *whether* to defer an identity change (the embedding-
based axiom checks), these tests patch that decision directly rather
than depending on real embedding output, so the tests stay
deterministic. The proposal *mechanism* is what's under test here, not
the axiom-similarity thresholds -- those need real conversations to
evaluate, not a unit test.

Setup: save this file inside anaxi_final/, next to anaxi_protocol_sqlite.py
and orchestration.py.
    pip install pytest

Run:
    pytest test_anaxi_protocol.py -v
"""

import json
import time

from anaxi_protocol_sqlite import ConstitutionalMind
from orchestration import AnaxiOrchestrator
import hippocampus_store
from provenance_schema import create_provenance_db


def _build_isolated_hippocampus_paths(tmp_path):
    """Slice-C compatibility helper: AnaxiOrchestrator.prepare_context()
    now syncs a real hippocampal DB before returning. This builds the
    smallest schema-valid, empty fixture (real canonical provenance DB,
    real relational_events table, empty JSONL, real hippocampal DB) so
    the existing prepare_context() call below continues to exercise real
    code with zero hippocampal items, rather than hitting the production
    default path this isolated test must never touch."""
    import sqlite3
    prov_path = str(tmp_path / "anaxi_provenance.db")
    rel_path = str(tmp_path / "anaxi_relational_llama.db")
    jsonl_path = str(tmp_path / "anaxi_log.jsonl")
    hip_path = str(tmp_path / "anaxi_hippocampus.db")
    create_provenance_db(prov_path).close()
    rel_conn = sqlite3.connect(rel_path)
    rel_conn.execute("""CREATE TABLE relational_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
        created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
        agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
        status TEXT NOT NULL DEFAULT 'recorded', event_id TEXT
    )""")
    rel_conn.commit()
    rel_conn.close()
    open(jsonl_path, "w").close()
    hippocampus_store.create_hippocampus_db(hip_path).close()
    return hippocampus_store.HippocampusPaths(prov_path, rel_path, jsonl_path, hip_path)


DEFAULT_KARDIA = {
    "moral_valve": "Value human agency and objective truth.",
    "volitional_channel": "Seek systemic clarity and simplify complexity.",
    "affective_stance": "Calm, intellectually enthusiastic, and objective.",
    "aesthetic_valve": "Minimalist, precise, punchy.",
}

LONG_JUSTIFICATION = (
    "Justification text deliberately written long enough to clear the "
    "compliance-length check for either ADJUST or REWRITE evolution choices."
)


def make_reflection_payload(choice="ADJUST", kardia=None, justification=None):
    return json.dumps({
        "evolution_choice": choice,
        "updated_kardia": kardia or DEFAULT_KARDIA,
        "constitutional_argument": justification or LONG_JUSTIFICATION,
    })


def upsert_test_node(mind, user_id, node_id="test_node", timestamp=1000):
    """Create a real node via the public consolidation entry point, so
    later tests can propose deleting something that actually exists."""
    rem_payload = json.dumps({
        "upsert_nodes": [{
            "id": node_id, "label": "Test node", "type": "Fact",
            "salience_score": 5.0, "description": "A node created for testing.",
        }],
        "add_edges": [],
        "delete_nodes": [],
    })
    result = mind.execute_sleep_consolidation(user_id, rem_payload, timestamp)
    assert result["status"] == "success", result
    return node_id


# ---------------------------------------------------------------------
# 1. Pending proposals survive restart
# ---------------------------------------------------------------------

def test_pending_proposal_survives_restart(tmp_path):
    db_path = str(tmp_path / "anaxi_test.db")
    user_id = "tester_restart"

    mind = ConstitutionalMind(db_path)
    node_id = upsert_test_node(mind, user_id, timestamp=1000)

    delete_payload = json.dumps({"upsert_nodes": [], "add_edges": [], "delete_nodes": [node_id]})
    result = mind.execute_sleep_consolidation(user_id, delete_payload, raw_log_timestamp=2000)
    assert result["status"] == "success"

    pending_before = mind.list_pending_proposals(user_id)
    assert any(p["proposal_type"] == "delete_node" for p in pending_before)
    mind.close()

    # Fresh connection, same file -- simulates the process restarting.
    reopened = ConstitutionalMind(db_path)
    pending_after = reopened.list_pending_proposals(user_id)
    reopened.close()

    assert pending_after == pending_before
    assert any(p["proposal_type"] == "delete_node" for p in pending_after)


# ---------------------------------------------------------------------
# 2. Rejected proposals remain inspectable
# ---------------------------------------------------------------------

def test_rejected_proposal_remains_inspectable(tmp_path):
    db_path = str(tmp_path / "anaxi_test.db")
    user_id = "tester_reject"

    mind = ConstitutionalMind(db_path)
    node_id = upsert_test_node(mind, user_id, timestamp=1000)
    delete_payload = json.dumps({"upsert_nodes": [], "add_edges": [], "delete_nodes": [node_id]})
    mind.execute_sleep_consolidation(user_id, delete_payload, raw_log_timestamp=2000)

    proposal_id = mind.list_pending_proposals(user_id)[0]["id"]
    outcome = mind.resolve_proposal(user_id, proposal_id, accept=False)
    assert outcome["status"] == "success"

    # No longer pending...
    assert mind.list_pending_proposals(user_id) == []

    # ...but not deleted. Should still exist in storage, marked rejected.
    mind.cursor.execute(
        "SELECT status, resolved_at FROM proposals WHERE id = ? AND user_id = ?",
        (proposal_id, user_id),
    )
    row = mind.cursor.fetchone()
    mind.close()

    assert row is not None, "rejected proposal should not be deleted from storage"
    status, resolved_at = row
    assert status == "rejected"
    assert resolved_at is not None


# ---------------------------------------------------------------------
# 3. Accepted proposals produce the expected state transition
# ---------------------------------------------------------------------

def test_accepted_delete_proposal_actually_deletes_node(tmp_path):
    db_path = str(tmp_path / "anaxi_test.db")
    user_id = "tester_accept"

    mind = ConstitutionalMind(db_path)
    node_id = upsert_test_node(mind, user_id, timestamp=1000)
    delete_payload = json.dumps({"upsert_nodes": [], "add_edges": [], "delete_nodes": [node_id]})
    mind.execute_sleep_consolidation(user_id, delete_payload, raw_log_timestamp=2000)
    proposal_id = mind.list_pending_proposals(user_id)[0]["id"]

    mind.cursor.execute("SELECT 1 FROM nodes WHERE user_id = ? AND id = ?", (user_id, node_id))
    assert mind.cursor.fetchone() is not None, "node should exist before acceptance"

    outcome = mind.resolve_proposal(user_id, proposal_id, accept=True)
    assert outcome["status"] == "success"

    mind.cursor.execute("SELECT 1 FROM nodes WHERE user_id = ? AND id = ?", (user_id, node_id))
    row = mind.cursor.fetchone()
    mind.close()

    assert row is None, "accepting the proposal should have actually deleted the node"


# ---------------------------------------------------------------------
# 4. An unresolved proposal cannot silently mutate Kardia
# ---------------------------------------------------------------------

def test_unresolved_identity_proposal_does_not_mutate_kardia(tmp_path, monkeypatch):
    db_path = str(tmp_path / "anaxi_test.db")
    user_id = "tester_kardia"

    mind = ConstitutionalMind(db_path)
    original_kardia = mind.get_current_kardia(user_id)  # establishes the cold-start default

    # Force destination to pass and trajectory to defer, regardless of what
    # embeddings would actually say -- the proposal *mechanism* is under
    # test here, not the axiom-similarity thresholds.
    monkeypatch.setattr(mind, "_validate_axioms_destination", lambda new_kardia: None)
    monkeypatch.setattr(
        mind, "_validate_axioms_trajectory",
        lambda old, new: "Change is large enough to warrant human review.",
    )

    changed_kardia = dict(original_kardia)
    changed_kardia["aesthetic_valve"] = "A substantially different aesthetic stance."
    message = mind.govern_identity_revision(user_id, make_reflection_payload(kardia=changed_kardia))
    assert "deferred" in message.lower()

    still_current = mind.get_current_kardia(user_id)
    mind.close()

    assert still_current == original_kardia, (
        "Kardia changed even though the identity proposal was never resolved"
    )


# ---------------------------------------------------------------------
# 5. The orchestrator can execute the intended proposal lifecycle
#
# NOTE ON SEMANTICS THIS TEST ASSUMES, DELIBERATELY, NOT NEUTRALLY:
# resolve_proposal re-checks the proposed transition against whatever
# Kardia has become BY THE TIME OF RESOLUTION, not against the snapshot
# that existed when the proposal was created. That is one defensible
# reading of "resolve a proposal" -- "re-evaluate whether this is still
# acceptable now" -- and not the only one; "apply the recorded decision
# to current state, full stop" is a different, also-defensible contract.
# This test documents and exercises the codebase's actual choice. It is
# not asserting that choice is the only legitimate one.
# ---------------------------------------------------------------------

def test_orchestrator_resolution_reevaluates_against_current_kardia(tmp_path, monkeypatch):
    db_path = str(tmp_path / "anaxi_test.db")
    user_id = "tester_orchestrator"

    orch = AnaxiOrchestrator(db_path, hippocampus_paths=_build_isolated_hippocampus_paths(tmp_path))
    original_kardia = orch.mind.get_current_kardia(user_id)

    # First call (at proposal creation) defers; second call (at acceptance)
    # passes -- modeling a real accept under the re-evaluation semantics
    # documented above, not just a creation that never gets resolved.
    trajectory_calls = {"n": 0}
    def fake_trajectory(old, new):
        trajectory_calls["n"] += 1
        return "Deferred for this test." if trajectory_calls["n"] == 1 else None
    monkeypatch.setattr(orch.mind, "_validate_axioms_destination", lambda new_kardia: None)
    monkeypatch.setattr(orch.mind, "_validate_axioms_trajectory", fake_trajectory)

    prepared = orch.prepare_context(user_id, "What have you learned about me so far?")
    assert prepared["kardia"] == original_kardia
    assert isinstance(prepared["messages"], list) and prepared["messages"]
    assert "temperature" in prepared["controls"] and "top_p" in prepared["controls"]

    changed_kardia = dict(original_kardia)
    changed_kardia["aesthetic_valve"] = "Different, for this test."
    rem_payload = json.dumps({"upsert_nodes": [], "add_edges": [], "delete_nodes": []})

    result = orch.run_sleep_cycle(
        user_id=user_id,
        rem_json_payload=rem_payload,
        reflection_json_payload=make_reflection_payload(kardia=changed_kardia),
    )
    assert result["sleep_status"] == "success"
    assert "deferred" in result["identity_status"].lower()

    identity_proposals = [p for p in result["pending_proposals"] if p["proposal_type"] == "identity_shift"]
    assert len(identity_proposals) == 1

    resolution = orch.mind.resolve_proposal(user_id, identity_proposals[0]["id"], accept=True)
    assert resolution["status"] == "success"

    final_kardia = orch.mind.get_current_kardia(user_id)
    orch.close()

    assert final_kardia == changed_kardia, (
        "accepting the identity proposal should have updated active Kardia"
    )


# ---------------------------------------------------------------------
# 6. Identity-shift proposals carry a reference to what triggered them
#
# Deletion proposals store raw_log_timestamp in their payload -- an
# explicit pointer back to the log entry that requested the deletion.
# This test checks whether identity-shift proposals carry an equivalent
# reference to whatever observation or turn prompted the reflection
# call, under *some* field -- not specifically raw_log_timestamp. The
# property under test is "a reference exists," not a particular schema.
# Expected, going in: this fails. govern_identity_revision doesn't even
# accept a triggering-context parameter, so there's nothing for such a
# field to be built from. Left in as a real, runnable check rather than
# a claim, so it stops being a claim and starts being a result.
# ---------------------------------------------------------------------

def test_identity_proposal_carries_provenance_reference(tmp_path, monkeypatch):
    db_path = str(tmp_path / "anaxi_test.db")
    user_id = "tester_provenance"

    mind = ConstitutionalMind(db_path)
    mind.get_current_kardia(user_id)  # establish baseline

    monkeypatch.setattr(mind, "_validate_axioms_destination", lambda new_kardia: None)
    monkeypatch.setattr(
        mind, "_validate_axioms_trajectory",
        lambda old, new: "Deferred so this ends up as an inspectable proposal.",
    )

    changed_kardia = dict(DEFAULT_KARDIA)
    changed_kardia["aesthetic_valve"] = "Changed to trigger a proposal."
    mind.govern_identity_revision(user_id, make_reflection_payload(kardia=changed_kardia))

    proposals = mind.list_pending_proposals(user_id)
    mind.close()
    assert len(proposals) == 1
    proposal = proposals[0]

    # A provenance reference could reasonably live in the payload (matching
    # how delete_node stores raw_log_timestamp) under any plausible name.
    payload_keys = set(proposal["payload"].keys())
    provenance_like_keys = {
        k for k in payload_keys
        if any(term in k.lower() for term in ("log", "turn", "observation", "source", "trigger"))
    }

    assert provenance_like_keys, (
        f"identity_shift proposal payload has no field referencing an "
        f"originating observation/turn -- only {sorted(payload_keys)} present. "
        f"Compare to delete_node proposals, which carry raw_log_timestamp."
    )


# ---------------------------------------------------------------------
# 7. Resolving a proposal is a one-way, terminal action
# ---------------------------------------------------------------------

def test_resolved_proposal_is_terminal(tmp_path):
    db_path = str(tmp_path / "anaxi_test.db")
    user_id = "tester_terminal"

    mind = ConstitutionalMind(db_path)

    # accepted -> accepted again must not re-run the acceptance or move resolved_at
    node_id = upsert_test_node(mind, user_id, node_id="node_a", timestamp=1000)
    delete_payload = json.dumps({"upsert_nodes": [], "add_edges": [], "delete_nodes": [node_id]})
    mind.execute_sleep_consolidation(user_id, delete_payload, raw_log_timestamp=2000)
    proposal_id = mind.list_pending_proposals(user_id)[0]["id"]

    first = mind.resolve_proposal(user_id, proposal_id, accept=True)
    assert first["status"] == "success"
    mind.cursor.execute("SELECT status, resolved_at FROM proposals WHERE id = ?", (proposal_id,))
    status_1, resolved_at_1 = mind.cursor.fetchone()
    assert status_1 == "accepted"

    second = mind.resolve_proposal(user_id, proposal_id, accept=True)
    assert second["status"] != "success", "resolving an already-accepted proposal must not report success"
    mind.cursor.execute("SELECT status, resolved_at FROM proposals WHERE id = ?", (proposal_id,))
    status_2, resolved_at_2 = mind.cursor.fetchone()
    assert status_2 == status_1
    assert resolved_at_2 == resolved_at_1, "resolved_at moved on a no-op re-resolution"

    # rejected -> accepted must not reopen it and must not delete the node
    node_id_2 = upsert_test_node(mind, user_id, node_id="node_b", timestamp=3000)
    delete_payload_2 = json.dumps({"upsert_nodes": [], "add_edges": [], "delete_nodes": [node_id_2]})
    mind.execute_sleep_consolidation(user_id, delete_payload_2, raw_log_timestamp=4000)
    proposal_id_2 = next(
        p["id"] for p in mind.list_pending_proposals(user_id)
        if p["payload"].get("node_id") == node_id_2
    )

    mind.resolve_proposal(user_id, proposal_id_2, accept=False)
    reopen_attempt = mind.resolve_proposal(user_id, proposal_id_2, accept=True)
    assert reopen_attempt["status"] != "success", "a rejected proposal should not be reopenable by accepting it"

    mind.cursor.execute("SELECT 1 FROM nodes WHERE user_id = ? AND id = ?", (user_id, node_id_2))
    node_still_present = mind.cursor.fetchone()
    mind.close()
    assert node_still_present is not None, "node was deleted by an accept that should have been refused"
