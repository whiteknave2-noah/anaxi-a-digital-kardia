"""Opt-in, production-shaped check with the REAL production model: a thread Clark put in
his own journal is reopened by natural reference (not by id, not as a library book) and
then continued as the SAME thread. Synthetic workspace only -- never the owner's journal,
never Private Space; a similarly titled library file is present to prove it cannot steal
the request. Enable with ANAXI_REAL_MODEL_HARNESS=1 (see test_real_model_ordinary_resource_use.py)."""
import json
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

QUESTION = (
    "If the human mind is the ultimate processor of experience, and the digital interface is merely "
    "a tool for externalizing that processing, what is the most critical, yet currently "
    "unquantifiable, component of human understanding that we are failing to adequately model?"
)
ENTRY = (
    "**Topic: Cognitive Persistence & Unquantifiable Understanding**\n\n**Open-Ended Question:** "
    + QUESTION + "\n\n**Initial Thoughts:** I suspect the gap is meaning derived from shared, embodied context."
)


PAIRS = [
    ("Please open the Cognitive Persistence thread.", "Please add this thought to the same thread: perhaps what we cannot capture is the returning itself."),
    ("Read the Cognitive Persistence entry from your journal.", "Continue that entry with one more thought of your own."),
    ("Return to the Cognitive Persistence thread.", "Add one more thought there."),
    ("Revisit what you wrote about Cognitive Persistence.", "Append that to Cognitive Persistence: the interface may matter less than the attention we bring to it."),
]


@pytest.mark.parametrize("reopen,cont", PAIRS)
def test_journal_thread_is_reopened_and_continued_with_the_real_model(monkeypatch, tmp_path, reopen, cont):
    import workspace_capability as wc
    from test_wsp1_production_hard_floor import _build

    real_chat = _real_ollama.chat
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    paths = h.paths
    Path(paths.library_dir, "The Hobbit.pdf").write_text("a book", encoding="utf-8")
    Path(paths.library_dir, "Cognitive Science Handbook.pdf").write_text("another book", encoding="utf-8")
    original, _ = wc.append_journal_entry(paths, "actor-synthetic", ENTRY, source_event_id="H-seed")

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        h.calls.append({"model": model, "format": format, "options": options, "messages": [dict(m) for m in messages]})
        reply = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        if format:
            print("\n[raw]", reply["message"]["content"][:300].replace("\n", " "))
        return reply

    h.la.ollama.chat = chat
    run = lambda text: h.la.run_waking_turn(h.la.AnaxiOrchestrator(), text, interaction_mode="conversation")  # noqa: E731

    first = run(reopen)
    log = wc.query_action_log(paths)
    print("\n[reopen] actions:", [(r["resource_class"], r["action"], r["result"], r["relative_path"]) for r in log])
    print("[reopen] reply:", first["reply"][:500])
    reads = [r for r in log if r["resource_class"] == "journal" and r["action"] == "read" and r["result"] == "performed"]
    assert reads, "the request never reached the stored journal entry"
    assert not [r for r in log if r["resource_class"] == "library" and r["action"] == "read"]

    second = run(cont)
    files = sorted(Path(paths.journal_dir).glob("*.json"))
    print("[continue] reply:", second["reply"][:500])
    added = [json.loads(p.read_text()) for p in files if p.name != f"{original['entry_id']}.json"]
    print("[continue] new entries:", added)
    # Clark may lawfully answer in conversation instead of writing; when he does write, the open
    # thread is bound mechanically, so the entry must be linked without the model naming it.
    assert len(added) <= 1
    if added:
        assert added[0].get("continues_entry_id") == original["entry_id"], "the same-thread request lost its link"
    else:
        print("[continue] Clark chose not to write")
