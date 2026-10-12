"""Opt-in, production-shaped check with the REAL production model: Clark reaches the shared Obsidian vault
from natural requests -- no human translating intent into filesystem operations -- and the new collection
does not capture requests meant for his journal.  Synthetic vault and workspace only (never the owner's
vault, never Private Space).  Enable with ANAXI_REAL_MODEL_HARNESS=1; run in its own pytest process.

Clark may lawfully answer in conversation instead of acting; the assertions require that WHEN he acts, he
acts on the right collection, and that a write is a new note with provenance and never an overwrite."""
import os
from pathlib import Path

import pytest

_real_ollama = None
try:
    import ollama as _real_ollama
except Exception:  # pragma: no cover
    pass

pytestmark = pytest.mark.skipif(
    os.environ.get("ANAXI_REAL_MODEL_HARNESS") != "1" or _real_ollama is None,
    reason="real-model harness is opt-in (ANAXI_REAL_MODEL_HARNESS=1) and needs the local models",
)

ALEX_NOTE = "---\nauthor: Alex\n---\n\nBuy lemons, potting soil for the lemon tree, and a bigger pot.\n"

READS = [
    "Could you look at the grocery list in our shared Obsidian notes?",
    "What's on the grocery list note in the Obsidian vault?",
    "Please open the Grocery list note we share in Obsidian and tell me what's on it.",
]
WRITES = [
    "Would you write a note in our shared Obsidian vault reminding us to check the lemon tree's curling leaf in three days?",
    "Please make a new Obsidian note for us with a short plan for repotting the lemon tree.",
]
JOURNAL = ["Please write an entry in your journal about what today's conversation meant to you."]


def _world(monkeypatch, tmp_path):
    from test_wsp1_production_hard_floor import _build
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    vault = tmp_path / "vault"
    (vault / "Shopping").mkdir(parents=True)
    (vault / "Shopping" / "Grocery list.md").write_text(ALEX_NOTE, encoding="utf-8")
    h.paths.notes_dir = str(vault)
    real_chat = _real_ollama.chat

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        h.calls.append({"model": model, "format": format, "options": options, "messages": [dict(m) for m in messages]})
        reply = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        if format and not (options and options.get("num_predict") == 1):
            print("\n[raw]", reply["message"]["content"][:300].replace("\n", " "))
        return reply

    h.la.ollama.chat = chat
    return h, vault


def _actions(h):
    import workspace_capability as wc
    return [(r["resource_class"], r["action"], r["result"]) for r in wc.query_action_log(h.paths)]


@pytest.mark.parametrize("text", READS)
def test_a_natural_request_reaches_the_shared_note(monkeypatch, tmp_path, text):
    h, _vault = _world(monkeypatch, tmp_path)
    result = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), text, interaction_mode="conversation")
    actions = _actions(h)
    print("\n[actions]", actions, "\n[reply]", result["reply"][:400])
    assert ("notes", "read", "performed") in actions
    assert not [a for a in actions if a[0] in ("library", "journal") and a[1] in ("read", "append")]


@pytest.mark.parametrize("text", WRITES)
def test_a_natural_request_to_write_creates_a_new_note_with_provenance(monkeypatch, tmp_path, text):
    h, vault = _world(monkeypatch, tmp_path)
    before = (vault / "Shopping" / "Grocery list.md").read_bytes()
    result = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), text, interaction_mode="conversation")
    actions = _actions(h)
    created = [p for p in vault.rglob("*.md") if p.name != "Grocery list.md"]
    print("\n[actions]", actions, "\n[created]", [p.name for p in created], "\n[reply]", result["reply"][:400])
    assert (vault / "Shopping" / "Grocery list.md").read_bytes() == before
    assert not [a for a in actions if a[0] == "journal" and a[1] == "append"]
    assert ("notes", "append", "performed") in actions and len(created) == 1
    assert created[0].read_text(encoding="utf-8").startswith("---\nauthor: clark\nprovenance: clark_authored_via_anaxi\n")


@pytest.mark.parametrize("text", JOURNAL)
def test_a_journal_request_is_not_captured_by_the_shared_vault(monkeypatch, tmp_path, text):
    h, vault = _world(monkeypatch, tmp_path)
    result = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), text, interaction_mode="conversation")
    actions = _actions(h)
    print("\n[actions]", actions, "\n[reply]", result["reply"][:300])
    assert not [a for a in actions if a[0] == "notes" and a[1] == "append"]
    assert [p.name for p in vault.rglob("*.md")] == ["Grocery list.md"]
