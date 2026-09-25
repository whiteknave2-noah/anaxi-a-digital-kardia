"""
Anaxi -- Bounded hippocampus core, Slice B: standing hippocampal audit.

Host-side, entirely read-only. Reports exact counts across twelve
required families. Never repairs, deletes, relabels, rebuilds,
synchronizes, or otherwise mutates any hippocampal or source store.

Deliberately does NOT reuse hippocampus_store.py's ingestion/
classification functions (_enumerate_component_candidates,
_derive_authentication_status, _derive_session, etc.) -- this module
independently re-derives every expected value from the same frozen
rules, matching the established project convention
(standing_provenance_audit.py) that an audit sharing its ingestion
code's actual logic path cannot catch a bug in that logic. The only
things reused from hippocampus_store.py are pure, frozen, versioned
constants and the deterministic item-ID function itself -- sharing a
frozen ID derivation is not sharing ingestion decision logic.
"""

import dataclasses
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hippocampus_store import (
    derive_hippocampal_item_id, SCHEMA_VERSION, ITEM_ID_VERSION,
    HippocampusUnavailableError, HippocampusSchemaError,
)
from provenance_schema import derive_historical_event_id

HISTORICAL_JSONL_SOURCE_ID = "anaxi_log.jsonl"

# v1 eligible scope, documented explicitly per the governing task's
# request: every row in event_components is in scope -- there is no
# event_type-based (or other) exclusion anywhere in the real ingestion
# path (hippocampus_store._enumerate_component_candidates scans the
# entire table unconditionally), and every canonical event currently
# has event_type='waking_turn' (confirmed directly against
# native_provenance_writer.py and migrate_historical_data.py, neither
# of which ever inserts any other event_type). If a second event_type
# is ever introduced, this scoping rule -- "all of event_components" --
# would need deliberate reconsideration, not a silent narrowing here.
_SUPPORTED_COMPONENT_KINDS = ("bounded_clause", "conversational_prose", "assembled_reply_unsplit_historical")
_HOST_SYSTEM_ACTOR_TYPE = "host_system"
_CLARK_AGENT_ACTOR_TYPE = "clark_agent"

AUDIT_FAMILIES = (
    "dangling_event_id",
    "missing_source_record",
    "content_sha256_mismatch",
    "historical_event_id_reconstruction_mismatch",
    "duplicate_logical_item",
    "invalid_memory_kind_mapping",
    "invalid_attribution_mapping",
    "invalid_authentication_mapping",
    "invalid_session_resolution",
    "ambiguous_session_resolution",
    "unsupported_component_kind",
    "fts_item_mismatch",
)


@dataclasses.dataclass(frozen=True)
class AuditResult:
    counts: dict
    details: dict

    def is_clean(self) -> bool:
        return all(self.counts[f] == 0 for f in AUDIT_FAMILIES)


# =============================================================================
# Read-only access (independent of hippocampus_store.py's own helpers).
# =============================================================================
def _readonly_uri(path: str) -> str:
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


def _open_readonly(path: str, label: str) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise HippocampusUnavailableError(f"{label} not found: {path!r}")
    try:
        conn = sqlite3.connect(_readonly_uri(path), uri=True)
        conn.execute("PRAGMA query_only = ON;")
        conn.execute("SELECT count(*) FROM sqlite_master;")
    except sqlite3.OperationalError as e:
        raise HippocampusUnavailableError(f"could not open {label} read-only: {path!r} ({e})") from e
    return conn


def _validate_hippocampus_schema(conn: sqlite3.Connection) -> None:
    try:
        rows = dict(conn.execute("SELECT key, value FROM hippocampus_meta;").fetchall())
    except sqlite3.OperationalError as e:
        raise HippocampusSchemaError(f"hippocampal database is missing required schema: {e}") from e
    if rows.get("schema_version") != str(SCHEMA_VERSION) or rows.get("item_id_version") != str(ITEM_ID_VERSION):
        raise HippocampusSchemaError(
            f"unexpected schema/item_id version: {rows.get('schema_version')!r}/{rows.get('item_id_version')!r}"
        )


# =============================================================================
# Independent re-derivation helpers (frozen rules, reimplemented).
# =============================================================================
def _expected_memory_kind(source_store: str, component_kind: Optional[str]) -> Optional[str]:
    if source_store == "event_components":
        if component_kind == "bounded_clause":
            return "mechanical_record"
        if component_kind == "conversational_prose":
            return "clark_expression"
        if component_kind == "assembled_reply_unsplit_historical":
            return "mixed_historical_expression"
        return None  # unsupported kind indexed -- no valid mapping exists at all
    if source_store in ("relational_events", "anaxi_log_jsonl"):
        return "human_expression"
    return None


def _resolve_actor_subtype(prov_conn: sqlite3.Connection, actor_id: str):
    row = prov_conn.execute(
        "SELECT a.actor_type, "
        "(SELECT 1 FROM actor_host_system WHERE actor_id = a.actor_id) IS NOT NULL, "
        "(SELECT 1 FROM actor_clark_agent WHERE actor_id = a.actor_id) IS NOT NULL "
        "FROM actors a WHERE a.actor_id = ?",
        (actor_id,),
    ).fetchone()
    if row is None:
        return None, False, False
    return row[0], bool(row[1]), bool(row[2])


def _derive_authentication_status(prov_conn: sqlite3.Connection, event_id: str) -> str:
    migration_row = prov_conn.execute(
        "SELECT migration_status FROM event_migration_status WHERE event_id = ?", (event_id,)
    ).fetchone()
    if migration_row is not None and migration_row[0] == "pre_authentication_layer":
        return "pre_authentication_layer"
    event_row = prov_conn.execute("SELECT auth_context_id FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if event_row is None or event_row[0] is None:
        return "unknown"
    auth_row = prov_conn.execute(
        "SELECT auth_state FROM auth_contexts WHERE auth_context_id = ?", (event_row[0],)
    ).fetchone()
    if auth_row is None:
        return "unknown"
    return auth_row[0] if auth_row[0] in ("unauthenticated", "claimed_only", "authenticated") else "unknown"


def _is_historical_event(prov_conn: sqlite3.Connection, event_id: str) -> bool:
    row = prov_conn.execute(
        "SELECT 1 FROM event_migration_status WHERE event_id = ? AND migration_status = 'pre_authentication_layer';",
        (event_id,),
    ).fetchone()
    return row is not None


def _derive_session(prov_conn: sqlite3.Connection, event_id: str, pipeline_id: Optional[str],
                     occurred_at: int, is_historical: bool):
    """Amendment S1, independently re-derived here -- deliberately never
    calls into hippocampus_store.py's own _derive_session(), matching
    this module's established independent-derivation discipline (see
    module docstring). Same canonical-link rule: events.auth_context_id
    -> auth_contexts.session_id -> sessions.session_id, never temporal
    overlap. occurred_at is kept for call-site/signature symmetry with
    the ingestion side; unused by S1.

    Returns (session_id, session_resolution, integrity_ok, reason).
    integrity_ok=False means the canonical reference ITSELF is broken
    (a dangling session_id, or a pipeline_id mismatch) -- this is
    distinct from "no canonical session assigned" (integrity_ok=True,
    resolution='unknown'), and the audit never raises for it (an audit
    counts every problem in one pass; it does not abort on the first
    one) -- the caller folds it into invalid_session_resolution."""
    if is_historical:
        return None, "unknown", True, None

    auth_context_row = prov_conn.execute(
        "SELECT auth_context_id FROM events WHERE event_id = ?;", (event_id,)
    ).fetchone()
    if auth_context_row is None or auth_context_row[0] is None:
        return None, "unknown", True, None
    auth_context_id = auth_context_row[0]

    session_id_row = prov_conn.execute(
        "SELECT session_id FROM auth_contexts WHERE auth_context_id = ?;", (auth_context_id,)
    ).fetchone()
    if session_id_row is None or session_id_row[0] is None:
        return None, "unknown", True, None
    session_id = session_id_row[0]

    session_row = prov_conn.execute(
        "SELECT pipeline_id FROM sessions WHERE session_id = ?;", (session_id,)
    ).fetchone()
    if session_row is None:
        return None, "unknown", False, "canonical session_id does not resolve to any sessions row"
    session_pipeline_id = session_row[0]
    if pipeline_id is not None and session_pipeline_id != pipeline_id:
        return None, "unknown", False, "canonical session pipeline_id disagrees with the event's own pipeline_id"

    return session_id, "resolved", True, None


# =============================================================================
# The audit itself.
# =============================================================================
def run_standing_hippocampus_audit(hippocampus_db_path: str, provenance_db_path: str,
                                    relational_db_path: str, historical_jsonl_path: str) -> AuditResult:
    counts = {f: 0 for f in AUDIT_FAMILIES}
    details = {f: [] for f in AUDIT_FAMILIES}

    hip_conn = _open_readonly(hippocampus_db_path, "hippocampal database")
    try:
        _validate_hippocampus_schema(hip_conn)
        prov_conn = _open_readonly(provenance_db_path, "canonical provenance database")
        try:
            rel_conn = _open_readonly(relational_db_path, "relational database")
            try:
                jsonl_lines = None
                if os.path.isfile(historical_jsonl_path):
                    with open(historical_jsonl_path, "r", encoding="utf-8") as f:
                        jsonl_lines = f.readlines()

                items = hip_conn.execute(
                    "SELECT row_id, item_id, event_id, source_store, source_locator, source_line_index, "
                    "component_kind, content_text, content_sha256, memory_kind, attribution_status, "
                    "authentication_status, creator_actor_id, creator_actor_type, pipeline_id, occurred_at, "
                    "session_id, session_resolution FROM hippocampal_items ORDER BY item_id;"
                ).fetchall()
                cols = ("row_id", "item_id", "event_id", "source_store", "source_locator", "source_line_index",
                        "component_kind", "content_text", "content_sha256", "memory_kind", "attribution_status",
                        "authentication_status", "creator_actor_id", "creator_actor_type", "pipeline_id",
                        "occurred_at", "session_id", "session_resolution")

                seen_item_ids = {}

                for raw in items:
                    it = dict(zip(cols, raw))
                    item_id = it["item_id"]

                    # --- 5. duplicate_logical_item ---
                    if item_id in seen_item_ids:
                        counts["duplicate_logical_item"] += 1
                        details["duplicate_logical_item"].append({"item_id": item_id, "reason": "duplicate row_id for same item_id"})
                    seen_item_ids[item_id] = it["row_id"]
                    recomputed_item_id = derive_hippocampal_item_id(it["event_id"], it["source_store"], it["source_locator"])
                    if recomputed_item_id != item_id:
                        counts["duplicate_logical_item"] += 1
                        details["duplicate_logical_item"].append({
                            "item_id": item_id, "reason": "stored item_id disagrees with deterministic recomputation",
                            "recomputed": recomputed_item_id,
                        })

                    # --- 1. dangling_event_id ---
                    event_row = prov_conn.execute(
                        "SELECT occurred_at, pipeline_id FROM events WHERE event_id = ?;", (it["event_id"],)
                    ).fetchone()
                    if event_row is None:
                        counts["dangling_event_id"] += 1
                        details["dangling_event_id"].append({"item_id": item_id, "event_id": it["event_id"]})
                        continue  # nothing further can be checked meaningfully without a canonical event
                    canonical_occurred_at, canonical_pipeline_id = event_row

                    # --- 2/3. missing_source_record / content_sha256_mismatch ---
                    source_text = None
                    if it["source_store"] == "event_components":
                        row = prov_conn.execute(
                            "SELECT event_id, component_text, content_sha256, component_kind, creator_actor_id, "
                            "authorship_resolution FROM event_components WHERE component_id = ?;",
                            (it["source_locator"],),
                        ).fetchone()
                        if row is None or row[0] != it["event_id"]:
                            counts["missing_source_record"] += 1
                            details["missing_source_record"].append({"item_id": item_id, "source_store": it["source_store"]})
                        else:
                            _, component_text, canonical_hash, component_kind, creator_actor_id, authorship_resolution = row
                            source_text = component_text
                            recomputed = hashlib.sha256(component_text.encode("utf-8")).hexdigest()
                            if recomputed != it["content_sha256"] or recomputed != canonical_hash:
                                counts["content_sha256_mismatch"] += 1
                                details["content_sha256_mismatch"].append({"item_id": item_id})

                            # --- 6. invalid_memory_kind_mapping ---
                            expected_kind = _expected_memory_kind("event_components", component_kind)
                            if expected_kind is None or it["memory_kind"] != expected_kind:
                                counts["invalid_memory_kind_mapping"] += 1
                                details["invalid_memory_kind_mapping"].append({"item_id": item_id, "component_kind": component_kind})

                            # --- 7. invalid_attribution_mapping ---
                            attribution_ok = True
                            if component_kind in ("bounded_clause", "conversational_prose"):
                                if creator_actor_id is None or it["creator_actor_id"] != creator_actor_id:
                                    attribution_ok = False
                                else:
                                    actor_type, has_host, has_clark = _resolve_actor_subtype(prov_conn, creator_actor_id)
                                    expected_type = _HOST_SYSTEM_ACTOR_TYPE if component_kind == "bounded_clause" else _CLARK_AGENT_ACTOR_TYPE
                                    subtype_ok = has_host if component_kind == "bounded_clause" else has_clark
                                    if actor_type != expected_type or not subtype_ok or it["creator_actor_type"] != actor_type:
                                        attribution_ok = False
                                if it["attribution_status"] != authorship_resolution:
                                    attribution_ok = False
                            elif component_kind == "assembled_reply_unsplit_historical":
                                if it["creator_actor_id"] is not None or it["creator_actor_type"] is not None:
                                    attribution_ok = False
                                if it["attribution_status"] != authorship_resolution:
                                    attribution_ok = False
                            if not attribution_ok:
                                counts["invalid_attribution_mapping"] += 1
                                details["invalid_attribution_mapping"].append({"item_id": item_id})

                    elif it["source_store"] == "relational_events":
                        row = rel_conn.execute(
                            "SELECT event_id, external_observation FROM relational_events WHERE id = ?;",
                            (it["source_locator"],),
                        ).fetchone()
                        if row is None or row[0] != it["event_id"]:
                            counts["missing_source_record"] += 1
                            details["missing_source_record"].append({"item_id": item_id, "source_store": it["source_store"]})
                        else:
                            external_observation = row[1]
                            source_text = external_observation
                            recomputed = hashlib.sha256((external_observation or "").encode("utf-8")).hexdigest()
                            if recomputed != it["content_sha256"]:
                                counts["content_sha256_mismatch"] += 1
                                details["content_sha256_mismatch"].append({"item_id": item_id})
                            if it["memory_kind"] != "human_expression":
                                counts["invalid_memory_kind_mapping"] += 1
                                details["invalid_memory_kind_mapping"].append({"item_id": item_id})
                            if it["creator_actor_id"] is not None or it["creator_actor_type"] is not None or it["attribution_status"] != "unknown":
                                counts["invalid_attribution_mapping"] += 1
                                details["invalid_attribution_mapping"].append({"item_id": item_id})

                    elif it["source_store"] == "anaxi_log_jsonl":
                        line_index = it["source_line_index"]
                        line_exists = jsonl_lines is not None and line_index is not None and 0 <= line_index < len(jsonl_lines)
                        if not line_exists:
                            counts["missing_source_record"] += 1
                            details["missing_source_record"].append({"item_id": item_id, "source_store": it["source_store"]})
                        else:
                            raw_line = jsonl_lines[line_index]
                            line = raw_line.rstrip("\n")
                            reconstructed_event_id = derive_historical_event_id(
                                HISTORICAL_JSONL_SOURCE_ID, line.encode("utf-8"), line_index
                            )
                            recon_event_row = prov_conn.execute(
                                "SELECT 1 FROM events WHERE event_id = ?;", (reconstructed_event_id,)
                            ).fetchone()
                            if reconstructed_event_id != it["event_id"] or recon_event_row is None:
                                counts["historical_event_id_reconstruction_mismatch"] += 1
                                details["historical_event_id_reconstruction_mismatch"].append({"item_id": item_id})
                            else:
                                try:
                                    entry = json.loads(line)
                                    prompt_text = entry["prompt"]
                                except (json.JSONDecodeError, KeyError):
                                    prompt_text = None
                                if prompt_text is not None:
                                    source_text = prompt_text
                                    recomputed = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
                                    if recomputed != it["content_sha256"]:
                                        counts["content_sha256_mismatch"] += 1
                                        details["content_sha256_mismatch"].append({"item_id": item_id})
                            if it["memory_kind"] != "human_expression":
                                counts["invalid_memory_kind_mapping"] += 1
                                details["invalid_memory_kind_mapping"].append({"item_id": item_id})
                            if it["creator_actor_id"] is not None or it["creator_actor_type"] is not None or it["attribution_status"] != "unknown":
                                counts["invalid_attribution_mapping"] += 1
                                details["invalid_attribution_mapping"].append({"item_id": item_id})

                    # --- 8. invalid_authentication_mapping ---
                    expected_auth = _derive_authentication_status(prov_conn, it["event_id"])
                    if it["authentication_status"] != expected_auth:
                        counts["invalid_authentication_mapping"] += 1
                        details["invalid_authentication_mapping"].append({"item_id": item_id, "expected": expected_auth, "stored": it["authentication_status"]})

                    # --- 9/10. session (S1: canonical, not temporal) ---
                    historical = _is_historical_event(prov_conn, it["event_id"])
                    expected_session_id, expected_session_resolution, session_integrity_ok, session_integrity_reason = (
                        _derive_session(prov_conn, it["event_id"], canonical_pipeline_id, canonical_occurred_at, historical)
                    )
                    if not session_integrity_ok:
                        # The canonical reference itself is broken (dangling
                        # session_id, or a pipeline_id mismatch) -- reported
                        # via the existing invalid_session_resolution family
                        # rather than a new one, per the reuse-first rule;
                        # the reason string distinguishes it from an ordinary
                        # stored-vs-expected disagreement in the details payload.
                        counts["invalid_session_resolution"] += 1
                        details["invalid_session_resolution"].append({
                            "item_id": item_id, "reason": session_integrity_reason,
                        })
                    elif (it["session_id"], it["session_resolution"]) != (expected_session_id, expected_session_resolution):
                        counts["invalid_session_resolution"] += 1
                        details["invalid_session_resolution"].append({
                            "item_id": item_id, "expected": (expected_session_id, expected_session_resolution),
                            "stored": (it["session_id"], it["session_resolution"]),
                        })
                    # ambiguous_session_resolution: structurally unreachable
                    # via S1's canonical rule now (temporal overlap is never
                    # consulted for a native event, and historical items
                    # never resolve to "ambiguous" either) -- this check is
                    # kept, not deleted, so the family's counting mechanism
                    # and AUDIT_FAMILIES taxonomy stay intact rather than
                    # silently pruned.
                    if expected_session_resolution == "ambiguous":
                        counts["ambiguous_session_resolution"] += 1
                        details["ambiguous_session_resolution"].append({"item_id": item_id})

                # --- 11. unsupported_component_kind: scan the FULL canonical
                # scope (see module-level scoping-rule comment), not merely
                # indexed items. ---
                for component_id, component_kind, event_id in prov_conn.execute(
                    "SELECT component_id, component_kind, event_id FROM event_components;"
                ).fetchall():
                    if component_kind not in _SUPPORTED_COMPONENT_KINDS:
                        counts["unsupported_component_kind"] += 1
                        details["unsupported_component_kind"].append({
                            "component_id": component_id, "component_kind": component_kind, "event_id": event_id,
                        })

                # --- 12. fts_item_mismatch ---
                # IMPORTANT: hippocampal_items_fts is an EXTERNAL-CONTENT
                # FTS5 table. A plain `SELECT rowid, content_text FROM
                # hippocampal_items_fts` does NOT read the FTS index at
                # all -- for a content= table it re-derives every column
                # live from the base table by content_rowid, so it would
                # report perfect parity even if the FTS index were
                # completely empty or stale (confirmed empirically). True
                # index membership lives in FTS5's own `<table>_docsize`
                # shadow table (one row per rowid actually present in the
                # index), and true content freshness can only be checked
                # via a real MATCH query, not a column read.
                items_rows = dict(hip_conn.execute("SELECT row_id, content_text FROM hippocampal_items;").fetchall())
                indexed_row_ids = {r[0] for r in hip_conn.execute("SELECT id FROM hippocampal_items_fts_docsize;").fetchall()}

                for row_id, content_text in items_rows.items():
                    if row_id not in indexed_row_ids:
                        counts["fts_item_mismatch"] += 1
                        details["fts_item_mismatch"].append({"row_id": row_id, "reason": "missing FTS index entry (absent from _docsize)"})
                        continue
                    if content_text.strip():
                        quoted = '"' + content_text.replace('"', '""') + '"'
                        match = hip_conn.execute(
                            "SELECT 1 FROM hippocampal_items_fts WHERE hippocampal_items_fts MATCH ? AND rowid = ?;",
                            (quoted, row_id),
                        ).fetchone()
                        if match is None:
                            counts["fts_item_mismatch"] += 1
                            details["fts_item_mismatch"].append({"row_id": row_id, "reason": "indexed FTS tokens do not match current content_text"})
                for row_id in indexed_row_ids:
                    if row_id not in items_rows:
                        counts["fts_item_mismatch"] += 1
                        details["fts_item_mismatch"].append({"row_id": row_id, "reason": "orphaned FTS index entry (indexed rowid has no base row)"})

            finally:
                rel_conn.close()
        finally:
            prov_conn.close()
    finally:
        hip_conn.close()

    return AuditResult(counts=counts, details=details)
