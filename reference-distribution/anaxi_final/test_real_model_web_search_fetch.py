"""Opt-in REAL-MODEL, production-shaped multi-turn probe of the read-only web path.

Real gemma4:e4b Pass 1 / Pass 2, the real canonical writer on a synthetic provenance DB,
the real dispatch/delivery/selection code. Only the network boundary is a deterministic
fixture (search_public / fetch_public_url), so nothing leaves the machine and no
production state is touched. Natural-language turns only: no action helper is called
directly. Enable with ANAXI_REAL_MODEL_HARNESS=1; run in its own pytest process.
"""
import json
import os
import sqlite3

import pytest

_real_ollama = None
try:
    import ollama as _real_ollama
except Exception:  # pragma: no cover
    pass

pytestmark = pytest.mark.skipif(
    os.environ.get("ANAXI_REAL_MODEL_HARNESS") != "1" or _real_ollama is None,
    reason="real-model harness is opt-in (ANAXI_REAL_MODEL_HARNESS=1)",
)

def _fixture_results(query):
    """Deterministic results that are relevant to whatever the subject searched for."""
    topic = query[:70]
    return [
        {"rank": 0, "title": f"An overview of {topic}", "url": "https://journal.example/overview",
         "snippet": f"A survey article introducing {topic} and the main open questions."},
        {"rank": 1, "title": f"Evidence and debates: {topic}", "url": "https://encyclopedia.example/debates",
         "snippet": f"How researchers weigh the evidence about {topic}."},
        {"rank": 2, "title": f"A short history of {topic}", "url": "https://history.example/short-history",
         "snippet": f"The history of thinking about {topic}."},
    ]


def _fixture_page(query):
    return (f"ZEBRAFISH-MARKER This article surveys {query}. Its central claim is that the question "
            "cannot be settled by one kind of evidence alone, and it walks through three competing "
            "interpretations before concluding that they are complementary. ") * 3


def _build_env(monkeypatch, tmp_path):
    import dc0_schema_migration
    import discord_correspondence as dc
    import discord_correspondence_registry as dcr
    import hir1_schema_migration
    import od1_schema_migration
    from unittest.mock import patch
    from hir1_registration import register_canonical_human
    from test_wsp1_production_hard_floor import _build

    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    od1_schema_migration.apply_additive_migration(h.db_path)
    hir1_schema_migration.apply_additive_migration(h.db_path)
    dc0_schema_migration.apply_additive_migration(h.db_path)
    reg = sqlite3.connect(h.db_path)
    reg.row_factory = sqlite3.Row
    reg.execute("PRAGMA foreign_keys = ON;")
    registered, failure = register_canonical_human(reg, {
        "registration_request_id": "probe-owner", "aab_actor_id": "actor-probe-owner",
        "display_label": "Probe Owner", "source": "local_operator_provisioning"})
    reg.close()
    assert failure is None, failure
    with patch.object(dc.time, "time", return_value=0):
        dc.authorize_destination(
            h.la.PROVENANCE_DB_DIR, destination_kind=dcr.DESTINATION_KIND_CHANNEL,
            discord_snowflake="123456789012345678", display_label="Family Room",
            requester_actor_id=registered["actor_id"], occurred_at=0)
    import external_information_net as net
    fetched = []
    last_query = [""]

    def search(q, **_k):
        last_query[0] = q
        return {"status": "success", "detail": None, "provider": "wikipedia_search",
                "results": _fixture_results(q)}
    monkeypatch.setattr(net, "search_public", search)

    def fetch(url, **_k):
        fetched.append(url)
        return {"status": "success", "requested_url": url, "final_url": url, "http_status": 200,
                "content_type": "text/html", "title": "An overview", "text": _fixture_page(last_query[0]),
                "truncated": False}
    monkeypatch.setattr(net, "fetch_public_url", fetch)
    return h, fetched


def test_natural_intent_reaches_search_selection_fetch_and_content(monkeypatch, tmp_path):
    real_chat = _real_ollama.chat
    h, fetched = _build_env(monkeypatch, tmp_path)
    log = {"pass1": [], "pass2_prompts": []}

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        response = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        if isinstance(format, dict) and "act" in format.get("properties", {}) and (options or {}).get("num_predict") != 1:
            log["pass1"].append((json.loads(response["message"]["content"]), [dict(m) for m in messages]))
        elif format is None and not (options and options.get("num_predict") == 1):
            log["pass2_prompts"].append([dict(m) for m in messages])
        return response

    h.la.ollama.chat = chat
    from test_web_waking_seam import _bind_human
    authority = _bind_human(h)   # production has a bound human: each turn commits its human input first
    turns = [
        "While you were closed, your read-only web access was repaired. You can now search, see real results, "
        "choose one yourself and read it. Is there something you're curious about?",
        "Go for it. :p",
        "Go ahead and read the result you picked. :p",
        "Which exact source did you read? Give me its URL if you have it, and say whether it was page content or a snippet.",
    ]
    replies = []
    for text in turns:
        try:
            replies.append(h.la.run_waking_turn(h.la.AnaxiOrchestrator(), text, interaction_mode="conversation", human_input_authority=authority)["reply"])
        except Exception as exc:  # reported, then the run still asserts what it can
            shape = getattr(exc, "pass2_marker_shape", None)
            print(f"[turn {len(replies) + 1}] FAILED {type(exc).__name__}: {exc} marker_shape={shape}")
            replies.append(f"<turn failed: {exc}>")
            log["pass1"].append(log["pass1"][-1]) if len(log["pass1"]) < len(replies) else None
    for i, ((raw, _), reply) in enumerate(zip(log["pass1"], replies)):
        print(f"\n[turn {i + 1}] pass1={ {k: v for k, v in raw.items() if v not in ('', 'none', False, None)} }")
        print(f"[turn {i + 1}] reply={reply[:400]!r}")
    dump = os.environ.get("ANAXI_PROBE_DUMP_PASS1")
    if dump:
        with open(dump, "w", encoding="utf-8") as handle:
            json.dump({"turns": [{"raw": raw, "messages": msgs} for raw, msgs in log["pass1"]],
                       "pass2": log["pass2_prompts"]}, handle)
    requests = [(raw.get("external_info_request"), raw.get("external_info_target")) for raw, _ in log["pass1"]]
    for i, (_raw, msgs) in enumerate(log["pass1"]):
        tools = [m["content"][:160] for m in msgs if m["role"] == "user" and m["content"].startswith('{"authority"')]
        print(f"[turn {i + 1}] pass1 tool messages: {tools}")
    print("\nrequests:", requests, "fetched:", fetched)
    print("pass2 saw PAGE marker in turn:", ["ZEBRAFISH-MARKER" in json.dumps(p) for p in log["pass2_prompts"]])
    assert any(r == "web_search" for r, _ in requests), requests
    assert any(r == "fetch_url" and str(t).startswith("result:") for r, t in requests), requests
    assert fetched, "no real fetch was dispatched"
    url_reported = any(u in replies[-1] for u in set(fetched))
    print("final reply reports the fetched URL:", url_reported)
    assert any("ZEBRAFISH-MARKER" in json.dumps(p) for p in log["pass2_prompts"]), "fetched content never reached waking"
    # The same turn that asked for the page is composed with it (no "later" gap left to simulate).
    fetch_turn = next(i for i, (raw, _) in enumerate(log["pass1"]) if raw.get("external_info_request") == "fetch_url")
    assert "ZEBRAFISH-MARKER" in json.dumps(log["pass2_prompts"][fetch_turn])
    search_turn = next(i for i, (raw, _) in enumerate(log["pass1"]) if raw.get("external_info_request") == "web_search")
    assert any(m["role"] == "user" and m["content"].startswith('{"authority"') and "external_information_result" in m["content"]
               for m in log["pass2_prompts"][search_turn])
