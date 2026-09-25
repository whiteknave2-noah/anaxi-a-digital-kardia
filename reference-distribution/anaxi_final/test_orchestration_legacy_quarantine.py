"""SLP1-S1 acceptance tests for orchestration.py's legacy waking
quarantine (spec section 7). No model call anywhere in this file.
"""
import os
import shutil
import sys
import tempfile
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import orchestration as orch_mod

TEST_ROOT = tempfile.mkdtemp(prefix="slp1s1_quarantine_test_")


def test_empty_legacy_memory_renders_nothing():
    assert orch_mod.render_legacy_memory_quarantine("") == ""
    assert orch_mod.render_legacy_memory_quarantine(None) == ""
    assert orch_mod.render_legacy_memory_quarantine("   ") == ""


def test_nonempty_legacy_memory_gets_wrapped_with_all_three_labels():
    rendered = orch_mod.render_legacy_memory_quarantine("Clark once liked jazz.")
    assert "LEGACY" in rendered
    assert "UNTYPED" in rendered
    assert "NON-AUTHORITATIVE" in rendered
    assert "Clark once liked jazz." in rendered


def test_wrapper_never_guesses_original_speaker_type():
    # "human expression"/"Clark expression"/etc may appear only inside
    # the disclaimer explaining that the type is NOT known -- proven
    # here by requiring the disclaimer framing itself to be present,
    # rather than asserting those phrases are absent (they legitimately
    # appear, just never as a positive claim).
    rendered = orch_mod.render_legacy_memory_quarantine("Some legacy content.")
    assert "not known whether" in rendered or "not mechanically preserved" in rendered


def test_content_is_never_treated_as_established_fact_language():
    rendered = orch_mod.render_legacy_memory_quarantine("Some legacy content.")
    assert "must not be treated as mechanically established fact" in rendered


def test_wrapper_is_deterministic_pure_function():
    a = orch_mod.render_legacy_memory_quarantine("same input")
    b = orch_mod.render_legacy_memory_quarantine("same input")
    assert a == b


def test_prepare_context_routes_legacy_memory_through_the_quarantine_wrapper():
    # Integration proof: prepare_context() must call
    # render_legacy_memory_quarantine() on whatever retrieve_waking_
    # context() returns, before it becomes part of memory_context --
    # never concatenate legacy text raw. Exercises only the host-side
    # sequencing; no model call and no real DB is touched (mind and
    # hippocampus calls are stubbed).
    class FakeMind:
        def retrieve_waking_context(self, user_id, user_prompt):
            return "MARKER_LEGACY_TEXT_UNIQUE_9f3a"

        def get_current_kardia(self, user_id):
            return {"moral_valve": "x", "volitional_channel": "x", "affective_stance": "x", "aesthetic_valve": "x"}

    class FakeHippocampusPaths:
        hippocampus_db_path = os.path.join(TEST_ROOT, "fake_hippocampus.db")

    orchestrator = orch_mod.AnaxiOrchestrator.__new__(orch_mod.AnaxiOrchestrator)
    orchestrator.mind = FakeMind()
    orchestrator.hippocampus_paths = FakeHippocampusPaths()

    real_sync = orch_mod.hippocampus_store.sync_hippocampus
    real_retrieve = orch_mod.hippocampus_retrieval.retrieve_hippocampal_context
    real_render = orch_mod.hippocampus_retrieval.render_hippocampal_context
    orch_mod.hippocampus_store.sync_hippocampus = lambda *a, **k: None
    orch_mod.hippocampus_retrieval.retrieve_hippocampal_context = lambda *a, **k: None
    orch_mod.hippocampus_retrieval.render_hippocampal_context = lambda *a, **k: ""
    try:
        prepared = orchestrator.prepare_context("test-user", "hello")
    finally:
        orch_mod.hippocampus_store.sync_hippocampus = real_sync
        orch_mod.hippocampus_retrieval.retrieve_hippocampal_context = real_retrieve
        orch_mod.hippocampus_retrieval.render_hippocampal_context = real_render

    memory = prepared["memory_context"]
    assert "MARKER_LEGACY_TEXT_UNIQUE_9f3a" in memory
    assert "LEGACY" in memory and "UNTYPED" in memory and "NON-AUTHORITATIVE" in memory
    # The marker must appear strictly after the quarantine header --
    # never rendered before it, never as a bare/unwrapped leading string.
    assert memory.index("LEGACY_MEMORY_CONTEXT_V1") < memory.index("MARKER_LEGACY_TEXT_UNIQUE_9f3a")


def test_prepare_context_renders_nothing_for_empty_legacy_memory():
    class FakeMind:
        def retrieve_waking_context(self, user_id, user_prompt):
            return ""

        def get_current_kardia(self, user_id):
            return {"moral_valve": "x", "volitional_channel": "x", "affective_stance": "x", "aesthetic_valve": "x"}

    class FakeHippocampusPaths:
        hippocampus_db_path = os.path.join(TEST_ROOT, "fake_hippocampus2.db")

    orchestrator = orch_mod.AnaxiOrchestrator.__new__(orch_mod.AnaxiOrchestrator)
    orchestrator.mind = FakeMind()
    orchestrator.hippocampus_paths = FakeHippocampusPaths()

    real_sync = orch_mod.hippocampus_store.sync_hippocampus
    real_retrieve = orch_mod.hippocampus_retrieval.retrieve_hippocampal_context
    real_render = orch_mod.hippocampus_retrieval.render_hippocampal_context
    orch_mod.hippocampus_store.sync_hippocampus = lambda *a, **k: None
    orch_mod.hippocampus_retrieval.retrieve_hippocampal_context = lambda *a, **k: None
    orch_mod.hippocampus_retrieval.render_hippocampal_context = lambda *a, **k: ""
    try:
        prepared = orchestrator.prepare_context("test-user", "hello")
    finally:
        orch_mod.hippocampus_store.sync_hippocampus = real_sync
        orch_mod.hippocampus_retrieval.retrieve_hippocampal_context = real_retrieve
        orch_mod.hippocampus_retrieval.render_hippocampal_context = real_render

    assert "LEGACY_MEMORY_CONTEXT_V1" not in prepared["memory_context"]


ALL_TESTS = [
    test_empty_legacy_memory_renders_nothing,
    test_nonempty_legacy_memory_gets_wrapped_with_all_three_labels,
    test_wrapper_never_guesses_original_speaker_type,
    test_content_is_never_treated_as_established_fact_language,
    test_wrapper_is_deterministic_pure_function,
    test_prepare_context_routes_legacy_memory_through_the_quarantine_wrapper,
    test_prepare_context_renders_nothing_for_empty_legacy_memory,
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
