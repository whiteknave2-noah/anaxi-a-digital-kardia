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
    "session_id", "session_resolution", "component_kind", "content_text",
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
        "SELECT " + columns + ", hi.content_sha256, bm25(hippocampal_items_fts) AS score "
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


def retrieve_hippocampal_context(hippocampus_db_path: str, query_text: str) -> RetrievalResult:
    """Requires an existing, schema-valid hippocampal DB; opens it
    read-only; normalizes the query; returns structured results.
    Performs no model call and no source sync -- sync -> retrieve ->
    render is composed by the future Slice-C host seam, not here."""
    terms = normalize_query(query_text)
    conn = _open_hippocampus_readonly(hippocampus_db_path)
    try:
        fts_query = build_fts_query(terms)
        if fts_query is None:
            return RetrievalResult(query_terms=tuple(terms), items=())

        relevance = _fetch_candidates(conn, fts_query, limit=CANDIDATE_LIMIT)
        recency = _fetch_candidates(conn, fts_query, limit=RECENCY_RESERVED_SLOTS + RESULT_LIMIT, order="recency")
        merged = _merge_relevance_and_recency(relevance, recency, RESULT_LIMIT, RECENCY_RESERVED_SLOTS)
        basis = {row["item_id"]: b for row, b in merged}
        bounded = _select_bounded_items([row for row, _b in merged], RESULT_LIMIT, MAX_CHARS_PER_ITEM, MAX_AGGREGATE_CHARS)

        items = []
        for row, content, truncated in bounded:
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
                selection_basis=basis[row["item_id"]], repeat_count=int(row["repeat_count"]),
                first_occurred_at=(row["first_occurred_at"] if int(row["repeat_count"]) > 1 else None),
            ))
        return RetrievalResult(query_terms=tuple(terms), items=tuple(items))
    finally:
        conn.close()


# =============================================================================
# Deterministic HIPPOCAMPAL_CONTEXT_V1 renderer.
# =============================================================================
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
    if not result.items:
        lines.append(EMPTY_RESULT_MARKER)
        return "\n".join(lines)
    for item in result.items:
        payload = {
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
        if item.selection_basis != SELECTION_RELEVANCE:
            payload["selection_basis"] = item.selection_basis
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
