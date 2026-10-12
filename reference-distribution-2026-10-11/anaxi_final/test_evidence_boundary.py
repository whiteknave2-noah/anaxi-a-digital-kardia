"""Boundary verifier classification and receipt-binding fail-closed properties."""

import json
from pathlib import Path

import scenario_evidence as se
import verify_evidence_boundary as vb

HERE = Path(__file__).resolve().parent


def test_classification_of_every_path_kind():
    assert vb.classify("anaxi_final/completion_evidence/whole_system.json") == "evidence"
    assert vb.classify("WHOLE_SYSTEM_CONTINUATION.md") == "report"
    assert vb.classify("anaxi_final/scenario_evidence.py") == "evidence_tooling"
    assert vb.classify("anaxi_final/test_kardia_completion.py") == "test"
    assert vb.classify("anaxi_final/llama_anaxi.py") == "runtime_or_other"
    assert vb.classify("anaxi_final/Launch Anaxi.command") == "runtime_or_other"


def test_verdicts_fail_closed_on_runtime_or_test_change_or_tooling_import():
    ev = [{"class": "evidence"}, {"class": "report"}]
    assert vb.verdict(ev, []) == "EVIDENCE_ONLY"
    assert vb.verdict(ev + [{"class": "evidence_tooling"}], []) == "EVIDENCE_TOOLING_CHANGED"
    assert vb.verdict(ev + [{"class": "runtime_or_other"}], []) == "EXECUTABLE_CODE_OR_TEST_CHANGED"
    assert vb.verdict(ev + [{"class": "test"}], []) == "EXECUTABLE_CODE_OR_TEST_CHANGED"
    assert vb.verdict(ev, ["llama_anaxi.py"]) == "EXECUTABLE_CODE_OR_TEST_CHANGED"


def test_no_runtime_module_imports_evidence_tooling():
    assert vb.runtime_importers_of_tooling() == []


def _receipt(tmp_path, **overrides):
    doc = json.loads((HERE / "completion_evidence" / "specific_h_eligibility_receipt.json").read_text())
    for key, value in overrides.items():
        if key == "receipt":
            doc["receipt"].update(value)
        else:
            doc[key] = value
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(doc))
    return {"kind": "eligibility_receipt", "path": str(path.relative_to(HERE)) if False else str(path),
            "event_id": "01M2V4ZPX0JM2Q0JA0S1WZW11R"}


def _outcome(artifact):
    # receipt_outcome resolves paths under HERE; absolute paths resolve to themselves
    return se.receipt_outcome(artifact)


def test_recorded_real_receipt_passes_and_reports_the_actual_decision():
    artifact = {"kind": "eligibility_receipt", "path": "completion_evidence/specific_h_eligibility_receipt.json",
                "event_id": "01M2V4ZPX0JM2Q0JA0S1WZW11R"}
    result = se.receipt_outcome(artifact)
    assert result["outcome"] == "passed" and result["decision"] in se.DECISIONS


def test_receipt_for_another_h_or_unsafe_or_inconsistent_receipts_fail(tmp_path):
    assert _outcome({**_receipt(tmp_path), "event_id": "SOMEONE_ELSE"})["outcome"] == "failed"
    assert _outcome(_receipt(tmp_path, db_open_mode="read-write"))["outcome"] == "failed"
    assert _outcome(_receipt(tmp_path, h_text_exposed=True))["outcome"] == "failed"
    assert _outcome(_receipt(tmp_path, recovery_reserved=True))["outcome"] == "failed"
    assert _outcome(_receipt(tmp_path, receipt={"decision": "MAYBE"}))["outcome"] == "failed"
    assert _outcome(_receipt(tmp_path, receipt={"canonical_x_event_id": "X1"}))["outcome"] == "failed"
    assert _outcome(_receipt(tmp_path, receipt={"failure_evidence_id": None}))["outcome"] == "failed"
    missing = {"kind": "eligibility_receipt", "path": str(tmp_path / "nope.json"), "event_id": "x"}
    assert _outcome(missing)["outcome"] == "failed"
