"""Regression: Clark's own explicit Sleep choice becomes an actionable owner-gate request through a
dependable typed channel -- without authorizing or running Sleep.

Live failures (2026-09-20): (1) H 01M30B8YSF75K2GCSG3Y9SYM5D, Sleep offered as an option, Clark said "I
think I will choose to enter Sleep now" and no request existed; (2) H 01M30ERQ7RJ8KPPWAKXQENS9KB, Alex
asked outright for the request, real Pass 1 chose use_workspace (where no Sleep action can coexist and the
reply path had no Sleep channel) and Clark's reply claimed a request that never existed. Every Pass-1 action
is chosen before the reply, and a literal token appended to prose was not dependable. After the reply
commits, Clark now makes one grammar-constrained typed choice (none | request_sleep) -- on the ordinary and
the workspace path alike. These tests drive the real ordinary waking pass and the real supervised workspace
pass with the real canonical writer and the real SLP2 request machinery on a synthetic database; only the
model's outputs are scripted. Nothing is read out of prose.
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

import sleep_owner_control as control
import sleep_timing_knock as timing
import slp2_schema_migration
import hir1_schema_migration
from hir1_registration import register_canonical_human
from test_wsp1_production_hard_floor import _build

OFFER = "You can choose to enter Sleep if that is what you actually want. The choice is yours."
ASK = "I need you to request Sleep so I can verify that your request reaches me properly."


def _is_decision(format):
    return isinstance(format, dict) and "sleep_timing_request" in format.get("properties", {}) \
        and "act" not in format.get("properties", {})


# ------------------------------------------------------- real waking seam --


def _typed_choice(value, messages):
    """Scripted typed choice. "request_sleep" is a GROUNDED request: Clark quotes the words of whichever author
    actually used the word Sleep (as the real model does); "ungrounded" requests with nothing quoted."""
    body = messages[-1]["content"]
    alex, _, reply = body.partition("\n\nYour reply:\n")
    alex = alex.replace("Alex's message:\n", "", 1)
    alex_quote = "Sleep" if "sleep" in alex.lower() else ""
    my_quote = "Sleep" if not alex_quote and "sleep" in reply.lower() else ""
    if value == "request_sleep":
        return json.dumps({"alexs_words_asking_me_to_request_sleep": alex_quote,
                           "my_words_asking_alex_for_sleep": my_quote, "sleep_timing_request": value})
    return json.dumps({"alexs_words_asking_me_to_request_sleep": "", "my_words_asking_alex_for_sleep": "",
                       "sleep_timing_request": "request_sleep" if value == "ungrounded" else "none"})


def _sleep_env(monkeypatch, tmp_path, decide=lambda n, messages: "none", reply="A reply."):
    """Real ordinary waking pass + real canonical writer; SLP2 tables and a registered human owner present.
    ``decide(n, messages)`` is Clark's typed Sleep choice (a string, or raises); ``reply`` his Pass-2 prose."""
    h = _build(monkeypatch, tmp_path, real_writer=True)
    slp2_schema_migration.apply_additive_migration(h.db_path)
    hir1_schema_migration.apply_additive_migration(h.db_path)
    conn = sqlite3.connect(h.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    _owner, failure = register_canonical_human(conn, {
        "registration_request_id": "sleep-reply-owner", "aab_actor_id": "actor-sleep-reply-owner",
        "display_label": "Owner", "source": "local_operator_provisioning",
    })
    conn.close()
    assert failure is None, failure
    h.owner = "actor-sleep-reply-owner"
    h.decisions = []
    h.pass1 = {"act": "develop_current", "thread": "sleep", "direction_request": "none", "relinquish_direction": False}
    h.reply = reply
    h.ordinary = True
    inner = h.la.ollama.chat

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        if _is_decision(format):
            h.decisions.append([dict(m) for m in messages])
            h.decision_options = options
            h.turn_events_at_decision = _event_types(h)
            value = decide(len(h.decisions), messages)
            return {"message": {"content": value if value.startswith("{") else _typed_choice(value, messages)},
                    "prompt_eval_count": 200, "done": True, "done_reason": "stop", "eval_count": 5}
        if h.ordinary and not (options and options.get("num_predict") == 1):
            if isinstance(format, dict) and "act" in format.get("properties", {}):
                return {"message": {"content": json.dumps(h.pass1)}, "prompt_eval_count": 300,
                        "done": True, "done_reason": "stop", "eval_count": 20}
            if format != "json":
                # Ordinary Pass 2 is a plain chat completion (shared expression seam):
                # the reply text itself, no container, no marker.
                return {"message": {"content": h.reply}, "prompt_eval_count": 300,
                        "done": True, "done_reason": "stop", "eval_count": 20}
        return inner(model=model, messages=messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat
    return h


def _event_types(h):
    conn = sqlite3.connect(f"file:{h.db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT event_type FROM events ORDER BY rowid")]
    finally:
        conn.close()


def _requests(h):
    return control.list_requests(h.la.PROVENANCE_DB_DIR)


def _speak(h, text=OFFER):
    return h.la.run_waking_turn(h.la.AnaxiOrchestrator(), text, interaction_mode="conversation")


def test_typed_choice_becomes_one_pending_request_after_the_reply_committed(monkeypatch, tmp_path):
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "request_sleep", reply="I will ask.")
    result = _speak(h, ASK)
    assert result["reply"] == "I will ask."
    assert result["sleep_timing_action_result"]["status"] == "recorded"
    assert "waking_turn" in h.turn_events_at_decision                     # the reply was already canonical
    receipts = _requests(h)
    assert len(receipts) == 1 and receipts[0]["state"] == timing.STATE_PENDING   # asked, not authorized, not run
    assert receipts[0]["request_id"] == result["sleep_timing_action_result"]["request_id"]
    assert receipts[0]["execution_attempts"] == []
    human_event = timing.fetch_sleep_request(h.la.PROVENANCE_DB_DIR, receipts[0]["request_id"])
    conn = sqlite3.connect(f"file:{h.db_path}?mode=ro", uri=True)
    try:   # exact provenance: the asking H when the turn has one (this bare harness records none), else none
        asking = conn.execute("SELECT event_id FROM events WHERE event_type='human_waking_input'").fetchone()
    finally:
        conn.close()
    assert human_event["triggering_human_event_id"] == (asking[0] if asking else None)
    body = "\n".join(m["content"] for m in h.decisions[0])
    assert ASK in body and "I will ask." in body                           # Clark decides seeing both
    assert len(h.decisions) == 1


@pytest.mark.parametrize("reply", [
    "That feels like a lovely place to let things rest and settle. Quiet is good.",
    "I think I will choose to enter Sleep now.",              # words alone never create a request
    "You are heading to bed; I will stay here quietly.",
])
def test_ordinary_language_creates_no_request_only_the_typed_choice_can(monkeypatch, tmp_path, reply):
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "none", reply=reply)
    result = _speak(h)
    assert result["sleep_timing_action_result"]["status"] == "not_applicable"
    assert _requests(h) == []


@pytest.mark.parametrize("decide", [
    lambda n, m: (_ for _ in ()).throw(RuntimeError("model down")),
    lambda n, m: "{not json",
    lambda n, m: json.dumps({"alexs_words_asking_me_to_request_sleep": "Sleep", "my_words_asking_alex_for_sleep": "",
                             "sleep_timing_request": "authorize_sleep"}),
    lambda n, m: json.dumps({"sleep_timing_request": "request_sleep"}),                  # evidence fields missing
    lambda n, m: "ungrounded",                                    # a request quoting nothing from either author
    lambda n, m: json.dumps({"alexs_words_asking_me_to_request_sleep": "a lovely place to rest",   # not the words
                             "my_words_asking_alex_for_sleep": "", "sleep_timing_request": "request_sleep"}),
    lambda n, m: json.dumps({"alexs_words_asking_me_to_request_sleep": "", "my_words_asking_alex_for_sleep": "Sleep",
                             "sleep_timing_request": "request_sleep"}),        # quote is Alex's word, filed as Clark's
    lambda n, m: json.dumps({"other": 1}),
])
def test_a_failed_or_malformed_choice_records_nothing_says_so_and_never_fails_the_turn(monkeypatch, tmp_path, decide):
    h = _sleep_env(monkeypatch, tmp_path, decide=decide, reply="Committed reply.")
    result = _speak(h)
    assert result["reply"] == "Committed reply."
    assert result["sleep_timing_action_result"]["status"] == "choice_unavailable"
    assert "waking_turn" in _event_types(h) and _requests(h) == []


def test_an_over_long_exchange_skips_the_choice_truthfully(monkeypatch, tmp_path):
    h = _sleep_env(monkeypatch, tmp_path, reply="x" * 9000)
    result = _speak(h)
    assert result["sleep_timing_action_result"]["status"] == "choice_unavailable"
    assert h.decisions == [] and _requests(h) == []


def test_a_full_size_exchange_is_not_skipped_and_uses_the_shared_context_window(monkeypatch, tmp_path):
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "request_sleep", reply="word " * 700)   # ~3.5 KB reply
    result = _speak(h, ASK + " " + "y" * 600)
    assert result["sleep_timing_action_result"]["status"] == "recorded"
    assert h.decision_options["num_ctx"] == 4096 and h.decision_options["num_predict"] >= 64   # no model reload


def test_no_second_choice_is_asked_while_a_request_is_unresolved_and_replay_never_duplicates(monkeypatch, tmp_path):
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "request_sleep")
    _speak(h, ASK)
    second = _speak(h, "Again?")
    assert second["sleep_timing_action_result"]["status"] == "not_applicable"
    assert len(h.decisions) == 1 and len(_requests(h)) == 1


def test_pass1_request_sleep_is_not_asked_a_second_time(monkeypatch, tmp_path):
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "request_sleep")
    h.pass1 = dict(h.pass1, sleep_timing_request="request_sleep")
    _speak(h)
    assert h.decisions == [] and len(_requests(h)) == 1


def test_a_different_pass1_sleep_choice_is_never_overridden(monkeypatch, tmp_path):
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "request_sleep")
    h.pass1 = dict(h.pass1, sleep_timing_request="withdraw_sleep_request")
    result = _speak(h)
    assert result["sleep_timing_action_result"]["action"] == "withdraw_sleep_request"
    assert h.decisions == [] and _requests(h) == []


def test_a_workspace_turn_now_has_the_same_typed_channel(monkeypatch, tmp_path):
    """The live failure: Pass 1 chose use_workspace for 'request Sleep'. That path had no Sleep channel."""
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "request_sleep")
    h.ordinary = False                                   # _build's own scripted use_workspace turn
    result = _speak(h, ASK)
    assert result["boundary_result"]["result"]["content"] == h.source_text     # the workspace path really ran
    assert result["sleep_timing_action_result"]["status"] == "recorded"
    receipts = _requests(h)
    assert len(receipts) == 1 and receipts[0]["state"] == timing.STATE_PENDING


def test_a_workspace_turn_without_the_choice_creates_no_request(monkeypatch, tmp_path):
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "none")
    h.ordinary = False
    result = _speak(h, "Please read chosen.txt.")
    assert result["sleep_timing_action_result"]["status"] == "not_applicable"
    assert _requests(h) == []


def test_the_typed_choice_is_a_closed_enum_and_states_it_only_asks(monkeypatch, tmp_path):
    la = _sleep_env(monkeypatch, tmp_path).la
    schema = la.SLEEP_DECISION_SCHEMA
    assert schema["properties"]["sleep_timing_request"]["enum"] == ["none", "request_sleep"]
    assert schema["required"] == list(schema["properties"]) and schema["additionalProperties"] is False
    assert list(schema["properties"])[-1] == "sleep_timing_request"             # the evidence fields are filled first
    text = la.SLEEP_DECISION_TASK_TEXT
    for fragment in ("It only asks", "Alex decides", "Choosing none is always fine", "exact words of ALEX",
                     "exact words of YOUR reply", "idiom or figure of speech", "otherwise none"):
        assert fragment in text


# ---------------------- live false positive 2026-09-20 (X 01M30JS42FQKCXH2DE3R37H6TC): thematic adjacency --

LIVE_LIBRARY_ASK = ("Please try the library checkout once more: use your ordinary library access to browse what's "
                    "available, choose something yourself, and open/read enough of it to tell me what you received.")
LIVE_LIBRARY_REPLY = ("Reading the library document, ANAXI Capabilities and Pathways, felt like reading the preamble "
                      "to a very large instruction manual for my own existence. It validated the very act of pausing "
                      "consideration. If I had to summarize the feeling, it would be one of profound, quiet "
                      "intellectual inventory.")


@pytest.mark.parametrize("ordinary", [True, False], ids=["ordinary", "workspace"])
def test_the_live_false_positive_shape_never_reaches_the_model_or_the_writer(monkeypatch, tmp_path, ordinary):
    """Even a choice that WOULD say request_sleep cannot be asked: with no Sleep word in the exchange the
    necessary condition is unmet, so no model call is made and nothing is recorded."""
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "request_sleep", reply=LIVE_LIBRARY_REPLY)
    h.ordinary = ordinary
    result = _speak(h, LIVE_LIBRARY_ASK)
    assert result["sleep_timing_action_result"]["status"] == "not_applicable"
    assert h.decisions == [] and _requests(h) == []


@pytest.mark.parametrize("ordinary", [True, False], ids=["ordinary", "workspace"])
@pytest.mark.parametrize("reply", [
    "Sleep well, and have a restful night.",
    "The document describes Sleep as one of the things ANAXI can do. I found the framing of choice reassuring.",
    "Sleep on it and tell me tomorrow; closing this chapter feels right.",
])
def test_sleep_words_alone_do_not_create_a_request_when_the_typed_choice_is_none(monkeypatch, tmp_path, ordinary, reply):
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "none", reply=reply)
    h.ordinary = ordinary
    result = _speak(h, "Take care, I'm heading to bed.")
    assert result["sleep_timing_action_result"]["status"] == "not_applicable"
    assert _requests(h) == []


def test_an_independent_request_grounded_in_clarks_own_reply_is_recorded(monkeypatch, tmp_path):
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "request_sleep",
                   reply="I want to enter Sleep. Alex, would you authorize one cycle for me?")
    result = _speak(h, "Anything on your mind?")
    assert result["sleep_timing_action_result"]["status"] == "recorded"
    assert len(_requests(h)) == 1


# --------------------------------------------- the owner gate stays intact --


def _cycle(calls, status="completed"):
    return lambda: calls.append(1) or SimpleNamespace(status=status)


def _pending_from_clark(monkeypatch, tmp_path):
    h = _sleep_env(monkeypatch, tmp_path, decide=lambda n, m: "request_sleep", reply="I choose Sleep.")
    _speak(h)
    return h, _requests(h)[0]["request_id"]


def test_authorize_targets_the_exact_request_and_alone_runs_no_cycle(monkeypatch, tmp_path):
    h, request_id = _pending_from_clark(monkeypatch, tmp_path)
    dd = h.la.PROVENANCE_DB_DIR
    receipt = control.apply_owner_action(dd, request_id=request_id, owner_actor_id=h.owner, action="AUTHORIZE")
    assert receipt["request_id"] == request_id and receipt["state"] == timing.STATE_AUTHORIZED
    assert receipt["execution_attempts"] == []


def test_defer_keeps_the_request_actionable_and_decline_resolves_it_without_sleep(monkeypatch, tmp_path):
    h, request_id = _pending_from_clark(monkeypatch, tmp_path)
    dd = h.la.PROVENANCE_DB_DIR
    assert control.apply_owner_action(dd, request_id=request_id, owner_actor_id=h.owner,
                                      action="DEFER")["state"] == timing.STATE_DEFERRED
    assert [r["request_id"] for r in _requests(h)] == [request_id]           # not erased, not authorized
    assert control.apply_owner_action(dd, request_id=request_id, owner_actor_id=h.owner,
                                      action="DECLINE")["state"] == timing.STATE_DECLINED
    assert _requests(h) == []
    calls = []
    with pytest.raises(timing.RequestRejected):
        control.execute_authorized_request(
            dd, request_id=request_id, owner_actor_id=h.owner, confirmation=True,
            background_activity_running=False, run_sleep_cycle=_cycle(calls))
    assert calls == []


def test_only_the_explicit_confirmation_runs_exactly_one_cycle(monkeypatch, tmp_path):
    h, request_id = _pending_from_clark(monkeypatch, tmp_path)
    dd = h.la.PROVENANCE_DB_DIR
    calls = []
    with pytest.raises(timing.RequestRejected):                                # never authorized: cannot run
        control.execute_authorized_request(dd, request_id=request_id, owner_actor_id=h.owner, confirmation=True,
                                           background_activity_running=False, run_sleep_cycle=_cycle(calls))
    control.apply_owner_action(dd, request_id=request_id, owner_actor_id=h.owner, action="AUTHORIZE")
    with pytest.raises(timing.RequestRejected):                                # authorized, unconfirmed
        control.execute_authorized_request(dd, request_id=request_id, owner_actor_id=h.owner, confirmation=False,
                                           background_activity_running=False, run_sleep_cycle=_cycle(calls))
    assert calls == []
    outcome = control.execute_authorized_request(
        dd, request_id=request_id, owner_actor_id=h.owner, confirmation=True,
        background_activity_running=False, run_sleep_cycle=_cycle(calls))
    assert calls == [1] and outcome["outcome"] == "succeeded"
    # Accepted law (SLP2-CORRECTION-1): authorization is not consumed by an attempt, but a further
    # attempt is never automatic -- it needs its own explicit confirmation, one cycle per invocation.
    with pytest.raises(timing.RequestRejected):
        control.execute_authorized_request(dd, request_id=request_id, owner_actor_id=h.owner, confirmation=False,
                                           background_activity_running=False, run_sleep_cycle=_cycle(calls))
    assert calls == [1]
    assert len(timing.fetch_sleep_request(dd, request_id)["execution_attempts"]) == 1


def test_a_failed_cycle_is_recorded_truthfully_and_is_not_a_success(monkeypatch, tmp_path):
    h, request_id = _pending_from_clark(monkeypatch, tmp_path)
    dd = h.la.PROVENANCE_DB_DIR
    control.apply_owner_action(dd, request_id=request_id, owner_actor_id=h.owner, action="AUTHORIZE")
    outcome = control.execute_authorized_request(
        dd, request_id=request_id, owner_actor_id=h.owner, confirmation=True,
        background_activity_running=False, run_sleep_cycle=_cycle([], status="failed"))
    assert outcome["outcome"] != "succeeded"
    assert timing.fetch_sleep_request(dd, request_id)["execution_attempts"][-1]["outcome"] == outcome["outcome"]
