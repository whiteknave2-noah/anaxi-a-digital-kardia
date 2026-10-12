"""OWC2-S1 acceptance tests. Zero real prepare_context()/model calls.
Every test operates on a temp copy of anaxi_provenance.db's schema plus
a temp staging JSONL file, written through the real
native_turn_staging.append_staging_entry() (never hand-rolled JSON) so
staging_id integrity verification is exercised honestly.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import uuid

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

from provenance_schema import create_provenance_db, derive_stable_id
from native_turn_staging import append_staging_entry
from session_dialogue_window import (
    build_session_dialogue_window,
    collect_session_dialogue_pairs,
    insert_dialogue_window,
    select_bounded_dialogue_pairs,
    CLARK_ACTOR_ID,
    DEFAULT_MAX_TURNS,
    DEFAULT_MAX_DIALOGUE_CHARS,
)

TEST_DIR = tempfile.mkdtemp(prefix="owc2_test_")
SOURCE_DB = os.path.join(ANAXI_FINAL, "anaxi_provenance.db")


def fresh_copy_db(name):
    path = os.path.join(TEST_DIR, f"{name}.db")
    conn = create_provenance_db(path)
    conn.execute(
        "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
        "VALUES (?, 'clark_agent', 'clark', 1000)", (CLARK_ACTOR_ID,),
    )
    conn.commit()
    conn.close()
    return path


def open_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def fresh_staging_path(name):
    return os.path.join(TEST_DIR, f"{name}_staging.jsonl")


def seed_session(conn, session_id, started_at):
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, started_at) VALUES (?, ?)",
        (session_id, started_at),
    )
    conn.commit()


def seed_waking_turn(conn, staging_path, session_id, prompt, clark_text, occurred_at, event_id=None, write_canonical=True, write_staging=True):
    """Mirrors record_native_waking_turn()'s real INSERT shape for
    auth_contexts/events/event_components, and calls the real
    append_staging_entry() for the staging side -- never a hand-built
    fixture shortcut for either."""
    event_id = event_id or f"evt-{uuid.uuid4().hex[:12]}"
    auth_context_id = f"authctx-{uuid.uuid4().hex[:12]}"

    if write_canonical:
        conn.execute(
            "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
            "VALUES (?, ?, 'unknown', ?)",
            (auth_context_id, session_id, occurred_at),
        )
        conn.execute(
            "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
            "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
            "VALUES (?, 'waking_turn', NULL, 'unknown', NULL, ?, NULL, ?, ?)",
            (event_id, auth_context_id, occurred_at, occurred_at),
        )
        conn.execute(
            "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
            "authorship_resolution, component_text, content_sha256, model_revision_id, "
            "span_start, span_end) VALUES (?, 1, ?, 'conversational_prose', 'resolved', ?, ?, NULL, 0, ?)",
            (event_id, CLARK_ACTOR_ID, clark_text, hashlib.sha256(clark_text.encode("utf-8")).hexdigest(), len(clark_text)),
        )
        conn.commit()

    if write_staging:
        payload = {
            "session_id": session_id, "session_started_at": occurred_at, "event_id": event_id,
            "auth_context_id": auth_context_id, "user_id": "nate", "prompt": prompt,
            "bounded_clause": "", "clark_prose": clark_text, "kardia": {}, "controls": {},
            "waking_model_tag": "gemma4:e4b", "pipeline_key": "test_pipeline",
            "artifact_pass_ran": False, "occurred_at": occurred_at,
        }
        append_staging_entry(staging_path, payload)

    return event_id


# ------------------------------------------------------------ A: empty ----


def test_empty_session_produces_empty_window():
    db_path = fresh_copy_db("empty")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("empty")
    window = build_session_dialogue_window(conn, "session-empty", staging_path, max_turns=8)
    assert window == []

    prepared_messages = [
        {"role": "system", "content": "kardia+memory"},
        {"role": "user", "content": "current prompt"},
    ]
    final = insert_dialogue_window(prepared_messages, window)
    assert final == prepared_messages  # exactly today's current behavior


# --------------------------------------------------------- B: one pair ----


def test_one_prior_pair_referent_fixture():
    db_path = fresh_copy_db("one_pair")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("one_pair")
    session_id = "session-referent"
    seed_session(conn, session_id, 1000)
    seed_waking_turn(conn, staging_path, session_id, "What interests you?", "Human agency.", 1000)

    window = build_session_dialogue_window(conn, session_id, staging_path)
    assert window == [
        {"role": "user", "content": "What interests you?"},
        {"role": "assistant", "content": "Human agency."},
    ]

    prepared_messages = [
        {"role": "system", "content": "kardia+memory"},
        {"role": "user", "content": "Yeah? What about it?"},
    ]
    final = insert_dialogue_window(prepared_messages, window)
    assert final == [
        {"role": "system", "content": "kardia+memory"},
        {"role": "user", "content": "What interests you?"},
        {"role": "assistant", "content": "Human agency."},
        {"role": "user", "content": "Yeah? What about it?"},
    ]


# ------------------------------------------------------- C: multi-pair ----


def test_multiple_pairs_exact_chronological_interleaving():
    db_path = fresh_copy_db("multi")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("multi")
    session_id = "session-multi"
    seed_session(conn, session_id, 1000)
    turns = [
        ("Hey, what's up?", "Nothing pressing.", 1000),
        ("Coding all day.", "Coding takes focus.", 1010),
        ("You.", "I am an AI. I process information.", 1020),
    ]
    for prompt, reply, ts in turns:
        seed_waking_turn(conn, staging_path, session_id, prompt, reply, ts)

    window = build_session_dialogue_window(conn, session_id, staging_path)
    expected = []
    for prompt, reply, _ts in turns:
        expected.append({"role": "user", "content": prompt})
        expected.append({"role": "assistant", "content": reply})
    assert window == expected


# --------------------------------------------------------- D: 8-pair bound


def test_nine_pairs_oldest_dropped_newest_eight_retained():
    db_path = fresh_copy_db("bound")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("bound")
    session_id = "session-bound"
    seed_session(conn, session_id, 1000)
    for i in range(9):
        seed_waking_turn(conn, staging_path, session_id, f"prompt-{i}", f"reply-{i}", 1000 + i * 10)

    window = build_session_dialogue_window(conn, session_id, staging_path, max_turns=8)
    assert len(window) == 16  # 8 pairs * 2 messages
    assert window[0] == {"role": "user", "content": "prompt-1"}  # oldest (prompt-0) dropped
    assert window[1] == {"role": "assistant", "content": "reply-1"}
    assert window[-2] == {"role": "user", "content": "prompt-8"}
    assert window[-1] == {"role": "assistant", "content": "reply-8"}
    assert "prompt-0" not in [m["content"] for m in window]


# ---------------------------------------------------- E: session isolation


def test_session_isolation_no_cross_session_leakage():
    db_path = fresh_copy_db("isolation")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("isolation")
    seed_session(conn, "session-A", 1000)
    seed_session(conn, "session-B", 2000)
    seed_waking_turn(conn, staging_path, "session-A", "A prompt", "A reply", 1000)
    seed_waking_turn(conn, staging_path, "session-B", "B prompt", "B reply", 2000)

    window_a = build_session_dialogue_window(conn, "session-A", staging_path)
    window_b = build_session_dialogue_window(conn, "session-B", staging_path)
    assert window_a == [{"role": "user", "content": "A prompt"}, {"role": "assistant", "content": "A reply"}]
    assert window_b == [{"role": "user", "content": "B prompt"}, {"role": "assistant", "content": "B reply"}]

    window_new = build_session_dialogue_window(conn, "session-C-never-used", staging_path)
    assert window_new == []


# --------------------------------------------------- F: incomplete pair ---


def test_incomplete_pair_skipped_without_fabrication():
    db_path = fresh_copy_db("incomplete")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("incomplete")
    session_id = "session-incomplete"
    seed_session(conn, session_id, 1000)
    # complete pair
    seed_waking_turn(conn, staging_path, session_id, "first prompt", "first reply", 1000)
    # canonical Clark turn with NO matching staged prompt (simulates a
    # crash after canonical commit but before/without a staging entry,
    # or a staging file that predates this event for any other reason)
    seed_waking_turn(conn, staging_path, session_id, "unused", "orphan reply", 1010, write_staging=False)
    # another complete pair, chronologically after the incomplete one
    seed_waking_turn(conn, staging_path, session_id, "third prompt", "third reply", 1020)

    pairs, skipped = collect_session_dialogue_pairs(conn, session_id, staging_path)
    assert skipped == 1
    assert [p["clark_text"] for p in pairs] == ["first reply", "third reply"]

    window = build_session_dialogue_window(conn, session_id, staging_path)
    assert window == [
        {"role": "user", "content": "first prompt"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "third prompt"},
        {"role": "assistant", "content": "third reply"},
    ]
    assert "orphan reply" not in [m["content"] for m in window]


# --------------------------------------------------- G: no semantic retrieval


def test_window_invariant_to_current_prompt_wording():
    db_path = fresh_copy_db("invariant")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("invariant")
    session_id = "session-invariant"
    seed_session(conn, session_id, 1000)
    seed_waking_turn(conn, staging_path, session_id, "totally unrelated topic", "totally unrelated reply", 1000)

    window_1 = build_session_dialogue_window(conn, session_id, staging_path)
    # build_session_dialogue_window takes no current-prompt argument at
    # all -- there is no parameter through which prompt wording could
    # influence its result. Calling it twice with identical session
    # state proves determinism/no hidden semantic dependency.
    window_2 = build_session_dialogue_window(conn, session_id, staging_path)
    assert window_1 == window_2
    assert window_1[0]["content"] == "totally unrelated topic"


# ----------------------------------------------- H: current prompt exactly once


def test_current_prompt_appears_exactly_once():
    db_path = fresh_copy_db("once")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("once")
    session_id = "session-once"
    seed_session(conn, session_id, 1000)
    seed_waking_turn(conn, staging_path, session_id, "prior prompt", "prior reply", 1000)

    window = build_session_dialogue_window(conn, session_id, staging_path)
    prepared_messages = [
        {"role": "system", "content": "kardia+memory"},
        {"role": "user", "content": "current prompt text"},
    ]
    final = insert_dialogue_window(prepared_messages, window)
    current_prompt_occurrences = sum(1 for m in final if m["content"] == "current prompt text")
    assert current_prompt_occurrences == 1
    assert final[-1] == {"role": "user", "content": "current prompt text"}


# ------------------------------------------------------- I: roles exact ---


def test_roles_exact_human_user_clark_assistant():
    db_path = fresh_copy_db("roles")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("roles")
    session_id = "session-roles"
    seed_session(conn, session_id, 1000)
    seed_waking_turn(conn, staging_path, session_id, "human said this", "clark said this", 1000)
    window = build_session_dialogue_window(conn, session_id, staging_path)
    assert window[0]["role"] == "user"
    assert window[0]["content"] == "human said this"
    assert window[1]["role"] == "assistant"
    assert window[1]["content"] == "clark said this"


# --------------------------------------------- J: preparation count -------


def test_no_prepare_context_dependency():
    """build_session_dialogue_window/insert_dialogue_window never call
    or import anything named prepare_context -- source-audited."""
    with open(os.path.join(ANAXI_FINAL, "session_dialogue_window.py"), encoding="utf-8") as f:
        source = f.read()
    assert "prepare_context" not in source
    assert "ollama" not in source.lower()


# ------------------------------------------------- durability / exact text


def test_exact_text_including_punctuation_and_newlines_not_rewritten():
    db_path = fresh_copy_db("exact_text")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("exact_text")
    session_id = "session-exact"
    seed_session(conn, session_id, 1000)
    tricky_prompt = "Line one.\nLine two -- with an em dash, \"quotes,\" and trailing spaces.   "
    tricky_reply = "Reply with\ttabs, unicode “curly quotes”, and a newline\nhere."
    seed_waking_turn(conn, staging_path, session_id, tricky_prompt, tricky_reply, 1000)
    window = build_session_dialogue_window(conn, session_id, staging_path)
    assert window[0]["content"] == tricky_prompt
    assert window[1]["content"] == tricky_reply


def test_llama_anaxi_integration_wiring_present():
    """Source-level check that llama_anaxi.py actually wires the
    builder in at the documented insertion point, without executing it."""
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    assert "from session_dialogue_window import build_session_dialogue_window, insert_dialogue_window" in source
    assert "build_session_dialogue_window(" in source
    assert "insert_dialogue_window(" in source
    # exactly one _get_native_session() CALL site remains (moved, not
    # duplicated) -- excludes the "def _get_native_session():" line itself,
    # which also contains that substring.
    assert source.count("session_id, session_started_at = _get_native_session()") == 1


def test_hippocampus_and_kardia_untouched():
    with open(os.path.join(ANAXI_FINAL, "session_dialogue_window.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("hippocampus_store", "hippocampus_retrieval", "active_kardia", "get_current_kardia", "sync_hippocampus"):
        assert forbidden not in source


# =============================================================== OWC8-S1 ===
# Bounded waking dialogue budget: DEFAULT_MAX_TURNS (pair-count bound,
# preserved unchanged) plus DEFAULT_MAX_DIALOGUE_CHARS (new aggregate
# character bound). select_bounded_dialogue_pairs() is a pure function
# over plain pair dicts -- most of this section exercises it directly,
# without a DB, for speed and precision; the closing regression test
# reproduces the real failure's structural shape end-to-end through
# build_session_dialogue_window() itself.


def make_pair(prompt, clark_text, occurred_at=0, event_id=None):
    return {
        "event_id": event_id or f"evt-{occurred_at}",
        "occurred_at": occurred_at,
        "prompt": prompt,
        "clark_text": clark_text,
    }


def test_owc8_bounded_A_all_included_when_under_both_bounds():
    pairs = [make_pair(f"p{i}", f"r{i}", occurred_at=i) for i in range(5)]
    selected = select_bounded_dialogue_pairs(pairs, max_turns=8, max_chars=5000)
    assert selected == pairs


def test_owc8_bounded_B_more_than_max_turns_keeps_newest_eight():
    pairs = [make_pair(f"p{i}", f"r{i}", occurred_at=i) for i in range(12)]
    selected = select_bounded_dialogue_pairs(pairs, max_turns=8, max_chars=5000)
    assert [p["prompt"] for p in selected] == [f"p{i}" for i in range(4, 12)]


def test_owc8_bounded_C_aggregate_cap_selects_newest_until_next_would_exceed():
    # each pair costs 1000 + 1000 = 2000 chars; 5 pairs = 10000 total,
    # far over the 5000 cap -- only the newest 2 (4000 <= 5000) fit,
    # since a 3rd would push the aggregate to 6000 > 5000.
    pairs = [make_pair("a" * 1000, "b" * 1000, occurred_at=i) for i in range(5)]
    selected = select_bounded_dialogue_pairs(pairs, max_turns=8, max_chars=5000)
    assert len(selected) == 2
    assert [p["occurred_at"] for p in selected] == [3, 4]


def test_owc8_bounded_D_selection_is_deterministic():
    pairs = [make_pair("a" * 1000, "b" * 1000, occurred_at=i) for i in range(5)]
    first = select_bounded_dialogue_pairs(pairs, max_turns=8, max_chars=5000)
    second = select_bounded_dialogue_pairs(pairs, max_turns=8, max_chars=5000)
    assert first == second


def test_owc8_bounded_E_no_semantic_ranking_source_audit():
    # Import-level audit rather than a raw text scan -- this module's
    # OWN docstrings legitimately use words like "similarity" and
    # "embedding" to describe what it deliberately does NOT do (see
    # the module header above); a text-scan would false-trigger on its
    # own honesty. What actually rules out semantic ranking is that no
    # scoring/embedding library is imported at all -- the behavioral
    # proof (deterministic, recency+size-only selection) is tests
    # B/C/D/H above.
    import ast
    with open(os.path.join(ANAXI_FINAL, "session_dialogue_window.py"), encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    joined = " ".join(imported_names).lower()
    for forbidden in ("sklearn", "numpy", "sentence_transformers", "embedding", "openai", "tiktoken", "ollama"):
        assert forbidden not in joined


def test_owc8_bounded_F_selected_pairs_preserve_exact_text():
    tricky = make_pair(
        "Line one.\nLine two -- \"quotes,\" trailing spaces.   ",
        "Reply with\ttabs, unicode “curly quotes”, and emoji.",
        occurred_at=1,
    )
    selected = select_bounded_dialogue_pairs([tricky], max_turns=8, max_chars=5000)
    assert selected[0]["prompt"] == tricky["prompt"]
    assert selected[0]["clark_text"] == tricky["clark_text"]


def test_owc8_bounded_G_current_message_not_part_of_historical_budget():
    import inspect
    assert "current_message" not in inspect.signature(select_bounded_dialogue_pairs).parameters
    assert "current_prompt" not in inspect.signature(build_session_dialogue_window).parameters
    # the current-turn message is appended separately, exactly once,
    # by insert_dialogue_window()'s caller -- already exercised by
    # test_current_prompt_appears_exactly_once above; this test only
    # confirms there is no parameter through which it could be counted
    # against the historical budget in the first place.


def test_owc8_bounded_H_oversized_newest_pair_included_intact_alone():
    huge = make_pair("x" * 4000, "y" * 4000, occurred_at=2)  # 8000 chars alone, over the 5000 cap
    older = make_pair("older prompt", "older reply", occurred_at=1)
    selected = select_bounded_dialogue_pairs([older, huge], max_turns=8, max_chars=5000)
    assert len(selected) == 1
    assert selected[0] is huge
    assert selected[0]["prompt"] == "x" * 4000  # intact, not sliced
    assert selected[0]["clark_text"] == "y" * 4000


def test_owc8_bounded_I_same_history_reaches_both_passes_source_audit():
    # OWC9 (spec section 12/23): a concrete, disclosed incompatibility
    # with this test's original assertion -- Pass-1 and Pass-2 may now
    # legitimately end up with a DIFFERENT NUMBER of dialogue pairs
    # (Pass-1 leaner, Pass-2 richer, each trimmed under its own
    # aggregate context budget), so "passed unchanged to both" is no
    # longer literally true. What remains true, and is what this test
    # now proves instead: base_dialogue_window is still sliced exactly
    # ONCE from the shared prepared pass2_messages (never independently
    # re-queried per pass -- OWC9 section 12's own snapshot-identity
    # requirement), and each pass constructs its own list() COPY of
    # that single fetch (never a second DB query, never a different
    # source) before any pass-specific trimming is applied.
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    assert source.count("base_dialogue_window = pass2_messages[1:-1]") == 1
    assert source.count("list(base_dialogue_window)") == 2


def test_owc8_bounded_J_failed_turn_does_not_alter_retry_window():
    db_path = fresh_copy_db("owc8_retry_window")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("owc8_retry_window")
    session_id = "session-owc8-retry"
    seed_session(conn, session_id, 1000)
    seed_waking_turn(conn, staging_path, session_id, "prior prompt", "prior reply", 1000)

    window_attempt_1 = build_session_dialogue_window(conn, session_id, staging_path)
    # A contained Pass-2 failure never persists -- no canonical write,
    # no staging entry for the retried message (confirmed by the live
    # forensic: event_id/staging_id both null on both failed turns) --
    # so nothing is added to the DB/staging file here, mirroring that
    # exact invariant.
    window_attempt_2 = build_session_dialogue_window(conn, session_id, staging_path)
    assert window_attempt_1 == window_attempt_2


def test_owc8_wsp2_episode_context_cap_unchanged():
    # Section 5/6: OWC8-S1 must not touch WSP2-P3 Bridge C's own hard
    # cap -- only create headroom ahead of it.
    import workspace_episode_context as wec
    assert wec.MAX_TOTAL_CHARS == 1000


def test_owc8_context_starvation_regression_bounded_by_aggregate_chars():
    """Reproduces the structural shape of the live OWC/WSP2-P3 forensic
    failure (7 accumulated real dialogue pairs, growing turn-over-turn,
    9305 raw visible-history chars total) as a model-free fixture -- no
    Ollama, no real tokenizer. Proves only what a character-based fixture
    honestly can: that the OLD unbounded-aggregate window is no longer
    what Pass-1/Pass-2 receive once OWC8-S1's aggregate cap is wired in.
    A real prompt_eval_count requires the real tokenizer/model -- the
    first live retry after this gate provides that, not this test."""
    db_path = fresh_copy_db("owc8_starvation")
    conn = open_conn(db_path)
    staging_path = fresh_staging_path("owc8_starvation")
    session_id = "session-owc8-starvation"
    seed_session(conn, session_id, 1000)
    # Same growth shape as the real session's turns 1-7 (increasing
    # prompt/reply sizes) -- not the literal private text.
    sizes = [(227, 32), (1233, 500), (82, 300), (253, 400), (575, 450), (347, 500), (559, 600)]
    for i, (prompt_len, reply_len) in enumerate(sizes):
        seed_waking_turn(conn, staging_path, session_id, "p" * prompt_len, "r" * reply_len, 1000 + i * 10)

    pairs, _skipped = collect_session_dialogue_pairs(conn, session_id, staging_path)
    old_unbounded_chars = sum(len(p["prompt"]) + len(p["clark_text"]) for p in pairs)
    assert old_unbounded_chars > DEFAULT_MAX_DIALOGUE_CHARS  # reproduces the starved shape

    window = build_session_dialogue_window(conn, session_id, staging_path)
    bounded_chars = sum(len(m["content"]) for m in window)
    assert bounded_chars <= DEFAULT_MAX_DIALOGUE_CHARS
    assert bounded_chars < old_unbounded_chars

    window_again = build_session_dialogue_window(conn, session_id, staging_path)
    assert window == window_again  # deterministic


ALL_TESTS = [
    test_empty_session_produces_empty_window,
    test_one_prior_pair_referent_fixture,
    test_multiple_pairs_exact_chronological_interleaving,
    test_nine_pairs_oldest_dropped_newest_eight_retained,
    test_session_isolation_no_cross_session_leakage,
    test_incomplete_pair_skipped_without_fabrication,
    test_window_invariant_to_current_prompt_wording,
    test_current_prompt_appears_exactly_once,
    test_roles_exact_human_user_clark_assistant,
    test_no_prepare_context_dependency,
    test_exact_text_including_punctuation_and_newlines_not_rewritten,
    test_llama_anaxi_integration_wiring_present,
    test_hippocampus_and_kardia_untouched,
    test_owc8_bounded_A_all_included_when_under_both_bounds,
    test_owc8_bounded_B_more_than_max_turns_keeps_newest_eight,
    test_owc8_bounded_C_aggregate_cap_selects_newest_until_next_would_exceed,
    test_owc8_bounded_D_selection_is_deterministic,
    test_owc8_bounded_E_no_semantic_ranking_source_audit,
    test_owc8_bounded_F_selected_pairs_preserve_exact_text,
    test_owc8_bounded_G_current_message_not_part_of_historical_budget,
    test_owc8_bounded_H_oversized_newest_pair_included_intact_alone,
    test_owc8_bounded_I_same_history_reaches_both_passes_source_audit,
    test_owc8_bounded_J_failed_turn_does_not_alter_retry_window,
    test_owc8_wsp2_episode_context_cap_unchanged,
    test_owc8_context_starvation_regression_bounded_by_aggregate_chars,
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
