"""Regression: setting the words that are ALREADY the active operative directive
is a no-op, not a new revision.

Found with the real model: once a directive was active the subject re-emitted
its own ``set_directive`` (same words) on every ordinary turn. Each one wrote a
new superseding revision -- repetition re-dating and re-writing history for a
choice that had not changed ("repetition/time do not strengthen").
"""
import json
import sqlite3

import od1_schema_migration
import operative_directive as od
from test_wsp1_production_hard_floor import _build, _message


def _set(text):
    return {
        "act": "develop_current", "thread": "t", "direction_request": "none", "relinquish_direction": False,
        "operative_directive_request": "set_directive", "operative_directive_text": text,
    }


def _turns(h, acts):
    inner = h.la.ollama.chat
    queue = list(acts)

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        if isinstance(format, dict) and "act" in format.get("properties", {}) and queue:
            return {"message": {"content": json.dumps(queue.pop(0))}, "prompt_eval_count": 1,
                    "done": True, "done_reason": "stop", "eval_count": 20}
        if format is None and not (options and options.get("num_predict") == 1):   # ordinary Pass 2: plain prose + marker
            return {"message": {"content": "Understood."}, "prompt_eval_count": 1,
                    "done": True, "done_reason": "stop", "eval_count": 5}
        return inner(model, messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat


def _transitions(h):
    conn = sqlite3.connect(h.db_path)
    try:
        return conn.execute("SELECT action FROM operative_directive_transitions ORDER BY rowid").fetchall()
    finally:
        conn.close()


def test_identical_set_is_unchanged_and_writes_no_revision(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    od1_schema_migration.apply_additive_migration(h.db_path)
    _turns(h, [_set("Answer in one short sentence."), _set("Answer in one short sentence."),
               _set("Answer in two short sentences."), _set("Answer in two short sentences.")])

    results = [
        h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(60), interaction_mode="conversation")
        for _ in range(4)
    ]

    statuses = [r["operative_directive_action_result"]["status"] for r in results]
    assert statuses == ["recorded", "unchanged", "recorded", "unchanged"]
    assert [a for (a,) in _transitions(h)] == ["ACTIVATE", "REPLACE"] or len(_transitions(h)) == 2
    assert od.fetch_active_directive(h.la.PROVENANCE_DB_DIR)["directive_text"] == "Answer in two short sentences."
