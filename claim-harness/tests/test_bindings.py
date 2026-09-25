import pytest

from claim_harness.bindings import parse_bindings, parse_node_id
from claim_harness.inventory import AccountingError, parse_inventory
from helpers import TEST_FILE, bindings, claim, inventory, write_project

INV = parse_inventory(inventory(
    claim("A"), claim("B"),
    claim("L", "live_only", live_record={"observed": "x", "summary": "y"}),
    claim("D", "disposition", disposition={"decided_by": "o", "decision": "d", "rationale": "r"}),
    claim("W", withdrawn={"reason": "gone"})))


def problems(data, root=None):
    with pytest.raises(AccountingError) as info:
        parse_bindings(data, INV, project_root=root)
    return "\n".join(info.value.problems)


def test_node_id_maps_to_pytest_junit_names():
    node = parse_node_id("tests/unit/test_x.py::TestThing::test_it[a-1]")
    assert node.junit_key == ("tests.unit.test_x.TestThing", "test_it[a-1]")
    assert node.function == "test_it"
    assert parse_node_id("test_top.py::test_f").junit_key == ("test_top", "test_f")


@pytest.mark.parametrize("bad", ["test_x.py", "/abs/test_x.py::t", "../up/test_x.py::t", "test_x.txt::t",
                                 "test_x.py::", " test_x.py::t"])
def test_malformed_node_ids_rejected(bad):
    with pytest.raises(ValueError):
        parse_node_id(bad)


def test_valid_bindings(tmp_path):
    write_project(tmp_path)
    result = parse_bindings(bindings(A=["test_a", "test_b"], B=["test_c"]), INV, project_root=tmp_path)
    assert [n.raw for n in result.required("A")] == [f"{TEST_FILE}::test_a", f"{TEST_FILE}::test_b"]


def test_unknown_claim_rejected():
    assert "bindings[NOPE]: no such claim" in problems(bindings(A=["test_a"], B=["test_b"], NOPE=["test_c"]))


def test_duplicate_binding_within_claim_rejected():
    assert "duplicate binding" in problems(bindings(A=["test_a", "test_a"], B=["test_b"]))


def test_same_evidence_may_support_two_claims():
    parse_bindings(bindings(A=["test_a"], B=["test_a"]), INV)


def test_executable_claim_without_bindings_rejected():
    assert "claim B: executable claim has no bound evidence" in problems(bindings(A=["test_a"]))


def test_empty_binding_list_rejected():
    assert "must be a non-empty list" in problems(bindings(A=["test_a"], B=[]))


@pytest.mark.parametrize("cid", ["L", "D"])
def test_live_only_and_disposition_cannot_take_bindings(cid):
    assert "only executable claims take bindings" in problems(bindings(A=["test_a"], B=["test_b"],
                                                                       **{cid: ["test_c"]}))


def test_withdrawn_claim_needs_no_bindings_but_may_keep_them():
    parse_bindings(bindings(A=["test_a"], B=["test_b"]), INV)
    parse_bindings(bindings(A=["test_a"], B=["test_b"], W=["test_c"]), INV)


def test_unknown_evidence_is_an_accounting_error(tmp_path):
    write_project(tmp_path, names=("test_a",))
    found = problems(bindings(A=["test_a"], B=["test_typo"]), root=tmp_path)
    assert "unknown evidence" in found and "test_typo" in found
    data = bindings(A=["test_a"], B=["test_b"])
    data["bindings"]["B"] = ["missing/test_file.py::test_b"]
    assert "file missing/test_file.py not found" in problems(data, root=tmp_path)


def test_class_based_and_parametrized_ids_resolve(tmp_path):
    (tmp_path / "tests_demo").mkdir()
    (tmp_path / TEST_FILE).write_text("class TestK:\n    def test_m(self):\n        pass\n\ndef test_p(x):\n    pass\n")
    data = bindings(A=["TestK::test_m"], B=["test_p[1-2]"])
    parse_bindings(data, INV, project_root=tmp_path)
    data = bindings(A=["TestK::test_other"], B=["test_p[1-2]"])
    assert "test not defined" in problems(data, root=tmp_path)
