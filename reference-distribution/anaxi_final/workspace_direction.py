"""WSP1-S2: typed Clark workspace-action pathway.

Distinct from conversation_direction.py's typed conversational-
direction acts (spec section 1) -- a workspace action targets
workspace_capability.py's resource classes/actions (list/inspect_
metadata/read/append against library/music/photographs/journal),
never a conversation thread/topic. Never merged into OWC5's act enum.

CLARK CHOOSES THE REQUESTED WORKSPACE ACTION. HOST ESTABLISHES:
whether the action exists, whether it is permitted, what resource it
targets, what boundary applies, what mechanical result occurred.
Model-visible allowed actions are advisory context only -- host
capability state (workspace_capability.CAPABILITY_DESCRIPTORS, via
workspace_capability.check_permission()) is authoritative; every
executor call re-checks permission internally regardless of what
Pass 1 selected.

Pure pathway/dispatch module except for execute_workspace_action(),
which calls into workspace_capability.py's own functions (themselves
the only code that touches the filesystem) -- never opens an arbitrary
model-supplied path directly.
"""
import json

import obsidian_workspace as ow
import workspace_capability as wc
import workspace_organization as wo
import workspace_provenance as wprov

RAW_ACTION_ALLOWED_FIELDS = {"resource_class", "action", "relative_path", "content"}
PASS2_ALLOWED_FIELDS = {"expression"}

# WSP2-MA2: the Stage-2 ordinary-action STRUCTURAL envelope, as a plain
# JSON Schema dict -- passed as ask_llama_for_json()'s own
# `structured_schema` argument so Ollama constrains generation to this
# exact object shape (machine-confirmed supported by the installed
# ollama==0.6.2 client; see llama_anaxi.py's own ask_llama_for_json()
# docstring). Deliberately encodes ONLY mechanical structure --
# RAW_ACTION_ALLOWED_FIELDS's own four keys, all required, all typed
# string, nothing else -- never resource_class/action legality, never
# path containment, never journal-only content semantics. Those remain
# validate_pass1_workspace_action()'s own responsibility, unchanged and
# still authoritative after generation; this schema narrows what shape
# of JSON the model can even emit, it does not decide whether that
# shape is semantically legal. Kept in exact sync with
# RAW_ACTION_ALLOWED_FIELDS by construction (built FROM it, never a
# second, independently-typed literal) so the two can never drift.
STAGE2_ACTION_SCHEMA = {
    "type": "object",
    "properties": {field: {"type": "string"} for field in sorted(RAW_ACTION_ALLOWED_FIELDS)},
    "required": sorted(RAW_ACTION_ALLOWED_FIELDS),
    "additionalProperties": False,
}

# ================================================== CAP2A-A: organization overlay
#
# resource_class="organization" reuses the IDENTICAL four-field raw
# envelope (RAW_ACTION_ALLOWED_FIELDS/STAGE2_ACTION_SCHEMA, both
# byte-unchanged by this addition) every other resource class already
# uses -- no new schema field, no free-prose parsing of a combined
# payload. Organization operations that need more than one piece of
# information (a container_type on create, a target resource_class on
# add/remove_reference) get it from a FIXED, HOST-OWNED action-name ->
# parameter lookup table, never by parsing Clark's own free text --
# Clark still only ever picks one action name from an enumerated,
# host-defined list, exactly like choosing "read" vs "append" already
# works for every other resource class.
#
# Convention: `relative_path` always means "container_id" for an
# organization action (mirrors journal's own relative_path-as-entry-id
# convention exactly). `content` is the one flexible free-text payload
# slot -- a new container's name, a rename's new name, or an add/
# remove_reference's own target relative_path within the class the
# action name already names -- exactly the same role content already
# plays for a journal append.
ORGANIZATION = "organization"

ORG_LIST = "list"
ORG_INSPECT = "inspect"
ORG_RENAME = "rename"
ORG_DELETE_CONTAINER = "delete_container"
ORG_CREATE_COLLECTION = "create_collection"
ORG_CREATE_SHELF = "create_shelf"
ORG_CREATE_PLAYLIST = "create_playlist"
ORG_CREATE_ALBUM = "create_album"
ORG_ADD_REFERENCE_LIBRARY = "add_reference_library"
ORG_ADD_REFERENCE_MUSIC = "add_reference_music"
ORG_ADD_REFERENCE_PHOTOGRAPHS = "add_reference_photographs"
ORG_ADD_REFERENCE_JOURNAL = "add_reference_journal"
ORG_REMOVE_REFERENCE_LIBRARY = "remove_reference_library"
ORG_REMOVE_REFERENCE_MUSIC = "remove_reference_music"
ORG_REMOVE_REFERENCE_PHOTOGRAPHS = "remove_reference_photographs"
ORG_REMOVE_REFERENCE_JOURNAL = "remove_reference_journal"

ORG_CREATE_ACTIONS_TO_CONTAINER_TYPE = {
    ORG_CREATE_COLLECTION: "collection",
    ORG_CREATE_SHELF: "shelf",
    ORG_CREATE_PLAYLIST: "playlist",
    ORG_CREATE_ALBUM: "album",
}
ORG_ADD_REFERENCE_ACTIONS_TO_RESOURCE_CLASS = {
    ORG_ADD_REFERENCE_LIBRARY: wc.LIBRARY,
    ORG_ADD_REFERENCE_MUSIC: wc.MUSIC,
    ORG_ADD_REFERENCE_PHOTOGRAPHS: wc.PHOTOGRAPHS,
    ORG_ADD_REFERENCE_JOURNAL: wc.JOURNAL,
}
ORG_REMOVE_REFERENCE_ACTIONS_TO_RESOURCE_CLASS = {
    ORG_REMOVE_REFERENCE_LIBRARY: wc.LIBRARY,
    ORG_REMOVE_REFERENCE_MUSIC: wc.MUSIC,
    ORG_REMOVE_REFERENCE_PHOTOGRAPHS: wc.PHOTOGRAPHS,
    ORG_REMOVE_REFERENCE_JOURNAL: wc.JOURNAL,
}
ORG_ALL_ACTIONS = frozenset({
    ORG_LIST, ORG_INSPECT, ORG_RENAME, ORG_DELETE_CONTAINER,
    *ORG_CREATE_ACTIONS_TO_CONTAINER_TYPE,
    *ORG_ADD_REFERENCE_ACTIONS_TO_RESOURCE_CLASS,
    *ORG_REMOVE_REFERENCE_ACTIONS_TO_RESOURCE_CLASS,
})

# Organization has no workspace_capability.CAPABILITY_DESCRIPTORS entry
# (it is deliberately NOT a fifth wc.RESOURCE_CLASSES member -- it is
# metadata about resources, not a resource root of its own; see
# workspace_organization.py's own module docstring). One fixed boundary
# id communicates that plainly and consistently in every trace/log this
# produces, mirroring the shape (not the source) of wc.check_permission()'s
# own (allowed, boundary_id, rationale) triple.
ORGANIZATION_BOUNDARY_ID = "workspace.organization.overlay"
ORGANIZATION_RATIONALE = (
    "Organizational overlay: reference-only bookkeeping over existing resources. "
    "Never mutates, moves, or deletes a referenced library/music/photograph/journal original."
)


# First live-exposure allowed action surface (spec section 10) --
# advisory context for Pass 1 only; workspace_capability's own
# CAPABILITY_DESCRIPTORS remains the authoritative permission source
# checked again inside every executor call.
LIVE_ALLOWED_SURFACE = {
    wc.LIBRARY: {wc.LIST, wc.INSPECT_METADATA, wc.READ, wc.VIEW_PAGE},
    wc.JOURNAL: {wc.LIST, wc.READ, wc.APPEND},
    # The shared Obsidian vault: list / read anyone's notes; append = create one new note of Clark's own.
    wc.NOTES: {wc.LIST, wc.READ, wc.APPEND},
    # CAP2F: LISTEN/INSPECT_AUDIO added -- genuine bounded acoustic
    # decode/measurement now exists (wc.access_music_listen()/
    # access_music_inspect_audio()). This is NOT the model-level audio
    # PERCEPTION pathway (still AUDIO BLOCKED BY CURRENT RUNTIME -- see
    # CAP2/CAP2C) -- no model call is ever made with these actions;
    # they produce bounded mechanical text/numeric context only.
    # V1: OBSERVE added -- whole-source bounded acoustic observation
    # (profile + temporal map), same measurement-not-perception boundary.
    wc.MUSIC: {wc.LIST, wc.INSPECT_METADATA, wc.LISTEN, wc.INSPECT_AUDIO, wc.OBSERVE},
    wc.PHOTOGRAPHS: {wc.LIST, wc.INSPECT_METADATA, wc.VIEW},
    # CAP2A-A: the organization overlay's own fixed action vocabulary --
    # reuses this exact same envelope/dispatcher/validator, never a
    # second control architecture. Reused verbatim by workspace_
    # roaming.py too, exactly like every other resource class here
    # (see that module's own test_music_and_photos_remain_list_and_
    # metadata_only for the established "one shared table" convention).
    ORGANIZATION: ORG_ALL_ACTIONS,
}

# The ordinary waking bridge exposes the four concrete owner resources
# named by its model-visible affordance. Organization remains available
# to explicit WSP1/roaming callers, but is deliberately absent here.
RESOURCE_ALLOWED_SURFACE = {
    resource_class: actions
    for resource_class, actions in LIVE_ALLOWED_SURFACE.items()
    if resource_class != ORGANIZATION
}


def resource_allowed_surface(paths):
    """RESOURCE_ALLOWED_SURFACE as it truly stands for these paths: the shared vault is offered only
    when it is configured and exists -- an unavailable collection is absent, never shown as empty."""
    return {k: v for k, v in RESOURCE_ALLOWED_SURFACE.items() if k != wc.NOTES or paths.notes_available()}

_PASS1_COMMON_TASK_INSTRUCTION = (
    "Take one supervised workspace action. `content` is journal text, a prior "
    "`next_request`, or JSON selectors (`query`/`page`); lists accept directory "
    "paths. journal=your own entries, library=reference docs. To continue an "
    "earlier journal entry, append with relative_path naming it (its id, or words "
    "from it). Unsure which item to read? Leave relative_path empty; the host shows "
    "what exists.\n\n"
    # WSP2-MA1: resource_class, action, relative_path, and content are
    # ALL required keys in every response, regardless of which action
    # is chosen -- the validator rejects a response missing any of
    # them. The prose above, by itself, only explains WHEN content is
    # meaningful (a journal append); it never said the other three
    # actions still need the key present (empty string). Mirrors
    # workspace_private.PRIVATE_TASK_INSTRUCTION's own, already-
    # unambiguous equivalent clause for the sibling private_act
    # contract -- both control contracts now state this identically.
    "resource_class, action, relative_path, and content are all required fields; "
    "use an empty string when the action does not need it."
)

_PASS1_ORGANIZATION_TASK_CLAUSE = (
    "For an 'organization' action, relative_path means the container_id "
    "(leave empty when creating or listing containers), and content "
    "means the container's name, a new name, or the target item's own "
    "relative_path within the resource class already named by your "
    "chosen action (e.g. add_reference_library)."
)

PASS1_RESOURCE_TASK_INSTRUCTION = (
    _PASS1_COMMON_TASK_INSTRUCTION + "\n\n"
    + "Do not invent capabilities that are not listed."
)

# Present only when the shared vault is actually offered (the WSP1 hard floor is tight: an absent
# collection costs nothing, and the text above stays byte-identical without it).
_PASS1_NOTES_TASK_CLAUSE = "notes=shared Obsidian notes, anyone's; append makes a new note of yours."

PASS1_TASK_INSTRUCTION = (
    _PASS1_COMMON_TASK_INSTRUCTION + "\n\n"
    + _PASS1_ORGANIZATION_TASK_CLAUSE + "\n\n"
    + "Do not invent capabilities that are not listed."
)

PASS2_TASK_INSTRUCTION = "Respond naturally to what happened when you took this workspace action."


class WorkspaceDirectionFailure(Exception):
    """The narrowest existing error surface -- raised by the live
    supervisor and left uncaught (spec section 13/29: no live hotfix,
    no automatic retry/fallback)."""

    def __init__(self, stage, failure_code, executed_action=None):
        self.stage = stage  # "pass1" or "pass2"
        self.failure_code = failure_code
        # (resource_class, action) of the workspace action that had ALREADY run when this failure
        # occurred (Pass-2 failures only); None when no action ran. Set only by the supervisor.
        self.executed_action = executed_action
        super().__init__(f"workspace direction failed at {stage}: {failure_code}")


# Workspace actions that only READ. A failure after one of these ran leaves nothing durable to
# duplicate or undo (no write, no outward effect; the encounter record is written only after a
# canonical X commits), so replaying the same H is safe. Journal append and organization actions
# are deliberately absent: they change state.
READ_ONLY_WORKSPACE_ACTIONS = frozenset({
    (wc.LIBRARY, wc.LIST), (wc.LIBRARY, wc.INSPECT_METADATA), (wc.LIBRARY, wc.READ), (wc.LIBRARY, wc.VIEW_PAGE),
    (wc.MUSIC, wc.LIST), (wc.MUSIC, wc.INSPECT_METADATA), (wc.MUSIC, wc.LISTEN), (wc.MUSIC, wc.INSPECT_AUDIO),
    (wc.MUSIC, wc.OBSERVE),
    (wc.PHOTOGRAPHS, wc.LIST), (wc.PHOTOGRAPHS, wc.INSPECT_METADATA), (wc.PHOTOGRAPHS, wc.VIEW),
    (wc.JOURNAL, wc.LIST), (wc.JOURNAL, wc.READ),
    (wc.NOTES, wc.LIST), (wc.NOTES, wc.READ),
})


class DirectionFailure:
    MALFORMED_ACTION = "MALFORMED_ACTION"
    PROTOCOL_LEAKAGE = "PROTOCOL_LEAKAGE"
    UNKNOWN_RESOURCE_CLASS = "UNKNOWN_RESOURCE_CLASS"
    NOT_IN_ALLOWED_SURFACE = "NOT_IN_ALLOWED_SURFACE"
    INVALID_RELATIVE_PATH = "INVALID_RELATIVE_PATH"
    INVALID_CONTENT = "INVALID_CONTENT"
    MALFORMED_EXPRESSION = "MALFORMED_EXPRESSION"
    EMPTY_EXPRESSION = "EMPTY_EXPRESSION"


ACTION_PAYLOAD_MALFORMED = "ACTION_PAYLOAD_MALFORMED"
MAX_LIBRARY_READ_CHARS = 4000


def _parse_json_content(content, allowed_keys):
    if content == "":
        return {}, None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None, ACTION_PAYLOAD_MALFORMED
    if not isinstance(payload, dict) or set(payload) - set(allowed_keys):
        return None, ACTION_PAYLOAD_MALFORMED
    return payload, None


def parse_list_content(content):
    payload, failure = _parse_json_content(content, {"cursor", "query"})
    if failure is not None:
        return None, failure
    cursor = payload.get("cursor")
    query = payload.get("query")
    if cursor is not None and not isinstance(cursor, str):
        return None, ACTION_PAYLOAD_MALFORMED
    if query is not None and not isinstance(query, str):
        return None, ACTION_PAYLOAD_MALFORMED
    return {"cursor": cursor, "query": query}, None


def parse_library_read_content(content):
    # A bare character offset ("3114") is unambiguous and is what the real model writes when told where
    # to continue; it means {"offset": 3114}.  (The typed "next" is resolved by the supervisor.)
    if isinstance(content, str) and content.strip().strip('"').isdigit():
        content = json.dumps({"offset": int(content.strip().strip('"'))})
    payload, failure = _parse_json_content(content, {"offset", "max_chars"})
    if failure is not None:
        return None, failure
    offset = payload.get("offset", 0)
    max_chars = payload.get("max_chars", MAX_LIBRARY_READ_CHARS)
    if (
        not isinstance(offset, int) or isinstance(offset, bool) or offset < 0
        or not isinstance(max_chars, int) or isinstance(max_chars, bool)
        or max_chars <= 0 or max_chars > MAX_LIBRARY_READ_CHARS
    ):
        return None, ACTION_PAYLOAD_MALFORMED
    return {"offset": offset, "max_chars": max_chars}, None


def parse_pdf_page_content(content):
    # A bare page number ("3") is unambiguous and is what the real model actually writes when
    # asked for a page; it means {"page": 3}. Anything else non-object stays malformed.
    try:
        bare = json.loads(content) if isinstance(content, str) and content.strip() else None
    except ValueError:
        bare = None
    if isinstance(bare, int) and not isinstance(bare, bool):
        return ({"page": bare}, None) if bare >= 1 else (None, ACTION_PAYLOAD_MALFORMED)
    payload, failure = _parse_json_content(content, {"page"})
    if failure is not None or "page" not in payload:
        return None, ACTION_PAYLOAD_MALFORMED
    page = payload.get("page")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        return None, ACTION_PAYLOAD_MALFORMED
    return {"page": page}, None


# ------------------------------------------------------------------- Pass 1 --


def build_pass1_interface(system_content, dialogue_window, current_user_message, allowed_surface=None):
    allowed_surface = allowed_surface if allowed_surface is not None else LIVE_ALLOWED_SURFACE
    task_instruction = (
        PASS1_TASK_INSTRUCTION
        if ORGANIZATION in allowed_surface
        else PASS1_RESOURCE_TASK_INSTRUCTION
    )
    if wc.NOTES in allowed_surface:
        task_instruction = task_instruction.replace(
            "library=reference docs.", "library=reference docs, " + _PASS1_NOTES_TASK_CLAUSE, 1)
    return {
        "system_content": system_content,
        "dialogue_window": list(dialogue_window),
        "current_user_message": current_user_message,
        "allowed_surface": {k: sorted(v) for k, v in allowed_surface.items()},
        "raw_action_schema": {"resource_class": "string", "action": "string", "relative_path": "string", "content": "string"},
        "task_instruction": task_instruction,
    }


CONTENT_OPTIONAL_ACTIONS = frozenset({wc.LIST, wc.READ, wc.INSPECT_METADATA, wc.VIEW, wc.LISTEN, wc.OBSERVE})


def validate_pass1_workspace_action(raw_model_action, allowed_surface=None):
    """Structural validation only -- mirrors HDI2/API2/OWC5's own
    pattern exactly. Returns (validated_action, None) or
    (None, DirectionFailure)."""
    allowed_surface = allowed_surface if allowed_surface is not None else LIVE_ALLOWED_SURFACE

    if isinstance(raw_model_action, str):
        try:
            raw = json.loads(raw_model_action)
        except (TypeError, ValueError):
            return None, DirectionFailure.MALFORMED_ACTION
    elif isinstance(raw_model_action, dict):
        raw = raw_model_action
    else:
        return None, DirectionFailure.MALFORMED_ACTION

    if not isinstance(raw, dict):
        return None, DirectionFailure.MALFORMED_ACTION
    if "content" not in raw and raw.get("action") in CONTENT_OPTIONAL_ACTIONS:
        # Real model (2026-09-21): once relative_path names the target it omits ``content`` on a
        # read/list, which carries nothing. Absence is read as empty ONLY for the actions whose
        # empty payload is itself complete and valid (see CONTENT_OPTIONAL_ACTIONS); every action
        # whose content is meaningful (append text, a page, an audio view, organization names)
        # stays strictly required, and the other three fields always are.
        raw = dict(raw, content="")
    if not RAW_ACTION_ALLOWED_FIELDS.issubset(raw.keys()):
        return None, DirectionFailure.MALFORMED_ACTION
    if set(raw.keys()) - RAW_ACTION_ALLOWED_FIELDS:
        return None, DirectionFailure.PROTOCOL_LEAKAGE

    resource_class = raw.get("resource_class")
    if resource_class not in allowed_surface:
        return None, DirectionFailure.UNKNOWN_RESOURCE_CLASS

    action = raw.get("action")
    if not isinstance(action, str) or action not in allowed_surface[resource_class]:
        return None, DirectionFailure.NOT_IN_ALLOWED_SURFACE

    relative_path = raw.get("relative_path")
    if not isinstance(relative_path, str):
        return None, DirectionFailure.INVALID_RELATIVE_PATH

    content = raw.get("content")
    if not isinstance(content, str):
        return None, DirectionFailure.INVALID_CONTENT

    return {"resource_class": resource_class, "action": action, "relative_path": relative_path, "content": content}, None


# --------------------------------------------------------------- execution --


# CAP2-B: execute_workspace_action() is the ONE shared dispatcher used
# by BOTH the WSP1 supervised single-action pathway (workspace_
# supervisor.py) and the WSP2 unattended roaming pathway (workspace_
# roaming.py -- see that module's own test_music_and_photos_remain_
# list_and_metadata_only, which asserts roaming reuses this exact
# LIVE_ALLOWED_SURFACE rather than defining a broader one of its own).
# Roaming has no narration/Pass-2 step that could ever honestly use
# raw pixels, and unconditionally JSON-serializes boundary_result
# (workspace_roaming.py's own record_public_action(external_result_
# text=json.dumps(boundary_result, ...))) -- bytes in that dict would
# be a hard TypeError there, not a soft cost. So boundary_result's own
# "result" is ALWAYS JSON-safe/metadata-only for every caller; a real
# VIEW's raw image bytes are instead stashed in this one-shot,
# same-process side channel (mirrors the existing _last_trace_state
# convention below exactly) for the ONE caller (workspace_supervisor.py)
# that actually knows to retrieve and immediately clear it before
# attaching it to a real model request. Roaming never calls the getter,
# so it never touches raw bytes at all -- safe by omission, not by a
# second permission check.
_last_view_payload_state = {"image_bytes": None}


def _stash_last_view_image_bytes(image_bytes):
    _last_view_payload_state["image_bytes"] = image_bytes


def get_and_clear_last_view_image_bytes():
    """Consumes (returns, then clears) whatever VIEW dispatch most
    recently stashed -- None if the last dispatched action was not a
    successful photograph VIEW, or if this has already been consumed
    once. Call exactly once, immediately after execute_workspace_action()
    returns, never speculatively."""
    image_bytes = _last_view_payload_state["image_bytes"]
    _last_view_payload_state["image_bytes"] = None
    return image_bytes


def _consequence_text(action, performed, resource_class=None):
    if action == wc.APPEND and resource_class == wc.NOTES:
        return ("New note created in the shared vault; no existing note was changed." if performed
                else "Action blocked; nothing written and no note changed.")
    if action == wc.APPEND:
        return "Entry written; nothing else changed." if performed else "Action blocked; nothing written."
    if action in (wc.LIST, wc.INSPECT_METADATA, wc.READ, wc.VIEW, wc.VIEW_PAGE, wc.LISTEN, wc.INSPECT_AUDIO, wc.OBSERVE, ORG_LIST, ORG_INSPECT):
        return "Read permitted; source remains unchanged." if performed else "Action blocked; source remains unchanged."
    if action == ORG_DELETE_CONTAINER:
        return "Organizational container deleted; referenced source resources are untouched." if performed else "Action blocked; nothing deleted."
    if action in ORG_REMOVE_REFERENCE_ACTIONS_TO_RESOURCE_CLASS:
        return "Reference removed from container; the referenced source resource is untouched." if performed else "Action blocked; nothing removed."
    if action in ORG_CREATE_ACTIONS_TO_CONTAINER_TYPE or action == ORG_RENAME or action in ORG_ADD_REFERENCE_ACTIONS_TO_RESOURCE_CLASS:
        return "Organizational metadata changed; no source resource was created, moved, or modified." if performed else "Action blocked; no organizational metadata changed."
    return "Action performed." if performed else "Action blocked."


def _execute_organization_action(paths, action, relative_path, content, requester_actor_id):
    """CAP2A-A: dispatch for resource_class="organization" -- the ONLY
    place that translates a fixed action NAME into workspace_
    organization.py's own typed parameters (container_type/target
    resource_class), via the lookup tables above -- never by parsing
    Clark's own free text. `relative_path` is always this action's
    container_id; `content` is its one free-text payload slot (a name,
    or a target relative_path), exactly as documented above."""
    container_id = relative_path
    if action == ORG_LIST:
        return wo.list_containers(paths, requester_actor_id)
    if action == ORG_INSPECT:
        return wo.inspect_container(paths, container_id, requester_actor_id)
    if action == ORG_DELETE_CONTAINER:
        return wo.delete_container(paths, container_id, requester_actor_id)
    if action == ORG_RENAME:
        return wo.rename_container(paths, container_id, content, requester_actor_id)
    if action in ORG_CREATE_ACTIONS_TO_CONTAINER_TYPE:
        return wo.create_container(paths, ORG_CREATE_ACTIONS_TO_CONTAINER_TYPE[action], content, requester_actor_id)
    if action in ORG_ADD_REFERENCE_ACTIONS_TO_RESOURCE_CLASS:
        return wo.add_reference(paths, container_id, ORG_ADD_REFERENCE_ACTIONS_TO_RESOURCE_CLASS[action], content, requester_actor_id)
    if action in ORG_REMOVE_REFERENCE_ACTIONS_TO_RESOURCE_CLASS:
        return wo.remove_reference(paths, container_id, ORG_REMOVE_REFERENCE_ACTIONS_TO_RESOURCE_CLASS[action], content, requester_actor_id)
    return None, {"rationale": "Unknown organization action."}


def _canonical_owner_note_lookup():
    """event_id -> the canonical owner_vault_note binding (read-only), or None -- the only way an owner-authored
    note is confirmed as the owner's.  None when the waking runtime is not loaded (never assumed)."""
    import sqlite3
    import sys
    import human_session_binding as hsb
    la = sys.modules.get("llama_anaxi")
    if la is None:
        return None
    db = f"{la.PROVENANCE_DB_DIR}/anaxi_provenance.db"

    def lookup(event_id):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            return hsb.owner_vault_note_record(conn, event_id)
        except sqlite3.Error:
            return None
        finally:
            conn.close()
    return lookup


def _canonical_event_lookup():
    """event_id -> bool over ANAXI's canonical provenance (read-only).  Only when the waking runtime is
    loaded; otherwise None, and no Clark authorship can be confirmed (never assumed)."""
    import sqlite3
    import sys
    la = sys.modules.get("llama_anaxi")
    if la is None:
        return None
    db = f"{la.PROVENANCE_DB_DIR}/anaxi_provenance.db"

    def lookup(event_id):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            return False
        try:
            return conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone() is not None
        except sqlite3.Error:
            return False
        finally:
            conn.close()
    return lookup


def _execute_notes_action(paths, action, relative_path, content, requester_actor_id, boundary_id, source_event_id):
    """The shared Obsidian vault (obsidian_workspace).  Permission was re-checked by the caller's
    wc.check_permission; this adds the vault's own boundaries (containment, dot-store, Private Space,
    .md only, create-only).  Every attempt is logged (ids and paths only, never note text)."""
    def log(target, outcome, detail=None):
        wc._log_action(paths, wc.NOTES, action, target, requester_actor_id, outcome, boundary_id, detail)

    allowed, _bid, rationale = wc.check_permission(wc.NOTES, action)
    if not allowed:
        log(relative_path or None, "denied", rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}
    if not paths.notes_available():
        log(None, "denied", "vault unavailable")
        return None, {"boundary_id": boundary_id,
                      "rationale": "The shared Obsidian vault is not available (not configured or not present)."}
    try:
        vault = ow.ObsidianWorkspace(paths.notes_dir)
    except ow.ObsidianWorkspaceConfigurationError as exc:
        log(None, "denied", "vault inside Private Space")
        return None, {"boundary_id": boundary_id, "rationale": str(exc)}
    if action == wc.LIST:
        payload, payload_failure = parse_list_content(content)
        if payload_failure is not None:
            return None, {"boundary_id": boundary_id, "rationale": payload_failure}
        folder = relative_path.strip().strip("/")   # validated: always a string
        query = payload["query"] or None
        try:   # the SAME cursor every listing uses (workspace_delivery windows issue it for continuation)
            start = wc._decode_list_cursor(payload["cursor"], wc.NOTES, folder, query)
        except ValueError:
            return None, {"boundary_id": boundary_id, "rationale": wc.LIST_INVALID_CURSOR}
        result, failure = vault.list_notes_compact(start_index=start, query=query, folder=folder)
        if result is not None:
            result["next_cursor"] = (wc._encode_list_cursor(wc.NOTES, folder, query, start + result["returned_count"])
                                     if result["has_more"] else None)
            request = {"cursor": result["next_cursor"]}
            if query:
                request["query"] = query
            result["next_request"] = json.dumps(request, separators=(",", ":")) if result["has_more"] else None
    elif action == wc.READ:
        payload, payload_failure = parse_library_read_content(content)
        if payload_failure is not None:
            return None, {"boundary_id": boundary_id, "rationale": payload_failure}
        name = relative_path if relative_path.lower().endswith(ow.NOTE_EXTENSION) else relative_path + ow.NOTE_EXTENSION
        result, failure = vault.read_note(name, offset=payload["offset"], max_chars=payload["max_chars"],
                                          event_lookup=_canonical_event_lookup(),
                                          owner_note_lookup=_canonical_owner_note_lookup())
        if result is not None:
            # The same continuation shape as a library read, so workspace_delivery can window a long note
            # to the room a prompt really has and Clark can read on in bounded pieces (live 2026-09-24: a
            # 10,027-char note matched no windower and was WITHHELD whole -- only field names arrived).
            # Only what Clark needs (live 2026-09-24: 16 host fields -- author three ways, provenance twice,
            # a name duplicating the path -- surrounded the note text; he rightly called it scaffolding).
            slim = {"relative_path": result["relative_path"], "authorship": result["authorship_text"]}
            if result.get("frontmatter"):
                slim["frontmatter"] = result["frontmatter"]
            if result.get("frontmatter_parse") == ow.FRONTMATTER_MALFORMED:
                slim["frontmatter_parse"] = result["frontmatter_parse"]
            slim.update(content=result["content"], total_chars=result["total_chars"])
            result = slim
            next_offset = payload["offset"] + len(result["content"])
            result["next_offset"] = next_offset
            result["has_more"] = next_offset < result["total_chars"]
            result["next_request"] = (json.dumps({"offset": next_offset, "max_chars": payload["max_chars"]},
                                                 separators=(",", ":")) if result["has_more"] else None)
    else:   # APPEND: one new note of Clark's own
        result, failure = ow.create_note(vault, relative_path, content, clark_actor_id=requester_actor_id,
                                         source_event_id=source_event_id)
        if result is not None:
            result = {k: result[k] for k in ("relative_path", "name", "created", "replayed", "authorship_text",
                                             "total_chars") if k in result}
    if failure is not None:
        log(relative_path or None, "denied", failure.get("code"))
        return None, {"boundary_id": boundary_id, "rationale": f"{failure.get('code')}: {failure.get('rationale')}"}
    detail = ({"offset": result["next_offset"] - len(result["content"]), "chars_returned": len(result["content"]),
               "total_chars": result["total_chars"]} if action == wc.READ else None)
    log((result or {}).get("relative_path") or relative_path or None, "performed", detail)
    return result, None


def normalize_workspace_action(paths, validated_action):
    """The action with its relative_path stated relative to its class root (a
    redundant leading class segment removed only when that is what makes it
    name something real). Every other field is untouched; organization and
    journal appends carry no filesystem target."""
    resource_class = validated_action["resource_class"]
    action = validated_action["action"]
    if resource_class not in wc.RESOURCE_CLASSES or action == wc.APPEND:
        return validated_action
    normalized = wc.normalize_relative_path(paths, resource_class, validated_action["relative_path"])
    if normalized == validated_action["relative_path"]:
        return validated_action
    return dict(validated_action, relative_path=normalized)


def execute_workspace_action(paths, validated_action, requester_actor_id, *,
                             session_id=None, source_event_id=None):
    """The ONLY function in this module that calls into
    workspace_capability.py. Never opens an arbitrary model-supplied
    path directly -- every branch below delegates to an existing,
    already-tested workspace_capability function, which independently
    re-checks permission (spec section 11: host capability state is
    authoritative, not Pass 1's selection).

    Returns (five_part_boundary_result, performed: bool)."""
    validated_action = normalize_workspace_action(paths, validated_action)
    resource_class = validated_action["resource_class"]
    action = validated_action["action"]
    relative_path = validated_action["relative_path"]
    content = validated_action["content"]

    # CAP2A-A: "organization" is deliberately not a wc.RESOURCE_CLASSES
    # member (see workspace_organization.py's own docstring), so it has
    # no wc.CAPABILITY_DESCRIPTORS row for wc.check_permission() to
    # look up -- calling that function with it would just fail closed
    # as "Unknown resource class", which is not what's happening here.
    # One fixed, always-true boundary triple stands in for it instead;
    # structural/existence validation still happens per-action inside
    # workspace_organization.py's own functions, which fail closed on
    # bad input exactly like every wc.* function does.
    if resource_class == ORGANIZATION:
        allowed, boundary_id, rationale = True, ORGANIZATION_BOUNDARY_ID, ORGANIZATION_RATIONALE
    else:
        allowed, boundary_id, rationale = wc.check_permission(resource_class, action)

    result, failure = None, None
    if resource_class == ORGANIZATION:
        result, failure = _execute_organization_action(paths, action, relative_path, content, requester_actor_id)
    elif resource_class == wc.LIBRARY:
        if action == wc.LIST:
            payload, payload_failure = parse_list_content(content)
            if payload_failure is None:
                result, failure = wc.list_contents(
                    paths, wc.LIBRARY, requester_actor_id,
                    directory=relative_path, cursor=payload["cursor"], query=payload["query"],
                )
            else:
                failure = {"boundary_id": boundary_id, "rationale": payload_failure}
        elif action == wc.INSPECT_METADATA:
            result, failure = wc.inspect_metadata(paths, wc.LIBRARY, relative_path, requester_actor_id)
        elif action == wc.READ:
            payload, payload_failure = parse_library_read_content(content)
            if payload_failure is None:
                result, failure = wc.read_library_bounded(
                    paths, relative_path, offset=payload["offset"], max_chars=payload["max_chars"],
                    requester_actor_id=requester_actor_id,
                )
            else:
                failure = {"boundary_id": boundary_id, "rationale": payload_failure}
        elif action == wc.VIEW_PAGE:
            payload, payload_failure = parse_pdf_page_content(content)
            if payload_failure is None:
                raw_result, failure = wc.render_library_pdf_page(
                    paths, relative_path, payload["page"], requester_actor_id,
                )
                if raw_result is not None:
                    raw_result = dict(raw_result)
                    _stash_last_view_image_bytes(raw_result.pop("image_bytes", None))
                result = raw_result
            else:
                failure = {"boundary_id": boundary_id, "rationale": payload_failure}
        else:
            failure = {"boundary_id": boundary_id, "rationale": "Action not available for library in this gate."}
    elif resource_class == wc.JOURNAL:
        if action == wc.LIST:
            payload, payload_failure = parse_list_content(content)
            if payload_failure is None and not relative_path:
                result, failure = wc.list_journal_entries(
                    paths, requester_actor_id, cursor=payload["cursor"], query=payload["query"],
                )
            else:
                failure = {"boundary_id": boundary_id, "rationale": payload_failure or ACTION_PAYLOAD_MALFORMED}
        elif action == wc.READ:
            entry_id = relative_path[:-5] if relative_path.endswith(".json") else relative_path
            payload, payload_failure = parse_library_read_content(content)
            if payload_failure is None:
                result, failure = wc.read_journal_entry(
                    paths, entry_id, requester_actor_id,
                    offset=payload["offset"], max_chars=payload["max_chars"],
                )
            else:
                failure = {"boundary_id": boundary_id, "rationale": payload_failure}
        elif action == wc.APPEND:
            result, failure = wc.append_journal_entry(
                paths, requester_actor_id, content,
                session_id=session_id, source_event_id=source_event_id,
                requester_actor_id=requester_actor_id,
                continues=relative_path or None,
            )
        else:
            failure = {"boundary_id": boundary_id, "rationale": "Action not available for journal in this gate."}
    elif resource_class == wc.NOTES:
        result, failure = _execute_notes_action(
            paths, action, relative_path, content, requester_actor_id, boundary_id, source_event_id)
    elif resource_class == wc.MUSIC:
        if action == wc.LIST:
            payload, payload_failure = parse_list_content(content)
            if payload_failure is None:
                result, failure = wc.list_contents(
                    paths, wc.MUSIC, requester_actor_id,
                    directory=relative_path, cursor=payload["cursor"], query=payload["query"],
                )
            else:
                failure = {"boundary_id": boundary_id, "rationale": payload_failure}
        elif action == wc.INSPECT_METADATA:
            result, failure = wc.inspect_metadata(paths, wc.MUSIC, relative_path, requester_actor_id)
        elif action == wc.LISTEN:
            result, failure = wc.access_music_listen(paths, relative_path, requester_actor_id)
        elif action == wc.OBSERVE:
            result, failure = wc.access_music_observe(paths, relative_path, requester_actor_id)
        elif action == wc.INSPECT_AUDIO:
            result, failure = wc.access_music_inspect_audio(paths, relative_path, content, requester_actor_id)
        else:
            failure = {"boundary_id": boundary_id, "rationale": "Only list/inspect_metadata/listen/observe/inspect_audio are available for music in this gate -- no model-level audio-perception pathway exists."}
    elif resource_class == wc.PHOTOGRAPHS:
        if action == wc.LIST:
            payload, payload_failure = parse_list_content(content)
            if payload_failure is None:
                result, failure = wc.list_contents(
                    paths, wc.PHOTOGRAPHS, requester_actor_id,
                    directory=relative_path, cursor=payload["cursor"], query=payload["query"],
                )
            else:
                failure = {"boundary_id": boundary_id, "rationale": payload_failure}
        elif action == wc.INSPECT_METADATA:
            result, failure = wc.inspect_metadata(paths, wc.PHOTOGRAPHS, relative_path, requester_actor_id)
        elif action == wc.VIEW:
            # CAP2-B: wc.deliver_photograph_bytes()'s own result carries
            # raw `image_bytes` -- stripped out HERE, unconditionally,
            # before `result` is ever assigned into boundary_result
            # below, so boundary_result stays JSON-safe/metadata-only
            # for every caller (see the module comment on
            # get_and_clear_last_view_image_bytes() above for why this
            # matters for the shared roaming pathway specifically). The
            # real bytes are stashed in the one-shot side channel for
            # workspace_supervisor.py alone to retrieve.
            raw_result, failure = wc.deliver_photograph_bytes(paths, relative_path, requester_actor_id)
            if raw_result is not None:
                raw_result = dict(raw_result)
                _stash_last_view_image_bytes(raw_result.pop("image_bytes", None))
            result = raw_result
        else:
            failure = {"boundary_id": boundary_id, "rationale": "Only list/inspect_metadata/view are available for photographs in this gate -- no audio pathway exists and view is genuine-pixel-only."}
    else:
        failure = {"boundary_id": None, "rationale": "Unknown resource class."}

    performed = failure is None
    # The owner's logical source (the named folder an item is filed under) is
    # meaningful context that must reach the subject with the item: a workspace
    # copy's own path can hide it (a duplicate bound to an existing root copy).
    if performed and isinstance(result, dict) and resource_class in wprov.PUBLIC_CLASSES:
        if action == wc.LIST:
            result = wprov.attach_entry_folders(paths, resource_class, relative_path, result)
        elif wc.is_target_bearing(resource_class, action):
            result = wprov.attach_resource_provenance(paths, resource_class, relative_path, result)
    # The permission descriptor explains the standing boundary.  A concrete
    # runtime/payload failure explains why THIS requested operation did not
    # happen and must take precedence in the returned receipt.  Returning the
    # generic permission rationale here used to hide PDF_TEXT_UNAVAILABLE,
    # malformed payloads, decoder failures, and other truthful operational
    # reasons from ordinary callers even though lower layers produced them.
    if failure is not None:
        effective_boundary_id = failure.get("boundary_id") or boundary_id
        effective_rationale = failure.get("rationale") or rationale
    else:
        effective_boundary_id = boundary_id
        effective_rationale = rationale

    boundary_result = {
        "action": action,
        "scope": f"local_workspace/{resource_class}",
        "boundary": effective_boundary_id,
        "consequence": _consequence_text(action, performed, resource_class),
        "rationale": effective_rationale,
        "result": result if performed else None,
    }
    return boundary_result, performed


# ------------------------------------------------------------------- Pass 2 --


def build_pass2_interface(system_content, dialogue_window, current_user_message, validated_action, boundary_result):
    return {
        "system_content": system_content,
        "dialogue_window": list(dialogue_window),
        "current_user_message": current_user_message,
        "typed_action": dict(validated_action),
        "boundary_result": dict(boundary_result),
        "task_instruction": PASS2_TASK_INSTRUCTION,
    }


def validate_pass2_expression(raw_pass2_output):
    if isinstance(raw_pass2_output, str):
        try:
            raw = json.loads(raw_pass2_output)
        except (TypeError, ValueError):
            return None, DirectionFailure.MALFORMED_EXPRESSION
    elif isinstance(raw_pass2_output, dict):
        raw = raw_pass2_output
    else:
        return None, DirectionFailure.MALFORMED_EXPRESSION

    if not isinstance(raw, dict) or set(raw.keys()) != PASS2_ALLOWED_FIELDS:
        return None, DirectionFailure.MALFORMED_EXPRESSION

    expression = raw.get("expression")
    if not isinstance(expression, str) or len(expression.strip()) == 0:
        return None, DirectionFailure.EMPTY_EXPRESSION

    return {"expression": expression}, None


# --------------------------------------------------------------- orchestration


def run_typed_workspace_turn(paths, requester_actor_id, pass1_callable, pass2_callable):
    """Orchestrates exactly one Pass 1 and, only on structural success,
    exactly one host execution and one Pass 2. No retries anywhere. A
    malformed Pass 1 fails closed with zero host execution; a Pass-2
    failure leaves the already-performed (or already-denied) action
    fully observable via the returned boundary_result, but produces no
    expression/release.

    pass1_callable/pass2_callable: zero-arg callables, matching
    conversation_direction.py's own convention.

    Returns (result, failure, boundary_result, pass1_calls, pass2_calls).
    result is None on any failure; otherwise
    {"validated_action", "boundary_result", "expression"}.
    """
    raw1 = pass1_callable()
    pass1_calls = 1

    validated_action, failure = validate_pass1_workspace_action(raw1)
    if failure is not None:
        return None, failure, None, pass1_calls, 0

    boundary_result, _performed = execute_workspace_action(paths, validated_action, requester_actor_id)

    raw2 = pass2_callable()
    pass2_calls = 1

    validated_expr, failure2 = validate_pass2_expression(raw2)
    if failure2 is not None:
        return None, failure2, boundary_result, pass1_calls, pass2_calls

    result = {
        "validated_action": validated_action,
        "boundary_result": boundary_result,
        "expression": validated_expr["expression"],
    }
    return result, None, boundary_result, pass1_calls, pass2_calls


# ------------------------------------------------ diagnostic trace (S2 sec 26)

_last_trace_state = {"trace": None}


def set_last_workspace_direction_trace(trace):
    _last_trace_state["trace"] = dict(trace)


def get_last_workspace_direction_trace():
    trace = _last_trace_state["trace"]
    return dict(trace) if trace is not None else None


def reset_last_workspace_direction_trace():
    _last_trace_state["trace"] = None
