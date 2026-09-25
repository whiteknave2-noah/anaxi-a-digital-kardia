"""BOUNDARY INSPECTOR v1 -- operator CLI suite.

Executes the real boundary_inspect_cli.py in clean subprocesses against
disposable synthetic temp directories. Every invocation passes an
explicit --data-dir (never the package default) so the production
provenance DB is untouchable; the CLI is asserted READ-ONLY.

No inference, no Ollama, no network, no live/Private Space access.

Run from repository root:
    python3 -B anaxi_final/test_boundary_inspect_cli.py
(also pytest-compatible: bare def test_*() functions).
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(ANAXI_FINAL)
CLI_PATH = os.path.join(ANAXI_FINAL, "boundary_inspect_cli.py")

TEST_DIR = tempfile.mkdtemp(prefix="boundary_cli_test_")


def _fresh_dir(name):
    data_dir = os.path.join(TEST_DIR, name)
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _run_cli(data_dir, inquiry, *extra):
    return subprocess.run(
        [sys.executable, "-B", CLI_PATH, inquiry, "--data-dir", data_dir] + list(extra),
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def test_cli_host_rule_success_exit_zero():
    """Ground truth (traced against conversation_direction.py): the
    closed act schema has no reason field, so this must be a genuine,
    complete NO_HOST_BOUNDARY_FOUND -- never a fabricated boundary."""
    data_dir = _fresh_dir("cli-host-rule")
    proc = _run_cli(data_dir, "host_rule:participation.requires_reason")
    assert proc.returncode == 0, proc.stderr
    assert "classification: NO_HOST_BOUNDARY_FOUND" in proc.stdout
    assert "boundaries: none" in proc.stdout
    assert "participation.requires_reason" in proc.stdout


def test_cli_boundary_id_on_dbless_dir_is_incomplete_exit_zero():
    data_dir = _fresh_dir("cli-boundary-id-empty")
    proc = _run_cli(data_dir, "boundary_id:private_space.public_rule")
    assert proc.returncode == 0, proc.stderr
    assert "classification: OBSERVATION_INCOMPLETE" in proc.stdout
    assert "boundaries: none" in proc.stdout


def test_cli_capability_substrate_importable_and_complete_exit_zero():
    """Fresh supported subprocess imports the real capability substrate and
    reports its established host boundary."""
    data_dir = _fresh_dir("cli-capability-incomplete")
    proc = _run_cli(data_dir, "capability:library")
    assert proc.returncode == 0, proc.stderr
    assert "classification: ACTUAL_HOST_BOUNDARY" in proc.stdout
    assert "evidence_complete: True" in proc.stdout
    assert "workspace.capability.closed_action_domain" in proc.stdout


def test_cli_validation_rejection_exit_two():
    data_dir = _fresh_dir("cli-validation")
    proc = _run_cli(data_dir, "capability:appdata")
    assert proc.returncode == 2, proc.stdout
    assert "rejected" in proc.stderr
    proc_malformed = _run_cli(data_dir, "not-a-real-inquiry")
    assert proc_malformed.returncode == 2
    assert "malformed" in proc_malformed.stderr


def test_cli_session_scoped_budget_refused_without_session_exit_one():
    data_dir = _fresh_dir("cli-budget-no-session")
    proc = _run_cli(data_dir, "recent_rejected_action:budget.most_recent")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "SESSION-SCOPED" in proc.stderr


def test_cli_outward_unresolvable_refused_exit_one():
    data_dir = _fresh_dir("cli-outward-unresolvable")
    opts = ["--session-id", "cli-sess-outward"]
    proc = _run_cli(data_dir, "recent_rejected_action:outward.0000000000000000000000000A", *opts)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "does not resolve" in proc.stderr or "no provenance DB" in proc.stderr


def test_cli_internal_detail_emits_registry_anchors():
    data_dir = _fresh_dir("cli-internal-detail")
    proc = _run_cli(data_dir, "host_rule:participation.requires_reason", "--internal-detail")
    assert proc.returncode == 0, proc.stderr
    assert "internal-detail" in proc.stdout
    assert "boundary_rationale_registry.py" in proc.stdout


def test_cli_is_read_only_against_a_real_db():
    """By default the CLI would default --data-dir to anaxi_final; this
    test proves the CLI's evaluation path never writes: a fresh seeded
    synthetic DB is byte-identical before and after a boundary_id
    evaluation."""
    import sqlite3
    import migrate_historical_data
    import provenance_schema

    data_dir = _fresh_dir("cli-read-only")
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    provenance_schema.create_provenance_db(db_path).close()
    pipeline_map = migrate_historical_data.build_pipeline_map({
        "pipelines": {"llama": {"routing_constant_value": "nate"},
                      "claude": {"routing_constant_value": "nate"}}})
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    migrate_historical_data.seed_reference_data(conn, pipeline_map, int(__import__("time").time()))
    conn.close()
    before = _sha256(db_path)
    proc = _run_cli(data_dir, "boundary_id:sleep.one_unresolved_root")
    assert proc.returncode == 0, proc.stderr
    assert "classification: NO_HOST_BOUNDARY_FOUND" in proc.stdout
    assert _sha256(db_path) == before


def test_cli_budget_trace_absent_is_incomplete_exit_zero():
    """CLI/programmatic parity for the lawful-uncertainty semantics: a
    session-scoped budget query with no readable trace reports
    OBSERVATION_INCOMPLETE (never a fabricated complete negative) and
    still exits 0 -- the CLI renders the shared engine's classification."""
    data_dir = _fresh_dir("cli-budget-incomplete")
    proc = _run_cli(data_dir, "recent_rejected_action:budget.most_recent",
                    "--session-id", "cli-sess-budget")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "classification: OBSERVATION_INCOMPLETE" in proc.stdout
    assert "evidence_complete: False" in proc.stdout


def test_cli_never_creates_a_database_when_absent():
    data_dir = _fresh_dir("cli-no-db-created")
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    assert not os.path.exists(db_path)
    proc = _run_cli(data_dir, "host_rule:participation.requires_reason")
    assert proc.returncode == 0, proc.stderr
    assert not os.path.exists(db_path)
    assert os.listdir(data_dir) == []


ALL_TESTS = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def main():
    failed = 0
    for test in ALL_TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(ALL_TESTS)} tests, {len(ALL_TESTS) - failed} passed, {failed} failed")
    import shutil
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
