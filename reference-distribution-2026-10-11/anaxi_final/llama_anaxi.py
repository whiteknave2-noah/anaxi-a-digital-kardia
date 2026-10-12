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

import contextvars
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
import action_verification
import epistemic_status
import time
import uuid
import witness_gate
import waking_pipeline_lock
import retrieval_delivery
import provider_boundary_receipt
import context_budget
import family_membership
import canonical_person
import hippocampus_retrieval
import historical_unit_reconstruction
import historical_continuity_access
import relational_history_projection
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
from session_dialogue_window import (
    build_authenticated_dialogue_window, dialogue_contribution, dialogue_messages,
    dialogue_consolidation_state, dialogue_target_event_ids, target_event_ids,
)
from session_dialogue_window import build_correspondent_dialogue_window, build_occasion_dialogue_window
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
    CONSOLIDATION_REQUEST_NONE,
    EXTERNAL_INFO_REQUEST_NONE,
    ACTION_VERIFICATION_REQUEST_NONE,
    DISCORD_CORRESPONDENCE_REQUEST_NONE,
    DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE,
    DISCORD_CORRESPONDENCE_REQUEST_DRAFT_MESSAGE,
    USE_WORKSPACE,
    VERIFY_ACTION,
    SEARCH_HISTORICAL_CONTINUITY,
    READ_HISTORICAL_ITEM,
    BROWSE_HISTORICAL_CONTINUITY,
    WAYMARK,
    WAYMARK_AFFORDANCE_TEXT,
    HISTORICAL_BROWSE_AFFORDANCE_TEXT,
    render_pass1_action_menu,
    render_pass1_action_menu_parts,
    pass1_schema_with_destination_choices,
    pass1_schema_with_action_verification,
    pass1_schema_with_waymark,
    pass1_schema_with_historical_continuity,
    pass1_control_schema,
    pass1_control_max_bytes,
    pass1_binding_fields,
    pass1_selection_grammars,
    pass1_selection_schema,
    pass1_payload_schema,
    pass1_payload_max_output_tokens,
    pass1_payload_boundary_failure,
    payload_echo_texts,
    payload_echo_max_texts,
    WORKSPACE_CLAIM_ITEM_FIELD,
    drop_failed_payload_requests,
    render_pass1_binding_request,
    render_pass1_payload_correction_request,
    render_waymark_correction_request,
    WAYMARK_CORRECTION_REASON_BYTES,
    PASS1_BINDING_REQUEST_MARK,
    render_waymark_read_request,
    waymark_read_payload_schema,
    pass1_schema_waymark_reads_exhausted,
    pass1_schema_waymark_reads_offered,
    WAYMARK_READ_REQUESTS,
    MAX_WAYMARK_READS_PER_OCCASION,
    PASS1_PAYLOAD_FIELD_MAX_CHARS,
    PASS1_SELECTION_FIELDS,
    MAX_HISTORICAL_CURSOR_LENGTH,
    MAX_READ_ONLY_CHECKS_PER_OCCASION,
    pass1_schema_checks_exhausted,
    pass1_schema_history_exhausted,
    pass1_schema_receipt_reentry,
    render_occasion_check_state,
    pass1_schema_with_explicit_reply_choice,
    pass1_schema_with_workspace_exclusivity,
    pass1_schema_with_workspace_split,
    translate_candidate_workspace_act,
    pass1_schema_with_receipt_acknowledgement,
    take_receipt_acknowledgement,
    pass1_schema_with_workspace_claim,
    take_workspace_claim,
    WORKSPACE_MODE_INSPECT,
    WORKSPACE_MODE_MUTATE,
    translate_explicit_reply_choice,
    occasion_new_act_fields,
    OCCASION_CHECK_BOUND_REACHED,
    RECEIPT_REENTRY_ACT_NOT_AVAILABLE,
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
import waymark
import external_information
import discord_correspondence
import discord_author_mapping
import correspondence_surfaces
import consolidation
from actor_legibility import CLARK_ACTOR_ID

PIPELINE_KEY = PIPELINE_STRUCTURE["llama"]["pipeline_key"]
# Trace value for a turn on which Clark chose no_reply: Pass 2 was never composed (not a failure).
PASS2_STATUS_NOT_COMPOSED_NO_REPLY = "not_composed_no_reply"

# PULSE V0 CANDIDATE PROTOCOL MECHANICS (default off: the production waking path stays byte-identical).
# When on, three substrate-facing protocol mechanics change and nothing semantic does:
#   * the reply choice is an explicit, REQUIRED grammar field (reply | no_reply), translated to the
#     production convention before validation (conversation_direction.translate_explicit_reply_choice);
#   * use_workspace is its own grammar branch carrying only the fields the validator lets it carry
#     (conversation_direction.pass1_schema_with_workspace_exclusivity);
#   * the nested workspace action is a generation-time grammar (workspace_direction.
#     pass1_workspace_action_schema): only offered (resource class, action) pairs can be produced.
# The Llama + Kardia + Pulse candidate runs with it on; adopting it as the default is the owner's decision.
#
# Owner-adopted (2026-10-02, LLAMA + KARDIA + PULSE PRODUCTION ADOPTION V0): ON in production, paired with
# the adopted Llama substrate below.  With it on, the ordinary waking path also carries the accepted
# candidate architecture that is keyed to this switch: inspect_workspace / mutate_workspace, the Witness
# Gate before workspace mutations and outward sends, canonical receipt acknowledgement, the non-human
# occasion envelope, the speech-boundary workspace witness (declared only after `reply` is chosen) and
# actor legibility.  The name is kept so the accepted candidate bytes stay as evaluated.  It is a pair
# with MODEL: rollback to the Gemma substrate sets it back to False (the off path is the prior production
# path, byte-identical -- see test_witness_gate_v0 / test_pulse_v0).
CANDIDATE_PROTOCOL_MECHANICS = True

# PULSE V0: the two kinds of host-supplied occasion run_waking_turn accepts (see pulse.py).
PULSE_OCCASION_TIMING = "pulse"
PULSE_OCCASION_ACTION_RECEIPT = "action_receipt"


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
#
# Owner-adopted substrate switch (2026-09-25): waking generation now runs
# on Ministral 3 14B (Stage-1 f5c3556, Stage-2 64b7049). A changed
# inference substrate, not a new subject: canonical history, identity,
# directive, Sleep, Kardia and every authority surface are untouched, and
# events written from here on simply name this tag in their existing
# model participation. The incumbent Gemma weights stay installed as the
# rollback target; rollback is `MODEL = WAKING_ROLLBACK_MODEL` and changes
# future inference only (its calibrated fixed costs below match again).
#
# Owner-ordered substrate rollback (2026-09-26): the frozen grounding
# discriminator (experiment/grounding-substrate-discriminator-v0, d3150cf)
# established a false-continuity defect on Ministral 3 14B, so waking
# generation returns to the pinned rollback weights. Substrate only:
# canonical history (Ministral-era events keep their tag and digest),
# directive, workspace and every later repair are untouched.
#
# A tag is a locator, not an identity: each qualified tag is pinned to the
# exact Ollama manifest digest it was qualified/served as, and
# verify_waking_substrate() refuses to start a production waking process
# whose local tag resolves to anything else.
#
# Owner-adopted substrate switch (2026-10-02, LLAMA + KARDIA + PULSE PRODUCTION
# ADOPTION V0): waking generation runs on the stock Llama 3.1 8B Instruct
# Q4_K_M weights that served the accepted Llama + Kardia + Pulse candidate
# (build/llama-kardia-pulse-v0 @ ccf9e66, kardia_pulse_v0/REPORT.md):
# Ollama manifest 46e0c10c...666e; GGUF blob 667b0c19...6a29 (4,920,738,944
# bytes), template 948af274...cf85, params 56bb8bd4...4dcb, config
# 455f3472...ef9a; no adapter.  It is adopted together with
# CANDIDATE_PROTOCOL_MECHANICS = True.  Substrate only: canonical history,
# identity, Kardia, Sleep (llama_sleep.py keeps its own model), permissions,
# workspace, correspondence, directives and recovery are untouched; earlier
# events keep their own tag and digest.  Rollback to the prior production
# substrate: `MODEL = WAKING_ROLLBACK_MODEL` and
# `CANDIDATE_PROTOCOL_MECHANICS = False` (the Gemma calibrations then match
# again; under Llama they fall back to live admission probes).
LLAMA_KARDIA_PULSE_MODEL = "llama3.1:8b-instruct-q4_K_M"
# Owner-adopted substrate switch (2026-10-05, MINISTRAL WHOLE-PACKAGE
# COMPLETION + PRODUCTION INTEGRATION V0): with the host package complete
# (subject-directed consolidation reachable through the ordinary waking
# path) and stock Ministral qualified through the assembled production
# system, waking generation runs on the stock Ministral 3 14B Instruct
# 2512 Q4_K_M weights.  Substrate only: canonical history, identity,
# Kardia, Sleep (llama_sleep.py keeps its own model), permissions,
# workspace, correspondence, directives and recovery are untouched;
# earlier events keep their own tag and digest.  The Gemma calibrations
# do not match this fingerprint, so the fixed costs fall back to live
# admission probes exactly as they did under Llama.  Rollback to the
# prior production substrate: `MODEL = LLAMA_KARDIA_PULSE_MODEL` (or
# `MODEL = WAKING_ROLLBACK_MODEL` for the preserved Gemma incumbent).
MINISTRAL_WAKING_MODEL = "ministral-3:14b-instruct-2512-q4_K_M"
QUALIFIED_WAKING_SUBSTRATES = {
    MINISTRAL_WAKING_MODEL: "4760c35aeb9d9e9c6174c2492562c0b999e80a222804fd96b1915ab72bbcdcf7",
    "gemma4:e4b": "c6eb396dbd5992bbe3f5cdb947e8bbc0ee413d7c17e2beaae69f5d569cf982eb",
    LLAMA_KARDIA_PULSE_MODEL: "46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e",
}
WAKING_ROLLBACK_MODEL = "gemma4:e4b"
MODEL = MINISTRAL_WAKING_MODEL


class WakingSubstrateMismatchError(RuntimeError):
    """The configured waking tag is unpinned, absent, or resolves to other weights."""


# tag -> exact digest, set only by a successful verify_waking_substrate() in THIS process.
# Carried into each waking event's canonical model revision, so the event itself names the
# exact weights that served it -- durable after MODEL, the pin table or operators change.
_VERIFIED_WAKING_DIGESTS = {}


def verified_waking_model_digest(tag):
    """The pinned digest this process verified for `tag`, when waking is served through Ollama.
    None otherwise (unverified process, another tag such as vision, or another provider): the
    event then records the tag alone, exactly as before -- never an unverified digest."""
    if inference_provider.resolve_provider() != inference_provider.PROVIDER_OLLAMA:
        return None
    return _VERIFIED_WAKING_DIGESTS.get(tag)


def _verified_digest_kwarg(tag):
    digest = verified_waking_model_digest(tag)
    return {"waking_model_digest": digest} if digest is not None else {}


def verify_waking_substrate(ollama_module=None, model=None) -> dict:
    """Fail-closed pre-flight for a production waking process: the local
    tag must resolve to exactly its pinned digest. Read fresh (never the
    calibration cache, which fails soft by design). Returns the verified
    identity, with the Ollama server version when it can be read."""
    client = ollama_module or ollama
    tag = model or MODEL
    _VERIFIED_WAKING_DIGESTS.pop(tag, None)
    expected = QUALIFIED_WAKING_SUBSTRATES.get(tag)
    if expected is None:
        raise WakingSubstrateMismatchError(f"waking model {tag!r} has no pinned manifest digest")
    try:
        installed = {m.model: m.digest for m in client.list().models}
    except Exception as exc:
        raise WakingSubstrateMismatchError(
            f"cannot establish the local weights for {tag!r}: {type(exc).__name__}") from exc
    actual = installed.get(tag)
    if actual != expected:
        raise WakingSubstrateMismatchError(
            f"waking model {tag!r} resolves to {actual!r}, expected pinned digest {expected!r}")
    _VERIFIED_WAKING_DIGESTS[tag] = actual
    server_version = None
    try:
        import urllib.request
        base_url = str(client.Client()._client.base_url).rstrip("/")
        with urllib.request.urlopen(f"{base_url}/api/version", timeout=5) as resp:
            server_version = json.loads(resp.read()).get("version")
    except Exception:
        pass
    return {"provider": "ollama", "model_tag": tag, "model_digest": actual,
            "ollama_server_version": server_version}

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
    #
    # DRAFT-MESSAGE recalibration (2026-09-26, owner capability refinement): discord_correspondence_request
    # gained "draft_message". Same method, live gemma4:e4b (digest c6eb396d...) / Ollama 0.34.0: the retired
    # schema reproduced WITH=708, WITHOUT=15, delta 693 first; the new schema measured 708/15/693 in three
    # trials, zero variance -- unchanged, so the cost stays 693.
    # Retired: "e68b413500f0fdf4c6f4a9805db826c3c57f628570ec4742bd5e60b82c1e9b48"
    #
    # SUBJECT-DIRECTED CONSOLIDATION recalibration (2026-10-05): PASS1_SCHEMA gained the optional
    # consolidation_request enum and its four free-string companion fields. The scaffold text itself
    # (pass1_system_content + PASS1_TASK_INSTRUCTION + closing cue) is UNCHANGED, so the calibrated
    # cost stays 693 -- the schema hash is the only fingerprint field that moved. Re-measured with the
    # identical method (merged system scaffold vs empty system, user "x", format=the new PASS1_SCHEMA,
    # num_predict=1, live gemma4:e4b / Ollama 0.34.0, digest reconfirmed): WITH=708, WITHOUT=15,
    # delta 693 -- unchanged. The retired schema reproduced 708/15/693 first.
    # Retired: "c73b2695ad71d3d4fda7613af9cdabc99f82922fdbea2a3ef17605bcf7245f14"
    "pass1_schema_sha256": "c76e40da9c2958f325cc6306975c0abda7caa2def294c0a01d0fabe4763a1c7a",
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
# DRAFT-MESSAGE schema change (2026-09-26): block unchanged; the retired schema reproduced 300/300/300 first and
# the new PASS1_SCHEMA measured 300/300/300 (full representative Pass-1 system message with vs without the block,
# user "Hello there.") -- the calibrated cost stands.
# SUBJECT-DIRECTED CONSOLIDATION recalibration (2026-10-05): the fixed block gained the consolidation affordance
# line (1187 -> 1555 bytes). Same method (full representative ordinary Pass-1 system message with vs without the
# block, user "Hello there.", format=the new PASS1_SCHEMA, num_predict=1, live gemma4:e4b / Ollama 0.34.0): the
# retired block reproduced 300/300/300 first; the new block measured 300/300/300, zero variance -- the calibrated
# cost stands at 308. Retired block sha: "3a5cd5934021faa3f5ec6be89e8836026784ee4c966a0b973c2a09558d62d36e".
_PASS1_MENU_CALIBRATION = dict(
    _PASS1_SCAFFOLD_CALIBRATION,
    scaffold_sha256="e224ee06bf2ffbe902e3a7a58d5604136e71b115f8f88db07edeb4d3537f4050",
)


def _pass1_menu_calibration():
    """The menu's own calibration record, derived from the CURRENT
    _PASS1_SCAFFOLD_CALIBRATION at call time rather than frozen at import.
    The menu shares every environment field with the scaffold (model digest,
    server version, chat template, schema hash) and differs only in the
    block-text hash; deriving it here keeps the two records consistent when
    a caller deliberately patches the scaffold record (e.g. the OWC9-P3C
    fingerprint tests, which alter one shared environment field to prove the
    check is live)."""
    return dict(
        _PASS1_SCAFFOLD_CALIBRATION,
        scaffold_sha256="e224ee06bf2ffbe902e3a7a58d5604136e71b115f8f88db07edeb4d3537f4050",
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
    relying on Ollama's default: originally measured against the then-current
    model/runtime (gemma4:e4b, Ollama 0.32.15) that an omitted `think`
    silently enables hidden reasoning consuming the large majority of
    decode tokens, while think=False returns a normal, complete,
    non-degraded response. Pass-1 (ask_llama_for_json() below) is a
    separate function and is untouched -- whether reasoning helps or
    hurts governed artifact construction has not been measured.
    The adopted Ministral path keeps the explicit no-hidden-reasoning
    transport contract.

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
    MODEL (the configured waking substrate) exactly as before when omitted. The ONLY
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
        # Ordinary waking admits input with the fixed minimum Pass-2 reserve,
        # then uses at most the finite PASS2_GENERATION_CEILING inside the
        # explicit waking serving window. No caller may request less than the
        # protected reserve or more than that ceiling.
        if (type(generation_reserve) is not int
                or not context_budget.PASS2_GENERATION_RESERVE <= generation_reserve
                <= context_budget.PASS2_GENERATION_CEILING):
            raise ValueError("ordinary waking requires a lawful OWC9 Pass-2 allowance")
        generation_options.update(num_predict=generation_reserve)
    # Every waking call is served on the one waking context (context_budget.WAKING_SERVING_CONTEXT): the
    # runner is never reloaded between waking calls; admission stays on the 4096-derived budgets.
    generation_options["num_ctx"] = context_budget.WAKING_SERVING_CONTEXT
    if runtime_options:
        # A bounded, explicit RUNTIME window for a call whose model reasons hidden regardless of
        # think=False (the vision pass; see context_budget.QWEN_VISION_RUNTIME_*). Admission is
        # unchanged; only the window the generation runs in is set, and only these two keys.
        if set(runtime_options) - {"num_ctx", "num_predict"}:
            raise ValueError("runtime_options accepts only num_ctx and num_predict")
        generation_options.update(runtime_options)
    resolved_provider = inference_provider.resolve_provider(provider)
    boundary_token = provider_boundary_receipt.begin_provider_call(
        messages=ollama_messages,
        model=model or MODEL,
        provider=resolved_provider,
        options=generation_options,
        format_value=structured_schema,
        think=False,
    )
    try:
        if resolved_provider == inference_provider.PROVIDER_OLLAMA:
            ui_turn_diagnostics.note_provider_request(
                model=model or MODEL,
                num_ctx=generation_options.get("num_ctx"),
                num_predict=generation_options.get("num_predict"),
                format_mode="json_schema" if structured_schema is not None else "plain",
            )
            response = ollama.chat(
                model=model or MODEL,
                messages=ollama_messages,
                format=structured_schema,
                think=False,
                options=generation_options,
            )
            ui_turn_diagnostics.note_provider_response(response)
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
        provider_boundary_receipt.complete_provider_call(boundary_token, response)
    except Exception as exc:
        provider_boundary_receipt.fail_provider_call(boundary_token, exc)
        raise
    return response["message"]["content"]


_REAL_TEXT_COST_PROBE_BASELINE = "You are a helpful assistant."
# The window a measurement probe may fill: the ordinary 4096 window, except inside the turn-reserve fallback
# (_compose_with_turn_reserve), whose probes may measure an admitted input on the waking serving context.
_MEASUREMENT_CEILING = contextvars.ContextVar("measurement_ceiling", default=context_budget.CONTEXT_CEILING)


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
    ceiling = _MEASUREMENT_CEILING.get()
    if with_upper + 1 > ceiling:
        return None

    # EXACT LOCAL COUNT FIRST (live acceptance 6, H 01M4HAR7FPN62F1RM2A6RHW81C: 98 of the occasion's model calls,
    # 96.4 s of 230.8 s, were these two probes).  The quantity is unchanged: the provider's own prompt_eval_count
    # of exactly these two requests, subtracted.  On the pinned Ministral substrate it is computed by
    # ministral_prompt_tokens -- the same identity-bound tokenizer/template port that already admits human
    # input before canonical H -- and is independent of ``structured_schema`` (format does not enter the
    # counted prompt; qualified live).  Any other or unestablished identity (model, manifest/tokenizer, chat
    # template, server version, Unicode tables, message shape) returns None there, and the two live probes
    # below run exactly as before: never a guess, never a stale count.
    local = _local_prompt_eval_counts(with_messages, without_messages)
    if local is not None:
        with_count, without_count = local
        if (not 0 < with_count <= with_upper or not 0 < without_count <= without_upper
                or with_count >= ceiling - 1 or without_count >= ceiling - 1 or with_count < without_count):
            return None
        return with_count - without_count

    with_text = ui_turn_diagnostics.timed_model_call(
        "real_text_cost_recovery_probe",
        lambda: ollama.chat(
            model=MODEL,
            messages=with_messages, format=structured_schema, think=False,
            options={"temperature": 0.2, "num_predict": 1, "num_ctx": context_budget.WAKING_SERVING_CONTEXT},
        ),
    )
    without_text = ui_turn_diagnostics.timed_model_call(
        "real_text_cost_recovery_probe",
        lambda: ollama.chat(
            model=MODEL,
            messages=without_messages, format=structured_schema, think=False,
            options={"temperature": 0.2, "num_predict": 1, "num_ctx": context_budget.WAKING_SERVING_CONTEXT},
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
        or with_count >= ceiling - 1
        or without_count >= ceiling - 1
        or with_count < without_count
    ):
        return None
    return with_count - without_count


def _measure_local_text_cost_on_waking_context(text):
    """Exact qualified-tokenizer delta for a large waking contribution.

    Unlike ``_measure_real_text_cost``, this helper has no inference fallback.
    It exists only for text whose conservative probe shape exceeds the legacy
    4,096 measurement window but fits the real 46,080 waking serving context.
    Any substrate/shape mismatch returns None and preserves the old fail-closed
    accounting.
    """
    with_messages = [
        {"role": "system", "content": _REAL_TEXT_COST_PROBE_BASELINE},
        {"role": "user", "content": text},
    ]
    without_messages = [{"role": "system", "content": _REAL_TEXT_COST_PROBE_BASELINE}]
    with_upper = sum(context_budget.estimate_tokens(m["content"]) for m in with_messages) \
        + context_budget.SAFETY_MARGIN
    without_upper = sum(context_budget.estimate_tokens(m["content"]) for m in without_messages) \
        + context_budget.SAFETY_MARGIN
    if with_upper + 1 > context_budget.WAKING_SERVING_CONTEXT:
        return None
    local = _local_prompt_eval_counts(with_messages, without_messages)
    if local is None:
        return None
    with_count, without_count = local
    if (not 0 < with_count <= with_upper or not 0 < without_count <= without_upper
            or with_count >= context_budget.WAKING_SERVING_CONTEXT - 1
            or without_count >= context_budget.WAKING_SERVING_CONTEXT - 1
            or with_count < without_count):
        return None
    return with_count - without_count


def _local_prompt_eval_counts(*requests):
    """The provider's exact prompt_eval_count of each request, computed locally (no model activity), or None
    unless every one is established for the pinned substrate (ministral_prompt_tokens)."""
    import ministral_prompt_tokens
    environment = _live_ollama_environment()
    start = time.monotonic()
    try:
        counts = tuple(ministral_prompt_tokens.prompt_eval_count(MODEL, environment, list(r)) for r in requests)
    except Exception:
        return None
    ui_turn_diagnostics.record_local_token_count((time.monotonic() - start) * 1000.0)
    if any(type(c) is not int for c in counts):
        return None
    return counts


def human_input_ingress_tokens(text):
    """The exact token cost of ``text`` as the current human message -- what the provider's own count of
    [baseline system, user] less [baseline system] reports -- from the pinned local Ministral tokenizer
    (ministral_prompt_tokens: deterministic, no model activity, None unless the substrate identity and
    tokenizer are established).  Used only to decide admission before canonical H."""
    import ministral_prompt_tokens
    environment = _live_ollama_environment()
    base = [{"role": "system", "content": _REAL_TEXT_COST_PROBE_BASELINE}]
    with_text = ministral_prompt_tokens.prompt_eval_count(MODEL, environment, base + [{"role": "user", "content": text}])
    without = ministral_prompt_tokens.prompt_eval_count(MODEL, environment, base)
    if type(with_text) is not int or type(without) is not int:
        return None
    return with_text - without


def _admit_human_input_at_ingress(text):
    """BEFORE canonical H: admit the human input only if the waking path can carry it (at most
    context_budget.HUMAN_INGRESS_CAPACITY tokens).  A byte count that already fits needs no tokenizer
    (each token is >= 1 byte); otherwise the exact local count decides; when neither establishes it, the
    input is not admitted.  Raises ConversationDirectionFailure("ingress", HUMAN_INPUT_EXCEEDS_WAKING_CAPACITY)
    -- no H, no model activity."""
    capacity = context_budget.HUMAN_INGRESS_CAPACITY
    if context_budget.estimate_tokens(text) + context_budget.MEASURED_BOUNDARY_TOKENS <= capacity:
        return
    exact = human_input_ingress_tokens(text)
    if exact is not None and exact <= capacity:
        return
    failure = ConversationDirectionFailure("ingress", context_budget.HUMAN_INPUT_EXCEEDS_WAKING_CAPACITY)
    failure.human_input_tokens = exact
    failure.human_input_capacity = capacity
    raise failure


def admit_human_input_at_ingress(text):
    """Public application-service seam for the exact ordinary admission law."""
    return _admit_human_input_at_ingress(text)


_MEASURED_COST_MEMO = {}


def _measure_real_text_cost_memoized(text, structured_schema=None):
    """``_measure_real_text_cost`` with exact-text memoization. Tokenization is
    deterministic for a given (text, model digest, server, chat template), so
    an identical text never needs a second probe within a process (identity
    and fixed control text repeat every turn). Only successful measurements
    are stored; any failure returns None so callers keep the safe estimator."""
    env = _live_ollama_environment()
    import ministral_prompt_tokens
    key = (
        hashlib.sha256(text.encode("utf-8")).hexdigest(), MODEL, env["model_digest"],
        env["server_version"], env["chat_template_sha256"], ministral_prompt_tokens.TOKENIZER_IDENTITY,
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


def recost_when_shedding(contributions, max_prompt_budget, *, refill_unused_capacity=False,
                         structured_schema=None, skip_kinds=()):
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
    # LIVE PASS-1 BUDGET REGRESSION (2026-10-10): the dynamic mechanical block exceeded the legacy
    # 4,096-byte measurement-probe window but was only 1,025
    # exact local tokens (1,033 with boundary slack) on the pinned Ministral tokenizer.  The former
    # early return labelled that whole hard floor unmeasurable and rejected before Pass 1.  Give only
    # oversized atomic HARD blocks one no-inference local-count chance on the actual waking context.
    # An unqualified substrate still returns None and takes the byte-identical old zero-call exit.
    probe_limit = context_budget.CONTEXT_CEILING - context_budget.SAFETY_MARGIN - 64
    oversized_hard = [
        c for c in contributions
        if c.hard and c.droppable_units is None
        and context_budget.estimate_tokens(c.rendered_text) > probe_limit
    ]
    oversized_hard_ids = {id(c) for c in oversized_hard}
    unrecoverable_hard = sum(
        c.cost for c in contributions
        if c.hard and id(c) not in oversized_hard_ids
        and c.droppable_units is not None and len(c.droppable_units) != 1
    )
    lowered = context_budget.recost_by_measurement(
        oversized_hard, _measure_local_text_cost_on_waking_context,
        skip_kinds=skip_kinds,
    )
    if unrecoverable_hard + sum(c.cost for c in oversized_hard) > max_prompt_budget:
        return lowered

    # A single retained H/X unit is not atomic provider text: its messages keep
    # their roles, and canonical target/consolidation evidence is appended to
    # system context.  Measure that real shape before generic atomic re-costing.
    # The live unit is 1,489 tokens in this representation but 1,477 when
    # incorrectly flattened into one user string, so the distinction is safety-
    # relevant at the boundary.  Multi-unit families retain the existing
    # per-subset machinery; the dedicated pressure valve re-measures the final
    # retained unit after deterministic trimming.
    role_costed_dialogue = False
    for contribution in contributions:
        if (contribution.kind != context_budget.RECENT_DIALOGUE
                or contribution.droppable_units is None
                or len(contribution.droppable_units) != 1):
            continue
        measured = _measure_retained_dialogue_cost(
            getattr(contribution, "dialogue_messages", dialogue_messages(contribution)),
            structured_schema,
            additional_system_text=getattr(
                contribution, "dialogue_system_text", dialogue_consolidation_state(contribution)),
            allow_inference=False,
        )
        if type(measured) is int and 0 < measured < contribution.cost:
            contribution.cost = measured
            lowered += 1
            role_costed_dialogue = True

    effective_skip_kinds = tuple(dict.fromkeys(
        tuple(skip_kinds) + ((context_budget.RECENT_DIALOGUE,) if role_costed_dialogue else ())))
    return lowered + context_budget.recost_by_measurement(
        contributions, lambda text: _measure_real_text_cost_memoized(text, structured_schema),
        skip_kinds=effective_skip_kinds,
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
    *, refill_unused_capacity=False,
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
            result = context_budget.compose_within_budget(
                all_contributions, max_prompt_budget,
                refill_unused_capacity=refill_unused_capacity,
            )
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
    # A dialogue contribution may also carry the exact canonical target handles and Clark-authored
    # consolidation evidence rendered into the final system message.  Price that text in the same
    # role-preserving probe instead of measuring only H/X and silently treating the handle block as
    # free.  The full live retained unit measured 1,489 tokens with this block versus 1,431 without it.
    measured = _measure_retained_dialogue_cost(
        dialogue_messages(dialogue), structured_schema,
        additional_system_text=dialogue_consolidation_state(dialogue),
    )
    recovered = result
    if measured is not None and measured < dialogue.cost:
        dialogue.cost = measured
        # Recompose from the authoritative contribution set.  A prior trim
        # pass may have dropped a small same-turn receipt before reaching the
        # oversized pinned dialogue.  Once real measurement makes the intact
        # dialogue fit, ordinary Pass 2 must reconsider that receipt rather
        # than treating the provisional trim as final.
        recovered = context_budget.compose_within_budget(
            all_contributions, max_prompt_budget,
            refill_unused_capacity=refill_unused_capacity,
        )
        recovered.dropped_kinds = list(dict.fromkeys(
            [kind for kind in result.dropped_kinds
             if recovered.included_kind(kind) is None]
            + recovered.dropped_kinds
        ))
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
    # Recompose from every authoritative contribution, not merely the set
    # that survived the failed, dialogue-pinned trial.  Live H
    # 01M4BQH9VJHSCY7WP8NW18X9YQ exposed the difference: Pass 2 first
    # provisionally dropped a 410-unit blocked-fetch receipt, then the
    # dialogue pressure valve removed the 5,252-unit prior pair and returned
    # a 1,073/3,200 prompt without reconsidering the now-fitting receipt.
    # `refill_unused_capacity` restores only mechanically fitting whole
    # contributions under the existing fixed priority law; it never slices,
    # summarizes, or invents evidence.
    fallback = context_budget.compose_within_budget(
        all_contributions, max_prompt_budget,
        refill_unused_capacity=refill_unused_capacity,
    )
    fallback.dropped_kinds = list(dict.fromkeys(
        [kind for kind in recovered.dropped_kinds
         if fallback.included_kind(kind) is None]
        + fallback.dropped_kinds
    ))
    fallback.trimmed_kinds = list(dict.fromkeys(recovered.trimmed_kinds + fallback.trimmed_kinds))
    return fallback


def _measure_retained_dialogue_cost(
        messages, structured_schema=None, additional_system_text="", *, allow_inference=True):
    """Fresh bounded admission probe; generated content is discarded.

    Charge the ENTIRE probe prompt count to the retained dialogue, including
    the neutral system/user boundaries and template overhead. No subtraction,
    cached estimate, assistant-to-user relabeling, or persisted probe output.
    The terminal user boundary matches ordinary waking's generation shape.
    The probe itself must fit using byte accounting before inference starts.
    ``allow_inference=False`` restricts proactive admission to the qualified
    local tokenizer; the established failed-composition recovery keeps its
    bounded inference fallback.
    """
    if not isinstance(additional_system_text, str):
        return None
    probe_system = _REAL_TEXT_COST_PROBE_BASELINE + (
        "\n\n" + additional_system_text if additional_system_text else "")
    probe = ([{"role": "system", "content": probe_system}]
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
    requests = [(probe, upper)] if upper is not None else None

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
    # HISTORY RESPONSE COMPLETION V0 (follow-up turn of a 5,983-char / 1,437-token reply: the next turn's Pass 1
    # and Pass 2 both dropped the newest pair at 1,581/3,913 and 719/3,200 -- room for it existed, but its BYTE
    # upper bound exceeded the 4096 window this probe law was written for, so it was "unmeasurable").  The
    # 4096 window bounds a LIVE inference probe and stays exactly as below.  The pinned local count involves no
    # inference: when the window-bound measurement below cannot be made, the whole unchanged pair is counted
    # locally on the serving window every waking call is served on.  The count only prices the pair; whether it
    # is carried is still decided by the unchanged ordinary admission budget.
    if requests is None and messages and all(
            isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
            for m in messages):
        part_uppers = [admitted([{"role": "system", "content": probe_system}, m]
                                + ([{"role": "user", "content": _REAL_TEXT_COST_PROBE_BASELINE}]
                                   if m["role"] == "assistant" else []))
                       for m in messages]
        serving_upper = (sum(context_budget.estimate_tokens(m["content"]) for m in probe)
                         + context_budget.SAFETY_MARGIN)
        if None in part_uppers and serving_upper + 1 <= context_budget.WAKING_SERVING_CONTEXT:
            local = _local_prompt_eval_counts(probe)
            if local is not None:
                count = local[0]
                return count if 0 < count <= serving_upper and count < context_budget.WAKING_SERVING_CONTEXT - 1 \
                    else None

    probes = []
    for message in (messages if requests is None else ()):
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            return None
        content = message.get("content")
        if not isinstance(content, str):
            return None
        candidate = [
            {"role": "system", "content": probe_system},
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
    for candidate, part_upper in (requests if requests is not None else probes):
        # The pinned substrate's exact count of this very request, computed locally (ministral_prompt_tokens
        # qualifies the alternating dialogue shapes; a replay of 2026-10-10 reached this recovery on an ordinary
        # turn); the live probe only when that identity or shape is not established.
        local = _local_prompt_eval_counts(candidate)
        if local is not None:
            count = valid_count({"done": True, "prompt_eval_count": local[0]}, part_upper)
        elif not allow_inference:
            return None
        else:
            response = ui_turn_diagnostics.timed_model_call(
                "retained_dialogue_cost_recovery_probe",
                lambda: ollama.chat(
                    model=MODEL, messages=candidate, format=structured_schema, think=False,
                    options={"temperature": 0.2, "num_predict": 1, "num_ctx": context_budget.WAKING_SERVING_CONTEXT},
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
    Pass-1 failures on the historical Gemma runtime recorded
    `truncated = 1` at the 4096-token context boundary, with the
    prompt alone consuming ~3664 of those tokens -- leaving only
    ~432 tokens for the entire generation. A required JSON control
    object this small (six fields, few dozen tokens) has no business
    exhausting that budget; unconstrained hidden reasoning -- already
    measured to do exactly this for call_llama() on that model/runtime --
    is the most plausible reason it did. This is a
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
        # A Pass-1-family structured call declares its truthful allowance: the closed grammar's exact
        # maximum (context_budget.pass1_control_allowance for a control act; a selection's own maximum
        # for a bound handle), never the whole serving context.
        if (type(generation_reserve) is not int or generation_reserve <= 0
                or generation_reserve >= context_budget.CONTEXT_CEILING - context_budget.SAFETY_MARGIN):
            raise ValueError("ordinary waking Pass 1 requires a lawful control allowance")
        generation_options.update(num_predict=generation_reserve)
    # The one waking serving context unless a caller's own runtime window says otherwise.
    generation_options.setdefault("num_ctx", context_budget.WAKING_SERVING_CONTEXT)
    if resolved_provider == inference_provider.PROVIDER_OLLAMA:
        ui_turn_diagnostics.note_provider_request(
            model=MODEL,
            num_ctx=generation_options.get("num_ctx"),
            num_predict=generation_options.get("num_predict"),
            format_mode="json_schema" if structured_schema is not None else "json",
        )
        response = ollama.chat(
            model=MODEL,
            messages=to_ollama_format(messages),
            format=resolved_format,
            think=False,
            options=generation_options,
        )
        ui_turn_diagnostics.note_provider_response(response)
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
        try:
            context_budget.require_complete_response(response, generation_reserve)
        except context_budget.IncompleteCompletionError as exc:
            # Carried only so a failed Pass 1 can attach the existing bounded,
            # failure-only diagnostic preview; never parsed or used as an act.
            exc.partial_output = (response.get("message") or {}).get("content")
            raise
    return strip_code_fence(response["message"]["content"].strip())


# ------------------------------------------------ compact Pass-1 control: turn budget + payloads
#
# Post-deploy no-reply incident (H 01M4C66MAAT24RZ7B3CP4AKXF1, 2026-10-07).  See
# conversation_direction.pass1_control_schema: Pass 1 chooses with closed control fields only; each
# free text field the chosen act needs is written by Clark afterwards in its own payload generation.

# The Pass-1 prompt budget of the waking turn in progress (None outside one: the historical budget).
# Set once per turn from the turn's truthful control allowance; every Pass-1-family composition of
# the turn reads it, so the prompt it admits always leaves that allowance free.
_PASS1_TURN_PROMPT_BUDGET = contextvars.ContextVar("pass1_turn_prompt_budget", default=None)


# THE ECHO OF CLARK'S WORDS (whole-occasion capacity, owner decision 2026-10-08).  Once a payload is bound,
# later prompts of the same occasion may show Clark his own words back.  Which ones they can show is fixed
# by the protocol, so the room reserved for them follows the occasion's STATE, never a running sum:
#   query   the latest search receipt is the only one any later prompt displays: the largest query so far
#   checks  each executed ordinary check's own words (verify target, inspected topic label): at most
#           MAX_READ_ONLY_CHECKS_PER_OCCASION of them, settled when the check runs
#   act     the current act's displayed words (topic label, outward words); a check act's outward words are
#           superseded when it re-enters and are never displayed, so settling a check drops them
#   claim   the declared claim item, shown in its witness receipt
# Every later composition of the occasion -- re-entries (and payloads authored beside them), the claim
# declaration, the witness gates, workspace passes, Pass 2 -- admits that reserve beyond its ordinary
# budget (most only when the ordinary budget cannot hold it), on the one waking serving context, which is
# sized for the largest whole-occasion case (whole_occasion_prompt_bound).
_TURN_ECHO = contextvars.ContextVar("turn_echo", default=None)


def _echo_cost(texts):
    return sum(context_budget.estimate_tokens(t) + context_budget.MEASURED_BOUNDARY_TOKENS for t in texts)


def _historical_search_receipt_floor_cost(query):
    """Conservative cost of the zero-candidate receipt for an exact query.

    The query echo is not the whole mandatory re-entry record: its provenance
    envelope, result status, and bounded-action state are hard content too.
    Reserve the complete floor so a byte-dense lawful query does not become
    unusable merely because exact provider token counts are unavailable.
    Candidate records remain prefix-fitted by the re-entry compositor.
    """
    rendered = historical_continuity_access.render_search({
        "schema": "anaxi.historical_continuity.search.v1",
        "query": query,
        # Only the count is rendered. Use the maximum count so a two-digit
        # lexical-term cardinality cannot exceed this conservative floor.
        "query_terms": [None] * hippocampus_retrieval.MAX_QUERY_TERMS,
        "candidates": [],
        "withheld": [],
    })
    states = [
        f"Historical read-only actions used in this occasion: {used} of "
        f"{historical_continuity_access.MAX_HISTORY_ACTIONS_PER_OCCASION}. "
        + (f"{historical_continuity_access.MAX_HISTORY_ACTIONS_PER_OCCASION - used} more may be chosen "
           "in this occasion."
           if used < historical_continuity_access.MAX_HISTORY_ACTIONS_PER_OCCASION
           else "No further historical action is available in this occasion.")
        for used in range(1, historical_continuity_access.MAX_HISTORY_ACTIONS_PER_OCCASION + 1)
    ]
    return (
        context_budget.estimate_tokens(rendered)
        + context_budget.estimate_tokens("\n")
        + max(context_budget.estimate_tokens(state) for state in states)
        + OCCASION_EVIDENCE_JOIN_SLACK
    )


def _turn_echo_reserve():
    state = _TURN_ECHO.get()
    if state is None:
        return 0
    return state["query"] + sum(state["checks"]) + sum(state["act"].values()) + state["claim"]


def _turn_human_reserve():
    """HUMAN_INGRESS_CAPACITY when this turn's human input was admitted at ingress, else 0."""
    state = _TURN_ECHO.get()
    return context_budget.HUMAN_INGRESS_CAPACITY if state is not None and state.get("human_admitted") else 0


def _turn_overflow_reserve():
    """The room a composition of this turn may take above its ordinary budget when the ordinary budget
    cannot hold Clark's echoed words or the admitted human input: both are guaranteed by construction."""
    return _turn_echo_reserve() + _turn_human_reserve()


def _compose_with_turn_reserve(contributions, budget, *, refill_unused_capacity=False):
    """After an ordinary composition (and its own recovery) failed: re-cost the hard content by the
    provider's own count and admit up to the turn's reserve above ``budget``.  None when the turn has no
    reserve (the ordinary failure stands)."""
    reserve = _turn_overflow_reserve()
    if not reserve:
        return None
    # Measured on the waking serving context the call is served on (an admitted input can be larger
    # than the ordinary 4096 measurement window); only here, only after the ordinary composition failed.
    token = _MEASUREMENT_CEILING.set(context_budget.WAKING_SERVING_CONTEXT)
    try:
        context_budget.recost_by_measurement([c for c in contributions if c.hard], _measure_real_text_cost_memoized)
    finally:
        _MEASUREMENT_CEILING.reset(token)
    return context_budget.compose_within_budget(contributions, budget + reserve,
                                                refill_unused_capacity=refill_unused_capacity)


def _begin_act_echo():
    """A new act is being bound: the previous act's displayed words no longer exist (a check act was
    settled when its check ran)."""
    state = _TURN_ECHO.get()
    if state is not None:
        state["act"] = {}


def _reserve_payload_echo(field, value):
    """Add a bound payload's echo to the occasion's state (a no-op outside a turn)."""
    state = _TURN_ECHO.get()
    if state is None:
        return
    cost = _echo_cost(payload_echo_texts(field, value))
    if field == "historical_query":
        # The mandatory host envelope/status surrounding the exact query is
        # part of what the next call must carry.  Reserving only the quoted
        # query left dense scripts stranded when live token measurement was
        # unavailable, even though the capability promised the full ceiling.
        state["query"] = max(state["query"], cost, _historical_search_receipt_floor_cost(value))
    elif field == WORKSPACE_CLAIM_ITEM_FIELD:
        state["claim"] = cost
    else:
        state["act"][field] = cost


def _settle_check_echo():
    """The current act ran as a read-only check: its own words stay displayed in the check's receipt; any
    outward words it carried are superseded by the act the occasion re-enters to."""
    state = _TURN_ECHO.get()
    if state is None:
        return
    state["checks"].append(sum(cost for field, cost in state["act"].items()
                               if field in ("action_verification_target", "thread")))
    state["act"] = {}


def whole_occasion_echo_bounds():
    """(before the final act's payloads, after them): the most echo room any lawful occasion state can
    require -- the worst query, the worst two ordinary checks, the worst final act, the worst claim."""
    worst = {f: _echo_cost(payload_echo_max_texts(f)) for f in PASS1_PAYLOAD_FIELD_MAX_CHARS}
    worst["historical_query"] = max(
        worst["historical_query"],
        *(_historical_search_receipt_floor_cost(unit * PASS1_PAYLOAD_FIELD_MAX_CHARS["historical_query"])
          for unit in ("\x01", "\U0001d518", '"')),
    )
    before = worst["historical_query"] + MAX_READ_ONLY_CHECKS_PER_OCCASION * max(
        worst["action_verification_target"], worst["thread"])
    final_act = worst["thread"] + worst["operative_directive_text"] + worst["discord_message_text"] + worst[
        "external_info_target"]
    return before, before + final_act + worst[WORKSPACE_CLAIM_ITEM_FIELD]


def whole_occasion_prompt_bound():
    """The largest prompt + generation any waking call of a lawful occasion can need: a full-capacity payload
    authored beside a re-entry carrying the worst earlier echo, or a post-binding pass (Pass 2, a witness
    gate, the claim declaration and its item) carrying the worst whole-occasion echo -- each with everything
    a Waymark can lawfully place beside it (whole_occasion_budget_proof)."""
    return whole_occasion_budget_proof()["worst_case"]


def whole_occasion_budget_proof():
    """Exact production-faithful branch accounting for the waking promise.

    Every term is an authoritative token upper bound from the same functions/constants admission and
    composition use; a branch is one model call (prompt + its generation), so the serving context must hold
    the largest branch, and each branch carries EVERYTHING that can lawfully be co-resident in that call:
      * the admitted human input at its full capacity (it may sit above the ordinary budget in every pass);
      * the worst echo of Clark's earlier words the occasion state allows at that point;
      * WAYMARK: the offered menu line's own room in every Pass-1-family call (it is in the control prompt a
        payload is authored beside; the claim declaration and re-entries are composed on the same budget);
        in Pass 2, the Waymark result AND an eligible notice together, each in its own room with its message
        boundary and the non-human occasion envelope.  A Waymark result exists only when the final act is
        waymark, which carries no outward payload, so charging it beside the worst outward echo (``after``)
        over-counts -- never under-counts.  Dormant Waymarks contribute nothing to any branch.
    """
    before, after = whole_occasion_echo_bounds()
    worst_payload = max(pass1_payload_max_output_tokens(f) for f in PASS1_PAYLOAD_FIELD_MAX_CHARS)
    claim_echo = _echo_cost(payload_echo_max_texts(WORKSPACE_CLAIM_ITEM_FIELD))
    margin = context_budget.SAFETY_MARGIN
    request = context_budget.PAYLOAD_REQUEST_MAX_TOKENS
    human = context_budget.HUMAN_INGRESS_CAPACITY
    line = WAYMARK_AFFORDANCE_RESERVE
    browse_line = HISTORY_BROWSE_AFFORDANCE_RESERVE
    pass1 = context_budget.PASS1_MAX_PROMPT_BUDGET + line + browse_line
    pass2_worst_prompt = (context_budget.PASS2_MAX_PROMPT_BUDGET + after + human
                          + context_budget.WAYMARK_QUERY_MAX_TOKENS + context_budget.WAYMARK_NOTICE_MAX_TOKENS)
    branches = {
        "payload_authoring_after_prior_checks": pass1 + before + human + request + worst_payload + margin,
        "waymark_spec_authoring": (pass1 + before + human + context_budget.WAYMARK_SPEC_REQUEST_MAX_TOKENS
                                   + pass1_payload_max_output_tokens("waymark_spec") + margin),
        # The one correction call: the SAME admitted prompt as attempt 1 with the correction request in place
        # of the first request (attempt 1's request and output are not in it; its spec is shown back inside
        # the bounded correction request).  Attempts are separate calls, never co-resident.
        "waymark_spec_correction_authoring": (
            pass1 + before + human + context_budget.WAYMARK_CORRECTION_REQUEST_MAX_TOKENS
            + pass1_payload_max_output_tokens("waymark_spec") + margin),
        # The ordinary Pass-2 completion (History Response Completion V0).  After final composition the FULL
        # prompt is charged against the serving contract and the allowance is
        # min(PASS2_GENERATION_CEILING, WAKING_SERVING_CONTEXT - margin - full_prompt_cost).  At the worst lawful
        # Pass-2 prompt below (pinned equal to context_budget.PASS2_WORST_LAWFUL_PROMPT) that is the guaranteed
        # PASS2_GUARANTEED_GENERATION; the branch, margin included like every other branch, is the serving
        # context itself: one reply, one call, never more than the window holds.
        "pass2_with_waymark_result_and_notice": min(
            pass2_worst_prompt + context_budget.PASS2_GENERATION_CEILING,
            context_budget.WAKING_SERVING_CONTEXT - margin) + margin,
        "pass1_reentry_after_all_echo": pass1 + after + human + context_budget.PASS1_GENERATION_RESERVE + margin,
        "workspace_claim_payload": (pass1 + (after - claim_echo) + human + request
                                    + pass1_payload_max_output_tokens(WORKSPACE_CLAIM_ITEM_FIELD) + margin),
    }
    # NON-HUMAN OCCASIONS (pre-existing, made explicit here).  Inside a Pulse each evidence text a call carries is
    # its own message in the occasion's host envelope, and compositions price the texts without it.  Such an
    # occasion has no human input, so the 2048-token human term every branch charges is unused there; the
    # unpriced envelopes are charged against it, and any excess is added to the branch that carries them.
    #   re-entries: at most the displayed checks -- the ordinary checks, the latest historical check, and the
    #               latest Waymark list and inspect receipts;
    #   claim declaration / witness gates: every check except superseded Waymark receipts.
    envelope = context_budget.WAYMARK_HOST_ENVELOPE_MAX_TOKENS + context_budget.MEASURED_BOUNDARY_TOKENS
    displayed = MAX_READ_ONLY_CHECKS_PER_OCCASION + 1 + 2
    carried = MAX_READ_ONLY_CHECKS_PER_OCCASION + historical_continuity_access.MAX_HISTORY_ACTIONS_PER_OCCASION + 2
    hless = {
        "envelope_per_evidence_message": envelope,
        "reentry_messages": displayed, "reentry_excess": max(0, displayed * envelope - human),
        "claim_or_witness_messages": carried, "claim_or_witness_excess": max(0, carried * envelope - human),
    }
    branches["hless_reentry_with_unpriced_envelopes"] = (
        branches["pass1_reentry_after_all_echo"] - human + displayed * envelope)
    branches["hless_witness_gate_with_unpriced_envelopes"] = (
        branches["pass1_reentry_after_all_echo"] - human + carried * envelope)
    branches["hless_claim_payload_with_unpriced_envelopes"] = (
        branches["workspace_claim_payload"] - human + carried * envelope)
    maximum = max(branches.values())
    return {
        "serving_context": context_budget.WAKING_SERVING_CONTEXT,
        "hless_envelopes": hless,
        "maximum_human_ingress": human,
        "waymark_menu_line_room": line,
        "history_browse_menu_line_room": browse_line,
        "waymark_spec_request": context_budget.WAYMARK_SPEC_REQUEST_MAX_TOKENS,
        "waymark_correction_request": context_budget.WAYMARK_CORRECTION_REQUEST_MAX_TOKENS,
        "waymark_result_raw": waymark.QUERY_RESULT_MAX_TOKENS,
        "waymark_notice_raw": waymark.NOTICE_MAX_TOKENS,
        "waymark_host_envelope": context_budget.WAYMARK_HOST_ENVELOPE_MAX_TOKENS,
        "waymark_result_carriage": context_budget.WAYMARK_QUERY_MAX_TOKENS,
        "waymark_notice_carriage": context_budget.WAYMARK_NOTICE_MAX_TOKENS,
        "safety_margin": margin,
        "pass2_worst_prompt": pass2_worst_prompt,
        "pass2_guaranteed_generation": min(context_budget.PASS2_GENERATION_CEILING,
                                           context_budget.WAKING_SERVING_CONTEXT - margin - pass2_worst_prompt),
        "pass2_generation_ceiling": context_budget.PASS2_GENERATION_CEILING,
        "branches": branches,
        "worst_case": maximum,
        "remaining": context_budget.WAKING_SERVING_CONTEXT - maximum,
        "fits": maximum <= context_budget.WAKING_SERVING_CONTEXT,
    }


def _pass1_prompt_budget():
    budget = _PASS1_TURN_PROMPT_BUDGET.get()
    state = _TURN_ECHO.get()
    human = _turn_human_reserve() if state is not None and state.get("human_extended") else 0
    waymark_line = state.get("waymark_affordance", 0) if state is not None else 0
    history_browse_line = state.get("history_browse_affordance", 0) if state is not None else 0
    return ((context_budget.PASS1_MAX_PROMPT_BUDGET if budget is None else budget) + _turn_echo_reserve() + human
            + waymark_line + history_browse_line)


def _pass2_prompt_budget():
    return context_budget.PASS2_MAX_PROMPT_BUDGET + _turn_echo_reserve()


def _with_pass1_turn_scope(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        token = _PASS1_TURN_PROMPT_BUDGET.set(None)
        echo_token = _TURN_ECHO.set({"query": 0, "checks": [], "act": {}, "claim": 0,
                                     "human_admitted": False, "human_extended": False, "waymark_affordance": 0,
                                     "history_browse_affordance": 0,
                                     "waymark_reads": _empty_waymark_reads()})
        try:
            return fn(*args, **kwargs)
        finally:
            _TURN_ECHO.reset(echo_token)
            _PASS1_TURN_PROMPT_BUDGET.reset(token)
    return wrapper


# The longest canonical receipt status a verified re-entry can echo (action_verification's closed vocabulary).
_LONGEST_RECEIPT_STATUS = max(
    (f"{completion} ({outcome})"
     for completion in (action_verification.COMPLETED, action_verification.NOT_COMPLETED, action_verification.UNKNOWN)
     for outcome in action_verification.OUTCOME_RECORD_TEXT),
    key=lambda text: len(text.encode("utf-8")))


def _pass1_reentry_control_allowance(history_schema_base, history_available, pending_send_refs, *, receipt_echo):
    """The truthful control allowance of a re-entry: its longest control form (historical branches with
    a maximal cursor; a verified receipt echo when the check being answered carries a canonical result)."""
    reentry = history_schema_base
    if history_available:
        reentry = pass1_schema_with_historical_continuity(
            reentry, ["x" * MAX_HISTORICAL_CURSOR_LENGTH],
            browse_cursors=["x" * MAX_HISTORICAL_CURSOR_LENGTH])
    if receipt_echo:
        reentry = pass1_schema_with_receipt_acknowledgement(reentry, _LONGEST_RECEIPT_STATUS)
    return context_budget.pass1_control_allowance(pass1_control_max_bytes(
        pass1_control_schema(reentry, pending_send_refs=pending_send_refs)))


class Pass1PayloadFailure(Exception):
    """A selection or payload of a chosen control act did not complete and validate; the act proceeds
    nowhere."""

    def __init__(self, failure_code, field, partial_output=None, termination=None):
        self.failure_code = failure_code
        self.field = field
        self.partial_output = partial_output
        self.termination = termination        # the provider's own done_reason, or "not_generated"
        super().__init__(f"{field}: {failure_code}")


def ask_llama_for_payload(messages, allowance, provider=None, format=None):
    """One atomic payload call in the field's one-field grammar, bounded to ``allowance`` output tokens,
    on the payload serving context (context_budget.WAKING_SERVING_CONTEXT) -- the only waking call that
    uses it.  Completion is judged by the caller."""
    options = {"temperature": 0.2, "num_predict": allowance, "num_ctx": context_budget.WAKING_SERVING_CONTEXT}
    resolved = inference_provider.resolve_provider(provider)
    if resolved == inference_provider.PROVIDER_OLLAMA:
        ui_turn_diagnostics.note_provider_request(
            model=MODEL, num_ctx=options["num_ctx"], num_predict=options["num_predict"],
            format_mode="json_schema" if isinstance(format, dict) else "json",
        )
        return ollama.chat(model=MODEL, messages=to_ollama_format(messages), format=format, think=False,
                           options=options)
    result = inference_provider.dispatch(resolved, model=MODEL, messages=to_ollama_format(messages),
                                         options=options, format=format)
    if result.status not in {inference_provider.SUCCESS, inference_provider.TRUNCATED}:
        raise inference_provider.ProviderError(result)
    return result.raw


def _binding_request(field, act, occasion_envelope):
    # Its own first line states it is a mechanical host request, not human speech, in every occasion
    # (an occasion envelope would not fit within PAYLOAD_REQUEST_MAX_TOKENS).
    # Bounded for every binding: a selection request fits beside its closed answer in the smallest control
    # allowance, and the bound is the request term of the payload context's worst case.
    if field == "waymark_spec" and _is_waymark_read(act):
        content = render_waymark_read_request(act, **_waymark_read_grammar())
    else:
        content = render_pass1_binding_request(field, act)
    request = {"role": "user", "content": content}
    cost = context_budget.estimate_tokens(request["content"]) + context_budget.MEASURED_BOUNDARY_TOKENS
    # A Waymark spec's request states the chosen request's exact syntax: its own bound, in its own branch of
    # the whole-occasion bound; every other request keeps the generic bound.
    bound = (context_budget.WAYMARK_SPEC_REQUEST_MAX_TOKENS if field == "waymark_spec"
             else context_budget.PAYLOAD_REQUEST_MAX_TOKENS)
    if cost > bound:
        raise Pass1PayloadFailure("PAYLOAD_REQUEST_TOO_LARGE", field)
    return request, cost


def _select_pass1_handle(field, grammar, act, base_messages, base_cost, occasion_envelope):
    """Clark chooses the one handle a chosen request binds, from its closed grammar, beside the same
    admitted prompt.  The selection's allowance is its own exact maximum."""
    request, request_cost = _binding_request(field, act, occasion_envelope)
    schema = pass1_selection_schema(field, grammar)
    allowance = pass1_control_max_bytes(schema)
    # Beside the admitted prompt on the one waking serving context (a re-entry's prompt may carry the
    # occasion's echo of Clark's words above the ordinary budget).
    if base_cost + request_cost + allowance + context_budget.SAFETY_MARGIN > context_budget.WAKING_SERVING_CONTEXT:
        raise Pass1PayloadFailure("PAYLOAD_ALLOWANCE_EXHAUSTED", field)
    try:
        raw = ui_turn_diagnostics.timed_model_call(
            "pass1_selection", ask_llama_for_json, list(base_messages) + [request],
            structured_schema=schema, generation_reserve=allowance)
    except context_budget.IncompleteCompletionError as exc:
        raise Pass1PayloadFailure(exc.failure_code, field, getattr(exc, "partial_output", None)) from None
    try:
        value = json.loads(raw).get(field)
    except (TypeError, ValueError, AttributeError):
        value = None
    if not isinstance(value, str):
        raise Pass1PayloadFailure("SELECTION_NOT_ESTABLISHED", field, raw)
    return value


def _empty_waymark_reads():
    """What Waymark results of THIS occasion have issued: the only continuations and handle specs a later read
    of the occasion may name (the read grammar is closed to them; the host refuses anything else); the exact
    evidence identities its reads delivered; and the host's current read offer (None until a re-entry computes
    one: before any read everything is offered)."""
    return {"list_continuations": set(), "inspect_choices": set(), "delivered_lines": set(),
            "delivered_snapshots": set(), "delivered_projection": {}, "room_refused": set(), "shown_in": {},
            "offer": None}


# READ PROGRESSION (live acceptance 6, H 01M4HAR7FPN62F1RM2A6RHW81C): the same view=live list, on the same
# unchanged shelf, was delivered four times in one occasion; the fourth spent the last Waymark read and the
# grounded inspect it had offered never ran.  A read that would deliver nothing this occasion has not already
# delivered is no longer offered: it has no form in the read grammar (the read act itself goes when no read
# remains), so repetition cannot spend the allowance.  Mechanical identities only:
#   * a list's first page (any view) adds something iff the view's ``snapshot`` -- every entry's exact shelf
#     line in order, independent of the view label and page size -- was never delivered: the shelf changed, or
#     this view shows other entries.  So view=all adds nothing when it shows exactly what view=live showed;
#   * an issued next page adds something iff its snapshot was never delivered or it starts at an exact shelf
#     line this occasion has not delivered (pages are contiguous from their cursor: a re-cut page that would
#     start by re-showing delivered lines adds nothing while the page that continues past them is offered);
#   * an inspect from the start adds something iff that Waymark's current projection (its digest) was never
#     delivered; an issued projection continuation adds something iff it continues the current projection and
#     bytes from its cursor on were not delivered in this occasion;
#   * a continuation of a shelf or projection that has since changed is not offered (it could only be refused;
#     the changed shelf or projection is itself offered again from the start);
#   * a read refused for evidence room is not offered again in the occasion (the room a later check gets only
#     shrinks as evidence accumulates: the same read would meet the same refusal).

def _list_adds_evidence(reads, view, cursor, shelf_digest=None, *, stale=False):
    """Would a list of ``view`` from ``cursor`` (None: the first page) deliver anything new now?  A stale
    continuation answers ``stale`` (not offered; the host's own guard lets its existing refusal speak)."""
    if cursor is not None and shelf_digest is not None and shelf_digest != view["shelf_digest"]:
        return stale
    if view["snapshot"] not in reads["delivered_snapshots"]:
        return True
    following = [line for entry_cursor, line in view["lines"] if cursor is not None and entry_cursor > cursor]
    return bool(following) and following[0] not in reads["delivered_lines"]


def _inspect_adds_evidence(reads, handle, current_digest, cursor, continued_digest=None, *, stale=False):
    if cursor and continued_digest != current_digest:
        return stale
    ranges = reads["delivered_projection"].get((handle, current_digest))
    if ranges is None:
        return True
    if not cursor:
        return False
    total = ranges["total"]
    covered = cursor
    for start, end in sorted(ranges["spans"]):
        if start <= covered < end:
            covered = end
    return covered < total


def _waymark_read_adds_evidence(parsed, session_id):
    reads = _waymark_reads_state()
    if parsed["request"] == waymark.LIST:
        view = waymark.shelf_view(PROVENANCE_DB_DIR, session_id=session_id, include_historical=parsed["view"] == "all")
        return _list_adds_evidence(reads, view, parsed.get("cursor"), parsed.get("shelf_digest"), stale=True)
    current = waymark.projection_identity(PROVENANCE_DB_DIR, session_id=session_id, handle=parsed["handle"])
    return _inspect_adds_evidence(reads, parsed["handle"], current, parsed.get("projection_cursor") or 0,
                                  parsed.get("projection_sha256"), stale=True)


def _waymark_read_offer(session_id, reads_used):
    """The Waymark reads the host offers at this point of the occasion -- mechanically, from canonical state and
    what this occasion delivered; nothing about what Clark wants.  {list_specs, inspect_choices, requests}:
      * nothing once MAX_WAYMARK_READS_PER_OCCASION reads are used;
      * list: view=live / view=all / each issued next page, unless that exact evidence was already delivered
        and is unchanged;
      * inspect: each issued handle/continuation spec on the same rule; before any shelf page, the exact
        handle form (offered as before);
      * the occasion's last read is kept for a grounded inspect while one is offered (no list can take it)."""
    reads = _waymark_reads_state()
    if reads_used >= MAX_WAYMARK_READS_PER_OCCASION:
        return {"list_specs": frozenset(), "inspect_choices": frozenset(), "requests": frozenset()}
    views = {view: waymark.shelf_view(PROVENANCE_DB_DIR, session_id=session_id, include_historical=view == "all")
             for view in ("live", "all")}
    list_specs = {f"view={view}" for view in ("live", "all") if _list_adds_evidence(reads, views[view], None)
                  and f"view={view}" not in reads["room_refused"]}
    for spec in reads["list_continuations"] - reads["room_refused"]:
        try:
            parsed = waymark.parse_request(waymark.LIST, spec)
        except waymark.WaymarkError:
            list_specs.add(spec)
            continue
        if _list_adds_evidence(reads, views[parsed["view"]], parsed.get("cursor"), parsed.get("shelf_digest")):
            list_specs.add(spec)
    projections, inspect_choices = {}, set()
    for spec in reads["inspect_choices"] - reads["room_refused"]:
        try:
            parsed = waymark.parse_request(waymark.INSPECT, spec)
        except waymark.WaymarkError:
            inspect_choices.add(spec)
            continue
        handle = parsed["handle"]
        if handle not in projections:
            projections[handle] = waymark.projection_identity(PROVENANCE_DB_DIR, session_id=session_id, handle=handle)
        if _inspect_adds_evidence(reads, handle, projections[handle], parsed.get("projection_cursor") or 0,
                                  parsed.get("projection_sha256")):
            inspect_choices.add(spec)
    if reads_used == MAX_WAYMARK_READS_PER_OCCASION - 1 and inspect_choices:
        list_specs = set()
    # The current canonical creation facts of each offered handle, from the view that showed it (stated beside the
    # choice in the request that binds it); a continuation is marked as one.
    inspect_facts = {}
    for spec in inspect_choices:
        try:
            parsed = waymark.parse_request(waymark.INSPECT, spec)
        except waymark.WaymarkError:
            continue
        if parsed.get("projection_cursor"):
            inspect_facts[spec] = f"continues this occasion's inspect of {parsed['handle']} from byte {parsed['projection_cursor']}"
            continue
        view = reads["shown_in"].get(parsed["handle"], "all")
        facts = views[view]["creation_facts"].get(parsed["handle"])
        inspect_facts[spec] = (f"{facts} in view={view}" if facts is not None
                               else "not on the current shelf view that showed it")
    requests = ({"list"} if list_specs else set()) | (
        {"inspect"} if inspect_choices or not reads["inspect_choices"] else set())
    return {"list_specs": frozenset(list_specs), "inspect_choices": frozenset(inspect_choices),
            "inspect_facts": inspect_facts, "requests": frozenset(requests)}


def _remember_projection_delivery(reads, handle, digest, start, text):
    """The byte span of the projection ``digest`` an inspect result actually delivered (its own header)."""
    total = int(text.split("total_bytes=", 1)[1].split(" ", 1)[0])
    following = text.split("next_projection_cursor=", 1)[1].split(" ", 1)[0]
    end = total if following == "None" else int(following)
    entry = reads["delivered_projection"].setdefault((handle, digest), {"total": total, "spans": []})
    entry["spans"].append((start, end))


def _waymark_read_grammar():
    """The keyword arguments of this point's read grammar and read request: the host's current offer once a
    re-entry has computed one; before that, every lawful read (nothing has been delivered yet)."""
    reads = _waymark_reads_state()
    offer = reads.get("offer")
    if offer is None:
        return {"list_continuations": reads["list_continuations"], "inspect_choices": reads["inspect_choices"]}
    return {"list_continuations": reads["list_continuations"], "inspect_choices": offer["inspect_choices"],
            "list_specs": offer["list_specs"], "inspect_facts": offer.get("inspect_facts")}


def _waymark_reads_state():
    state = _TURN_ECHO.get()
    return state["waymark_reads"] if state is not None and "waymark_reads" in state else _empty_waymark_reads()


def _is_waymark_read(act):
    return isinstance(act, dict) and act.get("act") == WAYMARK and act.get("waymark_request") in WAYMARK_READ_REQUESTS


def _remember_waymark_binding(act, base_messages, base_cost, occasion_envelope):
    """The exact admitted prompt the chosen Waymark spec was authored beside: its one correction (if any) is
    authored beside the same prompt, so the correction composes exactly like the first attempt."""
    state = _TURN_ECHO.get()
    if state is not None:
        state["waymark_binding"] = {"act": {k: v for k, v in act.items() if k != "waymark_spec"},
                                    "base_messages": list(base_messages), "base_cost": base_cost,
                                    "occasion_envelope": occasion_envelope}


def _waymark_correction_request(act, refused_spec, code, reason):
    if _is_waymark_read(act):
        # A read's correction states the refusal and the same closed choices (the grammar is the same).
        reason_text = str(reason).encode("utf-8")[:WAYMARK_CORRECTION_REASON_BYTES].decode("utf-8", "ignore")
        content = (f"{PASS1_BINDING_REQUEST_MARK} The host REFUSED that Waymark {act.get('waymark_request')} spec "
                   f"(refusal {code}: {reason_text}). This is the one correction this turn allows. "
                   + render_waymark_read_request(act, **_waymark_read_grammar()))
    else:
        content = render_waymark_correction_request(act, refused_spec, code, reason)
    request = {"role": "user", "content": content}
    cost = context_budget.estimate_tokens(request["content"]) + context_budget.MEASURED_BOUNDARY_TOKENS
    if cost > context_budget.WAYMARK_CORRECTION_REQUEST_MAX_TOKENS:
        raise Pass1PayloadFailure("PAYLOAD_REQUEST_TOO_LARGE", "waymark_spec")
    return request, cost


WAYMARK_MAX_SPEC_ATTEMPTS = 2
# WAYMARK ATTEMPT EVIDENCE: every occasion kind (a Pulse or Caret turn is not wrapped by the GUI's per-turn
# diagnostic record) appends its Waymark lane events here, beside the canonical store -- operational and
# host-side like witness_gate_log.jsonl, never canonical history or continuity material.  The GUI's per-turn
# record carries the same events.
#
# The spec is recorded WHOLE, exactly as admitted to validation -- never truncated or only hashed.  Its size
# is bounded by the admission itself: the validator takes any completed string, and the one call that writes
# it (attempt 1 or the correction) is a waymark_spec payload capped at pass1_payload_max_output_tokens
# ("waymark_spec") = 3180 output tokens; the pinned Ministral vocabulary's longest token decodes to 76 bytes,
# so an admitted spec is at most 3180 x 76 = 241,680 UTF-8 bytes (JSON escapes only shorten it).  The
# parser itself refuses anything beyond 512 characters, but that refused attempt is exactly what must be
# recoverable.
WAYMARK_ATTEMPT_LOG_NAME = "waymark_attempt_log.jsonl"


def _record_waymark_event(**facts):
    spec = facts.pop("spec", None)
    if isinstance(spec, str):
        facts.update(spec=spec, spec_chars=len(spec),
                     spec_sha256=hashlib.sha256(spec.encode("utf-8")).hexdigest())
    lane = (_TURN_ECHO.get() or {}).get("waymark_lane") or {}
    facts = dict(lane, **facts)
    ui_turn_diagnostics.record_waymark_event(**facts)
    try:
        with open(os.path.join(PROVENANCE_DB_DIR, WAYMARK_ATTEMPT_LOG_NAME), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(facts, ts=int(time.time())), sort_keys=True, ensure_ascii=False) + "\n")
    except OSError:
        pass        # evidence never decides a turn


def _attempt_waymark(request, spec, session_id, attempt, *, max_bytes=None):
    """Validate and (for a read) perform one Waymark spec.  Returns (pending action or None, result text,
    outcome, read facts); raises waymark.WaymarkError for a refusal.  Reading never mutates; a mutation is
    only staged with the turn.  A read names only what a Waymark result of this occasion issued."""
    parsed = waymark.parse_request(request, spec)
    if parsed["request"] in waymark.MUTATION_REQUESTS:
        waymark.preflight_mutation(PROVENANCE_DB_DIR, session_id=session_id, action=parsed)
        condition = waymark.action_resource_condition(parsed)
        text = waymark.render_accepted_for_commit(
            parsed["request"], handle=parsed.get("handle"), attempt=attempt,
            resource_seen=(waymark.resource_delivered_before(PROVENANCE_DB_DIR, session_id=session_id,
                                                             condition=condition)
                           if condition is not None else None))
        return parsed, text, "ACCEPTED_FOR_COMMIT", None
    reads = _waymark_reads_state()
    if not _waymark_read_adds_evidence(parsed, session_id):
        # The read grammar no longer offers it; the host refuses it regardless.
        raise waymark.WaymarkSpecError(
            "SPEC_ALREADY_DELIVERED", "this occasion already delivered exactly that Waymark evidence, unchanged")
    if parsed["request"] == waymark.LIST:
        continuation = parsed.get("cursor") is not None or parsed.get("shelf_digest") is not None
        if continuation and spec.replace(" ", "") not in {c.replace(" ", "") for c in reads["list_continuations"]}:
            raise waymark.WaymarkSpecError(
                "SPEC_CONTINUATION", "that next-page spec was not issued by a Waymark shelf page of this occasion")
        # One complete shelf page per result (never a fragment of a larger page).
        page = waymark.list_waymarks(
            PROVENANCE_DB_DIR, session_id=session_id, include_historical=parsed["view"] == "all",
            cursor=parsed.get("cursor"), shelf_digest=parsed.get("shelf_digest"), max_bytes=max_bytes)
        handles = [item["handle"] for item in page["items"]]
        reads["inspect_choices"].update(f"handle={h}" for h in handles)
        next_spec = (f"view={parsed['view']};cursor={page['next_cursor']};shelf_digest={page['shelf_digest']}"
                     if page["more_exists"] else None)
        if next_spec:
            reads["list_continuations"].add(next_spec)
        reads["delivered_snapshots"].add(page["snapshot"])
        reads["shown_in"].update((h, parsed["view"]) for h in handles)
        reads["delivered_lines"].update(waymark.shelf_line(item) for item in page["items"])
        return None, page["rendered"], "READ", {
            "operation": "list", "continuation": continuation, "view": parsed["view"], "handles": handles,
            "more_exists": page["more_exists"], "next_spec": next_spec, "shelf_digest": page["shelf_digest"]}
    continuation = parsed.get("projection_cursor", 0) > 0
    if continuation and spec.replace(" ", "") not in {c.replace(" ", "") for c in reads["inspect_choices"]}:
        raise waymark.WaymarkSpecError(
            "SPEC_CONTINUATION", "that inspect continuation was not issued by a Waymark result of this occasion")
    projection = waymark.inspect_waymark(PROVENANCE_DB_DIR, session_id=session_id, handle=parsed["handle"])
    text = waymark.render_query_result(
        projection, handle=parsed["handle"], projection_cursor=parsed.get("projection_cursor", 0),
        expected_sha256=parsed.get("projection_sha256"), max_bytes=max_bytes)
    marker = "next_projection_cursor="
    next_cursor = text.split(marker, 1)[1].split(" ", 1)[0]
    digest = text.split("projection_sha256=", 1)[1].split(" ", 1)[0]
    next_spec = (f"handle={parsed['handle']};projection_cursor={next_cursor};projection_sha256={digest}"
                 if next_cursor != "None" else None)
    if next_spec:
        reads["inspect_choices"].add(next_spec)
    _remember_projection_delivery(reads, parsed["handle"], digest, parsed.get("projection_cursor") or 0, text)
    return None, text, "READ", {
        "operation": "inspect", "continuation": continuation, "handle": parsed["handle"],
        "visible": bool(projection.get("exists")), "next_spec": next_spec}


def _resolve_waymark(validated_act, session_id, *, occasion=None, human_input_event_id=None, max_bytes=None):
    """One Waymark request of the occasion, settled before the pass that answers it reads its result (a read
    check's re-entry, or Pass 2 for the final mutating act):

        spec attempt 1 -> accepted/read: done
                       -> correctable refusal (waymark.WaymarkSpecError): the exact refusal goes back to the
                          authoring path; ONE correction spec, authored beside the same admitted prompt
                          -> accepted/read: done
                          -> refused: terminal
                       -> any other refusal (authority, scope, target state, control, room): terminal

    Not an action-reselection loop: the request is fixed; only its spec is rewritten, at most once.  A
    correction call that does not complete fails the turn closed (the act's own payload law).  Integrity or
    store errors propagate (fail closed).  Outcomes are typed by operation: only a create can be NOT_CREATED.
    Returns {action, text, outcome, attempts, read}."""
    request, spec = validated_act["waymark_request"], validated_act["waymark_spec"]
    binding = (_TURN_ECHO.get() or {}).get("waymark_binding")
    if _TURN_ECHO.get() is not None:
        _TURN_ECHO.get()["waymark_lane"] = {"waymark_lane_id": uuid.uuid4().hex, "session_id": session_id,
                                            "occasion": occasion, "human_input_event_id": human_input_event_id}
    attempts = []
    for attempt in range(1, WAYMARK_MAX_SPEC_ATTEMPTS + 1):
        try:
            action, text, outcome, read = _attempt_waymark(request, spec, session_id, attempt, max_bytes=max_bytes)
        except waymark.WaymarkError as exc:
            retryable = (isinstance(exc, waymark.WaymarkSpecError) and exc.code in waymark.RETRYABLE_REFUSAL_CODES)
            attempts.append({"attempt": attempt, "outcome": "REFUSED", "code": exc.code, "reason": exc.reason,
                             "retryable": retryable})
            _record_waymark_event(
                event="spec_attempt", attempt=attempt, request=request, spec=spec, outcome="REFUSED",
                refusal_code=exc.code, refusal_reason=exc.reason, retryable=retryable,
                refusal_class=type(exc).__name__, executed=False)
            if attempt == WAYMARK_MAX_SPEC_ATTEMPTS or not retryable or binding is None:
                if isinstance(exc, waymark.WaymarkRoomError) and request in WAYMARK_READ_REQUESTS:
                    _waymark_reads_state()["room_refused"].add(spec.replace(" ", ""))
                terminal = waymark.terminal_outcome(request)
                text = waymark.render_not_created(request, exc.code, exc.reason, attempt=attempt, max_bytes=max_bytes)
                _record_waymark_event(
                    event="resolution", request=request, outcome=terminal, attempts=attempt,
                    pending_action_created=False)
                return {"action": None, "text": text, "outcome": terminal, "attempts": attempts, "read": None,
                        "spec": spec}
            try:
                spec = _generate_pass1_payload(
                    "waymark_spec", binding["act"], binding["base_messages"], binding["base_cost"],
                    binding["occasion_envelope"],
                    request=_waymark_correction_request(binding["act"], spec, exc.code, exc.reason))
            except Pass1PayloadFailure as failure:
                _record_waymark_event(
                    event="correction_authoring_failed", request=request, attempt=attempt + 1,
                    failure=failure.failure_code, termination=failure.termination, pending_action_created=False)
                direction_failure = ConversationDirectionFailure("waymark_correction", failure.failure_code)
                ui_turn_diagnostics.attach_pass1_failure_preview(direction_failure, failure.partial_output)
                raise direction_failure from failure
            continue
        attempts.append({"attempt": attempt, "outcome": outcome})
        _record_waymark_event(
            event="spec_attempt", attempt=attempt, request=request, spec=spec, outcome=outcome,
            executed=read is not None, **({"read": read} if read is not None else {}),
            **({"delivered_text": text} if read is not None else {}))
        _record_waymark_event(
            event="resolution", request=request, outcome=outcome, attempts=attempt,
            pending_action_created=action is not None)
        return {"action": action, "text": text, "outcome": outcome, "attempts": attempts, "read": read, "spec": spec}


def _generate_pass1_payload(field, act, base_messages, base_cost, occasion_envelope, request=None):
    """Clark authors one payload on a context sufficient for every lawful value.

    A lawful value completes in one call. A structurally complete value that
    violates the authoritative character/nonblank boundary may be re-authored
    once from the unchanged admitted context. Malformed/incomplete output fails
    immediately. Nothing is truncated, continued, or accepted before it fits.

    The ordinary call is a complete call on a context sufficient for every
    lawful payload of that field (owner decisions 2026-10-08: a promised capability is completely
    accessible; continuation failed live three ways and is not a path).

    Context: exactly the control prompt admitted for the act (``base_messages``/``base_cost``) -- what
    Clark saw when he chose it, including the receipts of any checks he chose -- plus the host's
    mechanical request.  Nothing is selected, dropped, summarized or reordered.
    Shell: the field name and JSON frame are fixed by the one-field grammar, whose string rule admits
    every Unicode scalar value; ids, act and bound handles are the host's (_bind_pass1_payloads).  Clark
    writes only the words.
    Window: the call runs on WAKING_SERVING_CONTEXT with an allowance of the field's worst lawful output
    (pass1_payload_max_output_tokens), which with any admitted prompt fits by construction -- so every
    body up to the field's character ceiling completes, whatever its tokenization.
    Completion: a cut-off generation (PAYLOAD_OUTPUT_LIMIT_REACHED -- only possible beyond the lawful
    maximum), an unfinished or a malformed one fails closed; nothing partial is used."""
    request, request_cost = request if request is not None else _binding_request(field, act, occasion_envelope)
    if field == "waymark_spec" and _is_waymark_read(act):
        # A read's spec is closed to what this occasion lawfully offers (no foreign cursor can be written).
        schema = waymark_read_payload_schema(act["waymark_request"], **_waymark_read_grammar())
    else:
        schema = pass1_payload_schema(field)
    required = pass1_payload_max_output_tokens(field)
    for attempt in (1, 2):
        messages = list(base_messages) + [request]
        prompt_tokens = base_cost + request_cost
        outcome, allowance = context_budget.atomic_payload_window(prompt_tokens, required)
        ui_turn_diagnostics.record_payload_preflight(
            field=field, outcome=outcome, method="admitted_estimate", prompt_tokens=prompt_tokens,
            allowance=allowance, attempt=attempt)
        if outcome != context_budget.FITS_ATOMIC_WINDOW:
            raise Pass1PayloadFailure(context_budget.ATOMIC_WINDOW_INSUFFICIENT, field, termination="not_generated")
        response = ui_turn_diagnostics.timed_model_call(
            "pass1_payload" if attempt == 1 else "pass1_payload_correction",
            ask_llama_for_payload, messages, allowance, format=schema)
        written = (response.get("message") or {}).get("content") or ""
        termination = response.get("done_reason")
        try:
            context_budget.require_complete_response(response, allowance)
        except context_budget.IncompleteCompletionError as exc:
            code = (context_budget.PAYLOAD_OUTPUT_LIMIT_REACHED
                    if exc.failure_code == "COMPLETION_LIMIT_REACHED" else exc.failure_code)
            ui_turn_diagnostics.record_payload_result(
                field=field, outcome=code, termination=termination,
                eval_count=response.get("eval_count"), allowance=allowance, attempt=attempt)
            raise Pass1PayloadFailure(code, field, written, termination) from None
        try:
            value = json.loads(written).get(field)
        except (TypeError, ValueError, AttributeError):
            value = None
        failure = pass1_payload_boundary_failure(field, value)
        if failure is None:
            ui_turn_diagnostics.record_payload_result(
                field=field, outcome="COMPLETED", termination=termination,
                eval_count=response.get("eval_count"), allowance=allowance, attempt=attempt)
            return value
        ui_turn_diagnostics.record_payload_result(
            field=field, outcome=failure, termination=termination,
            eval_count=response.get("eval_count"), allowance=allowance, attempt=attempt)
        if failure == "PAYLOAD_MALFORMED":
            raise Pass1PayloadFailure(failure, field, written, termination)
        if field != "historical_query":
            # Existing optional/outward capabilities own their established
            # drop-and-disclose or syntax-aware validation semantics.  The
            # correction is narrowly for historical re-entry, whose invalid
            # query otherwise aborts the whole waking occasion (the observed
            # blank same-H recovery) instead of dropping an optional effect.
            return value
        if attempt == 2:
            raise Pass1PayloadFailure(failure, field, written, termination)
        correction = {
            "role": "user",
            "content": render_pass1_payload_correction_request(field, failure),
        }
        correction_cost = (
            context_budget.estimate_tokens(correction["content"])
            + context_budget.MEASURED_BOUNDARY_TOKENS
        )
        if correction_cost > context_budget.PAYLOAD_REQUEST_MAX_TOKENS:
            raise Pass1PayloadFailure("PAYLOAD_CORRECTION_REQUEST_TOO_LARGE", field, written, termination)
        request, request_cost = correction, correction_cost


def _bind_pass1_payloads(raw, control_schema, selection_grammars, base_messages, base_cost, occasion_envelope,
                         payload_failures=None):
    """Assemble one chosen control act with the selections and payloads it structurally requires, in
    the unchanged raw-act shape the validator judges.  A field the control grammar cannot produce is
    never taken from the control output; ``thread`` is "" unless the act reads it.  Nothing is executed
    or recorded here.

    A selection or payload that does not complete (owner decision 2026-10-08): when it belongs to an
    OPTIONAL request and ``payload_failures`` is given, that request is not made (dropped with its own
    fields, conversation_direction.drop_failed_payload_requests), a mechanical failure fact is appended,
    and binding goes on -- the act and every other choice stand and reach Pass 2.  Otherwise (the act's
    own payload, or no list given) Pass1PayloadFailure is raised and the turn fails closed."""
    try:
        control = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(control, dict):
        return raw
    carried = {k for b in control_schema.get("anyOf") or [control_schema] for k in b["properties"]}
    bound = set(PASS1_PAYLOAD_FIELD_MAX_CHARS) | set(PASS1_SELECTION_FIELDS)
    if any(k not in carried and k not in bound for k in control):
        # A control field this grammar cannot produce (a provider that ignored the grammar): nothing is
        # bound or generated for it; the act goes to the validator and the host's refusals unchanged.
        return raw
    act = {k: v for k, v in control.items() if k not in bound or k in carried}
    act.setdefault("thread", "")
    _begin_act_echo()
    for field in pass1_binding_fields(act, control_schema, selection_grammars):
        if field not in pass1_binding_fields(act, control_schema, selection_grammars):
            continue                                  # its request was already not made
        try:
            if field in PASS1_SELECTION_FIELDS:
                act[field] = _select_pass1_handle(field, selection_grammars[field], act,
                                                  base_messages, base_cost, occasion_envelope)
            else:
                act[field] = _generate_pass1_payload(field, act, base_messages, base_cost, occasion_envelope)
                _reserve_payload_echo(field, act[field])
                if field == "waymark_spec":
                    _remember_waymark_binding(act, base_messages, base_cost, occasion_envelope)
        except Pass1PayloadFailure as exc:
            repaired, dropped = drop_failed_payload_requests(act, field)
            if payload_failures is None or repaired is None:
                raise
            act = repaired
            payload_failures.append({"field": field, "failure_class": exc.failure_code,
                                     "termination": exc.termination, "dropped": tuple(dropped)})
    return json.dumps(act, ensure_ascii=False)


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
_SLEEP_DECISION_OPTIONS = {"num_ctx": context_budget.WAKING_SERVING_CONTEXT, "num_predict": 160}


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
    if not result.fits:      # an admitted human input is carried (HUMAN INPUT ADMISSION, 2026-10-08)
        result = _compose_with_turn_reserve(
            [hard_core, hard_human, soft_dialogue], context_budget.PASS2_MAX_PROMPT_BUDGET) or result
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
    result = context_budget.compose_within_budget(
        [hard_scaffold, hard_dynamic], context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET,
    )
    if not result.fits:      # an admitted human input is carried (HUMAN INPUT ADMISSION, 2026-10-08)
        result = _compose_with_turn_reserve(
            [hard_scaffold, hard_dynamic], context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET) or result
    return result


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


def _canonical_status_text(result):
    """The exact canonical receipt status as the host knows it (enum plus its mechanical outcome)."""
    return f"{result['completion']} ({result['mechanical_outcome']})"


def _candidate_translate(raw, acknowledged, claims=None):
    """Candidate provider translation of one Pass-1 output: the explicit reply choice, the receipt acknowledgement
    (recorded into ``acknowledged``) and the inspect/mutate workspace acts.  Returns (raw for the production
    validator, workspace mode or None).  A no-op unless CANDIDATE_PROTOCOL_MECHANICS."""
    if not CANDIDATE_PROTOCOL_MECHANICS:
        return raw, None
    raw = translate_explicit_reply_choice(raw)
    raw, ack = take_receipt_acknowledgement(raw)
    if ack is not None:
        acknowledged.append(ack)
    raw, claim = take_workspace_claim(raw)
    if claims is not None:
        claims.append(claim)
    return translate_candidate_workspace_act(raw)


def _fit_verify_evidence(result, current_presence, evidence_room):
    """Bound a verify receipt by whole provenance records.

    The full structured result remains in ``occasion_checks`` for canonical
    recording.  Only its model-facing projection is bounded.  The projection
    always retains the exact query and mechanical outcome, and, when the full
    provenance list does not fit, retains the largest newest suffix of whole
    exact source records plus an explicit window count.  No record is torn or
    summarized into a claimed conclusion.
    """
    full = action_verification.render_result(result, current_presence=current_presence)
    if evidence_room is None:
        return full

    token_room = max(0, evidence_room.get("tokens", 0))
    estimate_room = max(0, evidence_room.get("estimate", 0))
    provenance = list(result.get("provenance") or ())

    def rendered(count):
        projected = dict(result, provenance=provenance[-count:] if count else [])
        text = action_verification.render_result(projected, current_presence=current_presence)
        if count < len(provenance):
            text += (f"\nmodel-facing provenance window: newest {count} of {len(provenance)} exact records "
                     "shown; the complete structured lookup result remains durably attached to this occasion."
                     + (" Each shown Discord turn id can be checked directly with target turn:<id>."
                        if result.get("query", {}).get("target", {}).get("identity_kind")
                        == "discord_destination_id" else ""))
        return text

    def fits(text):
        estimated = context_budget.estimate_tokens(text)
        if estimated > estimate_room:
            return False
        return _real_cost(text, estimated) <= token_room

    if fits(full):
        return full
    low, high = 0, len(provenance)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(rendered(middle)):
            low = middle
        else:
            high = middle - 1
    bounded = rendered(low)
    if not fits(bounded):
        raise ConversationDirectionFailure("pass1_reentry", context_budget.BUDGET_EXCEEDED)
    return bounded


def _historical_search_projection(result, count):
    """Render one truthful whole-candidate prefix of a search result."""
    candidates = list(result.get("candidates") or ())
    count = max(0, min(count, len(candidates)))
    projected = dict(result, candidates=candidates[:count])
    text = historical_continuity_access.render_search(projected)
    if count < len(candidates):
        text += (
            f"\nmodel-facing candidate window: first {count} of {len(candidates)} "
            "verified visible candidates shown in retrieval order; only shown read cursors "
            "are selectable in this occasion. The complete structured search result remains "
            "attached to the occasion."
        )
    return text, candidates[:count]


def _fit_historical_search_reentry(
        result, compose_projection, compose_projection_with_dialogue_pressure=None):
    """Choose the largest prefix accepted by the actual final compositor.

    ``compose_projection(text, candidates)`` must construct the exact re-entry
    schema and exact contribution set that will be used for generation, then
    return a bundle whose ``result`` is the real ``CompositionResult``.  This
    keeps candidate fitting and final admission under one budget authority.
    Earlier historical receipts are therefore neither charged when the final
    latest-only carrier replaces them nor omitted when the final carrier keeps
    them.  Zero candidates remains a truthful, selectable-cursor-free
    projection and is tested like every other whole prefix.
    """
    candidates = list(result.get("candidates") or ())

    def trial(count):
        text, presented = _historical_search_projection(result, count)
        return text, presented, compose_projection(text, presented)

    full = trial(len(candidates))
    if full[2]["result"].fits:
        return full
    low, high = 0, len(candidates)
    best = None
    while low < high:
        middle = (low + high + 1) // 2
        attempted = trial(middle)
        if attempted[2]["result"].fits:
            low = middle
            best = attempted
        else:
            high = middle - 1
    bounded = best if best is not None and len(best[1]) == low else trial(low)
    if not bounded[2]["result"].fits:
        if compose_projection_with_dialogue_pressure is not None:
            # Preserve the established priority law: first reduce the search
            # to a truthful whole-candidate prefix while retaining the newest
            # dialogue pair. Only when even zero candidates cannot fit may the
            # ordinary whole-pair pressure valve reclaim that SOFT dialogue.
            return _fit_historical_search_reentry(
                result, compose_projection_with_dialogue_pressure)
        raise ConversationDirectionFailure("pass1_reentry", context_budget.BUDGET_EXCEEDED)
    return bounded


def _verify_check(occasion_checks, validated_act, raw_act_only, evidence_room=None):
    """Execute one read-only verify_action check of this occasion and append its exact result."""
    query = action_verification.make_query(
        validated_act["action_verification_request"], validated_act["action_verification_target"])
    result = action_verification.verify_action(
        PROVENANCE_DB_DIR, query,
        workspace_action_log_path=os.path.join(runtime_roots.workspace_root(), "workspace_action_log.jsonl"),
    )
    import workspace_capability as _wc_check_paths
    paths = _wc_check_paths.WorkspacePaths.production_defaults()
    try:
        presence = action_verification.exact_current_presence(query, notes_dir=paths.notes_dir, journal_dir=paths.journal_dir)
    except OSError:
        presence = None
    occasion_checks.append({"kind": "verify", "query": query, "result": result, "raw_act": raw_act_only,
                            "rendered": _fit_verify_evidence(result, presence, evidence_room),
                            "evidence_presence": presence})


def _check_summary(check):
    return {"kind": check.get("kind", "verify"), "query": check.get("query"), "result": check.get("result"),
            "inspection": check.get("inspection"), "claim_witness": check.get("claim_witness"),
            "historical_result": check.get("historical_result")}


def _history_view_kwargs(family_view):
    """Return only the already-resolved privacy view; never infer it from query text."""
    if family_view is None:
        return {
            "viewer_principal_actor_id": None, "viewer_scope": None,
            "active_family_principal_ids": frozenset(),
        }
    return {
        "viewer_principal_actor_id": family_view["principal_actor_id"],
        "viewer_scope": family_view["visibility_scope"],
        "active_family_principal_ids": family_view["active_family_principal_ids"],
    }


def _history_check(paths, validated_act, allowed_cursors, allowed_browse_cursors,
                   family_view, raw_act_only, evidence_room=None, search_cache=None):
    kwargs = _history_view_kwargs(family_view)
    if validated_act["act"] == SEARCH_HISTORICAL_CONTINUITY:
        query = validated_act["historical_query"]
        # Historical access is read-only and the privacy view is fixed for
        # this synchronous occasion.  Re-running the literal same query can
        # therefore add no mechanical information; reuse its authoritative
        # structured result without suppressing the subject's search choice
        # or its ensuing re-entry.
        cache_hit = search_cache is not None and query in search_cache
        if cache_hit:
            result = search_cache[query]
        else:
            result = historical_continuity_access.search(paths, query, **kwargs)
            if search_cache is not None:
                search_cache[query] = result
        # Candidate fitting and cursor binding happen only against the exact
        # final re-entry composition below.  Binding here used to make a
        # preliminary evidence-room estimate authoritative even when the
        # final carrier represented a different historical snapshot.
        rendered = historical_continuity_access.render_search(result)
        kind = "historical_search"
    elif validated_act["act"] == READ_HISTORICAL_ITEM:
        cache_hit = False
        result = historical_continuity_access.read(
            paths, validated_act["historical_cursor"], allowed_cursors=allowed_cursors, **kwargs)
        if result.get("next_cursor"):
            allowed_cursors.add(result["next_cursor"])
        rendered = historical_continuity_access.render_read(result)
        kind = "historical_read"
    else:
        cache_hit = False
        result = historical_continuity_access.browse(
            paths, validated_act.get("historical_browse_cursor") or None,
            allowed_cursors=allowed_browse_cursors, **kwargs)
        rendered = historical_continuity_access.render_browse(result)
        kind = "historical_browse"
    return {
        "kind": kind, "query": None, "result": None,
        "historical_result": result, "raw_act": raw_act_only, "rendered": rendered,
        "mechanical_cache_hit": cache_hit,
        "historical_browse_request_cursor": (
            validated_act.get("historical_browse_cursor") or None
            if validated_act["act"] == BROWSE_HISTORICAL_CONTINUITY else None),
    }


def _latest_waymark_checks(occasion_checks):
    """Of this occasion's Waymark reads, the receipts still displayed: the latest list page and the latest inspect
    result (an earlier page/fragment is superseded by the later read Clark chose, like a historical search)."""
    latest = {}
    for check in occasion_checks:
        if check.get("kind", "").startswith("waymark_"):
            latest[check["kind"]] = check
    return [c for c in occasion_checks if c.get("kind", "").startswith("waymark_") and latest.get(c["kind"]) is c]


def _without_superseded_waymark(occasion_checks):
    """Every check of the occasion, except Waymark read receipts a later read of the same operation superseded
    (the evidence carried into claim/witness/workspace calls): Waymark adds at most two receipts anywhere."""
    shown = _latest_waymark_checks(occasion_checks)
    return [c for c in occasion_checks if not c.get("kind", "").startswith("waymark_") or any(c is w for w in shown)]


def _displayed_occasion_checks(occasion_checks):
    """What a Pass-1 re-entry displays: every ordinary check, the latest historical check, and the latest Waymark
    list and inspect receipts -- in occasion order."""
    history_checks = [c for c in occasion_checks if c.get("kind", "").startswith("historical_")]
    waymark_shown = _latest_waymark_checks(occasion_checks)
    return [c for c in occasion_checks
            if (not c.get("kind", "").startswith(("historical_", "waymark_")))
            or (history_checks and c is history_checks[-1])
            or any(c is w for w in waymark_shown)]


def _pass2_evidence_checks(occasion_checks):
    """Carry all source-bearing history evidence into final prose generation.

    Chronological pages are independent contiguous evidence and can never be
    superseded by a later page.  A search excerpt is superseded only when an
    exact read of one of its candidates follows; all exact reads remain.
    Preserve occasion order so model-facing provenance matches execution.
    """
    reads = [c for c in occasion_checks if c.get("kind") == "historical_read"]
    last_search = next((c for c in reversed(occasion_checks)
                        if c.get("kind") == "historical_search"), None)
    waymark_shown = _latest_waymark_checks(occasion_checks)
    selected = []
    for check in occasion_checks:
        kind = check.get("kind", "")
        if kind == "historical_browse" or kind == "historical_read":
            selected.append(check)
        elif kind == "historical_search" and not reads and check is last_search:
            selected.append(check)
        elif kind.startswith("waymark_"):
            if any(check is shown for shown in waymark_shown):
                selected.append(check)
        elif not kind.startswith("historical_"):
            selected.append(check)
    return selected


def _historical_evidence_manifest(occasion_checks, pass2_checks):
    """Canonicalizable receipt for exactly what history reached this turn.

    Source text is not duplicated: its canonical locator and digest authenticate
    it.  The manifest records both per-reentry delivery and final-Pass-2
    carriage, closing the distinction that the recovered turn exposed.
    """
    import hashlib as _hashlib
    actions = []
    for action_ordinal, check in enumerate(
            c for c in occasion_checks if c.get("kind", "").startswith("historical_")):
        kind = check["kind"]
        result = check.get("historical_result") or {}
        rendered = check.get("rendered") or ""
        receipt = {
            "action_ordinal": action_ordinal,
            "action_type": kind.removeprefix("historical_"),
            "delivered_in_pass1_reentry": True,
            "delivered_in_final_pass2": any(check is carried for carried in pass2_checks),
            "rendered_utf8_bytes": len(rendered.encode("utf-8")),
            "rendered_sha256": _hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        }
        if kind == "historical_browse":
            receipt.update({
                "request_cursor": check.get("historical_browse_request_cursor"),
                "beginning": bool(result.get("beginning")),
                "end": bool(result.get("end")),
                "next_cursor": result.get("next_cursor"),
                "ordering": result.get("ordering"),
                "sources": [{key: row.get(key) for key in (
                    "occurrence_index", "event_id", "item_id", "canonical_anchor_event", "occurred_at", "source_store",
                    "source_locator", "content_sha256", "authentication", "canonical_structural_role",
                    "canonical_component_kind", "actor_relation", "start", "end", "segment_complete")}
                    for row in result.get("records", [])],
                "withheld": result.get("withheld", []),
            })
        elif kind == "historical_search":
            receipt.update({
                "query": result.get("query"),
                "sources": [{key: row.get(key) for key in (
                    "rank", "handle", "read_cursor", "occurred_at", "source_store", "source_locator",
                    "content_sha256", "authentication", "canonical_anchor_event", "canonical_structural_role",
                    "canonical_component_kind", "actor_relation")}
                    for row in result.get("candidates", [])],
                "withheld": result.get("withheld", []),
            })
        else:
            receipt.update({
                "handle": result.get("handle"), "start": result.get("start"), "end": result.get("end"),
                "source_chars": result.get("source_chars"), "next_cursor": result.get("next_cursor"),
                "source": {key: result.get(key) for key in (
                    "occurred_at", "source_store", "source_locator", "content_sha256", "authentication",
                    "canonical_anchor_event", "canonical_structural_role", "canonical_component_kind",
                    "actor_relation")},
            })
        actions.append(receipt)
    if not actions:
        return None
    return {
        "schema": "anaxi.history_evidence_manifest.v1",
        "source_text_storage": "canonical_source_by_locator_and_sha256_not_duplicated",
        "actions": actions,
    }


def _provider_boundary_history_evidence(
        hippocampal_contribution, delivered_hippocampal_contribution,
        occasion_checks, history_evidence_manifest):
    """Build transient evidence descriptors for the final provider seam."""
    evidence = retrieval_delivery.provider_boundary_evidence(
        getattr(hippocampal_contribution, "retrieval_candidates", None),
        delivered_hippocampal_contribution,
    ) if hippocampal_contribution is not None else []
    historical_checks = [
        check for check in occasion_checks
        if check.get("kind", "").startswith("historical_")
    ]
    actions = (history_evidence_manifest or {}).get("actions", [])
    if len(historical_checks) != len(actions):
        raise provider_boundary_receipt.ProviderBoundaryReceiptError(
            "historical evidence manifest/check cardinality drifted before provider dispatch"
        )
    for check, action in zip(historical_checks, actions):
        delivered = action["delivered_in_final_pass2"] is True
        record = {
            "evidence_kind": "explicit_historical_" + action["action_type"],
            "evidence_id": f"history-action-{action['action_ordinal']}",
            "action_ordinal": action["action_ordinal"],
            "rendered_sha256": action["rendered_sha256"],
            "source_refs": action.get("sources") or ([action.get("source")] if action.get("source") else []),
            "expected_delivery": delivered,
            "exact_text": check.get("rendered") or "",
            "disposition": (
                "retained_for_provider_boundary" if delivered
                else "superseded_before_final_pass2"
            ),
        }
        evidence.append(record)
    return evidence


def _outward_send_witness_gate(validated_act, *, authorized_discord_destinations, pulse_occasion, caret_occasion,
                               system_text, dialogue_units, current_input, current_input_role, evidence_texts,
                               occasion_envelope=None):
    """WITNESS GATE V0 at the outward-send boundary.  Returns (validated_act, gate outcome); on cancel the send
    request is removed before anything is committed (no canonical send, no dispatch)."""
    destination_id = validated_act["discord_destination_id"]
    label = next((d.get("display_label") for d in authorized_discord_destinations
                  if d.get("destination_id") == destination_id), None)
    words = validated_act["discord_message_text"]
    action_lines = [
        "proposed action: send a Discord message (send_message); it is dispatched when this turn completes",
        f"destination: {destination_id} ({label or 'unlabeled'}), an owner-authorized destination",
        f'exact words to be sent: "{words}"',
        "thread/event binding: " + ("a reply to the inbound Caret message of this occasion" if caret_occasion is not None
                                    else "none (a new message)"),
        "time now: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    ]

    def inspect_prior_sends():
        query = action_verification.make_query(action_verification.DISCORD_SEND, destination_id)
        result = action_verification.verify_action(
            PROVENANCE_DB_DIR, query,
            workspace_action_log_path=os.path.join(runtime_roots.workspace_root(), "workspace_action_log.jsonl"))
        return action_verification.render_result(result).split("\n", 1)[1]
    outcome = witness_gate.run_gate(
        sys.modules[__name__], family="outward_send",
        occasion=witness_gate.occasion_lines(pulse_kind=(pulse_occasion or {}).get("kind"),
                                             caret=caret_occasion is not None),
        action_lines=action_lines, system_text=system_text, dialogue_units=dialogue_units,
        current_input=current_input, current_input_role=current_input_role, evidence_texts=evidence_texts,
        occasion_envelope=occasion_envelope,
        inspect_description="one read-only look at the canonical record of earlier sends to this destination",
        inspect_fn=inspect_prior_sends,
        facts={"destination_id": destination_id, "destination_label": label, "words_chars": len(words),
               "occasion_kind": (pulse_occasion or {}).get("kind") or ("caret" if caret_occasion is not None else "human")},
        log_dir=PROVENANCE_DB_DIR)
    if outcome["decision"] != witness_gate.PROCEED:
        validated_act = dict(validated_act, discord_correspondence_request=DISCORD_CORRESPONDENCE_REQUEST_NONE,
                             discord_destination_id="", discord_message_text="", discord_correspondent_ref="")
    return validated_act, outcome


CLAIM_WITNESS_ROOM = 1200   # fallback only (no real room given): conservative estimator units

# REAL-ROOM EVIDENCE (canonical-history-first repair, 2026-10-03): a read-only check's evidence rides HARD, joined
# with the occasion's earlier checks, in the Pass-1 re-entry (the tightest carrier: its hard control text is
# larger than Pass 2's) and in Pass 2.  Its room is what that re-entry really leaves -- the Pass-1 budget less the
# re-entry's hard control contributions, the earlier evidence and the check-state line.  Each bounded receipt is
# measured independently; the joined carrier is charged by summing those safe costs and its literal separators.
# Nothing else changes: the same contributions, budgets and num_ctx.
OCCASION_EVIDENCE_MEASURABLE_LIMIT = context_budget.CONTEXT_CEILING - context_budget.SAFETY_MARGIN - 64
OCCASION_EVIDENCE_JOIN_SLACK = 16


def _real_cost(text, fallback):
    """The exact cost the compositor will charge an atomic ``text`` block.

    ``recost_by_measurement`` retains a deliberate/calibrated cost unchanged.
    Otherwise it charges the measured provider tokens plus its fixed message-
    boundary allowance, capped by the conservative estimator.  Evidence-room
    arithmetic must use those same units; raw measured tokens alone undercount
    every contribution by ``MEASURED_BOUNDARY_TOKENS``.
    """
    estimated = context_budget.estimate_tokens(text)
    if fallback != estimated:
        return fallback
    measured = _measure_real_text_cost_memoized(text) if text else 0
    return (min(measured + context_budget.MEASURED_BOUNDARY_TOKENS, fallback)
            if type(measured) is int and measured > 0 else fallback)


def _occasion_evidence_framing_cost():
    states = [render_occasion_check_state(i) for i in range(MAX_READ_ONLY_CHECKS_PER_OCCASION + 1)]
    state_cost = max(_real_cost(state, context_budget.estimate_tokens(state)) for state in states)
    # One separator may join prior/current receipts and one joins the current
    # receipt to its check-state line.
    return (state_cost + context_budget.estimate_tokens("\n\n\n")
            + OCCASION_EVIDENCE_JOIN_SLACK)


def _joined_evidence_cost(occasion_checks):
    """Safe compositor-unit cost for separately measurable check receipts.

    A joined receipt can exceed the probe's own admission ceiling even when
    every bounded receipt and the final prompt fit.  Sum the independently
    measurable whole receipts and conservatively charge their literal joiners;
    this remains a safe upper bound and avoids making the final join itself a
    prerequisite for measuring evidence already admitted truthfully.
    """
    texts = [c["rendered"] for c in occasion_checks]
    if not texts:
        return 0
    return (sum(_real_cost(text, context_budget.estimate_tokens(text)) for text in texts)
            + (len(texts) - 1) * context_budget.estimate_tokens("\n\n"))


def _reentry_action_record_cost(occasion_checks, check_state):
    return (_joined_evidence_cost(occasion_checks)
            + context_budget.estimate_tokens("\n")
            + _real_cost(check_state, context_budget.estimate_tokens(check_state))
            + OCCASION_EVIDENCE_JOIN_SLACK)


def _compose_pass1_reentry(*, hard_contributions, dialogue_units, displayed_checks,
                           check_state, reentry_schema, search_choices_contrib=None,
                           open_page_contrib=None, allow_dialogue_pressure=False):
    """Build and compose the exact contribution set used by Pass-1 re-entry.

    Historical candidate fitting calls this same function and retains the
    chosen result for generation.  There is no separate approximation of
    fixed overhead, pinned dialogue, schema, or which historical receipt the
    latest-only carrier will actually show.
    """
    evidence_texts = [c["rendered"] for c in displayed_checks]
    evidence_texts[-1] = evidence_texts[-1] + "\n" + check_state
    dialogue_contrib = dialogue_contribution(dialogue_units)
    action_record = context_budget.Contribution(
        context_budget.ACTION_RECORD_RESULT, "\n\n".join(evidence_texts), hard=True)
    action_record.cost = _reentry_action_record_cost(displayed_checks, check_state)
    contributions = list(hard_contributions) + [dialogue_contrib, action_record]
    if search_choices_contrib is not None:
        contributions.append(context_budget.Contribution(
            context_budget.PASS1_SEARCH_CHOICES,
            search_choices_contrib.rendered_text, hard=False))
    if open_page_contrib is not None:
        contributions.append(context_budget.Contribution(
            context_budget.PASS1_OPEN_PAGE,
            open_page_contrib.rendered_text, hard=False))
    recost_when_shedding(
        contributions, _pass1_prompt_budget(),
        structured_schema=reentry_schema)
    result = context_budget.compose_within_budget(
        contributions, _pass1_prompt_budget())
    if not result.fits and allow_dialogue_pressure:
        # The ordinary initial Pass 1 already applies this exact pressure
        # valve to an oversized newest dialogue pair.  Re-entry must use the
        # same law: preserve the intact pair when it can be measured safely;
        # otherwise drop that whole SOFT pair before allowing it to veto the
        # current H plus the HARD receipt Clark explicitly requested.  Live
        # H 01M4BNHQ8F68FTKRF9EY80V455 exposed the missing seam: initial
        # Pass 1 lawfully dropped an unmeasurable 5,252-unit recovered pair,
        # then re-entry rebuilt and pinned it, producing 7,386/3,456 even
        # though its complete HARD floor plus receipt was only 2,134.
        hard_human = next(
            c for c in hard_contributions
            if c.kind == context_budget.CURRENT_HUMAN_MESSAGE)
        result = _recover_hard_overflow_via_real_measurement(
            result, contributions, _pass1_prompt_budget(),
            hard_human, hard_human.rendered_text,
            structured_schema=reentry_schema,
        )
    return {
        "result": result,
        "dialogue_contrib": dialogue_contrib,
        "evidence_texts": evidence_texts,
        "schema": reentry_schema,
    }


def _resource_encounter_text(fact):
    """CAP2-F: the fixed mechanical-fact template for one genuinely-delivered resource encounter."""
    text = (
        "Recent public workspace encounter (mechanical fact only -- not a memory, "
        "preference, or belief): a "
        f"{fact['modality']} resource ({fact['resource_class']}/{fact['relative_path']}) "
        f"was delivered to your own model pathway via a {fact['source_action']} action."
    )
    if fact.get("delivered_portion"):   # what part was delivered, so a continuation can pick up there
        text += f" Delivered portion: {fact['delivered_portion']}."
    if fact.get("source_folder"):
        folders = fact["source_folder"]
        folders = ", ".join(folders) if isinstance(folders, list) else folders
        text += (
            f" The owner files it under the folder {folders} (that is the owner's filing structure, "
            "not an identification of anyone or anything in it)."
        )
    return text


def _encounter_resource_in_checks(fact, occasion_checks):
    """DEMONSTRATED DUPLICATION (Lacrimosa acceptance turn, 2026-10-03): the continuity line restating that a
    resource was delivered earlier rode beside this occasion's own read of that same resource.  When a check of
    this occasion read the same item (same collection, same path), the earlier-delivery line is redundant and is
    not offered -- it stays pending, so it is never marked delivered without having been shown."""
    def _key(resource_class, path):
        return (resource_class, (path or "").removesuffix(".json").removesuffix(".md"))
    wanted = _key(fact.get("resource_class"), fact.get("relative_path"))
    for check in occasion_checks or ():
        seen = check.get("inspection") or check.get("claim_witness") or {}
        if seen.get("performed") and _key(seen.get("resource_class"), seen.get("relative_path")) == wanted:
            return True
    return False


def _occasion_evidence_room(hard_contributions, occasion_checks, shares=1):
    """{"tokens", "estimate"}: the room the next check's evidence text may use (workspace_supervisor.
    fit_check_evidence).  Real room is priced by real measurement where available (falling back to the
    conservative cost); measurability by the estimator, exactly as recost_when_shedding judges it.  ``shares``: the checks this occasion may still run, this one included -- the
    remaining room is divided evenly among them, so an early check never leaves a later one starved.

    REPRESENTATION LAW (2026-10-06 waking budget incident).  A room sized for the check being taken
    now is not a room sized for the re-entry that will carry every check taken so far.  Live: two
    read-only checks each fitted to the room left when it was taken; the joined action-record text
    grew 464 -> 926 estimator units while the hard control floor stayed fixed, the second re-entry
    composed at 3476/3456, and the turn died at pass1_reentry with BUDGET_EXCEEDED -- a representation
    failure, not a capacity failure, since the same two checks fit comfortably when their evidence is
    represented boundedly.  The room offered here is therefore also capped by the room the JOINED
    re-entry really has: prior evidence is charged at its real cost, and the total the joined evidence
    may occupy is the budget left after the hard floor, the join slack, and the bound-state line."""
    prior = "\n\n".join(c["rendered"] for c in occasion_checks)
    framing = _occasion_evidence_framing_cost()
    hard_cost = sum(_real_cost(c.rendered_text, c.cost) for c in hard_contributions)
    prior_cost = _joined_evidence_cost(occasion_checks)
    estimate = OCCASION_EVIDENCE_MEASURABLE_LIMIT - (context_budget.estimate_tokens(prior) if prior else 0) - framing
    tokens = _pass1_prompt_budget() - hard_cost - prior_cost - framing
    shares = max(1, shares)
    return {"tokens": max(0, min(tokens, estimate)) // shares, "estimate": max(0, estimate) // shares}


def _occasion_evidence_room_for_final(hard_contributions, occasion_checks, dialogue_contrib):
    """The room the current check may use in the final joined re-entry.

    REPRESENTATION LAW (2026-10-06 waking budget incident).  ``_occasion_evidence_room`` sizes a check
    against the room left when that check is taken, so the SUM of independently-fitted windows is
    fitted to nothing -- and the re-entry carries all of them at once.  Live: two read-only checks
    whose joined action-record text grew 464 -> 926 estimator units while the hard control floor
    stayed fixed; the second re-entry composed at 3476/3456 and the turn died at pass1_reentry with
    BUDGET_EXCEEDED.  The same two checks fit comfortably when each window is sized against the room
    the FINAL re-entry has.  Prior evidence is charged exactly, so the current
    check may use all room that really remains.  A hypothetical later check is
    not charged before it exists; if one is actually requested, it receives
    the then-remaining room.  This preserves the invariant inductively without
    halving an ordinary single read for an unused reservation."""
    framing = _occasion_evidence_framing_cost()
    # The hard floor is priced exactly as the composition will charge it: real measurement where
    # available (recost_by_measurement lowers every atomic contribution before composing), its
    # conservative cost otherwise.  Pricing it by the byte upper bound instead would shrink every
    # window for a floor the re-entry never actually pays.
    hard_cost = sum(_real_cost(c.rendered_text, c.cost) for c in hard_contributions)
    # Recent dialogue is SOFT but its newest pair is PINNED (session_dialogue_window.dialogue_
    # contribution sets minimum_units=1), so that pair can never be evicted and the composition must
    # carry it.  It is therefore charged here -- this is the material the incident's second re-entry
    # could not shed.  Only the pairs ABOVE the pinned minimum are reclaimable.
    pinned = _pinned_dialogue_cost(dialogue_contrib)
    # Prior evidence is priced by the same function the composition uses to charge it (real
    # measurement where available, the conservative byte bound otherwise), exactly as the floor is.
    prior_cost = _joined_evidence_cost(occasion_checks)
    room = _pass1_prompt_budget() - hard_cost - pinned - prior_cost - framing
    # The estimator room keeps the pre-existing per-receipt measurability ceiling: it bounds how
    # large this receipt may grow while staying measurable and is deliberately NOT reduced by the
    # hard floor or prior receipts.  The joined carrier is charged by _joined_evidence_cost, so a
    # prior receipt's conservative byte size does not consume the next receipt's probe ceiling.
    estimate = OCCASION_EVIDENCE_MEASURABLE_LIMIT
    return {"tokens": max(0, room), "estimate": max(0, estimate)}


def _refit_waymark_read(check, max_bytes):
    """A smaller exact projection of one prior Waymark READ of this occasion (a page with fewer whole entries and
    its own truthful continuation, or a shorter fragment), re-rendered from canonical state -- never a slice of
    the delivered text.  None when it was not a completed read or nothing whole fits."""
    facts = check.get("waymark") or {}
    if facts.get("outcome") != "READ":
        return None
    try:
        parsed = waymark.parse_request(facts["request"], facts["spec"])
        reads = _waymark_reads_state()
        if parsed["request"] == waymark.LIST:
            page = waymark.list_waymarks(
                PROVENANCE_DB_DIR, session_id=facts["session_id"], include_historical=parsed["view"] == "all",
                cursor=parsed.get("cursor"), shelf_digest=parsed.get("shelf_digest"), max_bytes=max_bytes)
            if page["more_exists"]:
                reads["list_continuations"].add(
                    f"view={parsed['view']};cursor={page['next_cursor']};shelf_digest={page['shelf_digest']}")
            return page["rendered"]
        text = waymark.render_query_result(
            waymark.inspect_waymark(PROVENANCE_DB_DIR, session_id=facts["session_id"], handle=parsed["handle"]),
            handle=parsed["handle"], projection_cursor=parsed.get("projection_cursor", 0),
            expected_sha256=parsed.get("projection_sha256"), max_bytes=max_bytes)
        next_cursor = text.split("next_projection_cursor=", 1)[1].split(" ", 1)[0]
        if next_cursor != "None":
            digest = text.split("projection_sha256=", 1)[1].split(" ", 1)[0]
            reads["inspect_choices"].add(
                f"handle={parsed['handle']};projection_cursor={next_cursor};projection_sha256={digest}")
        return text
    except waymark.WaymarkError:
        return None


def _refit_prior_check_evidence(check, token_room):
    """Return a truthful, smaller model-facing projection of one prior check.

    Re-fitting is permitted only from the exact structured source retained in
    memory for this occasion.  It never slices rendered prose or invents a
    semantic summary.  The original larger projection has already reached the
    model in the preceding re-entry; this produces the exact bounded projection
    carried forward when the model actually asks for another check.
    """
    room = {"tokens": max(0, token_room), "estimate": OCCASION_EVIDENCE_MEASURABLE_LIMIT}
    try:
        if check.get("kind") == "verify" and check.get("result") is not None:
            rendered = _fit_verify_evidence(
                check["result"], check.get("evidence_presence"), room)
        elif check.get("kind", "").startswith("waymark_"):
            rendered = _refit_waymark_read(check, token_room)
            if rendered is None:
                return None
        elif check.get("evidence_source") is not None:
            import workspace_delivery
            import workspace_supervisor
            source = check["evidence_source"]
            fitted, _info = workspace_supervisor.fit_check_evidence(
                source["boundary_result"], room, source["framing_text"])
            rendered = source["framing_text"] + "\n" + workspace_delivery.render_observation(fitted)
        else:
            return None
    except ConversationDirectionFailure:
        return None
    cost = _real_cost(rendered, context_budget.estimate_tokens(rendered))
    return rendered if cost <= token_room else None


def _rebalance_prior_evidence_for_current(hard_contributions, occasion_checks, dialogue_contrib):
    """Reclaim room only when another evidence-producing check is real.

    A first check receives all room the carrier can safely hold.  If the model
    then requests another check, prior refittable receipts are reduced to at
    most an equal share of the capacity for the checks that now actually
    exist.  This is the reclaim step missing from a prospective ``shares``
    reservation: no hypothetical check starves a one-check occasion, while a
    later real check cannot be made impossible merely because the first used
    the room then available.
    """
    actual_count = len(occasion_checks) + 1
    if actual_count <= 1:
        return _occasion_evidence_room_for_final(hard_contributions, occasion_checks, dialogue_contrib)
    empty_room = _occasion_evidence_room_for_final(hard_contributions, [], dialogue_contrib)["tokens"]
    # _occasion_evidence_framing_cost already covers one receipt separator.
    # Three actual receipts need one additional literal separator; generalize
    # for any future increase in the bounded occasion count.
    extra_joiners = max(0, actual_count - 2) * context_budget.estimate_tokens("\n\n")
    share = max(0, empty_room - extra_joiners) // actual_count
    for check in occasion_checks:
        current_cost = _real_cost(check["rendered"], context_budget.estimate_tokens(check["rendered"]))
        if current_cost <= share:
            continue
        refitted = _refit_prior_check_evidence(check, share)
        if refitted is not None:
            check["rendered"] = refitted
    return _occasion_evidence_room_for_final(hard_contributions, occasion_checks, dialogue_contrib)


def _pinned_dialogue_cost(dialogue_contrib):
    """The cost of the dialogue units the composition can never shed (its pinned newest suffix)."""
    if dialogue_contrib is None:
        return 0
    units = dialogue_contrib.droppable_units
    if units is None:
        return _real_cost(dialogue_contrib.rendered_text, dialogue_contrib.cost)
    minimum = getattr(dialogue_contrib, "minimum_units", 0) or 0
    if minimum <= 0:
        return 0
    pinned_text = dialogue_contrib.render_fn(units[-minimum:])
    return _real_cost(pinned_text, context_budget.estimate_tokens(pinned_text))


def _declare_workspace_claim(*, system_text, dialogue_units, current_input, current_input_role, evidence_texts,
                             evidence_cost=None, occasion_envelope, collections):
    """One structured declaration call (after reply was chosen): (collection, exact item) or None."""
    from conversation_direction import (render_workspace_claim_line, workspace_claim_control_schema,
                                        take_workspace_claim, WORKSPACE_CLAIM_FIELD, WORKSPACE_CLAIM_ITEM_FIELD)
    # COMPACT CONTROL (2026-10-07): the declaration chooses the collection; the exact item name is
    # free text and is written next as a payload of that choice (_generate_pass1_payload).
    schema = workspace_claim_control_schema(collections)
    instruction = (render_workspace_claim_line(collections) + '\n\nAnswer with exactly this JSON and nothing else: '
                   '{"workspace_claim": "<' + " | ".join(["none"] + sorted(collections)) + '>"} '
                   '(when you name a collection, you are asked for the exact item name next)')
    hard_core = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, system_text + "\n\n" + instruction, hard=True)
    hard_human = context_budget.Contribution(context_budget.CURRENT_HUMAN_MESSAGE, current_input, hard=True)
    contributions = [hard_core, hard_human]
    if evidence_texts:
        evidence_contrib = context_budget.Contribution(
            context_budget.ACTION_RECORD_RESULT, "\n\n".join(evidence_texts), hard=True)
        if evidence_cost is not None:
            evidence_contrib.cost = evidence_cost
        contributions.append(evidence_contrib)
    dialogue = dialogue_contribution(list(dialogue_units))
    contributions.append(dialogue)
    claim_budget = context_budget.PASS1_MAX_PROMPT_BUDGET + _turn_echo_reserve()
    recost_when_shedding(contributions, claim_budget)
    result = context_budget.compose_within_budget(contributions, claim_budget)
    if not result.fits:
        result = _compose_with_turn_reserve(contributions, context_budget.PASS1_MAX_PROMPT_BUDGET) or result
    if not result.fits:
        return None
    messages = ([{"role": "system", "content": hard_core.rendered_text}] + dialogue_messages(dialogue)
                + [{"role": current_input_role, "content": current_input}]
                + [_host_evidence_message(t, occasion_envelope) for t in evidence_texts])
    allowance = context_budget.pass1_control_allowance(pass1_control_max_bytes(schema))
    raw = ui_turn_diagnostics.timed_model_call(
        "workspace_claim_declaration", ask_llama_for_json, messages, structured_schema=schema,
        generation_reserve=allowance)
    try:
        chosen = json.loads(raw)
    except (TypeError, ValueError):
        chosen = None
    collection = chosen.get(WORKSPACE_CLAIM_FIELD) if isinstance(chosen, dict) else None
    if isinstance(collection, str) and collection in collections:
        try:
            item = _generate_pass1_payload(WORKSPACE_CLAIM_ITEM_FIELD, {WORKSPACE_CLAIM_FIELD: collection},
                                           messages, result.final_prompt_cost, occasion_envelope)
        except Pass1PayloadFailure as exc:
            exc.collection = collection          # the completed choice the failed item name belonged to
            raise
        _reserve_payload_echo(WORKSPACE_CLAIM_ITEM_FIELD, item)
        raw = json.dumps({WORKSPACE_CLAIM_FIELD: collection, WORKSPACE_CLAIM_ITEM_FIELD: item}, ensure_ascii=False)
    _raw, claim = take_workspace_claim(raw)
    return claim


def _claim_resolution(claim):
    """(workspace paths, exact target status, normalized path) for a declared (collection, item) -- mechanical."""
    import workspace_capability as wc
    paths = wc.WorkspacePaths.production_defaults()
    status, normalized = wc.classify_target(paths, claim[0], claim[1])
    return paths, status, normalized


def _claim_already_witnessed(claim, occasion_checks):
    """Witnessed in this occasion: the exact item was read; or, when no item has exactly that name, the
    collection was listed.  Purely by identity -- never by what the reply says."""
    import workspace_capability as wc
    _paths, status, normalized = _claim_resolution(claim)
    for check in occasion_checks:
        seen = check.get("inspection") or check.get("claim_witness") or {}
        if seen.get("resource_class") != claim[0] or not seen.get("performed"):
            continue
        if status == wc.TARGET_RESOLVED and seen.get("action") == wc.READ and (
                (seen.get("relative_path") or "").removesuffix(".md").removesuffix(".json")
                == normalized.removesuffix(".md").removesuffix(".json")):
            return True
        if status != wc.TARGET_RESOLVED and seen.get("action") == wc.LIST:
            return True
    return False


def _claim_witness(claim, raw_act_only, source_event_id, session_id, evidence_room=None):
    import workspace_capability as wc
    import workspace_delivery
    import workspace_direction as wd
    paths, status, normalized = _claim_resolution(claim)
    collection, item = claim
    exact = status == wc.TARGET_RESOLVED
    # Auditability (non-contamination repair): the exact declared target and the turn's source event, beside the
    # existing operational facts -- a locator, as the workspace action log already records relative paths.
    log = {"ts": int(time.time()), "family": "speech_claim_witness",
           "facts": {"collection": collection, "exact_item_exists": exact},
           "declared_target": {"collection": collection, "item": item}, "source_event_id": source_event_id,
           "decisions": [], "inspected": exact}
    if not exact:
        # NON-CONTAMINATION LAW (live defect 2026-10-03): a witness supplies only the minimum evidence that establishes
        # or rejects the declared claim.  An exact target that does not resolve is rejected by that fact alone: nothing
        # in the collection is read or listed -- no sibling names, openings, summaries or nearby items -- so the
        # witness can never create the subject matter it checks.
        witness_gate._log(PROVENANCE_DB_DIR, log)
        rendered = "\n".join([
            "Host check for the claim you declared (host-delivered, read-only; it changed nothing; not anyone's words): "
            f'your answer was to state the current state of "{item}" in {collection}.',
            (f'The exact declared target "{item}" was not found in {collection}.' if item else
             f"No exact item was named in {collection}."),
            f"No other contents of {collection} were read or supplied."])
        return {"kind": "claim_witness", "query": None, "result": None, "raw_act": raw_act_only, "rendered": rendered,
                "claim_witness": {"resource_class": collection, "action": None, "relative_path": None,
                                  "performed": False, "exact_item_exists": False}}
    action = {"resource_class": collection, "action": wc.READ, "relative_path": normalized, "content": ""}
    result, performed = wd.execute_workspace_action(paths, action, derive_stable_id("actor", "clark"),
                                                    session_id=session_id, source_event_id=source_event_id)
    framing = "\n".join([
        "Read for the claim you declared (host-delivered, read-only; it changed nothing; not anyone's words): your "
        f'answer was to state the current state of "{item}" in {collection}, which had not been read in this occasion.',
        f'"{item}" resolved to an item path in {collection}; its read follows (witness evidence for this claim -- '
        "the delivery status below states whether it is the whole item)"])
    if evidence_room is None:
        fitted, _info = workspace_delivery.fit_boundary_result(result, CLAIM_WITNESS_ROOM)
    else:
        import workspace_supervisor as _ws_fit
        fitted, _info = _ws_fit.fit_check_evidence(result, evidence_room, framing)
    rendered = "\n".join([framing, workspace_delivery.render_observation(fitted)])
    witness_gate._log(PROVENANCE_DB_DIR, log)
    # MEANINGFUL-ACCESS repair: what the witness delivered is a real (possibly partial) reading of the item -- its open
    # reading continues through inspect_workspace ("next"), and its encounter is recorded once the turn is canonical.
    import workspace_supervisor as _ws
    _ws._track_open_text_read(session_id, action, performed, fitted)
    encounter = _ws.delivered_read_encounter(action, result, fitted, _info, performed=performed,
                                             pathway="speech_claim_witness")
    return {"kind": "claim_witness", "query": None, "result": None, "raw_act": raw_act_only, "rendered": rendered,
            "claim_witness": {"resource_class": collection, "action": action["action"],
                              "relative_path": action["relative_path"], "performed": bool(performed),
                              "exact_item_exists": exact, "encounter": encounter}}


# NON-HUMAN OCCASION ENVELOPE (candidate; provider/context-serialization boundary).  An occasion in which no human
# spoke (timer-only Pulse, receipt re-entry) is represented to the substrate as an explicit host envelope: exact
# source, human_speech = none, no authority introduced, and the exact host text delimited verbatim.  It rides in the
# input slot the Llama 3.1 template opens a generation after (user), because a trailing "system" message is collated
# into the leading system block and a bare "tool" (ipython) turn was answered as conversation (P4a receipt ignored
# 3/3; Clark's own journal entry attributed to the human).  The envelope states provenance only -- no meaning, no
# instruction -- and never attributes words or authority to anyone.  Human-started turns are unchanged.
OCCASION_ENVELOPE_SOURCES = {
    "pulse": "ANAXI Pulse timer (timer only; no human message and no external event initiated this occasion)",
    "action_receipt": "ANAXI host: canonical receipt of an outward act chosen earlier in this occasion",
}


def render_occasion_envelope(text, source, part="occasion"):
    return ("ANAXI HOST OCCASION -- not speech; no human wrote this message.\n"
            f"occasion_source: {source}\n"
            f"part: {part}\n"
            "human_speech: none\n"
            "authority_introduced: none (no human request or permission is carried by this occasion)\n"
            # CONTENT provenance, in the one shared vocabulary: this text is a host
            # mechanical record, so its actor relation is HOST -- stated rather than left
            # to the prose header to convey.
            f"actor relation of this occasion: {epistemic_status.HOST}\n"
            "content (exact host text):\n<<<\n" + text + "\n>>>")


def _delivery_identity(encounter):
    # What was delivered, not how the item was named: a replay read one journal entry as "entry-<id>.json" and
    # again as "entry-<id>", delivering the same bytes twice.
    return (encounter.get("resource_class"), encounter.get("delivered_portion"),
            encounter.get("content_sha256")) if isinstance(encounter, dict) else None


def _delivered_earlier_in_occasion(occasion_checks, encounter):
    """Was exactly this delivered window (same collection, same portion, byte-identical content) already delivered
    by an earlier check of this occasion?"""
    identity = _delivery_identity(encounter)
    if identity is None:
        return False
    return any(_delivery_identity((c.get("inspection") or c.get("claim_witness") or {}).get("encounter")) == identity
               for c in occasion_checks)


def _repeated_delivery_record(inspection):
    import workspace_supervisor
    target = f"/{inspection['relative_path']}" if inspection.get("relative_path") else ""
    return "\n".join([
        workspace_supervisor.INSPECTION_HEAD,
        f"action taken: {inspection['action']} on {inspection['resource_class']}{target}",
        "Its delivered window is byte-identical to one this occasion already delivered (same collection, same "
        "portion, identical content): that earlier delivery stands above and is not repeated here.",
    ])


def _staged_encounters(occasion_checks):
    """The resource encounters this occasion's checks positively established -- each performed substantive inspection
    or witness read whose exact bounded result reached the subject (LIST / metadata / failed / withheld / not-found
    carry none; image views stage their own on the vision path).  Staged with the turn and recorded exactly once when it
    is canonical, on the live commit or by crash replay (native_provenance_writer post_commit_provenance)."""
    staged = []
    for check in occasion_checks:
        encounter = (check.get("inspection") or check.get("claim_witness") or {}).get("encounter")
        if encounter:
            staged.append(dict(encounter, target_model_pathway=MODEL, genuinely_delivered=True))
    return staged


def _post_commit_kwargs(occasion_checks, extra_encounters=()):
    """Writer kwargs carrying this turn's positively established delivery provenance (omitted when there is none)."""
    staged = {"resource_encounters": list(extra_encounters or []) + _staged_encounters(occasion_checks),
              "retrieval_delivery": retrieval_delivery.take_manifest()}
    return {"post_commit_provenance": staged} if (staged["resource_encounters"] or staged["retrieval_delivery"]) else {}


def _hless_session_family_view(session_id):
    """The Family view of an H-less occasion's session: its one authenticated human binding, re-validated by the same
    per-call law a human turn's authority passes (human_session_binding.validate_human_input_authority), and required
    to be the single unambiguous binding audience the H-less turn is recorded under.  None otherwise (fail closed)."""
    db_path = f"{PROVENANCE_DB_DIR}/anaxi_provenance.db"
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        authority = human_session_binding.resolve_bound_human_for_session(conn, session_id)
        if authority is None or authority.visibility_scope is None:
            return None
        if family_membership.session_binding_audience(conn, session_id) != (
                authority.authenticated_actor_id, authority.visibility_scope):
            return None
        if not human_session_binding.validate_human_input_authority(conn, authority, session_id=session_id):
            return None
        return family_membership.family_session_view(
            conn, principal_actor_id=authority.authenticated_actor_id, visibility_scope=authority.visibility_scope)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _occasion_provenance_kwargs(pulse_occasion, occasion_text, session_id):
    """Writer kwargs for an H-less non-human occasion turn: its exact source and host text, plus the visibility
    scope of the one real human binding of the session it ran in (none for an unbound or unscoped session)."""
    kwargs = {"occasion_source": pulse_occasion["kind"], "occasion_input": occasion_text}
    db_path = f"{PROVENANCE_DB_DIR}/anaxi_provenance.db"
    if os.path.exists(db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            audience = family_membership.session_binding_audience(conn, session_id)
        finally:
            conn.close()
        if audience is not None and audience[1] is not None:
            kwargs["visibility_scope"] = audience[1]
    return kwargs


def _host_evidence_message(text, occasion_envelope=None):
    """Host-delivered evidence beside the turn: the established carrier in human-started turns; inside a
    non-human occasion, the same envelope as the occasion itself."""
    if occasion_envelope:
        return {"role": "user", "content": render_occasion_envelope(text, occasion_envelope, part="host evidence")}
    return external_data_message(text)


# The exact room the offered Waymark line takes in every Pass-1-family prompt of a turn that offers it (the
# line and its joining newline, at the estimator's authoritative upper bound): admitted above the ordinary
# Pass-1 budget so nothing that fit before is displaced by it.
WAYMARK_AFFORDANCE_RESERVE = context_budget.estimate_tokens("\n" + WAYMARK_AFFORDANCE_TEXT)

# Chronological browsing is additive to the pre-existing search/read menu.
# Reserve the new line above the ordinary Pass-1 budget so merely exposing the
# capability cannot displace a full lawful historical query or other existing
# capacity.  The whole-occasion proof charges the same room in every Pass-1
# branch and keeps the 46,080-token serving ceiling unchanged.
HISTORY_BROWSE_AFFORDANCE_RESERVE = context_budget.estimate_tokens(
    "\n" + HISTORICAL_BROWSE_AFFORDANCE_TEXT)


def _waymark_session_audience(session_id):
    """Read-only: the (principal, scope) owning this session's Waymark shelf, or None (unbound/ambiguous)."""
    db_path = f"{PROVENANCE_DB_DIR}/anaxi_provenance.db"
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return family_membership.session_binding_audience(conn, session_id)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _waymark_host_evidence_text(text, occasion_envelope=None):
    """The exact Waymark text placed in the provider message.

    Unlike older evidence lanes, Waymark prices this final carrier text in
    the compositor.  That keeps non-human occasion envelope bytes inside the
    authoritative bound rather than appending them after admission.
    """
    rendered = _host_evidence_message(text, occasion_envelope)["content"]
    if occasion_envelope:
        overhead = len(rendered.encode("utf-8")) - len(text.encode("utf-8"))
        if overhead > context_budget.WAYMARK_HOST_ENVELOPE_MAX_TOKENS:
            raise ConversationDirectionFailure("waymark_carriage", context_budget.BUDGET_EXCEEDED)
    return rendered


def _retrieved_history_ledger_path():
    """The canonical ledger the retrieved-history seam reads.

    ONE resolver, used by BOTH the contribution's pricing and the seam that sends it.  Pricing the
    native form against one ledger and delivering it from another would make the budget decision and
    the delivered prompt disagree -- the exact seam this package exists to close -- so the two must
    never resolve the path independently."""
    return os.path.join(PROVENANCE_DB_DIR, "anaxi_provenance.db")


def _native_retrieved_history_messages(contribution, *, viewer_human_actor_id, provenance_db_path,
                                       person_annotations=None, source_paths=None):
    """The surviving ``RETRIEVED_HISTORY`` contribution -> native role-tagged provider messages, or ``[]``.

    THE BUDGET IS THE AUTHORITY ON WHAT ARRIVES, AND IT IS NOT RE-DERIVED HERE.  ``compose_within_budget``
    already decided which delivery units fit, trimming whole episodes worst-first; this reads back
    exactly those survivors via ``hippocampus_retrieval.delivered_items`` and reconstructs them from
    the canonical ledger.  It never re-ranks, never re-selects, and never widens what survived -- if
    the compositor emptied the contribution, the actor sees nothing, which is the correct outcome and
    not a failure.

    THE ACCOUNTS MUST MATCH.  The compositor priced this contribution on the legacy rendered block,
    which is the text this call replaces.  The native form is a different byte count, so it is bounded
    to the SAME accounted cost the compositor reserved; otherwise this call could silently push the
    assembled prompt past ``PASS2_MAX_PROMPT_BUDGET`` after the budget check already passed.  Bounding
    drops the OLDEST whole turn groups first and records every drop in the sidecar, so what the model
    sees and what the ledger holds stay reconcilable.

    FAILURE IS CONTAINED, NOT PROPAGATED.  A missing ledger, a drifted digest or an ambiguous linkage
    makes reconstruction withhold units; a broken projection contract must not take down a live turn.
    Such a turn proceeds with NO retrieved history rather than with unverified text in the prompt."""
    if contribution is None or not contribution.droppable_units:
        return []

    try:
        items = hippocampus_retrieval.delivered_items(contribution.droppable_units)
        if not items:
            return []
        reconstruction = historical_unit_reconstruction.reconstruct(
            items, provenance_db_path,
            relational_db_path=getattr(source_paths, "relational_db_path", None),
            historical_jsonl_path=getattr(source_paths, "historical_jsonl_path", None),
        )
        projection = relational_history_projection.project(
            reconstruction, viewer_human_actor_id=viewer_human_actor_id,
            person_annotations=person_annotations)
        bounded = relational_history_projection.bound_projection(
            projection, contribution.cost, cost_fn=getattr(contribution, "cost_fn", None))
        if not bounded.messages:
            return []
        relational_history_projection.verify_projection(
            bounded,
            [{"role": "system", "content": ""}] + [{"role": m.role, "content": m.content}
                                                   for m in bounded.messages]
            + [{"role": "user", "content": ""}],
            {"role": "user", "content": ""},
        )
    except Exception as exc:  # noqa: BLE001 - a retrieval defect must not end a live waking turn
        ui_turn_diagnostics.record_pass2_native_history_error(type(exc).__name__, str(exc))
        return []

    ui_turn_diagnostics.record_pass2_native_history(
        bound=bounded.sidecar["char_budget"], total=bounded.sidecar["char_total"],
        region=bounded.sidecar["region"] is not None,
        turns_dropped=len(bounded.sidecar["turns_dropped_by_bound"]),
        records_dropped=len(bounded.sidecar["records_dropped_by_bound"]),
        withheld=len(bounded.sidecar["withheld"]),
    )
    # The manifest must state what the model saw.  A unit can survive composition and still be
    # withheld here (its canonical row is absent or drifted), so the seam reports its own delivered
    # set rather than letting the budget's admission stand in for delivery.  The block and the
    # per-item lines are the NATIVE text the provider receives, so the manifest's digests describe
    # the delivered form rather than the legacy rendered block it replaced.
    delivered_unit_ids = {m.unit_id for m in bounded.messages}
    delivered_units = [u for u in reconstruction.units if u.unit_id in delivered_unit_ids]
    native_block = relational_history_projection.native_history_text(bounded)
    delivered_by_unit = {u.unit_id: u for u in delivered_units}
    messages_by_item = {}
    for index, message in enumerate(bounded.messages):
        unit = delivered_by_unit[message.unit_id]
        messages_by_item[unit.item_id] = {
            "role": message.role, "index": index, "content": message.content,
        }
    retrieval_delivery.note_native_delivery(
        (u.item_id for u in delivered_units), block=native_block, messages_by_item=messages_by_item)
    return [{"role": m.role, "content": m.content} for m in bounded.messages]


def _serialized_waking(fn):
    """Every waking turn holds the one process-wide pipeline lock, so a
    Mac-originated turn and a Caret-originated occasion can never race."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with waking_pipeline_lock.LOCK:
            return fn(*args, **kwargs)
    return wrapper


@_serialized_waking
@_with_pass1_turn_scope
def run_waking_turn(
    orch: AnaxiOrchestrator, prompt: str, interaction_mode: str = None, human_input_authority=None,
    existing_human_input_event_id: str = None, wtr0_recovery_id: str = None,
    caret_occasion: dict = None, pulse_occasion: dict = None,
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
    fabricating H. `wtr0_recovery_id` is accepted only with that exact
    existing H and is carried through durable staging/persistence so the
    writer can derive any post-restart cross-session allowance from the
    authoritative recovery ledger, never from this caller's assertion."""
    if wtr0_recovery_id is not None and existing_human_input_event_id is None:
        raise ValueError("WTR0 recovery provenance requires an exact existing human input event")
    retrieval_delivery.reset()   # positive retrieval-delivery accounting for this turn starts empty
    provider_boundary_receipt.reset()
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
    if pulse_occasion is not None:
        if caret_occasion is not None or human_input_authority is not None or existing_human_input_event_id is not None:
            raise ValueError("a Pulse occasion carries no human input and no Caret message")
        if pulse_occasion.get("kind") not in (PULSE_OCCASION_TIMING, PULSE_OCCASION_ACTION_RECEIPT):
            raise ValueError(f"unknown Pulse occasion kind {pulse_occasion.get('kind')!r}")
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

    # PULSE V0 OCCASION: `pulse_occasion` is one lawful inference occasion supplied by the Pulse layer
    # (pulse.py) -- a timing occasion or the one receipt re-entry after an outward act.  Nobody typed
    # anything: the turn's input is the host's rendering of truthful present circumstances, labelled as
    # host timing and never as anyone's words.  It supplies no conclusion, no task and no obligation to
    # act or speak; no human authority is bound and the signal gate is never consulted.  A Pulse occasion
    # is not a wake mechanism -- pulse.py never offers one while Clark is asleep.
    if pulse_occasion is not None:
        interaction_mode = CONVERSATION_MODE
        prompt = pulse_occasion["rendered"]
    # ROLE SERIALIZATION (candidate provider translation): an occasion with no human words never rides in
    # the human's role.  The Llama 3.1 template renders "tool" in place as its environment turn (ipython)
    # and then opens the assistant turn; a trailing "system" message is collated into the leading system
    # block instead (measured 2026-10-02).  Same precedent as the control-scaffold attribution repair.
    current_input_role = "user"
    occasion_envelope = (OCCASION_ENVELOPE_SOURCES[pulse_occasion["kind"]]
                         if pulse_occasion is not None and CANDIDATE_PROTOCOL_MECHANICS else None)
    if occasion_envelope:
        prompt = render_occasion_envelope(prompt, occasion_envelope)

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
            # HUMAN INPUT ADMISSION (2026-10-08): an input the waking path cannot carry never becomes H.
            _admit_human_input_at_ingress(prompt)
            human_input_event_id = human_session_binding.record_human_waking_input(
                PROVENANCE_DB_DIR, pipeline_key=PIPELINE_KEY, authority=human_input_authority,
                message=human_message_text, occurred_at=int(datetime.now(timezone.utc).timestamp()),
                source_inbound_event_id=caret_occasion["event_id"] if caret_occasion is not None else None,
            )
        # else: no valid authority -- proceed exactly as an ordinary
        # unbound/internal call, no human attribution, no error.

    if existing_human_input_event_id is not None and human_input_event_id is None:
        raise ValueError("existing human input continuation requires valid current human authority")
    if human_input_event_id is not None and _TURN_ECHO.get() is not None:
        _TURN_ECHO.get()["human_admitted"] = True     # this H's input is carried by every pass of the turn

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
    elif human_input_event_id is None and caret_occasion is None:
        # H-LESS SESSION-BOUND WAKING (Pulse, receipt re-entry): no human spoke, but the occasion runs in a session
        # whose audience is already authenticated.  Its Family view is that binding's -- the same principal/scope the
        # H-less turn is recorded under -- re-validated now exactly as a human turn's authority is.  No H is created
        # and nothing is attributed to a human; an absent, ambiguous, revoked or inactive binding stays None.
        _family_view = _hless_session_family_view(session_id)

    ui_turn_diagnostics.record_human_input_event_id(human_input_event_id)
    prepared = ui_turn_diagnostics.timed_stage(
        "context_prepare", orch.prepare_context, USER_ID, prompt)
    resolved_interaction_mode = _resolve_interaction_mode(interaction_mode)
    temporal_text = ''
    if resolved_interaction_mode == CONVERSATION_MODE:
        temporal_text = temporal_grounding.snapshot(
            os.path.join(PROVENANCE_DB_DIR, 'anaxi_provenance.db'), human_input_event_id,
            process_started=_temporal_process_started,
            lifecycle_notice=session_id not in _temporal_lifecycle_sessions,
            diagnostics_path=os.path.join(PROVENANCE_DB_DIR, ui_turn_diagnostics.DEFAULT_LOG_PATH),
        )

    if caret_occasion is not None or pulse_occasion is not None:
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
    consolidation_target_event_ids = []
    if os.path.exists(provenance_db_path):
        dialogue_conn = sqlite3.connect(readonly_db.readonly_sqlite_uri(provenance_db_path), uri=True)
        try:
            if caret_correspondent_view is not None and resolved_interaction_mode == CONVERSATION_MODE:
                # A non-principal correspondent Clark has standing with: his continuity with exactly
                # that source (from the establishing exchange on), never anyone else's.
                dialogue_window = build_correspondent_dialogue_window(
                    dialogue_conn, caret_correspondent_view["key"])
                consolidation_target_event_ids = target_event_ids(dialogue_window)
            elif (
                _family_feature_active and _family_view is None
                and resolved_interaction_mode == CONVERSATION_MODE
            ):
                # Once family mode exists, an identity-less waking call
                # gets no dialogue continuity at all.
                dialogue_window = []
            elif (human_input_event_id is None and _family_view is not None
                  and resolved_interaction_mode == CONVERSATION_MODE):
                # H-less session-bound occasion: the bound principal's own canonical window, up to now.
                dialogue_window = build_occasion_dialogue_window(
                    dialogue_conn, principal_actor_id=_family_view["principal_actor_id"], pipeline_key=PIPELINE_KEY,
                    viewer_visibility_scope=_family_view["visibility_scope"],
                    viewer_principal_actor_id=_family_view["principal_actor_id"],
                    active_family_principal_ids=_family_view["active_family_principal_ids"],
                )
                consolidation_target_event_ids = target_event_ids(dialogue_window)
            elif human_input_event_id is not None and resolved_interaction_mode == CONVERSATION_MODE:
                if _family_view is not None:
                    dialogue_window = build_authenticated_dialogue_window(
                        dialogue_conn, human_input_event_id,
                        viewer_visibility_scope=_family_view["visibility_scope"],
                        viewer_principal_actor_id=_family_view["principal_actor_id"],
                        active_family_principal_ids=_family_view["active_family_principal_ids"],
                    )
                    consolidation_target_event_ids = target_event_ids(dialogue_window)
                else:
                    dialogue_window = build_authenticated_dialogue_window(
                        dialogue_conn, human_input_event_id)
                    consolidation_target_event_ids = target_event_ids(dialogue_window)
            else:
                dialogue_window = build_session_dialogue_window(
                    dialogue_conn, session_id, STAGING_PATH)
                consolidation_target_event_ids = target_event_ids(dialogue_window)
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
    # SUBJECT-DIRECTED CONSOLIDATION V0: Clark's own typed consolidation
    # choice for this turn, if any -- same None-until-both-passes-succeed
    # discipline as its pending siblings above, for the same reason: a
    # contained/failed turn must never record a consolidation act Clark
    # never actually reached in a successfully released turn.
    pending_consolidation_request = CONSOLIDATION_REQUEST_NONE
    pending_consolidation_target = None
    pending_consolidation_interpretation = ""
    pending_consolidation_cues = []
    pending_consolidation_prior_record_id = None
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
    # CANONICAL ACTION VERIFICATION V0: same-turn, read-only mechanical
    # observation.  It is computed only after a valid typed Pass-1 choice,
    # carried into that turn's Pass 2, and atomically bound to the waking event.
    pending_action_verification_query = None
    live_action_verification_result = None
    pass2_action_record_text = None
    pass2_action_record_cost = None
    pass2_history_evidence_cost = None
    history_evidence_manifest = None
    provider_boundary_history_receipt = None
    pending_waymark_action = None
    waymark_resolution = None
    pass2_waymark_read_checks = []
    waymark_available = False
    pass2_waymark_query_text = None
    pending_waymark_surfaces = []
    delivered_waymark_surfaces = []
    occasion_checks = []              # PULSE V0: this occasion's read-only checks, in order
    prior_occasion_verifications = []
    witness_gates = []                # WITNESS GATE V0: gates this occasion ran (result data only)
    acknowledged_receipt_statuses = []
    declared_claims = []              # speech-boundary declarations, one per Pass-1 output (None = none)
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
        # PULSE V0: the one inference that interprets an outward act's receipt starts no new act.
        receipt_reentry = pulse_occasion is not None and pulse_occasion["kind"] == PULSE_OCCASION_ACTION_RECEIPT
        if receipt_reentry:
            authorized_discord_destinations = []
        workspace_action_available = _workspace_action_available(
            _family_feature_active, _family_view,
        ) and caret_occasion is None and not receipt_reentry   # untrusted correspondence never drives a workspace action
        # Historical continuity is a private/read-only waking capability.  A
        # non-principal correspondent never receives it; once Family is active,
        # an identity-less occasion cannot receive any history and is not offered
        # a misleading action.  The actual item filter is still authoritative.
        history_action_available = (
            caret_occasion is None and not receipt_reentry
            and (not _family_feature_active or _family_view is not None)
            and getattr(orch, "hippocampus_paths", None) is not None
        )
        history_paths = (
            historical_continuity_access.HistoricalPaths.from_hippocampus_paths(orch.hippocampus_paths)
            if history_action_available else None
        )
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
        # DESTINATION CHOICE (2026-09-26): the destinations offered this turn -- exactly those the menu lists,
        # plus the correspondents' surfaces where Clark may write first -- become a closed grammar choice.
        pass1_schema = pass1_schema_with_destination_choices(
            pass1_schema,
            [d["destination_id"] for d in authorized_discord_destinations]
            + [dest for c in known_correspondents for dest in c.get("initiation_destinations", [])],
        )
        pass1_schema = pass1_schema_with_action_verification(pass1_schema)
        # WAYMARK V0: offered only where a Waymark can lawfully exist -- a session with one real human binding
        # (its principal owns the shelf) -- and never to untrusted correspondence or the receipt re-entry
        # (which starts no new act).  The offering line has its own room above the ordinary Pass-1 budget.
        waymark_available = (caret_occasion is None and not receipt_reentry
                             and _waymark_session_audience(session_id) is not None)
        if waymark_available:
            pass1_schema = pass1_schema_with_waymark(pass1_schema)
            _TURN_ECHO.get()["waymark_affordance"] = WAYMARK_AFFORDANCE_RESERVE
        if CANDIDATE_PROTOCOL_MECHANICS:
            pass1_schema = pass1_schema_with_workspace_split(
                pass1_schema_with_explicit_reply_choice(pass1_schema), workspace_available=workspace_action_available)
        history_schema_base = pass1_schema
        if history_action_available:
            pass1_schema = pass1_schema_with_historical_continuity(pass1_schema)
            _TURN_ECHO.get()["history_browse_affordance"] = HISTORY_BROWSE_AFFORDANCE_RESERVE
        if receipt_reentry:
            pass1_schema = pass1_schema_receipt_reentry(pass1_schema)
            if CANDIDATE_PROTOCOL_MECHANICS and pulse_occasion.get("canonical_status"):
                # The exact canonical status the host already knows, acknowledged before any choice.
                pass1_schema = pass1_schema_with_receipt_acknowledgement(pass1_schema, pulse_occasion["canonical_status"])
        # COMPACT PASS-1 CONTROL (post-deploy no-reply incident, 2026-10-07): the provider receives the
        # closed control grammar; every free text field the chosen act needs is written afterwards as its
        # own payload (_bind_pass1_payloads).  The turn's control allowance is the truthful maximum over
        # every control act this turn can request, and every Pass-1 composition of the turn admits its
        # prompt against what that allowance leaves.
        pass1_pending_send_refs = [p.get("send_turn_event_id") for p in pending_sends]
        pass1_selection_grammars_turn = pass1_selection_grammars(
            pass1_schema, correspondent_refs=[c.get("ref") for c in known_correspondents])
        pass1_schema = pass1_control_schema(pass1_schema, pending_send_refs=pass1_pending_send_refs)
        pass1_control_allowance = context_budget.pass1_control_allowance(pass1_control_max_bytes(pass1_schema))
        _PASS1_TURN_PROMPT_BUDGET.set(context_budget.pass1_prompt_budget(pass1_control_allowance))
        consolidation_evidence = []
        if consolidation_target_event_ids:
            _consolidation_conn = consolidation.open_connection(PROVENANCE_DB_DIR)
            try:
                consolidation_evidence = consolidation.evidence_for_targets(
                    _consolidation_conn, consolidation_target_event_ids,
                    author_actor_id=CLARK_ACTOR_ID,
                )
            finally:
                _consolidation_conn.close()
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
            explicit_reply_choice=CANDIDATE_PROTOCOL_MECHANICS,
            historical_available=history_action_available,
            waymark_available=waymark_available,
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
            _pass1_menu_calibration(), _PASS1_MENU_CALIBRATED_COST,
        )
        pass1_soft_dialogue = dialogue_contribution(
            pass1_dialogue_units,
            target_event_ids=consolidation_target_event_ids,
            consolidation_evidence=consolidation_evidence,
        )
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
        # CAP2-F ON AN H-LESS OCCASION (2026-10-08): the pending resource encounter reaches the next waking
        # turn only through Pass 2, and a Pulse in which Clark stays silent never composes Pass 2 -- so the
        # one existing next-turn continuity of what he last opened never reached a Pulse at all (the 15:59
        # Pulse had no trace of what the 15:27 Pulse read).  On an H-less occasion Pass 1 is the whole
        # choice, so the same fixed mechanical fact is offered there too, SOFT, under the same law.
        if pulse_occasion is not None and pending_resource_encounter is not None:
            pass1_contributions.append(context_budget.Contribution(
                context_budget.RESOURCE_ENCOUNTER, _resource_encounter_text(pending_resource_encounter["fact"]),
                hard=False,
            ))
        recost_when_shedding(
            pass1_contributions, _pass1_prompt_budget(), structured_schema=pass1_schema,
        )
        pass1_result = ui_turn_diagnostics.timed_stage(
            "pass1_context_composition", context_budget.compose_within_budget,
            pass1_contributions, _pass1_prompt_budget())
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
                pass1_result, pass1_contributions, _pass1_prompt_budget(),
                pass1_hard_human, base_current_user_message, structured_schema=pass1_schema,
            )
        if not pass1_result.fits:
            # HUMAN INPUT ADMISSION (2026-10-08): this H was admitted at ingress, so the waking path carries
            # it -- above the ordinary budget by at most its admitted capacity, here and in every later
            # Pass-1-family composition of the turn.
            _reserved = _compose_with_turn_reserve(pass1_contributions, _pass1_prompt_budget())
            if _reserved is not None and _reserved.fits:
                pass1_result = _reserved
                _TURN_ECHO.get()["human_extended"] = True
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
        # Target handles and their authoritative prior-act evidence are
        # costed and trimmed as part of the SAME whole-pair contribution.
        # Therefore no handle can survive after the dialogue episode it
        # names was dropped, and no delivered episode lacks its handle.
        pass1_consolidation_state_text = dialogue_consolidation_state(pass1_soft_dialogue)
        consolidation_target_event_ids = dialogue_target_event_ids(pass1_soft_dialogue)
        pass1_search_choices_contrib = pass1_result.included_kind(context_budget.PASS1_SEARCH_CHOICES)
        pass1_open_page_contrib = pass1_result.included_kind(context_budget.PASS1_OPEN_PAGE)
        # What survived of the CAP2-F fact rides in the system message; every re-entry reuses that message,
        # so from here on it is part of the occasion's Pass-1 floor and is charged as such.
        pass1_encounter_contrib = pass1_result.included_kind(context_budget.RESOURCE_ENCOUNTER)
        pass1_encounter_text = pass1_encounter_contrib.rendered_text if pass1_encounter_contrib is not None else ""
        pass1_hard_floor = [pass1_hard_core, pass1_hard_human, pass1_hard_mechanical, pass1_hard_menu]
        if pass1_encounter_text:
            _shown_encounter = context_budget.Contribution(
                context_budget.RESOURCE_ENCOUNTER, pass1_encounter_text, hard=True)
            _shown_encounter.cost = pass1_encounter_contrib.cost
            pass1_hard_floor.append(_shown_encounter)
        pass1_delivered_resource_encounter_event_id = (
            pending_resource_encounter["event_id"] if pass1_encounter_text else None)

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
                    + (("\n\n" + pass1_consolidation_state_text)
                       if pass1_consolidation_state_text else "")
                    + (("\n\n" + pass1_encounter_text) if pass1_encounter_text else "")
                ),
            }]
            + pass1_dialogue_final
            + ([external_data_message(pass1_search_choices_contrib.rendered_text)]
               if pass1_search_choices_contrib is not None and pass1_search_choices_contrib.rendered_text else [])
            + ([external_data_message(pass1_open_page_contrib.rendered_text)]
               if pass1_open_page_contrib is not None and pass1_open_page_contrib.rendered_text else [])
            + [{"role": current_input_role, "content": base_current_user_message}]
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
                generation_reserve=pass1_control_allowance,
            )  # exactly one Pass-1 control call; no retry
            # Payloads of the chosen act, written beside the same admitted prompt; selection alone
            # executes nothing, and nothing below runs unless every payload completed.
            pass1_payload_failures = []
            raw1 = _bind_pass1_payloads(raw1, pass1_schema, pass1_selection_grammars_turn, pass1_messages,
                                        pass1_result.final_prompt_cost, occasion_envelope,
                                        payload_failures=pass1_payload_failures)
        except (context_budget.IncompleteCompletionError, Pass1PayloadFailure) as exc:
            raw1 = getattr(exc, "partial_output", None)   # failure preview only; never validated
            raw_act_only = None
            validated_act, failure1 = None, exc.failure_code
        else:
            # OWC6-O1: raw `act` only -- extracted here so the durable
            # trace can record it regardless of which branch below is
            # taken. OWC7-S1: there is no free-form `content` field left
            # in the schema at all for this to be tempted to also extract.
            raw_act_only = conversation_direction_trace.extract_raw_act(raw1)
            raw1, _ws_mode = _candidate_translate(raw1, acknowledged_receipt_statuses, declared_claims)
            validated_act, failure1 = validate_pass1_conversation_act(raw1)
            if failure1 in INCOMPLETE_OUTWARD_REQUEST_FAILURES:
                repaired_raw, dropped_requests = drop_incomplete_outward_requests(raw1, failure1)
                if repaired_raw is not None:
                    repaired_act, repaired_failure = validate_pass1_conversation_act(repaired_raw)
                    if repaired_failure is None:
                        validated_act, failure1 = dict(repaired_act, dropped_incomplete_requests=tuple(dropped_requests)), None
            if validated_act is not None and _ws_mode:
                validated_act = dict(validated_act, workspace_mode=_ws_mode)
            if validated_act is not None and declared_claims and declared_claims[-1] is not None:
                validated_act = dict(validated_act, workspace_claim=declared_claims[-1])
            if validated_act is not None and pass1_payload_failures:
                validated_act = dict(validated_act, payload_failures=tuple(pass1_payload_failures))
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

        # PULSE V0 BOUNDED RECOVERY: a read-only verify_action is executed HERE, and the subject then
        # re-enters Pass 1 with the exact canonical result beside the turn it answers.  The re-entry is
        # an ordinary Pass 1 (same grammar, same validator): answer, ask, stay silent, take an authorized
        # act, or -- while the occasion's bound allows -- choose one more read-only check.  The host
        # executes and reports; it never decides which choice fits.  At MAX_READ_ONLY_CHECKS_PER_OCCASION
        # the grammar no longer offers verify_action or use_workspace, so no third check can occur and
        # the loop cannot recurse.  Each check is recomputed from the authorities at the canonical write
        # boundary exactly like a single one (native_provenance_writer).
        if receipt_reentry and occasion_new_act_fields(validated_act):
            # The grammar cannot produce one; the host refuses it regardless (no chain from a receipt).
            raise ConversationDirectionFailure("pass1", RECEIPT_REENTRY_ACT_NOT_AVAILABLE)
        occasion_checks = []
        checks_used = 0                       # all chosen read-only checks (diagnostic only)
        ordinary_checks_used = 0              # existing verify/workspace bound remains exactly two
        history_actions_used = 0              # separate bounded search/read allowance
        history_allowed_cursors = set()       # only cursors actually returned in this occasion
        history_allowed_browse_cursors = set() # only chronological continuations returned in this occasion
        history_browse_started = False        # BEGIN AT BEGINNING is offered exactly once per occasion
        history_search_cache = {}             # literal query -> authoritative same-view result
        # An exact historical window already delivered in this occasion cannot
        # add information if selected again against the same read-only store.
        # Keep continuations and every other returned cursor available; only
        # the consumed cursor identity is withheld from later grammars.
        history_consumed_cursors = set()
        history_consumed_browse_cursors = set()
        waymark_reads_used = 0                # separate bounded Waymark list/inspect allowance
        image_view_result = None              # an inspection that chose an image view completes the occasion
        while True:
            _ordinary_check = (
                validated_act["action_verification_request"] != ACTION_VERIFICATION_REQUEST_NONE
                or validated_act.get("workspace_mode") == WORKSPACE_MODE_INSPECT
            )
            _history_action = validated_act["act"] in (
                SEARCH_HISTORICAL_CONTINUITY, READ_HISTORICAL_ITEM,
                BROWSE_HISTORICAL_CONTINUITY)
            # WAYMARK READ NAVIGATION: list/inspect are read-only checks of the occasion like a historical
            # search/read -- executed here, their exact result re-enters Pass 1 -- under their own bound.
            _waymark_read = _is_waymark_read(validated_act) and waymark_available
            _wants_check = (
                (_ordinary_check and ordinary_checks_used < MAX_READ_ONLY_CHECKS_PER_OCCASION)
                or (_history_action and history_actions_used
                    < historical_continuity_access.MAX_HISTORY_ACTIONS_PER_OCCASION)
                or (_waymark_read and waymark_reads_used < MAX_WAYMARK_READS_PER_OCCASION)
            )
            if not _wants_check:
                break
            # The re-entry answering this check gets its own truthful control allowance (a verified
            # check's re-entry also echoes the canonical receipt status); every room and composition
            # below admits its prompt against what that allowance leaves.
            _reentry_control_allowance = _pass1_reentry_control_allowance(
                history_schema_base, history_action_available, pass1_pending_send_refs,
                receipt_echo=CANDIDATE_PROTOCOL_MECHANICS and validated_act["act"] == VERIFY_ACTION)
            _PASS1_TURN_PROMPT_BUDGET.set(context_budget.pass1_prompt_budget(_reentry_control_allowance))
            _reentry_hard_floor = list(pass1_hard_floor)
            _current_evidence_room = _rebalance_prior_evidence_for_current(
                _reentry_hard_floor, occasion_checks, dialogue_contribution(pass1_dialogue_units))
            if _waymark_read:
                # The receipt is fitted to the room this re-entry really has for it (never above the Waymark
                # result bound); a refusal is itself the receipt (operation-scoped), so the occasion goes on.
                try:
                    _waymark_check = _resolve_waymark(
                        validated_act, session_id,
                        occasion=((pulse_occasion or {}).get("kind")
                                  or ("caret" if caret_occasion is not None else "human")),
                        human_input_event_id=human_input_event_id,
                        max_bytes=max(0, _current_evidence_room["tokens"]))
                except (waymark.WaymarkIntegrityError, sqlite3.Error) as exc:
                    raise ConversationDirectionFailure("pass1", f"WAYMARK_FAILED:{exc}") from exc
                occasion_checks.append({
                    "kind": "waymark_" + validated_act["waymark_request"], "query": None, "result": None,
                    "raw_act": raw_act_only, "rendered": _waymark_check["text"],
                    "waymark": {"outcome": _waymark_check["outcome"], "attempts": _waymark_check["attempts"],
                                "read": _waymark_check["read"], "spec": _waymark_check["spec"],
                                "request": validated_act["waymark_request"], "session_id": session_id,
                                "lane": dict((_TURN_ECHO.get() or {}).get("waymark_lane") or {})}})
                waymark_reads_used += 1
            elif _history_action:
                if not history_action_available or history_paths is None:
                    raise ConversationDirectionFailure(
                        "pass1", ConversationDirectionFailureCode.HISTORICAL_CONTINUITY_CONFLICT)
                try:
                    _consumed_history_cursor = (
                        validated_act.get("historical_cursor")
                        if validated_act["act"] == READ_HISTORICAL_ITEM else None)
                    _consumed_browse_cursor = (
                        validated_act.get("historical_browse_cursor")
                        if validated_act["act"] == BROWSE_HISTORICAL_CONTINUITY else None)
                    if (validated_act["act"] == BROWSE_HISTORICAL_CONTINUITY
                            and not _consumed_browse_cursor and history_browse_started):
                        raise historical_continuity_access.HistoricalAccessError(
                            "history_browse_begin_already_used")
                    occasion_checks.append(ui_turn_diagnostics.timed_stage(
                        "historical_search_or_read", _history_check,
                        history_paths, validated_act, history_allowed_cursors,
                        history_allowed_browse_cursors,
                        _family_view, raw_act_only, evidence_room=_current_evidence_room,
                        search_cache=history_search_cache))
                    if _consumed_history_cursor is not None:
                        history_consumed_cursors.add(_consumed_history_cursor)
                        history_allowed_cursors.discard(_consumed_history_cursor)
                    if validated_act["act"] == BROWSE_HISTORICAL_CONTINUITY:
                        history_browse_started = True
                    if _consumed_browse_cursor is not None:
                        history_consumed_browse_cursors.add(_consumed_browse_cursor)
                        history_allowed_browse_cursors.discard(_consumed_browse_cursor)
                except historical_continuity_access.HistoricalAccessError as exc:
                    raise ConversationDirectionFailure(
                        "historical_continuity", str(exc)) from exc
                history_actions_used += 1
            elif validated_act.get("workspace_mode") == WORKSPACE_MODE_INSPECT:
                # OBSERVATION (candidate): a read-only workspace inspection is a check like verify_action --
                # the existing supervised read path runs on a read-only surface and returns its exact result,
                # which reaches the re-entry beside the turn.  Nothing is written, nothing is expressed yet.
                if not workspace_action_available:
                    raise ConversationDirectionFailure("pass1", ConversationDirectionFailureCode.WORKSPACE_ACTION_NOT_AUTHORIZED)
                import workspace_capability as _wc_inspect
                import workspace_supervisor as _ws_inspect
                _inspect_paths = _wc_inspect.WorkspacePaths.production_defaults()
                try:
                    _inspection = _ws_inspect.run_one_supervised_workspace_action(
                        orch, prompt, derive_stable_id("actor", "clark"), _inspect_paths,
                        human_input_event_id=human_input_event_id,
                        visibility_scope=(_family_view["visibility_scope"] if _family_view is not None else None),
                        family_view=_family_view, family_feature_active=_family_feature_active,
                        allowed_surface=_ws_inspect.wd.resource_allowed_surface(_inspect_paths),
                        topic_hint=validated_act["thread"],
                        prior_evidence_text="\n\n".join(
                            c["rendered"] for c in _without_superseded_waymark(occasion_checks)) or None,
                        prior_evidence_cost=(_joined_evidence_cost(_without_superseded_waymark(occasion_checks))
                                             if occasion_checks else None),
                        current_input_role=current_input_role, occasion_envelope=occasion_envelope,
                        workspace_mode=WORKSPACE_MODE_INSPECT,
                        pulse_kind=(pulse_occasion or {}).get("kind"),
                        pending_encounters=_staged_encounters(occasion_checks),
                        evidence_room=_current_evidence_room,
                        wtr0_recovery_id=wtr0_recovery_id,
                    )
                except _ws_inspect.wd.WorkspaceDirectionFailure as exc:
                    raise ConversationDirectionFailure(f"workspace_{exc.stage}", exc.failure_code) from exc
                if _inspection.get("image_view_expressed"):
                    # IMAGE VIEW: actual pixels went to the vision path and that expression is this occasion's
                    # turn (recorded by the supervisor) -- it cannot re-enter a text-only Pass 1.
                    image_view_result = _inspection
                    break
                _inspection_rendered = _inspection["evidence_text"]
                _inspection_record = _inspection["inspection"]
                _inspection_source = _inspection.get("evidence_source")
                if _delivered_earlier_in_occasion(occasion_checks, _inspection_record.get("encounter")):
                    # NO-NEW-EVIDENCE READ (replay 2026-10-10, live 7): the same window of the same item was
                    # delivered again.  Its exact identity is stated instead of a second copy; the encounter stays
                    # recorded once (the earlier delivery).
                    _inspection_rendered = _repeated_delivery_record(_inspection_record)
                    _inspection_record = {k: v for k, v in _inspection_record.items() if k != "encounter"}
                    _inspection_source = None
                occasion_checks.append({"kind": "inspection", "query": None, "result": None, "raw_act": raw_act_only,
                                        "rendered": _inspection_rendered,
                                        "inspection": _inspection_record,
                                        "evidence_source": _inspection_source})
                ordinary_checks_used += 1
            else:
                _verify_check(
                    occasion_checks, validated_act, raw_act_only,
                    evidence_room=_current_evidence_room,
                )
                ordinary_checks_used += 1
            checks_used += 1
            _settle_check_echo()
            # The host's mechanical Waymark read offer for this re-entry (and the read it may bind).
            _waymark_offer = _waymark_read_offer(session_id, waymark_reads_used) if waymark_available else None
            _waymark_reads_state()["offer"] = _waymark_offer
            if _waymark_read:
                remaining = MAX_WAYMARK_READS_PER_OCCASION - waymark_reads_used
                _check_state = (
                    f"Waymark reads used in this occasion: {waymark_reads_used} of {MAX_WAYMARK_READS_PER_OCCASION}. "
                    + (f"{remaining} more may be chosen in this occasion." if remaining and _waymark_offer["requests"]
                       else "No further Waymark read is available in this occasion.")
                )
            elif _history_action:
                remaining = historical_continuity_access.MAX_HISTORY_ACTIONS_PER_OCCASION - history_actions_used
                _check_state = (
                    f"Historical read-only actions used in this occasion: {history_actions_used} of "
                    f"{historical_continuity_access.MAX_HISTORY_ACTIONS_PER_OCCASION}. "
                    + (f"{remaining} more may be chosen in this occasion." if remaining
                       else "No further historical action is available in this occasion.")
                )
            else:
                _check_state = render_occasion_check_state(ordinary_checks_used)

            def _schema_for_reentry(allowed_cursors):
                schema = history_schema_base
                if ordinary_checks_used >= MAX_READ_ONLY_CHECKS_PER_OCCASION:
                    schema = pass1_schema_checks_exhausted(schema)
                if (history_action_available and history_actions_used
                        < historical_continuity_access.MAX_HISTORY_ACTIONS_PER_OCCASION):
                    schema = pass1_schema_with_historical_continuity(
                        schema, allowed_cursors,
                        browse_begin=not history_browse_started,
                        browse_cursors=(c for c in history_allowed_browse_cursors
                                        if c not in history_consumed_browse_cursors))
                else:
                    schema = pass1_schema_history_exhausted(schema)
                if _waymark_offer is not None:
                    schema = pass1_schema_waymark_reads_offered(schema, _waymark_offer["requests"])
                if CANDIDATE_PROTOCOL_MECHANICS and occasion_checks[-1].get("result") is not None:
                    schema = pass1_schema_with_receipt_acknowledgement(
                        schema, _canonical_status_text(occasion_checks[-1]["result"]))
                schema = pass1_control_schema(schema, pending_send_refs=pass1_pending_send_refs)
                if pass1_control_max_bytes(schema) > _reentry_control_allowance:
                    # Mechanically impossible by _pass1_reentry_control_allowance; never sent if it happens.
                    raise ConversationDirectionFailure("pass1_reentry", "CONTROL_ALLOWANCE_EXCEEDED")
                return schema

            def _displayed_checks_for_reentry():
                return _displayed_occasion_checks(occasion_checks)

            def _compose_current_reentry(schema, *, allow_dialogue_pressure=False):
                return _compose_pass1_reentry(
                    hard_contributions=list(pass1_hard_floor),
                    dialogue_units=pass1_dialogue_units,
                    displayed_checks=_displayed_checks_for_reentry(),
                    check_state=_check_state, reentry_schema=schema,
                    search_choices_contrib=pass1_search_choices_contrib,
                    open_page_contrib=pass1_open_page_contrib,
                    allow_dialogue_pressure=allow_dialogue_pressure,
                )

            if occasion_checks[-1].get("kind") == "historical_search":
                search_check = occasion_checks[-1]

                def _compose_search_projection(text, presented_candidates):
                    search_check["rendered"] = text
                    trial_cursors = set(history_allowed_cursors)
                    trial_cursors.update(
                        c["read_cursor"] for c in presented_candidates
                        if c["read_cursor"] not in history_consumed_cursors)
                    return _compose_current_reentry(_schema_for_reentry(trial_cursors))

                def _compose_search_projection_with_dialogue_pressure(
                        text, presented_candidates):
                    search_check["rendered"] = text
                    trial_cursors = set(history_allowed_cursors)
                    trial_cursors.update(
                        c["read_cursor"] for c in presented_candidates
                        if c["read_cursor"] not in history_consumed_cursors)
                    return _compose_current_reentry(
                        _schema_for_reentry(trial_cursors),
                        allow_dialogue_pressure=True)

                (_chosen_rendered, _presented_candidates,
                 _reentry_composition) = _fit_historical_search_reentry(
                    search_check["historical_result"], _compose_search_projection,
                    _compose_search_projection_with_dialogue_pressure)
                search_check["rendered"] = _chosen_rendered
                history_allowed_cursors.update(
                    c["read_cursor"] for c in _presented_candidates
                    if c["read_cursor"] not in history_consumed_cursors)
            elif occasion_checks[-1].get("kind") == "historical_browse":
                browse_check = occasion_checks[-1]
                browse_request_cursor = browse_check.get("historical_browse_request_cursor")
                browse_kwargs = _history_view_kwargs(_family_view)

                def _browse_trial(page_chars, *, allow_dialogue_pressure=False):
                    projected = historical_continuity_access.browse(
                        history_paths, browse_request_cursor,
                        allowed_cursors=({browse_request_cursor} if browse_request_cursor else ()),
                        page_chars=page_chars, **browse_kwargs)
                    browse_check["historical_result"] = projected
                    browse_check["rendered"] = historical_continuity_access.render_browse(projected)
                    trial_browse_cursors = set(history_allowed_browse_cursors)
                    if projected.get("next_cursor"):
                        trial_browse_cursors.add(projected["next_cursor"])
                    schema = history_schema_base
                    if ordinary_checks_used >= MAX_READ_ONLY_CHECKS_PER_OCCASION:
                        schema = pass1_schema_checks_exhausted(schema)
                    if (history_action_available and history_actions_used
                            < historical_continuity_access.MAX_HISTORY_ACTIONS_PER_OCCASION):
                        schema = pass1_schema_with_historical_continuity(
                            schema, history_allowed_cursors,
                            browse_begin=not history_browse_started,
                            browse_cursors=(c for c in trial_browse_cursors
                                            if c not in history_consumed_browse_cursors))
                    else:
                        schema = pass1_schema_history_exhausted(schema)
                    if _waymark_offer is not None:
                        schema = pass1_schema_waymark_reads_offered(schema, _waymark_offer["requests"])
                    schema = pass1_control_schema(schema, pending_send_refs=pass1_pending_send_refs)
                    if pass1_control_max_bytes(schema) > _reentry_control_allowance:
                        raise ConversationDirectionFailure(
                            "pass1_reentry", "CONTROL_ALLOWANCE_EXCEEDED")
                    return projected, _compose_current_reentry(
                        schema, allow_dialogue_pressure=allow_dialogue_pressure)

                def _largest_browse_fit(*, allow_dialogue_pressure=False):
                    low, high = 1, historical_continuity_access.BROWSE_PAGE_CHARS
                    chosen = None
                    while low <= high:
                        middle = (low + high) // 2
                        projected, composition = _browse_trial(
                            middle, allow_dialogue_pressure=allow_dialogue_pressure)
                        if composition["result"].fits:
                            chosen = projected, composition
                            low = middle + 1
                        else:
                            high = middle - 1
                    return chosen

                _browse_fit = _largest_browse_fit()
                if _browse_fit is None:
                    _browse_fit = _largest_browse_fit(allow_dialogue_pressure=True)
                if _browse_fit is None:
                    raise ConversationDirectionFailure("pass1_reentry", context_budget.BUDGET_EXCEEDED)
                _chosen_browse, _reentry_composition = _browse_fit
                browse_check["historical_result"] = _chosen_browse
                browse_check["rendered"] = historical_continuity_access.render_browse(_chosen_browse)
                if _chosen_browse.get("next_cursor"):
                    history_allowed_browse_cursors.add(_chosen_browse["next_cursor"])
            else:
                _reentry_composition = _compose_current_reentry(
                    _schema_for_reentry(history_allowed_cursors),
                    allow_dialogue_pressure=True)

            _reentry_schema = _reentry_composition["schema"]
            _reentry_result = _reentry_composition["result"]
            _reentry_dialogue_contrib = _reentry_composition["dialogue_contrib"]
            _evidence_texts = _reentry_composition["evidence_texts"]
            ui_turn_diagnostics.record_context_budget_result("pass1_reentry", _reentry_result)
            if not _reentry_result.fits:
                raise ConversationDirectionFailure("pass1_reentry", context_budget.BUDGET_EXCEEDED)
            if occasion_checks[-1].get("kind", "").startswith("waymark_"):
                # The read's receipt is HARD in this composed re-entry: it reaches Clark before he answers.
                _lane = occasion_checks[-1]["waymark"]["lane"]
                _record_waymark_event(**dict(_lane, event="result_delivery", request=occasion_checks[-1]["kind"][8:],
                                             outcome=occasion_checks[-1]["waymark"]["outcome"],
                                             delivered_in_composed_reentry=True))
            _reentry_dialogue = dialogue_messages(_reentry_dialogue_contrib)
            _reentry_search = _reentry_result.included_kind(context_budget.PASS1_SEARCH_CHOICES)
            _reentry_page = _reentry_result.included_kind(context_budget.PASS1_OPEN_PAGE)
            _reentry_messages = (
                [dict(pass1_messages[0])]
                + _reentry_dialogue
                + ([external_data_message(_reentry_search.rendered_text)]
                   if _reentry_search is not None and _reentry_search.rendered_text else [])
                + ([external_data_message(_reentry_page.rendered_text)]
                   if _reentry_page is not None and _reentry_page.rendered_text else [])
                + [{"role": current_input_role, "content": base_current_user_message}]
                # Each check's exact receipt, in order, beside the turn it answers (host-labelled).
                + [_host_evidence_message(text, occasion_envelope) for text in _evidence_texts]
            )
            try:
                raw1 = ui_turn_diagnostics.timed_model_call(
                    "pass1_reentry", ask_llama_for_json, _reentry_messages,
                    structured_schema=_reentry_schema,
                    generation_reserve=_reentry_control_allowance,
                )
                _reentry_payload_failures = []
                raw1 = _bind_pass1_payloads(raw1, _reentry_schema, pass1_selection_grammars_turn, _reentry_messages,
                                            _reentry_result.final_prompt_cost, occasion_envelope,
                                            payload_failures=_reentry_payload_failures)
            except (context_budget.IncompleteCompletionError, Pass1PayloadFailure) as exc:
                direction_failure = ConversationDirectionFailure("pass1_reentry", exc.failure_code)
                ui_turn_diagnostics.attach_pass1_failure_preview(
                    direction_failure, getattr(exc, "partial_output", None))
                raise direction_failure from exc
            raw_act_only = conversation_direction_trace.extract_raw_act(raw1)
            raw1, _ws_mode = _candidate_translate(raw1, acknowledged_receipt_statuses, declared_claims)
            validated_act, failure1 = validate_pass1_conversation_act(raw1)
            if failure1 in INCOMPLETE_OUTWARD_REQUEST_FAILURES:
                repaired_raw, dropped_requests = drop_incomplete_outward_requests(raw1, failure1)
                if repaired_raw is not None:
                    repaired_act, repaired_failure = validate_pass1_conversation_act(repaired_raw)
                    if repaired_failure is None:
                        validated_act, failure1 = dict(repaired_act, dropped_incomplete_requests=tuple(dropped_requests)), None
            if validated_act is not None and _ws_mode:
                validated_act = dict(validated_act, workspace_mode=_ws_mode)
            if validated_act is not None and declared_claims and declared_claims[-1] is not None:
                validated_act = dict(validated_act, workspace_claim=declared_claims[-1])
            if validated_act is not None and _reentry_payload_failures:
                validated_act = dict(validated_act, payload_failures=tuple(_reentry_payload_failures))
            if failure1 is not None:
                conversation_direction_trace.record_trace(
                    session_id=session_id, raw_act=raw_act_only, validated_act=None, thread=None,
                    pass1_status=failure1, working_set_before=working_set_before, working_set_after=working_set_before,
                    direction_owner_before=working_set_before.get("direction_owner", DIRECTION_UNKNOWN),
                    direction_owner_after=working_set_before.get("direction_owner", DIRECTION_UNKNOWN),
                    direction_request=None, relinquish_direction=None,
                    pass2_status=None, event_id=None, staging_id=None,
                    trace_path=CONVERSATION_DIRECTION_TRACE_PATH,
                )
                direction_failure = ConversationDirectionFailure("pass1_reentry", failure1)
                ui_turn_diagnostics.attach_pass1_failure_preview(direction_failure, raw1)
                raise direction_failure
            if (ordinary_checks_used >= MAX_READ_ONLY_CHECKS_PER_OCCASION
                    and (validated_act["act"] == VERIFY_ACTION
                         or (validated_act["act"] == USE_WORKSPACE
                             and validated_act.get("workspace_mode") != WORKSPACE_MODE_MUTATE))):
                # The exhausted grammar cannot produce a third check; the host refuses one regardless.
                raise ConversationDirectionFailure("pass1_reentry", OCCASION_CHECK_BOUND_REACHED)
            if (history_actions_used >= historical_continuity_access.MAX_HISTORY_ACTIONS_PER_OCCASION
                    and validated_act["act"] in (
                        SEARCH_HISTORICAL_CONTINUITY, READ_HISTORICAL_ITEM,
                        BROWSE_HISTORICAL_CONTINUITY)):
                raise ConversationDirectionFailure("pass1_reentry", OCCASION_CHECK_BOUND_REACHED)
            if _is_waymark_read(validated_act) and (
                    waymark_reads_used >= MAX_WAYMARK_READS_PER_OCCASION
                    or (_waymark_offer is not None and validated_act["waymark_request"] not in _waymark_offer["requests"])):
                # The offered grammar cannot produce a read it does not offer; the host refuses one regardless.
                raise ConversationDirectionFailure("pass1_reentry", OCCASION_CHECK_BOUND_REACHED)
        if (CANDIDATE_PROTOCOL_MECHANICS and workspace_action_available
                and validated_act["act"] not in (VERIFY_ACTION, USE_WORKSPACE)
                and validated_act.get("reply_request") != REPLY_REQUEST_NO_REPLY):
            # SPEECH-BOUNDARY WITNESS, AFTER THE REPLY CHOICE: only once the act is final and words will be written
            # does a separate structured declaration ask whether those words will state the current state of an
            # exact item (none, or collection + exact item).  An unwitnessed declared item is read (or its
            # collection listed) and the evidence goes beside the turn into the pass that writes the words.  The
            # chosen act -- including no_reply -- is never shaped or reopened by this machinery.
            try:
                _claim = _declare_workspace_claim(
                    system_text=base_system_content, dialogue_units=pass1_dialogue_units,
                    current_input=base_current_user_message, current_input_role=current_input_role,
                    evidence_texts=[c["rendered"] for c in _without_superseded_waymark(occasion_checks)],
                    occasion_envelope=occasion_envelope,
                    evidence_cost=(_joined_evidence_cost(_without_superseded_waymark(occasion_checks))
                                   if occasion_checks else None),
                    collections=["library", "journal"] + (["notes"] if _shared_vault_available() else []))
            except Pass1PayloadFailure as exc:
                # The collection was chosen but its exact item name did not complete (owner decision
                # 2026-10-08): no claim is taken, no item is guessed or read, and Pass 2 is told so.
                _claim = None
                validated_act = dict(validated_act, payload_failures=tuple(validated_act.get("payload_failures", ())) + (
                    {"field": exc.field, "collection": getattr(exc, "collection", None),
                     "failure_class": exc.failure_code, "termination": exc.termination,
                     "dropped": ("workspace_claim",)},))
            except context_budget.IncompleteCompletionError as exc:
                # The declaration's own control choice did not complete: fails closed exactly as before (no
                # claim is guessed, nothing is written), carrying the existing bounded diagnostic preview.
                direction_failure = ConversationDirectionFailure("workspace_claim", exc.failure_code)
                ui_turn_diagnostics.attach_pass1_failure_preview(
                    direction_failure, getattr(exc, "partial_output", None))
                raise direction_failure from exc
            if _claim is not None and not _claim_already_witnessed(_claim, occasion_checks):
                _claim_room = _rebalance_prior_evidence_for_current(
                    list(pass1_hard_floor),
                    occasion_checks, dialogue_contribution(pass1_dialogue_units))
                occasion_checks.append(ui_turn_diagnostics.timed_stage(
                    "workspace_claim_witness_read", _claim_witness,
                    _claim, raw_act_only, human_input_event_id, session_id,
                    evidence_room=_claim_room))
        if occasion_checks:
            # Pass 2 receives every ordinary check and every exact historical
            # read window, in order, in the one hard action-record slot beside
            # the turn.  Search excerpts are omitted once Clark has inspected
            # the selected source so they do not displace its substantive text.
            _pass2_checks = _pass2_evidence_checks(occasion_checks)
            history_evidence_manifest = _historical_evidence_manifest(occasion_checks, _pass2_checks)
            pass2_waymark_read_checks = [c for c in _pass2_checks if c.get("kind", "").startswith("waymark_")]
            pass2_action_record_text = "\n\n".join(c["rendered"] for c in _pass2_checks)
            pass2_action_record_cost = _joined_evidence_cost(_pass2_checks)
            _pass2_history_checks = [c for c in _pass2_checks
                                     if c.get("kind", "").startswith("historical_")]
            pass2_history_evidence_cost = (
                _joined_evidence_cost(_pass2_history_checks) if _pass2_history_checks else None)
        if acknowledged_receipt_statuses:
            # RECEIPT ACKNOWLEDGEMENT (candidate): the status the subject stated, carried beside the turn into
            # the reply pass (a host fact about what it acknowledged; nothing about what it means).
            _acknowledged_status_text = (
                "Canonical receipt status you acknowledged in this occasion: " + acknowledged_receipt_statuses[-1])
            pass2_action_record_text = ((pass2_action_record_text + "\n") if pass2_action_record_text else "") + (
                _acknowledged_status_text)
            pass2_action_record_cost = ((pass2_action_record_cost or 0)
                                        + context_budget.estimate_tokens("\n")
                                        + _real_cost(_acknowledged_status_text,
                                                     context_budget.estimate_tokens(_acknowledged_status_text)))
        _verify_checks = [c for c in occasion_checks if c.get("query") is not None]
        if _verify_checks:
            pending_action_verification_query = _verify_checks[-1]["query"]
            live_action_verification_result = _verify_checks[-1]["result"]
            prior_occasion_verifications = [(c["query"], c["result"]) for c in _verify_checks[:-1]]
        else:
            prior_occasion_verifications = []

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

        # WITNESS GATE V0 -- OUTWARD SEND (candidate): before a send is committed, the host shows its exact
        # mechanical facts and the subject decides proceed | cancel | inspect (earlier sends to this
        # destination).  The host executes exactly that decision; it recommends nothing.
        if (CANDIDATE_PROTOCOL_MECHANICS
                and validated_act["discord_correspondence_request"] == DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE):
            validated_act, _send_gate = _outward_send_witness_gate(
                validated_act, authorized_discord_destinations=authorized_discord_destinations,
                pulse_occasion=pulse_occasion, caret_occasion=caret_occasion,
                system_text=base_system_content, dialogue_units=pass1_dialogue_units,
                current_input=base_current_user_message, current_input_role=current_input_role,
                evidence_texts=[c["rendered"] for c in _without_superseded_waymark(occasion_checks)],
                occasion_envelope=occasion_envelope)
            witness_gates.append(_send_gate)
            if _send_gate["decision"] != witness_gate.PROCEED:
                correspondence_notes = list(correspondence_notes) + [witness_gate.cancelled_note(
                    "Discord send", f"to {_send_gate['facts'].get('destination_label') or 'that destination'}")]
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
                workspace_result = image_view_result if image_view_result is not None else workspace_supervisor.run_one_supervised_workspace_action(
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
                    # PULSE V0: this occasion's earlier read-only check receipts, beside the turn.
                    prior_evidence_text=pass2_action_record_text if occasion_checks else None,
                    prior_evidence_cost=pass2_action_record_cost if occasion_checks else None,
                    current_input_role=current_input_role, occasion_envelope=occasion_envelope,
                    workspace_mode=validated_act.get("workspace_mode"),
                    pulse_kind=(pulse_occasion or {}).get("kind"),
                    pending_encounters=_staged_encounters(occasion_checks),
                    wtr0_recovery_id=wtr0_recovery_id,
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
                "action_verification_result": None,
                "discord_correspondence": {"outbound": None, "inbound_delivered": None},
                "occasion_checks": [_check_summary(c) for c in occasion_checks],
                "canonical_recorded": workspace_result.get("canonical_recorded", True),
                "witness_gates": witness_gates + list(workspace_result.get("witness_gates") or []),
                "acknowledged_receipt_statuses": list(acknowledged_receipt_statuses),
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
            delivered_resource_encounter_event_id = pass1_delivered_resource_encounter_event_id
            delivered_discord_inbound_event_ids = [caret_occasion["event_id"]] if caret_occasion is not None else None
            live_external_request = None
            external_info_pre_reply_state = "not_performed"
            pending_background_activity_request = validated_act["background_activity_request"]
            pending_sleep_timing_request = validated_act["sleep_timing_request"]
            pending_boundary_inquiry_request = validated_act["boundary_inquiry_request"]
            pending_operative_directive_request = validated_act["operative_directive_request"]
            pending_operative_directive_text = validated_act["operative_directive_text"]
            pending_consolidation_request = validated_act["consolidation_request"]
            pending_consolidation_target = validated_act["consolidation_target"]
            pending_consolidation_interpretation = validated_act["consolidation_interpretation"]
            pending_consolidation_cues = validated_act["consolidation_cues"]
            pending_consolidation_prior_record_id = validated_act["consolidation_prior_record_id"]
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

            # CANONICAL ACTION VERIFICATION V0: read the existing authorities
            # between Pass 1 and Pass 2.  No action, retry, resource read,
            # network call, or persistence occurs here.  The result is bounded
            # typed mechanics and contains no note/message content.
            if validated_act["action_verification_request"] != ACTION_VERIFICATION_REQUEST_NONE:
                pending_action_verification_query = action_verification.make_query(
                    validated_act["action_verification_request"],
                    validated_act["action_verification_target"],
                )
                live_action_verification_result = action_verification.verify_action(
                    PROVENANCE_DB_DIR,
                    pending_action_verification_query,
                    workspace_action_log_path=os.path.join(runtime_roots.workspace_root(), "workspace_action_log.jsonl"),
                )
                import workspace_capability as _wc_paths
                _paths = _wc_paths.WorkspacePaths.production_defaults()
                try:
                    _presence = action_verification.exact_current_presence(
                        pending_action_verification_query, notes_dir=_paths.notes_dir, journal_dir=_paths.journal_dir)
                except OSError:
                    _presence = None
                pass2_action_record_text = action_verification.render_result(
                    live_action_verification_result, current_presence=_presence)
                pass2_action_record_cost = _real_cost(
                    pass2_action_record_text, context_budget.estimate_tokens(pass2_action_record_text))
            # WAYMARK V0: exact mechanical syntax only.  Optional sunsets
            # are reconciled from Clark's preauthorization before the shelf
            # is read; neither inspection nor trigger eligibility mutates a
            # Waymark.  A mutation becomes canonical only with this turn.
            # The whole Waymark lane -- including its one correction -- is
            # settled HERE, before Pass 2: the reply is composed with its
            # final, authoritative result (READ / ACCEPTED FOR COMMIT /
            # NOT CREATED), so no Waymark attempt remains open while Clark
            # writes.  Only a canonical integrity or store failure fails the
            # turn closed.
            try:
                waymark.reconcile_sunsets(
                    PROVENANCE_DB_DIR, now=int(datetime.now(timezone.utc).timestamp()))
                if validated_act["act"] == WAYMARK:
                    waymark_resolution = _resolve_waymark(
                        validated_act, session_id,
                        occasion=((pulse_occasion or {}).get("kind")
                                  or ("caret" if caret_occasion is not None else "human")),
                        human_input_event_id=human_input_event_id)
                    pending_waymark_action = waymark_resolution["action"]
                    pass2_waymark_query_text = waymark_resolution["text"]
                pending_waymark_surfaces = (waymark.pending_surfaces(
                    PROVENANCE_DB_DIR, session_id=session_id,
                    now=int(datetime.now(timezone.utc).timestamp()), limit=1)
                    if waymark_available else [])
                if pending_waymark_action is not None and pending_waymark_action.get("handle"):
                    # The current Clark mutation has not committed yet. Do
                    # not surface the same handle under its pre-mutation
                    # fold and then ask the writer to validate that stale
                    # eligibility after atomically inserting the mutation.
                    pending_waymark_surfaces = [
                        item for item in pending_waymark_surfaces
                        if item["handle"] != pending_waymark_action["handle"]]
            except (waymark.WaymarkError, waymark.WaymarkIntegrityError, sqlite3.Error) as exc:
                raise ConversationDirectionFailure("pass1", f"WAYMARK_FAILED:{exc}") from exc
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
                    send_selected=validated_act["discord_correspondence_request"] == DISCORD_CORRESPONDENCE_REQUEST_SEND_MESSAGE,
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
            # An encounter record and its turn companions trim as one episode
            # (hippocampus_retrieval.delivery_units): never the record without the reply.
            pass2_hippocampal_units = hippocampus_retrieval.delivery_units(hippocampal_items)

            def _native_hippocampal_units(units):
                """The NATIVE form of these units -- the text the provider will actually receive.

                The compositor must price what is sent, not the legacy rendered block it replaces:
                the two differ in size, and pricing the smaller one lets the seam's own bound drop
                history the compositor had already admitted.  Reconstruction is read-only and
                withholds anything the ledger cannot confirm, so a unit that cannot be re-derived
                contributes no text here and is priced at zero -- exactly as it will be delivered."""
                try:
                    reconstruction = historical_unit_reconstruction.reconstruct(
                        hippocampus_retrieval.delivered_items(units),
                        _retrieved_history_ledger_path(),
                        relational_db_path=getattr(
                            getattr(orch, "hippocampus_paths", None), "relational_db_path", None),
                        historical_jsonl_path=getattr(
                            getattr(orch, "hippocampus_paths", None), "historical_jsonl_path", None),
                    )
                    projection = relational_history_projection.project(
                        reconstruction, viewer_human_actor_id=(
                            human_input_authority.authenticated_actor_id
                            if human_input_authority is not None else None),
                        person_annotations=_person_annotations)
                    return relational_history_projection.native_history_text(projection)
                except Exception:  # noqa: BLE001 - pricing must never end a live turn
                    return ""

            pass2_soft_hippocampal = context_budget.Contribution(
                context_budget.RETRIEVED_HISTORY,
                _native_hippocampal_units(pass2_hippocampal_units) if pass2_hippocampal_units else "",
                hard=False,
                droppable_units=pass2_hippocampal_units if pass2_hippocampal_units else None,
                render_fn=_native_hippocampal_units if pass2_hippocampal_units else None,
                source_ids=[item.event_id for item in pass2_hippocampal_units] if pass2_hippocampal_units else None,
            )
            retrieval_delivery.describe(pass2_soft_hippocampal, list(reversed(hippocampal_items)),
                                        query_terms=hippocampal_result.query_terms if hippocampal_result is not None else (),
                                        person_annotations=_person_annotations)

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
            pass2_hard_action_record = (
                context_budget.Contribution(context_budget.ACTION_RECORD_RESULT, pass2_action_record_text, hard=True)
                if pass2_action_record_text else None
            )
            if pass2_hard_action_record is not None and pass2_action_record_cost is not None:
                pass2_hard_action_record.cost = pass2_action_record_cost
            pass2_waymark_query_carriage = (
                _waymark_host_evidence_text(pass2_waymark_query_text, occasion_envelope)
                if pass2_waymark_query_text else None
            )
            pass2_hard_waymark_query = (
                context_budget.Contribution(
                    context_budget.WAYMARK_QUERY_RESULT, pass2_waymark_query_carriage, hard=True)
                if pass2_waymark_query_carriage else None
            )
            pass2_hard_waymark_notice = None
            if pending_waymark_surfaces:
                waymark_notice_carriage = _waymark_host_evidence_text(
                    "\n".join(item["notice"] for item in pending_waymark_surfaces), occasion_envelope)
                # HARD in its own reserved room: Clark's preauthorized resurfacing is never starved and never
                # displaces anything; its receipt still comes only from the composition it survived.
                pass2_hard_waymark_notice = context_budget.Contribution(
                    context_budget.WAYMARK_NOTICE,
                    waymark_notice_carriage,
                    hard=True,
                    source_ids=[item["handle"] for item in pending_waymark_surfaces],
                )
            # WAYMARK V0 rooms: the turn's Waymark result and notice are each admitted in their own exact room
            # above PASS2_MAX_PROMPT_BUDGET, so every older Pass-2 content keeps exactly the room it had (the
            # message boundary each adds is carried by the serving-context bound, context_budget
            # WAYMARK_*_MAX_TOKENS).  Zero on any turn without them: a dormant Waymark costs nothing.
            pass2_waymark_result_room = pass2_hard_waymark_query.cost if pass2_hard_waymark_query is not None else 0
            pass2_waymark_notice_room = pass2_hard_waymark_notice.cost if pass2_hard_waymark_notice is not None else 0
            # Historical evidence that reached a Pass-1 reentry is a HARD
            # final-reply input, not ordinary soft continuity. Give its
            # exact measured cost a dedicated room (as Waymark does), capped
            # by the already-frozen worst lawful Pass-2 prompt so the protected
            # completion guarantee and serving window remain unchanged. Dormant
            # history still costs zero.
            pass2_history_evidence_room = pass2_history_evidence_cost or 0
            pass2_budget = min(
                context_budget.PASS2_MAX_PROMPT_BUDGET + pass2_waymark_result_room
                + pass2_waymark_notice_room + pass2_history_evidence_room,
                context_budget.PASS2_WORST_LAWFUL_PROMPT,
            )
            # CAP2-F: bounded, deterministic, SOFT, source-grounded
            # continuity for one genuinely-delivered public resource
            # encounter -- no salience/importance/preference of any kind,
            # a fixed mechanical-fact template only. Empty (no Contribution
            # added at all) when nothing is pending, exactly like every
            # other conditional SOFT source in this function.
            pass2_soft_resource_encounter = None
            if pending_resource_encounter is not None and not _encounter_resource_in_checks(
                    pending_resource_encounter["fact"], occasion_checks):
                pass2_soft_resource_encounter = context_budget.Contribution(
                    context_budget.RESOURCE_ENCOUNTER, _resource_encounter_text(pending_resource_encounter["fact"]),
                    hard=False,
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
            # Verification itself is strictly read-only.  On a verification
            # turn do not run the ordinary receipt-reconciliation writer;
            # an interrupted send remains UNKNOWN rather than the act of
            # checking appending a new disposition.
            if live_action_verification_result is None:
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
            if pass2_hard_action_record is not None:
                pass2_contributions.append(pass2_hard_action_record)
            if pass2_hard_waymark_query is not None:
                pass2_contributions.append(pass2_hard_waymark_query)
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
            if pass2_hard_waymark_notice is not None:
                pass2_contributions.append(pass2_hard_waymark_notice)
            recost_when_shedding(
                pass2_contributions, pass2_budget, refill_unused_capacity=True,
                # RETRIEVED_HISTORY.rendered_text is the exact multi-message provider segment,
                # including role headers and boundaries.  _measure_real_text_cost measures one
                # string placed in one message, which is not that representation, so lowering its
                # cost with that probe would compare unlike forms.  Keep the conservative exact-
                # bytes bound for this contribution.
                skip_kinds=(context_budget.RETRIEVED_HISTORY,),
            )
            if pass2_soft_discord_inbound is not None:
                fit_inbound_fifo_prefix(
                    pass2_soft_discord_inbound, pending_discord_inbound,
                    pass2_contributions, pass2_budget, refill_unused_capacity=True,
                )
            if pending_external_info_result is not None:
                grow_external_info_window(
                    pass2_soft_external_info_result, pending_external_info_result["result"],
                    pass2_contributions, pass2_budget, refill_unused_capacity=True,
                )
            pass2_result = ui_turn_diagnostics.timed_stage(
                "pass2_context_composition", context_budget.compose_within_budget,
                pass2_contributions, pass2_budget,
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
                    pass2_result, pass2_contributions, pass2_budget,
                    pass2_hard_human, pass2_current_user_message, structured_schema=None,
                    refill_unused_capacity=True,
                )
            if not pass2_result.fits:
                # Clark's echoed words and the admitted human input are guaranteed room: only when the
                # ordinary budget cannot hold them does Pass 2 take the turn reserve above it; the generation
                # allowance below stays the ordinary one.
                _reserved = _compose_with_turn_reserve(pass2_contributions, pass2_budget,
                                                       refill_unused_capacity=True)
                if _reserved is not None:
                    pass2_result = _reserved
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
                else pass1_delivered_resource_encounter_event_id
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
            # RETRIEVED HISTORY IS NOT APPENDED TO THE SYSTEM MESSAGE ANY MORE.  It used to be, as a
            # JSON episode block, and that is the representation the formed candidate never saw:
            # 1,524 corpus rows, zero containing HIPPOCAMPAL_CONTEXT_V1 or historical_episode.  The
            # budget still governs it -- the same Contribution, the same delivery_units, the same
            # whole-episode trim discipline -- but what SURVIVES that budget is now re-derived from
            # the canonical ledger and projected as native role-tagged messages below, beside the
            # dialogue window, ahead of the present human message.  See
            # relational_history_projection.py.
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
            action_record_contrib = pass2_result.included_kind(context_budget.ACTION_RECORD_RESULT)
            waymark_query_contrib = pass2_result.included_kind(context_budget.WAYMARK_QUERY_RESULT)
            if pass2_waymark_read_checks:
                _action_record_in_pass2 = pass2_result.included_kind(context_budget.ACTION_RECORD_RESULT) is not None
                for _read_check in pass2_waymark_read_checks:
                    _record_waymark_event(**dict(_read_check["waymark"]["lane"], event="result_delivery",
                                                 request=_read_check["kind"][8:],
                                                 outcome=_read_check["waymark"]["outcome"],
                                                 delivered_in_composed_pass2_prompt=_action_record_in_pass2))
            if waymark_resolution is not None:
                _record_waymark_event(
                    event="result_delivery", outcome=waymark_resolution["outcome"],
                    delivered_in_composed_pass2_prompt=waymark_query_contrib is not None)
            waymark_notice_contrib = pass2_result.included_kind(context_budget.WAYMARK_NOTICE)
            delivered_waymark_handles = pass2_result.delivered_source_ids(context_budget.WAYMARK_NOTICE)
            if delivered_waymark_handles:
                delivered_waymark_surfaces = [
                    item["receipt"] for item in pending_waymark_surfaces
                    if item["handle"] in delivered_waymark_handles
                ]

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
            # RELATIONAL-HISTORY BINDING: whatever retrieved history survived the budget is
            # re-derived from the canonical ledger and projected as native role-tagged messages.
            # It sits directly after the system message and BEFORE the current session's own
            # dialogue window, because retrieved history is older than that window -- putting it
            # after would make the actor read his past as though it happened after the
            # conversation he is currently in.  The present human message stays LAST.  The system
            # message is untouched: history is never concatenated into host control.
            pass2_present_message = {"role": current_input_role,
                                     "content": pass2_iface["current_user_message"]}
            pass2_history_messages = _native_retrieved_history_messages(
                pass2_result.included_kind(context_budget.RETRIEVED_HISTORY),
                viewer_human_actor_id=(
                    human_input_authority.authenticated_actor_id
                    if human_input_authority is not None else None),
                provenance_db_path=_retrieved_history_ledger_path(),
                person_annotations=_person_annotations,
                source_paths=getattr(orch, "hippocampus_paths", None),
            )
            pass2_real_messages = (
                [{"role": "system", "content": pass2_system_content}]
                + pass2_history_messages
                + pass2_iface["dialogue_window"]
                + ([external_data_message(external_info_result_contrib.rendered_text)]
                   if external_info_result_contrib is not None
                   and external_info_result_contrib.rendered_text else [])
                + ([external_data_message(source_provenance_contrib.rendered_text)]
                   if source_provenance_contrib is not None and source_provenance_contrib.rendered_text else [])
                + ([external_data_message(discord_inbound_contrib.rendered_text)]
                   if discord_inbound_contrib is not None
                   and discord_inbound_contrib.rendered_text else [])
                + ([external_data_message(waymark_notice_contrib.rendered_text)]
                   if waymark_notice_contrib is not None and waymark_notice_contrib.rendered_text else [])
                + [pass2_present_message]
                # The receipt of Clark's own read-only check this turn: last, beside the turn it answers,
                # host-labelled (render_result's first line), never folded into the human's words.
                + ([_host_evidence_message(action_record_contrib.rendered_text, occasion_envelope)]
                   if action_record_contrib is not None and action_record_contrib.rendered_text else [])
                + ([external_data_message(waymark_query_contrib.rendered_text)]
                   if waymark_query_contrib is not None and waymark_query_contrib.rendered_text else [])
            )
            # SHARED ORDINARY EXPRESSION SEAM (conversation_direction): one plain chat completion whose
            # assistant message IS Clark's reply.  No grammar/JSON container (the retired
            # {"expression": ...} schema is the established cause of the 2026-09-22 echo and
            # meta-description replies) and no model-typed marker.  Completion is the provider's own
            # done/stop within the explicit decode reserve (call_llama -> require_complete_response);
            # exactly one call, no retry.
            # The FULL composed prompt is charged (pass2_generation_allowance takes pass2_result.final_prompt_cost
            # directly, min(PASS2_GENERATION_CEILING, WAKING_SERVING_CONTEXT - SAFETY_MARGIN - prompt)), so an
            # echo/human/waymark-extended prompt never loses reply room below what the serving contract itself
            # promises.  Every lawful occasion -- foreground or WTR0 recovery, greeting or deep-history -- gets
            # at least PASS2_GUARANTEED_GENERATION and, wherever the prompt leaves the room, the finite
            # PASS2_GENERATION_CEILING to finish its one reply in this one call (History Response Completion V0).
            pass2_generation_allowance = context_budget.pass2_generation_allowance(
                pass2_result.final_prompt_cost)
            provider_boundary_receipt.prepare(
                purpose="pass2",
                session_id=session_id,
                human_input_event_id=human_input_event_id,
                evidence=_provider_boundary_history_evidence(
                    pass2_soft_hippocampal,
                    pass2_result.included_kind(context_budget.RETRIEVED_HISTORY),
                    occasion_checks,
                    history_evidence_manifest,
                ),
            )
            try:
                raw2 = ui_turn_diagnostics.timed_model_call(
                    "pass2", call_llama, pass2_real_messages, prepared["controls"],
                    generation_reserve=pass2_generation_allowance,
                )  # the one Pass-2 call
            except context_budget.IncompleteCompletionError as exc:
                validated_expr, failure2 = None, exc.failure_code
            else:
                validated_expr, failure2 = validate_plain_reply(raw2)
                retrieval_delivery.note_composed("pass2", pass2_soft_hippocampal, pass2_result)

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
            # SUBJECT-DIRECTED CONSOLIDATION V0: tracked separately from
            # every sibling above -- dispatched further below, strictly
            # after canonical persistence succeeds, exactly mirroring
            # their ordering.
            pending_consolidation_request = validated_act["consolidation_request"]
            pending_consolidation_target = validated_act["consolidation_target"]
            pending_consolidation_interpretation = validated_act["consolidation_interpretation"]
            pending_consolidation_cues = validated_act["consolidation_cues"]
            pending_consolidation_prior_record_id = validated_act["consolidation_prior_record_id"]
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
    # PULSE V0: a Pulse occasion in which Clark chose silence and no typed act at all leaves no
    # canonical waking turn -- an operational no-op is not autobiographical dialogue (pulse.py keeps the
    # operational record).  Anything he said or chose is recorded exactly as any other turn.
    if (pulse_occasion is not None and subject_reply_choice == REPLY_REQUEST_NO_REPLY and not clark_prose
            and pending_background_activity_request == BACKGROUND_ACTIVITY_REQUEST_NONE
            and pending_sleep_timing_request == SLEEP_TIMING_REQUEST_NONE
            and pending_operative_directive_request == OPERATIVE_DIRECTIVE_REQUEST_NONE
            and pending_external_info_request == EXTERNAL_INFO_REQUEST_NONE
            and pending_discord_correspondence_request == DISCORD_CORRESPONDENCE_REQUEST_NONE
            and not validated_correspondence
            and pending_waymark_action is None
            and not delivered_waymark_surfaces):
        return {
            "reply": "", "reply_choice": subject_reply_choice, "canonical_recorded": False,
            "native_event_id": None, "kardia": prepared["kardia"], "memory_context": prepared["memory_context"],
            "artifact_result": artifact_result, "operation_status": operation_status,
            "_diagnostic_typed_act": conv_trace_pending["validated_act"] if conv_trace_pending is not None else None,
            "action_verification_result": live_action_verification_result,
            "waymark_result": {"mutation": None, "surfaces_delivered": [],
                               "outcome": waymark_resolution["outcome"] if waymark_resolution is not None else None,
                               "attempts": waymark_resolution["attempts"] if waymark_resolution is not None else []},
            "occasion_checks": [_check_summary(c) for c in occasion_checks],
            "discord_correspondence": {"outbound": None, "inbound_delivered": None, "caret_reply": None},
            "witness_gates": witness_gates, "acknowledged_receipt_statuses": list(acknowledged_receipt_statuses),
        }

    occurred_at = int(datetime.now(timezone.utc).timestamp())
    provider_boundary_history_receipt = provider_boundary_receipt.take_successful()
    native_result = ui_turn_diagnostics.timed_stage(
        "persistence", native_provenance_writer.stage_and_record_native_waking_turn,
        PROVENANCE_DB_DIR, STAGING_PATH,
        session_id=session_id, session_started_at=session_started_at,
        user_id=USER_ID, prompt=prompt, bounded_clause=bounded_clause, clark_prose=clark_prose,
        kardia=prepared["kardia"], controls=prepared["controls"],
        waking_model_tag=MODEL, pipeline_key=PIPELINE_KEY,
        **_verified_digest_kwarg(MODEL),
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
        **({"wtr0_recovery_id": wtr0_recovery_id}
           if wtr0_recovery_id is not None else {}),
        # FS1: pass the session's binding scope ONLY for authenticated
        # scoped sessions (omitted entirely otherwise -- the unbound
        # path and the integration-test fakes stay byte-identical; the
        # native writer cross-validates this against H's own scope).
        **({"visibility_scope": _family_view["visibility_scope"]}
           if _family_view is not None and human_input_event_id is not None else {}),
        # NON-HUMAN OCCASION (H-less continuity repair): a Pulse / receipt re-entry turn records what occasioned
        # it (source + exact host occasion text) and, in a bound session, that session's own real scope -- never
        # an H, never a scope the session's binding does not carry.
        **(_occasion_provenance_kwargs(pulse_occasion, prompt, session_id) if pulse_occasion is not None else {}),
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
        # CANONICAL ACTION VERIFICATION V0: exact query + host-derived
        # result observed by this Pass 2.  The native writer independently
        # recomputes it from the authority sources before accepting either
        # component, so these kwargs cannot forge completion state.
        **({
            "action_verification_query": pending_action_verification_query,
            "action_verification_result": live_action_verification_result,
        } if pending_action_verification_query is not None else {}),
        # PULSE V0: the occasion's earlier checks (each recomputed by the writer, like the last one).
        **({"prior_action_verifications": prior_occasion_verifications}
           if prior_occasion_verifications else {}),
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
        **({"waymark_action": pending_waymark_action}
           if pending_waymark_action is not None else {}),
        **({"delivered_waymark_surfaces": delivered_waymark_surfaces}
           if delivered_waymark_surfaces else {}),
        **({"history_evidence_manifest": history_evidence_manifest}
           if history_evidence_manifest is not None else {}),
        **({"provider_boundary_history_receipt": provider_boundary_history_receipt}
           if provider_boundary_history_receipt is not None else {}),
        **({"correspondent_source_ref": caret_source_ref} if caret_source_ref is not None else {}),
        **({"correspondent_standing_request": f"{validated_correspondence['standing'][0]}:{validated_correspondence['standing'][1]}"}
           if "standing" in validated_correspondence else {}),
        **({"discord_correspondent_ref": validated_act["discord_correspondent_ref"]}
           if resolved_interaction_mode == CONVERSATION_MODE and validated_act.get("discord_correspondent_ref")
           and pending_discord_correspondence_request == "send_message" else {}),
        **({"withdraw_pending_send_request": validated_correspondence["withdraw"]}
           if "withdraw" in validated_correspondence else {}),
        **_post_commit_kwargs(occasion_checks),
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
    if pending_discord_correspondence_request == DISCORD_CORRESPONDENCE_REQUEST_DRAFT_MESSAGE:
        # Clark's draft: canonical with this turn above, and NEVER dispatched (zero outward side effect).
        discord_correspondence_report = {"status": "draft_recorded_not_sent"}
    elif pending_discord_correspondence_request in discord_correspondence.SEND_REQUESTS:
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

    # SUBJECT-DIRECTED CONSOLIDATION V0: Clark's own typed consolidation
    # choice for this turn, durably recorded now -- strictly AFTER
    # canonical persistence has actually succeeded (native_result exists
    # above), mirroring pending_sleep_timing_request's and
    # pending_operative_directive_request's own ordering immediately
    # above. This is the WAKING interface to the same stewardship faculty
    # Sleep/REM also reaches: the subject can relate to its own memory
    # during ordinary waking, not only on a Sleep occasion. The occasion
    # is bound to THIS turn's own just-committed canonical waking_turn
    # event, so a retry of the same turn can never duplicate the act. A
    # rejection (unknown target, a prior record that is not Clark's own,
    # or a scope that would widen) or any other failure here is caught,
    # reported truthfully in the returned status below, and NEVER turns
    # an otherwise-successful waking turn into a failed one.
    consolidation_action_result = {"action": pending_consolidation_request, "status": "not_applicable"}
    if pending_consolidation_request != CONSOLIDATION_REQUEST_NONE:
        try:
            record = consolidation.apply_consolidation_act_in(
                PROVENANCE_DB_DIR,
                request=pending_consolidation_request,
                target_event_id=pending_consolidation_target,
                author_actor_id=CLARK_ACTOR_ID,
                interpretation_text=pending_consolidation_interpretation or None,
                cues=pending_consolidation_cues or [],
                prior_record_id=pending_consolidation_prior_record_id,
                occasion=consolidation.OCCASION_WAKING,
                occasion_event_id=native_result["event_id"],
                model_revision_id=native_result.get("model_revision_id"),
                allowed_target_event_ids=consolidation_target_event_ids,
            )
            consolidation_action_result = {
                "action": pending_consolidation_request, "status": "recorded",
                "record_id": record["record_id"] if record else None,
            }
        except consolidation.ConsolidationError as exc:
            consolidation_action_result = {
                "action": pending_consolidation_request, "status": "rejected", "detail": str(exc),
            }
        except Exception as exc:
            consolidation_action_result = {
                "action": pending_consolidation_request, "status": "error", "detail": str(exc),
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

    waymark_mutation_result = None
    if pending_waymark_action is not None and pending_waymark_action["request"] not in (
            waymark.LIST, waymark.INSPECT):
        waymark_mutation_result = {
            "request": pending_waymark_action["request"],
            "status": "recorded",
            "handle": (
                f"waymark.{native_result['event_id']}"
                if pending_waymark_action["request"] == waymark.CREATE
                else pending_waymark_action["handle"]
            ),
        }
        # Only now -- the canonical turn holds the action -- is it COMMITTED (never at acceptance).
        _record_waymark_event(
            event="commit", outcome="COMMITTED", request=waymark_mutation_result["request"],
            handle=waymark_mutation_result["handle"], waking_event_id=native_result["event_id"])

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
        # SUBJECT-DIRECTED CONSOLIDATION V0: truthful mechanical outcome
        # fields, exactly mirroring operative_directive_action_result's
        # own shape/discipline above. "not_applicable" for every turn
        # that made no consolidation choice; otherwise {"action": ...,
        # "status": "recorded"|"rejected"|"error", ...}. Never implies
        # the act influenced any particular reply -- see
        # consolidation.py for the full lifecycle.
        "consolidation_action_result": consolidation_action_result,
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
        # Same typed object that was carried to Pass 2 and atomically
        # integrity-bound to this waking event; None on a non-verification
        # turn.  This is diagnostic/result data, never Clark's wording.
        "action_verification_result": live_action_verification_result,
        # WAYMARK V0: this is post-commit mechanical outcome data.  A
        # mutation is "recorded" only after the native canonical transaction
        # above returned successfully.  Surface handles are drawn only from
        # receipts that survived Pass-2 composition and were committed in
        # that same transaction; eligibility alone is never reported as
        # delivery.
        "waymark_result": {
            "mutation": waymark_mutation_result,
            "surfaces_delivered": [r["handle"] for r in delivered_waymark_surfaces],
            "outcome": (("COMMITTED" if waymark_mutation_result is not None else waymark_resolution["outcome"])
                        if waymark_resolution is not None else None),
            "attempts": waymark_resolution["attempts"] if waymark_resolution is not None else [],
            # Read-only Waymark checks of the occasion (never canonical; nothing mutated).
            "reads": [{"request": c["kind"][8:], "outcome": c["waymark"]["outcome"], "read": c["waymark"]["read"]}
                      for c in occasion_checks if c.get("kind", "").startswith("waymark_")],
        },
        # PULSE V0: every read-only check this occasion made, in order (diagnostic/result data).
        "occasion_checks": [_check_summary(c) for c in occasion_checks],
        "canonical_recorded": True,
        "witness_gates": witness_gates,
        "acknowledged_receipt_statuses": list(acknowledged_receipt_statuses),
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
    verify_waking_substrate()   # fail closed before any waking write, as every production entry point does
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
