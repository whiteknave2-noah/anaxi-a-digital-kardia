"""Canonical person identity and relational PROVENANCE (never relational meaning).

    IDENTITY IS CANONICAL.  PROVENANCE IS DURABLE.  INTERIORS ARE NOT.  RELATIONSHIP MEANING BELONGS TO CLARK.

Four axes stay mechanically separate:

  1. PERSON IDENTITY     "who is this?"      canonical_persons + owner-bound stable identifiers (this module)
  2. AUTHORITY / SCOPE   "what may they do?"  family_membership / discord_author_mapping (UNCHANGED, untouched)
  3. HISTORICAL PROVENANCE "what happened, who said what, when"   the existing canonical events/components
  4. RELATIONAL MEANING  "what do they mean to Clark?"   NOT HERE.  No score, category, trust, closeness,
                                                          importance, mood or personality field exists.

Nothing in this module grants a capability, a visibility scope, a principal role, a family membership or a
correspondence authorization.  `resolve_*` functions return only identity facts; no authority predicate
anywhere consults this module.

Identity facts
  * a canonical person may be LINKED to an existing registered human actor (Alex, a family principal) --
    the linkage never changes that actor's principal/authority; or stand alone (an external human, or an
    AI / digital correspondent, who have no ANAXI actor and no authority);
  * a stable external identifier (a Discord author id, never a username) is bound to ONE person by an
    owner-only, append-only act; two active persons for one identifier resolve to nothing (ambiguous);
  * a Discord author id that the existing Caret mapping binds to a principal resolves to that principal's
    linked person; if that ever disagrees with an explicit identifier binding the result is ambiguous and
    fails closed.

Utterance framing (records over EXACT canonical utterances; the host never classifies text)
  Every retrieved human utterance is, by default, only a HISTORICAL UTTERANCE: this person said this, then.
  Additional framing exists only where an owner-recorded statement record says so, always quoting the exact
  words, always by the actual speaker, always dated, never as timeless fact about the person:
    self_report         "this person said this about themselves, at that time" -- current applicability unknown
    standing_request    an explicit prospective request/boundary; active only until the SAME speaker revises
                        or withdraws it in a later utterance (history preserved)
    third_party_report  the reporter is the speaker; the referent is named; never the referent's self-report
    clark_reflection    Clark-authored interpretation about a person; stays Clark's, never host biography
    correction          the person's answer to a question Clark asked, preserved as the event it was
"""
import json
import secrets
import sqlite3
import time

import cpi_schema_migration
import discord_author_mapping as dam
import discord_correspondence_registry as dcr
import family_membership

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_LABEL_LENGTH = 200
MAX_QUOTE_LENGTH = 2000
MAX_SCOPE_LENGTH = 500

ENTITY_HUMAN = "human"
ENTITY_AI_DIGITAL = "ai_digital"
ENTITY_KINDS = (ENTITY_HUMAN, ENTITY_AI_DIGITAL)
IDENTIFIER_DISCORD_AUTHOR = "discord_author_id"

IDENTITY_ESTABLISHED = "established"
IDENTITY_UNESTABLISHED = "unestablished"
IDENTITY_REVOKED = "revoked"
IDENTITY_AMBIGUOUS = "ambiguous"

CLASS_SELF_REPORT = "self_report"
CLASS_STANDING_REQUEST = "standing_request"
CLASS_THIRD_PARTY = "third_party_report"
CLASS_CLARK_REFLECTION = "clark_reflection"
CLASS_CORRECTION = "correction"
STATEMENT_CLASSES = (CLASS_SELF_REPORT, CLASS_STANDING_REQUEST, CLASS_THIRD_PARTY,
                     CLASS_CLARK_REFLECTION, CLASS_CORRECTION)

STATE_ACTIVE = "active"
STATE_WITHDRAWN = "withdrawn"
STATE_SUPERSEDED = "superseded"

HUMAN_INPUT_COMPONENT_KIND = "human_conversational_input"

# The one fixed epistemic frame for a retrieved person-attributed utterance.  It states what kind of fact
# this is; it never states anything about the person.
FRAME_TEXT = {
    "historical_utterance": "what this person said then; not established as true of them now",
    CLASS_SELF_REPORT: "this person's self-report at that time; whether it applies now is unknown",
    CLASS_STANDING_REQUEST: "an explicit request the speaker made prospectively; in force only while not later revised or withdrawn by them",
    CLASS_THIRD_PARTY: "what the speaker reported about someone else; not that person's own statement and not established fact",
    CLASS_CLARK_REFLECTION: "Clark's own interpretation when written; not established fact about the person",
    CLASS_CORRECTION: "the person's answer to a question Clark asked; says nothing about them beyond what they stated",
}


class CanonicalPersonError(Exception):
    """A canonical-person act cannot be carried out.  Never caught silently."""


def _new_id(prefix):
    raw = int(time.time() * 1000).to_bytes(6, "big") + secrets.token_bytes(10)
    value = int.from_bytes(raw, "big")
    return prefix + "".join(_CROCKFORD_ALPHABET[(value >> (5 * (25 - i))) & 0x1F] for i in range(26))


def _connect(data_dir):
    conn = sqlite3.connect(f"{data_dir}/anaxi_provenance.db")
    conn.execute("PRAGMA foreign_keys = ON;")
    cpi_schema_migration.apply_migration_on_connection(conn)
    return conn


def _require_owner(conn, requester_actor_id):
    owner = family_membership.resolve_owner_actor_id(conn)
    if owner is None:
        raise CanonicalPersonError("no canonical owner is resolvable; person administration is impossible")
    if requester_actor_id != owner:
        raise CanonicalPersonError("only the canonical owner may create or bind canonical persons")


def _clean_label(label):
    if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_LENGTH:
        raise CanonicalPersonError("a canonical person needs a label of 1-200 characters")
    return label.strip()


def _ledgers_present(conn):
    return cpi_schema_migration.tables_present(conn)


# ------------------------------------------------------------------ read side (identity only)


def person_record(conn, person_id):
    if not _ledgers_present(conn) or not isinstance(person_id, str):
        return None
    row = conn.execute(
        "SELECT person_id, entity_kind, display_label, linked_actor_id, occurred_at "
        "FROM canonical_persons WHERE person_id = ?", (person_id,)).fetchone()
    if row is None:
        return None
    return {"person_id": row[0], "entity_kind": row[1], "display_label": row[2],
            "linked_actor_id": row[3], "established_at": row[4]}


def person_for_actor(conn, actor_id):
    """The canonical person LINKED to an existing actor, or None.  Identity only: this never
    changes, reads or implies that actor's principal role or authority."""
    if not _ledgers_present(conn) or not isinstance(actor_id, str):
        return None
    row = conn.execute("SELECT person_id FROM canonical_persons WHERE linked_actor_id = ?", (actor_id,)).fetchone()
    return person_record(conn, row[0]) if row else None


def _identifier_rows(conn, kind, value):
    if not _ledgers_present(conn):
        return []
    return conn.execute(
        "SELECT binding_event_id, person_id, action, username_snapshot, occurred_at, created_at "
        "FROM canonical_person_identifier_events WHERE identifier_kind = ? AND identifier_value = ? "
        "ORDER BY rowid", (kind, value)).fetchall()


def _reduce_identifier(rows):
    active, ever = {}, False
    for event_id, person_id, action, _u, _o, created_at in rows:
        if action == "bind":
            ever = True
            active[person_id] = (event_id, created_at)
        else:
            active.pop(person_id, None)
    return active, ever


def resolve_identifier(conn, identifier_kind, identifier_value):
    """Fresh, read-only resolution of one stable external identifier by its explicit owner bindings ONLY.
    Never consults a username, message text, style, biography or name similarity."""
    result = {"status": IDENTITY_UNESTABLISHED, "identifier_kind": identifier_kind,
              "identifier_value": identifier_value}
    active, ever = _reduce_identifier(_identifier_rows(conn, identifier_kind, identifier_value))
    if not active:
        result["status"] = IDENTITY_REVOKED if ever else IDENTITY_UNESTABLISHED
        return result
    if len(active) > 1:
        result["status"] = IDENTITY_AMBIGUOUS
        return result
    (person_id, (event_id, recorded_at)), = active.items()
    person = person_record(conn, person_id)
    if person is None:
        result["status"] = IDENTITY_AMBIGUOUS
        return result
    result.update(status=IDENTITY_ESTABLISHED, person_id=person_id, display_label=person["display_label"],
                  entity_kind=person["entity_kind"], binding_event_id=event_id, binding_recorded_at=recorded_at)
    return result


def resolve_discord_author_person(conn, author_id):
    """Who is this Discord author, canonically?  Combines (a) an explicit identifier binding with (b) the
    Caret author->principal mapping's principal, when that principal is linked to a canonical person.  Two
    different persons -> ambiguous (fail closed).  Identity only; the Caret AUTHORIZATION decision stays
    entirely in discord_author_mapping."""
    explicit = resolve_identifier(conn, IDENTIFIER_DISCORD_AUTHOR, author_id)
    if explicit["status"] == IDENTITY_AMBIGUOUS:
        return explicit
    candidates = {}
    if explicit["status"] == IDENTITY_ESTABLISHED:
        candidates[explicit["person_id"]] = ("identifier_binding", explicit["binding_event_id"],
                                             explicit["binding_recorded_at"])
    mapped = dam.resolve_author(conn, author_id)
    if mapped["status"] == dam.STATUS_MAPPED:
        person = person_for_actor(conn, mapped["principal_actor_id"])
        if person is not None:
            candidates.setdefault(person["person_id"], ("principal_mapping", mapped["mapping_event_id"],
                                                        mapped["effective_at"]))
    if len(candidates) > 1:
        return {"status": IDENTITY_AMBIGUOUS, "identifier_kind": IDENTIFIER_DISCORD_AUTHOR,
                "identifier_value": author_id}
    if not candidates:
        return {"status": explicit["status"], "identifier_kind": IDENTIFIER_DISCORD_AUTHOR,
                "identifier_value": author_id}
    (person_id, (basis, event_id, recorded_at)), = candidates.items()
    person = person_record(conn, person_id)
    return {"status": IDENTITY_ESTABLISHED, "identifier_kind": IDENTIFIER_DISCORD_AUTHOR,
            "identifier_value": author_id, "person_id": person_id, "display_label": person["display_label"],
            "entity_kind": person["entity_kind"], "basis": basis, "binding_event_id": event_id,
            "binding_recorded_at": recorded_at}


def identifier_binding_history(conn, identifier_kind, identifier_value):
    """Every bind/revoke of one identifier with its host-recorded time, oldest first."""
    return [{"binding_event_id": r[0], "person_id": r[1], "action": r[2], "recorded_at": r[5]}
            for r in _identifier_rows(conn, identifier_kind, identifier_value)]


def later_person_identity(conn, *, identifier_kind, identifier_value, arrived_at):
    """READ-TIME re-contextualization for one historical event authored by a stable identifier: the identity
    later established for exactly that identifier, dated on its own.  None when no binding was recorded after
    `arrived_at` (or none exists).  Never rewrites the event and never claims the identity was known then."""
    if not isinstance(arrived_at, int):
        return None
    history = identifier_binding_history(conn, identifier_kind, identifier_value)
    later = [h for h in history if h["action"] == "bind" and h["recorded_at"] > arrived_at]
    if identifier_kind == IDENTIFIER_DISCORD_AUTHOR:
        for h in dam.binding_history(conn, identifier_value):
            if h["action"] == dam.ACTION_MAP and h["recorded_at"] > arrived_at:
                person = person_for_actor(conn, h["principal_actor_id"])
                if person is not None:
                    later.append({"binding_event_id": h["mapping_event_id"], "person_id": person["person_id"],
                                  "action": "bind", "recorded_at": h["recorded_at"], "via": "principal_mapping"})
    if not later:
        return None
    binding = sorted(later, key=lambda h: h["recorded_at"])[-1]
    current = resolve_discord_author_person(conn, identifier_value) if identifier_kind == IDENTIFIER_DISCORD_AUTHOR \
        else resolve_identifier(conn, identifier_kind, identifier_value)
    person = person_record(conn, binding["person_id"])
    return {"person_id": binding["person_id"], "display_label": person["display_label"] if person else None,
            "entity_kind": person["entity_kind"] if person else None,
            "binding_event_id": binding["binding_event_id"], "binding_recorded_at": binding["recorded_at"],
            "current_state": current["status"]}


# ------------------------------------------------------------------ owner administration


def create_person(data_dir, *, label, entity_kind, requester_actor_id, occurred_at, linked_actor_id=None):
    """Owner-gated.  Creates a canonical person; grants nothing.  `linked_actor_id`, when given, must be an
    ALREADY-registered human actor not yet linked; an AI/digital correspondent can never be linked to a
    human actor.  The linkage changes no principal, family or owner state."""
    if entity_kind not in ENTITY_KINDS:
        raise CanonicalPersonError("entity_kind must be 'human' or 'ai_digital'")
    if type(occurred_at) is not int:
        raise CanonicalPersonError("occurred_at must be an integer second timestamp")
    label = _clean_label(label)
    conn = _connect(data_dir)
    try:
        _require_owner(conn, requester_actor_id)
        if linked_actor_id is not None:
            if entity_kind != ENTITY_HUMAN:
                raise CanonicalPersonError("only a human person can be linked to a human actor")
            registered = conn.execute(
                "SELECT 1 FROM actors a JOIN actor_human_person h ON h.actor_id = a.actor_id "
                "WHERE a.actor_id = ? AND a.actor_type = 'human_person'", (linked_actor_id,)).fetchone()
            if registered is None:
                raise CanonicalPersonError("linked actor is not an already-registered human actor")
            if person_for_actor(conn, linked_actor_id) is not None:
                raise CanonicalPersonError("that actor is already linked to a canonical person")
        person_id = _new_id("cperson-")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO canonical_persons (person_id, entity_kind, display_label, linked_actor_id, "
                "requester_actor_id, occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (person_id, entity_kind, label, linked_actor_id, requester_actor_id, occurred_at, int(time.time())))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {"person_id": person_id, "entity_kind": entity_kind, "linked_actor_id": linked_actor_id}


def establish_principal_person(data_dir, *, principal_actor_id, requester_actor_id, occurred_at, label=None):
    """Idempotent, owner-gated: give an EXISTING owner/family principal its canonical person.  Nothing about
    that principal's authority, family membership or privacy changes."""
    conn = _connect(data_dir)
    try:
        existing = person_for_actor(conn, principal_actor_id)
        if existing is not None:
            return {"person_id": existing["person_id"], "created": False}
        fallback = dam.principal_display_label(conn, principal_actor_id) or \
            (conn.execute("SELECT display_label FROM actors WHERE actor_id = ?", (principal_actor_id,)).fetchone() or [None])[0]
    finally:
        conn.close()
    created = create_person(data_dir, label=label or fallback or "", entity_kind=ENTITY_HUMAN,
                            requester_actor_id=requester_actor_id, occurred_at=occurred_at,
                            linked_actor_id=principal_actor_id)
    return {"person_id": created["person_id"], "created": True}


def bind_identifier(data_dir, *, person_id, identifier_kind, identifier_value, requester_actor_id, occurred_at,
                    username_snapshot=None):
    """Owner-gated: bind ONE stable external identifier to ONE canonical person.  Refuses an identifier that is
    already actively bound (revoke first) and one whose Caret principal mapping names a DIFFERENT person.
    Grants nothing: it does not authorize the person to contact Clark."""
    if identifier_kind != IDENTIFIER_DISCORD_AUTHOR or not dam.is_valid_author_id(identifier_value):
        raise CanonicalPersonError("identifier must be a Discord author id (a numeric snowflake), never a username")
    if type(occurred_at) is not int:
        raise CanonicalPersonError("occurred_at must be an integer second timestamp")
    conn = _connect(data_dir)
    try:
        _require_owner(conn, requester_actor_id)
        if person_record(conn, person_id) is None:
            raise CanonicalPersonError("unknown canonical person")
        active, _ever = _reduce_identifier(_identifier_rows(conn, identifier_kind, identifier_value))
        if active:
            raise CanonicalPersonError("this identifier is already actively bound; revoke it first")
        mapped = dam.resolve_author(conn, identifier_value)
        if mapped["status"] == dam.STATUS_MAPPED:
            linked = person_for_actor(conn, mapped["principal_actor_id"])
            if linked is not None and linked["person_id"] != person_id:
                raise CanonicalPersonError(
                    "this Discord author id is already mapped to a principal linked to a different person")
        event_id = _new_id("cpbind-")
        snapshot = username_snapshot.strip()[:100] if isinstance(username_snapshot, str) and username_snapshot.strip() else None
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO canonical_person_identifier_events (binding_event_id, person_id, identifier_kind, "
                "identifier_value, action, username_snapshot, requester_actor_id, occurred_at, created_at) "
                "VALUES (?, ?, ?, ?, 'bind', ?, ?, ?, ?)",
                (event_id, person_id, identifier_kind, identifier_value, snapshot, requester_actor_id,
                 occurred_at, int(time.time())))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {"binding_event_id": event_id, "person_id": person_id}


def revoke_identifier(data_dir, *, identifier_kind, identifier_value, requester_actor_id, occurred_at):
    conn = _connect(data_dir)
    try:
        _require_owner(conn, requester_actor_id)
        active, _ever = _reduce_identifier(_identifier_rows(conn, identifier_kind, identifier_value))
        if not active:
            raise CanonicalPersonError("this identifier has no active binding to revoke")
        event_id = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            for person_id in sorted(active):
                bid = _new_id("cpbind-")
                event_id = event_id or bid
                conn.execute(
                    "INSERT INTO canonical_person_identifier_events (binding_event_id, person_id, "
                    "identifier_kind, identifier_value, action, username_snapshot, requester_actor_id, "
                    "occurred_at, created_at) VALUES (?, ?, ?, ?, 'revoke', NULL, ?, ?, ?)",
                    (bid, person_id, identifier_kind, identifier_value, requester_actor_id, occurred_at,
                     int(time.time())))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {"binding_event_id": event_id}


# ------------------------------------------------------------------ statement records


def _component(conn, component_id):
    return conn.execute(
        "SELECT c.component_id, c.event_id, c.creator_actor_id, c.component_kind, c.component_text, "
        "e.event_type, e.occurred_at FROM event_components c JOIN events e ON e.event_id = c.event_id "
        "WHERE c.component_id = ?", (component_id,)).fetchone()


def _speaker_of(conn, comp):
    """The canonical person who authored one component, established ONLY from canonical facts: the
    component's creator actor's link, or (external inbound) the stable Discord author id recorded on the
    inbound event and its owner-bound identity.  Returns (person_id, basis, identifier) or None."""
    _cid, event_id, creator, kind, _text, event_type, _occurred = comp
    if creator is not None:
        actor_type = conn.execute("SELECT actor_type FROM actors WHERE actor_id = ?", (creator,)).fetchone()
        if actor_type and actor_type[0] == "clark_agent":
            return (None, "clark_actor", None)
        person = person_for_actor(conn, creator)
        if person is not None and kind == HUMAN_INPUT_COMPONENT_KIND:
            return (person["person_id"], "actor_link", None)
    if event_type == dcr.DISCORD_INBOUND_MESSAGE_EVENT_TYPE and kind == dcr.DISCORD_INBOUND_CONTENT_COMPONENT_KIND:
        meta = conn.execute(
            "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
            (event_id, dcr.DISCORD_INBOUND_METADATA_COMPONENT_KIND)).fetchone()
        try:
            author_id = json.loads(meta[0]).get("author_id") if meta else None
        except (TypeError, ValueError):
            author_id = None
        if author_id:
            resolved = resolve_discord_author_person(conn, author_id)
            if resolved["status"] == IDENTITY_ESTABLISHED:
                return (resolved["person_id"], "discord_author_binding", author_id)
    return None


def record_statement(data_dir, *, action, statement_class, source_component_id, quoted_text, requester_actor_id,
                     occurred_at, speaker_person_id=None, referent_person_id=None, scope_text=None,
                     prompted_by_event_id=None, references_record_id=None):
    """Owner-recorded framing of ONE exact canonical utterance.

    The host never classifies text: the recorder names the class; the host verifies only mechanical facts --
    the quote is an exact substring of the canonical component, the component's actual author is the named
    speaker (or Clark, for a reflection), the referent exists, a revision/withdrawal comes from the SAME
    speaker in a LATER utterance.  `standing_request` needs an explicit stated scope: prospective force is
    never inferred from a self-report that merely sounds stable."""
    if action not in ("record", "revise", "withdraw") or statement_class not in STATEMENT_CLASSES:
        raise CanonicalPersonError("unknown statement action or class")
    if action != "record" and statement_class != CLASS_STANDING_REQUEST:
        raise CanonicalPersonError(
            "only a standing request can be revised or withdrawn; other statements coexist with later ones")
    if not isinstance(quoted_text, str) or not quoted_text or len(quoted_text) > MAX_QUOTE_LENGTH:
        raise CanonicalPersonError("an exact quote of 1-2000 characters is required")
    if scope_text is not None and (not isinstance(scope_text, str) or len(scope_text) > MAX_SCOPE_LENGTH):
        raise CanonicalPersonError("scope text must be at most 500 characters")
    scope = scope_text.strip() if isinstance(scope_text, str) and scope_text.strip() else None
    if statement_class == CLASS_STANDING_REQUEST and action != "withdraw" and scope is None:
        raise CanonicalPersonError("a standing request must state its scope; prospective force is never inferred")
    if type(occurred_at) is not int:
        raise CanonicalPersonError("occurred_at must be an integer second timestamp")
    conn = _connect(data_dir)
    try:
        _require_owner(conn, requester_actor_id)
        comp = _component(conn, source_component_id)
        if comp is None:
            raise CanonicalPersonError("unknown source component")
        if quoted_text not in comp[4]:
            raise CanonicalPersonError("the quote is not an exact excerpt of the canonical utterance")
        speaker = _speaker_of(conn, comp)
        if speaker is None:
            raise CanonicalPersonError("the utterance's author is not a canonical person")
        actual_person, basis, identifier = speaker
        if statement_class == CLASS_CLARK_REFLECTION:
            if basis != "clark_actor":
                raise CanonicalPersonError("a Clark reflection must quote a Clark-authored component")
            if speaker_person_id is not None:
                raise CanonicalPersonError("a Clark reflection has no person speaker")
        else:
            if basis == "clark_actor":
                raise CanonicalPersonError("a person's statement cannot be recorded from a Clark-authored component")
            if speaker_person_id != actual_person:
                raise CanonicalPersonError("the named speaker did not author that utterance")
        if statement_class in (CLASS_CLARK_REFLECTION, CLASS_THIRD_PARTY):
            if person_record(conn, referent_person_id) is None:
                raise CanonicalPersonError("a referent canonical person is required")
            if statement_class == CLASS_THIRD_PARTY and referent_person_id == actual_person:
                raise CanonicalPersonError("a report about the speaker themself is a self-report, not a third-party report")
        elif referent_person_id is not None:
            raise CanonicalPersonError("only third-party reports and Clark reflections name a referent")
        if statement_class == CLASS_CORRECTION:
            if conn.execute("SELECT 1 FROM events WHERE event_id = ?", (prompted_by_event_id,)).fetchone() is None:
                raise CanonicalPersonError("a correction must name the (existing) event of Clark's question")
        elif prompted_by_event_id is not None:
            raise CanonicalPersonError("only a correction names a prompting event")
        if action != "record":
            prior = conn.execute(
                "SELECT effective_at, speaker_person_id FROM canonical_person_statement_records WHERE record_id = ?",
                (references_record_id,)).fetchone()
            if prior is None:
                raise CanonicalPersonError("a revision must reference an existing standing request")
            if prior[1] != actual_person:
                raise CanonicalPersonError("only the original speaker can revise or withdraw their own request")
            if comp[6] <= prior[0]:
                raise CanonicalPersonError("a revision must come from a LATER utterance than the record it revises")
        elif references_record_id is not None:
            raise CanonicalPersonError("only a revision or withdrawal references a prior record")
        record_id = _new_id("cpstmt-")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO canonical_person_statement_records (record_id, action, statement_class, "
                "speaker_person_id, speaker_basis, speaker_identifier_value, referent_person_id, "
                "source_event_id, source_component_id, quoted_text, scope_text, prompted_by_event_id, "
                "references_record_id, effective_at, recorded_by_actor_id, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record_id, action, statement_class, actual_person, basis, identifier, referent_person_id,
                 comp[1], comp[0], quoted_text, scope, prompted_by_event_id, references_record_id,
                 comp[6], requester_actor_id, int(time.time())))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {"record_id": record_id}


_RECORD_COLUMNS = ("record_id, action, statement_class, speaker_person_id, speaker_basis, speaker_identifier_value, "
                   "referent_person_id, source_event_id, source_component_id, quoted_text, scope_text, "
                   "prompted_by_event_id, references_record_id, effective_at, recorded_at")
_RECORD_KEYS = tuple(c.strip() for c in _RECORD_COLUMNS.split(","))


def _rows_as_dicts(rows):
    return [dict(zip(_RECORD_KEYS, r)) for r in rows]


def _thread_states(conn, speaker_person_id=None):
    """record_id -> (state, thread_root) for every standing-request row.  Within one thread (root plus every
    revision/withdrawal reachable from it) only the LATEST row is current; earlier rows are superseded."""
    rows = _rows_as_dicts(conn.execute(
        f"SELECT {_RECORD_COLUMNS} FROM canonical_person_statement_records "
        "WHERE statement_class = 'standing_request' ORDER BY rowid").fetchall())
    root = {}
    for r in rows:
        root[r["record_id"]] = root.get(r["references_record_id"], r["references_record_id"]) or r["record_id"]
    latest = {}
    for r in rows:
        latest[root[r["record_id"]]] = r
    states = {}
    for r in rows:
        current = latest[root[r["record_id"]]]
        if r["record_id"] == current["record_id"]:
            states[r["record_id"]] = STATE_WITHDRAWN if r["action"] == "withdraw" else STATE_ACTIVE
        else:
            states[r["record_id"]] = STATE_SUPERSEDED
    return states, root


def records_for_component(conn, component_id):
    """The statement records that frame one exact utterance component, each with its current state."""
    if not _ledgers_present(conn):
        return []
    rows = _rows_as_dicts(conn.execute(
        f"SELECT {_RECORD_COLUMNS} FROM canonical_person_statement_records WHERE source_component_id = ? "
        "ORDER BY rowid", (component_id,)).fetchall())
    if not rows:
        return []
    states, _root = _thread_states(conn)
    for r in rows:
        r["state"] = states.get(r["record_id"])
    return rows


def _later_speaker_identity(conn, record):
    """For an utterance whose speaker was established by identifier binding: whether that identity was only
    established AFTER the utterance (so 'unverified then / established later' is preserved)."""
    if record["speaker_basis"] != "discord_author_binding":
        return None
    event = conn.execute("SELECT occurred_at FROM events WHERE event_id = ?", (record["source_event_id"],)).fetchone()
    later = later_person_identity(conn, identifier_kind=IDENTIFIER_DISCORD_AUTHOR,
                                  identifier_value=record["speaker_identifier_value"],
                                  arrived_at=event[0] if event else None)
    return later


# ------------------------------------------------------------------ retrieval-at-use


def _label(conn, person_id):
    person = person_record(conn, person_id)
    return person["display_label"] if person else None


def _component_for_item(conn, item):
    """The canonical component a retrieved item is an ingestion of: the component itself (event_components),
    or, for a native relational human-expression item, the human input component of the turn's canonical H
    event (the turn event's own `human_input_event_id` link).  Anything else -> None (no person framing)."""
    if item.source_store == "event_components" and str(item.source_locator).isdigit():
        return _component(conn, int(item.source_locator))
    if item.source_store != "relational_events":
        return None
    row = conn.execute("SELECT event_type FROM events WHERE event_id = ?", (item.event_id,)).fetchone()
    if row is None:
        return None
    human_event_id = item.event_id
    if row[0] == "waking_turn":
        link = conn.execute(
            "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = 'human_input_event_id'",
            (item.event_id,)).fetchone()
        human_event_id = link[0] if link else None
        if human_event_id is None:
            # A Caret occasion outside a scoped family session has no H event: its words are the carried
            # inbound message, whose author is the stable Discord id on that event.
            carried = conn.execute(
                "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
                (item.event_id, dcr.DISCORD_INBOUND_CARRIAGE_COMPONENT_KIND)).fetchone()
            if carried is None:
                return None
            comp = conn.execute(
                "SELECT component_id FROM event_components WHERE event_id = ? AND component_kind = ?",
                (carried[0], dcr.DISCORD_INBOUND_CONTENT_COMPONENT_KIND)).fetchone()
            return _component(conn, comp[0]) if comp else None
    if human_event_id is None:
        return None
    comp = conn.execute(
        "SELECT component_id FROM event_components WHERE event_id = ? AND component_kind = ? AND sequence = 0",
        (human_event_id, HUMAN_INPUT_COMPONENT_KIND)).fetchone()
    return _component(conn, comp[0]) if comp else None


def annotate_retrieved_items(conn, items):
    """Read-only.  For already-permitted hippocampal `items` (the visibility filter has run), the epistemic
    frame each person-attributed utterance must carry when it re-enters Clark's context.  Items with no
    person facts get NO annotation (ordinary retrieval unchanged).  Keyed by item_id.

    Speaker / class come only from canonical facts and owner-recorded statement records.  Nothing here
    interprets text, infers a trait, or asserts anything about a person's present state."""
    if not items or not _ledgers_present(conn):
        return {}
    out = {}
    for item in items:
        comp = _component_for_item(conn, item)
        if comp is None:
            continue
        records = records_for_component(conn, comp[0])
        speaker = _speaker_of(conn, comp)
        note = {}
        if speaker is not None and speaker[0] is not None:
            note["speaker"] = {"person_id": speaker[0], "label": _label(conn, speaker[0]),
                               "entity_kind": person_record(conn, speaker[0])["entity_kind"]}
            note["frame"] = "historical_utterance"
            note["frame_note"] = FRAME_TEXT["historical_utterance"]
        if records:
            note["records"] = []
            for r in records:
                entry = {"class": r["statement_class"], "effective_at": r["effective_at"],
                         "frame_note": FRAME_TEXT[r["statement_class"]]}
                if r["statement_class"] == CLASS_STANDING_REQUEST:
                    entry["state"] = r["state"]
                    entry["action"] = r["action"]
                    entry["stated_scope"] = r["scope_text"]
                if r["referent_person_id"]:
                    entry["about"] = _label(conn, r["referent_person_id"])
                if r["statement_class"] == CLASS_CORRECTION:
                    entry["in_answer_to_event_id"] = r["prompted_by_event_id"]
                later = _later_speaker_identity(conn, r)
                if later:
                    entry["speaker_identity_established_later"] = {
                        "at": later["binding_recorded_at"], "current_state": later["current_state"]}
                note["records"].append(entry)
        if note:
            out[item.item_id] = note
    return out


def person_history(conn, person_id, *, viewer_principal_actor_id, viewer_scope, active_family_principal_ids=frozenset(),
                   limit=20):
    """Retrieval BY canonical person, framed (never flattened into 'facts about' the person): every entry keeps
    its epistemic class, exact speaker, time and source event.  Subject to the existing FS1 delivery law per
    source event -- identity alone never widens visibility.  Read-only."""
    person = person_record(conn, person_id)
    if person is None:
        return None
    entries = []
    states, _ = _thread_states(conn)
    rows = _rows_as_dicts(conn.execute(
        f"SELECT {_RECORD_COLUMNS} FROM canonical_person_statement_records "
        "WHERE speaker_person_id = ? OR referent_person_id = ? ORDER BY effective_at, rowid",
        (person_id, person_id)).fetchall())
    recorded_components = set()

    def visible(event_id):
        return family_membership.can_receive_canonical_event(
            conn, event_id, viewer_principal_actor_id=viewer_principal_actor_id, viewer_scope=viewer_scope,
            active_family_principal_ids=active_family_principal_ids)

    for r in rows:
        if not visible(r["source_event_id"]):
            continue
        recorded_components.add(r["source_component_id"])
        entry = {"class": r["statement_class"], "record_action": r["action"], "effective_at": r["effective_at"],
                 "source_event_id": r["source_event_id"], "quoted_text": r["quoted_text"],
                 "speaker": _label(conn, r["speaker_person_id"]) if r["speaker_person_id"] else "Clark",
                 "frame_note": FRAME_TEXT[r["statement_class"]]}
        if r["statement_class"] == CLASS_STANDING_REQUEST:
            entry.update(state=states.get(r["record_id"]), stated_scope=r["scope_text"])
        if r["referent_person_id"]:
            entry["about"] = _label(conn, r["referent_person_id"])
        if r["statement_class"] == CLASS_CORRECTION:
            entry["in_answer_to_event_id"] = r["prompted_by_event_id"]
        later = _later_speaker_identity(conn, r)
        if later:
            entry["speaker_identity_established_later_at"] = later["binding_recorded_at"]
        entries.append(entry)
    if person["linked_actor_id"]:
        for comp_id, event_id, text, occurred in conn.execute(
                "SELECT c.component_id, c.event_id, c.component_text, e.occurred_at FROM event_components c "
                "JOIN events e ON e.event_id = c.event_id WHERE c.creator_actor_id = ? AND c.component_kind = ? "
                "ORDER BY e.occurred_at DESC, c.component_id DESC LIMIT ?",
                (person["linked_actor_id"], HUMAN_INPUT_COMPONENT_KIND, limit * 4)).fetchall():
            if comp_id in recorded_components or not visible(event_id):
                continue
            entries.append({"class": "historical_utterance", "effective_at": occurred, "source_event_id": event_id,
                            "quoted_text": text, "speaker": person["display_label"],
                            "frame_note": FRAME_TEXT["historical_utterance"]})
    entries.sort(key=lambda e: (e["effective_at"], e["source_event_id"]))
    return {"person": {"person_id": person_id, "display_label": person["display_label"],
                       "entity_kind": person["entity_kind"]},
            "entries": entries[-limit:] if limit else entries}


def render_person_history(history) -> str:
    """Deterministic, host-neutral: dated, attributed, framed.  Never a summary of who the person is."""
    if not history:
        return ""
    lines = [f"HISTORY_INVOLVING {history['person']['display_label']} (dated records; not a description of them)"]
    if not history["entries"]:
        lines.append("(no retrievable records)")
    for e in history["entries"]:
        tail = f" [{e['state']}; scope: {e['stated_scope']}]" if e.get("state") else ""
        about = f" about {e['about']}" if e.get("about") else ""
        lines.append(f"- {e['class']}{about} | {e['speaker']} | t={e['effective_at']} | {e['source_event_id']}"
                     f"{tail}: \"{e['quoted_text']}\" ({e['frame_note']})")
    return "\n".join(lines)
