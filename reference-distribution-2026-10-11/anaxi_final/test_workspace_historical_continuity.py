"""WSP2-P4-H1 acceptance tests: historical PUBLIC workspace continuity
through the existing hippocampal indexing/retrieval pathway. Fully
synthetic/temp canonical provenance fixtures -- no production DB, no
production private material, no model calls.

Covers the synthetic acceptance matrix (spec section 15): historical
conversation + public-Space items retrievable together, source/pathway
provenance preserved, metadata-only records never fabricate content,
private/telemetry component kinds never eligible, and rebuild stays
deterministic/idempotent.
"""
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

import sqlite3

from provenance_schema import create_provenance_db
from relational_history import RelationalHistory
from migrate_historical_data import _add_columns_if_missing, RELATIONAL_EVENTS_ADDITIVE_COLUMNS
import hippocampus_store as hs
import hippocampus_retrieval as hr


def _seed_actors(conn):
    now = int(time.time())
    host_actor_id = "actor-host-1"
    clark_actor_id = "actor-clark-1"
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', ?)", (host_actor_id, now))
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', ?)", (clark_actor_id, now))
    conn.execute("INSERT INTO actor_clark_agent (actor_id, canonical_key) VALUES (?, 'clark')", (clark_actor_id,))
    return host_actor_id, clark_actor_id


def _insert_event(conn, event_id, pipeline_id, event_type, occurred_at):
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        "auth_context_id, input_source_ref, occurred_at, record_created_at) "
        "VALUES (?, ?, ?, 'known', NULL, NULL, ?, ?, ?)",
        (event_id, event_type, pipeline_id, event_id, occurred_at, occurred_at),
    )


def _insert_component(conn, event_id, sequence, creator_actor_id, component_kind, text):
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256) VALUES (?, ?, ?, ?, 'resolved', ?, ?)",
        (event_id, sequence, creator_actor_id, component_kind, text, content_sha256),
    )


def _build_fixture(tmp_dir):
    """A minimal but complete fixture: one ordinary conversational
    item, one genuine public resource-encounter item, one public
    roaming action-metadata-only item, one public roaming external-
    result (content) item, one public roaming journal-text item, one
    ineligible internal-telemetry item (roaming_wait_fact), and one
    unsupported/private-shaped component kind that must never be
    ingested."""
    prov_path = os.path.join(tmp_dir, "anaxi_provenance.db")
    rel_path = os.path.join(tmp_dir, "anaxi_relational_llama.db")
    jsonl_path = os.path.join(tmp_dir, "anaxi_log.jsonl")
    hip_path = os.path.join(tmp_dir, "anaxi_hippocampus.db")

    conn = create_provenance_db(prov_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    pipeline_id = "pipe-llama-1"
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES (?, 'anaxi_orchestration_lineage_a', 'llama', 'test')", (pipeline_id,),
    )
    host_actor_id, clark_actor_id = _seed_actors(conn)

    # A: historical conversation item
    _insert_event(conn, "evt-conv", pipeline_id, "waking_turn", occurred_at=100)
    _insert_component(conn, "evt-conv", 0, clark_actor_id, "conversational_prose",
                       "I spent the afternoon thinking about the garden.")

    # B/E: historical public resource-encounter item -- actual content present
    _insert_event(conn, "evt-encounter", pipeline_id, "workspace_resource_encounter", occurred_at=200)
    encounter_fact = json.dumps({
        "resource_class": "library", "relative_path": "notes.txt", "modality": "text",
        "delivered_portion": "chars 0-400", "content_sha256": "abc123",
        "source_action": "read", "target_model_pathway": "gemma4:e4b", "genuinely_delivered": True,
    }, sort_keys=True)
    _insert_component(conn, "evt-encounter", 0, host_actor_id, "resource_encounter_fact", encounter_fact)

    # F: public roaming action -- METADATA ONLY, no fabricated content
    _insert_event(conn, "evt-action", pipeline_id, "workspace_roaming_public_action", occurred_at=300)
    action_fact = json.dumps({
        "resource_class": "photographs", "action": "list", "relative_path": None,
        "success": True, "action_id_backlink": "wsaction-xyz",
    }, sort_keys=True)
    _insert_component(conn, "evt-action", 0, host_actor_id, "roaming_action_fact", action_fact)

    # public roaming external result -- genuine bounded delivered content
    _insert_event(conn, "evt-external", pipeline_id, "workspace_roaming_public_action", occurred_at=400)
    _insert_component(conn, "evt-external", 0, host_actor_id, "roaming_external_result",
                       "Bounded excerpt: the garden log mentions tomatoes and basil.")

    # public roaming journal text -- Clark-authored content
    _insert_event(conn, "evt-journal", pipeline_id, "workspace_roaming_public_action", occurred_at=500)
    _insert_component(conn, "evt-journal", 0, clark_actor_id, "roaming_journal_text",
                       "Wrote a short note about the garden today.")

    # H: internal telemetry -- must never be eligible
    _insert_event(conn, "evt-telemetry", pipeline_id, "workspace_roaming_public_wait", occurred_at=600)
    _insert_component(conn, "evt-telemetry", 0, host_actor_id, "roaming_wait_fact", json.dumps({"wait_minutes": 5}))

    # G: private-shaped component kind -- must never be eligible (not in
    # the allowlist at all; workspace_private.py never writes canonical
    # components in production, but this proves the allowlist itself
    # would reject it even if it somehow appeared).
    _insert_event(conn, "evt-private-shaped", pipeline_id, "workspace_private_action", occurred_at=700)
    _insert_component(conn, "evt-private-shaped", 0, host_actor_id, "private_action_fact", json.dumps({"path": "secret.txt"}))

    conn.commit()
    conn.close()

    RelationalHistory(rel_path).close()
    rel_conn = sqlite3.connect(rel_path)
    _add_columns_if_missing(rel_conn, "relational_events", RELATIONAL_EVENTS_ADDITIVE_COLUMNS)
    rel_conn.commit()
    rel_conn.close()
    with open(jsonl_path, "w", encoding="utf-8"):
        pass

    hs.create_hippocampus_db(hip_path)
    paths = hs.HippocampusPaths(
        provenance_db_path=prov_path, relational_db_path=rel_path,
        historical_jsonl_path=jsonl_path, hippocampus_db_path=hip_path,
    )
    return paths, hip_path


# ---------------------------------------------------------------- A/B/D/E/F


def test_conversation_and_public_workspace_items_both_indexed():
    tmp_dir = tempfile.mkdtemp(prefix="wsp2p4h1_")
    try:
        paths, hip_path = _build_fixture(tmp_dir)
        result = hs.sync_hippocampus(hip_path, paths)
        inserted_kinds = {row["component_kind"] for row in _fetch_all_items(hip_path)}
        assert "conversational_prose" in inserted_kinds  # A
        assert "resource_encounter_fact" in inserted_kinds  # B/E
        assert "roaming_action_fact" in inserted_kinds
        assert "roaming_external_result" in inserted_kinds
        assert "roaming_journal_text" in inserted_kinds
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_mixed_query_returns_conversation_and_public_space_together():
    tmp_dir = tempfile.mkdtemp(prefix="wsp2p4h1_")
    try:
        paths, hip_path = _build_fixture(tmp_dir)
        hs.sync_hippocampus(hip_path, paths)
        result = hr.retrieve_hippocampal_context(hip_path, "garden")
        source_stores = {item.component_kind for item in result.items}
        # "garden" appears in BOTH the conversational item and the
        # roaming external-result/journal items -- one query, one
        # ranked result set, no accessibility wall between them.
        assert "conversational_prose" in source_stores
        assert ("roaming_external_result" in source_stores or "roaming_journal_text" in source_stores)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_public_workspace_item_retains_component_kind_provenance_in_render():
    tmp_dir = tempfile.mkdtemp(prefix="wsp2p4h1_")
    try:
        paths, hip_path = _build_fixture(tmp_dir)
        hs.sync_hippocampus(hip_path, paths)
        result = hr.retrieve_hippocampal_context(hip_path, "library")
        rendered = hr.render_hippocampal_context(result)
        lines = [json.loads(ln) for ln in rendered.splitlines() if ln.startswith("{")]
        matching = [p for p in lines if p.get("component_kind") == "resource_encounter_fact"]
        assert len(matching) == 1
        # Neutral, mechanical availability language only.
        assert "you remember" not in rendered.lower()
        assert "mattered" not in rendered.lower()
        assert "you learned" not in rendered.lower()
        assert "you felt" not in rendered.lower()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_metadata_only_record_exposes_only_metadata_no_fabrication():
    tmp_dir = tempfile.mkdtemp(prefix="wsp2p4h1_")
    try:
        paths, hip_path = _build_fixture(tmp_dir)
        hs.sync_hippocampus(hip_path, paths)
        result = hr.retrieve_hippocampal_context(hip_path, "photographs")
        action_items = [i for i in result.items if i.component_kind == "roaming_action_fact"]
        assert len(action_items) == 1
        payload = json.loads(action_items[0].content)
        assert set(payload.keys()) == {"resource_class", "action", "relative_path", "success", "action_id_backlink"}
        # No invented content field beyond what was actually persisted.
        assert "content_text" not in payload and "description" not in payload
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ------------------------------------------------------------------- G/H


def test_private_shaped_component_kind_never_eligible():
    tmp_dir = tempfile.mkdtemp(prefix="wsp2p4h1_")
    try:
        paths, hip_path = _build_fixture(tmp_dir)
        result = hs.sync_hippocampus(hip_path, paths)
        unsupported_kinds = {u["component_kind"] for u in result.unsupported_components}
        assert "private_action_fact" in unsupported_kinds
        all_kinds = {row["component_kind"] for row in _fetch_all_items(hip_path)}
        assert "private_action_fact" not in all_kinds
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_internal_telemetry_component_kind_never_eligible():
    tmp_dir = tempfile.mkdtemp(prefix="wsp2p4h1_")
    try:
        paths, hip_path = _build_fixture(tmp_dir)
        result = hs.sync_hippocampus(hip_path, paths)
        unsupported_kinds = {u["component_kind"] for u in result.unsupported_components}
        assert "roaming_wait_fact" in unsupported_kinds
        all_kinds = {row["component_kind"] for row in _fetch_all_items(hip_path)}
        assert "roaming_wait_fact" not in all_kinds
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------- I/L


def test_rebuild_deterministic_and_idempotent_with_new_kinds():
    tmp_dir = tempfile.mkdtemp(prefix="wsp2p4h1_")
    try:
        paths, hip_path = _build_fixture(tmp_dir)
        target1 = os.path.join(tmp_dir, "rebuild1.db")
        target2 = os.path.join(tmp_dir, "rebuild2.db")
        hs.rebuild_hippocampus(target1, paths)
        hs.rebuild_hippocampus(target2, paths)
        rows1 = hs.fetch_logical_rows(target1)
        rows2 = hs.fetch_logical_rows(target2)
        assert rows1 == rows2
        assert len(rows1) >= 5  # every eligible kind present, none duplicated/lost
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_sync_never_mutates_canonical_provenance():
    tmp_dir = tempfile.mkdtemp(prefix="wsp2p4h1_")
    try:
        paths, hip_path = _build_fixture(tmp_dir)
        with open(paths.provenance_db_path, "rb") as f:
            before = f.read()
        hs.sync_hippocampus(hip_path, paths)
        with open(paths.provenance_db_path, "rb") as f:
            after = f.read()
        assert before == after
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ------------------------------------------------------------------- helper


def _fetch_all_items(hip_path):
    import sqlite3
    conn = sqlite3.connect(hip_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM hippocampal_items;").fetchall()
    finally:
        conn.close()


ALL_TESTS = [
    test_conversation_and_public_workspace_items_both_indexed,
    test_mixed_query_returns_conversation_and_public_space_together,
    test_public_workspace_item_retains_component_kind_provenance_in_render,
    test_metadata_only_record_exposes_only_metadata_no_fabrication,
    test_private_shaped_component_kind_never_eligible,
    test_internal_telemetry_component_kind_never_eligible,
    test_rebuild_deterministic_and_idempotent_with_new_kinds,
    test_sync_never_mutates_canonical_provenance,
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
