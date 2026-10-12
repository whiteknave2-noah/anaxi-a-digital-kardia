"""Kardia (subject-authored constitutional identity state) completion checks.

Real ``ConstitutionalMind`` and ``AnaxiOrchestrator.prepare_context`` against
temporary databases; nothing here reads or writes production Kardia.
"""

import ast
import json
import re
import sqlite3
from pathlib import Path

import pytest

import constitutional_fingerprint as cf
import linguistic_pipeline as lp
from anaxi_protocol_sqlite import ConstitutionalMind
from orchestration import AnaxiOrchestrator
from test_anaxi_protocol import _build_isolated_hippocampus_paths, make_reflection_payload

HERE = Path(__file__).resolve().parent
KEYS = ("moral_valve", "volitional_channel", "affective_stance", "aesthetic_valve")
BASELINE = json.loads((HERE / "completion_evidence" / "constitutional_baseline.json").read_text(encoding="utf-8"))


def _kardia_rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return (conn.execute("SELECT * FROM active_kardia ORDER BY rowid").fetchall(),
                conn.execute("SELECT * FROM kardia_history ORDER BY rowid").fetchall())
    finally:
        conn.close()


def test_constitutional_meaning_is_unchanged_from_the_production_baseline():
    """Axiom constants, prototypes, validators, delta measure and the Kardia->preamble
    renderer are byte-for-byte semantically identical (AST) to the master baseline."""
    for module in ("anaxi_protocol_sqlite.py", "anaxi_protocol.py"):
        current = cf.protocol_fingerprint((HERE / module).read_text(encoding="utf-8"))
        assert current == BASELINE[module], f"{module}: constitutional meaning changed vs baseline"
    assert cf.whole_module_fingerprint((HERE / "linguistic_pipeline.py").read_text(encoding="utf-8")) == \
        BASELINE["linguistic_pipeline.py"]


def test_fingerprint_is_sensitive_to_axiom_and_validator_changes():
    source = (HERE / "anaxi_protocol_sqlite.py").read_text(encoding="utf-8")
    base = cf.protocol_fingerprint(source)
    constant_changed = re.sub(r"MAX_KARDIA_DELTA_RATIO = 0\.55", "MAX_KARDIA_DELTA_RATIO = 0.95", source)
    assert constant_changed != source
    assert cf.protocol_fingerprint(constant_changed) != base
    validator_changed = source.replace(
        "def _validate_axioms_destination(self, new_kardia: Dict[str, str]) -> Optional[str]:",
        "def _validate_axioms_destination(self, new_kardia: Dict[str, str]) -> Optional[str]:\n        return None",
        1)
    assert cf.protocol_fingerprint(validator_changed) != base


def test_current_values_are_complete_and_persist_across_a_process_restart(tmp_path):
    db = str(tmp_path / "mind.db")
    mind = ConstitutionalMind(db)
    first = mind.get_current_kardia("subject")
    assert set(KEYS) <= set(first) and all(isinstance(first[k], str) and first[k].strip() for k in KEYS)
    mind.close()
    reopened = ConstitutionalMind(db)
    assert reopened.get_current_kardia("subject") == first
    reopened.close()
    active, history = _kardia_rows(db)
    assert len(active) == 1


def test_prepare_context_delivers_the_current_kardia_into_the_generation_preamble(tmp_path):
    paths = _build_isolated_hippocampus_paths(tmp_path)
    orchestrator = AnaxiOrchestrator(str(tmp_path / "mind.db"), hippocampus_paths=paths)
    prepared = orchestrator.prepare_context("subject", "Hello.")
    current = orchestrator.mind.get_current_kardia("subject")
    assert prepared["kardia"] == current
    preamble = prepared["core_system_text"]
    for key in ("moral_valve", "volitional_channel", "affective_stance"):
        assert current[key] in preamble, f"{key} text must reach the model verbatim"
    assert prepared["messages"][0]["content"].startswith(preamble)
    orchestrator.close()


def test_unrelated_subsystems_never_rewrite_kardia(tmp_path):
    import workspace_capability as wc
    paths = _build_isolated_hippocampus_paths(tmp_path)
    db = str(tmp_path / "mind.db")
    orchestrator = AnaxiOrchestrator(db, hippocampus_paths=paths)
    orchestrator.prepare_context("subject", "prime")
    before = _kardia_rows(db)
    for index in range(3):
        orchestrator.prepare_context("subject", f"question {index}")
    workspace = wc.WorkspacePaths(str(tmp_path / "workspace"))
    workspace.ensure_exists()
    wc.append_journal_entry(workspace, "actor-x", "a journal entry")
    wc.list_journal_entries(workspace)
    assert _kardia_rows(db) == before
    orchestrator.close()


def test_only_the_protocol_modules_write_kardia_tables():
    writers = set()
    pattern = re.compile(
        r"(INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+(active_kardia|kardia_history)\b",
        re.IGNORECASE)
    for path in HERE.glob("*.py"):
        if path.name.startswith("test_") or path.name in {"_investigation.py"}:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            writers.add(path.name)
    assert writers == {"anaxi_protocol.py", "anaxi_protocol_sqlite.py"}, writers


def test_inference_provider_and_model_call_layers_have_no_kardia_authority():
    for module in ("inference_provider.py", "context_budget.py", "waking_turn_failure_capture.py"):
        source = (HERE / module).read_text(encoding="utf-8").lower()
        assert "active_kardia" not in source and "govern_identity_revision" not in source, module


def test_generation_controls_are_a_pure_function_of_kardia_with_no_host_personality_state():
    kardia = {"moral_valve": "M", "volitional_channel": "V", "affective_stance": "A", "aesthetic_valve": "Minimalist, precise, punchy."}
    baseline = lp.build_generation_controls(kardia)
    for _ in range(3):
        assert lp.build_generation_controls(dict(kardia)) == baseline
    for key in ("moral_valve", "volitional_channel", "affective_stance"):
        changed = dict(kardia, **{key: "different"})
        controls = lp.build_generation_controls(changed)
        assert (controls["temperature"], controls["top_p"], controls["style_instruction"]) == (
            baseline["temperature"], baseline["top_p"], baseline["style_instruction"]), \
            f"{key} must not steer sampling; only the subject's own aesthetic valve may"
    source = (HERE / "linguistic_pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    builder = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_generation_controls")
    names = {n.id for n in ast.walk(builder) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(builder) if isinstance(n, ast.Attribute)}
    assert not names & {"time", "datetime", "random", "session", "mood", "emotion", "sentiment", "affect_score"}


def test_identity_revision_carries_provenance_and_a_large_rewrite_never_applies_silently(tmp_path):
    db = str(tmp_path / "mind.db")
    mind = ConstitutionalMind(db)
    seeded = mind.get_current_kardia("subject")
    before_active, before_history = _kardia_rows(db)
    message = mind.govern_identity_revision(
        "subject", make_reflection_payload("REWRITE", kardia={k: "Completely different " + k for k in KEYS}),
        triggering_observation_timestamp=1234)
    assert message.startswith("REWRITE deferred for review"), message
    pending = mind.list_pending_proposals("subject")
    after_active, after_history = _kardia_rows(db)
    assert pending, "a large REWRITE must be held as a pending proposal"
    assert mind.get_current_kardia("subject") == seeded, "a held proposal must not change current Kardia"
    assert after_active == before_active and after_history == before_history
    assert any(p["payload"].get("triggering_observation_timestamp") == 1234 for p in pending), pending
    mind.close()
