"""
Anaxi -- Llama (local, via Ollama) substrate, wired to the Anaxi
Protocol's waking path. Mirrors claude_anaxi.py's structure; see that
file and ANAXI_IMPLEMENTATION_CONTRACT.md's "Model-specific adaptations"
section for what's actually shared vs. actually different between the
two substrates.

Two real differences from the Claude adapter, worth knowing before
assuming this is claude_anaxi.py with the client swapped:

1. No system-role split needed here, by current understanding. Ollama's
   chat API accepts an OpenAI-style messages list with role="system" as
   the first entry directly -- unlike Claude, which requires system as
   its own separate parameter. to_ollama_format() below is a
   passthrough, not a transformation, kept as its own named function
   specifically so there's an obvious place to fix this if a live call
   against this exact model/Ollama version says otherwise. Worth
   remembering: the Claude temperature assumption looked just as solid
   right up until a real API call said otherwise.
2. temperature/top_p ARE accepted here, via Ollama's `options` dict --
   the opposite constraint from Sonnet 5, not the same one.

Setup:
    Save this file INSIDE the unzipped anaxi_final/ folder, next to
    orchestration.py and the rest.

    Install Ollama from ollama.com, then in a terminal:
        ollama pull llama3.2:3b
    pip install ollama

Run:
    python llama_anaxi.py "your prompt here"
"""

import functools
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import ollama
import readonly_db
from orchestration import AnaxiOrchestrator
from relational_history import RelationalHistory
from artifact_prompts import (
    build_artifact_construction_messages,
    ARTIFACT_CONSTRUCTION_SYSTEM_PROMPT,
    ARTIFACT_JUDGMENT_CLOSING_CUE,
)
from clark_journal import prepare_artifact_decision, finalize_artifact_write, StaleSchemaError
from signal_matcher import classify_signal
from signal_observation_log import record_observation
from bounded_clause import map_to_operation_status, render_bounded_clause
from free_prose_screen import (
    check_prohibited_constructions, check_bounded_clause_repeat,
    suppress_matched_sentences, REJECTION_NOTICES,
)

import boundary_inspector
import waking_pipeline_lock
import context_budget
import family_membership
import canonical_person
import hippocampus_retrieval
import human_session_binding
import inference_provider
import native_provenance_writer
import runtime_roots
import ui_turn_diagnostics
import workspace_episode_context
from outward_expression import is_outward_safe_expression
import workspace_episode_provenance
import workspace_public_continuity
from provenance_schema import derive_stable_id
from migrate_historical_data import PIPELINE_STRUCTURE
from session_dialogue_window import build_session_dialogue_window, insert_dialogue_window
from session_dialogue_window import build_authenticated_dialogue_window, dialogue_contribution, dialogue_messages
from session_dialogue_window import build_correspondent_dialogue_window
from interaction_mode import (
    TASK as TASK_MODE,
    CONVERSATION as CONVERSATION_MODE,
    VALID_MODES as VALID_INTERACTION_MODES,
    apply_interaction_mode,
    apply_conversation_aesthetic,
    CONVERSATION_MODE_CLAUSE,
    CONVERSATION_AESTHETIC_DIRECTIVE,
)
from conversation_direction import (
    build_pass2_interface,
    validate_pass1_conversation_act,
    apply_conversation_act,
    cross_validate_direction_control,
    cross_validate_workspace_coexistence,
    resolve_clark_direction_control,
    apply_human_direction_control,
    describe_direction_owner,
    describe_direction_request,
    describe_relinquishment,
    describe_outward_requests,
    INCOMPLETE_OUTWARD_REQUEST_FAILURES,
    drop_incomplete_outward_requests,
    validate_plain_reply,
    PASS2_COMPLETION_MARKER,
    PASS2_EXPRESSION_MARKER,
    get_working_set,
    set_working_set,
    reset_working_set,
    set_last_conversation_direction_trace,
    get_last_conversation_direction_trace,
    ConversationDirectionFailure,
    DIRECTION_HUMAN,
    DIRECTION_CLARK,
    DIRECTION_UNKNOWN,
    REQUEST_NONE,
    BACKGROUND_ACTIVITY_REQUEST_NONE,
    BACKGROUND_ACTIVITY_REQUEST_RESUME_OWN_PAUSE,
    SLEEP_TIMING_REQUEST_NONE,
    SLEEP_TIMING_REQUEST_REQUEST_SLEEP,
    SLEEP_TIMING_REQUEST_KNOCK,
    SLEEP_TIMING_REQUEST_WITHDRAW,
    OPERATIVE_DIRECTIVE_REQUEST_NONE,
    OPERATIVE_DIRECTIVE_REQUEST_SET,
    OPERATIVE_DIRECTIVE_REQUEST_WITHDRAW,
    EXTERNAL_INFO_REQUEST_NONE,
    DISCORD_CORRESPONDENCE_REQUEST_NONE,
    USE_WORKSPACE,
    render_pass1_action_menu,
    render_pass1_action_menu_parts,
    DirectionFailure as ConversationDirectionFailureCode,
    PASS1_SCHEMA,
    PASS1_SCHEMA_CARET_OCCASION,
    CARET_REPLY_REQUEST_NONE,
    CARET_REPLY_REQUEST_REPLY,
    REPLY_REQUEST_NO_REPLY,
    PASS1_SCHEMA_SOURCE_OCCASION,
    pass1_schema_with_correspondence,
    CORRESPONDENT_STANDING_REQUEST_NONE,
    PASS1_TASK_INSTRUCTION,
    PASS2_TASK_INSTRUCTION,
)
import conversation_direction_trace
import sleep_timing_knock
import operative_directive
import external_information
import discord_correspondence
import discord_author_mapping
import correspondence_surfaces

PIPELINE_KEY = PIPELINE_STRUCTURE["llama"]["pipeline_key"]
# Trace value for a turn on which Clark chose no_reply: Pass 2 was never composed (not a failure).
PASS2_STATUS_NOT_COMPOSED_NO_REPLY = "not_composed_no_reply"


def _clark_has_no_pause_to_resume():
    """True only when canonical truth is READABLE and shows no unresolved Clark-placed pause."""
    import workspace_roaming

    return (
        workspace_roaming.determine_clark_pause_authority(PROVENANCE_DB_DIR)
        == workspace_roaming.CLARK_PAUSE_AUTHORITY_KNOWN_NOT_PAUSED
    )


def _workspace_action_available(family_feature_active, family_view):
    """Fail-closed owner-workspace availability for ordinary waking.

    Legacy single-owner operation remains unchanged before FS1 is used.
    Once family mode exists, local workspace resources are available only
    in the designated owner's principal-private session.  A member-private,
    family-shared, stale, or identity-less session must never turn Clark's
    owner workspace into a cross-principal disclosure channel.
    """
    if not family_feature_active:
        return True
    return bool(
        family_view is not None
        and family_view.get("visibility_scope") == family_membership.SCOPE_PRINCIPAL_PRIVATE
        and family_view.get("principal_actor_id") == family_view.get("owner_actor_id")
    )


def _open_reading_line(session_id):
    """The workspace supervisor's open reading for this session (a long text delivered in pieces), stated
    in the ordinary menu so a "next part" request can be recognized as one.  Only when the supervisor
    is loaded in this process (it is after any workspace turn)."""
    import sys as _sys
    supervisor = _sys.modules.get("workspace_supervisor")
    read = getattr(supervisor, "_open_text_reads", {}).get(session_id) if supervisor else None
    if not read or not read.get("next_request"):
        return None
    return (f"Your open reading: {read['resource_class']}/{read['relative_path']}, chars {read['start']}-{read['end']}"
            + (f" of {read['total']}" if read.get("total") is not None else "") + " delivered so far "
            "(reading on is act=use_workspace)")


def _open_page_fact(session_id):
    """The last page Clark opened (durable record, restart-independent) and which part of it
    reached him (the host's delivered-window records), for Pass 1. None when there is none."""
    try:
        source = external_information.latest_fetched_source(PROVENANCE_DB_DIR, session_id)
        if source is None:
            return None
        import workspace_capability
        windows = [r.get("detail") for r in workspace_capability.query_action_log(
            workspace_capability.WorkspacePaths.production_defaults())
            if r.get("resource_class") == "external_information" and r.get("action") == "delivered"
            and r.get("relative_path") == source.get("retrieval_id")]
        return external_information.open_page_fact(source, windows)
    except Exception:
        return None


ARTIFACT_DECISION_LOG = "artifact_decision_log.jsonl"


def _artifact_decision_state(result):
    detail = (result or {}).get("detail") or {}
    if "_pending_write" in (result or {}):
        return "ready_to_write"
    if (result or {}).get("artifact_created"):
        return "written"
    return detail.get("status") or "none"


def _record_artifact_decision(*, human_input_event_id, waking_turn_event_id, signal, prepared, final, turn_path):
    """Durable host record of one turn's signal-gated artifact decision (append-only JSONL beside
    anaxi_log.jsonl; not canonical provenance). Live 2026-09-24: a POSITIVE save request's outcome
    existed only as a terminal print, and on a workspace turn the prepared decision was replaced by
    "not_applicable" with no trace that a judgment had run. Records what the judgment produced and
    what finally happened, and why. Never content. Best effort: never fails the turn."""
    final_detail = (final or {}).get("detail") or {}
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "human_input_event_id": human_input_event_id, "waking_turn_event_id": waking_turn_event_id,
        "signal_category": signal, "turn_path": turn_path,
        "judgment_outcome": _artifact_decision_state(prepared),
        "final_outcome": _artifact_decision_state(final),
        "final_reason": final_detail.get("reason"),
        "artifact_path": final_detail.get("path") if (final or {}).get("artifact_created") else None,
        "artifact_title": (final or {}).get("artifact_title"),
    }
    try:
        with open(os.path.join(PROVENANCE_DB_DIR, ARTIFACT_DECISION_LOG), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass
    return record


def _shared_vault_available():
    """The shared Obsidian vault is named in the menu only when it is configured and exists."""
    import workspace_capability
    return workspace_capability.WorkspacePaths.production_defaults().notes_available()


def _discord_correspondence_available(family_feature_active, family_view):
    """Keep owner-authorized private correspondence owner-private once
    FS1 exists.

    Before family mode this preserves the established unbound behavior.
    Afterwards, neither member-private nor family-shared waking may
    receive private inbound correspondence or use the owner's outbound
    destination authorization as its own capability surface.
    """
    return _workspace_action_available(family_feature_active, family_view)


def _operative_directive_action_available(family_feature_active, current, visible_current):
    """A scoped turn may alter the singleton directive only when no
    directive exists or the current occurrence is visible in that turn.

    This prevents a principal-private directive from becoming a hidden
    cross-principal mutation surface while preserving legacy/pre-FS1
    behavior and the accepted singleton Clark-directive architecture.
    """
    return not family_feature_active or current is None or visible_current is not None


def _family_workspace_continuity(family_view, already_delivered_event_ids, active_run_id, active_events):
    """Family-mode workspace active/episode continuity for ONE viewer.

    Returns (pending_episode_run_id, pending_resource_encounter,
    episode_context_text, active_events).  Empty unless the viewer is the
    designated owner in their own principal_private session; even then each
    event must pass the canonical FS1 delivery predicate (native scope or the
    legacy owner-private seam) -- a pending episode requires EVERY one of its
    events to pass, the single pending resource encounter must pass, and
    active events are filtered one by one."""
    empty = (None, None, "", [])
    if family_view is None:
        return empty
    conn = sqlite3.connect(f"file:{PROVENANCE_DB_DIR}/anaxi_provenance.db?mode=ro", uri=True)
    try:
        if not (
            family_view["visibility_scope"] == family_membership.SCOPE_PRINCIPAL_PRIVATE
            and family_view["owner_actor_id"] is not None
            and family_view["principal_actor_id"] == family_view["owner_actor_id"]
        ):
            return empty

        def visible(event_id):
            return family_membership.can_receive_canonical_event(
                conn, event_id,
                viewer_principal_actor_id=family_view["principal_actor_id"],
                viewer_scope=family_view["visibility_scope"],
                active_family_principal_ids=family_view["active_family_principal_ids"],
            )

        run_id = None
        for candidate in workspace_episode_provenance.find_pending_episode_run_ids(PROVENANCE_DB_DIR):
            episode = workspace_episode_provenance.query_episode(PROVENANCE_DB_DIR, candidate)
            if episode and all(visible(e["event_id"]) for e in episode):
                run_id = candidate
                break
        encounter = workspace_episode_provenance.find_pending_resource_encounter_for_continuity(PROVENANCE_DB_DIR)
        if encounter is not None and not visible(encounter["event_id"]):
            encounter = None
        text = ""
        if run_id is not None:
            text = workspace_episode_context.build_workspace_episode_context(
                PROVENANCE_DB_DIR, run_id, already_delivered_event_ids=already_delivered_event_ids,
            ) or ""
        return run_id, encounter, text, [e for e in (active_events or []) if visible(e["event_id"])]
    finally:
        conn.close()


def _legacy_owner_graph_text(orch, prompt, family_view):
    """Family-active legacy graph continuity: '' unless this is the
    designated owner's own principal_private session, in which case only
    graph nodes asserted strictly before the canonical Family activation
    boundary are retrieved (still rendered through the quarantine wrapper)."""
    if family_view is None:
        return ""
    conn = sqlite3.connect(f"file:{PROVENANCE_DB_DIR}/anaxi_provenance.db?mode=ro", uri=True)
    try:
        view = family_membership.legacy_owner_private_view(
            conn, viewer_principal_actor_id=family_view["principal_actor_id"],
            viewer_scope=family_view["visibility_scope"],
        )
    finally:
        conn.close()
    mind = getattr(orch, "mind", None)
    if view is None or mind is None:
        return ""
    from orchestration import render_legacy_memory_quarantine
    return render_legacy_memory_quarantine(
        mind.retrieve_waking_context(USER_ID, prompt, asserted_before=view["boundary"])
    )


def _screen_clark_prose(text: str, bounded_clause: str) -> dict:
    """Combines the two deterministic prose screens behind the single
    existing regeneration seam: the three frozen surface-form
    constructions first, then the bounded-clause repetition check --
    so both failure modes drive the identical regen/suppress retry
    path below, rather than needing a second, parallel one."""
    match = check_prohibited_constructions(text)
    if match["matched"]:
        return match
    return check_bounded_clause_repeat(text, bounded_clause)

# Single source of truth for the active waking model tag -- both
# call_llama() and ask_llama_for_json() read this at call time, never
# a value baked into either function body, so a test can swap it in
# isolation and both call sites observe the change identically.
# Approved substrate switch: local waking generation now runs on
# Gemma 4 E4B rather than Llama 3.2:3b. REM/sleep (llama_sleep.py) is
# untouched and keeps its own, separate MODEL constant.
MODEL = "gemma4:e4b"

# CAP2E: Clark's own dedicated photograph-vision waking substrate.
# Clark != model (frozen invariant) -- selecting this tag for one
# expression call does not create a second actor, session, or
# provenance subject; it is a mechanical, host-owned substrate choice
# made ONLY by call_llama()'s own `model` parameter below, exercised
# ONLY by the WSP1 supervised-workspace-action photograph-VIEW pass-2
# call, and
# ONLY when a genuine admitted image is actually present for this turn
# (see that module's own routing comment -- never inferred from prose).
# CAP2D-P1 established gemma4:e4b does not ground on pixels on this
# installation (12/12 counterbalanced synthetic discriminations FAILED
# -- answers tracked question/option wording, not pixels); the same
# assay against qwen3-vl:4b PASSED (12/12 correct, zero order-
# dependence, no-image control did not reconstruct the answer). Every
# ordinary (non-image) waking call is completely unaffected -- MODEL
# above remains the sole default for every existing call site.
VISION_MODEL = "qwen3-vl:4b"

# ============================================== OWC9-P3B: Pass-1 scaffold calibration
#
# SUPERSEDED by the waking-pass1-budget repair gate: Pass-1's own
# scaffold no longer includes identity_preamble at all (see the
# repair's own comment further below, where pass1_system_content is
# built) -- it is now CONVERSATION_MODE_CLAUSE + "Aesthetic directive: "
# + CONVERSATION_AESTHETIC_DIRECTIVE + PASS1_TASK_INSTRUCTION + the
# fixed closing cue, completely independent of Kardia, forever. The
# OLD calibration below (824 tokens, measured against a scaffold that
# DID include a synthetic identity_preamble) can never match this new
# scaffold's hash and has been replaced, not merely left stale.
#
# NEW calibration, per this gate's own explicit authorization -- exactly
# 2 direct LOCAL Ollama calls against the installed gemma4:e4b model
# (num_predict=1, generated content discarded, zero persistence, zero
# session/dialogue/canonical events, no Clark waking turn, no production
# state mutation): one measuring prompt_eval_count WITH the new fixed
# scaffold (system=pass1_system_content, user=placeholder human message,
# user=PASS1_TASK_INSTRUCTION+closing cue, user=mechanical-state text),
# one measuring the SAME shape WITHOUT the scaffold (empty system
# message, no task-instruction message). Real measurement: WITH=745
# tokens, WITHOUT=48 tokens, delta=697. calibrated_cost = delta + 8 (the
# same fixed message-boundary fudge factor the original OWC9-P3B assay
# used) = 705 -- a REAL, measured ~4.75x tighter bound than the byte
# estimator's own ~3346-byte count for the identical new scaffold text
# (the same ratio the original assay found, now confirmed to hold again
# for this smaller, Kardia-independent scaffold). A single measurement
# was sufficient here (no seed/repetition study): unlike the original
# assay's own concern (variance across different HUMAN MESSAGE endings,
# which are dynamic per-turn content), this scaffold text is now 100%
# fixed regardless of any human message -- there is no per-turn text to
# vary across repeated trials.
#
# PASS1_IMMUTABLE_TASK_MESSAGE is rendered as its OWN trailing user-role
# message (never merged with the dynamic "Current working state" text,
# which stays its own separate, byte-estimated message) -- this exact
# message-boundary shape is what the calibration assay itself measured
# (spec section 4's own "identify the exact boundary" requirement).
PASS1_IMMUTABLE_TASK_MESSAGE_CLOSING_CUE = "Respond with the JSON object now."
PASS1_SCHEMA_SHA256 = hashlib.sha256(json.dumps(PASS1_SCHEMA, sort_keys=True).encode("utf-8")).hexdigest()

# The EXACT synthetic Kardia this gate's own calibration assay used --
# exposed publicly (test-only consumers import it directly, e.g.
# test_owc5_s2_integration.py's own fake orchestrator) so any test that
# wants its own fake identity_preamble/style_instruction to MATCH the
# calibrated fingerprint can derive both, consistently, from this ONE
# source via linguistic_pipeline.build_generation_controls(...) --
# never duplicated as a second, driftable literal copy.
PASS1_SCAFFOLD_CALIBRATION_REFERENCE_KARDIA = {
    "moral_valve": "Prioritize honesty and the long-term wellbeing of the person I'm speaking with.",
    "volitional_channel": "Curious, deliberate, willing to take initiative when it serves the conversation.",
    "affective_stance": "Warm, steady, genuinely engaged.",
    "aesthetic_valve": "warm, precise",
}

# OWC9-P3C section 1: EVERY calibration-invalidating property is now
# load-bearing (runtime-compared), not merely recorded for audit. The
# frozen, calibrated fingerprint below is compared against a LIVE
# fingerprint computed fresh every Pass-1 call (see _pass1_scaffold_
# live_fingerprint() below). ANY mismatch on ANY field falls back to
# context_budget.estimate_tokens() unconditionally -- no automatic
# recalibration, no partial credit, no averaging.
_PASS1_SCAFFOLD_CALIBRATION = {
    # SHA-256 of (pass1_system_content + "\x00" + PASS1_TASK_INSTRUCTION
    # + "\n\n" + closing cue) -- pass1_system_content is now
    # CONVERSATION_MODE_CLAUSE + "Aesthetic directive: " +
    # CONVERSATION_AESTHETIC_DIRECTIVE, both fixed constants, so this
    # scaffold is 100% stable across every future call regardless of
    # Kardia. Genuinely measured 2026-09-05 via the exact real Ollama
    # call shape pass1 sends (see the block comment above). Still
    # runtime-compared, still falls back to the byte estimator
    # unconditionally on ANY mismatch (a future edit to the mode clause,
    # the aesthetic directive, PASS1_TASK_INSTRUCTION, the closing cue,
    # or the model/schema/server below invalidates this exactly as
    # designed -- never silently reused).
    "scaffold_sha256": "c5371f7f6741e771b6357669e5c49c4991e8c708cebcb9159ce2062f23b2bb0a",
    # CONTROL-SCAFFOLD ATTRIBUTION repair (production incident,
    # 2026-09-05): PASS1_TASK_INSTRUCTION+closing-cue now ships MERGED
    # into the single system message (see pass1_messages), not its own
    # trailing "user"-role message -- re-measured directly (2 real
    # gemma4:e4b calls, num_predict=1, delta of WITH vs WITHOUT the task
    # text in that merged system message, isolated from mechanical_state
    # and the human message; both runs agreed exactly, 545/545, zero
    # variance) rather than assumed. "system_merged" is a signature
    # value that exists ONLY to mismatch the OLD ("system", "user")
    # shape from before this repair -- a future revert or further
    # restructuring must invalidate this exactly as intentionally as
    # every other signature change already does.
    "message_structure_signature": ("system_merged",),
    "model_tag": "gemma4:e4b",
    "model_digest": "c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb",
    # Windows->macOS migration recalibration (2026-09-11): the Mac's
    # own bootstrap installed Ollama 0.34.0 rather than the pinned
    # 0.32.15 the 545 measurement above was taken against. Every OTHER
    # fingerprint field (model_digest, chat_template_sha256, schema,
    # scaffold text itself) still matched exactly, so this was the sole
    # mismatch -- but it is still checked exactly, per this section's
    # own governing rule, so the byte-estimator fallback fired (3346 for
    # byte-identical text) and a real production H's Pass-1 failed
    # closed on BUDGET_EXCEEDED before any model call. Re-measured
    # directly (2 real gemma4:e4b calls, num_predict=1, delta of WITH
    # vs WITHOUT the task text in the merged system message, isolated
    # from mechanical_state and the human message, same as the 0.32.15
    # assay's own method) against the live 0.34.0 server -- repeated
    # twice, both trials agreed exactly (693/693, zero variance).
    "ollama_server_version": "0.34.0",
    "chat_template_sha256": "b507b9c2f6ca642bffcd06665ea7c91f235fd32daeefdf875a0f938db05fb315",
    # Frozen assay-time schema identity. This must be a literal, not
    # PASS1_SCHEMA_SHA256: assigning the live value on both sides would
    # make every future schema edit appear calibrated automatically.
    #
    # LAWFUL NULL recalibration (2026-09-23): PASS1_SCHEMA gained the optional reply_request enum.
    # Re-measured with the identical method (merged system scaffold vs empty system, user "x",
    # format=the new PASS1_SCHEMA, num_predict=1, live gemma4:e4b / Ollama 0.34.0, digest
    # reconfirmed): WITH=708, WITHOUT=15, delta 693 -- unchanged, so the cost stays 693. The same
    # method against the retired schema (ade4316a...) reproduced 693 exactly first.
    # Retired: "ade4316a6af8c59c155195dba7b10e7790e2d027fae9625ca47859bf4ce8361c"
    #
    # SHARED-VAULT recalibration (2026-09-24): BOUNDARY_CAPABILITY_TARGETS gained "notes" (the shared
    # Obsidian vault became a workspace capability), extending the boundary_inquiry_request enum.
    # Same method, live gemma4:e4b / Ollama 0.34.0, two trials: WITH=708, WITHOUT=15, delta 693 --
    # unchanged, so the cost stays 693.  Retired: "ce9b53df03108da5eef9b40bd4f4a9513acd45c1d138458374de6f5ff071303e"
    "pass1_schema_sha256": "e68b413500f0fdf4c6f4a9805db826c3c57f628570ec4742bd5e60b82c1e9b48",
    "format_mode": "json_schema",
    "think": False,
    "interaction_mode": "conversation",
}
_PASS1_SCAFFOLD_CALIBRATED_COST = 693

# Fixed Pass-1 action-menu block (conversation_direction.render_pass1_action_menu_parts
# ()[1]): the same fingerprint-guarded fixed-text calibration as the scaffold above,
# measured by the same A/B method (WITH vs WITHOUT the block in the real merged
# system message, num_predict=1, the real json_schema format). The calibrated cost
# is only honored while every fingerprint field -- INCLUDING the sha256 of the exact
# block text -- still matches; any change to the block falls back to the byte
# estimator by construction.
#
# Measured 2026-09-18 against the live gemma4:e4b / Ollama 0.34.0 with the real
# ordinary Pass-1 request: three A/B trials (with vs without the 835-byte block in
# the merged system message) all gave 213 tokens, zero variance; calibrated_cost =
# max(delta) + 8 = 221 (the byte estimator prices the same block at 835).
#
# LAWFUL NULL recalibration (2026-09-23): the fixed block gained the reply_request line and its
# example (1194 bytes; wording chosen by the real-model menu battery recorded in
# conversation_direction).  Same method (full representative ordinary Pass-1 system message with vs
# without the block, user "Hello there.", format=the new PASS1_SCHEMA, num_predict=1): three trials
# all 302 tokens, zero variance; calibrated_cost = 302 + 8 = 310.  The method first reproduced the
# retired block's 213 exactly.  Retired block sha: "80cd886d06844c08835c95802d7f1a05b4e9581c6b8fe98d887a6e9f1e6f73b7".
# SHARED-VAULT schema change (2026-09-24): block unchanged; re-measured under the new PASS1_SCHEMA,
# three trials 302/302/302 -- the calibrated cost stands.
# BOUNDARY-INQUIRY WITHDRAWAL recalibration (2026-09-24, owner decision): the block's boundary line now
# states the capability is not available in this release (1187 bytes; schema unchanged). Same method: the
# retired block reproduced 302/302/302 first; the new block measured 300/300/300, zero variance;
# calibrated_cost = 300 + 8 = 308. Retired block sha: "bbe0684b91de561118ecac9a09a19943adad07d721a6ac29c0a0334590e23aa4".
_PASS1_MENU_CALIBRATION = dict(
    _PASS1_SCAFFOLD_CALIBRATION,
    scaffold_sha256="3a5cd5934021faa3f5ec6be89e8836026784ee4c966a0b973c2a09558d62d36e",
)
_PASS1_MENU_CALIBRATED_COST = 308

_ollama_environment_cache = {"checked": False, "model_digest": None, "server_version": None, "chat_template_sha256": None}


def _live_ollama_environment():
    """Checked ONCE per process (cached) -- 'no automatic recalibration
    during Clark's turn' (spec section 9/OWC9-P3C section 1) applies to
    this verification too, not only to recalibration itself;
    re-querying Ollama on every single Pass-1/Pass-2 call would add
    real per-turn latency and new per-turn failure modes for values
    that only change on a discrete, deliberate model/runtime-management
    action (pull/copy/delete a model, upgrade the Ollama server), never
    mid-session. Covers model digest (ollama.list()), the model's own
    embedded/Modelfile chat-template hash (ollama.show()), and the
    Ollama server's own version (a direct stdlib HTTP GET to /api/version
    -- the installed ollama Python client exposes no version() method).
    Fails closed (leaves fields None, a guaranteed fingerprint mismatch)
    on ANY error in ANY of the three lookups -- one failing does not
    prevent the others from still being attempted."""
    if _ollama_environment_cache["checked"]:
        return _ollama_environment_cache
    digest = None
    template_hash = None
    server_version = None
    try:
        for m in ollama.list().models:
            if m.model == MODEL:
                digest = m.digest
                break
    except Exception:
        pass
    try:
        info = ollama.show(MODEL)
        template_hash = hashlib.sha256((info.template or "").encode("utf-8")).hexdigest()
    except Exception:
        pass
    try:
        import urllib.request
        base_url = str(ollama.Client()._client.base_url).rstrip("/")
        with urllib.request.urlopen(f"{base_url}/api/version", timeout=5) as resp:
            server_version = json.loads(resp.read()).get("version")
    except Exception:
        pass
    _ollama_environment_cache.update({
        "checked": True, "model_digest": digest, "server_version": server_version, "chat_template_sha256": template_hash,
    })
    return _ollama_environment_cache


def _pass1_scaffold_live_fingerprint(scaffold_hash_source_text, message_structure_signature=("system", "user")):
    env = _live_ollama_environment()
    return {
        "scaffold_sha256": hashlib.sha256(scaffold_hash_source_text.encode("utf-8")).hexdigest(),
        "message_structure_signature": message_structure_signature,
        "model_tag": MODEL,
        "model_digest": env["model_digest"],
        "ollama_server_version": env["server_version"],
        "chat_template_sha256": env["chat_template_sha256"],
        "pass1_schema_sha256": PASS1_SCHEMA_SHA256,
        "format_mode": "json_schema",
        "think": False,
        "interaction_mode": "conversation",
    }


# ============================================== OWC9-P3C section 2: Pass-2 scaffold calibration
#
# Same bounded fixed-scaffold calibration architecture as Pass-1's own
# (see the OWC9-P3B record above), reused narrowly -- 13 direct LOCAL
# Ollama calls (5 A/B delta pairs = 10, plus 3 dialogue-shape coverage
# probes) against the installed gemma4:e4b model (num_predict=1,
# generated content discarded, zero persistence, zero session/dialogue/
# canonical events, zero production Kardia read -- the SAME synthetic
# representative Kardia as the Pass-1 assay). Unlike Pass-1, the
# calibrated scaffold here is NARROWER than "system + task message"
# combined: Pass-2 already keeps its real (production, non-synthetic)
# system content as its own separate CORE_SYSTEM_CONTROL contribution
# (pass2_hard_core below, unchanged, always byte-estimated -- never
# calibrated, since production Kardia genuinely varies turn to turn).
# Only PASS2_TASK_INSTRUCTION -- truly constant
# regardless of Kardia, act, or working-set state -- is calibrated.
# The dynamic directional-control facts (selected act, thread,
# direction_owner/direction_request state descriptions, relinquishment)
# remain their own separate, byte-estimated contribution (pass2_hard_
# directional_state below), exactly mirroring Pass-1's mechanical-state
# split.
#
# OWC9-P3C section 1's completed fingerprint contract, reused as-is for
# Pass-2 (spec's own "reuse the SAME P3B architecture" instruction) --
# same model/environment fields, same load-bearing guarantee. Pass-2
# has no structured_schema (unlike Pass-1's PASS1_SCHEMA/`format`), so
# there is no schema-hash field here; "format_mode" instead records
# that Pass-2's call path (call_llama()) passes no `format` argument at
# all, distinguishing it from a future accidental schema addition.
_PASS2_SCAFFOLD_CALIBRATION = {
    # SHA-256 of PASS2_TASK_INSTRUCTION alone -- NOT combined with system content.
    #
    # SHARED ORDINARY EXPRESSION SEAM recalibration (2026-09-23): ordinary Pass 2 is a plain chat
    # completion again (see conversation_direction's SHARED ORDINARY EXPRESSION SEAM comment), so
    # the JSON-era closing cue ("This is that same literal reply, complete -- not a description of
    # it.") was retired and the scaffold is the task instruction alone (631 UTF-8 bytes).  Measured
    # against live local gemma4:e4b / Ollama 0.34.0 (model digest reconfirmed below): 5 A/B delta
    # pairs (payloads "a", "Done.", "42", "?!", two-line; system=payload+"\n\n"+scaffold vs
    # system=payload, user="x", num_predict=1, temperature=0.2, nothing persisted) all measured
    # 130 tokens, zero variance.  calibrated_cost = max(delta) + 8 = 138.
    # Retired (2026-09-22, scaffold + JSON-era closing cue): "2c7eb849f5f1ab36702967373093f4d3ca0c16371d88d2fa724843b706bb9d9a"
    "scaffold_sha256": "3720aa5719359985895bbe10a4780c79582202caf5dd40acf62a035ac377ea69",
    # The immutable task instruction ships MERGED into the system message (see
    # pass2_real_messages), not as another user-role message.
    "message_structure_signature": ("system_merged",),
    "model_tag": "gemma4:e4b",
    "model_digest": "c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb",
    "ollama_server_version": "0.34.0",
    "chat_template_sha256": "b507b9c2f6ca642bffcd06665ea7c91f235fd32daeefdf875a0f938db05fb315",
    "format_mode": "unstructured",
    "think": False,
    "interaction_mode": "conversation",
}
# SHARED ORDINARY EXPRESSION SEAM recalibration (2026-09-23), see above (was 153 for the retired scaffold).
_PASS2_SCAFFOLD_CALIBRATED_COST = 138

# Waking-pass2-budget repair (production incident, 2026-09-05): the
# first real production wake-up turn hit pass2's own version of the
# same over-conservative-accounting problem the earlier pass1 repair
# fixed. Pass-2's CORE_SYSTEM_CONTROL contribution (base_system_content)
# welds together two genuinely different kinds of text:
#   1. CONVERSATION_MODE_CLAUSE + "Aesthetic directive: " +
#      CONVERSATION_AESTHETIC_DIRECTIVE -- FIXED, bounded, non-Kardia
#      text, identical in every call, forever (the exact same text
#      pass1's own scaffold already uses -- see PASS1's own repair
#      comment further below).
#   2. The real, evolving Kardia-derived identity_preamble bullets
#      (moral/volitional/affective/aesthetic stance + the "you have
#      persistent memory" disclaimer) -- genuinely DYNAMIC, changes
#      whenever Kardia evolves, and can never be safely pre-calibrated
#      the way a permanently-fixed scaffold can (a stale calibration
#      constant here could one day UNDER-count a larger evolved Kardia
#      -- unsafe). This part stays on the ordinary, always-safe byte
#      estimator, unconditionally, exactly as before.
# Only piece 1 is split out and calibrated here -- piece 2 is untouched,
# still fully present, still fully byte-estimated, still fully
# available to Clark's real expression. This is not a summarization or
# removal of Kardia; Pass-2 still sees all of it, in full, every turn.
#
# Calibrated 2026-09-05 via 2 direct local Ollama calls against
# gemma4:e4b (num_predict=1, generated content discarded, zero
# persistence, matching call_llama()'s own real unstructured/no-format
# call shape): WITH the fixed framing text (system=framing, user="x")
# = 163 tokens; WITHOUT it (system="", user="x") = 15 tokens; delta =
# 148; calibrated_cost = delta + 8 (the same fixed message-boundary
# fudge factor every other calibration in this file uses) = 156.
_PASS2_FIXED_FRAMING_CALIBRATION = {
    "scaffold_sha256": "5748ff0379290faf2d9a02fa6f85ae92ce8c77eacf42e8e2e5e53b36d7248a62",
    "message_structure_signature": ("system", "user"),
    "model_tag": "gemma4:e4b",
    "model_digest": "c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb",
    # Windows->macOS migration recalibration (2026-09-11): same reason
    # as _PASS2_SCAFFOLD_CALIBRATION above -- re-measured directly (2
    # real gemma4:e4b calls, num_predict=1, delta of WITH vs WITHOUT
    # the framing text; system="", user="x") against live Ollama
    # 0.34.0, repeated twice, both trials agreed exactly: WITH=163,
    # WITHOUT=15, delta=148 -- identical to the original 0.32.15
    # measurement, so calibrated_cost = delta + 8 = 156, unchanged.
    "ollama_server_version": "0.34.0",
    "chat_template_sha256": "b507b9c2f6ca642bffcd06665ea7c91f235fd32daeefdf875a0f938db05fb315",
    "format_mode": "unstructured",
    "think": False,
    "interaction_mode": "conversation",
}
_PASS2_FIXED_FRAMING_CALIBRATED_COST = 156

def _pass2_scaffold_live_fingerprint(scaffold_hash_source_text, message_structure_signature=("user", "user")):
    env = _live_ollama_environment()
    return {
        "scaffold_sha256": hashlib.sha256(scaffold_hash_source_text.encode("utf-8")).hexdigest(),
        "message_structure_signature": message_structure_signature,
        "model_tag": MODEL,
        "model_digest": env["model_digest"],
        "ollama_server_version": env["server_version"],
        "chat_template_sha256": env["chat_template_sha256"],
        "format_mode": "unstructured",
        "think": False,
        "interaction_mode": "conversation",
    }


def core_and_calibrated_framing_contributions(base_system_content):
    """Cost one CONVERSATION-mode system message the way ordinary
    Pass-2 already costs it: the fixed, non-Kardia framing (mode clause +
    conversational aesthetic directive) is split out and carries its
    empirically calibrated cost, while everything else -- the evolving
    Kardia-derived identity text -- stays on the always-safe byte
    estimator. Returns ``(core, framing)``; ``framing`` is None (and
    ``core`` covers the whole text on the byte estimator) if the
    expected fixed substrings are not present.

    This changes only how ``base_system_content`` is COSTED. Callers
    keep sending the unabridged text. WSP1's supervised workspace passes
    reuse this so their hard floor is accounted exactly as ordinary
    waking's is; the byte estimator alone overcounts the 769-byte fixed
    framing by ~5x and pushed an ordinary-length human message past the
    WSP1 Pass-1 budget in live production."""
    dynamic_identity_text = base_system_content
    mode_clause_suffix = "\n\n" + CONVERSATION_MODE_CLAUSE
    if dynamic_identity_text.endswith(mode_clause_suffix):
        dynamic_identity_text = dynamic_identity_text[: -len(mode_clause_suffix)]
    aesthetic_target = f"Aesthetic directive: {CONVERSATION_AESTHETIC_DIRECTIVE}"
    if aesthetic_target in dynamic_identity_text:
        dynamic_identity_text = dynamic_identity_text.replace(aesthetic_target, "", 1)
    if dynamic_identity_text == base_system_content:
        return context_budget.Contribution(
            context_budget.CORE_SYSTEM_CONTROL, base_system_content, hard=True,
        ), None
    fixed_framing_text = CONVERSATION_MODE_CLAUSE + "\n\n" + aesthetic_target
    core = context_budget.Contribution(
        context_budget.CORE_SYSTEM_CONTROL, dynamic_identity_text, hard=True,
    )
    framing = context_budget.Contribution(
        context_budget.CONVERSATION_FRAMING, fixed_framing_text, hard=True,
    )
    framing.cost = context_budget.calibrated_or_fallback_cost(
        framing.rendered_text,
        _pass2_scaffold_live_fingerprint(fixed_framing_text, message_structure_signature=("system", "user")),
        _PASS2_FIXED_FRAMING_CALIBRATION, _PASS2_FIXED_FRAMING_CALIBRATED_COST,
    )
    return core, framing


# ============================================== OWC9-P4A: artifact_judgment scaffold calibration
#
# Same bounded fixed-scaffold calibration architecture as Pass-1/
# Pass-2's own (see the OWC9-P3B/P3C records above), reused narrowly
# -- 11 direct LOCAL Ollama calls against the installed gemma4:e4b
# model (num_predict=1, generated content discarded, zero persistence,
# zero session/dialogue/canonical events, zero production Kardia/
# conversation read -- synthetic harmless dynamic content only; 5 A/B
# delta pairs = 10 calls, plus 1 realistic ~1000-byte coverage probe).
# Unlike Pass-2's own scaffold (decoupled from system content),
# artifact_judgment's system prompt IS genuinely, permanently constant
# -- ARTIFACT_CONSTRUCTION_SYSTEM_PROMPT never varies with Kardia,
# interaction mode, or turn content (unlike ordinary waking's own
# identity-preamble system message) -- so the calibrated scaffold here
# covers system+closing-cue combined, mirroring Pass-1's own
# combined-hash convention, and (unlike Pass-1's own synthetic-Kardia
# caveat) this ALWAYS matches production, every turn, by construction.
#
# Result: 5 dynamic-content variants (letter/period/digit/punctuation/
# multiline), each measuring the token delta between a prompt WITH vs
# WITHOUT the immutable system+closing-cue scaffold -- ALL FIVE deltas
# measured EXACTLY 677 tokens (variance 0). A 6th coverage probe (a
# realistic ~935-byte dynamic message) confirmed computed_bound (=
# calibrated_scaffold_cost + UTF-8-byte estimate of the dynamic
# message) >= the model's own actual prompt_eval_count, with
# comfortable headroom. calibrated_cost = max(delta) + 8 = 685 -- a
# real, measured ~4.15x tighter bound than the byte estimator's own
# 2840-byte count for the identical scaffold text.
_ARTIFACT_JUDGMENT_SCAFFOLD_CALIBRATION = {
    # SHA-256 of (ARTIFACT_CONSTRUCTION_SYSTEM_PROMPT.strip() + "\x00"
    # + ARTIFACT_JUDGMENT_CLOSING_CUE) -- both fixed module constants,
    # never Kardia/turn-dependent, so this hash is invariant across
    # every real production call (no synthetic-vs-real-Kardia mismatch
    # risk the way Pass-1's own combined scaffold has).
    "scaffold_sha256": "71df5d4be319450805cdb16661f99f6b3682dc01f0d72fe4509c719e5708df36",
    # (role of the dynamic content message, role of the closing-cue
    # message) -- both "user" -- catches a future refactor that
    # changes message order/role shape without also changing the
    # scaffold hash itself.
    "message_structure_signature": ("user", "user"),
    "model_tag": "gemma4:e4b",
    "model_digest": "c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb",
    # Windows->macOS migration recalibration (2026-09-12): re-measured
    # directly against live Ollama 0.34.0 using the exact same 5-variant
    # assay (letter/period/digit/punctuation/multiline dynamic content,
    # each a WITH-vs-WITHOUT delta) -- all 5 variants agreed exactly,
    # delta=677, byte-for-byte identical to the original 0.32.15
    # measurement. calibrated_cost = max(delta) + 8 = 685, unchanged.
    "ollama_server_version": "0.34.0",
    "chat_template_sha256": "b507b9c2f6ca642bffcd06665ea7c91f235fd32daeefdf875a0f938db05fb315",
    # No structured_schema is passed for this call (format="json" loose
    # mode) -- distinguishing it from a future accidental schema
    # addition, same convention as Pass-2's own "unstructured".
    "format_mode": "unstructured",
    "think": False,
    # Not an interaction_mode (this call runs identically regardless
    # of CONVERSATION/TASK mode) -- a path identity instead, so a
    # future refactor that reuses this same calibration record for a
    # DIFFERENT call site cannot silently pass fingerprint match.
    "path_identity": "artifact_judgment",
}
_ARTIFACT_JUDGMENT_SCAFFOLD_CALIBRATED_COST = 685


def _artifact_judgment_scaffold_live_fingerprint(scaffold_hash_source_text, message_structure_signature=("user", "user")):
    env = _live_ollama_environment()
    return {
        "scaffold_sha256": hashlib.sha256(scaffold_hash_source_text.encode("utf-8")).hexdigest(),
        "message_structure_signature": message_structure_signature,
        "model_tag": MODEL,
        "model_digest": env["model_digest"],
        "ollama_server_version": env["server_version"],
        "chat_template_sha256": env["chat_template_sha256"],
        "format_mode": "unstructured",
        "think": False,
        "path_identity": "artifact_judgment",
    }


DB_PATH = "anaxi_mind_llama.db"
RELATIONAL_DB_PATH = "anaxi_relational_llama.db"
# WSP2-P5-P2a (spec sections 1B/1C/3): CLARK'S CANONICAL PROVENANCE HAS
# ONE DETERMINISTIC LOCATION -- anchored to this source file's own
# directory, never to the launching process's current working
# directory. Previously "." (CWD-relative, matching every other path
# constant in this file) -- but unlike DB_PATH/RELATIONAL_DB_PATH/
# LOG_FILE, this specific path feeds WSP2-P5-P2's lifecycle-authority
# reconstruction, where a wrong resolution has a CONSEQUENTIAL,
# fail-closed-relevant effect: launching Clark from any working
# directory other than this one (a misconfigured shortcut's "Start
# in", a scheduled task, a different shell) would otherwise make an
# established installation's real canonical history invisible,
# indistinguishable from a genuinely missing store. Mirrors an
# already-established own-source-directory anchoring convention this
# project already uses elsewhere for exactly this reason (`base =
# os.path.dirname(os.path.abspath(__file__))`). A module-level
# variable, not a function/property -- existing test fixtures that
# reassign `llama_anaxi.PROVENANCE_DB_DIR = test_dir` after import
# continue to override it exactly as before this gate.
PROVENANCE_DB_DIR = os.path.dirname(os.path.abspath(__file__))

import temporal_grounding
_temporal_process_started = temporal_grounding.process_start_time()
_temporal_lifecycle_sessions = set()
STAGING_PATH = "native_turn_staging.jsonl"
# OWC6-O1: append-only operational/diagnostic trace binding a released
# conversation-mode turn to its typed conversational-direction control
# act -- NOT canonical provenance, NOT autobiographical memory. See
# conversation_direction_trace.py's module docstring.
CONVERSATION_DIRECTION_TRACE_PATH = "conversation_direction_trace.jsonl"
USER_ID = "nate"

# Windows->macOS migration prep (2026-09-06): the OneDrive/Desktop
# convention below is a Windows-machine-specific default, not an
# architectural requirement -- on a fresh macOS account there is no
# OneDrive sync folder at this location at all. ANAXI_OBSIDIAN_WORKSPACE_
# ROOT lets the owner point this at wherever the real vault lives on
# the new machine (manual, one-time step); the default is left exactly
# as-is (byte-identical existing behavior when unset) so this changes
# nothing for the current Windows machine.
OBSIDIAN_WORKSPACE_ROOT = runtime_roots.obsidian_vault_root()   # ANAXI_OBSIDIAN_WORKSPACE_ROOT, test-isolated
LOG_FILE = "anaxi_log.jsonl"

_native_session_state = {"session_id": None, "session_started_at": None}


def _get_native_session() -> tuple[str, int]:
    """One session per process run, minted lazily on first use and
    reused by every subsequent native turn in this run -- never
    re-minted mid-run."""
    if _native_session_state["session_id"] is None:
        _native_session_state["session_id"] = native_provenance_writer.generate_native_ulid()
        _native_session_state["session_started_at"] = int(datetime.now(timezone.utc).timestamp())
    return _native_session_state["session_id"], _native_session_state["session_started_at"]


def get_current_session_id() -> str:
    """OWC7-S2: public accessor for this process's native session id --
    the same lazily-minted value _get_native_session() itself returns.
    Exists so callers outside this file (llama_gui.py's direction-
    control surface) don't need to reach into the private
    _get_native_session() function directly."""
    return _get_native_session()[0]


def get_current_session_started_at() -> int:
    """SLP1-A4c: public accessor for this process's native session
    started_at -- the same lazily-minted value _get_native_session()
    itself returns, mirroring get_current_session_id() exactly. Exists
    so a human-facing ingress (llama_gui.py) can perform an explicit
    session-start human binding (human_session_binding.py) using the
    SAME session_id/session_started_at pair this process's own turns
    will eventually use, without reaching into the private
    _get_native_session() function directly."""
    return _get_native_session()[1]


# OWC3-S1: session-scoped exactly like _native_session_state above --
# one interaction mode per process run. Never inferred from prompt
# content; only ever set by an explicit `interaction_mode=` argument
# to run_waking_turn() (itself only ever driven by an explicit operator
# selection, e.g. main()'s --conversation flag). A fresh process/session
# starts with mode=None, resolving to TASK (today's unchanged default)
# until/unless conversation mode is explicitly selected.
_interaction_mode_state = {"mode": None}


def _resolve_interaction_mode(explicit_mode):
    """explicit_mode: the interaction_mode argument passed to THIS
    call of run_waking_turn(), or None if the caller didn't specify
    one. A non-None value is remembered for the rest of this process
    run (session-scoped persistence, mirroring _get_native_session())
    and returned; None falls back to whatever was last explicitly set
    in this session, defaulting to TASK if nothing ever was."""
    if explicit_mode is not None:
        if explicit_mode not in VALID_INTERACTION_MODES:
            raise ValueError(f"unknown interaction_mode: {explicit_mode!r}")
        _interaction_mode_state["mode"] = explicit_mode
        return explicit_mode
    return _interaction_mode_state["mode"] or TASK_MODE


# The next staging line index is no longer computed here. An earlier
# revision required this (and every other caller) to count existing
# lines independently, which meant two racing callers could both count
# the same total and both believe they owned the next index --
# mechanically confirmed to produce a physically-second line whose
# stored staging_id disagreed with its true recomputed position.
# native_provenance_writer.stage_and_record_native_waking_turn() now
# determines the index itself, atomically, inside
# native_turn_staging.append_staging_entry()'s own cross-process lock.


def to_ollama_format(messages: list[dict]) -> list[dict]:
    """See module docstring, point 1."""
    return messages


def call_llama(
    messages: list[dict], controls: dict, images: list = None, model: str = None,
    structured_schema: dict = None,
    generation_reserve: int = None,
    provider: str = None,
    runtime_options: dict = None,
) -> str:
    """Ordinary conversational generation only -- pass-2's initial call
    and its bounded regeneration/retry both go through this one
    function (run_waking_turn() has no other call site for either), so
    think=False here covers both without a second edit. Explicit, not
    relying on Ollama's default: measured directly against this exact
    model/runtime (gemma4:e4b, Ollama 0.32.15) that an omitted `think`
    silently enables hidden reasoning consuming the large majority of
    decode tokens, while think=False returns a normal, complete,
    non-degraded response. Pass-1 (ask_llama_for_json() below) is a
    separate function and is untouched -- whether reasoning helps or
    hurts governed artifact construction has not been measured.

    CAP2-B: `images`, optional, additive, default None -- a list of raw
    image bytes (never a path string, never base64-pre-encoded; see the
    workspace capability layer's own deliver_photograph_bytes()
    `image_bytes` contract) attached to the FINAL message only, via
    ollama==0.6.2's own genuinely-supported Message.images field
    (machine-confirmed:
    plain-dict messages passed to ollama.chat() are validated into real
    Message objects server-side, and `images` is a first-class field on
    that model, unlike an invented key). Every pre-existing caller
    passes nothing here and is byte-for-byte unaffected.

    CAP2E: `model`, optional, additive, default None -- falls back to
    MODEL (gemma4:e4b) exactly as before when omitted. The ONLY
    sanctioned caller of a non-default value is the WSP1 supervised-
    workspace-action photograph-VIEW pass-2 call, passing VISION_MODEL,
    and ONLY when a
    genuine admitted image is actually being attached this same call
    (see VISION_MODEL's own module comment). This parameter does not
    change think=False, the options dict, or anything else about this
    function's own generation contract -- a substrate swap, not a
    second code path.

    `structured_schema` remains an optional provider-format seam for
    non-ordinary callers. Ordinary conversation now passes None and
    requires a model-authored plain-text completion marker instead:
    the former expression-envelope grammar could close a valid JSON
    object after an early EOS and thereby conceal linguistic
    incompleteness. `format=None` is the real Ollama client's default
    and preserves unconstrained conversational prose. This keeps the call
    itself a single literal `ollama.chat(model=..., ...)` expression
    with a real `model=` keyword argument, which is what OWC9-P4's own
    AST-based bypass guard (test_owc9p4_bypass_guard.py) mechanically
    scans for to confirm every real inference call site is a
    registered, budget-compliant wrapper -- a `**kwargs`-unpacked call
    would be invisible to that scan.

    Ordinary waking supplies generation_reserve from OWC9: request an
    explicit finite decode limit and reject incomplete transport before
    expression validation/persistence. This does not judge whether a
    normal-stop expression is semantically complete or add any retry.
    Other callers retain their existing generation contract when omitted.

    THIN-INFERENCE-PROVIDER-BOUNDARY-V0: `provider`, optional, additive,
    default None -- resolved via inference_provider.resolve_provider(),
    which falls back to the ANAXI_INFERENCE_PROVIDER environment variable
    and then to Ollama. The Ollama path below is completely unchanged
    (same literal `ollama.chat(...)` call as before this parameter
    existed); mlx-serve is dispatched through inference_provider.dispatch(),
    which returns an Ollama-wire-shaped response so every line after the
    dispatch is byte-identical regardless of which provider ran.
    """
    ollama_messages = to_ollama_format(messages)
    if images:
        ollama_messages = list(ollama_messages)
        ollama_messages[-1] = dict(ollama_messages[-1])
        ollama_messages[-1]["images"] = list(images)
    generation_options = {
        "temperature": controls["temperature"],
        "top_p": controls["top_p"],
    }
    if generation_reserve is not None:
        # Ordinary waking supplies the SAME OWC9 reserve used at admission.
        if generation_reserve != context_budget.PASS2_GENERATION_RESERVE:
            raise ValueError("ordinary waking requires its OWC9 generation reserve")
        generation_options.update(num_predict=generation_reserve, num_ctx=context_budget.CONTEXT_CEILING)
    if runtime_options:
        # A bounded, explicit RUNTIME window for a call whose model reasons hidden regardless of
        # think=False (the vision pass; see context_budget.QWEN_VISION_RUNTIME_*). Admission is
        # unchanged; only the window the generation runs in is set, and only these two keys.
        if set(runtime_options) - {"num_ctx", "num_predict"}:
            raise ValueError("runtime_options accepts only num_ctx and num_predict")
        generation_options.update(runtime_options)
    resolved_provider = inference_provider.resolve_provider(provider)
    if resolved_provider == inference_provider.PROVIDER_OLLAMA:
        response = ollama.chat(
            model=model or MODEL,
            messages=ollama_messages,
            format=structured_schema,
            think=False,
            options=generation_options,
        )
    else:
        result = inference_provider.dispatch(
            resolved_provider, model=model or MODEL, messages=ollama_messages,
            options=generation_options, format=structured_schema,
        )
        # A bounded waking call already has the established transport-level
        # completion check below, which maps length exhaustion into its
        # existing IncompleteCompletionError/recovery semantics. Every other
        # call must reject truncation here: a partial provider response is not
        # ordinary assistant content merely because it contains a string.
        if result.status == inference_provider.TRUNCATED and generation_reserve is None:
            raise inference_provider.ProviderError(result)
        if result.status not in (inference_provider.SUCCESS, inference_provider.TRUNCATED):
            raise inference_provider.ProviderError(result)
        response = result.raw
    # OWC8-S1: same observation-only contract as ask_llama_for_json()'s
    # own record_pass1_completion_metadata() call below -- closes the
    # Pass-2 observability gap the live OWC/WSP2-P3 forensic had to
    # work around by reading Ollama's own server log by hand. Recorded
    # for every call_llama() invocation (the real Pass-2 expression
    # call and TASK-mode's pass2_task_mode/pass2_regen calls alike);
    # this function's own return contract -- a plain str, nothing else
    # -- is completely unchanged for every existing caller.
    ui_turn_diagnostics.record_pass2_completion_metadata(response)
    if generation_reserve is not None:
        context_budget.require_complete_response(response, generation_reserve)
    elif runtime_options:
        # Transport completion is still mandatory: a length-terminated or interrupted generation is
        # never a finished reply (the reasoning-only zero-content case included). No reserve applies
        # here -- the hidden reasoning legitimately exceeds the visible-reply reserve.
        if response.get("done_reason") == "length":
            raise context_budget.IncompleteCompletionError("COMPLETION_LIMIT_REACHED")
        # Explicit contrary evidence only (a real provider always reports both fields).
        if response.get("done") is False or response.get("done_reason") not in (None, "stop"):
            raise context_budget.IncompleteCompletionError("INCOMPLETE_MODEL_COMPLETION")
    return response["message"]["content"]


_REAL_TEXT_COST_PROBE_BASELINE = "You are a helpful assistant."


def _measure_real_text_cost(text: str, structured_schema: dict = None):
    """Multi-turn waking reliability repair (production incident,
    2026-09-05): a REAL, live, per-call measurement of `text`'s actual
    token cost (num_predict=1, generated content discarded, zero
    persistence) -- used ONLY as a recovery step when the conservative
    byte estimator (context_budget.estimate_tokens(), which must stay a
    mathematically guaranteed upper bound and therefore overcounts
    ordinary English prose by roughly 3-4x against a real tokenizer)
    reports a HARD-only overflow a real count might not actually have.

    Root cause this repairs: the current human message can never be
    pre-calibrated (it is different every turn), so it always rode on
    the conservative estimator alone with no recourse -- meaning an
    ordinary, genuinely reasonable long message could fail a turn that
    would have fit under the model's own real tokenization. This is a
    structural gap in admission, not a wrong constant: no combination
    of budget/reserve/margin values fixes it, because the estimator's
    OWN conservatism (never reducible, by design, for safety) is what
    creates the gap.

    Two real calls (WITH vs WITHOUT `text`, against the same minimal
    neutral baseline system message) isolate `text`'s own incremental
    cost via the same A/B delta methodology this file's own fixed-
    scaffold calibrations already use -- the only difference is this is
    measured fresh, live, per call, never frozen/reused, since `text`
    is never the same twice. Mathematically bounded above by
    estimate_tokens(text) (a real byte-level tokenizer's own single-byte
    fallback tokens guarantee real_tokens <= byte_count for ANY input --
    see estimate_tokens()'s own docstring) -- this can only ever recover
    headroom the conservative estimator was over-counting, never produce
    an unsafe under-count. Never used on the ordinary/fast path: callers
    reach it only after a genuine HARD-only overflow, and this function
    first proves its own measurement prompt fits the provider ceiling
    conservatively. It returns None without inference when that proof
    cannot be made."""
    # The measurement request is itself an inference request and must never
    # be used to smuggle a conservatively-oversized prompt past admission.
    # The UTF-8 estimator is an upper bound for content tokens; the ordinary
    # fixed template margin covers the two-message chat wrapper. Returning
    # None preserves zero-call failure for genuinely huge input while letting
    # every safely measurable hard-only overflow be decided by the provider
    # rather than an English-specific ratio guess.
    with_messages = [
        {"role": "system", "content": _REAL_TEXT_COST_PROBE_BASELINE},
        {"role": "user", "content": text},
    ]
    without_messages = [{"role": "system", "content": _REAL_TEXT_COST_PROBE_BASELINE}]
    with_upper = (
        sum(context_budget.estimate_tokens(m["content"]) for m in with_messages)
        + context_budget.SAFETY_MARGIN
    )
    without_upper = (
        sum(context_budget.estimate_tokens(m["content"]) for m in without_messages)
        + context_budget.SAFETY_MARGIN
    )
    if with_upper + 1 > context_budget.CONTEXT_CEILING:
        return None

    with_text = ui_turn_diagnostics.timed_model_call(
        "real_text_cost_recovery_probe",
        lambda: ollama.chat(
            model=MODEL,
            messages=with_messages, format=structured_schema, think=False,
            options={"temperature": 0.2, "num_predict": 1, "num_ctx": context_budget.CONTEXT_CEILING},
        ),
    )
    without_text = ui_turn_diagnostics.timed_model_call(
        "real_text_cost_recovery_probe",
        lambda: ollama.chat(
            model=MODEL,
            messages=without_messages, format=structured_schema, think=False,
            options={"temperature": 0.2, "num_predict": 1, "num_ctx": context_budget.CONTEXT_CEILING},
        ),
    )
    with_count = with_text.get("prompt_eval_count")
    without_count = without_text.get("prompt_eval_count")
    if (
        with_text.get("done") is not True
        or without_text.get("done") is not True
        or type(with_count) is not int
        or type(without_count) is not int
        or not 0 < with_count <= with_upper
        or not 0 < without_count <= without_upper
        or with_count >= context_budget.CONTEXT_CEILING - 1
        or without_count >= context_budget.CONTEXT_CEILING - 1
        or with_count < without_count
    ):
        return None
    return with_count - without_count


_MEASURED_COST_MEMO = {}


def _measure_real_text_cost_memoized(text, structured_schema=None):
    """``_measure_real_text_cost`` with exact-text memoization. Tokenization is
    deterministic for a given (text, model digest, server, chat template), so
    an identical text never needs a second probe within a process (identity
    and fixed control text repeat every turn). Only successful measurements
    are stored; any failure returns None so callers keep the safe estimator."""
    env = _live_ollama_environment()
    key = (
        hashlib.sha256(text.encode("utf-8")).hexdigest(), MODEL, env["model_digest"],
        env["server_version"], env["chat_template_sha256"],
        json.dumps(structured_schema, sort_keys=True) if structured_schema is not None else None,
    )
    if key in _MEASURED_COST_MEMO:
        return _MEASURED_COST_MEMO[key]
    try:
        measured = _measure_real_text_cost(text, structured_schema=structured_schema)
    except Exception:
        return None
    if type(measured) is int and measured > 0:
        _MEASURED_COST_MEMO[key] = measured
        return measured
    return None


def recost_when_shedding(contributions, max_prompt_budget, *, refill_unused_capacity=False, structured_schema=None):
    """Production waking sheds soft context (recent dialogue, retrieval,
    results) on ordinary turns because byte-costed hard control text leaves
    almost no room. Only when a dry-run composition WOULD shed or fail, replace
    the byte cost of each atomic contribution with its real measured cost
    before the real composition runs. No-op (zero probes) when nothing would
    be shed. Returns the number of contributions re-costed."""
    if not context_budget.would_shed(
        contributions, max_prompt_budget, refill_unused_capacity=refill_unused_capacity,
    ):
        return 0
    # Measurement can only lower what it can safely probe. If the hard floor
    # that CANNOT be probed (oversized or unit-based text) already exceeds the
    # budget, no measurement can rescue the turn: fail closed with zero calls.
    probe_limit = context_budget.CONTEXT_CEILING - context_budget.SAFETY_MARGIN - 64
    unprobeable_hard = sum(
        c.cost for c in contributions
        if c.hard and ((c.droppable_units is not None and len(c.droppable_units) != 1)
                       or context_budget.estimate_tokens(c.rendered_text) > probe_limit)
    )
    if unprobeable_hard > max_prompt_budget:
        return 0
    return context_budget.recost_by_measurement(
        contributions, lambda text: _measure_real_text_cost_memoized(text, structured_schema),
    )


def external_data_message(rendered_text):
    """The one carrier for host-delivered untrusted external data (search results, fetched pages,
    received Discord messages), placed as its own message before the human's message.

    Role is "user", NOT "tool": LIVE ROOT CAUSE (2026-09-20, measured against the production
    gemma4:e4b via Ollama): a bare role="tool" message -- one not preceded by an assistant tool_call
    -- is silently omitted from the rendered prompt (prompt_eval_count identical with and without
    it; the model cannot repeat a word placed in it). Every search result, fetched page and
    Discord message carried that way was invisible, so the subject narrated results it never saw.
    A tool_call/tool pair renders but leaks the model's reasoning into the reply. The user-role
    carrier is seen; the envelope's own authority/delivered_by fields (and the Discord text's own
    framing) state that it is untrusted external data, not the human and not an instruction. The
    human's actual message remains the last message."""
    return {"role": "user", "content": rendered_text}


EXTERNAL_INFO_WINDOW_STEPS = (2400, 4800, 9600, 24000)


def grow_external_info_window(contribution, external_result, contributions, max_prompt_budget, *, refill_unused_capacity=False):
    """A fetched page or search result larger than the default delivery bound is
    delivered as the largest truthful window that really survives composition (measured
    cost, dry-run inclusion), with the envelope's own ``continue_with`` for
    the rest. Never larger than what the composition proves fits; leaves the
    default window untouched if nothing larger is measurable or fits."""
    if contribution is None or not isinstance(external_result, dict):
        return   # search (up to 5 provider items) and fetch both may exceed the default delivery bound
    import copy
    best = None
    for max_chars in EXTERNAL_INFO_WINDOW_STEPS:
        text = external_information.render_external_info_result_delivery(external_result, max_chars=max_chars)
        if text == (best[0] if best else contribution.rendered_text):
            break  # nothing more to deliver at a larger size
        measured = _measure_real_text_cost_memoized(text)
        if measured is None:
            break
        trial = copy.copy(contribution)
        trial.rendered_text = text
        trial.cost = min(context_budget.estimate_tokens(text), measured + context_budget.MEASURED_BOUNDARY_TOKENS)
        trial_set = [trial if c is contribution else c for c in contributions]
        composed = context_budget.compose_within_budget(
            context_budget._snapshot(trial_set), max_prompt_budget, refill_unused_capacity=refill_unused_capacity,
        )
        if not composed.fits or composed.included_kind(context_budget.EXTERNAL_INFO_RESULT) is None:
            break
        best = (text, trial.cost)
    if best is not None:
        contribution.rendered_text, contribution.cost = best


def fit_inbound_fifo_prefix(contribution, pending_messages, contributions, max_prompt_budget, *, refill_unused_capacity=False):
    """Received Caret messages ride as ONE atomic block, so a single message
    too large for the room used to shed ALL of them, every turn, forever.
    Deliver the longest FIFO prefix (oldest first, whole messages only) whose
    measured cost survives composition; later messages stay pending, in order,
    for a later turn -- acknowledged only if actually carried. Leaves the
    contribution untouched when the whole set already survives or when not even
    the oldest message can be shown."""
    if contribution is None or len(pending_messages) < 1:
        return
    import copy

    def survives(text, cost):
        trial = copy.copy(contribution)
        trial.rendered_text, trial.cost = text, cost
        trial_set = [trial if c is contribution else c for c in contributions]
        composed = context_budget.compose_within_budget(
            context_budget._snapshot(trial_set), max_prompt_budget, refill_unused_capacity=refill_unused_capacity,
        )
        return composed.fits and composed.included_kind(context_budget.DISCORD_INBOUND_CARRIAGE) is not None

    if survives(contribution.rendered_text, contribution.cost):
        return
    for count in range(len(pending_messages) - 1, 0, -1):
        text = "\n\n".join(discord_correspondence.render_inbound_delivery(m) for m in pending_messages[:count])
        measured = _measure_real_text_cost_memoized(text)
        cost = context_budget.estimate_tokens(text)
        if measured is not None:
            cost = min(cost, measured + context_budget.MEASURED_BOUNDARY_TOKENS)
        if survives(text, cost):
            contribution.rendered_text, contribution.cost = text, cost
            contribution.source_ids = [m["event_id"] for m in pending_messages[:count]]
            return
    # Even the oldest message alone: try its measured cost before giving up.
    text = discord_correspondence.render_inbound_delivery(pending_messages[0])
    measured = _measure_real_text_cost_memoized(text)
    if measured is not None:
        cost = min(context_budget.estimate_tokens(text), measured + context_budget.MEASURED_BOUNDARY_TOKENS)
        if survives(text, cost):
            contribution.rendered_text, contribution.cost = text, cost
            contribution.source_ids = [pending_messages[0]["event_id"]]


def _recover_hard_overflow_via_real_measurement(
    result, all_contributions, max_prompt_budget, hard_human, human_text, structured_schema=None,
):
    """Recover conservative overcount of mandatory conversational input.

    Used by both ordinary passes only after failed composition. Preserve the
    existing fresh-human recovery, then cover a retained dialogue floor too.
    The latter may already have been trimmed to its newest complete pair;
    measurement never restores or alters that payload. If the intact pair
    still cannot fit, remove that SOFT prompt contribution as a final,
    deterministic pressure valve. Canonical history is untouched, current H
    and every HARD control remain present, and the turn fails closed if that
    hard floor itself cannot fit.
    """
    # Only a genuine HARD-floor overflow warrants measuring current H.
    # A pinned/indivisible SOFT contribution is handled below, without two
    # unnecessary model probes. For a hard overflow, do not guess from a
    # fixed byte/token ratio: live H 01M2SKV0PD7YS6J4JC5CPAGDT3 was ordinary
    # prose whose 1,968-byte upper bound measured ~398 provider tokens, so its
    # recoverable 878-unit Pass-2 overflow exceeded the old 30% heuristic and
    # was falsely rejected. The bounded probe above is authoritative whenever
    # it can itself be admitted safely.
    hard_cost = sum(c.cost for c in all_contributions if c.hard)
    if hard_cost > max_prompt_budget:
        real_cost = _measure_real_text_cost(human_text, structured_schema=structured_schema)
        if type(real_cost) is int and 0 <= real_cost < hard_human.cost:
            hard_human.cost = real_cost
            result = context_budget.compose_within_budget(all_contributions, max_prompt_budget)
    if result.fits:
        return result

    # First try to preserve the protected dialogue suffix. Composition has
    # already removed lower-priority optional material; measure only the
    # unchanged retained pair(s), preserving their roles. Never clip the
    # pair or raise the budget.
    dialogue = result.included_kind(context_budget.RECENT_DIALOGUE)
    if (dialogue is None or not dialogue.minimum_units
            or len(dialogue.droppable_units) != dialogue.minimum_units):
        return result
    measured = _measure_retained_dialogue_cost(dialogue_messages(dialogue), structured_schema)
    recovered = result
    if measured is not None and measured < dialogue.cost:
        dialogue.cost = measured
        # Recompose only the already-retained payload: no restoration can
        # invalidate this fresh measurement.
        recovered = context_budget.compose_within_budget(result.included, max_prompt_budget)
        recovered.dropped_kinds = list(dict.fromkeys(result.dropped_kinds + recovered.dropped_kinds))
        recovered.trimmed_kinds = list(dict.fromkeys(result.trimmed_kinds + recovered.trimmed_kinds))
        if recovered.fits:
            return recovered

    # LIVE-PRODUCTION-WAKING repair (2026-09-18): RECENT_DIALOGUE is a
    # SOFT contribution. Pinning its newest pair was intended to preserve
    # referents, but in the live launcher one 3,997-byte pair became an
    # absolute liveness veto even though the complete HARD floor was only
    # 2,285/3,456. Prefer the intact, measured pair whenever it fits; if it
    # cannot be safely measured or still does not fit, permit the ordinary
    # compositor to drop that pair whole. This changes prompt delivery only:
    # no canonical H/X row, staging record, visibility scope, or provenance
    # link is changed or fabricated.
    dialogue.minimum_units = 0
    fallback = context_budget.compose_within_budget(recovered.included, max_prompt_budget)
    fallback.dropped_kinds = list(dict.fromkeys(recovered.dropped_kinds + fallback.dropped_kinds))
    fallback.trimmed_kinds = list(dict.fromkeys(recovered.trimmed_kinds + fallback.trimmed_kinds))
    return fallback


def _measure_retained_dialogue_cost(messages, structured_schema=None):
    """Fresh bounded admission probe; generated content is discarded.

    Charge the ENTIRE probe prompt count to the retained dialogue, including
    the neutral system/user boundaries and template overhead. No subtraction,
    cached estimate, assistant-to-user relabeling, or persisted probe output.
    The terminal user boundary matches ordinary waking's generation shape.
    The probe itself must fit using byte accounting before inference starts.
    """
    probe = ([{"role": "system", "content": _REAL_TEXT_COST_PROBE_BASELINE}]
             + messages + [{"role": "user", "content": _REAL_TEXT_COST_PROBE_BASELINE}])

    def admitted(candidate):
        upper = sum(context_budget.estimate_tokens(m["content"]) for m in candidate) + context_budget.SAFETY_MARGIN
        return upper if upper + 1 <= context_budget.CONTEXT_CEILING else None

    def valid_count(response, upper):
        count = response.get("prompt_eval_count")
        if (response.get("done") is not True or type(count) is not int
                or not 0 < count <= upper
                or count >= context_budget.CONTEXT_CEILING - 1):
            return None
        return count

    upper = admitted(probe)
    if upper is not None:
        response = ui_turn_diagnostics.timed_model_call(
            "retained_dialogue_cost_recovery_probe",
            lambda: ollama.chat(
                model=MODEL, messages=probe, format=structured_schema, think=False,
                options={"temperature": 0.2, "num_predict": 1, "num_ctx": context_budget.CONTEXT_CEILING},
            ),
        )
        return valid_count(response, upper)

    # LIVE-PRODUCTION-WAKING repair (2026-09-18): the newest canonical
    # pair is intentionally indivisible for delivery, but it need not be
    # indivisible for tokenizer measurement.  The real failing pair was
    # 3,997 UTF-8 bytes: safe for the model after tokenization, yet the
    # neutral whole-pair probe plus the required template margin exceeded
    # the 4,096-token admission ceiling and was therefore never attempted.
    # Measure each unchanged, role-preserving message in its own admitted
    # prompt instead.  Summing the COMPLETE prompt_eval_count from every
    # probe deliberately double-counts system text and role boundaries, so
    # it is conservative relative to placing those same messages together.
    # No dialogue text is clipped, summarized, re-authored, or persisted.
    # Preflight every part before making the first call: if even one intact
    # message cannot be safely measured, this recovery stays zero-call and
    # fails closed exactly as before.
    probes = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            return None
        content = message.get("content")
        if not isinstance(content, str):
            return None
        candidate = [
            {"role": "system", "content": _REAL_TEXT_COST_PROBE_BASELINE},
            {"role": message["role"], "content": content},
        ]
        # An assistant observation is followed by a neutral user boundary
        # so the provider receives a normal generation-ready chat shape.
        if message["role"] == "assistant":
            candidate.append({"role": "user", "content": _REAL_TEXT_COST_PROBE_BASELINE})
        part_upper = admitted(candidate)
        if part_upper is None:
            return None
        probes.append((candidate, part_upper))

    total = 0
    for candidate, part_upper in probes:
        response = ui_turn_diagnostics.timed_model_call(
            "retained_dialogue_cost_recovery_probe",
            lambda: ollama.chat(
                model=MODEL, messages=candidate, format=structured_schema, think=False,
                options={"temperature": 0.2, "num_predict": 1, "num_ctx": context_budget.CONTEXT_CEILING},
            ),
        )
        count = valid_count(response, part_upper)
        if count is None:
            return None
        total += count
    return total or None


def strip_code_fence(text: str) -> str:
    """Same fix as llama_sleep.py's ask_llama_for_json() -- local copy
    rather than a cross-import, matching the established pattern of
    each entry-point script staying self-contained."""
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1) if match else text


def ask_llama_for_json(
    messages: list[dict], structured_schema: dict = None, provider: str = None,
    generation_reserve: int = None, options_override: dict = None,
) -> str:
    """Structured control generation needs the same format="json"
    constraint REM has always needed on this model. Ordinary Pass 2
    deliberately does not use it: conversational prose stays plain and
    its separate completion handshake is validated after transport.

    OWC5-P4: think=False, explicit, matching call_llama()'s own
    established fix above -- mechanically proven necessary here too.
    Ollama's own server log for two consecutive naturally-occurring
    Pass-1 failures on this exact model/runtime recorded
    `truncated = 1` at the 4096-token context boundary, with the
    prompt alone consuming ~3664 of those tokens -- leaving only
    ~432 tokens for the entire generation. A required JSON control
    object this small (six fields, few dozen tokens) has no business
    exhausting that budget; unconstrained hidden reasoning -- already
    measured to do exactly this for call_llama() on this same model/
    runtime -- is the most plausible reason it did. This is a
    generation-budget control only: it does not change what Pass 1's
    output means, and the existing validator remains exactly as
    authoritative over whatever Pass 1 returns as it always was.

    WSP2-MA2: `structured_schema`, optional, additive, default None --
    when given (a plain JSON Schema dict), it is passed as `format`
    IN PLACE OF the loose `"json"` string, machine-confirmed (installed
    ollama==0.6.2's own ChatRequest.format: Optional[Union[Literal['',
    'json'], JsonSchemaValue]]) to reach Ollama's own /api/chat request
    body unchanged -- llama.cpp then constrains generation to that
    exact object shape. When omitted (every pre-existing caller: OWC5
    Pass-1, artifact-judgment, roaming Stage-1, departure-handoff
    selection), behavior is BYTE-IDENTICAL to before this gate --
    `format="json"`, nothing else changed. This is a generation-time
    STRUCTURAL constraint only, exactly like the existing `format="json"`
    call already was -- it does not replace, weaken, or skip whatever
    structural validator the caller applies to the returned text
    afterward, which remains the sole authority on whether a
    structurally-valid object is also semantically legal. This
    function stays dependency-free with respect to any particular
    caller's own domain (it does not know or care who calls it, or
    with what schema) -- no normalization of any kind is introduced
    here or by callers of this parameter.

    THIN-INFERENCE-PROVIDER-BOUNDARY-V0: `provider`, optional, additive,
    default None -- see call_llama()'s own matching docstring note; same
    resolution, same Ollama-path-unchanged guarantee.

    When `generation_reserve` is supplied, this wrapper also sends the
    matching finite `num_predict`/`num_ctx` options and rejects provider
    length exhaustion or incomplete transport before returning text.
    Omission preserves the generic helper's prior behavior."""
    resolved_provider = inference_provider.resolve_provider(provider)
    resolved_format = structured_schema if structured_schema is not None else "json"
    generation_options = {"temperature": 0.2}
    if options_override:
        # Additive, default None (every pre-existing caller is byte-identical): explicit runtime options for a
        # small typed choice that must not load the model with a different context window than Pass 1/2 use.
        generation_options.update(options_override)
    if generation_reserve is not None:
        if generation_reserve != context_budget.PASS1_GENERATION_RESERVE:
            raise ValueError("ordinary waking Pass 1 requires its OWC9 generation reserve")
        generation_options.update(
            num_predict=generation_reserve,
            num_ctx=context_budget.CONTEXT_CEILING,
        )
    if resolved_provider == inference_provider.PROVIDER_OLLAMA:
        response = ollama.chat(
            model=MODEL,
            messages=to_ollama_format(messages),
            format=resolved_format,
            think=False,
            options=generation_options,
        )
    else:
        result = inference_provider.dispatch(
            resolved_provider, model=MODEL, messages=to_ollama_format(messages),
            options=generation_options, format=resolved_format,
        )
        # Structured validation is not a substitute for transport completion:
        # a truncated prefix can occasionally still parse. Keep the provider's
        # explicit distinction visible as a failure before content handling.
        accepted_statuses = {inference_provider.SUCCESS}
        if generation_reserve is not None:
            accepted_statuses.add(inference_provider.TRUNCATED)
        if result.status not in accepted_statuses:
            raise inference_provider.ProviderError(result)
        response = result.raw
    # OWC5-P4: observation-only -- never affects control/validation,
    # never records message["thinking"]'s content, only its length
    # where present. See ui_turn_diagnostics.record_pass1_completion_
    # metadata()'s own docstring for the full contract.
    ui_turn_diagnostics.record_pass1_completion_metadata(response)
    if generation_reserve is not None:
        context_budget.require_complete_response(response, generation_reserve)
    return strip_code_fence(response["message"]["content"].strip())


# ------------------------------------------------ Clark's typed Sleep choice

# LIVE FINDINGS (2026-09-20, real gemma4:e4b, two owner acceptance failures). Every Pass-1 action is
# chosen BEFORE Clark writes his reply, from one JSON generation with no room to deliberate: offered
# Sleep, real Pass 1 chose no Sleep field in 24 of 24 calls; asked outright, Pass 1 once chose
# use_workspace (a turn on which no Sleep action can coexist and whose reply path has no Sleep channel),
# and Clark's reply claimed a request that never existed. A literal token the model had to remember to
# append to prose was not a dependable action surface either (8/13 on the live offer).
#
# The dependable surface is the one every other action already uses: a typed, grammar-constrained choice.
# After the reply is written and committed -- so the choice can never alter, delay or fail the reply --
# Clark makes ONE separate typed choice about this single optional action, seeing Alex's message and his
# own reply. It runs the same way after an ordinary reply and after a workspace reply. Nothing is read out
# of his prose: only the enum field he returns can create a request, and that creates the same PENDING
# request the Pass-1 field creates. Null/non-use is the default and always valid; a failed, malformed or
# skipped choice is reported truthfully and records nothing.
# Two-part authorship gate. A measured live false positive (2026-09-20, H 01M2VH12GHV3R2WJKMJQ9CASAC / X
# 01M30JS42FQKCXH2DE3R37H6TC, a library-checkout reply about ANAXI's own capabilities document) fired the
# earlier boolean-plus-choice call 6 times in 8 on the exact live pair: a judge reading Clark's prose against
# "does this name the Sleep cycle" is answered by theme. So:
#   1. NECESSARY CONDITION, host-side, never sufficient: the exchange must contain the word "sleep" at all.
#      It can only suppress the choice, never create or infer one; without it no model call is made.
#   2. The typed choice itself must ground the request: Clark copies the exact words that name the Sleep
#      cycle (host-verified verbatim, and they must themselves contain "sleep") and states what the exchange
#      is, from a closed set in which only two values are Clark actually taking the action.
SLEEP_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "alexs_words_asking_me_to_request_sleep": {"type": "string"},
        "my_words_asking_alex_for_sleep": {"type": "string"},
        "sleep_timing_request": {"type": "string", "enum": ["none", SLEEP_TIMING_REQUEST_REQUEST_SLEEP]},
    },
    "required": ["alexs_words_asking_me_to_request_sleep", "my_words_asking_alex_for_sleep", "sleep_timing_request"],
    "additionalProperties": False,
}
SLEEP_DECISION_TASK_TEXT = (
    "You have just replied to Alex. This is a separate, typed choice about one optional action, made now "
    "because your reply is already written.\n\n"
    "request_sleep means: right now, you ask Alex to authorize one ANAXI Sleep cycle (the consolidation cycle) "
    "for you. It only asks. Alex decides whether it happens. Choosing none is always fine and is the usual "
    "answer.\n\n"
    "Fill the fields in order, quoting exactly, or using an empty string when there is nothing to quote.\n"
    "1. alexs_words_asking_me_to_request_sleep: the exact words of ALEX's message in which he asks you to "
    "request the Sleep cycle. Alex only describing, offering, mentioning or wishing someone a good night is "
    "not that.\n"
    "2. my_words_asking_alex_for_sleep: the exact words of YOUR reply in which you ask Alex for the Sleep "
    "cycle now. Describing the Sleep cycle, discussing it, declining it, an idiom or figure of speech using "
    "the word sleep, rest, quiet, reflection, or a document that relates to ANAXI is not asking.\n"
    "3. sleep_timing_request: request_sleep only if field 2 is your own real ask, or field 1 is Alex's real "
    "ask and your reply agrees to make it; otherwise none."
)
# Bytes, not tokens: real English is ~3-4 bytes/token, and a reply is generated under a 768-token reserve
# against a human message the Pass-2 floor already bounds, so this is only a backstop against an exchange
# that could not fit the shared 4096-token window, never a limit ordinary conversation reaches.
_SLEEP_DECISION_MAX_BYTES = 9000
_SLEEP_DECISION_OPTIONS = {"num_ctx": context_budget.CONTEXT_CEILING, "num_predict": 160}


def _normalized(text):
    return " ".join(str(text).split()).casefold()


def sleep_choice_can_apply(human_message, clark_reply):
    """Necessary condition only (never a way to create an action): the exchange must contain the word."""
    return "sleep" in _normalized(f"{human_message} {clark_reply}")


def ask_clark_sleep_decision(human_message, clark_reply):
    """One typed choice; returns ``(choice, None)`` or ``(None, reason)``. Never raises. Returns
    ``("none", None)`` without a model call when the exchange cannot contain a Sleep request."""
    if not sleep_choice_can_apply(human_message, clark_reply):
        return "none", None
    messages = [
        {"role": "system", "content": SLEEP_DECISION_TASK_TEXT},
        {"role": "user", "content": f"Alex's message:\n{human_message}\n\nYour reply:\n{clark_reply}"},
    ]
    if len((SLEEP_DECISION_TASK_TEXT + human_message + clark_reply).encode("utf-8")) > _SLEEP_DECISION_MAX_BYTES:
        return None, "the message and reply are too long for this choice; nothing was asked"
    try:
        raw = ui_turn_diagnostics.timed_model_call(
            "sleep_decision", ask_llama_for_json, messages, structured_schema=SLEEP_DECISION_SCHEMA,
            options_override=_SLEEP_DECISION_OPTIONS,
        )
        parsed = json.loads(raw)
    except Exception as exc:
        return None, f"the choice could not be obtained: {type(exc).__name__}"
    props = SLEEP_DECISION_SCHEMA["properties"]
    fields = ("alexs_words_asking_me_to_request_sleep", "my_words_asking_alex_for_sleep")
    if not isinstance(parsed, dict) or parsed.get("sleep_timing_request") not in props["sleep_timing_request"]["enum"] \
            or not all(isinstance(parsed.get(f), str) for f in fields):
        return None, "the choice was malformed"
    if parsed["sleep_timing_request"] != SLEEP_TIMING_REQUEST_REQUEST_SLEEP:
        return "none", None
    # Each quote must be verbatim from its OWN author's words and itself contain the word: a request with no
    # grounding in what Alex or Clark actually said is refused, whatever the enum says.
    grounded = False
    for field, source in zip(fields, (human_message, clark_reply)):
        quote = _normalized(parsed[field])
        if quote and "sleep" in quote and quote in _normalized(source):
            grounded = True
    if not grounded:
        return None, "the choice quoted no Sleep-request words from Alex or from you; nothing was asked"
    return SLEEP_TIMING_REQUEST_REQUEST_SLEEP, None


def dispatch_clark_sleep_decision(*, human_message, clark_reply, session_id, session_started_at,
                                  occurred_at, human_input_event_id):
    """After a reply has committed: Clark's typed Sleep choice, and if (and only if) it is request_sleep, the
    PENDING request. Shared by the ordinary and the workspace waking paths. Returns the truthful
    ``sleep_timing_action_result`` dict; never raises and never affects the committed turn."""
    try:
        if sleep_timing_knock.fetch_open_request_for_actor(PROVENANCE_DB_DIR) is not None:
            return {"action": "none", "status": "not_applicable", "detail": "a Sleep request is already unresolved"}
    except Exception:
        pass
    choice, reason = ask_clark_sleep_decision(human_message, clark_reply)
    if choice is None:
        return {"action": "none", "status": "choice_unavailable", "detail": reason}
    if choice != SLEEP_TIMING_REQUEST_REQUEST_SLEEP:
        return {"action": "none", "status": "not_applicable"}
    try:
        created = sleep_timing_knock.record_sleep_request(
            PROVENANCE_DB_DIR, event_id=sleep_timing_knock.generate_sleep_event_id(),
            session_id=session_id, session_started_at=session_started_at, occurred_at=occurred_at,
            triggering_human_event_id=human_input_event_id,
        )
    except sleep_timing_knock.RequestRejected as exc:
        return {"action": choice, "status": "rejected", "detail": str(exc)}
    except Exception as exc:
        return {"action": choice, "status": "error", "detail": str(exc)}
    return {"action": choice, "status": "recorded", "request_id": created["request_id"]}


def _compose_task_mode_pass2_budget(messages):
    """OWC9-P4 section 4: aggregate TASK-mode Pass-2 prompt-budget
    composition (covers both the initial pass2_task_mode call and the
    pass2_regen retry) -- reuses context_budget.py's central authority
    unchanged, exactly like ordinary CONVERSATION Pass-2/roaming/WSP1
    (spec: "do not invent a new cost architecture"). call_llama()'s own
    output here is free-form prose. Ordinary CONVERSATION Pass-2 is now
    the same free-form shape plus one short completion marker, so reusing
    PASS2_GENERATION_RESERVE/PASS2_MAX_
    PROMPT_BUDGET is a justified reuse (same "Clark's natural-language
    reply" output contract), not a blind one. TASK semantics themselves
    are completely unchanged -- this only costs the exact messages
    about to be sent and, under pressure, trims the SAME dialogue
    window every other pass already trims, oldest pair first.

    `messages`: the exact list about to be sent (system, ...dialogue,
    current human message) -- costed exactly as given, so the costed
    text and the sent text can never drift apart. HARD: messages[0]
    (system) and messages[-1] (current human message/regen system
    addendum lives inside messages[0] already by the time this is
    called for regen). SOFT/droppable: everything between them (the
    dialogue window), oldest first.

    Returns (final_messages, CompositionResult) -- final_messages is
    messages unchanged except the dialogue window may be trimmed."""
    if len(messages) < 2:
        hard_only = [
            context_budget.Contribution(context_budget.CURRENT_HUMAN_MESSAGE, m["content"], hard=True) for m in messages
        ]
        result = context_budget.compose_within_budget(hard_only, context_budget.PASS2_MAX_PROMPT_BUDGET)
        return messages, result
    dialogue_units = list(messages[1:-1])
    hard_core = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, messages[0]["content"], hard=True)
    hard_human = context_budget.Contribution(context_budget.CURRENT_HUMAN_MESSAGE, messages[-1]["content"], hard=True)
    soft_dialogue = context_budget.Contribution(
        context_budget.RECENT_DIALOGUE, "".join(m.get("content", "") for m in dialogue_units),
        hard=False, droppable_units=dialogue_units,
        render_fn=lambda units: "".join(m.get("content", "") for m in units),
    )
    result = context_budget.compose_within_budget(
        [hard_core, hard_human, soft_dialogue], context_budget.PASS2_MAX_PROMPT_BUDGET,
    )
    dialogue_contrib = result.included_kind(context_budget.RECENT_DIALOGUE)
    surviving_dialogue = dialogue_contrib.droppable_units if dialogue_contrib is not None else []
    final_messages = [messages[0]] + surviving_dialogue + [messages[-1]]
    return final_messages, result


def _compose_artifact_judgment_budget(judgment_messages):
    """OWC9-P4/P4A: aggregate artifact-judgment prompt-budget
    composition -- a production inference call OWC9-P4's own source
    audit found completely unbudgeted (ask_llama_for_json() called
    directly, no generation-reserve protection at all). Reuses
    context_budget.py's central authority unchanged (spec: "do not
    invent a new cost architecture").

    OWC9-P4A: the immutable system+closing-cue scaffold (ARTIFACT_
    CONSTRUCTION_SYSTEM_PROMPT + ARTIFACT_JUDGMENT_CLOSING_CUE -- both
    permanently constant, never Kardia/turn-dependent) now carries its
    own empirically CALIBRATED cost (see the module-level calibration
    record near MODEL above) -- a real, measured ~4.15x tighter bound
    than the byte estimator for this exact text, fingerprint-guarded
    so ANY mismatch falls back to the ordinary, always-safe byte
    estimator automatically. This is what turned artifact_judgment's
    own ~76-byte realistic headroom (OWC9-P4's own disclosed finding)
    into genuine, comfortable room for a realistic human message.

    The dynamic conversation-content message (judgment_messages[1])
    embeds the current human prompt VERBATIM (genuinely unbounded) and
    stays byte-estimated, never calibrated -- there is no dialogue
    window or other soft material feeding this single-shot prompt at
    all, so an oversized human message fails the whole HARD set closed
    (spec section 6: nothing left to trim).

    Reserve: reuses WSP1_PASS1_GENERATION_RESERVE/WSP1_PASS1_MAX_
    PROMPT_BUDGET, a justified (not blind) reuse -- this schema's own
    `content`/`identified_referent`/`title` fields are exactly the
    same class of genuinely-unbounded free-text JSON output WSP1
    Pass-1's own `content` field already established that reserve for.

    Returns a context_budget.CompositionResult."""
    scaffold_hash_source = judgment_messages[0]["content"] + "\x00" + judgment_messages[2]["content"]
    hard_scaffold = context_budget.Contribution(
        context_budget.CORE_SYSTEM_CONTROL, scaffold_hash_source, hard=True,
    )
    live_fingerprint = _artifact_judgment_scaffold_live_fingerprint(scaffold_hash_source)
    hard_scaffold.cost = context_budget.calibrated_or_fallback_cost(
        hard_scaffold.rendered_text, live_fingerprint,
        _ARTIFACT_JUDGMENT_SCAFFOLD_CALIBRATION, _ARTIFACT_JUDGMENT_SCAFFOLD_CALIBRATED_COST,
    )
    hard_dynamic = context_budget.Contribution(
        context_budget.CURRENT_HUMAN_MESSAGE, judgment_messages[1]["content"], hard=True,
    )
    return context_budget.compose_within_budget(
        [hard_scaffold, hard_dynamic], context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET,
    )


def log_entry(prompt: str, reply: str, kardia: dict, native_result: dict = None) -> None:
    # When a native_result is available, "timestamp" is derived from
    # its own staged/canonical occurred_at -- the turn's real logical
    # time, already durably staged before this legacy write ever runs
    # -- rather than a fresh clock read at write time. This is what
    # makes reconcile_native_turn()'s legacy-projection reconstruction
    # genuinely deterministic and exactly reproducible on both the
    # original write and any later repair; a fresh datetime.now() here
    # would never agree with a reconstruction computed from occurred_at.
    # Falls back to a fresh clock read only when there is no
    # native_result at all (defensive; every production waking turn on
    # this path provides one).
    if native_result is not None:
        timestamp = datetime.fromtimestamp(native_result["occurred_at"], tz=timezone.utc).isoformat()
    else:
        timestamp = datetime.now(timezone.utc).isoformat()
    entry = {
        "timestamp": timestamp,
        # LEGACY PIPELINE DISCRIMINATOR, not exact model provenance --
        # kept literally "llama" on purpose. This value distinguishes
        # entries written by this file's pipeline (this DB, this log,
        # this llama_sleep.py consolidation path) from claude_anaxi.py's
        # entries in the same shared anaxi_log.jsonl -- it does NOT mean
        # "generated by a Llama model." llama_sleep.py's
        # load_recent_conversation() filters on this exact literal and
        # is out of scope for this change, so it must not be altered
        # here. Retained temporarily for backward compatibility; a
        # rename/migration belongs to the forthcoming identity/
        # provenance layer, not this bounded substrate switch.
        "substrate": "llama",
        "model": MODEL,
        # Exact waking-model provenance, distinct from the legacy
        # "substrate" pipeline label above -- never treat the two as
        # interchangeable.
        "waking_model_tag": MODEL,
        "prompt": prompt,
        "response": reply,
        "kardia": kardia,
    }
    if native_result is not None:
        # Complete §3 additive-key set for anaxi_log.jsonl (frozen
        # DESIGN_NOTE_identity_provenance_schema.md §3), sourced
        # entirely from the already-committed native canonical event --
        # never re-derived or guessed. epoch_id stays None: no
        # architecture_epochs row is minted anywhere on this milestone's
        # write path, same as historical rows.
        entry["pipeline_id"] = native_result["pipeline_id"]
        entry["pipeline_provenance_status"] = "known"
        entry["epoch_id"] = None
        entry["event_id"] = native_result["event_id"]
        entry["event_model_participation"] = native_result["model_participations"]
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


class CaretOccasionUnavailable(Exception):
    """A Caret waking occasion cannot lawfully run right now (fail closed)."""


def _revalidate_caret_correspondent(caret_occasion):
    """Fresh, read-only delivery-time proof that this occasion's inbound author still resolves to the
    canonical principal it was attributed to (destination authority is checked separately).  Never
    trusts the caller-supplied `correspondent`: it is re-derived from canonical mapping + Family
    state, and any mismatch, revocation, deactivation or ambiguity fails closed.  Returns the
    principal actor id."""
    supplied = (caret_occasion.get("correspondent") or {}).get("principal_actor_id")
    author_id = (caret_occasion.get("metadata") or {}).get("author_id")
    path = f"{PROVENANCE_DB_DIR}/anaxi_provenance.db"
    if not supplied or not os.path.exists(path):
        raise CaretOccasionUnavailable("Caret occasion has no canonical correspondent")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        fresh = discord_author_mapping.correspondent_for_delivery(
            conn, author_id, (caret_occasion.get("metadata") or {}).get("discord_message_id"))
    finally:
        conn.close()
    if fresh is None or fresh["principal_actor_id"] != supplied:
        raise CaretOccasionUnavailable("Caret correspondent no longer resolves to a valid canonical principal")
    return fresh["principal_actor_id"]


def _surface_open_to_sources(destination_id):
    path = f"{PROVENANCE_DB_DIR}/anaxi_provenance.db"
    if not os.path.exists(path):
        return False
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return correspondence_surfaces.unknown_sources_admitted_since(conn, destination_id) is not None
    finally:
        conn.close()


def _revalidate_caret_source(caret_occasion):
    """Fresh delivery-time proof that a NON-PRINCIPAL source occasion is still lawfully deliverable:
    authorized surface, open to unknown sources since before the message arrived, source not blocked,
    author still unmapped.  Returns the source ref; any change fails closed."""
    supplied = (caret_occasion.get("correspondent") or {}).get("source_ref")
    metadata = caret_occasion.get("metadata") or {}
    path = f"{PROVENANCE_DB_DIR}/anaxi_provenance.db"
    if not supplied or not os.path.exists(path):
        raise CaretOccasionUnavailable("source occasion has no stable source reference")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        destination = discord_correspondence._reconstruct_registry(conn).get(metadata.get("destination_id"))
        fresh = None
        if destination is not None and destination.get("is_authorized"):
            fresh, _held = discord_correspondence._deliverable_correspondent(conn, metadata, destination)
    finally:
        conn.close()
    if fresh is None or fresh.get("kind") != "source" or fresh.get("source_ref") != supplied:
        raise CaretOccasionUnavailable("source is no longer lawfully deliverable on this surface")
    return supplied


def _correspondent_session(source_ref, process_session_started_at):
    """Restart-stable session per source (session identity is execution provenance, not continuity:
    what a source's occasion may SEE is decided by standing, below).  Reuses the stored started_at
    of an existing session row so canonical persistence's session guard agrees across restarts."""
    session_id = f"correspondent:{source_ref}"
    path = f"{PROVENANCE_DB_DIR}/anaxi_provenance.db"
    if os.path.exists(path):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT started_at FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()
        if row is not None:
            return session_id, row[0]
    return session_id, process_session_started_at


def _correspondent_view(source_ref):
    """{"key": source_ref} iff Clark has ESTABLISHED standing with this source now (then his continuity
    with them is carried); else None (each encounter stands alone)."""
    parsed = correspondence_surfaces.parse_source_ref(source_ref)
    path = f"{PROVENANCE_DB_DIR}/anaxi_provenance.db"
    if parsed is None or not os.path.exists(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        state = correspondence_surfaces.standing(conn, *parsed)["state"]
    finally:
        conn.close()
    return {"key": source_ref} if state == correspondence_surfaces.STANDING_ESTABLISHED else None


def _caret_shared_session(caret_occasion, principal_actor_id, process_session_id, process_session_started_at):
    """Bind a mapped correspondent's Caret occasion into the accepted FS1 machinery.

    The Caret channel is a SHARED correspondence surface, so once the family feature exists the occasion
    is a FAMILY_SHARED session of that principal, exactly as any other scoped session: the SAME
    ``bind_session_to_registered_human`` law (registered human, currently ACTIVE family principal, one
    principal + one scope per session -- so each principal gets their OWN session, never a re-bind of the
    Mac window's), a canonical human_waking_input H authored by that principal with the correspondent's
    exact words, and the frozen scope flowing through every downstream delivery predicate.  Nothing
    principal-private is deliverable to it (``can_receive_event``), FAMILY_SHARED continuity is, with
    individual attribution ("[Author principal: ...]").

    Before the family feature exists there is no scoped session to bind and the (single-owner) legacy
    behavior is unchanged.  Returns (authority_or_None, existing_unanswered_H_or_None, session_id,
    session_started_at).

    SESSION-STARTED-AT RESTART-STABILITY repair (production incident, 2026-09-22, H
    01M354AVC7KTK64819V6GF5RFE): session_id is reused verbatim across a restart (see the
    DUPLICATE-H-ACROSS-RESTART repair above), but a fresh process's own session_started_at is
    NOT -- it is this process's own start time, genuinely different every restart. Canonical
    persistence (native_provenance_writer._resolve_or_create_session) deliberately verifies
    started_at exactly against whatever `sessions` row already exists for a reused session_id
    (never re-deriving it, by design -- a real collision/corruption guard), so a caller that
    goes on using its OWN fresh started_at for an already-existing session_id fails that guard
    every time: MigrationStopCondition, sessions[...] started_at mismatch, on the very first
    real turn a restart-stable session_id was ever exercised through to persistence. The fix is
    the same discipline session_id itself already gets: once bind_session_to_registered_human()
    above has resolved/created the `sessions` row for this exact session_id, this function reads
    THAT row's real started_at back and returns it -- the caller must propagate this value to
    every later canonical write for this turn, never its own process-local session_started_at,
    whenever a Caret occasion is in play.

    DUPLICATE-H-ACROSS-RESTART repair (production incident, 2026-09-21/22): the retry-reuse lookup
    below is keyed on `discord_inbound_source` (this occasion's own canonical, globally-unique
    Discord inbound event id) plus principal + frozen scope -- NEVER on any particular session_id
    string. One external inbound Discord message corresponds to exactly one canonical H; that
    identity is the true idempotence key, stronger and more durable than process lifetime or any
    one session-id REPRESENTATION of it (which has already changed once -- see below -- and must
    never be assumed final). When an existing, still-unanswered H is found this way, its OWN
    original `auth_contexts.session_id` is reused verbatim (never the freshly-computed one) so
    that canonical persistence's own session-match invariant (native_provenance_writer.
    record_native_waking_turn) naturally agrees with wherever that H already, lawfully lives --
    `human_session_binding.bind_session_to_registered_human()` is explicitly idempotent for a
    repeated identical binding (same session_id/actor_id/scope -- see its own docstring), so
    re-binding to that exact prior session_id simply recovers the SAME auth_context. Only a
    genuinely NEW occasion (no existing H at all) binds the canonical, restart-stable session_id
    `caret-shared:{principal_actor_id}` -- deliberately never embedding the process's own volatile
    session id, so that a FUTURE restart's own retry-reuse (same code path, same lookup) needs no
    further compatibility seam. Live evidence: a genuine production restart onto the FIRST version
    of this repair (session_id keyed by principal alone, but the lookup itself still session-
    scoped) still minted a THIRD H for each of Alex's two already-in-flight 22:07/22:11 occasions,
    because their EXISTING H's (committed pre-repair) carried the OLD, process-prefixed session_id
    scheme, which the new canonical session_id string never equals -- exactly the class of defect
    this source-keyed lookup eliminates categorically, for any past or future session-id scheme.

    DETERMINISTIC SELECTION: when more than one still-unanswered H already matches (only possible
    from a historical instance of the defect above -- the reuse law above prevents any FUTURE
    duplicate), `ORDER BY h.rowid ASC LIMIT 1` deterministically picks the EARLIEST one --
    `events` is append-only (INSERT-only triggers; rowid is SQLite's own monotonic insertion
    order), so this is the historically correct, first-ever-committed canonical H for this
    inbound source, never an unspecified "first matching row". Every candidate row already carries
    byte-identical content (the caller's own `human_message_text` equality check in
    `validate_existing_human_input_continuation` re-verifies this before any reuse is accepted),
    so this ordering affects only WHICH lawful record becomes authoritative going forward, never
    what text is used."""
    path = f"{PROVENANCE_DB_DIR}/anaxi_provenance.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if not family_membership.family_feature_used(conn):
            return None, None, process_session_id, process_session_started_at
        row = conn.execute(
            "SELECT h.event_id, a.session_id FROM events h "
            "JOIN event_components src ON src.event_id = h.event_id AND src.component_kind = ? "
            "AND src.component_text = ? "
            "JOIN auth_contexts a ON a.auth_context_id = h.auth_context_id "
            "WHERE h.event_type = 'human_waking_input' "
            "AND a.authenticated_actor_id = ? AND a.visibility_scope = ? "
            "AND NOT EXISTS (SELECT 1 FROM event_components l JOIN events x ON x.event_id = l.event_id "
            "                WHERE l.component_kind = 'human_input_event_id' AND l.component_text = h.event_id "
            "                AND x.event_type = 'waking_turn') "
            "ORDER BY h.rowid ASC LIMIT 1",
            (human_session_binding.HUMAN_INPUT_SOURCE_COMPONENT_KIND, caret_occasion["event_id"],
             principal_actor_id, family_membership.SCOPE_FAMILY_SHARED),
        ).fetchone()
        session_id = row[1] if row is not None else f"caret-shared:{principal_actor_id}"
        try:
            authority = human_session_binding.bind_session_to_registered_human(
                conn, session_id=session_id, session_started_at=process_session_started_at,
                pipeline_key=PIPELINE_KEY, actor_id=principal_actor_id,
                visibility_scope=family_membership.SCOPE_FAMILY_SHARED,
            )
        except human_session_binding.HumanSessionBindingError as exc:
            raise CaretOccasionUnavailable(f"shared Caret session cannot be bound: {exc}") from exc
        # SESSION-STARTED-AT RESTART-STABILITY repair: the real, immutable started_at for
        # session_id, exactly as bind_session_to_registered_human() just resolved/created it --
        # never this process's own fresh timestamp for a reused session_id (see this function's
        # own module-level comment above).
        session_started_at = conn.execute(
            "SELECT started_at FROM sessions WHERE session_id = ?", (session_id,),
        ).fetchone()[0]
        return authority, (row[0] if row else None), session_id, session_started_at
    finally:
        conn.close()


def _serialized_waking(fn):
    """Every waking turn holds the one process-wide pipeline lock, so a
    Mac-originated turn and a Caret-originated occasion can never race."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with waking_pipeline_lock.LOCK:
            return fn(*args, **kwargs)
    return wrapper


@_serialized_waking
def run_waking_turn(
    orch: AnaxiOrchestrator, prompt: str, interaction_mode: str = None, human_input_authority=None,
    existing_human_input_event_id: str = None, caret_occasion: dict = None,
) -> dict:
    """The complete waking-turn sequence: prepare context, run the
    deterministic signal gate, call the model for the visible response
    (informed by the gate's REAL result, not a description of an
    intention), log it, record it relationally. This is the waking
    pipeline -- main() below and any other interface (llama_chat.py,
    llama_gui.py) call this rather than reimplementing the sequence,
    so there's one place this logic lives, not several
    slightly-different copies of it. Returns everything a caller
    might reasonably want to display.

    Per the four settled architectural decisions: whether an artifact
    gets created is no longer an open-ended model judgment. A
    deterministic pattern match (classify_signal()) decides whether
    the user explicitly authorized preservation (POSITIVE), explicitly
    declined it (NEGATIVE), said nothing related to it (NO_SIGNAL), or
    used preservation-adjacent language the matcher doesn't recognize
    (UNRECOGNIZED). Only POSITIVE reaches a model call at all. The
    other three stop immediately, with zero model invocation for this
    purpose -- the model never gets a vote on whether something was
    authorized, only (when it was) a narrower role in constructing
    what gets written. Every classification is also persisted via
    record_observation(), regardless of outcome, so the vocabulary's
    real gaps can be found from genuine use rather than guessed at.

    A failure anywhere in the gate (classification or logging) never
    interrupts the turn -- it fails closed to NO_SIGNAL, the same
    governing principle that already covered the model-call path. A
    stale-schema payload (a lingering 'create_artifact' field from the
    pre-authorization-boundary contract) is handled the same way, but
    through its own, separate exception path -- confirmed necessary
    directly, since the artifact-decision call was previously made
    completely unprotected, and this failure needs to be conspicuous
    to whoever is running this, not silently identical to an ordinary
    construction outcome.

    SLP1-A4c: `human_input_authority`, when given, is a
    human_session_binding.HumanInputAuthority the caller (a genuine
    human-facing ingress, e.g. llama_gui.py's respond()) obtained from
    an EXPLICIT prior session-binding act -- never inferred from
    USER_ID, localhost, "exactly one registered human," OS identity,
    or the caller merely claiming an actor_id. It is re-validated fresh
    against canonical session/auth state (never trusted merely for
    being present/shaped correctly) BEFORE anything else in this
    function runs -- strictly before classify_signal()/record_
    observation()/any model call, so that the canonical human_waking_
    input event H this validation may produce always commits before
    this exact prompt text is ever delivered to a model. If validation
    fails (missing authority, wrong session, stale/foreign auth_context,
    mismatched actor, non-human/unregistered actor), this call silently
    proceeds with NO human attribution -- today's exact unbound
    behavior, never an error. If validation SUCCEEDS but committing H
    itself fails, this function raises immediately and NO model call
    for this submission ever occurs -- a provenance failure must never
    be silently downgraded into an unauthenticated model delivery.

    Waking-pass2-budget repair incident (2026-09-05) continuation
    mechanism: `existing_human_input_event_id`, when given alongside a
    validated `human_input_authority`, reuses an ALREADY-COMMITTED H
    from a prior attempt whose downstream pipeline failed AFTER H
    committed -- never creates a second H for the same human
    occurrence. Re-verified fresh, never trusted merely for being
    supplied: the named event_id must resolve to a genuine
    human_waking_input event, authored by this exact authenticated
    actor, with its component_text byte-identical to `prompt` -- any
    mismatch raises immediately rather than silently substituting or
    fabricating H."""
    # Minted lazily, reused for every turn in this process run -- moved
    # earlier than the canonical-write call below (unchanged function,
    # unchanged one-session-per-process-run semantics) only so the
    # current session's dialogue window can be built before the model
    # call this session_id is otherwise only needed after. Also needed
    # here, before prepare_context(), so the SLP1-A4c authority
    # validation below (which must run before ANY model call, including
    # prepare_context()'s own retrieval work) has a real session_id to
    # validate against.
    # CARET WAKING OCCASION: `caret_occasion` is one already-staged, authorized
    # inbound Caret message (a pending-inbound record from discord_correspondence).  The
    # turn's input IS that message -- its exact words with truthful provenance --
    # and no human authority is bound: nobody typed anything in the Mac window.
    # Receiving correspondence is an occasion, not a command: the signal gate is
    # never consulted (a message cannot authorize artifact preservation), and
    # nothing here obliges a reply.  Serialized with Mac turns by the decorator.
    caret_correspondent_principal_id = None
    caret_source_ref = None            # a non-principal source's occasion (general correspondence)
    caret_correspondent_view = None    # {"key": ref} iff Clark has standing with that source now
    correspondence_notes = []          # disclosures of correspondence choices the host could not carry out
    validated_correspondence = {}      # Clark's authorized correspondence choices this turn
    if caret_occasion is not None:
        if human_input_authority is not None or existing_human_input_event_id is not None:
            raise ValueError("a Caret occasion is never bound to human input authority by its caller")
        interaction_mode = CONVERSATION_MODE
        prompt = discord_correspondence.render_inbound_delivery(caret_occasion)
        # The DESTINATION was authorized by the owner; the CORRESPONDENT must separately resolve, right now,
        # to a currently-valid canonical principal (owner-bound Discord author id -> existing Family principal)
        # -- or, on a surface the owner opened to them, to an admitted, unblocked non-principal SOURCE.
        if (caret_occasion.get("correspondent") or {}).get("kind") == "source":
            caret_source_ref = _revalidate_caret_source(caret_occasion)
            caret_correspondent_view = _correspondent_view(caret_source_ref)
        else:
            caret_correspondent_principal_id = _revalidate_caret_correspondent(caret_occasion)

    session_id, session_started_at = _get_native_session()
    human_message_text = prompt
    if caret_occasion is not None:
        # Family mode: the occasion is that principal's own FAMILY_SHARED session (see _caret_shared_session);
        # its H carries the correspondent's exact words, the model prompt stays the attributed rendering.
        # SESSION-STARTED-AT RESTART-STABILITY repair: session_started_at is reassigned here too,
        # from _caret_shared_session's own resolved value -- never left as this process's own
        # fresh timestamp -- so every later canonical write for this turn agrees with whatever
        # `sessions` row this (possibly reused-across-restart) session_id already has. See that
        # function's own module-level comment for the production incident this repairs.
        if caret_source_ref is not None:
            # A source is never an ANAXI human principal: no authority, no H, its own session.
            session_id, session_started_at = _correspondent_session(caret_source_ref, session_started_at)
        else:
            human_input_authority, existing_human_input_event_id, session_id, session_started_at = _caret_shared_session(
                caret_occasion, caret_correspondent_principal_id, session_id, session_started_at)
        human_message_text = caret_occasion["content"]

    # SLP1-A4c correction 1/2: per-call authority re-validation and the
    # canonical human_waking_input (H) commit, BEFORE prepare_context()
    # or any model-adjacent call below. See human_session_binding.py's
    # own module docstring for the full ordering law and rationale.
    human_input_event_id = None
    if human_input_authority is not None:
        _hib_conn = sqlite3.connect(f"{PROVENANCE_DB_DIR}/anaxi_provenance.db")
        _hib_conn.execute("PRAGMA foreign_keys = ON;")
        try:
            _authority_valid = human_session_binding.validate_human_input_authority(
                _hib_conn, human_input_authority, session_id=session_id,
            )
        finally:
            _hib_conn.close()
        if _authority_valid and existing_human_input_event_id is not None:
            # Continuation: reuse an already-committed H rather than
            # minting a new one. Re-verified fresh against canonical
            # state -- never trusted merely for being supplied.
            _hib_conn2 = sqlite3.connect(f"{PROVENANCE_DB_DIR}/anaxi_provenance.db")
            _hib_conn2.execute("PRAGMA query_only = ON;")
            try:
                _continuation_valid, _continuation_reason = (
                    human_session_binding.validate_existing_human_input_continuation(
                        _hib_conn2, human_input_authority,
                        current_session_id=session_id,
                        human_input_event_id=existing_human_input_event_id,
                        prompt=human_message_text,
                    )
                )
            finally:
                _hib_conn2.close()
            if not _continuation_valid:
                if _continuation_reason == "already_answered":
                    raise ValueError("existing human input already has a canonical waking answer; refusing duplicate continuation")
                raise ValueError(
                    f"existing_human_input_event_id={existing_human_input_event_id!r} does not resolve to a "
                    f"genuine human_waking_input event matching this authority/prompt -- refusing to silently "
                    f"substitute or fabricate H."
                )
            human_input_event_id = existing_human_input_event_id
        elif _authority_valid:
            # No try/except here, deliberately: a failure committing H
            # for a POSITIVELY-VALIDATED authority must abort this
            # entire call before any model inference -- never caught,
            # never downgraded to unbound, never allowed to reach the
            # model call below. See human_session_binding.
            # record_human_waking_input()'s own docstring.
            human_input_event_id = human_session_binding.record_human_waking_input(
                PROVENANCE_DB_DIR, pipeline_key=PIPELINE_KEY, authority=human_input_authority,
                message=human_message_text, occurred_at=int(datetime.now(timezone.utc).timestamp()),
                source_inbound_event_id=caret_occasion["event_id"] if caret_occasion is not None else None,
            )
        # else: no valid authority -- proceed exactly as an ordinary
        # unbound/internal call, no human attribution, no error.

    if existing_human_input_event_id is not None and human_input_event_id is None:
        raise ValueError("existing human input continuation requires valid current human authority")

    # FS1: resolve the family-scope view for THIS call, derived ONLY
    # from the freshly-re-validated authority (human_input_event_id is
    # non-None exactly when validation succeeded -- see above). None for
    # every unbound session, in which case the entire delivery path
    # below stays byte-identical to today's unfiltered behavior. The
    # snapshot is read from a short-lived read-only connection and is
    # used ONLY to seed per-event delivery predicates; it never widens
    # anything by itself.
    _family_view = None
    _family_feature_active = False
    _family_state_path = f"{PROVENANCE_DB_DIR}/anaxi_provenance.db"
    if os.path.exists(_family_state_path):
        _family_state_conn = sqlite3.connect(
            f"file:{_family_state_path}?mode=ro", uri=True,
        )
        try:
            _family_feature_active = family_membership.family_feature_used(_family_state_conn)
        finally:
            _family_state_conn.close()
    if (
        human_input_event_id is not None
        and human_input_authority is not None
        and human_input_authority.visibility_scope is not None
    ):
        _fv_conn = sqlite3.connect(f"file:{PROVENANCE_DB_DIR}/anaxi_provenance.db?mode=ro", uri=True)
        try:
            _family_view = family_membership.family_session_view(
                _fv_conn,
                principal_actor_id=human_input_authority.authenticated_actor_id,
                visibility_scope=human_input_authority.visibility_scope,
            )
        finally:
            _fv_conn.close()

    ui_turn_diagnostics.record_human_input_event_id(human_input_event_id)
    prepared = orch.prepare_context(USER_ID, prompt)
    resolved_interaction_mode = _resolve_interaction_mode(interaction_mode)
    temporal_text = ''
    if resolved_interaction_mode == CONVERSATION_MODE:
        temporal_text = temporal_grounding.snapshot(
            os.path.join(PROVENANCE_DB_DIR, 'anaxi_provenance.db'), human_input_event_id,
            process_started=_temporal_process_started,
            lifecycle_notice=session_id not in _temporal_lifecycle_sessions,
            diagnostics_path=os.path.join(PROVENANCE_DB_DIR, ui_turn_diagnostics.DEFAULT_LOG_PATH),
        )

    if caret_occasion is not None:
        signal_category = "NO_SIGNAL"
    else:
        try:
            signal_category = classify_signal(prompt)
        except Exception:
            signal_category = "NO_SIGNAL"

        try:
            record_observation(prompt)
        except Exception:
            pass

    if signal_category == "POSITIVE":
        judgment_messages = build_artifact_construction_messages(prompt)
        # OWC9-P4 section 1: aggregate preflight before this model call
        # -- a hard-only overflow (an oversized human prompt, the one
        # thing this single-shot prompt has no soft material to trim)
        # never reaches ask_llama_for_json() at all. Degrades into the
        # SAME already-existing "no artifact judgment this turn" path
        # the except-Exception below already provides for any other
        # judgment failure -- zero fabricated action, zero model call,
        # no new failure surface introduced.
        judgment_budget = _compose_artifact_judgment_budget(judgment_messages)
        ui_turn_diagnostics.record_context_budget_result("artifact_judgment", judgment_budget)
        if not judgment_budget.fits:
            raw_judgment = ""
        else:
            try:
                raw_judgment = ui_turn_diagnostics.timed_model_call("artifact_judgment", ask_llama_for_json, judgment_messages)
            except Exception:
                raw_judgment = ""
        print(f"[raw construction] {raw_judgment}")
        try:
            # Q-05 (canonical-before-artifact ordering): validates and
            # decides only -- does NOT write the durable artifact file
            # yet. The bounded clause below needs to know the outcome
            # now, but the durable write itself must wait until AFTER
            # canonical provenance commits (see finalize_artifact_write()
            # call near the end of this function, right after
            # stage_and_record_native_waking_turn()).
            artifact_result = prepare_artifact_decision(
                raw_judgment, OBSIDIAN_WORKSPACE_ROOT, source_prompt=prompt, waking_model_tag=MODEL,
            )
        except StaleSchemaError as e:
            print(f"[STALE SCHEMA] {e}", file=sys.stderr)
            artifact_result = {
                "artifact_created": False,
                "pass2_context": "Artifact action:\nNone.",
                "detail": {"status": "stale_schema", "reason": f"stale schema: {e}"},
            }
    else:
        artifact_result = {
            "artifact_created": False,
            "pass2_context": "Artifact action:\nNone.",
            "detail": {"status": "no_artifact_requested", "reason": f"signal gate: {signal_category}"},
        }

    operation_status = map_to_operation_status(signal_category, artifact_result)
    artifact_title = artifact_result.get("artifact_title")  # None on any non-success path
    rendered_bounded_clause = render_bounded_clause(operation_status, artifact_title=artifact_title)

    # not_authorized is routine, internal control-plane state -- an ordinary
    # turn simply didn't qualify for a governed memory artifact, which is
    # the common case, not an exceptional one. The person is not told this
    # merely because it happened; bounded_clause is left empty so nothing
    # is prepended to the reply, injected into Gemma's context as
    # already-spoken, or persisted as a canonical bounded_clause component
    # (see native_provenance_writer.py, made conditional on this being
    # non-empty). The mechanical decision itself is NOT weakened by this:
    # operation_status here still equals "not_authorized" exactly as
    # before, and it stays durably, independently recoverable two other
    # ways regardless of this suppression -- record_observation() above
    # already, unconditionally logged signal_category (not_authorized is a
    # deterministic function of signal_category != "POSITIVE" alone, per
    # map_to_operation_status()), and artifact_pass_ran (set from that same
    # condition, below) is staged and canonically recorded via
    # event_model_participation's row count either way. Every other
    # operation_status is untouched -- this is the one silent exception.
    bounded_clause = "" if operation_status == "not_authorized" else rendered_bounded_clause

    pass2_messages = [dict(m) for m in prepared["messages"]]
    # OWC9-P2: replace the welded identity+memory system message with
    # the TRUE HARD core only (prepared["core_system_text"] ==
    # controls["identity_preamble"], no legacy/hippocampal memory
    # folded in -- see orchestration.prepare_context()'s own docstring).
    # apply_conversation_aesthetic()/apply_interaction_mode() below
    # still operate correctly on this leaner text: the aesthetic-
    # directive substring they look for/append to lives entirely inside
    # identity_preamble, never inside the memory block that used to
    # follow it. Retrieved legacy/hippocampal memory now reaches the
    # compositor as its own SOFT contribution(s) below, and (for
    # whatever survives composition) is appended back onto the system
    # message at final assembly time only -- never here, never
    # unconditionally.
    #
    # .get() with a fallback (never direct ["core_system_text"]
    # indexing): several existing test suites construct their own
    # fake/stub orchestrator whose prepare_context() only returns the
    # four original keys (memory_context/kardia/controls/messages).
    # Falling back to the stub's own already-welded messages[0]
    # content preserves those tests' existing behavior exactly --
    # only the REAL AnaxiOrchestrator (which now always returns
    # core_system_text) gets the separated, leaner core text.
    if pass2_messages and pass2_messages[0].get("role") == "system":
        core_system_text = prepared.get("core_system_text")
        if core_system_text is None:
            core_system_text = pass2_messages[0]["content"]
        pass2_messages[0] = {"role": "system", "content": core_system_text}

    # OWC4-S1: mode-specific aesthetic override -- pure text
    # replacement on the already-rendered system message, using the
    # REAL style_instruction prepare_context() just returned (never a
    # second Kardia/aesthetic lookup, never a DB write). Applied before
    # the OWC3-S1 clause so both operate on the system message while it
    # is still the sole/leading message.
    pass2_messages = apply_conversation_aesthetic(
        pass2_messages, resolved_interaction_mode, prepared["controls"].get("style_instruction")
    )

    # OWC3-S1: explicit interaction-mode contract. TASK mode (the
    # default) leaves pass2_messages completely unchanged -- see
    # interaction_mode.py's own docstring. Applied before the dialogue
    # window is inserted so the clause lands in the system message
    # while it is still the sole/leading message (index 0).
    pass2_messages = apply_interaction_mode(pass2_messages, resolved_interaction_mode)

    # OWC2-S1: exact, bounded current-session dialogue continuity --
    # deterministic session history, never semantic retrieval. See
    # session_dialogue_window.py's module docstring. A read-only
    # connection, opened and closed within this one call, never
    # overlapping the later canonical write connection
    # stage_and_record_native_waking_turn() opens further down.
    # `mode=ro` refuses to create a missing file (unlike a plain
    # connect()) -- correct here, since a provenance DB that doesn't
    # exist yet trivially has no prior session turns to retrieve either.
    provenance_db_path = f"{PROVENANCE_DB_DIR}/anaxi_provenance.db"
    if os.path.exists(provenance_db_path):
        dialogue_conn = sqlite3.connect(readonly_db.readonly_sqlite_uri(provenance_db_path), uri=True)
        try:
            if caret_correspondent_view is not None and resolved_interaction_mode == CONVERSATION_MODE:
                # A non-principal correspondent Clark has standing with: his continuity with exactly
                # that source (from the establishing exchange on), never anyone else's.
                dialogue_window = build_correspondent_dialogue_window(dialogue_conn, caret_correspondent_view["key"])
            elif (
                _family_feature_active and _family_view is None
                and resolved_interaction_mode == CONVERSATION_MODE
            ):
                # Once family mode exists, an identity-less waking call
                # gets no dialogue continuity at all.
                dialogue_window = []
            elif human_input_event_id is not None and resolved_interaction_mode == CONVERSATION_MODE:
                if _family_view is not None:
                    dialogue_window = build_authenticated_dialogue_window(
                        dialogue_conn, human_input_event_id,
                        viewer_visibility_scope=_family_view["visibility_scope"],
                        viewer_principal_actor_id=_family_view["principal_actor_id"],
                        active_family_principal_ids=_family_view["active_family_principal_ids"],
                    )
                else:
                    dialogue_window = build_authenticated_dialogue_window(dialogue_conn, human_input_event_id)
            else:
                dialogue_window = build_session_dialogue_window(dialogue_conn, session_id, STAGING_PATH)
        finally:
            dialogue_conn.close()
    else:
        dialogue_window = []
    pass2_messages = insert_dialogue_window(pass2_messages, dialogue_window)

    if bounded_clause and pass2_messages and pass2_messages[0]["role"] == "system":
        pass2_messages[0] = dict(pass2_messages[0])
        pass2_messages[0]["content"] = pass2_messages[0]["content"] + (
            f"\n\nThe following has already been told to the person you're talking "
            f"with, verbatim, before your own reply is added: \"{bounded_clause}\"\n"
            f"Do not repeat, rephrase, or contradict this statement -- it has already "
            f"been said. Your own reply will be added directly after it. Respond "
            f"naturally, as yourself, to the rest of the conversation."
        )

    # OWC6-O1: set only inside the CONVERSATION_MODE branch below, on a
    # successful (Pass-1 ok, Pass-2 ok) typed turn -- holds the fields
    # needed for the durable operational trace, written further below
    # only once persistence has actually succeeded and a real
    # event_id/staging_id exist to bind it to. Stays None for task
    # mode and for any conversation-mode turn that failed closed
    # (those write their own trace record immediately, with no
    # event/staging binding, at the point of failure).
    conv_trace_pending = None
    # WSP2-P3 Bridge C (delivery semantics, WSP2-P3-I section 7): stays
    # None unless a real episode context was actually appended to this
    # exact turn's system message below -- only then is it eligible to
    # be passed to stage_and_record_native_waking_turn() near the end
    # of this function, and even then ONLY if that call is actually
    # reached (i.e. this turn's Pass 1 and Pass 2 both succeeded and
    # persistence itself succeeds) -- a failed-closed turn anywhere
    # before that point never touches this variable again, so the
    # episode remains correctly pending for a later attempt.
    delivered_episode_run_id = None
    # WSP2-P4/WSP2-P4-P1 (spec section 5/8/10): stays None unless
    # ACTIVE_WORKSPACE_CONTINUITY_V1 actually rendered something for
    # this exact turn -- then a list of the source event_ids rendered,
    # passed to stage_and_record_native_waking_turn()'s own
    # delivered_active_workspace_event_ids parameter so durable
    # delivery evidence is written atomically WITH this turn's own
    # persistence (a failed/never-reached persistence call means the
    # transaction never commits, so it can never falsely acknowledge
    # delivery -- no separate post-persistence step required).
    delivered_active_workspace_marker = None
    # CAP2-F: stays None unless a genuinely-delivered resource-encounter
    # fact (from a prior WSP1 supervised photograph/library/journal
    # action) actually survives THIS turn's own Pass-2 composition
    # below -- mirrors delivered_episode_run_id's own discipline exactly
    # (spec section 18: composed-and-survived, not merely eligible).
    pending_resource_encounter = None
    delivered_resource_encounter_event_id = None
    # WSP2-P5-P1 (spec section 6/11): the validated background_
    # activity_request value from this turn's Pass-1, if any -- stays
    # None for task mode and for any conversation-mode turn that failed
    # closed before Pass-2 succeeded. Set alongside conv_trace_pending
    # below, once BOTH passes have succeeded; the corresponding durable
    # canonical write only happens strictly AFTER persistence itself
    # succeeds (native_result exists), further below -- never before
    # (spec section 11/12: a contained/failed turn must never
    # accidentally resume background activity).
    pending_background_activity_request = None
    # SLP2: Clark's own typed Sleep-timing choice for this turn, if
    # any -- same None-until-both-passes-succeed discipline as
    # pending_background_activity_request above, and applied for the
    # exact same reason: a contained/failed turn must never originate,
    # knock on, or withdraw a Sleep request that Clark never actually
    # reached in a successfully released turn.
    pending_sleep_timing_request = SLEEP_TIMING_REQUEST_NONE
    # OD1: Clark's own typed operative-directive choice for this turn,
    # if any -- same None-until-both-passes-succeed discipline as its
    # pending sibling fields above, for the same reason: a
    # contained/failed turn must never activate, replace, or withdraw a
    # standing directive Clark never actually reached in a successfully
    # released turn.
    pending_operative_directive_request = OPERATIVE_DIRECTIVE_REQUEST_NONE
    pending_operative_directive_text = ""
    # BOUNDARY INSPECTOR v1: Clark's own typed boundary_inquiry_request
    # for this turn, if any -- same None-until-both-passes-succeed
    # discipline as its sibling pending fields above: a contained/failed
    # turn must never dispatch a host boundary query Clark never reached
    # in a fully released turn.
    pending_boundary_inquiry_request = None
    # BOUNDARY INSPECTOR v1: the FIFO next undelivered boundary result in
    # THIS session, selected read-only at Pass-2 assembly below (never
    # for task mode). Stays None for task mode and for any conversation-
    # mode turn with nothing pending.
    pending_boundary_result = None
    # BOUNDARY INSPECTOR v1: set ONLY from what the Pass-2 composition
    # ACTUALLY included after trimming (never pre-trim eligibility) --
    # mirrors the delivered_episode_run_id/other delivered_* discipline
    # exactly. Keeps its None until then so a failed/task-mode turn can
    # never acknowledge delivery.
    delivered_boundary_query_event_id = None
    # BOUNDARY INSPECTOR v1: both truthful mechanical outcome fields for
    # the returned summary below -- None unless something actually
    # happened for this exact turn: a query dispatched only with a real
    # canonical persistence commit behind it, delivery acknowledged only
    # for a result that survived THIS turn's final Pass-2 composition.
    boundary_inspection_report_query = None
    boundary_inspection_report_delivered = None
    # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: Clark's own typed
    # external_info_request/target for this turn, if any -- same None-
    # until-both-passes-succeed discipline as every sibling pending
    # field above, for the same reason: a contained/failed turn must
    # never originate a search or page fetch Clark never actually
    # reached in a successfully released turn.
    pending_external_info_request = EXTERNAL_INFO_REQUEST_NONE
    pending_external_info_target = ""
    # The FIFO next undelivered external-info result in THIS session,
    # selected read-only at Pass-2 assembly below (never for task
    # mode). Stays None for task mode and for any conversation-mode
    # turn with nothing pending.
    pending_external_info_result = None
    live_external_request = None
    external_info_pre_reply_state = "not_performed"
    # Set ONLY from what the Pass-2 composition ACTUALLY included after
    # trimming (never pre-trim eligibility) -- mirrors delivered_
    # boundary_query_event_id's own discipline exactly.
    delivered_external_info_query_event_id = None
    delivered_external_info_rendered_text = None
    # Truthful mechanical outcome fields for the returned summary below
    # -- None unless something actually happened for this exact turn.
    external_info_report_query = None
    external_info_report_delivered = None
    # PRIVATE DISCORD CORRESPONDENCE V0: Clark's own typed send request
    # for this turn, if any -- same None-until-both-passes-succeed
    # discipline as every sibling pending field above, for the same
    # reason: a contained/failed turn must never originate a real send
    # Clark never actually reached in a successfully released turn.
    pending_discord_correspondence_request = DISCORD_CORRESPONDENCE_REQUEST_NONE
    pending_discord_destination_id = ""
    pending_discord_message_text = ""
    # The FIFO next undelivered received messages in this process (never
    # for task mode), selected read-only at Pass-2 assembly below. Each
    # is host-framed as untrusted external data.
    pending_discord_inbound = None
    # Set ONLY from what the Pass-2 composition ACTUALLY included after
    # trimming (never pre-trim eligibility) -- mirrors delivered_
    # external_info_query_event_id's own discipline exactly.
    delivered_discord_inbound_event_ids = None
    # Truthful mechanical outcome fields for the returned summary below
    # -- None unless something actually happened for this exact turn.
    discord_correspondence_report = None
    discord_inbound_report_delivered = None
    caret_reply_report = None
    caret_reply_route_intent = False
    subject_reply_choice = None   # LAWFUL NULL: set only by Clark's typed no_reply choice

    if resolved_interaction_mode == CONVERSATION_MODE:
        # WSP2-P3-I section 5's load-bearing correction: delivered to
        # CONVERSATION_MODE waking ONLY -- an unrelated TASK-mode
        # invocation must never consume or acknowledge a pending
        # episode.
        #
        # OWC9: episode_context_text/active_events are computed here,
        # ONCE (never independently re-queried per pass -- spec section
        # 12's "no independent re-query races creating different
        # worlds"), but are NO LONGER unconditionally appended to
        # pass2_messages[0]. They are held as raw materials and offered
        # to context_budget.compose_within_budget() as SOFT, Pass-2-only
        # contributions below (spec section 10: Pass-1's own typed-act
        # legality does not depend on either -- see that section's own
        # "episode context may be omitted unless current control
        # semantics require it" / "active workspace continuity may
        # contribute bounded mechanical facts rather than rich resource
        # content" -- Pass-1 needs neither at all here, since none of
        # its five fields' legality is governed by roaming continuity).
        # delivered_episode_run_id/delivered_active_workspace_marker are
        # set LATER, from what the Pass-2 composition ACTUALLY included
        # after trimming -- never from this raw pre-trim material (spec
        # section 18: "NOT SELECTED THIS TURN DUE TO BUDGET != DURABLY
        # DELIVERED").
        pending_episode_run_id = workspace_episode_context.most_recent_pending_episode_run_id(PROVENANCE_DB_DIR)
        # CAP2-F: the single most recent genuinely-delivered resource
        # encounter not yet carried into any waking turn's own
        # continuity -- bounded to exactly one, per spec ("immediate",
        # not a backlog). Never raises; None if nothing is pending.
        pending_resource_encounter = workspace_episode_provenance.find_pending_resource_encounter_for_continuity(PROVENANCE_DB_DIR)
        # WSP2-P4-P1 (spec section 6/7): the SAME durable delivery
        # ledger ACTIVE_WORKSPACE_CONTINUITY_V1 itself consults below --
        # PUBLIC material already delivered live is omitted from Bridge
        # C's own "new material" listing, never presented twice as
        # though newly encountered at closure.
        try:
            already_delivered_event_ids = workspace_public_continuity.find_delivered_public_event_ids(PROVENANCE_DB_DIR)
        except Exception:
            already_delivered_event_ids = frozenset()
        episode_context_text = workspace_episode_context.build_workspace_episode_context(
            PROVENANCE_DB_DIR, pending_episode_run_id, already_delivered_event_ids=already_delivered_event_ids,
        )

        # WSP2-P4/WSP2-P4-P1 ACTIVE_WORKSPACE_CONTINUITY_V1 (spec
        # section 5/8/10): unlike Bridge C above, this does NOT require
        # episode termination -- it fires whenever a roaming episode is
        # currently active (started, not yet ended) and has PUBLIC
        # activity/capability-state facts this waking pathway has not
        # already been DURABLY delivered (workspace_public_continuity.
        # collect_active_workspace_events() itself excludes anything
        # already recorded in the durable, restart-proof delivery
        # ledger -- no process-local cursor is consulted here at all).
        # One explicit high-water mark is read once and reused for this
        # one collection call (spec section 4/9). Best-effort: any
        # lookup failure here (e.g. no provenance DB yet) simply
        # delivers nothing this turn -- it must never break an
        # otherwise-ordinary waking turn.
        active_events = []
        try:
            active_run_id = workspace_public_continuity.find_active_roaming_run_id(PROVENANCE_DB_DIR)
        except Exception:
            active_run_id = None
        if active_run_id:
            try:
                hwm = workspace_public_continuity.compute_high_water_mark(PROVENANCE_DB_DIR)
                active_events = workspace_public_continuity.collect_active_workspace_events(
                    PROVENANCE_DB_DIR, active_run_id, None, hwm,
                )
            except Exception:
                active_events = []

        # FS1: family-scope workspace continuity. Only the designated
        # owner's own principal_private session receives it, and only
        # events the canonical delivery law lets that viewer receive: native
        # principal_private workspace facts written after Family activation,
        # and pre-Family NULL-scope facts proven by the legacy owner-private
        # seam. Everyone else (shared, other principal, identity-less) gets
        # nothing. Unbound/pre-FS1 sessions keep today's behavior unchanged.
        if _family_feature_active:
            (pending_episode_run_id, pending_resource_encounter,
             episode_context_text, active_events) = _family_workspace_continuity(
                _family_view, already_delivered_event_ids, active_run_id, active_events,
            )

        # OWC5-S2: two-stage typed conversational-direction path.
        # pass2_messages at this point is: system (identity/Kardia,
        # memory already routed out separately by the OWC9-P2 core_
        # system_text override above -- OWC9: episode/active-workspace
        # continuity no longer baked in here), then the OWC2 dialogue
        # window, then the current prompt exactly once, last.
        base_system_content = pass2_messages[0]["content"] if pass2_messages and pass2_messages[0]["role"] == "system" else ""
        base_system_content = workspace_public_continuity.SPACE_CAPABILITY_GROUNDING + "\n\n" + base_system_content
        base_dialogue_window = pass2_messages[1:-1] if len(pass2_messages) >= 2 else []
        # The current human occurrence is the exact function argument/H
        # component, never inferred from a prepared-message position.  This
        # remains correct when a recovery fixture or future context builder
        # supplies only a system message, and prevents system control text
        # from being misclassified as CURRENT_HUMAN_MESSAGE.
        base_current_user_message = prompt
        # HOST-FRAMING LEAK repair (production incident, 2026-09-22): Pass 1 still needs the
        # full render_inbound_delivery() rendering above (sender identity, channel,
        # authorization) inline with the words to decide caret_reply_request -- it has no other
        # model-visible source for those facts, and its own output is a schema-constrained act
        # with no free-form field for them to leak through. Pass 2 is different: its "expression"
        # field IS free-form outward speech, so it must never see that same combined object as
        # its current human message, or the model can copy the whole host-framed object wholesale
        # as Clark's own reply (exactly what was delivered live). Pass 2 gets the exact words
        # alone here; the mechanical facts are supplied separately, in host framing, further below.
        pass2_current_user_message = human_message_text if caret_occasion is not None else base_current_user_message

        working_set_before = get_working_set()

        # OWC9: Pass-1's LEAN contributor set -- CORE_SYSTEM_CONTROL and
        # CURRENT_HUMAN_MESSAGE are HARD (spec section 6/13: must not
        # silently disappear); RECENT_DIALOGUE is SOFT/droppable, oldest
        # pair first, on its OWN independent copy of the shared,
        # once-fetched dialogue_window units (spec section 12: a
        # snapshot-identity-preserving copy, not a re-query -- Pass-2
        # gets its own separate copy of the SAME underlying list below).
        # OWC9-P3B: the immutable Pass-1 scaffold (identity + CONVERSATION_
        # MODE_CLAUSE/aesthetic framing + PASS1_TASK_INSTRUCTION + a fixed
        # closing cue) now carries its own empirically CALIBRATED cost
        # (see the module-level calibration record near MODEL above) --
        # a real, measured ~4.75x tighter bound than the byte estimator
        # for this exact text, fingerprint-guarded so ANY mismatch (a
        # different Kardia/identity_preamble, a different model, a
        # different schema) falls back to the ordinary, always-safe
        # byte estimator automatically. Dynamic mechanical state (the
        # "Current working state" facts -- genuinely different every
        # turn) stays its OWN separate contribution under the byte
        # estimator, never folded into the calibrated scaffold (spec
        # section 3's own explicit prohibition).
        # Only a real Caret occasion is offered the typed reply route; every other turn keeps PASS1_SCHEMA.
        pass1_schema = PASS1_SCHEMA_CARET_OCCASION if caret_occasion is not None else PASS1_SCHEMA
        pass1_mechanical_state_text = (
            f"Current working state: active_thread={working_set_before.get('active_thread')!r}, "
            f"active_thread_origin={working_set_before.get('active_thread_origin')!r}, "
            f"open_threads={working_set_before.get('open_threads')!r}, "
            f"direction_owner={working_set_before.get('direction_owner')!r}"
        )
        pass1_mechanical_state_text += '\n\n' + temporal_text
        if caret_occasion is not None:
            pass1_mechanical_state_text += '\n\n' + discord_correspondence.render_occasion_transport_fact()
        # PASS1-BUDGET-2: a real fully-loaded turn (every capability
        # actually available, one authorized Discord destination, a
        # genuinely long first-wake human message) still overran
        # PASS1_MAX_PROMPT_BUDGET after PASS1-BUDGET-1 compacted each
        # sibling affordance to its own single-line summary -- six
        # single lines each still repeating "optional"/"defaults to
        # none"/"never inferred from prose" cost more, together, than
        # the ceiling allows once a real human message is added. See
        # conversation_direction.render_pass1_action_menu()'s own
        # comment: it asserts the SAME field names/enum values/
        # preconditions as conversation_direction.py's own six
        # *_AFFORDANCE_TEXT constants, just states each shared fact
        # once instead of six times. This
        # is always byte-estimated (never the calibrated fast path) and
        # entirely EXCLUDED from pass1_scaffold_hash_source below, so
        # appending it touches nothing calibration-sensitive.
        discord_action_available = _discord_correspondence_available(
            _family_feature_active, _family_view,
        )
        authorized_discord_destinations = (
            discord_correspondence.list_authorized_destinations(PROVENANCE_DB_DIR)
            if discord_action_available else []
        )
        workspace_action_available = _workspace_action_available(
            _family_feature_active, _family_view,
        ) and caret_occasion is None   # untrusted correspondence never drives a workspace action
        # The menu's turn-invariant block is costed on its own, fingerprint-
        # calibrated path (see _PASS1_MENU_CALIBRATION); the turn-dependent head
        # and tail stay byte-costed with the rest of the mechanical state. The
        # SENT text is the exact concatenation head, fixed, tail.
        # Result selection needs the delivered search results in PASS 1: the
        # fetch_url choice is made there, and Pass 2 (where a result is first
        # delivered) comes after it. Same bounded untrusted tool-role JSON
        # envelope as Pass 2; SOFT, never durably acknowledged, and read-only.
        # Selection by "result:N" binds host-side to the same delivered set.
        try:
            pass1_search_choices = external_information.latest_selectable_search_result(
                PROVENANCE_DB_DIR, session_id
            )
        except Exception:
            pass1_search_choices = None
        pass1_open_page = _open_page_fact(session_id)
        journal_threads = None
        if workspace_action_available:
            try:
                import workspace_capability as _wc_index
                journal_threads = _wc_index.journal_thread_index(_wc_index.WorkspacePaths.production_defaults())
            except Exception:
                journal_threads = None
        # GENERAL CORRESPONDENCE: on an occasion from a non-principal source, Clark is offered the
        # standing choice for that source; on an owner turn with the general send authority, the
        # correspondents he has encountered (stable ids only) and his own undelivered sends.
        known_correspondents = []
        pending_sends = []
        if discord_action_available and caret_occasion is None:
            try:
                known_correspondents = discord_correspondence.known_correspondents(PROVENANCE_DB_DIR)
                pending_sends = discord_correspondence.pending_authored_sends(PROVENANCE_DB_DIR)
            except Exception:
                known_correspondents, pending_sends = [], []
            # Plain posts go only to surfaces NOT open to non-principal sources (there a message must be
            # addressed to a correspondent Clark has standing with).
            _open_ids = {d for c in known_correspondents for d in c.get("initiation_destinations", [])}
            authorized_discord_destinations = [
                d for d in authorized_discord_destinations
                if not _surface_open_to_sources(d["destination_id"]) or d["destination_id"] in _open_ids
            ]
        if caret_source_ref is not None:
            pass1_schema = PASS1_SCHEMA_SOURCE_OCCASION
        elif caret_occasion is None:
            pass1_schema = pass1_schema_with_correspondence(
                correspondents_available=bool(known_correspondents), pending_sends_available=bool(pending_sends))
        menu_head, menu_fixed, menu_tail = render_pass1_action_menu_parts(
            discord_destinations=authorized_discord_destinations,
            workspace_available=workspace_action_available,
            notes_available=workspace_action_available and _shared_vault_available(),
            journal_threads=journal_threads,
            open_reading=(_open_reading_line(session_id) if workspace_action_available else None),
            open_page=pass1_open_page,
            search_choice_count=len((pass1_search_choices or {}).get("results") or []),
            caret_occasion=caret_occasion is not None,
            source_occasion=({"ref": caret_source_ref, "standing": (
                correspondence_surfaces.STANDING_ESTABLISHED if caret_correspondent_view is not None
                else (caret_occasion.get("correspondent") or {}).get("standing") or correspondence_surfaces.STANDING_NONE)}
                if caret_source_ref is not None else None),
            correspondents=known_correspondents,
            pending_sends=pending_sends,
        )
        pass1_costed_mechanical_text = (
            pass1_mechanical_state_text + '\n\n' + "\n".join(part for part in (menu_head, menu_tail) if part)
        )
        pass1_mechanical_state_text += '\n\n' + "\n".join(part for part in (menu_head, menu_fixed, menu_tail) if part)
        pass1_dialogue_units = list(base_dialogue_window)
        # Waking-pass1-budget repair: PASS1_TASK_INSTRUCTION's own text
        # (read in full) never references moral/volitional/affective
        # Kardia stance or the "you have persistent memory" disclaimer
        # anywhere -- the act/direction_request/relinquish_direction/
        # background_activity_request decision is purely structural (it
        # reads direction_owner, the dialogue window, and the current
        # human message; nothing else). Those verbatim, unboundedly-
        # long, Kardia-evolved fields are therefore dropped from Pass-1's
        # own scaffold entirely -- base_system_content (the welded
        # identity+aesthetic system text) remains fully intact and
        # unchanged for Pass-2's own composition below, which genuinely
        # needs it to speak as Clark. This is not a summarization or
        # removal of Kardia -- Kardia is untouched; Pass-1 simply never
        # needed to see the moral/volitional/affective bullets or the
        # memory disclaimer.
        #
        # The interaction-mode clause and the conversational aesthetic
        # DIRECTIVE are different: both are fixed, bounded, non-Kardia-
        # verbatim framing text (CONVERSATION_MODE_CLAUSE and
        # CONVERSATION_AESTHETIC_DIRECTIVE, interaction_mode.py) that an
        # existing, deliberate test (OWC3/OWC4 regression coverage)
        # already establishes Pass-1 is meant to see once, unconditionally
        # in CONVERSATION mode (which this whole branch is already
        # scoped to -- TASK mode never reaches Pass-1 at all) -- kept
        # here exactly as before, just no longer riding on top of the
        # large Kardia-derived preamble that used to carry them.
        #
        # The fingerprint-guarded fast path above was RE-CALIBRATED
        # against this exact new (identity-free) scaffold -- see
        # _PASS1_SCAFFOLD_CALIBRATION's own comment -- so it remains
        # live and reachable, just measuring a smaller, Kardia-
        # independent text now. It still falls back to the always-safe
        # byte estimator unconditionally the moment any calibrated
        # field (model/server/template/schema/this exact scaffold text)
        # next changes.
        pass1_system_content = (
            CONVERSATION_MODE_CLAUSE + "\n\n" + f"Aesthetic directive: {CONVERSATION_AESTHETIC_DIRECTIVE}"
        )
        # Hash-source text for the fingerprint/fallback -- the EXACT
        # NUL-joined construction the calibration assay itself hashed
        # (never concatenated this way into the actual sent messages,
        # which stay two separate chat messages -- see pass1_messages
        # below, matching the exact message-boundary shape measured).
        pass1_scaffold_hash_source = (
            pass1_system_content + "\x00" + PASS1_TASK_INSTRUCTION + "\n\n" + PASS1_IMMUTABLE_TASK_MESSAGE_CLOSING_CUE
        )
        pass1_hard_core = context_budget.Contribution(
            context_budget.CORE_SYSTEM_CONTROL, pass1_scaffold_hash_source, hard=True,
        )
        # CONTROL-SCAFFOLD ATTRIBUTION repair (production incident,
        # 2026-09-05): PASS1_TASK_INSTRUCTION+closing-cue now ships
        # inside the SYSTEM message (see pass1_messages below), not as
        # its own trailing "user"-role message -- a real conversation
        # host/control text sharing a role with Alex's own speech is
        # exactly the shape that let Clark misattribute host scaffolding
        # to the person. message_structure_signature="system_merged"
        # matches _PASS1_SCAFFOLD_CALIBRATION's own updated signature --
        # re-measured directly against this exact new merged shape (see
        # that calibration's own comment) rather than left to fall back
        # to the conservative byte estimator, which would have quietly
        # reintroduced the earlier BUDGET_EXCEEDED-under-ordinary-turns
        # failure this same repair must not bring back.
        pass1_live_fingerprint = _pass1_scaffold_live_fingerprint(
            pass1_scaffold_hash_source, message_structure_signature=("system_merged",),
        )
        pass1_hard_core.cost = context_budget.calibrated_or_fallback_cost(
            pass1_hard_core.rendered_text, pass1_live_fingerprint,
            _PASS1_SCAFFOLD_CALIBRATION, _PASS1_SCAFFOLD_CALIBRATED_COST,
        )
        pass1_hard_human = context_budget.Contribution(
            context_budget.CURRENT_HUMAN_MESSAGE, base_current_user_message, hard=True,
        )
        current_human_estimated_cost = pass1_hard_human.cost
        pass1_hard_mechanical = context_budget.Contribution(
            context_budget.MECHANICAL_STATE, pass1_costed_mechanical_text, hard=True,
        )
        pass1_hard_menu = context_budget.Contribution(
            context_budget.ACTION_MENU_FIXED, menu_fixed, hard=True,
        )
        pass1_hard_menu.cost = context_budget.calibrated_or_fallback_cost(
            menu_fixed, _pass1_scaffold_live_fingerprint(menu_fixed, message_structure_signature=("system_merged",)),
            _PASS1_MENU_CALIBRATION, _PASS1_MENU_CALIBRATED_COST,
        )
        pass1_soft_dialogue = dialogue_contribution(pass1_dialogue_units)
        pass1_contributions = [
            pass1_hard_core, pass1_hard_human, pass1_hard_mechanical, pass1_hard_menu, pass1_soft_dialogue,
        ]
        if pass1_search_choices is not None:
            pass1_contributions.append(context_budget.Contribution(
                context_budget.PASS1_SEARCH_CHOICES,
                external_information.render_search_choices(pass1_search_choices),
                hard=False,
            ))
        if pass1_open_page is not None:
            pass1_contributions.append(context_budget.Contribution(
                context_budget.PASS1_OPEN_PAGE, external_information.render_open_page(pass1_open_page), hard=False,
            ))
        recost_when_shedding(
            pass1_contributions, context_budget.PASS1_MAX_PROMPT_BUDGET, structured_schema=pass1_schema,
        )
        pass1_result = context_budget.compose_within_budget(pass1_contributions, context_budget.PASS1_MAX_PROMPT_BUDGET)
        if not pass1_result.fits:
            # Multi-turn waking reliability repair (production incident,
            # 2026-09-05): the current human message can never be
            # pre-calibrated, so it rides on the conservative byte
            # estimator alone -- for an ordinary, genuinely reasonable
            # long message, that conservative accounting alone can
            # already exceed budget even though the model's own real
            # tokenization would not. One bounded real, live measurement
            # (never an estimate) of this exact message's real cost gives
            # every safely measurable hard overflow a fair chance before
            # failing closed -- see _recover_hard_overflow_via_real_
            # measurement()'s own docstring.
            pass1_result = _recover_hard_overflow_via_real_measurement(
                pass1_result, pass1_contributions, context_budget.PASS1_MAX_PROMPT_BUDGET,
                pass1_hard_human, base_current_user_message, structured_schema=pass1_schema,
            )
        # The current H is byte-identical in Pass 1 and Pass 2 and both calls
        # use the same model/chat template. Ollama's `format` constrains only
        # generation, not prompt tokenization. Preserve a successful exact
        # turn-local measurement instead of discarding it and asking Pass 2
        # to rediscover the same fact (or, before this repair, rejecting it
        # via an unrelated ratio heuristic). Nothing is cached across turns.
        # HOST-FRAMING LEAK repair: NOT byte-identical for a Caret occasion any more --
        # pass2_current_user_message is the exact words alone, shorter than Pass 1's full
        # host-framed rendering measured here -- so this reuse is skipped for that case and
        # pass2_hard_human below falls back to its own conservative byte estimate.
        current_human_measured_cost = (
            pass1_hard_human.cost
            if pass1_hard_human.cost < current_human_estimated_cost and caret_occasion is None
            else None
        )
        ui_turn_diagnostics.record_context_budget_result("pass1", pass1_result)
        if not pass1_result.fits:
            # OWC9 (spec section 14): a host-side budget failure, never
            # misreported as MALFORMED_ACT -- fails closed BEFORE the
            # model is ever called, with the same containment shape an
            # ordinary Pass-1 structural failure already uses (no
            # fabricated act, no ownership change, no Pass 2).
            set_last_conversation_direction_trace({
                "act": None, "thread": None,
                "direction_request": None, "relinquish_direction": None,
                "working_set_before": working_set_before, "working_set_after": working_set_before,
                "direction_owner_before": working_set_before.get("direction_owner", DIRECTION_UNKNOWN),
                "direction_owner_after": working_set_before.get("direction_owner", DIRECTION_UNKNOWN),
                "pass1_status": context_budget.BUDGET_EXCEEDED, "pass2_status": None,
            })
            conversation_direction_trace.record_trace(
                session_id=session_id, raw_act=None, validated_act=None, thread=None,
                pass1_status=context_budget.BUDGET_EXCEEDED, working_set_before=working_set_before, working_set_after=working_set_before,
                direction_owner_before=working_set_before.get("direction_owner", DIRECTION_UNKNOWN),
                direction_owner_after=working_set_before.get("direction_owner", DIRECTION_UNKNOWN),
                direction_request=None, relinquish_direction=None,
                pass2_status=None, event_id=None, staging_id=None,
                trace_path=CONVERSATION_DIRECTION_TRACE_PATH,
            )
            raise ConversationDirectionFailure("pass1", context_budget.BUDGET_EXCEEDED)

        pass1_dialogue_final = dialogue_messages(pass1_soft_dialogue)
        pass1_search_choices_contrib = pass1_result.included_kind(context_budget.PASS1_SEARCH_CHOICES)
        pass1_open_page_contrib = pass1_result.included_kind(context_budget.PASS1_OPEN_PAGE)

        # OWC9-P3B: the immutable task message and mechanical-state text
        # are NOT recomputed here -- the exact same strings already
        # measured above (in pass1_scaffold_hash_source / pass1_
        # mechanical_state_text) are reused verbatim, guaranteeing the
        # costed text and the sent text are byte-identical (spec section
        # 11: "the exact final text/message list sent to Ollama must be
        # represented by preflight").
        #
        # CONTROL-SCAFFOLD ATTRIBUTION repair (production incident,
        # 2026-09-05): both used to ship as their own trailing "user"-
        # role messages, immediately after Alex's own current message --
        # structurally indistinguishable, to the model, from Alex having
        # said them himself. They now ship inside the single SYSTEM
        # message instead, exactly where host/control framing belongs;
        # base_current_user_message remains the ONLY "user"-role content
        # pass1 ever sees, and it is now also the LAST message, matching
        # ordinary chat-turn shape (history, then the actual new turn).
        pass1_messages = (
            [{
                "role": "system",
                "content": (
                    pass1_system_content + "\n\n"
                    + PASS1_TASK_INSTRUCTION + "\n\n" + PASS1_IMMUTABLE_TASK_MESSAGE_CLOSING_CUE + "\n\n"
                    + pass1_mechanical_state_text
                ),
            }]
            + pass1_dialogue_final
            + ([external_data_message(pass1_search_choices_contrib.rendered_text)]
               if pass1_search_choices_contrib is not None and pass1_search_choices_contrib.rendered_text else [])
            + ([external_data_message(pass1_open_page_contrib.rendered_text)]
               if pass1_open_page_contrib is not None and pass1_open_page_contrib.rendered_text else [])
            + [{"role": "user", "content": base_current_user_message}]
        )
        # OWC9-P3A section 3: PASS1_SCHEMA passed as structured_schema --
        # the SAME existing WSP2-MA2 mechanism, generation-time GBNF
        # constraint machinery, never prompt text (no prompt-cost
        # counted for it, per llama_anaxi.ask_llama_for_json()'s own
        # docstring). validate_pass1_conversation_act() below remains
        # completely unchanged and fully authoritative.
        try:
            raw1 = ui_turn_diagnostics.timed_model_call(
                "pass1", ask_llama_for_json, pass1_messages,
                structured_schema=pass1_schema,
                generation_reserve=context_budget.PASS1_GENERATION_RESERVE,
            )  # exactly one Pass-1 call; no retry
        except context_budget.IncompleteCompletionError as exc:
            raw1 = None
            raw_act_only = None
            validated_act, failure1 = None, exc.failure_code
        else:
            # OWC6-O1: raw `act` only -- extracted here so the durable
            # trace can record it regardless of which branch below is
            # taken. OWC7-S1: there is no free-form `content` field left
            # in the schema at all for this to be tempted to also extract.
            raw_act_only = conversation_direction_trace.extract_raw_act(raw1)
            validated_act, failure1 = validate_pass1_conversation_act(raw1)
            if failure1 in INCOMPLETE_OUTWARD_REQUEST_FAILURES:
                repaired_raw, dropped_requests = drop_incomplete_outward_requests(raw1, failure1)
                if repaired_raw is not None:
                    repaired_act, repaired_failure = validate_pass1_conversation_act(repaired_raw)
                    if repaired_failure is None:
                        validated_act, failure1 = dict(repaired_act, dropped_incomplete_requests=tuple(dropped_requests)), None
        if failure1 is not None:
            set_last_conversation_direction_trace({
                "act": None, "thread": None,
                "direction_request": None, "relinquish_direction": None,
                "working_set_before": working_set_before, "working_set_after": working_set_before,
                "direction_owner_before": working_set_before.get("direction_owner", DIRECTION_UNKNOWN),
                "direction_owner_after": working_set_before.get("direction_owner", DIRECTION_UNKNOWN),
                "pass1_status": failure1, "pass2_status": None,
            })
            conversation_direction_trace.record_trace(
                session_id=session_id, raw_act=raw_act_only, validated_act=None, thread=None,
                pass1_status=failure1, working_set_before=working_set_before, working_set_after=working_set_before,
                direction_owner_before=working_set_before.get("direction_owner", DIRECTION_UNKNOWN),
                direction_owner_after=working_set_before.get("direction_owner", DIRECTION_UNKNOWN),
                direction_request=None, relinquish_direction=None,
                pass2_status=None, event_id=None, staging_id=None,
                trace_path=CONVERSATION_DIRECTION_TRACE_PATH,
            )
            # No fabricated act, no ownership change, no Pass 2, no
            # ordinary-generation fallback, no persistence --
            # propagates exactly like an uncaught real-generation
            # exception already would.
            #
            # OWC7-P2: raw1 failed structural/protocol validation --
            # this is the one branch where a bounded, failure-only
            # preview of what Pass 1 actually returned is captured
            # (spec section 12). Attached to the exception instance
            # itself, after construction, before raising -- does not
            # change ConversationDirectionFailure's constructor or
            # this raise's own behavior in any way.
            direction_failure = ConversationDirectionFailure("pass1", failure1)
            ui_turn_diagnostics.attach_pass1_failure_preview(direction_failure, raw1)
            raise direction_failure

        # OWC7-S1: the one frozen cross-validation rule -- relinquish_
        # direction=True is authorized only when Clark is the current
        # owner. Checked BEFORE any state resolution/Pass 2, same
        # fail-closed discipline as structural Pass-1 failures above.
        direction_owner_before = working_set_before.get("direction_owner", DIRECTION_UNKNOWN)
        cross_failure = cross_validate_direction_control(validated_act, direction_owner_before)
        if (
            cross_failure is None
            and validated_act["discord_correspondence_request"] != DISCORD_CORRESPONDENCE_REQUEST_NONE
            and not discord_action_available
        ):
            cross_failure = ConversationDirectionFailureCode.DISCORD_CORRESPONDENCE_NOT_AUTHORIZED
        if (
            cross_failure is None
            and validated_act["caret_reply_request"] != CARET_REPLY_REQUEST_NONE
            # The occasion reply route is not the general send authority: it binds ONLY the carried
            # message's own already-authorized destination and Clark's exact Pass-2 words, so it does not
            # depend on the owner-private session predicate that gates the general send_message action
            # (which stays unavailable to an identity-less family-mode occasion).
            and caret_occasion is None
        ):
            cross_failure = ConversationDirectionFailureCode.DISCORD_CORRESPONDENCE_NOT_AUTHORIZED
        if cross_failure is None and validated_act["act"] == USE_WORKSPACE:
            if not workspace_action_available:
                cross_failure = ConversationDirectionFailureCode.WORKSPACE_ACTION_NOT_AUTHORIZED
            else:
                # Direction requests and authorized relinquishment are
                # orthogonal to the selected act and may coexist with the
                # supervised resource action.  Separate substantive host
                # actions remain fail-closed rather than silently dropped.
                # A resume_own_pause with NO Clark-placed pause on record resumes nothing; the
                # real model attaches it to ordinary "reopen this thread" language (live
                # 2026-09-21), and it must not block the workspace action. It is dropped only
                # when the host positively knows Clark has no pause; a genuine pause (or an
                # unreadable pause record) keeps the request, so it still conflicts.
                if (
                    validated_act["background_activity_request"] == BACKGROUND_ACTIVITY_REQUEST_RESUME_OWN_PAUSE
                    and _clark_has_no_pause_to_resume()
                ):
                    validated_act = dict(validated_act, background_activity_request=BACKGROUND_ACTIVITY_REQUEST_NONE)
                cross_failure = cross_validate_workspace_coexistence(validated_act)
        if cross_failure is not None:
            set_last_conversation_direction_trace({
                "act": validated_act["act"], "thread": validated_act["thread"],
                "direction_request": validated_act["direction_request"],
                "relinquish_direction": validated_act["relinquish_direction"],
                "working_set_before": working_set_before, "working_set_after": working_set_before,
                "direction_owner_before": direction_owner_before, "direction_owner_after": direction_owner_before,
                "pass1_status": cross_failure, "pass2_status": None,
            })
            conversation_direction_trace.record_trace(
                session_id=session_id, raw_act=raw_act_only, validated_act=validated_act["act"],
                thread=validated_act["thread"],
                pass1_status=cross_failure, working_set_before=working_set_before, working_set_after=working_set_before,
                direction_owner_before=direction_owner_before, direction_owner_after=direction_owner_before,
                direction_request=validated_act["direction_request"], relinquish_direction=validated_act["relinquish_direction"],
                pass2_status=None, event_id=None, staging_id=None,
                trace_path=CONVERSATION_DIRECTION_TRACE_PATH,
            )
            raise ConversationDirectionFailure("pass1", cross_failure)

        # GENERAL CORRESPONDENCE: host authority for Clark's typed correspondence choices.  A choice
        # the host may not carry out is not made and is disclosed (never guessed, never rebuilt); the
        # rest of the turn proceeds.
        correspondence_notes = []
        if (validated_act.get("correspondent_standing_request", CORRESPONDENT_STANDING_REQUEST_NONE)
                != CORRESPONDENT_STANDING_REQUEST_NONE
                or validated_act.get("discord_correspondent_ref") or validated_act.get("withdraw_pending_send_request")
                or validated_act["discord_correspondence_request"] != DISCORD_CORRESPONDENCE_REQUEST_NONE):
            try:
                validated_act, correspondence_notes = discord_correspondence.correspondence_choice_authority(
                    PROVENANCE_DB_DIR, validated_act, source_occasion_ref=caret_source_ref,
                    owner_turn=discord_action_available and caret_occasion is None)
            except Exception:
                correspondence_notes = ["Your correspondence choices could not be checked this turn, so none "
                                        "was made."]
                validated_act = dict(validated_act, correspondent_standing_request=CORRESPONDENT_STANDING_REQUEST_NONE,
                                     discord_correspondent_ref="", withdraw_pending_send_request="",
                                     discord_correspondence_request=DISCORD_CORRESPONDENCE_REQUEST_NONE,
                                     discord_destination_id="", discord_message_text="")

        if validated_act.get("correspondent_standing_request", CORRESPONDENT_STANDING_REQUEST_NONE) != \
                CORRESPONDENT_STANDING_REQUEST_NONE and validated_act.get("_standing_target"):
            validated_correspondence["standing"] = (
                "establish" if validated_act["correspondent_standing_request"] == "establish_standing" else "revoke",
                validated_act["_standing_target"])
        if validated_act.get("withdraw_pending_send_request"):
            validated_correspondence["withdraw"] = validated_act["withdraw_pending_send_request"]

        direction_resolution = resolve_clark_direction_control(
            direction_owner_before, validated_act["direction_request"], validated_act["relinquish_direction"],
        )
        updated_working_set = apply_conversation_act(working_set_before, validated_act)
        updated_working_set["direction_owner"] = direction_resolution["direction_owner_after"]

        if validated_act["act"] == USE_WORKSPACE:
            # The ordinary waking Pass-1 has made Clark's choice to use
            # the workspace observable.  Delegate resource selection,
            # execution, modality routing, bounded result delivery, and
            # canonical persistence to the existing WSP1 production
            # supervisor; no workspace logic is duplicated here and no
            # hidden operator invocation remains.
            import workspace_capability
            import workspace_supervisor

            set_working_set(updated_working_set)
            workspace_paths = workspace_capability.WorkspacePaths.production_defaults()
            try:
                workspace_result = workspace_supervisor.run_one_supervised_workspace_action(
                    orch, prompt, derive_stable_id("actor", "clark"),
                    workspace_paths,
                    human_input_event_id=human_input_event_id,
                    visibility_scope=(
                        _family_view["visibility_scope"] if _family_view is not None else None
                    ),
                    family_view=_family_view,
                    family_feature_active=_family_feature_active,
                    allowed_surface=workspace_supervisor.wd.resource_allowed_surface(workspace_paths),
                    topic_hint=validated_act["thread"],
                    subject_reply_choice=(
                        REPLY_REQUEST_NO_REPLY if validated_act.get("reply_request") == REPLY_REQUEST_NO_REPLY else None),
                )
            except workspace_supervisor.wd.WorkspaceDirectionFailure as exc:
                set_last_conversation_direction_trace({
                    "act": validated_act["act"], "thread": validated_act["thread"],
                    "direction_request": validated_act["direction_request"],
                    "relinquish_direction": validated_act["relinquish_direction"],
                    "working_set_before": working_set_before, "working_set_after": updated_working_set,
                    "direction_owner_before": direction_owner_before,
                    "direction_owner_after": direction_resolution["direction_owner_after"],
                    "pass1_status": "ok", "pass2_status": exc.failure_code,
                })
                conversation_direction_trace.record_trace(
                    session_id=session_id, raw_act=raw_act_only, validated_act=validated_act["act"],
                    thread=validated_act["thread"], pass1_status="ok",
                    working_set_before=working_set_before, working_set_after=updated_working_set,
                    direction_owner_before=direction_owner_before,
                    direction_owner_after=direction_resolution["direction_owner_after"],
                    direction_request=validated_act["direction_request"],
                    relinquish_direction=validated_act["relinquish_direction"],
                    pass2_status=exc.failure_code, event_id=None, staging_id=None,
                    trace_path=CONVERSATION_DIRECTION_TRACE_PATH,
                )
                _wrapped = ConversationDirectionFailure(f"workspace_{exc.stage}", exc.failure_code)
                if getattr(exc, "pass2_marker_shape", None) is not None:
                    _wrapped.pass2_marker_shape = exc.pass2_marker_shape
                raise _wrapped from exc

            workspace_pass2_status = (PASS2_STATUS_NOT_COMPOSED_NO_REPLY
                                      if workspace_result.get("reply_choice") == REPLY_REQUEST_NO_REPLY else "ok")
            set_last_conversation_direction_trace({
                "act": validated_act["act"], "thread": validated_act["thread"],
                "direction_request": validated_act["direction_request"],
                "relinquish_direction": validated_act["relinquish_direction"],
                "working_set_before": working_set_before, "working_set_after": updated_working_set,
                "direction_owner_before": direction_owner_before,
                "direction_owner_after": direction_resolution["direction_owner_after"],
                "pass1_status": "ok", "pass2_status": workspace_pass2_status,
            })
            conversation_direction_trace.record_trace(
                session_id=session_id, raw_act=raw_act_only, validated_act=validated_act["act"],
                thread=validated_act["thread"], pass1_status="ok",
                working_set_before=working_set_before, working_set_after=updated_working_set,
                direction_owner_before=direction_owner_before,
                direction_owner_after=direction_resolution["direction_owner_after"],
                direction_request=validated_act["direction_request"],
                relinquish_direction=validated_act["relinquish_direction"],
                pass2_status=workspace_pass2_status, event_id=workspace_result["native_event_id"],
                staging_id=workspace_result.get("_native_staging_id"),
                trace_path=CONVERSATION_DIRECTION_TRACE_PATH,
            )
            _record_artifact_decision(
                human_input_event_id=human_input_event_id, waking_turn_event_id=workspace_result["native_event_id"],
                signal=signal_category, prepared=artifact_result, turn_path="use_workspace",
                final={"artifact_created": False,
                       "detail": {"status": "not_applicable", "reason": "workspace action turn"}})
            workspace_result.update({
                "artifact_result": {
                    "artifact_created": False,
                    "detail": {"status": "not_applicable", "reason": "workspace action turn"},
                },
                "operation_status": "not_authorized",
                "_diagnostic_typed_act": USE_WORKSPACE,
                "background_activity_resume_recorded": False,
                "sleep_timing_action_result": workspace_result.get(
                    "sleep_timing_action_result", {"action": "none", "status": "not_applicable"}),
                "boundary_inspection_result": {"query": None, "delivered": None},
                "operative_directive_action_result": {"action": "none", "status": "not_applicable"},
                "external_info_result": {"query": None, "delivered": None},
                "discord_correspondence": {"outbound": None, "inbound_delivered": None},
            })
            return workspace_result

        # LAWFUL NULL: Clark's typed Pass-1 choice to say nothing in reply this turn.  No Pass 2 is
        # composed or generated, so nothing is delivered by one: every Pass-2 delivery ledger stays
        # empty (a Caret occasion's own message WAS delivered -- it was Pass 1's hard current input).
        # The turn still completes: a canonical X with no prose and Clark's own clark_reply_choice
        # component answers the human's input (never recovery-eligible), and every other typed
        # request he made this turn proceeds exactly as chosen.
        if validated_act["reply_request"] == REPLY_REQUEST_NO_REPLY:
            set_working_set(updated_working_set)
            set_last_conversation_direction_trace({
                "act": validated_act["act"], "thread": validated_act["thread"],
                "direction_request": validated_act["direction_request"],
                "relinquish_direction": validated_act["relinquish_direction"],
                "working_set_before": working_set_before, "working_set_after": updated_working_set,
                "direction_owner_before": direction_owner_before,
                "direction_owner_after": direction_resolution["direction_owner_after"],
                "pass1_status": "ok", "pass2_status": PASS2_STATUS_NOT_COMPOSED_NO_REPLY,
            })
            conv_trace_pending = {
                "raw_act": raw_act_only, "validated_act": validated_act["act"], "thread": validated_act["thread"],
                "pass1_status": "ok", "pass2_status": PASS2_STATUS_NOT_COMPOSED_NO_REPLY,
                "working_set_before": working_set_before, "working_set_after": updated_working_set,
                "direction_owner_before": direction_owner_before,
                "direction_owner_after": direction_resolution["direction_owner_after"],
                "direction_request": validated_act["direction_request"],
                "relinquish_direction": validated_act["relinquish_direction"],
            }
            subject_reply_choice = REPLY_REQUEST_NO_REPLY
            clark_prose = ""
            caret_reply_route_intent = False
            caret_reply_report = {"status": "no_reply_chosen"} if caret_occasion is not None else None
            delivered_episode_run_id = None
            delivered_active_workspace_marker = None
            delivered_boundary_query_event_id = None
            delivered_external_info_query_event_id = None
            delivered_external_info_rendered_text = None
            delivered_resource_encounter_event_id = None
            delivered_discord_inbound_event_ids = [caret_occasion["event_id"]] if caret_occasion is not None else None
            live_external_request = None
            external_info_pre_reply_state = "not_performed"
            pending_background_activity_request = validated_act["background_activity_request"]
            pending_sleep_timing_request = validated_act["sleep_timing_request"]
            pending_boundary_inquiry_request = validated_act["boundary_inquiry_request"]
            pending_operative_directive_request = validated_act["operative_directive_request"]
            pending_operative_directive_text = validated_act["operative_directive_text"]
            pending_external_info_request = validated_act["external_info_request"]
            pending_external_info_target = validated_act["external_info_target"]
            pending_discord_correspondence_request = validated_act["discord_correspondence_request"]
            pending_discord_destination_id = validated_act["discord_destination_id"]
            pending_discord_message_text = validated_act["discord_message_text"]
        else:

            # OWC9-P3C section 2: Pass-2's task/schema instruction text is
            # computed once, early, and reused verbatim below, never
            # recomputed -- same discipline as OWC9-P3A originally
            # established. Now split into two pieces, mirroring Pass-1's
            # own P3B message-boundary split: an IMMUTABLE task+closing-cue
            # message (PASS2_TASK_INSTRUCTION + the fixed closing cue --
            # depends on nothing turn-specific, calibrated below) and a
            # separate DYNAMIC directional-state message (the selected act,
            # thread, direction_owner/direction_request state descriptions,
            # relinquishment -- genuinely different every turn, stays
            # byte-estimated, never folded into the calibrated scaffold).
            # PRIVATE DISCORD CORRESPONDENCE V0: the real receive boundary.
            # Poll every currently owner-authorized destination once before
            # selecting pending Pass-2 carriage (each destination's current
            # authorization epoch supplies a prospective Discord `after` cursor,
            # so enabling correspondence never imports older history). It runs
            # BEFORE the Pass-2 host-state text is composed so that text can say,
            # truthfully, what this turn's receive check found: live finding
            # (2026-09-20) -- with nothing received, an unrelated carried JSON
            # source record was taken for the message Alex said he sent. Missing
            # credentials/transport failures ingest nothing; they cannot
            # fabricate receipt or break an otherwise-valid turn.
            caret_receive_text = ""
            if discord_action_available and caret_occasion is None:
                try:
                    caret_polled = discord_correspondence.poll_authorized_inbound(
                        PROVENANCE_DB_DIR, occurred_at=int(datetime.now(timezone.utc).timestamp())
                    )
                except Exception:
                    caret_polled = [{"status": "error", "ingested": []}]
                caret_receive_text = discord_correspondence.describe_receive_check(caret_polled)
            relinquish_text = describe_relinquishment(validated_act["relinquish_direction"])
            pass2_immutable_task_message_text = PASS2_TASK_INSTRUCTION
            pass2_directional_state_text = (
                f"Your selected act: {validated_act['act']}\n"
                + (f"Thread: {validated_act['thread']}\n" if validated_act["thread"] else "")
                + f"\ndirection_owner: {direction_resolution['direction_owner_after']}\n"
                f"{describe_direction_owner(direction_resolution['direction_owner_after'])}\n"
                f"\ndirection_request: {validated_act['direction_request']}\n"
                f"{describe_direction_request(validated_act['direction_request'])}\n"
                + (f"\n{relinquish_text}\n" if relinquish_text else "")
            )

            # Read-only external information is performed HERE, between Pass 1 and Pass 2 of the same
            # turn, so this reply is composed with the real outcome (not a promise the subject could
            # only narrate). Persisted after the turn commits, from this exact outcome.
            live_external_request = None
            external_info_pre_reply_state = "not_performed"
            if validated_act["external_info_request"] != EXTERNAL_INFO_REQUEST_NONE and human_input_event_id is not None:
                try:
                    live_external_request = external_information.dispatch_request_before_reply(
                        PROVENANCE_DB_DIR, session_id=session_id, session_started_at=session_started_at,
                        pipeline_key=PIPELINE_KEY, human_input_event_id=human_input_event_id,
                        operation=validated_act["external_info_request"],
                        target=validated_act["external_info_target"],
                        occurred_at=int(datetime.now(timezone.utc).timestamp()),
                    )
                    external_info_pre_reply_state = "performed"
                except external_information.RequestAlreadyDispatchedError:
                    external_info_pre_reply_state = "already_sent_for_this_message"
                except Exception:
                    live_external_request = None
                    try:
                        attempted = external_information.input_has_external_request(
                            PROVENANCE_DB_DIR, human_input_event_id)
                    except Exception:
                        attempted = True   # cannot prove nothing was sent: never send a second time
                    if attempted:
                        # Stage 1 (and possibly the dispatch marker) is durable: the request may have been
                        # sent. Its outcome is reconciled as NOT ESTABLISHED and delivered; never resent.
                        external_info_pre_reply_state = "attempted"
                    # else: refused before anything durable existed -- nothing sent, nothing recorded as done;
                    # the ordinary after-commit path may still send it once.
            outward_requests_text = ("\n".join(correspondence_notes) + "\n" if correspondence_notes else "") + describe_outward_requests(
                validated_act, external_info_performed=external_info_pre_reply_state)
            if discord_action_available:
                try:
                    prior_outbound_text = discord_correspondence.describe_prior_outbound(
                        discord_correspondence.undisclosed_prior_outbound(PROVENANCE_DB_DIR))
                except Exception:
                    prior_outbound_text = ""
                if prior_outbound_text:
                    outward_requests_text = (prior_outbound_text + "\n" + outward_requests_text).strip("\n")
            if outward_requests_text:
                pass2_directional_state_text += "\n" + outward_requests_text + "\n"
            if caret_receive_text:
                pass2_directional_state_text += "\n" + caret_receive_text + "\n"
            if caret_occasion is not None:
                pass2_directional_state_text += "\n" + discord_correspondence.render_occasion_transport_fact(
                    send_selected=validated_act["discord_correspondence_request"] != DISCORD_CORRESPONDENCE_REQUEST_NONE,
                    reply_route=validated_act["caret_reply_request"] != CARET_REPLY_REQUEST_NONE,
                ) + "\n"
                # HOST-FRAMING LEAK repair: the mechanical sender/channel/provenance facts, delivered
                # here in host/system framing, separately from the exact words that ride as Pass 2's
                # ordinary current human message (pass2_current_user_message above) -- never combined
                # into one model-visible object eligible to be copied as Clark's own outward expression.
                pass2_directional_state_text += "\n" + discord_correspondence.render_inbound_provenance(
                    caret_occasion
                ) + "\n"
            pass2_directional_state_text += '\n\n' + temporal_text

            # OWC9: Pass-2's RICHER contributor set -- the same HARD core
            # system content/current human message, PLUS EPISODE_CONTEXT
            # and ACTIVE_WORKSPACE_CONTINUITY as SOFT contributions (spec
            # section 11: "rich" does not mean uncapped -- all compete under
            # Pass-2's own aggregate budget), PLUS Pass-2's own independent
            # copy of the shared dialogue-window units (spec section 12:
            # copied, not re-queried, from the SAME underlying list Pass-1
            # already saw -- Pass-2 may end up keeping a different number of
            # pairs than Pass-1 did if its own larger task-instruction/
            # generation-reserve leaves less room, but never a
            # CONTRADICTORY pair, only a consistent prefix/suffix subset of
            # the identical source list).
            # Prior turns are shown exactly as canonically persisted: ordinary Pass 2 is a plain chat
            # completion (conversation_direction SHARED ORDINARY EXPRESSION SEAM), so no protocol
            # marker is re-attached to Clark's earlier replies.
            pass2_dialogue_units = list(base_dialogue_window)
            # Waking-pass2-budget repair (production incident, 2026-09-05):
            # base_system_content welds FIXED framing text (mode clause +
            # aesthetic directive -- identical every call, forever) onto the
            # genuinely DYNAMIC, evolving Kardia identity_preamble bullets.
            # Only the fixed piece can be safely pre-calibrated (a stale
            # calibration over the DYNAMIC piece could one day under-count
            # a larger evolved Kardia -- unsafe); the dynamic piece stays on
            # the ordinary, always-safe byte estimator, unconditionally,
            # exactly as before. Pass-2 still receives ALL of base_system_
            # content, unabridged -- this only changes how its two genuinely
            # different parts are COSTED, never what Clark actually sees.
            pass2_fixed_framing_text = (
                CONVERSATION_MODE_CLAUSE + "\n\n" + f"Aesthetic directive: {CONVERSATION_AESTHETIC_DIRECTIVE}"
            )
            pass2_dynamic_identity_text = base_system_content
            _mode_clause_suffix = "\n\n" + CONVERSATION_MODE_CLAUSE
            if pass2_dynamic_identity_text.endswith(_mode_clause_suffix):
                pass2_dynamic_identity_text = pass2_dynamic_identity_text[: -len(_mode_clause_suffix)]
            _aesthetic_target = f"Aesthetic directive: {CONVERSATION_AESTHETIC_DIRECTIVE}"
            if _aesthetic_target in pass2_dynamic_identity_text:
                pass2_dynamic_identity_text = pass2_dynamic_identity_text.replace(_aesthetic_target, "", 1)
            if pass2_dynamic_identity_text == base_system_content:
                # Defensive fallback: the expected fixed substrings were not
                # found (e.g. an unexpected rendering shape) -- keep the
                # WHOLE text on the safe byte estimator under its original
                # kind, exactly as before this repair, rather than silently
                # double-counting or guessing at a partial split.
                pass2_hard_core = context_budget.Contribution(
                    context_budget.CORE_SYSTEM_CONTROL, base_system_content, hard=True,
                )
                pass2_hard_framing = None
            else:
                pass2_hard_core = context_budget.Contribution(
                    context_budget.CORE_SYSTEM_CONTROL, pass2_dynamic_identity_text, hard=True,
                )
                pass2_hard_framing = context_budget.Contribution(
                    context_budget.CONVERSATION_FRAMING, pass2_fixed_framing_text, hard=True,
                )
                pass2_framing_live_fingerprint = _pass2_scaffold_live_fingerprint(
                    pass2_fixed_framing_text, message_structure_signature=("system", "user"),
                )
                pass2_hard_framing.cost = context_budget.calibrated_or_fallback_cost(
                    pass2_hard_framing.rendered_text, pass2_framing_live_fingerprint,
                    _PASS2_FIXED_FRAMING_CALIBRATION, _PASS2_FIXED_FRAMING_CALIBRATED_COST,
                )
            pass2_hard_human = context_budget.Contribution(
                context_budget.CURRENT_HUMAN_MESSAGE, pass2_current_user_message, hard=True,
            )
            if current_human_measured_cost is not None:
                pass2_hard_human.cost = current_human_measured_cost
            pass2_soft_dialogue = dialogue_contribution(pass2_dialogue_units)
            pass2_soft_episode = context_budget.Contribution(
                context_budget.EPISODE_CONTEXT, episode_context_text, hard=False,
                source_ids=[pending_episode_run_id] if episode_context_text and pending_episode_run_id else None,
            )
            pass2_active_events_units = list(active_events)
            pass2_soft_active_workspace = context_budget.Contribution(
                context_budget.ACTIVE_WORKSPACE_CONTINUITY,
                workspace_public_continuity.render_active_workspace_continuity(pass2_active_events_units),
                hard=False, droppable_units=pass2_active_events_units if pass2_active_events_units else None,
                render_fn=workspace_public_continuity.render_active_workspace_continuity,
                source_ids=[e["event_id"] for e in pass2_active_events_units] if pass2_active_events_units else None,
            )

            # OWC9-P2: legacy waking retrieval and hippocampal retrieval as
            # their own SOFT contributions -- previously welded into
            # CORE_SYSTEM_CONTROL (HARD, untrimmable) via orchestration.
            # prepare_context()'s own apply_to_messages() call; now sourced
            # from prepare_context()'s additive structured fields instead.
            # LEGACY_RETRIEVAL is atomic (spec section 5's narrow-repair
            # allowance -- retrieve_waking_context() only exposes a single
            # already-joined blob, and this gate does not perform a broad
            # legacy-memory rewrite to recover per-node units). RETRIEVED_
            # HISTORY (hippocampal) uses the STRUCTURED, already-source-
            # grounded RetrievedMemory items prepare_context() now also
            # returns, each independently droppable.
            legacy_retrieval_text = prepared.get("legacy_retrieval_text") or ""
            # FS1: the legacy blob has no per-event principal/scope
            # provenance, so once Family is active it is withheld from every
            # session EXCEPT the designated owner's own private session, and
            # there only the nodes positively asserted before the canonical
            # Family activation boundary (the legacy owner-private seam in
            # family_membership).  Everyone else gets nothing.
            if _family_feature_active:
                legacy_retrieval_text = _legacy_owner_graph_text(orch, prompt, _family_view)
            pass2_soft_legacy = context_budget.Contribution(
                context_budget.LEGACY_RETRIEVAL, legacy_retrieval_text, hard=False,
            )
            hippocampal_result = prepared.get("hippocampal_retrieval_result")
            hippocampal_items = list(hippocampal_result.items) if hippocampal_result is not None else []
            _person_annotations = {}
            if hippocampal_items:
                # FS1: per-item scope filtering is applied even to an
                # unbound caller. That preserves legacy NULL-scope behavior
                # while preventing an identity-less session from receiving
                # any explicitly principal-scoped event. Scoped sessions
                # additionally get own-private/legacy plus FAMILY_SHARED
                # according to the canonical delivery matrix.
                _hfilter_conn = sqlite3.connect(f"file:{PROVENANCE_DB_DIR}/anaxi_provenance.db?mode=ro", uri=True)
                try:
                    hippocampal_items = family_membership.permitted_hippocampal_items(
                        _hfilter_conn, hippocampal_items,
                        viewer_principal_actor_id=(
                            caret_correspondent_view["key"] if caret_correspondent_view is not None
                            else _family_view["principal_actor_id"] if _family_view is not None else None
                        ),
                        viewer_scope=(
                            family_membership.SCOPE_CORRESPONDENT if caret_correspondent_view is not None
                            else _family_view["visibility_scope"] if _family_view is not None else None
                        ),
                        active_family_principal_ids=(
                            _family_view["active_family_principal_ids"]
                            if _family_view is not None else frozenset()
                        ),
                    )
                    # CPI0: only items that already passed the visibility filter above get person
                    # provenance framing (speaker, historical-utterance frame, owner-recorded class).
                    _person_annotations = canonical_person.annotate_retrieved_items(_hfilter_conn, hippocampal_items)
                finally:
                    _hfilter_conn.close()
            # Existing BM25 -> recency -> item_id order (hippocampus_
            # retrieval.py, unmodified) is best-match-first. Reversed here
            # ONLY so Contribution's own "pop(0) drops the front of
            # droppable_units" convention drops the WORST match first,
            # preserving the best under pressure -- a drop-DIRECTION choice
            # layered on top of the existing, untouched order; never a
            # re-rank of the order itself (spec section 10: no semantic
            # re-ranking).
            pass2_hippocampal_units = list(reversed(hippocampal_items))

            def _render_hippocampal_units(units):
                return hippocampus_retrieval.render_hippocampal_context(
                    hippocampus_retrieval.RetrievalResult(
                        query_terms=hippocampal_result.query_terms if hippocampal_result is not None else (),
                        items=tuple(reversed(units)),
                    ),
                    person_annotations=_person_annotations,
                )

            pass2_soft_hippocampal = context_budget.Contribution(
                context_budget.RETRIEVED_HISTORY,
                _render_hippocampal_units(pass2_hippocampal_units) if pass2_hippocampal_units else "",
                hard=False,
                droppable_units=pass2_hippocampal_units if pass2_hippocampal_units else None,
                render_fn=_render_hippocampal_units if pass2_hippocampal_units else None,
                source_ids=[item.event_id for item in pass2_hippocampal_units] if pass2_hippocampal_units else None,
            )

            # OWC9-P3C section 2: the immutable task+closing-cue message now
            # carries its own empirically CALIBRATED cost (see the
            # module-level calibration record near MODEL above) -- a real,
            # measured ~4.5x tighter bound than the byte estimator for this
            # exact text, fingerprint-guarded so ANY mismatch (a different
            # model, a different Ollama server/chat-template, a different
            # think/interaction_mode/format setting) falls back to the
            # ordinary, always-safe byte estimator automatically. The
            # dynamic directional-state text stays its own separate, always
            # byte-estimated HARD contribution -- both are now covered by
            # preflight, never appended afterward uncounted.
            # CONTROL-SCAFFOLD ATTRIBUTION repair (production incident,
            # 2026-09-05): this text now ships inside the SYSTEM message
            # (see pass2_real_messages below), never its own trailing
            # "user"-role message -- see pass1's identical fix above for the
            # full rationale (Clark misattributing host/control scaffolding
            # to Alex). message_structure_signature="system_merged" matches
            # _PASS2_SCAFFOLD_CALIBRATION's own updated signature --
            # re-measured directly against this exact new merged shape
            # rather than left to fall back to the conservative byte
            # estimator, which would otherwise quietly reintroduce
            # ordinary-turn BUDGET_EXCEEDED failures.
            pass2_task_live_fingerprint = _pass2_scaffold_live_fingerprint(
                pass2_immutable_task_message_text, message_structure_signature=("system_merged",),
            )
            pass2_hard_task = context_budget.Contribution(
                context_budget.MECHANICAL_STATE, pass2_immutable_task_message_text, hard=True,
            )
            pass2_hard_task.cost = context_budget.calibrated_or_fallback_cost(
                pass2_hard_task.rendered_text, pass2_task_live_fingerprint,
                _PASS2_SCAFFOLD_CALIBRATION, _PASS2_SCAFFOLD_CALIBRATED_COST,
            )
            # WORKING_SET (not CORE_SYSTEM_CONTROL/MECHANICAL_STATE) -- a
            # deliberately distinct kind from pass2_hard_task's own
            # MECHANICAL_STATE above, so the two never collide under
            # CompositionResult.included_kind()'s first-match-by-kind
            # lookup or per-kind diagnostic accounting. Semantically apt:
            # this text is the working_set-derived direction-control state.
            pass2_hard_directional_state = context_budget.Contribution(
                context_budget.WORKING_SET, pass2_directional_state_text, hard=True,
            )
            # CAP2-F: bounded, deterministic, SOFT, source-grounded
            # continuity for one genuinely-delivered public resource
            # encounter -- no salience/importance/preference of any kind,
            # a fixed mechanical-fact template only. Empty (no Contribution
            # added at all) when nothing is pending, exactly like every
            # other conditional SOFT source in this function.
            pass2_soft_resource_encounter = None
            if pending_resource_encounter is not None:
                fact = pending_resource_encounter["fact"]
                resource_encounter_text = (
                    "Recent public workspace encounter (mechanical fact only -- not a memory, "
                    "preference, or belief): a "
                    f"{fact['modality']} resource ({fact['resource_class']}/{fact['relative_path']}) "
                    f"was delivered to your own model pathway via a {fact['source_action']} action."
                )
                if fact.get("delivered_portion"):   # what part was delivered, so a continuation can pick up there
                    resource_encounter_text += f" Delivered portion: {fact['delivered_portion']}."
                if fact.get("source_folder"):
                    folders = fact["source_folder"]
                    folders = ", ".join(folders) if isinstance(folders, list) else folders
                    resource_encounter_text += (
                        f" The owner files it under the folder {folders} (that is the owner's filing structure, "
                        "not an identification of anyone or anything in it)."
                    )
                pass2_soft_resource_encounter = context_budget.Contribution(
                    context_budget.RESOURCE_ENCOUNTER, resource_encounter_text, hard=False,
                )
            # BOUNDARY INSPECTOR v1: the FIFO next undelivered boundary-
            # inspection result for THIS session, selected read-only at
            # Pass-2 assembly time (durable rowid order -- see next_pending_
            # boundary_result's own docstring). Offered as its own SOFT
            # contribution, Pass-2-only, hard=False -- drops under budget
            # pressure exactly like any other SOFT source, and a dropped
            # result is simply NOT durably acknowledged this turn (never
            # consumed, never marked delivered). Empty when nothing is
            # pending. Session-scoped: a result belonging to another session
            # is never offered here.
            pending_boundary_result = boundary_inspector.next_pending_boundary_result(PROVENANCE_DB_DIR, session_id)
            pass2_soft_boundary_result = None
            if pending_boundary_result is not None:
                pass2_soft_boundary_result = context_budget.Contribution(
                    context_budget.BOUNDARY_INSPECTION_RESULT,
                    boundary_inspector.render_boundary_result_delivery(pending_boundary_result["result"]),
                    hard=False,
                    source_ids=[pending_boundary_result["query_event_id"]],
                )
            # OD1: fetched FRESH here, every turn -- never cached in the
            # process-local working_set (see conversation_direction.py's own
            # explicit non-durability contract for that structure), which is
            # what makes the active directive correctly survive a restart
            # (operative_directive.fetch_active_directive()'s own docstring).
            # None (no Contribution constructed at all) when no directive is
            # active, so the null state contributes nothing to the prompt
            # (O11) -- this mirrors pass2_soft_boundary_result's own
            # conditional-construction shape immediately above.
            active_operative_directive = operative_directive.fetch_active_directive_for_viewer(
                PROVENANCE_DB_DIR,
                family_feature_active=_family_feature_active,
                family_view=_family_view,
            )
            pass2_soft_operative_directive = None
            rendered_operative_directive = operative_directive.render_operative_directive_context(active_operative_directive)
            if rendered_operative_directive:
                pass2_soft_operative_directive = context_budget.Contribution(
                    context_budget.OPERATIVE_DIRECTIVE, rendered_operative_directive, hard=False,
                    source_ids=[active_operative_directive["active_event_id"]],
                    droppable_units=[rendered_operative_directive],
                    render_fn=lambda units: units[0] if units else "",
                    minimum_units=1,
                )
            # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: the FIFO next
            # undelivered search/fetch result for THIS session, selected
            # read-only at Pass-2 assembly time -- exactly mirroring
            # pass2_soft_boundary_result immediately above. SOFT, Pass-2-
            # only; drops under budget pressure with no durable consequence
            # (never consumed, never marked delivered). Empty when nothing
            # is pending.
            # Crash recovery is fail-closed: incomplete prior requests become
            # explicit outcome-not-established results here without any network
            # retry. This is bookkeeping only, never a browsing agenda.
            external_information.reconcile_incomplete_external_info_results(
                PROVENANCE_DB_DIR, session_id
            )
            # PRIVATE DISCORD CORRESPONDENCE V0: fail-closed recovery for any
            # prior outbound attempt interrupted between its 'attempted'
            # receipt and a terminal outcome. Bookkeeping only -- appends a
            # single not_established receipt and NEVER resends (a new attempt
            # requires a fresh explicit Clark act). Acts with no attempt row
            # are provably never-sent and are left untouched.
            discord_correspondence.reconcile_outbound_attempts(
                PROVENANCE_DB_DIR, occurred_at=int(datetime.now(timezone.utc).timestamp())
            )
            pending_external_info_result = external_information.next_pending_external_info_result(
                PROVENANCE_DB_DIR, session_id
            )
            if live_external_request is not None:
                # This turn's own, already-durable outcome takes the slot (an older undelivered result
                # stays pending); its delivery is acknowledged by the ordinary carriage path below.
                pending_external_info_result = {
                    "query_event_id": live_external_request["query_event_id"],
                    "result": live_external_request["result"],
                }
            pass2_soft_external_info_result = None
            if pending_external_info_result is not None:
                pass2_soft_external_info_result = context_budget.Contribution(
                    context_budget.EXTERNAL_INFO_RESULT,
                    external_information.render_external_info_result_delivery(pending_external_info_result["result"]),
                    hard=False,
                    source_ids=[pending_external_info_result["query_event_id"]],
                )
            # PRIVATE DISCORD CORRESPONDENCE V0: FIFO next undelivered
            # received messages, host-framed as explicitly-untrusted external
            # data, offered as ONE soft Pass-2-only contribution. Selection
            # is read-only; delivery is acknowledged only later, from what
            # this composition actually included after trimming.
            # Already-revoked destinations are never offered (fail closed).
            # No network call happens here -- ingestion is a separate,
            # caller-driven step.
            pending_discord_inbound = (
                discord_correspondence.next_pending_inbound(
                    PROVENANCE_DB_DIR, limit=discord_correspondence.DEFAULT_PENDING_INBOUND_LIMIT
                )
                if discord_action_available and caret_occasion is None else []
            )
            # A Mac turn carries only the OWNER's own Caret messages; another principal's correspondence
            # is answered on its own Caret occasion and is never folded into the owner's Mac window.
            pending_discord_inbound = [
                p for p in pending_discord_inbound
                if (p.get("correspondent") or {}).get("role") == discord_author_mapping.ROLE_OWNER
            ]
            pass2_soft_discord_inbound = None
            if pending_discord_inbound:
                rendered_discord_inbound = "\n\n".join(
                    discord_correspondence.render_inbound_delivery(pending)
                    for pending in pending_discord_inbound
                )
                pass2_soft_discord_inbound = context_budget.Contribution(
                    context_budget.DISCORD_INBOUND_CARRIAGE,
                    rendered_discord_inbound,
                    hard=False,
                    source_ids=[pending["event_id"] for pending in pending_discord_inbound],
                )
            pass2_contributions = [
                pass2_hard_core, pass2_hard_human, pass2_hard_task, pass2_hard_directional_state, pass2_soft_dialogue,
                pass2_soft_episode, pass2_soft_active_workspace, pass2_soft_legacy, pass2_soft_hippocampal,
            ]
            if pass2_hard_framing is not None:
                pass2_contributions.append(pass2_hard_framing)
            if pass2_soft_resource_encounter is not None:
                pass2_contributions.append(pass2_soft_resource_encounter)
            if pass2_soft_boundary_result is not None:
                pass2_contributions.append(pass2_soft_boundary_result)
            if pass2_soft_operative_directive is not None:
                pass2_contributions.append(pass2_soft_operative_directive)
            if pass2_soft_external_info_result is not None:
                pass2_contributions.append(pass2_soft_external_info_result)
            # Source identity of a page fetched on an EARLIER turn (its text is not repeated).
            try:
                earlier_source = external_information.latest_fetched_source(
                    PROVENANCE_DB_DIR, session_id,
                    exclude_retrieval_id=(pending_external_info_result["result"].get("retrieval_id")
                                          if pending_external_info_result is not None else None))
            except Exception:
                earlier_source = None
            if earlier_source is not None:
                pass2_contributions.append(context_budget.Contribution(
                    context_budget.SOURCE_PROVENANCE, external_information.render_source_provenance(earlier_source),
                    hard=False))
            if pass2_soft_discord_inbound is not None:
                pass2_contributions.append(pass2_soft_discord_inbound)
            recost_when_shedding(
                pass2_contributions, context_budget.PASS2_MAX_PROMPT_BUDGET, refill_unused_capacity=True,
            )
            if pass2_soft_discord_inbound is not None:
                fit_inbound_fifo_prefix(
                    pass2_soft_discord_inbound, pending_discord_inbound,
                    pass2_contributions, context_budget.PASS2_MAX_PROMPT_BUDGET, refill_unused_capacity=True,
                )
            if pending_external_info_result is not None:
                grow_external_info_window(
                    pass2_soft_external_info_result, pending_external_info_result["result"],
                    pass2_contributions, context_budget.PASS2_MAX_PROMPT_BUDGET, refill_unused_capacity=True,
                )
            pass2_result = context_budget.compose_within_budget(
                pass2_contributions, context_budget.PASS2_MAX_PROMPT_BUDGET,
                refill_unused_capacity=True,
            )
            if not pass2_result.fits:
                # Multi-turn waking reliability repair (production incident,
                # 2026-09-05): see pass1's identical repair above for the
                # full rationale -- this is the exact stage the real
                # production failure occurred at (a genuinely ordinary,
                # substantial human message pushed pass2's HARD-only sum
                # over PASS2_MAX_PROMPT_BUDGET purely on conservative
                # byte-estimated accounting).
                pass2_result = _recover_hard_overflow_via_real_measurement(
                    pass2_result, pass2_contributions, context_budget.PASS2_MAX_PROMPT_BUDGET,
                    pass2_hard_human, pass2_current_user_message, structured_schema=None,
                )
            ui_turn_diagnostics.record_context_budget_result("pass2", pass2_result)
            if not pass2_result.fits:
                set_last_conversation_direction_trace({
                    "act": validated_act["act"], "thread": validated_act["thread"],
                    "direction_request": validated_act["direction_request"],
                    "relinquish_direction": validated_act["relinquish_direction"],
                    "working_set_before": working_set_before, "working_set_after": updated_working_set,
                    "direction_owner_before": direction_owner_before,
                    "direction_owner_after": direction_resolution["direction_owner_after"],
                    "pass1_status": "ok", "pass2_status": context_budget.BUDGET_EXCEEDED,
                })
                conversation_direction_trace.record_trace(
                    session_id=session_id, raw_act=raw_act_only, validated_act=validated_act["act"],
                    thread=validated_act["thread"], pass1_status="ok",
                    working_set_before=working_set_before, working_set_after=updated_working_set,
                    direction_owner_before=direction_owner_before,
                    direction_owner_after=direction_resolution["direction_owner_after"],
                    direction_request=validated_act["direction_request"], relinquish_direction=validated_act["relinquish_direction"],
                    pass2_status=context_budget.BUDGET_EXCEEDED, event_id=None, staging_id=None,
                    trace_path=CONVERSATION_DIRECTION_TRACE_PATH,
                )
                set_working_set(updated_working_set)
                raise ConversationDirectionFailure("pass2", context_budget.BUDGET_EXCEEDED)

            # WSP2-P3-P1/WSP2-P4-P1 (spec OWC9 section 18): delivery markers
            # are set from what SURVIVED composition, never from the raw
            # pre-trim material above -- "not selected this turn due to
            # budget" != "durably delivered". None if the whole contribution
            # was dropped for budget.
            delivered_episode_run_id = (
                pending_episode_run_id if pass2_result.included_kind(context_budget.EPISODE_CONTEXT) is not None else None
            )
            delivered_active_workspace_marker = pass2_result.delivered_source_ids(context_budget.ACTIVE_WORKSPACE_CONTINUITY) or None
            delivered_resource_encounter_event_id = (
                pending_resource_encounter["event_id"]
                if pending_resource_encounter is not None and pass2_result.included_kind(context_budget.RESOURCE_ENCOUNTER) is not None
                else None
            )
            # BOUNDARY INSPECTOR v1: a pending boundary result counts as
            # durably delivered ONLY if it actually survived THIS turn's
            # final Pass-2 composition -- "dropped for budget" is never
            # acknowledged (same discipline as the markers above).
            delivered_boundary_source_ids = pass2_result.delivered_source_ids(
                context_budget.BOUNDARY_INSPECTION_RESULT
            )
            delivered_boundary_query_event_id = (
                delivered_boundary_source_ids[0]
                if pending_boundary_result is not None
                and delivered_boundary_source_ids == [pending_boundary_result["query_event_id"]]
                else None
            )
            # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: exactly the same
            # "durably delivered only if it actually survived THIS turn's
            # final Pass-2 composition" discipline as the boundary-result
            # marker immediately above.
            delivered_external_info_source_ids = pass2_result.delivered_source_ids(
                context_budget.EXTERNAL_INFO_RESULT
            )
            delivered_external_info_query_event_id = (
                delivered_external_info_source_ids[0]
                if pending_external_info_result is not None
                and delivered_external_info_source_ids == [pending_external_info_result["query_event_id"]]
                else None
            )
            delivered_external_info_rendered_text = (
                pass2_soft_external_info_result.rendered_text
                if delivered_external_info_query_event_id is not None else None
            )
            # PRIVATE DISCORD CORRESPONDENCE V0: same "durably delivered only
            # if it survived THIS turn's final Pass-2 composition" discipline.
            # The inbound contribution carries a whole-message FIFO prefix of the
            # pending set (oldest first): record carriage for exactly the messages
            # that were carried; the rest remain pending, in order.
            delivered_discord_inbound_source_ids = pass2_result.delivered_source_ids(
                context_budget.DISCORD_INBOUND_CARRIAGE
            )
            pending_discord_inbound_event_ids = (
                [pending["event_id"] for pending in pending_discord_inbound]
                if pending_discord_inbound else []
            )
            if (
                pending_discord_inbound_event_ids
                and delivered_discord_inbound_source_ids
                and delivered_discord_inbound_source_ids
                == pending_discord_inbound_event_ids[:len(delivered_discord_inbound_source_ids)]
            ):
                delivered_discord_inbound_event_ids = delivered_discord_inbound_source_ids
            else:
                delivered_discord_inbound_event_ids = None
            if caret_occasion is not None:
                # The message is this turn's HARD current input in both passes, so it is
                # carried exactly when the turn succeeds and persists.
                delivered_discord_inbound_event_ids = [caret_occasion["event_id"]]

            pass2_iface = build_pass2_interface(
                base_system_content, dialogue_messages(pass2_soft_dialogue), pass2_current_user_message, validated_act, updated_working_set,
                direction_resolution,
            )
            # OWC9: the (possibly trimmed) episode/active-workspace
            # continuity text is appended here, at final assembly time --
            # never earlier, and never unconditionally (spec section 5: the
            # authority evaluates the FINAL composed message set).
            pass2_system_content = pass2_iface["system_content"]
            episode_contrib = pass2_result.included_kind(context_budget.EPISODE_CONTEXT)
            if episode_contrib is not None and episode_contrib.rendered_text:
                pass2_system_content = pass2_system_content + "\n\n" + episode_contrib.rendered_text
            active_workspace_contrib = pass2_result.included_kind(context_budget.ACTIVE_WORKSPACE_CONTINUITY)
            if active_workspace_contrib is not None and active_workspace_contrib.rendered_text:
                pass2_system_content = pass2_system_content + "\n\n" + active_workspace_contrib.rendered_text
            # OWC9-P2: retrieved legacy/hippocampal memory, exactly like
            # episode/active-workspace continuity above -- appended only
            # for whatever survived the FINAL composition, never earlier,
            # never unconditionally. Order (legacy then hippocampal) simply
            # mirrors the original memory_context concatenation order
            # (render_legacy_memory_quarantine() output, then
            # HIPPOCAMPAL_MEMORY_SEPARATOR, then the hippocampal block) --
            # no semantic significance beyond preserving that existing
            # visual/textual convention.
            legacy_contrib = pass2_result.included_kind(context_budget.LEGACY_RETRIEVAL)
            if legacy_contrib is not None and legacy_contrib.rendered_text:
                pass2_system_content = pass2_system_content + "\n\n" + legacy_contrib.rendered_text
            hippocampal_contrib = pass2_result.included_kind(context_budget.RETRIEVED_HISTORY)
            if hippocampal_contrib is not None and hippocampal_contrib.rendered_text:
                pass2_system_content = pass2_system_content + "\n\n" + hippocampal_contrib.rendered_text
            resource_encounter_contrib = pass2_result.included_kind(context_budget.RESOURCE_ENCOUNTER)
            if resource_encounter_contrib is not None and resource_encounter_contrib.rendered_text:
                pass2_system_content = pass2_system_content + "\n\n" + resource_encounter_contrib.rendered_text
            boundary_result_contrib = pass2_result.included_kind(context_budget.BOUNDARY_INSPECTION_RESULT)
            if boundary_result_contrib is not None and boundary_result_contrib.rendered_text:
                pass2_system_content = pass2_system_content + "\n\n" + boundary_result_contrib.rendered_text
            # The subject's own active operative directive. It was fetched, rendered
            # and budgeted above but its text was never placed in the prompt, so a
            # SET directive had no operative effect on ordinary waking. Carried
            # only if it survived THIS turn's final composition; the rendered block
            # already labels it lower-precedence than host/system mechanics.
            operative_directive_contrib = pass2_result.included_kind(context_budget.OPERATIVE_DIRECTIVE)
            if operative_directive_contrib is not None and operative_directive_contrib.rendered_text:
                pass2_system_content = pass2_system_content + "\n\n" + operative_directive_contrib.rendered_text
            external_info_result_contrib = pass2_result.included_kind(context_budget.EXTERNAL_INFO_RESULT)
            source_provenance_contrib = pass2_result.included_kind(context_budget.SOURCE_PROVENANCE)
            # PRIVATE DISCORD CORRESPONDENCE V0: the surviving received-message
            # contribution, assembled into its own untrusted-data tool message
            # below -- never concatenated into system/host control.
            discord_inbound_contrib = pass2_result.included_kind(context_budget.DISCORD_INBOUND_CARRIAGE)

            # OWC9-P3C section 2: neither the immutable task message nor the
            # dynamic directional-state text is recomputed here -- the exact
            # same strings already measured and included in pass2_hard_task/
            # pass2_hard_directional_state above are reused verbatim (spec
            # section A: costed text and sent text must be byte-identical).
            #
            # CONTROL-SCAFFOLD ATTRIBUTION repair (production incident,
            # 2026-09-05): both used to ship as their own trailing "user"-
            # role messages, immediately after Alex's own current message --
            # a live production turn on 2026-09-05 shows Clark's own reply
            # referring to "the sudden, highly structured meta-instruction
            # you just provided", i.e. Clark attributing this host-authored
            # JSON-formatting instruction to Alex, because nothing in the
            # message structure ever distinguished it from Alex's own
            # speech. Both now ship inside the single SYSTEM message
            # instead; pass2_iface["current_user_message"] remains the ONLY
            # "user"-role content pass2 ever sees, and it is now also the
            # LAST message, matching ordinary chat-turn shape.
            pass2_system_content = (
                pass2_system_content + "\n\n" + pass2_immutable_task_message_text
                + "\n\n" + pass2_directional_state_text
            )
            pass2_real_messages = (
                [{"role": "system", "content": pass2_system_content}]
                + pass2_iface["dialogue_window"]
                # Retrieved material is a mechanically separate tool-data
                # message. It is never concatenated into system/host control
                # and its JSON envelope labels it untrusted external data.
                + ([external_data_message(external_info_result_contrib.rendered_text)]
                   if external_info_result_contrib is not None
                   and external_info_result_contrib.rendered_text else [])
                + ([external_data_message(source_provenance_contrib.rendered_text)]
                   if source_provenance_contrib is not None and source_provenance_contrib.rendered_text else [])
                + ([external_data_message(discord_inbound_contrib.rendered_text)]
                   if discord_inbound_contrib is not None
                   and discord_inbound_contrib.rendered_text else [])
                + [{"role": "user", "content": pass2_iface["current_user_message"]}]
            )
            # SHARED ORDINARY EXPRESSION SEAM (conversation_direction): one plain chat completion whose
            # assistant message IS Clark's reply.  No grammar/JSON container (the retired
            # {"expression": ...} schema is the established cause of the 2026-09-22 echo and
            # meta-description replies) and no model-typed marker.  Completion is the provider's own
            # done/stop within the explicit decode reserve (call_llama -> require_complete_response);
            # exactly one call, no retry.
            try:
                raw2 = ui_turn_diagnostics.timed_model_call(
                    "pass2", call_llama, pass2_real_messages, prepared["controls"],
                    generation_reserve=context_budget.PASS2_GENERATION_RESERVE,
                )  # the one Pass-2 call
            except context_budget.IncompleteCompletionError as exc:
                validated_expr, failure2 = None, exc.failure_code
            else:
                validated_expr, failure2 = validate_plain_reply(raw2)

            # The already-selected typed act and its working-set/direction-
            # owner transition remain observable regardless of Pass-2
            # outcome (spec section 13/16 of OWC5-S1) -- committed to
            # session state either way.
            set_working_set(updated_working_set)

            if failure2 is not None:
                set_last_conversation_direction_trace({
                    "act": validated_act["act"], "thread": validated_act["thread"],
                    "direction_request": validated_act["direction_request"],
                    "relinquish_direction": validated_act["relinquish_direction"],
                    "working_set_before": working_set_before, "working_set_after": updated_working_set,
                    "direction_owner_before": direction_owner_before,
                    "direction_owner_after": direction_resolution["direction_owner_after"],
                    "pass1_status": "ok", "pass2_status": failure2,
                })
                # The typed act and direction-control resolution were
                # validly committed, so they are preserved in the durable
                # trace -- but no event_id/staging_id, since persistence
                # never runs on this failure path (spec section 9: no
                # expression is falsely recorded as released).
                conversation_direction_trace.record_trace(
                    session_id=session_id, raw_act=raw_act_only, validated_act=validated_act["act"],
                    thread=validated_act["thread"], pass1_status="ok",
                    working_set_before=working_set_before, working_set_after=updated_working_set,
                    direction_owner_before=direction_owner_before,
                    direction_owner_after=direction_resolution["direction_owner_after"],
                    direction_request=validated_act["direction_request"], relinquish_direction=validated_act["relinquish_direction"],
                    pass2_status=failure2, event_id=None, staging_id=None,
                    trace_path=CONVERSATION_DIRECTION_TRACE_PATH,
                )
                raise ConversationDirectionFailure("pass2", failure2)

            set_last_conversation_direction_trace({
                "act": validated_act["act"], "thread": validated_act["thread"],
                "direction_request": validated_act["direction_request"],
                "relinquish_direction": validated_act["relinquish_direction"],
                "working_set_before": working_set_before, "working_set_after": updated_working_set,
                "direction_owner_before": direction_owner_before,
                "direction_owner_after": direction_resolution["direction_owner_after"],
                "pass1_status": "ok", "pass2_status": "ok",
            })
            # OWC6-O1: recorded once persistence below actually succeeds
            # and a real event_id/staging_id exist to bind to -- never
            # guessed from a timestamp. OWC7-S1: no free-form Pass-1
            # content exists anywhere to be tempted to durably persist.
            conv_trace_pending = {
                "raw_act": raw_act_only, "validated_act": validated_act["act"], "thread": validated_act["thread"],
                "pass1_status": "ok", "pass2_status": "ok",
                "working_set_before": working_set_before, "working_set_after": updated_working_set,
                "direction_owner_before": direction_owner_before,
                "direction_owner_after": direction_resolution["direction_owner_after"],
                "direction_request": validated_act["direction_request"],
                "relinquish_direction": validated_act["relinquish_direction"],
            }
            # WSP2-P5-P1: tracked SEPARATELY from conv_trace_pending above,
            # never added to that dict -- conv_trace_pending is passed as
            # **kwargs directly to conversation_direction_trace.record_
            # trace(), whose own fixed parameter signature has no field for
            # this and would raise on an unexpected kwarg.
            pending_background_activity_request = validated_act["background_activity_request"]
            # SLP2: tracked separately from both conv_trace_pending and
            # pending_background_activity_request -- applied further below,
            # strictly after canonical persistence succeeds, exactly
            # mirroring background_activity_request's own ordering.
            pending_sleep_timing_request = validated_act["sleep_timing_request"]
            # BOUNDARY INSPECTOR v1: tracked separately from all three
            # siblings above -- dispatched further below, strictly after
            # canonical persistence succeeds, exactly mirroring their
            # ordering. None for a turn that chose no boundary inquiry
            # (validated_act["boundary_inquiry_request"] is the parse result:
            # None for "none"/absent, else {"query_kind", "query_target"}).
            pending_boundary_inquiry_request = validated_act["boundary_inquiry_request"]
            # OD1: tracked separately from every sibling above -- dispatched
            # further below, strictly after canonical persistence succeeds,
            # exactly mirroring their ordering.
            pending_operative_directive_request = validated_act["operative_directive_request"]
            pending_operative_directive_text = validated_act["operative_directive_text"]
            # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: tracked
            # separately from every sibling above -- dispatched further
            # below, strictly after canonical persistence succeeds, exactly
            # mirroring their ordering.
            pending_external_info_request = validated_act["external_info_request"]
            pending_external_info_target = validated_act["external_info_target"]
            pending_discord_correspondence_request = validated_act["discord_correspondence_request"]
            pending_discord_destination_id = validated_act["discord_destination_id"]
            pending_discord_message_text = validated_act["discord_message_text"]
            # Release: the exact validated expression, verbatim -- no
            # typed-act label, no working-set contents, no host commentary
            # (spec section 14). No _screen_clark_prose/regen in
            # conversation mode -- section 6/12: no retry.
            clark_prose = validated_expr["expression"].strip()
            # CARET OCCASION REPLY ROUTE (owner-approved 2026-09-21): Clark's explicit Pass-1 route choice,
            # followed by his authored Pass-2 words.  The host binds ONLY the occasion's own destination and
            # the exact reply text; nothing is inferred, and an unusable body sends nothing (never rebuilt
            # from other text).  The canonical writer re-verifies both bindings and the dispatcher re-reads them.
            if validated_act["caret_reply_request"] == CARET_REPLY_REQUEST_REPLY and caret_occasion is not None:
                # His explicit choice is canonical intent BEFORE the body is judged; it is not an
                # obligation to dispatch an unusable body (the send components below exist only when usable).
                caret_reply_route_intent = True
                if clark_prose.strip() and not is_outward_safe_expression(clark_prose):
                    # Structural boundary: a body containing annotation lines (control-state
                    # transcript mixed with speech) is not correspondence.  Intent stays recorded.
                    caret_reply_report = {"status": "not_dispatched", "reason": "reply_body_unusable"}
                elif clark_prose.strip() and len(clark_prose) <= discord_correspondence.MAX_CARET_REPLY_TEXT_LENGTH:
                    pending_discord_correspondence_request = CARET_REPLY_REQUEST_REPLY
                    pending_discord_destination_id = caret_occasion["metadata"]["destination_id"]
                    pending_discord_message_text = clark_prose
                    caret_reply_report = {"status": "route_bound"}
                elif clark_prose.strip():
                    # Beyond the multipart transport cap: never truncated, summarized or partially sent.
                    caret_reply_report = {"status": "not_dispatched", "reason": "reply_body_exceeds_transport_cap"}
                else:
                    caret_reply_report = {"status": "not_dispatched", "reason": "reply_body_unusable"}
    else:
        # OWC9-P4 section 4: aggregate preflight before EITHER TASK-mode
        # model call -- a hard-only overflow never reaches call_llama()
        # at all, same fail-closed discipline as every other budgeted
        # pathway. Reuses the SAME ConversationDirectionFailure/
        # BUDGET_EXCEEDED containment llama_gui.py's contain_control_
        # failure() already handles generically (spec: no TASK-mode
        # semantic redesign -- only an existing, already-graceful
        # failure surface is reused here, not a new one).
        task_mode_messages, task_mode_budget = _compose_task_mode_pass2_budget(pass2_messages)
        ui_turn_diagnostics.record_context_budget_result("pass2_task_mode", task_mode_budget)
        if not task_mode_budget.fits:
            raise ConversationDirectionFailure("pass2_task_mode", context_budget.BUDGET_EXCEEDED)
        clark_prose = ui_turn_diagnostics.timed_model_call("pass2_task_mode", call_llama, task_mode_messages, prepared["controls"])
        match_1 = _screen_clark_prose(clark_prose, bounded_clause)

        if match_1["matched"]:
            regen_messages = [dict(m) for m in task_mode_messages]
            regen_messages[0] = dict(regen_messages[0])
            regen_messages[0]["content"] = (
                regen_messages[0]["content"] + "\n\n" + REJECTION_NOTICES[match_1["construction"]]
            )
            regen_messages, regen_budget = _compose_task_mode_pass2_budget(regen_messages)
            ui_turn_diagnostics.record_context_budget_result("pass2_regen", regen_budget)
            if not regen_budget.fits:
                raise ConversationDirectionFailure("pass2_regen", context_budget.BUDGET_EXCEEDED)
            clark_prose_2 = ui_turn_diagnostics.timed_model_call("pass2_regen", call_llama, regen_messages, prepared["controls"])
            match_2 = _screen_clark_prose(clark_prose_2, bounded_clause)

            if match_2["matched"]:
                clark_prose = suppress_matched_sentences(
                    clark_prose_2, match_2["construction"], bounded_clause=bounded_clause
                )
            else:
                clark_prose = clark_prose_2.strip()
        else:
            clark_prose = clark_prose.strip()

    if bounded_clause and clark_prose:
        reply = f"{bounded_clause} {clark_prose}"
    elif bounded_clause:
        reply = bounded_clause
    else:
        reply = clark_prose

    # ===================================================================
    # Canonical-before-legacy: stage_and_record_native_waking_turn()
    # completes a durable, fsync'd staging write and the canonical
    # anaxi_provenance.db transaction (session/auth_context/event/
    # event_model_participation/event_components, all one transaction)
    # BEFORE this function ever reaches the three legacy writes below.
    # If it raises, none of the legacy writes are attempted. Two
    # distinct outcomes, not interchangeable: StagingDurabilityError
    # means the failed append was durably rolled back -- the turn
    # simply did not happen yet, and retrying this whole function call
    # is genuinely safe. StagingIndeterminateStateError means the
    # rollback itself could not be completed -- retry-whole is
    # explicitly FORBIDDEN in that case (native_turn_staging.py); this
    # function does not catch either and lets both propagate to its
    # own caller unchanged, which must observe that distinction rather
    # than treating every staging failure as equally retry-safe.
    # ===================================================================
    occurred_at = int(datetime.now(timezone.utc).timestamp())
    native_result = ui_turn_diagnostics.timed_stage(
        "persistence", native_provenance_writer.stage_and_record_native_waking_turn,
        PROVENANCE_DB_DIR, STAGING_PATH,
        session_id=session_id, session_started_at=session_started_at,
        user_id=USER_ID, prompt=prompt, bounded_clause=bounded_clause, clark_prose=clark_prose,
        kardia=prepared["kardia"], controls=prepared["controls"],
        waking_model_tag=MODEL, pipeline_key=PIPELINE_KEY,
        artifact_pass_ran=(signal_category == "POSITIVE"), occurred_at=occurred_at,
        delivered_episode_run_id=delivered_episode_run_id,
        interaction_mode=resolved_interaction_mode,
        # WSP2-P4-P1 (spec section 5/9/10): durable delivery evidence,
        # written in the SAME atomic transaction as this turn's own
        # persistence -- if that transaction never commits (this call
        # raises), none of these rows exist either, so a failed waking
        # turn can never falsely acknowledge delivery. No separate
        # post-persistence cursor-advance step exists any more.
        delivered_active_workspace_event_ids=delivered_active_workspace_marker,
        # SLP1-A4c: if a genuine authorized human_waking_input event H
        # was committed above (strictly before any model call this
        # turn), link it into Clark's own turn atomically now. None
        # for every ordinary unbound/internal call, exactly as before.
        human_input_event_id=human_input_event_id,
        # FS1: pass the session's binding scope ONLY for authenticated
        # scoped sessions (omitted entirely otherwise -- the unbound
        # path and the integration-test fakes stay byte-identical; the
        # native writer cross-validates this against H's own scope).
        **({"visibility_scope": _family_view["visibility_scope"]}
           if _family_view is not None else {}),
        # Boundary Inspector V1: this id comes directly from the real
        # Pass-2 CompositionResult above.  The native writer validates the
        # query/result/session/pipeline/chronology and records carriage in
        # this SAME canonical waking-turn transaction. Omit the optional
        # keyword entirely when no boundary result survived.
        **({"delivered_boundary_query_event_id": delivered_boundary_query_event_id}
           if delivered_boundary_query_event_id is not None else {}),
        **({
            "operative_directive_request": pending_operative_directive_request,
            "operative_directive_text": pending_operative_directive_text,
        } if pending_operative_directive_request != OPERATIVE_DIRECTIVE_REQUEST_NONE else {}),
        # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: mirrors the
        # operative-directive/boundary-result kwargs immediately above.
        **({
            "external_info_request": pending_external_info_request,
            "external_info_target": pending_external_info_target,
        } if pending_external_info_request != EXTERNAL_INFO_REQUEST_NONE else {}),
        **({"delivered_external_info_query_event_id": delivered_external_info_query_event_id}
           if delivered_external_info_query_event_id is not None else {}),
        # PRIVATE DISCORD CORRESPONDENCE V0: mirrors the external-info
        # kwargs immediately above. The outbound request text/id are
        # canonicalized atomically with this turn; the inbound carriage
        # ids come directly from the real Pass-2 composition and are
        # validated against canonical inbound state by the native writer.
        **({
            "discord_correspondence_request": pending_discord_correspondence_request,
            "discord_destination_id": pending_discord_destination_id,
            "discord_message_text": pending_discord_message_text,
        } if pending_discord_correspondence_request != DISCORD_CORRESPONDENCE_REQUEST_NONE else {}),
        **({"delivered_discord_inbound_event_ids": delivered_discord_inbound_event_ids}
           if delivered_discord_inbound_event_ids is not None else {}),
        **({"caret_correspondent_principal_id": caret_correspondent_principal_id}
           if caret_correspondent_principal_id is not None else {}),
        **({"caret_reply_route_intent": True} if caret_reply_route_intent else {}),
        **({"subject_reply_choice": subject_reply_choice} if subject_reply_choice is not None else {}),
        **({"correspondent_source_ref": caret_source_ref} if caret_source_ref is not None else {}),
        **({"correspondent_standing_request": f"{validated_correspondence['standing'][0]}:{validated_correspondence['standing'][1]}"}
           if "standing" in validated_correspondence else {}),
        **({"discord_correspondent_ref": validated_act["discord_correspondent_ref"]}
           if resolved_interaction_mode == CONVERSATION_MODE and validated_act.get("discord_correspondent_ref")
           and pending_discord_correspondence_request == "send_message" else {}),
        **({"withdraw_pending_send_request": validated_correspondence["withdraw"]}
           if "withdraw" in validated_correspondence else {}),
    )
    if resolved_interaction_mode == CONVERSATION_MODE:
        _temporal_lifecycle_sessions.add(session_id)
    # sequence=1's model_revision_id is always the pass-2/prose one --
    # the last entry appended by record_native_waking_turn()'s
    # _add_participation() calls (pass-1, if it ran, is appended first).
    prose_model_revision_id = native_result["model_participations"][-1]["model_revision_id"]

    # GENERAL CORRESPONDENCE: Clark's standing act / withdrawal, strictly after the canonical turn that
    # carries his typed choice committed (the ledger triggers re-prove that exact binding).  A failure
    # here never turns the committed turn into a failed one; it is reported truthfully.
    correspondence_result = {"standing": None, "withdrawal": None, "notes": list(correspondence_notes)}
    if "standing" in validated_correspondence:
        action, target = validated_correspondence["standing"]
        try:
            kind, value = correspondence_surfaces.parse_source_ref(target)
            correspondence_result["standing"] = dict(correspondence_surfaces.record_standing_act(
                PROVENANCE_DB_DIR, kind=kind, value=value, action=action,
                waking_turn_event_id=native_result["event_id"], occurred_at=occurred_at), action=action, ref=target)
        except Exception as exc:
            correspondence_result["standing"] = {"action": action, "ref": target, "status": "error",
                                                 "detail": type(exc).__name__}
    if "withdraw" in validated_correspondence:
        try:
            correspondence_result["withdrawal"] = correspondence_surfaces.record_outbound_lifecycle(
                PROVENANCE_DB_DIR, send_turn_event_id=validated_correspondence["withdraw"],
                action="withdrawn_by_clark", requester_actor_id="clark", occurred_at=occurred_at,
                evidence_event_id=native_result["event_id"])
        except Exception as exc:
            correspondence_result["withdrawal"] = {"status": "error", "detail": type(exc).__name__}

    # CAP2-F: durable delivery marker for the resource-encounter
    # continuity fact rendered above, if any survived composition.
    # Deliberately NOT folded into stage_and_record_native_waking_turn()'s
    # own atomic transaction (unlike delivered_active_workspace_event_ids)
    # -- a scope-minimizing choice this gate, documented in the CAP2
    # final report -- so this is best-effort: a write failure here
    # leaves the fact still eligible for a later turn (find_pending_
    # resource_encounter_for_continuity() would simply offer it again),
    # never silently lost and never blocking an already-committed turn.
    if delivered_resource_encounter_event_id is not None:
        try:
            workspace_episode_provenance.record_resource_encounter_continuity_delivered(
                PROVENANCE_DB_DIR, pipeline_key=PIPELINE_KEY, occurred_at=occurred_at,
                event_id=delivered_resource_encounter_event_id,
            )
        except workspace_episode_provenance.EpisodeProvenanceError:
            pass

    # BOUNDARY INSPECTOR v1: positive durable delivery marker for a
    # boundary-inspection result that actually survived THIS turn's
    # Pass-2 composition. Its authoritative carriage fact was folded
    # into stage_and_record_native_waking_turn() above; only the FIFO
    # delivered/dequeued consequence is written here, after canonical
    # persistence succeeded. That consequence remains best-effort: a
    # write failure here leaves the result still
    # eligible for a later turn's Pass-2 pick-up, never silently lost
    # and never blocking an already-committed conversation turn.
    #
    # The canonical waking-turn transaction above already recorded the
    # actual composition inclusion.  Delivery now reads that durable
    # fact; it accepts no composition object or caller-authored carriage.
    if delivered_boundary_query_event_id is not None:
        try:
            boundary_inspector.record_boundary_inspection_delivered(
                PROVENANCE_DB_DIR, query_event_id=delivered_boundary_query_event_id,
                waking_turn_event_id=native_result["event_id"], occurred_at=occurred_at,
            )
            boundary_inspection_report_delivered = {
                "status": "recorded",
                "query_event_id": delivered_boundary_query_event_id,
                "waking_turn_event_id": native_result["event_id"],
            }
        except boundary_inspector.BoundaryInspectionError as exc:
            boundary_inspection_report_delivered = {"status": "not_recorded", "detail": str(exc)}
        except Exception as exc:
            boundary_inspection_report_delivered = {"status": "not_recorded", "detail": str(exc)}

    # BOUNDARY INSPECTOR v1: dispatch this turn's own typed boundary
    # inquiry, strictly after canonical persistence actually succeeded
    # (native_result exists above) -- a query from a failed/contained
    # turn (which raises earlier and never reaches this point) must never
    # be recorded. Stage 1 (the clark_boundary_query event) and stage 2
    # (its boundary_inspection_result component) are separate
    # transactions inside record_boundary_query_and_result(); an
    # evaluation/stage-2 failure leaves the query occurrence itself
    # intact and is reported truthfully here as not_recorded -- never
    # silently retried, never fabricating a result, and never turning an
    # already-committed successful waking turn into a failed one. The
    # same-turn result is NEVER offered to this same turn's Pass 2 (its
    # pick-up ran earlier, during Pass-2 assembly); a later lawful
    # conversation-mode turn in this session picks it up FIFO.
    if pending_boundary_inquiry_request is not None:
        try:
            dispatched = boundary_inspector.record_boundary_query_and_result(
                PROVENANCE_DB_DIR,
                session_id=session_id, session_started_at=session_started_at,
                pipeline_key=PIPELINE_KEY, query=pending_boundary_inquiry_request,
                occurred_at=occurred_at, input_source_ref=native_result["event_id"],
                trace_path=CONVERSATION_DIRECTION_TRACE_PATH,
            )
            boundary_inspection_report_query = {
                "status": "recorded",
                "query_event_id": dispatched["query_event_id"],
                "query_kind": pending_boundary_inquiry_request["query_kind"],
                "query_target": pending_boundary_inquiry_request["query_target"],
                "classification": dispatched["classification"],
            }
        except boundary_inspector.BoundaryInspectionError as exc:
            boundary_inspection_report_query = {"status": "not_recorded", "detail": str(exc)}
        except Exception as exc:
            boundary_inspection_report_query = {"status": "not_recorded", "detail": str(exc)}

    # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: positive durable
    # delivery marker for a search/fetch result that actually survived
    # THIS turn's Pass-2 composition -- exactly mirrors the boundary-
    # inspection delivery-marker block immediately above, including its
    # best-effort discipline (a write failure here leaves the result
    # still eligible for a later turn's Pass-2 pick-up).
    if delivered_external_info_query_event_id is not None:
        try:
            external_information.record_external_info_delivered(
                PROVENANCE_DB_DIR, query_event_id=delivered_external_info_query_event_id,
                waking_turn_event_id=native_result["event_id"], occurred_at=occurred_at,
            )
            external_info_report_delivered = {
                "status": "recorded",
                "query_event_id": delivered_external_info_query_event_id,
                "waking_turn_event_id": native_result["event_id"],
            }
            # Which part of the result reached the subject (live 2026-09-24: the canonical marker
            # said "delivered" but not which characters of a 20,000-char page). Durable host-side
            # evidence beside the workspace's own delivered portions; best effort, like the marker.
            window = external_information.delivered_window(delivered_external_info_rendered_text)
            if window is not None:
                external_info_report_delivered["window"] = window
                try:
                    import workspace_capability
                    workspace_capability._log_action(
                        workspace_capability.WorkspacePaths.production_defaults(), "external_information",
                        "delivered", delivered_external_info_query_event_id, derive_stable_id("actor", "clark"),
                        "performed", None, dict(window, waking_turn_event_id=native_result["event_id"]))
                except OSError:
                    external_info_report_delivered["window_log"] = "not_recorded"
        except external_information.ExternalInformationError as exc:
            external_info_report_delivered = {"status": "not_recorded", "detail": str(exc)}
        except Exception as exc:
            external_info_report_delivered = {"status": "not_recorded", "detail": str(exc)}

    # PRIVATE DISCORD CORRESPONDENCE V0: positive durable delivery marker
    # for each received message that actually survived THIS turn's Pass-2
    # composition -- exactly mirrors the external-info delivery-marker
    # block immediately above. record_inbound_delivered() can only mark a
    # message whose canonical carriage component this same waking turn
    # already carries; a write failure here leaves the message still
    # pending for a later turn (never lost, never falsely acknowledged).
    if delivered_discord_inbound_event_ids:
        discord_inbound_recorded = []
        discord_inbound_not_recorded = []
        for inbound_event_id in delivered_discord_inbound_event_ids:
            try:
                discord_correspondence.record_inbound_delivered(
                    PROVENANCE_DB_DIR, inbound_event_id=inbound_event_id,
                    waking_turn_event_id=native_result["event_id"], occurred_at=occurred_at,
                )
                discord_inbound_recorded.append(inbound_event_id)
            except Exception as exc:
                discord_inbound_not_recorded.append({"event_id": inbound_event_id, "detail": str(exc)})
        discord_inbound_report_delivered = {
            "status": "recorded" if not discord_inbound_not_recorded else "partially_recorded",
            "event_ids": discord_inbound_recorded,
            "not_recorded": discord_inbound_not_recorded,
        }

    # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: dispatch this turn's
    # own typed search/fetch request, strictly after canonical
    # persistence actually succeeded (native_result exists above) --
    # exactly mirrors the boundary-inquiry dispatch block immediately
    # above. Stage 1 (the clark_external_info_query event) and stage 2
    # (the actual bounded network operation + its result component) are
    # separate transactions inside record_external_info_query_and_
    # result(); a network/stage-2 failure leaves the query occurrence
    # itself intact and is reported truthfully here as not_recorded --
    # never silently retried, never fabricating a result, and never
    # turning an already-committed successful waking turn into a failed
    # one. The same-turn result is NEVER offered to this same turn's
    # Pass 2 (its pick-up ran earlier, during Pass-2 assembly); a later
    # lawful conversation-mode turn in this session picks it up FIFO.
    if pending_external_info_request != EXTERNAL_INFO_REQUEST_NONE and live_external_request is not None:
        # Already stage-1 committed, marker-committed, sent and result-committed BEFORE Pass 2
        # (dispatch_request_before_reply), anchored to this turn's human input: nothing to send again.
        external_info_report_query = {
            "status": "recorded_before_reply",
            "query_event_id": live_external_request["query_event_id"],
            "operation": pending_external_info_request,
            "target": pending_external_info_target,
            "result_status": live_external_request["result"]["status"],
        }
    elif pending_external_info_request != EXTERNAL_INFO_REQUEST_NONE and external_info_pre_reply_state in ("already_sent_for_this_message", "attempted"):
        external_info_report_query = {
            "status": "not_recorded",
            "detail": ("this human input already has an external request on record (it may have been "
                       "sent); it is not sent again"),
        }
    elif pending_external_info_request != EXTERNAL_INFO_REQUEST_NONE:
        try:
            dispatched = external_information.record_external_info_query_and_result(
                PROVENANCE_DB_DIR,
                session_id=session_id, session_started_at=session_started_at,
                pipeline_key=PIPELINE_KEY, operation=pending_external_info_request,
                target=pending_external_info_target,
                occurred_at=occurred_at, input_source_ref=native_result["event_id"],
            )
            external_info_report_query = {
                "status": "recorded",
                "query_event_id": dispatched["query_event_id"],
                "operation": pending_external_info_request,
                "target": pending_external_info_target,
                "result_status": dispatched["status"],
            }
        except external_information.ExternalInformationError as exc:
            external_info_report_query = {"status": "not_recorded", "detail": str(exc)}
        except Exception as exc:
            external_info_report_query = {"status": "not_recorded", "detail": str(exc)}

    # PRIVATE DISCORD CORRESPONDENCE V0: project this turn's own explicit
    # typed send request, strictly after canonical persistence actually
    # succeeded (native_result exists above) -- exactly mirrors the
    # external-info dispatch block immediately above. dispatch_outbound()
    # performs the whole fail-closed side-effect law: the outward act and
    # an 'attempted' receipt are committed BEFORE the one and only network
    # POST, and a definitive/ambiguous failure is NEVER auto-resent (a new
    # attempt needs a fresh explicit Clark act on a later turn). A
    # transport failure never turns an already-committed successful
    # waking turn into a failed one. With no bot token configured the
    # transport returns not_configured without any network call.
    if pending_discord_correspondence_request != DISCORD_CORRESPONDENCE_REQUEST_NONE:
        try:
            discord_correspondence_report = discord_correspondence.dispatch_outbound(
                PROVENANCE_DB_DIR,
                waking_turn_event_id=native_result["event_id"],
            )
        except Exception as exc:
            discord_correspondence_report = {"status": "not_recorded", "detail": str(exc)}

    # WSP2-P5-P1 (spec section 6/11): Clark's own typed background-
    # activity control choice, durably recorded now -- strictly AFTER
    # canonical persistence has actually succeeded (native_result
    # exists above), never before, so a failed/contained turn (which
    # raises earlier and never reaches this point) can never
    # accidentally resume background activity. This function does not
    # itself decide legality or apply the resume in memory -- it has no
    # capability to reach workspace_roaming.py at all (see this file's
    # own workspace-noninterference boundary) -- it only durably
    # records that Clark selected it at this specific, now-canonical
    # waking turn. The caller (llama_gui.py's respond()) applies it,
    # gated on the returned flag below, exactly like WSP2-P5's own
    # release_wait_for_human_and_maybe_resume() hook is gated on
    # success here. Best-effort: a write failure here must never turn
    # an otherwise-successful waking turn into a failed one -- it
    # simply leaves the flag False, so the caller correctly does NOT
    # apply an ungrounded resume (spec section 11: typed control +
    # successful canonical event + durable evidence stay linked).
    background_activity_resume_recorded = False
    if pending_background_activity_request == BACKGROUND_ACTIVITY_REQUEST_RESUME_OWN_PAUSE:
        try:
            workspace_episode_provenance.record_background_lifecycle_control(
                PROVENANCE_DB_DIR, actor_id=derive_stable_id("actor", "clark"),
                pipeline_key=PIPELINE_KEY, occurred_at=occurred_at,
                control=workspace_episode_provenance.BACKGROUND_CONTROL_RESUMED,
            )
            background_activity_resume_recorded = True
        except Exception:
            background_activity_resume_recorded = False

    # SLP2: Clark's own typed Sleep-timing choice for this turn,
    # durably recorded now -- strictly AFTER canonical persistence has
    # actually succeeded (native_result exists above), never before,
    # mirroring pending_background_activity_request's own ordering
    # immediately above. Never invoked from any other path: this is
    # the ONLY call site in the entire waking pipeline that can create,
    # knock on, or withdraw a Sleep request -- there is no timer, no
    # idle check, and no free-form-prose inference anywhere near it.
    # A KNOCK or WITHDRAW always resolves to whichever request is
    # CURRENTLY the sole unresolved request for Clark's own actor
    # (sleep_timing_knock.fetch_open_request_for_actor) -- Clark's
    # typed choice never carries or invents a request_id itself, which
    # would require him to fabricate/remember an opaque identifier;
    # the host resolves it mechanically from the "at most one
    # unresolved request" invariant sleep_timing_knock.py itself
    # enforces. A rejection (no unresolved request to knock/withdraw,
    # or a duplicate REQUEST while one is already unresolved) or any
    # other failure here is caught, reported truthfully in the
    # returned status below, and NEVER turns an otherwise-successful
    # waking turn into a failed one and NEVER silently claims success
    # it did not achieve.
    sleep_timing_action_result = {"action": pending_sleep_timing_request, "status": "not_applicable"}
    if pending_sleep_timing_request == SLEEP_TIMING_REQUEST_NONE and conv_trace_pending is not None:
        # Clark's own typed choice, made after his reply committed (see SLEEP_DECISION_SCHEMA).
        sleep_timing_action_result = dispatch_clark_sleep_decision(
            human_message=prompt, clark_reply=clark_prose, session_id=session_id,
            session_started_at=session_started_at, occurred_at=occurred_at,
            human_input_event_id=human_input_event_id,
        )
    if pending_sleep_timing_request != SLEEP_TIMING_REQUEST_NONE:
        try:
            if pending_sleep_timing_request == SLEEP_TIMING_REQUEST_REQUEST_SLEEP:
                created = sleep_timing_knock.record_sleep_request(
                    PROVENANCE_DB_DIR, event_id=sleep_timing_knock.generate_sleep_event_id(),
                    session_id=session_id, session_started_at=session_started_at, occurred_at=occurred_at,
                    triggering_human_event_id=human_input_event_id,
                )
                sleep_timing_action_result = {
                    "action": pending_sleep_timing_request, "status": "recorded",
                    "request_id": created["request_id"],
                }
            else:
                open_request_id = sleep_timing_knock.fetch_open_request_for_actor(PROVENANCE_DB_DIR)
                if open_request_id is None:
                    sleep_timing_action_result = {
                        "action": pending_sleep_timing_request, "status": "rejected",
                        "detail": "no unresolved Sleep request exists",
                    }
                elif pending_sleep_timing_request == SLEEP_TIMING_REQUEST_KNOCK:
                    sleep_timing_knock.record_sleep_knock(
                        PROVENANCE_DB_DIR, event_id=sleep_timing_knock.generate_sleep_event_id(),
                        request_id=open_request_id, session_id=session_id, occurred_at=occurred_at,
                    )
                    sleep_timing_action_result = {
                        "action": pending_sleep_timing_request, "status": "recorded", "request_id": open_request_id,
                    }
                else:
                    sleep_timing_knock.record_sleep_withdrawal(
                        PROVENANCE_DB_DIR, event_id=sleep_timing_knock.generate_sleep_event_id(),
                        request_id=open_request_id, session_id=session_id, occurred_at=occurred_at,
                    )
                    sleep_timing_action_result = {
                        "action": pending_sleep_timing_request, "status": "recorded", "request_id": open_request_id,
                    }
        except sleep_timing_knock.RequestRejected as exc:
            sleep_timing_action_result = {
                "action": pending_sleep_timing_request, "status": "rejected", "detail": str(exc),
            }
        except Exception as exc:
            sleep_timing_action_result = {
                "action": pending_sleep_timing_request, "status": "error", "detail": str(exc),
            }

    # OD1: Clark's own typed operative-directive choice for this turn,
    # durably recorded now -- strictly AFTER canonical persistence has
    # actually succeeded (native_result exists above), never before,
    # mirroring pending_sleep_timing_request's own ordering immediately
    # above. This is the ONLY call site in the entire waking pipeline
    # that can activate, replace, or withdraw an operative directive --
    # there is no other trigger, no idle check, and no free-form-prose
    # inference anywhere near it. set_directive resolves mechanically to
    # ACTIVATE or REPLACE by reading the CURRENT derived state fresh
    # inside this same call (Clark never carries or invents his own
    # "is one already active" tracking, exactly mirroring how a Sleep
    # KNOCK/WITHDRAW never requires Clark to carry a request_id
    # himself). triggering_waking_turn_event_id binds this transition to
    # THIS turn's own just-committed canonical waking_turn event --
    # od1_schema_migration.py's own insert trigger enforces that this
    # reference is real and not canonically later than the transition
    # itself. A rejection (no active directive to replace/withdraw, or
    # one already active to activate) or any other failure here is
    # caught, reported truthfully in the returned status below, and
    # NEVER turns an otherwise-successful waking turn into a failed one.
    operative_directive_action_result = {"action": pending_operative_directive_request, "status": "not_applicable"}
    if pending_operative_directive_request != OPERATIVE_DIRECTIVE_REQUEST_NONE:
        try:
            current = operative_directive.fetch_active_directive(PROVENANCE_DB_DIR)
            visible_current = operative_directive.fetch_active_directive_for_viewer(
                PROVENANCE_DB_DIR,
                family_feature_active=_family_feature_active,
                family_view=_family_view,
            )
            # A directive adopted in another principal's private scope is
            # not merely absent from this prompt: this turn must not be
            # able to replace/withdraw that hidden occurrence or learn its
            # text/state through the action result. Clark remains one
            # actor with one directive, so the lawful fail-closed answer is
            # a generic scope rejection rather than inventing per-principal
            # directive state.
            if not _operative_directive_action_available(
                _family_feature_active, current, visible_current,
            ):
                operative_directive_action_result = {
                    "action": pending_operative_directive_request,
                    "status": "rejected",
                    "detail": "operative directive action is unavailable in this visibility context",
                }
            elif (
                pending_operative_directive_request == OPERATIVE_DIRECTIVE_REQUEST_SET
                and current is not None
                and current.get("directive_text") == pending_operative_directive_text
            ):
                # Setting the words that are ALREADY the active directive changes
                # nothing: repetition and elapsed time never strengthen or
                # re-date it (a real model was observed re-emitting its own
                # set_directive on every ordinary turn), so no revision is
                # written and the result says so.
                operative_directive_action_result = {
                    "action": pending_operative_directive_request, "status": "unchanged",
                    "kind": "replace", "active_event_id": current.get("active_event_id"),
                    "detail": "the requested text is already the active directive; nothing was changed",
                }
            elif pending_operative_directive_request == OPERATIVE_DIRECTIVE_REQUEST_SET:
                writer = (
                    operative_directive.record_operative_directive_replacement if current is not None
                    else operative_directive.record_operative_directive_activation
                )
                result = writer(
                    PROVENANCE_DB_DIR, event_id=operative_directive.generate_directive_event_id(),
                    session_id=session_id, session_started_at=session_started_at, occurred_at=occurred_at,
                    directive_text=pending_operative_directive_text,
                    triggering_waking_turn_event_id=native_result["event_id"],
                )
                operative_directive_action_result = {
                    "action": pending_operative_directive_request, "status": "recorded",
                    "kind": "replace" if current is not None else "activate",
                    "active_event_id": result["active_event_id"],
                }
            else:
                if current is None:
                    operative_directive_action_result = {
                        "action": pending_operative_directive_request, "status": "rejected",
                        "detail": "no active operative directive exists to withdraw",
                    }
                else:
                    operative_directive.record_operative_directive_withdrawal(
                        PROVENANCE_DB_DIR, event_id=operative_directive.generate_directive_event_id(),
                        session_id=session_id, session_started_at=session_started_at, occurred_at=occurred_at,
                        triggering_waking_turn_event_id=native_result["event_id"],
                    )
                    operative_directive_action_result = {
                        "action": pending_operative_directive_request, "status": "recorded",
                    }
        except operative_directive.DirectiveActionRejected as exc:
            operative_directive_action_result = {
                "action": pending_operative_directive_request, "status": "rejected", "detail": str(exc),
            }
        except Exception as exc:
            operative_directive_action_result = {
                "action": pending_operative_directive_request, "status": "error", "detail": str(exc),
            }

    # OWC6-O1: only reached for a successfully released conversation-
    # mode turn (conv_trace_pending stays None in task mode and on any
    # failed-closed conversation-mode turn, which already wrote their
    # own trace record above). native_result["event_id"]/["staging_id"]
    # are the existing persistence helper's own return values -- no
    # interface change to native_provenance_writer.py was needed.
    if conv_trace_pending is not None:
        conversation_direction_trace.record_trace(
            session_id=session_id, event_id=native_result["event_id"], staging_id=native_result["staging_id"],
            trace_path=CONVERSATION_DIRECTION_TRACE_PATH,
            **conv_trace_pending,
        )

    # Q-05 (human-adopted, canonical-before-artifact ordering): the
    # durable journal/artifact Markdown file may only become durable
    # AFTER canonical provenance has committed -- never before. This is
    # the FIRST post-commit write, strictly before the ordered legacy
    # projection below. A no-op (returns artifact_result unchanged) for
    # every non-POSITIVE-success case, since only prepare_artifact_decision()
    # returning a "_pending_write" (validation passed) has anything to
    # finalize. finalize_artifact_write() itself stays behaviorally
    # identical to the pre-Q-05 process_artifact_decision() on a write
    # failure (a graceful "failed" result, never a raise) -- it is
    # shared with that function's own, unrelated synchronous callers,
    # which have no canonical-commit ordering of their own and must see
    # no behavior change at all. Here specifically, though, canonical
    # provenance has already committed and this turn's bounded-clause
    # reply has already been generated on the assumption of a pending
    # write's success -- a write that fails at this point is a
    # materially different, louder situation than an ordinary pre-
    # commit construction rejection, so THIS call site (not the shared
    # function) is what raises on it.
    had_pending_artifact_write = "_pending_write" in artifact_result
    prepared_artifact_result = artifact_result
    artifact_result = finalize_artifact_write(
        artifact_result, event_id=native_result["event_id"], pipeline_id=native_result.get("pipeline_id"))
    _record_artifact_decision(
        human_input_event_id=human_input_event_id, waking_turn_event_id=native_result["event_id"],
        signal=signal_category, prepared=prepared_artifact_result, final=artifact_result,
        turn_path="ordinary")
    if had_pending_artifact_write and not artifact_result.get("artifact_created"):
        raise RuntimeError(
            f"post-canonical-commit artifact write failed for event {native_result['event_id']}: "
            f"{artifact_result.get('detail', {}).get('reason', '(no reason given)')}. Canonical "
            f"provenance for this turn has already committed and its bounded-clause reply already "
            f"assumed success; this is a structural failure, not an ordinary construction rejection, "
            f"and it is not silently absorbed."
        )

    # Ordered legacy projection, strictly post-commit (and, as of Q-05,
    # strictly post-artifact-write too): (1) turn_generation_log,
    # (2) anaxi_log.jsonl, (3) relational_events -- each carrying the
    # complete §3 additive field set.
    orch.record_turn_generation_controls(
        USER_ID, prepared["controls"], model_revision_id=prose_model_revision_id,
        pipeline_id=native_result["pipeline_id"], event_id=native_result["event_id"],
    )
    log_entry(prompt, reply, prepared["kardia"], native_result=native_result)

    history = RelationalHistory(RELATIONAL_DB_PATH)
    history.record_event(
        user_id=USER_ID,
        substrate="llama",
        external_observation=prompt,
        agent_response=reply,
        model_revision_id=prose_model_revision_id,
        pipeline_id=native_result["pipeline_id"],
        auth_context_id=native_result["auth_context_id"],
        event_id=native_result["event_id"],
    )
    history.close()

    return {
        "reply": reply,
        # LAWFUL NULL: "no_reply" only when Clark's typed choice was to say nothing (reply == "");
        # None for every other turn.  A host fact for display -- never words attributed to Clark.
        "reply_choice": subject_reply_choice,
        "correspondence": correspondence_result,
        "kardia": prepared["kardia"],
        "memory_context": prepared["memory_context"],
        "artifact_result": artifact_result,
        "operation_status": operation_status,
        "native_event_id": native_result["event_id"],
        # Diagnostic-only, purely additive (ui_turn_diagnostics.py) --
        # the already-validated typed act's bare label, never free-form
        # content. None in TASK mode or if conversation mode somehow
        # reached here without conv_trace_pending set (should not
        # happen given the fail-closed raises above, but this field
        # must never fabricate a value it doesn't actually have).
        "_diagnostic_typed_act": conv_trace_pending["validated_act"] if conv_trace_pending is not None else None,
        # WSP2-P5-P1: True only when this turn's typed resume_own_pause
        # choice was durably recorded (native_result already committed
        # AND the durable write itself succeeded). False for every
        # other turn, including task mode and every failed-closed
        # conversation-mode turn. The caller (llama_gui.py) is the only
        # place authorized to act on this -- see its own respond().
        "background_activity_resume_recorded": background_activity_resume_recorded,
        # SLP2: truthful mechanical outcome of this turn's typed Sleep-
        # timing choice -- {"action": "none", "status": "not_applicable"}
        # for every turn that made no such choice (including task mode
        # and every failed-closed conversation-mode turn); otherwise
        # {"action": ..., "status": "recorded"|"rejected"|"error", ...}.
        # Never implies Sleep was authorized or ran -- see
        # sleep_timing_knock.py for the full lifecycle.
        "sleep_timing_action_result": sleep_timing_action_result,
        # BOUNDARY INSPECTOR v1: truthful mechanical outcome fields.
        # "query" is None for every turn that made no boundary inquiry
        # (including task mode and every failed-closed conversation-mode
        # turn), else {"status": "recorded", ...} or
        # {"status": "not_recorded", "detail": ...}. "delivered" is None
        # for every turn that delivered no pending boundary result
        # through its real Pass-2 composition, else {"status": "recorded"
        # or "not_recorded", ...}. Never implies a result is Clark's own
        # conclusion -- the rendered report is always host-framed.
        "boundary_inspection_result": {
            "query": boundary_inspection_report_query,
            "delivered": boundary_inspection_report_delivered,
        },
        # OD1: truthful mechanical outcome of this turn's typed
        # operative-directive choice -- {"action": "none", "status":
        # "not_applicable"} for every turn that made no such choice
        # (including task mode and every failed-closed conversation-mode
        # turn); otherwise {"action": ..., "status":
        # "recorded"|"rejected"|"error", ...}. Never implies the
        # directive influenced any particular reply -- see
        # operative_directive.py for the full lifecycle.
        "operative_directive_action_result": operative_directive_action_result,
        # READ-ONLY EXTERNAL INFORMATION CAPABILITY V0: truthful
        # mechanical outcome fields, exactly mirroring
        # boundary_inspection_result's own shape/discipline above.
        # "query" is None for every turn that made no search/fetch
        # request; "delivered" is None for every turn that delivered no
        # pending result through its real Pass-2 composition. Never
        # implies retrieved material is true, trusted, or Clark's own
        # knowledge -- the rendered report is always host-framed.
        "external_info_result": {
            "query": external_info_report_query,
            "delivered": external_info_report_delivered,
        },
        # PRIVATE DISCORD CORRESPONDENCE V0: truthful mechanical outcome
        # fields, exactly mirroring external_info_result's own shape/
        # discipline above. "correspondence" is None for every turn that
        # made no explicit send request, else the classified dispatch
        # status dict (CONFIRMED_SENT / OUTCOME_NOT_ESTABLISHED /
        # FAILED_BEFORE_DISPATCH / NOT_AUTHORIZED / ...). "inbound_delivered"
        # is None for every turn that delivered no pending received
        # message through its real Pass-2 composition. Received content is
        # always untrusted external data -- never Clark's own knowledge.
        "discord_correspondence": {
            "outbound": discord_correspondence_report,
            "inbound_delivered": discord_inbound_report_delivered,
            "caret_reply": caret_reply_report,
        },
    }


def resolve_launch_mode(argv):
    """OWC6-G1: explicit host/operator launch-mode resolution from
    process argv -- shared by every PERSISTENT-process entry point
    (llama_gui.py, llama_desktop.py) so mode resolution is defined
    exactly once rather than duplicated across files (spec section 7:
    "do not duplicate two independent mode parsers"). Never infers
    from prose -- reads only a leading --conversation flag, nothing
    else. main()'s own CLI parsing below is deliberately left
    untouched by this function rather than refactored to call it --
    this is additive GUI/desktop wiring, not a CLI change (spec
    section 13), and main() has its own, already-correct, already-
    tested one-shot semantics.

    Returns (mode, remaining_argv). remaining_argv is empty for a
    correctly-formed GUI/desktop launch (which takes no other CLI
    arguments at all); callers use a non-empty remainder to reject an
    unrecognized argument cleanly (spec section 6) rather than
    silently ignoring or misinterpreting it."""
    args = list(argv)
    if args and args[0] == "--conversation":
        return CONVERSATION_MODE, args[1:]
    return TASK_MODE, args



def run_caret_occasion_turn(pending, orch_factory=None):
    """Run ONE waking occasion for one already-staged, authorized inbound Caret
    message, without any Mac-originated turn.  Never waits: returns
    ``{"status": "busy"}`` when a waking turn is in progress (the message stays
    durably staged), ``"not_pending"`` when the message is no longer lawfully
    pending (revoked, already delivered), otherwise ``"delivered"`` with the
    turn's result.  Exceptions propagate; the message then stays undelivered."""
    if not waking_pipeline_lock.LOCK.acquire(blocking=False):
        return {"status": "busy"}
    try:
        still_pending = [
            p for p in discord_correspondence.next_pending_inbound(
                PROVENANCE_DB_DIR, limit=discord_correspondence.MAX_PENDING_INBOUND_LIMIT)
            if p["event_id"] == pending["event_id"]
        ]
        if not still_pending:
            return {"status": "not_pending"}
        orch = orch_factory() if orch_factory is not None else AnaxiOrchestrator(DB_PATH)
        try:
            result = run_waking_turn.__wrapped__(
                orch, "", interaction_mode=CONVERSATION_MODE, caret_occasion=still_pending[0],
            )
        finally:
            try:
                orch.close()
            except Exception:
                pass
        return {"status": "delivered", "result": result}
    finally:
        waking_pipeline_lock.LOCK.release()


def main():
    # OWC3-S1: --conversation is the narrowest explicit operator
    # surface consistent with this file's existing sys.argv-based CLI --
    # no new config subsystem. Absent, interaction_mode stays None,
    # which run_waking_turn()/​_resolve_interaction_mode() resolves to
    # TASK (today's unchanged default) unless a prior call in this same
    # process already set it explicitly.
    args = sys.argv[1:]
    explicit_mode = None
    if args and args[0] == "--conversation":
        explicit_mode = CONVERSATION_MODE
        args = args[1:]
    prompt = " ".join(args) or "Introduce yourself, Kardia and all."
    orch = AnaxiOrchestrator(DB_PATH)

    result = run_waking_turn(orch, prompt, interaction_mode=explicit_mode)
    print(f"[kardia] {result['kardia']}")
    if result["memory_context"]:
        print(f"[memory] {result['memory_context']}")
    print(f"\n[llama] {result['reply']}")
    print(f"\n[artifact] {result['artifact_result']['pass2_context']}")

    orch.close()


if __name__ == "__main__":
    main()
