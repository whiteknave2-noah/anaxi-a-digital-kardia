"""Read-only, model-facing historical-continuity search and bounded inspection.

Search uses the existing hippocampal FTS/BM25 machinery.  Inspection accepts
only a cursor returned by that search (or by the immediately preceding read),
then re-reads and verifies the exact source before returning a bounded window.

Two verification laws remain deliberately separate:

* current canonical material continues through historical_unit_reconstruction;
* imported pre-authentication human expression is verified from its immutable
  import chain (indexed row -> exact legacy source locator -> derived historical
  event id -> canonical migration/routing rows).  It is delivered as a
  historical source record, never as authenticated human speech.
* relational legacy observations with unknown authorship are admitted only when
  their exact relational row, canonical event metadata, and exact JSONL mirror
  agree.  They remain observations, never canonical human or Clark speech.

Every database is opened mode=ro/query_only.  No search/read creates a receipt,
event, cursor table, cache, acknowledgement, or other persistent state.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

import family_membership
import hippocampus_retrieval
import historical_unit_reconstruction
import epistemic_status
from hippocampus_store import HISTORICAL_JSONL_SOURCE_ID, derive_historical_event_id


HISTORY_SEARCH = "search_historical_continuity"
HISTORY_READ = "read_historical_item"
HISTORY_BROWSE = "browse_historical_continuity"
HANDLE_PREFIX = "history-item:"
CURSOR_PREFIX = "history-read:"
BROWSE_CURSOR_PREFIX = "history-browse:"
WINDOW_CHARS = 1200
EXCERPT_CHARS = 180
BROWSE_PAGE_CHARS = 6000
BROWSE_PAGE_RECORDS = 8
MAX_HISTORY_QUERY_CHARS = 2000
MAX_HISTORY_ACTIONS_PER_OCCASION = 8
PRE_AUTHENTICATION_LAYER = "pre_authentication_layer"
IDENTITY_NOT_ESTABLISHED = "NOT_ESTABLISHED"


class HistoricalAccessError(RuntimeError):
    """A requested historical result failed closed."""


@dataclasses.dataclass(frozen=True)
class HistoricalPaths:
    hippocampus_db_path: str
    provenance_db_path: str
    relational_db_path: str
    historical_jsonl_path: str

    @classmethod
    def from_hippocampus_paths(cls, paths):
        return cls(
            hippocampus_db_path=paths.hippocampus_db_path,
            provenance_db_path=paths.provenance_db_path,
            relational_db_path=paths.relational_db_path,
            historical_jsonl_path=paths.historical_jsonl_path,
        )


@dataclasses.dataclass(frozen=True)
class VerifiedSource:
    item_id: str
    event_id: str
    text: str
    content_sha256: str
    occurred_at: int
    source_class: str
    source_store: str
    source_locator: str
    canonical_anchor_event: str
    authentication: str
    modern_actor_identity: str
    historical_route_user_id: Optional[str]
    relational_event_id: Optional[int]
    substrate: Optional[str]
    session_id: Optional[str]
    session_resolution: str
    imported_at: Optional[int]
    visibility_scope: Optional[str]
    canonical_component_kind: Optional[str]
    # CONTENT provenance, kept independent of the retrieval provenance above.  What the
    # material originally IS (its indexed memory kind, its canonical structural role,
    # whose expression it is) is a different fact from HOW it arrived (source_store,
    # source_locator, retrieval method), and collapsing the two is what let a prior
    # Clark reply read as an unattributed record.  Each is NOT_ESTABLISHED when the
    # host did not mechanically establish it -- never guessed from a display label.
    indexed_memory_kind: str
    canonical_structural_role: Optional[str]
    actor_relation: Optional[str]
    source_category: str = historical_unit_reconstruction.SOURCE_CATEGORY_CANONICAL_COMPONENT
    mirror_source_store: Optional[str] = None
    mirror_source_locator: Optional[str] = None
    mirror_raw_sha256: Optional[str] = None
    source_raw_sha256: Optional[str] = None
    representation_event_id: Optional[str] = None
    representation_occurred_at: Optional[int] = None
    representation_recorded_at: Optional[int] = None


def _readonly_connection(path: str, label: str) -> sqlite3.Connection:
    if not path or not os.path.isfile(path):
        raise HistoricalAccessError(f"{label}_missing")
    try:
        uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT count(*) FROM sqlite_master")
        return conn
    except sqlite3.Error as exc:
        raise HistoricalAccessError(f"{label}_unreadable") from exc


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso8601(epoch: Optional[int]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def item_handle(item_id: str) -> str:
    return HANDLE_PREFIX + item_id


def read_cursor(item_id: str, offset: int) -> str:
    return f"{CURSOR_PREFIX}{item_id}:{offset}"


def browse_cursor(item_id: str, offset: int) -> str:
    return f"{BROWSE_CURSOR_PREFIX}{item_id}:{offset}"


def _parse_cursor(cursor: str) -> tuple[str, int]:
    if not isinstance(cursor, str) or not cursor.startswith(CURSOR_PREFIX):
        raise HistoricalAccessError("invalid_history_cursor")
    body = cursor[len(CURSOR_PREFIX):]
    item_id, separator, raw_offset = body.rpartition(":")
    if not separator or not item_id.startswith("hip-") or not raw_offset.isdigit():
        raise HistoricalAccessError("invalid_history_cursor")
    offset = int(raw_offset)
    if offset < 0:
        raise HistoricalAccessError("invalid_history_cursor")
    return item_id, offset


def _parse_browse_cursor(cursor: str) -> tuple[str, int]:
    if not isinstance(cursor, str) or not cursor.startswith(BROWSE_CURSOR_PREFIX):
        raise HistoricalAccessError("invalid_history_browse_cursor")
    body = cursor[len(BROWSE_CURSOR_PREFIX):]
    item_id, separator, raw_offset = body.rpartition(":")
    if not separator or not item_id.startswith("hip-") or not raw_offset.isdigit():
        raise HistoricalAccessError("invalid_history_browse_cursor")
    offset = int(raw_offset)
    if offset < 0:
        raise HistoricalAccessError("invalid_history_browse_cursor")
    return item_id, offset


def _legacy_jsonl_text(paths: HistoricalPaths, item) -> tuple[str, dict, bytes, int]:
    prefix = "line:"
    if item.source_store != "anaxi_log_jsonl" or not str(item.source_locator).startswith(prefix):
        raise HistoricalAccessError("legacy_source_locator_invalid")
    raw_index = str(item.source_locator)[len(prefix):]
    if not raw_index.isdigit():
        raise HistoricalAccessError("legacy_source_locator_invalid")
    line_index = int(raw_index)
    if not os.path.isfile(paths.historical_jsonl_path):
        raise HistoricalAccessError("legacy_source_missing")
    try:
        with open(paths.historical_jsonl_path, "rb") as source:
            lines = source.read().splitlines()
        raw_line = lines[line_index]
        entry = json.loads(raw_line.decode("utf-8"))
        text = entry["prompt"]
    except (IndexError, KeyError, TypeError, ValueError, UnicodeDecodeError, OSError) as exc:
        raise HistoricalAccessError("legacy_source_invalid") from exc
    if not isinstance(text, str):
        raise HistoricalAccessError("legacy_source_invalid")
    return text, entry, raw_line, line_index


def _verify_legacy_pre_auth(paths: HistoricalPaths, item) -> VerifiedSource:
    """Verify exact imported expression without manufacturing canonical speech.

    Required chain: exact JSONL locator and bytes derive the indexed historical
    event id; indexed full text/hash/metadata agree; the canonical anchor is a
    NULL-auth waking_turn with one pre-authentication migration record and a
    non-ambiguous legacy routing record; its pipeline/substrate and occurrence
    agree with the source.  Any absence or contradiction fails closed.
    """
    if not (
        item.memory_kind == "human_expression"
        and item.authentication_status == PRE_AUTHENTICATION_LAYER
        and item.creator_actor_id is None
        and item.creator_actor_type is None
        and item.component_kind is None
        and item.session_id is None
        and item.session_resolution == "unknown"
    ):
        raise HistoricalAccessError("legacy_index_provenance_contradictory")

    text, entry, raw_line, line_index = _legacy_jsonl_text(paths, item)
    digest = _sha256(text)
    if derive_historical_event_id(HISTORICAL_JSONL_SOURCE_ID, raw_line, line_index) != item.event_id:
        raise HistoricalAccessError("legacy_event_anchor_mismatch")
    indexed_text_matches = (
        text.startswith(item.content) if item.content_truncated else item.content == text
    )
    if not indexed_text_matches or item.source_chars != len(text):
        raise HistoricalAccessError("legacy_index_text_mismatch")
    if not item.content_sha256 or item.content_sha256 != digest:
        raise HistoricalAccessError("legacy_source_hash_mismatch")

    conn = _readonly_connection(paths.provenance_db_path, "canonical_provenance")
    try:
        event_columns = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        scope_expression = "e.visibility_scope" if "visibility_scope" in event_columns else "NULL"
        rows = conn.execute(
            "SELECT e.event_type,e.occurred_at,e.pipeline_id,e.auth_context_id," + scope_expression + ","
            "m.migration_status,m.migrated_at,p.legacy_substrate_label "
            "FROM events e JOIN event_migration_status m ON m.event_id=e.event_id "
            "LEFT JOIN pipelines p ON p.pipeline_id=e.pipeline_id WHERE e.event_id=?",
            (item.event_id,),
        ).fetchall()
        routes = conn.execute(
            "SELECT routing_constant_value,routing_mechanism,recorded_at "
            "FROM legacy_routing_provenance WHERE event_id=? ORDER BY legacy_routing_id",
            (item.event_id,),
        ).fetchall()
        human_components = conn.execute(
            "SELECT COUNT(*) FROM event_components WHERE event_id=? "
            "AND component_kind='human_conversational_input'",
            (item.event_id,),
        ).fetchone()[0]
    except sqlite3.Error as exc:
        raise HistoricalAccessError("legacy_canonical_chain_unreadable") from exc
    finally:
        conn.close()
    if len(rows) != 1:
        raise HistoricalAccessError("legacy_canonical_anchor_missing")
    (event_type, occurred_at, pipeline_id, auth_context_id, visibility_scope,
     migration_status, migrated_at, substrate) = rows[0]
    if (
        event_type != "waking_turn"
        or auth_context_id is not None
        or visibility_scope is not None
        or migration_status != PRE_AUTHENTICATION_LAYER
        or human_components != 0
        or pipeline_id != item.pipeline_id
        or occurred_at != item.occurred_at
    ):
        raise HistoricalAccessError("legacy_canonical_provenance_contradictory")
    if len(routes) != 1 or not routes[0][0] or not routes[0][1]:
        raise HistoricalAccessError("legacy_route_not_established")
    timestamp = entry.get("timestamp")
    source_substrate = entry.get("substrate")
    try:
        source_epoch = int(datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError) as exc:
        raise HistoricalAccessError("legacy_source_timestamp_invalid") from exc
    if source_epoch != occurred_at or source_substrate != substrate:
        raise HistoricalAccessError("legacy_source_provenance_mismatch")

    # The relational mirror is a second, independently-addressed part of the
    # imported-history chain.  It must contain exactly one matching external
    # observation and agree with the canonical pipeline plus the recorded
    # legacy route.  Its NULL auth/event fields are historical facts, not an
    # invitation to manufacture modern authentication or attribution.
    relational = _readonly_connection(paths.relational_db_path, "legacy_relational_mirror")
    try:
        mirror_rows = relational.execute(
            "SELECT id,user_id,substrate,created_at,external_observation,status,"
            "pipeline_id,auth_context_id,event_id FROM relational_events "
            "WHERE created_at=? AND external_observation=?",
            (occurred_at, text),
        ).fetchall()
    except sqlite3.Error as exc:
        raise HistoricalAccessError("legacy_relational_mirror_unreadable") from exc
    finally:
        relational.close()
    if len(mirror_rows) != 1:
        raise HistoricalAccessError("legacy_relational_mirror_missing_or_ambiguous")
    (relational_event_id, route_user_id, mirror_substrate, mirror_occurred_at,
     mirror_text, mirror_status, mirror_pipeline_id, mirror_auth_context_id,
     mirror_event_id) = mirror_rows[0]
    if (
        route_user_id != routes[0][0]
        or mirror_substrate != substrate
        or mirror_occurred_at != occurred_at
        or mirror_text != text
        or mirror_status != "recorded"
        or mirror_pipeline_id != pipeline_id
        or mirror_auth_context_id is not None
        or mirror_event_id not in (None, item.event_id)
    ):
        raise HistoricalAccessError("legacy_relational_provenance_contradictory")
    return VerifiedSource(
        item_id=item.item_id, event_id=item.event_id, text=text,
        content_sha256=digest, occurred_at=occurred_at,
        source_class="imported_historical_dialogue",
        source_store=item.source_store, source_locator=item.source_locator,
        canonical_anchor_event=item.event_id,
        authentication=PRE_AUTHENTICATION_LAYER,
        modern_actor_identity=IDENTITY_NOT_ESTABLISHED,
        historical_route_user_id=routes[0][0], relational_event_id=relational_event_id,
        substrate=substrate,
        session_id=None, session_resolution="unknown", imported_at=migrated_at,
        visibility_scope=visibility_scope, canonical_component_kind=None,
        # A legacy pre-auth item predates the canonical ledger, so it has no canonical
        # structural role and no canonical actor relation to state.  Its indexed memory
        # kind IS established (the store indexed it), so it is carried as-is; the other
        # two are NOT_A_CANONICAL_COMPONENT / NOT_ESTABLISHED rather than blank.
        indexed_memory_kind=item.memory_kind,
        canonical_structural_role=epistemic_status.NOT_A_CANONICAL_COMPONENT,
        actor_relation=epistemic_status.NOT_ESTABLISHED,
        source_category=historical_unit_reconstruction.SOURCE_CATEGORY_PRE_AUTHENTICATION,
        mirror_source_store=historical_unit_reconstruction.LEGACY_OBSERVATION_SOURCE_STORE,
        mirror_source_locator=str(relational_event_id),
        source_raw_sha256=hashlib.sha256(raw_line).hexdigest(),
        representation_event_id=item.event_id,
    )


def _verify_current_canonical(paths: HistoricalPaths, item) -> VerifiedSource:
    reconstructed = historical_unit_reconstruction.reconstruct(
        [item], paths.provenance_db_path,
        relational_db_path=paths.relational_db_path,
        historical_jsonl_path=paths.historical_jsonl_path,
    )
    if len(reconstructed.units) != 1 or reconstructed.withheld:
        reason = reconstructed.withheld[0][1] if reconstructed.withheld else "canonical_item_not_unique"
        raise HistoricalAccessError(reason)
    unit = reconstructed.units[0]
    canon = unit.canonical
    if canon is None or canon.component_text is None:
        raise HistoricalAccessError("canonical_component_absent")
    legacy_observation = canon.source_category == (
        historical_unit_reconstruction.SOURCE_CATEGORY_LEGACY_OBSERVATION)
    linked_human = canon.source_category == (
        historical_unit_reconstruction.SOURCE_CATEGORY_AUTHENTICATED_HUMAN_RELATIONAL)
    return VerifiedSource(
        item_id=item.item_id, event_id=item.event_id, text=canon.component_text,
        content_sha256=canon.content_sha256 or _sha256(canon.component_text),
        occurred_at=canon.occurred_at,
        source_class=("legacy_observation_unknown_authorship" if legacy_observation else
                      "authenticated_human_via_relational_link" if linked_human else
                      "authenticated_canonical_history"
                      if item.authentication_status == "authenticated"
                      else "canonical_component_history"),
        source_store=item.source_store, source_locator=item.source_locator,
        canonical_anchor_event=canon.event_id,
        authentication=(canon.authentication_status or item.authentication_status),
        modern_actor_identity=(IDENTITY_NOT_ESTABLISHED if legacy_observation
                               else canon.creator_actor_id or IDENTITY_NOT_ESTABLISHED),
        historical_route_user_id=None,
        relational_event_id=(int(item.source_locator)
                             if legacy_observation or linked_human else None),
        substrate=canon.source_substrate if legacy_observation or linked_human else None,
        session_id=canon.session_id, session_resolution=item.session_resolution,
        imported_at=None, visibility_scope=canon.visibility_scope,
        canonical_component_kind=None if legacy_observation else canon.component_kind,
        # CONTENT provenance from the canonical row this item was verified against: the
        # indexed memory kind, the reconstructed structural role, and the canonical
        # actor-equality relation.  All three are mechanically established here (the
        # unit was reconstructed from a canonical component), so none is guessed.
        indexed_memory_kind=item.memory_kind,
        canonical_structural_role=unit.role,
        actor_relation=(epistemic_status.NOT_ESTABLISHED if legacy_observation else
                        epistemic_status.actor_relation_of(
                            canon.creator_actor_id, item.memory_kind)),
        source_category=canon.source_category,
        mirror_source_store=canon.mirror_source_store,
        mirror_source_locator=canon.mirror_source_locator,
        mirror_raw_sha256=canon.mirror_raw_sha256,
        source_raw_sha256=canon.source_raw_sha256,
        representation_event_id=canon.representation_event_id,
        representation_occurred_at=canon.representation_occurred_at,
        representation_recorded_at=canon.representation_recorded_at,
    )


def verify_source(paths: HistoricalPaths, item) -> VerifiedSource:
    if (item.authentication_status == PRE_AUTHENTICATION_LAYER
            and item.memory_kind == "human_expression"
            and item.source_store == "anaxi_log_jsonl"):
        return _verify_legacy_pre_auth(paths, item)
    return _verify_current_canonical(paths, item)


def _permitted_items(paths: HistoricalPaths, items: Iterable, *, viewer_principal_actor_id,
                     viewer_scope, active_family_principal_ids):
    conn = _readonly_connection(paths.provenance_db_path, "canonical_provenance")
    try:
        return family_membership.permitted_hippocampal_items(
            conn, list(items), viewer_principal_actor_id=viewer_principal_actor_id,
            viewer_scope=viewer_scope,
            active_family_principal_ids=active_family_principal_ids,
        )
    finally:
        conn.close()


def search(paths: HistoricalPaths, query: str, *, viewer_principal_actor_id=None,
           viewer_scope=None, active_family_principal_ids=frozenset()) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_HISTORY_QUERY_CHARS:
        raise HistoricalAccessError("invalid_history_query")
    result = hippocampus_retrieval.retrieve_hippocampal_context(
        paths.hippocampus_db_path, query,
        provenance_db_path=paths.provenance_db_path,
    )
    permitted = _permitted_items(
        paths, result.items, viewer_principal_actor_id=viewer_principal_actor_id,
        viewer_scope=viewer_scope, active_family_principal_ids=active_family_principal_ids,
    )
    candidates, withheld = [], []
    for rank, item in enumerate(permitted, 1):
        try:
            verified = verify_source(paths, item)
        except HistoricalAccessError as exc:
            withheld.append({"item_id": item.item_id, "reason": str(exc)})
            continue
        candidates.append({
            "rank": rank,
            "handle": item_handle(item.item_id),
            "read_cursor": read_cursor(item.item_id, 0),
            "occurred_at": _iso8601(verified.occurred_at),
            "occurred_at_epoch": verified.occurred_at,
            "source_class": verified.source_class,
            "source_store": verified.source_store,
            "source_locator": verified.source_locator,
            "canonical_anchor_event": verified.canonical_anchor_event,
            "content_sha256": verified.content_sha256,
            "source_chars": len(verified.text),
            "authentication": verified.authentication,
            "modern_actor_identity": verified.modern_actor_identity,
            "historical_route_user_id": verified.historical_route_user_id,
            "relational_event_id": verified.relational_event_id,
            "substrate": verified.substrate,
            "session_id": verified.session_id,
            "session_resolution": verified.session_resolution,
            "imported_at": _iso8601(verified.imported_at),
            "visibility_scope": verified.visibility_scope,
            "canonical_component_kind": verified.canonical_component_kind,
            "indexed_memory_kind": verified.indexed_memory_kind,
            "canonical_structural_role": verified.canonical_structural_role,
            "actor_relation": verified.actor_relation,
            "source_category": verified.source_category,
            "mirror_source_store": verified.mirror_source_store,
            "mirror_source_locator": verified.mirror_source_locator,
            "mirror_raw_sha256": verified.mirror_raw_sha256,
            "source_raw_sha256": verified.source_raw_sha256,
            "representation_event_id": verified.representation_event_id,
            "representation_occurred_at": _iso8601(verified.representation_occurred_at),
            "representation_recorded_at": _iso8601(verified.representation_recorded_at),
            "exact_excerpt": verified.text[:EXCERPT_CHARS],
            "excerpt_complete": len(verified.text) <= EXCERPT_CHARS,
        })
    return {
        "schema": "anaxi.historical_continuity.search.v1",
        "query": query,
        "query_terms": list(result.query_terms),
        "candidates": candidates,
        "withheld": withheld,
        "read_only": True,
    }


def read(paths: HistoricalPaths, cursor: str, *, allowed_cursors: Iterable[str],
         viewer_principal_actor_id=None, viewer_scope=None,
         active_family_principal_ids=frozenset(), window_chars: int = WINDOW_CHARS) -> dict:
    allowed = frozenset(allowed_cursors or ())
    if cursor not in allowed:
        raise HistoricalAccessError("history_cursor_not_returned_by_search_or_read")
    if not isinstance(window_chars, int) or window_chars <= 0 or window_chars > WINDOW_CHARS:
        raise HistoricalAccessError("invalid_history_window")
    item_id, offset = _parse_cursor(cursor)
    item = hippocampus_retrieval.retrieve_indexed_item(paths.hippocampus_db_path, item_id)
    if item is None:
        raise HistoricalAccessError("history_item_missing")
    permitted = _permitted_items(
        paths, [item], viewer_principal_actor_id=viewer_principal_actor_id,
        viewer_scope=viewer_scope, active_family_principal_ids=active_family_principal_ids,
    )
    if len(permitted) != 1:
        raise HistoricalAccessError("history_visibility_denied")
    verified = verify_source(paths, permitted[0])
    if offset > len(verified.text):
        raise HistoricalAccessError("history_cursor_out_of_range")
    end = min(len(verified.text), offset + window_chars)
    text = verified.text[offset:end]
    more = end < len(verified.text)
    return {
        "schema": "anaxi.historical_continuity.read.v1",
        "handle": item_handle(item_id),
        "cursor": cursor,
        "start": offset,
        "end": end,
        "source_chars": len(verified.text),
        "more": more,
        "next_cursor": read_cursor(item_id, end) if more else None,
        "text": text,
        "content_sha256": verified.content_sha256,
        "source_class": verified.source_class,
        "source_store": verified.source_store,
        "source_locator": verified.source_locator,
        "canonical_anchor_event": verified.canonical_anchor_event,
        "occurred_at": _iso8601(verified.occurred_at),
        "authentication": verified.authentication,
        "modern_actor_identity": verified.modern_actor_identity,
        "historical_route_user_id": verified.historical_route_user_id,
        "relational_event_id": verified.relational_event_id,
        "substrate": verified.substrate,
        "session_id": verified.session_id,
        "session_resolution": verified.session_resolution,
        "imported_at": _iso8601(verified.imported_at),
        "visibility_scope": verified.visibility_scope,
        "canonical_component_kind": verified.canonical_component_kind,
        "indexed_memory_kind": verified.indexed_memory_kind,
        "canonical_structural_role": verified.canonical_structural_role,
        "actor_relation": verified.actor_relation,
        "source_category": verified.source_category,
        "mirror_source_store": verified.mirror_source_store,
        "mirror_source_locator": verified.mirror_source_locator,
        "mirror_raw_sha256": verified.mirror_raw_sha256,
        "source_raw_sha256": verified.source_raw_sha256,
        "representation_event_id": verified.representation_event_id,
        "representation_occurred_at": _iso8601(verified.representation_occurred_at),
        "representation_recorded_at": _iso8601(verified.representation_recorded_at),
        "read_only": True,
    }


def browse(paths: HistoricalPaths, cursor: Optional[str] = None, *,
           allowed_cursors: Iterable[str] = (), viewer_principal_actor_id=None,
           viewer_scope=None, active_family_principal_ids=frozenset(),
           page_chars: int = BROWSE_PAGE_CHARS,
           page_records: int = BROWSE_PAGE_RECORDS) -> dict:
    """Read one bounded, contiguous chronological block of verified historical sources.

    ``cursor=None`` means BEGIN AT BEGINNING and requires no opaque identifier.
    Every continuation cursor names the next exact source character (or the
    next exact occurrence) and is accepted only when the ordinary waking
    occasion already offered it.  Ordering is the stable immutable order from
    :func:`hippocampus_retrieval.retrieve_indexed_items_chronological`; there is
    no search score, summary, salience, grouping, replay collapse, or semantic
    ranking anywhere in this path.

    Visibility is applied before a record can affect the page.  A visible
    indexed record whose authoritative source cannot be verified is stated as
    withheld with its mechanical reason and still occupies its chronological
    position; it is never silently skipped for having a legacy or nonstandard
    shape.
    """
    if not isinstance(page_chars, int) or page_chars <= 0 or page_chars > BROWSE_PAGE_CHARS:
        raise HistoricalAccessError("invalid_history_browse_window")
    if not isinstance(page_records, int) or page_records <= 0 or page_records > BROWSE_PAGE_RECORDS:
        raise HistoricalAccessError("invalid_history_browse_record_limit")
    beginning = cursor is None
    if cursor is None:
        start_item_id, start_offset = None, 0
    else:
        if cursor not in frozenset(allowed_cursors or ()):
            raise HistoricalAccessError("history_browse_cursor_not_returned_by_browse")
        start_item_id, start_offset = _parse_browse_cursor(cursor)

    indexed = hippocampus_retrieval.retrieve_indexed_items_chronological(paths.hippocampus_db_path)
    permitted = list(_permitted_items(
        paths, indexed, viewer_principal_actor_id=viewer_principal_actor_id,
        viewer_scope=viewer_scope, active_family_principal_ids=active_family_principal_ids,
    ))
    if start_item_id is None:
        start_index = 0
    else:
        matches = [i for i, item in enumerate(permitted) if item.item_id == start_item_id]
        if len(matches) != 1:
            raise HistoricalAccessError("history_browse_cursor_target_unavailable")
        start_index = matches[0]

    records, withheld = [], []
    used_chars = 0
    index = start_index
    offset = start_offset
    while index < len(permitted) and len(records) + len(withheld) < page_records:
        item = permitted[index]
        try:
            verified = verify_source(paths, item)
        except HistoricalAccessError as exc:
            # A continuation into an unverified record cannot truthfully name a
            # character position in text the host refuses to deliver.
            if offset:
                raise HistoricalAccessError("history_browse_cursor_target_unverifiable") from exc
            withheld.append({
                "occurrence_index": index,
                "item_id": item.item_id,
                "event_id": item.event_id,
                "occurred_at": _iso8601(item.occurred_at),
                "source_store": item.source_store,
                "source_locator": item.source_locator,
                "reason": str(exc),
            })
            index += 1
            continue
        if offset > len(verified.text):
            raise HistoricalAccessError("history_browse_cursor_out_of_range")
        remaining = page_chars - used_chars
        if remaining <= 0:
            break
        end = min(len(verified.text), offset + remaining)
        records.append({
            "occurrence_index": index,
            "item_id": item.item_id,
            "event_id": item.event_id,
            "start": offset,
            "end": end,
            "source_chars": len(verified.text),
            "text": verified.text[offset:end],
            "content_sha256": verified.content_sha256,
            "occurred_at": _iso8601(verified.occurred_at),
            "occurred_at_epoch": verified.occurred_at,
            "source_class": verified.source_class,
            "source_store": verified.source_store,
            "source_locator": verified.source_locator,
            "canonical_anchor_event": verified.canonical_anchor_event,
            "authentication": verified.authentication,
            "modern_actor_identity": verified.modern_actor_identity,
            "historical_route_user_id": verified.historical_route_user_id,
            "relational_event_id": verified.relational_event_id,
            "substrate": verified.substrate,
            "session_id": verified.session_id,
            "session_resolution": verified.session_resolution,
            "imported_at": _iso8601(verified.imported_at),
            "visibility_scope": verified.visibility_scope,
            "canonical_component_kind": verified.canonical_component_kind,
            "indexed_memory_kind": verified.indexed_memory_kind,
            "canonical_structural_role": verified.canonical_structural_role,
            "actor_relation": verified.actor_relation,
            "source_category": verified.source_category,
            "mirror_source_store": verified.mirror_source_store,
            "mirror_source_locator": verified.mirror_source_locator,
            "mirror_raw_sha256": verified.mirror_raw_sha256,
            "source_raw_sha256": verified.source_raw_sha256,
            "representation_event_id": verified.representation_event_id,
            "representation_occurred_at": _iso8601(verified.representation_occurred_at),
            "representation_recorded_at": _iso8601(verified.representation_recorded_at),
            "segment_complete": end == len(verified.text),
        })
        used_chars += end - offset
        if end < len(verified.text):
            offset = end
            break
        index += 1
        offset = 0

    if index < len(permitted):
        next_cursor = browse_cursor(permitted[index].item_id, offset)
    else:
        next_cursor = None
    return {
        "schema": "anaxi.historical_continuity.browse.v1",
        "ordering": "occurred_at,event_id,source_store,source_locator,item_id",
        "selection_basis": "canonical_occurrence_order_not_search_or_discovery_order",
        "beginning": beginning,
        "end": next_cursor is None,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "records": records,
        "withheld": withheld,
        "read_only": True,
    }


def render_browse(result: dict) -> str:
    """Losslessly render a chronological page with source/metadata boundaries."""
    lines = [
        "HISTORICAL EVIDENCE ENVELOPE BEGIN",
        "HOST METADATA (mechanical provenance; not historical source text)",
        "action_type=chronological_browse; read_only=true; schema=" + result["schema"],
        f"beginning={str(bool(result['beginning'])).lower()}; end={str(bool(result['end'])).lower()}; "
        f"ordering={result['ordering']}",
        f"request_cursor={result['cursor'] or 'none (begin at historical source beginning)'}; "
        f"next_cursor={result['next_cursor'] or 'none (historical source end reached)'}; "
        f"delivered_source_count={len(result['records'])}; withheld_count={len(result['withheld'])}",
        "Scope: this envelope establishes only the source windows shown below. It supplies no total record count, "
        "percentage, category inventory, or claim about unseen history.",
        "occurrence_index is a zero-based mechanical position in this stable historical-source ordering; it is not a record ID, "
        "display ordinal, or total-count claim.",
    ]
    if not result["records"] and not result["withheld"]:
        lines.append("SOURCE WINDOWS: none visible")
    entries = [(r["occurrence_index"], "record", r) for r in result["records"]]
    entries += [(r["occurrence_index"], "withheld", r) for r in result["withheld"]]
    for _index, kind, entry in sorted(entries, key=lambda value: value[0]):
        if kind == "withheld":
            lines.append("WITHHELD SOURCE METADATA: " + json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            continue
        metadata = {key: value for key, value in entry.items() if key != "text"}
        lines.append("SOURCE WINDOW METADATA: " + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))
        lines.append("EXACT SOURCE TEXT BEGIN")
        lines.append(entry["text"])
        lines.append("EXACT SOURCE TEXT END")
    lines.extend([
        "INTERPRETATION BOUNDARY: commentary and inference remain yours. Do not present an unseen source, invented "
        "record number, aggregate statistic, or unexecuted tool action as established evidence; label it inference or "
        "NOT_ESTABLISHED unless this envelope mechanically supports it.",
        "HISTORICAL EVIDENCE ENVELOPE END",
    ])
    return "\n".join(lines)


def render_search(result: dict) -> str:
    lines = [
        "HISTORICAL EVIDENCE ENVELOPE BEGIN",
        "HOST METADATA (mechanical provenance; not historical source text)",
        "action_type=semantic_search; read_only=true; schema="
        + result.get("schema", "anaxi.historical_continuity.search.v1"),
        "The candidates below are retrieval results, not host conclusions. Search rank is not historical importance.",
        # Clark's query exactly as he wrote it (JSON-quoted: lossless, one line).  The lexical terms the
        # search used are a deterministic function of it, stated by rule and count rather than repeated: a
        # second copy of his words could be several times their size (NFKC expansion) and would crowd out
        # the evidence beside it (whole-occasion capacity, 2026-10-08).  The structured result keeps them.
        f"query: {json.dumps(result['query'], ensure_ascii=False)}",
        f"normalized lexical terms: {len(result['query_terms'])} (NFKC-normalized, lowercased alphanumeric "
        f"words of {hippocampus_retrieval.MIN_TERM_LENGTH}+ characters from the query above, the first "
        f"{hippocampus_retrieval.MAX_QUERY_TERMS} distinct)",
    ]
    if not result["candidates"]:
        lines.append("candidates: none verified and visible")
    for candidate in result["candidates"]:
        lines.extend([
            f"candidate {candidate['rank']}: handle={candidate['handle']}; read_cursor={candidate['read_cursor']}",
            f"occurred_at={candidate['occurred_at']}; source_class={candidate['source_class']}; "
            f"canonical_anchor_event={candidate['canonical_anchor_event']}",
            f"source={candidate['source_store']} {candidate['source_locator']}; chars={candidate['source_chars']}; "
            f"sha256={candidate['content_sha256']}",
            f"source_category={candidate.get('source_category')}; mirror_source="
            f"{candidate.get('mirror_source_store') or epistemic_status.NOT_APPLICABLE} "
            f"{candidate.get('mirror_source_locator') or epistemic_status.NOT_APPLICABLE}; "
            f"mirror_raw_sha256={candidate.get('mirror_raw_sha256') or epistemic_status.NOT_APPLICABLE}",
            f"source_raw_sha256={candidate.get('source_raw_sha256') or epistemic_status.NOT_APPLICABLE}; "
            f"representation_event_id={candidate.get('representation_event_id') or epistemic_status.NOT_APPLICABLE}",
            f"representation_occurred_at={candidate.get('representation_occurred_at') or epistemic_status.NOT_APPLICABLE}; "
            f"representation_recorded_at={candidate.get('representation_recorded_at') or epistemic_status.NOT_APPLICABLE}",
            f"authentication={candidate['authentication']}; modern_actor_identity={candidate['modern_actor_identity']}; "
            f"historical_route_user_id={candidate['historical_route_user_id'] or 'NOT_ESTABLISHED'}; "
            f"relational_event_id={candidate['relational_event_id'] or 'NOT_APPLICABLE'}; "
            f"session={candidate['session_resolution']}",
            # CONTENT provenance, stated independently of the retrieval provenance above:
            # what this material IS, not how it arrived.  A reader can now tell a prior
            # Clark expression from a human one from a host record without inferring it
            # from the store name, and an unestablished relation says so.
            f"record_kind={candidate['indexed_memory_kind']}; "
            f"actor_relation={candidate['actor_relation'] or epistemic_status.NOT_ESTABLISHED}; "
            f"structural_role={candidate['canonical_structural_role'] or epistemic_status.NOT_ESTABLISHED}; "
            f"component_kind={candidate['canonical_component_kind'] or epistemic_status.NOT_APPLICABLE}",
            "EXACT SOURCE EXCERPT BEGIN",
            candidate["exact_excerpt"],
            "EXACT SOURCE EXCERPT END",
        ])
    if result["withheld"]:
        lines.append("withheld candidates: " + json.dumps(result["withheld"], sort_keys=True))
    lines.extend([
        "NEXT ACTION MECHANICS: each exact_excerpt above is only a bounded search preview. To inspect a "
        "candidate's substantive source, choose act=read_historical_item on the next action and copy that "
        "candidate's returned read_cursor exactly into historical_cursor. Repeating or refining the search does "
        "not read the candidate; any other act remains available and stops or changes direction.",
        "Scope: this envelope establishes only the returned candidates/excerpts. It supplies no total record count, "
        "percentage, category inventory, or fact about unseen history.",
        "INTERPRETATION BOUNDARY: commentary and inference remain yours. Cite shown provenance and its source_category; "
        "claim canonical authority only when that category establishes it, or label the claim inference/NOT_ESTABLISHED.",
        "HISTORICAL EVIDENCE ENVELOPE END",
    ])
    return "\n".join(lines)


def render_read(result: dict) -> str:
    completeness = "complete" if not result["more"] else f"more remains; next_cursor={result['next_cursor']}"
    metadata = (
        f"handle={result['handle']}; characters={result['start']}-{result['end']} of {result['source_chars']}; {completeness}\n"
        f"occurred_at={result['occurred_at']}; source_class={result['source_class']}; "
        f"source={result['source_store']} {result['source_locator']}; canonical_anchor_event={result['canonical_anchor_event']}\n"
        f"source_category={result.get('source_category')}; mirror_source="
        f"{result.get('mirror_source_store') or epistemic_status.NOT_APPLICABLE} "
        f"{result.get('mirror_source_locator') or epistemic_status.NOT_APPLICABLE}; "
        f"mirror_raw_sha256={result.get('mirror_raw_sha256') or epistemic_status.NOT_APPLICABLE}\n"
        f"source_raw_sha256={result.get('source_raw_sha256') or epistemic_status.NOT_APPLICABLE}; "
        f"representation_event_id={result.get('representation_event_id') or epistemic_status.NOT_APPLICABLE}\n"
        f"representation_occurred_at={result.get('representation_occurred_at') or epistemic_status.NOT_APPLICABLE}; "
        f"representation_recorded_at={result.get('representation_recorded_at') or epistemic_status.NOT_APPLICABLE}\n"
        f"sha256={result['content_sha256']}; authentication={result['authentication']}; "
        f"modern_actor_identity={result['modern_actor_identity']}; "
        f"historical_route_user_id={result['historical_route_user_id'] or 'NOT_ESTABLISHED'}; "
        f"relational_event_id={result['relational_event_id'] or epistemic_status.NOT_APPLICABLE}; "
        f"session={result['session_resolution']}\n"
        # CONTENT provenance, independent of the retrieval provenance above -- see render_search.
        f"record_kind={result['indexed_memory_kind']}; "
        f"actor_relation={result['actor_relation'] or epistemic_status.NOT_ESTABLISHED}; "
        f"structural_role={result['canonical_structural_role'] or epistemic_status.NOT_ESTABLISHED}; "
        f"component_kind={result['canonical_component_kind'] or epistemic_status.NOT_APPLICABLE}"
    )
    return (
        "HISTORICAL EVIDENCE ENVELOPE BEGIN\n"
        "HOST METADATA (mechanical provenance; not historical source text)\n"
        "action_type=exact_read; read_only=true; schema="
        + result.get("schema", "anaxi.historical_continuity.read.v1") + "\n"
        + metadata + "\n"
        "Scope: this envelope establishes only this source window. It supplies no total record count, percentage, "
        "category inventory, or fact about unseen history.\n"
        "EXACT SOURCE TEXT WINDOW BEGIN\n" + result["text"] + "\nEXACT SOURCE TEXT WINDOW END\n"
        "INTERPRETATION BOUNDARY: commentary and inference remain yours. Cite the provenance and source_category above; "
        "claim canonical authority only when that category establishes it, or label the claim inference/NOT_ESTABLISHED.\n"
        "HISTORICAL EVIDENCE ENVELOPE END"
    )


def inspected_text(read_results: Iterable[dict]) -> dict[str, str]:
    """Reassemble only contiguous windows actually delivered this occasion."""
    groups = {}
    for result in read_results:
        groups.setdefault(result["handle"], []).append(result)
    assembled = {}
    for handle, windows in groups.items():
        ordered = sorted(windows, key=lambda row: row["start"])
        expected, pieces = 0, []
        for row in ordered:
            if row["start"] != expected:
                break
            pieces.append(row["text"])
            expected = row["end"]
        assembled[handle] = "".join(pieces)
    return assembled
