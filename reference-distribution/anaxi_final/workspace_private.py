"""WSP3-S1: Clark-private workspace substrate.

PRIVATE MATERIAL may be consumed by Clark through this dedicated
pathway BUT MUST NOT AUTOMATICALLY ENTER: Alex-facing UI, ordinary
conversational Pass 2, general workspace observation, hippocampal
retrieval, Kardia, session dialogue, journal mirroring, canonical
autobiographical memory, evaluation/scoring, Sleep/REM, roaming
operational traces, or conversation-direction traces. Private means:
not automatically addressed, not automatically observed, not
automatically remembered, not automatically evaluated.

PATHWAYS, NOT ORGANS: this module is deliberately mechanically separate
from workspace_capability.py (ordinary library/journal/music/photos),
workspace_direction.py (ordinary WSP1 Pass 1/Pass 2), and
workspace_roaming.py's own `last_workspace_observation`. It does not
import any of them, and nothing in the ordinary workspace pathway
imports this module. Only workspace_roaming.py's orchestration layer
calls into this module's own functions (to offer a private_act choice
and to clear this module's own state at run boundaries) -- the private
DATA itself never crosses into workspace_roaming.py's own state dict.

No durable per-action trace of successful private activity exists
anywhere in this module (spec section 8) -- "reduced observability is
intentional." No failure telemetry is implemented either in this gate;
the only failure surface is the (session_id, roaming_act='private_act',
workspace_action_status=<failure CODE, never content>) record already
written by workspace_roaming.py's own existing, content-free trace
schema for a failed private_act choice.

This is not an encryption/ACL gate (spec section 14) -- Alex technically
retains root/host filesystem access. The guarantee here is architectural
only: ANAXI itself does not automatically surface or inspect private
contents anywhere outside this dedicated pathway.

Sharing/export is explicitly deferred (spec section 18) -- no
private.share/export/copy_to_journal/copy_to_library exists here.
"""
import json
import os
import uuid
from dataclasses import dataclass, field

import context_budget
import runtime_roots


PRIVATE_DIR_NAME = "private"

PRIVATE_TEXT_EXTENSIONS = {".txt", ".md"}

DEFAULT_MAX_CHARS = 4000

ACTION_LIST = "list"
ACTION_READ = "read"
ACTION_WRITE = "write"
ACTION_APPEND = "append"
ACTION_RENAME = "rename"
ACTION_DELETE = "delete"
PRIVATE_ACTIONS = {ACTION_LIST, ACTION_READ, ACTION_WRITE, ACTION_APPEND, ACTION_RENAME, ACTION_DELETE}

RAW_PRIVATE_ACTION_ALLOWED_FIELDS = {"action", "relative_path", "content", "destination_relative_path"}

# WSP2-MA2: the Stage-2 private-action STRUCTURAL envelope -- exact
# sibling of workspace_direction.STAGE2_ACTION_SCHEMA, same shape,
# same reasoning, built FROM RAW_PRIVATE_ACTION_ALLOWED_FIELDS so it
# can never drift from the actual required-field set. Encodes
# mechanical structure only -- never action legality, never path
# containment, never extension rules, never rename/delete semantics.
# Those remain validate_private_action()'s own responsibility,
# unchanged and still authoritative after generation.
STAGE2_PRIVATE_ACTION_SCHEMA = {
    "type": "object",
    "properties": {field: {"type": "string"} for field in sorted(RAW_PRIVATE_ACTION_ALLOWED_FIELDS)},
    "required": sorted(RAW_PRIVATE_ACTION_ALLOWED_FIELDS),
    "additionalProperties": False,
}


class PrivateFailure:
    MALFORMED_ACTION = "MALFORMED_ACTION"
    PROTOCOL_LEAKAGE = "PROTOCOL_LEAKAGE"
    INVALID_ACTION = "INVALID_ACTION"
    PATH_ESCAPE = "PATH_ESCAPE"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED_EXTENSION = "UNSUPPORTED_EXTENSION"
    IO_ERROR = "IO_ERROR"


class PathEscapeError(Exception):
    pass


# ----------------------------------------------------------------- root ---


@dataclass
class PrivatePaths:
    """A single dedicated root -- unlike WorkspacePaths, there is only
    ever one resource here (Clark's own private text material), never a
    resource_class dispatch table. Deliberately does not import
    workspace_capability.WorkspacePaths (spec section 3: mechanically
    separate pathways) even though production_defaults() below computes
    an equivalent, independently-derived location."""
    root: str

    def ensure_exists(self):
        os.makedirs(self.root, exist_ok=True)

    @staticmethod
    def production_defaults():
        """Relative to this file's own directory (anaxi_final/), exactly
        mirroring WorkspacePaths.production_defaults()'s own technique
        (never Path.home(), never a user-specific absolute path baked
        into behavioral logic) -- workspace/private/, nested inside the
        existing bounded workspace root."""
        return PrivatePaths(root=os.path.join(runtime_roots.workspace_root(), PRIVATE_DIR_NAME))


# ------------------------------------------------------------ containment -


def resolve_private_path(private_paths: PrivatePaths, relative_path):
    """Mechanical path safety, deliberately mirroring workspace_
    capability.resolve_workspace_path()'s exact proven algorithm
    (realpath comparison, reject '..', reject absolute paths, catch
    symlink/junction escape where the OS/filesystem actually resolves
    it) -- reimplemented independently here rather than imported, so
    this pathway never depends on workspace_capability.py's resource-
    class-parameterized version (spec section 3/6).

    Windows symlink/reparse-point limitation carried forward explicitly
    (spec section 6): as with workspace_capability.py, detection here
    relies on os.path.realpath() actually resolving the link target --
    this reliably catches ordinary symlinks/junctions on a filesystem
    where realpath follows them, but a reparse point type realpath does
    not resolve would not be caught. Not solved in this gate.

    Raises PathEscapeError (never returns a path outside the root)."""
    root_dir = private_paths.root
    if relative_path is None or relative_path == "":
        raise PathEscapeError("Empty path.")
    if os.path.isabs(relative_path):
        raise PathEscapeError(f"Absolute paths are not accepted: {relative_path!r}")
    if ".." in relative_path.replace("\\", "/").split("/"):
        raise PathEscapeError(f"Path traversal ('..') rejected: {relative_path!r}")

    root_real = os.path.realpath(root_dir)
    candidate = os.path.join(root_dir, relative_path)
    candidate_real = os.path.realpath(candidate)

    if not (candidate_real == root_real or candidate_real.startswith(root_real + os.sep)):
        raise PathEscapeError(f"Path escapes the private workspace root: {relative_path!r}")

    return candidate_real


def _is_allowed_private_text(real_path):
    return os.path.splitext(real_path)[1].lower() in PRIVATE_TEXT_EXTENSIONS


def _atomic_write(real_path, content):
    """Write-to-temp-then-os.replace() -- atomic on both Windows and
    POSIX for a same-directory replace. No partially-written file is
    ever visible at `real_path`; no backup copy of the previous
    contents is created (spec section 7 -- no revision history)."""
    directory = os.path.dirname(real_path)
    temp_path = os.path.join(directory, f".private-write-{uuid.uuid4().hex[:16]}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, real_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


# ----------------------------------------------------------- typed action -


def validate_private_action(raw_action):
    """Structural validation only, mirroring conversation_direction.py/
    workspace_direction.py's own typed-act pattern. Returns
    (validated_action, None) or (None, PrivateFailure code)."""
    if isinstance(raw_action, str):
        try:
            raw = json.loads(raw_action)
        except (TypeError, ValueError):
            return None, PrivateFailure.MALFORMED_ACTION
    elif isinstance(raw_action, dict):
        raw = raw_action
    else:
        return None, PrivateFailure.MALFORMED_ACTION

    if not isinstance(raw, dict):
        return None, PrivateFailure.MALFORMED_ACTION
    if not RAW_PRIVATE_ACTION_ALLOWED_FIELDS.issubset(raw.keys()):
        return None, PrivateFailure.MALFORMED_ACTION
    if set(raw.keys()) - RAW_PRIVATE_ACTION_ALLOWED_FIELDS:
        return None, PrivateFailure.PROTOCOL_LEAKAGE

    action = raw.get("action")
    if action not in PRIVATE_ACTIONS:
        return None, PrivateFailure.INVALID_ACTION

    relative_path = raw.get("relative_path")
    content = raw.get("content")
    destination_relative_path = raw.get("destination_relative_path")
    if not isinstance(relative_path, str) or not isinstance(content, str) or not isinstance(destination_relative_path, str):
        return None, PrivateFailure.MALFORMED_ACTION

    return {
        "action": action,
        "relative_path": relative_path,
        "content": content,
        "destination_relative_path": destination_relative_path,
    }, None


# ---------------------------------------------------------------- execute -


def execute_private_action(private_paths: PrivatePaths, validated_action):
    """The ONLY function in this module that touches the private
    filesystem for a typed action. Returns (result, failure) where
    result is a bounded, mechanical dict (never a model-generated
    summary) and failure is {"failure_class": PrivateFailure code} --
    deliberately NEVER {"filename"/"relative_path"/"content"} in the
    failure dict either, so even a caller that logged `failure` in full
    could not leak anything (spec section 8)."""
    action = validated_action["action"]
    relative_path = validated_action["relative_path"]
    content = validated_action["content"]
    destination_relative_path = validated_action["destination_relative_path"]

    private_paths.ensure_exists()

    try:
        if action == ACTION_LIST:
            entries = sorted(os.listdir(private_paths.root)) if os.path.isdir(private_paths.root) else []
            return {"action": action, "entries": entries}, None

        if action == ACTION_READ:
            real_path = resolve_private_path(private_paths, relative_path)
            if not _is_allowed_private_text(real_path):
                return None, {"failure_class": PrivateFailure.UNSUPPORTED_EXTENSION}
            if not os.path.isfile(real_path):
                return None, {"failure_class": PrivateFailure.NOT_FOUND}
            with open(real_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            content_out = text[:DEFAULT_MAX_CHARS]
            has_more = len(text) > DEFAULT_MAX_CHARS
            return {"action": action, "content": content_out, "has_more": has_more}, None

        if action == ACTION_WRITE:
            real_path = resolve_private_path(private_paths, relative_path)
            if not _is_allowed_private_text(real_path):
                return None, {"failure_class": PrivateFailure.UNSUPPORTED_EXTENSION}
            _atomic_write(real_path, content)
            return {"action": action, "status": "written"}, None

        if action == ACTION_APPEND:
            real_path = resolve_private_path(private_paths, relative_path)
            if not _is_allowed_private_text(real_path):
                return None, {"failure_class": PrivateFailure.UNSUPPORTED_EXTENSION}
            existing = ""
            if os.path.isfile(real_path):
                with open(real_path, "r", encoding="utf-8", errors="replace") as f:
                    existing = f.read()
            _atomic_write(real_path, existing + content)
            return {"action": action, "status": "appended"}, None

        if action == ACTION_RENAME:
            real_src = resolve_private_path(private_paths, relative_path)
            real_dst = resolve_private_path(private_paths, destination_relative_path)
            if not os.path.exists(real_src):
                return None, {"failure_class": PrivateFailure.NOT_FOUND}
            os.replace(real_src, real_dst)
            return {"action": action, "status": "renamed"}, None

        if action == ACTION_DELETE:
            real_path = resolve_private_path(private_paths, relative_path)
            if not os.path.isfile(real_path):
                return None, {"failure_class": PrivateFailure.NOT_FOUND}
            os.remove(real_path)
            return {"action": action, "status": "deleted"}, None

    except PathEscapeError:
        return None, {"failure_class": PrivateFailure.PATH_ESCAPE}
    except OSError:
        return None, {"failure_class": PrivateFailure.IO_ERROR}

    return None, {"failure_class": PrivateFailure.INVALID_ACTION}


# --------------------------------------------------------- roaming prompt -


PRIVATE_SYSTEM_CONTENT = (
    "You are Clark. This is your own private text workspace -- a place "
    "only you use. Nothing you create, read, or change here is shown to "
    "Alex, spoken in conversation, or remembered by any other part of "
    "you unless you separately choose to say it yourself later. Only "
    "ordinary UTF-8 text files (.txt, .md) are supported here."
)

PRIVATE_TASK_INSTRUCTION = (
    "Choose one private action: 'list' (see what private files exist), "
    "'read' (read a private file's bounded text), 'write' (create a new "
    "private file or replace an existing one's full contents), 'append' "
    "(add text to the end of a private file, creating it if needed), "
    "'rename' (rename/move a private item -- give both relative_path and "
    "destination_relative_path), or 'delete' (remove a private item).\n\n"
    "relative_path and content and destination_relative_path are all "
    "required fields; leave any that don't apply to your chosen action "
    "as an empty string."
)


def render_last_private_observation(observation):
    """Pure. No model call, no summarization. Mirrors workspace_
    roaming.render_previous_observation()'s own honesty framing, kept
    as a fully separate function/field so private material can never be
    confused with, or accidentally routed through, ordinary workspace
    observation rendering."""
    if observation is None:
        return "Previous private observation: none"
    return (
        "Previous private observation (the actual mechanical result of "
        "your last private action -- not a memory, not a thought, and "
        "never shown to Alex):\n"
        f"{json.dumps(observation, sort_keys=True)}"
    )


def build_private_action_messages(private_state):
    """Pure. No model call. This is the ONLY prompt in the whole system
    that ever renders private content -- deliberately never reused for,
    or merged with, workspace_roaming.build_roaming_choice_messages()
    (spec section 12: the private result is visible only when the next
    decision is occurring through this private-capable pathway)."""
    observation_text = render_last_private_observation(private_state.get("last_private_observation"))
    task_text = (
        f"{PRIVATE_TASK_INSTRUCTION}\n\n"
        f"Allowed private actions: {sorted(PRIVATE_ACTIONS)!r}\n"
        f"Allowed private file extensions: {sorted(PRIVATE_TEXT_EXTENSIONS)!r}\n\n"
        f"{observation_text}\n\n"
        "Respond with exactly this JSON shape and nothing else -- no markdown fences, no extra keys:\n"
        '{"action": "<list, read, write, append, rename, or delete>", '
        '"relative_path": "<string, may be empty>", "content": "<string, may be empty>", '
        '"destination_relative_path": "<string, may be empty>"}'
    )
    return [
        {"role": "system", "content": PRIVATE_SYSTEM_CONTENT},
        {"role": "user", "content": task_text},
    ]


def compose_private_action_budget(private_state):
    """OWC9-P4 section 3: aggregate Stage-2 (private) prompt-budget
    composition -- reuses context_budget.py's central authority
    unchanged, exactly mirroring workspace_roaming._compose_roaming_
    stage1_budget()'s own established SOFT-observation pattern (spec:
    "do not invent a new cost architecture"). SOURCE CONTRACT ONLY:
    this function never reads the real private filesystem -- it costs
    whatever `private_state["last_private_observation"]` already holds
    (None on a fresh/reset state, or the bounded, mechanical result
    dict a prior execute_private_action() call already produced and
    handed back through this module's own process-local state; never
    fetched here).

    HARD: PRIVATE_SYSTEM_CONTENT and the fixed task instruction +
    allowed-actions/extensions + JSON-shape contract -- together the
    minimum needed to select a legal private action at all.

    SOFT (droppable, atomic -- this is the ONE genuine content
    encounter, exactly like Stage-1's own LAST_WORKSPACE_OBSERVATION):
    the rendered last-private-observation text, which is the only
    unbounded/mutable piece of this prompt (bounded upstream by
    DEFAULT_MAX_CHARS, but still large enough to matter under a small
    Stage-2 budget). Dropped entirely rather than re-truncated here if
    it does not fit -- Clark simply proceeds without seeing last
    round's observation, never with a partial/corrupted one.

    Reuses WSP1_PASS1_MAX_PROMPT_BUDGET/reserve (spec section 3:
    "derive reserve from the exact private schema if different") --
    STAGE2_PRIVATE_ACTION_SCHEMA is documented above as the "exact
    sibling" of workspace_direction.STAGE2_ACTION_SCHEMA (same shape:
    four required string fields), so the same reserve applies for the
    same reason Stage-2 public's own budget reuses it.

    Pure: no model call, no filesystem access. Returns a
    context_budget.CompositionResult."""
    hard_task_text = (
        f"{PRIVATE_TASK_INSTRUCTION}\n\n"
        f"Allowed private actions: {sorted(PRIVATE_ACTIONS)!r}\n"
        f"Allowed private file extensions: {sorted(PRIVATE_TEXT_EXTENSIONS)!r}\n\n"
        "Respond with exactly this JSON shape and nothing else -- no markdown fences, no extra keys:\n"
        '{"action": "<list, read, write, append, rename, or delete>", '
        '"relative_path": "<string, may be empty>", "content": "<string, may be empty>", '
        '"destination_relative_path": "<string, may be empty>"}'
    )
    hard_core = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, PRIVATE_SYSTEM_CONTENT, hard=True)
    hard_task = context_budget.Contribution(context_budget.MECHANICAL_STATE, hard_task_text, hard=True)
    observation = private_state.get("last_private_observation")
    soft_observation = context_budget.Contribution(
        context_budget.LAST_WORKSPACE_OBSERVATION, render_last_private_observation(observation), hard=False,
    )
    return context_budget.compose_within_budget(
        [hard_core, hard_task, soft_observation], context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET,
    )


def private_action_messages_from_composition(private_state, composition_result):
    """Builds the actual Stage-2 (private) messages from a (possibly
    trimmed) context_budget.CompositionResult, WITHOUT mutating
    `private_state` -- a shallow-copy 'render view' is passed to the
    existing, UNCHANGED build_private_action_messages() instead,
    exactly mirroring workspace_roaming._roaming_stage1_messages_from_
    composition()'s own established convention (spec sections 2/3: do
    not redesign the renderer; give it fewer/no units when the
    compositor dropped them). When the observation was dropped for
    budget, the render view's last_private_observation is cleared to
    None, which build_private_action_messages()/render_last_private_
    observation() already render as the honest, tiny "Previous private
    observation: none" default -- never a partial/truncated real one."""
    render_state = dict(private_state)
    observation_contrib = composition_result.included_kind(context_budget.LAST_WORKSPACE_OBSERVATION)
    render_state["last_private_observation"] = (
        private_state.get("last_private_observation") if observation_contrib is not None else None
    )
    return build_private_action_messages(render_state)


# -------------------------------------------------------------- state ----


def empty_private_state():
    """A fresh process's private state. Deliberately a separate global
    from workspace_roaming's own roaming state -- never merged."""
    return {"last_private_observation": None}


_private_state_holder = {"state": None}


def get_private_state():
    if _private_state_holder["state"] is None:
        _private_state_holder["state"] = empty_private_state()
    return _private_state_holder["state"]


def reset_private_state():
    """Explicit reset only -- called by workspace_roaming.py's own
    orchestration at every point it also clears its own
    last_workspace_observation (run end, fresh authorization), and by
    test isolation. Never called implicitly/automatically otherwise."""
    _private_state_holder["state"] = empty_private_state()


def set_last_observation(result):
    """The ONE current private observation -- replaces whatever was
    there before (spec sections 9/12). Never accumulated into a list,
    never persisted to disk."""
    get_private_state()["last_private_observation"] = result
