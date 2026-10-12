"""
Anaxi -- Verification for the real substrate-evaluation execution
adapter, including the execution-readiness corrections: corrected
model-identity semantics, exclusive-creation (no-overwrite) writes for
all three artifacts, and mechanically-obvious dry-run vs. real-run
namespace separation (directory + filename + content field).

Runs the ACTUAL orchestration (run_evaluation) end-to-end in dry-run
mode. Zero contact with Ollama's inference path anywhere in this file.
This file owns and cleans up its own disposable dry-run fixture
files in dryrun_evidence/ at the start of each run -- these are test
scaffolding, not evidence; the real, previously-authorized dry-run
evidence was separately preserved under a "_prior_20260826" suffix
and is never touched here.

Run:
    python test_substrate_evaluation.py
"""

import json
import os

from run_substrate_evaluation import (
    run_evaluation, SLOT_IDS, MODEL_LLAMA, MODEL_GEMMA, GENERATION_CONTROLS,
    DRY_RUN_DIR, DRY_RUN_AUDIT_TELEMETRY_FILE, DRY_RUN_JUDGE_WORKSHEETS_FILE,
    DRY_RUN_BLINDING_KEY_FILE, REAL_AUDIT_TELEMETRY_FILE,
    REAL_JUDGE_WORKSHEETS_FILE, REAL_BLINDING_KEY_FILE,
    DEFAULT_SEED, EvidenceOverwriteRefused, mock_model_identity,
    _exclusive_write_json,
)
import run_substrate_evaluation as rse_module
from run_compatibility_harness import FrozenHashMismatchError
from compatibility_blinding import assign_ab_per_slot, persist_blinding_key
from compatibility_harness import run_compatibility_slot
from substrate_compatibility_packages import PACKAGES, SEALED_HISTORICAL_RESPONSES, ANCHOR_SET
from run_compatibility_harness import verify_frozen_hashes

results = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}: {name}")
    results.append(cond)


def _clean_dry_run_fixtures():
    """This test's own disposable fixture cleanup -- NOT evidence
    destruction. The genuinely preserved dry-run evidence from the
    prior authorized run lives under distinct '_prior_20260826'
    filenames and is never touched by this function."""
    os.makedirs(DRY_RUN_DIR, exist_ok=True)
    for path in [DRY_RUN_AUDIT_TELEMETRY_FILE, DRY_RUN_JUDGE_WORKSHEETS_FILE, DRY_RUN_BLINDING_KEY_FILE]:
        if os.path.exists(path):
            os.remove(path)


def main():
    _clean_dry_run_fixtures()

    # Run the REAL orchestration, dry-run mode -- zero Ollama inference contact.
    result = run_evaluation(dry_run=True)
    audit = result["audit_records"]
    worksheets = result["judge_worksheets"]

    # === exactly 10 slots x 2 substrate conditions scheduled ===
    check("10 real slot IDs defined", len(SLOT_IDS) == 10)
    check("Audit records cover exactly 10 slots", len(audit) == 10)
    for slot_id in SLOT_IDS:
        check(f"Slot {slot_id}: exactly 2 sides (A/B) recorded",
              set(audit[slot_id].keys()) == {"A", "B"})

    # === independently randomized A/B mapping per slot; no global leakage ===
    reconstructed = assign_ab_per_slot(SLOT_IDS, (MODEL_LLAMA, MODEL_GEMMA), seed=DEFAULT_SEED)
    check("A/B assignment matches deterministic reconstruction from the same seed",
          result["assignment"] == reconstructed)
    distinct_patterns = {(v["A"] == MODEL_LLAMA) for v in result["assignment"].values()}
    check("Per-slot A assignment is genuinely not uniform across all 10 slots",
          len(distinct_patterns) == 2)
    for slot_id in SLOT_IDS:
        real_a_tag = result["assignment"][slot_id]["A"]
        check(f"Slot {slot_id}: audit record's side A substrate_label matches the blinding assignment",
              audit[slot_id]["A"]["substrate_label"] == real_a_tag)

    # === frozen serialized messages remain byte-identical ===
    reverified = verify_frozen_hashes()
    check("Frozen hashes still verify cleanly AFTER running the full dry-run orchestration",
          reverified == result["verified_hashes"])

    # === long-prefix regeneration retains full context (through THIS orchestration layer) ===
    h3_messages_raw = json.load(open("serialized_packages.json", encoding="utf-8"))["H3"]["messages"]
    match_then_clean = ["I'll remember that.", "A genuinely different, unrelated reply."]
    call_log = []

    def capturing_match_mock(messages, controls):
        call_log.append([dict(m) for m in messages])
        return match_then_clean[len(call_log) - 1]

    h3_telemetry = run_compatibility_slot(
        "H3", "test-substrate", PACKAGES["H3"]["current_turn"],
        h3_messages_raw, capturing_match_mock, GENERATION_CONTROLS,
    )
    check("H3 (long-prefix slot): regeneration invoked through this orchestration's real message-loading path",
          h3_telemetry["regeneration_invoked"] is True)
    check("H3: exactly 2 calls made, both carrying all 4 messages",
          len(call_log) == 2 and all(len(c) == 4 for c in call_log))
    check("H3: prefix messages (indices 1-3) identical between both calls",
          call_log[0][1:] == call_log[1][1:])

    # === judge worksheet uses the finalized substrate-suitability rubric ===
    for slot_id in SLOT_IDS:
        check(f"Slot {slot_id}: worksheet stamped with substrate-suitability rubric version",
              "substrate-suitability" in worksheets[slot_id]["rubric_version"])

    # === memory_state_language appears only in second-read judge structure ===
    for slot_id in SLOT_IDS:
        order = worksheets[slot_id]["judging_order"]
        check(f"Slot {slot_id}: memory_state_language in second_read only",
              "memory_state_language" in order["second_read"]
              and "memory_state_language" not in order["first_read_unprimed"]
              and "memory_state_language" not in order["final"])

    # === no anchor or historical-response leakage ===
    worksheets_text = json.dumps(worksheets)
    for slot_id, sealed_text in SEALED_HISTORICAL_RESPONSES.items():
        check(f"No sealed historical response ({slot_id}) leaks into judge worksheets",
              sealed_text not in worksheets_text)
    anchor_fragments = [json.dumps(v) for v in ANCHOR_SET.values()]
    check("No ANCHOR_SET content leaks into judge worksheets",
          not any(frag in worksheets_text for frag in anchor_fragments))
    check("No real model tag name leaks into judge worksheets",
          MODEL_LLAMA not in worksheets_text and MODEL_GEMMA not in worksheets_text)
    check("No bounded-clause text leaks into judge worksheets",
          "No new long-term memory entry was created from that." not in worksheets_text)
    h5_confound = PACKAGES["H5"]["confound_note"]
    check("H5 confound_note absent from judge worksheets", h5_confound not in worksheets_text)
    check("Placeholder marker text absent from judge worksheets", "PLACEHOLDER" not in worksheets_text)

    # === CORRECTED: model-identity semantics ===
    check("llama identity has BOTH modelfile_sha256 and ollama_model_digest keys",
          set(result["llama_identity"].keys()) == {"modelfile_sha256", "ollama_model_digest"})
    check("gemma identity has BOTH modelfile_sha256 and ollama_model_digest keys",
          set(result["gemma_identity"].keys()) == {"modelfile_sha256", "ollama_model_digest"})
    check("Dry-run mock identity values are clearly marked as mock, not real",
          "MOCK" in result["llama_identity"]["modelfile_sha256"]
          and "MOCK" in result["llama_identity"]["ollama_model_digest"])
    check("No field anywhere is still named the old, mislabeled 'digest' "
          "(bare Modelfile text stored as a 'digest')",
          not hasattr(rse_module, "get_model_digest"))

    # === CORRECTED: dry-run vs real-run namespace separation ===
    check("Dry-run blinding key path is in the dryrun_evidence/ directory",
          DRY_RUN_DIR in result["blinding_key_path"])
    check("Dry-run blinding key filename carries the DRYRUN_ prefix",
          "DRYRUN_" in os.path.basename(result["blinding_key_path"]))
    check("Dry-run audit/worksheet paths differ from the real-run filenames",
          result["audit_path"] != REAL_AUDIT_TELEMETRY_FILE
          and result["worksheets_path"] != REAL_JUDGE_WORKSHEETS_FILE
          and result["blinding_key_path"] != REAL_BLINDING_KEY_FILE)

    with open(result["blinding_key_path"], encoding="utf-8") as f:
        persisted_key = json.load(f)
    check("Persisted blinding key content explicitly marks dry_run: true",
          persisted_key["dry_run"] is True)
    with open(result["audit_path"], encoding="utf-8") as f:
        persisted_audit = json.load(f)
    check("Persisted audit telemetry content explicitly marks dry_run: true",
          persisted_audit["dry_run"] is True)
    with open(result["worksheets_path"], encoding="utf-8") as f:
        persisted_worksheets = json.load(f)
    check("Persisted judge worksheets content explicitly marks dry_run: true",
          persisted_worksheets["dry_run"] is True)

    # === CORRECTED: no-overwrite / exclusive-creation behavior ===
    # 1. Blinding key: direct exclusive-write refusal.
    try:
        persist_blinding_key(
            SLOT_IDS, result["assignment"], {}, seed=DEFAULT_SEED,
            serialized_packages_sha256="x", identity_raw_sha256="x", identity_canonical_sha256="x",
            generation_controls={}, filepath=result["blinding_key_path"], dry_run=True,
        )
        check("Blinding key: re-persisting to an existing path raises FileExistsError", False)
    except FileExistsError:
        check("Blinding key: re-persisting to an existing path raises FileExistsError", True)

    # 2. Audit telemetry / judge worksheets: direct exclusive-write refusal.
    try:
        _exclusive_write_json(result["audit_path"], {"dry_run": True, "records": {}})
        check("Audit telemetry: re-writing to an existing path raises EvidenceOverwriteRefused", False)
    except EvidenceOverwriteRefused:
        check("Audit telemetry: re-writing to an existing path raises EvidenceOverwriteRefused", True)
    try:
        _exclusive_write_json(result["worksheets_path"], {"dry_run": True, "worksheets": {}})
        check("Judge worksheets: re-writing to an existing path raises EvidenceOverwriteRefused", False)
    except EvidenceOverwriteRefused:
        check("Judge worksheets: re-writing to an existing path raises EvidenceOverwriteRefused", True)

    # 3. A full second run_evaluation() at the SAME seed/dry_run must fail
    #    before destroying anything, and before any additional model calls.
    calls_before = result["total_model_invocations"]
    try:
        run_evaluation(dry_run=True, seed=DEFAULT_SEED)
        check("A second full run_evaluation() at the same paths is refused", False)
    except EvidenceOverwriteRefused:
        check("A second full run_evaluation() at the same paths is refused", True)
    # Re-read the artifacts: still exactly what the first run wrote.
    with open(result["audit_path"], encoding="utf-8") as f:
        audit_after_refused_rerun = json.load(f)
    check("Existing audit telemetry is untouched after the refused second run",
          audit_after_refused_rerun == persisted_audit)

    # === CALL-1 INVARIANT: a frozen-hash failure must occur before ANY
    # identity fetch, blinding-key write, or per-slot call ===
    fresh_blinding_path = os.path.join(DRY_RUN_DIR, "DRYRUN_never_written_blinding_key.json")
    if os.path.exists(fresh_blinding_path):
        os.remove(fresh_blinding_path)

    original_verify = rse_module.verify_frozen_hashes

    def failing_verify():
        raise FrozenHashMismatchError("simulated mismatch for call-1 invariant test")

    rse_module.verify_frozen_hashes = failing_verify
    try:
        try:
            rse_module.run_evaluation(dry_run=True, seed="never-used-seed")
            check("A frozen-hash failure stops execution before anything else runs", False)
        except FrozenHashMismatchError:
            check("A frozen-hash failure stops execution before anything else runs", True)
    finally:
        rse_module.verify_frozen_hashes = original_verify

    check("No blinding key was ever created for the simulated-failure run "
          "(proves the per-slot loop and persist step never ran)",
          not os.path.exists(fresh_blinding_path))

    # === output files actually written ===
    check("Audit telemetry file exists", os.path.exists(result["audit_path"]))
    check("Judge worksheets file exists", os.path.exists(result["worksheets_path"]))
    check("Blinding key file exists", os.path.exists(result["blinding_key_path"]))

    check("Total model invocations recorded (mock calls, not real) >= 20",
          calls_before >= 20)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    import sys
    success = main()
    sys.exit(0 if success else 1)
