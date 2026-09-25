import pytest

from claim_harness.evidence import parse_junit
from claim_harness.inventory import AccountingError
from helpers import junit


def test_outcomes_read_from_junit():
    run = parse_junit(junit(test_a="passed", test_b="failed", test_c="error", test_d="skipped"), source="j.xml")
    outcomes = {name: r.outcome for (_, name), r in run.results.items()}
    assert outcomes == {"test_a": "passed", "test_b": "failed", "test_c": "error", "test_d": "skipped"}
    assert run.results[("tests_demo.test_demo", "test_b")].message == "assert 1 == 2"
    assert run.run_id.startswith("junit-sha256:") and len(run.sha256) == 64
    assert run.suites[0]["timestamp"] == "2026-01-01T00:00:00"


def test_single_testsuite_root_and_properties():
    xml = (b'<testsuite name="pytest"><properties><property name="claim_harness.network_guard" value="active"/>'
           b'</properties><testcase classname="m" name="t"/></testsuite>')
    run = parse_junit(xml)
    assert run.properties == {"claim_harness.network_guard": "active"}
    assert run.results[("m", "t")].outcome == "passed"


def test_duplicate_testcases_are_flagged_and_keep_the_worst_outcome():
    xml = (b'<testsuite><testcase classname="m" name="t"><failure message="no"/></testcase>'
           b'<testcase classname="m" name="t"/></testsuite>')
    run = parse_junit(xml)
    assert ("m", "t") in run.duplicates
    assert run.results[("m", "t")].outcome == "failed"


def test_collection_errors_recorded_by_module():
    xml = b'<testsuite><testcase classname="" name="pkg.test_broken"><error message="collection failure"/></testcase></testsuite>'
    run = parse_junit(xml)
    assert run.collection_errors == {"pkg.test_broken": "collection failure"}
    assert run.results == {}


@pytest.mark.parametrize("xml", [b"not xml", b"<report/>"])
def test_invalid_junit_is_an_accounting_error(xml):
    with pytest.raises(AccountingError):
        parse_junit(xml)
