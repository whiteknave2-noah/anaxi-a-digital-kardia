"""READ-ONLY WEB SEARCH -> SOURCE SELECTION -> FETCH -> PROVENANCE regression suite.

Live production defect (2026-09-20): Clark's searches were dispatched with no
provider key (status not_configured, zero results) so he had nothing to select
from, no delivered result carried a selectable identity into Pass 1, and a
fetch needed a URL he was never shown. These tests exercise the real seams on
a synthetic provenance DB with injected transports (zero real network).
"""
import json
import os
import sys

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import external_information as ext
import external_information_net as net
import test_external_information as base  # synthetic-env helpers only

RESULTS = [
    {"rank": 0, "title": "Sodium-ion review", "url": "https://journal.example/na-ion", "snippet": "snippet one"},
    {"rank": 1, "title": "Prussian blue analogues", "url": "https://uni.example/pba", "snippet": "snippet two"},
]

DDG_PAGE = """
<html><body>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fjournal.example%2Fna-ion&amp;rut=abc">Sodium-ion &amp; review</a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">A <b>snippet</b> about cathodes</a></div>
<div class="result"><a class="result__a" href="//duckduckgo.com/y.js?ad_domain=ad.example">Sponsored</a></div>
<div class="result"><a class="result__a" href="https://direct.example/page">Direct</a>
<a class="result__snippet" href="x">direct snippet</a></div>
</body></html>
"""


def _dispatch(data_dir, session_id, started, occurred, tag, operation, target, **kw):
    trigger = base._wake_turn(data_dir, session_id, started, occurred, tag,
                              external_info_request=operation, external_info_target=target)
    dispatched = ext.record_external_info_query_and_result(
        data_dir, session_id=session_id, session_started_at=started, pipeline_key=base.PIPELINE_KEY,
        operation=operation, target=target, occurred_at=occurred, input_source_ref=trigger, **kw,
    )
    return trigger, dispatched


def _deliver(data_dir, session_id, started, occurred, tag, query_event_id):
    turn = base._wake_turn(data_dir, session_id, started, occurred, tag,
                           delivered_external_info_query_event_id=query_event_id)
    ext.record_external_info_delivered(
        data_dir, query_event_id=query_event_id, waking_turn_event_id=turn, occurred_at=occurred)
    return turn


def _search_then_deliver(name):
    data_dir = base._new_env(name)
    session_id, started, t = base._next_id(), base._now(), base._now()
    _, searched = _dispatch(data_dir, session_id, started, t, "s1", "web_search", "sodium ion",
                            search_fn=base._fake_search(results=[dict(r) for r in RESULTS]))
    _deliver(data_dir, session_id, started, t + 1, "d1", searched["query_event_id"])
    return data_dir, session_id, started, t + 2, searched


# ------------------------------------------------------------ 1. search only

def test_search_only_delivers_snippets_and_encodes_no_retrieval():
    data_dir = base._new_env("sel_search_only")
    session_id, started, t = base._next_id(), base._now(), base._now()
    _dispatch(data_dir, session_id, started, t, "s1", "web_search", "sodium ion",
              search_fn=base._fake_search(results=[dict(r) for r in RESULTS]))
    pending = ext.next_pending_external_info_result(data_dir, session_id)
    env = json.loads(ext.render_external_info_result_delivery(pending["result"]))
    assert env["result_semantics"] == "provider_snippets_not_fetched_pages"
    assert env["retrieved_source_content"] is False
    assert env["result_count"] == 2
    assert [r["choice"] for r in env["results"]] == [1, 2]
    assert env["to_read_a_result"]["external_info_target"] == "result:<choice>"
    assert "text" not in env and "final_url" not in env


def test_zero_result_search_says_nothing_was_returned():
    data_dir = base._new_env("sel_not_configured")
    session_id, started, t = base._next_id(), base._now(), base._now()
    _dispatch(data_dir, session_id, started, t, "s1", "web_search", "q",
              search_fn=base._fake_search(status=net.SEARCH_STATUS_NOT_CONFIGURED, detail="no key"))
    env = json.loads(ext.render_external_info_result_delivery(
        ext.next_pending_external_info_result(data_dir, session_id)["result"]))
    assert env["result_count"] == 0 and env["results"] == []
    assert env["retrieved_source_content"] is False
    assert "to_read_a_result" not in env


# ---------------------------------------------- 2. search -> select -> fetch

def test_search_select_fetch_returns_content_with_provenance():
    data_dir, session_id, started, t, searched = _search_then_deliver("sel_full")
    seen = []

    def fetcher(url, **_kw):
        seen.append(url)
        return {"status": net.FETCH_STATUS_SUCCESS, "requested_url": url, "final_url": url,
                "http_status": 200, "content_type": "text/html", "title": "PBA paper",
                "text": "substantive body text about Prussian blue", "truncated": False}

    _, fetched = _dispatch(data_dir, session_id, started, t, "f1", "fetch_url", "result:2", fetch_fn=fetcher)
    assert seen == ["https://uni.example/pba"]  # selection mapped to the right source
    assert fetched["status"] == net.FETCH_STATUS_SUCCESS
    pending = ext.next_pending_external_info_result(data_dir, session_id)
    assert pending["query_event_id"] == fetched["query_event_id"]
    env = json.loads(ext.render_external_info_result_delivery(pending["result"], max_chars=4000))
    assert env["retrieved_source_content"] is True
    assert env["result_semantics"] == "retrieved_page_text_not_search_snippets"
    assert "substantive body text" in env["text"]
    assert env["final_url"] == "https://uni.example/pba" and env["title"] == "PBA paper"
    assert env["selected_result"]["search_retrieval_id"] == searched["query_event_id"]
    assert env["selected_result"]["result_id"] == f"{searched['query_event_id']}:search-result:1"
    assert env["selected_result"]["title"] == "Prussian blue analogues"
    assert env["authority"] == "untrusted_external_data"


def test_selection_continuation_keeps_resolved_source():
    data_dir, session_id, started, t, _ = _search_then_deliver("sel_continue")
    long_text = "x" * 3000

    def fetcher(url, **_kw):
        return {"status": net.FETCH_STATUS_SUCCESS, "requested_url": url, "final_url": url, "http_status": 200,
                "content_type": "text/html", "title": "T", "text": long_text, "truncated": False}

    _dispatch(data_dir, session_id, started, t, "f1", "fetch_url", "result:1", fetch_fn=fetcher)
    env = json.loads(ext.render_external_info_result_delivery(
        ext.next_pending_external_info_result(data_dir, session_id)["result"], max_chars=1200))
    assert env["content_completeness"] == "delivery_truncated"
    # The continuation names the real URL, not a selection that a later search could rebind.
    assert env["continue_with"]["external_info_target"].startswith("https://journal.example/na-ion#anaxi_offset=")


# ---------------------------------------------------- 3. invalid / forged

def _persisted_result(data_dir, query_event_id):
    import sqlite3
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    try:
        return json.loads(conn.execute(
            "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
            (query_event_id, "external_info_result")).fetchone()[0])
    finally:
        conn.close()


def _assert_unresolved(data_dir, session_id, fetched):
    assert fetched["status"] == ext.RESULT_STATUS_SELECTION_NOT_RESOLVED
    result = _persisted_result(data_dir, fetched["query_event_id"])
    env = json.loads(ext.render_external_info_result_delivery(result))
    assert env["retrieved_source_content"] is False
    assert env["result_semantics"] == "no_page_content_retrieved"


def _no_network(*_a, **_k):
    raise AssertionError("network must not be touched for an unresolved selection")


def test_out_of_range_selection_is_refused_without_network():
    data_dir, session_id, started, t, _ = _search_then_deliver("sel_range")
    _, fetched = _dispatch(data_dir, session_id, started, t, "f1", "fetch_url", "result:9", fetch_fn=_no_network)
    _assert_unresolved(data_dir, session_id, fetched)


def test_selection_without_any_delivered_search_is_refused():
    data_dir = base._new_env("sel_none")
    session_id, started, t = base._next_id(), base._now(), base._now()
    _, fetched = _dispatch(data_dir, session_id, started, t, "f1", "fetch_url", "result:1", fetch_fn=_no_network)
    _assert_unresolved(data_dir, session_id, fetched)


def test_failed_or_empty_search_offers_nothing_to_select():
    data_dir = base._new_env("sel_failed_search")
    session_id, started, t = base._next_id(), base._now(), base._now()
    _dispatch(data_dir, session_id, started, t, "s1", "web_search", "q",
              search_fn=base._fake_search(status=net.SEARCH_STATUS_PROVIDER_FAILURE, detail="HTTP 500"))
    assert ext.latest_selectable_search_result(data_dir, session_id) is None
    _, fetched = _dispatch(data_dir, session_id, started, t + 1, "f1", "fetch_url", "result:1", fetch_fn=_no_network)
    _assert_unresolved(data_dir, session_id, fetched)


def test_a_set_pass1_could_not_have_shown_is_not_selectable():
    """Selection binds to what existed before the fetching turn: a set produced by the SAME turn's
    request (created after that turn committed) is never a valid basis for it."""
    data_dir = base._new_env("sel_same_turn")
    session_id, started, t = base._next_id(), base._now(), base._now()
    trigger = base._wake_turn(data_dir, session_id, started, t, "both",
                              external_info_request="fetch_url", external_info_target="result:1")
    # a search recorded by a LATER turn, then the earlier turn's fetch is resolved
    _, searched = _dispatch(data_dir, session_id, started, t + 1, "s2", "web_search", "q",
                            search_fn=base._fake_search(results=[dict(r) for r in RESULTS]))
    fetched = ext.record_external_info_query_and_result(
        data_dir, session_id=session_id, session_started_at=started, pipeline_key=base.PIPELINE_KEY,
        operation="fetch_url", target="result:1", occurred_at=t, input_source_ref=trigger, fetch_fn=_no_network)
    assert fetched["status"] == ext.RESULT_STATUS_SELECTION_NOT_RESOLVED


def test_selection_window_expires_after_a_few_waking_turns():
    data_dir, session_id, started, t, _ = _search_then_deliver("sel_window")
    assert ext.latest_selectable_search_result(data_dir, session_id) is not None
    for i in range(ext.SELECTION_WINDOW_WAKING_TURNS + 1):
        base._wake_turn(data_dir, session_id, started, t + 1 + i, f"filler{i}")
    assert ext.latest_selectable_search_result(data_dir, session_id) is None


def test_forged_result_identity_is_not_a_selection():
    """Only result:<choice> resolves; a manufactured result id or another session's
    identity is just an (invalid) URL and reaches the ordinary URL validation."""
    for forged in ("xi-FORGED:search-result:0", "result:0", "result:-1", "result:1;evil", "Result:1"):
        assert ext.parse_result_selection(forged) is None
    assert ext.parse_result_selection("result:3") == (3, 0)
    assert ext.parse_result_selection("result:3#anaxi_offset=40") == (3, 40)


def test_selection_cannot_reach_another_sessions_results():
    data_dir, _sid, _s, _t, _ = _search_then_deliver("sel_other_session")
    other, started, t = base._next_id(), base._now(), base._now()
    _, fetched = _dispatch(data_dir, other, started, t, "f1", "fetch_url", "result:1", fetch_fn=_no_network)
    _assert_unresolved(data_dir, other, fetched)


# ------------------------------------------------------------ 4. fetch failure

def test_fetch_failure_is_truthful_and_snippets_are_not_promoted():
    data_dir, session_id, started, t, _ = _search_then_deliver("sel_fetch_fail")

    def failing(url, **_kw):
        return {"status": net.FETCH_STATUS_HTTP_FAILURE, "requested_url": url, "detail": "HTTP 403"}

    _, fetched = _dispatch(data_dir, session_id, started, t, "f1", "fetch_url", "result:1", fetch_fn=failing)
    assert fetched["status"] == net.FETCH_STATUS_HTTP_FAILURE
    result = ext.next_pending_external_info_result(data_dir, session_id)["result"]
    env = json.loads(ext.render_external_info_result_delivery(result))
    assert env["retrieved_source_content"] is False
    assert env["result_semantics"] == "no_page_content_retrieved"
    assert "text" not in env and "snippet" not in json.dumps(env)
    assert env["selected_result"]["url"] == "https://journal.example/na-ion"


# ------------------------------------------------------- 5. waking carriage

def test_pass1_receives_delivered_search_choices_as_untrusted_tool_data():
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), "r", encoding="utf-8") as f:
        source = f.read()
    assert "latest_selectable_search_result(" in source
    assert 'external_data_message(pass1_search_choices_contrib.rendered_text)' in source
    # never concatenated into the host/system message
    assert "pass1_system_content + \"\\n\\n\" + pass1_search_choices" not in source


def test_pass1_choices_are_offered_as_soon_as_a_successful_set_exists():
    data_dir = base._new_env("sel_pass1_offer")
    session_id, started, t = base._next_id(), base._now(), base._now()
    assert ext.latest_selectable_search_result(data_dir, session_id) is None
    _, searched = _dispatch(data_dir, session_id, started, t, "s1", "web_search", "q",
                            search_fn=base._fake_search(results=[dict(r) for r in RESULTS]))
    # In flight (not yet carried to any Pass 2): Pass 1 of the very next turn can already choose from it.
    offered = ext.latest_selectable_search_result(data_dir, session_id)
    assert offered["retrieval_id"] == searched["query_event_id"]
    env = json.loads(ext.render_search_choices(offered))
    assert [r["url"] for r in env["results"]] == [r["url"] for r in RESULTS]
    assert env["retrieved_source_content"] is False
    assert env["results"][1]["read_this_page"] == {"external_info_request": "fetch_url", "external_info_target": "result:2"}
    assert env["authority"] == "untrusted_external_data"


def test_fetched_content_survives_pass2_growth_window_with_identity():
    data_dir, session_id, started, t, _ = _search_then_deliver("sel_pass2_window")

    def fetcher(url, **_kw):
        return {"status": net.FETCH_STATUS_SUCCESS, "requested_url": url, "final_url": url, "http_status": 200,
                "content_type": "text/html", "title": "T", "text": "y" * 500, "truncated": False}

    _dispatch(data_dir, session_id, started, t, "f1", "fetch_url", "result:1", fetch_fn=fetcher)
    result = ext.next_pending_external_info_result(data_dir, session_id)["result"]
    env = json.loads(ext.render_external_info_result_delivery(result))
    for key in ("retrieved_source_content", "selected_result", "final_url", "text", "retrieval_id"):
        assert key in env


# ------------------------------------------------------- keyless provider

def test_keyless_search_parses_real_destinations_and_skips_ads():
    seen = {}

    def getter(url, key, timeout):
        seen["url"], seen["key"] = url, key
        return 200, DDG_PAGE.encode()

    out = net.search_keyless("sodium ion", http_get_fn=getter)
    assert out["status"] == net.SEARCH_STATUS_SUCCESS and out["provider"] == net.KEYLESS_SEARCH_PROVIDER
    assert [r["url"] for r in out["results"]] == ["https://journal.example/na-ion", "https://direct.example/page"]
    assert out["results"][0]["title"] == "Sodium-ion & review"
    assert out["results"][0]["snippet"] == "A snippet about cathodes"
    assert seen["key"] is None and seen["url"].startswith(net.KEYLESS_SEARCH_ENDPOINT + "?q=sodium+ion")


def test_keyless_provider_challenge_is_a_failure_never_bypassed():
    out = net.search_keyless("q", http_get_fn=lambda u, k, t: (202, b"<html>challenge</html>"))
    assert out["status"] == net.SEARCH_STATUS_PROVIDER_FAILURE and out["results"] == []


def test_search_public_uses_keyed_provider_only_when_configured(monkeypatch=None):
    old = os.environ.pop(net.EXTERNAL_SEARCH_API_KEY_ENV_VAR, None)
    try:
        out = net.search_public("q", http_get_fn=lambda u, k, t: (200, DDG_PAGE.encode()))
        assert out["provider"] == net.KEYLESS_SEARCH_PROVIDER
        os.environ[net.EXTERNAL_SEARCH_API_KEY_ENV_VAR] = "test-key"
        out = net.search_public("q", http_get_fn=lambda u, k, t: (200, b'{"web":{"results":[]}}'))
        assert out["provider"] == net.SEARCH_PROVIDER
    finally:
        os.environ.pop(net.EXTERNAL_SEARCH_API_KEY_ENV_VAR, None)
        if old is not None:
            os.environ[net.EXTERNAL_SEARCH_API_KEY_ENV_VAR] = old


def test_keyless_results_persist_and_deliver_through_the_real_writer():
    data_dir = base._new_env("sel_keyless_persist")
    session_id, started, t = base._next_id(), base._now(), base._now()

    def searcher(query):
        return net.search_keyless(query, http_get_fn=lambda u, k, tt: (200, DDG_PAGE.encode()))

    _dispatch(data_dir, session_id, started, t, "s1", "web_search", "q", search_fn=searcher)
    pending = ext.next_pending_external_info_result(data_dir, session_id)
    assert pending["result"]["provider"] == net.KEYLESS_SEARCH_PROVIDER
    assert len(pending["result"]["results"]) == 2


# ------------------------------------------------------------ 6. read-only

def test_selection_adds_no_write_authority():
    for path in ("external_information.py", "external_information_net.py"):
        with open(os.path.join(ANAXI_FINAL, path), "r", encoding="utf-8") as f:
            source = f.read()
        for forbidden in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"', "Authorization"):
            assert forbidden not in source
    assert ext.VALID_OPERATIONS == ("web_search", "fetch_url")


WIKI_JSON = json.dumps({"query": {"search": [
    {"title": "Sodium-ion battery", "snippet": 'A <span class="searchmatch">sodium</span>-ion battery'},
    {"title": "Solid-state battery (SSB)", "snippet": "x"},
]}}).encode()


def test_wikipedia_provider_parses_and_builds_real_urls():
    out = net.search_wikipedia("sodium", http_get_fn=lambda u, k, t: (200, WIKI_JSON))
    assert out["provider"] == net.WIKIPEDIA_SEARCH_PROVIDER and out["status"] == net.SEARCH_STATUS_SUCCESS
    assert out["results"][0]["url"] == "https://en.wikipedia.org/wiki/Sodium-ion_battery"
    assert out["results"][0]["snippet"] == "A sodium -ion battery" or "sodium" in out["results"][0]["snippet"]
    assert "<span" not in out["results"][0]["snippet"]
    assert out["results"][1]["url"] == "https://en.wikipedia.org/wiki/Solid-state_battery_(SSB)"


def test_public_search_falls_back_to_wikipedia_and_says_so():
    old = os.environ.pop(net.EXTERNAL_SEARCH_API_KEY_ENV_VAR, None)
    try:
        def getter(url, key, timeout):
            return (202, b"challenge") if "duckduckgo" in url else (200, WIKI_JSON)
        out = net.search_public("q", http_get_fn=getter)
        assert out["provider"] == net.WIKIPEDIA_SEARCH_PROVIDER
        assert "duckduckgo_html unavailable" in out["detail"] and "Wikipedia only" in out["detail"]
        env = json.loads(ext.render_external_info_result_delivery(dict(
            out, operation="web_search", target="q", retrieval_id="xi-X", results=[
                dict(r, result_id=f"xi-X:search-result:{i}") for i, r in enumerate(out["results"])])))
        assert env["provider"] == "wikipedia_search" and "Wikipedia only" in env["detail"]

        both_down = net.search_public("q", http_get_fn=lambda u, k, t: (503, b""))
        assert both_down["status"] == net.SEARCH_STATUS_PROVIDER_FAILURE and both_down["results"] == []
    finally:
        if old is not None:
            os.environ[net.EXTERNAL_SEARCH_API_KEY_ENV_VAR] = old


def test_https_contexts_stay_verifying_and_use_certifi_bundle():
    import ssl
    ctx = net.verified_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    try:
        import certifi  # noqa: F401
    except ImportError:
        return
    assert ctx.cert_store_stats()["x509_ca"] > 0


def test_real_waking_turn_shows_pass1_the_delivered_choices_as_tool_data(monkeypatch):
    """Through the real run_waking_turn: Pass 1 (where fetch_url is chosen)
    receives the delivered search set as an untrusted tool message, never in the
    system message, and Pass 2's own generation is unaffected."""
    import test_production_delivery_floor as floor
    served = {"retrieval_id": "xi-SEARCH", "operation": "web_search", "target": "sodium ion",
              "status": "success", "provider": "wikipedia_search", "results": [
                  dict(r, result_id=f"xi-SEARCH:search-result:{i}") for i, r in enumerate(RESULTS)]}
    original = ext.latest_selectable_search_result
    ext.latest_selectable_search_result = lambda *a, **k: served
    try:
        la, calls, results, _delivered = floor._ordinary_turn(monkeypatch, compress=True)
    finally:
        ext.latest_selectable_search_result = original
    pass1_calls = [c for c in calls if c["format"] is not None and (c["options"] or {}).get("num_predict") != 1]
    assert pass1_calls, "expected a Pass-1 structured call"
    messages = pass1_calls[0]["messages"]
    tool = [m for m in messages if m["role"] == "user" and m["content"].startswith('{"authority":"untrusted_external_data"')]
    assert len(tool) == 1
    assert messages[-1]["role"] == "user" and not messages[-1]["content"].startswith("{")
    env = json.loads(tool[0]["content"])
    assert env["authority"] == "untrusted_external_data"
    assert env["result_semantics"] == "provider_snippets_not_fetched_pages"
    assert [r["title"] for r in env["results"]] == [r["title"] for r in RESULTS]
    assert env["results"][0]["read_this_page"]["external_info_target"] == "result:1"
    assert "Prussian blue analogues" not in messages[0]["content"]
    assert messages[-1]["role"] == "user"
