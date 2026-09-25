"""Executed evidence: read what a pytest run actually did from its JUnit XML.

Produce the file with ``pytest --junitxml=PATH``.  Nothing here imports pytest;
the JUnit file is the whole interface, so the record of what ran is the same
file a CI system would archive.

Per-test outcomes:

``passed``    ran and passed
``failed``    ran and an assertion failed (includes strict xpass)
``error``     setup/teardown or collection broke; the test did not establish anything
``skipped``   deliberately not executed (skip, skipif, xfail)
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .inventory import AccountingError

PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"
_MESSAGE_LIMIT = 500


@dataclass(frozen=True)
class TestResult:
    classname: str
    name: str
    outcome: str
    message: str = ""
    seconds: Optional[float] = None


@dataclass
class EvidenceRun:
    """One JUnit file: its identity, run configuration and per-test outcomes."""

    source: str
    sha256: str
    run_id: str
    suites: list = field(default_factory=list)  # [{name, timestamp, hostname, tests, failures, ...}]
    properties: dict = field(default_factory=dict)  # testsuite-level properties
    results: dict = field(default_factory=dict)  # (classname, name) -> TestResult
    duplicates: set = field(default_factory=set)  # keys that appeared more than once
    collection_errors: dict = field(default_factory=dict)  # dotted module -> message

    def describe(self) -> dict:
        return {
            "run_id": self.run_id,
            "source": self.source,
            "sha256": self.sha256,
            "suites": self.suites,
            "properties": self.properties,
            "testcases": len(self.results),
        }


def _message(element) -> str:
    text = element.get("message") or (element.text or "")
    text = " ".join(text.split())
    return text[:_MESSAGE_LIMIT]


def _outcome(case) -> tuple:
    failure = case.find("failure")
    error = case.find("error")
    skipped = case.find("skipped")
    if error is not None:
        return ERROR, _message(error)
    if failure is not None:
        return FAILED, _message(failure)
    if skipped is not None:
        return SKIPPED, _message(skipped)
    return PASSED, ""


def parse_junit(text: bytes, source: str = "<memory>", run_id: Optional[str] = None) -> EvidenceRun:
    digest = hashlib.sha256(text).hexdigest()
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise AccountingError([f"{source}: not valid JUnit XML ({exc})"]) from None
    if root.tag == "testsuites":
        suites = root.findall("testsuite")
    elif root.tag == "testsuite":
        suites = [root]
    else:
        raise AccountingError([f"{source}: root element must be <testsuites> or <testsuite>, got <{root.tag}>"])

    run = EvidenceRun(source=source, sha256=digest, run_id=run_id or f"junit-sha256:{digest[:16]}")
    for suite in suites:
        run.suites.append({k: suite.get(k) for k in
                           ("name", "timestamp", "hostname", "tests", "failures", "errors", "skipped")
                           if suite.get(k) is not None})
        for prop in suite.findall("properties/property"):
            run.properties[prop.get("name", "")] = prop.get("value", "")
        for case in suite.iter("testcase"):
            classname = case.get("classname", "")
            name = case.get("name", "")
            outcome, message = _outcome(case)
            if not classname and outcome == ERROR:
                # pytest reports a module that failed to import/collect as a nameless error case.
                run.collection_errors[name] = message
                continue
            key = (classname, name)
            seconds = case.get("time")
            result = TestResult(classname, name, outcome, message,
                                float(seconds) if seconds not in (None, "") else None)
            if key in run.results:
                run.duplicates.add(key)
                # keep the worst outcome so a duplicate can never hide a failure
                order = [PASSED, SKIPPED, FAILED, ERROR]
                if order.index(outcome) <= order.index(run.results[key].outcome):
                    continue
            run.results[key] = result
    return run


def load_junit(path, run_id: Optional[str] = None) -> EvidenceRun:
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise AccountingError([f"{path}: JUnit file not found (run pytest with --junitxml={path})"]) from None
    return parse_junit(data, source=str(path), run_id=run_id)


def sha256_file(path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
