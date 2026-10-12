"""Every turn's signal-gated artifact decision leaves a durable host record (live 2026-09-24: a POSITIVE
save request's outcome existed only as a terminal print, and on a workspace turn the prepared decision
was replaced by "not_applicable" with no trace that a judgment had run)."""
import json
import os

from test_web_waking_seam import Seam
from test_wsp1_production_hard_floor import _build
from test_workspace_target_discovery import _script

SAVE = "Could you save this for me: the blue heron came back to the pond this morning."
JUDGMENT = {"identified_referent": "the blue heron came back to the pond this morning.",
            "construction_status": "success", "artifact_type": "journal", "artifact_scope": "personal",
            "namespace": "journal", "title": "Heron sighting", "content": "The heron came back."}


def _records(la):
    path = os.path.join(la.PROVENANCE_DB_DIR, la.ARTIFACT_DECISION_LOG)
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_an_ordinary_turn_records_the_written_artifact_and_its_turn(monkeypatch, tmp_path):
    s = Seam(monkeypatch, tmp_path)
    monkeypatch.setattr(s.h.la, "OBSIDIAN_WORKSPACE_ROOT", str(tmp_path / "vault"))   # never the suite's shared root
    judgment_messages = [{"role": "system", "content": "judge"}, {"role": "user", "content": "x"}, {"role": "user", "content": SAVE}]
    real_ask = s.h.la.ask_llama_for_json
    monkeypatch.setattr(s.h.la, "build_artifact_construction_messages", lambda prompt: judgment_messages)
    monkeypatch.setattr(s.h.la, "ask_llama_for_json", lambda messages, *a, **k: (
        json.dumps(JUDGMENT) if messages is judgment_messages else real_ask(messages, *a, **k)))
    result = s.turn(SAVE)
    (record,) = _records(s.h.la)
    assert record["signal_category"] == "POSITIVE" and record["turn_path"] == "ordinary"
    assert record["judgment_outcome"] == "ready_to_write" and record["final_outcome"] == "written"
    assert record["waking_turn_event_id"] == result["native_event_id"] and record["human_input_event_id"]
    assert record["artifact_path"] and os.path.exists(record["artifact_path"])
    assert record["artifact_path"].startswith(str(tmp_path / "vault"))
    assert set(record) == {"timestamp", "human_input_event_id", "waking_turn_event_id", "signal_category",
                           "turn_path", "judgment_outcome", "final_outcome", "final_reason", "artifact_path",
                           "artifact_title"}                  # never the artifact's content


def test_a_workspace_turn_records_that_the_prepared_decision_was_not_applied(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True,
               action={"resource_class": "journal", "action": "append", "relative_path": "", "content": "noted"})
    _script(h, [JUDGMENT, {"resource_class": "journal", "action": "append", "relative_path": "", "content": "noted"}])
    result = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), SAVE, interaction_mode="conversation")
    (record,) = _records(h.la)
    assert record["signal_category"] == "POSITIVE" and record["turn_path"] == "use_workspace"
    assert record["judgment_outcome"] == "ready_to_write"            # the judgment ran and succeeded ...
    assert record["final_outcome"] == "not_applicable" and record["final_reason"] == "workspace action turn"
    assert record["waking_turn_event_id"] == result["native_event_id"] and record["artifact_path"] is None


def test_a_turn_without_a_save_signal_records_that_none_was_requested(monkeypatch, tmp_path):
    s = Seam(monkeypatch, tmp_path)
    s.turn("How has your evening been?")
    (record,) = _records(s.h.la)
    assert record["signal_category"] == "NO_SIGNAL" and record["judgment_outcome"] == "no_artifact_requested"
    assert record["final_outcome"] == "no_artifact_requested"
