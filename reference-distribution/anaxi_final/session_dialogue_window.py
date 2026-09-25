"""Exact, bounded dialogue continuity for ordinary waking.

Authenticated ordinary turns use explicit canonical H/X links for the same
human and pipeline across process sessions. A restart changes execution
provenance, not which participant authored the immediately preceding reply.
The original current-session/staging API below remains the unbound fallback;
the OWC2-S1 description that follows documents that legacy API only.

Frozen principle (see OWC1's diagnostic report): current-session
dialogue is NOT hippocampal long-term memory. This module never
performs semantic retrieval, text-similarity matching, embedding
search, or timestamp-only pairing. It reuses three already-existing
primitives only:

  - the per-process `session_id` (llama_anaxi.py's `_get_native_session()`,
    unchanged by this module -- minted lazily, reused for every turn
    in one process run);
  - canonical Clark-authored turns, in `event_components`
    (`event_type='waking_turn'`, `component_kind='conversational_prose'`,
    `creator_actor_id` = the canonical Clark actor);
  - the durable human-prompt staging file (`native_turn_staging.jsonl`,
    read via `native_turn_staging.read_verified_staging_entries()` --
    never re-parsed by hand here).

Pairing is exclusively by `event_id` -- never by timestamp proximity,
text similarity, or any semantic signal. A canonical Clark turn with
no matching staged human prompt for the same event_id (or vice versa)
is an INCOMPLETE pair and is silently skipped, never fabricated.

Pure/read-only. No model call. No DB write. No Kardia/hippocampus
interaction of any kind.
"""
import sqlite3

from provenance_schema import derive_stable_id
from native_turn_staging import read_verified_staging_entries
from family_membership import can_receive_event

CLARK_ACTOR_ID = derive_stable_id("actor", "clark")
DEFAULT_MAX_TURNS = 8

# OWC8-S1: a bounded turn COUNT is not a bounded CONTEXT -- the live
# OWC/WSP2-P3 forensic proved two consecutive Pass-2 failures were
# caused by an accumulated dialogue window (7 pairs, 9305 chars) that,
# together with Pass-2's own task text, left only 5 tokens of
# generation budget before the 4096-token ceiling. DEFAULT_MAX_TURNS
# alone cannot bound that -- a handful of long pairs can still exhaust
# it. This aggregate cap is the second, independent bound
# select_bounded_dialogue_pairs() below enforces alongside it.
DEFAULT_MAX_DIALOGUE_CHARS = 5000


def _pair_permitted_for_viewer(*, h_scope, x_scope, human_actor_id,
                               viewer_visibility_scope, viewer_principal_actor_id,
                               active_family_principal_ids):
    """PURE. FS1 delivery predicate for one canonical H/X dialogue pair
    (H's prompt + X's reply share one scope by writer cross-validation).
    A pair whose H and X scopes disagree is anomalous and NEVER
    delivered. Otherwise the governing scope is H's (the human's own
    attribution), and can_receive_event() decides: unbound viewers retain
    only legacy NULL-scope pairs; scoped viewers get only what their own
    principal/scope permit."""
    # A scoped pair is one provenance unit: one-sided absence is just as
    # inconsistent as two different non-NULL values. Only the legacy
    # (NULL, NULL) pair is a truthful unscoped pair.
    if h_scope != x_scope:
        return False
    governing_scope = h_scope
    return can_receive_event(
        viewer_principal_actor_id, viewer_visibility_scope,
        human_actor_id, governing_scope,
        active_family_principal_ids=active_family_principal_ids,
    )


def build_authenticated_dialogue_window(conn, human_input_event_id,
                                       max_turns=DEFAULT_MAX_TURNS,
                                       max_chars=DEFAULT_MAX_DIALOGUE_CHARS,
                                       viewer_visibility_scope=None,
                                       viewer_principal_actor_id=None,
                                       active_family_principal_ids=frozenset()):
    """Recent canonical H/X for the same authenticated human and pipeline.

    Process sessions record execution provenance, not conversational identity.
    Pair only by explicit X -> H links. Bound the read before the current H's
    component frontier, so it cannot include itself or a subsequently written
    answer. No staging/GUI hydration or semantic retrieval is involved.

    FS1: when a viewer scope/principal is supplied (a scoped session), each
    candidate pair is filtered by _pair_permitted_for_viewer() -- another
    principal's private H/X pairs never leak into this window. With no
    viewer scope (the unbound default), the query retains the historical
    same-human restriction and the predicate excludes explicit FS1 scope.
    """
    current = conn.execute(
        "SELECT hc.component_id, hc.creator_actor_id, h.pipeline_id "
        "FROM events h JOIN event_components hc ON hc.event_id=h.event_id "
        "JOIN auth_contexts a ON a.auth_context_id=h.auth_context_id "
        "WHERE h.event_id=? AND h.event_type='human_waking_input' "
        "AND hc.component_kind='human_conversational_input' AND hc.sequence=0 "
        "AND a.auth_state='authenticated' AND a.authenticated_actor_id=hc.creator_actor_id",
        (human_input_event_id,),
    ).fetchone()
    if current is None or max_turns < 1:
        raise ValueError('canonical dialogue requires authenticated H and a positive turn bound')
    frontier, human_actor_id, pipeline_id = current
    event_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    scope_h = "h.visibility_scope" if "visibility_scope" in event_columns else "NULL"
    scope_x = "x.visibility_scope" if "visibility_scope" in event_columns else "NULL"
    # Legacy/unbound authenticated dialogue retains its historical
    # same-human query. A scoped family view may consider every prior
    # human author in the pipeline; the canonical per-pair predicate
    # below then admits only own-private/legacy and FAMILY_SHARED rows.
    author_clause = "AND hc.creator_actor_id=? " if viewer_visibility_scope is None else ""
    params = [CLARK_ACTOR_ID]
    if viewer_visibility_scope is None:
        params.append(human_actor_id)
    params.extend([pipeline_id, pipeline_id, frontier, frontier])
    # LAWFUL NULL: an X whose Clark-authored component is his typed no_reply choice (no prose) is a
    # complete exchange too -- shown exactly as it happened: the human's words, then an empty reply.
    rows = conn.execute(
        "SELECT x.event_id, x.occurred_at, hc.component_text, "
        "CASE WHEN xc.component_kind='clark_reply_choice' THEN '' ELSE xc.component_text END, "
        f"hc.creator_actor_id, {scope_h}, {scope_x} "
        "FROM events x JOIN event_components xc ON xc.event_id=x.event_id "
        "JOIN event_components link ON link.event_id=x.event_id AND link.component_kind='human_input_event_id' "
        "JOIN events h ON h.event_id=link.component_text AND h.event_type='human_waking_input' "
        "JOIN event_components hc ON hc.event_id=h.event_id AND hc.component_kind='human_conversational_input' AND hc.sequence=0 "
        "JOIN auth_contexts a ON a.auth_context_id=h.auth_context_id "
        "WHERE x.event_type='waking_turn' "
        "AND (xc.component_kind='conversational_prose' "
        "     OR (xc.component_kind='clark_reply_choice' AND xc.component_text='no_reply')) "
        "AND xc.creator_actor_id=? "
        f"{author_clause}"
        "AND a.auth_state='authenticated' AND a.authenticated_actor_id=hc.creator_actor_id "
        "AND x.pipeline_id=? AND h.pipeline_id=? AND xc.component_id<? AND hc.component_id<? "
        "ORDER BY xc.component_id DESC",
        tuple(params),
    ).fetchall()
    rows = [r for r in rows if _pair_permitted_for_viewer(
        h_scope=r[5], x_scope=r[6], human_actor_id=r[4],
        viewer_visibility_scope=viewer_visibility_scope,
        viewer_principal_actor_id=viewer_principal_actor_id,
        active_family_principal_ids=active_family_principal_ids,
    )][:max_turns]
    if len({r[0] for r in rows}) != len(rows):
        raise ValueError('ambiguous canonical dialogue linkage')
    pairs = [dict(
        event_id=r[0], occurred_at=r[1], prompt=r[2], clark_text=r[3],
        principal_actor_id=r[4], visibility_scope=r[5],
    ) for r in reversed(rows)]
    bounded = select_bounded_dialogue_pairs(pairs, max_turns=max_turns, max_chars=max_chars)
    return [message for p in bounded for message in (
        {
            'role':'user',
            'content': (
                f"[Author principal: {p['principal_actor_id']}]\n{p['prompt']}"
                if p.get('visibility_scope') == 'family_shared' else p['prompt']
            ),
        },
        {'role':'assistant', 'content':p['clark_text']},
    )]


def dialogue_contribution(messages):
    """Whole-pair OWC9 units; the newest pair may never be evicted."""
    import context_budget
    if len(messages) % 2 or any(m.get('role') != ('user' if i % 2 == 0 else 'assistant')
                                for i, m in enumerate(messages)):
        raise ValueError('recent dialogue must contain complete user/assistant pairs')
    pairs = [[dict(m) for m in messages[i:i+2]] for i in range(0, len(messages), 2)]
    render = lambda units: ''.join(m['content'] for pair in units for m in pair)
    return context_budget.Contribution(
        context_budget.RECENT_DIALOGUE, render(pairs), hard=False,
        droppable_units=pairs, render_fn=render, minimum_units=1 if pairs else 0,
    )


def dialogue_messages(contribution):
    return [dict(m) for pair in contribution.droppable_units for m in pair]


def collect_session_dialogue_pairs(conn, session_id, staging_path):
    """Pure/read-only. Returns (pairs, skipped_incomplete_count).

    pairs: chronologically ordered list of {"event_id", "occurred_at",
    "prompt", "clark_text"} dicts, one per COMPLETE dialogue turn in
    `session_id` -- both the canonical Clark-authored text and the
    staged human prompt mechanically recoverable for the same event_id.

    skipped_incomplete_count: number of canonical Clark turns in this
    session for which no matching staged human prompt was found (the
    reverse case -- a staged prompt with no canonical Clark turn --
    is not itself a turn to report, since a turn only ever enters this
    reckoning from the canonical side to begin with)."""
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    event_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    h_scope_expr = "h.visibility_scope" if "visibility_scope" in event_columns else "NULL"

    clark_rows = conn.execute(
        "SELECT e.event_id AS event_id, e.occurred_at AS occurred_at, "
        "CASE WHEN ec.component_kind = 'clark_reply_choice' THEN '' ELSE ec.component_text END AS clark_text "
        "FROM events e "
        "JOIN auth_contexts ac ON e.auth_context_id = ac.auth_context_id "
        "JOIN event_components ec ON ec.event_id = e.event_id "
        "WHERE e.event_type = 'waking_turn' "
        "AND ac.session_id = ? "
        "AND (ec.component_kind = 'conversational_prose' "
        "     OR (ec.component_kind = 'clark_reply_choice' AND ec.component_text = 'no_reply')) "
        "AND ec.creator_actor_id = ? "
        "ORDER BY e.occurred_at ASC, e.event_id ASC",
        (session_id, CLARK_ACTOR_ID),
    ).fetchall()

    prompts_by_event_id = {}
    for _line_index, payload, _staging_id in read_verified_staging_entries(staging_path):
        if payload.get("session_id") == session_id:
            prompts_by_event_id[payload.get("event_id")] = payload.get("prompt")

    pairs = []
    skipped_incomplete_count = 0
    for row in clark_rows:
        prompt = prompts_by_event_id.get(row["event_id"])
        if prompt is None:
            skipped_incomplete_count += 1
            continue
        human_actor_rows = conn.execute(
            f"SELECT hc.creator_actor_id, {h_scope_expr} "
            "FROM event_components link "
            "JOIN events h ON h.event_id=link.component_text "
            "JOIN event_components hc ON hc.event_id=h.event_id "
            "JOIN auth_contexts a ON a.auth_context_id=h.auth_context_id "
            "WHERE link.event_id=? AND link.component_kind='human_input_event_id' "
            "AND h.event_type='human_waking_input' "
            "AND hc.component_kind='human_conversational_input' AND hc.sequence=0 "
            "AND a.auth_state='authenticated' "
            "AND a.authenticated_actor_id=hc.creator_actor_id",
            (row["event_id"],),
        ).fetchall()
        # This legacy collection API feeds the public workspace handoff,
        # whose V0 events have no principal visibility_scope. Never copy
        # a scoped family H/X pair into that unscoped channel.
        if len(human_actor_rows) == 1 and human_actor_rows[0][1] is not None:
            skipped_incomplete_count += 1
            continue
        pairs.append({
            "event_id": row["event_id"],
            "occurred_at": row["occurred_at"],
            "prompt": prompt,
            "clark_text": row["clark_text"],
            # Pre-SLP1 legacy pairs legitimately have no canonical H.
            # A modern pair carries exactly one mechanically-derived
            # principal; ambiguity is represented as unknown, never
            # guessed from a display label or current owner.
            "human_actor_id": (
                human_actor_rows[0][0] if len(human_actor_rows) == 1 else None
            ),
            "visibility_scope": (
                human_actor_rows[0][1] if len(human_actor_rows) == 1 else None
            ),
        })

    return pairs, skipped_incomplete_count


def select_bounded_dialogue_pairs(pairs, max_turns=DEFAULT_MAX_TURNS, max_chars=DEFAULT_MAX_DIALOGUE_CHARS):
    """Pure. `pairs` is chronologically ordered (oldest first), exactly
    as returned by collect_session_dialogue_pairs(). Returns the
    bounded subset actually delivered to the model, still
    chronologically ordered, selected by BOTH bounds (OWC8-S1):

      - at most `max_turns` pairs (the pre-existing OWC2-S1 bound,
        preserved unchanged);
      - newest complete pairs first, adding the next-older complete
        pair only while doing so keeps the aggregate character cost
        (len(prompt) + len(clark_text), summed over selected pairs) at
        or under `max_chars`.

    Deterministic; no semantic ranking, no summarization, no text
    similarity. Recency wins mechanically: once the next-older pair
    would push the aggregate over `max_chars`, selection stops --
    older-but-smaller pairs further back are never substituted in
    instead (spec OWC8-S1 section 3).

    Explicit narrow exception: if the single newest candidate pair
    alone already exceeds `max_chars`, it is still included whole (a
    pair's own text is never sliced to fit), and no further pairs are
    added -- this is the one case where the returned total legitimately
    exceeds `max_chars`."""
    candidates = pairs[-max_turns:] if max_turns else list(pairs)
    selected = []
    total_chars = 0
    for pair in reversed(candidates):  # newest first
        pair_chars = len(pair["prompt"]) + len(pair["clark_text"])
        if selected and total_chars + pair_chars > max_chars:
            break
        selected.append(pair)
        total_chars += pair_chars
    selected.reverse()  # restore chronological order
    return selected


def build_session_dialogue_window(conn, session_id, staging_path, max_turns=DEFAULT_MAX_TURNS, max_chars=DEFAULT_MAX_DIALOGUE_CHARS):
    """Pure/read-only. Returns alternating role-tagged messages
    (`[{"role": "user", "content": ...}, {"role": "assistant",
    "content": ...}, ...]`) for the bounded recent COMPLETE dialogue
    pairs in `session_id` selected by select_bounded_dialogue_pairs()
    above (at most `max_turns` pairs AND at most `max_chars` aggregate
    characters), chronologically ordered. No system message. No
    current prompt -- the caller's own current-turn user message is
    appended separately, exactly once, by the caller, and is never
    counted against this historical budget.
    """
    pairs, _skipped = collect_session_dialogue_pairs(conn, session_id, staging_path)
    bounded_pairs = select_bounded_dialogue_pairs(pairs, max_turns=max_turns, max_chars=max_chars)

    messages = []
    for pair in bounded_pairs:
        messages.append({"role": "user", "content": pair["prompt"]})
        messages.append({"role": "assistant", "content": pair["clark_text"]})
    return messages


def insert_dialogue_window(prepared_messages, dialogue_window):
    """Pure. Inserts `dialogue_window` (role-tagged user/assistant
    pairs) immediately after any leading system message(s) in
    `prepared_messages` and before everything else (the current-turn
    user message, unchanged, exactly once). Does not mutate its
    arguments. Never pushes conversation history into the system
    message as prose -- messages stay natively role-tagged."""
    if prepared_messages and prepared_messages[0].get("role") == "system":
        return [prepared_messages[0]] + list(dialogue_window) + list(prepared_messages[1:])
    return list(dialogue_window) + list(prepared_messages)


def build_correspondent_dialogue_window(conn, source_ref, max_turns=DEFAULT_MAX_TURNS,
                                        max_chars=DEFAULT_MAX_DIALOGUE_CHARS):
    """Recent exchanges with ONE non-principal correspondent, for an occasion from that same source,
    only while Clark has standing with them and only from the exchange in which he established it
    (correspondence_surfaces.correspondent_event_receivable).  Each pair is exactly what happened:
    the correspondent's received words, then Clark's reply ('' when he chose no_reply).  Paired only
    by the canonical carriage + correspondent_source_ref components -- never by name or content."""
    import correspondence_surfaces as cs
    rows = conn.execute(
        "SELECT x.event_id, x.occurred_at, car.component_text FROM events x "
        "JOIN event_components ref ON ref.event_id = x.event_id AND ref.component_kind = ? AND ref.component_text = ? "
        "JOIN event_components car ON car.event_id = x.event_id AND car.component_kind = 'discord_inbound_carriage' "
        "WHERE x.event_type = 'waking_turn' ORDER BY x.rowid DESC",
        (cs.CORRESPONDENT_SOURCE_REF_COMPONENT_KIND, source_ref)).fetchall()
    pairs = []
    for x_event_id, occurred_at, inbound_event_id in rows:
        if not cs.correspondent_event_receivable(conn, x_event_id, source_ref):
            continue
        words = conn.execute(
            "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = 'discord_inbound_content'",
            (inbound_event_id,)).fetchone()
        reply = conn.execute(
            "SELECT component_kind, component_text FROM event_components WHERE event_id = ? AND creator_actor_id = ? "
            "AND (component_kind = 'conversational_prose' OR (component_kind = 'clark_reply_choice' "
            "AND component_text = 'no_reply'))", (x_event_id, CLARK_ACTOR_ID)).fetchone()
        if words is None or reply is None:
            continue
        pairs.append({"event_id": x_event_id, "occurred_at": occurred_at, "prompt": words[0],
                      "clark_text": "" if reply[0] == "clark_reply_choice" else reply[1]})
        if len(pairs) >= max_turns:
            break
    bounded = select_bounded_dialogue_pairs(list(reversed(pairs)), max_turns=max_turns, max_chars=max_chars)
    return [m for p in bounded for m in ({"role": "user", "content": p["prompt"]},
                                        {"role": "assistant", "content": p["clark_text"]})]
