"""The owner Sleep surface shows Clark's canonical request by itself.

Live failure (2026-09-20): the "Clark-originated Sleep request" field is a read-only
discovery of canonical requests, never an owner transcription field. It read the
canonical table only on page load, so a request recorded during a waking turn was
not shown until the owner reloaded the page.
"""
import re
from pathlib import Path

import pytest

import sleep_timing_knock as timing
import test_llama_gui_conversation_mode as gui_fixture
import test_sleep_timing_knock as fixture


def _gui_with_owner_and_env(name):
    gui, *_ = gui_fixture.fresh_gui(["llama_gui.py", "--conversation"])
    data_dir = fixture._new_env(name)
    owner = fixture._register_owner(data_dir, name)
    gui.PROVENANCE_DB_DIR = data_dir
    gui.BOUND_HUMAN_ACTOR_ID = owner
    return gui, data_dir, owner


def test_no_request_says_so_and_a_canonical_request_is_discovered_without_owner_typing():
    gui, data_dir, owner = _gui_with_owner_and_env("owner_surface_discovery")
    dropdown, status = gui.refresh_sleep_controls()
    assert "no actionable Clark-originated request" in status
    assert dropdown["choices"] == [] and dropdown["interactive"] is False

    request_id, _ = fixture._make_pending(data_dir)          # Clark's canonical request
    dropdown, status = gui.refresh_sleep_controls()
    assert dropdown["value"] == request_id and dropdown["interactive"] is True
    assert request_id in status and timing.STATE_PENDING in status
    assert [c[1] for c in dropdown["choices"]] == [request_id]


def test_owner_actions_from_the_surface_target_the_selected_request_and_never_execute():
    gui, data_dir, owner = _gui_with_owner_and_env("owner_surface_actions")
    request_id, _ = fixture._make_pending(data_dir)
    _, status = gui.apply_sleep_owner_action(request_id, "AUTHORIZE")
    assert "AUTHORIZED" in status and "Owner action AUTHORIZE recorded" in status
    receipt = timing.fetch_sleep_request(data_dir, request_id)
    assert receipt["state"] == timing.STATE_AUTHORIZED and receipt["execution_attempts"] == []
    _, _, cleared = gui.execute_sleep_from_owner_surface(request_id, False)   # unchecked confirmation
    assert cleared is False
    assert timing.fetch_sleep_request(data_dir, request_id)["execution_attempts"] == []


def test_the_surface_is_refreshed_after_every_waking_turn_not_only_on_page_load():
    source = Path(gui_fixture.ANAXI_FINAL, "llama_gui.py").read_text(encoding="utf-8")
    assert re.search(
        r"submitted\.then\(\s*fn=refresh_sleep_controls,\s*inputs=None,\s*"
        r"outputs=\[sleep_request_selector, sleep_control_status\]", source)


def test_every_owner_sleep_action_is_traced_and_the_selection_stays_on_the_request_acted_on():
    # Live 2026-09-24: an Authorize click left no durable record and its cause could not be
    # established; after a refusal the selection silently moved to a different request.
    import json
    import os
    gui, data_dir, owner = _gui_with_owner_and_env("owner_surface_trace")
    first, _ = fixture._make_pending(data_dir)
    gui.apply_sleep_owner_action(first, "AUTHORIZE")
    second, _ = fixture._make_pending(data_dir)                  # a newer request lists first
    dropdown, status = gui.apply_sleep_owner_action(first, "AUTHORIZE")   # illegal: already AUTHORIZED
    assert "Owner action refused" in status
    assert dropdown["value"] == first and first in status             # never silently moved to `second`
    dropdown, status = gui.apply_sleep_owner_action(second, "AUTHORIZE")
    assert dropdown["value"] == second and "Owner action AUTHORIZE recorded" in status
    _, _, _ = gui.execute_sleep_from_owner_surface(second, False)     # unchecked confirmation: refused
    with open(os.path.join(data_dir, gui.SLEEP_CONTROL_TRACE), encoding="utf-8") as handle:
        trace = [json.loads(line) for line in handle]
    assert [(t["action"], t["request_id"], t["status"]) for t in trace] == [
        ("AUTHORIZE", first, "recorded"), ("AUTHORIZE", first, "refused"),
        ("AUTHORIZE", second, "recorded"), ("EXECUTE", second, "refused")]
    assert all(t["owner_actor_id"] == owner for t in trace)
    assert "not a legal transition" in trace[1]["detail"]
