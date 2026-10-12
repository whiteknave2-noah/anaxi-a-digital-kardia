"""Test support for compact Pass-1 control (post-deploy no-reply incident, 2026-10-07).

A scripted fake model writes its whole intended act at once.  Under the compact control protocol the
host asks for that act in parts: the closed control choice (a Pass-1 call), then one call per bound
SELECTION (a single-field closed JSON grammar) and per PAYLOAD (a single-field free-string JSON
grammar, authored atomically in one call after a capacity preflight).  ``binding_response`` answers
selections and payloads from the scripted intention, so a fake needs one line to speak the protocol.  No production code imports this module.
"""
import json

SELECTION_FIELDS = frozenset({
    "discord_destination_id", "discord_correspondent_ref",
    "consolidation_target", "consolidation_prior_record_id",
})
PAYLOAD_FIELDS = frozenset({
    "thread", "historical_query", "action_verification_target", "operative_directive_text",
    "external_info_target", "discord_message_text", "consolidation_interpretation", "consolidation_cues",
    "workspace_claim_item", "waymark_spec",
})
REQUEST_MARK = "Host request (mechanical; not a human message)."


def binding_field(messages, format=None):
    """("field", "selection" | "payload") for a binding call (or its preflight count), else (None, None)."""
    if isinstance(format, dict):
        props = set(format.get("properties", {}))
        if len(props) == 1 and props <= SELECTION_FIELDS:
            return next(iter(props)), "selection"
        if len(props) == 1 and props <= PAYLOAD_FIELDS:
            return next(iter(props)), "payload"
    return None, None


def _as_dict(scripted):
    if isinstance(scripted, str):
        try:
            scripted = json.loads(scripted)
        except ValueError:
            return {}
    return scripted if isinstance(scripted, dict) else {}


def binding_response(messages, format, *scripted):
    """The scripted model's answer to a binding call, or None for any other call.  ``scripted`` are the
    intentions it wrote (later ones win), e.g. the last Pass-1 act and a workspace claim declaration."""
    field, _kind = binding_field(messages, format)
    if field is None:
        return None
    values = {}
    for item in scripted:
        values.update(_as_dict(item))
    return {"message": {"content": json.dumps({field: values.get(field, "")})}, "prompt_eval_count": 1,
            "done": True, "done_reason": "stop", "eval_count": 5}
