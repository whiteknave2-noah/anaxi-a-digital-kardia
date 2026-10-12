"""SUBJECT-DIRECTED CONSOLIDATION V0.

The continuing subject determines what from its own experience receives
stronger continuity. ANAXI supplies a truthful past and a lawful
consolidation mechanism; it does not determine meaning or importance on
the subject's behalf.

WHAT THIS MODULE IS NOT. It is not a memory system. Canonical history
stays authoritative and untouched. A consolidation record is a
subject-authored record ABOUT canonical history: it points mechanically
at an exact canonical event, carries the subject's own words, and can be
revised by appending -- never by overwriting. Nothing here scores,
ranks, or interprets the subject's choice, and nothing here converts
subject testimony into an objective claim.

THE FIVE ACTS (closed vocabulary, from conversation_direction.py):

  select        -- the subject names a canonical episode that should
                   receive stronger continuity. Creates a record.
  interpret     -- the subject records its own reading of a canonical
                   episode. Creates a record carrying his exact words.
  revise        -- the subject appends a later reading to an earlier
                   record's lineage. Creates a NEW record whose
                   prior_record_id names the earlier one; the earlier
                   record is never modified.
  de_emphasize  -- the subject ceases giving a record preferential
                   accessibility. Appends a new act naming the earlier
                   record. Deletes or overwrites nothing, and the fact
                   that it was once selected remains.
  none          -- no act. A Sleep/REM occasion is not required to
                   produce one.

RECOVERY/IDEMPOTENCE. Every act is keyed by the canonical waking event
that carried it (occasion_event_id). A retry of the same occasion
returns the already-committed record rather than creating a second one;
if the commit never happened, nothing is claimed. This mirrors
sleep_timing_knock.py's own single-unresolved-request discipline.
"""
import hashlib
import os
import sqlite3
import time

import consolidation_schema
import conversation_direction
import family_membership
import native_provenance_writer

OCCASION_WAKING = "waking"
OCCASION_SLEEP = "sleep"

# Every non-null act creates a new append-only record row.
RECORD_CREATING_REQUESTS = frozenset({
    conversation_direction.CONSOLIDATION_REQUEST_SELECT,
    conversation_direction.CONSOLIDATION_REQUEST_INTERPRET,
    conversation_direction.CONSOLIDATION_REQUEST_REVISE,
    conversation_direction.CONSOLIDATION_REQUEST_DE_EMPHASIZE,
})


class ConsolidationError(Exception):
    """A consolidation act could not be lawfully applied."""


class ConsolidationTargetNotFound(ConsolidationError):
    """The named canonical event does not exist in canonical history."""


class ConsolidationPriorNotFound(ConsolidationError):
    """The named prior record does not exist, or is not the subject's own."""


class ConsolidationScopeError(ConsolidationError):
    """The act would widen scope, which is never permitted."""


class ConsolidationTargetNotOffered(ConsolidationError):
    """The waking act named history that was not offered on that turn."""


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now():
    return int(time.time())


def _data_dir():
    import llama_anaxi
    return getattr(llama_anaxi, "PROVENANCE_DB_DIR", None)


def _db_path(data_dir):
    return os.path.join(data_dir, "anaxi_provenance.db")


def open_connection(data_dir):
    """Open the anchored provenance DB for a consolidation act, applying
    the additive migration first. Mirrors the data_dir convention every
    other canonical writer in this project uses (operative_directive,
    sleep_timing_knock): the caller passes the directory, never a path
    or an open connection."""
    conn = _connect(_db_path(data_dir))
    consolidation_schema.ensure_schema(conn)
    return conn


def apply_consolidation_act_in(data_dir, **kwargs):
    """apply_consolidation_act() against a data_dir, opening and closing
    the anchored provenance DB. The waking-path entry point."""
    conn = open_connection(data_dir)
    try:
        return apply_consolidation_act(conn, **kwargs)
    finally:
        conn.close()


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def schema_present(conn):
    required = {"consolidation_records", "consolidation_cues", "consolidation_scope"}
    present = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)",
            tuple(sorted(required)),
        ).fetchall()
    }
    return present == required


def _canonical_event_exists(conn, event_id):
    row = conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone()
    return row is not None


def _canonical_event_scope(conn, event_id):
    """The target's own canonical scope, or None for legacy/unscoped.
    Read-only; never guessed broader than what canonical history says."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    if "visibility_scope" not in cols:
        return None
    row = conn.execute(
        "SELECT visibility_scope FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row is None:
        return None
    scope = row[0]
    return scope if scope in family_membership.VALID_SCOPES else None


def _resolve_scope(conn, target_event_id, requested_scope):
    """A consolidation record may INHERIT its target's scope or NARROW
    it. It may never widen it. Returns (scope_kind, narrowed_bool).

    Narrowing means: the target is family_shared and the record is
    principal_private. Widening (target private, record shared) is
    refused outright. A legacy/unscoped target inherits None, and a
    request to make it shared is refused -- an unscoped event is never
    guessed into a broader context."""
    target_scope = _canonical_event_scope(conn, target_event_id)
    if requested_scope is None or requested_scope == target_scope:
        return target_scope, False
    if requested_scope not in family_membership.VALID_SCOPES:
        raise ConsolidationScopeError(f"unknown scope {requested_scope!r}")
    if target_scope is None:
        raise ConsolidationScopeError(
            "an unscoped canonical target cannot be given a broader scope"
        )
    if target_scope == family_membership.SCOPE_FAMILY_SHARED and \
            requested_scope == family_membership.SCOPE_PRINCIPAL_PRIVATE:
        return requested_scope, True
    raise ConsolidationScopeError(
        f"consolidation may not widen scope from {target_scope!r} to {requested_scope!r}"
    )


def _existing_record_for_occasion(conn, occasion_event_id):
    row = conn.execute(
        "SELECT record_id FROM consolidation_records WHERE occasion_event_id = ?",
        (occasion_event_id,),
    ).fetchone()
    return row[0] if row else None


def _record_scope(conn, record_id):
    row = conn.execute(
        "SELECT scope_kind FROM consolidation_scope WHERE record_id = ?", (record_id,)
    ).fetchone()
    if row is None:
        return None
    return row[0] or None


def _load_record(conn, record_id):
    row = conn.execute(
        "SELECT record_id, request, target_event_id, target_component_id, interpretation_text,"
        " prior_record_id, author_actor_id, occasion, occasion_event_id, model_revision_id,"
        " created_at, de_emphasized_at"
        " FROM consolidation_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    if row is None:
        return None
    keys = ("record_id", "request", "target_event_id", "target_component_id", "interpretation_text",
            "prior_record_id", "author_actor_id", "occasion", "occasion_event_id", "model_revision_id",
            "created_at", "de_emphasized_at")
    return dict(zip(keys, row))


def apply_consolidation_act(
    conn, *, request, target_event_id, author_actor_id,
    interpretation_text=None, cues=None, prior_record_id=None,
    occasion=OCCASION_WAKING, occasion_event_id=None, model_revision_id=None,
    requested_scope=None, now=None, allowed_target_event_ids=None, commit=True,
):
    """Apply one subject-directed consolidation act. Returns the record
    dict (existing or newly created), or None for request="none".

    Idempotent per occasion_event_id: a retry of the same occasion
    returns the already-committed record and creates nothing new.
    """
    if request == conversation_direction.CONSOLIDATION_REQUEST_NONE:
        if commit:
            consolidation_schema.ensure_schema(conn)
        return None
    if request not in conversation_direction.VALID_CONSOLIDATION_REQUESTS:
        raise ConsolidationError(f"unknown consolidation request {request!r}")
    if not isinstance(author_actor_id, str) or not author_actor_id:
        raise ConsolidationError("a consolidation act requires an author actor id")

    # ``ensure_schema`` uses executescript(), which implicitly commits an
    # open SQLite transaction. Atomic callers (Sleep's fenced final commit)
    # establish the schema before BEGIN and pass commit=False here.
    if commit:
        consolidation_schema.ensure_schema(conn)
    now = _now() if now is None else now
    if occasion_event_id is not None:
        existing = _existing_record_for_occasion(conn, occasion_event_id)
        if existing is not None:
            record = _load_record(conn, existing)
            same = (
                record["request"] == request
                and record["target_event_id"] == target_event_id
                and record["author_actor_id"] == author_actor_id
                and record["prior_record_id"] == prior_record_id
                and record["occasion"] == occasion
                and record["model_revision_id"] == model_revision_id
                and record["interpretation_text"] == (interpretation_text or None)
                and cues_for_record(conn, existing) == list(cues or ())
                and (requested_scope is None or _record_scope(conn, existing) == requested_scope)
            )
            if not same:
                raise ConsolidationError(
                    "occasion id was already committed with a different consolidation act"
                )
            return record

    if (allowed_target_event_ids is not None
            and target_event_id not in frozenset(allowed_target_event_ids)):
        raise ConsolidationTargetNotOffered(
            f"canonical target {target_event_id!r} was not offered on this occasion"
        )

    if not _canonical_event_exists(conn, target_event_id):
        raise ConsolidationTargetNotFound(
            f"no canonical event {target_event_id!r}"
        )

    prior = None
    if request in (
        conversation_direction.CONSOLIDATION_REQUEST_REVISE,
        conversation_direction.CONSOLIDATION_REQUEST_DE_EMPHASIZE,
    ):
        if prior_record_id is None:
            raise ConsolidationError(f"{request} requires a prior record id")
        prior = _load_record(conn, prior_record_id)
        if prior is None or prior["author_actor_id"] != author_actor_id:
            raise ConsolidationPriorNotFound(
                f"no subject-authored record {prior_record_id!r}"
            )
        if prior["target_event_id"] != target_event_id:
            raise ConsolidationError(
                "a revision or de-emphasis must name the prior record's canonical target"
            )

    scope_kind, narrowed = _resolve_scope(conn, target_event_id, requested_scope)
    if prior is not None:
        prior_scope = _record_scope(conn, prior_record_id)
        # A later act inherits the earlier subject record's already-lawful
        # scope. It may narrow again, but can never silently regain the
        # target event's broader scope.
        if requested_scope is None:
            scope_kind = prior_scope
            narrowed = prior_scope != _canonical_event_scope(conn, target_event_id)
        elif prior_scope == family_membership.SCOPE_PRINCIPAL_PRIVATE \
                and scope_kind != family_membership.SCOPE_PRINCIPAL_PRIVATE:
            raise ConsolidationScopeError("a later act may not widen its prior record's scope")

    record_id = native_provenance_writer.generate_native_ulid()
    interpretation_text = interpretation_text or None
    conn.execute(
        "INSERT INTO consolidation_records (record_id, request, target_event_id, target_component_id,"
        " interpretation_text, interpretation_sha256, prior_record_id, author_actor_id, occasion,"
        " occasion_event_id, model_revision_id, created_at, de_emphasized_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            record_id, request, target_event_id, None,
            interpretation_text,
            _sha256(interpretation_text) if interpretation_text else None,
            prior_record_id, author_actor_id, occasion, occasion_event_id, model_revision_id, now,
            now if request == conversation_direction.CONSOLIDATION_REQUEST_DE_EMPHASIZE else None,
        ),
    )
    for index, cue in enumerate(cues or []):
        conn.execute(
            "INSERT INTO consolidation_cues (record_id, cue_index, cue_text, cue_sha256)"
            " VALUES (?,?,?,?)",
            (record_id, index, cue, _sha256(cue)),
        )
    conn.execute(
        "INSERT INTO consolidation_scope (record_id, scope_kind, scope_ref, inherited_from_event_id,"
        " narrowed) VALUES (?,?,?,?,?)",
        (record_id, scope_kind or "", None, target_event_id, 1 if narrowed else 0),
    )
    if commit:
        conn.commit()
    return _load_record(conn, record_id)


def apply_from_direction(conn, direction, *, author_actor_id, occasion=OCCASION_WAKING,
                         occasion_event_id=None, model_revision_id=None, now=None):
    """Apply the consolidation fields carried by a validated Pass-1
    direction dict (conversation_direction.validate_pass1_conversation_act's
    return value). Returns the record dict or None. A direction with
    request="none" is a no-op."""
    request = direction.get("consolidation_request",
                            conversation_direction.CONSOLIDATION_REQUEST_NONE)
    if request == conversation_direction.CONSOLIDATION_REQUEST_NONE:
        return None
    return apply_consolidation_act(
        conn,
        request=request,
        target_event_id=direction.get("consolidation_target"),
        author_actor_id=author_actor_id,
        interpretation_text=direction.get("consolidation_interpretation") or None,
        cues=direction.get("consolidation_cues") or [],
        prior_record_id=direction.get("consolidation_prior_record_id"),
        occasion=occasion,
        occasion_event_id=occasion_event_id,
        model_revision_id=model_revision_id,
        now=now,
    )


def records_for_target(conn, target_event_id):
    """Read-only. Every consolidation record naming this canonical
    episode, oldest first. The canonical episode itself is untouched."""
    consolidation_schema.ensure_schema(conn)
    rows = conn.execute(
        "SELECT record_id FROM consolidation_records WHERE target_event_id = ?"
        " ORDER BY rowid",
        (target_event_id,),
    ).fetchall()
    return [_load_record(conn, r[0]) for r in rows]


def lineage_for_record(conn, record_id):
    """Read-only. The full revision lineage ending at this record,
    oldest first: canonical target -> earliest interpretation -> ...
    -> this record. Revision appends; nothing is replaced."""
    consolidation_schema.ensure_schema(conn)
    chain = []
    seen = set()
    current = record_id
    while current is not None and current not in seen:
        seen.add(current)
        record = _load_record(conn, current)
        if record is None:
            break
        chain.append(record)
        current = record["prior_record_id"]
    chain.reverse()
    return chain


def cues_for_record(conn, record_id):
    """Read-only. The subject-authored retrieval cues on a record."""
    consolidation_schema.ensure_schema(conn)
    rows = conn.execute(
        "SELECT cue_text FROM consolidation_cues WHERE record_id = ? ORDER BY cue_index",
        (record_id,),
    ).fetchall()
    return [r[0] for r in rows]


def is_de_emphasized(conn, record_id):
    """Read-only. Whether the subject has ceased giving this record
    preferential accessibility. The record and its target both remain."""
    consolidation_schema.ensure_schema(conn)
    row = conn.execute(
        "SELECT de_emphasized_at FROM consolidation_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    if row and row[0] is not None:  # compatibility with the pre-review writer
        return True
    return conn.execute(
        "SELECT 1 FROM consolidation_records WHERE request = ? AND prior_record_id = ? LIMIT 1",
        (conversation_direction.CONSOLIDATION_REQUEST_DE_EMPHASIZE, record_id),
    ).fetchone() is not None


def describe_for_subject(conn, record_id):
    """Read-only. The authoritative evidence by which the subject can
    later determine that it performed a consolidation act: what it
    asked for, which canonical episode it named, its own words, and
    when. Never a host interpretation of the act."""
    record = _load_record(conn, record_id)
    if record is None:
        return None
    return {
        "record_id": record["record_id"],
        "request": record["request"],
        "target_event_id": record["target_event_id"],
        "interpretation_text": record["interpretation_text"],
        "cues": cues_for_record(conn, record_id),
        "prior_record_id": record["prior_record_id"],
        "created_at": record["created_at"],
        "de_emphasized_at": record["de_emphasized_at"],
        "occasion": record["occasion"],
        "occasion_event_id": record["occasion_event_id"],
        "scope": _record_scope(conn, record_id),
    }


# =============================================================================
# RETRIEVAL INTEGRATION
# =============================================================================
# Subject-authored cues AUGMENT retrieval. They never replace the episode's
# own canonical language, which remains an independent retrieval surface:
# the ordinary hippocampal path (exact canonical episode text, FTS5/BM25)
# is untouched by anything here, and a cue can only ever ADD a target to a
# result set that the ordinary path would otherwise have missed. There is
# no scoring, no ranking, and no weighting -- a cue match is a mechanical
# membership test, exactly like the ordinary path's own lexical match.

def cue_index(conn):
    """Read-only. Every subject-authored cue, as
    (cue_text, record_id, target_event_id), oldest record first. The
    caller decides how to use it; this module never ranks.

    Ordering is by the record's own insertion order (rowid), which is
    stable and deterministic: created_at has one-second resolution, so
    two acts in the same second would otherwise be ordered by their
    random ULID."""
    if not schema_present(conn):
        return []
    rows = conn.execute(
        "SELECT c.cue_text, c.record_id, r.target_event_id"
        " FROM consolidation_cues c"
        " JOIN consolidation_records r ON r.record_id = c.record_id"
        " ORDER BY r.rowid, c.cue_index",
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def targets_matching_cues(conn, query_text):
    """Read-only. The canonical event ids whose subject-authored cues
    lexically match this query, in deterministic order. A mechanical
    membership test over the subject's own words -- never a score, never
    a ranking, never a judgement about what the subject cares about.

    De-emphasized records are excluded: the subject has ceased giving
    them preferential accessibility. The records and their targets both
    remain in canonical history and remain reachable by the ordinary
    episode-native path."""
    if not schema_present(conn):
        return []
    import hippocampus_retrieval
    terms = hippocampus_retrieval.normalize_query(query_text)
    if not terms:
        return []
    wanted = set(terms)
    matched = []
    seen = set()
    for cue_text, record_id, target_event_id in cue_index(conn):
        if target_event_id in seen:
            continue
        if is_de_emphasized(conn, record_id):
            continue
        cue_terms = set(hippocampus_retrieval.normalize_query(cue_text))
        if cue_terms & wanted:
            seen.add(target_event_id)
            matched.append(target_event_id)
    return matched


def augment_retrieval(conn, result, query_text, *, hippocampus_db_path=None,
                      provenance_db_path=None):
    """Read-only. Return a NEW RetrievalResult that additionally carries
    the canonical episodes the subject's own cues point at, for the
    episodes the ordinary path did not already return.

    The ordinary items are preserved exactly, in their original order,
    with their original selection_basis. Cue-matched additions are
    appended after them and are marked with their own basis, so a reader
    can always tell which surface produced which item. Nothing is
    removed, reordered, or re-scored."""
    import hippocampus_retrieval
    already = {item.event_id for item in result.items}
    additions = []
    for event_id in targets_matching_cues(conn, query_text):
        if event_id in already:
            continue
        already.add(event_id)
        additions.append(event_id)
    if not additions or not hippocampus_db_path:
        return result
    indexed = hippocampus_retrieval.retrieve_canonical_events(
        hippocampus_db_path, additions, query_terms=result.query_terms,
        provenance_db_path=provenance_db_path,
    )
    if not indexed:
        return result
    import dataclasses
    return dataclasses.replace(result, items=result.items + tuple(indexed))


def evidence_for_targets(conn, target_event_ids, *, author_actor_id):
    """Authoritative subject-visible evidence for the exact target handles
    offered on this occasion. No target outside that already-authorized set is
    described, so this projection cannot widen history visibility."""
    consolidation_schema.ensure_schema(conn)
    targets = list(dict.fromkeys(target_event_ids or ()))
    if not targets:
        return []
    marks = ",".join("?" for _ in targets)
    rows = conn.execute(
        "SELECT record_id FROM consolidation_records WHERE author_actor_id = ? "
        f"AND target_event_id IN ({marks}) ORDER BY rowid",
        (author_actor_id, *targets),
    ).fetchall()
    return [describe_for_subject(conn, row[0]) for row in rows]
