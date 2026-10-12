"""WSP2-P3 section 7: integration tests proving episode-context
delivery is acknowledged ONLY atomically with the successful native
waking-turn commit that received it. Reuses test_native_provenance_
integration.py's own established fixture pattern (seed_reference_data
against a fresh temp-dir DB) rather than inventing a second one."""
import os
import sqlite3
import sys
import tempfile
import time
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

from provenance_schema import create_provenance_db
from migrate_historical_data import build_pipeline_map, seed_reference_data
import native_provenance_writer as npw
import workspace_episode_provenance as wep

SYNTHETIC_MANIFEST = {
    "pipelines": {
        "llama": {"routing_constant_value": "synthetic_user"},
        "claude": {"routing_constant_value": "synthetic_user"},
    }
}
PIPELINE_KEY = None  # resolved per-fixture below, matches the real pipeline_map shape


def fresh_fixture():
    global PIPELINE_KEY
    tmp_root = tempfile.mkdtemp(prefix="wep_delivery_test_")
    data_dir = os.path.join(tmp_root, "data")
    os.makedirs(data_dir)
    create_provenance_db(os.path.join(data_dir, "anaxi_provenance.db")).close()
    pipeline_map = build_pipeline_map(SYNTHETIC_MANIFEST)
    PIPELINE_KEY = pipeline_map["llama"]["pipeline_key"]
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    conn.execute("PRAGMA foreign_keys = ON;")
    now = int(time.time())
    seed_reference_data(conn, pipeline_map, now)
    conn.close()
    staging_path = os.path.join(data_dir, "native_turn_staging.jsonl")
    return data_dir, staging_path, now


def _make_pending_episode(data_dir, clark_actor_id):
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(data_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_episode_ended(data_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1001, termination_class=wep.TERMINATION_HUMAN_STOP)
    return run_id


def test_episode_pending_before_any_waking_turn():
    data_dir, staging_path, now = fresh_fixture()
    from provenance_schema import derive_stable_id
    clark_actor_id = derive_stable_id("actor", "clark")
    run_id = _make_pending_episode(data_dir, clark_actor_id)
    assert run_id in wep.find_pending_episode_run_ids(data_dir)


def test_successful_waking_turn_acknowledges_atomically():
    data_dir, staging_path, now = fresh_fixture()
    from provenance_schema import derive_stable_id
    clark_actor_id = derive_stable_id("actor", "clark")
    run_id = _make_pending_episode(data_dir, clark_actor_id)
    assert run_id in wep.find_pending_episode_run_ids(data_dir)

    npw.stage_and_record_native_waking_turn(
        data_dir, staging_path, session_id="sess-1", session_started_at=now, user_id="nate",
        prompt="Good morning", bounded_clause="", clark_prose="Good morning to you too.",
        kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=PIPELINE_KEY,
        artifact_pass_ran=False, occurred_at=now, delivered_episode_run_id=run_id,
    )
    assert run_id not in wep.find_pending_episode_run_ids(data_dir)


def test_waking_turn_without_delivered_episode_leaves_episode_pending():
    # No episode context was supplied to this turn (delivered_episode_
    # run_id omitted) -- the pending episode must remain untouched.
    data_dir, staging_path, now = fresh_fixture()
    from provenance_schema import derive_stable_id
    clark_actor_id = derive_stable_id("actor", "clark")
    run_id = _make_pending_episode(data_dir, clark_actor_id)

    npw.stage_and_record_native_waking_turn(
        data_dir, staging_path, session_id="sess-1", session_started_at=now, user_id="nate",
        prompt="unrelated turn", bounded_clause="", clark_prose="An ordinary reply.",
        kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=PIPELINE_KEY,
        artifact_pass_ran=False, occurred_at=now,
    )
    assert run_id in wep.find_pending_episode_run_ids(data_dir)


def test_no_call_means_no_acknowledgment():
    # Simulates "Pass-1 failed / Pass-2 failed / persistence never
    # ran" -- stage_and_record_native_waking_turn() is simply never
    # called. The episode must remain exactly as pending as it started.
    data_dir, staging_path, now = fresh_fixture()
    from provenance_schema import derive_stable_id
    clark_actor_id = derive_stable_id("actor", "clark")
    run_id = _make_pending_episode(data_dir, clark_actor_id)
    assert run_id in wep.find_pending_episode_run_ids(data_dir)
    assert run_id in wep.find_pending_episode_run_ids(data_dir)  # stable across repeated read-only queries


def test_older_pending_episode_survives_a_different_episodes_delivery():
    data_dir, staging_path, now = fresh_fixture()
    from provenance_schema import derive_stable_id
    clark_actor_id = derive_stable_id("actor", "clark")
    older_run_id = _make_pending_episode(data_dir, clark_actor_id)
    newer_run_id = _make_pending_episode(data_dir, clark_actor_id)

    npw.stage_and_record_native_waking_turn(
        data_dir, staging_path, session_id="sess-1", session_started_at=now, user_id="nate",
        prompt="Good morning", bounded_clause="", clark_prose="Good morning.",
        kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=PIPELINE_KEY,
        artifact_pass_ran=False, occurred_at=now, delivered_episode_run_id=newer_run_id,
    )
    pending = wep.find_pending_episode_run_ids(data_dir)
    assert newer_run_id not in pending
    assert older_run_id in pending  # not silently discarded


def test_delivery_component_bare_run_id_no_json_wrapping():
    # find_pending_episode_run_ids() matches component_text against a
    # bare run_id string -- prove the delivery component is written
    # that way, not JSON-wrapped, or the whole mechanism silently breaks.
    data_dir, staging_path, now = fresh_fixture()
    from provenance_schema import derive_stable_id
    clark_actor_id = derive_stable_id("actor", "clark")
    run_id = _make_pending_episode(data_dir, clark_actor_id)
    npw.stage_and_record_native_waking_turn(
        data_dir, staging_path, session_id="sess-1", session_started_at=now, user_id="nate",
        prompt="p", bounded_clause="", clark_prose="c",
        kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=PIPELINE_KEY,
        artifact_pass_ran=False, occurred_at=now, delivered_episode_run_id=run_id,
    )
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    row = conn.execute(
        "SELECT component_text FROM event_components WHERE component_kind = 'episode_context_delivered'"
    ).fetchone()
    conn.close()
    assert row[0] == run_id


ALL_TESTS = [
    test_episode_pending_before_any_waking_turn,
    test_successful_waking_turn_acknowledges_atomically,
    test_waking_turn_without_delivered_episode_leaves_episode_pending,
    test_no_call_means_no_acknowledgment,
    test_older_pending_episode_survives_a_different_episodes_delivery,
    test_delivery_component_bare_run_id_no_json_wrapping,
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
