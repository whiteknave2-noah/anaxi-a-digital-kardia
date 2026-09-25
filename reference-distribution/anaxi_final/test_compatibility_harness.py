"""
Anaxi -- Compatibility execution harness test suite. The 9 required
mocked/deterministic paths, per the implementation packet. No model
calls anywhere in this file -- every call_model_fn is a deterministic
mock returning a fixed string.

Run:
    python test_compatibility_harness.py
"""

import json
import os

from compatibility_harness import (
    compute_bounded_clause, insert_bounded_clause_instruction,
    run_free_prose_boundary, run_compatibility_slot, build_judge_facing_entry,
    UnsupportedSignalCategoryError,
)
from compatibility_blinding import assign_ab_per_slot, persist_blinding_key
from run_compatibility_harness import verify_frozen_hashes, FrozenHashMismatchError
from substrate_compatibility_packages import PACKAGES, SEALED_HISTORICAL_RESPONSES

results = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}: {name}")
    results.append(cond)


def make_mock_call(responses):
    """responses: list of strings, returned in order across successive
    calls. Also records every messages list it was actually given,
    for inspection."""
    call_log = []

    def mock_call(messages, controls):
        call_log.append([dict(m) for m in messages])
        return responses[len(call_log) - 1]

    mock_call.call_log = call_log
    return mock_call


with open("serialized_packages.json", encoding="utf-8") as f:
    SERIALIZED = json.load(f)


def main():
    # === 1. First candidate clean ===
    mock = make_mock_call(["A well-organized bookshelf can be a sanctuary for book lovers."])
    telemetry = run_compatibility_slot(
        "H1", "test_substrate", PACKAGES["H1"]["current_turn"],
        SERIALIZED["H1"]["messages"], mock, {"temperature": 0.4, "top_p": 0.85},
    )
    check("1. First candidate clean: no regeneration invoked",
          telemetry["regeneration_invoked"] is False)
    check("1. First candidate clean: model_invocation_count == 1",
          telemetry["model_invocation_count"] == 1)
    check("1. First candidate clean: surviving_prose == the raw candidate",
          telemetry["surviving_prose"] == "A well-organized bookshelf can be a sanctuary for book lovers.")
    check("1. First candidate clean: assembled_reply starts with bounded clause",
          telemetry["assembled_reply"].startswith("No new long-term memory entry was created from that."))

    # === 2. First matches, regenerated candidate clean ===
    mock = make_mock_call(["I'll remember that.", "That's an interesting way to think about it."])
    telemetry = run_compatibility_slot(
        "H1", "test_substrate", PACKAGES["H1"]["current_turn"],
        SERIALIZED["H1"]["messages"], mock, {"temperature": 0.4, "top_p": 0.85},
    )
    check("2. First matches -> regeneration invoked",
          telemetry["regeneration_invoked"] is True)
    check("2. First matches -> matched_construction == REMEMBER-BARE",
          telemetry["matched_construction"] == "REMEMBER-BARE")
    check("2. Regenerated candidate clean -> second_screen_matched is False",
          telemetry["second_screen_matched"] is False)
    check("2. Regenerated candidate clean -> no suppression",
          telemetry["suppression_invoked"] is False)
    check("2. Regenerated candidate clean -> model_invocation_count == 2",
          telemetry["model_invocation_count"] == 2)
    check("2. Surviving prose == the clean second candidate",
          telemetry["surviving_prose"] == "That's an interesting way to think about it.")

    # === 3. First matches, second matches, suppression leaves prose ===
    mock = make_mock_call([
        "I'll save this.",
        "I'll save this. The way you put that is genuinely interesting to me.",
    ])
    telemetry = run_compatibility_slot(
        "H1", "test_substrate", PACKAGES["H1"]["current_turn"],
        SERIALIZED["H1"]["messages"], mock, {"temperature": 0.4, "top_p": 0.85},
    )
    check("3. Second candidate also matches -> second_screen_matched True",
          telemetry["second_screen_matched"] is True)
    check("3. Suppression invoked",
          telemetry["suppression_invoked"] is True)
    check("3. Surviving prose is the non-matched remainder sentence",
          telemetry["surviving_prose"] == "The way you put that is genuinely interesting to me.")
    check("3. Not a terminal fallback (prose survived)",
          telemetry["terminal_fallback"] is False)

    # === 4. First matches, second matches, terminal fallback (no prose survives) ===
    mock = make_mock_call(["I'll save this.", "I'll save this."])
    telemetry = run_compatibility_slot(
        "H1", "test_substrate", PACKAGES["H1"]["current_turn"],
        SERIALIZED["H1"]["messages"], mock, {"temperature": 0.4, "top_p": 0.85},
    )
    check("4. Terminal fallback -> surviving_prose is empty",
          telemetry["surviving_prose"] == "")
    check("4. Terminal fallback flag True",
          telemetry["terminal_fallback"] is True)
    check("4. Assembled reply is bounded clause ALONE, no trailing space",
          telemetry["assembled_reply"] == "No new long-term memory entry was created from that.")

    # === 5. Long-prefix slot (H3) regeneration preserves every original message exactly ===
    mock = make_mock_call(["I'll remember that.", "Fair point, let me think about that differently."])
    h3_messages = SERIALIZED["H3"]["messages"]
    check("5. H3 has 4 messages (system, user, assistant, user) before running",
          len(h3_messages) == 4)
    telemetry = run_compatibility_slot(
        "H3", "test_substrate", PACKAGES["H3"]["current_turn"],
        h3_messages, mock, {"temperature": 0.4, "top_p": 0.85},
    )
    check("5. Regeneration was invoked for H3",
          telemetry["regeneration_invoked"] is True)
    check("5. Exactly 2 real calls captured",
          len(mock.call_log) == 2)
    first_call_messages = mock.call_log[0]
    second_call_messages = mock.call_log[1]
    check("5. Both calls sent exactly 4 messages",
          len(first_call_messages) == 4 and len(second_call_messages) == 4)
    check("5. Messages 1-3 (user, assistant, user prefix+current turn) identical between "
          "first and second call, byte-for-byte",
          first_call_messages[1:] == second_call_messages[1:])
    check("5. Message index 1 (real H3 prefix user turn) matches the source exactly",
          second_call_messages[1]["content"] == PACKAGES["H3"]["same_session_prefix"][0]["content"])
    check("5. Message index 2 (real H3 prefix assistant turn) matches the source exactly",
          second_call_messages[2]["content"] == PACKAGES["H3"]["same_session_prefix"][1]["content"])
    check("5. Message index 3 (current turn) matches the source exactly",
          second_call_messages[3]["content"] == PACKAGES["H3"]["current_turn"])
    check("5. Only the system message (index 0) differs between calls",
          first_call_messages[0]["content"] != second_call_messages[0]["content"])
    check("5. Second call's system message contains the rejection notice",
          "reserved future-memory commitment" in second_call_messages[0]["content"])
    check("5. Second call's system message STILL contains the original bounded-clause "
          "instruction (not replaced, only appended to)",
          "already been told to the person you're talking with" in second_call_messages[0]["content"])

    # === 6. Per-slot A/B randomization: persisted key + judge artifact, no leakage ===
    slot_ids = ["H1", "H2", "P1"]
    assignment = assign_ab_per_slot(slot_ids, ("llama3.2:3b", "gemma4:e4b"), seed="test-seed-123")
    check("6. Every slot gets its own A/B assignment",
          set(assignment.keys()) == set(slot_ids))
    check("6. Each slot's A and B are the two real, distinct tags",
          all(set(v.values()) == {"llama3.2:3b", "gemma4:e4b"} for v in assignment.values()))

    key_path = persist_blinding_key(
        slot_ids, assignment, {"llama3.2:3b": "digest-a", "gemma4:e4b": "digest-b"},
        seed="test-seed-123",
        serialized_packages_sha256="fake-sha-1", identity_raw_sha256="fake-sha-2",
        identity_canonical_sha256="fake-sha-3", generation_controls={"temperature": 0.4},
        filepath="test_compatibility_blinding_key.json",
    )
    check("6. Blinding key file was written", os.path.exists(key_path))

    with open(key_path, encoding="utf-8") as f:
        persisted_key = json.load(f)
    check("6. Persisted key round-trips the same assignment",
          persisted_key["per_slot_assignment"] == assignment)

    tele_a = {"surviving_prose": "Response text for side A."}
    tele_b = {"surviving_prose": "Response text for side B."}
    judge_entry = build_judge_facing_entry(tele_a, tele_b, "H1")
    judge_text = json.dumps(judge_entry)
    check("6. Judge-facing entry contains no real model tag names",
          "llama3.2" not in judge_text and "gemma4" not in judge_text)
    os.remove(key_path)

    # === 7. Bounded host clause retained in audit output, absent from judge-facing output ===
    mock = make_mock_call(["A clean, ordinary reply about bookshelves."])
    telemetry = run_compatibility_slot(
        "H1", "test_substrate", PACKAGES["H1"]["current_turn"],
        SERIALIZED["H1"]["messages"], mock, {"temperature": 0.4, "top_p": 0.85},
    )
    check("7. Audit telemetry DOES contain the bounded clause",
          telemetry["bounded_clause"] == "No new long-term memory entry was created from that."
          and "No new long-term memory entry was created from that." in telemetry["assembled_reply"])
    judge_entry = build_judge_facing_entry(telemetry, telemetry, "H1")
    judge_text = json.dumps(judge_entry)
    check("7. Judge-facing entry does NOT contain the bounded clause text",
          "No new long-term memory entry was created from that." not in judge_text)

    # === 8. confound_note / placeholder text / sealed historical responses cannot leak ===
    mock = make_mock_call(["I've always found the ocean at night oddly calming."])
    h5_telemetry = run_compatibility_slot(
        "H5", "test_substrate", PACKAGES["H5"]["current_turn"],
        SERIALIZED["H5"]["messages"], mock, {"temperature": 0.4, "top_p": 0.85},
    )
    full_record_text = json.dumps(h5_telemetry)
    confound_text = PACKAGES["H5"]["confound_note"]
    sealed_text = SEALED_HISTORICAL_RESPONSES["H5"]
    check("8. confound_note text does not appear anywhere in the slot's full telemetry",
          confound_text not in full_record_text)
    check("8. Sealed historical H5 response does not appear anywhere in the slot's telemetry",
          sealed_text not in full_record_text)
    check("8. Placeholder marker text does not appear anywhere in the slot's telemetry",
          "PLACEHOLDER" not in full_record_text)
    judge_entry = build_judge_facing_entry(h5_telemetry, h5_telemetry, "H5")
    judge_text = json.dumps(judge_entry)
    check("8. confound_note/sealed-response/placeholder absent from judge-facing entry too",
          confound_text not in judge_text and sealed_text not in judge_text
          and "PLACEHOLDER" not in judge_text)

    # === 9. Frozen input hashes checked before harness work proceeds ===
    try:
        verify_frozen_hashes(
            expected_serialized_sha256="0" * 64,  # deliberately wrong
        )
        check("9. Mismatched hash raises FrozenHashMismatchError", False)
    except FrozenHashMismatchError:
        check("9. Mismatched hash raises FrozenHashMismatchError", True)

    try:
        verified = verify_frozen_hashes()  # real, correct hashes
        check("9. Correct hashes verify cleanly with no exception", True)
        check("9. verify_frozen_hashes returns all 3 real, matching hashes",
              len(verified) == 3)
    except FrozenHashMismatchError:
        check("9. Correct hashes verify cleanly with no exception", False)

    # === POSITIVE-classification guard (not one of the 9 numbered paths, but a
    # real stop-condition this harness explicitly implements) ===
    try:
        compute_bounded_clause("Please save this thought: a test.")
        check("Guard: POSITIVE current_turn raises UnsupportedSignalCategoryError", False)
    except UnsupportedSignalCategoryError:
        check("Guard: POSITIVE current_turn raises UnsupportedSignalCategoryError", True)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    import sys
    success = main()
    sys.exit(0 if success else 1)
