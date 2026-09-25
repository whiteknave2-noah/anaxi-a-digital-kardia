"""Mechanically classify every file changed between an evidenced code HEAD and another HEAD.

Verdicts:
  EVIDENCE_ONLY                  only completion_evidence/**, root reports/continuation.
  EVIDENCE_TOOLING_CHANGED       additionally evidence-generation tooling changed (never imported by runtime).
  EXECUTABLE_CODE_OR_TEST_CHANGED runtime code or tests changed: the evidence does NOT cover the newer HEAD.

Exit status is 0 only for the first two verdicts.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

TOOLING = {
    "anaxi_final/generate_current_completion_ledger.py", "anaxi_final/scenario_evidence.py",
    "anaxi_final/completion_evidence.py", "anaxi_final/public_resource_completion_assay.py",
    "anaxi_final/public_resource_delivery_assay.py",
    "anaxi_final/verify_evidence_boundary.py",
}
REPORT_SUFFIXES = (".md",)


def classify(path: str) -> str:
    if path.startswith("anaxi_final/completion_evidence/"):
        return "evidence"
    if "/" not in path and path.endswith(REPORT_SUFFIXES):
        return "report"
    if path in TOOLING:
        return "evidence_tooling"
    name = path.rsplit("/", 1)[-1]
    if name.startswith("test_") and name.endswith(".py"):
        return "test"
    return "runtime_or_other"


def runtime_importers_of_tooling() -> list[str]:
    """Runtime modules importing evidence tooling (must be empty)."""
    tooling_modules = {Path(t).stem for t in TOOLING}
    offenders = []
    for path in sorted(HERE.glob("*.py")):
        rel = f"anaxi_final/{path.name}"
        if classify(rel) != "runtime_or_other":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            if tooling_modules & set(names):
                offenders.append(path.name)
    return offenders


def changed(base: str, head: str) -> list[dict]:
    out = subprocess.run(["git", "diff", "--name-status", base, head], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        rows.append({"status": status, "path": path, "class": classify(path)})
    return rows


def verdict(rows: list[dict], offenders: list[str]) -> str:
    classes = {r["class"] for r in rows}
    if classes & {"test", "runtime_or_other"} or offenders:
        return "EXECUTABLE_CODE_OR_TEST_CHANGED"
    if "evidence_tooling" in classes:
        return "EVIDENCE_TOOLING_CHANGED"
    return "EVIDENCE_ONLY"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="evidenced code HEAD")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    rows = changed(args.base, args.head)
    offenders = runtime_importers_of_tooling()
    result = {
        "base": subprocess.run(["git", "rev-parse", args.base], cwd=REPO, capture_output=True, text=True).stdout.strip(),
        "head": subprocess.run(["git", "rev-parse", args.head], cwd=REPO, capture_output=True, text=True).stdout.strip(),
        "changed_files": rows, "runtime_modules_importing_evidence_tooling": offenders,
        "verdict": verdict(rows, offenders),
    }
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["verdict"] != "EXECUTABLE_CODE_OR_TEST_CHANGED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
