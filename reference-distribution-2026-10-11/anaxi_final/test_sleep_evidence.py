"""SLP1-S1/SLP1-A3 acceptance tests for sleep_evidence.py. Every test
uses a temporary, synthetic canonical provenance DB built via the real
create_provenance_db() -- no test touches the real production
anaxi_provenance.db. No model call anywhere in this file.
"""
import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

from provenance_schema import create_provenance_db
import sleep_evidence as se

TEST_ROOT = tempfile.mkdtemp(prefix="slp1s1_test_")


# =============================================================== fixtures =


def _seed_actors(conn):
    now = int(time.time())
    host_actor_id, clark_actor_id, human_actor_id = "actor-host-1", "actor-clark-1", "actor-human-1"
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', ?)", (host_actor_id, now))
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', ?)", (clark_actor_id, now))
    conn.execute("INSERT INTO actor_clark_agent (actor_id, canonical_key) VALUES (?, 'clark')", (clark_actor_id,))
    conn.execute("INSERT INTO persons (person_id, created_at) VALUES ('person-alex', ?)", (now,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', 'alex', ?)", (human_actor_id, now))
    conn.execute("INSERT INTO actor_human_person (actor_id, person_id, canonical_name, relationship_established_at) VALUES (?, 'person-alex', 'Alex', ?)", (human_actor_id, now))
    return host_actor_id, clark_actor_id, human_actor_id


def _insert_event(conn, event_id, pipeline_id, auth_context_id, occurred_at, event_type="waking_turn"):
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        "auth_context_id, input_source_ref, occurred_at, record_created_at) "
        "VALUES (?, ?, ?, 'known', NULL, ?, ?, ?, ?)",
        (event_id, event_type, pipeline_id, auth_context_id, event_id, occurred_at, occurred_at),
    )


def _insert_component(conn, event_id, sequence, creator_actor_id, component_kind, authorship_resolution, text, model_revision_id=None):
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256, model_revision_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, sequence, creator_actor_id, component_kind, authorship_resolution, text, content_sha256, model_revision_id),
    )
    return conn.execute("SELECT component_id FROM event_components WHERE event_id = ? AND sequence = ?", (event_id, sequence)).fetchone()[0]


def build_fixture(name):
    """One self-consistent synthetic provenance DB covering every
    eligibility branch. Returns (conn, ids-dict, prov_path)."""
    tmp_dir = os.path.join(TEST_ROOT, name)
    os.makedirs(tmp_dir, exist_ok=True)
    prov_path = os.path.join(tmp_dir, "anaxi_provenance.db")
    conn = create_provenance_db(prov_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    pipeline_id = "pipe-llama-1"
    conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) VALUES (?, 'anaxi_orchestration_lineage_a', 'llama', 'test')", (pipeline_id,))
    host_id, clark_id, human_id = _seed_actors(conn)
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-1', ?, 100, 200)", (pipeline_id,))
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, authenticated_actor_id, auth_state, auth_method, assurance_level, established_at) VALUES ('ac-auth', 'sess-1', ?, ?, 'authenticated', 'os_session', 'medium', 100)", (human_id, human_id))
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, authenticated_actor_id, auth_state, auth_method, assurance_level, established_at) VALUES ('ac-claimed', 'sess-1', ?, NULL, 'claimed_only', NULL, NULL, 100)", (human_id,))
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, authenticated_actor_id, auth_state, auth_method, assurance_level, established_at) VALUES ('ac-unauth', 'sess-1', NULL, NULL, 'unauthenticated', 'none_presented', NULL, 100)")

    ids = {"pipeline_id": pipeline_id, "host_id": host_id, "clark_id": clark_id, "human_id": human_id}

    _insert_event(conn, "evt-cp", pipeline_id, "ac-auth", 100)
    ids["clark_expr_id"] = _insert_component(conn, "evt-cp", 0, clark_id, "conversational_prose", "resolved", "Clark's own words.")

    _insert_event(conn, "evt-human-auth", pipeline_id, "ac-auth", 200)
    ids["human_expr_authenticated_id"] = _insert_component(conn, "evt-human-auth", 0, human_id, "conversational_prose", "resolved", "Alex's authenticated words.")

    _insert_event(conn, "evt-human-claimed", pipeline_id, "ac-claimed", 300)
    ids["human_expr_claimed_only_id"] = _insert_component(conn, "evt-human-claimed", 0, human_id, "conversational_prose", "resolved", "Alex's claimed-only words.")

    _insert_event(conn, "evt-human-unauth", pipeline_id, "ac-unauth", 400)
    ids["human_expr_unauthenticated_id"] = _insert_component(conn, "evt-human-unauth", 0, human_id, "conversational_prose", "resolved", "Alex's unauthenticated words.")

    _insert_event(conn, "evt-human-noauth", pipeline_id, None, 500)
    ids["human_expr_no_auth_context_id"] = _insert_component(conn, "evt-human-noauth", 0, human_id, "conversational_prose", "resolved", "Alex's words, no auth context at all.")

    _insert_event(conn, "evt-bc", pipeline_id, "ac-auth", 600)
    ids["mechanical_record_id"] = _insert_component(conn, "evt-bc", 0, host_id, "bounded_clause", "resolved", "Saved: a mechanical note.")

    _insert_event(conn, "evt-mixed", pipeline_id, "ac-auth", 700)
    ids["mixed_historical_id"] = _insert_component(conn, "evt-mixed", 0, None, "assembled_reply_unsplit_historical", "unresolved_mixed_historical", "An old, unsplit historical reply.")

    _insert_event(conn, "evt-unknownkind", pipeline_id, "ac-auth", 800)
    ids["unknown_kind_id"] = _insert_component(conn, "evt-unknownkind", 0, clark_id, "raw_debug_note", "resolved", "Not a recognized component kind.")

    _insert_event(conn, "evt-protected", pipeline_id, "ac-auth", 850)
    ids["protected_decision_id"] = _insert_component(conn, "evt-protected", 0, clark_id, "protected_decision_expression", "resolved", "A protected decision, not ordinary conversation.")

    # SLP1-A3: genuine roaming_journal_text -- correct event_type, plus
    # its mandatory workspace_roaming_run_id sibling in the same event.
    _insert_event(conn, "evt-journal", pipeline_id, "ac-auth", 900, event_type="workspace_roaming_public_action")
    ids["roaming_journal_id"] = _insert_component(conn, "evt-journal", 1, clark_id, "roaming_journal_text", "resolved", "Clark's own journal entry.")
    _insert_component(conn, "evt-journal", 0, host_id, "workspace_roaming_run_id", "resolved", "wrun-fixture-1")

    # SLP1-A3: roaming_journal_text lacking its mandatory sibling -- must
    # be rejected, not merely by convention but structurally.
    _insert_event(conn, "evt-journal-nosibling", pipeline_id, "ac-auth", 950, event_type="workspace_roaming_public_action")
    ids["roaming_journal_no_sibling_id"] = _insert_component(conn, "evt-journal-nosibling", 0, clark_id, "roaming_journal_text", "resolved", "Journal entry missing its run-id sibling.")

    # SLP1-A3: same component_kind, wrong event_type for each approved tuple.
    _insert_event(conn, "evt-cp-wrong-type", pipeline_id, "ac-auth", 1000, event_type="workspace_roaming_public_action")
    ids["conversational_prose_wrong_event_type_id"] = _insert_component(conn, "evt-cp-wrong-type", 0, clark_id, "conversational_prose", "resolved", "Clark's words under the wrong event_type.")

    _insert_event(conn, "evt-journal-wrong-type", pipeline_id, "ac-auth", 1050, event_type="waking_turn")
    ids["roaming_journal_wrong_event_type_id"] = _insert_component(conn, "evt-journal-wrong-type", 1, clark_id, "roaming_journal_text", "resolved", "Journal-shaped text under the wrong event_type.")
    _insert_component(conn, "evt-journal-wrong-type", 0, host_id, "workspace_roaming_run_id", "resolved", "wrun-fixture-2")

    # SLP1-A4c: genuine, authenticated human_waking_input.
    _insert_event(conn, "evt-human-waking-auth", pipeline_id, "ac-auth", 1400, event_type="human_waking_input")
    ids["human_waking_input_authenticated_id"] = _insert_component(conn, "evt-human-waking-auth", 0, human_id, "human_conversational_input", "resolved", "Genuine authenticated human utterance.")

    _insert_event(conn, "evt-human-waking-unauth", pipeline_id, "ac-unauth", 1450, event_type="human_waking_input")
    ids["human_waking_input_unauthenticated_id"] = _insert_component(conn, "evt-human-waking-unauth", 0, human_id, "human_conversational_input", "resolved", "Unauthenticated human utterance.")

    _insert_event(conn, "evt-human-waking-wrong-type", pipeline_id, "ac-auth", 1500, event_type="waking_turn")
    ids["human_waking_input_wrong_event_type_id"] = _insert_component(conn, "evt-human-waking-wrong-type", 0, human_id, "human_conversational_input", "resolved", "Human-shaped text under the wrong event_type.")

    # Actor/auth mismatch: component creator is Clark, not a human,
    # even though the event_type/component_kind/auth_context are
    # otherwise correct -- must never be admitted as human expression.
    _insert_event(conn, "evt-human-waking-actor-mismatch", pipeline_id, "ac-auth", 1550, event_type="human_waking_input")
    ids["human_waking_input_actor_mismatch_id"] = _insert_component(conn, "evt-human-waking-actor-mismatch", 0, clark_id, "human_conversational_input", "resolved", "Clark-attributed text in a human_waking_input event.")

    # SLP1-A4: delivered-resource-trace fixtures -- one resource_encounter
    # event per case, event_type='workspace_resource_encounter'.
    def _resource_fact(genuinely_delivered, modality, content_sha256):
        return json.dumps({
            "resource_class": "library", "relative_path": "notes.txt", "modality": modality,
            "delivered_portion": "chars 0-10", "content_sha256": content_sha256,
            "source_action": "read", "target_model_pathway": "gemma4:e4b",
            "genuinely_delivered": genuinely_delivered,
        })

    good_text = "Delivered library text."
    good_text_hash = hashlib.sha256(good_text.encode("utf-8")).hexdigest()
    _insert_event(conn, "evt-encounter-good-text", pipeline_id, "ac-auth", 1100, event_type="workspace_resource_encounter")
    _insert_component(conn, "evt-encounter-good-text", 0, host_id, "resource_encounter_fact", "resolved", _resource_fact(True, "text", good_text_hash))
    ids["delivered_text_good_id"] = _insert_component(conn, "evt-encounter-good-text", 1, host_id, "resource_encounter_delivered_text", "resolved", good_text)

    good_measurement = json.dumps({"view": "full", "interval_seconds": [0, 30]}, sort_keys=True)
    good_measurement_hash = hashlib.sha256(good_measurement.encode("utf-8")).hexdigest()
    _insert_event(conn, "evt-encounter-good-audio", pipeline_id, "ac-auth", 1150, event_type="workspace_resource_encounter")
    _insert_component(conn, "evt-encounter-good-audio", 0, host_id, "resource_encounter_fact", "resolved", _resource_fact(True, "audio", good_measurement_hash))
    ids["delivered_measurement_good_id"] = _insert_component(conn, "evt-encounter-good-audio", 1, host_id, "resource_encounter_delivered_measurement", "resolved", good_measurement)

    _insert_event(conn, "evt-encounter-bad-hash", pipeline_id, "ac-auth", 1200, event_type="workspace_resource_encounter")
    _insert_component(conn, "evt-encounter-bad-hash", 0, host_id, "resource_encounter_fact", "resolved", _resource_fact(True, "text", "0" * 64))
    ids["delivered_text_bad_hash_id"] = _insert_component(conn, "evt-encounter-bad-hash", 1, host_id, "resource_encounter_delivered_text", "resolved", "Text whose hash disagrees with the fact.")

    _insert_event(conn, "evt-encounter-not-delivered", pipeline_id, "ac-auth", 1250, event_type="workspace_resource_encounter")
    not_delivered_text = "Attempted but not genuinely delivered."
    _insert_component(conn, "evt-encounter-not-delivered", 0, host_id, "resource_encounter_fact", "resolved", _resource_fact(False, "text", hashlib.sha256(not_delivered_text.encode("utf-8")).hexdigest()))
    ids["delivered_text_not_delivered_id"] = _insert_component(conn, "evt-encounter-not-delivered", 1, host_id, "resource_encounter_delivered_text", "resolved", not_delivered_text)

    _insert_event(conn, "evt-encounter-wrong-modality", pipeline_id, "ac-auth", 1300, event_type="workspace_resource_encounter")
    wrong_modality_text = "Text component but fact claims audio modality."
    _insert_component(conn, "evt-encounter-wrong-modality", 0, host_id, "resource_encounter_fact", "resolved", _resource_fact(True, "audio", hashlib.sha256(wrong_modality_text.encode("utf-8")).hexdigest()))
    ids["delivered_text_wrong_modality_id"] = _insert_component(conn, "evt-encounter-wrong-modality", 1, host_id, "resource_encounter_delivered_text", "resolved", wrong_modality_text)

    _insert_event(conn, "evt-encounter-orphan", pipeline_id, "ac-auth", 1350, event_type="workspace_resource_encounter")
    ids["delivered_text_orphan_id"] = _insert_component(conn, "evt-encounter-orphan", 0, host_id, "resource_encounter_delivered_text", "resolved", "No sibling resource_encounter_fact exists in this event.")

    ids["resource_encounter_fact_good_id"] = conn.execute(
        "SELECT component_id FROM event_components WHERE event_id = 'evt-encounter-good-text' AND sequence = 0"
    ).fetchone()[0]

    _insert_event(conn, "evt-triggering-link", pipeline_id, "ac-auth", 1400, event_type="workspace_resource_encounter")
    ids["triggering_waking_event_id_component_id"] = _insert_component(conn, "evt-triggering-link", 0, host_id, "triggering_waking_event_id", "resolved", "evt-cp")

    conn.commit()
    return conn, ids, prov_path


def close_conn(conn):
    conn.close()


# ============================================================ eligibility =


def test_resolved_clark_expression_eligible():
    conn, ids, _ = build_fixture("elig_clark")
    assert se.is_sleep_eligible(conn, ids["clark_expr_id"]) is True
    close_conn(conn)


def test_resolved_authenticated_human_expression_eligible():
    conn, ids, _ = build_fixture("elig_human_auth")
    assert se.is_sleep_eligible(conn, ids["human_expr_authenticated_id"]) is True
    close_conn(conn)


def test_claimed_only_human_expression_rejected():
    conn, ids, _ = build_fixture("rej_human_claimed")
    assert se.is_sleep_eligible(conn, ids["human_expr_claimed_only_id"]) is False
    close_conn(conn)


def test_unauthenticated_human_expression_rejected():
    conn, ids, _ = build_fixture("rej_human_unauth")
    assert se.is_sleep_eligible(conn, ids["human_expr_unauthenticated_id"]) is False
    close_conn(conn)


def test_human_expression_with_no_auth_context_rejected():
    conn, ids, _ = build_fixture("rej_human_noauth")
    assert se.is_sleep_eligible(conn, ids["human_expr_no_auth_context_id"]) is False
    close_conn(conn)


def test_mechanical_record_rejected():
    conn, ids, _ = build_fixture("rej_mechanical")
    assert se.is_sleep_eligible(conn, ids["mechanical_record_id"]) is False
    close_conn(conn)


def test_bounded_clause_never_in_selection_tuples():
    # SLP1-A3 correction 3: bounded_clause must not appear in the
    # selection-bearing allowlist AT ALL, for any actor_type/event_type
    # -- it is not merely rejected downstream by actor-type.
    for tup in se.ELIGIBLE_SELECTION_TUPLES:
        assert tup[1] != "bounded_clause", tup


def test_mixed_historical_expression_rejected():
    conn, ids, _ = build_fixture("rej_mixed")
    assert se.is_sleep_eligible(conn, ids["mixed_historical_id"]) is False
    close_conn(conn)


def test_unknown_component_kind_rejected():
    conn, ids, _ = build_fixture("rej_unknown_kind")
    assert se.is_sleep_eligible(conn, ids["unknown_kind_id"]) is False
    close_conn(conn)


def test_protected_decision_expression_rejected_despite_resolved_clark_actor():
    # The one production-observed case where a component is resolved
    # AND creator-resolves to clark_agent, yet must still be excluded
    # -- proves classification is not "any resolved Clark-attributed
    # component is eligible," it is "only an approved
    # (event_type, component_kind, actor_type) triple is eligible."
    conn, ids, _ = build_fixture("rej_protected")
    assert se.is_sleep_eligible(conn, ids["protected_decision_id"]) is False
    close_conn(conn)


def test_nonexistent_component_id_rejected():
    conn, ids, _ = build_fixture("rej_nonexistent")
    assert se.is_sleep_eligible(conn, 999999) is False
    close_conn(conn)


def test_classifier_never_produces_derived_inference():
    # event_components has no memory_kind column and no representation
    # of derived_inference at all -- this is a structural guarantee,
    # not something a fixture could even construct. Proven by
    # inspecting the classifier's own complete set of possible outputs.
    assert "derived_inference" not in (
        se.MEMORY_KIND_HUMAN_EXPRESSION, se.MEMORY_KIND_CLARK_EXPRESSION, se.MEMORY_KIND_MECHANICAL_RECORD,
    )
    assert "derived_inference" not in se.ELIGIBLE_MEMORY_KINDS


# ==================================================== structural eligibility


def test_genuine_conversational_prose_eligible():
    conn, ids, _ = build_fixture("struct_cp_ok")
    assert se.is_sleep_eligible(conn, ids["clark_expr_id"]) is True
    close_conn(conn)


def test_conversational_prose_under_wrong_event_type_rejected():
    conn, ids, _ = build_fixture("struct_cp_wrong_type")
    assert se.is_sleep_eligible(conn, ids["conversational_prose_wrong_event_type_id"]) is False
    close_conn(conn)


def test_genuine_roaming_journal_text_eligible():
    conn, ids, _ = build_fixture("struct_journal_ok")
    assert se.is_sleep_eligible(conn, ids["roaming_journal_id"]) is True
    close_conn(conn)


def test_roaming_journal_text_under_wrong_event_type_rejected():
    conn, ids, _ = build_fixture("struct_journal_wrong_type")
    assert se.is_sleep_eligible(conn, ids["roaming_journal_wrong_event_type_id"]) is False
    close_conn(conn)


def test_roaming_journal_text_missing_run_id_sibling_rejected():
    conn, ids, _ = build_fixture("struct_journal_no_sibling")
    assert se.is_sleep_eligible(conn, ids["roaming_journal_no_sibling_id"]) is False
    close_conn(conn)


def test_authenticated_synthetic_human_expression_still_eligible():
    # Preserves the existing, already-tested authenticated-human-
    # expression code path exactly -- SLP1-A3 does not loosen it merely
    # because no live production writer currently reaches it.
    conn, ids, _ = build_fixture("struct_human_auth_preserved")
    assert se.is_sleep_eligible(conn, ids["human_expr_authenticated_id"]) is True
    close_conn(conn)


def test_unauthenticated_or_unknown_auth_human_still_rejected():
    conn, ids, _ = build_fixture("struct_human_unauth_preserved")
    assert se.is_sleep_eligible(conn, ids["human_expr_unauthenticated_id"]) is False
    assert se.is_sleep_eligible(conn, ids["human_expr_no_auth_context_id"]) is False
    close_conn(conn)


# ============================================= delivered-resource traces


def test_genuine_delivered_text_trace_eligible():
    conn, ids, _ = build_fixture("drt_text_ok")
    assert se.is_sleep_eligible(conn, ids["delivered_text_good_id"]) is True
    close_conn(conn)


def test_genuine_delivered_measurement_trace_eligible():
    conn, ids, _ = build_fixture("drt_audio_ok")
    assert se.is_sleep_eligible(conn, ids["delivered_measurement_good_id"]) is True
    close_conn(conn)


def test_delivered_trace_classified_as_delivered_resource_trace_not_mechanical_record():
    conn, ids, _ = build_fixture("drt_classification")
    result = se.build_sleep_evidence_v1(conn)
    item = next(i for i in result["items"] if i["component_id"] == ids["delivered_text_good_id"])
    assert item["memory_kind"] == se.MEMORY_KIND_DELIVERED_RESOURCE_TRACE
    assert item["memory_kind"] != se.MEMORY_KIND_MECHANICAL_RECORD
    close_conn(conn)


def test_delivered_text_hash_mismatch_ineligible():
    conn, ids, _ = build_fixture("drt_bad_hash")
    assert se.is_sleep_eligible(conn, ids["delivered_text_bad_hash_id"]) is False
    close_conn(conn)


def test_delivered_text_not_genuinely_delivered_ineligible():
    conn, ids, _ = build_fixture("drt_not_delivered")
    assert se.is_sleep_eligible(conn, ids["delivered_text_not_delivered_id"]) is False
    close_conn(conn)


def test_delivered_text_wrong_modality_ineligible():
    conn, ids, _ = build_fixture("drt_wrong_modality")
    assert se.is_sleep_eligible(conn, ids["delivered_text_wrong_modality_id"]) is False
    close_conn(conn)


def test_delivered_text_orphan_no_sibling_ineligible():
    conn, ids, _ = build_fixture("drt_orphan")
    assert se.is_sleep_eligible(conn, ids["delivered_text_orphan_id"]) is False
    close_conn(conn)


def test_resource_encounter_fact_never_selection_bearing():
    conn, ids, _ = build_fixture("drt_fact_not_selection_bearing")
    assert se.is_sleep_eligible(conn, ids["resource_encounter_fact_good_id"]) is False
    close_conn(conn)


def test_triggering_waking_event_id_component_never_selection_bearing():
    conn, ids, _ = build_fixture("drt_trigger_link_not_selection_bearing")
    assert se.is_sleep_eligible(conn, ids["triggering_waking_event_id_component_id"]) is False
    close_conn(conn)


# ==================================================== human waking input


def test_genuine_authenticated_human_waking_input_eligible():
    conn, ids, _ = build_fixture("hwi_auth_eligible")
    assert se.is_sleep_eligible(conn, ids["human_waking_input_authenticated_id"]) is True
    close_conn(conn)


def test_human_waking_input_classified_as_human_expression():
    conn, ids, _ = build_fixture("hwi_classification")
    result = se.build_sleep_evidence_v1(conn)
    item = next(i for i in result["items"] if i["component_id"] == ids["human_waking_input_authenticated_id"])
    assert item["memory_kind"] == se.MEMORY_KIND_HUMAN_EXPRESSION
    assert item["expression"] == "Genuine authenticated human utterance."
    close_conn(conn)


def test_unauthenticated_human_waking_input_ineligible():
    conn, ids, _ = build_fixture("hwi_unauth_ineligible")
    assert se.is_sleep_eligible(conn, ids["human_waking_input_unauthenticated_id"]) is False
    close_conn(conn)


def test_human_waking_input_under_wrong_event_type_ineligible():
    conn, ids, _ = build_fixture("hwi_wrong_event_type_ineligible")
    assert se.is_sleep_eligible(conn, ids["human_waking_input_wrong_event_type_id"]) is False
    close_conn(conn)


def test_human_waking_input_actor_mismatch_ineligible():
    # Clark-attributed text inside a human_waking_input event: the
    # tuple (event_type, component_kind, actor_type) never matches
    # because actor_type resolves to clark_agent, not human_person.
    conn, ids, _ = build_fixture("hwi_actor_mismatch_ineligible")
    assert se.is_sleep_eligible(conn, ids["human_waking_input_actor_mismatch_id"]) is False
    close_conn(conn)


# ============================================================= provenance =


def test_evidence_preserves_exact_event_and_component_ids():
    conn, ids, _ = build_fixture("prov_ids")
    result = se.build_sleep_evidence_v1(conn)
    item = next(i for i in result["items"] if i["component_id"] == ids["clark_expr_id"])
    assert item["event_id"] == "evt-cp"
    assert item["component_id"] == ids["clark_expr_id"]
    close_conn(conn)


def test_evidence_preserves_actor_mechanically():
    conn, ids, _ = build_fixture("prov_actor")
    result = se.build_sleep_evidence_v1(conn)
    item = next(i for i in result["items"] if i["component_id"] == ids["clark_expr_id"])
    assert item["speaker_actor_id"] == ids["clark_id"]
    assert item["memory_kind"] == se.MEMORY_KIND_CLARK_EXPRESSION
    item2 = next(i for i in result["items"] if i["component_id"] == ids["human_expr_authenticated_id"])
    assert item2["speaker_actor_id"] == ids["human_id"]
    assert item2["memory_kind"] == se.MEMORY_KIND_HUMAN_EXPRESSION
    close_conn(conn)


def test_evidence_preserves_auth_status_where_available():
    conn, ids, _ = build_fixture("prov_auth")
    result = se.build_sleep_evidence_v1(conn)
    item = next(i for i in result["items"] if i["component_id"] == ids["human_expr_authenticated_id"])
    assert item["authentication_status"] == "authenticated"
    close_conn(conn)


def test_evidence_preserves_pipeline_and_model_provenance_where_available():
    conn, ids, prov_path = build_fixture("prov_pipeline")
    # The fixture's clark_expr component was inserted with no
    # model_revision_id (event_components is append-only, so this is
    # tested at insert time, not via a later UPDATE).
    result = se.build_sleep_evidence_v1(conn)
    item = next(i for i in result["items"] if i["component_id"] == ids["clark_expr_id"])
    assert item["pipeline_id"] == "pipe-llama-1"
    assert item["model_revision_id"] is None  # never invented merely to fill the schema slot
    close_conn(conn)


def test_no_invented_metadata_for_rejected_items():
    # Rejected components never even reach the SLEEP_EVIDENCE_V1 items
    # list, so there is no risk of a placeholder/fabricated field for
    # them -- proven by their absence.
    conn, ids, _ = build_fixture("no_invented")
    result = se.build_sleep_evidence_v1(conn)
    eligible_ids = {i["component_id"] for i in result["items"]}
    assert ids["mechanical_record_id"] not in eligible_ids
    assert ids["mixed_historical_id"] not in eligible_ids
    assert ids["human_expr_unauthenticated_id"] not in eligible_ids
    close_conn(conn)


# ================================================================ content =


def test_exact_bounded_expression_preserved():
    conn, ids, _ = build_fixture("content_exact")
    result = se.build_sleep_evidence_v1(conn)
    item = next(i for i in result["items"] if i["component_id"] == ids["clark_expr_id"])
    assert item["expression"] == "Clark's own words."
    assert item["is_final_segment"] is True
    assert item["segment_index"] == 0
    close_conn(conn)


def test_no_semantic_summary_field_exists():
    conn, ids, _ = build_fixture("content_no_summary")
    result = se.build_sleep_evidence_v1(conn)
    item = next(i for i in result["items"] if i["component_id"] == ids["clark_expr_id"])
    for forbidden_key in ("summary", "topic", "meaning", "interpretation", "label", "emotion", "preference"):
        assert forbidden_key not in item
    close_conn(conn)


def test_content_hash_mismatch_raises_integrity_error():
    # event_components is append-only (no UPDATE possible) -- a hash
    # mismatch is instead constructed directly at insert time by
    # storing a content_sha256 that disagrees with the actual text.
    conn, ids, _ = build_fixture("content_hash_mismatch")
    _insert_event(conn, "evt-tampered", ids["pipeline_id"], "ac-auth", 1500)
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256) VALUES (?, 0, ?, 'conversational_prose', 'resolved', ?, ?)",
        ("evt-tampered", ids["clark_id"], "the real text", "0" * 64),
    )
    conn.commit()
    try:
        se.build_sleep_evidence_v1(conn)
        assert False, "expected SleepEvidenceIntegrityError"
    except se.SleepEvidenceIntegrityError:
        pass
    close_conn(conn)


def test_hash_corruption_fails_closed_before_any_segment_emitted():
    # A tampered component that WOULD otherwise require segmentation
    # must still fail closed before any of its segments are emitted --
    # integrity verification happens before segmentation, not after.
    conn, ids, _ = build_fixture("hash_corruption_before_segments")
    long_text = "Y" * 5000
    _insert_event(conn, "evt-tampered-long", ids["pipeline_id"], "ac-auth", 1600)
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256) VALUES (?, 0, ?, 'conversational_prose', 'resolved', ?, ?)",
        ("evt-tampered-long", ids["clark_id"], long_text, "0" * 64),
    )
    conn.commit()
    try:
        se.build_sleep_evidence_v1(conn, max_chars_per_segment=100, max_aggregate_chars=1_000_000)
        assert False, "expected SleepEvidenceIntegrityError"
    except se.SleepEvidenceIntegrityError:
        pass
    close_conn(conn)


# ============================================================= ordering ===


def test_oldest_eligible_items_selected_first_deterministic_order():
    conn, ids, _ = build_fixture("order_basic")
    result = se.build_sleep_evidence_v1(conn)
    component_ids = [i["component_id"] for i in result["items"]]
    assert component_ids == sorted(component_ids)  # component_id ASC, i.e. oldest-first
    close_conn(conn)


def test_per_segment_bound_enforced():
    conn, ids, _ = build_fixture("bound_per_item")
    result = se.build_sleep_evidence_v1(conn, max_chars_per_segment=5)
    for item in result["items"]:
        assert len(item["expression"]) <= 5
    close_conn(conn)


def test_aggregate_bound_enforced():
    conn, ids, prov_path = build_fixture("bound_aggregate")
    for n in range(5):
        _insert_event(conn, f"evt-agg-{n}", ids["pipeline_id"], "ac-auth", 2000 + n)
        _insert_component(conn, f"evt-agg-{n}", 0, ids["clark_id"], "conversational_prose", "resolved", "A" * 50)
    conn.commit()
    result = se.build_sleep_evidence_v1(conn, max_chars_per_segment=50, max_aggregate_chars=120)
    total_chars = sum(len(i["expression"]) for i in result["items"])
    assert total_chars <= 120
    close_conn(conn)


def test_batch_count_bound_enforced():
    conn, ids, prov_path = build_fixture("bound_count")
    for n in range(30):
        _insert_event(conn, f"evt-count-{n}", ids["pipeline_id"], "ac-auth", 3000 + n)
        _insert_component(conn, f"evt-count-{n}", 0, ids["clark_id"], "conversational_prose", "resolved", "hi")
    conn.commit()
    result = se.build_sleep_evidence_v1(conn, max_segments=3)
    assert len(result["items"]) == 3
    close_conn(conn)


def test_interleaved_ineligible_records_do_not_break_forward_selection():
    conn, ids, prov_path = build_fixture("interleave")
    # Alternate eligible/ineligible components across a wider component_id
    # range than the batch size, prove the batch still fills with the
    # correct oldest eligible items, skipping ineligible ones in place.
    for n in range(10):
        _insert_event(conn, f"evt-elig-{n}", ids["pipeline_id"], "ac-auth", 4000 + n * 2)
        _insert_component(conn, f"evt-elig-{n}", 0, ids["clark_id"], "conversational_prose", "resolved", f"eligible {n}")
        _insert_event(conn, f"evt-inelig-{n}", ids["pipeline_id"], "ac-auth", 4001 + n * 2)
        _insert_component(conn, f"evt-inelig-{n}", 0, ids["host_id"], "bounded_clause", "resolved", f"ineligible {n}")
    conn.commit()
    result = se.build_sleep_evidence_v1(conn, max_segments=5, after_component_id=ids["triggering_waking_event_id_component_id"])
    assert len(result["items"]) == 5
    for item in result["items"]:
        assert item["expression"].startswith("eligible")
    close_conn(conn)


def test_after_component_id_excludes_everything_at_or_before_it():
    conn, ids, _ = build_fixture("after_watermark")
    result = se.build_sleep_evidence_v1(conn, after_component_id=ids["clark_expr_id"])
    component_ids = {i["component_id"] for i in result["items"]}
    assert ids["clark_expr_id"] not in component_ids
    close_conn(conn)


# ========================================================== segmentation ==


def test_empty_component_yields_one_final_segment():
    conn, ids, prov_path = build_fixture("seg_empty")
    _insert_event(conn, "evt-empty", ids["pipeline_id"], "ac-auth", 5000)
    empty_id = _insert_component(conn, "evt-empty", 0, ids["clark_id"], "conversational_prose", "resolved", "")
    conn.commit()
    result = se.build_sleep_evidence_v1(conn, after_component_id=empty_id - 1)
    matches = [i for i in result["items"] if i["component_id"] == empty_id]
    assert len(matches) == 1
    item = matches[0]
    assert item["char_start"] == 0 and item["char_end"] == 0
    assert item["expression"] == ""
    assert item["is_final_segment"] is True
    assert item["segment_index"] == 0
    assert empty_id in result["component_ids_evidence_complete"]
    close_conn(conn)


def test_exactly_at_bound_component_yields_one_segment_no_loss():
    conn, ids, prov_path = build_fixture("seg_exact_bound")
    text = "Z" * 100
    _insert_event(conn, "evt-exact", ids["pipeline_id"], "ac-auth", 5100)
    exact_id = _insert_component(conn, "evt-exact", 0, ids["clark_id"], "conversational_prose", "resolved", text)
    conn.commit()
    result = se.build_sleep_evidence_v1(conn, after_component_id=exact_id - 1, max_chars_per_segment=100, max_aggregate_chars=1_000_000)
    matches = [i for i in result["items"] if i["component_id"] == exact_id]
    assert len(matches) == 1
    assert matches[0]["expression"] == text
    assert matches[0]["is_final_segment"] is True
    assert exact_id in result["component_ids_evidence_complete"]
    close_conn(conn)


def test_multi_segment_component_reconstructs_exactly_no_gap_no_overlap():
    conn, ids, prov_path = build_fixture("seg_multi_reconstruct")
    text = "".join(f"[{i:04d}]" for i in range(500))  # 6 chars * 500 = 3000 chars, deterministic content
    _insert_event(conn, "evt-multi", ids["pipeline_id"], "ac-auth", 5200)
    multi_id = _insert_component(conn, "evt-multi", 0, ids["clark_id"], "conversational_prose", "resolved", text)
    conn.commit()
    result = se.build_sleep_evidence_v1(conn, after_component_id=multi_id - 1, max_chars_per_segment=250, max_aggregate_chars=1_000_000, max_segments=1000)
    segs = sorted([i for i in result["items"] if i["component_id"] == multi_id], key=lambda i: i["segment_index"])
    assert len(segs) == 3000 // 250  # 12 exact segments
    reconstructed = "".join(s["expression"] for s in segs)
    assert reconstructed == text
    for j, s in enumerate(segs):
        assert s["segment_index"] == j
        assert s["char_start"] == j * 250
        assert s["char_end"] == min((j + 1) * 250, len(text))
        assert s["is_final_segment"] == (j == len(segs) - 1)
    # no overlap / no gap: consecutive segments' char ranges are contiguous
    for a, b in zip(segs, segs[1:]):
        assert a["char_end"] == b["char_start"]
    # deterministic ids: rebuilding produces byte-identical segment_ids
    result2 = se.build_sleep_evidence_v1(conn, after_component_id=multi_id - 1, max_chars_per_segment=250, max_aggregate_chars=1_000_000, max_segments=1000)
    segs2 = sorted([i for i in result2["items"] if i["component_id"] == multi_id], key=lambda i: i["segment_index"])
    assert [s["segment_id"] for s in segs] == [s["segment_id"] for s in segs2]
    assert multi_id in result["component_ids_evidence_complete"]
    close_conn(conn)


def test_multiple_segments_of_same_component_emit_in_one_batch_when_bounds_permit():
    conn, ids, prov_path = build_fixture("seg_multi_per_batch")
    text = "Q" * 900  # 9 segments of 100 chars each
    _insert_event(conn, "evt-manyseg", ids["pipeline_id"], "ac-auth", 5300)
    many_id = _insert_component(conn, "evt-manyseg", 0, ids["clark_id"], "conversational_prose", "resolved", text)
    conn.commit()
    result = se.build_sleep_evidence_v1(conn, after_component_id=many_id - 1, max_chars_per_segment=100, max_aggregate_chars=1_000_000, max_segments=1000)
    segs = [i for i in result["items"] if i["component_id"] == many_id]
    # MORE THAN ONE segment of the same canonical component emitted in one call.
    assert len(segs) == 9
    assert many_id in result["component_ids_evidence_complete"]
    close_conn(conn)


def test_partial_component_stops_batch_before_next_non_fitting_segment():
    conn, ids, prov_path = build_fixture("seg_partial_stop")
    text = "R" * 900  # 9 segments of 100 chars
    _insert_event(conn, "evt-partial", ids["pipeline_id"], "ac-auth", 5400)
    partial_id = _insert_component(conn, "evt-partial", 0, ids["clark_id"], "conversational_prose", "resolved", text)
    # A later, otherwise-eligible component that must NEVER be reached
    # while `partial_id` is still pending.
    _insert_event(conn, "evt-after-partial", ids["pipeline_id"], "ac-auth", 5401)
    later_id = _insert_component(conn, "evt-after-partial", 0, ids["clark_id"], "conversational_prose", "resolved", "should not be reached")
    conn.commit()
    # Only enough aggregate budget for 3 of the 9 segments (300 chars).
    result = se.build_sleep_evidence_v1(conn, after_component_id=partial_id - 1, max_chars_per_segment=100, max_aggregate_chars=300, max_segments=1000)
    segs = [i for i in result["items"] if i["component_id"] == partial_id]
    assert len(segs) == 3
    assert result["pending_component_id"] == partial_id
    assert result["pending_next_segment_index"] == 3
    assert partial_id not in result["component_ids_evidence_complete"]
    # No later canonical component was emitted or marked complete.
    assert not any(i["component_id"] == later_id for i in result["items"])
    assert later_id not in result["component_ids_evidence_complete"]
    close_conn(conn)


def test_resume_does_not_duplicate_and_completes_then_continues():
    conn, ids, prov_path = build_fixture("seg_resume")
    text = "S" * 900  # 9 segments of 100 chars
    _insert_event(conn, "evt-resume", ids["pipeline_id"], "ac-auth", 5500)
    resume_id = _insert_component(conn, "evt-resume", 0, ids["clark_id"], "conversational_prose", "resolved", text)
    _insert_event(conn, "evt-after-resume", ids["pipeline_id"], "ac-auth", 5501)
    later_id = _insert_component(conn, "evt-after-resume", 0, ids["clark_id"], "conversational_prose", "resolved", "reached after resume completes")
    conn.commit()

    # First call: only 3 segments fit.
    result1 = se.build_sleep_evidence_v1(conn, after_component_id=resume_id - 1, max_chars_per_segment=100, max_aggregate_chars=300, max_segments=1000)
    assert len(result1["items"]) == 3
    assert result1["pending_component_id"] == resume_id
    assert result1["pending_next_segment_index"] == 3

    # Second call: correct resume state, budget for the remaining 6 segments plus the later component.
    result2 = se.build_sleep_evidence_v1(
        conn, after_component_id=resume_id - 1,
        resume_component_id=result1["pending_component_id"], resume_segment_index=result1["pending_next_segment_index"],
        max_chars_per_segment=100, max_aggregate_chars=1_000_000, max_segments=1000,
    )
    resumed_segs = [i for i in result2["items"] if i["component_id"] == resume_id]
    assert len(resumed_segs) == 6  # segments 3..8, never re-emitting 0..2
    assert sorted(i["segment_index"] for i in resumed_segs) == list(range(3, 9))
    assert resume_id in result2["component_ids_evidence_complete"]
    # Budget remained after completion -> the later component is also emitted in the SAME call.
    assert any(i["component_id"] == later_id for i in result2["items"])
    assert later_id in result2["component_ids_evidence_complete"]
    close_conn(conn)


def test_aggregate_limit_never_marks_non_fitting_segment_complete():
    conn, ids, prov_path = build_fixture("seg_aggregate_pending")
    _insert_event(conn, "evt-fits", ids["pipeline_id"], "ac-auth", 5600)
    fits_id = _insert_component(conn, "evt-fits", 0, ids["clark_id"], "conversational_prose", "resolved", "A" * 50)
    _insert_event(conn, "evt-doesnotfit", ids["pipeline_id"], "ac-auth", 5601)
    doesnotfit_id = _insert_component(conn, "evt-doesnotfit", 0, ids["clark_id"], "conversational_prose", "resolved", "B" * 50)
    conn.commit()
    result = se.build_sleep_evidence_v1(conn, after_component_id=fits_id - 1, max_chars_per_segment=50, max_aggregate_chars=50, max_segments=1000)
    assert fits_id in result["component_ids_evidence_complete"]
    assert doesnotfit_id not in result["component_ids_evidence_complete"]
    assert result["pending_component_id"] == doesnotfit_id
    assert result["pending_next_segment_index"] == 0
    assert not any(i["component_id"] == doesnotfit_id for i in result["items"])
    close_conn(conn)


def test_ineligible_component_emits_nothing_and_is_evidence_complete_without_interference():
    conn, ids, prov_path = build_fixture("seg_ineligible")
    result = se.build_sleep_evidence_v1(conn)
    # mechanical_record_id is ineligible: no items, but it IS evidence-complete
    # at this layer (nothing further to emit for it), and does not block
    # later eligible components from being emitted.
    assert not any(i["component_id"] == ids["mechanical_record_id"] for i in result["items"])
    assert ids["mechanical_record_id"] in result["component_ids_evidence_complete"]
    assert any(i["component_id"] == ids["clark_expr_id"] for i in result["items"])
    close_conn(conn)


def test_impossible_config_rejected_before_scanning():
    conn, ids, prov_path = build_fixture("seg_bad_config")
    for kwargs in (
        dict(max_segments=0),
        dict(max_chars_per_segment=0),
        dict(max_aggregate_chars=0),
        dict(max_chars_per_segment=200, max_aggregate_chars=100),
    ):
        try:
            se.build_sleep_evidence_v1(conn, **kwargs)
            assert False, f"expected SleepEvidenceConfigurationError for {kwargs!r}"
        except se.SleepEvidenceConfigurationError:
            pass
    close_conn(conn)


def test_component_ids_scanned_is_diagnostic_only_and_not_authoritative():
    conn, ids, prov_path = build_fixture("scanned_diagnostic")
    result = se.build_sleep_evidence_v1(conn)
    # scanned may legitimately be a strict superset of evidence_complete
    # (it always is, once a pending component exists) -- it confers no
    # completion authority by itself.
    assert set(result["component_ids_evidence_complete"]).issubset(set(result["component_ids_scanned"]))
    close_conn(conn)


def test_evidence_complete_is_not_a_watermark_claim():
    # Regression: the field name/contract must not be read as
    # "safe to advance a Sleep watermark." No watermark-writing code
    # exists in this module at all (see isolation test below); this
    # test additionally asserts the module documents the distinction
    # explicitly in its own build_sleep_evidence_v1 docstring, so the
    # separation cannot silently rot away in a later edit.
    doc = se.build_sleep_evidence_v1.__doc__ or ""
    assert "NOT A WATERMARK-ADVANCE SIGNAL" in doc
    assert "BUILDING EVIDENCE != PROCESSING EVIDENCE" in doc


# ============================================================== bounds ====


def test_bounds_constants_are_conservative_defaults():
    assert se.MAX_SEGMENTS_PER_BATCH == 20
    assert se.MAX_CHARS_PER_SEGMENT == 2000
    assert se.MAX_AGGREGATE_EVIDENCE_CHARS == 20000


# ============================================================ exclusions ==


def test_sleep_evidence_module_imports_nothing_beyond_minimal_stdlib():
    with open(os.path.join(ANAXI_FINAL, "sleep_evidence.py"), encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert names.issubset({"dataclasses", "hashlib", "json", "os", "sqlite3", "typing", "pathlib"})
    # No path to Kardia, the legacy graph, hippocampus, workspace, or
    # any operational trace exists -- this module cannot import what it
    # never imports. Also confirms no watermark-writing dependency was
    # introduced by the SLP1-A3 frontier changes.
    for forbidden in ("workspace_capability", "workspace_private", "workspace_roaming", "workspace_direction",
                       "hippocampus_store", "hippocampus_retrieval", "anaxi_protocol", "anaxi_protocol_sqlite",
                       "orchestration", "conversation_direction", "clark_journal", "ollama", "anthropic",
                       "sleep_watermark", "provenance_schema"):
        assert forbidden not in names


def test_module_never_references_kardia_or_legacy_graph_tables():
    # Functional checks (actual SQL literals this module would need to
    # reach Kardia/legacy graph/workspace tables), not a bare
    # "anaxi_mind_llama" substring scan -- the module's own docstring
    # legitimately names anaxi_mind_llama.db in prose, explaining that
    # it is NOT read here.
    with open(os.path.join(ANAXI_FINAL, "sleep_evidence.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("get_current_kardia", "active_kardia", "FROM nodes", "FROM edges",
                       "sqlite3.connect(\"anaxi_mind", "sqlite3.connect('anaxi_mind",
                       "open(\"anaxi_log.jsonl", "open('anaxi_log.jsonl", "import workspace"):
        assert forbidden not in source, forbidden


def test_sleep_evidence_performs_no_db_writes():
    # Grep-level regression: no INSERT/UPDATE/DELETE/CREATE literal
    # anywhere in this module's own source -- it is read-only by
    # construction, and the SLP1-A3 frontier/resume contract must not
    # have introduced any persistence.
    with open(os.path.join(ANAXI_FINAL, "sleep_evidence.py"), encoding="utf-8") as f:
        source = f.read().upper()
    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE", "DROP TABLE"):
        assert forbidden not in source, forbidden


def test_anaxi_log_jsonl_never_used_as_fallback():
    # Even if the canonical provenance DB is unavailable, this module
    # must fail closed rather than fall back to the legacy log.
    missing_path = os.path.join(TEST_ROOT, "does_not_exist", "anaxi_provenance.db")
    try:
        se._open_provenance_readonly(missing_path)
        assert False, "expected SleepEvidenceUnavailableError"
    except se.SleepEvidenceUnavailableError:
        pass


def test_kardia_and_legacy_graph_unreachable_via_evidence_items():
    conn, ids, _ = build_fixture("no_kardia_leak")
    result = se.build_sleep_evidence_v1(conn)
    dumped = json.dumps(result["items"])
    for forbidden in ("kardia", "moral_valve", "volitional_channel", "affective_stance", "aesthetic_valve"):
        assert forbidden not in dumped.lower()
    close_conn(conn)


ALL_TESTS = [
    test_resolved_clark_expression_eligible,
    test_resolved_authenticated_human_expression_eligible,
    test_claimed_only_human_expression_rejected,
    test_unauthenticated_human_expression_rejected,
    test_human_expression_with_no_auth_context_rejected,
    test_mechanical_record_rejected,
    test_bounded_clause_never_in_selection_tuples,
    test_mixed_historical_expression_rejected,
    test_unknown_component_kind_rejected,
    test_protected_decision_expression_rejected_despite_resolved_clark_actor,
    test_nonexistent_component_id_rejected,
    test_classifier_never_produces_derived_inference,
    test_genuine_conversational_prose_eligible,
    test_conversational_prose_under_wrong_event_type_rejected,
    test_genuine_roaming_journal_text_eligible,
    test_roaming_journal_text_under_wrong_event_type_rejected,
    test_roaming_journal_text_missing_run_id_sibling_rejected,
    test_authenticated_synthetic_human_expression_still_eligible,
    test_unauthenticated_or_unknown_auth_human_still_rejected,
    test_genuine_delivered_text_trace_eligible,
    test_genuine_delivered_measurement_trace_eligible,
    test_delivered_trace_classified_as_delivered_resource_trace_not_mechanical_record,
    test_delivered_text_hash_mismatch_ineligible,
    test_delivered_text_not_genuinely_delivered_ineligible,
    test_delivered_text_wrong_modality_ineligible,
    test_delivered_text_orphan_no_sibling_ineligible,
    test_resource_encounter_fact_never_selection_bearing,
    test_triggering_waking_event_id_component_never_selection_bearing,
    test_genuine_authenticated_human_waking_input_eligible,
    test_human_waking_input_classified_as_human_expression,
    test_unauthenticated_human_waking_input_ineligible,
    test_human_waking_input_under_wrong_event_type_ineligible,
    test_human_waking_input_actor_mismatch_ineligible,
    test_evidence_preserves_exact_event_and_component_ids,
    test_evidence_preserves_actor_mechanically,
    test_evidence_preserves_auth_status_where_available,
    test_evidence_preserves_pipeline_and_model_provenance_where_available,
    test_no_invented_metadata_for_rejected_items,
    test_exact_bounded_expression_preserved,
    test_no_semantic_summary_field_exists,
    test_content_hash_mismatch_raises_integrity_error,
    test_hash_corruption_fails_closed_before_any_segment_emitted,
    test_oldest_eligible_items_selected_first_deterministic_order,
    test_per_segment_bound_enforced,
    test_aggregate_bound_enforced,
    test_batch_count_bound_enforced,
    test_interleaved_ineligible_records_do_not_break_forward_selection,
    test_after_component_id_excludes_everything_at_or_before_it,
    test_empty_component_yields_one_final_segment,
    test_exactly_at_bound_component_yields_one_segment_no_loss,
    test_multi_segment_component_reconstructs_exactly_no_gap_no_overlap,
    test_multiple_segments_of_same_component_emit_in_one_batch_when_bounds_permit,
    test_partial_component_stops_batch_before_next_non_fitting_segment,
    test_resume_does_not_duplicate_and_completes_then_continues,
    test_aggregate_limit_never_marks_non_fitting_segment_complete,
    test_ineligible_component_emits_nothing_and_is_evidence_complete_without_interference,
    test_impossible_config_rejected_before_scanning,
    test_component_ids_scanned_is_diagnostic_only_and_not_authoritative,
    test_evidence_complete_is_not_a_watermark_claim,
    test_bounds_constants_are_conservative_defaults,
    test_sleep_evidence_module_imports_nothing_beyond_minimal_stdlib,
    test_module_never_references_kardia_or_legacy_graph_tables,
    test_sleep_evidence_performs_no_db_writes,
    test_anaxi_log_jsonl_never_used_as_fallback,
    test_kardia_and_legacy_graph_unreachable_via_evidence_items,
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
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
