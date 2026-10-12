"""Live completion of Caret + Family: Mac session scope after Family activation, owner-panel truthfulness.

Root causes (production, 2026-09-21)
  * The Caret correspondents panel is a read of canonical state taken at one moment.  It had no way to be
    re-read, so a page loaded earlier (another tab / the desktop window) kept showing "No Discord author is
    mapped" while the canonical ledger held Alex's active mapping.
  * Activating the Family feature turns every UNSCOPED session identity-less (no dialogue, no retrieval), and
    the owner's Mac window binds its scope only from an env var the launcher never sets.
"""
import os
import sqlite3

import caret_correspondent_admin as cca
import caret_owner_status as cos
import discord_author_mapping as dam
import discord_correspondence as dc
import family_membership as fm
from hir1_registration import HOST_ACTOR_ID, register_canonical_human
from test_caret_correspondent_binding import (
    MEMBER_AUTHOR, _enter_family_mode, _from, _map_member, _snow, _destination_authorized_at,
)
from test_caret_wake_service import _serve, _service, _setup, _waking_turns

_scope = fm.default_bound_scope


def _db(s):
    return f"{s.data_dir}/anaxi_provenance.db"


def test_mac_session_scope_default_only_changes_after_family_activation_for_an_active_principal(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    owner = s.h.actor_id
    assert _scope(_db(s), owner, None) is None                          # family never used: unchanged
    member = _enter_family_mode(s)
    assert _scope(_db(s), owner, None) == fm.SCOPE_PRINCIPAL_PRIVATE     # owner's Mac window is owner-private
    assert _scope(_db(s), member, None) == fm.SCOPE_PRINCIPAL_PRIVATE
    assert _scope(_db(s), "human-actor-never-enrolled", None) is None    # never widened to a stranger
    assert _scope(_db(s), owner, fm.SCOPE_FAMILY_SHARED) == fm.SCOPE_FAMILY_SHARED   # explicit config wins
    assert _scope(_db(s), None, None) is None and _scope(str(tmp_path / "absent.db"), owner, None) is None


def test_family_activation_keeps_alex_mapped_and_pre_mapping_family_messages_permanently_held(monkeypatch, tmp_path):
    s = _setup(monkeypatch, tmp_path)
    a = _destination_authorized_at(s)
    svc = _service(s, tmp_path)
    t1, t2, t3 = a + 100, a + 200, a + 300
    _serve(s, _from(MEMBER_AUTHOR, "wifey", content="HELD-BEFORE-ENROLLMENT", mid=_snow(t1)))
    assert svc.step()[0] == "idle"                                       # unmapped author, family not yet active
    member = _enter_family_mode(s)                                       # activation through the accepted machinery
    conn = sqlite3.connect(_db(s))
    assert dam.resolve_author(conn, "700000000000000101")["status"] == dam.STATUS_MAPPED   # Alex's mapping survives
    assert dam.resolve_author(conn, "700000000000000101")["role"] == dam.ROLE_OWNER
    conn.close()
    from test_caret_correspondent_binding import _map_at
    _map_at(s, MEMBER_AUTHOR, member, t2, label="Wife")                   # prospective binding
    (held,) = dc.held_inbound(s.data_dir)
    assert held["reason"] == dam.STATUS_BEFORE_MAPPING
    assert svc.step()[0] == "idle" and _waking_turns(s) == 0              # never woken by the old message
    view = cca.load_view(s.data_dir)
    assert "Wife" in view["markdown"] and "active" in view["markdown"]
    assert any("Wife (family_member)" == label for label, _pid in view["principals"])
    assert view["active_author_ids"].count(MEMBER_AUTHOR) == 1
    (status,) = cos.caret_owner_status(s.data_dir, trace_path=str(tmp_path / "caret_wake_trace.jsonl"))
    assert "from arrived" not in status["summary"]
    assert status["summary"].startswith("Incoming Caret message that arrived before its author was mapped")


def test_held_status_wording_is_grammatical_for_every_held_reason():
    for status, lead in cos._HELD_LEAD.items():
        assert lead.startswith("Incoming Caret message ") and " from arrived" not in lead
    assert set(cos._HELD_LEAD) == set(dam.HELD_REASON_TEXT)


def test_correspondent_panel_is_reloaded_on_open_and_on_demand_not_only_at_page_load():
    src = open(os.path.join(os.path.dirname(__file__), "llama_gui.py")).read()
    assert "family_membership.default_bound_scope(" in src
    assert "caret_correspondents_accordion.expand" in src and "caret_map_refresh_btn.click" in src
    assert "for _caret_reload_trigger in (demo.load," in src
