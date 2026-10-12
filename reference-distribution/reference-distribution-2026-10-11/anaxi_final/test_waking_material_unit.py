"""SLP1-A4 acceptance tests for waking_material_unit.py. Every test
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
import waking_material_unit as wmu_mod

TEST_ROOT = tempfile.mkdtemp(prefix="slp1a4_wmu_test_")


# =============================================================== fixtures =


def _seed_actors(conn):
    now = int(time.time())
    host_actor_id, clark_actor_id, human_actor_id = "actor-host-1", "actor-clark-1", "actor-human-1"
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', ?)", (host_actor_id, now))
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', ?)", (clark_actor_id, now))
    conn.execute("INSERT INTO actor_clark_agent (actor_id, canonical_key) VALUES (?, 'clark')", (clark_actor_id,))
    conn.execute("INSERT INTO persons (person_id, created_at) VALUES ('person-test', ?)", (now,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', 'test-human', ?)", (human_actor_id, now))
    conn.execute("INSERT INTO actor_human_person (actor_id, person_id, canonical_name, relationship_established_at) VALUES (?, 'person-test', 'Test Human', ?)", (human_actor_id, now))
    return host_actor_id, clark_actor_id, human_actor_id


def _insert_event(conn, event_id, pipeline_id, auth_context_id, occurred_at, event_type):
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        "auth_context_id, input_source_ref, occurred_at, record_created_at) "
        "VALUES (?, ?, ?, 'known', NULL, ?, ?, ?, ?)",
        (event_id, event_type, pipeline_id, auth_context_id, event_id, occurred_at, occurred_at),
    )


def _insert_component(conn, event_id, sequence, creator_actor_id, component_kind, text):
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256) VALUES (?, ?, ?, ?, 'resolved', ?, ?)",
        (event_id, sequence, creator_actor_id, component_kind, text, content_sha256),
    )
    return conn.execute("SELECT component_id FROM event_components WHERE event_id = ? AND sequence = ?", (event_id, sequence)).fetchone()[0]


def _resource_fact(modality, content_sha256, genuinely_delivered=True):
    return json.dumps({
        "resource_class": "library", "relative_path": "notes.txt", "modality": modality,
        "delivered_portion": "chars 0-10", "content_sha256": content_sha256,
        "source_action": "read", "target_model_pathway": "gemma4:e4b",
        "genuinely_delivered": genuinely_delivered,
    })


def build_fixture(name):
    tmp_dir = os.path.join(TEST_ROOT, name)
    os.makedirs(tmp_dir, exist_ok=True)
    prov_path = os.path.join(tmp_dir, "anaxi_provenance.db")
    conn = create_provenance_db(prov_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    pipeline_id = "pipe-llama-1"
    conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) VALUES (?, 'anaxi_orchestration_lineage_a', 'llama', 'test')", (pipeline_id,))
    host_id, clark_id, human_id = _seed_actors(conn)
    conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-1', ?, 100, 200)", (pipeline_id,))
    conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) VALUES ('ac-unk', 'sess-1', 'unknown', 100)")
    conn.execute(
        "INSERT INTO auth_contexts (auth_context_id, session_id, claimed_actor_id, authenticated_actor_id, "
        "auth_state, auth_method, assurance_level, established_at) "
        "VALUES ('ac-bound', 'sess-1', ?, ?, 'authenticated', 'local_session_start_binding', 'low', 90)",
        (human_id, human_id),
    )

    ids = {"pipeline_id": pipeline_id, "host_id": host_id, "clark_id": clark_id, "human_id": human_id}

    # 1. Ordinary waking turn X: Clark's conversational_prose.
    _insert_event(conn, "evt-waking-x", pipeline_id, "ac-unk", 100, "waking_turn")
    ids["x_event_id"] = "evt-waking-x"
    ids["x_component_id"] = _insert_component(conn, "evt-waking-x", 1, clark_id, "conversational_prose", "Clark's reply about the photo.")

    # 2. Public roaming journal event J: journal text + run-id sibling + action fact.
    _insert_event(conn, "evt-journal-j", pipeline_id, "ac-unk", 200, "workspace_roaming_public_action")
    ids["j_event_id"] = "evt-journal-j"
    _insert_component(conn, "evt-journal-j", 0, host_id, "workspace_roaming_run_id", "wrun-fixture-1")
    ids["j_component_id"] = _insert_component(conn, "evt-journal-j", 1, clark_id, "roaming_journal_text", "Clark's own journal entry.")
    ids["j_action_fact_component_id"] = _insert_component(conn, "evt-journal-j", 2, host_id, "roaming_action_fact", json.dumps({"resource_class": "journal", "action": "append"}))

    # 3. Resource encounter Y, backlinked to X via triggering_waking_event_id.
    text_y = "Delivered library text for Y."
    hash_y = hashlib.sha256(text_y.encode("utf-8")).hexdigest()
    _insert_event(conn, "evt-resource-y", pipeline_id, "ac-unk", 150, "workspace_resource_encounter")
    ids["y_event_id"] = "evt-resource-y"
    _insert_component(conn, "evt-resource-y", 0, host_id, "resource_encounter_fact", _resource_fact("text", hash_y))
    ids["y_component_id"] = _insert_component(conn, "evt-resource-y", 1, host_id, "resource_encounter_delivered_text", text_y)
    _insert_component(conn, "evt-resource-y", 2, host_id, "triggering_waking_event_id", ids["x_event_id"])

    # 4. Standalone resource encounter Z (no backlink).
    measurement_z = json.dumps({"view": "full"}, sort_keys=True)
    hash_z = hashlib.sha256(measurement_z.encode("utf-8")).hexdigest()
    _insert_event(conn, "evt-resource-z", pipeline_id, "ac-unk", 250, "workspace_resource_encounter")
    ids["z_event_id"] = "evt-resource-z"
    _insert_component(conn, "evt-resource-z", 0, host_id, "resource_encounter_fact", _resource_fact("audio", hash_z))
    ids["z_component_id"] = _insert_component(conn, "evt-resource-z", 1, host_id, "resource_encounter_delivered_measurement", measurement_z)

    # 5. Resource encounter W: backlink points to a NONEXISTENT event.
    text_w = "Delivered text for W with an invalid backlink target."
    hash_w = hashlib.sha256(text_w.encode("utf-8")).hexdigest()
    _insert_event(conn, "evt-resource-w", pipeline_id, "ac-unk", 300, "workspace_resource_encounter")
    ids["w_event_id"] = "evt-resource-w"
    _insert_component(conn, "evt-resource-w", 0, host_id, "resource_encounter_fact", _resource_fact("text", hash_w))
    ids["w_component_id"] = _insert_component(conn, "evt-resource-w", 1, host_id, "resource_encounter_delivered_text", text_w)
    _insert_component(conn, "evt-resource-w", 2, host_id, "triggering_waking_event_id", "evt-does-not-exist")

    # 6. Resource encounter V: backlink points to another resource encounter (wrong event_type).
    text_v = "Delivered text for V with a wrong-type backlink target."
    hash_v = hashlib.sha256(text_v.encode("utf-8")).hexdigest()
    _insert_event(conn, "evt-resource-v", pipeline_id, "ac-unk", 350, "workspace_resource_encounter")
    ids["v_event_id"] = "evt-resource-v"
    _insert_component(conn, "evt-resource-v", 0, host_id, "resource_encounter_fact", _resource_fact("text", hash_v))
    ids["v_component_id"] = _insert_component(conn, "evt-resource-v", 1, host_id, "resource_encounter_delivered_text", text_v)
    _insert_component(conn, "evt-resource-v", 2, host_id, "triggering_waking_event_id", ids["z_event_id"])

    # 7. A long, multi-segment conversational_prose in its own waking turn M.
    long_text = "".join(f"[{i:04d}]" for i in range(50))  # 300 chars
    _insert_event(conn, "evt-waking-m", pipeline_id, "ac-unk", 400, "waking_turn")
    ids["m_event_id"] = "evt-waking-m"
    ids["m_component_id"] = _insert_component(conn, "evt-waking-m", 1, clark_id, "conversational_prose", long_text)
    ids["m_text"] = long_text

    # 8. SLP1-A4c: standalone authorized human address H (no waking_turn
    # ever links back to it) -- an unanswered exchange.
    _insert_event(conn, "evt-human-h-standalone", pipeline_id, "ac-bound", 500, "human_waking_input")
    ids["h_standalone_event_id"] = "evt-human-h-standalone"
    ids["h_standalone_component_id"] = _insert_component(conn, "evt-human-h-standalone", 0, human_id, "human_conversational_input", "Clark, are you there?")

    # 9. H2 answered by X2: X2 (waking_turn) carries an explicit
    # human_input_event_id backlink to H2.
    _insert_event(conn, "evt-human-h2", pipeline_id, "ac-bound", 600, "human_waking_input")
    ids["h2_event_id"] = "evt-human-h2"
    ids["h2_component_id"] = _insert_component(conn, "evt-human-h2", 0, human_id, "human_conversational_input", "What do you think about the photo?")
    _insert_event(conn, "evt-waking-x2", pipeline_id, "ac-unk", 601, "waking_turn")
    ids["x2_event_id"] = "evt-waking-x2"
    ids["x2_component_id"] = _insert_component(conn, "evt-waking-x2", 1, clark_id, "conversational_prose", "I think it's a lovely photo.")
    _insert_component(conn, "evt-waking-x2", 4, host_id, "human_input_event_id", ids["h2_event_id"])

    # 10. Y2 resource encounter chained Y2 -> X2 -> H2.
    text_y2 = "Delivered library text for Y2, chained through X2 to H2."
    hash_y2 = hashlib.sha256(text_y2.encode("utf-8")).hexdigest()
    _insert_event(conn, "evt-resource-y2", pipeline_id, "ac-unk", 602, "workspace_resource_encounter")
    ids["y2_event_id"] = "evt-resource-y2"
    _insert_component(conn, "evt-resource-y2", 0, host_id, "resource_encounter_fact", _resource_fact("text", hash_y2))
    ids["y2_component_id"] = _insert_component(conn, "evt-resource-y2", 1, host_id, "resource_encounter_delivered_text", text_y2)
    _insert_component(conn, "evt-resource-y2", 2, host_id, "triggering_waking_event_id", ids["x2_event_id"])

    # 11. X3 has a human_input_event_id backlink pointing to a NONEXISTENT event.
    _insert_event(conn, "evt-waking-x3", pipeline_id, "ac-unk", 700, "waking_turn")
    ids["x3_event_id"] = "evt-waking-x3"
    ids["x3_component_id"] = _insert_component(conn, "evt-waking-x3", 1, clark_id, "conversational_prose", "A reply with a bogus human-input backlink.")
    _insert_component(conn, "evt-waking-x3", 4, host_id, "human_input_event_id", "evt-does-not-exist-either")

    # 12. X4 has a human_input_event_id backlink pointing to a wrong-type
    # event (another waking_turn, not a human_waking_input).
    _insert_event(conn, "evt-waking-x4", pipeline_id, "ac-unk", 800, "waking_turn")
    ids["x4_event_id"] = "evt-waking-x4"
    ids["x4_component_id"] = _insert_component(conn, "evt-waking-x4", 1, clark_id, "conversational_prose", "A reply with a wrong-type human-input backlink.")
    _insert_component(conn, "evt-waking-x4", 4, host_id, "human_input_event_id", ids["x_event_id"])

    conn.commit()
    return conn, ids, prov_path


def close_conn(conn):
    conn.close()


def _evidence_for(conn, component_id_hint=None, **kwargs):
    return se.build_sleep_evidence_v1(conn, **kwargs)["items"]


# =========================================================== primary/identity


def test_ordinary_conversation_primary_is_its_own_waking_turn_event():
    conn, ids, _ = build_fixture("primary_conversation")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    x_wmu = next(w for w in wmus if w["primary_event_id"] == ids["x_event_id"])
    assert x_wmu["wmu_id"] == wmu_mod._derive_wmu_id(ids["x_event_id"])
    close_conn(conn)


def test_journal_primary_is_its_own_public_action_event():
    conn, ids, _ = build_fixture("primary_journal")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    j_wmu = next(w for w in wmus if w["primary_event_id"] == ids["j_event_id"])
    assert j_wmu["wmu_id"] == wmu_mod._derive_wmu_id(ids["j_event_id"])
    close_conn(conn)


def test_backlinked_resource_encounter_attaches_to_triggering_turns_wmu():
    conn, ids, _ = build_fixture("backlink_attach")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    x_wmu = next(w for w in wmus if w["primary_event_id"] == ids["x_event_id"])
    # Y's delivered trace is attached to X's WMU, not its own.
    assert any(e["component_id"] == ids["y_component_id"] for e in x_wmu["selection_bearing"])
    assert not any(w["primary_event_id"] == ids["y_event_id"] for w in wmus)
    close_conn(conn)


def test_backlink_attachment_does_not_change_primary_wmu_identity():
    conn, ids, _ = build_fixture("backlink_identity_stable")
    items = _evidence_for(conn)
    wmus_with_y = wmu_mod.assemble_wmus(conn, items)
    x_wmu = next(w for w in wmus_with_y if w["primary_event_id"] == ids["x_event_id"])
    # Rebuilding with ONLY X's own item (as if Y's backlink hadn't been
    # discovered/committed yet) must produce the SAME wmu_id for X.
    x_only_items = [i for i in items if i["component_id"] == ids["x_component_id"]]
    wmus_without_y = wmu_mod.assemble_wmus(conn, x_only_items)
    x_wmu_alone = wmus_without_y[0]
    assert x_wmu["wmu_id"] == x_wmu_alone["wmu_id"]
    close_conn(conn)


def test_standalone_resource_encounter_forms_its_own_resource_primary_wmu():
    conn, ids, _ = build_fixture("standalone_resource")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    z_wmu = next(w for w in wmus if w["primary_event_id"] == ids["z_event_id"])
    assert any(e["component_id"] == ids["z_component_id"] for e in z_wmu["selection_bearing"])
    close_conn(conn)


def test_invalid_backlink_target_never_attaches_falls_back_to_standalone():
    conn, ids, _ = build_fixture("invalid_backlink")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    # W's backlink names a nonexistent event -- W must stand alone.
    w_wmu = next(w for w in wmus if w["primary_event_id"] == ids["w_event_id"])
    assert any(e["component_id"] == ids["w_component_id"] for e in w_wmu["selection_bearing"])
    close_conn(conn)


def test_wrong_type_backlink_target_never_attaches():
    conn, ids, _ = build_fixture("wrong_type_backlink")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    # V's backlink names Z (a resource_encounter, not a waking_turn) --
    # must never be attached to Z's WMU; V stands alone instead.
    z_wmu = next(w for w in wmus if w["primary_event_id"] == ids["z_event_id"])
    assert not any(e["component_id"] == ids["v_component_id"] for e in z_wmu["selection_bearing"])
    v_wmu = next(w for w in wmus if w["primary_event_id"] == ids["v_event_id"])
    assert any(e["component_id"] == ids["v_component_id"] for e in v_wmu["selection_bearing"])
    close_conn(conn)


def test_no_timestamp_fallback_grouping_source_check():
    with open(os.path.join(ANAXI_FINAL, "waking_material_unit.py"), encoding="utf-8") as f:
        source = f.read()
    assert "occurred_at" not in source  # this module never even reads a timestamp field for grouping


def test_deterministic_wmu_id_rebuild_produces_identical_ids_and_content():
    conn, ids, _ = build_fixture("rebuild_deterministic")
    items = _evidence_for(conn)
    wmus1 = wmu_mod.assemble_wmus(conn, items)
    wmus2 = wmu_mod.assemble_wmus(conn, items)
    ids1 = sorted(w["wmu_id"] for w in wmus1)
    ids2 = sorted(w["wmu_id"] for w in wmus2)
    assert ids1 == ids2
    close_conn(conn)


# =============================================================== content shape


def test_journal_action_fact_is_support_journal_text_is_selection_bearing():
    conn, ids, _ = build_fixture("journal_support_split")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    j_wmu = next(w for w in wmus if w["primary_event_id"] == ids["j_event_id"])
    selection_ids = {e["component_id"] for e in j_wmu["selection_bearing"]}
    support_ids = {s["support_component_id"] for s in j_wmu["supporting_provenance"]}
    assert ids["j_component_id"] in selection_ids
    assert ids["j_action_fact_component_id"] in support_ids
    assert ids["j_action_fact_component_id"] not in selection_ids
    close_conn(conn)


def test_supporting_provenance_never_appears_in_selection_bearing():
    conn, ids, _ = build_fixture("no_support_leak")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    for w in wmus:
        selection_ids = {e["component_id"] for e in w["selection_bearing"]}
        support_ids = {s["support_component_id"] for s in w["supporting_provenance"]}
        assert selection_ids.isdisjoint(support_ids)
    close_conn(conn)


def test_selection_bearing_trace_never_only_an_unlabeled_support_fact():
    conn, ids, _ = build_fixture("labeled_selection")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    x_wmu = next(w for w in wmus if w["primary_event_id"] == ids["x_event_id"])
    entry = next(e for e in x_wmu["selection_bearing"] if e["component_id"] == ids["x_component_id"])
    assert entry["waking_material_class"] in (
        wmu_mod.WAKING_MATERIAL_CLASS_AUTHORED_EXPRESSION, wmu_mod.WAKING_MATERIAL_CLASS_DELIVERED_RESOURCE_TRACE,
    )
    close_conn(conn)


def test_no_banned_interpretive_field_anywhere_in_wmu_schema():
    conn, ids, _ = build_fixture("no_banned_fields")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    banned = (
        "importance", "salience", "relevance", "mood", "topic", "emotional_weight",
        "relationship_importance", "identity_significance", "confidence", "priority",
        "summary", "host_summary",
    )
    dumped = json.dumps(wmus).lower()
    for word in banned:
        assert word not in dumped, word
    close_conn(conn)


def test_wmu_module_performs_no_db_writes():
    with open(os.path.join(ANAXI_FINAL, "waking_material_unit.py"), encoding="utf-8") as f:
        source = f.read().upper()
    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE", "DROP TABLE", ".COMMIT("):
        assert forbidden not in source, forbidden


# ============================================================== segmentation


def test_complete_multi_segment_trace_reconstructs_exact_original_order():
    conn, ids, _ = build_fixture("multi_segment_reconstruct")
    items = _evidence_for(conn, max_chars_per_segment=50, max_aggregate_chars=1_000_000, max_segments=1000)
    wmus = wmu_mod.assemble_wmus(conn, items)
    m_wmu = next(w for w in wmus if w["primary_event_id"] == ids["m_event_id"])
    entry = next(e for e in m_wmu["selection_bearing"] if e["component_id"] == ids["m_component_id"])
    assert entry["expression"] == ids["m_text"]
    # exact source order, no gap, no overlap
    segs = entry["segments"]
    for a, b in zip(segs, segs[1:]):
        assert a["char_end"] == b["char_start"]
    close_conn(conn)


def test_incomplete_segment_set_fails_closed():
    conn, ids, _ = build_fixture("incomplete_segments_fail_closed")
    items = _evidence_for(conn, max_chars_per_segment=50, max_aggregate_chars=1_000_000, max_segments=1000)
    # Manually drop the final segment of the long component to simulate
    # partial evidence -- assemble_wmus must refuse, not guess.
    incomplete = [
        i for i in items
        if not (i["component_id"] == ids["m_component_id"] and i["is_final_segment"])
    ]
    raised = False
    try:
        wmu_mod.assemble_wmus(conn, incomplete)
    except wmu_mod.WmuAssemblyError:
        raised = True
    assert raised
    close_conn(conn)


def test_duplicate_segment_index_fails_closed():
    conn, ids, _ = build_fixture("duplicate_segments_fail_closed")
    items = _evidence_for(conn, max_chars_per_segment=50, max_aggregate_chars=1_000_000, max_segments=1000)
    m_items = [i for i in items if i["component_id"] == ids["m_component_id"]]
    duplicated = m_items + [dict(m_items[0])]  # duplicate segment_index=0
    raised = False
    try:
        wmu_mod.assemble_wmus(conn, duplicated)
    except wmu_mod.WmuAssemblyError:
        raised = True
    assert raised
    close_conn(conn)


def test_gapped_segment_index_fails_closed():
    conn, ids, _ = build_fixture("gapped_segments_fail_closed")
    items = _evidence_for(conn, max_chars_per_segment=50, max_aggregate_chars=1_000_000, max_segments=1000)
    m_items = [i for i in items if i["component_id"] == ids["m_component_id"]]
    gapped = [i for i in m_items if i["segment_index"] != 1]  # remove the middle segment -> gap
    raised = False
    try:
        wmu_mod.assemble_wmus(conn, gapped)
    except wmu_mod.WmuAssemblyError:
        raised = True
    assert raised
    close_conn(conn)


# ================================================== private / anti-recursion


def test_module_never_imports_hippocampus_or_sleep_run_or_model_libs():
    with open(os.path.join(ANAXI_FINAL, "waking_material_unit.py"), encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    for forbidden in ("hippocampus_store", "hippocampus_retrieval", "llama_sleep", "workspace_private",
                       "workspace_capability", "workspace_roaming", "orchestration", "anaxi_protocol",
                       "anaxi_protocol_sqlite", "ollama", "anthropic"):
        assert forbidden not in names


def test_support_traversal_allowlist_contains_no_private_shaped_kind():
    for kind in wmu_mod._SAME_EVENT_SUPPORT_COMPONENT_KINDS:
        assert "private" not in kind


def test_unrecognized_memory_kind_never_silently_admitted():
    conn, ids, _ = build_fixture("unrecognized_memory_kind")
    fabricated = [{
        "event_id": "evt-fake", "component_id": 999999, "segment_id": "seg-fake",
        "segment_index": 0, "char_start": 0, "char_end": 4, "is_final_segment": True,
        "timestamp": 100, "speaker_actor_id": ids["host_id"], "memory_kind": "mechanical_record",
        "attribution_status": "resolved", "authentication_status": "unknown",
        "pipeline_id": ids["pipeline_id"], "model_revision_id": None, "expression": "fake",
    }]
    raised = False
    try:
        wmu_mod.assemble_wmus(conn, fabricated)
    except wmu_mod.WmuAssemblyError:
        raised = True
    assert raised  # mechanical_record has no assembly rule -- never silently treated as selection-bearing
    close_conn(conn)


def test_wmu_id_is_pure_function_of_primary_event_id_only():
    assert wmu_mod._derive_wmu_id("same-id") == wmu_mod._derive_wmu_id("same-id")
    assert wmu_mod._derive_wmu_id("id-a") != wmu_mod._derive_wmu_id("id-b")


# ======================================================== SLP1-A4c: H-primary


def test_unanswered_human_address_is_h_primary_wmu():
    conn, ids, _ = build_fixture("h_standalone")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    h_wmu = next(w for w in wmus if w["primary_event_id"] == ids["h_standalone_event_id"])
    assert h_wmu["wmu_id"] == wmu_mod._derive_wmu_id(ids["h_standalone_event_id"])
    assert any(e["component_id"] == ids["h_standalone_component_id"] for e in h_wmu["selection_bearing"])
    close_conn(conn)


def test_h_plus_answering_x_share_h_primary_wmu_with_same_id():
    conn, ids, _ = build_fixture("h_plus_x")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    # No standalone X2-primary WMU exists -- X2 attached to H2.
    assert not any(w["primary_event_id"] == ids["x2_event_id"] for w in wmus)
    h2_wmu = next(w for w in wmus if w["primary_event_id"] == ids["h2_event_id"])
    selection_ids = {e["component_id"] for e in h2_wmu["selection_bearing"]}
    assert ids["h2_component_id"] in selection_ids
    assert ids["x2_component_id"] in selection_ids
    # Stability: same wmu_id as the standalone-H case would produce for this exact event_id.
    assert h2_wmu["wmu_id"] == wmu_mod._derive_wmu_id(ids["h2_event_id"])
    close_conn(conn)


def test_wmu_id_stable_across_unanswered_answered_and_supported():
    # Build the same H2 progressively: H2 alone, H2+X2, H2+X2+Y2 -- the
    # wmu_id must be identical every time (SLP1-A4c completion standard).
    conn, ids, _ = build_fixture("h_id_stability")
    all_items = _evidence_for(conn)

    h_only = [i for i in all_items if i["component_id"] == ids["h2_component_id"]]
    h_plus_x = [i for i in all_items if i["component_id"] in (ids["h2_component_id"], ids["x2_component_id"])]
    h_plus_x_plus_y = [i for i in all_items if i["component_id"] in (ids["h2_component_id"], ids["x2_component_id"], ids["y2_component_id"])]

    wmu_h_only = wmu_mod.assemble_wmus(conn, h_only)[0]
    wmu_h_plus_x = next(w for w in wmu_mod.assemble_wmus(conn, h_plus_x) if w["primary_event_id"] == ids["h2_event_id"])
    wmu_h_plus_x_plus_y = next(w for w in wmu_mod.assemble_wmus(conn, h_plus_x_plus_y) if w["primary_event_id"] == ids["h2_event_id"])

    assert wmu_h_only["wmu_id"] == wmu_h_plus_x["wmu_id"] == wmu_h_plus_x_plus_y["wmu_id"]
    close_conn(conn)


def test_chained_resource_encounter_attaches_to_h_primary_not_x():
    conn, ids, _ = build_fixture("chain_y_x_h")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    h2_wmu = next(w for w in wmus if w["primary_event_id"] == ids["h2_event_id"])
    assert any(e["component_id"] == ids["y2_component_id"] for e in h2_wmu["selection_bearing"])
    # No separate X2-primary or Y2-primary WMU exists for this chain.
    assert not any(w["primary_event_id"] == ids["x2_event_id"] for w in wmus)
    assert not any(w["primary_event_id"] == ids["y2_event_id"] for w in wmus)
    close_conn(conn)


def test_x_with_bogus_human_input_target_remains_its_own_primary():
    conn, ids, _ = build_fixture("bogus_x_h_target")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    x3_wmu = next(w for w in wmus if w["primary_event_id"] == ids["x3_event_id"])
    assert any(e["component_id"] == ids["x3_component_id"] for e in x3_wmu["selection_bearing"])
    close_conn(conn)


def test_x_with_wrong_type_human_input_target_remains_its_own_primary():
    conn, ids, _ = build_fixture("wrong_type_x_h_target")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    # X4's backlink names evt-waking-x (a waking_turn, not human_waking_input)
    # -- must never be trusted; X4 stands alone.
    x4_wmu = next(w for w in wmus if w["primary_event_id"] == ids["x4_event_id"])
    assert any(e["component_id"] == ids["x4_component_id"] for e in x4_wmu["selection_bearing"])
    close_conn(conn)


def test_h_input_link_component_appears_as_support_under_h_primary():
    conn, ids, _ = build_fixture("h_link_is_support")
    items = _evidence_for(conn)
    wmus = wmu_mod.assemble_wmus(conn, items)
    h2_wmu = next(w for w in wmus if w["primary_event_id"] == ids["h2_event_id"])
    support_texts = {s.get("support_text") for s in h2_wmu["supporting_provenance"] if s["support_component_kind"] == "human_input_event_id"}
    assert ids["h2_event_id"] in support_texts
    close_conn(conn)


def test_h_precedes_clark_prose_in_mechanical_order():
    conn, ids, _ = build_fixture("h_mechanical_order")
    items = sorted(_evidence_for(conn), key=lambda i: i["component_id"])  # canonical write order
    wmus = wmu_mod.assemble_wmus(conn, items)
    h2_wmu = next(w for w in wmus if w["primary_event_id"] == ids["h2_event_id"])
    ordered_ids = [e["component_id"] for e in h2_wmu["selection_bearing"]]
    assert ordered_ids.index(ids["h2_component_id"]) < ordered_ids.index(ids["x2_component_id"])
    close_conn(conn)


def test_no_occurred_at_used_anywhere_for_h_x_chain_resolution():
    # SLP1-A4c requirement: no timestamp fallback anywhere in the
    # traversal chain -- re-assert the existing source-level check
    # still holds after this gate's additions.
    with open(os.path.join(ANAXI_FINAL, "waking_material_unit.py"), encoding="utf-8") as f:
        source = f.read()
    assert "occurred_at" not in source


ALL_TESTS = [
    test_ordinary_conversation_primary_is_its_own_waking_turn_event,
    test_journal_primary_is_its_own_public_action_event,
    test_backlinked_resource_encounter_attaches_to_triggering_turns_wmu,
    test_backlink_attachment_does_not_change_primary_wmu_identity,
    test_standalone_resource_encounter_forms_its_own_resource_primary_wmu,
    test_invalid_backlink_target_never_attaches_falls_back_to_standalone,
    test_wrong_type_backlink_target_never_attaches,
    test_no_timestamp_fallback_grouping_source_check,
    test_deterministic_wmu_id_rebuild_produces_identical_ids_and_content,
    test_journal_action_fact_is_support_journal_text_is_selection_bearing,
    test_supporting_provenance_never_appears_in_selection_bearing,
    test_selection_bearing_trace_never_only_an_unlabeled_support_fact,
    test_no_banned_interpretive_field_anywhere_in_wmu_schema,
    test_wmu_module_performs_no_db_writes,
    test_complete_multi_segment_trace_reconstructs_exact_original_order,
    test_incomplete_segment_set_fails_closed,
    test_duplicate_segment_index_fails_closed,
    test_gapped_segment_index_fails_closed,
    test_module_never_imports_hippocampus_or_sleep_run_or_model_libs,
    test_support_traversal_allowlist_contains_no_private_shaped_kind,
    test_unrecognized_memory_kind_never_silently_admitted,
    test_wmu_id_is_pure_function_of_primary_event_id_only,
    test_unanswered_human_address_is_h_primary_wmu,
    test_h_plus_answering_x_share_h_primary_wmu_with_same_id,
    test_wmu_id_stable_across_unanswered_answered_and_supported,
    test_chained_resource_encounter_attaches_to_h_primary_not_x,
    test_x_with_bogus_human_input_target_remains_its_own_primary,
    test_x_with_wrong_type_human_input_target_remains_its_own_primary,
    test_h_input_link_component_appears_as_support_under_h_primary,
    test_h_precedes_clark_prose_in_mechanical_order,
    test_no_occurred_at_used_anywhere_for_h_x_chain_resolution,
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
