"""
Anaxi -- Amendment A1 (Scoped Production Activation) fail-closed guard
for Claude-generation executables.

No module implementing the Claude native waking-write contract (frozen
Identity/Provenance Schema Sec.4) exists yet. Amendment A1 Sec.A1.3
requires every production Claude-generation executable enumerated in
Sec.A1.3.1 -- anaxi_final/claude_anaxi.py, anaxi_final/claude_anaxi_battery.py,
and repo-root claude_agent.py -- to refuse before any generation or
persistence: zero model/API invocation, zero anaxi_log.jsonl line, zero
journal write, zero relational_events/turn_generation_log row, zero
provenance write. A refused attempt is a non-event, not an
unknown-provenance event.

This is a structural backstop, not a deprecation (A1.3): the deferral
ends upon separate authorization supported by its own evidence, once a
Claude native waking-write implementation exists and is verified to the
same evidence standard as the Llama/Gemma path. Module import remains
permitted; only invocation of the guarded entry points is refused.
"""


class ClaudeWakingDeferredError(Exception):
    """Raised by every Amendment-A1-Sec.A1.3.1-enumerated Claude-generation
    executable's entry point, before any generation or persistence.
    Distinct and non-swallowable per A1.3(2) -- callers must not catch
    this broadly and continue past it."""


def refuse_claude_waking(entry_point: str) -> None:
    """Call as the FIRST action of every A1.3.1 executable entry point --
    before constructing any orchestrator, before prepare_context(),
    before any model/API call, before any persistence. Always raises;
    never returns."""
    raise ClaudeWakingDeferredError(
        f"{entry_point}: Claude native waking is deferred per Amendment A1 "
        f"(Identity/Provenance Schema, frozen spec Sec.4 as narrowed by A1.3). "
        f"No Claude native waking-write implementation exists yet. This is a "
        f"structural backstop, not a permanent exclusion -- refusing before "
        f"any generation or persistence."
    )
