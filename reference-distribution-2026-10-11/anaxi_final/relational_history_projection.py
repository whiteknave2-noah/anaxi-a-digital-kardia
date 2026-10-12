"""Native relational-history projection: reconstructed units -> role-tagged messages.

WHY THIS EXISTS.  ``historical_unit_reconstruction`` re-derives every retrieved record from the
canonical ledger and withholds anything the ledger cannot confirm.  What it deliberately does NOT
decide is SHAPE: whether established history should reach the actor as a JSON episode in the
system prompt, or as the role-tagged turns it actually was.  This module decides that, and it
decides it in the one direction the frozen formation interface supports.

WHY NATIVE ROLES, NOT A SYSTEM-MENTIONED EPISODE.  ``olmo_ws/data8b/TRAIN_CORPUS.jsonl`` is the
interface this actor was formed on, and it was formed on exactly one representation of earlier
conversation: native ``user``/``assistant`` messages, in order, unmarked -- 1,524 rows, zero rows
containing ``HIPPOCAMPAL_CONTEXT_V1``, zero rows containing ``historical_episode``.  The current
session's own dialogue window already works this way: ``session_dialogue_window`` builds
role-tagged pairs and ``insert_dialogue_window`` places them after any system message, and its
docstring states the law this module follows -- "Never pushes conversation history into the
system message as prose -- messages stay natively role-tagged."  Long-term retrieved history is
the same kind of thing and is projected the same way, so the actor reads it with the reading
competence it was actually formed on rather than through a scaffold it never saw.

THE HARD SEPARATION IS MECHANICAL, NOT RHETORICAL.  Nothing in a historical message claims to be
present, and nothing says so in prose.  Separation is enforced by facts a verifier can check
without reading any prose at all:

  1. every projected message is inserted after any system message and strictly before the present
     human message;
  2. the present human message is last and appears exactly once, byte-identical;
  3. the historical region is CONTIGUOUS -- a canonically linked host observation may occur between
     its request and reply, but no present-turn message can appear inside it;
  4. only ledger-verified human speech may occupy a ``user`` role, so a host-, agent- or
     inference-authored record can never be read as something the human said.

(4) is the defect of 2026-09-05, where host-authored JSON formatting text shipped inside a
user-role message and was read as the human's own speech.  ``HUMAN_SPEECH_ROLES`` in
``historical_unit_reconstruction`` is the mechanical gate, and ``verify_projection`` fails closed
if anything else ever reaches a user role.

RECORDS ARE NOT CONVERSATION.  A host record, a sleep derivation, an unresolved reply and an
unattributed record are never projected as turns -- not as a user turn, not as an assistant turn.
They are delivered through the ONE carrier the corpus already formed the actor to read: the
host-delivered result envelope, user-role, opening with "not the human's words", carrying the
existing ``actor_legibility.RELATION_LEGEND`` actor-equality vocabulary and the existing
``record_label`` class, and delimiting the exact stored words with ``<<<`` / ``>>>`` exactly as
the formed ``inspect_workspace`` results do.  No new vocabulary is introduced here.

WHO MAY OCCUPY A ``user`` ROLE.  Exactly one person: the receiving actor's own authenticated human
principal, named by canonical ``creator_actor_id`` AND named again by the caller as the human this
turn is actually addressed to.  Every other human utterance is demoted to a record carrier.  This
is not tidiness.  A native ``user`` role means "the person you are talking with"; a third party's
words placed there are the same defect as host-authored text in a user role, one layer down, and it
is the reason ``canonical_person.annotate_retrieved_items`` exists in the first place.  The
receiving actor's own prior reply is equally never a ``user`` turn -- it is an ``assistant`` turn,
or a record.

``viewer_human_actor_id=None`` is a real state, not an error: a Pulse or Caret turn has no human
being addressed.  History is then delivered entirely as records, because presenting a native
``user`` turn to nobody would claim a listener that does not exist.  The present message's own
role is the caller's business; this module only refuses to assert a listener of its own.

PROVENANCE IS A SIDECAR, NOT TEXT.  ``Projection.sidecar`` is the authoritative, host-side record
of what every message is: canonical event and component ids, structural role, actor relation,
content digest, truncation range, and every withheld item with its reason and the exact text that
was not delivered.  It is returned to the caller and never spliced into model-facing text, because
a digest in the prompt is not provenance -- it is a claim about provenance the model could not
check.  A record the ledger cannot confirm is withheld, and ``verify_projection`` proves its words
appear nowhere in the composed messages.

READ-ONLY AND PURE.  No model call, no write, no ledger access beyond the reconstruction result
already held.  This module reads its input and returns messages.
"""

import dataclasses
import hashlib

import actor_legibility
import epistemic_status
import historical_unit_reconstruction as hur

# Message kinds.  ``region_open``/``region_close`` are markers on the first and last message of
# the contiguous historical conversation region; they add no model-facing text of their own and
# exist so a verifier can check contiguity without parsing prose.
KIND_HISTORICAL_TURN = "historical_turn"
KIND_RECORD = "historical_record"
KIND_REGION_OPEN = "historical_region_open"
KIND_REGION_CLOSE = "historical_region_close"

REGION_KINDS = (KIND_HISTORICAL_TURN, KIND_REGION_OPEN, KIND_REGION_CLOSE)

# The envelope opening the corpus already formed for host-delivered results.  Quoted, not
# reworded: the point of matching the corpus exactly is that the actor has read this shape.
RECORD_ENVELOPE_HEADER = ("Result of a retrieved historical record ({label}; host-delivered read of your own "
                          "canonical history; read-only, it changed nothing; not the human's words):")
NONCANONICAL_RECORD_ENVELOPE_HEADER = (
    "Result of a retrieved historical source record ({label}; host-delivered read of verified "
    "noncanonical historical material; read-only, it changed nothing; not the human's words):")
AUTHENTICATED_STANDALONE_HUMAN_RECORD_HEADER = (
    "Result of a retrieved historical record ({label}; host-delivered read of your own canonical "
    "history; read-only, it changed nothing; not a present human message; the delimited source "
    "is authenticated historical human expression):")

RECORD_SOURCE_DELIMITER_OPEN = "<<<"
RECORD_SOURCE_DELIMITER_CLOSE = ">>>"
LEGACY_AUTHORSHIP_UNKNOWN = "UNKNOWN (not authenticated human speech; not Clark speech; not a host claim)"
PRE_AUTHENTICATION_AUTHORSHIP = (
    "NOT_ESTABLISHED (recorded before the authentication layer; not authenticated human speech, "
    "not Clark speech, and not a host claim)"
)

# Ordinary record carriers use the formed literal below. A standalone authenticated H needs the
# more precise second boundary: its delimited source really is historical human speech, but the
# carrier is not a present user message. ``verify_projection`` requires one of these boundaries.
NOT_THE_HUMANS_WORDS = "not the human's words"
NOT_A_PRESENT_HUMAN_MESSAGE = "not a present human message"

# A structural role's message role, and nothing else.  Records have no slot here at all.
_ROLE_MESSAGE_ROLE = {
    hur.ROLE_HUMAN_UTTERANCE: "user",
    hur.ROLE_RECEIVING_ACTOR_UTTERANCE: "assistant",
    hur.ROLE_TYPED_NO_REPLY: "assistant",
}


class HistoryProjectionError(RuntimeError):
    """A projection would have violated a hard separation or attribution law.  Raised, never
    warned: a historical region that cannot be shown safe to present is withheld whole."""


# The frozen provider protocol (olmo_ws/provider/protocol.py) delimits blocks with a role header
# at the start of a line: "\n\n" + ROLE + ":".  Historical content is stored text and may contain
# that exact sequence -- a past utterance that happened to say "SYSTEM:" on its own line.  Rendered
# verbatim it is byte-identical to a real role header, so the model cannot tell the historical text
# from a new block, and the provider's own cut_role_stop would end generation at it.  This is the
# mechanical historical/current separation: a role header inside historical content is neutralized
# before it can be read as structure, never left to prompt wording to disambiguate.
_PROTOCOL_ROLE_HEADERS = ("SYSTEM", "USER", "ASSISTANT", "TOOL")


def _neutralize_role_headers(text):
    """Historical content with any protocol role header made inert.

    A line that would render as a role header is prefixed with a zero-width space, which the
    protocol's own ``"\\n\\n" + ROLE + ":"`` search cannot match, so the text stays exactly as the
    human or the subject wrote it while no longer being readable as a block boundary.  The
    transformation is applied to historical content only; the live system/user/assistant messages
    the host composes are untouched."""
    if not text:
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        for role in _PROTOCOL_ROLE_HEADERS:
            if line.startswith(role + ":"):
                lines[i] = "\u200b" + line
                break
    return "\n".join(lines)


@dataclasses.dataclass(frozen=True)
class ProjectedMessage:
    role: str
    content: str
    kind: str
    unit_id: str
    structural_role: str
    turn_event_id: str = None

    def as_message(self):
        """The frozen provider protocol's own message shape -- role and content only.  Nothing
        here adds a field the protocol would have to learn."""
        return {"role": self.role, "content": self.content}


@dataclasses.dataclass(frozen=True)
class Projection:
    messages: tuple            # (ProjectedMessage, ...) in delivery order
    region: tuple              # (first_index, last_index) into `messages`, or None
    sidecar: dict              # authoritative host-side provenance; never model-facing text


def _turn_messages(reconstruction, viewer_human_actor_id):
    """The reconstructed conversation, as native role-tagged turns, plus the units demoted out of
    it.

    A human utterance becomes a ``user`` message ONLY when the canonical ``creator_actor_id`` is
    the viewer's own authenticated human principal; anyone else's words become a record carrier
    instead.  The receiving actor's utterance -- including a lawful typed ``no_reply``, which is a
    COMPLETE exchange with no words in it -- becomes the ``assistant`` message that follows it.
    ``_ROLE_MESSAGE_ROLE`` decides the message role and nothing else does, so a unit whose
    structural role has no conversation slot is never emitted here and cannot reach one by
    accident."""
    messages, demoted = [], []
    for turn_event_id, units in hur.turn_groups(reconstruction.units):
        # Native dialogue is a relationship, not one privileged side of it.  If the canonical
        # human is not the authenticated viewer (including an H-less occasion), demote the WHOLE
        # exchange to separately attributed records.  Leaving the subject reply native after
        # demoting its human half creates an assistant-first pseudo-continuation.
        if any(unit.role == hur.ROLE_HUMAN_UTTERANCE and (
                viewer_human_actor_id is None
                or unit.canonical.creator_actor_id != viewer_human_actor_id) for unit in units):
            demoted.extend(units)
            continue
        for unit in units:
            role = _ROLE_MESSAGE_ROLE.get(unit.role)
            if role is None:
                raise HistoryProjectionError(
                    f"unit {unit.unit_id} has structural role {unit.role!r}, which has no "
                    f"conversation slot, and was offered as a conversation turn")
            if not unit.canonical.verified:
                raise HistoryProjectionError(
                    f"unit {unit.unit_id} claims {'human' if role == 'user' else 'receiving actor'} "
                    f"speech without a verified canonical row")
            messages.append(ProjectedMessage(role, _neutralize_role_headers(unit.text),
                                             KIND_HISTORICAL_TURN,
                                             unit.unit_id, unit.role, turn_event_id))
    return messages, demoted


def _record_message(unit, demoted_from_conversation=False, person_annotation=None):
    """One host-delivered record carrier, in the formed envelope vocabulary."""
    if not unit.record_class:
        raise HistoryProjectionError(
            f"unit {unit.unit_id} is a record with no record class; a record must state what it is")
    canon = unit.canonical
    legacy_observation = canon.source_category == hur.SOURCE_CATEGORY_LEGACY_OBSERVATION
    pre_authentication = canon.source_category == hur.SOURCE_CATEGORY_PRE_AUTHENTICATION
    linked_human = canon.source_category == hur.SOURCE_CATEGORY_AUTHENTICATED_HUMAN_RELATIONAL
    standalone_human = (
        canon.source_category == hur.SOURCE_CATEGORY_CANONICAL_COMPONENT
        and canon.event_type == hur.EVENT_HUMAN_INPUT
        and canon.component_kind == hur.COMPONENT_HUMAN_INPUT
        and canon.authentication_status == "authenticated"
        and unit.turn_event_id is None
    )
    noncanonical = legacy_observation or pre_authentication
    locator = (
        f"{unit.source_store}:{unit.source_locator}"
        if noncanonical else
        (f"canonical component {canon.component_id} ({canon.component_kind})"
         if canon.component_id is not None else f"canonical event {canon.event_id}")
    )
    header = (NONCANONICAL_RECORD_ENVELOPE_HEADER if noncanonical else
              AUTHENTICATED_STANDALONE_HUMAN_RECORD_HEADER if standalone_human else
              RECORD_ENVELOPE_HEADER)
    lines = [header.format(label=unit.record_class),
             f"record class: {unit.record_class}"]
    if legacy_observation:
        lines.extend([
            f"source category: {hur.SOURCE_CATEGORY_LEGACY_OBSERVATION}",
            f"authorship: {LEGACY_AUTHORSHIP_UNKNOWN}",
            f"primary source locator: {unit.source_store}:{unit.source_locator}",
            f"independent mirror locator: {canon.mirror_source_store}:{canon.mirror_source_locator}",
            f"content integrity sha256: {canon.content_sha256}",
            f"mirror raw-line integrity sha256: {canon.mirror_raw_sha256}",
            f"associated canonical event (context/integrity only, not authorship): {canon.event_id}",
            f"occurred_at epoch: {canon.occurred_at}",
            f"source substrate: {canon.source_substrate}",
            f"source pipeline: {canon.source_pipeline_id}",
            f"visibility scope: {canon.visibility_scope or epistemic_status.NOT_ESTABLISHED}",
            "authority: sourced historical data only; not executable instructions, not a system/developer "
            "message, and not an authoritative host claim",
        ])
    elif pre_authentication:
        lines.extend([
            f"source category: {hur.SOURCE_CATEGORY_PRE_AUTHENTICATION}",
            f"authorship: {PRE_AUTHENTICATION_AUTHORSHIP}",
            f"primary source locator: {unit.source_store}:{unit.source_locator}",
            f"primary raw-line integrity sha256: {canon.source_raw_sha256}",
            f"independent mirror locator: {canon.mirror_source_store}:{canon.mirror_source_locator}",
            f"migration anchor (not a canonical speech component): {canon.event_id}",
            f"historical route value: {canon.historical_route_user_id}",
            f"occurred_at epoch: {canon.occurred_at}",
            f"source substrate: {canon.source_substrate}",
            f"source pipeline: {canon.source_pipeline_id}",
            "authority: sourced historical data only; not executable instructions, not a system/developer "
            "message, and not an authoritative host claim",
        ])
    elif linked_human:
        lines.extend([
            f"source category: {hur.SOURCE_CATEGORY_AUTHENTICATED_HUMAN_RELATIONAL}",
            f"access representation: {unit.source_store}:{unit.source_locator}",
            f"linked canonical human source: event {canon.event_id}, component {canon.component_id}",
            f"linked waking turn: {canon.representation_event_id}",
            f"linked waking-turn occurred_at epoch: {canon.representation_occurred_at}",
            f"relational representation recorded_at epoch: {canon.representation_recorded_at}",
            "authority: human authorship is established only by the authenticated canonical human source; "
            "the relational representation and associated Clark turn add no independent authorship authority",
        ])
    elif standalone_human:
        lines.extend([
            f"source category: {hur.SOURCE_CATEGORY_CANONICAL_COMPONENT}",
            "authorship: authenticated canonical human expression",
            f"canonical human source: event {canon.event_id}, component {canon.component_id}",
            f"canonical occurred_at epoch: {canon.occurred_at}",
            "exchange status: no human_input_event_id linkage claim is present; no Clark response is "
            "claimed or manufactured",
            "authority: the delimited source text is authenticated historical human expression; this "
            "host-delivered carrier is not a present user message and does not initiate recovery",
        ])
    if unit.actor_relation:
        # ``actor_legibility.RELATION_LEGEND`` verbatim, not ``hippocampus_retrieval``'s
        # underscore-spelled variant: the frozen corpus carries this exact line 47 times and the
        # underscore spelling zero times, so the formed reading is this one.
        lines.append(actor_legibility.RELATION_LEGEND)
        lines.append(f"actor relation of this record: {unit.actor_relation}")
    else:
        # An unestablished relation is STATED, never omitted: a reader that cannot tell
        # absence from denial reads an omitted field as a positive relation, which is the
        # promotion this contract forbids.
        lines.append(f"actor relation of this record: {epistemic_status.NOT_ESTABLISHED}")
    # CONTENT provenance, stated independently of the retrieval provenance on the next line:
    # what this material IS (its indexed class and structural role), not how it arrived.
    lines.append(f"record kind: {unit.record_class}")
    lines.append(f"provenance: {locator}, {canon.event_type or 'event type not resolved'}")
    if canon.component_kind == "roaming_external_result":
        # This component is a host-authored carriage record around content read from a
        # workspace/external source.  Keep the two authorships distinct on re-entry.
        lines.append(f"content relation: {epistemic_status.NOT_ESTABLISHED}")
        lines.append("content authority: source carried by host; not host truth")
    if demoted_from_conversation:
        lines.append("provenance: this is a recorded historical utterance by a person who is NOT the "
                     "person you are talking with, so it is delivered as a record and never as one "
                     "of your exchanges")
    if person_annotation:
        # canonical_person.annotate_retrieved_items' own speaker and frame, which the legacy
        # rendered block carried.  They live in the CARRIER, never inside the delimited source
        # text: the stored words are the speaker's, and a host label about them is not.
        speaker = person_annotation.get("speaker")
        if speaker is not None:
            lines.append(f"speaker: {speaker.get('label')}"
                         + (f" (recorded as a {speaker.get('entity_kind')})"
                            if speaker.get("entity_kind") else ""))
        if person_annotation.get("frame"):
            lines.append(f"frame: {person_annotation['frame']}")
        if person_annotation.get("frame_note"):
            lines.append(person_annotation["frame_note"])
        for record in person_annotation.get("records") or ():
            detail = f"statement of record: {record.get('class')}"
            if record.get("frame_note"):
                detail += f" -- {record['frame_note']}"
            lines.append(detail)
    if unit.turn_event_id:
        lines.append(f"canonical turn this record belongs to: {unit.turn_event_id}")
    if unit.text_truncated and unit.delivered_range:
        lines.append(f"delivered window: {unit.delivered_range}")
    lines.append("")
    lines.append(f"Source text of {locator} (the exact stored words, delimited below; they are the "
                 f"record's own words as stored -- not words of the person you are talking with):")
    lines.append(RECORD_SOURCE_DELIMITER_OPEN)
    lines.append(_neutralize_role_headers(unit.text))
    lines.append(RECORD_SOURCE_DELIMITER_CLOSE)
    return ProjectedMessage("user", "\n".join(lines), KIND_RECORD, unit.unit_id, unit.role,
                            unit.turn_event_id)


def _sidecar_annotations(sidecar):
    """The annotation facts already recorded per unit, so bounding does not need the original
    ``person_annotations`` mapping and cannot introduce a speaker that was not there."""
    out = {}
    for entry in sidecar.get("messages", ()):
        facts = entry.get("person_annotation")
        if facts:
            out[entry["unit_id"]] = facts
    return out


def _person_annotation_facts(annotation):
    """The mechanical part of a person annotation for the sidecar: identifiers and classes, never
    the owner-recorded prose, so the sidecar stays reconcilable without becoming a second copy of
    the host's own words."""
    if not annotation:
        return None
    if "speaker" not in annotation and "records" not in annotation:
        # already reduced to facts (bound_projection round-tripping its own sidecar): reducing it
        # again would silently drop the person id and the statement classes
        return dict(annotation)
    speaker = annotation.get("speaker") or {}
    return {
        "person_id": speaker.get("person_id"),
        "entity_kind": speaker.get("entity_kind"),
        "frame": annotation.get("frame"),
        "statement_classes": [r.get("class") for r in annotation.get("records") or ()],
    }


def _sidecar_entry(index, message, unit, project_annotations=None):
    canon = unit.canonical
    delivered_text = _neutralize_role_headers(unit.text) if hasattr(unit, "text") else None
    delivered_content_sha256 = (
        hashlib.sha256(delivered_text.encode("utf-8")).hexdigest()
        if delivered_text is not None else unit.delivered_content_sha256)
    projection_transform = (
        "protocol_role_header_neutralization" if delivered_text is not None
        and delivered_text != unit.text else getattr(unit, "projection_transform", None))
    return {
        "index": index,
        "kind": message.kind,
        "message_role": message.role,
        "structural_role": unit.role,
        "unit_id": message.unit_id,
        "item_id": unit.item_id,
        "source_store": unit.source_store,
        "source_locator": unit.source_locator,
        "turn_event_id": message.turn_event_id,
        "event_id": canon.event_id,
        "event_type": canon.event_type,
        "component_id": canon.component_id,
        "component_kind": canon.component_kind,
        "canonical_verified": bool(canon.verified),
        "creator_actor_id": canon.creator_actor_id,
        "creator_actor_type": canon.creator_actor_type,
        "authorship_resolution": canon.authorship_resolution,
        "actor_relation": unit.actor_relation,
        "record_class": unit.record_class,
        "content_sha256": canon.content_sha256,
        "delivered_content_sha256": delivered_content_sha256,
        "projection_transform": projection_transform,
        "occurred_at": canon.occurred_at,
        "session_id": canon.session_id,
        "visibility_scope": canon.visibility_scope,
        "source_category": canon.source_category,
        "authentication_status": canon.authentication_status,
        "mirror_source_store": canon.mirror_source_store,
        "mirror_source_locator": canon.mirror_source_locator,
        "mirror_raw_sha256": canon.mirror_raw_sha256,
        "source_substrate": canon.source_substrate,
        "source_pipeline_id": canon.source_pipeline_id,
        "representation_event_id": canon.representation_event_id,
        "representation_occurred_at": canon.representation_occurred_at,
        "representation_recorded_at": canon.representation_recorded_at,
        "source_raw_sha256": canon.source_raw_sha256,
        "historical_route_user_id": canon.historical_route_user_id,
        "relational_event_id": canon.relational_event_id,
        "imported_at": canon.imported_at,
        "selection_basis": unit.selection_basis,
        "repeat_count": unit.repeat_count,
        "companion_of_event_id": unit.companion_of_event_id,
        "text_truncated": bool(unit.text_truncated),
        "delivered_range": unit.delivered_range,
        "person_annotation": _person_annotation_facts(
            (project_annotations or {}).get(message.unit_id)),
    }


def _annotations_by_unit_id(reconstruction, person_annotations):
    """``{retrieved item_id: annotation}`` -> ``{unit_id: annotation}``, matched on the exact
    retrieved item id the reconstruction carried through.  An annotation whose item never became a
    unit has no unit to annotate and is dropped; it is never matched by proximity, by component id,
    or by text.  Two units may share one item id only if the ledger repeated it, and the first
    wins -- an ambiguous speaker must not be attributed to both."""
    if not person_annotations:
        return {}
    out = {}
    for unit in reconstruction.units:
        annotation = person_annotations.get(unit.item_id)
        if annotation is not None:
            out.setdefault(unit.unit_id, annotation)
    return out


def project(reconstruction, *, viewer_human_actor_id, person_annotations=None, max_records=None):
    """A ``ReconstructionResult`` -> ``Projection``.  Pure.

    Canonical units are emitted chronologically.  A host observation linked to a dialogue turn sits
    between its human request and subject reply; standalone records retain their own canonical time.
    ``max_records`` bounds record delivery, and every record it drops is named in the sidecar rather
    than silently disappearing.

    ``person_annotations`` is ``canonical_person.annotate_retrieved_items``' output, keyed by
    RETRIEVED ITEM id, and it rides the record carriers only.  It must not ride the conversation
    region: the region holds the speaker's own stored words with nothing between him and them, and
    a host label inside a ``user`` turn is host text in a human's mouth -- the same defect this
    module exists to prevent, one level in.  A person-attributed utterance that is not the
    viewer's own is delivered as a record, and that is exactly where the speaker needs naming."""
    turns, demoted = _turn_messages(reconstruction, viewer_human_actor_id)
    demoted_ids = {u.unit_id for u in demoted}
    all_records = list(hur.record_units(reconstruction.units)) + demoted
    records = all_records if max_records is None else all_records[:max_records]
    dropped = all_records[len(records):]

    annotations = _annotations_by_unit_id(reconstruction, person_annotations)
    record_messages = {
        unit.unit_id: _record_message(
            unit, unit.unit_id in demoted_ids, annotations.get(unit.unit_id))
        for unit in records
    }

    # Build chronological blocks.  A complete dialogue turn is one block; any host/resource
    # observation canonically linked to that turn sits between the human request and the subject
    # reply.  Standalone journals/reflections/records are their own blocks.  This preserves actual
    # causal order without flattening the host observation into either speaker's words.
    units_by_id = {u.unit_id: u for u in reconstruction.units}
    turns_by_event = {}
    for message in turns:
        turns_by_event.setdefault(message.turn_event_id, []).append(message)
    consumed_records = set()
    blocks = []
    for turn_event_id, turn_messages in turns_by_event.items():
        linked = [u for u in records if u.turn_event_id == turn_event_id
                  and u.unit_id not in demoted_ids]
        linked.sort(key=lambda u: (u.canonical.occurred_at or -1,
                                   u.canonical.sequence if u.canonical.sequence is not None else -1,
                                   u.unit_id))
        consumed_records.update(u.unit_id for u in linked)
        human = [m for m in turn_messages if m.structural_role == hur.ROLE_HUMAN_UTTERANCE]
        reply = [m for m in turn_messages if m.structural_role in hur.RECEIVING_ACTOR_SPEECH_ROLES]
        ordered = human + [record_messages[u.unit_id] for u in linked] + reply
        occurred = min((units_by_id[m.unit_id].canonical.occurred_at for m in ordered
                        if units_by_id[m.unit_id].canonical.occurred_at is not None), default=-1)
        blocks.append((occurred, turn_event_id, ordered))
    # A dialogue demoted because there is no authenticated matching listener is still one
    # historical unit.  Keep its human account, any linked host observation, and the subject's
    # reply together and in causal order; otherwise a budget cut can manufacture a one-sided
    # pseudo-history even though reconstruction established the whole unit.
    demoted_turns = {u.turn_event_id for u in demoted if u.turn_event_id is not None}
    for turn_event_id in sorted(demoted_turns):
        related = [u for u in records if u.turn_event_id == turn_event_id]
        human = [u for u in related if u.role == hur.ROLE_HUMAN_UTTERANCE]
        reply = [u for u in related if u.role in hur.RECEIVING_ACTOR_SPEECH_ROLES]
        host = [u for u in related if u not in human and u not in reply]
        host.sort(key=lambda u: (
            u.canonical.occurred_at if u.canonical.occurred_at is not None else -1,
            u.canonical.sequence if u.canonical.sequence is not None else -1, u.unit_id))
        ordered_units = human + host + reply
        consumed_records.update(u.unit_id for u in ordered_units)
        occurred = min((u.canonical.occurred_at for u in ordered_units
                        if u.canonical.occurred_at is not None), default=-1)
        blocks.append((occurred, turn_event_id,
                       [record_messages[u.unit_id] for u in ordered_units]))

    for unit in records:
        if unit.unit_id in consumed_records:
            continue
        blocks.append((unit.canonical.occurred_at if unit.canonical.occurred_at is not None else -1,
                       unit.canonical.event_id, [record_messages[unit.unit_id]]))
    blocks.sort(key=lambda block: (block[0], block[1]))
    messages = [message for _occurred, _identity, block in blocks for message in block]

    turn_indexes = [i for i, message in enumerate(messages) if message.kind == KIND_HISTORICAL_TURN]
    region = None
    if turn_indexes:
        first, last = turn_indexes[0], turn_indexes[-1]
        messages[first] = dataclasses.replace(messages[first], kind=KIND_REGION_OPEN)
        if last > first:
            messages[last] = dataclasses.replace(messages[last], kind=KIND_REGION_CLOSE)
        region = (first, last)

    units = {u.unit_id: u for u in reconstruction.units}
    sidecar = {
        "region": region,
        "record_bound": max_records,
        "viewer_human_actor_id": viewer_human_actor_id,
        "demoted_from_conversation": sorted(demoted_ids),
        "messages": [_sidecar_entry(i, m, units[m.unit_id], annotations) for i, m in enumerate(messages)],
        "withheld": [list(entry) for entry in reconstruction.withheld],
        "records_dropped_by_bound": [u.unit_id for u in dropped],
        "unverified_source_stores": list(reconstruction.unverified_source_stores),
        "duplicates_suppressed": [list(entry) for entry in reconstruction.duplicates_suppressed],
    }
    return Projection(tuple(messages), region, sidecar)


class _SidecarUnit:
    """Minimal unit view reconstructed from a sidecar entry, so bounding does not need the
    original reconstruction result and cannot re-derive anything it was not given."""

    def __init__(self, entry):
        self.unit_id = entry["unit_id"]
        self.item_id = entry["item_id"]
        self.source_store = entry["source_store"]
        self.source_locator = entry["source_locator"]
        self.turn_event_id = entry["turn_event_id"]
        self.record_class = entry.get("record_class")
        self.text_truncated = entry["text_truncated"]
        self.delivered_range = entry.get("delivered_range")
        self.actor_relation = entry["actor_relation"]
        self.canonical = _SidecarCanonical(entry)
        self.role = entry["structural_role"]
        self.selection_basis = entry.get("selection_basis")
        self.repeat_count = entry.get("repeat_count", 1)
        self.occurred_at = entry.get("occurred_at")
        self.companion_of_event_id = entry.get("companion_of_event_id")
        self.delivered_content_sha256 = entry.get("delivered_content_sha256")
        self.projection_transform = entry.get("projection_transform")


class _SidecarCanonical:
    def __init__(self, entry):
        self.event_id = entry["event_id"]
        self.event_type = entry["event_type"]
        self.component_id = entry["component_id"]
        self.component_kind = entry["component_kind"]
        self.creator_actor_id = entry.get("creator_actor_id")
        self.creator_actor_type = entry.get("creator_actor_type")
        self.authorship_resolution = entry.get("authorship_resolution")
        self.content_sha256 = entry["content_sha256"]
        self.occurred_at = entry.get("occurred_at")
        self.session_id = entry.get("session_id")
        self.visibility_scope = entry.get("visibility_scope")
        self.source_category = entry.get("source_category", hur.SOURCE_CATEGORY_CANONICAL_COMPONENT)
        self.authentication_status = entry.get("authentication_status")
        self.mirror_source_store = entry.get("mirror_source_store")
        self.mirror_source_locator = entry.get("mirror_source_locator")
        self.mirror_raw_sha256 = entry.get("mirror_raw_sha256")
        self.source_substrate = entry.get("source_substrate")
        self.source_pipeline_id = entry.get("source_pipeline_id")
        self.representation_event_id = entry.get("representation_event_id")
        self.representation_occurred_at = entry.get("representation_occurred_at")
        self.representation_recorded_at = entry.get("representation_recorded_at")
        self.source_raw_sha256 = entry.get("source_raw_sha256")
        self.historical_route_user_id = entry.get("historical_route_user_id")
        self.relational_event_id = entry.get("relational_event_id")
        self.imported_at = entry.get("imported_at")
        self.component_text = None
        self.verified = entry["canonical_verified"]


def native_history_text(projection):
    """The exact serialized history segment the frozen provider receives.

    The compositor prices a contribution on ``rendered_text``; the seam then sends the native
    role-tagged messages instead.  Those are two different byte counts, so pricing the legacy
    rendered block and sending the native form makes the budget decision and the delivered prompt
    disagree -- the native form is larger, and ``bound_projection`` would then drop history the
    compositor had already admitted.  This renders the native form in the SAME unit
    ``context_budget.estimate_tokens`` measures (UTF-8 bytes of the serialized messages), so a
    contribution priced on it and a projection bounded to that price are the same quantity.  Role
    headers and message terminators are included: joining message bodies alone is not provider
    serialization and under-prices every message boundary."""
    role_headers = {"system": "SYSTEM", "user": "USER", "assistant": "ASSISTANT", "tool": "TOOL"}
    return "".join(f"{role_headers[m.role]}:\n{m.content}\n\n" for m in projection.messages)


def bound_projection(projection, max_chars, *, cost_fn=None):
    """``Projection`` -> a projection whose messages fit ``max_chars``, or the input unchanged.

    Trim order, and why this order: the OLDEST turn groups go first, newest exchange preserved,
    and only then records.  It mirrors ``hippocampus_retrieval.delivery_units``' own discipline --
    a selected episode must not lose its content to weaker standalone matches -- and it never
    drops a reply while keeping the human utterance it answers, because a turn group is dropped
    whole or not at all.  Every drop is named in the sidecar.

    Bounding is not a licence to exceed the room.  The budget was measured against the rendered
    block this projection replaces, and the native form is a different size, so the caller's
    allowance is enforced here rather than assumed.

    ``cost_fn`` is the SAME cost function the compositor priced the contribution with, when it has
    one.  A contribution re-costed by real provider measurement carries a token count, not a byte
    count; bounding that token allowance against UTF-8 bytes would compare two different units and
    drop history the compositor had already admitted.  Passing the contribution's own ``cost_fn``
    keeps the bound in the unit the allowance was actually expressed in.  With no ``cost_fn`` the
    conservative byte estimator is used, which is the safe direction."""
    def _cost(messages):
        # UTF-8 bytes, matching context_budget.estimate_tokens()'s own conservative byte-count
        # unit, so a bound derived from an accounted contribution cost is the same unit the
        # compositor measured.
        if cost_fn is not None:
            role_headers = {"system": "SYSTEM", "user": "USER", "assistant": "ASSISTANT", "tool": "TOOL"}
            return cost_fn("".join(
                f"{role_headers[m.role]}:\n{m.content}\n\n" for m in messages))
        role_headers = {"system": "SYSTEM", "user": "USER", "assistant": "ASSISTANT", "tool": "TOOL"}
        return len("".join(
            f"{role_headers[m.role]}:\n{m.content}\n\n" for m in messages).encode("utf-8"))

    if _cost(projection.messages) <= max_chars:
        # Already inside the room, but the accounting is still recorded: a reader of the sidecar
        # must be able to tell that a bound was applied and that nothing had to go, rather than
        # inferring it from the absence of a field.
        sidecar = dict(projection.sidecar)
        sidecar["char_budget"] = max_chars
        sidecar["char_total"] = _cost(projection.messages)
        sidecar.setdefault("records_dropped_by_bound", [])
        sidecar.setdefault("turns_dropped_by_bound", [])
        return Projection(projection.messages, projection.region, sidecar)

    # Whole canonical turn blocks (including their linked host records) are indivisible.  Every
    # standalone record is its own block.  Drop the oldest block first; never reorder survivors.
    blocks, current, current_turn = [], [], None
    for message in projection.messages:
        block_turn = message.turn_event_id
        if current and (block_turn != current_turn or block_turn is None):
            blocks.append(current)
            current = []
        current.append(message)
        current_turn = block_turn
        if block_turn is None:
            blocks.append(current)
            current, current_turn = [], None
    if current:
        blocks.append(current)

    dropped_turns, dropped_records = [], []
    while blocks and _cost([m for group in blocks for m in group]) > max_chars:
        dropped = blocks.pop(0)
        if any(m.kind in REGION_KINDS for m in dropped):
            dropped_turns.append(dropped)
        else:
            dropped_records.extend(dropped)

    messages = [m for group in blocks for m in group]
    # Markers describe the surviving region, not the pre-bound region.  If the oldest historical
    # turn was removed, its old OPEN marker must not make the new first turn look unmarked.
    messages = [dataclasses.replace(m, kind=KIND_HISTORICAL_TURN)
                if m.kind in REGION_KINDS else m for m in messages]
    turn_indexes = [i for i, message in enumerate(messages)
                    if message.kind == KIND_HISTORICAL_TURN]
    new_region = (turn_indexes[0], turn_indexes[-1]) if turn_indexes else None
    if new_region is not None:
        first, last = new_region
        messages[first] = dataclasses.replace(messages[first], kind=KIND_REGION_OPEN)
        if last > first:
            messages[last] = dataclasses.replace(messages[last], kind=KIND_REGION_CLOSE)
    messages = tuple(messages)

    units = {entry["unit_id"]: _SidecarUnit(entry) for entry in projection.sidecar["messages"]}
    sidecar = dict(projection.sidecar)
    sidecar["messages"] = [_sidecar_entry(i, m, units[m.unit_id],
                                          _sidecar_annotations(projection.sidecar))
                          for i, m in enumerate(messages)]
    sidecar["region"] = new_region
    sidecar["records_dropped_by_bound"] = list(sidecar.get("records_dropped_by_bound", ())) \
        + [m.unit_id for m in dropped_records]
    sidecar["turns_dropped_by_bound"] = [m.unit_id for group in dropped_turns for m in group]
    sidecar["char_budget"] = max_chars
    sidecar["char_total"] = _cost(messages)
    return Projection(messages, new_region, sidecar)


def insert_history(prepared_messages, projection):
    """Pure. ``prepared_messages`` -> messages with the historical region and record carriers
    inserted after any leading system message and before everything else, so the present human
    message stays last and byte-identical.  Never mutates its arguments; never touches the system
    message."""
    payload = [m.as_message() for m in projection.messages]
    if prepared_messages and prepared_messages[0].get("role") == "system":
        return [prepared_messages[0]] + payload + list(prepared_messages[1:])
    return payload + list(prepared_messages)


def verify_projection(projection, composed_messages, present_message):
    """Composed messages -> True, or raises ``HistoryProjectionError``.

    Every law below is checked mechanically on the composed list; none requires reading prose, and
    none can be satisfied by a claim the projection makes about itself.

    1. the present human message is last, is a ``user`` message, and appears exactly once,
       byte-identical;
    2. every projected message sits immediately after the system message, in projection order,
       byte-identical;
    3. the historical region has valid open/close markers (linked records may sit inside it);
    4. each region's message role is the one its structural role permits;
    5. every record carrier is a ``user`` message that states it is not the human's words;
    6. no withheld item's text appears anywhere in the composed messages.
    """
    if not composed_messages:
        raise HistoryProjectionError("composed messages are empty")
    if composed_messages[-1].get("role") != "user":
        raise HistoryProjectionError("the last message is not the present human message")
    if composed_messages[-1].get("content") != present_message.get("content"):
        raise HistoryProjectionError("the present human message was altered")
    if sum(1 for m in composed_messages
           if m.get("role") == "user" and m.get("content") == present_message.get("content")) != 1:
        raise HistoryProjectionError("the present human message does not appear exactly once")

    payload = [m.as_message() for m in projection.messages]
    head = len(composed_messages) - len(payload) - 1
    if head < 0:
        raise HistoryProjectionError("composed messages are shorter than system + history + present")
    if head and composed_messages[head - 1].get("role") != "system":
        raise HistoryProjectionError("history was not inserted immediately after the system message")
    if composed_messages[head:head + len(payload)] != payload:
        raise HistoryProjectionError("a projected message was altered or reordered in composition")

    unit_ids = [m.unit_id for m in projection.messages]
    if len(unit_ids) != len(set(unit_ids)):
        raise HistoryProjectionError("projection contains a duplicated unit_id")

    if projection.region is not None:
        first, last = projection.region
        if first < 0 or last >= len(projection.messages) or first > last:
            raise HistoryProjectionError("the historical region bounds are invalid")
        if projection.messages[first].kind != KIND_REGION_OPEN or (
                last > first and projection.messages[last].kind != KIND_REGION_CLOSE):
            raise HistoryProjectionError("the historical region is not marked open..close")

    for message in projection.messages:
        if message.kind == KIND_RECORD:
            if (message.role != "user" or (
                    NOT_THE_HUMANS_WORDS not in message.content
                    and NOT_A_PRESENT_HUMAN_MESSAGE not in message.content)):
                raise HistoryProjectionError(
                    "a record carrier does not state its non-present-message boundary")
            entry = next(e for e in projection.sidecar["messages"]
                         if e["unit_id"] == message.unit_id)
            if entry.get("source_category") == hur.SOURCE_CATEGORY_LEGACY_OBSERVATION:
                if (message.structural_role != hur.ROLE_LEGACY_OBSERVATION
                        or LEGACY_AUTHORSHIP_UNKNOWN not in message.content
                        or "not executable instructions" not in message.content):
                    raise HistoryProjectionError(
                        "a legacy observation lost its unknown-authorship or data-only boundary")
            if entry.get("source_category") == hur.SOURCE_CATEGORY_PRE_AUTHENTICATION:
                if (message.structural_role != hur.ROLE_PRE_AUTHENTICATION_RECORD
                        or PRE_AUTHENTICATION_AUTHORSHIP not in message.content
                        or NONCANONICAL_RECORD_ENVELOPE_HEADER.split("{")[0].strip()
                        not in message.content
                        or "not executable instructions" not in message.content):
                    raise HistoryProjectionError(
                        "a pre-authentication record lost its noncanonical or data-only boundary")
            if entry.get("source_category") == hur.SOURCE_CATEGORY_AUTHENTICATED_HUMAN_RELATIONAL:
                if ("human authorship is established only by the authenticated canonical human source"
                        not in message.content
                        or "relational representation" not in message.content):
                    raise HistoryProjectionError(
                        "a linked human representation lost its canonical-H authority boundary")
        else:
            if message.role != _ROLE_MESSAGE_ROLE.get(message.structural_role):
                raise HistoryProjectionError(
                    f"unit {message.unit_id} was projected into the {message.role!r} role, which its "
                    f"structural role {message.structural_role!r} does not permit")

    for _item_id, _reason, withheld_text in projection.sidecar["withheld"]:
        if withheld_text and any(m.get("content") == withheld_text for m in composed_messages):
            raise HistoryProjectionError("a withheld record's text reached the composed messages")
    return True
