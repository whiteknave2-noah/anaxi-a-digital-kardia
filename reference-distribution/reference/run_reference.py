#!/usr/bin/env python3
"""ANAXI reference distribution A -- the public reproducibility runner.

    python reference/run_reference.py setup-model   # once, needs network: caches the embedding model
    python reference/run_reference.py run           # offline reproducibility run -> reference_results/
    python reference/run_reference.py reset         # remove generated results and caches of previous runs

Run from the distribution root, inside the Python 3.14 virtual environment the
README creates. Every child process gets a scrubbed environment: no PYTHONPATH,
no user site-packages, the embedding-model cache inside this distribution
(.hf_home/), Hugging Face offline, and (for the test run) ANAXI's own
network guard, which blocks every non-loopback connection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DIST = Path(__file__).resolve().parent.parent
ANAXI = DIST / "anaxi_final"
RESULTS = DIST / "reference_results"
HF_HOME = DIST / ".hf_home"
PROVENANCE = DIST / "DISTRIBUTION_PROVENANCE.json"
RESULTS_MARKER = ".anaxi_reference_results"
FIXTURES = DIST / "reference_fixtures" / "public_resource_collection"
EVIDENCE = ANAXI / "completion_evidence"
EVIDENCE_FILES = ("synthetic_public_resources.json", "synthetic_public_resource_delivery.json")
# generated at run time and removed by `reset`; never part of the distribution itself
RUNTIME_DIRS = {".venv", ".hf_home", "reference_results", "__pycache__", ".pytest_cache"}


def child_env(*, offline: bool = True) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTHON", "ANAXI_", "HF_", "TRANSFORMERS_", "SENTENCE_TRANSFORMERS_"))}
    env.update({"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "HF_HOME": str(HF_HOME)})
    if offline:
        env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    return env


def run_child(args: list[str], log: Path | None = None, *, offline: bool = True, timeout: int = 3600) -> int:
    proc = subprocess.run([sys.executable, *args], cwd=ANAXI, env=child_env(offline=offline),
                          capture_output=True, text=True, timeout=timeout)
    if log is not None:
        log.write_text(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""), encoding="utf-8")
    return proc.returncode


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- checks ------------------------------------------------------------------

def check_environment() -> dict:
    problems = []
    if sys.platform != "darwin":
        problems.append(f"unsupported platform {sys.platform!r}: this distribution supports macOS only")
    if sys.version_info[:2] != (3, 14):
        problems.append(f"Python {platform.python_version()} found; Python 3.14 is required")
    if sys.prefix == sys.base_prefix:
        problems.append("not running inside a virtual environment (see README: python3.14 -m venv .venv)")
    return {"status": "FAIL" if problems else "PASS", "problems": problems,
            "python": platform.python_version(), "macos": platform.mac_ver()[0], "machine": platform.machine()}


def check_integrity() -> dict:
    """Every provenance-listed file present and unmodified; nothing else outside runtime dirs."""
    prov = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    listed = {f["path"]: f["sha256"] for f in prov["files"]}
    modified = [p for p, h in listed.items() if not (DIST / p).is_file() or _sha(DIST / p) != h]
    unexpected = []
    for root, dirs, files in os.walk(DIST):
        dirs[:] = [d for d in dirs if d not in RUNTIME_DIRS]
        for name in files:
            rel = (Path(root) / name).relative_to(DIST).as_posix()
            if rel not in listed and rel not in prov["metadata_files"]:
                unexpected.append(rel)
    status = "PASS" if not modified and not unexpected else "FAIL"
    return {"status": status, "files_checked": len(listed), "missing_or_modified": modified[:50],
            "unexpected_files": unexpected[:50], "source_commit": prov["source_commit"]}


def check_import_isolation() -> dict:
    """ANAXI modules resolve inside this distribution; no path entry points at another checkout."""
    probe = ("import json, sys, llama_anaxi, scenario_evidence, reference_harness_v0 as h, claude_agent;"
             "print(json.dumps({'files': [llama_anaxi.__file__, scenario_evidence.__file__, h.__file__,"
             " claude_agent.__file__], 'path': sys.path, 'prefix': sys.prefix, 'base': sys.base_prefix}))")
    code = f"import sys; sys.path.insert(0, {str(DIST)!r}); {probe}"
    proc = subprocess.run([sys.executable, "-c", code], cwd=ANAXI, env=child_env(), capture_output=True, text=True)
    if proc.returncode:
        return {"status": "FAIL", "problems": [proc.stderr[-1500:]]}
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    roots = [str(DIST), info["prefix"], info["base"]]
    outside = [p for p in info["path"] if p and not any(str(Path(p).resolve()).startswith(str(Path(r).resolve()))
                                                         for r in roots)]
    stray = [f for f in info["files"] if not str(Path(f).resolve()).startswith(str(DIST))]
    return {"status": "PASS" if not outside and not stray else "FAIL",
            "modules_resolved_in_distribution": not stray, "path_entries_outside_distribution_or_venv": outside}


def check_fixtures_and_evidence(results: Path) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import synthetic_collection
    regenerated = synthetic_collection.collection()
    shipped = {p.relative_to(FIXTURES).as_posix(): p.read_bytes() for p in FIXTURES.rglob("*") if p.is_file()}
    fixtures_ok = regenerated == shipped
    out = results / "resource_evidence"
    code = run_child([str(DIST / "reference" / "resource_evidence.py"), str(ANAXI), str(FIXTURES), str(out)],
                     results / "resource_evidence.log")
    same = {name: (out / name).is_file() and (out / name).read_bytes() == (EVIDENCE / name).read_bytes()
            for name in EVIDENCE_FILES}
    status = "PASS" if fixtures_ok and code == 0 and all(same.values()) else "FAIL"
    return {"status": status, "fixtures_regenerate_identically": fixtures_ok, "assays_exit_code": code,
            "evidence_reproduced_byte_identical": same}


def run_harness(results: Path) -> dict:
    log = results / "reference_harness.log"
    code = run_child(["reference_harness_v0.py"], log)
    tail = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()][-6:]
    return {"status": "PASS" if code == 0 else "FAIL", "exit_code": code, "summary_tail": tail}


def run_accounting(results: Path) -> dict:
    report = results / "scenario_report.json"
    code = run_child(["scenario_evidence.py", "--output", str(report)], results / "scenario_evidence.log")
    if not report.is_file():
        return {"status": "FAIL", "problems": [f"scenario_evidence.py exited {code} without a report"]}
    ledger = results / "ledger.json"
    run_child(["generate_current_completion_ledger.py", "--inventory", "owner_completion_inventory.json",
               "--scenario-report", str(report),
               "--release-contract", "completion_evidence/release_contract_2026-09-24.json",
               "--output", str(ledger)], results / "ledger.log")
    inventory = json.loads((ANAXI / "owner_completion_inventory.json").read_text(encoding="utf-8"))
    bindings = json.loads((EVIDENCE / "scenario_bindings.json").read_text(encoding="utf-8"))
    public = bindings["public_distribution"]
    items = json.loads(report.read_text(encoding="utf-8"))["item_results"]
    passed = sorted(f"{c}/{s}" for c, m in items.items() for s, r in m.items() if r["status"] == "PASS")
    failed = {f"{c}/{s}": r.get("reason", "")[:400] for c, m in items.items() for s, r in m.items()
              if r["status"] != "PASS"}
    omitted = sorted(f"{o['capability_id']}/{o['scenario']}" for o in public["omitted_production_only"])
    all_scenarios = {f"{c['id']}/{s}" for c in inventory["capabilities"] for s in c["scenarios"]}
    bound = set(passed) | set(failed)
    unbound = sorted(all_scenarios - bound)
    live_only = sum(len(c["live_only_requirements"]) for c in inventory["capabilities"])
    ledger_doc = json.loads(ledger.read_text(encoding="utf-8")) if ledger.is_file() else {}
    contract = ledger_doc.get("release_contract", {})
    ok = not failed and set(unbound) == set(omitted) and bound
    return {
        "status": "PASS" if ok else "FAIL",
        "inventory_scenarios": len(all_scenarios),
        "reproducible_bound_scenarios": len(bound),
        "reproducible_PASS": len(passed),
        "reproducible_FAIL": failed,
        "NOT_ATTEMPTED_production_only": omitted,
        "unbound_scenarios": unbound,
        "removed_nodes_outside_macos_contract": [r["node"] for r in public["removed_nodes_for_excluded_surfaces"]],
        "live_only_requirements": {"total": live_only, "established_locally": 0,
                                   "status": "NOT_REPRODUCIBLE_LOCALLY (live-only by definition; no live evidence ships)"},
        "withdrawn_from_release_contract": [w["capability_id"] for w in contract.get("withdrawn_from_release", [])],
        "ledger_whole_system_status": ledger_doc.get("whole_system", {}).get("status"),
        "ledger_note": "The ledger stays NOT_COMPLETE locally because live-only requirements need live evidence, "
                       "which is not reproducible outside production. Offline scenarios are the reproducible claims.",
        "bound_test_nodes_run": len(json.loads(report.read_text(encoding="utf-8"))["node_outcomes"]),
    }


# --- commands ----------------------------------------------------------------

def prepare_results_dir() -> Path:
    if RESULTS.exists():
        if not (RESULTS / RESULTS_MARKER).is_file():
            raise SystemExit(f"{RESULTS} exists but was not created by this runner; move it away first.")
        shutil.rmtree(RESULTS)
    RESULTS.mkdir()
    (RESULTS / RESULTS_MARKER).write_text("created by reference/run_reference.py\n", encoding="utf-8")
    return RESULTS


def cmd_run() -> int:
    env = check_environment()
    if env["status"] != "PASS":
        print(json.dumps(env, indent=1))
        return 2
    results = prepare_results_dir()
    summary: dict = {"source_release": json.loads(PROVENANCE.read_text(encoding="utf-8"))["source_commit"],
                     "environment": env}
    steps = [("distribution_integrity_before", lambda: check_integrity()),
             ("import_isolation", lambda: check_import_isolation()),
             ("synthetic_resources", lambda: check_fixtures_and_evidence(results)),
             ("reference_harness", lambda: run_harness(results)),
             ("scenario_accounting", lambda: run_accounting(results)),
             ("distribution_integrity_after", lambda: check_integrity())]
    for name, step in steps:
        print(f"[{name}] ...", flush=True)
        summary[name] = step()
        print(f"[{name}] {summary[name]['status']}", flush=True)
    checks = [summary[n]["status"] for n, _ in steps]
    summary["overall"] = "REPRODUCED" if all(s == "PASS" for s in checks) else "NOT_REPRODUCED"
    (results / "SUMMARY.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    acc = summary["scenario_accounting"]
    print()
    print(f"overall: {summary['overall']}")
    if "reproducible_PASS" in acc:
        print(f"reproducible scenarios: {acc['reproducible_PASS']}/{acc['reproducible_bound_scenarios']} PASS, "
              f"{len(acc['reproducible_FAIL'])} FAIL (of {acc['inventory_scenarios']} inventory scenarios)")
        print(f"NOT_ATTEMPTED (production-only): {len(acc['NOT_ATTEMPTED_production_only'])}")
        print(f"live-only requirements not reproducible locally: {acc['live_only_requirements']['total']}")
        print(f"withdrawn from the release contract: {', '.join(acc['withdrawn_from_release_contract']) or 'none'}")
    print(f"details: {results / 'SUMMARY.json'}")
    return 0 if summary["overall"] == "REPRODUCED" else 1


def cmd_setup_model() -> int:
    env = check_environment()
    if env["status"] != "PASS":
        print(json.dumps(env, indent=1))
        return 2
    HF_HOME.mkdir(exist_ok=True)
    proc = subprocess.run([sys.executable, "prepare_embedding_model.py"], cwd=ANAXI,
                          env=child_env(offline=False), text=True)
    return proc.returncode


def cmd_reset() -> int:
    removed = []
    if RESULTS.exists():
        if not (RESULTS / RESULTS_MARKER).is_file():
            raise SystemExit(f"{RESULTS} was not created by this runner; not removing it.")
        shutil.rmtree(RESULTS)
        removed.append(str(RESULTS.relative_to(DIST)))
    for root, dirs, _files in os.walk(DIST):
        dirs[:] = [d for d in dirs if d not in (".venv", ".hf_home")]
        for d in list(dirs):
            if d in ("__pycache__", ".pytest_cache"):
                shutil.rmtree(Path(root) / d)
                removed.append(str((Path(root) / d).relative_to(DIST)))
                dirs.remove(d)
    print(json.dumps({"removed": removed, "kept": [".venv", ".hf_home"]}, indent=1))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("setup-model", "run", "reset"))
    args = ap.parse_args(argv)
    return {"setup-model": cmd_setup_model, "run": cmd_run, "reset": cmd_reset}[args.command]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
