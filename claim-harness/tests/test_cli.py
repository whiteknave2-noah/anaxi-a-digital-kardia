import json

from claim_harness.cli import main
from helpers import bindings, claim, dump, inventory, junit, write_project


def setup(tmp_path, outcomes, **policy):
    write_project(tmp_path)
    inv = dump(tmp_path / "inventory.json", inventory(
        claim("A"), claim("L", "live_only", live_record={"observed": "o", "summary": "s"}), **policy))
    b = dump(tmp_path / "bindings.json", bindings(A=["test_a", "test_b"]))
    j = dump(tmp_path / "junit.xml", junit(**outcomes))
    return ["--inventory", str(inv), "--bindings", str(b), "--project-root", str(tmp_path)], str(j)


def test_run_exit_0_and_ledger_written(tmp_path, capsys):
    common, j = setup(tmp_path, {"test_a": "passed", "test_b": "passed"})
    out = tmp_path / "out" / "ledger.json"
    assert main(["run", *common, "--junit", j, "--output", str(out)]) == 0
    ledger = json.loads(out.read_text())
    assert ledger["summary"]["PASS"] == 1 and ledger["summary"]["LIVE_ONLY"] == 1
    assert ledger["claims"][0]["evidence"][0]["file_sha256"] is not None
    assert ledger["code_state"]["status"] in ("ESTABLISHED", "UNAVAILABLE")
    text = capsys.readouterr().out
    assert "PASS" in text and "exit 0" in text


def test_run_exit_1_on_fail(tmp_path, capsys):
    common, j = setup(tmp_path, {"test_a": "passed", "test_b": "failed"})
    assert main(["run", *common, "--junit", j]) == 1
    assert "reproducible claims FAIL: A" in capsys.readouterr().out


def test_not_attempted_is_exit_0_unless_policy_says_otherwise(tmp_path):
    common, j = setup(tmp_path, {"test_a": "passed"})
    assert main(["run", *common, "--junit", j]) == 0
    common, j = setup(tmp_path, {"test_a": "passed"}, not_attempted_fails_run=True)
    assert main(["run", *common, "--junit", j]) == 1


def test_invalid_accounting_exit_2_and_no_ledger(tmp_path, capsys):
    common, j = setup(tmp_path, {"test_a": "passed", "test_b": "passed"})
    dump(tmp_path / "bindings.json", bindings(A=["test_a", "test_a"], NOPE=["test_b"]))
    out = tmp_path / "ledger.json"
    assert main(["run", *common, "--junit", j, "--output", str(out)]) == 2
    assert not out.exists()
    err = capsys.readouterr().err
    assert "ACCOUNTING INVALID" in err and "duplicate binding" in err and "no such claim" in err


def test_missing_junit_is_exit_2(tmp_path):
    common, _ = setup(tmp_path, {})
    assert main(["run", *common, "--junit", str(tmp_path / "nope.xml")]) == 2


def test_claims_and_show(tmp_path, capsys):
    common, j = setup(tmp_path, {"test_a": "passed", "test_b": "skipped"})
    assert main(["claims", *common]) == 0
    assert "requires tests_demo/test_demo.py::test_b" in capsys.readouterr().out
    out = tmp_path / "ledger.json"
    main(["run", *common, "--junit", j, "--output", str(out)])
    capsys.readouterr()
    assert main(["show", str(out)]) == 0
    shown = capsys.readouterr().out
    assert "A  NOT_ATTEMPTED" in shown and "skipped" in shown and "live_record:" in shown
