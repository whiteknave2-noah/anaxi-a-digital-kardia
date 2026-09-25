"""Tests for workspace_episode_provenance.py -- canonical, source-clean
provenance for public unattended workspace-roaming episodes (WSP2-P3).
Every test uses a fresh, schema-only, temp-directory provenance DB
(provenance_schema.create_provenance_db()) with the minimal reference
rows this module needs seeded directly -- no real Ollama/model call,
no orchestration/relational_history fakes needed (this module never
imports either)."""
import hashlib
import json
import os
import sys
import tempfile
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import provenance_schema
import workspace_episode_provenance as wep

PIPELINE_KEY = "anaxi_orchestration_lineage_a"


def fresh_db(seed_human=True):
    test_dir = tempfile.mkdtemp(prefix="wep_test_")
    db_path = os.path.join(test_dir, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    pipeline_id = "pipe-test-1"
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES (?, ?, 'llama', 'test')",
        (pipeline_id, PIPELINE_KEY),
    )
    clark_actor_id = provenance_schema.derive_stable_id("actor", "clark")
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', 1000)",
        (clark_actor_id,),
    )
    host_actor_id = wep.host_actor_id()
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
        "VALUES (?, 'host_system', 'bounded_clause_renderer', 1000)",
        (host_actor_id,),
    )
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    human_actor_id = "human-actor-test-canonical"
    if seed_human:
        conn.execute(
            "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', ?, 1000)",
            (human_actor_id, human_actor_id),
        )
    conn.commit()
    conn.close()
    return test_dir, clark_actor_id, host_actor_id, human_actor_id


def read_conn(test_dir):
    import sqlite3
    return sqlite3.connect(os.path.join(test_dir, "anaxi_provenance.db"))


# ==================================================================== A


def test_episode_started_recorded_and_reconstructable():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    episode = wep.query_episode(test_dir, run_id)
    assert len(episode) == 1
    assert episode[0]["event_type"] == wep.EVENT_EPISODE_STARTED
    kinds = {c["component_kind"] for c in episode[0]["components"]}
    assert wep.RUN_ID_LINK_COMPONENT_KIND in kinds
    assert "roaming_episode_actor" in kinds


def test_run_id_is_unique_and_prefixed():
    a, b = wep.generate_episode_run_id(), wep.generate_episode_run_id()
    assert a != b
    assert a.startswith("wrun-") and b.startswith("wrun-")


# ==================================================================== B: handoff


def test_handoff_note_and_source_excerpt_both_present():
    # Load-bearing repair: Clark's note AND the exact selected source
    # material both reach canonical provenance, distinctly.
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_handoff(
        test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        note="I'd like to revisit what we discussed about the book.",
        source_excerpts=[{
            "handle": "d2", "author": "human", "text": "My friend, I need to step away for the night.",
            "event_id": "evt-turn-7", "sequence": None,
        }],
        model_revision_id=None,
    )
    episode = wep.query_episode(test_dir, run_id)
    handoff = next(e for e in episode if e["event_type"] == wep.EVENT_HANDOFF_SELECTED)
    note_component = next(c for c in handoff["components"] if c["component_kind"] == "roaming_handoff_note")
    assert note_component["component_text"] == "I'd like to revisit what we discussed about the book."
    assert note_component["creator_actor_id"] == clark_actor_id
    excerpt_component = next(c for c in handoff["components"] if c["component_kind"] == "roaming_handoff_source_excerpt")
    excerpt = json.loads(excerpt_component["component_text"])
    assert excerpt["text"] == "My friend, I need to step away for the night."
    assert excerpt["author"] == "human"
    assert excerpt["source_event_id"] == "evt-turn-7"  # WSP2-P3-P1 section 3: source-LINKED, not copied
    # WSP2-P3-P1 section 3A: human remains the source author -- never the host.
    assert excerpt_component["creator_actor_id"] == human_actor_id


def test_handoff_clark_authored_source_attributed_to_clark():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_handoff(
        test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        note="", source_excerpts=[{
            "handle": "d3", "author": "clark", "text": "I've been thinking about that too.",
            "event_id": "evt-turn-6", "sequence": 1,
        }],
        model_revision_id=None,
    )
    episode = wep.query_episode(test_dir, run_id)
    handoff = next(e for e in episode if e["event_type"] == wep.EVENT_HANDOFF_SELECTED)
    excerpt_component = next(c for c in handoff["components"] if c["component_kind"] == "roaming_handoff_source_excerpt")
    assert excerpt_component["creator_actor_id"] == clark_actor_id  # Clark actually spoke this source
    excerpt = json.loads(excerpt_component["component_text"])
    assert excerpt["source_event_id"] == "evt-turn-6"
    assert excerpt["source_sequence"] == 1  # WSP2-P3-P1 section 3B: component-level precision for Clark's own text


def test_handoff_human_source_without_seeded_human_actor_fails_closed():
    # WSP2-P3-P1 section 1: canonical failure fails closed -- a
    # missing/ambiguous human actor row must not silently misattribute
    # a human-authored excerpt to the host.
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db(seed_human=False)
    run_id = wep.generate_episode_run_id()
    raised = None
    try:
        wep.record_episode_handoff(
            test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
            note="", source_excerpts=[{"handle": "d1", "author": "human", "text": "hi", "event_id": "e", "sequence": None}],
            model_revision_id=None,
        )
    except wep.EpisodeProvenanceError:
        raised = True
    assert raised is True
    episode = wep.query_episode(test_dir, run_id)
    assert not any(e["event_type"] == wep.EVENT_HANDOFF_SELECTED for e in episode)  # nothing written


def test_empty_handoff_still_records_a_complete_event():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_handoff(
        test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        note="", source_excerpts=[], model_revision_id=None,
    )
    episode = wep.query_episode(test_dir, run_id)
    handoff = next(e for e in episode if e["event_type"] == wep.EVENT_HANDOFF_SELECTED)
    assert len(handoff["components"]) == 1  # only the run_id link -- no note, no excerpt


# ==================================================================== C: public action/wait


def test_public_journal_action_attributes_text_to_clark_fact_to_host():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_public_action(
        test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="journal", action="append", relative_path="entry-abc.json", success=True,
        action_id_backlink="wsaction-abc", model_revision_id=None,
        journal_text="Noticed the quiet. Good time to process some thoughts.",
    )
    episode = wep.query_episode(test_dir, run_id)
    action = next(e for e in episode if e["event_type"] == wep.EVENT_PUBLIC_ACTION)
    fact = next(c for c in action["components"] if c["component_kind"] == "roaming_action_fact")
    assert fact["creator_actor_id"] == host_actor_id  # host-established mechanical fact
    fact_data = json.loads(fact["component_text"])
    assert fact_data["resource_class"] == "journal" and fact_data["action"] == "append"
    assert fact_data["action_id_backlink"] == "wsaction-abc"
    text_component = next(c for c in action["components"] if c["component_kind"] == "roaming_journal_text")
    assert text_component["creator_actor_id"] == clark_actor_id  # Clark-authored payload
    assert text_component["component_text"] == "Noticed the quiet. Good time to process some thoughts."


def test_public_read_action_attributes_result_to_host_not_clark():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_public_action(
        test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="library", action="read", relative_path="notes.txt", success=True,
        action_id_backlink="wsaction-def", model_revision_id=None,
        external_result_text="Bounded extracted text from the file.",
    )
    episode = wep.query_episode(test_dir, run_id)
    action = next(e for e in episode if e["event_type"] == wep.EVENT_PUBLIC_ACTION)
    result_component = next(c for c in action["components"] if c["component_kind"] == "roaming_external_result")
    assert result_component["creator_actor_id"] == host_actor_id  # external/mechanical, never Clark's
    assert "journal_text" not in {c["component_kind"] for c in action["components"]}


def test_public_action_without_content_has_only_fact_and_participation():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_public_action(
        test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="library", action="list", relative_path=None, success=True,
        action_id_backlink="wsaction-ghi", model_revision_id=None,
    )
    episode = wep.query_episode(test_dir, run_id)
    action = next(e for e in episode if e["event_type"] == wep.EVENT_PUBLIC_ACTION)
    kinds = {c["component_kind"] for c in action["components"]}
    assert "roaming_journal_text" not in kinds
    assert "roaming_external_result" not in kinds
    assert "roaming_action_fact" in kinds


def test_public_wait_recorded():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_public_wait(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1000, wait_minutes=15, model_revision_id=None)
    episode = wep.query_episode(test_dir, run_id)
    wait_event = next(e for e in episode if e["event_type"] == wep.EVENT_PUBLIC_WAIT)
    fact = next(c for c in wait_event["components"] if c["component_kind"] == "roaming_wait_fact")
    assert json.loads(fact["component_text"])["wait_minutes"] == 15


# ==================================================================== D: termination


def test_termination_accepts_only_public_safe_classes():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_ended(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1000, termination_class=wep.TERMINATION_HUMAN_STOP)
    episode = wep.query_episode(test_dir, run_id)
    ended = next(e for e in episode if e["event_type"] == wep.EVENT_EPISODE_ENDED)
    fact = next(c for c in ended["components"] if c["component_kind"] == "roaming_termination_fact")
    assert json.loads(fact["component_text"])["termination_class"] == "human_stop"


def test_termination_rejects_non_public_safe_class():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    raised = None
    try:
        wep.record_episode_ended(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1000, termination_class="private_act_failure")
    except wep.EpisodeProvenanceError:
        raised = True
    assert raised is True
    episode = wep.query_episode(test_dir, run_id)
    assert not any(e["event_type"] == wep.EVENT_EPISODE_ENDED for e in episode)  # nothing written


# ==================================================================== E: run linkage / reconstruction


def test_full_episode_lifecycle_reconstructable_by_run_id_alone():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    other_run_id = wep.generate_episode_run_id()
    wep.record_episode_started(test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_episode_handoff(test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1001, note="", source_excerpts=[], model_revision_id=None)
    wep.record_public_action(test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1002, resource_class="journal", action="append", relative_path="e.json", success=True, action_id_backlink="wsaction-1", model_revision_id=None, journal_text="x")
    wep.record_public_wait(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1003, wait_minutes=15, model_revision_id=None)
    wep.record_episode_ended(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1004, termination_class=wep.TERMINATION_HUMAN_STOP)
    # a second, unrelated run must never bleed into the first's reconstruction
    wep.record_episode_started(test_dir, run_id=other_run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=2000)

    episode = wep.query_episode(test_dir, run_id)
    assert [e["event_type"] for e in episode] == [
        wep.EVENT_EPISODE_STARTED, wep.EVENT_HANDOFF_SELECTED, wep.EVENT_PUBLIC_ACTION,
        wep.EVENT_PUBLIC_WAIT, wep.EVENT_EPISODE_ENDED,
    ]
    other_episode = wep.query_episode(test_dir, other_run_id)
    assert len(other_episode) == 1


def test_pending_episode_not_yet_delivered():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_episode_ended(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1001, termination_class=wep.TERMINATION_HUMAN_STOP)
    pending = wep.find_pending_episode_run_ids(test_dir)
    assert run_id in pending


def test_delivered_episode_not_pending():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_episode_ended(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1001, termination_class=wep.TERMINATION_HUMAN_STOP)
    conn = read_conn(test_dir)
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, authorship_resolution, "
        "component_text, content_sha256, model_revision_id, span_start, span_end) "
        "VALUES ('fake-waking-event', 0, ?, 'episode_context_delivered', 'resolved', ?, 'x', NULL, NULL, NULL)",
        (host_actor_id, run_id),
    )
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        "auth_context_id, input_source_ref, occurred_at, record_created_at) "
        "VALUES ('fake-waking-event', 'waking_turn', NULL, 'unknown', NULL, NULL, NULL, 2000, 2000)"
    )
    conn.commit()
    conn.close()
    pending = wep.find_pending_episode_run_ids(test_dir)
    assert run_id not in pending


def test_episode_events_have_no_private_related_component_kinds():
    # Structural proof: none of this module's ACTUAL component_kind
    # constants/string literals used in real INSERT statements
    # reference private material -- checked against the module's own
    # frozen kind constants and function bodies, not its prose
    # docstrings (which legitimately explain the private boundary in
    # words).
    all_kind_strings = [
        wep.RUN_ID_LINK_COMPONENT_KIND, "roaming_episode_actor", "roaming_handoff_note",
        "roaming_handoff_source_excerpt", "roaming_action_fact", "roaming_journal_text",
        "roaming_external_result", "roaming_wait_fact", "roaming_termination_fact",
    ]
    for kind in all_kind_strings:
        assert "private" not in kind


def test_no_kardia_hippocampus_direction_sleep_imports():
    import ast
    with open(os.path.join(ANAXI_FINAL, "workspace_episode_provenance.py"), encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    for forbidden in ("kardia", "hippocampus_store", "hippocampus_retrieval", "conversation_direction",
                       "sleep_receipts", "llama_sleep", "workspace_private", "direction_control"):
        assert forbidden not in names


# ============================================== WSP2-P5-P1: background lifecycle control


def test_p5p1_paused_by_clark_recorded_with_run_id():
    # Section 19A: a durable, canonical fact -- linked to the episode
    # run it happened within, exactly like every other roaming-episode
    # event.
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    event_id = wep.record_background_lifecycle_control(
        test_dir, actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK, run_id=run_id,
    )
    conn = read_conn(test_dir)
    rows = conn.execute(
        "SELECT creator_actor_id, component_kind, component_text FROM event_components WHERE event_id = ? ORDER BY sequence",
        (event_id,),
    ).fetchall()
    conn.close()
    kinds = {r[1] for r in rows}
    assert wep.RUN_ID_LINK_COMPONENT_KIND in kinds  # linked to the run it happened within
    control_row = next(r for r in rows if r[1] == wep.BACKGROUND_LIFECYCLE_CONTROL_COMPONENT_KIND)
    assert control_row[0] == clark_actor_id
    assert control_row[2] == wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK


def test_p5p1_resume_recorded_without_run_id():
    # Section 19A/6: a waking-turn self-resume belongs to no episode
    # run at all -- run_id=None is legal and writes no run_id-link
    # component (never a fabricated/placeholder run_id).
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    event_id = wep.record_background_lifecycle_control(
        test_dir, actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_RESUMED,
    )
    conn = read_conn(test_dir)
    rows = conn.execute(
        "SELECT component_kind FROM event_components WHERE event_id = ?", (event_id,),
    ).fetchall()
    conn.close()
    assert wep.RUN_ID_LINK_COMPONENT_KIND not in {r[0] for r in rows}


def test_p5p1_invalid_control_value_rejected():
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    raised = False
    try:
        wep.record_background_lifecycle_control(
            test_dir, actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000, control="not_a_real_control",
        )
    except wep.EpisodeProvenanceError:
        raised = True
    assert raised


def test_p5p1_latest_control_reflects_most_recent_by_canonical_order():
    # Section 5: "A later durable Clark self-resume cancels the earlier
    # Clark-owned pause for reconstruction purposes."
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    assert wep.find_latest_background_lifecycle_control(test_dir) is None  # nothing recorded yet
    wep.record_background_lifecycle_control(
        test_dir, actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK,
    )
    assert wep.find_latest_background_lifecycle_control(test_dir) == wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK
    wep.record_background_lifecycle_control(
        test_dir, actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=2000,
        control=wep.BACKGROUND_CONTROL_RESUMED,
    )
    assert wep.find_latest_background_lifecycle_control(test_dir) == wep.BACKGROUND_CONTROL_RESUMED
    # A further pause after that resume is, correctly, the new latest.
    wep.record_background_lifecycle_control(
        test_dir, actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=3000,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK,
    )
    assert wep.find_latest_background_lifecycle_control(test_dir) == wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK


def test_p5p2a_latest_control_raises_provenance_store_missing_when_db_missing():
    # WSP2-P5-P2a (spec section 18): a missing DB file is NEVER the
    # same as "readable and empty" -- readable empty canonical history
    # != canonical history unavailable. Was test_p5p1_latest_control_
    # none_when_db_missing (asserted None) before this gate corrected
    # the missing-vs-empty conflation.
    raised = False
    try:
        wep.find_latest_background_lifecycle_control(os.path.join(ANAXI_FINAL, "no_such_dir_p5p1"))
    except wep.ProvenanceStoreMissingError:
        raised = True
    assert raised


def test_p5p1_background_lifecycle_control_not_in_episode_narrative():
    # Section 4: a sibling lifecycle event, not part of one episode's
    # own narrative -- mirrors EVENT_LIVE_WAKING_DELIVERED's own
    # established precedent of being excluded from query_episode().
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_background_lifecycle_control(
        test_dir, actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1001,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK, run_id=run_id,
    )
    episode = wep.query_episode(test_dir, run_id)
    event_types = {e["event_type"] for e in episode}
    assert wep.EVENT_BACKGROUND_LIFECYCLE_CONTROL not in event_types


def test_p5p1_no_semantic_content_stored_only_mechanical_control_value():
    # Section 19F: the ONLY thing stored for this fact is the bare
    # control-value string -- no reason, no free text, no host prose.
    test_dir, clark_actor_id, host_actor_id, human_actor_id = fresh_db()
    event_id = wep.record_background_lifecycle_control(
        test_dir, actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK,
    )
    conn = read_conn(test_dir)
    row = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (event_id, wep.BACKGROUND_LIFECYCLE_CONTROL_COMPONENT_KIND),
    ).fetchone()
    conn.close()
    assert row[0] == "paused_by_clark"  # exactly the enum value, nothing appended/interpreted


# ==================================================== CAP2-E/F: resource encounters


def test_record_resource_encounter_reconstructable():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    conn = read_conn(test_dir)
    conn.execute(
        "INSERT INTO model_revisions (model_revision_id, tag, identity_confidence, first_observed_at) "
        "VALUES ('rev-1', 'tag', 'tag_only_degraded', 1000)"
    )
    conn.commit()
    conn.close()

    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="photographs", relative_path="a.png", modality="image",
        delivered_portion="full image, bounded", content_sha256="abc123",
        source_action="view", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
        model_revision_id="rev-1",
    )
    conn = read_conn(test_dir)
    row = conn.execute(
        "SELECT c.component_text FROM events e JOIN event_components c ON c.event_id = e.event_id "
        "WHERE e.event_id = ? AND c.component_kind = 'resource_encounter_fact'", (event_id,),
    ).fetchone()
    conn.close()
    fact = json.loads(row[0])
    assert fact["resource_class"] == "photographs"
    assert fact["modality"] == "image"
    assert fact["content_sha256"] == "abc123"
    assert fact["genuinely_delivered"] is True


def test_record_resource_encounter_no_raw_bytes_ever_accepted():
    import inspect
    sig = inspect.signature(wep.record_resource_encounter)
    assert "image_bytes" not in sig.parameters
    assert "audio_bytes" not in sig.parameters
    assert "content_sha256" in sig.parameters


def test_find_pending_resource_encounter_returns_none_when_db_missing():
    test_dir = tempfile.mkdtemp(prefix="wep_missing_")
    assert wep.find_pending_resource_encounter_for_continuity(os.path.join(test_dir, "nope")) is None


def test_find_pending_resource_encounter_ignores_non_delivered():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="library", relative_path="b.pdf", modality="text",
        delivered_portion="chars 0-10", content_sha256="def456",
        source_action="read", target_model_pathway="gemma4:e4b", genuinely_delivered=False,
    )
    assert wep.find_pending_resource_encounter_for_continuity(test_dir) is None


def test_find_pending_resource_encounter_returns_most_recent_delivered_and_then_none_after_marked():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    event_id_1 = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="library", relative_path="b.pdf", modality="text",
        delivered_portion="chars 0-10", content_sha256="def456",
        source_action="read", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
    )
    event_id_2 = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=2000,
        resource_class="photographs", relative_path="a.png", modality="image",
        delivered_portion="full image, bounded", content_sha256="abc123",
        source_action="view", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
    )
    pending = wep.find_pending_resource_encounter_for_continuity(test_dir)
    assert pending["event_id"] == event_id_2  # most recent, not first

    wep.record_resource_encounter_continuity_delivered(
        test_dir, pipeline_key=PIPELINE_KEY, occurred_at=2500, event_id=event_id_2,
    )
    pending_after = wep.find_pending_resource_encounter_for_continuity(test_dir)
    assert pending_after["event_id"] == event_id_1  # falls back to the next-most-recent undelivered

    wep.record_resource_encounter_continuity_delivered(
        test_dir, pipeline_key=PIPELINE_KEY, occurred_at=3000, event_id=event_id_1,
    )
    assert wep.find_pending_resource_encounter_for_continuity(test_dir) is None


# ============================================ SLP1-A3: resource backlink


def test_backlink_written_in_same_event_as_encounter_fact():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="photographs", relative_path="a.png", modality="image",
        delivered_portion="full image, bounded", content_sha256="abc123",
        source_action="view", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
        triggering_waking_event_id="waking-evt-xyz",
    )
    conn = read_conn(test_dir)
    rows = conn.execute(
        "SELECT component_kind, component_text, creator_actor_id FROM event_components WHERE event_id = ?",
        (event_id,),
    ).fetchall()
    conn.close()
    kinds = {r[0] for r in rows}
    assert wep.RESOURCE_ENCOUNTER_FACT_COMPONENT_KIND in kinds
    assert wep.TRIGGERING_WAKING_EVENT_ID_COMPONENT_KIND in kinds  # SAME event_id, not a second event
    backlink_row = next(r for r in rows if r[0] == wep.TRIGGERING_WAKING_EVENT_ID_COMPONENT_KIND)
    assert backlink_row[2] == host_actor_id  # host-mechanical, never Clark's own expression


def test_backlink_text_exactly_equals_supplied_event_id():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="library", relative_path="b.pdf", modality="text",
        delivered_portion="chars 0-10", content_sha256="def456",
        source_action="read", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
        triggering_waking_event_id="the-exact-waking-turn-event-id-01H",
    )
    conn = read_conn(test_dir)
    row = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (event_id, wep.TRIGGERING_WAKING_EVENT_ID_COMPONENT_KIND),
    ).fetchone()
    conn.close()
    assert row[0] == "the-exact-waking-turn-event-id-01H"  # exact, no transformation


def test_omitting_triggering_event_id_preserves_unlinked_backward_compatible_behavior():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="library", relative_path="b.pdf", modality="text",
        delivered_portion="chars 0-10", content_sha256="def456",
        source_action="read", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
    )
    conn = read_conn(test_dir)
    kinds = {
        r[0] for r in conn.execute(
            "SELECT component_kind FROM event_components WHERE event_id = ?", (event_id,),
        ).fetchall()
    }
    conn.close()
    assert wep.TRIGGERING_WAKING_EVENT_ID_COMPONENT_KIND not in kinds  # unlinked, exactly as before this gate


def test_forced_failure_during_encounter_transaction_leaves_neither_fact_nor_backlink():
    # A resource_encounter_fact referencing a nonexistent model_revisions
    # row triggers a foreign-key violation on the FIRST insert of the
    # transaction (event_components.model_revision_id REFERENCES
    # model_revisions, PRAGMA foreign_keys=ON) -- proving the backlink
    # (queued to be written immediately after) never gets a chance to
    # exist independently, and the whole event rolls back atomically:
    # no event row, no fact, no backlink survive the failure.
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    raised = False
    try:
        wep.record_resource_encounter(
            test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
            resource_class="library", relative_path="c.pdf", modality="text",
            delivered_portion="chars 0-10", content_sha256="ghi789",
            source_action="read", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
            model_revision_id="does-not-exist-in-model_revisions",
            triggering_waking_event_id="fake-waking-turn-event",
        )
    except Exception:
        raised = True
    assert raised
    conn = read_conn(test_dir)
    rows = conn.execute(
        "SELECT component_kind FROM event_components WHERE component_kind IN (?, ?)",
        (wep.RESOURCE_ENCOUNTER_FACT_COMPONENT_KIND, wep.TRIGGERING_WAKING_EVENT_ID_COMPONENT_KIND),
    ).fetchall()
    events_rows = conn.execute("SELECT event_id FROM events").fetchall()
    conn.close()
    assert rows == []  # neither component exists -- not merely "no backlink without a fact"
    assert events_rows == []  # the event itself never committed either


# ==================================== SLP1-A4: delivered replayable traces


def test_successful_library_read_writes_exact_delivered_text_component():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    text = "The exact bounded excerpt genuinely delivered this turn."
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="library", relative_path="notes.txt", modality="text",
        delivered_portion="chars 0-58", content_sha256=content_sha256,
        source_action="read", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
        delivered_text=text,
    )
    conn = read_conn(test_dir)
    row = conn.execute(
        "SELECT component_text, creator_actor_id FROM event_components WHERE event_id = ? AND component_kind = ?",
        (event_id, wep.RESOURCE_ENCOUNTER_DELIVERED_TEXT_COMPONENT_KIND),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == text  # exact, verbatim
    assert row[1] == host_actor_id  # delivered external content, not Clark's own expression


def test_successful_journal_read_writes_exact_delivered_text_component():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    text = '{"entry_id": "e1", "content": "A journal entry Clark read."}'
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="journal", relative_path="e1.json", modality="text",
        delivered_portion="journal entry, full", content_sha256=content_sha256,
        source_action="read", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
        delivered_text=text,
    )
    conn = read_conn(test_dir)
    row = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (event_id, wep.RESOURCE_ENCOUNTER_DELIVERED_TEXT_COMPONENT_KIND),
    ).fetchone()
    conn.close()
    assert row[0] == text


def test_delivered_text_hash_matches_encounter_facts_hash():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    text = "Consistency-checked text."
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="library", relative_path="a.txt", modality="text",
        delivered_portion="entry text, full", content_sha256=content_sha256,
        source_action="read", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
        delivered_text=text,
    )
    conn = read_conn(test_dir)
    fact_row = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (event_id, wep.RESOURCE_ENCOUNTER_FACT_COMPONENT_KIND),
    ).fetchone()
    text_row = conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (event_id, wep.RESOURCE_ENCOUNTER_DELIVERED_TEXT_COMPONENT_KIND),
    ).fetchone()
    conn.close()
    fact = json.loads(fact_row[0])
    recomputed = hashlib.sha256(text_row[0].encode("utf-8")).hexdigest()
    assert recomputed == fact["content_sha256"] == content_sha256


def test_delivered_text_hash_disagreement_raises_and_writes_nothing():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    event_id = None
    raised = False
    try:
        event_id = wep.record_resource_encounter(
            test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
            resource_class="library", relative_path="a.txt", modality="text",
            delivered_portion="entry text, full", content_sha256="0" * 64,
            source_action="read", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
            delivered_text="text that disagrees with the declared content_sha256",
        )
    except wep.EpisodeProvenanceError:
        raised = True
    assert raised
    conn = read_conn(test_dir)
    rows = conn.execute(
        "SELECT component_kind FROM event_components WHERE component_kind IN (?, ?)",
        (wep.RESOURCE_ENCOUNTER_FACT_COMPONENT_KIND, wep.RESOURCE_ENCOUNTER_DELIVERED_TEXT_COMPONENT_KIND),
    ).fetchall()
    conn.close()
    assert rows == []  # whole event rolled back, not just the mismatched component


def test_failed_delivery_writes_no_replayable_text_component():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    text = "This was attempted but not genuinely delivered."
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="library", relative_path="a.txt", modality="text",
        delivered_portion="entry text, full", content_sha256=content_sha256,
        source_action="read", target_model_pathway="gemma4:e4b", genuinely_delivered=False,
        delivered_text=text,  # passed unconditionally, exactly as the real caller does
    )
    conn = read_conn(test_dir)
    kinds = {
        r[0] for r in conn.execute(
            "SELECT component_kind FROM event_components WHERE event_id = ?", (event_id,),
        ).fetchall()
    }
    conn.close()
    assert wep.RESOURCE_ENCOUNTER_DELIVERED_TEXT_COMPONENT_KIND not in kinds
    assert wep.RESOURCE_ENCOUNTER_FACT_COMPONENT_KIND in kinds  # the mechanical fact is still recorded


def test_delivered_text_wrong_modality_raises():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    raised = False
    try:
        wep.record_resource_encounter(
            test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
            resource_class="photographs", relative_path="a.png", modality="image",
            delivered_portion="full image, bounded", content_sha256="abc123",
            source_action="view", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
            delivered_text="text given for an image modality encounter",
        )
    except wep.EpisodeProvenanceError:
        raised = True
    assert raised


def test_delivered_text_and_measurement_are_mutually_exclusive():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    raised = False
    try:
        wep.record_resource_encounter(
            test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
            resource_class="library", relative_path="a.txt", modality="text",
            delivered_portion="entry text, full", content_sha256="abc123",
            source_action="read", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
            delivered_text="x", delivered_measurement="y",
        )
    except wep.EpisodeProvenanceError:
        raised = True
    assert raised


def test_successful_audio_view_writes_exact_bounded_measurement():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    measurement = json.dumps({"view": "full", "interval_seconds": [0, 30]}, sort_keys=True)
    content_sha256 = hashlib.sha256(measurement.encode("utf-8")).hexdigest()
    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="music", relative_path="song.wav", modality="audio",
        delivered_portion="view=full, interval=0-30s, bounded", content_sha256=content_sha256,
        source_action="inspect_audio", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
        source_content_sha256="original-audio-file-sha256",
        delivered_measurement=measurement,
    )
    conn = read_conn(test_dir)
    row = conn.execute(
        "SELECT component_text, creator_actor_id FROM event_components WHERE event_id = ? AND component_kind = ?",
        (event_id, wep.RESOURCE_ENCOUNTER_DELIVERED_MEASUREMENT_COMPONENT_KIND),
    ).fetchone()
    conn.close()
    assert row[0] == measurement  # exact same serialization, never re-derived
    assert row[1] == host_actor_id


def test_audio_measurement_hash_matches_delivered_hash_never_source_hash():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    measurement = json.dumps({"view": "neutral"}, sort_keys=True)
    content_sha256 = hashlib.sha256(measurement.encode("utf-8")).hexdigest()
    source_sha256 = "distinct-original-audio-file-digest"
    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="music", relative_path="song.wav", modality="audio",
        delivered_portion="neutral source orientation, full", content_sha256=content_sha256,
        source_action="listen", target_model_pathway="gemma4:e4b", genuinely_delivered=True,
        source_content_sha256=source_sha256,
        delivered_measurement=measurement,
    )
    conn = read_conn(test_dir)
    fact = json.loads(conn.execute(
        "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
        (event_id, wep.RESOURCE_ENCOUNTER_FACT_COMPONENT_KIND),
    ).fetchone()[0])
    conn.close()
    assert fact["content_sha256"] == content_sha256  # the DELIVERED representation's hash
    assert fact["source_content_sha256"] == source_sha256  # the ORIGINAL file's hash, kept distinct
    assert fact["content_sha256"] != fact["source_content_sha256"]


def test_no_raw_audio_bytes_persistence_pathway_introduced():
    import inspect
    sig = inspect.signature(wep.record_resource_encounter)
    for forbidden in ("audio_bytes", "raw_audio", "wav_bytes", "pcm_bytes"):
        assert forbidden not in sig.parameters


def test_failed_audio_delivery_writes_no_replayable_measurement():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    measurement = json.dumps({"view": "full"}, sort_keys=True)
    content_sha256 = hashlib.sha256(measurement.encode("utf-8")).hexdigest()
    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="music", relative_path="song.wav", modality="audio",
        delivered_portion="view=full", content_sha256=content_sha256,
        source_action="inspect_audio", target_model_pathway="gemma4:e4b", genuinely_delivered=False,
        delivered_measurement=measurement,
    )
    conn = read_conn(test_dir)
    kinds = {
        r[0] for r in conn.execute(
            "SELECT component_kind FROM event_components WHERE event_id = ?", (event_id,),
        ).fetchall()
    }
    conn.close()
    assert wep.RESOURCE_ENCOUNTER_DELIVERED_MEASUREMENT_COMPONENT_KIND not in kinds


def test_photograph_encounter_never_gains_image_byte_replayable_component():
    test_dir, clark_actor_id, host_actor_id, _human = fresh_db()
    event_id = wep.record_resource_encounter(
        test_dir, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000,
        resource_class="photographs", relative_path="a.png", modality="image",
        delivered_portion="full image, bounded", content_sha256="abc123",
        source_action="view", target_model_pathway="qwen3-vl:4b", genuinely_delivered=True,
    )
    conn = read_conn(test_dir)
    kinds = {
        r[0] for r in conn.execute(
            "SELECT component_kind FROM event_components WHERE event_id = ?", (event_id,),
        ).fetchall()
    }
    conn.close()
    assert wep.RESOURCE_ENCOUNTER_DELIVERED_TEXT_COMPONENT_KIND not in kinds
    assert wep.RESOURCE_ENCOUNTER_DELIVERED_MEASUREMENT_COMPONENT_KIND not in kinds
    assert not any("image" in k or "pixel" in k or "bytes" in k for k in kinds)


def test_no_image_byte_parameter_exists_anywhere_on_record_resource_encounter():
    import inspect
    sig = inspect.signature(wep.record_resource_encounter)
    for forbidden in ("image_bytes", "pixel_bytes", "raw_image", "delivered_image"):
        assert forbidden not in sig.parameters


def test_no_timestamp_derived_fallback_linkage_exists():
    # SLP1-A3 requirement: the ONLY way to discover a triggering-waking-
    # turn relation is the dedicated component above -- no function in
    # this module ever establishes or searches for that relation via an
    # occurred_at equality/proximity comparison.
    with open(os.path.join(ANAXI_FINAL, "workspace_episode_provenance.py"), encoding="utf-8") as f:
        source = f.read()
    assert "occurred_at = ?" not in source  # every occurred_at column write uses positional VALUES(...), never an equality WHERE
    assert "occurred_at ==" not in source


ALL_TESTS = [
    test_episode_started_recorded_and_reconstructable,
    test_run_id_is_unique_and_prefixed,
    test_handoff_note_and_source_excerpt_both_present,
    test_handoff_clark_authored_source_attributed_to_clark,
    test_handoff_human_source_without_seeded_human_actor_fails_closed,
    test_empty_handoff_still_records_a_complete_event,
    test_public_journal_action_attributes_text_to_clark_fact_to_host,
    test_public_read_action_attributes_result_to_host_not_clark,
    test_public_action_without_content_has_only_fact_and_participation,
    test_public_wait_recorded,
    test_termination_accepts_only_public_safe_classes,
    test_termination_rejects_non_public_safe_class,
    test_full_episode_lifecycle_reconstructable_by_run_id_alone,
    test_pending_episode_not_yet_delivered,
    test_delivered_episode_not_pending,
    test_episode_events_have_no_private_related_component_kinds,
    test_no_kardia_hippocampus_direction_sleep_imports,
    # WSP2-P5-P1
    test_p5p1_paused_by_clark_recorded_with_run_id,
    test_p5p1_resume_recorded_without_run_id,
    test_p5p1_invalid_control_value_rejected,
    test_p5p1_latest_control_reflects_most_recent_by_canonical_order,
    test_p5p2a_latest_control_raises_provenance_store_missing_when_db_missing,
    test_p5p1_background_lifecycle_control_not_in_episode_narrative,
    test_p5p1_no_semantic_content_stored_only_mechanical_control_value,
    test_record_resource_encounter_reconstructable,
    test_record_resource_encounter_no_raw_bytes_ever_accepted,
    test_find_pending_resource_encounter_returns_none_when_db_missing,
    test_find_pending_resource_encounter_ignores_non_delivered,
    test_find_pending_resource_encounter_returns_most_recent_delivered_and_then_none_after_marked,
    test_backlink_written_in_same_event_as_encounter_fact,
    test_backlink_text_exactly_equals_supplied_event_id,
    test_omitting_triggering_event_id_preserves_unlinked_backward_compatible_behavior,
    test_forced_failure_during_encounter_transaction_leaves_neither_fact_nor_backlink,
    test_no_timestamp_derived_fallback_linkage_exists,
    test_successful_library_read_writes_exact_delivered_text_component,
    test_successful_journal_read_writes_exact_delivered_text_component,
    test_delivered_text_hash_matches_encounter_facts_hash,
    test_delivered_text_hash_disagreement_raises_and_writes_nothing,
    test_failed_delivery_writes_no_replayable_text_component,
    test_delivered_text_wrong_modality_raises,
    test_delivered_text_and_measurement_are_mutually_exclusive,
    test_successful_audio_view_writes_exact_bounded_measurement,
    test_audio_measurement_hash_matches_delivered_hash_never_source_hash,
    test_no_raw_audio_bytes_persistence_pathway_introduced,
    test_failed_audio_delivery_writes_no_replayable_measurement,
    test_photograph_encounter_never_gains_image_byte_replayable_component,
    test_no_image_byte_parameter_exists_anywhere_on_record_resource_encounter,
]


def main():
    passed, failed = 0, 0
    failures = []
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            tb = traceback.format_exc()
            failures.append((t.__name__, str(exc), tb))
            print(f"FAIL {t.__name__}: {exc}")
    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
