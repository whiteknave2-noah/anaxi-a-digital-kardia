"""The ONE shared epistemic-status contract for every model-facing information path.

WHY THIS MODULE EXISTS
----------------------
ANAXI has many independent seams that turn host-held records into text the waking
substrate reads: hippocampal retrieval, the relational history projection, historical
continuity search/read, workspace observations, external-information delivery, action
verification receipts, the session dialogue window, consolidation evidence, Discord
inbound carriage, and Pulse occasions.  Each seam grew its own vocabulary for saying
what a piece of material IS.  Two consequences followed, both observed live:

  * the same mechanical fact was spelled differently on different paths, so a reader
    could not carry one reading across them; and
  * some paths stated a status the host had not actually established -- most often by
    omitting the fact that a status was unknown, which reads exactly like a positive
    status to a reader that has no way to tell absence from denial.

This module is the single place those facts are named.  It does not replace the
existing renderers, and it does not introduce a second taxonomy: every constant here
is either the literal already carried in the frozen corpus or the explicit spelling of
a fact those renderers already compute.  The renderers import from here so they cannot
drift from one another.

THE FOUR FACTS
--------------
Every model-facing carrier can state four independent things, and the contract keeps
them independent -- collapsing any two of them is the defect this module exists to
prevent:

  1. RETRIEVAL provenance -- HOW this material arrived (which store, which locator,
     which retrieval method).  This says nothing about what the material is.
  2. CONTENT provenance -- WHAT the material originally is (whose expression, which
     record class, which structural role).  This says nothing about how it arrived.
  3. COMPLETENESS -- whether what is shown is the whole of what was retrieved, or a
     bounded window of it.  A partial window is not a shorter complete thing.
  4. ACTOR RELATION -- whose material this is, stated as canonical actor EQUALITY with
     the actor receiving the context, never as a name or a pronoun.

THE TWO LAWS
------------
NOT_ESTABLISHED.  A fact the host has not mechanically established is stated as
``NOT_ESTABLISHED``.  It is never left blank, never defaulted to a positive value, and
never inferred from a display label, a current owner, or the shape of the material.
Absence of evidence is rendered as absence of evidence.

NO GENERIC PROMOTION.  No caller-supplied string, and no text the model itself
produced, may set a host-authoritative status.  Only the path-specific verifier that
understands the relevant canonical schema can establish a positive fact.  This module
therefore deliberately exposes no generic ``promote(value, evidence)`` helper: a
truthy dict is not proof, and pretending that one generic function could validate a
component row, a receipt, a retrieval result, and a delivery ledger would create a
promotion bypass rather than close one.
"""

from provenance_schema import derive_stable_id

# ---------------------------------------------------------------------------
# NOT_ESTABLISHED: the one spelling of "the host has not established this".
# ---------------------------------------------------------------------------
# The frozen corpus already carries this literal in historical_continuity_access'
# render_search/render_read (historical_route_user_id=NOT_ESTABLISHED).  It is the
# established spelling, so it is the one used everywhere rather than a new synonym.
NOT_ESTABLISHED = "NOT_ESTABLISHED"

# A fact that does not apply to this carrier at all -- distinct from NOT_ESTABLISHED,
# which means "it would apply, but the host did not establish it".  A legacy record
# with no canonical component has no structural role to state; that is NOT_APPLICABLE,
# not an unknown.
NOT_APPLICABLE = "NOT_APPLICABLE"

# A canonical component was expected but the record is not one (e.g. a legacy-log item
# that predates the canonical ledger).  Distinct from NOT_APPLICABLE: the question of a
# structural role is well-formed here, and the answer is that there is no canonical
# component to carry one.
NOT_A_CANONICAL_COMPONENT = "NOT_A_CANONICAL_COMPONENT"

# ---------------------------------------------------------------------------
# ACTOR RELATION: perspective-invariant, canonical-actor-equality only.
# ---------------------------------------------------------------------------
# Delegated to actor_legibility, which already owns this vocabulary and the legend
# that explains it.  Re-exported so a renderer has one import for the whole contract
# and cannot fork the spelling.
RECEIVING_ACTOR_ID = derive_stable_id("actor", "clark")
SAME_AS_RECEIVING_ACTOR = "SAME_AS_RECEIVING_ACTOR"
HUMAN_INTERLOCUTOR = "HUMAN_INTERLOCUTOR"
HOST = "HOST"

# ---------------------------------------------------------------------------
# COMPLETENESS: whether the shown material is the whole retrieved thing.
# ---------------------------------------------------------------------------
# These are the literals the existing renderers already use; naming them here keeps a
# reader from having to learn one spelling per path.
COMPLETE = "complete"
TRUNCATED = "truncated"
PARTIAL = "partial"
OMITTED = "omitted"

# ---------------------------------------------------------------------------
# CONTENT CLASS: what the material originally is, in the corpus' own vocabulary.
# ---------------------------------------------------------------------------
# These are hippocampus_retrieval.record_label()'s memory kinds, which the frozen
# corpus already carries.  Named here so every path that must state a content class
# states the same one.
KIND_CLARK_EXPRESSION = "clark_expression"
KIND_HUMAN_EXPRESSION = "human_expression"
KIND_MECHANICAL_RECORD = "mechanical_record"
KIND_MIXED_EXPRESSION = "mixed_historical_expression"
KIND_DERIVED_INFERENCE = "derived_inference"

# The literal every host-delivered carrier uses to say it is not the human's speech.
# relational_history_projection.verify_projection() requires it on record carriers, so
# it is a hard contract, not a convention.
NOT_THE_HUMANS_WORDS = "not the human's words"

def actor_relation_of(creator_actor_id, memory_kind, *, receiving_actor_id=RECEIVING_ACTOR_ID):
    """The canonical actor-equality relation for one record, or NOT_ESTABLISHED.

    Only mechanically established relations are stated: the creator is the receiving
    actor (SAME_AS_RECEIVING_ACTOR), the record is a human expression (HUMAN_INTERLOCUTOR),
    or the record is a host mechanical record (HOST).  Anything else is NOT_ESTABLISHED
    -- never guessed from a display label or a current owner.
    """
    if creator_actor_id is not None and creator_actor_id == receiving_actor_id:
        return SAME_AS_RECEIVING_ACTOR
    # ``human_expression`` without a canonical creator is common on legacy relational
    # and JSONL routes.  The class says what the index called the text; it does not
    # establish a modern actor relation.  Explicit historical continuity already says
    # NOT_ESTABLISHED for those records, and automatic retrieval must not promote them.
    if creator_actor_id is not None and memory_kind == KIND_HUMAN_EXPRESSION:
        return HUMAN_INTERLOCUTOR
    if creator_actor_id is not None and memory_kind == KIND_MECHANICAL_RECORD:
        return HOST
    return NOT_ESTABLISHED


def completeness(*, shown, total):
    """COMPLETE when everything retrieved is shown, TRUNCATED when a bounded window is.

    ``shown`` and ``total`` are character counts of the delivered and retrieved text.
    A caller that cannot state both gets NOT_ESTABLISHED rather than an assumed
    COMPLETE: an unstated completeness reads as complete to a reader, which is exactly
    the promotion this contract forbids.
    """
    if (type(shown) is not int or type(total) is not int
            or shown < 0 or total < 0 or shown > total):
        return NOT_ESTABLISHED
    return COMPLETE if shown >= total else TRUNCATED
