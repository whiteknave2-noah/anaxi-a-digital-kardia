"""Development protocol builder proof: only synthetic repositories under tmp_path."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SPEC = importlib.util.spec_from_file_location(
    "anaxi_dev", Path(__file__).resolve().parents[1] / "tools/anaxi_dev.py")
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)


def job(ident="A", requires=None, gate="none", field="none"):
    return {"id": ident, "outcome": "Bounded synthetic helper", "requires": requires or [],
            "contract_ref": ["contract.md"], "permissions": {},
            "mutation_scope": {"allowed": ["src/**", ".anaxi-dev/**"], "forbidden": ["src/forbidden*"]},
            "proof": {"required_claims": ["mechanism works"]}, "owner_gate": gate, "field_evidence": field}


class Repo:
    def __init__(self, root, capsys):
        self.root, self.capsys = root, capsys

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True,
                                       stderr=subprocess.PIPE).strip()

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, indent=2) if not isinstance(value, str) else value)
        return path

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic fixture")
        return self.git("rev-parse", "HEAD")

    def setup(self, jobs):
        self.write(".anaxi-dev/JOBS.yaml", {"jobs": jobs})
        self.write("contract.md", "Synthetic bounded contract.\n")
        self.commit()
        return self

    def call(self, *args, error=None):
        code = dev.main(["--repo", str(self.root), *args])
        output = self.capsys.readouterr()
        if error:
            assert code == 2, output
            assert error in output.err, output.err
            return
        assert code == 0, output.err
        return json.loads(output.out)

    def approve(self, ident="A"):
        return self.call("approve", ident, "--actor", "owner", "--reason", "approved bounded contract")

    def claim(self, ident="A", actor="builder"):
        return self.call("claim", ident, "--actor", actor)["writer_lease"]["token"]

    def observation(self, name="proof", source="synthetic", result="established"):
        return self.write(f".anaxi-dev/evidence/{name}.json", {
            "kind": "command_result", "source": source, "result": result,
            "occurred_at": "2026-01-01T00:00:00Z", "observed_at": "2026-01-01T00:01:00Z",
            "details": {"command": ["fixture-check"], "exit_code": 0, "output": "positive fixture assertion"}})

    def receipt(self, ident="A", name="builder", **extra):
        ref = self.observation(name + "-proof")
        test = {"result": "passed", "counts": {"passed": 1, "failed": 0, "xfailed": 0, "skipped": 0},
                "command": ["fixture-check"], "evidence_ref": ref}
        return {"job_id": ident, "builder": "builder", "claims_established": ["mechanism works"],
                "claims_not_established": ["real field behavior"], "claim_evidence": {"mechanism works": [ref]},
                "evidence": [ref], "tests": {"baseline": copy.deepcopy(test), "after": copy.deepcopy(test),
                "pre_existing_failures": [], "new_regressions": [], "newly_suppressed_failures": []},
                **extra}

    def build(self, ident="A"):
        self.approve(ident)
        token = self.claim(ident)
        receipt = self.receipt(ident)
        ref = self.write(f".anaxi-dev/handoffs/{ident}.json", receipt)
        self.write("src/helper.py", "VALUE = 1\n")
        head = self.commit()
        result = self.call("handoff", ident, "--actor", "builder", "--token", token,
                           "--final-head", head, "--handoff", ref)
        self.call("release", ident, "--actor", "builder", "--token", token, "--reason", "builder complete")
        return head, result

    def verify(self, head, ident="A", **extra):
        receipt = self.receipt(ident, "verifier", verifier="verifier", reviewed_head=head,
                               result="passed", **extra)
        ref = self.write(f".anaxi-dev/evidence/{ident}-verification.json", receipt)
        self.commit()
        return self.call("verify", ident, "--actor", "verifier", "--evidence", ref)


@pytest.fixture
def repo(tmp_path, capsys, monkeypatch):
    # No user Git hooks, signing, global configuration or ambient Git paths.
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=codex/test", str(root)], check=True)
    result = Repo(root, capsys)
    result.git("config", "core.hooksPath", str(tmp_path / "no-hooks"))
    return result


def test_linked_worktrees_share_one_global_lease_and_state(repo, tmp_path):
    repo.setup([job(), job("B")])
    repo.approve(); repo.approve("B")
    linked_path = tmp_path / "linked"
    repo.git("worktree", "add", "-qb", "codex/linked", str(linked_path))
    linked = Repo(linked_path, repo.capsys)
    token = repo.claim()
    first = repo.call("status")
    second = linked.call("status")
    assert first["runtime_directory"] == second["runtime_directory"]
    assert first["writer_lease"] == second["writer_lease"]
    assert (linked_path / ".git").is_file()
    assert first["writer_lease"]["role"] == "builder"
    assert first["writer_lease"]["start_head"] == repo.git("rev-parse", "HEAD")
    linked.call("claim", "B", "--actor", "other", error="not NEXT_ELIGIBLE")
    linked.call("release", "A", "--actor", "builder", "--token", token,
                "--reason", "wrong tree", error="does not match")
    path = dev.Protocol(repo.root).lease_path
    lease = json.loads(path.read_text()); lease["acquired_at"] = 0
    path.write_text(json.dumps(lease))
    linked.call("claim", "B", "--actor", "other", error="not NEXT_ELIGIBLE")
    linked.call("abandon", "A", "--actor", "owner", "--token", token,
                "--reason", "operator confirmed abandonment")
    state = dev.Protocol(repo.root).read_state()
    assert state["events"][-1]["reason"] == "operator confirmed abandonment"
    assert repo.call("status")["writer_lease"] is None
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["B"]


def test_missing_permissions_default_deny(repo):
    definition = job(); del definition["permissions"]
    repo.setup([definition])
    assert set(repo.call("status")["jobs"][0]["permissions"].values()) == {"deny"}


def test_dependency_chain_and_deterministic_next(repo):
    repo.setup([job("Z"), job("B", ["A"]), job("A"), job("C", ["B"])])
    for ident in ("Z", "B", "C"):
        repo.approve(ident)
    head, _ = repo.build()
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["Z"]
    repo.verify(head)
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["B", "Z"]
    assert "DEPENDENCIES_UNSATISFIED" in repo.call("next")["blocked"]["C"]


@pytest.mark.parametrize("gate", sorted(dev.GATES))
def test_each_external_gate_blocks_downstream_after_verification(repo, gate):
    definition = job(gate=gate) if gate != "FIELD_EVIDENCE_REQUIRED" else job(field="required")
    repo.setup([definition, job("B", ["A"])])
    repo.approve("B")
    head, _ = repo.build()
    result = repo.verify(head)
    assert result["job"]["phase"] == "VERIFIED"
    assert result["job"]["gates"] == {}
    assert gate in repo.call("next")["blocked"]["A"]
    assert repo.call("next")["NEXT_ELIGIBLE"] == []
    proof = repo.observation("external-gate", source="field" if gate in
                             ("FIELD_EVIDENCE_REQUIRED", "PRODUCTION_WAKE_REQUIRED") else "owner")
    ref = repo.write(".anaxi-dev/evidence/gate.json", {
        "job_id": "A", "owner": "owner", "gate": gate, "decision": "satisfied",
        "reviewed_head": head, "source": "field", "evidence": [proof]})
    repo.commit()
    repo.call("gate", "A", "--actor", "builder", "--gate", gate, "--evidence", ref,
              error="Only the recorded owner")
    assert repo.call("gate", "A", "--actor", "owner", "--gate", gate,
                     "--evidence", ref)["job"]["phase"] == "SATISFIED"
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["B"]


def test_handoff_keeps_separate_claims_and_verifier_identity(repo):
    repo.setup([job()])
    head, result = repo.build()
    assert result["job"]["phase"] == "BUILT"
    receipt = result["job"]["build_receipt"]
    assert receipt["claims_established"] == ["mechanism works"]
    assert receipt["claims_not_established"] == ["real field behavior"]
    assert result["job"]["verifier_state"] == "NOT_STARTED"
    repo.call("verify", "A", "--actor", "builder", "--evidence", "unused",
              error="independent")
    assert repo.verify(head)["job"]["phase"] == "SATISFIED"


@pytest.mark.parametrize("source", ["synthetic", "field"])
def test_synthetic_observation_cannot_satisfy_field_gate_even_if_receipt_claims_field(repo, source):
    repo.setup([job(field="required")])
    head, _ = repo.build(); repo.verify(head)
    proof = repo.observation("synthetic-field-attempt")
    ref = repo.write(".anaxi-dev/evidence/gate.json", {
        "job_id": "A", "owner": "owner", "gate": "FIELD_EVIDENCE_REQUIRED", "decision": "satisfied",
        "reviewed_head": head, "source": source, "evidence": [proof]})
    repo.commit()
    repo.call("gate", "A", "--actor", "owner", "--gate", "FIELD_EVIDENCE_REQUIRED", "--evidence", ref,
              error="Synthetic" if source == "synthetic" else "real field")
    assert repo.call("status")["jobs"][0]["phase"] == "VERIFIED"


def test_read_commands_do_not_create_or_modify_runtime(repo):
    repo.setup([job()])
    protocol = dev.Protocol(repo.root)
    repo.call("status"); repo.call("next"); repo.call("validate")
    assert not protocol.runtime.exists()
    repo.approve()
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in protocol.runtime.iterdir()}
    repo.call("status"); repo.call("next")
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in protocol.runtime.iterdir()}
    assert before == after


def test_invalid_head_and_uncommitted_evidence_fail_closed(repo):
    repo.setup([job()]); repo.approve()
    token = repo.claim()
    receipt = repo.receipt()
    ref = repo.write(".anaxi-dev/handoffs/A.json", receipt)
    head = repo.commit()
    args = ["handoff", "A", "--actor", "builder", "--token", token, "--handoff", ref]
    repo.call(*args, "--final-head", "does-not-exist", error="final HEAD")
    repo.write(ref, {**receipt, "claims_not_established": []})
    repo.call(*args, "--final-head", head, error="clean committed")
    assert repo.call("status")["jobs"][0]["phase"] == "IN_PROGRESS"


def test_baseline_failure_regression_and_suppression_are_distinct(repo):
    repo.setup([job()]); repo.approve(); token = repo.claim()
    receipt = repo.receipt()
    tests = receipt["tests"]
    tests["baseline"].update(result="failed", counts={"passed": 3, "failed": 1, "xfailed": 0, "skipped": 0})
    tests["after"].update(result="failed", counts={"passed": 4, "failed": 2, "xfailed": 1, "skipped": 1})
    tests["pre_existing_failures"] = ["old_failure"]
    tests["new_regressions"] = ["new_failure"]
    tests["newly_suppressed_failures"] = ["xfail added", "skip added", "assertion disabled", "test deleted", "tolerance broadened"]
    ref = repo.write(".anaxi-dev/handoffs/A.json", receipt); head = repo.commit()
    result = repo.call("handoff", "A", "--actor", "builder", "--token", token,
                       "--handoff", ref, "--final-head", head)
    assert result["job"]["build_receipt"]["tests"] == tests


def test_new_skip_cannot_be_hidden_in_passed_summary(repo):
    repo.setup([job()]); repo.approve(); token = repo.claim()
    receipt = repo.receipt(); receipt["tests"]["after"]["counts"]["skipped"] = 1
    ref = repo.write(".anaxi-dev/handoffs/A.json", receipt); head = repo.commit()
    repo.call("handoff", "A", "--actor", "builder", "--token", token,
              "--handoff", ref, "--final-head", head, error="suppression accounting")


def test_failed_verification_records_block_without_promoting_or_editing(repo):
    repo.setup([job()]); head, _ = repo.build()
    receipt = repo.receipt(name="failed-verification", verifier="verifier", reviewed_head=head,
                           result="failed", block_reason="ARCH_CONFLICT")
    receipt.update(claims_established=[], claims_not_established=["mechanism works"], claim_evidence={})
    ref = repo.write(".anaxi-dev/evidence/failed-verification.json", receipt); commit = repo.commit()
    result = repo.call("verify", "A", "--actor", "verifier", "--evidence", ref)
    assert result["job"]["phase"] == "BUILT"
    assert result["job"]["stop_reason"] == "ARCH_CONFLICT"
    assert repo.git("rev-parse", "HEAD") == commit
    assert repo.git("status", "--porcelain") == ""


def test_attempt_limit_is_explicit_per_event_not_per_coding_attempt(repo):
    definition = job(); definition["attempt_policy"] = {"same_event_retry_limit": 1}
    repo.setup([definition]); repo.approve(); token = repo.claim()
    args = ["attempt", "A", "--actor", "builder", "--token", token, "--event", "event-1", "--reason", "manual reservation"]
    repo.call(*args); repo.call(*args)
    repo.call(*args, error="ceiling")
    assert repo.call("status")["jobs"][0]["record"]["attempts"] == {"event-1": 2}


def test_scope_and_contract_drift_fail_closed(repo):
    repo.setup([job()]); repo.approve(); token = repo.claim()
    receipt = repo.receipt(); ref = repo.write(".anaxi-dev/handoffs/A.json", receipt)
    repo.write("src/forbidden.py", "not allowed"); head = repo.commit()
    repo.call("handoff", "A", "--actor", "builder", "--token", token,
              "--handoff", ref, "--final-head", head, error="outside mutation scope")
    repo.write("contract.md", "different contract"); repo.commit()
    repo.call("status", error="Contract drift")


def test_dependency_cycle_rejected(repo):
    repo.setup([job("A", ["B"]), job("B", ["A"])])
    repo.call("next", error="cycle")


def test_explicit_built_dependency_level(repo):
    repo.setup([job(), job("B", [{"id": "A", "level": "BUILT"}])]); repo.approve("B")
    repo.build()
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["B"]


def test_narrative_and_contradictory_claims_are_rejected(repo):
    repo.setup([job()]); repo.approve(); token = repo.claim()
    receipt = repo.receipt(); receipt["claims_not_established"].append("mechanism works")
    ref = repo.write(".anaxi-dev/handoffs/A.json", receipt); head = repo.commit()
    args = ["handoff", "A", "--actor", "builder", "--token", token, "--handoff", ref]
    repo.call(*args, "--final-head", head, error="Contradictory")
    receipt["claims_not_established"].remove("mechanism works")
    repo.write(ref, receipt); repo.write(receipt["evidence"][0], {"text": "Everything looks great"})
    head = repo.commit()
    repo.call(*args, "--final-head", head, error="structured observation")


def test_concurrent_claims_never_grant_two_writers(repo):
    repo.setup([job(), job("B")]); repo.approve(); repo.approve("B")
    command = [sys.executable, "-B", dev.__file__, "--repo", str(repo.root), "claim"]
    processes = [subprocess.Popen([*command, ident, "--actor", ident], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for ident in ("A", "B")]
    results = [(process.communicate(timeout=20), process.returncode) for process in processes]
    assert sorted(code for _, code in results) == [0, 2]
    winner = next(json.loads(output[0])["writer_lease"] for output, code in results if code == 0)
    assert repo.call("status")["writer_lease"] == winner


def test_modified_builder_evidence_cannot_be_verified(repo):
    repo.setup([job()]); head, result = repo.build()
    ref = result["job"]["build_receipt"]["evidence"][0]
    repo.write(ref, {"text": "rewritten proof"})
    receipt = repo.receipt(name="verifier", verifier="verifier", reviewed_head=head, result="passed")
    verification = repo.write(".anaxi-dev/evidence/verification.json", receipt); repo.commit()
    repo.call("verify", "A", "--actor", "verifier", "--evidence", verification, error="immutable")


def test_stop_is_durable_and_done_cannot_skip_acceptance(repo):
    repo.setup([job()]); repo.approve()
    repo.call("stop", "A", "--actor", "owner", "--reason", "ARCH_CONFLICT", "--detail", "Owner decision needed")
    assert repo.call("next")["blocked"]["A"] == ["ARCH_CONFLICT"]
    repo.call("claim", "A", "--actor", "builder", error="not NEXT_ELIGIBLE")
    repo.call("stop", "A", "--actor", "owner", "--reason", "DONE", "--detail", "not accepted", error="SATISFIED")
    repo.call("resume", "A", "--actor", "owner", "--reason", "Decision recorded")
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["A"]


def test_release_preserves_existing_stop_reason_instead_of_stamping_owner_gate(repo):
    repo.setup([job()]); repo.approve(); token = repo.claim()
    repo.call("stop", "A", "--actor", "builder", "--token", token,
              "--reason", "ARCH_CONFLICT", "--detail", "Frozen law conflict discovered mid-build")
    result = repo.call("release", "A", "--actor", "builder", "--token", token,
                       "--reason", "relinquish lease without resolving the conflict")
    assert result["writer_lease"] is None
    assert result["job"]["stop_reason"] == "ARCH_CONFLICT"
    assert result["job"]["stop_detail"] == "Frozen law conflict discovered mid-build"
    assert result["job"]["phase"] == "IN_PROGRESS"
    state = dev.Protocol(repo.root).read_state()
    assert state["jobs"]["A"]["stop_reason"] == "ARCH_CONFLICT"
    status = repo.call("status")
    assert status["writer_lease"] is None
    row = next(r for r in status["jobs"] if r["id"] == "A")
    assert row["stop_reason"] == "ARCH_CONFLICT"
    assert row["phase"] == "IN_PROGRESS"
    assert repo.call("next")["blocked"]["A"] == ["ARCH_CONFLICT"]


def test_release_preserves_any_existing_stop_reason_not_only_arch_conflict(repo):
    repo.setup([job()]); repo.approve(); token = repo.claim()
    repo.call("stop", "A", "--actor", "builder", "--token", token,
              "--reason", "UNRECOVERABLE", "--detail", "Terminal blocker found mid-build")
    result = repo.call("release", "A", "--actor", "builder", "--token", token,
                       "--reason", "relinquish lease")
    assert result["job"]["stop_reason"] == "UNRECOVERABLE"


def test_abandon_preserves_existing_stop_reason_instead_of_stamping_owner_gate(repo):
    repo.setup([job()]); repo.approve(); token = repo.claim()
    repo.call("stop", "A", "--actor", "builder", "--token", token,
              "--reason", "ARCH_CONFLICT", "--detail", "Frozen law conflict discovered mid-build")
    result = repo.call("abandon", "A", "--actor", "owner", "--token", token,
                       "--reason", "operator recovers stranded lease")
    assert result["writer_lease"] is None
    assert result["job"]["stop_reason"] == "ARCH_CONFLICT"


def test_release_still_stamps_owner_gate_when_no_stop_reason_exists(repo):
    repo.setup([job()]); repo.approve(); token = repo.claim()
    result = repo.call("release", "A", "--actor", "builder", "--token", token,
                       "--reason", "paused build")
    assert result["job"]["stop_reason"] == "OWNER_GATE"
    repo.call("resume", "A", "--actor", "owner", "--reason", "continue bounded work")


def test_release_does_not_alter_built_or_verified_phase_or_stop_reason(repo):
    repo.setup([job()])
    head, result = repo.build()
    assert result["job"]["phase"] == "BUILT"
    assert result["job"]["stop_reason"] is None
    verify_result = repo.verify(head)
    assert verify_result["job"]["phase"] == "SATISFIED"
    assert verify_result["job"]["stop_reason"] is None


def test_release_cannot_be_replayed_after_lease_already_cleared(repo):
    repo.setup([job()]); repo.approve(); token = repo.claim()
    repo.call("release", "A", "--actor", "builder", "--token", token, "--reason", "first release")
    repo.call("release", "A", "--actor", "builder", "--token", token,
              "--reason", "second release", error="No lease for this job")


def test_abandon_wrong_token_cannot_clear_another_writers_lease(repo):
    repo.setup([job()]); repo.approve(); repo.claim()
    repo.call("abandon", "A", "--actor", "owner", "--token", "not-the-real-token",
              "--reason", "attempted recovery", error="current lease token")


def test_gate_cannot_use_rewritten_verification_record(repo):
    repo.setup([job(gate="OWNER_ACCEPTANCE_REQUIRED")])
    head, _ = repo.build(); result = repo.verify(head)
    verification = result["job"]["verification_ref"]
    repo.write(verification, {"text": "replacement narrative"})
    proof = repo.observation("owner", source="owner")
    ref = repo.write(".anaxi-dev/evidence/acceptance.json", {
        "job_id": "A", "owner": "owner", "gate": "OWNER_ACCEPTANCE_REQUIRED",
        "reviewed_head": head, "decision": "satisfied", "evidence": [proof]})
    repo.commit()
    repo.call("gate", "A", "--actor", "owner", "--gate", "OWNER_ACCEPTANCE_REQUIRED",
              "--evidence", ref, error="immutable")


def test_dependency_result_must_be_present_in_claiming_worktree(repo, tmp_path):
    repo.setup([job(), job("B", ["A"])]); repo.approve("B")
    linked_path = tmp_path / "old-checkout"
    repo.git("worktree", "add", "-qb", "codex/old", str(linked_path))
    head, _ = repo.build(); repo.verify(head)
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["B"]
    linked = Repo(linked_path, repo.capsys)
    assert "DEPENDENCY_HEAD_NOT_IN_WORKTREE" in linked.call("next")["blocked"]["B"]
    linked.call("claim", "B", "--actor", "builder-2", error="not NEXT_ELIGIBLE")


def test_reclaim_cannot_hide_previous_out_of_scope_edits(repo):
    repo.setup([job()]); repo.approve(); token = repo.claim()
    repo.write("src/forbidden.py", "forbidden earlier edit"); repo.commit()
    repo.call("release", "A", "--actor", "builder", "--token", token, "--reason", "paused build")
    repo.call("resume", "A", "--actor", "owner", "--reason", "continue bounded work")
    token = repo.claim()
    ref = repo.write(".anaxi-dev/handoffs/A.json", repo.receipt()); head = repo.commit()
    repo.call("handoff", "A", "--actor", "builder", "--token", token, "--handoff", ref,
              "--final-head", head, error="outside mutation scope")


def test_post_terminal_evidence_drift_is_detected_and_blocks_dependents(repo):
    repo.setup([job(), job("B", ["A"]), job("Z")])
    repo.approve("B")
    head, _ = repo.build()
    assert repo.verify(head)["job"]["phase"] == "SATISFIED"

    # Build and verify Z independently with distinctly named evidence (default
    # fixture evidence names are shared across jobs) so it is a true untouched
    # control, unaffected by tampering with A's evidence below.
    repo.approve("Z")
    token_z = repo.claim("Z", actor="builder-z")
    receipt_z = repo.receipt("Z", "builder-z", builder="builder-z")
    handoff_ref = repo.write(".anaxi-dev/handoffs/Z.json", receipt_z)
    repo.write("src/z-helper.py", "VALUE = 1\n")
    head_z = repo.commit()
    repo.call("handoff", "Z", "--actor", "builder-z", "--token", token_z,
              "--final-head", head_z, "--handoff", handoff_ref)
    repo.call("release", "Z", "--actor", "builder-z", "--token", token_z, "--reason", "z builder complete")
    verification_z = repo.receipt("Z", "verifier-z", verifier="verifier-z",
                                  reviewed_head=head_z, result="passed")
    verify_ref = repo.write(".anaxi-dev/evidence/Z-verification.json", verification_z)
    repo.commit()
    result_z = repo.call("verify", "Z", "--actor", "verifier-z", "--evidence", verify_ref)
    assert result_z["job"]["phase"] == "SATISFIED"

    protocol = dev.Protocol(repo.root)
    state_before = protocol.state_path.read_bytes()
    lease_before = protocol.lease_path.exists()

    # Clean before tampering: validate/status/next all succeed and B is eligible.
    repo.call("validate")
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["B"]

    # Rewrite A's already-recorded, pinned evidence file in a new commit, after
    # A reached its terminal SATISFIED state.
    status_before = repo.call("status")
    row_a_before = next(r for r in status_before["jobs"] if r["id"] == "A")
    evidence_ref = row_a_before["record"]["build_receipt"]["evidence"][0]
    repo.write(evidence_ref, {"text": "rewritten after terminal satisfaction"})
    repo.commit()

    repo.call("validate", error="Evidence integrity failed")

    status_after = repo.call("status")
    row_a = next(r for r in status_after["jobs"] if r["id"] == "A")
    assert row_a["evidence_integrity_problems"]
    assert "EVIDENCE_INTEGRITY_FAILED" in row_a["ineligible_reasons"]

    nxt = repo.call("next")
    assert nxt["NEXT_ELIGIBLE"] == []
    assert "DEPENDENCY_EVIDENCE_INTEGRITY_FAILED" in nxt["blocked"]["B"]

    row_z = next(r for r in status_after["jobs"] if r["id"] == "Z")
    assert row_z["evidence_integrity_problems"] == []

    assert protocol.state_path.read_bytes() == state_before
    assert protocol.lease_path.exists() == lease_before


def build_and_satisfy(repo, ident):
    """Build, verify and satisfy ident using evidence file names scoped to
    ident, so multiple jobs in one synthetic repo never share a pinned blob
    path (the default fixture evidence names are shared across jobs)."""
    repo.approve(ident)
    token = repo.claim(ident, actor=f"builder-{ident}")
    receipt = repo.receipt(ident, f"builder-{ident}", builder=f"builder-{ident}")
    handoff_ref = repo.write(f".anaxi-dev/handoffs/{ident}.json", receipt)
    repo.write(f"src/{ident.lower()}-helper.py", "VALUE = 1\n")
    head = repo.commit()
    repo.call("handoff", ident, "--actor", f"builder-{ident}", "--token", token,
              "--final-head", head, "--handoff", handoff_ref)
    repo.call("release", ident, "--actor", f"builder-{ident}", "--token", token,
              "--reason", f"{ident} builder complete")
    verification = repo.receipt(ident, f"verifier-{ident}", verifier=f"verifier-{ident}",
                                reviewed_head=head, result="passed")
    verify_ref = repo.write(f".anaxi-dev/evidence/{ident}-verification.json", verification)
    repo.commit()
    result = repo.call("verify", ident, "--actor", f"verifier-{ident}", "--evidence", verify_ref)
    assert result["job"]["phase"] == "SATISFIED"
    return head


def tamper_evidence(repo, ident):
    status = repo.call("status")
    row = next(r for r in status["jobs"] if r["id"] == ident)
    evidence_ref = row["record"]["build_receipt"]["evidence"][0]
    repo.write(evidence_ref, {"text": f"rewritten after {ident} reached terminal satisfaction"})
    repo.commit()


def test_transitive_dependency_evidence_failure_blocks_grandchild(repo):
    # Reproduces the verifier's exact failed case: A <- B <- C, only A tampered.
    repo.setup([job("A"), job("B", ["A"]), job("C", ["B"])])
    build_and_satisfy(repo, "A")
    build_and_satisfy(repo, "B")
    repo.approve("C")
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["C"]

    tamper_evidence(repo, "A")

    status = repo.call("status")
    rows = {r["id"]: r for r in status["jobs"]}
    assert "EVIDENCE_INTEGRITY_FAILED" in rows["A"]["ineligible_reasons"]
    assert "DEPENDENCY_EVIDENCE_INTEGRITY_FAILED" in rows["B"]["ineligible_reasons"]
    assert "DEPENDENCY_EVIDENCE_INTEGRITY_FAILED" in rows["C"]["ineligible_reasons"]
    assert rows["B"]["dependencies_with_evidence_integrity_failed"] == ["A"]
    assert rows["C"]["dependencies_with_evidence_integrity_failed"] == ["A"]

    nxt = repo.call("next")
    assert nxt["NEXT_ELIGIBLE"] == []
    assert "DEPENDENCY_EVIDENCE_INTEGRITY_FAILED" in nxt["blocked"]["C"]

    # Historical state is untouched: B lawfully reached SATISFIED and stays SATISFIED.
    assert rows["B"]["phase"] == "SATISFIED"


def test_transitive_dependency_evidence_failure_propagates_through_deep_chain(repo):
    # A <- B <- C <- D; compromising A must still block D, not just its parent C.
    repo.setup([job("A"), job("B", ["A"]), job("C", ["B"]), job("D", ["C"])])
    build_and_satisfy(repo, "A")
    build_and_satisfy(repo, "B")
    build_and_satisfy(repo, "C")
    repo.approve("D")
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["D"]

    tamper_evidence(repo, "A")

    nxt = repo.call("next")
    assert nxt["NEXT_ELIGIBLE"] == []
    assert "DEPENDENCY_EVIDENCE_INTEGRITY_FAILED" in nxt["blocked"]["D"]

    status = repo.call("status")
    row_d = next(r for r in status["jobs"] if r["id"] == "D")
    assert row_d["dependencies_with_evidence_integrity_failed"] == ["A"]


def test_clean_chain_has_no_false_positive_integrity_blocking(repo):
    # Same depth as the deep-chain case, but nothing is tampered with.
    repo.setup([job("A"), job("B", ["A"]), job("C", ["B"]), job("D", ["C"])])
    build_and_satisfy(repo, "A")
    build_and_satisfy(repo, "B")
    build_and_satisfy(repo, "C")
    repo.approve("D")

    status = repo.call("status")
    for ident in ("A", "B", "C", "D"):
        row = next(r for r in status["jobs"] if r["id"] == ident)
        assert row["evidence_integrity_problems"] == []
        assert row["dependencies_with_evidence_integrity_failed"] == []
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["D"]


def test_dependency_evidence_failure_is_scoped_to_its_own_lineage(repo):
    # A <- B <- C is compromised at A; the unrelated X <- Y lineage must not
    # be affected merely because it exists in the same repository.
    repo.setup([job("A"), job("B", ["A"]), job("C", ["B"]), job("X"), job("Y", ["X"])])
    build_and_satisfy(repo, "A")
    build_and_satisfy(repo, "B")
    repo.approve("C")
    build_and_satisfy(repo, "X")
    repo.approve("Y")
    assert repo.call("next")["NEXT_ELIGIBLE"] == ["C", "Y"]

    tamper_evidence(repo, "A")

    nxt = repo.call("next")
    assert nxt["NEXT_ELIGIBLE"] == ["Y"]
    assert "DEPENDENCY_EVIDENCE_INTEGRITY_FAILED" in nxt["blocked"]["C"]
    assert "Y" not in nxt["blocked"]

    status = repo.call("status")
    rows = {r["id"]: r for r in status["jobs"]}
    assert rows["X"]["evidence_integrity_problems"] == []
    assert rows["Y"]["dependencies_with_evidence_integrity_failed"] == []


def test_transitive_integrity_checks_remain_read_only(repo):
    repo.setup([job("A"), job("B", ["A"]), job("C", ["B"])])
    build_and_satisfy(repo, "A")
    build_and_satisfy(repo, "B")
    repo.approve("C")
    tamper_evidence(repo, "A")

    protocol = dev.Protocol(repo.root)
    state_before = protocol.state_path.read_bytes()
    lease_before = protocol.lease_path.exists()

    repo.call("status")
    repo.call("next")
    repo.call("validate", error="Evidence integrity failed")

    assert protocol.state_path.read_bytes() == state_before
    assert protocol.lease_path.exists() == lease_before
