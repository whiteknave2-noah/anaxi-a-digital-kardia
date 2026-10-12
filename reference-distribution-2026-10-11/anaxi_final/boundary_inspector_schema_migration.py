"""BOUNDARY INSPECTOR v1 -- additive schema for boundary-inspection
provenance.

Additive, idempotent, self-contained. Registers the boundary-inspection
event type and its component kinds in one tiny guard table
(host_boundary_registry) so the invocation's own constants have a
durable, verifiable registration home -- it is NOT a parallel semantic
history store. Boundary-inspection state itself lives in the existing
canonical events/event_components rows:

  events:            one clark_boundary_query row per recorded query
                     occurrence (a plain rowid table, so rowid order
                     is the durable FIFO order the pending selector
                     uses -- never wall-clock timestamps).
  event_components:  boundary_query_spec (seq 0),
                     boundary_inspection_result (seq 1, appended in a
                     SEPARATE later transaction than the query row),
                     boundary_inspection_delivered (seq 2, appended
                     only after a waking turn whose real Pass-2 input
                     carried the result AND committed canonically);
                     and, on the CARRYING waking_turn event itself,
                     boundary_inspection_result_carriage -- written in
                     the same canonical transaction as that waking turn
                     from the real Pass-2 composition's surviving source
                     id, then read back mechanically by
                     record_boundary_inspection_delivered().

Mirrors the OC0/SLP2/WTR0 additive-migration convention (guarded
CREATE ... IF NOT EXISTS, one transaction, rollback on error) and
provenance_schema.install_identity_guards where applicable. Never
invoked against a live/production file by this job -- callers point it
only at synthetic or rehearsal database paths.
"""
import sqlite3
import time

from boundary_rationale_registry import (
    BOUNDARY_INSPECTION_EVENT_TYPE,
    BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND,
    BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND,
    BOUNDARY_INSPECTION_DELIVERED_COMPONENT_KIND,
    BOUNDARY_INSPECTION_QUERY_SPEC_COMPONENT_KIND,
)

REGISTRY_TABLE = "host_boundary_registry"

# The exact rows apply_additive_migration() registers. One row per
# canonical kind/type the invocation may create; INSERT OR IGNORE keeps
# registration idempotent.
_REGISTRY_ROWS = (
    (BOUNDARY_INSPECTION_EVENT_TYPE, "event_type"),
    (BOUNDARY_INSPECTION_QUERY_SPEC_COMPONENT_KIND, "component_kind"),
    (BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND, "component_kind"),
    (BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND, "component_kind"),
    (BOUNDARY_INSPECTION_DELIVERED_COMPONENT_KIND, "component_kind"),
)

NEW_TABLES_DDL = f"""
CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} (
    entry_key          TEXT PRIMARY KEY,
    event_type         TEXT,
    component_kind     TEXT,
    registered_at      INTEGER NOT NULL,
    notes              TEXT,
    CHECK ((event_type IS NULL) != (component_kind IS NULL))
);
CREATE TRIGGER IF NOT EXISTS trg_host_boundary_registry_no_update
    BEFORE UPDATE ON {REGISTRY_TABLE}
    BEGIN SELECT RAISE(ABORT, 'host_boundary_registry is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_host_boundary_registry_no_delete
    BEFORE DELETE ON {REGISTRY_TABLE}
    BEGIN SELECT RAISE(ABORT, 'host_boundary_registry is append-only'); END;
"""


def apply_additive_migration(db_path: str) -> dict:
    """Idempotent additive migration. Safe against a fresh database or
    one already carrying this exact registry (each converges on the
    identical final schema; no existing row is ever rewritten)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        existing_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + NEW_TABLES_DDL)
            registered = 0
            for entry_key, kind in _REGISTRY_ROWS:
                if conn.execute(
                    "SELECT 1 FROM host_boundary_registry WHERE entry_key = ?", (entry_key,)
                ).fetchone() is not None:
                    continue
                conn.execute(
                    "INSERT INTO host_boundary_registry (entry_key, event_type, component_kind, "
                    "registered_at, notes) VALUES (?, ?, ?, ?, ?)",
                    (entry_key,
                     entry_key if kind == "event_type" else None,
                     entry_key if kind == "component_kind" else None,
                     int(time.time()),
                     "boundary-inspector v1 additive registration"),
                )
                registered += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        existing_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        return {
            "new_tables_created": sorted(existing_after - existing_before),
            "registry_entries_registered": registered,
            "triggers": sorted(t for t in (
                "trg_host_boundary_registry_no_update",
                "trg_host_boundary_registry_no_delete",
            ) if t in triggers),
        }
    finally:
        conn.close()


def verify_migration_state(db_path: str) -> dict:
    """Read-only structural verification, mirroring SLP2's
    verify_migration_state() convention. Never modifies anything."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        return {
            "has_registry_table": REGISTRY_TABLE in tables,
            "has_event_type_registered": (
                conn.execute(
                    "SELECT 1 FROM host_boundary_registry WHERE entry_key=? AND event_type=?",
                    (BOUNDARY_INSPECTION_EVENT_TYPE, BOUNDARY_INSPECTION_EVENT_TYPE),
                ).fetchone() is not None
                if REGISTRY_TABLE in tables else False
            ),
            "has_result_component_kind_registered": (
                conn.execute(
                    "SELECT 1 FROM host_boundary_registry WHERE entry_key=? AND component_kind=?",
                    (BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND, BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND),
                ).fetchone() is not None
                if REGISTRY_TABLE in tables else False
            ),
            "has_carriage_component_kind_registered": (
                conn.execute(
                    "SELECT 1 FROM host_boundary_registry WHERE entry_key=? AND component_kind=?",
                    (BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND,
                     BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND),
                ).fetchone() is not None
                if REGISTRY_TABLE in tables else False
            ),
            "has_delivered_component_kind_registered": (
                conn.execute(
                    "SELECT 1 FROM host_boundary_registry WHERE entry_key=? AND component_kind=?",
                    (BOUNDARY_INSPECTION_DELIVERED_COMPONENT_KIND, BOUNDARY_INSPECTION_DELIVERED_COMPONENT_KIND),
                ).fetchone() is not None
                if REGISTRY_TABLE in tables else False
            ),
            "has_registry_guard_triggers": (
                "trg_host_boundary_registry_no_update" in triggers
                and "trg_host_boundary_registry_no_delete" in triggers
            ),
        }
    finally:
        conn.close()
