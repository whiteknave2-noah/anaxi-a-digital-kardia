"""Production-sized delivery of soft context in ORDINARY waking Pass 2.

At production sizes (1207-byte identity text, 456-byte human message) the
byte-costed hard floor of Pass 2 is ~2670 of 3200, so the pre-repair
compositor shed every soft kind on ordinary turns: recent dialogue, retrieved
memory, and -- fatally for their capabilities -- a fetched page or a received
Caret message, which were "delivered" nowhere. A real provider measurement of
the atomic contributions restores the room the byte upper bound over-priced.
"""
import json
import math
import os
import sys

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
if ANAXI_FINAL not in sys.path:
    sys.path.insert(0, ANAXI_FINAL)

import context_budget
import test_owc5_s2_integration as fixture
import test_wsp1_production_hard_floor as production
import ui_turn_diagnostics


def _is_external_data(message):
    """Host-delivered external data rides as its own user-role message (see
    llama_anaxi.external_data_message), recognizable by its untrusted-data framing."""
    return message["role"] == "user" and (
        message["content"].startswith('{"authority":"untrusted_external_data"')
        or message["content"].startswith("A Caret message was written to you"))


def _tokenizer_like(messages):
    return sum(math.ceil(len(m.get("content", "").encode("utf-8")) / 4) for m in messages) or 1


def _ordinary_turn(monkeypatch, *, compress, pending_external=None):
    _core, style = fixture._calibration_identity_preamble_and_style()
    choice = {"act": "develop_current", "thread": "x", "direction_request": "none", "relinquish_direction": False}
    la, calls, _p1, _p2, persistence = fixture.fresh_llama_anaxi(
        pass1_value=choice, pass2_value={"expression": "ok"},
        core_system_text=production._production_sized_core(style), style_instruction=style,
    )
    delivered = []
    original_writer = la.native_provenance_writer.stage_and_record_native_waking_turn

    def writer(*args, **kwargs):
        # The in-memory fake predates delivery markers; record and drop them.
        if kwargs.get("delivered_external_info_query_event_id") is not None:
            delivered.append(kwargs["delivered_external_info_query_event_id"])
        kwargs = {k: v for k, v in kwargs.items() if not k.startswith("delivered_external_info")}
        return original_writer(*args, **kwargs)

    monkeypatch.setattr(la.native_provenance_writer, "stage_and_record_native_waking_turn", writer)
    if pending_external is not None:
        monkeypatch.setattr(la.external_information, "reconcile_incomplete_external_info_results", lambda *a, **k: 0)
        monkeypatch.setattr(la.external_information, "next_pending_external_info_result", lambda *a, **k: pending_external)
        monkeypatch.setattr(la.external_information, "record_external_info_delivered", lambda *a, **k: {"delivered": True})
    results = {}
    monkeypatch.setattr(
        ui_turn_diagnostics, "record_context_budget_result", lambda name, r: results.__setitem__(name, r),
    )

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        calls.append({"format": format, "options": options, "messages": [dict(m) for m in messages]})
        if options and options.get("num_predict") == 1:
            count = _tokenizer_like(messages) if compress else sum(len(m["content"].encode()) for m in messages)
            return {"message": {"content": "x"}, "prompt_eval_count": count, "done": True,
                    "done_reason": "stop", "eval_count": 1}
        is_pass1 = isinstance(format, dict) and "act" in format.get("properties", {})
        content = json.dumps(choice) if is_pass1 else "ok"
        return {"message": {"content": content}, "prompt_eval_count": 1, "done": True,
                "done_reason": "stop", "eval_count": 20}

    la.ollama.chat = chat
    la.run_waking_turn(la.AnaxiOrchestrator(), production._message(456), interaction_mode="conversation")
    return la, calls, results, delivered


def _fetched_page(chars):
    body = ("Photosynthesis converts light energy into chemical energy in plants. " * 60)[:chars]
    return {"query_event_id": "q1", "result": {
        "operation": "fetch_url", "target": "https://x.example/", "status": "success",
        "retrieval_id": "r1", "requested_url": "https://x.example/", "final_url": "https://x.example/",
        "http_status": 200, "content_type": "text/html", "title": "T", "text": body, "truncated": False,
    }}


def test_fetched_page_reaches_the_subject_at_production_sizes(monkeypatch):
    la, calls, results, delivered = _ordinary_turn(monkeypatch, compress=True, pending_external=_fetched_page(1100))
    pass2 = results["pass2"]
    assert pass2.fits
    assert pass2.included_kind(context_budget.EXTERNAL_INFO_RESULT) is not None
    generation = next(
        c for c in calls if c["format"] is None
        and (c.get("options") or {}).get("num_predict") == context_budget.PASS2_GENERATION_RESERVE
    )
    tool_messages = [m for m in generation["messages"] if _is_external_data(m)]
    assert tool_messages and "Photosynthesis converts light energy" in tool_messages[0]["content"]
    assert delivered, "delivery must be acknowledged only because it really was carried"


def test_without_measurement_recovery_the_same_turn_sheds_the_page(monkeypatch):
    """The provider that cannot measure (or measures no lower than the byte
    bound) keeps the safe estimator: nothing is fabricated as delivered."""
    la, calls, results, delivered = _ordinary_turn(monkeypatch, compress=False, pending_external=_fetched_page(1100))
    assert results["pass2"].fits
    assert results["pass2"].included_kind(context_budget.EXTERNAL_INFO_RESULT) is None
    assert delivered == []


def test_recost_lowers_only_what_measurement_proves_and_never_above_the_bound(monkeypatch):
    la, *_ = fixture.fresh_llama_anaxi(pass1_value=None, pass2_value=None)
    hard = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, "x" * 2400, hard=True)
    soft = context_budget.Contribution(context_budget.EXTERNAL_INFO_RESULT, "y" * 1000, hard=False)
    unit = context_budget.Contribution(
        context_budget.RECENT_DIALOGUE, "z" * 500, hard=False,
        droppable_units=["z" * 500], render_fn=lambda units: "".join(units),
    )
    measured = {"x" * 2400: 600, "y" * 1000: 5000}   # a measurement above the bound is ignored
    lowered = context_budget.recost_by_measurement(
        [hard, soft, unit], lambda text: measured.get(text),
    )
    assert lowered == 1
    assert hard.cost == 600 + context_budget.MEASURED_BOUNDARY_TOKENS
    assert soft.cost == 1000              # measurement was not lower: bound kept
    assert unit.cost == 500               # unit-based contributions keep the estimator


def test_dry_run_never_mutates_the_live_contributions():
    unit = context_budget.Contribution(
        context_budget.RECENT_DIALOGUE, "abcd" * 300, hard=False,
        droppable_units=["abcd" * 100] * 3, render_fn=lambda units: "".join(units),
    )
    hard = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, "h" * 1000, hard=True)
    assert context_budget.would_shed([hard, unit], 1500)
    assert len(unit.droppable_units) == 3 and unit.cost == context_budget.estimate_tokens("abcd" * 300)


def test_large_fetched_page_is_delivered_as_a_window_that_continues_to_the_end(monkeypatch):
    """A page larger than one prompt is not cut off silently: the delivered
    window is real text, the envelope states where it ended, and following its
    ``continue_with`` reaches the end of the extracted text exactly."""
    import external_information as ext

    page = "".join(f"Sentence {i:05d} of a long fetched article about photosynthesis. " for i in range(600))
    pending = _fetched_page(10)
    pending["result"]["text"] = page
    pending["result"]["text_total_chars"] = len(page)
    la, calls, results, delivered = _ordinary_turn(monkeypatch, compress=True, pending_external=pending)

    assert results["pass2"].included_kind(context_budget.EXTERNAL_INFO_RESULT) is not None
    generation = next(
        c for c in calls if c["format"] is None
        and (c.get("options") or {}).get("num_predict") == context_budget.PASS2_GENERATION_RESERVE
    )
    envelope = json.loads(next(m for m in generation["messages"] if _is_external_data(m))["content"])
    assert envelope["content_completeness"] == "delivery_truncated"
    assert len(envelope["text"]) > ext.MAX_DELIVERY_TEXT_CHARS       # more than the old fixed bound
    assert page.startswith(envelope["text"])

    # Follow the continuation to the end, exactly as the subject would.
    collected, envelope_now = envelope["text"], envelope
    for _ in range(200):
        if "continue_with" not in envelope_now:
            break
        base, offset = ext.split_fetch_continuation(envelope_now["continue_with"]["external_info_target"])
        assert offset == len(collected) and base == "https://x.example/"
        result = {**pending["result"], "text": page[offset:], "text_offset": offset}
        envelope_now = json.loads(ext.render_external_info_result_delivery(result, max_chars=4800))
        assert page[offset:].startswith(envelope_now["text"])
        collected += envelope_now["text"]
    assert collected == page


def test_fetch_continuation_asks_for_the_same_url_and_slices_the_extracted_text():
    import external_information as ext

    seen = []

    def fetch(url, **kwargs):
        seen.append((url, kwargs))
        text = "0123456789" * 50
        return {"status": "success", "requested_url": url, "final_url": url, "http_status": 200,
                "content_type": "text/html", "title": "T", "text": text, "truncated": False}

    query = {"operation": "fetch_url", "target": "https://x.example/page#anaxi_offset=100"}
    outcome = ext._perform_operation(query, fetch_fn=fetch)
    assert seen[0][0] == "https://x.example/page"                     # the fragment is never requested
    assert seen[0][1]["max_text_chars"] >= 100 + 20000 - 1            # asked far enough to reach the window
    assert outcome["text"] == ("0123456789" * 50)[100:]
    assert outcome["text_offset"] == 100 and outcome["text_total_chars"] == 500
    assert outcome["target"] == "https://x.example/page#anaxi_offset=100"  # occurrence identity preserved


def _pending_message(index, text):
    return {
        "event_id": f"inbound-{index}", "content": text,
        "destination": {"display_label": "Caret private family channel"},
        "correspondent": {"principal_actor_id": "human-actor-owner", "display_label": "Alex", "role": "owner"},
        "metadata": {
            "destination_kind": "channel", "destination_id": "discord_destination-abc",
            "author_name": "Alex", "author_id": "1", "remote_timestamp": "2026-09-18T20:00:00Z",
            "discord_message_id": str(1000 + index),
        },
    }


def _discord_turn(monkeypatch, messages, *, compress=True):
    """One production-sized ordinary turn with ``messages`` pending."""
    _core, style = fixture._calibration_identity_preamble_and_style()
    choice = {"act": "develop_current", "thread": "x", "direction_request": "none", "relinquish_direction": False}
    la, calls, _p1, _p2, persistence = fixture.fresh_llama_anaxi(
        pass1_value=choice, pass2_value={"expression": "ok"},
        core_system_text=production._production_sized_core(style), style_instruction=style,
    )
    dc = la.discord_correspondence
    monkeypatch.setattr(la, "_discord_correspondence_available", lambda *a, **k: True)
    monkeypatch.setattr(dc, "list_authorized_destinations", lambda *a, **k: [])
    monkeypatch.setattr(dc, "reconcile_outbound_attempts", lambda *a, **k: None)
    monkeypatch.setattr(dc, "poll_authorized_inbound", lambda *a, **k: [])
    monkeypatch.setattr(dc, "next_pending_inbound", lambda *a, **k: list(messages))
    carried = []
    original_writer = la.native_provenance_writer.stage_and_record_native_waking_turn

    def writer(*args, **kwargs):
        carried.extend(kwargs.pop("delivered_discord_inbound_event_ids", None) or [])
        return original_writer(*args, **{k: v for k, v in kwargs.items() if not k.startswith("delivered_")})

    monkeypatch.setattr(la.native_provenance_writer, "stage_and_record_native_waking_turn", writer)
    results = {}
    monkeypatch.setattr(ui_turn_diagnostics, "record_context_budget_result", lambda n, r: results.__setitem__(n, r))

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        calls.append({"format": format, "options": options, "messages": [dict(m) for m in messages]})
        if options and options.get("num_predict") == 1:
            count = _tokenizer_like(messages) if compress else sum(len(m["content"].encode()) for m in messages)
            return {"message": {"content": "x"}, "prompt_eval_count": count, "done": True,
                    "done_reason": "stop", "eval_count": 1}
        is_pass1 = isinstance(format, dict) and "act" in format.get("properties", {})
        content = json.dumps(choice) if is_pass1 else "ok"
        return {"message": {"content": content}, "prompt_eval_count": 1, "done": True,
                "done_reason": "stop", "eval_count": 20}

    la.ollama.chat = chat
    la.run_waking_turn(la.AnaxiOrchestrator(), production._message(456), interaction_mode="conversation")
    generation = next(
        c for c in calls if c["format"] is None
        and (c.get("options") or {}).get("num_predict") == context_budget.PASS2_GENERATION_RESERVE
    )
    return generation, carried, results


def test_received_caret_messages_reach_the_subject_at_production_sizes(monkeypatch):
    messages = [_pending_message(1, "Hello from Alex, first."), _pending_message(2, "And a second short note.")]
    generation, carried, results = _discord_turn(monkeypatch, messages)
    tool_text = "\n".join(m["content"] for m in generation["messages"] if _is_external_data(m))
    assert "Hello from Alex, first." in tool_text and "And a second short note." in tool_text
    assert carried == ["inbound-1", "inbound-2"]
    assert results["pass2"].fits


def test_a_message_too_large_for_the_room_never_strands_the_earlier_ones(monkeypatch):
    """FIFO whole-message prefix: the oldest messages that fit are delivered and
    named as the carried ids; the oversized newest stays pending, in order."""
    la, *_ = fixture.fresh_llama_anaxi(pass1_value=None, pass2_value=None)
    messages = [
        _pending_message(1, "Hello from Alex, first."),
        _pending_message(2, "Second note."),
        _pending_message(3, "A very long message. " * 95),   # ~2000 chars
    ]
    rendered_all = "\n\n".join(la.discord_correspondence.render_inbound_delivery(m) for m in messages)
    contribution = context_budget.Contribution(
        context_budget.DISCORD_INBOUND_CARRIAGE, rendered_all, hard=False,
        source_ids=[m["event_id"] for m in messages],
    )
    hard = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, "h" * 2000, hard=True)
    # Real cost = one token per 3 bytes, so the first two messages fit the
    # 1300-token room and the third does not.
    monkeypatch.setattr(la, "_measure_real_text_cost_memoized", lambda text, schema=None: len(text.encode()) // 3)
    la.fit_inbound_fifo_prefix(contribution, messages, [hard, contribution], 3300)

    composed = context_budget.compose_within_budget([hard, contribution], 3300)
    assert composed.fits
    assert composed.delivered_source_ids(context_budget.DISCORD_INBOUND_CARRIAGE) == ["inbound-1", "inbound-2"]
    assert "Second note." in contribution.rendered_text and "A very long message." not in contribution.rendered_text


def test_recent_dialogue_survives_ordinary_pass2_at_production_sizes(monkeypatch):
    """Live diagnostics showed Pass 2 shedding ALL recent dialogue for a
    574-byte message (its kinds_dropped listed recent_dialogue): the subject
    replied without the conversation it was in. Measured re-costing restores
    the room, and what was retained is exactly the newest pairs, whole."""
    _core, style = fixture._calibration_identity_preamble_and_style()
    choice = {"act": "develop_current", "thread": "x", "direction_request": "none", "relinquish_direction": False}
    la, calls, _p1, _p2, _persistence = fixture.fresh_llama_anaxi(
        pass1_value=choice, pass2_value={"expression": "ok"},
        core_system_text=production._production_sized_core(style), style_instruction=style,
    )
    pairs = []
    for i in range(4):
        pairs += [
            {"role": "user", "content": f"Earlier question {i}: " + "q" * 260},
            {"role": "assistant", "content": f"Earlier answer {i}: " + "a" * 300},
        ]
    import provenance_schema
    provenance_schema.create_provenance_db(os.path.join(la.PROVENANCE_DB_DIR, "anaxi_provenance.db")).close()
    monkeypatch.setattr(la, "build_session_dialogue_window", lambda *a, **k: list(pairs))
    results = {}
    monkeypatch.setattr(ui_turn_diagnostics, "record_context_budget_result", lambda n, r: results.__setitem__(n, r))

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        calls.append({"format": format, "options": options, "messages": [dict(m) for m in messages]})
        if options and options.get("num_predict") == 1:
            return {"message": {"content": "x"}, "prompt_eval_count": _tokenizer_like(messages), "done": True,
                    "done_reason": "stop", "eval_count": 1}
        is_pass1 = isinstance(format, dict) and "act" in format.get("properties", {})
        content = json.dumps(choice) if is_pass1 else "ok"
        return {"message": {"content": content}, "prompt_eval_count": 1, "done": True,
                "done_reason": "stop", "eval_count": 20}

    la.ollama.chat = chat
    la.run_waking_turn(la.AnaxiOrchestrator(), production._message(574), interaction_mode="conversation")
    generation = next(
        c for c in calls if c["format"] is None
        and (c.get("options") or {}).get("num_predict") == context_budget.PASS2_GENERATION_RESERVE
    )
    sent = [m["content"] for m in generation["messages"]]
    retained = [c for c in ("Earlier answer 3:", "Earlier answer 2:", "Earlier answer 1:", "Earlier answer 0:") if any(c in t for t in sent)]
    assert retained and retained[0] == "Earlier answer 3:"      # newest first, never a gap
    assert results["pass2"].fits
    dialogue = results["pass2"].included_kind(context_budget.RECENT_DIALOGUE)
    assert dialogue is not None and len(dialogue.droppable_units) == len(retained)


def test_roaming_observation_larger_than_its_room_is_delivered_as_a_window():
    """Unattended roaming shows the subject the mechanical result of its last
    workspace action. A full 4000-character library page cannot ride whole in
    Stage 1's room; it must arrive as a truthful window, not vanish."""
    import workspace_roaming as roaming

    content = "".join(f"Line {i:04d}: text the subject read while roaming.\n" for i in range(90))[:4000]
    state = roaming.empty_roaming_state()
    state["last_workspace_observation"] = {
        "resource_class": "library", "action": "read", "relative_path": "book.txt", "status": "performed",
        "action_id": "a1",
        "bounded_result": {
            "action": "read", "scope": "local_workspace/library", "boundary": "b", "consequence": "c",
            "rationale": "r",
            "result": {"content": content, "next_offset": len(content), "has_more": True,
                       "next_request": '{"offset":%d,"max_chars":4000}' % len(content)},
        },
    }
    for _ in range(3):   # occupy the rest of the room with soft context too
        state["recent_public_decisions"].append({"act": "wait", "resource_class": None})
    composition = roaming._compose_roaming_stage1_budget(state, [])
    assert composition.fits
    assert composition.included_kind(context_budget.LAST_WORKSPACE_OBSERVATION) is not None
    messages, _units = roaming._roaming_stage1_messages_from_composition(state, composition)
    text = "\n".join(m["content"] for m in messages)
    assert "Line 0000" in text and "delivery_window" in text


def test_a_pinned_operative_directive_never_poisons_ordinary_waking(monkeypatch):
    """A pinned single-unit directive of ordinary length (the limit is 4000
    characters) cannot be shed, so under byte costs it pushed every later Pass 2
    over budget: setting a directive made every subsequent turn fail closed.
    Its real measured cost fits, and the subject's standing choice is carried."""
    _core, style = fixture._calibration_identity_preamble_and_style()
    choice = {"act": "develop_current", "thread": "x", "direction_request": "none", "relinquish_direction": False}
    la, calls, _p1, _p2, persistence = fixture.fresh_llama_anaxi(
        pass1_value=choice, pass2_value={"expression": "ok"},
        core_system_text=production._production_sized_core(style), style_instruction=style,
    )
    standing = "When I am uncertain I say so plainly and I ask before assuming. " * 14   # ~870 chars
    active = {"active_event_id": "dir-1", "activated_at": "2026-09-18T00:00:00Z", "directive_text": standing}
    monkeypatch.setattr(la.operative_directive, "fetch_active_directive_for_viewer", lambda *a, **k: active)
    results = {}
    monkeypatch.setattr(ui_turn_diagnostics, "record_context_budget_result", lambda n, r: results.__setitem__(n, r))

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        calls.append({"format": format, "options": options, "messages": [dict(m) for m in messages]})
        if options and options.get("num_predict") == 1:
            return {"message": {"content": "x"}, "prompt_eval_count": _tokenizer_like(messages), "done": True,
                    "done_reason": "stop", "eval_count": 1}
        is_pass1 = isinstance(format, dict) and "act" in format.get("properties", {})
        content = json.dumps(choice) if is_pass1 else "ok"
        return {"message": {"content": content}, "prompt_eval_count": 1, "done": True,
                "done_reason": "stop", "eval_count": 20}

    la.ollama.chat = chat
    la.run_waking_turn(la.AnaxiOrchestrator(), production._message(456), interaction_mode="conversation")
    assert results["pass2"].fits
    assert results["pass2"].included_kind(context_budget.OPERATIVE_DIRECTIVE) is not None
    generation = next(
        c for c in calls if c["format"] is None
        and (c.get("options") or {}).get("num_predict") == context_budget.PASS2_GENERATION_RESERVE
    )
    assert standing.strip() in "".join(m["content"] for m in generation["messages"])
    assert len(persistence) == 1


def test_every_soft_kind_composed_into_ordinary_waking_is_consumed_after_composition():
    """Structural guard for the defect class 'composed and costed, never sent':
    the operative directive was fetched, rendered and budgeted for ordinary
    Pass 2 but its text was never placed in the prompt, so a SET directive had
    no operative effect. Every soft kind ordinary waking constructs must be read
    back from the composition result (included_kind / delivered_source_ids)."""
    import re

    source = open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8").read()
    start = source.index("def run_waking_turn(")
    body = source[start:]
    constructed = set(re.findall(r"Contribution\(\s*context_budget\.([A-Z_]+)", body))
    consumed = set(re.findall(r"(?:included_kind|delivered_source_ids)\(\s*context_budget\.([A-Z_]+)", body))
    # Consumed structurally rather than through included_kind():
    #   hard kinds are always present; RECENT_DIALOGUE is read via dialogue_messages().
    structurally_consumed = {
        "CORE_SYSTEM_CONTROL", "CURRENT_HUMAN_MESSAGE", "MECHANICAL_STATE", "WORKING_SET",
        "CONVERSATION_FRAMING", "RECENT_DIALOGUE", "ACTION_MENU_FIXED",
    }
    unconsumed = sorted(constructed - consumed - structurally_consumed)
    assert unconsumed == [], f"composed and costed but never delivered to the subject: {unconsumed}"


def test_all_search_results_reach_the_subject_not_just_the_first_few(monkeypatch):
    """The default 1200-character bound silently omitted search items 4-5. At
    production sizes the measured room carries every provider result whole."""
    items = [
        {"rank": i, "result_id": f"q1:search-result:{i}", "title": f"Result title number {i} about photosynthesis",
         "url": f"https://example.org/article-{i}", "snippet": "A snippet describing the article in some detail. " * 4}
        for i in range(5)
    ]
    pending = {"query_event_id": "q1", "result": {
        "operation": "web_search", "target": "photosynthesis", "status": "success", "retrieval_id": "q1",
        "provider": "duckduckgo", "results": items,
    }}
    la, calls, results, delivered = _ordinary_turn(monkeypatch, compress=True, pending_external=pending)
    generation = next(
        c for c in calls if c["format"] is None
        and (c.get("options") or {}).get("num_predict") == context_budget.PASS2_GENERATION_RESERVE
    )
    envelope = json.loads(next(m for m in generation["messages"] if _is_external_data(m))["content"])
    assert [r["rank"] for r in envelope["results"]] == [0, 1, 2, 3, 4]
    assert envelope["omitted_result_count"] == 0


def test_multi_unit_families_are_priced_per_rendered_subset_by_measurement():
    """Retrieved memory (including Sleep-derived items reaching the next waking)
    and dialogue are multi-unit families the compositor trims one unit at a time
    on byte costs. With measurement each candidate subset is priced by the
    provider, so units the window can really carry are kept -- never above the
    byte bound, and only when the dry run would otherwise shed."""
    items = [f"Retrieved item {i}: " + "memory text about the day. " * 18 for i in range(6)]
    units = list(reversed(items))          # worst-first, as retrieval renders them

    def render(us):
        return "\n".join(reversed(us))

    def build():
        hard = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, "h" * 2700, hard=True)
        soft = context_budget.Contribution(
            context_budget.RETRIEVED_HISTORY, render(units), hard=False,
            droppable_units=list(units), render_fn=render, source_ids=[f"id{i}" for i in range(6)],
        )
        return hard, soft

    hard, soft = build()
    baseline = context_budget.compose_within_budget([hard, soft], 3200)
    shed = baseline.included_kind(context_budget.RETRIEVED_HISTORY)
    assert baseline.fits and (shed is None or len(shed.droppable_units) < 6)

    hard, soft = build()
    measure = lambda text: -(-len(text.encode()) // 4)          # a tokenizer at ~4 bytes/token
    assert context_budget.would_shed([hard, soft], 3200)
    context_budget.recost_by_measurement([hard, soft], measure)
    measured = context_budget.compose_within_budget([hard, soft], 3200)
    kept = measured.included_kind(context_budget.RETRIEVED_HISTORY)
    assert measured.fits and len(kept.droppable_units) == 6
    assert kept.source_ids == [f"id{i}" for i in range(6)]      # provenance follows the units exactly
    assert measured.final_prompt_cost <= 3200
