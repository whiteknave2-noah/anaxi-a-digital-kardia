"""Tests for workspace_episode_context.py -- the derived, bounded
waking renderer. Pure read + string construction against a real,
schema-only provenance DB seeded via workspace_episode_provenance's
own recorder functions -- no model call, no orchestration fake needed."""
import os
import sys
import tempfile
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import provenance_schema
import workspace_episode_provenance as wep
import workspace_episode_context as wec

PIPELINE_KEY = "anaxi_orchestration_lineage_a"


def fresh_db():
    test_dir = tempfile.mkdtemp(prefix="wec_test_")
    db_path = os.path.join(test_dir, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
        "VALUES ('pipe-test-1', ?, 'llama', 'test')", (PIPELINE_KEY,),
    )
    clark_actor_id = provenance_schema.derive_stable_id("actor", "clark")
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', 1000)", (clark_actor_id,))
    host_actor_id = wep.host_actor_id()
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', 1000)", (host_actor_id,))
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    human_actor_id = "human-actor-test-canonical"
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', ?, 1000)",
        (human_actor_id, human_actor_id),
    )
    conn.commit()
    conn.close()
    return test_dir, clark_actor_id


def test_no_run_id_returns_none():
    test_dir, clark_actor_id = fresh_db()
    assert wec.build_workspace_episode_context(test_dir, None) is None


def test_unknown_run_id_returns_none():
    test_dir, clark_actor_id = fresh_db()
    assert wec.build_workspace_episode_context(test_dir, "wrun-does-not-exist") is None


def test_full_episode_renders_header_span_counts_handoff_termination():
    test_dir, clark_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_episode_handoff(
        test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1001,
        note="Revisit the book conversation.",
        source_excerpts=[{"handle": "d1", "author": "human", "text": "Would you like to roam while I sleep?"}],
        model_revision_id=None,
    )
    wep.record_public_action(
        test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1002,
        resource_class="journal", action="append", relative_path="e.json", success=True,
        action_id_backlink="wsaction-1", model_revision_id=None, journal_text="Noticed the quiet.",
    )
    wep.record_public_wait(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1003, wait_minutes=15, model_revision_id=None)
    wep.record_episode_ended(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1900, termination_class=wep.TERMINATION_HUMAN_STOP)

    text = wec.build_workspace_episode_context(test_dir, run_id)
    assert wec.HEADER in text
    assert "1000 to 1900" in text
    assert "Public actions: 1. Public waits: 1." in text
    assert "Revisit the book conversation." in text
    assert "Would you like to roam while I sleep?" in text
    assert "human_stop" in text
    assert "journal.append: ok" in text
    assert "wait 15m" in text


def test_never_exceeds_max_total_chars():
    test_dir, clark_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    for i in range(40):  # far more than any realistic episode -- forces truncation
        wep.record_public_action(
            test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000 + i,
            resource_class="journal", action="append", relative_path=f"e{i}.json", success=True,
            action_id_backlink=f"wsaction-{i}", model_revision_id=None,
            journal_text="Noticed the quiet. " * 5,
        )
    wep.record_episode_ended(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=5000, termination_class=wep.TERMINATION_HUMAN_STOP)
    text = wec.build_workspace_episode_context(test_dir, run_id)
    assert len(text) <= wec.MAX_TOTAL_CHARS
    assert wec.TRUNCATION_MARKER in text


def test_truncation_is_deterministic_not_semantic():
    # Two runs with identical inputs must produce byte-identical
    # output -- proves no randomness/heuristic "importance" selection.
    test_dir, clark_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    for i in range(20):
        wep.record_public_action(
            test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000 + i,
            resource_class="journal", action="append", relative_path=f"e{i}.json", success=True,
            action_id_backlink=f"wsaction-{i}", model_revision_id=None, journal_text="text " * 10,
        )
    wep.record_episode_ended(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=2000, termination_class=wep.TERMINATION_HUMAN_STOP)
    text_a = wec.build_workspace_episode_context(test_dir, run_id)
    text_b = wec.build_workspace_episode_context(test_dir, run_id)
    assert text_a == text_b


def test_no_private_wording_ever_appears():
    test_dir, clark_actor_id = fresh_db()
    run_id = wep.generate_episode_run_id()
    wep.record_episode_started(test_dir, run_id=run_id, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_episode_ended(test_dir, run_id=run_id, pipeline_key=PIPELINE_KEY, occurred_at=1001, termination_class=wep.TERMINATION_CONTROL_VALIDATION_FAILURE)
    text = wec.build_workspace_episode_context(test_dir, run_id)
    for forbidden in ("private", "workspace/private"):
        assert forbidden not in text.lower()


def test_most_recent_pending_episode_selects_newest():
    test_dir, clark_actor_id = fresh_db()
    older = wep.generate_episode_run_id()
    newer = wep.generate_episode_run_id()
    wep.record_episode_started(test_dir, run_id=older, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=1000)
    wep.record_episode_ended(test_dir, run_id=older, pipeline_key=PIPELINE_KEY, occurred_at=1001, termination_class=wep.TERMINATION_HUMAN_STOP)
    wep.record_episode_started(test_dir, run_id=newer, clark_actor_id=clark_actor_id, pipeline_key=PIPELINE_KEY, occurred_at=2000)
    wep.record_episode_ended(test_dir, run_id=newer, pipeline_key=PIPELINE_KEY, occurred_at=2001, termination_class=wep.TERMINATION_HUMAN_STOP)
    assert wec.most_recent_pending_episode_run_id(test_dir) == newer


ALL_TESTS = [
    test_no_run_id_returns_none,
    test_unknown_run_id_returns_none,
    test_full_episode_renders_header_span_counts_handoff_termination,
    test_never_exceeds_max_total_chars,
    test_truncation_is_deterministic_not_semantic,
    test_no_private_wording_ever_appears,
    test_most_recent_pending_episode_selects_newest,
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
