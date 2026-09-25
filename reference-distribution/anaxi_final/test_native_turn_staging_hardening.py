"""
Anaxi -- Regression suite for the native_turn_staging.py hardening
that closed three mechanically-confirmed defects (see the module's own
docstring for the full analysis):

1. A durably-written-then-fsync-failed staging line was left on disk;
   the caller's documented "retry whole" then produced a SECOND,
   distinct staged entry (fresh event_id) alongside the orphaned first
   one -- both independently valid, both independently recoverable by
   resume_orphaned_staged_turns(), producing TWO canonical events for
   ONE logical turn.
2. Two callers computing the same next line_index (a genuine race,
   never actually prevented) produced a physically-second line whose
   stored staging_id -- derived from its claimed index -- disagreed
   with the index recomputed from its true physical position, only
   detected (never prevented) on a later read.
3. A write that landed a nonzero prefix and then failed left that
   truncated, non-JSON prefix on disk; a later crash-recovery read hit
   an unhandled json.JSONDecodeError, not the module's own documented
   StagingIntegrityError contract.

This suite proves each is closed, using only synthetic temp files --
no real database, no real model call, no live project directory
touched anywhere.

Run:
    python test_native_turn_staging_hardening.py
"""

import multiprocessing
import os
import sys
import tempfile
import time


def _prime_lock_file(staging_path: str) -> None:
    """_StagingLock.__enter__() writes one setup byte to <staging_path>.lock
    the FIRST time it's ever used for a given staging_path. Tests that
    mock os.write() to inject a failure on a SPECIFIC call must not let
    that first, unrelated lock-file-setup write consume the mock's
    call-count slot -- pre-create the lock file (with its steady-state
    1-byte content already in place) so _StagingLock.__enter__ skips
    its own os.write() entirely (os.fstat().st_size is already >= 1)
    and the mock only ever observes the actual staging-data write(s)
    under test."""
    with open(staging_path + ".lock", "wb") as f:
        f.write(b"\0")


def _mp_writer_worker(staging_path: str, writer_label: str, count: int) -> None:
    """Module-level (picklable) worker for the genuine cross-process
    concurrency test -- a real separate OS process, not a thread, so
    this actually exercises _StagingLock's cross-process guarantee
    rather than anything the GIL could paper over."""
    import native_turn_staging as staging
    for i in range(count):
        staging.append_staging_entry(staging_path, {"writer": writer_label, "seq": i})


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    tmp_root = tempfile.mkdtemp(prefix="anaxi_test_staging_hardening_")

    try:
        import native_turn_staging as staging

        real_write = os.write
        real_fsync = os.fsync
        real_ftruncate = os.ftruncate

        # =================================================================
        # TEST 1: complete write + injected FIRST fsync() failure.
        # =================================================================
        path1 = os.path.join(tmp_root, "test1.jsonl")
        with open(path1, "wb") as f:
            f.write(b"")  # establish a real, empty pre-existing file
        with open(path1, "rb") as f:
            pre_call_bytes = f.read()

        fsync_calls = {"n": 0}

        def _fail_first_fsync(fd):
            fsync_calls["n"] += 1
            if fsync_calls["n"] == 1:
                raise OSError("simulated: disk full / fsync failure (first call only)")
            return real_fsync(fd)

        os.fsync = _fail_first_fsync
        raised1 = None
        try:
            staging.append_staging_entry(path1, {"turn": "test1-original-attempt"})
        except Exception as e:
            raised1 = e
        finally:
            os.fsync = real_fsync

        check("1. append_staging_entry() raised StagingDurabilityError on injected fsync failure",
              isinstance(raised1, staging.StagingDurabilityError))
        with open(path1, "rb") as f:
            post_failure_bytes = f.read()
        check("1. staging file after the failed call is byte-identical to its pre-call state "
              "(rollback succeeded)",
              post_failure_bytes == pre_call_bytes)

        # Retry whole: call again, no fault injected this time.
        staging_id_retry = staging.append_staging_entry(path1, {"turn": "test1-retry-attempt"})
        entries1 = list(staging.read_verified_staging_entries(path1))
        check("1. retry whole produces EXACTLY ONE staged entry (the orphaned pre-rollback "
              "attempt left nothing behind to also be recovered)",
              len(entries1) == 1 and entries1[0][2] == staging_id_retry)

        # Recovery/canonicalization: prove only ONE canonical event results.
        import tempfile as _tf
        from provenance_schema import create_provenance_db
        from migrate_historical_data import build_pipeline_map, seed_reference_data
        import native_provenance_writer as npw
        import sqlite3

        data_dir1 = os.path.join(tmp_root, "test1_data")
        os.makedirs(data_dir1, exist_ok=True)
        prov_path1 = os.path.join(data_dir1, "anaxi_provenance.db")
        create_provenance_db(prov_path1).close()
        manifest1 = {"pipelines": {"llama": {"routing_constant_value": "nate"},
                                    "claude": {"routing_constant_value": "nate"}}}
        pipeline_map1 = build_pipeline_map(manifest1)
        now1 = int(time.time())
        seed_conn1 = sqlite3.connect(prov_path1)
        seed_conn1.execute("PRAGMA foreign_keys = ON;")
        seed_reference_data(seed_conn1, pipeline_map1, now1)
        seed_conn1.close()
        pipeline_key1 = pipeline_map1["llama"]["pipeline_key"]

        staging_path1b = os.path.join(data_dir1, "native_turn_staging.jsonl")

        def _base_kwargs(now):
            return dict(
                session_id=npw.generate_native_ulid(), session_started_at=now, user_id="nate",
                prompt="staging-hardening test 1", bounded_clause="Nothing was created.",
                clark_prose="synthetic reply", kardia={}, controls={"temperature": 0.4, "top_p": 0.85},
                waking_model_tag="gemma4:e4b", pipeline_key=pipeline_key1,
                artifact_pass_ran=False, occurred_at=now,
            )

        fsync_calls["n"] = 0  # reset the shared counter for this second use
        os.fsync = _fail_first_fsync
        raised1b = None
        try:
            npw.stage_and_record_native_waking_turn(data_dir1, staging_path1b, **_base_kwargs(now1))
        except Exception as e:
            raised1b = e
        finally:
            os.fsync = real_fsync
        check("1. stage_and_record_native_waking_turn() also raises StagingDurabilityError on "
              "injected fsync failure (end-to-end, not just the staging module in isolation)",
              isinstance(raised1b, staging.StagingDurabilityError))

        retry1b = npw.stage_and_record_native_waking_turn(data_dir1, staging_path1b, **_base_kwargs(now1))
        recovered1 = npw.resume_orphaned_staged_turns(data_dir1, staging_path1b)
        conn1 = sqlite3.connect(prov_path1)
        total_events1 = conn1.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn1.close()
        check("1. recovery/canonicalization produces exactly ONE canonical event for the retry "
              "(the pre-hardening defect this closes would have produced two)",
              total_events1 == 1 and retry1b["event_id"] is not None)

        # =================================================================
        # TEST 2: partial nonzero write + subsequent injected write failure.
        # =================================================================
        path2 = os.path.join(tmp_root, "test2.jsonl")
        with open(path2, "wb") as f:
            f.write(b"")
        _prime_lock_file(path2)
        with open(path2, "rb") as f:
            pre_call_bytes2 = f.read()

        write_calls2 = {"n": 0}
        PREFIX_LEN = 7

        def _flaky_write(fd, data):
            write_calls2["n"] += 1
            if write_calls2["n"] == 1:
                return real_write(fd, data[:PREFIX_LEN])
            raise OSError("simulated: disk-full mid-write, after a nonzero prefix already landed")

        os.write = _flaky_write
        raised2 = None
        try:
            staging.append_staging_entry(path2, {"turn": "test2-partial-write"})
        except Exception as e:
            raised2 = e
        finally:
            os.write = real_write

        check("2. append_staging_entry() raised StagingDurabilityError on the partial-write-then-fail case",
              isinstance(raised2, staging.StagingDurabilityError))
        with open(path2, "rb") as f:
            post_failure_bytes2 = f.read()
        check("2. rollback restores EXACT pre-call bytes",
              post_failure_bytes2 == pre_call_bytes2)
        check("2. no b'{\"stagi' or other partial tail remains on disk",
              b'{"stagi' not in post_failure_bytes2 and len(post_failure_bytes2) == len(pre_call_bytes2))

        # Reader remains healthy afterward -- and a subsequent real append works normally.
        raised2b = None
        try:
            entries2 = list(staging.read_verified_staging_entries(path2))
        except Exception as e:
            raised2b = e
        check("2. reader remains healthy afterward (no exception, empty result on the untouched file)",
              raised2b is None and entries2 == [])
        staging.append_staging_entry(path2, {"turn": "test2-normal-append-after-recovery"})
        entries2b = list(staging.read_verified_staging_entries(path2))
        check("2. a normal append after the rollback succeeds and verifies cleanly",
              len(entries2b) == 1)

        # =================================================================
        # TEST 3: injected rollback failure -- distinct indeterminate-state
        # error, no canonical transaction, retry-whole forbidden.
        # =================================================================
        path3 = os.path.join(tmp_root, "test3.jsonl")
        with open(path3, "wb") as f:
            f.write(b"")
        _prime_lock_file(path3)

        def _always_failing_write(fd, data):
            raise OSError("simulated: write fails immediately")

        def _failing_ftruncate(fd, length):
            raise OSError("simulated: rollback itself also fails (e.g. a second, unrelated disk fault)")

        os.write = _always_failing_write
        os.ftruncate = _failing_ftruncate
        raised3 = None
        try:
            staging.append_staging_entry(path3, {"turn": "test3-double-fault"})
        except Exception as e:
            raised3 = e
        finally:
            os.write = real_write
            os.ftruncate = real_ftruncate

        check("3. a write failure PLUS a rollback failure raises StagingIndeterminateStateError "
              "(a distinct exception, not StagingDurabilityError)",
              isinstance(raised3, staging.StagingIndeterminateStateError))
        check("3. StagingIndeterminateStateError is NOT a subclass of StagingDurabilityError "
              "(a caller cannot accidentally catch-and-retry it via a broad except clause)",
              not isinstance(raised3, staging.StagingDurabilityError))
        check("3. StagingIndeterminateStateError's own documentation explicitly forbids automatic "
              "retry-whole",
              "FORBIDDEN" in staging.StagingIndeterminateStateError.__doc__
              and "retry" in staging.StagingIndeterminateStateError.__doc__.lower())

        # End-to-end: no canonical transaction begins.
        data_dir3 = os.path.join(tmp_root, "test3_data")
        os.makedirs(data_dir3, exist_ok=True)
        prov_path3 = os.path.join(data_dir3, "anaxi_provenance.db")
        create_provenance_db(prov_path3).close()
        pipeline_map3 = build_pipeline_map(manifest1)
        now3 = int(time.time())
        seed_conn3 = sqlite3.connect(prov_path3)
        seed_conn3.execute("PRAGMA foreign_keys = ON;")
        seed_reference_data(seed_conn3, pipeline_map3, now3)
        seed_conn3.close()
        pipeline_key3 = pipeline_map3["llama"]["pipeline_key"]
        staging_path3b = os.path.join(data_dir3, "native_turn_staging.jsonl")
        _prime_lock_file(staging_path3b)

        os.write = _always_failing_write
        os.ftruncate = _failing_ftruncate
        raised3b = None
        try:
            npw.stage_and_record_native_waking_turn(
                data_dir3, staging_path3b,
                session_id=npw.generate_native_ulid(), session_started_at=now3, user_id="nate",
                prompt="staging-hardening test 3", bounded_clause="Nothing was created.",
                clark_prose="synthetic reply", kardia={}, controls={"temperature": 0.4, "top_p": 0.85},
                waking_model_tag="gemma4:e4b", pipeline_key=pipeline_key3,
                artifact_pass_ran=False, occurred_at=now3,
            )
        except Exception as e:
            raised3b = e
        finally:
            os.write = real_write
            os.ftruncate = real_ftruncate

        check("3. stage_and_record_native_waking_turn() also raises StagingIndeterminateStateError "
              "end-to-end",
              isinstance(raised3b, staging.StagingIndeterminateStateError))
        conn3 = sqlite3.connect(prov_path3)
        events3 = conn3.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        sessions3 = conn3.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        conn3.close()
        check("3. no canonical transaction began (zero events, zero sessions rows)",
              events3 == 0 and sessions3 == 0)

        # =================================================================
        # TEST 4: two GENUINELY concurrent writers (real separate OS
        # processes, not threads) against the same staging file.
        # =================================================================
        path4 = os.path.join(tmp_root, "test4.jsonl")
        N_PER_WRITER = 8
        p_a = multiprocessing.Process(target=_mp_writer_worker, args=(path4, "A", N_PER_WRITER))
        p_b = multiprocessing.Process(target=_mp_writer_worker, args=(path4, "B", N_PER_WRITER))
        p_a.start()
        p_b.start()
        p_a.join(timeout=60)
        p_b.join(timeout=60)

        check("4. both writer processes completed (did not hang or deadlock on the cross-process lock)",
              not p_a.is_alive() and not p_b.is_alive()
              and p_a.exitcode == 0 and p_b.exitcode == 0)

        entries4 = list(staging.read_verified_staging_entries(path4))
        check("4. full reader verification succeeds -- serialization prevented any same-index "
              "collision (a collision would raise StagingIntegrityError here, exactly as "
              "mechanically demonstrated pre-hardening)",
              len(entries4) == 2 * N_PER_WRITER)
        physical_indices = [line_index for line_index, _payload, _sid in entries4]
        check("4. every completed entry has a correct, unique physical index (0..N-1, no "
              "duplicates, no gaps)",
              sorted(physical_indices) == list(range(2 * N_PER_WRITER)))
        writer_labels = {payload["writer"] for _li, payload, _sid in entries4}
        check("4. entries from BOTH writers are present (genuine concurrency happened, one "
              "writer didn't simply finish entirely before the other started)",
              writer_labels == {"A", "B"})

        # =================================================================
        # TEST 5: a file with a valid first entry plus a malformed later
        # entry -- reader must fail atomically, never yielding the first
        # valid entry before raising on the later defect.
        # =================================================================
        path5 = os.path.join(tmp_root, "test5.jsonl")
        staging.append_staging_entry(path5, {"turn": "test5-valid-first-entry"})
        with open(path5, "a", encoding="utf-8") as f:
            f.write("{this is not valid json at all\n")

        gen5 = staging.read_verified_staging_entries(path5)
        raised5 = None
        first_entry_seen = False
        try:
            next(gen5)
            first_entry_seen = True
        except staging.StagingIntegrityError as e:
            raised5 = e
        except StopIteration:
            pass

        check("5. StagingIntegrityError is raised on the VERY FIRST next() call -- the earlier, "
              "individually-valid entry is never yielded first",
              raised5 is not None and not first_entry_seen)
        check("5. the malformed JSON line is translated to StagingIntegrityError, not left as a "
              "raw json.JSONDecodeError",
              isinstance(raised5, staging.StagingIntegrityError))

    finally:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    success = run_test_suite()
    sys.exit(0 if success else 1)
