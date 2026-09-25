"""Small builders for synthetic inventories, bindings and JUnit files."""

import json
from pathlib import Path

from claim_harness.inventory import INVENTORY_SCHEMA
from claim_harness.bindings import BINDINGS_SCHEMA

TEST_FILE = "tests_demo/test_demo.py"


def inventory(*claims, **policy):
    data = {"schema": INVENTORY_SCHEMA, "project": "demo", "claims": list(claims)}
    if policy:
        data["policy"] = policy
    return data


def claim(cid, evidence="executable", **extra):
    return {"id": cid, "statement": f"statement of {cid}", "evidence": evidence, **extra}


def bindings(**by_claim):
    return {"schema": BINDINGS_SCHEMA,
            "bindings": {cid.replace("_", "-"): [f"{TEST_FILE}::{n}" for n in names]
                         for cid, names in by_claim.items()}}


def junit(**outcomes):
    """junit(test_a="passed", test_b="failed", ...) -> JUnit XML bytes as pytest writes it."""
    cases = []
    for name, outcome in outcomes.items():
        body = {"passed": "", "failed": '<failure message="assert 1 == 2">trace</failure>',
                "error": '<error message="fixture broke">trace</error>',
                "skipped": '<skipped type="pytest.skip" message="not today"/>'}[outcome]
        cases.append(f'<testcase classname="tests_demo.test_demo" name="{name}" time="0.01">{body}</testcase>')
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="%d" '
            'timestamp="2026-01-01T00:00:00" hostname="h">%s</testsuite></testsuites>'
            % (len(cases), "".join(cases))).encode()


def write_project(root: Path, names=("test_a", "test_b", "test_c", "test_d")):
    """A project tree whose demo test file defines the given test functions."""
    (root / "tests_demo").mkdir(parents=True, exist_ok=True)
    (root / TEST_FILE).write_text("".join(f"def {n}():\n    pass\n\n" for n in names))
    return root


def dump(path: Path, data) -> Path:
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    return path
