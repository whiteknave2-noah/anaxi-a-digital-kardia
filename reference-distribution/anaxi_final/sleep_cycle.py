"""SLP1-C: the dormant Sleep cycle orchestrator.

Wires the three already-closed stages together over ONE bounded,
fenced, canonical source window:

    sleep_evidence.py (A)  ->  waking_material_unit.py (A)
        ->  sleep_selection.py (B, unchanged)
        ->  sleep_transformation.py (C)
        ->  ONE atomic canonical commit (C)

Reachability (corrected): `run_sleep_cycle()` is not called by any scheduler
or by `llama_sleep.py`'s own (mechanically disabled) `run_sleep()`. It IS the
route the owner-surface "Execute one authorized Sleep cycle" control runs, via
`run_sleep_cycle_with_production_defaults()`, only after a subject-originated
request is owner-authorized and separately confirmed. Nothing here starts a
cycle by itself.

SLP1-E: this module is the ONE authoritative dormant Sleep v1 execution
route. `run_sleep_cycle_with_production_defaults()` below is the exact
answer to "if Sleep v1 were later activated, what function would run
it" -- it is the only function in this codebase that opens the
anchored canonical `anaxi_provenance.db` (via sleep_c_schema.SleepCPaths.
production_defaults(), the same __file__-relative convention every
other Sleep-v1 production-defaults path already uses) specifically to
run a cycle. No caller of `run_sleep_cycle()` itself can accidentally
substitute an arbitrary CWD-relative path for canonical v1 Sleep state
merely by using this entrypoint -- and nothing calls it: it exists, and
is dormant, exactly like every other piece of this architecture.

CRASH MODEL (frozen, see module docstring for the full mandate text):
evidence, Selection, and Transformation all happen in memory before the
one final atomic commit. If the process dies before that commit,
nothing durable happened -- no watermark change, no cycle record, no
derivations -- and a later attempt may simply re-run the same source
window. If the process dies AFTER the commit, the watermark has already
advanced and the same window is never reprocessed. No mid-cycle journal
is built because none is needed for that guarantee.
"""
import hashlib
import sqlite3
import time
import uuid

import sleep_c_schema as schema
import sleep_evidence as se
import sleep_lease as lease_mod
import sleep_selection
import sleep_transformation as st
import sleep_watermark as watermark_mod
import waking_material_unit as wmu_mod
from llama_sleep import MODEL as _SLEEP_MODEL_TAG

DEFAULT_OWNER_ID_PREFIX = "sleep-cycle"


class CycleResult:
    def __init__(self, status, *, cycle_id=None, window_start_component_id=None,
                 window_end_component_id=None, selected_count=0, derivation_count=0):
        self.status = status
        self.cycle_id = cycle_id
        self.window_start_component_id = window_start_component_id
        self.window_end_component_id = window_end_component_id
        self.selected_count = selected_count
        self.derivation_count = derivation_count

    def __repr__(self):
        return (
            f"CycleResult(status={self.status!r}, cycle_id={self.cycle_id!r}, "
            f"window=({self.window_start_component_id}, {self.window_end_component_id}], "
            f"selected_count={self.selected_count}, derivation_count={self.derivation_count})"
        )


class SleepCycleFailure(Exception):
    """Raised for any failure that must leave zero canonical trace --
    lease unavailable/lost mid-cycle, Selection failure, Transformation
    failure. The caller's connection is left with no uncommitted
    canonical writes (every write happens inside the one final
    transaction, which is rolled back on any exception)."""


def _default_owner_id():
    return f"{DEFAULT_OWNER_ID_PREFIX}-{uuid.uuid4().hex}"


def _stable_id(domain_tag, *parts):
    domain = domain_tag.encode("utf-8")
    preimage = domain
    for part in parts:
        b = str(part).encode("utf-8")
        preimage += b"|" + len(b).to_bytes(8, "big") + b
    digest = hashlib.sha256(preimage).hexdigest()
    return digest[:26]


def _derive_cycle_id(owner_id, generation, window_start, window_end):
    return f"cycle-{_stable_id('anaxi-sleep-c-cycle-id-v1', owner_id, generation, window_start, window_end)}"


def _derive_derivation_id(cycle_id, batch_index, item_index):
    return f"deriv-{_stable_id('anaxi-sleep-c-derivation-id-v1', cycle_id, batch_index, item_index)}"


def _read_max_component_id(conn):
    row = conn.execute("SELECT MAX(component_id) FROM event_components").fetchone()
    return row[0] if row is not None and row[0] is not None else 0


def gather_window_evidence(conn, *, after_component_id, through_component_id,
                            max_segments=se.MAX_SEGMENTS_PER_BATCH,
                            max_chars_per_segment=se.MAX_CHARS_PER_SEGMENT,
                            max_aggregate_chars=se.MAX_AGGREGATE_EVIDENCE_CHARS):
    """Runs sleep_evidence.build_sleep_evidence_v1 for one bounded
    initial batch, completes its pending component, then closes the
    contiguous window over connected WMUs inside the fixed snapshot.
    Component limits are collection targets, never permission to split
    an H/X or delivered-resource unit. Model packing remains bounded
    independently and rejects oversized whole WMUs without clipping.

    Returns (items, window_end_component_id). `window_end_component_id`
    is the highest canonical component fully and contiguously accounted
    for -- never a partially-segmented one."""
    batch = se.build_sleep_evidence_v1(
        conn, after_component_id=after_component_id, through_component_id=through_component_id,
        max_segments=max_segments, max_chars_per_segment=max_chars_per_segment,
        max_aggregate_chars=max_aggregate_chars,
    )
    items = list(batch["items"])
    evidence_complete = list(batch["component_ids_evidence_complete"])
    pending_component_id = batch["pending_component_id"]
    pending_next_segment_index = batch["pending_next_segment_index"]

    while pending_component_id is not None:
        resume_batch = se.build_sleep_evidence_v1(
            conn, after_component_id=after_component_id, through_component_id=through_component_id,
            # Wide-open capacity here: this resume call's only job is to
            # finish emitting the ONE pending component's remaining
            # segments -- never to stop again partway through it.
            max_segments=10**9, max_chars_per_segment=max_chars_per_segment,
            max_aggregate_chars=10**12,
            resume_component_id=pending_component_id, resume_segment_index=pending_next_segment_index,
        )
        items.extend(it for it in resume_batch["items"] if it["component_id"] == pending_component_id)
        if pending_component_id in resume_batch["component_ids_evidence_complete"]:
            evidence_complete.append(pending_component_id)
            pending_component_id = None
        else:
            pending_component_id = resume_batch["pending_component_id"]
            pending_next_segment_index = resume_batch["pending_next_segment_index"]

    window_end_component_id = max(evidence_complete) if evidence_complete else after_component_id
    groups = wmu_mod.snapshot_component_groups(conn, through_component_id)
    while True:
        closed_end = window_end_component_id
        for primary, component_ids in groups.items():
            if not any(after_component_id < cid <= closed_end for cid in component_ids):
                continue
            if component_ids[0] <= after_component_id:
                # Never silently replay an already-processed source or present
                # its later fragment as a whole WMU. No watermark moves here.
                raise SleepCycleFailure(
                    f"WMU primary={primary!r} crosses the processed watermark; "
                    "refusing detached evidence or implicit source replay"
                )
            closed_end = max(closed_end, component_ids[-1])
        if closed_end == window_end_component_id:
            break
        tail = se.build_sleep_evidence_v1(
            conn, after_component_id=window_end_component_id,
            through_component_id=closed_end, max_segments=10**9,
            max_chars_per_segment=max_chars_per_segment,
            max_aggregate_chars=10**12,
        )
        if tail["pending_component_id"] is not None:
            raise SleepCycleFailure("whole-WMU window exceeds evidence collection capacity")
        items.extend(tail["items"])
        window_end_component_id = closed_end
    return items, window_end_component_id


def _expand_wmu_sources(wmu):
    """Mechanical expansion of one WMU into the exact canonical
    selection-bearing (event_id, component_id, segment_id,
    segment_index) tuples the model actually saw for it -- provenance
    work, never a semantic judgment. Supporting-provenance entries are
    never included (they were never shown to the model as semantic
    material either)."""
    for entry in wmu["selection_bearing"]:
        for seg in entry["segments"]:
            yield (entry["event_id"], entry["component_id"], seg["segment_id"], seg["segment_index"])


def _commit_successful_cycle(conn, *, owner_id, generation, now, window_start_component_id,
                              window_end_component_id, selected_count, derivations, id_to_wmu):
    """The ONE atomic final transaction. Verifies lease ownership+
    generation+validity INSIDE this same transaction before writing
    anything; any failure (including a lost lease) rolls back with
    zero canonical trace. Returns the new cycle_id."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not lease_mod.verify_lease_for_commit(conn, owner_id=owner_id, generation=generation, now=now):
            raise SleepCycleFailure(
                "lease lost before final commit -- no canonical write performed"
            )

        cycle_id = _derive_cycle_id(owner_id, generation, window_start_component_id, window_end_component_id)
        conn.execute(
            "INSERT INTO sleep_cycles (cycle_id, window_start_component_id, window_end_component_id, "
            "selected_count, derivation_count, model_tag, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cycle_id, window_start_component_id, window_end_component_id, selected_count,
             len(derivations), _SLEEP_MODEL_TAG, now, now),
        )

        for d in derivations:
            derivation_id = _derive_derivation_id(cycle_id, d.batch_index, d.item_index)
            text_hash = hashlib.sha256(d.derived_text.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO sleep_derivations (derivation_id, cycle_id, batch_index, item_index, "
                "derived_text, derived_text_sha256, model_tag, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (derivation_id, cycle_id, d.batch_index, d.item_index, d.derived_text, text_hash,
                 _SLEEP_MODEL_TAG, now),
            )
            for cite_order, wmu_id in enumerate(d.source_wmu_ids):
                wmu = id_to_wmu[wmu_id]
                for (source_event_id, source_component_id, segment_id, segment_index) in _expand_wmu_sources(wmu):
                    conn.execute(
                        "INSERT INTO sleep_derivation_sources (derivation_id, wmu_id, primary_event_id, "
                        "cite_order, source_event_id, source_component_id, segment_id, segment_index) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (derivation_id, wmu_id, wmu["primary_event_id"], cite_order,
                         source_event_id, source_component_id, segment_id, segment_index),
                    )

        watermark_mod.advance_watermark_in_conn(
            conn, new_last_processed_component_id=window_end_component_id, cycle_id=cycle_id, now=now,
        )
        conn.execute(
            "DELETE FROM sleep_v1_lease WHERE id = 1 AND owner_id = ? AND generation = ?",
            (owner_id, generation),
        )
        conn.execute("COMMIT")
        return cycle_id
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def run_sleep_cycle(conn, *, owner_id=None, now=None, lease_duration_seconds=lease_mod.DEFAULT_LEASE_DURATION_SECONDS,
                     max_segments=se.MAX_SEGMENTS_PER_BATCH, max_chars_per_segment=se.MAX_CHARS_PER_SEGMENT,
                     max_aggregate_chars=se.MAX_AGGREGATE_EVIDENCE_CHARS,
                     measure=None, selection_chat=None, transformation_chat=None):
    """The single dormant entry point. `conn` must be an open
    sqlite3.Connection to the anchored anaxi_provenance.db with
    `conn.isolation_level = None` (manual transaction control -- see
    sleep_lease.py's own requirement) already set by the caller.
    `owner_id`/`now` are injectable for deterministic tests; production
    callers may omit both.

    Never itself opens or closes `conn` -- the caller owns the
    connection's lifetime, exactly like every other read-only Sleep
    module in this codebase.
    """
    owner_id = owner_id if owner_id is not None else _default_owner_id()
    # Production must check real time after inference, not the stale start
    # timestamp. Explicit `now` remains the existing deterministic test seam.
    current_time = (lambda: int(time.time())) if now is None else (lambda: int(now))
    started_at = current_time()

    schema.ensure_schema(conn)

    handle = lease_mod.acquire_lease(conn, owner_id=owner_id, now=started_at, duration_seconds=lease_duration_seconds)
    if handle is None:
        return CycleResult(status="lease_unavailable")

    try:
        watermark = watermark_mod.read_watermark_from_conn(conn)
        after_component_id = watermark["last_processed_component_id"]
        snapshot_max_component_id = _read_max_component_id(conn)

        if snapshot_max_component_id <= after_component_id:
            lease_mod.release_lease(conn, owner_id=owner_id, generation=handle.generation)
            return CycleResult(status="no_work")

        items, window_end_component_id = gather_window_evidence(
            conn, after_component_id=after_component_id, through_component_id=snapshot_max_component_id,
            max_segments=max_segments, max_chars_per_segment=max_chars_per_segment,
            max_aggregate_chars=max_aggregate_chars,
        )

        wmus = wmu_mod.assemble_wmus(conn, items)
        id_to_wmu = {w["wmu_id"]: w for w in wmus}

        if not lease_mod.renew_lease(conn, owner_id=owner_id, generation=handle.generation, now=current_time(),
                                      duration_seconds=lease_duration_seconds):
            raise SleepCycleFailure("lease lost before Selection -- no canonical write performed")

        selection_result = sleep_selection.select_wmus(wmus, measure=measure, chat=selection_chat)

        derivations = []
        if selection_result.selected_wmu_ids:
            if not lease_mod.renew_lease(conn, owner_id=owner_id, generation=handle.generation, now=current_time(),
                                          duration_seconds=lease_duration_seconds):
                raise SleepCycleFailure("lease lost before Transformation -- no canonical write performed")
            selected_wmus_ordered = [id_to_wmu[wid] for wid in selection_result.selected_wmu_ids]
            derivations = st.transform(selected_wmus_ordered, measure=measure, chat=transformation_chat)
            derivations = st.dedupe_derivations(derivations)

        cycle_id = _commit_successful_cycle(
            conn, owner_id=owner_id, generation=handle.generation, now=current_time(),
            window_start_component_id=after_component_id, window_end_component_id=window_end_component_id,
            selected_count=len(selection_result.selected_wmu_ids), derivations=derivations, id_to_wmu=id_to_wmu,
        )

        return CycleResult(
            status="completed", cycle_id=cycle_id,
            window_start_component_id=after_component_id, window_end_component_id=window_end_component_id,
            selected_count=len(selection_result.selected_wmu_ids), derivation_count=len(derivations),
        )
    except BaseException:
        # Best-effort release -- harmless no-op if the lease was already
        # lost to a newer owner (release_lease is itself compare-and-
        # clear, so it can never clear someone else's lease). No
        # canonical write of any kind happens on this path.
        lease_mod.release_lease(conn, owner_id=owner_id, generation=handle.generation)
        raise


def run_sleep_cycle_with_production_defaults(**kwargs):
    """SLP1-E: the one authoritative dormant Sleep v1 entrypoint. Opens
    the SAME anchored anaxi_provenance.db every other Sleep-v1 module's
    own production_defaults() already resolves (sleep_c_schema.py,
    sleep_evidence.py) -- never the current working directory, never a
    caller-supplied path -- with the manual-transaction-control mode
    sleep_lease.py's fencing requires, runs exactly one
    run_sleep_cycle() attempt, and closes the connection. Accepts the
    same keyword arguments as run_sleep_cycle() (owner_id, now,
    lease_duration_seconds, max_segments, max_chars_per_segment,
    max_aggregate_chars) for tests/future callers; none are required.

    Called by exactly one production surface: the owner Sleep control
    (llama_gui.execute_sleep_from_owner_surface -> sleep_owner_control.
    execute_authorized_request), after owner authorization and explicit
    confirmation, with no background activity running. Not wired to any
    scheduler, CLI, or waking pathway. Calling it against the real production
    database outside that authorized flow is the "genuine production Sleep" this
    architecture refuses to perform implicitly.
    """
    # Production admits a prompt the byte upper bound rejects by the provider's
    # own measurement of the exact prompt (one whole exchange routinely exceeds
    # the byte-costed budget while using a fraction of the real window), and
    # bounds every generation by its budgeted reserve. Test callers of
    # run_sleep_cycle() keep the estimator-only, no-model default.
    import llama_sleep
    kwargs.setdefault("measure", llama_sleep.measure_prompt_tokens)
    kwargs.setdefault("selection_chat", llama_sleep.selection_chat)
    kwargs.setdefault("transformation_chat", llama_sleep.transformation_chat)
    provenance_db_path = schema.SleepCPaths.production_defaults().provenance_db_path
    conn = sqlite3.connect(provenance_db_path)
    conn.isolation_level = None
    try:
        return run_sleep_cycle(conn, **kwargs)
    finally:
        conn.close()
