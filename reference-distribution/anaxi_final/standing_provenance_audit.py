"""
Anaxi -- Standing two-direction soft-reference integrity audit (frozen
Identity/Provenance Schema §2.0 and §3, extended for native
post-cutover projection-pending detection per §2.0's own
acceptance-criteria description and Amendment A1 §A1.6).

Read-only. Never writes, never repairs. reconcile_native_turn()
(native_provenance_writer.py) is the separate, explicitly-invoked
repair mechanism this audit's findings feed into -- this module only
detects and reports, exactly as §2.0 specifies: "a periodic, read-only
audit query (run by a standing test, not by the application at write
time)".

Two distinguishable directions:

1. **Dangling soft reference** (legacy -> canonical): a legacy row
   carries a non-NULL soft-reference column (§3's additive-deltas
   table) that does not resolve to any row in its canonical
   `anaxi_provenance.db` target table. Direct generalization of §2.0's
   own worked example (there written for `relational_events.pipeline_id`
   against `prov.pipelines`) to EVERY §3 soft-reference column actually
   present in the migrated schema, across every store §3 names. Only
   non-NULL values are checked -- NULL is a legitimate, honestly-unknown
   value (invariant 1), never itself a fault.

2. **Projection pending** (canonical -> legacy, post-cutover only): a
   NATIVE `events` row (`event_type='waking_turn'`, carrying neither an
   `event_migration_status` nor a `legacy_routing_provenance` row --
   the identical native-origin test `verify_native_bundle_contract()`
   already uses in native_provenance_writer.py) exists with no
   matching row yet in `turn_generation_log` or `relational_events` --
   the two stores a native waking turn actually projects into (§4 step
   9). Historical/migrated events are deliberately excluded: their
   backfill is migrate_historical_data.py's separate, already-idempotent
   concern, not this audit's. This direction is NOT extended to
   `nodes`/`active_kardia`/`kardia_history`/`proposals`: nothing in the
   accepted native waking-turn write path (§4, A1 §A1.2) projects into
   those stores, so "pending" has no meaning for them -- extending this
   direction there would invent an expectation the frozen spec and A1
   never created.

Physical topology, exercised for real, not assumed: `turn_generation_log`,
`nodes`, `active_kardia`, `kardia_history`, and `proposals` all live in
the SAME physical file (`anaxi_mind_llama.db`, confirmed directly from
anaxi_protocol_sqlite.py's `_init_core_tables()` and
migrate_historical_data.py's `alter_existing_stores_schema()`, which
applies all five against one `mind_conn`); `relational_events` lives in
a separate physical file (`anaxi_relational_llama.db`). Neither is the
same file as `anaxi_provenance.db`. SQLite does not enforce foreign
keys across attached files. Every check here `ATTACH`es the canonical
file onto the relevant legacy connection and queries across that
attachment, never a Python-side dict/set lookup standing in for the
real cross-file check the frozen spec requires.

=====================================================================
§3 SOFT-REFERENCE COVERAGE -- mechanically verified against the ACTUAL
migrated schema (migrate_historical_data.py's RELATIONAL_EVENTS_
ADDITIVE_COLUMNS / TURN_GENERATION_LOG_ADDITIVE_COLUMNS / NODES_
ADDITIVE_COLUMNS / ACTIVE_KARDIA_ADDITIVE_COLUMNS / KARDIA_HISTORY_
ADDITIVE_COLUMNS / PROPOSALS_ADDITIVE_COLUMNS -- not merely the
frozen-spec prose), not invented:

  relational_events:   pipeline_id, model_revision_id, epoch_id,
                        auth_context_id, event_id
  turn_generation_log:  pipeline_id, model_revision_id, epoch_id,
                        event_id
  nodes:                pipeline_id, epoch_id, asserting_event_id,
                        grounding_assertion_id
  active_kardia:        pipeline_id, epoch_id, source_event_id
  kardia_history:       pipeline_id, epoch_id, source_event_id
  proposals:            pipeline_id, epoch_id

All six match the frozen §3 table exactly for these six stores.

DISCREPANCY, reported rather than invented: §3's generic "any
artifact/table relying on an authorization" row (`relied_upon_consent_
event_id`, `relied_upon_guardian_auth_event_id`,
`relied_upon_persistence_auth_event_id`) has NO corresponding column
anywhere in the actual migrated schema -- confirmed by direct
inspection of all six *_ADDITIVE_COLUMNS constants above; none of them
contains any `relied_upon_*` name. This is not an oversight this audit
should paper over: §3's own text names the join-table alternative as
"preferably" -- "a row in the corresponding `event_relied_upon_*` join
table ... if the artifact is itself represented as an `event`" -- and
that alternative is exactly what was implemented instead
(`event_relied_upon_consent`, `event_relied_upon_guardian_authorization`,
`event_relied_upon_persistence_authorization`, all confirmed to live
INSIDE `anaxi_provenance.db` itself in `native_provenance_writer.py`'s
own `_EVENT_RELIANCE_TABLES` tuple). Those are real, enforced,
same-file foreign keys -- not cross-file soft references -- so they
are outside this audit's own stated scope (§2.0: soft references only)
and are not checked here.
"""

import sqlite3

# (soft-reference column, canonical target table, canonical target PK column)
SOFT_REFERENCE_SPECS = {
    "relational_events": [
        ("pipeline_id", "pipelines", "pipeline_id"),
        ("model_revision_id", "model_revisions", "model_revision_id"),
        ("epoch_id", "architecture_epochs", "epoch_id"),
        ("auth_context_id", "auth_contexts", "auth_context_id"),
        ("event_id", "events", "event_id"),
    ],
    "turn_generation_log": [
        ("pipeline_id", "pipelines", "pipeline_id"),
        ("model_revision_id", "model_revisions", "model_revision_id"),
        ("epoch_id", "architecture_epochs", "epoch_id"),
        ("event_id", "events", "event_id"),
    ],
    "nodes": [
        ("pipeline_id", "pipelines", "pipeline_id"),
        ("epoch_id", "architecture_epochs", "epoch_id"),
        ("asserting_event_id", "events", "event_id"),
        ("grounding_assertion_id", "assertions", "assertion_id"),
    ],
    "active_kardia": [
        ("pipeline_id", "pipelines", "pipeline_id"),
        ("epoch_id", "architecture_epochs", "epoch_id"),
        ("source_event_id", "events", "event_id"),
    ],
    "kardia_history": [
        ("pipeline_id", "pipelines", "pipeline_id"),
        ("epoch_id", "architecture_epochs", "epoch_id"),
        ("source_event_id", "events", "event_id"),
    ],
    "proposals": [
        ("pipeline_id", "pipelines", "pipeline_id"),
        ("epoch_id", "architecture_epochs", "epoch_id"),
    ],
}

# The two stores a native waking turn actually projects into (§4 step 9) --
# the only stores "projection pending" has a defined meaning for.
_WAKING_TURN_PROJECTION_STORES = ("turn_generation_log", "relational_events")


def attach_provenance_db(legacy_conn: sqlite3.Connection, provenance_db_path: str, alias: str = "prov") -> None:
    """Call once per legacy connection before either audit function
    below. Uses a bound parameter for the path (not string
    interpolation) -- SQLite's ATTACH DATABASE statement accepts a
    parameter in the filename position like any other expression."""
    legacy_conn.execute(f"ATTACH DATABASE ? AS {alias}", (provenance_db_path,))


def find_dangling_legacy_references(legacy_conn: sqlite3.Connection, legacy_table: str) -> list:
    """Direction 1. `legacy_conn` must already have anaxi_provenance.db
    ATTACHed as `prov` (attach_provenance_db()). `legacy_table` is one
    of SOFT_REFERENCE_SPECS's keys. Checks EVERY §3 soft-reference
    column implemented for this table (not merely event_id) -- one
    query per column, since each points at a different canonical
    table. Only non-NULL values are checked. Returns one dict per
    dangling reference found -- a real, detected soft-reference
    dangling pointer, to be investigated, never silently ignored
    (§2.0). A single row with two independently-dangling columns
    produces two distinct findings, never collapsed into one."""
    specs = SOFT_REFERENCE_SPECS.get(legacy_table)
    if specs is None:
        raise ValueError(
            f"{legacy_table!r} has no registered §3 soft-reference spec -- "
            f"add it to SOFT_REFERENCE_SPECS rather than guessing its columns."
        )
    findings = []
    for column, target_table, target_column in specs:
        rows = legacy_conn.execute(
            f"""
            SELECT rowid, {column} FROM {legacy_table}
            WHERE {column} IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM prov.{target_table} x WHERE x.{target_column} = {legacy_table}.{column}
              )
            """
        ).fetchall()
        for rowid, value in rows:
            findings.append({
                "fault": "dangling_legacy_reference",
                "store": legacy_table,
                "column": column,
                "target_table": target_table,
                "rowid": rowid,
                "value": value,
                # Retained for callers/tests written against the original
                # event_id-only shape of this finding.
                "event_id": value if column == "event_id" else None,
            })
    return findings


def find_projection_pending(legacy_conn: sqlite3.Connection, legacy_table: str) -> list:
    """Direction 2. `legacy_conn` must already have anaxi_provenance.db
    ATTACHed as `prov`. `legacy_table` must be "turn_generation_log" or
    "relational_events" -- the only two stores a native waking turn
    actually projects into (§4 step 9); any other table raises, rather
    than silently returning an always-empty, meaningless result.
    Returns one dict per native waking_turn event still awaiting its
    projection into this specific legacy store. Never conflated with
    direction 1's dangling-reference rows (a legacy row pointing at a
    nonexistent event) -- this direction starts from the canonical
    side and asks the opposite question."""
    if legacy_table not in _WAKING_TURN_PROJECTION_STORES:
        raise ValueError(
            f"{legacy_table!r} is not a native waking-turn projection target "
            f"({_WAKING_TURN_PROJECTION_STORES}) -- 'projection pending' has no "
            f"defined meaning for it; refusing to invent one."
        )
    rows = legacy_conn.execute(
        f"""
        SELECT e.event_id FROM prov.events e
        WHERE e.event_type = 'waking_turn'
          AND NOT EXISTS (SELECT 1 FROM prov.event_migration_status m WHERE m.event_id = e.event_id)
          AND NOT EXISTS (SELECT 1 FROM prov.legacy_routing_provenance l WHERE l.event_id = e.event_id)
          AND NOT EXISTS (SELECT 1 FROM {legacy_table} t WHERE t.event_id = e.event_id)
        """
    ).fetchall()
    return [{"fault": "projection_pending", "store": legacy_table, "event_id": r[0]} for r in rows]
