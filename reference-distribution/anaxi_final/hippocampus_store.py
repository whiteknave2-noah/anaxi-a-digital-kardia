"""
Anaxi -- Bounded hippocampus core, Slice A: derived-store foundation.

Owns the hippocampal database/schema, deterministic item identity,
read-only source access, the three frozen v1 ingestion paths (canonical
component, native relational human expression, historical JSONL human
expression), epistemic/authentication classification, session
derivation, deterministic scan-and-upsert catch-up, source-drift/
disappearance detection, and rebuild.

Does NOT own retrieval ranking, prompt querying, Kardia/context
construction, model behavior, emotional/affective weighting, vector
retrieval, or longitudinal instrumentation -- none of that is Slice A.

No model/API dependency anywhere in this module. Every connection to an
existing source SQLite database (anaxi_provenance.db,
anaxi_relational_llama.db) is opened read-only via a `file:...?mode=ro`
URI; only the hippocampal database itself is ever opened writable. The
historical JSONL is opened read-only.

There is no enable/disable feature flag anywhere in this module, by
design (see the governing implementation authorization for Slice A).
"""

import dataclasses
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import struct
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from provenance_schema import derive_historical_event_id

# =============================================================================
# Path configuration -- one small immutable object. Production defaults
# resolve relative to this file's own directory, never process CWD.
# Tests always pass explicit temporary paths.
# =============================================================================
@dataclasses.dataclass(frozen=True)
class HippocampusPaths:
    provenance_db_path: str
    relational_db_path: str
    historical_jsonl_path: str
    hippocampus_db_path: str

    @staticmethod
    def production_defaults() -> "HippocampusPaths":
        base = os.path.dirname(os.path.abspath(__file__))
        return HippocampusPaths(
            provenance_db_path=os.path.join(base, "anaxi_provenance.db"),
            relational_db_path=os.path.join(base, "anaxi_relational_llama.db"),
            historical_jsonl_path=os.path.join(base, "anaxi_log.jsonl"),
            hippocampus_db_path=os.path.join(base, "anaxi_hippocampus.db"),
        )


# =============================================================================
# Error taxonomy -- externally meaningful failure categories, never
# silently downgraded into an empty result.
# =============================================================================
class HippocampusUnavailableError(Exception):
    """A required database/file/capability is missing or cannot be
    opened at all (source file absent, hippocampal DB absent when sync
    requires it to already exist, FTS5 module unavailable)."""
    pass


class HippocampusSchemaError(Exception):
    """The hippocampal database exists but its schema/meta state is
    not the expected v1 shape (missing/mismatched schema_version or
    item_id_version, missing required table)."""
    pass


class HippocampusGroundingError(Exception):
    """A source record that claims to be indexable fails self-
    consistency or canonical-grounding verification (e.g. a canonical
    component's recomputed content hash disagrees with its own stored
    content_sha256)."""
    pass


class HippocampusSourceDriftError(Exception):
    """An already-indexed hippocampal item's current source candidate
    disagrees with what is stored on an immutable derived field, or an
    already-indexed item's source candidate has disappeared entirely.
    Never silently repaired, rewritten, or deleted."""
    pass


class HippocampusIntegrityError(Exception):
    """Canonical actor structure contradicts an expected subtype
    relationship this module's v1 whitelist mapping depends on (e.g. a
    component whose creator is typed host_system in `actors` but has
    no corresponding `actor_host_system` subtype row, or vice versa)."""
    pass


# =============================================================================
# CHECK vocabularies (also the module's own single source of truth for
# the hippocampal_items schema's CHECK constraints).
# =============================================================================
SOURCE_STORE_VALUES = ("event_components", "relational_events", "anaxi_log_jsonl", "sleep_derivations")
MEMORY_KIND_VALUES = (
    "mechanical_record", "human_expression", "clark_expression",
    "mixed_historical_expression", "derived_inference", "unknown",
)
ATTRIBUTION_STATUS_VALUES = ("resolved", "unresolved_mixed_historical", "unknown")
AUTHENTICATION_STATUS_VALUES = (
    "unauthenticated", "claimed_only", "authenticated", "pre_authentication_layer", "unknown",
)
SESSION_RESOLUTION_VALUES = ("resolved", "unknown", "ambiguous")

SCHEMA_VERSION = 2
ITEM_ID_VERSION = 1

HISTORICAL_JSONL_SOURCE_ID = "anaxi_log.jsonl"

# v1 canonical-component whitelist -- component kinds outside this set
# are not guessed at; they are reported as unsupported, never ingested.
#
# WSP2-P4-H1: an explicit, narrow ALLOWLIST addition (never "all public
# events") of the smallest set of canonical PUBLIC workspace/roaming
# component kinds that carry genuine historical content -- not action
# metadata alone unless that is genuinely all a given record has, and
# never internal telemetry/retry/budget/failure-counter bookkeeping.
# Each is structurally PUBLIC-ONLY by construction: workspace_private.py
# has no import of workspace_episode_provenance and writes none of
# these component kinds anywhere (confirmed by source audit) -- there
# is no code path that could ever place private content under one of
# these four kinds.
#   resource_encounter_fact   -- workspace_resource_encounter (CAP2E):
#       the bounded mechanical fact of a genuine public resource
#       delivery (resource_class/relative_path/modality/delivered_
#       portion/content_sha256/...). Host-authored.
#   roaming_action_fact       -- workspace_roaming_public_action: the
#       bounded mechanical fact of one public roaming action (resource_
#       class/action/relative_path/success). Host-authored.
#   roaming_external_result   -- workspace_roaming_public_action: the
#       actual bounded external/mechanical result text of a public
#       roaming action (e.g. a library.read's own bounded extracted
#       text) -- genuine delivered CONTENT, not metadata. Host-authored.
#   roaming_journal_text      -- workspace_roaming_public_action:
#       Clark's own authored journal text from a public roaming journal
#       append -- genuine Clark-authored content. Clark-authored.
# Deliberately EXCLUDED (bookkeeping/telemetry, not content):
# roaming_wait_fact, roaming_termination_fact, roaming_handoff_note,
# roaming_handoff_source_excerpt, workspace_roaming_run_id,
# background_lifecycle_control_fact, delivered_waking_event_id,
# resource_encounter_continuity_delivered marker, episode_context_
# delivered, roaming_episode_actor.
_HOST_AUTHORED_COMPONENT_KINDS = frozenset({
    "bounded_clause", "resource_encounter_fact", "roaming_action_fact", "roaming_external_result",
})
_CLARK_AUTHORED_COMPONENT_KINDS = frozenset({"conversational_prose", "roaming_journal_text"})
_SUPPORTED_COMPONENT_KINDS = tuple(
    _HOST_AUTHORED_COMPONENT_KINDS | _CLARK_AUTHORED_COMPONENT_KINDS | {"assembled_reply_unsplit_historical"}
)


# =============================================================================
# Deterministic hippocampal item identity (frozen v1 construction).
# =============================================================================
_ITEM_ID_DOMAIN = b"anaxi-hippocampus-item-v1\x00"


def derive_hippocampal_item_id(event_id: str, source_store: str, source_locator: str) -> str:
    """Domain-separated, length-prefixed construction: domain bytes,
    then for event_id/source_store/source_locator in order, an 8-byte
    big-endian length prefix followed by the UTF-8 bytes themselves.
    SHA-256 over the complete preimage; "hip-" + first 26 hex chars of
    the digest. Depends on nothing but these three logical-identity
    strings -- not content, not DB rowid, not insertion order, not
    clock, not random state. Changed bytes at one logical source stay
    one item (source drift), never a second identity."""
    preimage = bytearray(_ITEM_ID_DOMAIN)
    for value in (event_id, source_store, source_locator):
        encoded = value.encode("utf-8")
        preimage += struct.pack(">Q", len(encoded))
        preimage += encoded
    digest = hashlib.sha256(bytes(preimage)).hexdigest()
    return "hip-" + digest[:26]


# =============================================================================
# Read-only source access.
# =============================================================================
def _readonly_uri(path: str) -> str:
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


def _open_source_readonly(path: str, label: str) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise HippocampusUnavailableError(f"{label} not found: {path!r}")
    try:
        conn = sqlite3.connect(_readonly_uri(path), uri=True)
        conn.execute("PRAGMA query_only = ON;")
        # Cheap, real proof the connection is genuinely usable read-only
        # against this file (also surfaces a not-a-database file early).
        conn.execute("SELECT count(*) FROM sqlite_master;")
    except sqlite3.OperationalError as e:
        raise HippocampusUnavailableError(f"could not open {label} read-only: {path!r} ({e})") from e
    return conn


def _assert_fts5_available() -> None:
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE fts5_probe USING fts5(x);")
    except sqlite3.OperationalError as e:
        raise HippocampusUnavailableError(f"SQLite FTS5 module is not available: {e}") from e
    finally:
        probe.close()


# =============================================================================
# Hippocampal DB schema.
# =============================================================================
_SCHEMA_SQL = f"""
CREATE TABLE hippocampus_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE hippocampal_items (
    row_id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    source_store TEXT NOT NULL CHECK (source_store IN {SOURCE_STORE_VALUES!r}),
    source_locator TEXT NOT NULL,
    source_line_index INTEGER NULL,
    component_kind TEXT NULL,
    content_text TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    memory_kind TEXT NOT NULL CHECK (memory_kind IN {MEMORY_KIND_VALUES!r}),
    attribution_status TEXT NOT NULL CHECK (attribution_status IN {ATTRIBUTION_STATUS_VALUES!r}),
    authentication_status TEXT NOT NULL CHECK (authentication_status IN {AUTHENTICATION_STATUS_VALUES!r}),
    creator_actor_id TEXT NULL,
    creator_actor_type TEXT NULL,
    pipeline_id TEXT NULL,
    occurred_at INTEGER NOT NULL,
    session_id TEXT NULL,
    session_resolution TEXT NOT NULL CHECK (session_resolution IN {SESSION_RESOLUTION_VALUES!r}),
    index_schema_version INTEGER NOT NULL,
    UNIQUE(event_id, source_store, source_locator)
);

CREATE VIRTUAL TABLE hippocampal_items_fts USING fts5(
    content_text,
    content='hippocampal_items',
    content_rowid='row_id'
);

CREATE TRIGGER hippocampal_items_ai AFTER INSERT ON hippocampal_items BEGIN
    INSERT INTO hippocampal_items_fts(rowid, content_text) VALUES (new.row_id, new.content_text);
END;

CREATE TRIGGER hippocampal_items_ad AFTER DELETE ON hippocampal_items BEGIN
    INSERT INTO hippocampal_items_fts(hippocampal_items_fts, rowid, content_text)
        VALUES('delete', old.row_id, old.content_text);
END;

CREATE TRIGGER hippocampal_items_au AFTER UPDATE ON hippocampal_items BEGIN
    INSERT INTO hippocampal_items_fts(hippocampal_items_fts, rowid, content_text)
        VALUES('delete', old.row_id, old.content_text);
    INSERT INTO hippocampal_items_fts(rowid, content_text) VALUES (new.row_id, new.content_text);
END;
"""

_HIPPOCAMPAL_ITEMS_COLUMNS = (
    "item_id", "event_id", "source_store", "source_locator", "source_line_index",
    "component_kind", "content_text", "content_sha256", "memory_kind",
    "attribution_status", "authentication_status", "creator_actor_id",
    "creator_actor_type", "pipeline_id", "occurred_at", "session_id",
    "session_resolution", "index_schema_version",
)


def create_hippocampus_db(path: str) -> sqlite3.Connection:
    """Refuses accidental overwrite of an existing database; verifies
    SQLite FTS5 support (no fallback if unavailable -- never LIKE,
    embeddings, or another search backend); creates the v1 hippocampal
    schema; commits cleanly; leaves a valid newly created DB or
    raises (and removes any partial file it created)."""
    if os.path.exists(path):
        raise HippocampusSchemaError(f"refusing to overwrite existing database: {path!r}")
    _assert_fts5_available()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.execute("INSERT INTO hippocampus_meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.execute("INSERT INTO hippocampus_meta (key, value) VALUES ('item_id_version', ?)", (str(ITEM_ID_VERSION),))
        conn.commit()
    except Exception:
        conn.close()
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return conn


def _open_hippocampus_writable(path: str) -> sqlite3.Connection:
    """Sync (and rebuild, via create+sync) require the hippocampal DB
    to already exist -- there is no auto-bootstrap. Creation is always
    explicit, via create_hippocampus_db()."""
    if not os.path.isfile(path):
        raise HippocampusUnavailableError(
            f"hippocampal database does not exist: {path!r} -- sync never bootstraps one implicitly; "
            f"call create_hippocampus_db() explicitly first."
        )
    conn = sqlite3.connect(path)
    try:
        _migrate_schema(conn)
        _verify_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _normalized_sql(sql):
    return " ".join(sql.strip().rstrip(";").split()).replace('"hippocampal_items"', 'hippocampal_items')


def _items_table_sql():
    body = _SCHEMA_SQL.split("CREATE TABLE hippocampal_items (", 1)[1].split(";", 1)[0]
    return "CREATE TABLE hippocampal_items (" + body + ";"


def _verify_items_contract(conn, *, allow_legacy=False):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='hippocampal_items'").fetchone()
    current = _normalized_sql(_items_table_sql())
    accepted = {current}
    if allow_legacy:
        accepted.add(current.replace(repr(SOURCE_STORE_VALUES), repr(SOURCE_STORE_VALUES[:3])))
    if row is None or _normalized_sql(row[0]) not in accepted:
        raise HippocampusSchemaError("unrecognized hippocampal_items contract; refusing automatic migration")


def _migrate_schema(conn):
    """Atomic v1 -> v2 upgrade of the known source-store CHECK only.

    Sleep landing already added a fourth source to fresh v1 databases.
    Older v1 databases retained the three-source CHECK. Accept precisely
    those two known shapes, preserving row IDs, item identity and FTS data.
    SQL copies rows inside SQLite; no source content is read by this code.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        meta = dict(conn.execute("SELECT key, value FROM hippocampus_meta"))
        if meta.get("item_id_version") != str(ITEM_ID_VERSION):
            raise HippocampusSchemaError("unexpected item_id_version")
        if meta.get("schema_version") == "1":
            _verify_items_contract(conn, allow_legacy=True)
            expected_triggers = {
                statement.split()[2]: _normalized_sql(statement)
                for statement in re.findall(r"CREATE TRIGGER .*?END;", _SCHEMA_SQL, re.S)
            }
            actual_triggers = {
                name: _normalized_sql(sql) for name, sql in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='hippocampal_items'"
                )
            }
            if actual_triggers != expected_triggers or conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name='hippocampal_items' AND sql IS NOT NULL"
            ).fetchone():
                raise HippocampusSchemaError("unrecognized hippocampal dependents; refusing automatic migration")
            conn.execute(_items_table_sql().replace("CREATE TABLE hippocampal_items (", "CREATE TABLE hippocampal_items_v2 (", 1))
            columns = "row_id, " + ", ".join(_HIPPOCAMPAL_ITEMS_COLUMNS)
            conn.execute(f"INSERT INTO hippocampal_items_v2 ({columns}) SELECT {columns} FROM hippocampal_items")
            # The external-content FTS table keeps its existing rowid index.
            # Dropping the base table removes its triggers without firing them.
            conn.execute("DROP TABLE hippocampal_items")
            conn.execute("ALTER TABLE hippocampal_items_v2 RENAME TO hippocampal_items")
            for statement in re.findall(r"CREATE TRIGGER .*?END;", _SCHEMA_SQL, re.S):
                conn.execute(statement)
            conn.execute("UPDATE hippocampus_meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
        elif meta.get("schema_version") != str(SCHEMA_VERSION):
            raise HippocampusSchemaError(f"unexpected schema_version: {meta.get('schema_version')!r}")
        _verify_schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _verify_schema(conn: sqlite3.Connection) -> None:
    try:
        rows = dict(conn.execute("SELECT key, value FROM hippocampus_meta;").fetchall())
    except sqlite3.OperationalError as e:
        raise HippocampusSchemaError(f"hippocampal database is missing required schema: {e}") from e
    if rows.get("schema_version") != str(SCHEMA_VERSION):
        raise HippocampusSchemaError(f"unexpected schema_version: {rows.get('schema_version')!r}")
    if rows.get("item_id_version") != str(ITEM_ID_VERSION):
        raise HippocampusSchemaError(f"unexpected item_id_version: {rows.get('item_id_version')!r}")
    _verify_items_contract(conn)


def _fts_is_synchronized(conn: sqlite3.Connection) -> bool:
    """Tiny internal helper strictly for testing FTS schema integrity
    (not a retrieval API): the external-content FTS index row count
    must always equal the base table's row count, since every insert/
    update/delete on hippocampal_items is trigger-mirrored."""
    items_count = conn.execute("SELECT COUNT(*) FROM hippocampal_items;").fetchone()[0]
    fts_count = conn.execute("SELECT COUNT(*) FROM hippocampal_items_fts;").fetchone()[0]
    return items_count == fts_count


# =============================================================================
# Actor-subtype resolution (canonical host-system / Clark-agent
# subtypes -- actor_host_system / actor_clark_agent, not merely the
# actors.actor_type column, though the schema's own triggers keep the
# two consistent at write time; we check both and treat disagreement
# as an integrity contradiction, never silently pick one).
# =============================================================================
_HOST_SYSTEM_ACTOR_TYPE = "host_system"
_CLARK_AGENT_ACTOR_TYPE = "clark_agent"


def _resolve_actor_subtype(prov_conn: sqlite3.Connection, actor_id: str) -> tuple:
    """Returns (actor_type, has_host_subtype, has_clark_subtype) for a
    real actor_id, joined directly from canonical actors/
    actor_host_system/actor_clark_agent -- never hardcoded."""
    row = prov_conn.execute(
        "SELECT a.actor_type, "
        "(SELECT 1 FROM actor_host_system WHERE actor_id = a.actor_id) IS NOT NULL, "
        "(SELECT 1 FROM actor_clark_agent WHERE actor_id = a.actor_id) IS NOT NULL "
        "FROM actors a WHERE a.actor_id = ?",
        (actor_id,),
    ).fetchone()
    if row is None:
        raise HippocampusIntegrityError(f"creator_actor_id {actor_id!r} does not resolve to any canonical actor")
    actor_type, has_host, has_clark = row
    return actor_type, bool(has_host), bool(has_clark)


# =============================================================================
# Ingestion path 1: canonical event_components.
# =============================================================================
def _enumerate_component_candidates(prov_conn: sqlite3.Connection):
    """Returns (candidates, unsupported) -- unsupported holds
    diagnostic dicts for component_kind values outside the v1
    whitelist, never silently classified as `unknown` and ingested."""
    candidates = []
    unsupported = []
    rows = prov_conn.execute(
        "SELECT ec.component_id, ec.event_id, ec.component_kind, ec.component_text, "
        "ec.content_sha256, ec.creator_actor_id, ec.authorship_resolution, "
        "ev.occurred_at, ev.pipeline_id "
        "FROM event_components ec JOIN events ev ON ev.event_id = ec.event_id "
        "ORDER BY ec.component_id;"
    ).fetchall()
    for (component_id, event_id, component_kind, component_text, content_sha256,
         creator_actor_id, authorship_resolution, occurred_at, pipeline_id) in rows:
        if component_kind not in _SUPPORTED_COMPONENT_KINDS:
            unsupported.append({
                "component_id": component_id, "event_id": event_id, "component_kind": component_kind,
            })
            continue

        recomputed = hashlib.sha256(component_text.encode("utf-8")).hexdigest()
        if recomputed != content_sha256:
            raise HippocampusGroundingError(
                f"event_components.component_id={component_id!r}: recomputed content hash "
                f"{recomputed!r} disagrees with canonical content_sha256 {content_sha256!r}"
            )

        source_locator = str(component_id)
        creator_actor_type = None

        if component_kind in _HOST_AUTHORED_COMPONENT_KINDS:
            if creator_actor_id is None:
                raise HippocampusIntegrityError(
                    f"component_id={component_id!r}: {component_kind} requires a resolved creator_actor_id"
                )
            actor_type, has_host, has_clark = _resolve_actor_subtype(prov_conn, creator_actor_id)
            if (actor_type == _HOST_SYSTEM_ACTOR_TYPE) != has_host:
                raise HippocampusIntegrityError(
                    f"component_id={component_id!r}: actor {creator_actor_id!r} actor_type={actor_type!r} "
                    f"disagrees with actor_host_system subtype presence ({has_host}) -- contradictory canonical actor structure"
                )
            if actor_type != _HOST_SYSTEM_ACTOR_TYPE or not has_host:
                raise HippocampusIntegrityError(
                    f"component_id={component_id!r}: {component_kind} creator does not resolve through "
                    f"the canonical host-system actor subtype (actor_type={actor_type!r})"
                )
            creator_actor_type = actor_type
            memory_kind = "mechanical_record"
            attribution_status = authorship_resolution

        elif component_kind in _CLARK_AUTHORED_COMPONENT_KINDS:
            if creator_actor_id is None:
                raise HippocampusIntegrityError(
                    f"component_id={component_id!r}: {component_kind} requires a resolved creator_actor_id"
                )
            actor_type, has_host, has_clark = _resolve_actor_subtype(prov_conn, creator_actor_id)
            if (actor_type == _CLARK_AGENT_ACTOR_TYPE) != has_clark:
                raise HippocampusIntegrityError(
                    f"component_id={component_id!r}: actor {creator_actor_id!r} actor_type={actor_type!r} "
                    f"disagrees with actor_clark_agent subtype presence ({has_clark}) -- contradictory canonical actor structure"
                )
            if actor_type != _CLARK_AGENT_ACTOR_TYPE or not has_clark:
                raise HippocampusIntegrityError(
                    f"component_id={component_id!r}: {component_kind} creator does not resolve through "
                    f"the canonical Clark-agent actor subtype (actor_type={actor_type!r})"
                )
            creator_actor_type = actor_type
            memory_kind = "clark_expression"
            attribution_status = authorship_resolution

        else:  # assembled_reply_unsplit_historical
            if creator_actor_id is not None:
                raise HippocampusIntegrityError(
                    f"component_id={component_id!r}: assembled_reply_unsplit_historical must never "
                    f"have a resolved creator_actor_id (found {creator_actor_id!r}) -- no invented creator"
                )
            memory_kind = "mixed_historical_expression"
            attribution_status = authorship_resolution

        candidates.append({
            "event_id": event_id, "source_store": "event_components", "source_locator": source_locator,
            "source_line_index": None, "component_kind": component_kind,
            "content_text": component_text, "content_sha256": content_sha256,
            "memory_kind": memory_kind, "attribution_status": attribution_status,
            "creator_actor_id": creator_actor_id, "creator_actor_type": creator_actor_type,
            "pipeline_id": pipeline_id, "occurred_at": occurred_at,
        })
    return candidates, unsupported


# =============================================================================
# Ingestion path 2: native relational human expression.
# =============================================================================
def _enumerate_relational_human_candidates(prov_conn: sqlite3.Connection, rel_conn: sqlite3.Connection):
    candidates = []
    rows = rel_conn.execute(
        "SELECT id, event_id, external_observation FROM relational_events "
        "WHERE external_observation IS NOT NULL AND external_observation != '' "
        "AND event_id IS NOT NULL ORDER BY id;"
    ).fetchall()
    for row_id, event_id, external_observation in rows:
        event_row = prov_conn.execute(
            "SELECT occurred_at, pipeline_id FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if event_row is None:
            # Dangling event_id -- eligibility requires the canonical event
            # to exist. Not eligible: excluded from the expected set, not
            # an error (this is a source-side gap, not something this
            # ingestion path caused or can repair).
            continue
        occurred_at, pipeline_id = event_row
        source_locator = str(row_id)
        candidates.append({
            "event_id": event_id, "source_store": "relational_events", "source_locator": source_locator,
            "source_line_index": None, "component_kind": None,
            "content_text": external_observation,
            "content_sha256": hashlib.sha256(external_observation.encode("utf-8")).hexdigest(),
            "memory_kind": "human_expression", "attribution_status": "unknown",
            # Frozen v1 behavior: current source does not mechanically
            # establish the human actor on this path. Never inferred from
            # relational_events.user_id, relationship role, prompt text,
            # USER_ID, or any production default.
            "creator_actor_id": None, "creator_actor_type": None,
            "pipeline_id": pipeline_id, "occurred_at": occurred_at,
        })
    return candidates


# =============================================================================
# Ingestion path 3: historical JSONL human expression.
# =============================================================================
def _enumerate_historical_jsonl_candidates(prov_conn: sqlite3.Connection, jsonl_path: str):
    if not os.path.isfile(jsonl_path):
        raise HippocampusUnavailableError(f"historical JSONL not found: {jsonl_path!r}")
    candidates = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for line_index, raw_line in enumerate(lines):
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        line_bytes = line.encode("utf-8")
        event_id = derive_historical_event_id(HISTORICAL_JSONL_SOURCE_ID, line_bytes, line_index)
        event_row = prov_conn.execute(
            "SELECT occurred_at, pipeline_id FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if event_row is None:
            # Not grounded to a migrated canonical event -- not eligible
            # (covers, among other things, a newer/native JSONL line that
            # was never historically migrated at all).
            continue
        occurred_at, pipeline_id = event_row
        entry = json.loads(line)
        prompt_text = entry["prompt"]
        source_locator = "line:" + str(line_index)
        candidates.append({
            "event_id": event_id, "source_store": "anaxi_log_jsonl", "source_locator": source_locator,
            "source_line_index": line_index, "component_kind": None,
            "content_text": prompt_text,
            "content_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            "memory_kind": "human_expression", "attribution_status": "unknown",
            "creator_actor_id": None, "creator_actor_type": None,
            "pipeline_id": pipeline_id, "occurred_at": occurred_at,
        })
    return candidates


# =============================================================================
# Ingestion path 4 (SLP1-D): canonical Sleep derivation occurrences.
#
# Reads from the SAME anaxi_provenance.db connection as path 1 above --
# SLP1-C's sleep_cycles/sleep_derivations tables live in that one
# anchored database, not a separate "memory DB" (see sleep_c_schema.py).
# The INNER JOIN to sleep_cycles is the whole eligibility rule: SLP1-C's
# own canonical design guarantees a sleep_cycles row exists ONLY for a
# cycle that reached its single atomic final commit (a failed/partial/
# stale-fenced-owner attempt leaves no trace anywhere -- see sleep_
# cycle.py's own CRASH MODEL), so any sleep_derivations row that joins
# to a real sleep_cycles row is, by construction, drawn from a
# successfully committed cycle. No separate "is this cycle successful"
# flag is needed or checked.
#
# The tables are optional from this module's point of view: an older or
# synthetic anaxi_provenance.db that never ran SLP1-C's additive
# migration simply contributes zero Sleep-derivation candidates -- never
# an error, and never a reason to fail the other three ingestion paths.
# This module is read-only against anaxi_provenance.db and therefore
# never runs that migration itself.
#
# `event_id` here is the canonical derivation_id itself, not a
# fabricated waking events.event_id -- no row is ever written to
# `events` for a Sleep derivation, and none of _is_historical_event()/
# _derive_authentication_status()/_derive_session() below can mistake
# this for a genuine waking event: none of them find a matching events
# row for a `deriv-...`-prefixed id, so they safely degrade to their
# own "not found" answers (False / "unknown" / (None, "unknown")) --
# never a fabricated auth/session claim.
# =============================================================================
def _sleep_derivation_tables_exist(prov_conn: sqlite3.Connection) -> bool:
    row = prov_conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('sleep_cycles', 'sleep_derivations');"
    ).fetchone()
    return row[0] == 2


def _enumerate_sleep_derivation_candidates(prov_conn: sqlite3.Connection):
    if not _sleep_derivation_tables_exist(prov_conn):
        return []
    candidates = []
    rows = prov_conn.execute(
        "SELECT d.derivation_id, d.derived_text, d.derived_text_sha256, d.created_at "
        "FROM sleep_derivations d JOIN sleep_cycles c ON c.cycle_id = d.cycle_id "
        "ORDER BY d.derivation_id;"
    ).fetchall()
    for derivation_id, derived_text, derived_text_sha256, created_at in rows:
        recomputed = hashlib.sha256(derived_text.encode("utf-8")).hexdigest()
        if recomputed != derived_text_sha256:
            raise HippocampusGroundingError(
                f"sleep_derivations.derivation_id={derivation_id!r}: recomputed content hash "
                f"{recomputed!r} disagrees with its own stored derived_text_sha256 {derived_text_sha256!r}"
            )
        candidates.append({
            "event_id": derivation_id, "source_store": "sleep_derivations", "source_locator": derivation_id,
            "source_line_index": None, "component_kind": None,
            "content_text": derived_text, "content_sha256": derived_text_sha256,
            # SLP1-D FROZEN PRINCIPLE: a Sleep derivation is not a
            # resolved expression by any actor identity this schema
            # already tracks -- "unknown" attribution/no creator_actor_id
            # (matching the two other non-event_components paths above),
            # never a claimed authorship that would overstate what is
            # mechanically known. memory_kind "derived_inference" is this
            # schema's own pre-existing, purpose-built value for exactly
            # this: never "clark_expression" (that would misrepresent a
            # tentative Sleep association as ordinary waking speech).
            "memory_kind": "derived_inference", "attribution_status": "unknown",
            "creator_actor_id": None, "creator_actor_type": None,
            "pipeline_id": None, "occurred_at": created_at,
        })
    return candidates


# =============================================================================
# Authentication derivation.
# =============================================================================
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
    auth_state = auth_row[0]
    if auth_state in ("unauthenticated", "claimed_only", "authenticated"):
        return auth_state
    return "unknown"


# =============================================================================
# Session derivation.
# =============================================================================
def _derive_session(prov_conn: sqlite3.Connection, event_id: str, pipeline_id: Optional[str], occurred_at: int,
                     is_historical: bool) -> tuple:
    """Amendment S1 (canonical native session resolution): for a native
    event, session membership is resolved through the exact canonical
    link -- events.auth_context_id -> auth_contexts.session_id ->
    sessions.session_id -- never through temporal-range inference
    against sessions.started_at/ended_at. An exact canonical assignment
    is strictly stronger evidence than timestamp overlap, so two
    separate sessions both being temporally "open" (ended_at IS NULL)
    no longer manufactures ambiguity for a canonically-assigned event --
    that overlap is simply not consulted anymore. occurred_at is kept
    as a parameter for call-site compatibility; S1 no longer uses it.

    Historical material is unaffected: it has no canonical native
    session to begin with and remains (None, "unknown") -- no
    historical session is ever invented from timestamps either, exactly
    as before.

    A canonical session_id that does not resolve to any sessions row,
    or whose pipeline_id disagrees with the event's own pipeline_id, is
    not silently downgraded to temporal inference -- it is a broken
    canonical reference and raises HippocampusIntegrityError, matching
    this module's established pattern for structurally-inconsistent
    canonical state elsewhere (e.g. actor-subtype checks above)."""
    if is_historical:
        return None, "unknown"

    auth_context_row = prov_conn.execute(
        "SELECT auth_context_id FROM events WHERE event_id = ?;", (event_id,)
    ).fetchone()
    if auth_context_row is None or auth_context_row[0] is None:
        return None, "unknown"
    auth_context_id = auth_context_row[0]

    session_id_row = prov_conn.execute(
        "SELECT session_id FROM auth_contexts WHERE auth_context_id = ?;", (auth_context_id,)
    ).fetchone()
    if session_id_row is None or session_id_row[0] is None:
        return None, "unknown"
    session_id = session_id_row[0]

    session_row = prov_conn.execute(
        "SELECT pipeline_id FROM sessions WHERE session_id = ?;", (session_id,)
    ).fetchone()
    if session_row is None:
        raise HippocampusIntegrityError(
            f"event_id={event_id!r}: auth_context {auth_context_id!r} names session_id "
            f"{session_id!r}, but no such row exists in sessions -- the canonical session "
            f"reference is broken; refusing to fall back to temporal inference."
        )
    session_pipeline_id = session_row[0]
    if pipeline_id is not None and session_pipeline_id != pipeline_id:
        raise HippocampusIntegrityError(
            f"event_id={event_id!r}: canonical session {session_id!r} has pipeline_id "
            f"{session_pipeline_id!r}, which disagrees with the event's own pipeline_id "
            f"{pipeline_id!r} -- refusing to trust a structurally inconsistent canonical link."
        )

    return session_id, "resolved"


def _is_historical_event(prov_conn: sqlite3.Connection, event_id: str) -> bool:
    row = prov_conn.execute(
        "SELECT 1 FROM event_migration_status WHERE event_id = ? AND migration_status = 'pre_authentication_layer';",
        (event_id,),
    ).fetchone()
    return row is not None


# =============================================================================
# Sync result.
# =============================================================================
@dataclasses.dataclass
class SyncResult:
    inserted: list
    unchanged: int
    session_updated: list
    unsupported_components: list


def _finish_candidate(prov_conn, candidate: dict) -> dict:
    """Fills in item_id, authentication_status, session fields, and
    index_schema_version for one raw ingestion candidate -- shared by
    all three ingestion paths, applied uniformly."""
    item_id = derive_hippocampal_item_id(candidate["event_id"], candidate["source_store"], candidate["source_locator"])
    historical = _is_historical_event(prov_conn, candidate["event_id"])
    authentication_status = _derive_authentication_status(prov_conn, candidate["event_id"])
    session_id, session_resolution = _derive_session(
        prov_conn, candidate["event_id"], candidate["pipeline_id"], candidate["occurred_at"], historical
    )
    full = dict(candidate)
    full["item_id"] = item_id
    full["authentication_status"] = authentication_status
    full["session_id"] = session_id
    full["session_resolution"] = session_resolution
    full["index_schema_version"] = ITEM_ID_VERSION
    return full


_DRIFT_CHECKED_FIELDS = (
    "event_id", "source_store", "source_locator", "component_kind",
    "content_text", "content_sha256", "memory_kind", "attribution_status",
    "authentication_status", "creator_actor_id", "creator_actor_type", "pipeline_id", "occurred_at",
)


def sync_hippocampus(hippocampus_path: str, source_paths: HippocampusPaths) -> SyncResult:
    """Deterministic scan-and-upsert. Correctness is based on full
    re-enumeration every time, never a cursor. Never bootstraps a
    missing hippocampal database."""
    hip_conn = _open_hippocampus_writable(hippocampus_path)
    try:
        prov_conn = _open_source_readonly(source_paths.provenance_db_path, "canonical provenance database")
        try:
            rel_conn = _open_source_readonly(source_paths.relational_db_path, "relational database")
            try:
                component_candidates, unsupported = _enumerate_component_candidates(prov_conn)
                relational_candidates = _enumerate_relational_human_candidates(prov_conn, rel_conn)
                jsonl_candidates = _enumerate_historical_jsonl_candidates(prov_conn, source_paths.historical_jsonl_path)
                sleep_derivation_candidates = _enumerate_sleep_derivation_candidates(prov_conn)

                expected = {}
                for raw in component_candidates + relational_candidates + jsonl_candidates + sleep_derivation_candidates:
                    full = _finish_candidate(prov_conn, raw)
                    expected[full["item_id"]] = full

                existing_rows = hip_conn.execute(
                    "SELECT item_id, " + ", ".join(_DRIFT_CHECKED_FIELDS) +
                    ", session_id, session_resolution FROM hippocampal_items;"
                ).fetchall()
                existing = {}
                for row in existing_rows:
                    item_id = row[0]
                    rec = dict(zip(("item_id",) + _DRIFT_CHECKED_FIELDS + ("session_id", "session_resolution"), row))
                    existing[item_id] = rec

                inserted = []
                session_updated = []
                unchanged = 0

                for item_id, candidate in expected.items():
                    if item_id not in existing:
                        hip_conn.execute(
                            "INSERT INTO hippocampal_items (" + ", ".join(_HIPPOCAMPAL_ITEMS_COLUMNS) + ") "
                            "VALUES (" + ", ".join("?" for _ in _HIPPOCAMPAL_ITEMS_COLUMNS) + ")",
                            tuple(candidate[c] for c in _HIPPOCAMPAL_ITEMS_COLUMNS),
                        )
                        inserted.append(item_id)
                        continue

                    stored = existing[item_id]
                    for field in _DRIFT_CHECKED_FIELDS:
                        if stored[field] != candidate[field]:
                            raise HippocampusSourceDriftError(
                                f"item_id={item_id!r}: field {field!r} changed from {stored[field]!r} "
                                f"to {candidate[field]!r} for an already-indexed item -- refusing to "
                                f"silently rewrite it."
                            )
                    if (stored["session_id"], stored["session_resolution"]) != (candidate["session_id"], candidate["session_resolution"]):
                        hip_conn.execute(
                            "UPDATE hippocampal_items SET session_id = ?, session_resolution = ? WHERE item_id = ?",
                            (candidate["session_id"], candidate["session_resolution"], item_id),
                        )
                        session_updated.append(item_id)
                    else:
                        unchanged += 1

                disappeared = set(existing) - set(expected)
                if disappeared:
                    raise HippocampusSourceDriftError(
                        f"{len(disappeared)} previously-indexed item(s) no longer have an eligible "
                        f"source candidate (e.g. {sorted(disappeared)[0]!r}) -- refusing to silently "
                        f"delete, mark stale, or replace them."
                    )

                hip_conn.commit()
                return SyncResult(
                    inserted=inserted, unchanged=unchanged,
                    session_updated=session_updated, unsupported_components=unsupported,
                )
            finally:
                rel_conn.close()
        finally:
            prov_conn.close()
    except Exception:
        hip_conn.rollback()
        raise
    finally:
        hip_conn.close()


def rebuild_hippocampus(target_path: str, source_paths: HippocampusPaths) -> SyncResult:
    """Target must not already exist. Creates fresh schema via the
    same creation path, then uses the exact same ingestion/
    classification code as ordinary sync. Reads all sources read-only;
    writes only to the new hippocampal DB."""
    create_hippocampus_db(target_path).close()
    return sync_hippocampus(target_path, source_paths)


def fetch_logical_rows(hippocampus_path: str) -> list:
    """Test/verification support only -- a raw dump of every logical
    (non-physical-row_id) column, sorted by item_id. Not a retrieval
    API: no ranking, no filtering, no bounding."""
    conn = sqlite3.connect(hippocampus_path)
    try:
        cols = ("item_id",) + _HIPPOCAMPAL_ITEMS_COLUMNS[1:]
        rows = conn.execute(
            "SELECT " + ", ".join(cols) + " FROM hippocampal_items ORDER BY item_id;"
        ).fetchall()
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()
