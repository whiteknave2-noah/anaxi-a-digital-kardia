"""WSP1-S2: one-shot supervised live workspace-action activation.

Mirrors api2_supervisor.py's own pattern: one bounded invocation, no
persistent global enablement, no reusable outstanding capability, and
no automatic loop of subject actions. The subject chooses ONE action; the
only continuation is the bounded, subject-answered discovery step in
workspace_discovery.py (the host lists, the subject chooses an item or
stops -- at most MAX_SELECTION_ROUNDS listings, then one item action).
It may be entered explicitly by an operator or
through ordinary waking's typed ``use_workspace`` act. Requires the
CURRENT process/session to already be in interaction_mode ==
conversation (spec section 8) -- refuses before any prepare_context()/
model call otherwise.

Reuses the exact same context-assembly building blocks
run_waking_turn()'s conversation-mode branch already uses (OWC4
aesthetic override -> OWC3 clause -> OWC2 dialogue window), imported
directly rather than duplicated, so there is exactly one place this
sequence is defined. Does NOT run the artifact/signal-detection gate
(classify_signal/prepare_artifact_decision) -- this narrow, explicitly
supervised pathway is not "ordinary waking" and does not claim to
replicate every one of its features; bounded_clause is always empty
here, and this is reported as an explicit limitation, not silently
assumed equivalent.

Persistence: on a successful action, Clark's final Pass-2 expression
flows through the existing REAL native_provenance_writer.
stage_and_record_native_waking_turn() exactly once -- the same
canonical/legacy/hippocampal-N+1 path any ordinary conversation-mode
turn already uses. Pass-1's raw JSON and the host boundary_result are
never persisted as if Clark had said them in conversation.
"""
import datetime
import hashlib
import json
import os
import sqlite3

import context_budget
import workspace_delivery
import workspace_discovery
import hippocampus_retrieval
import llama_anaxi as la
import ui_turn_diagnostics
import workspace_audio
import workspace_capability as wc
import workspace_direction as wd
import workspace_episode_provenance
import native_provenance_writer
import family_membership
import operative_directive
from native_turn_staging import StagingDurabilityError
from relational_history import RelationalHistory
from interaction_mode import CONVERSATION as CONVERSATION_MODE, apply_interaction_mode, apply_conversation_aesthetic
from session_dialogue_window import (
    build_authenticated_dialogue_window, build_session_dialogue_window,
    insert_dialogue_window,
)
from provenance_schema import derive_stable_id


def _executed(action):
    """The (resource_class, action) pair that has run, for failure evidence."""
    return (action["resource_class"], action["action"])


class SupervisorRefused(Exception):
    """Raised before any prepare_context()/model call -- e.g. the
    current session is not in conversation mode."""
    pass


class WorkspacePostActionPersistenceFailure(Exception):
    """A retry-safety boundary: the workspace action already ran, but
    native waking persistence then failed before canonical X committed.

    This deliberately does not inherit StagingDurabilityError. WTR may
    safely replay an ordinary turn after that exact staging failure, but
    cannot replay a workspace turn whose model-selected action could be
    selected differently on a fresh inference attempt.
    """


class UnknownActorError(Exception):
    """WSP1-P1A: raised by resolve_canonical_actor_id() when a
    syntactically valid stable_key has no corresponding row in the
    canonical actors registry (or, in the impossible case, a row whose
    stored actor_id disagrees with the deterministic derivation).
    Deliberately distinct from ValueError (malformed/empty input) so
    the two failure classes are reportable and testable separately."""
    pass


def resolve_canonical_actor_id(stable_key, conn=None):
    """WSP1-P1A: the canonical actor-resolution mechanism for this
    seam. provenance_schema.derive_stable_id() is a PURE deterministic
    hash -- it derives an actor-shaped ID for any non-empty string
    without ever checking whether that actor is actually registered.
    It establishes identity, not existence. This function establishes
    BOTH: it looks up stable_key in the authoritative canonical actors
    registry (provenance_schema.actors, stable_key UNIQUE) and only
    returns an ID once a matching row is found, additionally confirming
    the stored actor_id agrees with the deterministic derivation used
    everywhere else in the project (session_dialogue_window.
    CLARK_ACTOR_ID, native_provenance_writer.py,
    migrate_historical_data.py). No second identity system -- the
    actors table already IS the authoritative registry; this function
    is a read-only lookup against it, never an insert.

    Never infers from display name, prose, model name, or caller
    assertion. Fails closed:
      - ValueError on a malformed/empty stable_key (ill-formed input);
      - UnknownActorError on a syntactically valid stable_key with no
        registered row (valid input, unestablished actor).
    Does not special-case any one production value -- "clark" is
    resolved through exactly the same registry lookup as any other
    stable_key, never hardcoded here.

    conn: an open sqlite3.Connection to anaxi_provenance.db. Passed
    explicitly for testability (mirrors hippocampus_store.
    _resolve_actor_subtype(prov_conn, ...)); when omitted, opens the
    production DB read-only via la.PROVENANCE_DB_DIR and closes it
    before returning. Never mutates the actors table in either case."""
    if not isinstance(stable_key, str) or not stable_key.strip():
        raise ValueError("resolve_canonical_actor_id: stable_key must be a non-empty string")

    owns_conn = conn is None
    if owns_conn:
        db_path = os.path.join(la.PROVENANCE_DB_DIR, "anaxi_provenance.db")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT actor_id FROM actors WHERE stable_key = ?", (stable_key,)).fetchone()
    finally:
        if owns_conn:
            conn.close()

    if row is None:
        raise UnknownActorError(f"resolve_canonical_actor_id: no canonical actor registered for stable_key={stable_key!r}")

    established_actor_id = row[0]
    derived_actor_id = derive_stable_id("actor", stable_key)
    if established_actor_id != derived_actor_id:
        raise UnknownActorError(
            f"resolve_canonical_actor_id: registry/derivation mismatch for stable_key={stable_key!r} "
            f"(registry={established_actor_id!r}, derived={derived_actor_id!r})"
        )
    return established_actor_id


# ------------------------------------------------------------------ open journal thread
#
# Once the subject has opened (read) or written a journal entry, that thread is the CURRENT
# journal thread of this session: a mechanical host fact, never inferred from prose. A journal
# append that leaves relative_path empty continues it; relative_path "new" is the subject's
# explicit choice of a separate entry. Any other resource action (library, music, photographs)
# closes the binding, and it lives only as long as the session -- a thread in an earlier session
# is reopened by reading it, exactly as before.
NEW_JOURNAL_ENTRY = "new"
_open_journal_threads = {}


def open_journal_thread(session_id):
    return _open_journal_threads.get(session_id)


def _apply_open_journal_thread(session_id, action):
    """The action with its continuation target made explicit (typed, not guessed)."""
    if action["resource_class"] != wc.JOURNAL or action["action"] != wc.APPEND:
        return action
    if action["relative_path"].strip().casefold() == NEW_JOURNAL_ENTRY:
        return dict(action, relative_path="")
    bound = _open_journal_threads.get(session_id)
    if bound and not action["relative_path"].strip():
        return dict(action, relative_path=bound)
    return action


def _named_journal_reference(action, topic_hint):
    """The words that name the journal entry a read/list is about, or None. Precedence: what
    the subject put in the action itself (relative_path, then a plain or {"query"} content),
    then the subject's own typed topic for this turn -- never the current-thread binding, and
    never the host's reading of anyone's prose. A cursor/offset payload names nothing."""
    if action["resource_class"] != wc.JOURNAL or action["action"] not in (wc.READ, wc.LIST):
        return None, None
    path = wc.clean_journal_reference(action["relative_path"])
    if path:
        return path, "action"
    content = action["content"].strip()
    if content:
        try:
            payload = json.loads(content)
        except ValueError:
            payload = content                                    # a plain-text name
        if isinstance(payload, str):
            named = wc.clean_journal_reference(payload)
            return (named, "action") if named else (None, None)
        if isinstance(payload, dict):
            if payload.get("cursor") or payload.get("offset") is not None or payload.get("max_chars") is not None:
                return None, None
            named = wc.clean_journal_reference(payload.get("query"))
            return (named, "action") if named else (None, None)
        return None, None
    named = wc.clean_journal_reference(topic_hint)
    return (named, "topic") if named else (None, None)


def _bind_named_journal_target(workspace_paths, action, topic_hint):
    """An action that names an existing journal thread is bound to that logical artifact by the
    host (its root entry, thread delivered with it) instead of being left for the subject to
    rediscover in a listing that may not even show it. Several threads named -> the listing is
    filtered to exactly those candidates (never a pick); nothing named/found -> unchanged."""
    ref, source = _named_journal_reference(action, topic_hint)
    if not ref:
        return action
    already = action["action"] == wc.READ and wc.classify_target(
        workspace_paths, wc.JOURNAL, action["relative_path"])[0] == wc.TARGET_RESOLVED
    if already:
        return action
    target, failure = wc.resolve_journal_reference(workspace_paths, ref, prefer="root")
    if target is not None:
        keep = action["content"] if action["action"] == wc.READ and action["content"].strip().startswith("{") else ""
        return dict(action, action=wc.READ, relative_path=target, content=keep)
    if failure["code"] == wc.JOURNAL_REFERENCE_AMBIGUOUS:
        return dict(action, action=wc.LIST, relative_path="", content=json.dumps({"query": ref}))
    return action


def _track_open_journal_thread(session_id, action, performed, boundary_result):
    if not performed:
        return
    if action["resource_class"] == wc.JOURNAL and action["action"] in (wc.READ, wc.APPEND):
        result = (boundary_result or {}).get("result")
        entry_id = result.get("entry_id") if isinstance(result, dict) else None
        if entry_id:
            _open_journal_threads[session_id] = entry_id
    elif action["resource_class"] != wc.JOURNAL:
        _open_journal_threads.pop(session_id, None)


# OPEN READING (live finding 2026-09-24): a long library/vault text is delivered in prompt-sized pieces,
# but the NEXT workspace choice was never told where the last piece stopped, so "read the next part"
# could only restart at the beginning or guess.  The host states the exact item, delivered range and the
# request that continues it; the subject decides whether to (never auto-continued).  Session-scoped,
# like the open journal thread; any other resource action closes it.
_open_text_reads = {}


def _track_open_text_read(session_id, action, performed, delivered_boundary_result):
    result = (delivered_boundary_result or {}).get("result")
    if (performed and action["action"] == wc.READ and action["resource_class"] in (wc.LIBRARY, wc.NOTES)
            and isinstance(result, dict) and isinstance(result.get("content"), str) and "next_offset" in result):
        end = result["next_offset"]
        _open_text_reads[session_id] = {
            "resource_class": action["resource_class"], "relative_path": action["relative_path"],
            "start": end - len(result["content"]), "end": end, "total": result.get("total_chars"),
            "next_request": result.get("next_request") if result.get("has_more") else None,
        }
    elif performed:
        _open_text_reads.pop(session_id, None)


CONTINUE_READING = "next"


def _open_text_read_line(session_id):
    """Rendered INSIDE the workspace-choice task text.  Real model (2026-09-24, captured live-shaped prompt,
    6 seeds each): in the system section 0/6 continued; a JSON request to copy into `content` 0/6; this
    line next to the task with the typed token "next" 6/6."""
    read = _open_text_reads.get(session_id)
    if not read:
        return None
    where = f"{read['resource_class']}/{read['relative_path']}: chars {read['start']}-{read['end']}" + (
        f" of {read['total']}" if read.get("total") is not None else "")
    if not read["next_request"]:
        return f"Last reading (host fact): {where} delivered; that reaches the end of it."
    return f'Last reading (host fact): {where} delivered. To read on: action read, same relative_path, content "{CONTINUE_READING}".'


def _apply_open_text_read(session_id, action):
    """content "next" on a read of the open item is resolved by the host to that reading's exact
    continuation (typed, never guessed; never applied without the subject's own "next")."""
    if action["action"] != wc.READ or action["content"].strip().strip('"').casefold() != CONTINUE_READING:
        return action
    read = _open_text_reads.get(session_id)

    def _names(path):   # the open item by its full path or its own file name, with or without extension
        base = path.replace("\\", "/").rsplit("/", 1)[-1]
        return {n.casefold() for n in (path, path.rsplit(".", 1)[0], base, base.rsplit(".", 1)[0])}
    named = action["relative_path"].strip()
    # (Real model: after reading "Clark Kara Other/X.md" it continued with relative_path "X.md".)
    same = read and read["resource_class"] == action["resource_class"] and (
        not named or {named.casefold(), named.rsplit(".", 1)[0].casefold()} & _names(read["relative_path"]))
    if same and read["next_request"]:
        return dict(action, relative_path=read["relative_path"], content=read["next_request"])
    return action


def _open_journal_thread_contribution(session_id):
    entry_id = _open_journal_threads.get(session_id)
    lines = []
    if entry_id:
        lines.append(f"Open journal thread (host fact): {entry_id}. A journal append with relative_path empty "
                     f'continues it; relative_path "{NEW_JOURNAL_ENTRY}" starts a separate entry.')
    if not lines:
        return None
    return context_budget.Contribution(context_budget.RESOURCE_ENCOUNTER, "\n".join(lines), hard=False)



def _build_base_context(
    orch, prompt, *, human_input_event_id=None, family_view=None,
    family_feature_active=False,
):
    """Exactly OWC4 -> OWC3 -> OWC2, in that order -- the same
    sequence run_waking_turn()'s conversation-mode branch uses,
    reusing the same public functions rather than a second
    implementation of the same assembly.

    OWC9-P3 Part B: starts from the TRUE core system content (no
    legacy/hippocampal memory welded in), exactly the same repair
    OWC9-P2 already made for llama_anaxi.py's own run_waking_turn() --
    prepared["messages"]/prepared["memory_context"] stay unchanged
    (orchestration.prepare_context()'s own additive-only contract), so
    this is a pure substitution, not a new retrieval query. Falls back
    to the stub's own already-welded content when core_system_text
    isn't provided (test-double compatibility, mirrors llama_anaxi.py's
    identical fallback)."""
    prepared = orch.prepare_context(la.USER_ID, prompt)
    if family_feature_active:
        # Legacy graph continuity in family mode: the designated owner's own
        # private session only, bounded to the pre-Family boundary.
        prepared = dict(
            prepared,
            family_legacy_retrieval_text=la._legacy_owner_graph_text(orch, prompt, family_view),
        )
    pass2_messages = [dict(m) for m in prepared["messages"]]
    if pass2_messages and pass2_messages[0].get("role") == "system":
        core_system_text = prepared.get("core_system_text")
        if core_system_text is None:
            core_system_text = pass2_messages[0]["content"]
        pass2_messages[0] = {"role": "system", "content": core_system_text}
    pass2_messages = apply_conversation_aesthetic(pass2_messages, CONVERSATION_MODE, prepared["controls"].get("style_instruction"))
    pass2_messages = apply_interaction_mode(pass2_messages, CONVERSATION_MODE)

    session_id, session_started_at = la._get_native_session()
    provenance_db_path = f"{la.PROVENANCE_DB_DIR}/anaxi_provenance.db"
    if os.path.exists(provenance_db_path):
        conn = sqlite3.connect(f"file:{provenance_db_path}?mode=ro", uri=True)
        try:
            if family_feature_active and family_view is None:
                dialogue_window = []
            elif human_input_event_id is not None:
                if family_view is not None:
                    dialogue_window = build_authenticated_dialogue_window(
                        conn, human_input_event_id,
                        viewer_visibility_scope=family_view["visibility_scope"],
                        viewer_principal_actor_id=family_view["principal_actor_id"],
                        active_family_principal_ids=family_view["active_family_principal_ids"],
                    )
                else:
                    dialogue_window = build_authenticated_dialogue_window(conn, human_input_event_id)
            else:
                dialogue_window = build_session_dialogue_window(conn, session_id, la.STAGING_PATH)
        finally:
            conn.close()
    else:
        dialogue_window = []
    pass2_messages = insert_dialogue_window(pass2_messages, dialogue_window)

    base_system_content = pass2_messages[0]["content"] if pass2_messages and pass2_messages[0]["role"] == "system" else ""
    base_dialogue_window = pass2_messages[1:-1] if len(pass2_messages) >= 2 else []
    base_current_user_message = prompt
    return prepared, session_id, session_started_at, base_system_content, base_dialogue_window, base_current_user_message


def _retrieval_contributions(prepared, *, family_view=None, family_feature_active=False):
    """OWC9-P3 Part B/C: builds this call's OWN independent LEGACY_
    RETRIEVAL (atomic) / RETRIEVED_HISTORY (hippocampal, per-item
    droppable) Contributions from orchestration.prepare_context()'s
    additive structured fields -- exactly the same construction
    llama_anaxi.py's run_waking_turn() already uses for ordinary
    waking Pass-2 (spec section C: reuse the existing prepared
    snapshot, never re-query memory). Returns (legacy_contrib,
    hippocampal_contrib, hippocampal_items) -- called once per pass,
    so each pass gets its own Contribution objects (compose_within_
    budget() mutates a Contribution's droppable_units/rendered_text
    in place while trimming, so passes must never share one)."""
    # FS1 has no item-level attribution for the legacy blob, so in family
    # mode it is supplied only by the legacy owner-private seam (see
    # _build_base_context) and is otherwise empty.  Hippocampal items do carry
    # canonical attribution/scope and use FS1's real delivery predicate.
    legacy_retrieval_text = (
        (prepared.get("family_legacy_retrieval_text") or "") if family_feature_active
        else (prepared.get("legacy_retrieval_text") or "")
    )
    legacy_contrib = context_budget.Contribution(
        context_budget.LEGACY_RETRIEVAL, legacy_retrieval_text, hard=False,
    )
    hippocampal_result = prepared.get("hippocampal_retrieval_result")
    hippocampal_items = list(hippocampal_result.items) if hippocampal_result is not None else []
    if family_feature_active:
        _hconn = sqlite3.connect(f"file:{la.PROVENANCE_DB_DIR}/anaxi_provenance.db?mode=ro", uri=True)
        try:
            hippocampal_items = family_membership.permitted_hippocampal_items(
                _hconn, hippocampal_items,
                viewer_principal_actor_id=(family_view["principal_actor_id"] if family_view is not None else None),
                viewer_scope=(family_view["visibility_scope"] if family_view is not None else None),
                active_family_principal_ids=(family_view["active_family_principal_ids"] if family_view is not None else frozenset()),
            )
        finally:
            _hconn.close()
    hippocampal_units = list(reversed(hippocampal_items))  # worst-match-first, so pop(0) drops the worst first

    def _render_hippocampal_units(units):
        return hippocampus_retrieval.render_hippocampal_context(
            hippocampus_retrieval.RetrievalResult(
                query_terms=hippocampal_result.query_terms if hippocampal_result is not None else (),
                items=tuple(reversed(units)),
            )
        )

    hippocampal_contrib = context_budget.Contribution(
        context_budget.RETRIEVED_HISTORY,
        _render_hippocampal_units(hippocampal_units) if hippocampal_units else "",
        hard=False,
        droppable_units=hippocampal_units if hippocampal_units else None,
        render_fn=_render_hippocampal_units if hippocampal_units else None,
        source_ids=[item.event_id for item in hippocampal_units] if hippocampal_units else None,
    )
    return legacy_contrib, hippocampal_contrib


def _operative_directive_contribution(*, family_view=None, family_feature_active=False):
    """Build the same lower-precedence directive contribution ordinary
    waking uses, filtered through the current FS1 view.

    WSP1 is a delegated ordinary-waking interface, so omitting the active
    directive here would make the standing choice disappear precisely on
    turns that use a claimed capability.
    """
    active = operative_directive.fetch_active_directive_for_viewer(
        la.PROVENANCE_DB_DIR,
        family_feature_active=family_feature_active,
        family_view=family_view,
    )
    rendered = operative_directive.render_operative_directive_context(active)
    if not rendered:
        return None
    return context_budget.Contribution(
        context_budget.OPERATIVE_DIRECTIVE, rendered, hard=False,
        source_ids=[active["active_event_id"]],
        droppable_units=[rendered], render_fn=lambda units: units[0] if units else "",
        minimum_units=1,
    )


def _recover_human_overcount(result, contributions, budget, hard_human, human_text):
    """Ordinary waking recovers an OVERCOUNTED current human message by a
    bounded real measurement before failing closed; WSP1 must too, or an
    ordinary long message that the byte upper bound over-prices dies at
    ``workspace_pass1`` after ordinary Pass 1 already chose the workspace.

    Measurement is attempted only when the human message's overcount is
    what could flip the outcome (the hard floor minus the human message
    fits the budget). A genuinely unrecoverable overflow -- e.g. an
    oversized core -- still fails closed with zero inference, as before."""
    if result.fits:
        return result
    hard_cost = sum(c.cost for c in contributions if c.hard)
    if hard_cost - hard_human.cost > budget:
        return result
    return la._recover_hard_overflow_via_real_measurement(
        result, contributions, budget, hard_human, human_text,
    )


def _ask_target_selection(
    prepared, base_system_content, base_dialogue_window, base_current_user_message,
    human_measured_cost, listing_boundary_result, requested_action, status, allowed_surface, *,
    family_view=None, family_feature_active=False, task_text_fn=None,
):
    """One selection call: the subject sees the host's listing (a truthful
    window sized to the room the prompt really has) and answers with the same
    JSON action shape. Composed by the same aggregate authority as Pass 1
    (fixed framing/human/task text HARD; listing, dialogue, retrieval and the
    operative directive SOFT), so nothing is asked that the context cannot
    carry. Raises SelectionUnavailable rather than ever guessing a choice."""
    budget = context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET
    core, framing = la.core_and_calibrated_framing_contributions(base_system_content)
    human = context_budget.Contribution(
        context_budget.CURRENT_HUMAN_MESSAGE, base_current_user_message, hard=True,
    )
    if human_measured_cost is not None:
        human.cost = human_measured_cost
    surface_text = str({k: sorted(v) for k, v in allowed_surface.items()})

    def _task(listing_text):
        head = (
            task_text_fn(listing_text, surface_text) if task_text_fn is not None
            else workspace_discovery.selection_task_text(requested_action, status, listing_text, surface_text)
        )
        return (
            head
            + "\n\nRespond with exactly this JSON shape and nothing else -- no markdown fences, no extra keys:\n"
            '{"resource_class": "<resource class>", "action": "<action>", "relative_path": "<string, may be empty>", "content": "<string, may be empty>"}'
        )

    task = context_budget.Contribution(context_budget.MECHANICAL_STATE, _task(""), hard=True)
    hard = [core, human, task] + ([framing] if framing is not None else [])
    hard_floor = sum(c.cost for c in hard)
    if hard_floor > budget:
        context_budget.recost_by_measurement(hard, la._measure_real_text_cost_memoized)
        hard_floor = sum(c.cost for c in hard)
        if hard_floor > budget:
            raise workspace_discovery.SelectionUnavailable(context_budget.BUDGET_EXCEEDED)

    windowed, delivery = workspace_delivery.fit_boundary_result(
        listing_boundary_result, budget - hard_floor,
        measure_fn=la._measure_real_text_cost,
        measurable_limit=context_budget.CONTEXT_CEILING - context_budget.SAFETY_MARGIN - 64,
    )
    listing_contrib = context_budget.Contribution(
        context_budget.LAST_WORKSPACE_OBSERVATION, workspace_delivery.render_observation(windowed), hard=False,
    )
    if delivery.get("measured_cost") is not None:
        listing_contrib.cost = delivery["measured_cost"]

    dialogue_units = list(base_dialogue_window)
    dialogue = context_budget.Contribution(
        context_budget.RECENT_DIALOGUE, "".join(m.get("content", "") for m in dialogue_units),
        hard=False, droppable_units=dialogue_units,
        render_fn=lambda units: "".join(m.get("content", "") for m in units),
    )
    legacy, hippocampal = _retrieval_contributions(
        prepared, family_view=family_view, family_feature_active=family_feature_active,
    )
    directive = _operative_directive_contribution(
        family_view=family_view, family_feature_active=family_feature_active,
    )
    contributions = hard + [listing_contrib, dialogue, legacy, hippocampal] + ([directive] if directive is not None else [])
    la.recost_when_shedding(contributions, budget)
    result = context_budget.compose_within_budget(contributions, budget)
    result = _recover_human_overcount(result, contributions, budget, human, base_current_user_message)
    ui_turn_diagnostics.record_context_budget_result("wsp1_select", result)
    if not result.fits:
        raise workspace_discovery.SelectionUnavailable(context_budget.BUDGET_EXCEEDED)
    shown = result.included_kind(context_budget.LAST_WORKSPACE_OBSERVATION)
    if delivery.get("delivery") in ("withheld", "none") or shown is None or not shown.rendered_text:
        # The listing is the whole point of this call; never ask a subject to
        # choose from a list it was not shown.
        raise workspace_discovery.SelectionUnavailable("LISTING_NOT_DELIVERED")

    system_content = base_system_content
    for kind in (context_budget.LEGACY_RETRIEVAL, context_budget.RETRIEVED_HISTORY, context_budget.OPERATIVE_DIRECTIVE):
        included = result.included_kind(kind)
        if included is not None and included.rendered_text:
            system_content = system_content + "\n\n" + included.rendered_text
    messages = (
        [{"role": "system", "content": system_content}]
        + dialogue.droppable_units
        + [{"role": "user", "content": base_current_user_message},
           {"role": "user", "content": _task(shown.rendered_text)}]
    )
    return la.ask_llama_for_json(messages)


def _record_silent_workspace_turn(orch, prompt, prepared, session_id, session_started_at, human_input_event_id,
                                  visibility_scope, validated_action, boundary_result, performed):
    """The canonical X for use_workspace + Clark's typed no_reply: no reply pass runs and no prose
    exists; Clark's own clark_reply_choice answers the human's input (never recovery-eligible), exactly
    as on the ordinary null path.  The workspace outcome stays in the action log and trace and is
    returned to the caller -- it is a host fact, never words put in Clark's mouth."""
    wd.set_last_workspace_direction_trace({
        "action": validated_action["action"], "resource_class": validated_action["resource_class"],
        "relative_path": validated_action["relative_path"], "boundary_result": boundary_result,
        "pass1_status": "ok", "pass2_status": la.PASS2_STATUS_NOT_COMPOSED_NO_REPLY,
    })
    occurred_at = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    try:
        native_result = native_provenance_writer.stage_and_record_native_waking_turn(
            la.PROVENANCE_DB_DIR, la.STAGING_PATH,
            session_id=session_id, session_started_at=session_started_at,
            user_id=la.USER_ID, prompt=prompt, bounded_clause="", clark_prose="",
            kardia=prepared["kardia"], controls=prepared["controls"],
            waking_model_tag=la.MODEL, pipeline_key=la.PIPELINE_KEY,
            artifact_pass_ran=False, occurred_at=occurred_at,
            human_input_event_id=human_input_event_id,
            subject_reply_choice=la.REPLY_REQUEST_NO_REPLY,
            **({"visibility_scope": visibility_scope} if visibility_scope is not None else {}),
        )
    except StagingDurabilityError as exc:
        raise WorkspacePostActionPersistenceFailure(
            "workspace action completed before native waking persistence failed; "
            "retry-whole is not established safe"
        ) from exc
    model_revision_id = native_result["model_participations"][-1]["model_revision_id"]
    orch.record_turn_generation_controls(
        la.USER_ID, prepared["controls"], model_revision_id=model_revision_id,
        pipeline_id=native_result["pipeline_id"], event_id=native_result["event_id"],
    )
    la.log_entry(prompt, "", prepared["kardia"], native_result=native_result)
    return {
        "reply": "",
        "reply_choice": la.REPLY_REQUEST_NO_REPLY,
        "kardia": prepared["kardia"],
        "memory_context": prepared["memory_context"],
        "native_event_id": native_result["event_id"],
        "_native_staging_id": native_result["staging_id"],
        "boundary_result": boundary_result,
        "workspace_action_performed": performed,
    }


def run_one_supervised_workspace_action(
    orch, prompt, clark_actor_id, workspace_paths, *,
    human_input_event_id=None, visibility_scope=None,
    family_view=None, family_feature_active=False, allowed_surface=None,
    topic_hint=None, subject_reply_choice=None,
):
    """The one bounded supervised entry point.

    ``run_waking_turn()`` can enter it only after its closed Pass-1 act
    selects ``use_workspace``; the supervisor then performs its own
    closed resource/action selection. Any Pass-1/Pass-2 structural
    failure has no retry, fallback, or persistence. Raises
    SupervisorRefused if the current session is not in conversation
    mode.

    `clark_actor_id`: the opaque canonical Clark actor ID (e.g.
    provenance_schema.derive_stable_id("actor", "clark")) -- WSP1-P1
    fix: this is now the ONLY actor-identity parameter and is threaded
    into every workspace-action-log/journal-attribution call below.
    Prior to this patch, a separate, unused `clark_actor_id` and a
    second, wrongly-defaulted `requester_actor_id="clark"` parameter
    coexisted here -- only the latter (a literal stable-key string, not
    an actor ID) was ever actually used. See WSP1-P1 final report for
    the full defect trace.

    Returns the ordinary run_waking_turn()-shaped result dict:
    {"reply", "kardia", "memory_context", "native_event_id"}.
    """
    if la._interaction_mode_state.get("mode") != CONVERSATION_MODE:
        raise SupervisorRefused("workspace actions require an active conversation-mode session")
    # The shared vault is offered only when it truly exists for these paths (every caller, every call
    # below): an unavailable collection is absent from the surface, never shown as empty.
    allowed_surface = {
        k: v for k, v in (allowed_surface if allowed_surface is not None else wd.LIVE_ALLOWED_SURFACE).items()
        if k != wd.wc.NOTES or workspace_paths.notes_available()
    }
    silent = subject_reply_choice == la.REPLY_REQUEST_NO_REPLY
    if silent:
        # Clark chose use_workspace AND no reply: he acts, then stays silent.  Perception of a read only
        # happens in the reply pass, so a silent read would deliver nothing to him -- only writes are
        # offered this turn (the surface is his own choice's consequence, never widened).
        allowed_surface = {k: {wc.APPEND} for k, v in allowed_surface.items() if wc.APPEND in v}

    prepared, session_id, session_started_at, base_system_content, base_dialogue_window, base_current_user_message = _build_base_context(
        orch, prompt, human_input_event_id=human_input_event_id,
        family_view=family_view, family_feature_active=family_feature_active,
    )

    # OWC9-P3 Part B: reuses context_budget.py's existing aggregate
    # authority unchanged (no second compositor). HARD: true core
    # system content, the current human/action request, and WSP1's own
    # task/schema instruction -- unlike ordinary waking's (paused this
    # gate pending an estimator decision, see llama_anaxi.py's own
    # notes), WSP1's task instruction was measured safe to include:
    # 565 bytes vs ordinary waking Pass-1's 2541, giving a real-
    # production hard floor of ~2428 bytes against a 2944-byte budget.
    # That figure predates the 980-byte ordinary-bridge task text and
    # predates costing the 769-byte fixed conversation framing on the
    # byte estimator; live H 01M2VH12GHV3R2WJKMJQ9CASAC measured 3237
    # (see the calibrated core/framing costing below).
    # SOFT: LEGACY_RETRIEVAL/RETRIEVED_HISTORY (source C: reused
    # unchanged from prepared's own structured fields) and RECENT_
    # DIALOGUE (this call's own independent copy of the OWC2 window).
    pass1_dialogue_units = list(base_dialogue_window)
    # The fixed conversation framing inside base_system_content is costed
    # exactly as ordinary waking costs it (calibrated, fingerprint-guarded,
    # byte-estimator fallback). Under the byte estimator alone WSP1's hard
    # floor exceeded its budget for an ordinary-length human message in live
    # production (H 01M2VH12GHV3R2WJKMJQ9CASAC: 3237 > 2944). Only the costing
    # changes; the model still receives the unabridged base_system_content.
    pass1_hard_core, pass1_hard_framing = la.core_and_calibrated_framing_contributions(base_system_content)
    pass1_hard_human = context_budget.Contribution(
        context_budget.CURRENT_HUMAN_MESSAGE, base_current_user_message, hard=True,
    )
    pass1_iface = wd.build_pass1_interface(
        base_system_content, [], base_current_user_message, allowed_surface,
    )
    pass1_task_text = (
        f"{pass1_iface['task_instruction']}\n\n"
        f"Available workspace capabilities: {pass1_iface['allowed_surface']}\n\n"
        + (f"{_open_text_read_line(session_id)}\n\n" if _open_text_read_line(session_id) else "")
        + "Respond with exactly this JSON shape and nothing else -- no markdown fences, no extra keys:\n"
        '{"resource_class": "<resource class>", "action": "<action>", "relative_path": "<string, may be empty>", "content": "<string, may be empty>"}'
    )
    pass1_hard_task = context_budget.Contribution(
        context_budget.MECHANICAL_STATE, pass1_task_text, hard=True,
    )
    pass1_soft_dialogue = context_budget.Contribution(
        context_budget.RECENT_DIALOGUE,
        "".join(m.get("content", "") for m in pass1_dialogue_units),
        hard=False, droppable_units=pass1_dialogue_units,
        render_fn=lambda units: "".join(m.get("content", "") for m in units),
    )
    pass1_legacy_contrib, pass1_hippocampal_contrib = _retrieval_contributions(
        prepared, family_view=family_view, family_feature_active=family_feature_active,
    )
    pass1_directive_contrib = _operative_directive_contribution(
        family_view=family_view, family_feature_active=family_feature_active,
    )
    pass1_contributions = [
        pass1_hard_core, pass1_hard_human, pass1_hard_task, pass1_soft_dialogue,
        pass1_legacy_contrib, pass1_hippocampal_contrib,
    ]
    pass1_open_thread = _open_journal_thread_contribution(session_id)
    if pass1_open_thread is not None:
        pass1_contributions.append(pass1_open_thread)
    if pass1_hard_framing is not None:
        pass1_contributions.append(pass1_hard_framing)
    if pass1_directive_contrib is not None:
        pass1_contributions.append(pass1_directive_contrib)
    pass1_human_estimated_cost = pass1_hard_human.cost
    la.recost_when_shedding(pass1_contributions, context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET)
    pass1_result = context_budget.compose_within_budget(
        pass1_contributions,
        context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET,
    )
    pass1_result = _recover_human_overcount(
        pass1_result, pass1_contributions, context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET,
        pass1_hard_human, base_current_user_message,
    )
    pass1_human_measured_cost = (
        pass1_hard_human.cost if pass1_hard_human.cost < pass1_human_estimated_cost else None
    )
    ui_turn_diagnostics.record_context_budget_result("wsp1_pass1", pass1_result)
    if not pass1_result.fits:
        # spec Part B: hard-only overflow -> BUDGET_EXCEEDED, zero
        # inference, no fabricated workspace action/result, no retry --
        # the same host-side containment shape a structural Pass-1
        # failure already uses below.
        wd.set_last_workspace_direction_trace({
            "action": None, "resource_class": None, "relative_path": None,
            "boundary_result": None, "pass1_status": context_budget.BUDGET_EXCEEDED, "pass2_status": None,
        })
        raise wd.WorkspaceDirectionFailure("pass1", context_budget.BUDGET_EXCEEDED)

    pass1_system_content = base_system_content
    legacy_contrib_1 = pass1_result.included_kind(context_budget.LEGACY_RETRIEVAL)
    if legacy_contrib_1 is not None and legacy_contrib_1.rendered_text:
        pass1_system_content = pass1_system_content + "\n\n" + legacy_contrib_1.rendered_text
    hippocampal_contrib_1 = pass1_result.included_kind(context_budget.RETRIEVED_HISTORY)
    if hippocampal_contrib_1 is not None and hippocampal_contrib_1.rendered_text:
        pass1_system_content = pass1_system_content + "\n\n" + hippocampal_contrib_1.rendered_text
    directive_contrib_1 = pass1_result.included_kind(context_budget.OPERATIVE_DIRECTIVE)
    if directive_contrib_1 is not None and directive_contrib_1.rendered_text:
        pass1_system_content = pass1_system_content + "\n\n" + directive_contrib_1.rendered_text
    open_thread_1 = pass1_result.included_kind(context_budget.RESOURCE_ENCOUNTER)
    if open_thread_1 is not None and open_thread_1.rendered_text:
        pass1_system_content = pass1_system_content + "\n\n" + open_thread_1.rendered_text
    pass1_messages = (
        [{"role": "system", "content": pass1_system_content}]
        + pass1_soft_dialogue.droppable_units
        + [{"role": "user", "content": base_current_user_message}, {"role": "user", "content": pass1_task_text}]
    )
    raw1 = la.ask_llama_for_json(pass1_messages)

    validated_action, failure1 = wd.validate_pass1_workspace_action(raw1, allowed_surface)
    if failure1 is not None:
        wd.set_last_workspace_direction_trace({
            "action": None, "resource_class": None, "relative_path": None,
            "boundary_result": None, "pass1_status": failure1, "pass2_status": None,
        })
        raise wd.WorkspaceDirectionFailure("pass1", failure1)

    validated_action = _apply_open_journal_thread(session_id, validated_action)
    validated_action = _apply_open_text_read(session_id, validated_action)
    validated_action = _bind_named_journal_target(workspace_paths, validated_action, topic_hint)

    # An action on ONE item needs an item the host can resolve. When the
    # subject chose the action but could not name an existing item (it has
    # never seen a listing -- the live "read something from the library"
    # failure), the host LISTS and the subject CHOOSES from that listing; the
    # host never picks or substitutes an item. See workspace_discovery.
    discovery_steps = 0
    discovery_status = None
    subject_initiated_list = False
    target_unresolved = False
    discovery_resolved = False
    surface = allowed_surface if allowed_surface is not None else wd.LIVE_ALLOWED_SURFACE
    needs_discovery, discovery_status, validated_action = workspace_discovery.discovery_needed(
        workspace_paths, validated_action,
    )

    def _ask_selection(listing_boundary_result, requested_action, status):
        return _ask_target_selection(
            prepared, base_system_content, base_dialogue_window, base_current_user_message,
            pass1_human_measured_cost, listing_boundary_result, requested_action, status, surface,
            family_view=family_view, family_feature_active=family_feature_active,
        )

    def _run_discovery(initial_listing=None):
        return workspace_discovery.resolve_target(
            workspace_paths, clark_actor_id, validated_action, discovery_status if initial_listing is None else None,
            _ask_selection, surface,
            session_id=session_id, source_event_id=human_input_event_id, initial_listing=initial_listing,
        )

    # An item action whose REQUIRED parameter is missing (view_page with no page, inspect_audio
    # with no view): the host states the facts (page count, available views); the subject chooses.
    def _complete_parameters(action):
        need = workspace_discovery.parameter_choice_needed(workspace_paths, action)
        if need is None:
            return action, None
        try:
            raw_choice = _ask_target_selection(
                prepared, base_system_content, base_dialogue_window, base_current_user_message,
                pass1_human_measured_cost, need["boundary_result"], action, None, surface,
                family_view=family_view, family_feature_active=family_feature_active,
                task_text_fn=lambda text, surface_text: workspace_discovery.parameter_task_text(
                    action, need, text, surface_text,
                ),
            )
        except workspace_discovery.SelectionUnavailable:
            return action, None          # cannot ask honestly: the executor reports the truthful failure
        outcome, chosen, _failure = workspace_discovery.interpret_parameter_choice(
            workspace_paths, action, raw_choice, surface,
        )
        return (chosen, "chosen") if outcome == workspace_discovery.RESOLVED else (action, "declined")

    if needs_discovery:
        discovery = _run_discovery()
    else:
        validated_action, _parameter_outcome = _complete_parameters(validated_action)
        boundary_result, _performed = wd.execute_workspace_action(
            workspace_paths, validated_action, clark_actor_id,
            session_id=session_id, source_event_id=human_input_event_id,
        )
        discovery = None
        if (
            _performed and validated_action["action"] == wc.LIST
            and validated_action["resource_class"] in wc.TARGET_BEARING_ACTIONS
        ):
            # The subject chose to LIST. It has now seen what exists; give it the
            # same chance to act on an item (or stop) that it would have had
            # if the host had listed for it. One neutral choice, never a prompt to act.
            subject_initiated_list = True
            discovery = _run_discovery(initial_listing=boundary_result)
    if discovery is not None:
        # Durable, content-free record of how the selection step ended (live 2026-09-24: "he stopped" and
        # "the step was unavailable" were indistinguishable after the fact -- only in-memory diagnostics).
        wc._log_action(workspace_paths, validated_action["resource_class"], "selection",
                       (discovery.get("action") or {}).get("relative_path"), clark_actor_id,
                       {workspace_discovery.RESOLVED: "resolved", workspace_discovery.FINISHED: "stopped_by_subject"}
                       .get(discovery["outcome"], "unresolved"), None,
                       {"listing_steps": discovery["listing_steps"], "failure": discovery.get("failure")})
        discovery_steps = discovery["listing_steps"]
        if discovery["outcome"] == workspace_discovery.RESOLVED:
            discovery_resolved = True
            validated_action, _parameter_outcome = _complete_parameters(discovery["action"])
            boundary_result, _performed = wd.execute_workspace_action(
                workspace_paths, validated_action, clark_actor_id,
                session_id=session_id, source_event_id=human_input_event_id,
            )
        elif subject_initiated_list:
            # The subject stopped (or no honest selection could be made): the
            # listing it asked for IS the result it receives.
            boundary_result, _performed = discovery["last_listing"], True
        else:
            boundary_result = workspace_discovery.unresolved_boundary_result(
                validated_action, discovery_status, discovery["last_listing"],
                "SUBJECT_STOPPED" if discovery["outcome"] == workspace_discovery.FINISHED else discovery["failure"],
            )
            _performed = False
            target_unresolved = True

    # A document with no text layer (scanned / image-only PDF) cannot be read as
    # text; the subject may instead choose to view a page of it. One neutral
    # follow-up choice, never a page picked by the host.
    text_layer_note = ""
    if workspace_discovery.text_layer_alternative_available(validated_action, _performed, boundary_result):
        try:
            raw_alternative = _ask_target_selection(
                prepared, base_system_content, base_dialogue_window, base_current_user_message,
                pass1_human_measured_cost, workspace_discovery.alternative_boundary_result(boundary_result),
                validated_action, None, surface,
                family_view=family_view, family_feature_active=family_feature_active,
                task_text_fn=lambda text, surface_text: workspace_discovery.alternative_task_text(
                    validated_action, text, surface_text,
                ),
            )
        except workspace_discovery.SelectionUnavailable:
            raw_alternative = None
        if raw_alternative is not None:
            alt_outcome, alt_action, _alt_failure = workspace_discovery.interpret_alternative(
                workspace_paths, validated_action, raw_alternative, surface,
            )
            if alt_outcome == workspace_discovery.RESOLVED:
                text_layer_note = (
                    "Your read of this document found no text layer (PDF_TEXT_UNAVAILABLE); "
                    "you then chose to view a page of it as an image."
                )
                validated_action, _parameter_outcome = _complete_parameters(alt_action)
                boundary_result, _performed = wd.execute_workspace_action(
                    workspace_paths, validated_action, clark_actor_id,
                    session_id=session_id, source_event_id=human_input_event_id,
                )

    _track_open_journal_thread(session_id, validated_action, _performed, boundary_result)

    if silent:
        return _record_silent_workspace_turn(
            orch, prompt, prepared, session_id, session_started_at, human_input_event_id,
            visibility_scope, validated_action, boundary_result, _performed)

    # CAP2-B: boundary_result itself is ALWAYS JSON-safe/metadata-only
    # by construction now (wd.execute_workspace_action() strips raw
    # image_bytes before ever assigning `result` -- see that module's
    # own get_and_clear_last_view_image_bytes() docstring for why,
    # including the shared-roaming-pathway hazard this specifically
    # avoids). This supervisor is the one caller that actually
    # retrieves the real bytes, via the one-shot side channel,
    # immediately after dispatch -- never speculatively, never more
    # than once per action.
    is_photograph_view = (
        validated_action["resource_class"] == wc.PHOTOGRAPHS and validated_action["action"] == wc.VIEW
    )
    is_pdf_page_view = (
        validated_action["resource_class"] == wc.LIBRARY and validated_action["action"] == wc.VIEW_PAGE
    )
    is_image_view = is_photograph_view or is_pdf_page_view
    delivered_image_bytes = wd.get_and_clear_last_view_image_bytes() if is_image_view and _performed else None
    delivered_image_sha256 = (
        boundary_result["result"].get("source_sha256") or boundary_result["result"].get("sha256")
        if delivered_image_bytes is not None and isinstance(boundary_result.get("result"), dict)
        else None
    )

    # CAP2E-P1: REQUIRED ORDERING -- source validation and decode
    # already happened above (wc.deliver_photograph_bytes(), inside
    # wd.execute_workspace_action()); the bounded delivery
    # REPRESENTATION is derived HERE, immediately, before any budget
    # contribution is built from it -- the dimensions/cost used for
    # Qwen admission below describe THIS representation, never the
    # original source dimensions, and the bytes attached to the final
    # model call (further below) are these SAME representation bytes,
    # never a different pair of pixels than what was costed. Never
    # written to disk; `delivered_representation` is a purely in-
    # memory, ephemeral inference payload for this one turn. The
    # ORIGINAL photograph on disk (and delivered_image_sha256, its own
    # unrelated digest) is completely untouched by this step.
    delivered_representation = (
        wc.derive_bounded_vision_representation(delivered_image_bytes)
        if delivered_image_bytes is not None
        else None
    )

    # OWC9-P2/P3 Part B/C: the workspace action's own mechanical result
    # (workspace context "according to actual source semantics" --
    # CAP1/WSP2-P4's own established finding that genuine resource
    # content, e.g. a library read, reaches this exact path) is a SOFT,
    # atomic LAST_WORKSPACE_OBSERVATION contribution -- the same kind
    # OWC9-P1 already established for roaming's own analogous
    # last_workspace_observation, most-protected/dropped-last, never
    # re-truncated beyond its own existing bound (e.g. read_library_
    # bounded()'s own max_chars). Action/boundary/consequence/rationale
    # stay small, fixed-shape, HARD mechanical framing (unlike `result`,
    # none of these four fields is unbounded).
    pass2_dialogue_units = list(base_dialogue_window)   # prior turns exactly as persisted
    # Same costing as Pass 1 above (see the note there): calibrated fixed
    # framing, unabridged text still sent. A hard-floor overflow here would
    # surface only AFTER the workspace action had already executed.
    pass2_hard_core, pass2_hard_framing_split = la.core_and_calibrated_framing_contributions(base_system_content)
    pass2_hard_human = context_budget.Contribution(
        context_budget.CURRENT_HUMAN_MESSAGE, base_current_user_message, hard=True,
    )
    if pass1_human_measured_cost is not None:
        # Byte-identical H, same model/chat template: keep the exact
        # turn-local measurement Pass 1 obtained (never cached across turns).
        pass2_hard_human.cost = pass1_human_measured_cost
    audio_delivered = (
        _performed and validated_action["resource_class"] == wc.MUSIC
        and validated_action["action"] in (wc.LISTEN, wc.INSPECT_AUDIO, wc.OBSERVE)
    )
    pass2_iface = wd.build_pass2_interface(base_system_content, [], base_current_user_message, validated_action, boundary_result)
    pass2_framing_text = (
        f"{pass2_iface['task_instruction']}\n\n"
        f"{'Action requested (not performed)' if target_unresolved else 'Action taken'}: "
        f"{validated_action['action']} on {validated_action['resource_class']}"
        + (f" ({validated_action['relative_path']})" if validated_action["relative_path"] else "") + "\n"
        f"Boundary: {boundary_result['boundary']}\n"
        f"Consequence: {boundary_result['consequence']}\n"
        f"Rationale: {boundary_result['rationale']}"
        + (f"\n{text_layer_note}" if text_layer_note else "")
        + (f"\n{workspace_audio.AUDIO_DELIVERY_FRAMING}" if audio_delivered else "")
        + (
            f"\n{workspace_discovery.describe_discovery(discovery_steps, discovery_resolved, subject_initiated_list)}"
            if discovery_steps and (discovery_resolved or not subject_initiated_list) else ""
        )
    )
    # SHARED ORDINARY EXPRESSION SEAM (conversation_direction): the narration is one plain chat
    # completion whose text IS Clark's reply.  The earlier JSON envelope failed MALFORMED_EXPRESSION
    # about 1 in 8 real turns (an unescaped quotation mark) and the marker handshake that replaced it
    # failed whenever the model left a protocol string out -- each AFTER the workspace action had run.
    # Completion is the provider's own stop within its limits (call_llama); nothing is typed back.
    pass2_json_shape_text = "Reply in plain prose (no JSON, no markdown fences)."
    pass2_hard_framing = context_budget.Contribution(
        context_budget.MECHANICAL_STATE, pass2_framing_text + "\n\n" + pass2_json_shape_text, hard=True,
    )
    pass2_soft_dialogue = context_budget.Contribution(
        context_budget.RECENT_DIALOGUE,
        "".join(m.get("content", "") for m in pass2_dialogue_units),
        hard=False, droppable_units=pass2_dialogue_units,
        render_fn=lambda units: "".join(m.get("content", "") for m in units),
    )
    pass2_legacy_contrib, pass2_hippocampal_contrib = _retrieval_contributions(
        prepared, family_view=family_view, family_feature_active=family_feature_active,
    )
    pass2_directive_contrib = _operative_directive_contribution(
        family_view=family_view, family_feature_active=family_feature_active,
    )
    pass2_contributions = [
        pass2_hard_core, pass2_hard_human, pass2_hard_framing, pass2_soft_dialogue,
        pass2_legacy_contrib, pass2_hippocampal_contrib,
    ]
    if pass2_hard_framing_split is not None:
        pass2_contributions.append(pass2_hard_framing_split)
    if pass2_directive_contrib is not None:
        pass2_contributions.append(pass2_directive_contrib)
    # CAP2-B/CAP2E: the photograph's real, modality-specific admission
    # cost is a HARD contribution (see context_budget.build_image_
    # contribution()/build_qwen_image_contribution() for why) -- present
    # ONLY when a photograph was genuinely delivered by the host above.
    # If this pushes the hard set over budget, compose_within_budget()'s
    # existing hard-overflow path fires exactly as it already does for
    # every other hard contribution: BUDGET_EXCEEDED, zero inference,
    # the image is never sent partially or silently dropped.
    #
    # CAP2E: MODEL SELECTION HAPPENS HERE, MECHANICALLY, BEFORE the
    # budget composition and BEFORE the final model call -- never
    # inferred from prose, never decided after the fact. A genuine
    # admitted image routes this turn's expression to VISION_MODEL
    # (qwen3-vl:4b, the only model CAP2D-P1 proved grounds on pixels on
    # this installation) under ITS OWN measured budget profile
    # (QWEN_VISION_MAX_PROMPT_BUDGET) -- reusing WSP1_PASS2_MAX_PROMPT_
    # BUDGET here would silently apply gemma4:e4b's own generation-
    # reserve/image-cost assumptions to a materially different model,
    # exactly the mistake this gate's own budget audit was run to avoid.
    # Every non-image action is completely unaffected: pass2_target_model
    # stays la.MODEL and pass2_budget stays WSP1_PASS2_MAX_PROMPT_BUDGET,
    # byte-for-byte the pre-CAP2E behavior.
    pass2_image_contrib = None
    pass2_target_model = la.MODEL
    pass2_budget = context_budget.WSP1_PASS2_MAX_PROMPT_BUDGET
    if delivered_image_bytes is not None:
        # CAP2E-P1: cost is derived from the REPRESENTATION's own
        # dimensions -- never the original source's -- since that is
        # what actually gets attached to the model call below.
        pass2_image_contrib = context_budget.build_qwen_image_contribution(
            delivered_representation["width"], delivered_representation["height"],
        )
        pass2_contributions.append(pass2_image_contrib)
        pass2_target_model = la.VISION_MODEL
        pass2_budget = context_budget.QWEN_VISION_MAX_PROMPT_BUDGET
    # The action has executed and its result is in hand. Owner law: it must
    # reach the subject, not be dropped whole because it is larger than the
    # room Pass 2 has left. The observation is delivered as a truthful
    # window of that same result (continuation fields rewritten to match)
    # sized to the room left after every HARD contribution; whatever the
    # subject was given is exactly what provenance below records.
    pass2_hard_floor = sum(c.cost for c in pass2_contributions if c.hard)
    if (
        pass2_delivery_needs_room := (
            context_budget.estimate_tokens(workspace_delivery.render_observation(boundary_result))
            > pass2_budget - pass2_hard_floor
        )
    ) and boundary_result.get("result") is not None:
        # The observation would not ride whole under byte costs: re-cost the
        # hard atomic control text by real measurement first, so the window is
        # sized to the room the prompt REALLY has (same authority as above).
        context_budget.recost_by_measurement(
            [c for c in pass2_contributions if c.hard],
            la._measure_real_text_cost_memoized,
        )
        pass2_hard_floor = sum(c.cost for c in pass2_contributions if c.hard)
    boundary_result, pass2_delivery = workspace_delivery.fit_boundary_result(
        boundary_result, pass2_budget - pass2_hard_floor,
        measure_fn=la._measure_real_text_cost,
        measurable_limit=context_budget.CONTEXT_CEILING - context_budget.SAFETY_MARGIN - 64,
    )
    _track_open_text_read(session_id, validated_action, _performed, boundary_result)
    pass2_soft_result = context_budget.Contribution(
        context_budget.LAST_WORKSPACE_OBSERVATION,
        workspace_delivery.render_observation(boundary_result), hard=False,
    )
    if pass2_delivery.get("measured_cost") is not None:
        # Provider-measured, not estimated: the same authority ordinary
        # waking already accepts for recovering an overcounted message.
        pass2_soft_result.cost = pass2_delivery["measured_cost"]
    pass2_contributions.append(pass2_soft_result)
    # The image contribution above and the observation carry deliberately set
    # costs (skipped by re-costing); everything atomic and byte-costed is
    # re-costed by real measurement only if composition would otherwise shed.
    la.recost_when_shedding(pass2_contributions, pass2_budget)
    pass2_result = context_budget.compose_within_budget(
        pass2_contributions,
        pass2_budget,
    )
    pass2_result = _recover_human_overcount(
        pass2_result, pass2_contributions, pass2_budget, pass2_hard_human, base_current_user_message,
    )
    ui_turn_diagnostics.record_context_budget_result("wsp1_pass2", pass2_result)
    if not pass2_result.fits:
        wd.set_last_workspace_direction_trace({
            "action": validated_action["action"], "resource_class": validated_action["resource_class"],
            "relative_path": validated_action["relative_path"], "boundary_result": boundary_result,
            "pass1_status": "ok", "pass2_status": context_budget.BUDGET_EXCEEDED,
        })
        raise wd.WorkspaceDirectionFailure("pass2", context_budget.BUDGET_EXCEEDED, _executed(validated_action))

    pass2_system_content = base_system_content
    legacy_contrib_2 = pass2_result.included_kind(context_budget.LEGACY_RETRIEVAL)
    if legacy_contrib_2 is not None and legacy_contrib_2.rendered_text:
        pass2_system_content = pass2_system_content + "\n\n" + legacy_contrib_2.rendered_text
    hippocampal_contrib_2 = pass2_result.included_kind(context_budget.RETRIEVED_HISTORY)
    if hippocampal_contrib_2 is not None and hippocampal_contrib_2.rendered_text:
        pass2_system_content = pass2_system_content + "\n\n" + hippocampal_contrib_2.rendered_text
    directive_contrib_2 = pass2_result.included_kind(context_budget.OPERATIVE_DIRECTIVE)
    if directive_contrib_2 is not None and directive_contrib_2.rendered_text:
        pass2_system_content = pass2_system_content + "\n\n" + directive_contrib_2.rendered_text

    # spec Part B/D: ELIGIBLE result content != content actually
    # delivered to the model -- only the surviving (possibly dropped-
    # under-pressure) result text is spliced into the task message;
    # if LAST_WORKSPACE_OBSERVATION was dropped for budget, the model
    # never sees a "Result:" line at all rather than a fabricated one.
    result_contrib = pass2_result.included_kind(context_budget.LAST_WORKSPACE_OBSERVATION)
    pass2_task_text = pass2_framing_text + "\n\n"
    if result_contrib is not None and result_contrib.rendered_text:
        pass2_task_text += result_contrib.rendered_text + "\n\n"
    pass2_task_text += pass2_json_shape_text
    pass2_real_messages = (
        [{"role": "system", "content": pass2_system_content}]
        + pass2_soft_dialogue.droppable_units
        + [{"role": "user", "content": base_current_user_message}, {"role": "user", "content": pass2_task_text}]
    )
    # CAP2-B: images are attached ONLY when the image contribution
    # actually survived composition above (it is HARD, so "survived"
    # here is equivalent to "the whole call fits at all" -- but checked
    # explicitly rather than assumed, so a future change to this
    # contribution's hardness can never silently start sending pixels
    # that were never actually admitted).
    pass2_images = None
    if pass2_image_contrib is not None and pass2_result.included_kind(pass2_image_contrib.kind) is pass2_image_contrib:
        # CAP2E-P1: the SAME bytes that were costed above -- never the
        # original source bytes -- so admission and delivery can never
        # silently diverge.
        pass2_images = [delivered_representation["representation_bytes"]]
    vision_runtime = (
        {"num_ctx": context_budget.QWEN_VISION_RUNTIME_CONTEXT,
         "num_predict": context_budget.QWEN_VISION_RUNTIME_MAX_GENERATION}
        if pass2_images is not None else None
    )
    try:
        raw2_original = la.call_llama(
            pass2_real_messages, prepared["controls"], images=pass2_images, model=pass2_target_model,
            **({"runtime_options": vision_runtime} if vision_runtime else {}),
        )
    except context_budget.IncompleteCompletionError as exc:
        wd.set_last_workspace_direction_trace({
            "action": validated_action["action"], "resource_class": validated_action["resource_class"],
            "relative_path": validated_action["relative_path"], "boundary_result": boundary_result,
            "pass1_status": "ok", "pass2_status": exc.failure_code,
        })
        raise wd.WorkspaceDirectionFailure("pass2", exc.failure_code, _executed(validated_action)) from exc
    raw2 = la.strip_code_fence(raw2_original.strip())
    # A model that nevertheless answers with an exact {"expression": "..."} object is still read
    # structurally (never parsed out of prose); anything else is the plain reply itself.
    validated_expr, failure2 = wd.validate_pass2_expression(raw2)
    if failure2 is not None:
        validated_expr, failure2 = la.validate_plain_reply(raw2_original)
    if failure2 is not None:
        wd.set_last_workspace_direction_trace({
            "action": validated_action["action"], "resource_class": validated_action["resource_class"],
            "relative_path": validated_action["relative_path"], "boundary_result": boundary_result,
            "pass1_status": "ok", "pass2_status": failure2,
        })
        raise wd.WorkspaceDirectionFailure("pass2", failure2, _executed(validated_action))

    wd.set_last_workspace_direction_trace({
        "action": validated_action["action"], "resource_class": validated_action["resource_class"],
        "relative_path": validated_action["relative_path"], "boundary_result": boundary_result,
        "pass1_status": "ok", "pass2_status": "ok",
    })

    clark_prose = validated_expr["expression"].strip()

    occurred_at = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    try:
        native_result = native_provenance_writer.stage_and_record_native_waking_turn(
            la.PROVENANCE_DB_DIR, la.STAGING_PATH,
            session_id=session_id, session_started_at=session_started_at,
            user_id=la.USER_ID, prompt=prompt, bounded_clause="", clark_prose=clark_prose,
            kardia=prepared["kardia"], controls=prepared["controls"],
            # CAP2E: the CANONICAL model_participation record must name the
            # model that actually produced clark_prose -- pass2_target_model
            # is la.VISION_MODEL for a genuine photograph-VIEW turn, la.MODEL
            # for every other action, never hardcoded here. Attributing a
            # qwen3-vl:4b-produced expression to gemma4:e4b's own model
            # revision would be a false canonical record, not a cosmetic gap.
            waking_model_tag=pass2_target_model, pipeline_key=la.PIPELINE_KEY,
            artifact_pass_ran=False, occurred_at=occurred_at,
            human_input_event_id=human_input_event_id,
            **({"visibility_scope": visibility_scope} if visibility_scope is not None else {}),
        )
    except StagingDurabilityError as exc:
        raise WorkspacePostActionPersistenceFailure(
            "workspace action completed before native waking persistence failed; "
            "retry-whole is not established safe"
        ) from exc
    # Same ordered legacy projection run_waking_turn() uses, minus the
    # artifact/signal-gate machinery (this narrow supervised pathway
    # does not run classify_signal()/prepare_artifact_decision() at
    # all -- an explicit, documented limitation, not a silent gap).
    prose_model_revision_id = native_result["model_participations"][-1]["model_revision_id"]
    orch.record_turn_generation_controls(
        la.USER_ID, prepared["controls"], model_revision_id=prose_model_revision_id,
        pipeline_id=native_result["pipeline_id"], event_id=native_result["event_id"],
    )
    la.log_entry(prompt, clark_prose, prepared["kardia"], native_result=native_result)

    # CAP2-E: canonical resource-encounter provenance -- recorded ONLY
    # here, after native persistence has already committed, so a fact
    # is never written for a turn that didn't actually complete. Scoped
    # to READ-shaped actions (library.read, journal.read, photographs.
    # view) -- the ones where content is delivered INTO the model's
    # context -- never journal.append (Clark's own authored output is
    # not something Clark "encountered"). `genuinely_delivered` reflects
    # what actually survived this turn's own budget composition, not
    # what was merely eligible.
    source_content_sha256 = None
    # SLP1-A4: the exact replayable representation genuinely delivered
    # this turn, when one is legitimate for this modality -- text and
    # bounded audio measurement only (see workspace_episode_provenance.
    # record_resource_encounter()'s own consistency/gating discipline).
    # Photographs deliberately never populate either (Task 6: no pixel
    # persistence for Sleep -- Clark's own conversational_prose is the
    # correct v1 selection-bearing trace for a photograph encounter).
    delivered_text = None
    delivered_measurement = None
    is_music_audio = (
        validated_action["resource_class"] == wc.MUSIC
        and validated_action["action"] in (wc.LISTEN, wc.INSPECT_AUDIO, wc.OBSERVE)
    )
    if validated_action["action"] in (wc.READ, wc.VIEW, wc.VIEW_PAGE, wc.LISTEN, wc.INSPECT_AUDIO, wc.OBSERVE) and _performed:
        if is_music_audio:
            # CAP2F: content_sha256 is the digest of the bounded
            # rendered representation actually delivered into the
            # prompt (the AUDIO_SOURCE_V1/AUDIO_VIEW_V1 dict's own JSON
            # text) -- never the raw audio bytes, and never a claim
            # that Clark "heard" the source (MEASUREMENT != HEARING).
            # source_content_sha256 is the original audio FILE's own
            # digest, computed by decode_audio_bounded() over the exact
            # bytes it genuinely decoded -- proving real decode
            # happened, not merely that a path existed.
            audio_result = boundary_result.get("result") or {}
            rendered_text_for_hash = json.dumps(audio_result, sort_keys=True)
            content_sha256 = hashlib.sha256(rendered_text_for_hash.encode("utf-8")).hexdigest()
            source_content_sha256 = audio_result.get("sha256") or audio_result.get("source_sha256")
            modality = "audio"
            if validated_action["action"] == wc.LISTEN:
                delivered_portion = "neutral source orientation, full (see workspace_audio.AUDIO_SOURCE_V1)"
            elif validated_action["action"] == wc.OBSERVE:
                segment_info = audio_result.get("analysis") or {}
                delivered_portion = (
                    f"whole-source acoustic observation (profile + temporal map): "
                    f"{segment_info.get('segments_planned')} segments "
                    f"({segment_info.get('segments_deep')} deep, {segment_info.get('segments_map_only')} map-only), bounded"
                )
            else:
                interval = audio_result.get("interval_seconds") or [None, None]
                delivered_portion = f"view={audio_result.get('view')}, interval={interval[0]}-{interval[1]}s, bounded"
            genuinely_delivered = result_contrib is not None and bool(result_contrib.rendered_text) and pass2_delivery.get("delivery") != "withheld"
            # SLP1-A4: the EXACT same serialization already hashed above
            # -- never re-derived, never re-serialized a second time,
            # so the persisted replayable representation and
            # content_sha256 can never silently diverge.
            delivered_measurement = rendered_text_for_hash
        elif is_image_view:
            # CAP2E-P1: content_sha256 now describes the REPRESENTATION
            # actually delivered to the model (resized when the source
            # exceeded the vision working plateau); source_content_sha256
            # preserves the ORIGINAL resource's own, separate identity --
            # the two are only ever equal when
            # source_and_representation_identical is True (no resize
            # occurred, e.g. the source was already <=1024 on both axes).
            content_sha256 = delivered_representation["sha256"]
            source_content_sha256 = delivered_image_sha256
            modality = "image"
            if is_pdf_page_view:
                page_result = boundary_result.get("result") or {}
                delivered_portion = (
                    f"PDF page {page_result.get('page_number')} of {page_result.get('page_count')}, "
                    f"rendered pixels {delivered_representation['width']}x{delivered_representation['height']}, bounded"
                )
            else:
                delivered_portion = (
                    f"image, resized to {delivered_representation['width']}x{delivered_representation['height']}, bounded"
                    if not delivered_representation["source_and_representation_identical"]
                    else "full image, bounded (see workspace_capability.MAX_IMAGE_BYTES/MAX_IMAGE_DIMENSION)"
                )
            genuinely_delivered = pass2_images is not None
        else:
            text_result = boundary_result.get("result") or {}
            windowed_text = validated_action["resource_class"] in (wc.LIBRARY, wc.NOTES)
            content_text = text_result.get("content") if windowed_text else json.dumps(text_result, sort_keys=True)
            content_sha256 = hashlib.sha256((content_text or "").encode("utf-8")).hexdigest()
            modality = "text"
            if windowed_text:
                # The exact delivered range (after prompt-room windowing); a vault note also states its
                # length, so the next turn knows how much remains.  (A vault read was recorded as
                # "journal entry, full" until 2026-09-24 -- false for a note, false for a window.)
                delivered_portion = f"chars {text_result.get('next_offset', 0) - len(content_text or '')}-{text_result.get('next_offset', 0)}"
                if validated_action["resource_class"] == wc.NOTES and text_result.get("total_chars") is not None:
                    delivered_portion += f" of {text_result['total_chars']}"
            else:
                delivered_portion = "journal entry, full"
            genuinely_delivered = result_contrib is not None and bool(result_contrib.rendered_text) and pass2_delivery.get("delivery") != "withheld"
            # SLP1-A4: exactly the same string content_sha256 was just
            # computed over -- never re-fetched, never re-derived.
            delivered_text = content_text or ""
        _provenance = (boundary_result.get("result") or {}).get("resource_provenance") if isinstance(boundary_result.get("result"), dict) else None
        try:
            workspace_episode_provenance.record_resource_encounter(
                la.PROVENANCE_DB_DIR,
                clark_actor_id=clark_actor_id, pipeline_key=la.PIPELINE_KEY, occurred_at=occurred_at,
                resource_class=validated_action["resource_class"], relative_path=validated_action["relative_path"],
                modality=modality, delivered_portion=delivered_portion, content_sha256=content_sha256,
                source_action=validated_action["action"], target_model_pathway=pass2_target_model,
                genuinely_delivered=genuinely_delivered, model_revision_id=prose_model_revision_id,
                source_content_sha256=source_content_sha256,
                # SLP1-A3: this WSP1 supervised turn's own already-committed
                # native waking-turn event_id -- the exact, positively-
                # possessed triggering event, never a guess or a backlink
                # manufactured for a caller that lacks one.
                triggering_waking_event_id=native_result["event_id"],
                # SLP1-A4: forward-only exact replayable representation --
                # record_resource_encounter() itself gates persistence on
                # genuinely_delivered and verifies hash consistency; a
                # dropped/failed delivery writes no replayable component
                # even though these are passed unconditionally here.
                delivered_text=delivered_text,
                delivered_measurement=delivered_measurement,
                # The owner's filing folder rides with the encounter, so the NEXT
                # waking turn's continuity can still say where the item came from.
                source_folder=(_provenance or {}).get("source_folder"),
            )
        except workspace_episode_provenance.EpisodeProvenanceError:
            pass  # host-side provenance bookkeeping only -- never blocks an already-committed turn

    history = RelationalHistory(la.RELATIONAL_DB_PATH)
    history.record_event(
        user_id=la.USER_ID, substrate="llama", external_observation=prompt,
        agent_response=clark_prose, model_revision_id=prose_model_revision_id,
        pipeline_id=native_result["pipeline_id"], auth_context_id=native_result["auth_context_id"],
        event_id=native_result["event_id"],
    )
    history.close()

    # Clark's own typed Sleep choice, made after this reply committed -- the same step the ordinary path
    # runs (llama_anaxi.dispatch_clark_sleep_decision). A workspace turn cannot also carry a Pass-1 Sleep
    # field (they are mutually exclusive), so without this a Sleep request made on a workspace turn had
    # no channel at all. Never raises and never affects the committed turn.
    sleep_timing_action_result = la.dispatch_clark_sleep_decision(
        human_message=prompt, clark_reply=clark_prose, session_id=session_id,
        session_started_at=session_started_at, occurred_at=occurred_at,
        human_input_event_id=human_input_event_id,
    )

    return {
        "reply": clark_prose,
        "kardia": prepared["kardia"],
        "memory_context": prepared["memory_context"],
        "native_event_id": native_result["event_id"],
        "_native_staging_id": native_result["staging_id"],
        "boundary_result": boundary_result,
        "sleep_timing_action_result": sleep_timing_action_result,
    }
