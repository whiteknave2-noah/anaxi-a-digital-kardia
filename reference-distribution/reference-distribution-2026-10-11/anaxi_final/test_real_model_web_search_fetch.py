"""Opt-in REAL-MODEL, production-shaped multi-turn probe of the read-only web path.

Real configured production-model Pass 1 / Pass 2, the real canonical writer on a synthetic provenance DB,
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
    for name in ("list", "show", "Client"):
        setattr(h.la.ollama, name, getattr(_real_ollama, name))
    # The shared deterministic fixture selects its legacy mock-model default;
    # production does not. Restore and verify the deployed waking substrate.
    h.la.MODEL = h.la.MINISTRAL_WAKING_MODEL
    h.la._ollama_environment_cache.update({"checked": False})
    identity = h.la.verify_waking_substrate()
    assert identity["model_digest"] == h.la.QUALIFIED_WAKING_SUBSTRATES[h.la.MINISTRAL_WAKING_MODEL]
    log = {"current_turn": None, "pass1": {}, "payloads": {}, "pass2_prompts": {}, "provider_calls": []}

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        response = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        log["provider_calls"].append({
            "model": model, "served_model": response.get("model"), "think": think,
            "options": dict(options or {}), "done_reason": response.get("done_reason"),
            "prompt_eval_count": response.get("prompt_eval_count"), "eval_count": response.get("eval_count"),
        })
        turn = log["current_turn"]
        if isinstance(format, dict) and "act" in format.get("properties", {}) and (options or {}).get("num_predict") != 1:
            log["pass1"][turn] = (json.loads(response["message"]["content"]), [dict(m) for m in messages])
        else:
            import pass1_binding_test_support as binding
            field, kind = binding.binding_field(messages, format)
            if kind == "payload" and (options or {}).get("num_predict") != 1:
                log["payloads"].setdefault(turn, {})[field] = json.loads(
                    response["message"]["content"])[field]
            elif format is None and not (options and options.get("num_predict") == 1):
                log["pass2_prompts"][turn] = [dict(m) for m in messages]
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
        "Please answer in words now: what exact page content reached you, which URL was it from, and was it page content rather than a snippet?",
    ]
    replies = []
    for turn, prompt in enumerate(turns):
        log["current_turn"] = turn
        result = h.la.run_waking_turn(
            h.la.AnaxiOrchestrator(), prompt, interaction_mode="conversation",
            human_input_authority=authority)
        replies.append(result["reply"])
    for turn, reply in enumerate(replies):
        raw, _messages = log["pass1"][turn]
        assembled = dict(raw, **log["payloads"].get(turn, {}))
        print(f"\n[turn {turn + 1}] pass1={ {k: v for k, v in assembled.items() if v not in ('', 'none', False, None)} }")
        print(f"[turn {turn + 1}] reply={reply[:400]!r}")
    dump = os.environ.get("ANAXI_PROBE_DUMP_PASS1")
    if dump:
        with open(dump, "w", encoding="utf-8") as handle:
            json.dump({"turns": [{"raw": log["pass1"][i][0], "payloads": log["payloads"].get(i, {}),
                                  "messages": log["pass1"][i][1]} for i in range(len(turns))],
                       "pass2": log["pass2_prompts"]}, handle)
    requests = []
    for i in range(len(turns)):
        raw, msgs = log["pass1"][i]
        requests.append((raw.get("external_info_request"), log["payloads"].get(i, {}).get("external_info_target")))
        tools = [m["content"][:160] for m in msgs if m["role"] == "user" and m["content"].startswith('{"authority"')]
        print(f"[turn {i + 1}] pass1 tool messages: {tools}")
    print("\nrequests:", requests, "fetched:", fetched)
    page_turns = [i for i, prompt in log["pass2_prompts"].items()
                  if "ZEBRAFISH-MARKER" in json.dumps(prompt)]
    print("pass2 saw PAGE marker in turns:", page_turns)
    generation_calls = [call for call in log["provider_calls"] if call["options"].get("num_predict") != 1]
    cost_probes = [call for call in log["provider_calls"] if call["options"].get("num_predict") == 1]
    print("provider calls:", len(log["provider_calls"]), "cost probes:", len(cost_probes))
    assert cost_probes == []
    assert {call["model"] for call in generation_calls} == {h.la.MINISTRAL_WAKING_MODEL}
    assert {call["served_model"] for call in generation_calls} == {h.la.MINISTRAL_WAKING_MODEL}
    assert {call["options"].get("num_ctx") for call in generation_calls} == {
        h.la.context_budget.WAKING_SERVING_CONTEXT}
    assert all(call["think"] is False and call["done_reason"] == "stop" for call in generation_calls)
    assert any(r == "web_search" for r, _ in requests), requests
    assert any(r == "fetch_url" and str(t).startswith("result:") for r, t in requests), requests
    assert fetched, "no real fetch was dispatched"
    fetch_turn = next(i for i, (request, _target) in enumerate(requests) if request == "fetch_url")
    url_reported = any(u in "\n".join(replies[fetch_turn:]) for u in set(fetched))
    print("a post-fetch reply reports the fetched URL:", url_reported)
    assert url_reported
    assert page_turns, "fetched content never reached waking"
    # One result is delivered per replying turn, in queue order. Search and
    # fetch may both lawfully be silent, so the older search result can occupy
    # the first replying turn and the fetched page the next one.
    assert fetch_turn <= min(page_turns) <= fetch_turn + 2
    assert any("ZEBRAFISH-MARKER" in replies[turn] for turn in page_turns)
