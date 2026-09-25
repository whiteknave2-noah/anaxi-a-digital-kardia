"""
Anaxi -- Amendment S1 (canonical native session resolution) coverage.

Focused, dedicated coverage for the specific defect S1 corrects:
hippocampus_store.py's and standing_hippocampus_audit.py's session
derivation used to infer session membership from temporal-range
overlap against sessions.started_at/ended_at alone. Since
sessions.ended_at is never populated by any production writer, every
session stays "open" forever, and once more than one session exists,
any native event's occurred_at legitimately matches more than one
open session -- producing false ambiguous_session_resolution findings
for events whose true session was never actually in doubt: it is
already recorded exactly, once, at write time, via
events.auth_context_id -> auth_contexts.session_id -> sessions.session_id.

S1 resolves native session membership through that exact canonical
link instead, and no longer consults temporal overlap for native
events at all. Historical material (no canonical native session ever
existed for it) is unaffected -- it remains (None, "unknown").

This file exercises hippocampus_store.py's ingestion-side
_derive_session() and standing_hippocampus_audit.py's independently
re-derived version together, proving they agree, and specifically
reconstructs the exact three-session/five-event shape observed live
(session A/B/C, all ended_at=NULL, later sessions temporally
overlapping earlier ones) to prove the real false-ambiguity pattern
comes back clean under S1.

No model/API call anywhere in this file.

Run:
    python test_session_attribution_s1.py
"""

import hashlib
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hippocampus_store as hs
import standing_hippocampus_audit as audit
from provenance_schema import create_provenance_db

_PASS = 0
_FAIL = 0


def check(name: str, condition: bool) -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"PASS: {name}")
    else:
        _FAIL += 1
        print(f"FAIL: {name}")


def raises(fn, exc_types) -> bool:
    try:
        fn()
    except exc_types:
        return True
    except Exception as e:
        print(f"  (wrong exception type: {type(e).__name__}: {e})")
        return False
    return False


def _seed_actors(conn):
    now = int(time.time())
    host_actor_id = "actor-host-1"
    conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', ?)", (host_actor_id, now))
    conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    return host_actor_id


def _insert_event(conn, event_id, pipeline_id, auth_context_id, occurred_at):
    conn.execute(
        "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, epoch_id, "
        "auth_context_id, input_source_ref, occurred_at, record_created_at) "
        "VALUES (?, 'waking_turn', ?, 'known', NULL, ?, ?, ?, ?)",
        (event_id, pipeline_id, auth_context_id, event_id, occurred_at, occurred_at),
    )


def _insert_bc_component(conn, event_id, host_actor_id, text):
    conn.execute(
        "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
        "authorship_resolution, component_text, content_sha256) VALUES (?, 0, ?, 'bounded_clause', 'resolved', ?, ?)",
        (event_id, host_actor_id, text, hashlib.sha256(text.encode("utf-8")).hexdigest()),
    )


def run_audit(paths):
    return audit.run_standing_hippocampus_audit(
        paths.hippocampus_db_path, paths.provenance_db_path, paths.relational_db_path, paths.historical_jsonl_path
    )


def run_test_suite() -> bool:
    with tempfile.TemporaryDirectory(prefix="anaxi_session_s1_test_") as root:

        # =====================================================================
        # Reconstruction of the exact live pattern: three sessions (A, B, C),
        # all ended_at=NULL (never closed, matching production reality),
        # each strictly later than the one before, each with exactly one
        # canonically-assigned native event -- plus one event in the FIRST
        # session whose occurred_at is deliberately later than B and C's
        # start times too, so it temporally overlaps both. This is the exact
        # shape that produced ambiguous_session_resolution=4 live.
        # =====================================================================
        d = os.path.join(root, "abc")
        os.makedirs(d)
        prov_path = os.path.join(d, "anaxi_provenance.db")
        rel_path = os.path.join(d, "anaxi_relational_llama.db")
        jsonl_path = os.path.join(d, "anaxi_log.jsonl")
        hip_path = os.path.join(d, "anaxi_hippocampus.db")

        conn = create_provenance_db(prov_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        pipeline_id = "pipe-llama-1"
        conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
                     "VALUES (?, 'anaxi_orchestration_lineage_a', 'llama', 'test')", (pipeline_id,))
        host_actor_id = _seed_actors(conn)

        # Sessions A, B, C -- strictly increasing starts, all open forever.
        conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-A', ?, 1000, NULL)", (pipeline_id,))
        conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-B', ?, 5000, NULL)", (pipeline_id,))
        conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-C', ?, 9000, NULL)", (pipeline_id,))

        conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) VALUES ('ac-A1', 'sess-A', 'unknown', 1000)")
        conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) VALUES ('ac-A2', 'sess-A', 'unknown', 8500)")  # temporally overlaps B and C's open windows
        conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) VALUES ('ac-B1', 'sess-B', 'unknown', 5000)")
        conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) VALUES ('ac-C1', 'sess-C', 'unknown', 9000)")

        # evt-a1: early, unambiguous even under the OLD rule (only sess-A exists yet).
        _insert_event(conn, "evt-a1", pipeline_id, "ac-A1", occurred_at=1500)
        # evt-a2: canonically in sess-A, but occurred_at is AFTER sess-B and
        # sess-C both started -- under the old temporal rule this would be
        # "ambiguous" (matches sess-A, sess-B, sess-C all at once); under S1
        # it must resolve to sess-A alone, exactly like the real turns 3/4
        # that were showing false ambiguity live.
        _insert_event(conn, "evt-a2", pipeline_id, "ac-A2", occurred_at=8500)
        # evt-b1: canonically in sess-B, occurred_at after sess-C started too
        # -- temporally overlaps sess-C's open window as well as its own.
        _insert_event(conn, "evt-b1", pipeline_id, "ac-B1", occurred_at=9500)
        # evt-c1: canonically in sess-C.
        _insert_event(conn, "evt-c1", pipeline_id, "ac-C1", occurred_at=9600)

        for eid, actor_suffix in (("evt-a1", "1"), ("evt-a2", "2"), ("evt-b1", "3"), ("evt-c1", "4")):
            _insert_bc_component(conn, eid, host_actor_id, f"Saved: S1 fixture {actor_suffix}.")

        conn.commit()
        conn.close()

        rel_conn = sqlite3.connect(rel_path)
        rel_conn.execute("""CREATE TABLE relational_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
            created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
            agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
            status TEXT NOT NULL DEFAULT 'recorded', event_id TEXT
        )""")
        rel_conn.commit()
        rel_conn.close()
        open(jsonl_path, "w").close()

        hs.create_hippocampus_db(hip_path).close()
        paths = hs.HippocampusPaths(prov_path, rel_path, jsonl_path, hip_path)
        sync_result = hs.sync_hippocampus(hip_path, paths)

        check("1/2/3. exact canonical resolution: evt-a1 (unambiguous even under the old rule) -> sess-A",
              _session_of(hip_path, "evt-a1") == ("sess-A", "resolved"))
        check("2. overlapping open sessions: evt-a2 (canonically sess-A) resolves to sess-A even though "
              "sess-B and sess-C are ALSO open and temporally cover its occurred_at",
              _session_of(hip_path, "evt-a2") == ("sess-A", "resolved"))
        check("2. overlapping open sessions: evt-b1 (canonically sess-B) resolves to sess-B even though "
              "sess-C is also open and temporally covers its occurred_at",
              _session_of(hip_path, "evt-b1") == ("sess-B", "resolved"))
        check("2. overlapping open sessions: evt-c1 (canonically sess-C) resolves to sess-C",
              _session_of(hip_path, "evt-c1") == ("sess-C", "resolved"))

        result = run_audit(paths)
        check("10. current false-ambiguity pattern becomes clean: ambiguous_session_resolution == 0 "
              "for this exact reconstruction of the live shape (3 open sessions, later events "
              "temporally overlapping earlier still-open ones)",
              result.counts["ambiguous_session_resolution"] == 0)
        check("9. audit independently agrees with canonical assignment: invalid_session_resolution == 0",
              result.counts["invalid_session_resolution"] == 0)
        check("audit fully clean on this fixture", result.is_clean())

        # =====================================================================
        # 3. Concurrency-style overlap: two sessions whose time ranges
        # GENUINELY overlap (both have real, non-NULL ended_at, and those
        # ranges intersect) -- not merely "both open." Their own canonically-
        # assigned events must still each resolve to their own session.
        # =====================================================================
        d2 = os.path.join(root, "concurrent")
        os.makedirs(d2)
        prov2 = os.path.join(d2, "anaxi_provenance.db")
        rel2 = os.path.join(d2, "anaxi_relational_llama.db")
        jsonl2 = os.path.join(d2, "anaxi_log.jsonl")
        hip2 = os.path.join(d2, "anaxi_hippocampus.db")

        conn = create_provenance_db(prov2)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
                     "VALUES ('pipe-llama-1', 'anaxi_orchestration_lineage_a', 'llama', 'test')")
        host_actor_id2 = _seed_actors(conn)
        # Genuinely overlapping ranges: X = [1000, 2000], Y = [1500, 2500].
        conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-X', 'pipe-llama-1', 1000, 2000)")
        conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-Y', 'pipe-llama-1', 1500, 2500)")
        conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) VALUES ('ac-X', 'sess-X', 'unknown', 1000)")
        conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) VALUES ('ac-Y', 'sess-Y', 'unknown', 1500)")
        # Both events occur at 1800 -- squarely inside BOTH ranges. Under the
        # old temporal rule this would be double-ambiguous for both events;
        # under S1 each resolves purely to its own canonical session.
        _insert_event(conn, "evt-x", "pipe-llama-1", "ac-X", occurred_at=1800)
        _insert_event(conn, "evt-y", "pipe-llama-1", "ac-Y", occurred_at=1800)
        _insert_bc_component(conn, "evt-x", host_actor_id2, "Saved: concurrent X.")
        _insert_bc_component(conn, "evt-y", host_actor_id2, "Saved: concurrent Y.")
        conn.commit()
        conn.close()

        rel_conn = sqlite3.connect(rel2)
        rel_conn.execute("""CREATE TABLE relational_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
            created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
            agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
            status TEXT NOT NULL DEFAULT 'recorded', event_id TEXT
        )""")
        rel_conn.commit()
        rel_conn.close()
        open(jsonl2, "w").close()

        hs.create_hippocampus_db(hip2).close()
        paths2 = hs.HippocampusPaths(prov2, rel2, jsonl2, hip2)
        hs.sync_hippocampus(hip2, paths2)

        check("3/8. concurrency-style overlap: evt-x (same occurred_at as evt-y, genuinely overlapping "
              "session time ranges) resolves to its own canonical session sess-X",
              _session_of(hip2, "evt-x") == ("sess-X", "resolved"))
        check("3/8. concurrency-style overlap: evt-y resolves to its own canonical session sess-Y, "
              "not sess-X and not ambiguous",
              _session_of(hip2, "evt-y") == ("sess-Y", "resolved"))
        result2 = run_audit(paths2)
        check("3/8. concurrency-style overlap: audit fully clean (no ambiguous/invalid session findings)",
              result2.counts["ambiguous_session_resolution"] == 0 and result2.counts["invalid_session_resolution"] == 0)

        # =====================================================================
        # 4. Missing canonical session -> unknown (event's own auth_context_id
        # is NULL -- no canonical assignment exists at all).
        # =====================================================================
        d3 = os.path.join(root, "missing")
        os.makedirs(d3)
        prov3 = os.path.join(d3, "anaxi_provenance.db")
        rel3 = os.path.join(d3, "anaxi_relational_llama.db")
        jsonl3 = os.path.join(d3, "anaxi_log.jsonl")
        hip3 = os.path.join(d3, "anaxi_hippocampus.db")
        conn = create_provenance_db(prov3)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
                     "VALUES ('pipe-llama-1', 'anaxi_orchestration_lineage_a', 'llama', 'test')")
        host_actor_id3 = _seed_actors(conn)
        _insert_event(conn, "evt-noauth", "pipe-llama-1", None, occurred_at=100)
        _insert_bc_component(conn, "evt-noauth", host_actor_id3, "Saved: no canonical auth context.")
        conn.commit()
        conn.close()
        rel_conn = sqlite3.connect(rel3)
        rel_conn.execute("""CREATE TABLE relational_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
            created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
            agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
            status TEXT NOT NULL DEFAULT 'recorded', event_id TEXT
        )""")
        rel_conn.commit()
        rel_conn.close()
        open(jsonl3, "w").close()
        hs.create_hippocampus_db(hip3).close()
        paths3 = hs.HippocampusPaths(prov3, rel3, jsonl3, hip3)
        hs.sync_hippocampus(hip3, paths3)
        check("4. missing canonical session (auth_context_id IS NULL) -> unknown, never guessed from timestamp",
              _session_of(hip3, "evt-noauth") == (None, "unknown"))
        result3 = run_audit(paths3)
        check("4. audit agrees: fully clean", result3.is_clean())

        # =====================================================================
        # 6. Invalid referenced session: auth_context names a session_id that
        # does not exist in sessions. Schema FK + append-only triggers make
        # this unreachable through any real production write path -- the
        # ONLY way to construct it in a fixture is a deliberate, temporary
        # FK bypass, exactly analogous to this project's established
        # hippocampal-side-tampering technique for exercising integrity
        # checks against otherwise-enforced/append-only state.
        # =====================================================================
        d4 = os.path.join(root, "dangling")
        os.makedirs(d4)
        prov4 = os.path.join(d4, "anaxi_provenance.db")
        conn = create_provenance_db(prov4)
        # auth_contexts is append-only (trigger-enforced, same as events/
        # event_components) -- a dangling session_id cannot be produced by
        # INSERT-then-UPDATE. It must be inserted with the bad value
        # directly, with FK enforcement OFF for that one INSERT -- the only
        # way to construct, in a fixture, a state that current schema
        # invariants otherwise make unreachable through any real write path.
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
                     "VALUES ('pipe-llama-1', 'anaxi_orchestration_lineage_a', 'llama', 'test')")
        host_actor_id4 = _seed_actors(conn)
        conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-real', 'pipe-llama-1', 100, NULL)")
        conn.commit()
        conn.close()

        # foreign_keys is a no-op mid-transaction -- must be set on a fresh
        # connection with nothing pending, before the INSERT that needs it off.
        conn = sqlite3.connect(prov4)
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
                     "VALUES ('ac-dangling', 'sess-does-not-exist', 'unknown', 100)")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(prov4)
        conn.execute("PRAGMA foreign_keys = ON;")
        _insert_event(conn, "evt-dangling", "pipe-llama-1", "ac-dangling", occurred_at=150)
        _insert_bc_component(conn, "evt-dangling", host_actor_id4, "Saved: dangling session test.")
        conn.commit()
        conn.close()

        dangling_conn = sqlite3.connect(prov4)
        check("6. ingestion (hippocampus_store): a dangling canonical session_id raises "
              "HippocampusIntegrityError, never silently falls back to temporal inference",
              raises(lambda: hs._derive_session(dangling_conn, "evt-dangling", "pipe-llama-1", 150, False),
                     (hs.HippocampusIntegrityError,)))

        session_id, resolution, integrity_ok, reason = audit._derive_session(dangling_conn, "evt-dangling", "pipe-llama-1", 150, False)
        check("6. standing audit: independently detects the same dangling reference without raising "
              "(an audit counts every problem in one pass, it does not abort on the first one)",
              integrity_ok is False and reason is not None and session_id is None)

        # =====================================================================
        # 5. Historical material: no invented native session, S1 unchanged.
        # =====================================================================
        check("5. historical event -> (None, 'unknown') regardless of any canonical link that would "
              "otherwise apply -- is_historical short-circuits before any lookup",
              hs._derive_session(dangling_conn, "evt-dangling", "pipe-llama-1", 150, True) == (None, "unknown"))
        dangling_conn.close()

        # =====================================================================
        # 7. Existing session metadata updates cleanly through normal sync --
        # simulating the exact live remediation path: a stale item, ingested
        # under the OLD temporal rule and stored as ambiguous, must be
        # corrected via session_updated on the next ordinary sync, never
        # treated as source drift.
        # =====================================================================
        d5 = os.path.join(root, "stale_migration")
        os.makedirs(d5)
        prov5 = os.path.join(d5, "anaxi_provenance.db")
        rel5 = os.path.join(d5, "anaxi_relational_llama.db")
        jsonl5 = os.path.join(d5, "anaxi_log.jsonl")
        hip5 = os.path.join(d5, "anaxi_hippocampus.db")
        conn = create_provenance_db(prov5)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
                     "VALUES ('pipe-llama-1', 'anaxi_orchestration_lineage_a', 'llama', 'test')")
        host_actor_id5 = _seed_actors(conn)
        conn.execute("INSERT INTO sessions (session_id, pipeline_id, started_at, ended_at) VALUES ('sess-stale', 'pipe-llama-1', 100, NULL)")
        conn.execute("INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) VALUES ('ac-stale', 'sess-stale', 'unknown', 100)")
        _insert_event(conn, "evt-stale", "pipe-llama-1", "ac-stale", occurred_at=150)
        _insert_bc_component(conn, "evt-stale", host_actor_id5, "Saved: stale migration test.")
        conn.commit()
        conn.close()
        rel_conn = sqlite3.connect(rel5)
        rel_conn.execute("""CREATE TABLE relational_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
            created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
            agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
            status TEXT NOT NULL DEFAULT 'recorded', event_id TEXT
        )""")
        rel_conn.commit()
        rel_conn.close()
        open(jsonl5, "w").close()
        hs.create_hippocampus_db(hip5).close()
        paths5 = hs.HippocampusPaths(prov5, rel5, jsonl5, hip5)
        hs.sync_hippocampus(hip5, paths5)
        # Simulate the exact live defect: hand-set this item's stored fields
        # to what the OLD temporal rule would have produced (ambiguous),
        # as if it had been ingested before S1 existed.
        hconn = sqlite3.connect(hip5)
        hconn.execute("UPDATE hippocampal_items SET session_id = NULL, session_resolution = 'ambiguous' WHERE event_id = 'evt-stale'")
        hconn.commit()
        hconn.close()
        check("7. pre-condition: stale item is stored as ambiguous (simulating pre-S1 ingestion)",
              _session_of(hip5, "evt-stale") == (None, "ambiguous"))

        sync_result5 = hs.sync_hippocampus(hip5, paths5)
        check("7. a normal re-sync corrects it via session_updated -- never raises "
              "HippocampusSourceDriftError, never treated as content/source drift",
              sync_result5.session_updated == [hs.derive_hippocampal_item_id("evt-stale", "event_components", str(_component_id_of(prov5, "evt-stale")))])
        check("7/8. after the ordinary sync, the item is correctly resolved to its canonical session",
              _session_of(hip5, "evt-stale") == ("sess-stale", "resolved"))
        result5 = run_audit(paths5)
        check("7. no content/source drift error occurred (sync completed and returned normally) and "
              "the audit now reports this item clean", result5.is_clean())

        print(f"\n{'='*70}\n{_PASS}/{_PASS + _FAIL} pass\n{'='*70}")
        return _FAIL == 0


def _session_of(hip_path, event_id):
    conn = sqlite3.connect(hip_path)
    row = conn.execute("SELECT session_id, session_resolution FROM hippocampal_items WHERE event_id = ?", (event_id,)).fetchone()
    conn.close()
    return tuple(row) if row else None


def _component_id_of(prov_path, event_id):
    conn = sqlite3.connect(prov_path)
    row = conn.execute("SELECT component_id FROM event_components WHERE event_id = ?", (event_id,)).fetchone()
    conn.close()
    return row[0]


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
