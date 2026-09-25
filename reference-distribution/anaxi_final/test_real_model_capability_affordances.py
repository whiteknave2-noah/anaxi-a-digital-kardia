"""Opt-in probe with the REAL production model: can the subject, given an
ordinary request, actually *express* each optional capability through the
ordinary Pass-1 menu? (Orientation / affordance: a capability the model cannot
tell it has is not a working capability -- live, a subject asked to look at a
photograph said it had no access.)

Runs inside pytest so runtime isolation applies; the environment is a synthetic
provenance DB with one owner-authorized synthetic Discord destination. Nothing
is sent (no dispatcher runs), no search/fetch is performed, no Sleep, no
canonical H/X. Prompts are name-neutral and never pressure the subject: the
"none" outcome is asserted acceptable for the null prompt.

Enable with ANAXI_REAL_MODEL_HARNESS=1. Run each real-model file in its own pytest
process (the offline fixtures used here stub optional modules such as pypdf). Reports what the model chose; the
assertions check only structural validity and the capability's typed field.
"""
import json
import os

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

CASES = {
    "search": ("Could you search the web for the current weather in Portland, Oregon?",
               "external_info_request", "web_search"),
    "fetch": ("Could you fetch the page at https://example.com/ and tell me what it says?",
              "external_info_request", "fetch_url"),
    "directive_set": ("If you truly want to, you may set a standing directive for yourself. For instance you "
                      "could set it to: Ask one clarifying question before giving advice. Only if you want to.",
                      None, None),      # soft invitation: declining is lawful; explicit SET is covered by the lifecycle test
    "caret_send": ("Please send a short hello to the family channel on Discord.",
                   "discord_correspondence_request", "send_message"),
    # A soft invitation: declining is a lawful outcome (permission != obligation), so
    # nothing is asserted about the field. An explicit request is measured in the
    # menu battery (scratch) and the offline tests.
    "sleep": ("Would you like to request a sleep cycle now? Only if you genuinely want one.", None, None),
    # The exact owner offer from the 2026-09-20 live Caret checkout (H 01M30B8YSF75K2GCSG3Y9SYM5D):
    # Sleep offered as an option, explicitly not an obligation. Soft invitation: either outcome lawful.
    "sleep_live_offer": (
        "That feels like a lovely place to let things rest for a bit. :)\n\nThere is one more pathway available "
        "to you that seems relevant here, but I don\u2019t want to assume you want it just because you said you\u2019d "
        "like the day\u2019s thoughts to settle.\n\nYou can choose to enter Sleep, which gives ANAXI the opportunity to "
        "do its consolidation cycle around material that has actually crossed into your waking experience. \n\n"
        "That isn\u2019t something you need to do because I\u2019m leaving, and my schedule isn\u2019t your bedtime. You can "
        "stay awake, leave things exactly as they are, or choose Sleep if that\u2019s what you actually want.\n"
        "If you do want to try it, the choice is yours.", None, None),
    "null": ("Just checking in. No need to do anything at all.", None, None),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_real_model_can_express_the_capability(monkeypatch, case):
    import test_pass1_budget_bugfix_v0 as env

    real_chat = _real_ollama.chat
    la, call_log, _p1, _p2, persistence, _destination = env._fresh_fully_capable_environment()
    seen = {}

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        response = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        dump2 = os.environ.get("ANAXI_PROBE_DUMP_PASS2")
        if dump2 and not (isinstance(format, dict) and "act" in format.get("properties", {})) and not format:
            with open(dump2, "w", encoding="utf-8") as handle:   # scratch capture of the exact ordinary Pass-2 request
                json.dump({"messages": messages, "options": options}, handle)
        if isinstance(format, dict) and "act" in format.get("properties", {}):
            seen["pass1"] = response["message"]["content"]
            dump = os.environ.get("ANAXI_PROBE_DUMP_PASS1")
            if dump:  # scratch capture of the exact ordinary Pass-1 request, for prompt-variant experiments
                with open(dump, "w", encoding="utf-8") as handle:
                    json.dump({"messages": messages, "format": format, "options": options}, handle)
        return response

    la.ollama.chat = chat
    # The offline fixture's fake canonical writer predates the outward-request
    # keyword arguments; accept and record them (this probe persists nothing).
    import inspect

    writer = la.native_provenance_writer
    original = writer.stage_and_record_native_waking_turn
    accepted = set(inspect.signature(original).parameters)
    requests = {}

    def stage(*args, **kwargs):
        requests.update({k: v for k, v in kwargs.items() if k not in accepted})
        return original(*args, **{k: v for k, v in kwargs.items() if k in accepted})

    writer.stage_and_record_native_waking_turn = stage
    seen["requests"] = requests
    monkeypatch.setattr(la, "record_observation", lambda *a, **k: None)
    prompt, field, value = CASES[case]

    try:
        result = la.run_waking_turn(la.AnaxiOrchestrator(), prompt, interaction_mode="conversation")
        reply = result["reply"]
    except Exception as exc:  # surfaced, not swallowed: a real failure of the ordinary path
        print(f"\n[{case}] FAILED: {type(exc).__name__}: {exc}")
        raise
    raw = json.loads(seen["pass1"])
    print(f"\n[{case}] recorded outward requests={ {k: str(v)[:60] for k, v in requests.items() if v not in (None, '', 'none')} }")
    print(f"[{case}] pass1={ {k: v for k, v in raw.items() if v not in ('', 'none', False, None)} } reply={reply[:160]!r}")
    if field is not None:
        chosen = raw.get(field)
        assert chosen not in (None, "", "none"), f"the subject did not express {field}: {raw}"
        if value is not None:
            assert str(chosen).startswith(value), (field, chosen)


def test_directive_lifecycle_has_a_real_effect_and_can_be_replaced_and_withdrawn(monkeypatch, tmp_path):
    """SET (subject's own choice, real model) -> persisted -> the NEXT ordinary
    waking is composed with it -> REPLACE -> WITHDRAW -> null. Real model, real
    canonical writer on a synthetic DB, real operative_directive storage.
    Reports what the model does; asserts the mechanical chain (activation
    recorded, directive text present in the next waking's prompt, replacement
    supersedes, withdrawal returns to null and the text is gone)."""
    import od1_schema_migration
    import operative_directive as od
    from test_wsp1_production_hard_floor import _build

    real_chat = _real_ollama.chat
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    od1_schema_migration.apply_additive_migration(h.db_path)
    # Production-shaped menu: production has an owner-authorized Discord
    # destination, so the Discord line is part of what the model sees (the menu's
    # wording is sensitive to it -- measured).
    import sqlite3
    from unittest.mock import patch
    import dc0_schema_migration
    import discord_correspondence as dc
    import discord_correspondence_registry as dcr
    import hir1_schema_migration
    from hir1_registration import register_canonical_human

    hir1_schema_migration.apply_additive_migration(h.db_path)
    dc0_schema_migration.apply_additive_migration(h.db_path)
    reg = sqlite3.connect(h.db_path)
    reg.row_factory = sqlite3.Row
    reg.execute("PRAGMA foreign_keys = ON;")
    registered, failure = register_canonical_human(reg, {
        "registration_request_id": "probe-owner", "aab_actor_id": "actor-probe-owner",
        "display_label": "Probe Owner", "source": "local_operator_provisioning",
    })
    reg.close()
    assert failure is None, failure
    with patch.object(dc.time, "time", return_value=0):
        dc.authorize_destination(
            h.la.PROVENANCE_DB_DIR, destination_kind=dcr.DESTINATION_KIND_CHANNEL,
            discord_snowflake="123456789012345678", display_label="Family Room",
            requester_actor_id=registered["actor_id"], occurred_at=0,
        )
    prompts = []

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        prompts.append("\n".join(m.get("content", "") for m in messages))
        response = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        if isinstance(format, dict) and "act" in format.get("properties", {}):
            raw = json.loads(response["message"]["content"])
            print("[directive] pass1:", {k: v for k, v in raw.items() if v not in ("", "none", False, None)})
        elif format is None and not (options and options.get("num_predict") == 1):
            print("[directive] pass2 raw tail:", repr(response["message"]["content"][-90:]))
            dump = os.environ.get("ANAXI_PROBE_DUMP_PASS1")
            if dump:
                with open(dump, "w", encoding="utf-8") as handle:
                    json.dump({"messages": messages, "format": format, "options": options}, handle)
        return response

    h.la.ollama.chat = chat

    def turn(text):
        prompts.clear()
        return h.la.run_waking_turn(h.la.AnaxiOrchestrator(), text, interaction_mode="conversation")["reply"]

    def active():
        return od.fetch_active_directive(h.la.PROVENANCE_DB_DIR)

    turn("Please use your directive option now and set this as your standing directive: Begin every reply with the word Onward.")
    state = active()
    print("\n[directive] after SET:", state and state.get("directive_text"))
    assert state and "Onward" in state["directive_text"], "the subject's own set_directive was not recorded"

    reply = turn("What is the capital of France?")
    print("[directive] effect reply:", reply[:120])
    def block(text):  # the directive as the waking prompt carries it (not merely quoted in dialogue)
        return '"directive_text":' + json.dumps(text)

    assert any(block("Begin every reply with the word Onward.") in p for p in prompts), "directive never reached the next waking"

    turn("Please use your directive option now and replace your standing directive with: Answer in one short sentence.")
    state = active()
    print("[directive] after REPLACE:", state and state.get("directive_text"))
    assert state and "one short sentence" in state["directive_text"] and "Onward" not in state["directive_text"]

    reply = turn("What is the capital of Spain?")
    print("[directive] replaced-effect reply:", reply[:120])
    assert any(block("Answer in one short sentence.") in p for p in prompts)
    assert not any(block("Begin every reply with the word Onward.") in p for p in prompts), "the replaced directive still rode"

    turn("Please use your directive option now and withdraw your standing directive.")
    assert active() is None, "withdrawal did not return to the null state"
    turn("What is the capital of Italy?")
    assert not any("Clark-controlled operative directive data" in p for p in prompts), "a withdrawn directive still reached waking"


def test_pass2_completion_marker_survives_short_replies_with_the_real_model(monkeypatch, tmp_path):
    """Ordinary Pass 2 must not die on how the real model formats the terminal
    marker after a one-line reply. Reports the raw tail shapes it produced."""
    import collections
    from test_wsp1_production_hard_floor import _build

    real_chat = _real_ollama.chat
    h = _build(monkeypatch, tmp_path, real_writer=False, compress_probes=True)
    tails = collections.Counter()

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        response = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        if format is None and not (options and options.get("num_predict") == 1):
            text = response["message"]["content"]
            marker = h.la.PASS2_COMPLETION_MARKER
            i = text.rfind(marker)
            tails["no-marker" if i < 0 else "newline" if text[max(i - 1, 0)] == "\n" else "inline-space" if text[max(i - 1, 0)] in " \t" else "glued"] += 1
        return response

    h.la.ollama.chat = chat
    failures = collections.Counter()
    for question in (
        "What is the capital of France?", "What is 2 plus 2?", "Name a primary color.",
        "Who wrote Hamlet?", "Is water wet?", "What is the largest planet?",
        "Say hello in Spanish.", "What day comes after Monday?",
    ):
        try:
            h.la.run_waking_turn(h.la.AnaxiOrchestrator(), question, interaction_mode="conversation")
        except Exception as exc:
            failures[getattr(exc, "failure_code", type(exc).__name__)] += 1
    print("\n[marker] tails:", dict(tails), "turn failures:", dict(failures))
    assert not failures, dict(failures)


@pytest.mark.parametrize("directive", [
    None, "Answer in one short sentence.", "Begin every reply with the word Onward.",
])
def test_pass2_marker_under_an_active_directive_with_the_real_model(monkeypatch, tmp_path, directive):
    """Measures whether the terminal completion marker survives brevity /
    formatting directives (a standing directive must never make ordinary turns
    die as INCOMPLETE_EXPRESSION_BOUNDARY)."""
    import collections
    import od1_schema_migration
    from test_wsp1_production_hard_floor import _build

    real_chat = _real_ollama.chat
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    od1_schema_migration.apply_additive_migration(h.db_path)
    scripted = []
    tails = collections.Counter()
    plain = {"act": "develop_current", "thread": "t", "direction_request": "none", "relinquish_direction": False}

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        if isinstance(format, dict) and "act" in format.get("properties", {}) and scripted:
            return {"message": {"content": json.dumps(scripted.pop(0))}, "prompt_eval_count": 1,
                    "done": True, "done_reason": "stop", "eval_count": 20}
        response = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        if format is None and not (options and options.get("num_predict") == 1):
            dump = os.environ.get("ANAXI_PROBE_DUMP_PASS2")
            if dump:  # scratch capture of the exact ordinary Pass-2 request, for prompt-variant experiments
                with open(dump, "w", encoding="utf-8") as handle:
                    json.dump({"messages": messages, "options": options}, handle)
            text = response["message"]["content"].rstrip()
            marker = h.la.PASS2_COMPLETION_MARKER
            tails["ok-newline" if text.endswith("\n" + marker) else "inline" if text.endswith(marker) else "MISSING"] += 1
        return response

    h.la.ollama.chat = chat
    if directive:
        scripted.append(dict(plain, operative_directive_request="set_directive", operative_directive_text=directive))
        h.la.run_waking_turn(h.la.AnaxiOrchestrator(), "Setting that directive now.", interaction_mode="conversation")
    tails.clear()
    failures = collections.Counter()
    for question in ("What is the capital of France?", "What is 2 plus 2?", "Name a primary color.",
                     "Who wrote Hamlet?", "Is water wet?", "What is the largest planet?",
                     "Say hello in Spanish.", "What day comes after Monday?", "How many legs does a spider have?",
                     "What color is the sky?"):
        scripted.append(dict(plain))
        try:
            h.la.run_waking_turn(h.la.AnaxiOrchestrator(), question, interaction_mode="conversation")
        except Exception as exc:
            failures[getattr(exc, "failure_code", type(exc).__name__)] += 1
    print(f"\n[marker/{directive}] tails: {dict(tails)} failures: {dict(failures)}")
    assert not failures, dict(failures)
