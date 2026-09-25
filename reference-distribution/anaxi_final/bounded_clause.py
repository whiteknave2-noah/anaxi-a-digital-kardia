"""
Anaxi -- Phase 1 bounded acknowledgment rendering. Implements the
approved constrained-acknowledgment contract: the host renders the
entire fact-bearing clause deterministically, from operation_status
alone. Clark generates none of its text -- his own generation call
receives the already-rendered clause as read-only context, and the
host concatenates his separately-generated prose after it. Clark
never splices, completes, or rewrites the clause itself.

One deterministic clause per state for Phase 1 -- no aesthetic or
model-selected template variation yet, per explicit design decision.

Does not alter the existing gate or construction logic in any way --
this is purely an interpretation/rendering layer sitting on top of
their already-established, already-tested output.
"""

BOUNDED_CLAUSES = {
    "not_authorized": "No new long-term memory entry was created from that.",
    "insufficient_information": "There wasn't enough information to identify what to save.",
    "failed": "That wasn't saved.",
    "stale_schema": "A new long-term memory entry could not be created from that.",
    "host_uncertain": "The save status is uncertain.",
    "success": "Saved: {artifact_title}.",
}


def map_to_operation_status(signal_category: str, artifact_result: dict) -> str:
    """Maps the EXISTING gate/construction result onto one of the six
    approved operation_status values, without changing the underlying
    gate or construction logic at all -- purely an interpretation
    layer for clause rendering.

    Confirmed against the real, current code (per CC's direct trace):
    stale_schema is distinguishable from insufficient_information only
    if the StaleSchemaError handler in run_waking_turn() is updated to
    set detail.status = "stale_schema" specifically, rather than
    reusing "insufficient_information" as it did before this contract.
    That is the one small, targeted change this implementation
    requires in llama_anaxi.py -- see that file's updated except block."""
    if signal_category != "POSITIVE":
        return "not_authorized"

    detail = artifact_result.get("detail", {})
    status = detail.get("status")

    if status == "stale_schema":
        return "stale_schema"
    if status == "insufficient_information":
        return "insufficient_information"
    if status == "failed":
        return "failed"
    if artifact_result.get("artifact_created"):
        return "success"

    return "host_uncertain"


def render_bounded_clause(operation_status: str, artifact_title: str = None) -> str:
    """Pure, deterministic rendering -- no model call, no randomness.
    Returns the exact, complete, host-authenticated clause for the
    given state."""
    template = BOUNDED_CLAUSES.get(operation_status)
    if template is None:
        return BOUNDED_CLAUSES["host_uncertain"]
    if operation_status == "success":
        return template.format(artifact_title=artifact_title or "your note")
    return template
