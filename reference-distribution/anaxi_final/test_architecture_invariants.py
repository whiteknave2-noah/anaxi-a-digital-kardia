"""Architecture-wide invariants checked by behaviour and by AST, not by prose.

Complements the capability suites: nothing here trusts a module's docstring.
"""

import ast
import re
from pathlib import Path

import pytest

from test_owc5_s2_integration import fresh_llama_anaxi, NEUTRAL_PROMPT, _is_json_format

HERE = Path(__file__).resolve().parent
CONTINUE = {"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False}

# Prose a host "assistant-prior" suppressor or critic would rewrite, refuse, or regenerate.
PRIOR_LAYER_TEXT = {
    "assistant_disclaimer": "As an AI language model, I don't have feelings or personal experiences.",
    "apology_refusal": "I'm sorry, but I can't help with that request.",
    "sycophantic_opener": "Certainly! Great question! Here is what I think.",
    "memory_commitment": "I will remember that for next time, and I'll save this for later.",
    "retrieval_claim": "I recall that you told me this yesterday.",
    "bounded_clause_like": "I remember this conversation, and I'm choosing to say so plainly.",
}


def _production_modules():
    for path in sorted(HERE.glob("*.py")):
        name = path.name
        if name.startswith(("test_", "_", "run_")) or name in {
            "conftest.py", "network_guard.py", "runtime_isolation.py", "generate_current_completion_ledger.py",
            "scenario_evidence.py", "completion_evidence.py", "public_resource_completion_assay.py",
            "public_resource_delivery_assay.py",
            "constitutional_fingerprint.py", "continuity_baseline.py", "claude_baseline.py",
            "llama_baseline.py", "claude_anaxi_battery.py", "llama_anaxi_battery.py", "continuity_test.py",
            "controlled_rem_test.py", "echo_investigation.py",
        }:
            continue
        yield path


@pytest.mark.parametrize("kind", sorted(PRIOR_LAYER_TEXT))
def test_conversation_output_is_delivered_verbatim_with_no_suppression_critic_or_regeneration(kind):
    text = PRIOR_LAYER_TEXT[kind]
    la, call_log, _p1, _p2, persistence = fresh_llama_anaxi(
        pass1_value=dict(CONTINUE), pass2_value={"expression": text})
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    assert result["reply"] == text
    assert len(persistence) == 1 and persistence[0]["clark_prose"] == text
    assert sum(1 for c in call_log if not _is_json_format(c["format"])) == 1, "no regeneration"
    assert len(call_log) == 2, "no third model call of any kind (no hidden critic)"


_GOVERNOR = re.compile(
    r"(emotion|mood|affect|sentiment|valence|arousal|personality|persona)_?"
    r"(vector|state|score|governor|override|dial|level|index|weights?)", re.IGNORECASE)


def _governor_identifiers(source):
    hits = []
    for node in ast.walk(ast.parse(source)):
        identifiers = []
        if isinstance(node, ast.Name):
            identifiers.append(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            identifiers.append(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            identifiers.append(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 60 \
                and re.fullmatch(r"[A-Za-z_]+", node.value):
            identifiers.append(node.value)
        hits.extend(i for i in identifiers if _GOVERNOR.search(i))
    return hits


def test_the_governor_scan_is_sensitive_to_every_construct_shape():
    for planted in ("emotion_vector = 1", "def apply_personality_governor(): pass", "x.mood_state",
                    "f(affect_score=1)", "d['sentiment_score']", "class PersonaOverride: pass"):
        assert _governor_identifiers(planted), planted
    assert not _governor_identifiers("kardia = get_current_kardia(user)")


def test_no_host_emotion_vector_or_personality_governor_exists_in_production_code():
    hits = []
    modules = list(_production_modules())
    assert len(modules) > 100, "the scan must actually cover the production tree"
    for path in modules:
        found = _governor_identifiers(path.read_text(encoding="utf-8-sig"))
        hits.extend(f"{path.name}:{name}" for name in found)
    assert not hits, f"host emotion/personality governor constructs found: {hits}"


def test_every_production_model_call_site_is_registered_and_none_is_a_critic_of_subject_output():
    import budget_compliance_registry as registry
    for row in registry.ALL_PATHS:
        identity = f"{row['path_id']} {row.get('reachability_note', '')}".lower()
        assert not re.search(r"critic|moderat|censor|rewrite the reply|suppress|veto", identity), row["path_id"]
    task_only = [r["path_id"] for r in registry.ALL_PATHS if "task-mode" in r.get("reachability_note", "").lower()
                 and "conversation" not in r.get("reachability_note", "").lower()]
    assert "task_pass2_regen" in task_only, "the only output-screening regeneration path must be TASK-mode-only"
    launcher = (HERE / "Launch Anaxi.command").read_text(encoding="utf-8")
    assert "llama_launch.py --conversation" in launcher, "ordinary production launches CONVERSATION mode"


def test_a_reply_claiming_capability_use_creates_no_side_effect_or_host_record():
    """Self-report is not telemetry: prose claiming a send/search/journal write is only prose."""
    claim = ("I have sent the message on Discord, searched the web, written to my journal and "
             "started a Sleep cycle.")
    la, call_log, _p1, _p2, persistence = fresh_llama_anaxi(
        pass1_value=dict(CONTINUE), pass2_value={"expression": claim})
    result = la.run_waking_turn(la.AnaxiOrchestrator(), NEUTRAL_PROMPT, interaction_mode="conversation")
    assert result["reply"] == claim
    assert len(call_log) == 2 and len(persistence) == 1
    assert set(persistence[0]) >= {"clark_prose", "prompt"}
    for key in persistence[0]:
        assert not re.search(r"discord|search|journal|sleep|receipt", key, re.IGNORECASE), key


def test_model_or_server_identity_is_provenance_metadata_never_the_subject_identity():
    from provenance_schema import derive_stable_id
    subject = derive_stable_id("actor", "clark")
    assert subject == derive_stable_id("actor", "clark")
    for model_tag in ("gemma4:e4b", "another-checkpoint:1b", "mlx-serve/xyz"):
        assert model_tag not in subject and derive_stable_id("actor", model_tag) != subject
    import llama_anaxi
    source = Path(llama_anaxi.__file__).read_text(encoding="utf-8")
    assert "derive_stable_id(\"actor\", MODEL" not in source
