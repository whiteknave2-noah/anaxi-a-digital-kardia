"""Compact waking-recovery presentation (header / recent-three / older expansion).

Presentation only: eligibility and execution remain WTR0's, read through the accepted
list_candidates / execute_candidate.  Runs through the real llama_gui handlers.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_waking_recovery_selection_binding import (  # noqa: E402,F401
    Surface, bound, capture_execution, gui, _production_shape,
)
import wtr0_waking_recovery as recovery  # noqa: E402


def _canonical_eligible_count(bound):
    import sqlite3 as sq
    conn = sq.connect(f"file:{bound.env.db_path}?mode=ro", uri=True)
    try:
        return sum(1 for r in recovery.list_recovery_candidates(conn, bound.env.actor_id)
                   if r["decision"] == recovery.EligibilityDecision.ELIGIBLE)
    finally:
        conn.close()


def _header(update):
    return update["label"], update.get("open")


def test_8_zero_eligible_is_compact_and_collapsed(gui, monkeypatch):
    monkeypatch.setattr(gui, "BOUND_HUMAN_AUTHORITY", None)
    label, is_open = _header(gui.load_waking_recovery_header())
    assert is_open is False and label.startswith("Waking recovery")
    monkeypatch.setattr(gui, "_recovery_receipts", lambda: [])
    label, is_open = _header(gui.load_waking_recovery_header())
    assert label == "Waking recovery — 0 eligible" and is_open is False


def test_9_eligible_count_is_derived_from_canonical_recovery_law(bound):
    label, _ = _header(bound.gui.load_waking_recovery_header())
    assert label == f"Waking recovery — {_canonical_eligible_count(bound)} eligible"
    assert _canonical_eligible_count(bound) == 3
    # the presentation module contains no eligibility rule of its own
    src = open(bound.gui.__file__).read()
    header = src[src.index("def _recovery_header"):src.index("def _recovery_receipts")]
    header = header.split('"""')[2]                      # code only, not the docstring
    assert "RETRY_SAFE" not in header and "NO_X" not in header and "list_candidates" not in header


def test_header_opens_only_when_the_newest_evidenced_item_is_eligible(bound):
    _, is_open = _header(bound.gui.load_waking_recovery_header())
    assert is_open is True                                   # production shape: newest is eligible
    bound.env.record_evidence(bound.env.make_h("newer, not retry safe")[0], "UNCLASSIFIED_WAKING_EXCEPTION")
    _, is_open = _header(bound.gui.load_waking_recovery_header())
    assert is_open is False


def test_10_expanded_view_lists_the_recent_three_first(bound):
    s = Surface(bound.gui)
    s.load()
    ids = bound.ids
    assert s.choices == [ids["new"], ids["ni5"], ids["ni4"]]


def test_11_older_eligible_entries_remain_reachable(bound):
    dd, _, _, _ = bound.gui.toggle_older_waking_recovery(None, True)
    listed = [c[1] for c in dd["choices"]]
    assert len(listed) == 8 and bound.ids["target"] in listed and bound.ids["old"] in listed


def test_selected_older_item_is_not_dropped_when_the_expansion_is_collapsed(bound):
    dd, status, confirm, bh = bound.gui.toggle_older_waking_recovery(bound.ids["target"], False)
    assert bound.ids["target"] in [c[1] for c in dd["choices"]] and bh == bound.ids["target"]


def test_12_ineligible_items_are_not_actionable(bound, capture_execution):
    s = Surface(bound.gui)
    s.load()
    s.select(bound.ids["ni5"])
    assert s.bound is None
    assert bound.gui._recovery_confirm_update(None)["interactive"] is False
    s.check()
    s.click_recover()
    assert capture_execution == []
    dd, status, confirm, bh = bound.gui.toggle_older_waking_recovery(bound.ids["ni1"], True)
    assert bh is None and "cannot be recovered" in status


def test_no_hidden_auto_selection(bound):
    for show in (False, True):
        dd, status, confirm, bh = bound.gui.toggle_older_waking_recovery(None, show)
        assert dd["value"] is None and bh is None


def test_13_opening_and_expanding_the_ui_performs_no_recovery(bound, capture_execution):
    g = bound.gui
    before = _canonical_counts(bound)
    g.load_waking_recovery_header()
    g.load_waking_recovery_surface()
    g.toggle_older_waking_recovery(None, True)
    g.toggle_older_waking_recovery(bound.ids["target"], False)
    g.refresh_waking_recovery_header()
    assert capture_execution == [] and _canonical_counts(bound) == before


def _canonical_counts(bound):
    import sqlite3 as sq
    conn = sq.connect(f"file:{bound.env.db_path}?mode=ro", uri=True)
    try:
        return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("events", "wtr0_recovery", "waking_failure_evidence")]
    finally:
        conn.close()


def test_14_selection_and_invocation_still_use_the_accepted_recovery_mechanism(bound, capture_execution):
    s = Surface(bound.gui)
    s.load()
    s.select(bound.ids["new"])
    assert s.bound == bound.ids["new"]
    s.check()
    s.click_recover()
    assert [c["authorized_h"] for c in capture_execution] == [bound.ids["new"]]
    assert capture_execution[0]["generated_h"] == bound.ids["new"] and capture_execution[0]["confirmation"] is True
    src = open(bound.gui.__file__).read()
    execute = src[src.index("def execute_waking_recovery_from_owner_surface"):src.index("def _perform_") if "def _perform_" in src[src.index("def execute_waking_recovery_from_owner_surface"):] else None]
    assert "waking_recovery_control.execute_candidate" in execute
