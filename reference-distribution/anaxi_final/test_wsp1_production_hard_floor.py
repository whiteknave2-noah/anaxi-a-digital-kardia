"""Regression: a production-shaped ordinary waking turn that selects
``use_workspace`` must reach and complete the supervised workspace action.

Live incident (2026-09-18 ~17:28 PDT, H 01M2VH12GHV3R2WJKMJQ9CASAC): the
ordinary Pass 1 chose ``use_workspace``, then WSP1 Pass 1 failed closed with
``BUDGET_EXCEEDED`` -- hard floor 3237 against a 2944 budget -- before any
workspace action. WSP1 costed the whole 1799-byte system message on the byte
upper-bound estimator, while ordinary waking costs its fixed conversation
framing on the calibrated path. The unit-sized fixtures used elsewhere never
came near the floor. These tests use the production's measured sizes.
"""
import importlib
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

import pytest
import PIL.Image  # real PIL must be in sys.modules before any harness stubs an absent one

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
if ANAXI_FINAL not in sys.path:
    sys.path.insert(0, ANAXI_FINAL)

# Measured from the live failure's ui_turn_diagnostics record.
PRODUCTION_BASE_SYSTEM_BYTES = 1799   # core_system_control cost in wsp1_pass1
PRODUCTION_HUMAN_BYTES = 456          # incoming message of the failed turn
PRODUCTION_WSP1_PASS1_BUDGET = 2944

REQUEST_STEM = (
    "Please try the library checkout once more: use your ordinary library access to "
    "browse what is available, choose something yourself, and open and read enough of "
    "it to tell me what you actually received. If the pathway fails, something is "
    "missing, or you only receive metadata rather than the material itself, tell me "
    "plainly. "
)


def _message(length):
    text = (REQUEST_STEM * (length // len(REQUEST_STEM) + 1))[:length]
    assert len(text.encode("utf-8")) == length
    return text


def _production_sized_core(style):
    """A synthetic identity preamble padded so the conversation-mode system
    message is exactly the production's byte size (no production Kardia read)."""
    import interaction_mode as im
    import test_owc5_s2_integration as fixture

    core, _style = fixture._calibration_identity_preamble_and_style()

    def base_len(candidate):
        messages = im.apply_conversation_aesthetic(
            [{"role": "system", "content": candidate}], "conversation", style,
        )
        messages = im.apply_interaction_mode(messages, "conversation")
        return len(messages[0]["content"].encode("utf-8"))

    padding = PRODUCTION_BASE_SYSTEM_BYTES - base_len(core) - 1
    assert padding > 0
    core = core + "\n" + ("Stay attentive to the person. " * 100)[:padding]
    assert base_len(core) == PRODUCTION_BASE_SYSTEM_BYTES
    return core


class _Harness:
    pass


def _build(monkeypatch, tmp_path, *, real_writer, compress_probes=False, source_text=None, action=None):
    import test_owc5_s2_integration as fixture
    import test_workspace_waking_affordance as affordance

    _core, style = fixture._calibration_identity_preamble_and_style()
    h = _Harness()
    h.la, h.calls, _p1, _p2, h.persistence = fixture.fresh_llama_anaxi(
        pass1_value=None, pass2_value=None,
        core_system_text=_production_sized_core(style), style_instruction=style,
    )
    if real_writer:
        # fresh_llama_anaxi installs an in-memory writer; use the real one.
        sys.modules.pop("native_provenance_writer", None)
        h.writer = importlib.import_module("native_provenance_writer")
        h.la.native_provenance_writer = h.writer
    affordance._install_optional_workspace_stubs()
    import workspace_capability as wc
    import workspace_episode_provenance
    import workspace_supervisor

    h.wc, h.supervisor = wc, workspace_supervisor
    workspace_supervisor.la = h.la
    workspace_supervisor.native_provenance_writer = (
        h.writer if real_writer else sys.modules["native_provenance_writer"]
    )
    h.db_path = os.path.join(h.la.PROVENANCE_DB_DIR, "anaxi_provenance.db")
    if real_writer:
        from migrate_historical_data import build_pipeline_map, seed_reference_data
        from provenance_schema import create_provenance_db

        create_provenance_db(h.db_path).close()
        conn = sqlite3.connect(h.db_path)
        manifest = {"pipelines": {k: {"routing_constant_value": "synthetic_user"} for k in ("llama", "claude")}}
        seed_reference_data(conn, build_pipeline_map(manifest), 1000)
        conn.close()

    h.paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    h.paths.ensure_exists()
    h.source_text = source_text or "Substantive library material that must reach the model."
    Path(h.paths.library_dir, "chosen.txt").write_text(h.source_text, encoding="utf-8")
    monkeypatch.setattr(wc.WorkspacePaths, "production_defaults", classmethod(lambda cls: h.paths))
    monkeypatch.setattr(h.la, "record_observation", lambda *a, **k: None)
    monkeypatch.setattr(workspace_episode_provenance, "record_resource_encounter", lambda *a, **k: None)

    h.budget_results = {}
    import ui_turn_diagnostics
    monkeypatch.setattr(
        ui_turn_diagnostics, "record_context_budget_result",
        lambda name, result: h.budget_results.__setitem__(name, result),
    )

    conversation_choice = {
        "act": "use_workspace", "thread": "library",
        "direction_request": "none", "relinquish_direction": False,
    }
    workspace_choice = action or {
        "resource_class": "library", "action": "read",
        "relative_path": "chosen.txt", "content": "",
    }
    h.expression = {"expression": "I read chosen.txt and received its text."}

    def fake_chat(model, messages, format=None, options=None, think=None, **kwargs):
        if isinstance(format, dict) and "sleep_timing_request" in format.get("properties", {}) \
                and "act" not in format.get("properties", {}):
            # Clark's typed post-reply Sleep choice: its own third call, kept out of the Pass-1/Pass-2
            # call log these tests inspect; answers "none" (null/non-use).
            return {"message": {"content": json.dumps({"alexs_words_asking_me_to_request_sleep": "", "my_words_asking_alex_for_sleep": "", "sleep_timing_request": "none"})},
                    "prompt_eval_count": 1, "done": True, "done_reason": "stop", "eval_count": 5}
        h.calls.append({"format": format, "options": options, "messages": [dict(m) for m in messages]})
        byte_count = sum(len(m.get("content", "").encode("utf-8")) for m in messages)
        if options and options.get("num_predict") == 1:
            # Real-measurement probe. ``compress_probes`` models a real
            # tokenizer (~4 bytes/token on prose); otherwise the honest,
            # maximally conservative "no compression" stand-in.
            count = sum(
                math.ceil(len(m.get("content", "").encode("utf-8")) / 4) if compress_probes
                else len(m.get("content", "").encode("utf-8"))
                for m in messages
            ) or 1
            return {"message": {"content": "x"}, "prompt_eval_count": count,
                    "done": True, "done_reason": "stop", "eval_count": 1}
        if isinstance(format, dict) and "act" in format.get("properties", {}):
            value = conversation_choice
        elif format == "json":
            value = workspace_choice
        else:
            value = h.expression
        return {"message": {"content": json.dumps(value)}, "prompt_eval_count": byte_count,
                "done": True, "done_reason": "stop", "eval_count": 20}

    h.la.ollama.chat = fake_chat

    def workspace_calls():
        return [c for c in h.calls if c["format"] == "json"]

    h.workspace_calls = workspace_calls
    return h


@pytest.mark.parametrize("human_bytes", [PRODUCTION_HUMAN_BYTES, 670])  # the failed turn, and the earlier 670-char H
def test_production_sized_library_turn_completes_and_persists_canonical_x(monkeypatch, tmp_path, human_bytes):
    """The failing live shape, through the real ordinary waking pass, the
    real supervised dispatcher and the real canonical writer."""
    h = _build(monkeypatch, tmp_path, real_writer=True)

    result = h.la.run_waking_turn(
        h.la.AnaxiOrchestrator(), _message(human_bytes), interaction_mode="conversation",
    )

    assert result["reply"] == h.expression["expression"]
    assert result["boundary_result"]["result"]["content"] == h.source_text
    # The workspace material actually reached the model in Pass 2.
    assert h.source_text in "\n".join(m["content"] for m in h.calls[-1]["messages"])

    pass1 = h.budget_results["wsp1_pass1"]
    assert pass1.fits and pass1.max_prompt_budget == PRODUCTION_WSP1_PASS1_BUDGET
    assert pass1.final_prompt_cost < PRODUCTION_WSP1_PASS1_BUDGET
    assert h.budget_results["wsp1_pass2"].fits
    # Costing changed; what the model is shown did not: the unabridged system
    # message (identity + fixed framing) is still what WSP1 sends.
    import interaction_mode as im
    sent_system = h.workspace_calls()[0]["messages"][0]["content"]
    assert len(sent_system.encode("utf-8")) == PRODUCTION_BASE_SYSTEM_BYTES
    assert sent_system.count(im.CONVERSATION_MODE_CLAUSE) == 1
    assert f"Aesthetic directive: {im.CONVERSATION_AESTHETIC_DIRECTIVE}" in sent_system

    trace = h.la.get_last_conversation_direction_trace()
    assert trace["pass1_status"] == trace["pass2_status"] == "ok"
    conn = sqlite3.connect(f"file:{h.db_path}?mode=ro", uri=True)
    try:
        event = conn.execute(
            "SELECT event_type FROM events WHERE event_id=?", (result["native_event_id"],)
        ).fetchone()
        prose = conn.execute(
            "SELECT component_text FROM event_components WHERE event_id=? "
            "AND component_kind='conversational_prose'", (result["native_event_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert event == ("waking_turn",)
    assert prose == (h.expression["expression"],)


def test_true_hard_overflow_still_fails_closed_before_any_workspace_action(monkeypatch, tmp_path):
    """Calibrated costing does not raise the budget: a human message whose
    hard floor genuinely exceeds it still fails closed, with zero WSP1 model
    calls, no workspace action and no persistence."""
    h = _build(monkeypatch, tmp_path, real_writer=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("workspace action executed despite a budget failure")

    monkeypatch.setattr(h.supervisor.wd, "execute_workspace_action", forbidden)
    import conversation_direction as cd

    with pytest.raises(cd.ConversationDirectionFailure) as excinfo:
        h.la.run_waking_turn(
            h.la.AnaxiOrchestrator(), _message(1000), interaction_mode="conversation",
        )

    assert excinfo.value.stage == "workspace_pass1"
    assert excinfo.value.failure_code == "BUDGET_EXCEEDED"
    assert h.workspace_calls() == []  # WSP1 selector never called
    assert h.persistence == []
    assert not h.budget_results["wsp1_pass1"].fits


def test_calibrated_framing_split_is_costed_but_never_altered(monkeypatch):
    import interaction_mode as im
    import test_owc5_s2_integration as fixture

    la, *_ = fixture.fresh_llama_anaxi(pass1_value=None, pass2_value=None)
    framing_text = im.CONVERSATION_MODE_CLAUSE + "\n\n" + f"Aesthetic directive: {im.CONVERSATION_AESTHETIC_DIRECTIVE}"
    identity = "Identity bullets that evolve with Kardia."
    base = identity + "\n\n" + f"Aesthetic directive: {im.CONVERSATION_AESTHETIC_DIRECTIVE}" + "\n\n" + im.CONVERSATION_MODE_CLAUSE

    core, framing = la.core_and_calibrated_framing_contributions(base)
    assert core.rendered_text.strip() == identity
    assert framing.rendered_text == framing_text
    assert core.cost == len(core.rendered_text.encode("utf-8"))  # dynamic part: byte estimator
    assert framing.cost == la._PASS2_FIXED_FRAMING_CALIBRATED_COST < len(framing_text.encode("utf-8"))

    # Fingerprint mismatch (different chat template): safe byte fallback.
    monkeypatch.setitem(la._ollama_environment_cache, "chat_template_sha256", "different")
    _core, mismatched = la.core_and_calibrated_framing_contributions(base)
    assert mismatched.cost == len(framing_text.encode("utf-8"))

    # Unexpected shape: whole text stays one byte-estimated core, no split.
    whole, none_framing = la.core_and_calibrated_framing_contributions("no fixed framing here")
    assert none_framing is None
    assert whole.rendered_text == "no fixed framing here"


def test_overcounted_long_message_recovers_by_real_measurement(monkeypatch, tmp_path):
    """The byte upper bound over-prices prose ~4x. Ordinary waking recovers an
    overcounted human message by a bounded real measurement; the workspace
    stage must too, or an ordinary long message dies at ``workspace_pass1``
    after ordinary Pass 1 already chose the workspace."""
    h = _build(monkeypatch, tmp_path, real_writer=False, compress_probes=True)
    message = _message(1000)

    result = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), message, interaction_mode="conversation")

    assert result["reply"] == h.expression["expression"]
    pass1 = h.budget_results["wsp1_pass1"]
    assert pass1.fits
    assert pass1.included_kind("current_human_message").cost < len(message.encode("utf-8"))
    assert len(h.persistence) == 1


def test_large_library_read_reaches_the_subject_as_a_truthful_window(monkeypatch, tmp_path):
    """A 4000-character read cannot ride whole in the room Pass 2 has left.
    It must arrive as a real prefix with an accurate continuation, never be
    silently dropped."""
    text = "".join(f"Line {i:04d}: the substantive text of a library book.\n" for i in range(80))
    assert len(text) > 4000 - 100
    h = _build(monkeypatch, tmp_path, real_writer=False, source_text=text)

    result = h.la.run_waking_turn(
        h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation",
    )

    delivered = result["boundary_result"]["result"]
    pass2_text = "\n".join(m["content"] for m in h.calls[-1]["messages"])
    assert delivered["content"] and delivered["content"] in pass2_text.replace("\\n", "\n")
    assert text.startswith(delivered["content"])
    assert delivered["has_more"] is True and len(delivered["content"]) < len(text)
    assert delivered["next_offset"] == len(delivered["content"])
    assert json.loads(delivered["next_request"])["offset"] == delivered["next_offset"]
    assert delivered["delivery_window"]["reason"] == "prompt_room"
    assert h.budget_results["wsp1_pass2"].fits
    assert h.budget_results["wsp1_pass2"].included_kind("last_workspace_observation") is not None


def test_large_collection_listing_reaches_the_subject_and_is_fully_walkable(monkeypatch, tmp_path):
    """Browsing a large collection must deliver a real page of entries, and
    following the delivered continuation must reach every entry."""
    import workspace_capability as wc
    import workspace_delivery

    h = _build(monkeypatch, tmp_path, real_writer=False, action={
        "resource_class": "music", "action": "list", "relative_path": "", "content": "",
    })
    names = [f"Artist {i:03d} - A Reasonably Long Track Title Number {i:03d}.mp3" for i in range(151)]
    for name in names:
        Path(h.paths.music_dir, name).write_bytes(b"")

    result = h.la.run_waking_turn(
        h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation",
    )
    page = result["boundary_result"]["result"]
    assert page["entries"] and page["entries"] == names[: len(page["entries"])]
    assert "Track Title Number 000" in "\n".join(m["content"] for m in h.calls[-1]["messages"])
    assert h.budget_results["wsp1_pass2"].fits

    # Walk every page exactly as the subject would, each under the same room.
    seen, request = list(page["entries"]), page.get("next_request")
    room = h.budget_results["wsp1_pass2"].max_prompt_budget - sum(
        c.cost for c in h.budget_results["wsp1_pass2"].included if c.kind != "last_workspace_observation"
    )
    guard = 0
    while request:
        guard += 1
        assert guard < 500
        payload = json.loads(request)
        raw, failure = wc.list_contents(h.paths, wc.MUSIC, "clark", cursor=payload["cursor"])
        assert failure is None
        fitted, info = workspace_delivery.fit_boundary_result(
            {"scope": "local_workspace/music", "result": raw}, room,
        )
        seen.extend(fitted["result"]["entries"])
        assert fitted["result"]["entries"], "a page was delivered empty"
        request = fitted["result"].get("next_request")
    assert seen == names


def test_measured_cost_lets_the_window_use_what_the_prompt_can_really_carry(monkeypatch, tmp_path):
    """The estimator window is a safe floor; a real provider measurement of the
    exact window may enlarge it, never beyond what the measurement proves fits."""
    text = "".join(f"Line {i:04d}: the substantive text of a library book.\n" for i in range(200))
    estimator = _build(monkeypatch, tmp_path / "a", real_writer=False, source_text=text)
    estimated = estimator.la.run_waking_turn(
        estimator.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation",
    )["boundary_result"]["result"]

    measured = _build(monkeypatch, tmp_path / "b", real_writer=False, compress_probes=True, source_text=text)
    result = measured.la.run_waking_turn(
        measured.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation",
    )["boundary_result"]["result"]

    assert len(result["content"]) > 2 * len(estimated["content"])
    assert text.startswith(result["content"])
    assert result["next_offset"] == len(result["content"])
    pass2 = measured.budget_results["wsp1_pass2"]
    assert pass2.fits and pass2.final_prompt_cost <= pass2.max_prompt_budget


def test_photograph_view_completes_at_production_sizes(monkeypatch, tmp_path):
    """The vision Pass 2 admits a 1100-token image against a 2944 budget. With
    production-sized control text and an ordinary 456-byte message the byte
    upper bound alone exceeds it AFTER the view already happened; real
    measurement of the control text (never of the image, whose admission cost
    is the modality's own) restores the room."""
    from PIL import Image

    h = _build(monkeypatch, tmp_path, real_writer=False, compress_probes=True, action={
        "resource_class": "photographs", "action": "view", "relative_path": "p.png", "content": "",
    })
    Image.new("RGB", (640, 480), color=(10, 20, 30)).save(Path(h.paths.photographs_dir, "p.png"))

    result = h.la.run_waking_turn(
        h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation",
    )
    assert result["reply"] == h.expression["expression"]
    pass2 = h.budget_results["wsp1_pass2"]
    assert pass2.fits
    image_cost = context_budget_image_cost()
    assert any(c.cost == image_cost for c in pass2.included)   # image admitted at its real, unreduced cost
    vision_call = h.calls[-1]
    assert vision_call["messages"][-1].get("images"), "pixels must actually be attached"


def context_budget_image_cost():
    import context_budget
    return context_budget.qwen_image_admission_cost(640, 480)


def test_a_long_journal_entry_is_read_progressively_to_its_exact_end(monkeypatch, tmp_path):
    """Journal entries are Clark-authored and unbounded. Reading one back must
    deliver real text and let the subject continue to the end; nothing may be
    silently dropped or duplicated across windows."""
    import workspace_capability as wc
    import workspace_delivery

    entry_text = "".join(f"Entry sentence {i:04d}, written by the subject earlier.\n" for i in range(220))
    h = _build(monkeypatch, tmp_path, real_writer=False, action={
        "resource_class": "journal", "action": "read", "relative_path": "entry-long.json", "content": "",
    })
    Path(h.paths.journal_dir).mkdir(parents=True, exist_ok=True)
    Path(h.paths.journal_dir, "entry-long.json").write_text(json.dumps({
        "entry_id": "entry-long", "created_at": "2026-09-18T00:00:00+00:00",
        "author_actor_id": "clark", "content": entry_text,
    }), encoding="utf-8")

    result = h.la.run_waking_turn(
        h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation",
    )
    first = result["boundary_result"]["result"]
    assert first["content"] and entry_text.startswith(first["content"])
    assert first["entry_id"] == "entry-long" and first["author_actor_id"] == "clark"   # identity always whole
    assert first["content_window"]["has_more"] is True

    room = h.budget_results["wsp1_pass2"].max_prompt_budget - sum(
        c.cost for c in h.budget_results["wsp1_pass2"].included if c.kind != "last_workspace_observation"
    )
    collected, request, guard = first["content"], first["content_window"]["next_request"], 0
    while request:
        guard += 1
        assert guard < 200
        payload = json.loads(request)
        raw, failure = wc.read_journal_entry(h.paths, "entry-long", "clark", offset=payload["offset"], max_chars=payload["max_chars"])
        assert failure is None
        fitted, _info = workspace_delivery.fit_boundary_result({"scope": "local_workspace/journal", "result": raw}, room)
        window = fitted["result"]
        assert window["content"], "a delivered journal window was empty"
        assert payload["offset"] == len(collected)
        collected += window["content"]
        request = (window.get("content_window") or {}).get("next_request")
    assert collected == entry_text
