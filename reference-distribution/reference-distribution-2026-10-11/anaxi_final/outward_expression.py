"""Shared re-verification that an already-extracted Pass-2 string really is Clark's
validated outward expression -- never a fresh judgement about it.

The boundary between host control material and Clark's outward speech is established
structurally, once: every expression pass (ordinary Mac turn, Caret occasion, workspace
narration) is a plain chat completion whose assistant message IS the reply
(conversation_direction SHARED ORDINARY EXPRESSION SEAM).  Host facts never share that
message: they ride in the system message or in separately framed data messages.

Everywhere downstream (Caret route binding, the canonical writer, the dispatcher's re-read,
Mac persistence) receives the ALREADY-extracted expression.  This module is those callers'
shared, pure re-check that the value they were handed is still that kind of value:
non-blank, real content, and free of the two retired protocol strings (whose presence could
only mean an extraction bug handed machine text through, never that Clark chose to write
them).  It draws no conclusion from wording, punctuation, or shape.

Pure; never raises on str input. No imports (stdlib or otherwise).
"""

_ENVELOPE_MARKERS = ("<ANAXI_EXPRESSION>", "<ANAXI_RESPONSE_COMPLETE>")


def is_conversational_expression(expression):
    if not isinstance(expression, str) or not expression.strip():
        return False
    return not any(marker in expression for marker in _ENVELOPE_MARKERS)


def is_outward_safe_expression(expression):
    return is_conversational_expression(expression)
