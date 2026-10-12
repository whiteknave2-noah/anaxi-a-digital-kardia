"""Historical unit reconstruction: retrieved records -> exact canonical turns.

WHY THIS EXISTS.  ``hippocampus_retrieval`` selects and bounds records; since
``fe88eca``/``c107be3``/``8b512c0`` it also groups a resource-encounter record with the
canonical turn that triggered it, trims an episode as one delivery unit, and renders the
group as one compact JSON line.  What it does NOT do is read the canonical ledger to
establish *what the historical unit was*: it renders whatever the hippocampal index holds
and trusts that the index and the ledger still agree.  This module is the missing half --
it re-derives the unit from ``anaxi_provenance.db`` and fails closed when the two disagree.

WHAT A "HISTORICAL UNIT" IS HERE.  One canonical turn.  A turn is identified by its
``event_id``; the human utterance, the receiving actor's reply (including a lawful typed
``no_reply``), and any host record that canonically references the turn are recovered by
EXACT component linkage only:

  * human utterance  -- the ``human_conversational_input`` component of the ``human_waking_input``
    event named by the turn's ``human_input_event_id`` component;
  * receiving-actor utterance -- the ``conversational_prose`` component of the turn event
    itself, authored by the receiving actor;
  * host record -- a ``workspace_resource_encounter`` event carrying
    ``triggering_waking_event_id`` == the turn.

Never by timestamp proximity, text similarity, embedding, or any other semantic signal, and
never by a displayed name or a display label: only canonical component ids and component
kinds decide.  This is the same linkage law ``session_dialogue_window`` and
``conversation_projection`` already enforce for current-session continuity, applied here to
long-term retrieved history.

FAIL CLOSED.  Every ``event_components``-sourced item is re-read from the ledger and its
delivered text must be a prefix of the canonical ``component_text`` (exact equality when
retrieval did not truncate it).  An item whose text does not match, whose component does not
exist, or whose ``content_sha256`` disagrees is NOT delivered and NOT silently repaired:
it is returned in ``withheld`` with a reason and with the exact text that was NOT delivered,
so a caller can mechanically prove the withheld words appear nowhere downstream.  A record the ledger cannot confirm is never
presented as though it were established.

READ-ONLY.  No model call, no write, no Kardia interaction, no hippocampus sync, and no
schema or ledger mutation of any kind.  Pure functions over a read-only connection.
"""

import dataclasses
import hashlib
import json
import os
import pathlib
import sqlite3
from datetime import datetime
from typing import Optional

from provenance_schema import derive_stable_id

RECEIVING_ACTOR_ID = derive_stable_id("actor", "clark")

# Structural roles.  These classify a canonical component by what it IS in the ledger, not
# by what it means.  They are deliberately NOT a new actor ontology: who authored a record
# is stated separately by `actor_legibility.actor_relation()`, which is the existing
# perspective-invariant actor-EQUALITY vocabulary (SAME_AS_RECEIVING_ACTOR /
# HUMAN_INTERLOCUTOR / HOST) already carried in the frozen corpus.  Nothing here names a
# person, a role in a play, or a speaker label.
ROLE_HUMAN_UTTERANCE = "human_utterance"
ROLE_RECEIVING_ACTOR_UTTERANCE = "receiving_actor_utterance"
ROLE_TYPED_NO_REPLY = "typed_no_reply"
ROLE_HOST_RECORD = "host_record"
ROLE_UNRESOLVED_EXPRESSION = "unresolved_expression"
ROLE_DERIVED_INFERENCE = "derived_inference"
ROLE_UNATTRIBUTED_RECORD = "unattributed_record"
ROLE_LEGACY_OBSERVATION = "legacy_observation_unknown_authorship"
ROLE_PRE_AUTHENTICATION_RECORD = "pre_authentication_legacy_record"

# Roles whose text is human speech.  Only these may ever occupy a "user"-role message
# (see relational_history_projection): a host-, agent- or inference-authored record inside a
# user-role message is exactly the CONTROL-SCAFFOLD ATTRIBUTION defect of 2026-09-05, in
# which host-authored JSON formatting text shipped as the human's own speech.
HUMAN_SPEECH_ROLES = frozenset({ROLE_HUMAN_UTTERANCE})

# Roles whose text is the receiving actor's own speech.
RECEIVING_ACTOR_SPEECH_ROLES = frozenset({ROLE_RECEIVING_ACTOR_UTTERANCE, ROLE_TYPED_NO_REPLY})

# Roles that are records ABOUT something, never conversation.  These never enter the
# role-tagged historical region at all.
RECORD_ROLES = frozenset({
    ROLE_HOST_RECORD, ROLE_UNRESOLVED_EXPRESSION, ROLE_DERIVED_INFERENCE, ROLE_UNATTRIBUTED_RECORD,
    ROLE_LEGACY_OBSERVATION, ROLE_PRE_AUTHENTICATION_RECORD,
})

COMPONENT_HUMAN_INPUT = "human_conversational_input"
COMPONENT_HUMAN_LINK = "human_input_event_id"
COMPONENT_PROSE = "conversational_prose"
COMPONENT_REPLY_CHOICE = "clark_reply_choice"
COMPONENT_TRIGGERING_TURN = "triggering_waking_event_id"
COMPONENT_ENCOUNTER_FACT = "resource_encounter_fact"
EVENT_HUMAN_INPUT = "human_waking_input"
EVENT_WAKING_TURN = "waking_turn"
EVENT_RESOURCE_ENCOUNTER = "workspace_resource_encounter"
NO_REPLY_CHOICE = "no_reply"

# Only these source stores are canonical-ledger records and can therefore be verified.
# `sleep_derivations` and `relational_events` are not event_components rows: a sleep
# derivation is a tentative association and a relational_events row is an experimental
# legacy projection, so neither is re-derivable from event_components and neither is
# claimed to be.
CANONICAL_SOURCE_STORE = "event_components"
SLEEP_SOURCE_STORE = "sleep_derivations"
LEGACY_LOG_SOURCE_STORE = "anaxi_log_jsonl"
LEGACY_OBSERVATION_SOURCE_STORE = "relational_events"

SOURCE_CATEGORY_CANONICAL_COMPONENT = "canonical_component"
SOURCE_CATEGORY_SLEEP_DERIVATION = "sleep_derivation"
SOURCE_CATEGORY_LEGACY_OBSERVATION = "legacy_observation_unknown_authorship"
SOURCE_CATEGORY_AUTHENTICATED_HUMAN_RELATIONAL = "authenticated_human_via_relational_link"
SOURCE_CATEGORY_PRE_AUTHENTICATION = "pre_authentication_legacy_record"
LEGACY_OBSERVATION_COMPONENT_KIND = "legacy_external_observation"
PRE_AUTHENTICATION_COMPONENT_KIND = "legacy_pre_authentication_prompt"
PRE_AUTHENTICATION_LAYER = "pre_authentication_layer"

# Withholding reasons (host-side; never model-facing text).
WITHHELD_NOT_FOUND = "canonical_component_absent"
WITHHELD_TEXT_MISMATCH = "delivered_text_not_a_prefix_of_canonical_text"
WITHHELD_TRUNCATED_MISMATCH = "delivered_text_truncated_but_differs_from_canonical_text"
WITHHELD_DIGEST_MISMATCH = "canonical_content_digest_disagrees"
WITHHELD_AMBIGUOUS = "canonical_linkage_ambiguous"
WITHHELD_PROVENANCE_MISMATCH = "retrieval_provenance_disagrees_with_canonical_row"
WITHHELD_UNVERIFIED_SOURCE = "source_not_rederivable_from_canonical_ledger"
WITHHELD_LEGACY_SOURCE_UNAVAILABLE = "legacy_observation_source_unavailable"
WITHHELD_LEGACY_PROVENANCE_MISMATCH = "legacy_observation_provenance_mismatch"
WITHHELD_LEGACY_MIRROR_MISSING = "legacy_observation_mirror_missing_or_ambiguous"
WITHHELD_LINKED_HUMAN_PROVENANCE_MISMATCH = "linked_human_provenance_mismatch"
WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH = "pre_authentication_provenance_mismatch"
WITHHELD_PRE_AUTHENTICATION_MIRROR_MISSING = "pre_authentication_mirror_missing_or_ambiguous"


class HistoricalUnitError(RuntimeError):
    """The canonical ledger could not be read at all.  Never caught into a partial result:
    an unreadable ledger withholds the whole historical region rather than delivering
    unverified records."""


@dataclasses.dataclass(frozen=True)
class CanonicalComponent:
    """The ledger row a projected piece of history was read from.  Nothing is projected
    without one of these.  Event components and committed Sleep derivations are both re-read
    from Kardia's anchored database; an index-only source is withheld."""
    event_id: str
    event_type: str
    component_id: Optional[int]
    sequence: Optional[int]
    component_kind: Optional[str]
    creator_actor_id: Optional[str]
    creator_actor_type: Optional[str]
    authorship_resolution: Optional[str]
    content_sha256: Optional[str]
    occurred_at: Optional[int]
    session_id: Optional[str]
    visibility_scope: Optional[str]
    component_text: Optional[str]
    source_category: str = SOURCE_CATEGORY_CANONICAL_COMPONENT
    authentication_status: Optional[str] = None
    mirror_source_store: Optional[str] = None
    mirror_source_locator: Optional[str] = None
    mirror_raw_sha256: Optional[str] = None
    source_substrate: Optional[str] = None
    source_pipeline_id: Optional[str] = None
    representation_event_id: Optional[str] = None
    representation_occurred_at: Optional[int] = None
    representation_recorded_at: Optional[int] = None
    source_raw_sha256: Optional[str] = None
    historical_route_user_id: Optional[str] = None
    relational_event_id: Optional[int] = None
    imported_at: Optional[int] = None

    @property
    def verified(self):
        return self.component_text is not None


@dataclasses.dataclass(frozen=True)
class HistoricalUnit:
    """One reconstructed piece of history, with the canonical row it came from and the
    provenance that will be attached to it on the model-facing side."""
    unit_id: str
    item_id: str
    source_store: str
    source_locator: str
    turn_event_id: Optional[str]
    role: str
    actor_relation: Optional[str]
    text: str
    text_truncated: bool
    delivered_start: int
    delivered_end: int
    source_chars: int
    canonical: CanonicalComponent
    selection_basis: str
    repeat_count: int
    record_class: Optional[str]   # the EXISTING record_label() class, computed for every unit so
                                  # the projection can reuse it whichever slot a unit lands in
    occurred_at: Optional[int]
    companion_of_event_id: Optional[str]

    @property
    def delivered_range(self):
        if not self.text_truncated:
            return None
        return f"characters {self.delivered_start}-{self.delivered_end} of {self.source_chars}"


@dataclasses.dataclass(frozen=True)
class ReconstructionResult:
    units: tuple
    withheld: tuple          # ((item_id, reason, withheld_text), ...) -- never delivered
    unverified_source_stores: tuple
    duplicates_suppressed: tuple = ()


def _readonly_uri(path):
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


def open_ledger_readonly(provenance_db_path):
    if not provenance_db_path or not os.path.isfile(provenance_db_path):
        raise HistoricalUnitError(f"canonical provenance database does not exist: {provenance_db_path!r}")
    try:
        conn = sqlite3.connect(_readonly_uri(provenance_db_path), uri=True)
        conn.execute("PRAGMA query_only = ON;")
        conn.execute("SELECT count(*) FROM sqlite_master;")
    except sqlite3.Error as e:
        raise HistoricalUnitError(f"could not open canonical provenance database read-only: {e}") from e
    return conn


def _has_column(conn, table, column):
    return column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


class Ledger:
    """Read-only canonical lookups, each cached per connection.  Every read is by exact
    canonical identifier."""

    def __init__(self, conn):
        self.conn = conn
        self._events = {}
        self._components = {}
        self._actors = {}
        self._scope_h = _has_column(conn, "events", "visibility_scope")
        self._scope_a = _has_column(conn, "auth_contexts", "visibility_scope")
        self._session = _has_column(conn, "auth_contexts", "session_id")
        self._has_sleep_derivations = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sleep_derivations'"
        ).fetchone())
        self._has_sleep_cycles = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sleep_cycles'"
        ).fetchone())

    def event(self, event_id):
        if event_id in self._events:
            return self._events[event_id]
        # Both events and auth_contexts may carry this column.  It must be
        # qualified: an unqualified name fails on the mixed-schema production
        # ledger with ``ambiguous column name: visibility_scope``.
        scope = "e.visibility_scope" if self._scope_h else "NULL"
        row = self.conn.execute(
            f"SELECT e.event_id, e.event_type, e.occurred_at, {scope}, "
            "a.session_id,a.auth_state FROM events e "
            "LEFT JOIN auth_contexts a ON a.auth_context_id=e.auth_context_id "
            "WHERE e.event_id=?", (event_id,)).fetchone()
        self._events[event_id] = row
        return row

    def component(self, event_id, component_kind=None, component_id=None):
        key = (event_id, component_kind, component_id)
        if key in self._components:
            return self._components[key]
        if component_id is not None:
            row = self.conn.execute(
                "SELECT component_id, event_id, sequence, component_kind, component_text, "
                "content_sha256, creator_actor_id, authorship_resolution FROM event_components "
                "WHERE event_id=? AND component_id=?", (event_id, component_id)).fetchone()
        else:
            # More than one component of the same kind on one event is ambiguous linkage; it
            # is not resolved by picking one, and callers use component_count() to refuse it.
            rows = self.conn.execute(
                "SELECT component_id, event_id, sequence, component_kind, component_text, "
                "content_sha256, creator_actor_id, authorship_resolution FROM event_components "
                "WHERE event_id=? AND component_kind=? ORDER BY sequence ASC, component_id ASC",
                (event_id, component_kind)).fetchall()
            row = rows[0] if len(rows) == 1 else None
        self._components[key] = row
        return row

    def component_count(self, event_id, component_kind):
        return self.conn.execute(
            "SELECT COUNT(*) FROM event_components WHERE event_id=? AND component_kind=?",
            (event_id, component_kind)).fetchone()[0]

    def human_input_component(self, event_id):
        """The canonical ``human_conversational_input`` of a human input event, or ``None``.

        Same predicate as ``wtr0_waking_recovery._load_canonical_human_input`` /
        ``resume_human_input.read_unanswered_input``: the event must be typed
        ``human_waking_input``, the component must be ``sequence = 0``, and the
        ``auth_contexts`` row must actually AUTHENTICATE the component's creator.  A
        human utterance that the ledger does not authenticate is not treated as the human's
        speech here."""
        key = ("human_input", event_id)
        if key in self._components:
            return self._components[key]
        auth = "a.auth_state='authenticated' AND a.authenticated_actor_id = ec.creator_actor_id"
        rows = self.conn.execute(
            "SELECT ec.component_id, ec.event_id, ec.sequence, ec.component_kind, ec.component_text, "
            "ec.content_sha256, ec.creator_actor_id, ec.authorship_resolution "
            "FROM events e JOIN event_components ec ON ec.event_id = e.event_id "
            f"JOIN auth_contexts a ON a.auth_context_id = e.auth_context_id "
            f"WHERE e.event_id = ? AND e.event_type = ? AND ec.sequence = 0 "
            f"AND ec.component_kind = ? AND {auth} ORDER BY ec.component_id",
            (event_id, EVENT_HUMAN_INPUT, COMPONENT_HUMAN_INPUT)).fetchall()
        row = rows[0] if len(rows) == 1 else None
        self._components[key] = row
        return row

    def turns_for_human_input(self, human_input_event_id):
        """Every canonical ``waking_turn`` that names this human input event, by exact
        component linkage only -- the same law as
        ``wtr0_waking_recovery._linked_canonical_x_event_ids``.

        Returns a tuple so that cardinality is visible: one exact turn is a pairing, zero
        means the utterance is not yet part of a completed exchange, and more than one is
        ambiguous linkage that is never resolved by picking one."""
        key = ("turns_for", human_input_event_id)
        if key not in self._components:
            rows = self.conn.execute(
                "SELECT e.event_id FROM event_components c JOIN events e ON e.event_id = c.event_id "
                "WHERE c.component_kind = ? AND c.component_text = ? AND e.event_type = ? "
                "ORDER BY e.event_id",
                (COMPONENT_HUMAN_LINK, human_input_event_id, EVENT_WAKING_TURN)).fetchall()
            self._components[key] = tuple(r[0] for r in rows)
        return self._components[key]

    def actor_type(self, actor_id):
        if actor_id is None:
            return None
        if actor_id not in self._actors:
            row = self.conn.execute(
                "SELECT actor_type FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
            self._actors[actor_id] = row[0] if row else None
        return self._actors[actor_id]

    def _from_row(self, event_id, row):
        if row is None:
            return None
        event = self.event(event_id)
        _cid, _eid, sequence, kind, text, digest, creator, authorship = row
        return CanonicalComponent(
            event_id=event_id,
            event_type=event[1] if event else None,
            component_id=_cid,
            sequence=sequence,
            component_kind=kind,
            creator_actor_id=creator,
            creator_actor_type=self.actor_type(creator),
            authorship_resolution=authorship,
            content_sha256=digest,
            occurred_at=event[2] if event else None,
            session_id=(event[4] if event and self._session else None),
            visibility_scope=(event[3] if event and self._scope_h else None),
            component_text=text,
            source_category=SOURCE_CATEGORY_CANONICAL_COMPONENT,
            authentication_status=(event[5] if event else None),
        )

    def canonical(self, event_id, component_id, component_kind):
        """The ledger row for one retrieved item, or ``None`` when it cannot be found."""
        if component_id is not None:
            return self._from_row(event_id, self.component(event_id, component_id=component_id))
        if component_kind is not None and self.component_count(event_id, component_kind) == 1:
            return self._from_row(event_id, self.component(event_id, component_kind=component_kind))
        return None

    def canonical_human_input(self, event_id):
        """The authenticated canonical human utterance on this event, or ``None``."""
        return self._from_row(event_id, self.human_input_component(event_id))

    def linked_authenticated_human(self, item, relational_db_path):
        """Verify a relational X representation through its exact canonical H link.

        Returns ``(component, reason, link_present)``.  ``link_present`` keeps a
        contradictory X->H claim from falling through into the unknown-authorship
        observation class.  The relational X is evidence of representation and
        linkage only; authorship comes exclusively from the authenticated H row.
        """
        if not relational_db_path or not os.path.isfile(relational_db_path):
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE, False
        if not (
            item.source_store == LEGACY_OBSERVATION_SOURCE_STORE
            and item.memory_kind == "human_expression"
            and item.attribution_status == "unknown"
            and item.authentication_status == "unknown"
            and item.creator_actor_id is None
            and item.creator_actor_type is None
            and item.component_kind is None
        ):
            return None, WITHHELD_LINKED_HUMAN_PROVENANCE_MISMATCH, False
        try:
            relational_id = int(item.source_locator)
        except (TypeError, ValueError):
            return None, WITHHELD_LINKED_HUMAN_PROVENANCE_MISMATCH, False
        if str(relational_id) != str(item.source_locator):
            return None, WITHHELD_LINKED_HUMAN_PROVENANCE_MISMATCH, False

        try:
            link_rows = self.conn.execute(
                "SELECT l.component_text,l.creator_actor_id,l.authorship_resolution "
                "FROM event_components l JOIN actor_host_system host "
                "ON host.actor_id=l.creator_actor_id "
                "WHERE l.event_id=? AND l.component_kind=?",
                (item.event_id, COMPONENT_HUMAN_LINK),
            ).fetchall()
        except sqlite3.Error:
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE, False
        if not link_rows:
            # A non-host or otherwise malformed link is still a linkage claim and
            # must fail closed rather than being reclassified as unknown authorship.
            raw_links = self.conn.execute(
                "SELECT COUNT(*) FROM event_components WHERE event_id=? AND component_kind=?",
                (item.event_id, COMPONENT_HUMAN_LINK),
            ).fetchone()[0]
            return (None, WITHHELD_LINKED_HUMAN_PROVENANCE_MISMATCH, True) if raw_links else (
                None, None, False)
        if len(link_rows) != 1:
            return None, WITHHELD_LINKED_HUMAN_PROVENANCE_MISMATCH, True
        human_event_id, link_creator, link_authorship = link_rows[0]
        if not human_event_id or not link_creator or link_authorship != "resolved":
            return None, WITHHELD_LINKED_HUMAN_PROVENANCE_MISMATCH, True

        try:
            relational = sqlite3.connect(_readonly_uri(relational_db_path), uri=True)
            relational.execute("PRAGMA query_only = ON;")
            rows = relational.execute(
                "SELECT id,event_id,substrate,created_at,external_observation,status,"
                "pipeline_id,auth_context_id FROM relational_events WHERE id=?",
                (relational_id,),
            ).fetchall()
        except sqlite3.Error:
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE, True
        finally:
            if "relational" in locals():
                relational.close()
        if len(rows) != 1:
            return None, WITHHELD_LINKED_HUMAN_PROVENANCE_MISMATCH, True
        (row_id, row_event_id, substrate, created_at, source_text, status,
         row_pipeline, row_auth_context) = rows[0]

        scope = "e.visibility_scope" if self._scope_h else "NULL"
        auth_scope = "a.visibility_scope" if self._scope_a else "NULL"
        try:
            x_rows = self.conn.execute(
                f"SELECT e.event_type,e.occurred_at,e.pipeline_id,e.auth_context_id,{scope},"
                f"a.session_id,{auth_scope},a.auth_state,a.authenticated_actor_id FROM events e "
                "LEFT JOIN auth_contexts a ON a.auth_context_id=e.auth_context_id "
                "WHERE e.event_id=?", (item.event_id,),
            ).fetchall()
            h_rows = self.conn.execute(
                f"SELECT h.event_type,h.occurred_at,h.pipeline_id,{scope.replace('e.', 'h.')},"
                f"a.auth_state,a.authenticated_actor_id,a.session_id,{auth_scope},"
                "c.component_id,c.sequence,c.component_kind,c.component_text,c.content_sha256,"
                "c.creator_actor_id,c.authorship_resolution "
                "FROM events h JOIN auth_contexts a ON a.auth_context_id=h.auth_context_id "
                "JOIN event_components c ON c.event_id=h.event_id "
                "WHERE h.event_id=? AND c.component_kind=? AND c.sequence=0",
                (human_event_id, COMPONENT_HUMAN_INPUT),
            ).fetchall()
            pipeline_rows = self.conn.execute(
                "SELECT legacy_substrate_label FROM pipelines WHERE pipeline_id=?",
                (item.pipeline_id,),
            ).fetchall()
        except sqlite3.Error:
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE, True
        if len(x_rows) != 1 or len(h_rows) != 1 or len(pipeline_rows) != 1:
            return None, WITHHELD_LINKED_HUMAN_PROVENANCE_MISMATCH, True
        (x_type, x_occurred_at, x_pipeline, x_auth_context,
         x_scope, x_session, x_auth_scope, x_auth_state, x_authenticated_actor) = x_rows[0]
        (h_type, h_occurred_at, h_pipeline, h_scope, h_auth_state,
         h_authenticated_actor, h_session, h_auth_scope, component_id,
         sequence, component_kind, human_text, human_digest, human_creator,
         human_authorship) = h_rows[0]
        digest = _sha256(source_text) if isinstance(source_text, str) else None
        indexed_text_matches = (
            source_text.startswith(item.content) if item.content_truncated
            and isinstance(source_text, str) else source_text == item.content
        )
        if not (
            row_id == relational_id
            and row_event_id == item.event_id
            and status == "recorded"
            and x_type == EVENT_WAKING_TURN
            and isinstance(created_at, int)
            and isinstance(x_occurred_at, int)
            and isinstance(item.occurred_at, int)
            and created_at >= x_occurred_at
            and x_occurred_at == item.occurred_at
            and row_pipeline == x_pipeline == h_pipeline == item.pipeline_id
            and row_auth_context == x_auth_context
            and x_auth_state == item.authentication_status == "unknown"
            and x_authenticated_actor is None
            and x_scope == x_auth_scope == h_scope == h_auth_scope
            and item.session_id == x_session
            and substrate == pipeline_rows[0][0]
            and h_type == EVENT_HUMAN_INPUT
            and h_occurred_at <= x_occurred_at
            and h_auth_state == "authenticated"
            and h_authenticated_actor is not None
            and h_authenticated_actor == human_creator
            and sequence == 0
            and component_kind == COMPONENT_HUMAN_INPUT
            and human_authorship == "resolved"
            and human_text == source_text
            and human_digest == digest
            and indexed_text_matches
            and item.source_chars == len(source_text)
            and item.content_sha256 == digest
            and self.turns_for_human_input(human_event_id) == (item.event_id,)
        ):
            return None, WITHHELD_LINKED_HUMAN_PROVENANCE_MISMATCH, True

        canonical = self._from_row(human_event_id, (
            component_id, human_event_id, sequence, component_kind, human_text,
            human_digest, human_creator, human_authorship,
        ))
        return dataclasses.replace(
            canonical,
            source_category=SOURCE_CATEGORY_AUTHENTICATED_HUMAN_RELATIONAL,
            authentication_status="authenticated",
            representation_event_id=item.event_id,
            representation_occurred_at=x_occurred_at,
            representation_recorded_at=created_at,
            source_substrate=substrate,
            source_pipeline_id=row_pipeline,
            relational_event_id=relational_id,
        ), None, True

    def canonical_sleep_derivation(self, derivation_id):
        """A committed canonical Sleep derivation, re-read from Kardia's anchored tables."""
        if not (self._has_sleep_derivations and self._has_sleep_cycles):
            return None
        row = self.conn.execute(
            "SELECT d.derivation_id, d.derived_text, d.derived_text_sha256, d.created_at "
            "FROM sleep_derivations d JOIN sleep_cycles c ON c.cycle_id=d.cycle_id "
            "WHERE d.derivation_id=?", (derivation_id,)).fetchall()
        if len(row) != 1:
            return None
        derivation_id, text, digest, created_at = row[0]
        return CanonicalComponent(
            event_id=derivation_id, event_type="sleep_derivation", component_id=None,
            sequence=None, component_kind="derived_text", creator_actor_id=None,
            creator_actor_type=None, authorship_resolution="unknown", content_sha256=digest,
            occurred_at=created_at, session_id=None, visibility_scope=None,
            component_text=text,
            source_category=SOURCE_CATEGORY_SLEEP_DERIVATION,
        )

    def pre_authentication_record(self, item, relational_db_path, historical_jsonl_path):
        """Verify one indexed pre-authentication JSONL record and its mirror.

        This is the same source chain used by explicit historical access, made
        available to automatic reconstruction without promoting the record to a
        canonical component or to authenticated human speech.
        """
        if not relational_db_path or not historical_jsonl_path:
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE
        if not os.path.isfile(relational_db_path) or not os.path.isfile(historical_jsonl_path):
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE
        if not (
            item.source_store == LEGACY_LOG_SOURCE_STORE
            and item.memory_kind == "human_expression"
            and item.authentication_status == PRE_AUTHENTICATION_LAYER
            and item.attribution_status == "unknown"
            and item.creator_actor_id is None
            and item.creator_actor_type is None
            and item.component_kind is None
            and item.session_id is None
            and item.session_resolution == "unknown"
        ):
            return None, WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH
        prefix = "line:"
        locator = str(item.source_locator)
        if not locator.startswith(prefix) or not locator[len(prefix):].isdigit():
            return None, WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH
        line_index = int(locator[len(prefix):])
        try:
            with open(historical_jsonl_path, "rb") as source:
                raw_line = source.read().splitlines()[line_index]
            entry = json.loads(raw_line.decode("utf-8"))
            source_text = entry["prompt"]
        except (IndexError, KeyError, TypeError, ValueError, UnicodeDecodeError, OSError):
            return None, WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH
        if not isinstance(source_text, str):
            return None, WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH
        from hippocampus_store import HISTORICAL_JSONL_SOURCE_ID, derive_historical_event_id
        if derive_historical_event_id(HISTORICAL_JSONL_SOURCE_ID, raw_line, line_index) != item.event_id:
            return None, WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH
        digest = _sha256(source_text)
        indexed_text_matches = (
            source_text.startswith(item.content) if item.content_truncated else source_text == item.content
        )
        if not (
            indexed_text_matches
            and item.source_chars == len(source_text)
            and item.content_sha256 == digest
        ):
            return None, WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH

        scope = "e.visibility_scope" if self._scope_h else "NULL"
        try:
            rows = self.conn.execute(
                "SELECT e.event_type,e.occurred_at,e.pipeline_id,e.auth_context_id," + scope + ","
                "m.migration_status,m.migrated_at,p.legacy_substrate_label "
                "FROM events e JOIN event_migration_status m ON m.event_id=e.event_id "
                "LEFT JOIN pipelines p ON p.pipeline_id=e.pipeline_id WHERE e.event_id=?",
                (item.event_id,),
            ).fetchall()
            routes = self.conn.execute(
                "SELECT routing_constant_value,routing_mechanism,recorded_at "
                "FROM legacy_routing_provenance WHERE event_id=? ORDER BY legacy_routing_id",
                (item.event_id,),
            ).fetchall()
            human_components = self.conn.execute(
                "SELECT COUNT(*) FROM event_components WHERE event_id=? "
                "AND component_kind=?", (item.event_id, COMPONENT_HUMAN_INPUT),
            ).fetchone()[0]
        except sqlite3.Error:
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE
        if len(rows) != 1 or len(routes) != 1 or not routes[0][0] or not routes[0][1]:
            return None, WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH
        (event_type, occurred_at, pipeline_id, auth_context_id, visibility_scope,
         migration_status, migrated_at, substrate) = rows[0]
        try:
            source_time = datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00"))
            if source_time.tzinfo is None:
                return None, WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH
            source_epoch = int(source_time.timestamp())
        except (TypeError, ValueError):
            return None, WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH
        if not (
            event_type == EVENT_WAKING_TURN
            and auth_context_id is None
            and visibility_scope is None
            and migration_status == PRE_AUTHENTICATION_LAYER
            and human_components == 0
            and pipeline_id == item.pipeline_id
            and occurred_at == item.occurred_at == source_epoch
            and entry.get("substrate") == substrate
        ):
            return None, WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH

        try:
            relational = sqlite3.connect(_readonly_uri(relational_db_path), uri=True)
            relational.execute("PRAGMA query_only = ON;")
            mirror_rows = relational.execute(
                "SELECT id,user_id,substrate,created_at,external_observation,status,"
                "pipeline_id,auth_context_id,event_id FROM relational_events "
                "WHERE created_at=? AND external_observation=?",
                (occurred_at, source_text),
            ).fetchall()
        except sqlite3.Error:
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE
        finally:
            if "relational" in locals():
                relational.close()
        if len(mirror_rows) != 1:
            return None, WITHHELD_PRE_AUTHENTICATION_MIRROR_MISSING
        (relational_event_id, route_user_id, mirror_substrate, mirror_occurred_at,
         mirror_text, mirror_status, mirror_pipeline_id, mirror_auth_context_id,
         mirror_event_id) = mirror_rows[0]
        if not (
            route_user_id == routes[0][0]
            and mirror_substrate == substrate
            and mirror_occurred_at == occurred_at
            and mirror_text == source_text
            and mirror_status == "recorded"
            and mirror_pipeline_id == pipeline_id
            and mirror_auth_context_id is None
            and mirror_event_id in (None, item.event_id)
        ):
            return None, WITHHELD_PRE_AUTHENTICATION_PROVENANCE_MISMATCH
        return CanonicalComponent(
            event_id=item.event_id,
            event_type=event_type,
            component_id=None,
            sequence=None,
            component_kind=PRE_AUTHENTICATION_COMPONENT_KIND,
            creator_actor_id=None,
            creator_actor_type=None,
            authorship_resolution="unknown",
            content_sha256=digest,
            occurred_at=occurred_at,
            session_id=None,
            visibility_scope=visibility_scope,
            component_text=source_text,
            source_category=SOURCE_CATEGORY_PRE_AUTHENTICATION,
            authentication_status=PRE_AUTHENTICATION_LAYER,
            mirror_source_store=LEGACY_OBSERVATION_SOURCE_STORE,
            mirror_source_locator=str(relational_event_id),
            source_substrate=substrate,
            source_pipeline_id=pipeline_id,
            representation_event_id=item.event_id,
            source_raw_sha256=hashlib.sha256(raw_line).hexdigest(),
            historical_route_user_id=route_user_id,
            relational_event_id=relational_event_id,
            imported_at=migrated_at,
        ), None

    def legacy_observation(self, item, relational_db_path, historical_jsonl_path):
        """Re-read one legacy observation from two independent historical sources.

        The relational row is the addressed source.  The JSONL record is required as an
        exact mirror and is located by exact event id plus byte-identical prompt, never by
        user id, adjacency, response text, or conversational plausibility.  The associated
        waking event supplies only timestamp/pipeline/scope integrity; it is never evidence
        that the observation was human-authored.
        """
        if not relational_db_path or not historical_jsonl_path:
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE
        if not os.path.isfile(relational_db_path) or not os.path.isfile(historical_jsonl_path):
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE
        if not (
            item.source_store == LEGACY_OBSERVATION_SOURCE_STORE
            and item.memory_kind == "human_expression"
            and item.attribution_status == "unknown"
            and item.authentication_status == "unknown"
            and item.creator_actor_id is None
            and item.creator_actor_type is None
            and item.component_kind is None
        ):
            return None, WITHHELD_LEGACY_PROVENANCE_MISMATCH
        try:
            relational_id = int(item.source_locator)
        except (TypeError, ValueError):
            return None, WITHHELD_LEGACY_PROVENANCE_MISMATCH
        if str(relational_id) != str(item.source_locator):
            return None, WITHHELD_LEGACY_PROVENANCE_MISMATCH

        try:
            relational = sqlite3.connect(_readonly_uri(relational_db_path), uri=True)
            relational.execute("PRAGMA query_only = ON;")
            rows = relational.execute(
                "SELECT id,event_id,substrate,created_at,external_observation,status,"
                "pipeline_id,auth_context_id FROM relational_events WHERE id=?",
                (relational_id,),
            ).fetchall()
        except sqlite3.Error:
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE
        finally:
            if "relational" in locals():
                relational.close()
        if len(rows) != 1:
            return None, WITHHELD_LEGACY_PROVENANCE_MISMATCH
        (row_id, event_id, substrate, created_at, source_text, status,
         pipeline_id, auth_context_id) = rows[0]
        if not isinstance(source_text, str) or not source_text:
            return None, WITHHELD_LEGACY_PROVENANCE_MISMATCH

        scope = "e.visibility_scope" if self._scope_h else "NULL"
        auth_scope = "a.visibility_scope" if self._scope_a else "NULL"
        try:
            event_rows = self.conn.execute(
                f"SELECT e.event_type,e.occurred_at,e.pipeline_id,e.auth_context_id,{scope},"
                f"a.auth_state,a.authenticated_actor_id,{auth_scope},a.session_id "
                "FROM events e LEFT JOIN auth_contexts a ON a.auth_context_id=e.auth_context_id "
                "WHERE e.event_id=?",
                (item.event_id,),
            ).fetchall()
            human_claims = self.conn.execute(
                "SELECT COUNT(*) FROM event_components WHERE event_id=? AND component_kind IN "
                "('human_conversational_input','human_input_event_id')",
                (item.event_id,),
            ).fetchone()[0]
        except sqlite3.Error:
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE
        if len(event_rows) != 1:
            return None, WITHHELD_LEGACY_PROVENANCE_MISMATCH
        (event_type, occurred_at, event_pipeline, event_auth_context, visibility_scope,
         auth_state, authenticated_actor_id, auth_scope_value, session_id) = event_rows[0]

        digest = _sha256(source_text)
        indexed_text_matches = (
            source_text.startswith(item.content) if item.content_truncated else source_text == item.content
        )
        if not (
            row_id == relational_id
            and event_id == item.event_id
            and status == "recorded"
            and event_type == EVENT_WAKING_TURN
            and created_at == occurred_at == item.occurred_at
            and pipeline_id == event_pipeline == item.pipeline_id
            and auth_context_id == event_auth_context
            and auth_state == "unknown"
            and authenticated_actor_id is None
            and visibility_scope == auth_scope_value
            and human_claims == 0
            and indexed_text_matches
            and item.source_chars == len(source_text)
            and item.content_sha256 == digest
        ):
            return None, WITHHELD_LEGACY_PROVENANCE_MISMATCH

        mirrors = []
        try:
            with open(historical_jsonl_path, "rb") as source:
                for line_index, raw_line in enumerate(source.read().splitlines()):
                    try:
                        entry = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("event_id") != item.event_id or entry.get("prompt") != source_text:
                        continue
                    try:
                        source_time = datetime.fromisoformat(
                            str(entry.get("timestamp")).replace("Z", "+00:00"))
                        if source_time.tzinfo is None:
                            continue
                        source_epoch = int(source_time.timestamp())
                    except (TypeError, ValueError):
                        continue
                    if not (
                        source_epoch == occurred_at
                        and entry.get("substrate") == substrate
                        and entry.get("pipeline_id") == pipeline_id
                    ):
                        continue
                    mirrors.append((line_index, raw_line))
        except OSError:
            return None, WITHHELD_LEGACY_SOURCE_UNAVAILABLE
        if len(mirrors) != 1:
            return None, WITHHELD_LEGACY_MIRROR_MISSING
        line_index, raw_line = mirrors[0]
        return CanonicalComponent(
            event_id=item.event_id,
            event_type=event_type,
            component_id=None,
            sequence=None,
            component_kind=LEGACY_OBSERVATION_COMPONENT_KIND,
            creator_actor_id=None,
            creator_actor_type=None,
            authorship_resolution="unknown",
            content_sha256=digest,
            occurred_at=occurred_at,
            session_id=session_id,
            visibility_scope=visibility_scope,
            component_text=source_text,
            source_category=SOURCE_CATEGORY_LEGACY_OBSERVATION,
            authentication_status="unknown",
            mirror_source_store=LEGACY_LOG_SOURCE_STORE,
            mirror_source_locator=f"line:{line_index}",
            mirror_raw_sha256=hashlib.sha256(raw_line).hexdigest(),
            source_substrate=substrate,
            source_pipeline_id=pipeline_id,
        ), None


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _verify_against_canonical(item, canon):
    """(ok, reason).  The delivered text must be a genuine prefix of the canonical text.

    Retrieval truncates to a character bound and never rewrites, so a delivered prefix is
    lawful; anything else -- a different string, a suffix, an edited record -- is a drift
    between the index and the ledger and is withheld."""
    if canon is None or canon.component_text is None:
        return False, WITHHELD_NOT_FOUND
    if item.content_truncated:
        if not canon.component_text.startswith(item.content):
            return False, WITHHELD_TEXT_MISMATCH
    elif canon.component_text != item.content:
        return False, WITHHELD_TRUNCATED_MISMATCH
    if canon.content_sha256 and _sha256(canon.component_text) != canon.content_sha256:
        return False, WITHHELD_DIGEST_MISMATCH
    # The canonical row is authority for provenance as well as words.  Retrieval is an index,
    # not a second truth store: stale or caller-constructed metadata must not be silently replaced
    # with convenient canonical values while the item is still reported as the selected source.
    claimed = (
        (item.component_kind, canon.component_kind),
        (item.creator_actor_id, canon.creator_actor_id),
        (item.creator_actor_type, canon.creator_actor_type),
        (item.session_id, canon.session_id),
        (item.occurred_at, canon.occurred_at),
    )
    if any(left is not None and left != right for left, right in claimed):
        return False, WITHHELD_PROVENANCE_MISMATCH
    return True, None


def _component_locator(item):
    """hippocampus_store records the event_components row's ``component_id`` in
    ``source_locator``.  Anything else is not addressable and is not guessed."""
    try:
        return int(item.source_locator)
    except (TypeError, ValueError):
        return None


def _classify(item, canon, ledger):
    """(role, turn_event_id).  Derived only from canonical event_type / component_kind /
    creator actor.  A record whose class cannot be established stays
    ROLE_UNATTRIBUTED_RECORD -- it is never promoted into conversation.

    A human utterance is not its own turn: the turn is the ``waking_turn`` event that names
    the human input event in its ``human_input_event_id`` component.  When that link is
    absent the utterance is still a real human utterance and is still delivered, but it
    carries no turn identity and is therefore grouped on its own event rather than being
    paired with anything."""
    kind = canon.component_kind if canon else item.component_kind
    event_type = canon.event_type if canon else None

    if canon is not None and canon.source_category == SOURCE_CATEGORY_LEGACY_OBSERVATION:
        return ROLE_LEGACY_OBSERVATION, None
    if canon is not None and canon.source_category == SOURCE_CATEGORY_PRE_AUTHENTICATION:
        return ROLE_PRE_AUTHENTICATION_RECORD, None
    if item.source_store == SLEEP_SOURCE_STORE:
        return ROLE_DERIVED_INFERENCE, None
    if event_type == EVENT_HUMAN_INPUT and kind == COMPONENT_HUMAN_INPUT:
        turns = ledger.turns_for_human_input(canon.event_id)
        # Exactly one exact link is a pairing.  Zero is an unpaired utterance; more than one
        # is ambiguous and is never collapsed by choosing one.  Without that one link the words
        # remain a record; they must not disappear from projection merely because they cannot form
        # a native dialogue turn.
        if len(turns) != 1:
            return ROLE_UNATTRIBUTED_RECORD, None
        return ROLE_HUMAN_UTTERANCE, turns[0]
    if event_type == EVENT_WAKING_TURN:
        has_one_human_link = ledger.component_count(canon.event_id, COMPONENT_HUMAN_LINK) == 1
        if kind == COMPONENT_PROSE and canon.creator_actor_id == RECEIVING_ACTOR_ID:
            return ((ROLE_RECEIVING_ACTOR_UTTERANCE, canon.event_id) if has_one_human_link
                    else (ROLE_UNATTRIBUTED_RECORD, None))
        if kind == COMPONENT_REPLY_CHOICE and canon.component_text == NO_REPLY_CHOICE:
            return ((ROLE_TYPED_NO_REPLY, canon.event_id) if has_one_human_link
                    else (ROLE_UNATTRIBUTED_RECORD, None))
        return ROLE_UNATTRIBUTED_RECORD, canon.event_id
    if event_type == EVENT_RESOURCE_ENCOUNTER or kind == COMPONENT_ENCOUNTER_FACT:
        return ROLE_HOST_RECORD, _triggering_turn(ledger, canon)
    if item.memory_kind == "mixed_historical_expression":
        return ROLE_UNRESOLVED_EXPRESSION, None
    return ROLE_UNATTRIBUTED_RECORD, None


def _triggering_turn(ledger, canon):
    if canon is None:
        return None
    row = ledger.component(canon.event_id, component_kind=COMPONENT_TRIGGERING_TURN)
    if row is None:
        return None
    return row[4] or None


def _record_class(item, role, canon=None):
    """The one-line, literal evidence class for ANY reconstructed unit, conversational or not.
    Reuses hippocampus_retrieval.record_label() -- the existing vocabulary already carried in the
    frozen corpus -- so no parallel taxonomy is introduced here.  It is computed for conversational
    units too, because whether a reconstructed human utterance may occupy a user role is the
    projection's decision, and a unit the projection demotes to a record carrier still needs a
    class to state there."""
    if role == ROLE_LEGACY_OBSERVATION:
        return "legacy historical observation (authorship unknown)"
    if role == ROLE_PRE_AUTHENTICATION_RECORD:
        return "pre-authentication historical source record (authorship not established)"
    if (canon is not None
            and canon.source_category == SOURCE_CATEGORY_AUTHENTICATED_HUMAN_RELATIONAL):
        return "authenticated canonical human expression (relational representation)"
    import hippocampus_retrieval
    return hippocampus_retrieval.record_label(item)


def _actor_relation(item, canonical=None):
    """The EXISTING perspective-invariant actor-equality relation.  Delegated to
    ``hippocampus_retrieval.actor_relation`` -- never redefined here -- so this module cannot
    drift from, or fork, the vocabulary already carried in the frozen corpus.

    Reconstruction has just re-read the authoritative component.  Its canonical creator wins
    over an index route's deliberately unknown creator (notably legacy relational/JSONL human
    rows): otherwise an authenticated ``human_conversational_input`` is verified as human speech
    but its sidecar still says the actor relation is unknown.  This is not inference from text or
    display labels; it is the creator id on the exact canonical component that admitted the unit.
    """
    import hippocampus_retrieval
    if canonical is not None and canonical.verified:
        item = dataclasses.replace(
            item,
            creator_actor_id=canonical.creator_actor_id,
            creator_actor_type=canonical.creator_actor_type,
        )
    return hippocampus_retrieval.actor_relation(item)


def _unit_id(item, role, turn_event_id):
    return "hist-" + hashlib.sha256(
        "\x1f".join((item.item_id, role, turn_event_id or "")).encode("utf-8")).hexdigest()[:24]


def reconstruct(items, provenance_db_path, *, relational_db_path=None, historical_jsonl_path=None):
    """Retrieved items -> (ReconstructionResult).  Pure/read-only over the canonical ledger.

    ``items`` are post-privacy-filter ``hippocampus_retrieval.RetrievedMemory`` values, in
    retrieval (rank) order.  Exact-echo items are already never delivered upstream and are
    skipped here too.  Every ``event_components`` item is verified against the ledger and
    withheld on any disagreement.  Committed Sleep derivations are re-read from their anchored
    Kardia tables.  Any other source that cannot be independently re-derived is withheld rather
    than presented as ledger-confirmed.
    """
    conn = open_ledger_readonly(provenance_db_path)
    try:
        ledger = Ledger(conn)
        units, withheld, unverified_stores = [], [], []
        for item in items:
            if item.selection_basis == "exact_echo_of_present_message":
                continue
            canon = None
            if item.source_store == CANONICAL_SOURCE_STORE:
                canon = ledger.canonical(item.event_id, _component_locator(item), item.component_kind)
                ok, reason = _verify_against_canonical(item, canon)
                if not ok:
                    withheld.append((item.item_id, reason, item.content))
                    continue
            elif (item.source_store == LEGACY_OBSERVATION_SOURCE_STORE
                  and item.authentication_status == "unknown"
                  and relational_db_path is not None):
                canon, reason, link_present = ledger.linked_authenticated_human(
                    item, relational_db_path)
                if canon is None and link_present:
                    withheld.append((item.item_id, reason, item.content))
                    unverified_stores.append(item.source_store)
                    continue
                if canon is None:
                    # Older direct relational H representations name H itself
                    # rather than X.  Preserve that exact canonical path before
                    # considering the genuinely unknown-authorship class.
                    direct_h = ledger.canonical_human_input(item.event_id)
                    if direct_h is not None:
                        ok, reason = _verify_against_canonical(item, direct_h)
                        if not ok:
                            withheld.append((item.item_id, reason, item.content))
                            continue
                        canon = direct_h
                    elif historical_jsonl_path is not None:
                        canon, reason = ledger.legacy_observation(
                            item, relational_db_path, historical_jsonl_path)
                        if canon is None:
                            withheld.append((item.item_id, reason, item.content))
                            unverified_stores.append(item.source_store)
                            continue
                    else:
                        withheld.append((item.item_id, WITHHELD_LEGACY_SOURCE_UNAVAILABLE,
                                         item.content))
                        unverified_stores.append(item.source_store)
                        continue
            elif (item.source_store == LEGACY_LOG_SOURCE_STORE
                  and item.authentication_status == PRE_AUTHENTICATION_LAYER
                  and relational_db_path is not None
                  and historical_jsonl_path is not None):
                canon, reason = ledger.pre_authentication_record(
                    item, relational_db_path, historical_jsonl_path)
                if canon is None:
                    withheld.append((item.item_id, reason, item.content))
                    unverified_stores.append(item.source_store)
                    continue
            elif item.memory_kind == "human_expression":
                # A human utterance is ingested from the legacy relational_events log or the
                # migrated JSONL rather than from event_components, but the WORDS are still
                # canonical -- they are the authenticated human_conversational_input component
                # of the human_waking_input event.  They are verified against that component
                # here, because a user-role message may only ever carry text the ledger
                # authenticates as this human's speech.
                canon = ledger.canonical_human_input(item.event_id)
                ok, reason = _verify_against_canonical(item, canon)
                if not ok:
                    withheld.append((item.item_id, reason, item.content))
                    continue
            elif item.source_store == SLEEP_SOURCE_STORE:
                canon = ledger.canonical_sleep_derivation(item.event_id)
                ok, reason = _verify_against_canonical(item, canon)
                if item.source_locator != item.event_id:
                    ok, reason = False, WITHHELD_PROVENANCE_MISMATCH
                if not ok:
                    withheld.append((item.item_id, reason, item.content))
                    continue
            else:
                # A retrieval-index row that cannot be re-read from the authoritative ledger is
                # not historical evidence merely because its labels look plausible.  Keep the
                # store visible in the result, but fail closed on its content.
                unverified_stores.append(item.source_store)
                withheld.append((item.item_id, WITHHELD_UNVERIFIED_SOURCE, item.content))
                continue
            role, turn_event_id = _classify(item, canon, ledger)
            # Canonical text length, not an index-authored claim, is the truncation authority.
            source_chars = len(canon.component_text)
            text = item.content
            # A typed no_reply is a lawful COMPLETE exchange with no words in it: the
            # canonical component text is the choice token "no_reply", not speech, and
            # projecting that token as the actor's utterance would put a machine value in
            # an assistant message.  The exchange is shown as it happened -- the human's
            # words, then an empty reply -- exactly as session_dialogue_window does.
            if role == ROLE_TYPED_NO_REPLY:
                text = ""
            units.append(HistoricalUnit(
                unit_id=_unit_id(item, role, turn_event_id),
                item_id=item.item_id,
                source_store=item.source_store,
                source_locator=item.source_locator,
                turn_event_id=turn_event_id,
                role=role,
                actor_relation=(None if role in (ROLE_LEGACY_OBSERVATION,
                                                 ROLE_PRE_AUTHENTICATION_RECORD)
                                else _actor_relation(item, canon)),
                text=text,
                text_truncated=bool(item.content_truncated),
                delivered_start=0,
                delivered_end=len(item.content),
                source_chars=source_chars,
                canonical=canon,
                selection_basis=item.selection_basis,
                repeat_count=item.repeat_count,
                record_class=_record_class(item, role, canon),
                occurred_at=canon.occurred_at,
                companion_of_event_id=item.companion_of_event_id,
            ))

        # Multiple representations of one canonical component carry one authority,
        # not repeated authority.  Keep the first retrieval-ranked representation
        # and record every suppressed representation without treating its words as
        # withheld (the same verified words remain delivered once).
        deduplicated, duplicates_suppressed, seen_authority = [], [], {}
        for unit in units:
            canon = unit.canonical
            authority_key = None
            if canon.component_id is not None and canon.source_category in (
                    SOURCE_CATEGORY_CANONICAL_COMPONENT,
                    SOURCE_CATEGORY_AUTHENTICATED_HUMAN_RELATIONAL):
                authority_key = ("canonical_component", canon.event_id, canon.component_id)
            if authority_key is None or authority_key not in seen_authority:
                deduplicated.append(unit)
                if authority_key is not None:
                    seen_authority[authority_key] = unit.item_id
            else:
                duplicates_suppressed.append((
                    unit.item_id, seen_authority[authority_key], ":".join(map(str, authority_key))))
        units = deduplicated

        # Conversation is useful only as an authoritative historical unit.  A lone historical
        # assistant turn can otherwise become indistinguishable from a continuation prefix, and a
        # lone human request invents an unanswered exchange.  Do not fetch or manufacture the
        # missing half here.  Fail closed on the RELATIONAL claim by demoting the verified words to
        # records; the authoritative text remains usable without pretending a partial unit is a
        # native exchange.
        by_turn = {}
        for unit in units:
            if unit.turn_event_id is not None:
                by_turn.setdefault(unit.turn_event_id, []).append(unit)
        incomplete_turns = set()
        for turn_id, group in by_turn.items():
            humans = [u for u in group if u.role == ROLE_HUMAN_UTTERANCE]
            replies = [u for u in group if u.role in RECEIVING_ACTOR_SPEECH_ROLES]
            if len(humans) != 1 or len(replies) != 1:
                incomplete_turns.add(turn_id)
        if incomplete_turns:
            units = [
                dataclasses.replace(
                    unit,
                    unit_id="hist-" + hashlib.sha256(
                        "\x1f".join((unit.item_id, ROLE_UNATTRIBUTED_RECORD, "")).encode("utf-8")
                    ).hexdigest()[:24],
                    turn_event_id=None,
                    role=ROLE_UNATTRIBUTED_RECORD,
                )
                if unit.turn_event_id in incomplete_turns
                and unit.role in (ROLE_HUMAN_UTTERANCE, *RECEIVING_ACTOR_SPEECH_ROLES)
                else unit
                for unit in units
            ]
        return ReconstructionResult(
            tuple(units), tuple(withheld), tuple(sorted(set(unverified_stores))),
            tuple(duplicates_suppressed))
    finally:
        conn.close()


def turn_groups(units):
    """Reconstructed units -> ordered historical units of conversation.

    A turn group is every reconstructed piece sharing one ``turn_event_id``; groups are
    ordered by the canonical ``occurred_at`` of their first member and ties broken by
    ``turn_event_id``, so the order is a function of the ledger alone.  Pieces with no turn
    identity (a sleep derivation, an unattributed legacy record) are records and are not
    grouped here; they are projected as records.
    """
    groups = {}
    for unit in units:
        if unit.turn_event_id is None or unit.role in RECORD_ROLES:
            continue
        if unit.turn_event_id not in groups:
            groups[unit.turn_event_id] = []
        groups[unit.turn_event_id].append(unit)
    order = sorted(groups, key=lambda turn_id: (
        min((u.canonical.occurred_at for u in groups[turn_id]
             if u.canonical.occurred_at is not None), default=-1), turn_id))
    for turn_event_id in order:
        groups[turn_event_id].sort(key=lambda u: (
            0 if u.role == ROLE_HUMAN_UTTERANCE else 1,
            u.canonical.component_id if u.canonical.component_id is not None else 0, u.unit_id))
    return [(turn_event_id, tuple(groups[turn_event_id])) for turn_event_id in order]


def record_units(units):
    """The non-conversational pieces, in retrieval order."""
    return tuple(u for u in units if u.role in RECORD_ROLES)
