"""
Anaxi -- Bounded hippocampus core, Slice B: deterministic retrieval.

Owns only: query normalization, FTS5 query construction, bounded
search, deterministic ranking/tie-breaking, structured retrieval
result construction, and deterministic HIPPOCAMPAL_CONTEXT_V1
rendering.

Does NOT ingest, rebuild, or mutate any store (source or hippocampal);
does not call a model; does not touch node_embeddings/_embed_text/the
existing vector retrieval mechanism; does not decide emotional
importance; does not alter Kardia/runtime -- none of that is Slice B.
Slice-B retrieval never runs sync_hippocampus() -- sync -> retrieve ->
render is the future Slice-C host seam's job, not this module's.

The query input is exactly the current human prompt string. No model
preprocessing, no query-expansion model, no synonym model, no
embeddings anywhere in this file.
"""

import dataclasses
import json
import os
import pathlib
import sqlite3
import sys
import unicodedata
from typing import Optional

import epistemic_status

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hippocampus_store import (
    SCHEMA_VERSION, ITEM_ID_VERSION,
    HippocampusUnavailableError, HippocampusSchemaError,
)

# =============================================================================
# Frozen v1 retrieval limits -- do not increase.
# =============================================================================
CANDIDATE_LIMIT = 24
RESULT_LIMIT = 6
MAX_CHARS_PER_ITEM = 1200
MAX_AGGREGATE_CHARS = 6000
MAX_QUERY_TERMS = 12
MIN_TERM_LENGTH = 3

RETRIEVAL_METHOD = "fts5_bm25_v1"

# CORRECTION / RETRIEVAL LAW (final completion build, 2026-09-24).  Measured defect: with 30 older
# repetitions of a statement, a single later correction was never retrieved (every one of the
# RESULT_LIMIT slots went to a repetition).  Two deterministic, non-semantic, inspectable rules --
# neither scores meaning, neither writes a synthetic sentence:
#   * records with IDENTICAL text and identical provenance class (creator, memory kind, attribution)
#     occupy ONE slot: the most recent real record, stating how many times that text was recorded and
#     when first -- a host count, never a merged or "centroid" statement;
#   * RECENCY_RESERVED_SLOTS of the RESULT_LIMIT slots go to the most recent matching records whatever
#     their lexical score, so repetition can never write the ranking against what came later.
# Every item says which rule selected it (selection_basis).
RECENCY_RESERVED_SLOTS = 2
SELECTION_RELEVANCE = "lexical_relevance"
SELECTION_RECENCY = "most_recent_match"

# ENCOUNTER-TURN COHERENCE (canonical-history-first repair, 2026-10-03).  Measured defect: an explicit revisit of a
# past resource encounter retrieved the encounter's mechanical record (path, digests, delivered portion) -- its
# best lexical match -- but never the waking turn in which that encounter was received and answered, although the
# canonical provenance link (the encounter's triggering_waking_event_id) is exact.  The subject got metadata and a
# later recap instead of the episode.  Rule (provenance-structural, never semantic, never a ranking change): a
# selected resource-encounter record is followed, in its own rank position, by the indexed records of its
# triggering turn -- that turn's human expression and Clark expression, bounded like every item.  They do not take
# any of the RESULT_LIMIT places and are labelled with this selection basis and the record they complete.
SELECTION_ENCOUNTER_TURN = "encounter_turn_companion"

# EXACT CURRENT-MESSAGE ECHO (2026-10-03).  Live: an earlier human message byte-identical to the present one was the
# best lexical match for it and was delivered back into the same inference as "history".  A human-expression record
# whose full stored text equals the present query text exactly is withheld from delivery (bytes only: no
# paraphrase, similarity or topic test).  It takes no result place, stays a listed candidate marked with this basis
# (so the delivery manifest shows it was withheld), and the canonical record is untouched.
SELECTION_EXACT_ECHO = "exact_echo_of_present_message"
SELECTION_CONSOLIDATION_CUE = "consolidation_cue"
ENCOUNTER_RECORD_COMPONENT_KIND = "resource_encounter_fact"
_TRIGGERING_TURN_COMPONENT_KIND = "triggering_waking_event_id"
_HUMAN_INPUT_EVENT_COMPONENT_KIND = "human_input_event_id"
_TURN_COMPANION_KINDS = ("human_expression", "clark_expression")

CONTEXT_HEADING = "HIPPOCAMPAL_CONTEXT_V1"
EPISTEMIC_STATEMENT = (
    "These are retrieved historical records. Their memory-kind, attribution, "
    "authentication, and source fields describe their epistemic status. "
    "Expression records establish that something was expressed; they do not "
    "by themselves establish that the expressed content is true."
)
EMPTY_RESULT_MARKER = "(no retrieved items)"

# CPI0: emitted only when at least one retrieved item carries canonical-person provenance.  It states what
# kind of fact those annotations are; it says nothing about any person.
PERSON_PROVENANCE_STATEMENT = (
    "Items with person_provenance name who spoke and how the record is framed. A person's past words describe "
    "what they said then; they do not establish what is true of that person now, and are not a description "
    "of who they are. Only a standing_request that is still active was made prospectively by the speaker."
)

# SLP1-D: the one neutral, source-consistent label for a retrieved item
# whose source_store is "sleep_derivations" (hippocampus_store.py's
# fourth ingestion path). Deliberately says none of: memory, belief,
# truth, important, insight, revelation, identity, subconscious truth --
# only that this is a tentative association that arose during Sleep,
# exactly the FROZEN PRINCIPLE's own wording ("acquires no additional
# authority merely because he dreamed it"). Emitted as a plain text line
# immediately before that item's own JSON payload line -- never folded
# into the JSON itself, so it reads as an orientation cue, not another
# machine field to parse.
SLEEP_DERIVATION_SOURCE_STORE = "sleep_derivations"
SLEEP_DERIVED_LABEL = "[sleep-derived tentative association]"


# =============================================================================
# Frozen query normalization (step order is the frozen algorithm itself).
# =============================================================================
def normalize_query(query_text: str) -> list:
    """1. Unicode NFKC normalization; 2. lowercase; 3. replace non-
    alphanumeric characters with spaces (Unicode-aware, matching step 1's
    own Unicode intent -- not ASCII-only); 4. split on whitespace;
    5. discard tokens shorter than 3 characters; 6. deduplicate while
    preserving first occurrence; 7. retain at most the first 12
    surviving terms."""
    normalized = unicodedata.normalize("NFKC", query_text).lower()
    spaced = "".join(ch if ch.isalnum() else " " for ch in normalized)
    tokens = spaced.split()
    seen = set()
    terms = []
    for token in tokens:
        if len(token) < MIN_TERM_LENGTH:
            continue
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return terms


def _fts5_quote(term: str) -> str:
    """A double-quoted FTS5 string literal is always treated as literal
    text, never as query syntax -- this is what keeps raw user text (or
    a normalized term derived from it) from ever being interpreted as
    an FTS5 operator, column filter, NEAR clause, or prefix wildcard."""
    return '"' + term.replace('"', '""') + '"'


def build_fts_query(terms: list) -> Optional[str]:
    """Safely quoted OR query. Returns None for zero terms -- the
    caller must treat that as "no query to run," never as "match
    everything.\""""
    if not terms:
        return None
    return " OR ".join(_fts5_quote(t) for t in terms)


# =============================================================================
# Structured retrieval result.
# =============================================================================
@dataclasses.dataclass(frozen=True)
class RetrievedMemory:
    item_id: str
    event_id: str
    occurred_at: int
    source_store: str
    source_locator: str
    memory_kind: str
    attribution_status: str
    authentication_status: str
    creator_actor_id: Optional[str]
    creator_actor_type: Optional[str]
    pipeline_id: Optional[str]
    session_id: Optional[str]
    session_resolution: str
    component_kind: Optional[str]
    content: str
    content_truncated: bool
    retrieval_method: str
    bm25_score: float
    query_terms: tuple
    selection_basis: str = SELECTION_RELEVANCE
    repeat_count: int = 1
    first_occurred_at: Optional[int] = None
    companion_of_event_id: Optional[str] = None
    source_chars: Optional[int] = None      # length of the full stored text (content may be a prefix of it)
    content_sha256: Optional[str] = None    # index integrity claim; source/ledger remains authoritative


@dataclasses.dataclass(frozen=True)
class RetrievalResult:
    query_terms: tuple
    items: tuple


# =============================================================================
# Read-only hippocampus access.
# =============================================================================
def _readonly_uri(path: str) -> str:
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


def _open_hippocampus_readonly(path: str) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise HippocampusUnavailableError(f"hippocampal database does not exist: {path!r}")
    try:
        conn = sqlite3.connect(_readonly_uri(path), uri=True)
        conn.execute("PRAGMA query_only = ON;")
        conn.execute("SELECT count(*) FROM sqlite_master;")
    except sqlite3.OperationalError as e:
        raise HippocampusUnavailableError(f"could not open hippocampal database read-only: {path!r} ({e})") from e
    _validate_schema(conn)
    return conn


def _validate_schema(conn: sqlite3.Connection) -> None:
    """Sufficient validation to avoid querying an incompatible DB --
    deliberately does not import/call hippocampus_store.py's own
    (private, non-reused) schema-verification helper; this is a
    small, independent, read-only re-check against the same public
    version constants."""
    try:
        rows = dict(conn.execute("SELECT key, value FROM hippocampus_meta;").fetchall())
    except sqlite3.OperationalError as e:
        raise HippocampusSchemaError(f"hippocampal database is missing required schema: {e}") from e
    if rows.get("schema_version") != str(SCHEMA_VERSION):
        raise HippocampusSchemaError(f"unexpected schema_version: {rows.get('schema_version')!r}")
    if rows.get("item_id_version") != str(ITEM_ID_VERSION):
        raise HippocampusSchemaError(f"unexpected item_id_version: {rows.get('item_id_version')!r}")
    required_tables = {"hippocampal_items", "hippocampal_items_fts"}
    existing_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view');").fetchall()}
    missing = required_tables - existing_tables
    if missing:
        raise HippocampusSchemaError(f"hippocampal database is missing required table(s): {sorted(missing)!r}")


# =============================================================================
# Candidate fetch (FTS5 BM25) and deterministic bounding.
# =============================================================================
_CANDIDATE_COLUMNS = (
    "item_id", "event_id", "occurred_at", "source_store", "source_locator",
    "memory_kind", "attribution_status", "authentication_status",
    "creator_actor_id", "creator_actor_type", "pipeline_id",
    "session_id", "session_resolution", "component_kind", "content_text", "content_sha256",
)


def _fetch_candidates(conn: sqlite3.Connection, fts_query: str, limit: int = CANDIDATE_LIMIT,
                      order: str = "relevance") -> list:
    """Ordering (relevance): bm25(hippocampal_items_fts) ASCENDING first -- SQLite
    FTS5's bm25() returns a NEGATIVE score where a SMALLER (more
    negative) value is a BETTER match (confirmed empirically and
    documented in SQLite's own FTS5 docs); ORDER BY ... ASC therefore
    puts the best lexical match first, satisfying "better BM25
    relevance first" even though the raw numbers get larger as
    relevance gets worse. Then occurred_at DESCENDING (more recent
    first). Then item_id ASCENDING (lexicographically smaller first) --
    a fixed, content-independent, fully deterministic final
    tie-breaker. ``order="recency"``: occurred_at DESCENDING, then item_id.
    Identical-text-and-provenance records are collapsed BEFORE the LIMIT (see the
    CORRECTION / RETRIEVAL LAW note above) into their most recent record with its
    repeat count and first occurrence.  LIMIT bounds how many candidate rows are ever
    fetched from SQL at all, independent of the later, smaller returned-item bound."""
    columns = ", ".join("hi." + c for c in _CANDIDATE_COLUMNS)
    order_sql = ("score ASC, occurred_at DESC, item_id ASC" if order == "relevance"
                 else "occurred_at DESC, item_id ASC")
    key = "content_sha256, creator_actor_id, memory_kind, attribution_status"
    # bm25() is only callable in the MATCH query itself, so it is materialized first; the collapse
    # windows run over that fixed result.
    sql = (
        "WITH matched AS MATERIALIZED ("
        "SELECT " + columns + ", bm25(hippocampal_items_fts) AS score "
        "FROM hippocampal_items_fts "
        "JOIN hippocampal_items hi ON hi.row_id = hippocampal_items_fts.rowid "
        "WHERE hippocampal_items_fts MATCH ?), "
        "ranked AS (SELECT matched.*, "
        "ROW_NUMBER() OVER (PARTITION BY " + key + " ORDER BY occurred_at DESC, item_id ASC) AS rn, "
        "COUNT(*) OVER (PARTITION BY " + key + ") AS repeat_count, "
        "MIN(occurred_at) OVER (PARTITION BY " + key + ") AS first_occurred_at FROM matched) "
        "SELECT " + ", ".join(_CANDIDATE_COLUMNS) + ", score, repeat_count, first_occurred_at "
        "FROM ranked WHERE rn = 1 ORDER BY " + order_sql + " LIMIT ?;"
    )
    rows = conn.execute(sql, (fts_query, limit)).fetchall()
    cols = _CANDIDATE_COLUMNS + ("score", "repeat_count", "first_occurred_at")
    return [dict(zip(cols, row)) for row in rows]


def _merge_relevance_and_recency(relevance, recency, result_limit, reserved):
    """Relevance order, with ``reserved`` of the ``result_limit`` places kept for the most recent
    matches not already chosen.  Deterministic; returns (row, basis) pairs in the order delivered."""
    chosen, seen = [], set()
    for row in relevance[: max(0, result_limit - reserved)]:
        chosen.append((row, SELECTION_RELEVANCE))
        seen.add(row["item_id"])
    for row in recency:
        if len(chosen) >= result_limit or sum(b == SELECTION_RECENCY for _r, b in chosen) >= reserved:
            break
        if row["item_id"] not in seen:
            chosen.append((row, SELECTION_RECENCY))
            seen.add(row["item_id"])
    for row in relevance[max(0, result_limit - reserved):]:       # unused reserved places: relevance
        if len(chosen) >= result_limit:
            break
        if row["item_id"] not in seen:
            chosen.append((row, SELECTION_RELEVANCE))
            seen.add(row["item_id"])
    return chosen


def _select_bounded_items(candidates: list, result_limit: int, max_item_chars: int, max_aggregate_chars: int) -> list:
    """Deterministic character truncation, never mutating the stored
    content_text itself (only the in-memory copy placed into the
    returned item). Processes candidates in their already-ranked
    order; stops at whichever bound (result_limit items, or the
    aggregate character budget) is reached first."""
    selected = []
    aggregate = 0
    for row in candidates:
        if len(selected) >= result_limit:
            break
        remaining_budget = max_aggregate_chars - aggregate
        if remaining_budget <= 0:
            break
        content = row["content_text"]
        truncated = False
        if len(content) > max_item_chars:
            content = content[:max_item_chars]
            truncated = True
        if len(content) > remaining_budget:
            content = content[:remaining_budget]
            truncated = True
        aggregate += len(content)
        selected.append((row, content, truncated))
    return selected


def _encounter_turns(provenance_db_path: Optional[str], encounter_event_ids: list) -> dict:
    """{encounter event id: its triggering waking turn id}, read-only from canonical provenance.  Empty when no
    provenance path is given or the database is unavailable -- the rule then simply does not apply."""
    if not provenance_db_path or not encounter_event_ids or not os.path.isfile(provenance_db_path):
        return {}
    try:
        conn = sqlite3.connect(_readonly_uri(provenance_db_path), uri=True)
    except sqlite3.Error:
        return {}
    try:
        marks = ",".join("?" * len(encounter_event_ids))
        rows = conn.execute(
            "SELECT event_id, component_text FROM event_components WHERE component_kind = ? AND event_id IN ("
            + marks + ")", (_TRIGGERING_TURN_COMPONENT_KIND, *encounter_event_ids)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {event_id: turn_id for event_id, turn_id in rows if turn_id}


def _turn_human_inputs(provenance_db_path: Optional[str], turn_event_ids: list) -> dict:
    """{waking turn id: canonical human-input event id}, read-only.

    Production indexes authenticated human speech under its own ``human_waking_input`` event, not
    under the waking-turn event that points to it.  Treating only equal event ids as companions
    makes a real human request independently droppable from the observation and reply it caused.
    """
    if not provenance_db_path or not turn_event_ids or not os.path.isfile(provenance_db_path):
        return {}
    try:
        conn = sqlite3.connect(_readonly_uri(provenance_db_path), uri=True)
    except sqlite3.Error:
        return {}
    try:
        marks = ",".join("?" * len(turn_event_ids))
        rows = conn.execute(
            "SELECT event_id, component_text FROM event_components WHERE component_kind = ? "
            "AND event_id IN (" + marks + ")",
            (_HUMAN_INPUT_EVENT_COMPONENT_KIND, *turn_event_ids)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {turn_id: human_event_id for turn_id, human_event_id in rows if human_event_id}


def _turn_companion_rows(conn: sqlite3.Connection, turn_event_id: str,
                         human_input_event_id: Optional[str] = None) -> list:
    """The triggering turn's indexed records: human expression, then receiving-actor expression."""
    columns = ", ".join(_CANDIDATE_COLUMNS)
    event_ids = (turn_event_id,) if human_input_event_id is None else (
        turn_event_id, human_input_event_id)
    marks = ",".join("?" * len(event_ids))
    rows = conn.execute(
        "SELECT " + columns + " FROM hippocampal_items WHERE event_id IN (" + marks + ") "
        "AND memory_kind IN (?, ?) "
        "ORDER BY CASE memory_kind WHEN 'human_expression' THEN 0 ELSE 1 END, item_id ASC",
        (*event_ids, *_TURN_COMPANION_KINDS)).fetchall()
    return [dict(zip(_CANDIDATE_COLUMNS, row)) for row in rows]


def _with_encounter_turns(conn, merged, provenance_db_path):
    """(row, basis, companion_of) in delivery order: each selected encounter record followed by its triggering
    turn's records (skipping any record already selected on its own)."""
    turns = _encounter_turns(provenance_db_path, [row["event_id"] for row, _b in merged
                                                  if row["component_kind"] == ENCOUNTER_RECORD_COMPONENT_KIND])
    human_inputs = _turn_human_inputs(provenance_db_path, list(set(turns.values())))
    companion_rows = {
        encounter_id: _turn_companion_rows(conn, turn_id, human_inputs.get(turn_id))
        for encounter_id, turn_id in turns.items()
    }
    # A companion may also rank on its own.  It still belongs to the canonical episode for
    # trimming, so remove that top-level occurrence and emit it once beside the encounter.  This
    # changes no selected item and no lexical rank; it only prevents pressure from splitting a
    # canonically linked historical unit.
    claimed_companion_ids = {
        row["item_id"] for rows in companion_rows.values() for row in rows
    }
    selected_by_id = {row["item_id"]: row for row, _basis in merged}
    sequence = []
    emitted_companion_ids = set()
    for row, basis in merged:
        if row["item_id"] in claimed_companion_ids:
            continue
        sequence.append((row, basis, None))
        turn_id = turns.get(row["event_id"]) if row["component_kind"] == ENCOUNTER_RECORD_COMPONENT_KIND else None
        if turn_id is None:
            continue
        for companion in companion_rows[row["event_id"]]:
            if companion["item_id"] in emitted_companion_ids:
                continue
            emitted_companion_ids.add(companion["item_id"])
            selected = selected_by_id.get(companion["item_id"])
            companion = selected if selected is not None else dict(
                companion, score=None, repeat_count=1, first_occurred_at=None)
            sequence.append((companion,
                             SELECTION_ENCOUNTER_TURN, row["event_id"]))
    return sequence


def _bound_sequence(sequence, result_limit: int, max_item_chars: int, max_aggregate_chars: int) -> list:
    """_select_bounded_items over a delivery sequence in which companion records (companion_of set) take no
    result_limit place but share the per-item and aggregate character bounds, in order."""
    selected = []
    aggregate = 0
    anchors = 0
    for row, basis, companion_of in sequence:
        if companion_of is None and anchors >= result_limit:
            break
        remaining_budget = max_aggregate_chars - aggregate
        if remaining_budget <= 0:
            break
        content = row["content_text"]
        truncated = False
        if len(content) > max_item_chars:
            content = content[:max_item_chars]
            truncated = True
        if len(content) > remaining_budget:
            content = content[:remaining_budget]
            truncated = True
        aggregate += len(content)
        anchors += companion_of is None
        selected.append((row, basis, companion_of, content, truncated))
    return selected


def retrieve_hippocampal_context(hippocampus_db_path: str, query_text: str,
                                 provenance_db_path: Optional[str] = None) -> RetrievalResult:
    """Requires an existing, schema-valid hippocampal DB; opens it
    read-only; normalizes the query; returns structured results.
    Performs no model call and no source sync -- sync -> retrieve ->
    render is composed by the future Slice-C host seam, not here.
    ``provenance_db_path`` (read-only) enables the ENCOUNTER-TURN
    COHERENCE rule above; without it retrieval is exactly as before."""
    terms = normalize_query(query_text)
    conn = _open_hippocampus_readonly(hippocampus_db_path)
    try:
        fts_query = build_fts_query(terms)
        if fts_query is None:
            return RetrievalResult(query_terms=tuple(terms), items=())

        relevance = _fetch_candidates(conn, fts_query, limit=CANDIDATE_LIMIT)
        recency = _fetch_candidates(conn, fts_query, limit=RECENCY_RESERVED_SLOTS + RESULT_LIMIT, order="recency")

        def _echo(row):
            return row["memory_kind"] == "human_expression" and row["content_text"] == query_text
        echoes = list({row["item_id"]: row for row in relevance + recency if _echo(row)}.values())
        merged = _merge_relevance_and_recency([r for r in relevance if not _echo(r)],
                                              [r for r in recency if not _echo(r)], RESULT_LIMIT, RECENCY_RESERVED_SLOTS)
        bounded = _bound_sequence(_with_encounter_turns(conn, merged, provenance_db_path),
                                  RESULT_LIMIT, MAX_CHARS_PER_ITEM, MAX_AGGREGATE_CHARS)
        # Withheld exact echoes ride after every delivered candidate, outside every bound (never rendered).
        bounded += [(row, SELECTION_EXACT_ECHO, None, row["content_text"][:MAX_CHARS_PER_ITEM],
                     len(row["content_text"]) > MAX_CHARS_PER_ITEM) for row in echoes]

        items = []
        for row, basis, companion_of, content, truncated in bounded:
            items.append(RetrievedMemory(
                item_id=row["item_id"], event_id=row["event_id"], occurred_at=row["occurred_at"],
                source_store=row["source_store"], source_locator=row["source_locator"],
                memory_kind=row["memory_kind"], attribution_status=row["attribution_status"],
                authentication_status=row["authentication_status"],
                creator_actor_id=row["creator_actor_id"], creator_actor_type=row["creator_actor_type"],
                pipeline_id=row["pipeline_id"], session_id=row["session_id"],
                session_resolution=row["session_resolution"], component_kind=row["component_kind"],
                content=content, content_truncated=truncated,
                retrieval_method=RETRIEVAL_METHOD, bm25_score=row["score"], query_terms=tuple(terms),
                selection_basis=basis, repeat_count=int(row["repeat_count"]),
                first_occurred_at=(row["first_occurred_at"] if int(row["repeat_count"]) > 1 else None),
                companion_of_event_id=companion_of, source_chars=len(row["content_text"]),
                content_sha256=row.get("content_sha256"),
            ))
        return RetrievalResult(query_terms=tuple(terms), items=tuple(items))
    finally:
        conn.close()


def retrieve_indexed_item(hippocampus_db_path: str, item_id: str) -> Optional[RetrievedMemory]:
    """Read one exact indexed item, untruncated, by its stable item id.

    This is an address lookup for a handle already returned by retrieval, not
    another search.  It opens the hippocampus read-only and has no sync or
    persistence side effect.  The returned index row is still only a claim:
    callers must re-verify its text and provenance against the authoritative
    canonical/source chain before delivery.
    """
    if not isinstance(item_id, str) or not item_id:
        return None
    conn = _open_hippocampus_readonly(hippocampus_db_path)
    try:
        columns = ", ".join(_CANDIDATE_COLUMNS)
        rows = conn.execute(
            "SELECT " + columns + " FROM hippocampal_items WHERE item_id = ?",
            (item_id,),
        ).fetchall()
        if len(rows) != 1:
            return None
        row = dict(zip(_CANDIDATE_COLUMNS, rows[0]))
        return RetrievedMemory(
            item_id=row["item_id"], event_id=row["event_id"], occurred_at=row["occurred_at"],
            source_store=row["source_store"], source_locator=row["source_locator"],
            memory_kind=row["memory_kind"], attribution_status=row["attribution_status"],
            authentication_status=row["authentication_status"],
            creator_actor_id=row["creator_actor_id"], creator_actor_type=row["creator_actor_type"],
            pipeline_id=row["pipeline_id"], session_id=row["session_id"],
            session_resolution=row["session_resolution"], component_kind=row["component_kind"],
            content=row["content_text"], content_truncated=False,
            retrieval_method=RETRIEVAL_METHOD, bm25_score=None, query_terms=(),
            selection_basis="stable_handle_read", source_chars=len(row["content_text"]),
            content_sha256=row["content_sha256"],
        )
    finally:
        conn.close()


def retrieve_indexed_items_chronological(hippocampus_db_path: str) -> tuple:
    """Read every indexed record in stable canonical-occurrence order.

    This is the mechanical source iterator for first-class chronological
    browsing.  It performs no lexical search, ranking, grouping, deduplication,
    recency reservation, or model call.  The full source text is returned so
    the historical-continuity layer can verify it against the authoritative
    source before applying its own bounded page window.

    The total order uses only immutable indexed facts and the stable item id:
    ``occurred_at, event_id, source_store, source_locator, item_id``.  It is
    therefore independent of discovery order and SQLite row insertion order.
    Callers still must apply the canonical visibility and source-verification
    laws before delivering any record.
    """
    conn = _open_hippocampus_readonly(hippocampus_db_path)
    try:
        columns = ", ".join(_CANDIDATE_COLUMNS)
        rows = conn.execute(
            "SELECT " + columns + " FROM hippocampal_items "
            "ORDER BY occurred_at ASC, event_id ASC, source_store ASC, "
            "source_locator ASC, item_id ASC"
        ).fetchall()
        items = []
        for raw in rows:
            row = dict(zip(_CANDIDATE_COLUMNS, raw))
            items.append(RetrievedMemory(
                item_id=row["item_id"], event_id=row["event_id"], occurred_at=row["occurred_at"],
                source_store=row["source_store"], source_locator=row["source_locator"],
                memory_kind=row["memory_kind"], attribution_status=row["attribution_status"],
                authentication_status=row["authentication_status"],
                creator_actor_id=row["creator_actor_id"], creator_actor_type=row["creator_actor_type"],
                pipeline_id=row["pipeline_id"], session_id=row["session_id"],
                session_resolution=row["session_resolution"], component_kind=row["component_kind"],
                content=row["content_text"], content_truncated=False,
                retrieval_method="chronological_canonical_v1", bm25_score=None, query_terms=(),
                selection_basis="canonical_occurrence_order", repeat_count=1,
                first_occurred_at=None, companion_of_event_id=None,
                source_chars=len(row["content_text"]), content_sha256=row["content_sha256"],
            ))
        return tuple(items)
    finally:
        conn.close()


def retrieve_canonical_events(hippocampus_db_path: str, event_ids: list, *,
                              query_terms=(), provenance_db_path: Optional[str] = None) -> list:
    """Read exact already-indexed records for subject-cued canonical events.

    This is not another semantic search. ``event_ids`` are mechanically bound
    canonical targets selected by subject-authored cues. Rows are re-read from
    the same hippocampal index ordinary retrieval uses, with their real source
    metadata intact so canonical reconstruction can verify them. For a waking
    turn, its explicitly linked human-input event is included as part of the
    same episode. Missing/unindexed targets add nothing; no placeholder record
    is fabricated.
    """
    ordered_targets = list(dict.fromkeys(e for e in event_ids if isinstance(e, str) and e))
    if not ordered_targets:
        return []
    linked_humans = _turn_human_inputs(provenance_db_path, ordered_targets)
    wanted = []
    for event_id in ordered_targets:
        human_id = linked_humans.get(event_id)
        if human_id:
            wanted.append(human_id)
        wanted.append(event_id)
    wanted = list(dict.fromkeys(wanted))

    conn = _open_hippocampus_readonly(hippocampus_db_path)
    try:
        marks = ",".join("?" for _ in wanted)
        columns = ", ".join(_CANDIDATE_COLUMNS)
        rows = conn.execute(
            "SELECT " + columns + " FROM hippocampal_items WHERE event_id IN (" + marks + ")",
            tuple(wanted),
        ).fetchall()
    finally:
        conn.close()
    by_event = {event_id: [] for event_id in wanted}
    for row in rows:
        item = dict(zip(_CANDIDATE_COLUMNS, row))
        by_event.setdefault(item["event_id"], []).append(item)
    for event_rows in by_event.values():
        event_rows.sort(key=lambda r: (
            0 if r["memory_kind"] == "human_expression" else
            1 if r["memory_kind"] == "clark_expression" else 2,
            r["item_id"],
        ))

    result = []
    aggregate = 0
    for event_id in wanted:
        for row in by_event.get(event_id, ()):
            if aggregate >= MAX_AGGREGATE_CHARS:
                return result
            content = row["content_text"]
            limit = min(MAX_CHARS_PER_ITEM, MAX_AGGREGATE_CHARS - aggregate)
            delivered = content[:limit]
            aggregate += len(delivered)
            result.append(RetrievedMemory(
                item_id=row["item_id"], event_id=row["event_id"], occurred_at=row["occurred_at"],
                source_store=row["source_store"], source_locator=row["source_locator"],
                memory_kind=row["memory_kind"], attribution_status=row["attribution_status"],
                authentication_status=row["authentication_status"],
                creator_actor_id=row["creator_actor_id"], creator_actor_type=row["creator_actor_type"],
                pipeline_id=row["pipeline_id"], session_id=row["session_id"],
                session_resolution=row["session_resolution"], component_kind=row["component_kind"],
                content=delivered, content_truncated=len(delivered) < len(content),
                retrieval_method=RETRIEVAL_METHOD, bm25_score=None,
                query_terms=tuple(query_terms), selection_basis=SELECTION_CONSOLIDATION_CUE,
                repeat_count=1, first_occurred_at=None, companion_of_event_id=None,
                source_chars=len(content), content_sha256=row["content_sha256"],
            ))
    return result


# =============================================================================
# Delivery units: what one prompt-budget trim step removes.
# =============================================================================
# ENCOUNTER EPISODE AS ONE DELIVERY UNIT (live 2026-10-03, acceptance turn 01M42CBG0K...).  Measured defect: with
# the episode's companions trimmed as independent units, Pass 2 kept the encounter's metadata record and the human
# request and dropped the Clark reply -- the substantive turn -- because it was the largest and ranked after them.
# Rule (trimming only; ranking unchanged): an encounter record and its turn companions form one episode.  Its
# core -- the record, the request and the opening of Clark's reply -- is dropped or kept whole; the rest of the
# reply rides in further parts placed directly behind the core, so pressure truncates the reply from its end
# (content_truncated: true) before it can remove the episode, and never leaves the record without the reply.
EPISODE_PROSE_PART_CHARS = 300


@dataclasses.dataclass(frozen=True)
class DeliveryUnit:
    """One droppable unit: ``items`` carried whole, plus at most one part (``part_index``) of an episode's Clark
    reply ``prose_item``.  ``event_id`` is the unit's source id for composition."""
    items: tuple
    prose_item: Optional[RetrievedMemory] = None
    part_index: int = 0
    part_bounds: tuple = ()
    rank: int = 0

    @property
    def event_id(self):
        return self.items[0].event_id if self.items else self.prose_item.event_id


def _prose_part_bounds(content: str) -> list:
    """Deterministic [start, end) parts of a reply, each ending at a whitespace boundary where one exists."""
    bounds, start = [], 0
    while start < len(content):
        end = min(len(content), start + EPISODE_PROSE_PART_CHARS)
        if end < len(content):
            # Prefer the last sentence or line end in the part's second half, then the last space.
            half = start + EPISODE_PROSE_PART_CHARS // 2
            cut = max(content.rfind(". ", half, end), content.rfind("\n", half, end))
            if cut > start:
                end = cut + 1
            else:
                cut = content.rfind(" ", start + 1, end)
                end = cut + 1 if cut > start else end
        bounds.append((start, end))
        start = end
    return bounds or [(0, 0)]


def delivery_units(items) -> list:
    """Best-first items -> WORST-first droppable units (the compositor drops from the front).

    EPISODE SPACE (live 2026-10-03, acceptance turn 01M42EF2S6...): a selected encounter episode ranked third
    behind two standalone retrieved messages lost all its content, because those messages filled the soft-history
    room first.  Trim order within retrieved history is therefore: standalone items (worst first), then episode
    reply tails (worst episode first, each from its end), then episode cores.  This decides only what is dropped
    under pressure -- selection, ranking and the delivered (rank) order are unchanged -- so a selected episode keeps
    a coherent, substantive representation while room for it exists.  Exact-echo items (SELECTION_EXACT_ECHO) are
    never delivered."""
    items = [item for item in items if item.selection_basis != SELECTION_EXACT_ECHO]
    standalone, tails, cores = [], [], []
    i = 0
    while i < len(items):
        item = items[i]
        companions = []
        if item.component_kind == ENCOUNTER_RECORD_COMPONENT_KIND:
            j = i + 1
            while j < len(items) and items[j].companion_of_event_id == item.event_id:
                companions.append(items[j])
                j += 1
        if not companions:
            standalone.append(DeliveryUnit(items=(item,), rank=i))
            i += 1
            continue
        prose = next((c for c in companions if c.memory_kind == "clark_expression"), None)
        others = tuple(c for c in companions if c is not prose)
        if prose is None:
            cores.append(DeliveryUnit(items=(item,) + others, rank=i))
        else:
            bounds = tuple(_prose_part_bounds(prose.content))
            cores.append(DeliveryUnit(items=(item,) + others, prose_item=prose, part_index=0, part_bounds=bounds,
                                      rank=i))
            tails.append([DeliveryUnit(items=(), prose_item=prose, part_index=n, part_bounds=bounds, rank=i)
                          for n in range(1, len(bounds))])
        i += 1 + len(companions)
    worst_first = list(reversed(standalone))
    for episode_tail in reversed(tails):              # worst episode's tail first, each from its end
        worst_first.extend(reversed(episode_tail))
    worst_first.extend(reversed(cores))
    return worst_first


def delivered_items(units) -> list:
    """Surviving units (any order) -> the items they deliver, in retrieval (rank) order.  An episode reply carries
    exactly the contiguous opening its surviving parts cover; anything less than the retrieved reply is
    content_truncated."""
    units = list(units)
    if not units or not isinstance(units[0], DeliveryUnit):
        return list(reversed(units))
    present = {}
    for unit in units:
        if unit.prose_item is not None:
            present.setdefault(unit.prose_item.item_id, set()).add(unit.part_index)
    out = []
    for unit in sorted((u for u in units if u.items), key=lambda u: u.rank):
        out.extend(unit.items)
        if unit.prose_item is not None and unit.part_index == 0:
            prose, kept = unit.prose_item, 0
            while kept in present.get(prose.item_id, ()):
                kept += 1
            end = unit.part_bounds[kept - 1][1]
            if end >= len(prose.content):
                out.append(prose)
            else:
                out.append(dataclasses.replace(prose, content=prose.content[:end], content_truncated=True))
    return out


# =============================================================================
# Deterministic HIPPOCAMPAL_CONTEXT_V1 renderer.
# =============================================================================
# EVIDENCE CLASS LEGIBILITY (2026-10-03): a short, literal record type on every rendered item, derived only from
# its provenance class -- never what it means.
def record_label(item) -> str:
    if item.memory_kind == "clark_expression":
        return "canonical historical utterance"
    if item.memory_kind == "human_expression":
        return ("legacy-log historical human utterance" if item.source_store == "anaxi_log_jsonl"
                else "canonical historical human utterance")
    if item.memory_kind == "mechanical_record":
        return {ENCOUNTER_RECORD_COMPONENT_KIND: "host resource-encounter record",
                "roaming_action_fact": "host roaming action record",
                "roaming_external_result": "host roaming external-result record"}.get(
                    item.component_kind, "host mechanical record")
    if item.memory_kind == "mixed_historical_expression":
        return "historical reply record (authorship not resolved)"
    if item.memory_kind == "derived_inference":
        return "sleep-derived tentative association"
    return "retrieved record"


# PERSPECTIVE-INVARIANT ACTOR RELATION (2026-10-03): whose record this is, stated as canonical actor EQUALITY with
# the actor receiving this context -- never a name, never a pronoun (a second-person label for the waking subject's
# own prior reply was read live as the human's).  Only mechanically established relations are stated.
RELATION_LEGEND = ("actor_relation: SAME_AS_RECEIVING_ACTOR = authored by the same canonical actor receiving this "
                   "context now; HUMAN_INTERLOCUTOR = a human who addressed that actor; HOST = the ANAXI host "
                   "(canonical actor identity only)")


def actor_relation(item) -> Optional[str]:
    """The canonical actor-equality relation for one retrieved item, or None.

    Delegated to ``epistemic_status.actor_relation_of`` -- the ONE shared contract --
    so this module cannot fork the vocabulary.  None means the host did not establish
    a relation; renderers state that as NOT_ESTABLISHED rather than omitting it.
    """
    relation = epistemic_status.actor_relation_of(item.creator_actor_id, item.memory_kind)
    return None if relation == epistemic_status.NOT_ESTABLISHED else relation


# COMPACT HISTORICAL EPISODE (2026-10-03).  Measured on the Lacrimosa acceptance turn: an encounter record and its
# two turn companions rendered as three independent JSON lines -- 1,101 + 637 + 1,825 characters for 575 + 133 +
# 1,200 characters of record and utterance -- so per-line wrappers, repeated provenance and the record's
# JSON-in-JSON escaping consumed about half of the episode's room.  A selected episode now renders as ONE line:
# its turn identity once; the encounter's mechanical facts as fields (only the delivered-representation digest,
# which the manifest and canonical record keep, is omitted); the exact human and Clark utterances with their
# provenance, the reply's exact delivered range and truncation.  Nothing is summarized; meaning is not stated.
EPISODE_RECORD_LABEL = "historical episode: a host resource-encounter record and the canonical turn it belongs to"
_ENCOUNTER_FIELDS = ("modality", "resource_class", "relative_path", "source_folder", "source_action",
                     "delivered_portion", "genuinely_delivered", "target_model_pathway", "source_content_sha256")


def _delivered_extent(item):
    total = item.source_chars if item.source_chars is not None else len(item.content)
    return f"characters 0-{len(item.content)} of {total}"


def _utterance_payload(item, person_annotations):
    payload = {"record": record_label(item), "event_id": item.event_id,
               "actor_relation": actor_relation(item) or epistemic_status.NOT_ESTABLISHED}
    payload.update(attribution_status=item.attribution_status, authentication_status=item.authentication_status)
    if item.creator_actor_id is not None:
        payload["creator_actor_id"] = item.creator_actor_id
    if person_annotations and item.item_id in person_annotations:
        payload["person_provenance"] = person_annotations[item.item_id]
    payload["content_truncated"] = item.content_truncated
    if item.content_truncated:
        payload["delivered"] = _delivered_extent(item)
    payload["text"] = item.content
    return payload


def _episode_payload(record, companions, person_annotations):
    try:
        fact = json.loads(record.content)
        encounter = {k: fact[k] for k in _ENCOUNTER_FIELDS if k in fact}
    except (TypeError, ValueError):
        fact, encounter = None, {"content": record.content}
    encounter = dict({"record": record_label(record), "event_id": record.event_id,
                      "actor_relation": actor_relation(record) or epistemic_status.NOT_ESTABLISHED},
                     **encounter)
    turn = companions[0]
    episode = {"record": EPISODE_RECORD_LABEL, "turn_event_id": turn.event_id, "occurred_at": turn.occurred_at}
    if turn.session_resolution == "resolved":
        episode["session_id"] = turn.session_id
    if record.selection_basis != SELECTION_RELEVANCE:
        episode["selection_basis"] = record.selection_basis
    episode["encounter"] = encounter
    for kind, key in (("human_expression", "human_utterance"), ("clark_expression", "receiving_actor_utterance")):
        for item in companions:
            if item.memory_kind == kind:
                episode[key] = _utterance_payload(item, person_annotations)
    return {"historical_episode": episode}


def _rendered_groups(items):
    """[(items, is_episode)] in order: an encounter record directly followed by its turn companions is one group."""
    items = list(items)
    groups, i = [], 0
    while i < len(items):
        item = items[i]
        j = i + 1
        if item.component_kind == ENCOUNTER_RECORD_COMPONENT_KIND:
            while j < len(items) and items[j].companion_of_event_id == item.event_id:
                j += 1
        groups.append((tuple(items[i:j]), j > i + 1))
        i = j
    return groups


def rendered_lines_by_item(result: RetrievalResult, person_annotations: Optional[dict] = None) -> dict:
    """{item_id: the exact rendered line that carries it} -- every item of an episode maps to its one episode line."""
    lines = render_hippocampal_context(result, person_annotations).splitlines()
    payload_lines = [l for l in lines if l.startswith("{")]
    out = {}
    shown = [item for item in result.items if item.selection_basis != SELECTION_EXACT_ECHO]
    for (group, _episode), line in zip(_rendered_groups(shown), payload_lines):
        for item in group:
            out[item.item_id] = line
    return out


def render_hippocampal_context(result: RetrievalResult, person_annotations: Optional[dict] = None) -> str:
    """Deterministic. Begins with the literal heading, then the fixed
    epistemic-status explanatory statement, then one JSON object per
    line for each retrieved item. Retrieved content is always placed
    as ordinary JSON string data (via json.dumps' own escaping) -- it
    can never inject a fake heading, brace-delimited structure, or
    prompt-like instruction into the rendered block, regardless of
    what bytes it contains."""
    lines = [CONTEXT_HEADING, EPISTEMIC_STATEMENT]
    if person_annotations and any(item.item_id in person_annotations for item in result.items):
        lines.append(PERSON_PROVENANCE_STATEMENT)
    if any(actor_relation(item) == "SAME_AS_RECEIVING_ACTOR" for item in result.items
           if item.selection_basis != SELECTION_EXACT_ECHO):
        lines.append(RELATION_LEGEND)
    if not any(item.selection_basis != SELECTION_EXACT_ECHO for item in result.items):
        lines.append(EMPTY_RESULT_MARKER)
        return "\n".join(lines)
    for group, is_episode in _rendered_groups(item for item in result.items
                                              if item.selection_basis != SELECTION_EXACT_ECHO):
        if is_episode:
            # Insertion order (deterministic): identity, encounter, the request, then the reply -- reading order.
            lines.append(json.dumps(_episode_payload(group[0], group[1:], person_annotations), ensure_ascii=False))
            continue
        item = group[0]
        payload = {
            "record": record_label(item),
            "event_id": item.event_id,
            "occurred_at": item.occurred_at,
            "memory_kind": item.memory_kind,
            "attribution_status": item.attribution_status,
            "authentication_status": item.authentication_status,
            "source_store": item.source_store,
            "source_locator": item.source_locator,
            # WSP2-P4-H1: component_kind is the one field that
            # distinguishes an ordinary conversational item
            # ("bounded_clause"/"conversational_prose") from a public
            # workspace/roaming item ("resource_encounter_fact"/
            # "roaming_action_fact"/"roaming_external_result"/
            # "roaming_journal_text") -- surfaced explicitly so the
            # source/pathway distinction stays visible even though
            # retrieval itself no longer discriminates between them
            # (PATHWAY-FIRST ATTRIBUTION: no accessibility wall, but no
            # erased provenance either). None for the two non-
            # event_components ingestion paths, which never had a
            # component_kind to begin with -- never guessed.
            "component_kind": item.component_kind,
            "retrieval_method": item.retrieval_method,
            "bm25_score": item.bm25_score,
            "content": item.content,
            "content_truncated": item.content_truncated,
        }
        # CONTENT provenance: the relation is STATED even when unestablished, so a
        # reader cannot read an omitted field as a positive relation.  The value is
        # None only when the host did not establish one, and that is what is said.
        payload["actor_relation"] = (actor_relation(item)
                                     or epistemic_status.NOT_ESTABLISHED)
        if item.component_kind == "roaming_external_result":
            # The canonical component/envelope is host-authored, but its embedded library,
            # note, or other workspace result is not thereby host-authored or endorsed.
            payload["content_relation"] = epistemic_status.NOT_ESTABLISHED
            payload["content_authority"] = "source; not host truth"
        if item.selection_basis != SELECTION_RELEVANCE:
            payload["selection_basis"] = item.selection_basis
        if item.companion_of_event_id is not None:
            # The encounter record (event id) whose triggering turn this record is -- a canonical link, not a match.
            payload["companion_of_event_id"] = item.companion_of_event_id
        if item.repeat_count > 1:
            # A host count over real records with this exact text and provenance class -- this item is
            # the most recent of them; nothing was merged or rewritten.
            payload["same_text_recorded_times"] = item.repeat_count
            payload["first_recorded_at"] = item.first_occurred_at
        if item.creator_actor_id is not None:
            payload["creator_actor_id"] = item.creator_actor_id
            payload["creator_actor_type"] = item.creator_actor_type
        if item.session_resolution == "resolved":
            payload["session_id"] = item.session_id
        if person_annotations and item.item_id in person_annotations:
            payload["person_provenance"] = person_annotations[item.item_id]
        if item.source_store == SLEEP_DERIVATION_SOURCE_STORE:
            lines.append(SLEEP_DERIVED_LABEL)
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)
