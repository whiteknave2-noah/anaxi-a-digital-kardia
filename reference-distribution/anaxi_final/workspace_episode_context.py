"""WSP2-P3 Bridge C: the one derived, bounded waking renderer.

Built fresh, every time it is needed, from workspace_episode_
provenance.py's canonical facts alone -- never persisted itself, never
a second source of truth, never independently reconstructed per Pass
(see llama_anaxi.py's insertion point, which builds this once and
reuses the same string for both Pass 1 and Pass 2).

PATHWAYS, NOT ORGANS: this module performs no model call, no write,
no interpretation. It assembles already-established mechanical facts
into a fixed, bounded, deterministically-truncated string. It does not
decide what the episode MEANT.

Delivery/consumption semantics (spec section 7) live in llama_anaxi.py
(what gets passed to native_provenance_writer.stage_and_record_native_
waking_turn() as delivered_episode_run_id) and workspace_episode_
provenance.py (find_pending_episode_run_ids()) -- this module only
renders whatever run_id it is given; it has no opinion on delivery.
"""
import json

import workspace_episode_provenance as wep

# WSP2-P3-I section 5: hard aggregate cap, deliberately conservative --
# Gemma's context remains 4096 tokens and the Gate-A forensic already
# showed prompt_eval_count near that ceiling. This bound is NOT
# negotiable per-episode; it is enforced unconditionally below.
MAX_TOTAL_CHARS = 1000

HEADER = (
    "This is a bounded mechanical record of ordinary public workspace "
    "activity associated with your actor identity. It does not "
    "establish memories, motives, feelings, values, or subjective "
    "experience. Interpret it yourself."
)

TRUNCATION_MARKER = "...[truncated]"


def most_recent_pending_episode_run_id(data_dir):
    """V1 bound (spec section 9 / WSP2-P3-I section 7): at most ONE
    pending episode is ever rendered/delivered per waking turn -- the
    most recent. Older pending episodes are left completely intact in
    canonical provenance (nothing here ever deletes or marks them
    unrecoverable) and become eligible again on a later turn."""
    pending = wep.find_pending_episode_run_ids(data_dir)  # newest-first
    return pending[0] if pending else None


def _component(components, kind):
    return next((c for c in components if c["component_kind"] == kind), None)


def _fact(components, kind):
    comp = _component(components, kind)
    if comp is None:
        return None
    try:
        return json.loads(comp["component_text"])
    except (TypeError, ValueError):
        return None


def build_workspace_episode_context(data_dir, run_id, already_delivered_event_ids=None):
    """Pure read + string construction. Returns None (never an empty
    string, never a fabricated placeholder) if run_id is None or the
    episode has no recorded canonical events. Never exceeds
    MAX_TOTAL_CHARS -- enforced unconditionally as the final step.

    WSP2-P4-P1 (spec section 6): `already_delivered_event_ids` is an
    optional set of PUBLIC action/wait event_ids that
    ACTIVE_WORKSPACE_CONTINUITY_V1 already durably delivered to waking
    LIVE, before this episode closed -- callers pass the SAME durable
    ledger (workspace_public_continuity.find_delivered_public_event_ids())
    ACTIVE_WORKSPACE_CONTINUITY_V1 itself consults, never a second,
    independently-tracked notion of "already shown" (spec section 7).
    Those events are OMITTED from the itemized chronological "new
    material" listing below (Option A, spec section 6) -- they are
    never presented here as though newly encountered. The header's own
    aggregate counts ("Public actions: N. Public waits: M.") remain
    TOTAL counts regardless -- an honest mechanical fact about the
    whole episode, not itself a novelty claim, so it is deliberately
    NOT filtered. Omitting the parameter (the default) reproduces
    WSP2-P4's original, fully unfiltered behavior exactly -- existing
    callers are unaffected."""
    if run_id is None:
        return None
    episode = wep.query_episode(data_dir, run_id)
    if not episode:
        return None
    already_delivered_event_ids = already_delivered_event_ids or frozenset()

    started = next((e for e in episode if e["event_type"] == wep.EVENT_EPISODE_STARTED), None)
    handoff = next((e for e in episode if e["event_type"] == wep.EVENT_HANDOFF_SELECTED), None)
    ended = next((e for e in episode if e["event_type"] == wep.EVENT_EPISODE_ENDED), None)
    actions = [e for e in episode if e["event_type"] == wep.EVENT_PUBLIC_ACTION]
    waits = [e for e in episode if e["event_type"] == wep.EVENT_PUBLIC_WAIT]

    lines = [HEADER, ""]
    if started and ended:
        lines.append(f"Episode span (host clock seconds): {started['occurred_at']} to {ended['occurred_at']}.")
    lines.append(f"Public actions: {len(actions)}. Public waits: {len(waits)}.")

    if handoff:
        note = _component(handoff["components"], "roaming_handoff_note")
        if note and note["component_text"]:
            lines.append(f"You carried forward a note: {note['component_text']!r}")
        for excerpt_comp in handoff["components"]:
            if excerpt_comp["component_kind"] != "roaming_handoff_source_excerpt":
                continue
            excerpt = json.loads(excerpt_comp["component_text"])
            lines.append(f"Selected source ({excerpt['author']}): {excerpt['text']!r}")

    if ended:
        term = _fact(ended["components"], "roaming_termination_fact")
        if term:
            lines.append(f"Ended: {term['termination_class']}.")

    # Deterministic chronology: complete list if the remaining budget
    # allows it, otherwise the earliest entries that fit plus an
    # explicit truncation marker -- never a semantic "importance"
    # selection (spec section 5's hard-bound requirement). Already-
    # live-delivered events are skipped entirely here (WSP2-P4-P1
    # section 6, Option A) -- never presented as newly encountered.
    chrono_lines = []
    for a in actions:
        if a["event_id"] in already_delivered_event_ids:
            continue
        fact = _fact(a["components"], "roaming_action_fact")
        if fact:
            chrono_lines.append(f"- {fact['resource_class']}.{fact['action']}: {'ok' if fact['success'] else 'failed'}")
    for w in waits:
        if w["event_id"] in already_delivered_event_ids:
            continue
        fact = _fact(w["components"], "roaming_wait_fact")
        if fact:
            chrono_lines.append(f"- wait {fact['wait_minutes']}m")

    body = "\n".join(lines)
    remaining = MAX_TOTAL_CHARS - len(body) - 1 - len(TRUNCATION_MARKER)
    included_chrono = []
    used = 0
    truncated = False
    for line in chrono_lines:
        cost = len(line) + 1
        if used + cost > max(remaining, 0):
            truncated = True
            break
        included_chrono.append(line)
        used += cost
    if included_chrono:
        body += "\n" + "\n".join(included_chrono)
    if truncated:
        body += "\n" + TRUNCATION_MARKER

    if len(body) > MAX_TOTAL_CHARS:
        body = body[: MAX_TOTAL_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return body
