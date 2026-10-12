"""Positive retrieval-delivery provenance (continuity meaningful-access repair, 2026-10-03).

Live gap: a waking turn's Pass 2 carried `retrieved_history`, but which retrieved units actually reached the model was
persisted nowhere, so afterwards no one could establish whether a given historical item had been delivered.

This module only ACCOUNTS for delivery; it never changes what is retrieved, ranked, admitted or trimmed.  For each model
call that actually ran with a RETRIEVED_HISTORY contribution, the caller notes -- after the call returned -- the
candidates offered to composition and the contribution that survived it.  The manifest is staged with the turn
(native_provenance_writer post_commit_provenance) and recorded beside it, exactly once, when the turn is canonical --
on the live commit or by crash replay (workspace_episode_provenance.record_turn_delivery_provenance).

The manifest holds mechanical facts only: item / source event ids, provenance class, and SHA-256 digests of exactly
what was delivered (each unit's rendered line and content, and the whole delivered block).  Candidates trimmed by the
budget are listed with genuinely_delivered=false.  No retrieved text is copied, no reasoning, no summary.  Calls with
no retrieved history (e.g. ordinary Pass 1) note nothing.  Per-thread, like ui_turn_diagnostics, so a Pulse or Caret
occasion on another thread never mixes with this one."""
import hashlib
import threading

import hippocampus_retrieval

_state = threading.local()


def reset():
    """Start of one waking turn: nothing has been delivered yet."""
    _state.deliveries = []
    _state.native_delivered = None
    _state.native_block = None
    _state.native_messages = None


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def note_native_delivery(delivered_item_ids, *, block=None, messages_by_item=None):
    """The relational-history seam reports the items it ACTUALLY delivered to the provider.

    The compositor decides which units fit the budget; the seam then re-derives each one from the
    canonical ledger and withholds any it cannot confirm.  A unit can therefore survive composition
    and still never reach the model.  The manifest must state what the model saw, not what the
    budget admitted, so the seam records its own delivered set here and ``note`` prefers it.

    ``block`` is the exact serialized native history segment, including role headers and message
    boundaries. ``messages_by_item`` maps each delivered item to its exact projected message body,
    role, and order.  The legacy block and joined bodies are not provider serialization."""
    _state.native_delivered = set(delivered_item_ids or ())
    _state.native_block = block
    _state.native_messages = dict(messages_by_item or {})


def provider_boundary_evidence(candidates, delivered_contribution):
    """Describe every retrieval candidate for the actual provider seam.

    Exact projected text is transient: provider_boundary_receipt removes it
    after checking the final call argument.  Dropped/withheld candidates retain
    source identity and a mechanical disposition but can never be upgraded to
    delivery merely because the compositor admitted them.
    """
    candidates = list(candidates or ())
    delivered_items = (
        hippocampus_retrieval.delivered_items(delivered_contribution.droppable_units or [])
        if delivered_contribution is not None else []
    )
    composition_ids = {item.item_id for item in delivered_items}
    native_ids = set(getattr(_state, "native_delivered", None) or ())
    native_messages = dict(getattr(_state, "native_messages", None) or {})
    evidence = []
    for item in candidates:
        record = {
            "evidence_kind": "automatic_hippocampal",
            "evidence_id": item.item_id,
            "source_event_id": item.event_id,
            "source_store": item.source_store,
            "source_locator": item.source_locator,
            "canonical_component_kind": item.component_kind,
            "memory_kind": item.memory_kind,
            "authoritative_content_sha256": item.content_sha256,
            "source_chars": item.source_chars,
            "retrieved_range": [0, len(item.content or "")],
            "retrieved_content_truncated": bool(item.content_truncated),
            "expected_delivery": item.item_id in native_ids,
        }
        if item.item_id in native_ids:
            message = native_messages.get(item.item_id)
            if message is None:
                raise ValueError("native-delivered hippocampal item has no projected provider message")
            record.update(
                exact_text=message["content"],
                expected_role=message["role"],
                disposition="retained_for_provider_boundary",
            )
        elif item.selection_basis == hippocampus_retrieval.SELECTION_EXACT_ECHO:
            record["disposition"] = hippocampus_retrieval.SELECTION_EXACT_ECHO
        elif item.item_id in composition_ids:
            record["disposition"] = "withheld_by_canonical_reconstruction_or_native_bound"
        else:
            record["disposition"] = "dropped_before_provider_boundary"
        evidence.append(record)
    return evidence



def note(call, candidates, delivered_contribution, *, query_terms=(), person_annotations=None):
    """Record that model call `call` ran.  `candidates`: the retrieved items offered to composition (any order);
    `delivered_contribution`: the RETRIEVED_HISTORY contribution the composed prompt actually carried (None when the
    kind was dropped).  Only items still in its droppable_units -- the ones rendered into the prompt -- count as
    delivered."""
    if not candidates:
        return
    delivered_items = []
    block = None
    if delivered_contribution is not None and delivered_contribution.rendered_text:
        # Exactly the items the surviving units render (an episode reply possibly truncated within its unit).
        delivered_items = hippocampus_retrieval.delivered_items(delivered_contribution.droppable_units or [])
        block = delivered_contribution.rendered_text
    delivered = {item.item_id: item for item in delivered_items}
    # The seam's own report wins when it ran: it re-derives every unit from the canonical ledger and
    # withholds what it cannot confirm, so a unit that survived composition may still never have
    # reached the model.  The manifest states what the model saw.
    native_delivered = getattr(_state, "native_delivered", None)
    if native_delivered is not None:
        delivered = {k: v for k, v in delivered.items() if k in native_delivered}
        # The native form is what the provider received, so the block and per-item lines the
        # manifest digests must be the native ones -- the legacy rendered block is no longer sent.
        block = getattr(_state, "native_block", None)
    # Native delivery records the exact projected message body for each item.  ``line_sha256`` is
    # retained as the durable legacy field name, but on the native path it hashes that whole body;
    # the explicit message_* fields below state the representation without ambiguity.
    native_messages = getattr(_state, "native_messages", None)
    if native_messages is not None:
        shown_lines = {item_id: facts["content"] for item_id, facts in native_messages.items()}
    else:
        shown_lines = hippocampus_retrieval.rendered_lines_by_item(
            hippocampus_retrieval.RetrievalResult(query_terms=tuple(query_terms or ()), items=tuple(delivered_items)),
            person_annotations=person_annotations) if delivered_items else {}
    units = []
    for item in candidates:
        unit = {
            "item_id": item.item_id, "event_id": item.event_id, "source_store": item.source_store,
            "source_locator": item.source_locator, "component_kind": item.component_kind,
            "memory_kind": item.memory_kind, "genuinely_delivered": item.item_id in delivered,
        }
        if unit["genuinely_delivered"]:
            shown = delivered[item.item_id]
            line = shown_lines[item.item_id]
            unit.update(content_sha256=_sha(shown.content or ""), content_chars=len(shown.content or ""),
                        content_truncated=bool(shown.content_truncated), line_sha256=_sha(line),
                        line_in_block=(line in block))
            if native_messages is not None:
                facts = native_messages[item.item_id]
                unit.update(message_role=facts["role"], message_index=facts["index"],
                            message_chars=len(facts["content"]), message_sha256=_sha(facts["content"]),
                            message_in_block=(facts["content"] in block))
        if item.companion_of_event_id is not None:
            unit["companion_of_event_id"] = item.companion_of_event_id
        if item.selection_basis == hippocampus_retrieval.SELECTION_EXACT_ECHO:
            unit["withheld"] = hippocampus_retrieval.SELECTION_EXACT_ECHO   # byte-identical to the present message
        units.append(unit)
    record = {"call": call, "candidates": len(units), "delivered": len(delivered), "units": units}
    if block is not None:
        record.update(block_sha256=_sha(block), block_chars=len(block))
    if not hasattr(_state, "deliveries"):
        _state.deliveries = []
    _state.deliveries.append(record)


def describe(contribution, candidates, *, query_terms=(), person_annotations=None):
    """Attach to a built RETRIEVED_HISTORY contribution what was offered to composition (its own droppable_units are
    trimmed in place by compose_within_budget, so the candidate list is kept beside it)."""
    contribution.retrieval_candidates = list(candidates or [])
    contribution.retrieval_query_terms = tuple(query_terms or ())
    contribution.retrieval_person_annotations = person_annotations
    return contribution


def note_composed(call, described_contribution, composition_result):
    """`call` ran with `composition_result`'s prompt: note the RETRIEVED_HISTORY contribution it actually included."""
    import context_budget
    if described_contribution is None:
        return
    note(call, getattr(described_contribution, "retrieval_candidates", None),
         composition_result.included_kind(context_budget.RETRIEVED_HISTORY) if composition_result is not None else None,
         query_terms=getattr(described_contribution, "retrieval_query_terms", ()),
         person_annotations=getattr(described_contribution, "retrieval_person_annotations", None))


def take_manifest():
    """Just before the turn is staged: this turn's manifest ({"calls": [...]}), or None when no model call carried
    retrieved history; then clear, so it cannot be staged twice.  It is staged with the turn and recorded by the
    writer once the turn is canonical -- on the live commit and, after a crash, by staged replay."""
    deliveries = list(getattr(_state, "deliveries", []) or [])
    _state.deliveries = []
    return {"calls": deliveries} if deliveries else None
