"""Alex's absence is not subject Sleep.

A long interval with no human input, with or without background Workspace
activity, must never be described as a Sleep cycle nor cause a Sleep request,
knock, cycle or receipt; only an authorized ``sleep_cycles`` row may do so.
"""

from contextlib import closing
from datetime import timezone
import os
import sqlite3
import sys
import tempfile

import temporal_grounding as tg


NOW = 1788703200
DAYS = 86400


def _db(directory):
    path = os.path.join(directory, "canonical.db")
    with closing(sqlite3.connect(path)) as c, c:
        c.executescript(
            "CREATE TABLE events(event_id TEXT,event_type TEXT,occurred_at INTEGER,pipeline_id TEXT);"
            "CREATE TABLE event_components(event_id TEXT,sequence INTEGER,component_kind TEXT,"
            "component_text TEXT,creator_actor_id TEXT,authorship_resolution TEXT);"
            "CREATE TABLE sleep_cycles(started_at INTEGER,completed_at INTEGER);"
        )
        for ident, kind, epoch in (("h0", "human_waking_input", NOW - 5 * DAYS),
                                   ("current", "human_waking_input", NOW)):
            c.execute("INSERT INTO events VALUES (?,?,?,?)", (ident, kind, epoch, "llama"))
            c.execute("INSERT INTO event_components VALUES (?,0,'human_input','SYNTHETIC',?, 'resolved')",
                      (ident, "human"))
        c.execute("INSERT INTO events VALUES ('x0','waking_turn',?, 'llama')", (NOW - 5 * DAYS + 60,))
        c.execute("INSERT INTO event_components VALUES ('x0',1,'human_input_event_id','h0','host','resolved')")
    return path


def _snapshot(path, **kw):
    return tg.snapshot(path, "current", now=NOW, process_started=NOW - 100, tz=timezone.utc, **kw)


def test_five_days_without_the_human_is_elapsed_time_never_a_sleep_claim():
    with tempfile.TemporaryDirectory() as directory:
        path = _db(directory)
        for exhaustive in (False, True):
            text = _snapshot(path, previous_end=NOW - 5 * DAYS, sleep_ledger_exhaustive=exhaustive)
            assert "Dormant interval: 4 days" in text
            assert "Sleep cycle(s) completed" not in text
            assert ("No authorized Sleep cycle" in text) if exhaustive else (
                "Sleep occurrence during that interval: not established" in text)


def test_only_an_authorized_sleep_cycle_row_can_make_the_grounding_mention_completed_sleep():
    with tempfile.TemporaryDirectory() as directory:
        path = _db(directory)
        with closing(sqlite3.connect(path)) as c, c:
            c.execute("INSERT INTO sleep_cycles VALUES (?,?)", (NOW - 4 * DAYS, NOW - 4 * DAYS + 600))
        text = _snapshot(path, previous_end=NOW - 5 * DAYS)
        assert "1 authorized Sleep cycle(s) completed" in text


def test_absence_grounding_performs_no_sleep_import_write_or_schedule():
    with tempfile.TemporaryDirectory() as directory:
        path = _db(directory)
        before = open(path, "rb").read()
        sleep_modules_before = {m for m in ("anaxi_sleep", "sleep_owner_control") if m in sys.modules}
        _snapshot(path, previous_end=NOW - 5 * DAYS)
        assert open(path, "rb").read() == before, "rendering absence must not write canonical state"
        assert {m for m in ("anaxi_sleep", "sleep_owner_control") if m in sys.modules} == sleep_modules_before
        source = open(tg.__file__, encoding="utf-8").read()
        for forbidden in ("run_sleep", "import anaxi_sleep", "schedule", "sleep_timing_request"):
            assert forbidden not in source, forbidden
