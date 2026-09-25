import pytest

from claim_harness.bindings import parse_bindings
from claim_harness.evidence import parse_junit
from claim_harness.inventory import AccountingError, parse_inventory
from claim_harness.ledger import build_ledger
from helpers import bindings, claim, inventory, junit


def ledger(claims, binds, *xmls, **policy):
    inv = parse_inventory(inventory(*claims, **policy))
    b = parse_bindings(binds, inv)
    return build_ledger(inv, b, [parse_junit(x, source=f"run{i}") for i, x in enumerate(xmls)],
                        code_state={"status": "UNAVAILABLE"})


def status(result, cid):
    return next(c for c in result["claims"] if c["id"] == cid)


def test_pass_requires_every_binding_to_pass():
    result = ledger([claim("A")], bindings(A=["test_a", "test_b", "test_c"]),
                    junit(test_a="passed", test_b="passed", test_c="passed"))
    a = status(result, "A")
    assert (a["status"], a["passed"], a["required"]) == ("PASS", 3, 3)
    assert result["verdict"]["exit_code"] == 0


def test_m_of_n_is_not_pass():
    result = ledger([claim("A")], bindings(A=["test_a", "test_b", "test_c"]),
                    junit(test_a="passed", test_b="passed"))
    a = status(result, "A")
    assert (a["status"], a["passed"], a["required"]) == ("NOT_ATTEMPTED", 2, 3)
    assert a["not_executed"] == ["tests_demo/test_demo.py::test_c"]
    assert result["verdict"]["exit_code"] == 0


def test_skipped_required_evidence_is_not_attempted():
    a = status(ledger([claim("A")], bindings(A=["test_a", "test_b"]),
                      junit(test_a="passed", test_b="skipped")), "A")
    assert a["status"] == "NOT_ATTEMPTED"
    assert a["evidence"][1]["outcome"] == "skipped" and a["evidence"][1]["message"] == "not today"


@pytest.mark.parametrize("bad", ["failed", "error"])
def test_one_failing_binding_fails_the_claim(bad):
    result = ledger([claim("A")], bindings(A=["test_a", "test_b"]), junit(test_a="passed", test_b=bad))
    a = status(result, "A")
    assert (a["status"], a["passed"], a["failed"]) == ("FAIL", 1, ["tests_demo/test_demo.py::test_b"])
    assert result["verdict"] == {"exit_code": 1, "reasons": ["reproducible claims FAIL: A"]}


def test_fail_dominates_not_executed():
    a = status(ledger([claim("A")], bindings(A=["test_a", "test_b"]), junit(test_a="failed")), "A")
    assert a["status"] == "FAIL" and a["not_executed"] == ["tests_demo/test_demo.py::test_b"]


def test_no_runs_at_all_is_not_attempted_not_fail():
    a = status(ledger([claim("A")], bindings(A=["test_a"])), "A")
    assert a["status"] == "NOT_ATTEMPTED"


def test_policy_can_make_not_attempted_fail_the_run():
    result = ledger([claim("A")], bindings(A=["test_a"]), junit(), not_attempted_fails_run=True)
    assert result["verdict"]["exit_code"] == 1
    assert "not_attempted_fails_run" in result["verdict"]["reasons"][0]


def test_live_only_disposition_and_withdrawn_stay_separate():
    claims = [claim("L", "live_only", live_record={"observed": "once", "summary": "worked"}),
              claim("D", "disposition", disposition={"decided_by": "owner", "decision": "accepted",
                                                     "rationale": "r"}),
              claim("W", withdrawn={"reason": "not a requirement"}),
              claim("X", withdrawn={"reason": "was wrong"})]
    result = ledger(claims, bindings(W=["test_a"], X=["test_b"]), junit(test_a="passed", test_b="failed"))
    assert [c["status"] for c in result["claims"]] == ["LIVE_ONLY", "DISPOSITION", "WITHDRAWN", "WITHDRAWN"]
    assert status(result, "W")["observed_evidence"][0]["outcome"] == "passed"
    assert status(result, "X")["observed_evidence"][0]["outcome"] == "failed"
    assert result["summary"]["PASS"] == 0 and result["summary"]["FAIL"] == 0
    assert result["verdict"]["exit_code"] == 0
    assert "required" not in status(result, "L")


def test_same_evidence_in_two_runs_is_ambiguous():
    with pytest.raises(AccountingError, match="more than once"):
        ledger([claim("A")], bindings(A=["test_a"]), junit(test_a="passed"), junit(test_a="failed"))


def test_duplicate_within_a_run_is_ambiguous():
    xml = junit(test_a="passed").replace(b"</testsuite>",
                                         b'<testcase classname="tests_demo.test_demo" name="test_a"/></testsuite>')
    with pytest.raises(AccountingError, match="more than once"):
        ledger([claim("A")], bindings(A=["test_a"]), xml)


def test_unrelated_duplicates_do_not_matter():
    xml = junit(test_a="passed").replace(b"</testsuite>", b'<testcase classname="o" name="t"/>'
                                                          b'<testcase classname="o" name="t"/></testsuite>')
    assert status(ledger([claim("A")], bindings(A=["test_a"]), xml), "A")["status"] == "PASS"


def test_collection_error_fails_bound_claim():
    xml = (b'<testsuite><testcase classname="" name="tests_demo.test_demo">'
           b'<error message="ImportError"/></testcase></testsuite>')
    a = status(ledger([claim("A")], bindings(A=["test_a"]), xml), "A")
    assert a["status"] == "FAIL" and "failed to collect" in a["evidence"][0]["message"]
