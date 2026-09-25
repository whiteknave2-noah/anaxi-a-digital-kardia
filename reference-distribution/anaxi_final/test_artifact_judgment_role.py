"""artifact_judgment is a DISCLOSED, host-triggered construction step over a
HUMAN-AUTHORIZED preservation request, run before subject generation. It is not
a critic, filter, scorer or rewriter of the subject's output, and not a hidden
personality/output governor. This pins that role: structural checks on the
waking path, plus content pins of the modules that define it, so any later
change to the role forces a fresh constitutional review instead of slipping in.

Reviewed unchanged against the independently verified commit 2598006: the four
defining modules are byte-identical to it, and the set of artifact/bounded-
clause/signal/judgment lines in llama_anaxi.py is identical."""
import hashlib
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

PINNED_MODULE_SHA256 = {
    "artifact_prompts.py": "19674fc4d460d08b4a339e1e94f9e0a5ed20866bf7eaba6c3d34ca02bcf1210b",
    "signal_matcher.py": "c329f3ddfe6bc48516d59aa4bdd3b5af7319767611091a6888c8e3584e7a6cc2",
    "bounded_clause.py": "c177bbc9a03cc9125f019ec6590d178378ebc2019810f90fb2d1d1224c03cabb",
    "clark_journal.py": "819a8ba9bc93796619958b7cee8feff6b5c332e52c17a1f80c21ca6fdc962454",
}
# Explicitly approved constitutional re-review for the Caret-primary refinement.  The only change is the
# `caret_occasion` branch pinning signal_category to NO_SIGNAL (and skipping classify_signal /
# record_observation), so correspondence text can never open the human-authorization gate.  Authority is
# narrowed only; pinned by test_caret_wake_service.py's artifact/signal invariant tests.
# Re-reviewed 2026-09-24 (final completion build, collaborative Obsidian workspace): the durable write
# (a) never overwrites an existing note in the SHARED vault -- a name collision takes the first free
# "<name> (n).md" -- and (b) records the committing waking turn's event_id / pipeline_id and the body's
# sha256 in frontmatter, post-commit (Q-05 unchanged).  When judgment runs, what it reads, what it may
# decide and its authority are unchanged; the only waking-path line changed passes the already-committed
# canonical ids to finalize_artifact_write.
# Re-reviewed 2026-09-24 (live artifact-decision check): additive, recording-only.  Each turn's decision is
# appended to artifact_decision_log.jsonl (host evidence, no content) after it is final -- on the ordinary
# path after finalize_artifact_write, on a use_workspace turn beside the existing "not_applicable"
# replacement, so a judgment that ran and was not applied is no longer invisible.  When judgment runs,
# what it reads, what it may decide, the gate, the ordering and its authority are all unchanged.
PINNED_WAKING_ROLE_LINES_SHA256 = "7016092bc74f3f0e88ac6574d13b089eeb4701ca17e5f0567a3fd3479931115a"

WAKING = (HERE / "llama_anaxi.py").read_text(encoding="utf-8")
BODY = WAKING[WAKING.index("def run_waking_turn("):]


def test_the_defining_modules_are_unchanged_since_the_reviewed_role():
    for name, expected in PINNED_MODULE_SHA256.items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == expected, (
            f"{name} changed: artifact_judgment's constitutional role must be re-reviewed")


def test_the_waking_path_lines_that_define_the_role_are_unchanged():
    lines = sorted({ln for ln in WAKING.splitlines()
                    if re.search(r"artifact|bounded_clause|classify_signal|signal_category|judgment", ln, re.I)})
    assert hashlib.sha256("\n".join(lines).encode()).hexdigest() == PINNED_WAKING_ROLE_LINES_SHA256, (
        "artifact/signal/bounded-clause handling in run_waking_turn changed: re-review its role")


def test_it_runs_only_on_a_deterministic_positive_signal_and_only_reads_the_humans_words():
    gate = BODY.index('if signal_category == "POSITIVE":')
    build = BODY.index("build_artifact_construction_messages(prompt)")
    assert gate < build
    assert re.findall(r"build_artifact_construction_messages\(([^)]*)\)", BODY) == ["prompt"]
    assert re.findall(r"prepare_artifact_decision\(\s*raw_judgment,[^)]*source_prompt=prompt", BODY)


def test_it_precedes_subject_generation_and_never_sees_or_alters_subject_output():
    judgment = BODY.index('timed_model_call("artifact_judgment"')
    generation = BODY.index("call_llama(")
    assert judgment < generation                              # decided before the subject speaks
    # The subject's own prose never enters the judgment or the decision.
    for call in ("build_artifact_construction_messages(", "prepare_artifact_decision(", "ask_llama_for_json(judgment_messages"):
        for match in re.finditer(re.escape(call), BODY):
            window = BODY[match.start(): match.start() + 400]
            assert "clark_prose" not in window and "expression" not in window.split(")")[0], call
    # (The TASK-mode-only free-prose screen is a separate, already-guarded path: see
    # test_architecture_invariants.py -- unreachable from the conversation launcher.)
