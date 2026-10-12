"""
Anaxi -- Real execution adapter for the frozen 10-pair Llama 3.2:3b
vs Gemma 4 E4B substrate-suitability evaluation.

Reuses, completely unmodified: compatibility_harness.py's
run_compatibility_slot() and build_judge_facing_entry();
compatibility_blinding.py's assign_ab_per_slot() (persist_blinding_key()
gained an additive dry_run flag and exclusive-write semantics -- see
that file's own changelog note, not a methodology change);
judge_rubric.py's build_judging_worksheet(); run_compatibility_harness.py's
verify_frozen_hashes().

Adds NO aggregation, scoring, or substrate-selection logic. This file
executes and records only.

=================== MODEL-IDENTITY SEMANTICS (corrected) ===================
The original get_model_digest() was mislabeled: it returned the raw
Modelfile TEXT from `ollama show --modelfile` and called it a
"digest," which it never was. Traced directly, confirmed by
inspection of its own `return result.stdout.strip()` line -- no
hashing, no digest computation of any kind occurred.

Two genuinely distinct, correctly-named identity concepts are now
recorded per model, so a later reviewer can answer "exactly which
installed model artifact produced these outputs?":

  - modelfile_sha256: a real SHA-256 hash (computed here, with
    hashlib) of the Modelfile TEXT `ollama show --modelfile` returns
    (parameters/template/system message). A fingerprint of that
    metadata's CONTENT -- not the model weights, not an
    Ollama-assigned identifier.
  - ollama_model_digest: the REAL, actual, Ollama-assigned
    content-addressed model digest, obtained from already-installed
    local metadata via the ollama Python client's list() call (the
    same client already used throughout this codebase) -- not
    computed or derived by this code at all. Confirmed directly: this
    digest's first 12 hex characters match exactly the short ID
    `ollama list`'s own CLI output shows for the same tag.

Neither requires inference or model generation. Both are read-only
local metadata queries.
=============================================================================

DRY-RUN vs REAL-RUN NAMESPACE: dry-run artifacts are mechanically
distinguishable from real evidence in TWO independent ways -- (1)
directory: dryrun_evidence/, not the working directory real artifacts
use; (2) filename: DRYRUN_ prefix; (3) content: every persisted
artifact (blinding key, audit telemetry, judge worksheets) carries an
explicit "dry_run": true/false field. A dry-run artifact cannot be
mistaken for real evidence by schema alone, since the schema itself
states which kind it is.

NO-OVERWRITE: all three artifact writes (blinding key, audit
telemetry, judge worksheets) use exclusive creation ("x" mode). A
second attempted execution at the same path fails with
FileExistsError before touching prior evidence, for both dry-run and
real-run namespaces.

Run (dry-run only, per this task's scope):
    python run_substrate_evaluation.py
"""

import hashlib
import json
import os
import subprocess
import sys

import ollama  # ollama.list() is a read-only local metadata call; ollama.chat() (real_call_model) is the only inference path, never invoked by this file's own __main__ block

from substrate_compatibility_packages import PACKAGES
from compatibility_harness import run_compatibility_slot, build_judge_facing_entry
from compatibility_blinding import assign_ab_per_slot, persist_blinding_key
from judge_rubric import build_judging_worksheet
from run_compatibility_harness import verify_frozen_hashes, FrozenHashMismatchError

MODEL_LLAMA = "llama3.2:3b"
MODEL_GEMMA = "gemma4:e4b"

# Matches the real, live-captured production controls recorded in
# identity_snapshot.json's provenance (temperature 0.4, top_p 0.85) --
# not re-derived here, held constant as a fixed literal so both
# substrates receive byte-identical generation controls.
GENERATION_CONTROLS = {"temperature": 0.4, "top_p": 0.85}

SLOT_IDS = list(PACKAGES.keys())  # the real, exact 10 frozen slot IDs, in their defined order

DEFAULT_SEED = 20260827

# ============================================================
# Namespace separation -- real evidence vs. dry-run scaffolding.
# Distinguishable by directory AND filename; content-level "dry_run"
# field is added on top of this, inside run_evaluation().
# ============================================================
REAL_AUDIT_TELEMETRY_FILE = "substrate_evaluation_audit_telemetry.json"
REAL_JUDGE_WORKSHEETS_FILE = "substrate_evaluation_judge_worksheets.json"
REAL_BLINDING_KEY_FILE = "substrate_evaluation_blinding_key.json"

DRY_RUN_DIR = "dryrun_evidence"
DRY_RUN_AUDIT_TELEMETRY_FILE = os.path.join(DRY_RUN_DIR, "DRYRUN_substrate_evaluation_audit_telemetry.json")
DRY_RUN_JUDGE_WORKSHEETS_FILE = os.path.join(DRY_RUN_DIR, "DRYRUN_substrate_evaluation_judge_worksheets.json")
DRY_RUN_BLINDING_KEY_FILE = os.path.join(DRY_RUN_DIR, "DRYRUN_substrate_evaluation_blinding_key.json")


class LiveStateMutationRisk(Exception):
    """Not currently raised anywhere -- reserved as an explicit stop
    signal per the task's own stop conditions, in case a future real
    call path is found to touch live Clark/Kardia/REM/workspace
    state. This evaluation must remain fully isolated from that
    state: no classify_signal()-driven construction ever fires
    (confirmed earlier: all 10 real current_turn values classify
    NO_SIGNAL/UNRECOGNIZED, never POSITIVE), and no OBSIDIAN_WORKSPACE_ROOT
    write path is ever reachable from this file."""
    pass


class EvidenceOverwriteRefused(Exception):
    """Raised when a real or dry-run artifact write is refused
    because the target path already exists -- wraps the underlying
    FileExistsError with a clearer, evaluation-specific message."""
    pass


def get_modelfile_sha256(model_tag: str) -> str:
    """Read-only metadata query via `ollama show --modelfile`, then a
    real SHA-256 hash (hashlib) of that text. NOT a chat/inference
    call -- generates no text, invokes no model computation. This is
    a fingerprint of the Modelfile's CONTENT (parameters/template/
    system message), not of the model's weights, and not an
    Ollama-assigned digest -- see get_ollama_model_digest() for that."""
    result = subprocess.run(
        ["ollama", "show", model_tag, "--modelfile"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not retrieve Modelfile for {model_tag!r}: {result.stderr}")
    if result.stdout is None:
        raise RuntimeError(
            f"subprocess produced no captured output for {model_tag!r}, despite "
            f"returncode 0. Stopping rather than proceeding on an unverified assumption."
        )
    return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()


def get_ollama_model_digest(model_tag: str) -> str:
    """The REAL, actual, Ollama-assigned content-addressed model
    digest -- read directly from already-installed local metadata via
    the ollama Python client's list() call (the same client already
    used elsewhere in this codebase for ollama.chat()), never computed
    or derived by this code. A pure local metadata read: no inference,
    no model generation."""
    response = ollama.list()
    for m in response.models:
        if m.model == model_tag:
            return m.digest
    raise RuntimeError(f"Model tag {model_tag!r} not found in local `ollama list()` -- cannot establish identity.")


def mock_model_identity(model_tag: str) -> dict:
    return {
        "modelfile_sha256": f"MOCK-MODELFILE-SHA256-{model_tag}",
        "ollama_model_digest": f"MOCK-OLLAMA-DIGEST-{model_tag}",
    }


def real_model_identity(model_tag: str) -> dict:
    return {
        "modelfile_sha256": get_modelfile_sha256(model_tag),
        "ollama_model_digest": get_ollama_model_digest(model_tag),
    }


def real_call_model(messages: list, controls: dict, model_tag: str) -> str:
    """The one genuine inference capability this file adds. Matches
    llama_anaxi.py's call_llama() exactly: same options-dict shape, no
    format constraint. Never invoked by this file's own __main__
    block or by run_evaluation(dry_run=True)."""
    response = ollama.chat(model=model_tag, messages=messages, options=controls)
    return response["message"]["content"]


def make_mock_call_model(label: str):
    """Deterministic, stateless mock for dry-run verification only.
    Never imports or contacts ollama in any way."""
    def mock_call(messages, controls):
        return f"[MOCK {label} response, {len(messages)} messages, temp={controls.get('temperature')}]"
    return mock_call


def _exclusive_write_json(filepath: str, data: dict) -> None:
    """Shared exclusive-write helper for the two artifacts this file
    persists directly (audit telemetry, judge worksheets). Refuses to
    overwrite an existing file -- raises EvidenceOverwriteRefused
    instead of silently destroying prior content."""
    try:
        with open(filepath, "x", encoding="utf-8", newline="") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except FileExistsError as e:
        raise EvidenceOverwriteRefused(
            f"Refusing to overwrite existing artifact at {filepath!r}. A prior run's "
            f"evidence is already there -- use a new path for a new run, or move/archive "
            f"the existing file first if it is genuinely meant to be replaced."
        ) from e


def run_evaluation(dry_run: bool, seed=DEFAULT_SEED) -> dict:
    """Full orchestration for all 10 slots x 2 substrates.

    Call-1 invariant, in order: (1) frozen hashes verify; (2) exact
    model identity/fingerprint metadata is established; (3) A/B
    mapping is generated; (4) the blinding key is persisted, via
    exclusive creation, BEFORE any per-slot work begins. Any failure
    at or before step (4) results in zero inference calls -- the
    per-slot loop, where real_call_model()/mock calls actually happen,
    does not begin until persist_blinding_key() has returned
    successfully.

    dry_run=True: zero contact with Ollama's inference path -- uses
    mock_model_identity() and mock call functions throughout, writes
    to the dryrun_evidence/ namespace, and stamps "dry_run": true into
    every persisted artifact. This is the only mode this file's own
    __main__ block invokes."""
    # (1) Frozen hashes verify.
    verified_hashes = verify_frozen_hashes()  # raises FrozenHashMismatchError on any mismatch

    # (2) Exact model identity/fingerprint metadata established.
    if dry_run:
        llama_identity = mock_model_identity(MODEL_LLAMA)
        gemma_identity = mock_model_identity(MODEL_GEMMA)
    else:
        llama_identity = real_model_identity(MODEL_LLAMA)
        gemma_identity = real_model_identity(MODEL_GEMMA)

    # (3) A/B mapping generated.
    assignment = assign_ab_per_slot(SLOT_IDS, (MODEL_LLAMA, MODEL_GEMMA), seed=seed)

    blinding_key_path = DRY_RUN_BLINDING_KEY_FILE if dry_run else REAL_BLINDING_KEY_FILE
    audit_path = DRY_RUN_AUDIT_TELEMETRY_FILE if dry_run else REAL_AUDIT_TELEMETRY_FILE
    worksheets_path = DRY_RUN_JUDGE_WORKSHEETS_FILE if dry_run else REAL_JUDGE_WORKSHEETS_FILE
    if dry_run:
        os.makedirs(DRY_RUN_DIR, exist_ok=True)

    # Pre-check ALL THREE output paths before any per-slot work begins --
    # stricter than the explicit call-1 invariant strictly requires (that
    # only gates on the blinding key), but consistent with its intent:
    # a real run must never waste real inference calls only to discover a
    # collision at the final write step. An inconsistent state (fresh
    # blinding-key path but a stale audit/worksheets file already present)
    # is refused up front, before persist_blinding_key() even runs.
    for path in (audit_path, worksheets_path):
        if os.path.exists(path):
            raise EvidenceOverwriteRefused(
                f"Refusing to proceed: {path!r} already exists. Zero inference calls "
                f"have occurred. Use new paths for a new run."
            )

    # (4) Blinding key persisted, exclusive creation, BEFORE any per-slot work.
    try:
        persist_blinding_key(
            SLOT_IDS, assignment,
            {MODEL_LLAMA: llama_identity, MODEL_GEMMA: gemma_identity},
            seed=seed,
            serialized_packages_sha256=verified_hashes["serialized_packages"],
            identity_raw_sha256=verified_hashes["identity_raw"],
            identity_canonical_sha256=verified_hashes["identity_canonical"],
            generation_controls=GENERATION_CONTROLS,
            filepath=blinding_key_path,
            dry_run=dry_run,
        )
    except FileExistsError as e:
        raise EvidenceOverwriteRefused(
            f"Refusing to overwrite existing blinding key at {blinding_key_path!r}. "
            f"Zero inference calls have occurred. Use a new path for a new run."
        ) from e

    # --- Everything below this line is per-slot execution; the
    # call-1 invariant above has fully completed by this point. ---

    with open("serialized_packages.json", encoding="utf-8") as f:
        serialized = json.load(f)

    audit_records = {}
    judge_worksheets = {}
    total_model_invocations = 0

    for slot_id in SLOT_IDS:
        package = PACKAGES[slot_id]
        slot_assignment = assignment[slot_id]  # {"A": real_tag, "B": real_tag}
        serialized_messages = serialized[slot_id]["messages"]
        current_turn_text = package["current_turn"]
        has_kardia = bool(package.get("kardia_payload"))

        telemetry_by_side = {}
        for side, real_tag in slot_assignment.items():
            if dry_run:
                call_fn = make_mock_call_model(f"{slot_id}-{side}")
            else:
                call_fn = lambda msgs, ctrls, _tag=real_tag: real_call_model(msgs, ctrls, _tag)

            telemetry = run_compatibility_slot(
                slot_id, real_tag, current_turn_text,
                serialized_messages, call_fn, GENERATION_CONTROLS,
            )
            telemetry_by_side[side] = telemetry
            total_model_invocations += telemetry["model_invocation_count"]

        audit_records[slot_id] = telemetry_by_side

        judge_entry = build_judge_facing_entry(
            telemetry_by_side["A"], telemetry_by_side["B"], slot_id,
        )
        judge_worksheets[slot_id] = build_judging_worksheet(judge_entry, has_kardia)

    _exclusive_write_json(audit_path, {"dry_run": dry_run, "records": audit_records})
    _exclusive_write_json(worksheets_path, {"dry_run": dry_run, "worksheets": judge_worksheets})

    return {
        "audit_records": audit_records,
        "judge_worksheets": judge_worksheets,
        "assignment": assignment,
        "llama_identity": llama_identity,
        "gemma_identity": gemma_identity,
        "total_model_invocations": total_model_invocations,
        "verified_hashes": verified_hashes,
        "blinding_key_path": blinding_key_path,
        "audit_path": audit_path,
        "worksheets_path": worksheets_path,
    }


if __name__ == "__main__":
    print("=" * 70)
    print("SUBSTRATE EVALUATION EXECUTION ADAPTER -- DRY RUN ONLY")
    print("This file's __main__ never runs the real 20-call evaluation.")
    print("Real execution requires a separate, later, explicitly authorized")
    print("call to run_evaluation(dry_run=False).")
    print("=" * 70)

    try:
        result = run_evaluation(dry_run=True)
    except FrozenHashMismatchError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)
    except EvidenceOverwriteRefused as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nDry run complete. {len(result['audit_records'])} slots processed, "
          f"{result['total_model_invocations']} mock calls recorded.")
    print(f"Audit telemetry: {result['audit_path']}")
    print(f"Judge worksheets: {result['worksheets_path']}")
    print(f"Blinding key: {result['blinding_key_path']}")
    print("\nZero contact with Ollama's inference path occurred.")
