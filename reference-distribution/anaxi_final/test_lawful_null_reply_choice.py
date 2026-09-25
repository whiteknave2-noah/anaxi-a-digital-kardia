"""LAWFUL NULL: Clark's typed choice to say nothing in reply is an ANSWERED, completed turn.

Owner law protected (SILENCE / NULL / FAILURE LAW): a mechanical state must never impersonate an
authored choice, and an authored choice must never be reclassified as mechanical failure merely
because it produces no outward text.  Silence is Clark's only when it is his completed,
provenance-classified typed choice.

Shared cross-law scenario `subject_selected_null_after_valid_H` (H valid; provenance
subject-selected; expression null; no outward prose; turn completed) is evaluated below by EVERY
subsystem it touches -- canonical writer, GUI projection/display, dialogue continuity, WTR
eligibility, WTR recovery success, temporal grounding, Caret owner status -- and each must say
COMPLETED/ANSWERED.  Its countermodel `blank_completion_after_valid_H` (the host never obtained a
reply) must be classified the opposite way by the same subsystems: no X, unanswered, recovery
eligible, never "Clark chose not to reply".
"""
import json
import os
import sqlite3
import time

import pytest

import caret_owner_status as cos
import conversation_direction as cd
import conversation_display
import conversation_projection as cp
import native_provenance_writer as npw
import wtr0_schema_migration
import wtr0_waking_recovery as recovery
import waking_failure_evidence as wfe
from test_web_waking_seam import Seam
from test_caret_inbound_seam import _remote
from test_caret_wake_service import _setup as _caret_setup, _service as _caret_service, _serve as _caret_serve
from wtr0_cold_reset import ColdResetOutcome

BASE_ACT = {"act": "develop_current", "thread": "", "direction_request": "none", "relinquish_direction": False}


def _q(s, sql, *args):
    conn = sqlite3.connect(f"file:{s.h.db_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------------ Pass-1 vocabulary

def test_no_reply_is_an_optional_typed_choice_and_absence_is_an_ordinary_reply():
    ok, failure = cd.validate_pass1_conversation_act(dict(BASE_ACT, reply_request="no_reply"))
    assert failure is None and ok["reply_request"] == "no_reply"
    ok, failure = cd.validate_pass1_conversation_act(dict(BASE_ACT))
    assert failure is None and ok["reply_request"] == "none"
    assert cd.validate_pass1_conversation_act(dict(BASE_ACT, reply_request="silence"))[1] == \
        cd.DirectionFailure.INVALID_REPLY_REQUEST
    assert cd.PASS1_SCHEMA["properties"]["reply_request"]["enum"] == ["no_reply", "none"]
    assert "reply_request" not in cd.PASS1_SCHEMA["required"]


@pytest.mark.parametrize("contradiction", [
    dict(caret_reply_request="reply_through_caret"),
])
def test_no_reply_contradicting_a_reply_delivering_choice_fails_validation(contradiction):
    raw = dict(BASE_ACT, reply_request="no_reply", **contradiction)
    assert cd.validate_pass1_conversation_act(raw)[1] == cd.DirectionFailure.INVALID_REPLY_REQUEST


def test_the_menu_offers_it_as_a_plain_option():
    _head, fixed, _tail = cd.render_pass1_action_menu_parts()
    assert "reply_request: no_reply" in fixed and "Leave it out to reply" in fixed
    assert "Silence exists only through this field" in fixed
    assert '"reply_request":"no_reply"' in fixed          # the example the real-model battery required


# ------------------------------------------------------------------ the shared scenario

def _null_turn(monkeypatch, tmp_path, **extra):
    s = Seam(monkeypatch, tmp_path)
    s.pass2_text = "UNREACHABLE-PASS2"
    result = s.turn("Goodnight. No need to answer this one.", reply_request="no_reply", **extra)
    return s, result


def test_subject_selected_null_after_valid_h_is_answered_in_every_subsystem(monkeypatch, tmp_path):
    s, result = _null_turn(monkeypatch, tmp_path)
    # the waking driver: completed, no Pass-2 call at all, nothing attributed as words
    assert result["reply"] == "" and result["reply_choice"] == "no_reply"
    assert s.pass2_prompts == []
    trace = s.h.la.get_last_conversation_direction_trace()
    assert trace["pass1_status"] == "ok" and trace["pass2_status"] == "not_composed_no_reply"
    # canonical writer: one X linked to H, no prose, Clark-authored choice, truthful participation
    (h_id,) = [r[0] for r in _q(s, "SELECT event_id FROM events WHERE event_type='human_waking_input'")]
    (x_id,) = [r[0] for r in _q(s, "SELECT event_id FROM events WHERE event_type='waking_turn'")]
    comps = _q(s, "SELECT c.component_kind, c.component_text, a.actor_type FROM event_components c "
                  "JOIN actors a ON a.actor_id=c.creator_actor_id WHERE c.event_id=?", x_id)
    kinds = {k: (t, a) for k, t, a in comps}
    assert "conversational_prose" not in kinds
    assert kinds["clark_reply_choice"] == ("no_reply", "clark_agent")
    assert kinds["human_input_event_id"][0] == h_id
    notes = [r[0] for r in _q(s, "SELECT participation_note FROM event_model_participation WHERE event_id=?", x_id)]
    assert notes == [npw.PASS1_TYPED_CHOICE_PARTICIPATION_NOTE]
    # GUI projection + display: an answered exchange, rendered as a host fact, never as Clark's words
    rows = cp.project_waking_conversation(s.h.db_path, include_event_metadata=True)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["reply_choice"] == "no_reply" and rows[1]["content"] == ""
    shown = conversation_display.display_messages(rows)[1]["content"][0]["text"]
    assert shown == conversation_display.NO_REPLY_DISPLAY_TEXT and "Host" in shown
    # WTR eligibility: an answered H is never recovery-eligible
    wtr0_schema_migration.apply_additive_migration(s.h.db_path)
    conn = sqlite3.connect(s.h.db_path)
    try:
        receipt = recovery.assess_eligibility(conn, h_id, s.h.actor_id)
    finally:
        conn.close()
    assert receipt["decision"] == "INELIGIBLE" and receipt["basis"] == "X_ALREADY_LINKED"
    # the NEXT turn, through the real seam: temporal grounding states the exchange as Clark's own
    # choice (not an unanswered input, not a "reply generated and persisted"), and dialogue
    # continuity shows exactly what happened (the words, then no reply)
    s.pass2_text = "Good morning."
    s.turn("Good morning!")
    host_facts = s.pass2_prompts[-1][0]["content"]
    assert "You chose not to reply (your own typed choice)" in host_facts
    assert "have no linked canonical Clark reply" not in host_facts
    window = s.pass2_prompts[-1][1:-1]
    assert {"role": "user", "content": "Goodnight. No need to answer this one."} in window
    idx = window.index({"role": "user", "content": "Goodnight. No need to answer this one."})
    assert window[idx + 1] == {"role": "assistant", "content": ""}


def test_countermodel_blank_completion_after_valid_h_is_the_opposite_everywhere(monkeypatch, tmp_path):
    s = Seam(monkeypatch, tmp_path)
    s.pass2_raw = "   "
    with pytest.raises(s.h.la.ConversationDirectionFailure) as caught:
        s.turn("Goodnight.")
    (h_id,) = [r[0] for r in _q(s, "SELECT event_id FROM events WHERE event_type='human_waking_input'")]
    assert _q(s, "SELECT COUNT(*) FROM events WHERE event_type='waking_turn'")[0][0] == 0
    assert _q(s, "SELECT COUNT(*) FROM event_components WHERE component_kind='clark_reply_choice'")[0][0] == 0
    # The harness reloads conversation_direction; classify with a capture module bound to the same
    # exception class (production has exactly one).
    import importlib
    import sys
    sys.modules.pop("waking_turn_failure_capture", None)
    capture = importlib.import_module("waking_turn_failure_capture")
    classification, _basis = capture._classify_waking_failure(caught.value, has_x=False)
    assert classification == "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"
    wtr0_schema_migration.apply_additive_migration(s.h.db_path)
    wfe.record_waking_failure(s.h.db_path, human_input_event_id=h_id, failure_class=classification, basis="test")
    conn = sqlite3.connect(s.h.db_path)
    try:
        assert recovery.assess_eligibility(conn, h_id, s.h.actor_id)["decision"] == "ELIGIBLE"
    finally:
        conn.close()
    rows = cp.project_waking_conversation(s.h.db_path)
    assert [r["role"] for r in rows] == ["user"] and not any("reply_choice" in r for r in rows)


def test_other_typed_choices_made_with_no_reply_still_proceed(monkeypatch, tmp_path):
    import od1_schema_migration
    s = Seam(monkeypatch, tmp_path)
    od1_schema_migration.apply_additive_migration(s.h.db_path)
    result = s.turn("Goodnight. No need to answer this one.", reply_request="no_reply",
                    operative_directive_request="set_directive", operative_directive_text="Keep replies short.")
    assert result["reply_choice"] == "no_reply"
    assert result["operative_directive_action_result"] == {"action": "set_directive", "status": "recorded"} or (
        result["operative_directive_action_result"]["action"] == "set_directive"
        and result["operative_directive_action_result"]["status"] == "recorded")
    kinds = {r[0] for r in _q(s, "SELECT component_kind FROM event_components WHERE event_id=("
                                "SELECT event_id FROM events WHERE event_type='waking_turn')")}
    assert {"clark_reply_choice", "operative_directive_request", "operative_directive_text"} <= kinds


# ------------------------------------------------------------------ writer contract

def test_the_writer_refuses_a_no_reply_turn_that_carries_prose_or_a_reply_route():
    with pytest.raises(npw.IncompleteEventBundleError):
        npw._validate_subject_reply_choice("no_reply", "some words", "none", False)
    with pytest.raises(npw.IncompleteEventBundleError):
        npw._validate_subject_reply_choice("no_reply", "", "reply_through_caret", False)
    with pytest.raises(npw.IncompleteEventBundleError):
        npw._validate_subject_reply_choice("no_reply", "", "none", True)
    with pytest.raises(npw.IncompleteEventBundleError):
        npw._validate_subject_reply_choice("maybe", "", "none", False)
    npw._validate_subject_reply_choice("no_reply", "", "send_message", False)   # a Discord send with its own words


def test_a_crashed_null_turn_is_resumed_from_staging_exactly(monkeypatch, tmp_path):
    s, _result = _null_turn(monkeypatch, tmp_path)
    (x_id,) = [r[0] for r in _q(s, "SELECT event_id FROM events WHERE event_type='waking_turn'")]
    # a resume over already-committed staging re-verifies the exact contract (choice + participation)
    results = npw.resume_orphaned_staged_turns(s.h.la.PROVENANCE_DB_DIR, s.h.la.STAGING_PATH)
    assert any(r["event_id"] == x_id for r in results)
    conn = sqlite3.connect(s.h.db_path)
    try:
        payloads = [json.loads(l) for l in open(s.h.la.STAGING_PATH, encoding="utf-8") if l.strip()]
    finally:
        conn.close()
    assert any("subject_reply_choice" in json.dumps(p) for p in payloads)


# ------------------------------------------------------------------ WTR recovery completing as null

def test_a_recovery_that_completes_with_clarks_null_choice_is_success_not_failure(tmp_path):
    from test_waking_turn_recovery import Env
    env = Env(f"null_recovery_{int(time.time() * 1000)}")
    h_id, _ = env.make_h()
    env.record_evidence(h_id, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    def reset_fn():
        return {"status": ColdResetOutcome.SUCCEEDED, "basis": "ok"}

    def generation_fn(prompt):
        result = npw.stage_and_record_native_waking_turn(
            env.tmp_root, os.path.join(env.tmp_root, "native_turn_staging.jsonl"),
            session_id="sess-null-recovery", session_started_at=int(time.time()), user_id="nate",
            prompt=prompt, bounded_clause="", clark_prose="",
            kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=env.pipeline_key,
            artifact_pass_ran=False, occurred_at=int(time.time()), human_input_event_id=h_id,
            subject_reply_choice="no_reply",
        )
        return {"native_event_id": result["event_id"]}

    outcome = recovery.execute_recovery(env.db_path, h_id, env.actor_id, reset_fn=reset_fn, generation_fn=generation_fn)
    assert outcome["terminal_state"] == "SUCCESS" and outcome["reply_choice"] == "no_reply"
    with env.conn() as conn:
        status = recovery.recovery_status(conn, h_id)
        assert "no_reply" in status["persistence_detail"]
        assert recovery.assess_eligibility(conn, h_id, env.actor_id)["basis"] == "X_ALREADY_LINKED"


# ------------------------------------------------------------------ Caret occasion

def test_a_caret_occasion_answered_with_null_is_delivered_and_reported_as_clarks_choice(monkeypatch, tmp_path):
    s = _caret_setup(monkeypatch, tmp_path)
    s.pass1_script.append({"reply_request": "no_reply"})
    svc = _caret_service(s, tmp_path)
    _caret_serve(s, _remote())
    assert svc.step()[0] == "delivered"
    assert s.pass2_prompts == [] and s.fake.posts == []
    assert svc._attempts == {}                                  # never a retry condition
    assert svc.step()[0] == "idle"                              # never woken twice
    (status,) = cos.caret_owner_status(s.data_dir, trace_path=str(tmp_path / "caret_wake_trace.jsonl"))
    assert status["code"] == cos.CLARK_CHOSE_NO_REPLY


def test_a_caret_occasion_whose_reply_was_never_obtained_is_not_reported_as_silence(monkeypatch, tmp_path):
    s = _caret_setup(monkeypatch, tmp_path)
    s.pass2_raw = "  "
    svc = _caret_service(s, tmp_path)
    _caret_serve(s, _remote())
    assert svc.step()[0] == "failed"
    (status,) = cos.caret_owner_status(s.data_dir, trace_path=str(tmp_path / "caret_wake_trace.jsonl"))
    assert status["code"] != cos.CLARK_CHOSE_NO_REPLY and status["code"] != cos.NO_OUTBOUND_REPLY_SELECTED


def test_an_already_migrated_database_gets_the_widened_directive_guard_in_place(tmp_path):
    """Production carries the pre-null OD1 trigger (CREATE TRIGGER IF NOT EXISTS never replaces it).
    The writer's own connection upgrades it idempotently; no row is touched."""
    import od1_schema_migration as od1
    from provenance_schema import create_provenance_db
    db = str(tmp_path / "anaxi_provenance.db")
    create_provenance_db(db).close()
    od1.apply_additive_migration(db)
    conn = sqlite3.connect(db)
    old_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (od1._TRANSITION_TRIGGER,)).fetchone()[0]
    legacy = old_sql.replace(
        "participation_note IN (\n              'Model backing this native waking turn''s pass-2 conversational reply.',\n"
        "              'Model backing this native waking turn''s pass-1 typed choice; no pass-2 reply was composed.')",
        "participation_note = 'Model backing this native waking turn''s pass-2 conversational reply.'")
    assert legacy != old_sql
    conn.executescript(f"DROP TRIGGER {od1._TRANSITION_TRIGGER};\n{legacy};")
    tables_before = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    assert od1.upgrade_transition_trigger_on_connection(conn) is True
    assert od1.upgrade_transition_trigger_on_connection(conn) is False          # idempotent
    new_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (od1._TRANSITION_TRIGGER,)).fetchone()[0]
    assert od1._NULL_NOTE_FRAGMENT in new_sql and "append-only" not in new_sql
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == tables_before
    conn.close()


def test_a_discord_send_with_its_own_words_proceeds_on_a_null_turn(monkeypatch, tmp_path):
    from test_caret_outbound_seam import _setup as _out_setup, _send
    s = _out_setup(monkeypatch, tmp_path)
    result = s.turn("Please send Blair a note; you don't need to say anything to me.",
                    reply_request="no_reply", **_send(s))
    assert result["reply_choice"] == "no_reply" and s.pass2_prompts == []
    assert len(s.fake.posts) == 1
    assert json.loads(s.fake.posts[0][2]) == {"content": "Hello Alex, this one is mine."}


def test_a_web_lookup_chosen_on_a_null_turn_is_recorded_once_and_reaches_the_next_turn(monkeypatch, tmp_path):
    from test_web_waking_seam import TOPIC, _external, _search
    s = Seam(monkeypatch, tmp_path)
    s.turn("Look that up; no need to reply now.", reply_request="no_reply", **_search())
    assert s.pass2_prompts == [] and s.searches == [TOPIC]           # performed after commit, exactly once
    assert len(s.query_rows()) == 1
    s.turn("What did you find?")
    (env,) = _external(s.pass2_prompts[-1])                           # carried into the next composed reply
    assert env["operation"] == "web_search" and s.searches == [TOPIC]


def test_a_web_lookup_and_a_discord_send_in_one_turn_both_persist(monkeypatch, tmp_path):
    """Regression for the writer's component-sequence collision found while binding the null law:
    the external-info target never advanced the sequence, so a same-turn Discord send collided."""
    from test_caret_outbound_seam import _setup as _out_setup, _send
    from test_web_waking_seam import _search
    s = _out_setup(monkeypatch, tmp_path)
    s.turn("Look it up and send Blair a note.", **_search(), **_send(s))
    kinds = [r[0] for r in _q(s, "SELECT component_kind FROM event_components WHERE event_id=("
                                 "SELECT event_id FROM events WHERE event_type='waking_turn')")]
    assert "external_info_target" in kinds and "discord_message_text" in kinds
    assert len(s.fake.posts) == 1
