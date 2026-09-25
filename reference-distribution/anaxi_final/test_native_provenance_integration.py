"""
Anaxi -- Targeted unit/integration tests for the native (post-cutover)
provenance write path: native_turn_staging.py + native_provenance_writer.py.

Scope, deliberately: this suite exercises the two new modules in
isolation, against synthetic temp-directory fixtures -- it does NOT
touch any live file, does NOT set up the git worktree, does NOT wire
llama_anaxi.py/llama_sleep.py/anaxi_sleep.py, and does NOT run the
full copy-based cutover rehearsal. That wiring and its own tests are a
separate, later step, run only after this module's contract is proven
correct on its own.

Run:
    python test_native_provenance_integration.py
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

from provenance_schema import create_provenance_db, derive_stable_id
from migrate_historical_data import (
    build_pipeline_map, seed_reference_data,
    MigrationStopCondition, IncompleteEventBundleError, resolve_or_create_model_revision,
)
import native_turn_staging as staging
import native_provenance_writer as npw

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


SYNTHETIC_MANIFEST = {
    "pipelines": {
        "llama": {"routing_constant_value": "synthetic_user"},
        "claude": {"routing_constant_value": "synthetic_user"},
    }
}


def _fresh_fixture(tmp_root: str):
    """Builds a fresh, isolated synthetic data directory -- a plain
    temp directory, no marker file, no rehearsal-only aliasing guard --
    plus a seeded anaxi_provenance.db and a fresh staging file path.
    Returns (data_dir, pipeline_map, now, staging_path)."""
    data_dir = os.path.join(tmp_root, f"data_{os.urandom(4).hex()}")
    os.makedirs(data_dir)

    # create_provenance_db() RETURNS an open connection -- it does not
    # close it itself. Must be closed explicitly or Windows keeps the
    # file locked until garbage collection, breaking temp-dir cleanup.
    create_provenance_db(os.path.join(data_dir, "anaxi_provenance.db")).close()
    pipeline_map = build_pipeline_map(SYNTHETIC_MANIFEST)
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    conn.execute("PRAGMA foreign_keys = ON;")
    now = int(time.time())
    seed_reference_data(conn, pipeline_map, now)
    conn.close()

    staging_path = os.path.join(data_dir, "native_turn_staging.jsonl")
    return data_dir, pipeline_map, now, staging_path


def _base_turn_kwargs(session_id, session_started_at, occurred_at, pipeline_key, suffix=""):
    return dict(
        session_id=session_id,
        session_started_at=session_started_at,
        user_id="synthetic_rehearsal_identity",
        prompt=f"SYNTHETIC REHEARSAL TURN{suffix} -- no real content, testing the native write path.",
        bounded_clause="Nothing was created.",
        clark_prose=f"This is a synthetic reply{suffix}, not a real conversation.",
        kardia={"tone": "neutral", "note": "synthetic fixture"},
        controls={"temperature": 0.4, "top_p": 0.85, "style_instruction": "synthetic"},
        waking_model_tag="gemma4:e4b",
        pipeline_key=pipeline_key,
        artifact_pass_ran=False,
        occurred_at=occurred_at,
    )


def run_suite() -> bool:
    with tempfile.TemporaryDirectory(prefix="anaxi_native_prov_test_") as tmp_root:

        # ------------------------------------------------------------
        # 1. Two turns in one session share session_id/session_started_at
        #    but have distinct auth_context_id -- and each auth_context
        #    is referenced by exactly one event.
        # ------------------------------------------------------------
        data_dir, pipeline_map, now, staging_path = _fresh_fixture(tmp_root)
        pipeline_key = pipeline_map["llama"]["pipeline_key"]
        session_id = npw.generate_native_ulid()
        session_started_at = now

        turn1 = npw.stage_and_record_native_waking_turn(
            data_dir, staging_path,
            **_base_turn_kwargs(session_id, session_started_at, now, pipeline_key, " one"),
        )
        turn2 = npw.stage_and_record_native_waking_turn(
            data_dir, staging_path,
            **_base_turn_kwargs(session_id, session_started_at, now + 5, pipeline_key, " two"),
        )
        check("two turns in one session share session_id",
              turn1["session_id"] == turn2["session_id"] == session_id)
        check("two turns in one session have DIFFERENT auth_context_id",
              turn1["auth_context_id"] != turn2["auth_context_id"])
        check("two turns produced DIFFERENT event_id",
              turn1["event_id"] != turn2["event_id"])

        conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
        session_rows = conn.execute("SELECT COUNT(*) FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]
        check("exactly one sessions row exists despite two turns (resolve-or-create, not re-created)",
              session_rows == 1)
        started_at_row = conn.execute("SELECT started_at FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        check("sessions.started_at matches the staged session_started_at exactly",
              started_at_row[0] == session_started_at)
        auth_count_1 = conn.execute(
            "SELECT COUNT(*) FROM events WHERE auth_context_id = ?", (turn1["auth_context_id"],)
        ).fetchone()[0]
        auth_count_2 = conn.execute(
            "SELECT COUNT(*) FROM events WHERE auth_context_id = ?", (turn2["auth_context_id"],)
        ).fetchone()[0]
        check("each turn's auth_context is referenced by exactly one event (never shared)",
              auth_count_1 == 1 and auth_count_2 == 1)
        auth_state_rows = conn.execute(
            "SELECT auth_state, claimed_actor_id, authenticated_actor_id FROM auth_contexts "
            "WHERE auth_context_id IN (?, ?)", (turn1["auth_context_id"], turn2["auth_context_id"]),
        ).fetchall()
        check("both auth_contexts are auth_state='unknown' with no claimed/authenticated actor",
              all(r == ("unknown", None, None) for r in auth_state_rows))
        check("verify_native_bundle_contract confirms both turns",
              npw.verify_native_bundle_contract(conn, turn1["event_id"])
              and npw.verify_native_bundle_contract(conn, turn2["event_id"]))
        conn.close()

        # ------------------------------------------------------------
        # 2 & 3. staging_id derivation: non-circular, verified on read,
        # corrupted/mutated stored staging_id is rejected.
        # ------------------------------------------------------------
        data_dir2, pipeline_map2, now2, staging_path2 = _fresh_fixture(tmp_root)
        pipeline_key2 = pipeline_map2["llama"]["pipeline_key"]
        payload = {"a": 1, "b": "text", "session_id": "s1"}
        sid = staging.append_staging_entry(staging_path2, payload)
        check("appended staging entry's ID is recomputable from its own payload bytes",
              sid == staging.compute_staging_id(payload, 0))
        check("staging_id derivation rejects a payload that already contains staging_id (non-circular)",
              raises(lambda: staging.append_staging_entry(staging_path2, {**payload, "staging_id": "x"}),
                     (ValueError,)))
        entries = list(staging.read_verified_staging_entries(staging_path2))
        check("read_verified_staging_entries returns the one written entry, verified",
              len(entries) == 1 and entries[0][1] == payload and entries[0][2] == sid)

        # Corrupt the stored staging_id in place.
        with open(staging_path2, "r", encoding="utf-8") as f:
            lines = f.readlines()
        record = json.loads(lines[0])
        record["staging_id"] = "corrupted" + record["staging_id"][9:]
        with open(staging_path2, "w", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        check("a corrupted/mutated stored staging_id is rejected on read",
              raises(lambda: list(staging.read_verified_staging_entries(staging_path2)),
                     (staging.StagingIntegrityError,)))

        # ------------------------------------------------------------
        # 4. Changing ANY canonical-payload field changes/breaks the ID.
        # ------------------------------------------------------------
        base_payload = {"x": "one", "y": 2, "z": [1, 2, 3]}
        base_id = staging.compute_staging_id(base_payload, 0)
        for mutated in (
            {**base_payload, "x": "ONE"},
            {**base_payload, "y": 3},
            {**base_payload, "z": [1, 2, 4]},
            {**base_payload, "new_field": "added"},
        ):
            check(f"mutated payload field {list(set(mutated) ^ set(base_payload)) or 'value'} changes staging_id",
                  staging.compute_staging_id(mutated, 0) != base_id)
        check("same payload, different line_index, changes staging_id (domain separation)",
              staging.compute_staging_id(base_payload, 1) != base_id)

        # ------------------------------------------------------------
        # 5. fsync failure prevents ALL canonical writes (and, by
        # construction, all legacy writes, since those only ever run
        # after a canonical commit that never happens).
        # ------------------------------------------------------------
        data_dir3, pipeline_map3, now3, staging_path3 = _fresh_fixture(tmp_root)
        pipeline_key3 = pipeline_map3["llama"]["pipeline_key"]
        real_fsync = os.fsync

        # Fail only the FIRST fsync (the real append's) -- the hardened
        # append_staging_entry() now attempts an ftruncate-rollback after
        # a failed write/fsync, fsync'ing the rollback itself before
        # raising. A mock that fails EVERY fsync call would also fail
        # that rollback fsync and produce StagingIndeterminateStateError
        # instead -- a mock artifact, not the disk-full-then-recovered
        # scenario this test actually intends (one bad fsync, disk fine
        # again immediately after).
        fsync_call_count = {"n": 0}

        def _raising_fsync(fd):
            fsync_call_count["n"] += 1
            if fsync_call_count["n"] == 1:
                raise OSError("simulated: disk full / fsync failure")
            return real_fsync(fd)

        os.fsync = _raising_fsync
        try:
            check("stage_and_record_native_waking_turn raises StagingDurabilityError when fsync fails",
                  raises(lambda: npw.stage_and_record_native_waking_turn(
                      data_dir3, staging_path3,
                      **_base_turn_kwargs(npw.generate_native_ulid(), now3, now3, pipeline_key3)),
                      (staging.StagingDurabilityError,)))
        finally:
            os.fsync = real_fsync
        conn3 = sqlite3.connect(os.path.join(data_dir3, "anaxi_provenance.db"))
        events_after_fsync_failure = conn3.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        sessions_after_fsync_failure = conn3.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        check("a failed staging fsync leaves ZERO events rows -- no canonical transaction began",
              events_after_fsync_failure == 0)
        check("a failed staging fsync leaves ZERO sessions rows -- no canonical transaction began",
              sessions_after_fsync_failure == 0)
        conn3.close()

        # ------------------------------------------------------------
        # 6 & 7. Canonical transaction failure (during session creation
        # in that SAME transaction) leaves no turn-specific bundle at
        # all; retry from the staged envelope reproduces the identical
        # event_id.
        # ------------------------------------------------------------
        data_dir4, pipeline_map4, now4, staging_path4 = _fresh_fixture(tmp_root)
        pipeline_key4 = pipeline_map4["llama"]["pipeline_key"]

        event_id4 = npw.generate_native_ulid()
        auth_context_id4 = npw.generate_native_ulid()
        session_id4 = npw.generate_native_ulid()
        turn_kwargs4 = _base_turn_kwargs(session_id4, now4, now4, pipeline_key4)
        staging_payload4 = {
            "session_id": session_id4, "session_started_at": now4, "event_id": event_id4,
            "auth_context_id": auth_context_id4, "user_id": turn_kwargs4["user_id"],
            "prompt": turn_kwargs4["prompt"], "bounded_clause": turn_kwargs4["bounded_clause"],
            "clark_prose": turn_kwargs4["clark_prose"], "kardia": turn_kwargs4["kardia"],
            "controls": turn_kwargs4["controls"], "waking_model_tag": turn_kwargs4["waking_model_tag"],
            "pipeline_key": pipeline_key4, "artifact_pass_ran": False, "occurred_at": now4,
        }
        staging_id4 = staging.append_staging_entry(staging_path4, staging_payload4)

        real_sha256 = hashlib.sha256
        call_count = {"n": 0}

        def _failing_sha256(data=b""):
            call_count["n"] += 1
            if call_count["n"] == 2:  # first call = bounded_clause hash (ok); second = clark_prose hash (fails)
                raise RuntimeError("simulated mid-transaction failure")
            return real_sha256(data)

        hashlib.sha256 = _failing_sha256
        try:
            check("a forced mid-transaction failure (during session-creation turn) raises",
                  raises(lambda: npw.record_native_waking_turn(
                      data_dir4,
                      event_id=event_id4, staging_id=staging_id4, session_id=session_id4,
                      session_started_at=now4, auth_context_id=auth_context_id4,
                      prompt=staging_payload4["prompt"], bounded_clause=staging_payload4["bounded_clause"],
                      clark_prose=staging_payload4["clark_prose"], pipeline_key=pipeline_key4,
                      waking_model_tag=staging_payload4["waking_model_tag"], artifact_pass_ran=False,
                      occurred_at=now4),
                      (RuntimeError,)))
        finally:
            hashlib.sha256 = real_sha256

        conn4 = sqlite3.connect(os.path.join(data_dir4, "anaxi_provenance.db"))
        check("after rollback: ZERO sessions rows (session creation was inside the failed transaction)",
              conn4.execute("SELECT COUNT(*) FROM sessions WHERE session_id = ?", (session_id4,)).fetchone()[0] == 0)
        check("after rollback: ZERO auth_contexts rows for this turn",
              conn4.execute("SELECT COUNT(*) FROM auth_contexts WHERE auth_context_id = ?", (auth_context_id4,)).fetchone()[0] == 0)
        check("after rollback: ZERO events rows for this turn",
              conn4.execute("SELECT COUNT(*) FROM events WHERE event_id = ?", (event_id4,)).fetchone()[0] == 0)
        conn4.close()

        # Retry: same staged IDs, no failure injected this time.
        retry_result = npw.record_native_waking_turn(
            data_dir4,
            event_id=event_id4, staging_id=staging_id4, session_id=session_id4,
            session_started_at=now4, auth_context_id=auth_context_id4,
            prompt=staging_payload4["prompt"], bounded_clause=staging_payload4["bounded_clause"],
            clark_prose=staging_payload4["clark_prose"], pipeline_key=pipeline_key4,
            waking_model_tag=staging_payload4["waking_model_tag"], artifact_pass_ran=False,
            occurred_at=now4,
        )
        check("retry after pre-commit failure reproduces the EXACT staged event_id",
              retry_result["event_id"] == event_id4)
        check("retry after pre-commit failure reproduces the EXACT staged auth_context_id",
              retry_result["auth_context_id"] == auth_context_id4)
        check("retry after pre-commit failure reproduces the EXACT staged session_id",
              retry_result["session_id"] == session_id4)

        conn4b = sqlite3.connect(os.path.join(data_dir4, "anaxi_provenance.db"))
        check("retry succeeded: exactly one sessions row now exists with the staged started_at",
              conn4b.execute("SELECT started_at FROM sessions WHERE session_id = ?", (session_id4,)).fetchone() == (now4,))
        check("retry succeeded: verify_native_bundle_contract confirms the resumed event",
              npw.verify_native_bundle_contract(conn4b, event_id4))

        # Idempotent double-retry: calling again must not duplicate anything.
        retry_result_2 = npw.record_native_waking_turn(
            data_dir4,
            event_id=event_id4, staging_id=staging_id4, session_id=session_id4,
            session_started_at=now4, auth_context_id=auth_context_id4,
            prompt=staging_payload4["prompt"], bounded_clause=staging_payload4["bounded_clause"],
            clark_prose=staging_payload4["clark_prose"], pipeline_key=pipeline_key4,
            waking_model_tag=staging_payload4["waking_model_tag"], artifact_pass_ran=False,
            occurred_at=now4,
        )
        check("a second call with the same event_id is idempotent (resume check, no duplicate rows)",
              retry_result_2["event_id"] == event_id4
              and conn4b.execute("SELECT COUNT(*) FROM events WHERE event_id = ?", (event_id4,)).fetchone()[0] == 1)
        check("idempotent resume also reproduces the exact session/auth_context IDs",
              retry_result_2["session_id"] == session_id4 and retry_result_2["auth_context_id"] == auth_context_id4)
        conn4b.close()

        # ------------------------------------------------------------
        # 6b (crash-recovery variant, using resume_orphaned_staged_turns
        # end-to-end): stage a turn, DON'T commit it (simulate crash by
        # never calling record_native_waking_turn), then recover.
        # ------------------------------------------------------------
        data_dir5, pipeline_map5, now5, staging_path5 = _fresh_fixture(tmp_root)
        pipeline_key5 = pipeline_map5["llama"]["pipeline_key"]
        event_id5 = npw.generate_native_ulid()
        auth_context_id5 = npw.generate_native_ulid()
        session_id5 = npw.generate_native_ulid()
        payload5 = {
            "session_id": session_id5, "session_started_at": now5, "event_id": event_id5,
            "auth_context_id": auth_context_id5, "user_id": "synthetic_rehearsal_identity",
            "prompt": "SYNTHETIC REHEARSAL TURN -- orphan recovery case.",
            "bounded_clause": "Nothing was created.", "clark_prose": "Synthetic recovered reply.",
            "kardia": {"note": "synthetic"}, "controls": {"temperature": 0.4, "top_p": 0.85, "style_instruction": "s"},
            "waking_model_tag": "gemma4:e4b", "pipeline_key": pipeline_key5,
            "artifact_pass_ran": False, "occurred_at": now5,
        }
        staging.append_staging_entry(staging_path5, payload5)  # staged, never committed -- simulated crash

        conn5_pre = sqlite3.connect(os.path.join(data_dir5, "anaxi_provenance.db"))
        check("orphan case: staged-but-never-committed turn leaves zero rows before recovery",
              conn5_pre.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0)
        conn5_pre.close()

        recovered = npw.resume_orphaned_staged_turns(data_dir5, staging_path5)
        check("resume_orphaned_staged_turns recovers exactly one turn",
              len(recovered) == 1)
        check("recovered turn preserves the exact staged event_id/auth_context_id/session_id",
              recovered[0]["event_id"] == event_id5
              and recovered[0]["auth_context_id"] == auth_context_id5
              and recovered[0]["session_id"] == session_id5)
        conn5_post = sqlite3.connect(os.path.join(data_dir5, "anaxi_provenance.db"))
        check("recovered turn's session_started_at matches the staged value",
              conn5_post.execute("SELECT started_at FROM sessions WHERE session_id = ?", (session_id5,)).fetchone()[0] == now5)
        conn5_post.close()

        # ------------------------------------------------------------
        # 8. Reconciliation operates solely from durable staged/
        # canonical information and is idempotent.
        # ------------------------------------------------------------
        rel_db_path = os.path.join(data_dir5, "synthetic_relational.db")
        mind_db_path = os.path.join(data_dir5, "synthetic_mind.db")
        jsonl_path = os.path.join(data_dir5, "synthetic_anaxi_log.jsonl")

        rel_conn = sqlite3.connect(rel_db_path)
        rel_conn.execute("""
            CREATE TABLE relational_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
                created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
                agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
                status TEXT NOT NULL DEFAULT 'recorded',
                model_revision_id TEXT, pipeline_id TEXT, epoch_id TEXT, auth_context_id TEXT, event_id TEXT
            )
        """)
        rel_conn.commit()
        rel_conn.close()

        mind_conn = sqlite3.connect(mind_db_path)
        mind_conn.execute("""
            CREATE TABLE turn_generation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, timestamp INTEGER,
                temperature REAL, top_p REAL, style_instruction TEXT,
                model_revision_id TEXT, pipeline_id TEXT, epoch_id TEXT, event_id TEXT
            )
        """)
        mind_conn.commit()
        mind_conn.close()

        report1 = npw.reconcile_native_turn(
            data_dir5, staging_path5, event_id5, rel_db_path, mind_db_path, jsonl_path,
        )
        check("reconciliation repairs all three legacy stores from staged/canonical info alone",
              report1 == {"turn_generation_log": "repaired", "anaxi_log_jsonl": "repaired", "relational_events": "repaired"})

        mind_conn2 = sqlite3.connect(mind_db_path)
        tgl_row = mind_conn2.execute(
            "SELECT user_id, temperature, top_p, style_instruction, event_id FROM turn_generation_log WHERE event_id=?",
            (event_id5,),
        ).fetchone()
        check("reconciled turn_generation_log row has the staged generation controls",
              tgl_row == ("synthetic_rehearsal_identity", 0.4, 0.85, "s", event_id5))
        mind_conn2.close()

        with open(jsonl_path, "r", encoding="utf-8") as f:
            jsonl_line = json.loads(f.readline())
        check("reconciled anaxi_log.jsonl line has the reassembled reply and staged kardia",
              jsonl_line["response"] == "Nothing was created. Synthetic recovered reply."
              and jsonl_line["kardia"] == {"note": "synthetic"}
              and jsonl_line["event_id"] == event_id5)

        rel_conn2 = sqlite3.connect(rel_db_path)
        rel_row = rel_conn2.execute(
            "SELECT external_observation, agent_response, auth_context_id, event_id FROM relational_events WHERE event_id=?",
            (event_id5,),
        ).fetchone()
        check("reconciled relational_events row has the staged prompt/reply and this turn's own auth_context_id",
              rel_row == (payload5["prompt"], "Nothing was created. Synthetic recovered reply.", auth_context_id5, event_id5))
        rel_conn2.close()

        report2 = npw.reconcile_native_turn(
            data_dir5, staging_path5, event_id5, rel_db_path, mind_db_path, jsonl_path,
        )
        check("a second reconciliation call is idempotent -- reports already_present, no duplicates",
              report2 == {"turn_generation_log": "already_present", "anaxi_log_jsonl": "already_present", "relational_events": "already_present"})

        rel_conn3 = sqlite3.connect(rel_db_path)
        check("idempotent reconciliation did not duplicate the relational_events row",
              rel_conn3.execute("SELECT COUNT(*) FROM relational_events WHERE event_id=?", (event_id5,)).fetchone()[0] == 1)
        rel_conn3.close()
        with open(jsonl_path, "r", encoding="utf-8") as f:
            jsonl_line_count = sum(1 for line in f if line.strip())
        check("idempotent reconciliation did not duplicate the anaxi_log.jsonl line",
              jsonl_line_count == 1)

        check("reconciliation refuses to operate on an incomplete/nonexistent canonical bundle",
              raises(lambda: npw.reconcile_native_turn(
                  data_dir5, staging_path5, "nonexistent-event-id",
                  rel_db_path, mind_db_path, jsonl_path),
                  (IncompleteEventBundleError,)))

        # ------------------------------------------------------------
        # 9. Staging durability: short writes are recovered from, a
        # zero/non-progress write is a hard failure, and neither case
        # permits any canonical work before staging succeeds completely.
        # ------------------------------------------------------------
        staging_short_path = os.path.join(tmp_root, "short_write_staging.jsonl")
        real_os_write = os.write
        short_write_calls = {"n": 0}

        def _short_first_write(fd, data):
            short_write_calls["n"] += 1
            if short_write_calls["n"] == 1 and len(data) > 5:
                return real_os_write(fd, data[:5])  # short write: only 5 bytes land
            return real_os_write(fd, data)

        os.write = _short_first_write
        try:
            payload9a = {"x": "short-write recovery test"}
            staging_id_9a = staging.append_staging_entry(staging_short_path, payload9a)
        finally:
            os.write = real_os_write
        check("a short first os.write() followed by successful completion still produces "
              "a byte-identical, fully-written staging line",
              short_write_calls["n"] > 1)
        recovered_9a = list(staging.read_verified_staging_entries(staging_short_path))
        check("the short-write-recovered staging entry reads back correctly and verifies",
              len(recovered_9a) == 1 and recovered_9a[0][1] == payload9a and recovered_9a[0][2] == staging_id_9a)

        def _zero_progress_write(fd, data):
            return 0

        os.write = _zero_progress_write
        try:
            check("a zero/non-progress os.write() raises StagingDurabilityError, not silently "
                  "reported as success",
                  raises(lambda: staging.append_staging_entry(
                      staging_short_path, {"x": "zero write test"}),
                      (staging.StagingDurabilityError,)))
        finally:
            os.write = real_os_write

        # Neither case permits canonical work before staging succeeds
        # completely: end-to-end, a zero-write failure during
        # stage_and_record_native_waking_turn must leave the canonical
        # DB completely untouched (mirrors test 5's fsync-failure
        # check, now for the write step itself rather than fsync).
        data_dir9, pipeline_map9, now9, staging_path9 = _fresh_fixture(tmp_root)
        pipeline_key9 = pipeline_map9["llama"]["pipeline_key"]
        os.write = _zero_progress_write
        try:
            check("stage_and_record_native_waking_turn raises StagingDurabilityError on a "
                  "zero-progress write",
                  raises(lambda: npw.stage_and_record_native_waking_turn(
                      data_dir9, staging_path9,
                      **_base_turn_kwargs(npw.generate_native_ulid(), now9, now9, pipeline_key9)),
                      (staging.StagingDurabilityError,)))
        finally:
            os.write = real_os_write
        conn9 = sqlite3.connect(os.path.join(data_dir9, "anaxi_provenance.db"))
        check("a zero-progress staging write leaves ZERO events rows -- no canonical "
              "transaction began, same guarantee as the fsync-failure case",
              conn9.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0)
        conn9.close()

        # os.write() raising an actual OSError outright (not merely
        # returning a short/zero count) must ALSO be wrapped as
        # StagingDurabilityError -- a bare OSError propagating instead
        # would not be recognized by callers checking specifically for
        # the durability-failure type.
        def _raising_os_write(fd, data):
            raise OSError("simulated: I/O error mid-write")

        os.write = _raising_os_write
        try:
            check("an actual OSError raised BY os.write() (not just a short/zero return) is "
                  "wrapped as StagingDurabilityError, not left as a bare OSError",
                  raises(lambda: staging.append_staging_entry(
                      staging_short_path, {"x": "os.write() OSError test"}),
                      (staging.StagingDurabilityError,)))
        finally:
            os.write = real_os_write

        # ------------------------------------------------------------
        # 10. verify_native_bundle_contract(): exact verification, not
        # shape-only. Corruption tests analogous to
        # verify_historical_bundle_contract()'s own: wrong event type,
        # wrong staging/input reference, wrong participation
        # note/tag under otherwise-valid IDs, wrong component
        # actor/kind/hash/span/model. Each must hard-stop.
        # ------------------------------------------------------------
        data_dir10, pipeline_map10, now10, staging_path10 = _fresh_fixture(tmp_root)
        pipeline_key10 = pipeline_map10["llama"]["pipeline_key"]
        turn10 = npw.stage_and_record_native_waking_turn(
            data_dir10, staging_path10,
            **_base_turn_kwargs(npw.generate_native_ulid(), now10, now10, pipeline_key10, " ten"),
        )
        conn10 = sqlite3.connect(os.path.join(data_dir10, "anaxi_provenance.db"))
        conn10.execute("PRAGMA foreign_keys = ON;")

        def _expected10():
            return {
                "staging_id": turn10["staging_id"], "session_id": turn10["session_id"],
                "session_started_at": now10, "auth_context_id": turn10["auth_context_id"],
                "pipeline_id": turn10["pipeline_id"], "occurred_at": now10,
                "bounded_clause": "Nothing was created.", "clark_prose": "This is a synthetic reply ten, not a real conversation.",
                "artifact_pass_ran": False, "waking_model_tag": "gemma4:e4b",
            }

        check("verify_native_bundle_contract with the CORRECT expected contract confirms the turn",
              npw.verify_native_bundle_contract(conn10, turn10["event_id"], expected=_expected10()))

        check("wrong expected occurred_at is rejected",
              raises(lambda: npw.verify_native_bundle_contract(
                  conn10, turn10["event_id"], expected={**_expected10(), "occurred_at": now10 + 999}),
                  (MigrationStopCondition,)))
        check("wrong expected auth_context_id is rejected",
              raises(lambda: npw.verify_native_bundle_contract(
                  conn10, turn10["event_id"], expected={**_expected10(), "auth_context_id": npw.generate_native_ulid()}),
                  (MigrationStopCondition,)))
        check("wrong expected staging_id/input_source_ref is rejected",
              raises(lambda: npw.verify_native_bundle_contract(
                  conn10, turn10["event_id"], expected={**_expected10(), "staging_id": "wrong-staging-id"}),
                  (MigrationStopCondition,)))
        check("wrong expected session_started_at is rejected",
              raises(lambda: npw.verify_native_bundle_contract(
                  conn10, turn10["event_id"], expected={**_expected10(), "session_started_at": now10 + 999}),
                  (MigrationStopCondition,)))
        check("wrong expected artifact_pass_ran (implying wrong participation cardinality/notes) is rejected",
              raises(lambda: npw.verify_native_bundle_contract(
                  conn10, turn10["event_id"], expected={**_expected10(), "artifact_pass_ran": True}),
                  (MigrationStopCondition,)))
        check("wrong expected bounded_clause (implying wrong component text/hash/span) is rejected",
              raises(lambda: npw.verify_native_bundle_contract(
                  conn10, turn10["event_id"], expected={**_expected10(), "bounded_clause": "A different bounded clause."}),
                  (MigrationStopCondition,)))
        check("wrong expected waking_model_tag (implying wrong model_revision_id) is rejected",
              raises(lambda: npw.verify_native_bundle_contract(
                  conn10, turn10["event_id"], expected={**_expected10(), "waking_model_tag": "a-different-tag"}),
                  (MigrationStopCondition,)))

        conn10.close()

        # --- Direct row-level corruption, built FROM SCRATCH (every
        # provenance table here is append-only -- no UPDATE trigger
        # permits it -- so each corrupted bundle must be inserted with
        # exactly one field wrong from the start, never
        # insert-correctly-then-corrupt). Mirrors
        # test_provenance_schema.py's own established pattern for this
        # exact reason. ---
        def _insert_native_bundle_from_scratch(
            conn, *, event_id, staging_id, session_id, session_started_at, auth_context_id,
            pipeline_id, occurred_at, bounded_clause, clark_prose, waking_model_tag,
            event_type="waking_turn", participation_note_seq1=None,
            component_creator_actor_id_seq1=None, component_kind_seq1=None,
            component_sha256_seq1=None, component_span_end_seq1=None,
            component_model_revision_id_seq1=None, participation_model_revision_id_override=None,
        ):
            conn.execute(
                "INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES (?, ?, ?)",
                (session_id, pipeline_id, session_started_at),
            )
            conn.execute(
                "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
                "VALUES (?, ?, 'unknown', ?)",
                (auth_context_id, session_id, occurred_at),
            )
            conn.execute(
                "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
                "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
                "VALUES (?, ?, ?, 'known', NULL, ?, ?, ?, ?)",
                (event_id, event_type, pipeline_id, auth_context_id, staging_id, occurred_at, int(time.time())),
            )
            note = participation_note_seq1 or "Model backing this native waking turn's pass-2 conversational reply."
            if participation_model_revision_id_override is not None:
                # digest_uniqueness_key is UNIQUE on (tag, digest-or-
                # '__NO_DIGEST__') -- two 'tag_only_degraded' rows for
                # the SAME tag can never coexist, so the real
                # deterministic row is deliberately NOT created here.
                # Insert exactly one row -- correct tag/identity_confidence,
                # but under a manually-chosen (non-deterministic) ID --
                # bypassing resolve_or_create_model_revision()'s
                # determinism entirely, to prove the participation-ID
                # check catches this even with no competing row and no
                # UNIQUE-constraint assistance.
                conn.execute(
                    "INSERT INTO model_revisions (model_revision_id, tag, identity_confidence, "
                    "first_observed_at) VALUES (?, ?, 'tag_only_degraded', ?)",
                    (participation_model_revision_id_override, waking_model_tag, occurred_at),
                )
                model_revision_id = participation_model_revision_id_override
                participation_model_revision_id = participation_model_revision_id_override
            else:
                model_revision_id = resolve_or_create_model_revision(conn, waking_model_tag, occurred_at)
                participation_model_revision_id = model_revision_id
            conn.execute(
                "INSERT INTO event_model_participation (event_id, model_revision_id, participation_note) "
                "VALUES (?, ?, ?)",
                (event_id, participation_model_revision_id, note),
            )
            host_actor_id = derive_stable_id("actor", "bounded_clause_renderer")
            clark_actor_id = derive_stable_id("actor", "clark")
            conn.execute(
                "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                "authorship_resolution, component_text, content_sha256, model_revision_id, "
                "span_start, span_end) VALUES (?, 0, ?, 'bounded_clause', 'resolved', ?, ?, NULL, 0, ?)",
                (event_id, host_actor_id, bounded_clause,
                 hashlib.sha256(bounded_clause.encode("utf-8")).hexdigest(), len(bounded_clause)),
            )
            if clark_prose:
                span_start = len(bounded_clause) + 1
                conn.execute(
                    "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                    "authorship_resolution, component_text, content_sha256, model_revision_id, "
                    "span_start, span_end) VALUES (?, 1, ?, ?, 'resolved', ?, ?, ?, ?, ?)",
                    (event_id,
                     component_creator_actor_id_seq1 or clark_actor_id,
                     component_kind_seq1 or "conversational_prose",
                     clark_prose,
                     component_sha256_seq1 or hashlib.sha256(clark_prose.encode("utf-8")).hexdigest(),
                     component_model_revision_id_seq1 if component_model_revision_id_seq1 is not None else model_revision_id,
                     span_start,
                     component_span_end_seq1 if component_span_end_seq1 is not None else span_start + len(clark_prose)),
                )
            conn.commit()

        def _fresh_bundle_expected(data_dir, pipeline_map, now, **overrides):
            event_id = npw.generate_native_ulid()
            staging_id = "test-staging-id-" + event_id
            session_id = npw.generate_native_ulid()
            auth_context_id = npw.generate_native_ulid()
            pipeline_id = pipeline_map["llama"]["pipeline_id"]
            bounded_clause = "Nothing was created."
            clark_prose = "This is a synthetic corruption-test reply."
            conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
            conn.execute("PRAGMA foreign_keys = ON;")
            _insert_native_bundle_from_scratch(
                conn, event_id=event_id, staging_id=staging_id, session_id=session_id,
                session_started_at=now, auth_context_id=auth_context_id, pipeline_id=pipeline_id,
                occurred_at=now, bounded_clause=bounded_clause, clark_prose=clark_prose,
                waking_model_tag="gemma4:e4b", **overrides,
            )
            expected = {
                "staging_id": staging_id, "session_id": session_id, "session_started_at": now,
                "auth_context_id": auth_context_id, "pipeline_id": pipeline_id, "occurred_at": now,
                "bounded_clause": bounded_clause, "clark_prose": clark_prose,
                "artifact_pass_ran": False, "waking_model_tag": "gemma4:e4b",
            }
            return conn, event_id, expected

        rd_ctrl, pm_ctrl, now_ctrl, _sp_ctrl = _fresh_fixture(tmp_root)
        conn_ctrl, event_id_ctrl, expected_ctrl = _fresh_bundle_expected(rd_ctrl, pm_ctrl, now_ctrl)
        check("control: a from-scratch, entirely-correct bundle passes exact verification",
              npw.verify_native_bundle_contract(conn_ctrl, event_id_ctrl, expected=expected_ctrl))
        conn_ctrl.close()

        rd_a, pm_a, now_a, _sp_a = _fresh_fixture(tmp_root)
        conn_a, event_id_a, expected_a = _fresh_bundle_expected(rd_a, pm_a, now_a, event_type="wrong_type")
        check("a wrong events.event_type is rejected",
              raises(lambda: npw.verify_native_bundle_contract(conn_a, event_id_a, expected=expected_a),
                     (MigrationStopCondition,)))
        conn_a.close()

        rd_b, pm_b, now_b, _sp_b = _fresh_fixture(tmp_root)
        conn_b, event_id_b, expected_b = _fresh_bundle_expected(
            rd_b, pm_b, now_b, participation_note_seq1="a wrong participation note")
        check("a wrong event_model_participation.participation_note under otherwise-valid IDs is rejected",
              raises(lambda: npw.verify_native_bundle_contract(conn_b, event_id_b, expected=expected_b),
                     (MigrationStopCondition,)))
        conn_b.close()

        rd_c, pm_c, now_c, _sp_c = _fresh_fixture(tmp_root)
        # Must be a REAL actor_id (event_components.creator_actor_id is a
        # genuine, FK-enforced reference within anaxi_provenance.db) --
        # the host actor is real but WRONG for a model-authored (sequence=1)
        # component, which is exactly the corruption this proves is caught.
        wrong_but_real_actor_id = derive_stable_id("actor", "bounded_clause_renderer")
        conn_c, event_id_c, expected_c = _fresh_bundle_expected(
            rd_c, pm_c, now_c, component_creator_actor_id_seq1=wrong_but_real_actor_id)
        check("a wrong (but real) event_components.creator_actor_id (sequence=1) is rejected",
              raises(lambda: npw.verify_native_bundle_contract(conn_c, event_id_c, expected=expected_c),
                     (MigrationStopCondition,)))
        conn_c.close()

        rd_d, pm_d, now_d, _sp_d = _fresh_fixture(tmp_root)
        conn_d, event_id_d, expected_d = _fresh_bundle_expected(
            rd_d, pm_d, now_d, component_kind_seq1="wrong_kind")
        check("a wrong event_components.component_kind (sequence=1) is rejected",
              raises(lambda: npw.verify_native_bundle_contract(conn_d, event_id_d, expected=expected_d),
                     (MigrationStopCondition,)))
        conn_d.close()

        rd_e, pm_e, now_e, _sp_e = _fresh_fixture(tmp_root)
        conn_e, event_id_e, expected_e = _fresh_bundle_expected(
            rd_e, pm_e, now_e, component_sha256_seq1="0" * 64)
        check("a wrong event_components.content_sha256 (sequence=1) is rejected",
              raises(lambda: npw.verify_native_bundle_contract(conn_e, event_id_e, expected=expected_e),
                     (MigrationStopCondition,)))
        conn_e.close()

        rd_f, pm_f, now_f, _sp_f = _fresh_fixture(tmp_root)
        conn_f, event_id_f, expected_f = _fresh_bundle_expected(
            rd_f, pm_f, now_f, component_span_end_seq1=999999)
        check("a wrong event_components.span_end (sequence=1) is rejected",
              raises(lambda: npw.verify_native_bundle_contract(conn_f, event_id_f, expected=expected_f),
                     (MigrationStopCondition,)))
        conn_f.close()

        rd_g, pm_g, now_g, _sp_g = _fresh_fixture(tmp_root)
        conn_g_pre = sqlite3.connect(os.path.join(rd_g, "anaxi_provenance.db"))
        conn_g_pre.execute("PRAGMA foreign_keys = ON;")
        fake_model_revision_id = npw.generate_native_ulid()
        conn_g_pre.execute(
            "INSERT INTO model_revisions (model_revision_id, tag, identity_confidence, first_observed_at) "
            "VALUES (?, 'a-decoy-tag', 'tag_only_degraded', ?)",
            (fake_model_revision_id, now_g),
        )
        conn_g_pre.commit()
        conn_g_pre.close()
        conn_g, event_id_g, expected_g = _fresh_bundle_expected(
            rd_g, pm_g, now_g, component_model_revision_id_seq1=fake_model_revision_id)
        check("a wrong event_components.model_revision_id (sequence=1, pointing at a real but "
              "unrelated model_revisions row) is rejected",
              raises(lambda: npw.verify_native_bundle_contract(conn_g, event_id_g, expected=expected_g),
                     (MigrationStopCondition,)))
        conn_g.close()

        # Decoy participation model_revision_id: a model_revisions row
        # carrying the CORRECT tag/identity_confidence but under a
        # manually-chosen, non-deterministic ID (never created via
        # resolve_or_create_model_revision()) --
        # event_model_participation.model_revision_id must equal the
        # deterministic expected ID exactly, not merely reference a row
        # whose tag/confidence happen to match. Built with clark_prose=""
        # (no sequence=1 component at all) so this isolates the
        # participation-level check specifically -- a component-level
        # check on the same wrong ID would otherwise also legitimately
        # fail, muddying which check actually caught it. Also: the
        # schema's own UNIQUE(digest_uniqueness_key) forbids two
        # 'tag_only_degraded' rows for the same tag from coexisting, so
        # the real deterministic row is deliberately never created in
        # this fixture -- only the decoy is.
        rd_h, pm_h, now_h, _sp_h = _fresh_fixture(tmp_root)
        pipeline_key_h = pm_h["llama"]["pipeline_key"]
        event_id_h = npw.generate_native_ulid()
        staging_id_h = "test-staging-id-" + event_id_h
        session_id_h = npw.generate_native_ulid()
        auth_context_id_h = npw.generate_native_ulid()
        pipeline_id_h = pm_h["llama"]["pipeline_id"]
        bounded_clause_h = "Nothing was created."
        decoy_model_revision_id = npw.generate_native_ulid()
        conn_h = sqlite3.connect(os.path.join(rd_h, "anaxi_provenance.db"))
        conn_h.execute("PRAGMA foreign_keys = ON;")
        _insert_native_bundle_from_scratch(
            conn_h, event_id=event_id_h, staging_id=staging_id_h, session_id=session_id_h,
            session_started_at=now_h, auth_context_id=auth_context_id_h, pipeline_id=pipeline_id_h,
            occurred_at=now_h, bounded_clause=bounded_clause_h, clark_prose="",
            waking_model_tag="gemma4:e4b",
            participation_model_revision_id_override=decoy_model_revision_id,
        )
        expected_h = {
            "staging_id": staging_id_h, "session_id": session_id_h, "session_started_at": now_h,
            "auth_context_id": auth_context_id_h, "pipeline_id": pipeline_id_h, "occurred_at": now_h,
            "bounded_clause": bounded_clause_h, "clark_prose": "",
            "artifact_pass_ran": False, "waking_model_tag": "gemma4:e4b",
        }
        check("event_model_participation.model_revision_id pointing at a row with the CORRECT "
              "tag/identity_confidence but a manually-chosen, non-deterministic ID is rejected -- "
              "the ID itself must equal the deterministic expected value, not merely reference "
              "a row with matching tag/confidence (isolated via clark_prose='', no component-"
              "level check involved)",
              raises(lambda: npw.verify_native_bundle_contract(conn_h, event_id_h, expected=expected_h),
                     (MigrationStopCondition,)))
        conn_h.close()

        # ------------------------------------------------------------
        # 11. Adversarial reconciliation: a correct event_id but WRONG
        # contents in each legacy projection must be rejected, never
        # reported already_present. Duplicate matching rows must also
        # be rejected.
        # ------------------------------------------------------------
        data_dir11, pipeline_map11, now11, staging_path11 = _fresh_fixture(tmp_root)
        pipeline_key11 = pipeline_map11["llama"]["pipeline_key"]
        turn11 = npw.stage_and_record_native_waking_turn(
            data_dir11, staging_path11,
            **_base_turn_kwargs(npw.generate_native_ulid(), now11, now11, pipeline_key11, " eleven"),
        )
        event_id11 = turn11["event_id"]

        rel_db_path11 = os.path.join(data_dir11, "adv_relational.db")
        mind_db_path11 = os.path.join(data_dir11, "adv_mind.db")
        jsonl_path11 = os.path.join(data_dir11, "adv_anaxi_log.jsonl")

        def _fresh_legacy_dbs():
            for path in (rel_db_path11, mind_db_path11, jsonl_path11):
                if os.path.exists(path):
                    os.remove(path)
            rc = sqlite3.connect(rel_db_path11)
            rc.execute("""CREATE TABLE relational_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
                created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
                agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
                status TEXT NOT NULL DEFAULT 'recorded',
                model_revision_id TEXT, pipeline_id TEXT, epoch_id TEXT, auth_context_id TEXT, event_id TEXT)""")
            rc.commit(); rc.close()
            mc = sqlite3.connect(mind_db_path11)
            mc.execute("""CREATE TABLE turn_generation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, timestamp INTEGER,
                temperature REAL, top_p REAL, style_instruction TEXT,
                model_revision_id TEXT, pipeline_id TEXT, epoch_id TEXT, event_id TEXT)""")
            mc.commit(); mc.close()

        # 11a. turn_generation_log: correct event_id, wrong temperature.
        _fresh_legacy_dbs()
        mc = sqlite3.connect(mind_db_path11)
        mc.execute(
            "INSERT INTO turn_generation_log (user_id, timestamp, temperature, top_p, style_instruction, "
            "model_revision_id, pipeline_id, epoch_id, event_id) VALUES (?,?,?,?,?,?,?,NULL,?)",
            ("synthetic_rehearsal_identity", int(time.time()), 0.999, 0.85, "s", "modelrev-wrong",
             turn11["pipeline_id"], event_id11),
        )
        mc.commit(); mc.close()
        check("reconciliation REJECTS a turn_generation_log row with the correct event_id "
              "but wrong content (not reported already_present)",
              raises(lambda: npw.reconcile_native_turn(
                  data_dir11, staging_path11, event_id11,
                  rel_db_path11, mind_db_path11, jsonl_path11),
                  (MigrationStopCondition,)))

        # 11b. anaxi_log.jsonl: correct event_id, wrong response text.
        _fresh_legacy_dbs()
        with open(jsonl_path11, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": "2026-01-01T00:00:00+00:00", "substrate": "llama", "model": "gemma4:e4b",
                "waking_model_tag": "gemma4:e4b", "prompt": turn11["prompt"], "response": "A WRONG response.",
                "kardia": turn11["kardia"], "pipeline_id": turn11["pipeline_id"],
                "pipeline_provenance_status": "known", "epoch_id": None, "event_id": event_id11,
                "event_model_participation": turn11["model_participations"],
            }) + "\n")
        check("reconciliation REJECTS an anaxi_log.jsonl line with the correct event_id "
              "but wrong response text (not reported already_present)",
              raises(lambda: npw.reconcile_native_turn(
                  data_dir11, staging_path11, event_id11,
                  rel_db_path11, mind_db_path11, jsonl_path11),
                  (MigrationStopCondition,)))

        # 11c. relational_events: correct event_id, wrong agent_response.
        _fresh_legacy_dbs()
        rc = sqlite3.connect(rel_db_path11)
        rc.execute(
            "INSERT INTO relational_events (user_id, substrate, created_at, external_observation, "
            "agent_response, linked_memories, status, model_revision_id, pipeline_id, epoch_id, "
            "auth_context_id, event_id) VALUES (?,?,?,?,?,'[]','recorded',?,?,NULL,?,?)",
            ("synthetic_rehearsal_identity", "llama", int(time.time()), turn11["prompt"], "A WRONG reply.",
             None, turn11["pipeline_id"], turn11["auth_context_id"], event_id11),
        )
        rc.commit(); rc.close()
        check("reconciliation REJECTS a relational_events row with the correct event_id "
              "but wrong agent_response (not reported already_present)",
              raises(lambda: npw.reconcile_native_turn(
                  data_dir11, staging_path11, event_id11,
                  rel_db_path11, mind_db_path11, jsonl_path11),
                  (MigrationStopCondition,)))

        # 11d. Duplicate matching rows: two relational_events rows for the same event_id.
        _fresh_legacy_dbs()
        rc = sqlite3.connect(rel_db_path11)
        for _ in range(2):
            rc.execute(
                "INSERT INTO relational_events (user_id, substrate, created_at, external_observation, "
                "agent_response, linked_memories, status, model_revision_id, pipeline_id, epoch_id, "
                "auth_context_id, event_id) VALUES (?,?,?,?,?,'[]','recorded',?,?,NULL,?,?)",
                ("synthetic_rehearsal_identity", "llama", int(time.time()), turn11["prompt"],
                 turn11["reassembled_reply"], turn11["model_participations"][-1]["model_revision_id"],
                 turn11["pipeline_id"], turn11["auth_context_id"], event_id11),
            )
        rc.commit(); rc.close()
        check("reconciliation REJECTS duplicate matching relational_events rows for the "
              "same event_id (never picks one arbitrarily)",
              raises(lambda: npw.reconcile_native_turn(
                  data_dir11, staging_path11, event_id11,
                  rel_db_path11, mind_db_path11, jsonl_path11),
                  (MigrationStopCondition,)))

        # 11e. A correct, matching row IS accepted as already_present (control case,
        # confirming the above are true rejections, not a blanket-always-fail bug).
        _fresh_legacy_dbs()
        report11e = npw.reconcile_native_turn(
            data_dir11, staging_path11, event_id11,
            rel_db_path11, mind_db_path11, jsonl_path11,
        )
        check("control case: reconciliation against fresh, empty legacy stores repairs all three",
              report11e == {"turn_generation_log": "repaired", "anaxi_log_jsonl": "repaired", "relational_events": "repaired"})
        report11e_2 = npw.reconcile_native_turn(
            data_dir11, staging_path11, event_id11,
            rel_db_path11, mind_db_path11, jsonl_path11,
        )
        check("control case: a second call against the now-correct rows reports already_present",
              report11e_2 == {"turn_generation_log": "already_present", "anaxi_log_jsonl": "already_present", "relational_events": "already_present"})

        # ------------------------------------------------------------
        # 12. Reconciliation for a BOUNDED-ONLY turn (clark_prose="").
        # This legitimately has no event_components.sequence=1 row at
        # all -- the pass-2 model call still happens and is still
        # recorded in event_model_participation, so the legacy
        # model_revision_id must come from there, never from a
        # sequence=1 component lookup that would silently resolve to
        # NULL for this exact case.
        # ------------------------------------------------------------
        data_dir12, pipeline_map12, now12, staging_path12 = _fresh_fixture(tmp_root)
        pipeline_key12 = pipeline_map12["llama"]["pipeline_key"]
        bounded_only_kwargs = _base_turn_kwargs(npw.generate_native_ulid(), now12, now12, pipeline_key12, " twelve")
        bounded_only_kwargs["clark_prose"] = ""
        turn12 = npw.stage_and_record_native_waking_turn(
            data_dir12, staging_path12, **bounded_only_kwargs,
        )
        check("a bounded-only turn (clark_prose='') has exactly ONE event_components row",
              len(turn12["model_participations"]) >= 1 and turn12["reassembled_reply"] == "Nothing was created.")
        conn12 = sqlite3.connect(os.path.join(data_dir12, "anaxi_provenance.db"))
        check("a bounded-only turn's canonical bundle has zero sequence=1 event_components rows",
              conn12.execute(
                  "SELECT COUNT(*) FROM event_components WHERE event_id = ? AND sequence = 1",
                  (turn12["event_id"],)
              ).fetchone()[0] == 0)
        check("a bounded-only turn STILL has a real pass-2 event_model_participation row",
              conn12.execute(
                  "SELECT COUNT(*) FROM event_model_participation WHERE event_id = ?",
                  (turn12["event_id"],)
              ).fetchone()[0] == 1)
        conn12.close()

        expected_bounded_only_model_revision_id = npw._expected_model_revision_id("gemma4:e4b")

        rel_db_path12 = os.path.join(data_dir12, "b12_relational.db")
        mind_db_path12 = os.path.join(data_dir12, "b12_mind.db")
        jsonl_path12 = os.path.join(data_dir12, "b12_anaxi_log.jsonl")
        rc12 = sqlite3.connect(rel_db_path12)
        rc12.execute("""CREATE TABLE relational_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, substrate TEXT NOT NULL,
            created_at INTEGER NOT NULL, agent_observation TEXT, external_observation TEXT,
            agent_response TEXT, linked_memories TEXT, linked_proposal_id INTEGER,
            status TEXT NOT NULL DEFAULT 'recorded',
            model_revision_id TEXT, pipeline_id TEXT, epoch_id TEXT, auth_context_id TEXT, event_id TEXT)""")
        rc12.commit(); rc12.close()
        mc12 = sqlite3.connect(mind_db_path12)
        mc12.execute("""CREATE TABLE turn_generation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, timestamp INTEGER,
            temperature REAL, top_p REAL, style_instruction TEXT,
            model_revision_id TEXT, pipeline_id TEXT, epoch_id TEXT, event_id TEXT)""")
        mc12.commit(); mc12.close()

        report12 = npw.reconcile_native_turn(
            data_dir12, staging_path12, turn12["event_id"],
            rel_db_path12, mind_db_path12, jsonl_path12,
        )
        check("reconciliation of a bounded-only turn repairs all three legacy stores",
              report12 == {"turn_generation_log": "repaired", "anaxi_log_jsonl": "repaired", "relational_events": "repaired"})

        mc12b = sqlite3.connect(mind_db_path12)
        tgl_mrid = mc12b.execute(
            "SELECT model_revision_id FROM turn_generation_log WHERE event_id = ?", (turn12["event_id"],)
        ).fetchone()[0]
        mc12b.close()
        check("bounded-only turn_generation_log.model_revision_id is the REAL pass-2 model "
              "revision, NOT NULL (the bug this item fixes would have produced NULL here, "
              "since there is no sequence=1 component to read it from)",
              tgl_mrid == expected_bounded_only_model_revision_id)

        rc12b = sqlite3.connect(rel_db_path12)
        rel_mrid = rc12b.execute(
            "SELECT model_revision_id FROM relational_events WHERE event_id = ?", (turn12["event_id"],)
        ).fetchone()[0]
        rc12b.close()
        check("bounded-only relational_events.model_revision_id is the REAL pass-2 model "
              "revision, NOT NULL",
              rel_mrid == expected_bounded_only_model_revision_id)

        # ------------------------------------------------------------
        # 13. Silent not_authorized: the mirror case of #12 -- a
        # PROSE-ONLY turn (bounded_clause=""), added for the bounded-clause
        # UX correction. This legitimately has no event_components.sequence=0
        # row at all: a routine, silent operation_status does not fabricate
        # a component claiming a clause was shown when none was.
        # ------------------------------------------------------------
        data_dir13, pipeline_map13, now13, staging_path13 = _fresh_fixture(tmp_root)
        pipeline_key13 = pipeline_map13["llama"]["pipeline_key"]
        silent_kwargs = _base_turn_kwargs(npw.generate_native_ulid(), now13, now13, pipeline_key13, " thirteen")
        silent_kwargs["bounded_clause"] = ""
        turn13 = npw.stage_and_record_native_waking_turn(
            data_dir13, staging_path13, **silent_kwargs,
        )
        check("a silent turn (bounded_clause='') reassembles to prose ALONE, no leading space, "
              "no stray clause text",
              turn13["reassembled_reply"] == "This is a synthetic reply thirteen, not a real conversation.")
        conn13 = sqlite3.connect(os.path.join(data_dir13, "anaxi_provenance.db"))
        check("a silent turn's canonical bundle has zero sequence=0 event_components rows",
              conn13.execute(
                  "SELECT COUNT(*) FROM event_components WHERE event_id = ? AND sequence = 0",
                  (turn13["event_id"],)
              ).fetchone()[0] == 0)
        check("a silent turn's sequence=1 (conversational_prose) component has span_start=0 "
              "(it is the entirety of what was shown, not offset past an unshown clause)",
              conn13.execute(
                  "SELECT span_start FROM event_components WHERE event_id = ? AND sequence = 1",
                  (turn13["event_id"],)
              ).fetchone()[0] == 0)
        check("a silent turn STILL has a real pass-2 event_model_participation row "
              "(the mechanical decision is not erased by the silent presentation)",
              conn13.execute(
                  "SELECT COUNT(*) FROM event_model_participation WHERE event_id = ?",
                  (turn13["event_id"],)
              ).fetchone()[0] == 1)
        check("verify_native_bundle_contract confirms a silent turn's bundle as complete and valid",
              npw.verify_native_bundle_contract(conn13, turn13["event_id"]))
        conn13.close()

        # Resume/idempotency: re-running the exact same staged silent turn
        # (as a crash-recovery retry would) must return the existing bundle
        # unchanged, never raise, never duplicate rows.
        turn13_resumed = npw.record_native_waking_turn(
            data_dir13,
            event_id=turn13["event_id"], staging_id=turn13["staging_id"],
            session_id=turn13["session_id"], session_started_at=now13,
            auth_context_id=turn13["auth_context_id"], prompt=silent_kwargs["prompt"],
            bounded_clause="", clark_prose=silent_kwargs["clark_prose"],
            pipeline_key=pipeline_key13, waking_model_tag="gemma4:e4b",
            artifact_pass_ran=False, occurred_at=now13,
        )
        check("resuming a silent turn with its exact staged contract is idempotent, no raise",
              turn13_resumed["event_id"] == turn13["event_id"]
              and turn13_resumed["reassembled_reply"] == turn13["reassembled_reply"])

        # Corruption check: if a sequence=0 row exists despite bounded_clause
        # being expected empty, verify_native_bundle_contract must hard-stop,
        # never silently accept it as a valid silent bundle.
        conn13_corrupt = sqlite3.connect(os.path.join(data_dir13, "anaxi_provenance.db"))
        conn13_corrupt.execute("PRAGMA foreign_keys = ON;")
        stray_actor_id = derive_stable_id("actor", "bounded_clause_renderer")
        conn13_corrupt.execute(
            "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
            "authorship_resolution, component_text, content_sha256, model_revision_id, "
            "span_start, span_end) VALUES (?, 0, ?, 'bounded_clause', 'resolved', ?, ?, NULL, 0, ?)",
            (turn13["event_id"], stray_actor_id, "A stray clause that was never shown.",
             hashlib.sha256(b"A stray clause that was never shown.").hexdigest(), 36),
        )
        conn13_corrupt.commit()
        check("a stray sequence=0 row on an event whose bounded_clause was expected empty is "
              "detected and hard-stopped, never silently accepted",
              raises(
                  lambda: npw.verify_native_bundle_contract(
                      conn13_corrupt, turn13["event_id"],
                      expected={
                          "staging_id": turn13["staging_id"], "session_id": turn13["session_id"],
                          "session_started_at": now13, "auth_context_id": turn13["auth_context_id"],
                          "pipeline_id": turn13["pipeline_id"], "occurred_at": now13,
                          "bounded_clause": "", "clark_prose": silent_kwargs["clark_prose"],
                          "artifact_pass_ran": False, "waking_model_tag": "gemma4:e4b",
                      },
                  ),
                  (MigrationStopCondition,),
              ))
        conn13_corrupt.close()

        # Fully-silent edge case: bounded_clause="" AND clark_prose="" ->
        # zero event_components rows is a real, valid state, not an error.
        data_dir13b, pipeline_map13b, now13b, staging_path13b = _fresh_fixture(tmp_root)
        pipeline_key13b = pipeline_map13b["llama"]["pipeline_key"]
        fully_silent_kwargs = _base_turn_kwargs(npw.generate_native_ulid(), now13b, now13b, pipeline_key13b, " thirteenb")
        fully_silent_kwargs["bounded_clause"] = ""
        fully_silent_kwargs["clark_prose"] = ""
        turn13b = npw.stage_and_record_native_waking_turn(
            data_dir13b, staging_path13b, **fully_silent_kwargs,
        )
        check("a fully-silent turn (both bounded_clause and clark_prose empty) reassembles to "
              "an empty string, not an error",
              turn13b["reassembled_reply"] == "")
        conn13b = sqlite3.connect(os.path.join(data_dir13b, "anaxi_provenance.db"))
        check("a fully-silent turn has ZERO event_components rows",
              conn13b.execute(
                  "SELECT COUNT(*) FROM event_components WHERE event_id = ?", (turn13b["event_id"],)
              ).fetchone()[0] == 0)
        check("verify_native_bundle_contract confirms a fully-silent turn's bundle as complete "
              "and valid despite zero components (event_model_participation alone is sufficient)",
              npw.verify_native_bundle_contract(conn13b, turn13b["event_id"]))
        conn13b.close()

        print()
        print("=" * 70)
        print(f"{_PASS}/{_PASS + _FAIL} pass")
        print("=" * 70)
        return _FAIL == 0


if __name__ == "__main__":
    success = run_suite()
    sys.exit(0 if success else 1)
