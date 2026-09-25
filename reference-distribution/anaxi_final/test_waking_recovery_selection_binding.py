"""WTR0 recovery selector binding: visible H == status H == authorized H ==
executed H, driven through the real llama_gui Blocks wiring (event graph and
handler functions), with synthetic isolated canonical state.

Regression for the live failure: the dropdown showed H-B while the status
line still named H-A (the load-time default), because no event connected the
selector to the status.  The preserved production shape is reproduced: 8
evidenced H events, newest-first, three ELIGIBLE (newest, middle, oldest)
and five INELIGIBLE, with the middle one being the owner's intended target.
"""
import os
import sys
import types
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_waking_turn_recovery import Env  # noqa: E402
import wtr0_waking_recovery as recovery  # noqa: E402

NO_X = "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED"


def _evidenced(env, message, failure_class=NO_X):
    h, _ = env.make_h(message)
    env.record_evidence(h, failure_class)
    return h


def _production_shape(env):
    """oldest -> newest: E_old, 4 x ineligible, E_target, 2 x ineligible, E_new
    (matches the live list: newest eligible, preserved target 4th, oldest last)."""
    ids = {}
    ids["old"] = _evidenced(env, "oldest eligible")
    ids["ni1"] = _evidenced(env, "not retry safe 1", "UNCLASSIFIED_WAKING_EXCEPTION")
    ids["ni2"] = _evidenced(env, "not retry safe 2", "UNCLASSIFIED_WAKING_EXCEPTION")
    ids["ni3"] = _evidenced(env, "not retry safe 3", "UNCLASSIFIED_WAKING_EXCEPTION")
    ids["target"] = _evidenced(env, "preserved qualifying failure")
    ids["ni4"] = _evidenced(env, "not retry safe 4", "UNCLASSIFIED_WAKING_EXCEPTION")
    ids["ni5"] = _evidenced(env, "not retry safe 5", "UNCLASSIFIED_WAKING_EXCEPTION")
    ids["new"] = _evidenced(env, "newest eligible")
    return ids


@pytest.fixture()
def gui(monkeypatch):
    monkeypatch.delenv("ANAXI_BOUND_HUMAN_ACTOR_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["llama_gui.py"])
    sys.modules.pop("llama_gui", None)
    import llama_gui
    yield llama_gui
    sys.modules.pop("llama_gui", None)


@pytest.fixture()
def bound(gui, monkeypatch):
    env = Env(f"selection_binding_{uuid.uuid4().hex[:8]}")
    ids = _production_shape(env)
    monkeypatch.setattr(gui, "_PROVENANCE_DB_PATH", env.db_path)
    monkeypatch.setattr(gui, "BOUND_HUMAN_ACTOR_ID", env.actor_id)
    monkeypatch.setattr(gui, "BOUND_HUMAN_AUTHORITY", types.SimpleNamespace(
        visibility_scope=None, authenticated_actor_id=env.actor_id))
    monkeypatch.setattr(gui.workspace_roaming, "is_worker_running", lambda: False)
    monkeypatch.setattr(gui, "refresh_waking_conversation", lambda: [])
    return types.SimpleNamespace(gui=gui, env=env, ids=ids)


# ---- real Blocks wiring helpers ------------------------------------------

def _component(gui, label):
    return next(b for b in gui.demo.blocks.values() if getattr(b, "label", None) == label)


def _fn_for(gui, trigger_component, event_name):
    return next(
        f for f in gui.demo.fns.values()
        if any(t[0] == trigger_component._id and t[1] == event_name for t in f.targets)
    )


def _outputs(fn_block, values):
    assert len(values) == len(fn_block.outputs)
    return {c._id: v for c, v in zip(fn_block.outputs, values)}


class Surface:
    """Simulates one browser session using the real event graph: the selector
    value, status, confirmation and bound-H State live in client components."""

    def __init__(self, gui):
        self.gui = gui
        self.selector = _component(gui, "Evidenced waking input recovery")
        self.confirm = _component(gui, gui.WAKING_RECOVERY_CONFIRM_LABEL)
        self.status_md = next(
            b for b in gui.demo.blocks.values()
            if b.__class__.__name__ == "Markdown"
            and any(b._id == o._id for f in gui.demo.fns.values() for o in f.outputs
                    if f.fn is gui.load_waking_recovery_surface))
        self.state = next(
            b for b in gui.demo.blocks.values()
            if b.__class__.__name__ == "State"
            and any(b._id == o._id for f in gui.demo.fns.values() for o in f.outputs
                    if f.fn is gui.render_waking_recovery_selection))
        self.value = None      # visible selector value
        self.status = None
        self.checked = False
        self.bound = None
        self.choices = []

    def _apply(self, outs):
        for cid, v in outs.items():
            if cid == self.selector._id:
                if "choices" in v:
                    self.choices = [c[1] for c in v["choices"]]
                self.value = v.get("value", self.value)
            elif cid == self.status_md._id:
                self.status = v
            elif cid == self.confirm._id:
                self.checked = v["value"] if isinstance(v, dict) else v
            elif cid == self.state._id:
                self.bound = v

    def load(self):
        f = next(f for f in self.gui.demo.fns.values() if f.fn is self.gui.load_waking_recovery_surface)
        self._apply(_outputs(f, f.fn()))

    def select(self, h):
        self.value = h
        f = _fn_for(self.gui, self.selector, "change")
        self._apply(_outputs(f, f.fn(self.value)))

    def check(self):
        self.checked = True

    def click_recover(self):
        f = _fn_for(self.gui, self._button(), "click")
        before = self.value
        vals = f.fn(self.value, self.checked, self.bound)
        self._apply(_outputs(f, vals))
        return before

    def _button(self):
        return next(b for b in self.gui.demo.blocks.values()
                    if getattr(b, "value", None) == "Recover selected waking input once")


def _status_h(status):
    import re
    return re.findall(r"selected (?:eligible )?H `([^`]+)`", status)


@pytest.fixture()
def capture_execution(bound, monkeypatch):
    calls = []

    def fake_execute(db_path, **kw):
        # Drive the real generation closure the GUI builds, capturing the H the
        # real waking entrypoint would receive, without touching a model.
        seen = {}

        def fake_run_waking_turn(orch, prompt, **kwargs):
            seen["existing_h"] = kwargs["existing_human_input_event_id"]
            return {}
        monkeypatch.setattr(bound.gui, "run_waking_turn", fake_run_waking_turn)
        # authorization check (same fresh assess_eligibility used by reserve_recovery)
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            receipt = recovery.assess_eligibility(
                conn, kw["human_input_event_id"], kw["actor_id"])
        finally:
            conn.close()
        assert receipt["decision"] == recovery.EligibilityDecision.ELIGIBLE
        kw["generation_fn"]("prompt")
        calls.append({"authorized_h": kw["human_input_event_id"],
                      "generated_h": seen["existing_h"],
                      "confirmation": kw["confirmation"]})
        return {"terminal_state": "SYNTHETIC"}

    monkeypatch.setattr(bound.gui.waking_recovery_control, "execute_candidate", fake_execute)
    monkeypatch.setattr(bound.gui, "AnaxiOrchestrator",
                        lambda *_: types.SimpleNamespace(close=lambda: None))
    return calls


# ---- tests ----------------------------------------------------------------

def test_load_selects_nothing_and_status_names_no_h(bound):
    s = Surface(bound.gui)
    s.load()
    assert s.value is None and s.bound is None
    assert _status_h(s.status) == []
    assert len(s.choices) == 3                      # recent three listed by default
    s.gui_toggle_older = bound.gui.toggle_older_waking_recovery(None, True)
    assert len(s.gui_toggle_older[0]["choices"]) == 8   # every evidenced H, no cap, behind the expansion


def test_selecting_h_a_then_h_b_keeps_selector_status_and_bound_in_agreement(bound):
    s = Surface(bound.gui)
    s.load()
    for key in ("new", "target", "old", "target"):
        s.select(bound.ids[key])
        assert s.value == bound.ids[key]
        assert _status_h(s.status) == [bound.ids[key]]
        assert s.bound == bound.ids[key]
        assert s.checked is False


def test_selector_change_event_is_wired_to_status_confirmation_and_bound_state(bound):
    g = bound.gui
    f = _fn_for(g, _component(g, "Evidenced waking input recovery"), "change")
    s = Surface(g)
    assert {o._id for o in f.outputs} == {s.status_md._id, s.confirm._id, s.state._id}


def test_exact_preserved_shape_target_binds_and_authorizes_only_itself(bound, capture_execution):
    s = Surface(bound.gui)
    s.load()
    s.select(bound.ids["target"])
    s.check()
    executed_visible = s.click_recover()
    assert executed_visible == bound.ids["target"]
    assert capture_execution == [{
        "authorized_h": bound.ids["target"], "generated_h": bound.ids["target"],
        "confirmation": True}]
    # the default-newest eligible H (the H the live status wrongly named) untouched
    assert bound.ids["new"] != bound.ids["target"]


def test_switching_h_a_to_h_b_authorizes_and_executes_h_b(bound, capture_execution):
    s = Surface(bound.gui)
    s.load()
    s.select(bound.ids["new"])
    s.check()
    s.select(bound.ids["old"])              # switch: confirmation must reset
    assert s.checked is False
    s.check()
    s.click_recover()
    assert [c["authorized_h"] for c in capture_execution] == [bound.ids["old"]]
    assert [c["generated_h"] for c in capture_execution] == [bound.ids["old"]]


def test_stale_status_state_fails_closed_with_no_substitution(bound, capture_execution):
    """Visible H-B but bound state still H-A (status/confirm not re-rendered)."""
    s = Surface(bound.gui)
    s.load()
    s.select(bound.ids["new"])
    s.check()
    s.value = bound.ids["target"]           # visible changed, change event not applied
    s.click_recover()
    assert capture_execution == []
    assert s.value == bound.ids["target"]   # selection preserved, not swapped
    assert s.checked is False
    assert "not given for this exact H" in s.status


def test_no_selection_and_ineligible_and_unknown_selection_fail_closed(bound, capture_execution):
    s = Surface(bound.gui)
    s.load()
    s.check()
    s.click_recover()                               # nothing selected
    s.select(bound.ids["ni1"])                      # ineligible
    assert s.bound is None and "cannot be recovered" in s.status
    s.check()
    s.click_recover()
    s.select("01NOTANEVIDENCEDH0000000000")         # unknown
    assert s.bound is None and "no longer an evidenced H" in s.status
    s.check()
    s.click_recover()
    assert capture_execution == []
    with bound.env.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM wtr0_recovery").fetchone()[0] == 0


def test_selection_that_became_ineligible_after_confirmation_fails_closed(bound, capture_execution):
    s = Surface(bound.gui)
    s.load()
    s.select(bound.ids["target"])
    s.check()
    bound.env.record_evidence(bound.ids["target"], "UNCLASSIFIED_WAKING_EXCEPTION")
    s.click_recover()
    assert capture_execution == []
    assert s.value == bound.ids["target"]           # not switched to another eligible H


def test_refresh_preserves_selection_and_never_reverts_to_default(bound):
    g = bound.gui
    dd, status = g.refresh_waking_recovery_controls(bound.ids["target"])
    assert dd["value"] == bound.ids["target"]
    assert _status_h(status) == [bound.ids["target"]]
    dd, status = g.refresh_waking_recovery_controls(None)
    assert dd["value"] is None and _status_h(status) == []
    dd, status = g.refresh_waking_recovery_controls("01NOPE")
    assert dd["value"] is None


def test_post_recovery_refresh_keeps_the_executed_h_selected_not_another_eligible(bound, capture_execution):
    s = Surface(bound.gui)
    s.load()
    s.select(bound.ids["target"])
    s.check()
    s.click_recover()
    assert s.value == bound.ids["target"]           # not jumped to 'new'
    assert _status_h(s.status) in ([bound.ids["target"]], [])
    assert s.checked is False


def test_one_shot_semantics_unchanged_real_execute_candidate_still_requires_confirmation(bound):
    """The GUI change adds gates only; the underlying control still refuses
    an unconfirmed attempt with zero consequential calls."""
    calls = []
    with pytest.raises(recovery.RecoveryDenied):
        bound.gui.waking_recovery_control.execute_candidate(
            bound.env.db_path, human_input_event_id=bound.ids["target"],
            actor_id=bound.env.actor_id, confirmation=False,
            background_activity_running=False, expected_visibility_scope=None,
            reset_fn=lambda: calls.append("reset"),
            generation_fn=lambda p: calls.append("gen"))
    assert calls == []
