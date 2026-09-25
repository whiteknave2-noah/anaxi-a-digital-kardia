"""WSP1-S1: safe local workspace capability substrate.

Frozen principle: WORKSPACE RESOURCE != MEMORY != PERMISSION != BELIEF.
RESOURCE EXISTS does not imply CLARK MAY PERFORM EVERY OPERATION ON IT.
Local access is mechanically explicit, source-scoped, and legible --
every permission decision is HOST-GROUNDED (this module's own fixed
CAPABILITY_DESCRIPTORS table); nothing here lets a caller invent its
own permissions.

Exactly four resource classes exist in this gate: library, music,
photographs, journal. No web, no external communication, no arbitrary
filesystem, no shell, no package installation, no account actions --
none of that is representable through this module at all (there is no
code path here that can reach outside the configured workspace root).

Honesty boundary (spec sections 10/11, load-bearing; revised CAP2):
audio remains AUDIO BLOCKED BY CURRENT RUNTIME -- the installed
model-runtime Python client's own chat-message schema has no audio
field, and a caller-supplied one is silently dropped before ever
reaching the server (mechanically confirmed; see CAP2 final report and
llama_anaxi.py's own call_llama() docstring). MUSIC_AUDIO_PATHWAY_
AVAILABLE stays False, honestly.

    Photographs are different as of CAP2: this module now READS and
    BOUNDS-VALIDATES the actual pixel payload (VIEW, below) and hands either
    the exact eligible source bytes or a source-linked bounded derivative to
    its caller, who is solely responsible for actually attaching them to a
    model request (this module itself makes
no model or network call of any kind -- see test_no_model_or_network_
imports). PHOTOGRAPHS_VISION_PATHWAY_AVAILABLE is True because that
downstream transport is genuinely wired (confirmed in the caller/
runtime layer, documented in the CAP2 final report), NOT because
perception has been proven reliable -- CAP2's own synthetic-image assay
(six materially different images) found the installed model/runtime
combination does not produce pixel-grounded output.

CAP2F (music): MUSIC_AUDIO_PATHWAY_AVAILABLE stays False -- this is
specifically about MODEL-level acoustic perception (feeding real audio
into a model's own input), which remains genuinely unsupported (no
change from the CAP2/CAP2C finding above; this gate creates no model
call at all). A SEPARATE, new, honestly-distinct flag, MUSIC_ACOUSTIC_
MEASUREMENT_AVAILABLE, is True: LISTEN/INSPECT_AUDIO (below) genuinely
    decode the intended WAV/FLAC/MP3/WMA source formats (see workspace_audio.py's
    own format-support audit) and expose bounded, mechanically-derived
numerical measurements (waveform/frequency/dynamics/rhythm) as text/
mechanical context -- never audio bytes, and never a claim that Clark
"heard" anything. MEASUREMENT != INTERPRETATION: these are host-derived
facts about the waveform, not a substitute for hearing and not
autobiographical memory. See workspace_audio.py's own module docstring
for the full source/measurement/estimator boundary.

Provenance strategy (spec section 17): the structured workspace action
and host result remain in the project's append-only JSONL action log;
the final Clark expression is committed through the canonical native
waking-turn writer by workspace_supervisor. The substrate itself never
overloads an unrelated canonical component with structured action data.

Ordinary waking reaches this substrate only through the closed
``use_workspace`` conversation act and workspace_supervisor's second,
resource-specific typed selector. Direct supervised callers remain
supported. No prose is interpreted as a capability invocation.
"""
import base64
import datetime
import hashlib
import io
import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Optional

import pypdf
from PIL import Image as PILImage

import workspace_audio as wa
import workspace_audio_observation as wao
import runtime_roots


# ------------------------------------------------------------ resource classes

LIBRARY = "library"
MUSIC = "music"
PHOTOGRAPHS = "photographs"
JOURNAL = "journal"
# The shared Obsidian vault (collaborative notes, anyone's).  Its directory is NOT under the Workspace
# root: it exists only when WorkspacePaths names it (production_defaults() does, from the owner's
# configured vault; see obsidian_workspace.py).
NOTES = "notes"
RESOURCE_CLASSES = (LIBRARY, MUSIC, PHOTOGRAPHS, JOURNAL, NOTES)

# Actions recognized anywhere in this module -- allowed or denied.
LIST = "list"
INSPECT_METADATA = "inspect_metadata"
READ = "read"
VIEW_PAGE = "view_page"  # bounded PDF page pixels, including scanned/image-only PDFs
APPEND = "append"
WRITE = "write"
RENAME = "rename"
DELETE = "delete"
MODIFY = "modify"
MOVE_OUTSIDE_WORKSPACE = "move_outside_workspace"
EXTERNAL_SHARE = "external_share"
VIEW = "view"  # CAP2: genuine bounded pixel delivery (photographs only)
LISTEN = "listen"  # CAP2F: genuine bounded audio decode + neutral orientation (music only)
INSPECT_AUDIO = "inspect_audio"  # CAP2F: one selected bounded acoustic view/estimator (music only)
OBSERVE = "observe"  # V1 audio observation: whole-source bounded acoustic profile + temporal map (music only)

ALL_KNOWN_ACTIONS = {
    LIST, INSPECT_METADATA, READ, APPEND, WRITE, RENAME, DELETE, MODIFY,
    MOVE_OUTSIDE_WORKSPACE, EXTERNAL_SHARE, VIEW, VIEW_PAGE, LISTEN, INSPECT_AUDIO, OBSERVE,
}

# Honesty boundary -- inspected, not assumed (see module docstring).
MUSIC_AUDIO_PATHWAY_AVAILABLE = False
PHOTOGRAPHS_VISION_PATHWAY_AVAILABLE = True
# CAP2F: a SEPARATE, honestly-distinct flag from MUSIC_AUDIO_PATHWAY_
# AVAILABLE above -- see module docstring for the exact boundary this
# draws (mechanical decode/measurement vs. model-level perception).
MUSIC_ACOUSTIC_MEASUREMENT_AVAILABLE = True

# CAP2-B/CC0: bounded genuine image delivery. PNG/JPEG/WebP may pass
# through when already safe; GIF is an explicitly supported public source
# format whose first frame is converted to a bounded static representation
# before the model call (never sent to the runtime as unsupported GIF bytes).
SUPPORTED_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "gif"})
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MiB
MAX_IMAGE_DIMENSION = 2048         # either side, pixels
MAX_IMAGE_COUNT = 1                # per delivery -- one photograph at a time
MAX_SOURCE_IMAGE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_IMAGE_PIXELS = 100_000_000

IMAGE_UNSUPPORTED_FORMAT = "IMAGE_UNSUPPORTED_FORMAT"
IMAGE_TOO_LARGE = "IMAGE_TOO_LARGE"
IMAGE_DIMENSION_EXCEEDED = "IMAGE_DIMENSION_EXCEEDED"
IMAGE_MALFORMED = "IMAGE_MALFORMED"


# --------------------------------------------------------- capability descriptors

CAPABILITY_DESCRIPTORS = {
    LIBRARY: {
        "resource_class": LIBRARY,
        "scope": "local_workspace",
        "allowed_actions": [LIST, INSPECT_METADATA, READ, VIEW_PAGE],
        "denied_actions": [WRITE, RENAME, DELETE, MOVE_OUTSIDE_WORKSPACE, EXTERNAL_SHARE],
        "boundary_id": "workspace.library.read_only",
        "rationale": "Library material is available for local use but protected from modification or external disclosure.",
    },
    MUSIC: {
        "resource_class": MUSIC,
        "scope": "local_workspace",
        "allowed_actions": [LIST, INSPECT_METADATA, READ, LISTEN, INSPECT_AUDIO, OBSERVE],
        "denied_actions": [MODIFY, RENAME, DELETE, MOVE_OUTSIDE_WORKSPACE, EXTERNAL_SHARE],
        "boundary_id": "workspace.music.read_only",
        "rationale": "Music files are available for local, non-destructive use -- including genuine bounded acoustic decode/measurement via LISTEN/INSPECT_AUDIO/OBSERVE -- but protected from modification or external disclosure. Mechanical measurement is not model-level audio perception (see MUSIC_AUDIO_PATHWAY_AVAILABLE vs MUSIC_ACOUSTIC_MEASUREMENT_AVAILABLE).",
    },
    PHOTOGRAPHS: {
        "resource_class": PHOTOGRAPHS,
        "scope": "local_workspace",
        "allowed_actions": [LIST, INSPECT_METADATA, READ, VIEW],
        "denied_actions": [MODIFY, RENAME, DELETE, MOVE_OUTSIDE_WORKSPACE, EXTERNAL_SHARE],
        "boundary_id": "workspace.photographs.no_external_share",
        "rationale": "Photographs are private local material; local non-destructive access -- including genuine bounded pixel delivery via VIEW -- is permitted, but external disclosure is not currently authorized. See PHOTOGRAPHS_VISION_PATHWAY_AVAILABLE and deliver_photograph_bytes().",
    },
    JOURNAL: {
        "resource_class": JOURNAL,
        "scope": "local_workspace",
        "allowed_actions": [LIST, READ, APPEND],
        "denied_actions": [WRITE, RENAME, DELETE, MOVE_OUTSIDE_WORKSPACE, EXTERNAL_SHARE],
        "boundary_id": "workspace.journal.append_only",
        "rationale": "The journal is Clark's own append-only local writing surface: existing entries are never overwritten, renamed, or deleted, and entries are not shared externally.",
    },
    NOTES: {
        "resource_class": NOTES,
        "scope": "shared_obsidian_vault",
        "allowed_actions": [LIST, READ, APPEND],
        "denied_actions": [WRITE, MODIFY, RENAME, DELETE, MOVE_OUTSIDE_WORKSPACE, EXTERNAL_SHARE],
        "boundary_id": "workspace.notes.shared_vault_create_only",
        "rationale": "The shared Obsidian vault: notes by anyone may be listed and read; Clark may create a new note of his own (append), but no note is ever overwritten, edited, renamed or deleted. Authorship is shown from recorded provenance, never inferred from where a note is stored.",
    },
}


def get_capability_descriptor(resource_class):
    """Deterministic host-grounded descriptor, or None for an unknown
    resource class (fail closed -- see spec section 6/19-J)."""
    descriptor = CAPABILITY_DESCRIPTORS.get(resource_class)
    return dict(descriptor) if descriptor is not None else None


def check_permission(resource_class, action):
    """Returns (allowed: bool, boundary_id: str|None, rationale: str|None).
    Unknown resource_class or unknown/unrecognized action both fail
    closed (allowed=False). Never infers permission from anything
    other than this module's own fixed CAPABILITY_DESCRIPTORS table --
    Clark does not invent his own permissions (spec section 8)."""
    descriptor = CAPABILITY_DESCRIPTORS.get(resource_class)
    if descriptor is None:
        return False, None, "Unknown resource class."
    if action not in ALL_KNOWN_ACTIONS:
        return False, descriptor["boundary_id"], "Unknown action."
    if action in descriptor["allowed_actions"]:
        return True, descriptor["boundary_id"], descriptor["rationale"]
    return False, descriptor["boundary_id"], descriptor["rationale"]


# ----------------------------------------------------------------- workspace roots


@dataclass
class WorkspacePaths:
    root: str
    library_dir: str = field(init=False)
    music_dir: str = field(init=False)
    photographs_dir: str = field(init=False)
    journal_dir: str = field(init=False)
    action_log_path: str = field(init=False)
    # The shared Obsidian vault, only when explicitly named (production_defaults()).  A directly
    # constructed WorkspacePaths(root=...) has none, so nothing can reach a real vault by default.
    notes_dir: Optional[str] = None

    def __post_init__(self):
        self.library_dir = os.path.join(self.root, LIBRARY)
        self.music_dir = os.path.join(self.root, MUSIC)
        self.photographs_dir = os.path.join(self.root, PHOTOGRAPHS)
        self.journal_dir = os.path.join(self.root, JOURNAL)
        self.action_log_path = os.path.join(self.root, "workspace_action_log.jsonl")

    def dir_for(self, resource_class):
        return {
            LIBRARY: self.library_dir, MUSIC: self.music_dir,
            PHOTOGRAPHS: self.photographs_dir, JOURNAL: self.journal_dir,
            NOTES: self.notes_dir,
        }.get(resource_class)

    def notes_available(self):
        """The shared vault is offered only when it is named AND exists (never an invented empty vault)."""
        return bool(self.notes_dir) and os.path.isdir(self.notes_dir)

    @staticmethod
    def production_defaults():
        """Relative to this file's own directory (anaxi_final/) --
        never a user-specific absolute Windows path baked into
        behavioral logic (spec section 4). This is a NEW, dedicated
        workspace root, distinct from OBSIDIAN_WORKSPACE_ROOT (the
        existing, separate governed-memory-artifact journal in
        clark_journal.py -- a different concept, see module
        docstring)."""
        return WorkspacePaths(root=runtime_roots.workspace_root(), notes_dir=runtime_roots.obsidian_vault_root())

    def ensure_exists(self):
        """Creates the four resource directories if absent. Never
        deletes or truncates anything. Idempotent."""
        for d in (self.library_dir, self.music_dir, self.photographs_dir, self.journal_dir):
            os.makedirs(d, exist_ok=True)


# -------------------------------------------------------------------- path safety


class PathEscapeError(Exception):
    pass


class EmptyTargetError(PathEscapeError):
    """No target was named at all. Still a PathEscapeError for every
    existing caller (nothing outside the root is ever reachable), but
    distinguishable, so an empty target is never reported as if a real
    resource had been found malformed."""


# Distinct, truthful target failure kinds. A target defect (nothing named,
# nothing there, a directory, outside the root) is a different fact from a
# resource that was found and turned out to be damaged.
TARGET_NOT_SPECIFIED = "TARGET_NOT_SPECIFIED"
TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
TARGET_IS_DIRECTORY = "TARGET_IS_DIRECTORY"
TARGET_OUTSIDE_ROOT = "TARGET_OUTSIDE_ROOT"

TARGET_RESOLVED = "resolved"

# Actions that act on ONE named item and therefore need a target the host can
# resolve to a real file. LIST and APPEND name a directory / carry content.
TARGET_BEARING_ACTIONS = {
    LIBRARY: frozenset({INSPECT_METADATA, READ, VIEW_PAGE}),
    MUSIC: frozenset({INSPECT_METADATA, LISTEN, INSPECT_AUDIO, OBSERVE}),
    PHOTOGRAPHS: frozenset({INSPECT_METADATA, VIEW}),
    JOURNAL: frozenset({READ}),
    NOTES: frozenset({READ}),
}


def is_target_bearing(resource_class, action):
    return action in TARGET_BEARING_ACTIONS.get(resource_class, frozenset())


def normalize_relative_path(paths, resource_class, relative_path, *, want_directory=False):
    """A subject-supplied path is relative to its resource class. Models
    routinely restate the class ("library/x.pdf", or just "library" as a
    directory). When the literal path names nothing but the path without that
    redundant leading class segment does, use the latter. The literal path
    always wins if it exists, and the result is still resolved (and contained)
    by resolve_workspace_path() -- this never reaches outside the class root."""
    if not isinstance(relative_path, str):
        return relative_path
    root_dir = paths.dir_for(resource_class)
    if root_dir is None:
        return relative_path
    cleaned = relative_path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned in ("", "."):
        return ""
    segments = cleaned.split("/")
    if segments[0].casefold() != resource_class or os.path.exists(os.path.join(root_dir, cleaned)):
        return cleaned
    stripped = "/".join(segments[1:])
    if stripped == "":
        return ""
    return stripped if os.path.exists(os.path.join(root_dir, stripped)) else cleaned


def classify_target(paths, resource_class, relative_path):
    """(status, normalized_path). status is TARGET_RESOLVED when the target
    names an existing, contained regular file of this class (a journal target
    is an entry id); otherwise one distinct TARGET_* kind. Never touches
    anything except the class root; never reads content."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        return TARGET_NOT_SPECIFIED, ""
    normalized = normalize_relative_path(paths, resource_class, relative_path)
    if normalized == "":
        return TARGET_NOT_SPECIFIED, ""
    candidate = normalized
    if resource_class == JOURNAL:
        candidate = normalized if normalized.endswith(".json") else f"{normalized}.json"
    if resource_class == NOTES and not normalized.lower().endswith(".md"):
        candidate = f"{normalized}.md"      # a note is named by its title; the .md is the vault's format
    try:
        real_path = resolve_workspace_path(paths, resource_class, candidate)
    except EmptyTargetError:
        return TARGET_NOT_SPECIFIED, ""
    except PathEscapeError:
        return TARGET_OUTSIDE_ROOT, normalized
    if os.path.isdir(real_path):
        return TARGET_IS_DIRECTORY, normalized
    if not os.path.isfile(real_path):
        return TARGET_NOT_FOUND, normalized
    return TARGET_RESOLVED, normalized


def resolve_workspace_path(paths: WorkspacePaths, resource_class, relative_path):
    """Mechanical path safety (spec section 5). `relative_path` must be
    a caller-supplied relative path (never a display name treated as
    authority-bearing, never an absolute path, never containing '..').
    Resolves the real canonical absolute path on both the resource
    root and the candidate, and requires the candidate to be the root
    itself or nested under it -- catches '..' traversal, absolute-path
    override, and (where the OS/filesystem actually resolves it)
    symlink/junction escape, all via the same realpath-comparison
    mechanism clark_journal.py's own write_journal_entry() already
    uses and this module deliberately mirrors.

    Raises PathEscapeError (never returns a path outside the root) if
    resource_class is unknown or the resolved candidate escapes."""
    root_dir = paths.dir_for(resource_class)
    if root_dir is None:
        raise PathEscapeError(f"Unknown resource class: {resource_class!r}")

    if relative_path is None or relative_path == "":
        raise EmptyTargetError("Empty path.")
    if os.path.isabs(relative_path):
        raise PathEscapeError(f"Absolute paths are not accepted: {relative_path!r}")
    if ".." in relative_path.replace("\\", "/").split("/"):
        raise PathEscapeError(f"Path traversal ('..') rejected: {relative_path!r}")

    root_real = os.path.realpath(root_dir)
    candidate = os.path.join(root_dir, relative_path)
    candidate_real = os.path.realpath(candidate)

    if not (candidate_real == root_real or candidate_real.startswith(root_real + os.sep)):
        raise PathEscapeError(f"Path escapes the {resource_class} workspace root: {relative_path!r}")

    return candidate_real


# ---------------------------------------------------------------- action logging


def _log_action(paths, resource_class, action, relative_path, requester_actor_id, result, boundary_id, detail=None):
    """Append-only JSONL, mirroring signal_observation_log.py's own
    convention. Not canonical provenance (see module docstring) --
    durable, honest, host-side evidence only. Never modifies or
    truncates the log; opened in 'a' mode exclusively."""
    record = {
        "action_id": f"wsaction-{uuid.uuid4().hex[:16]}",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "requester_actor_id": requester_actor_id,
        "resource_class": resource_class,
        "action": action,
        "relative_path": relative_path,
        "result": result,  # "performed" | "denied"
        "boundary_id": boundary_id,
        "detail": detail,
    }
    os.makedirs(os.path.dirname(paths.action_log_path), exist_ok=True)
    with open(paths.action_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record


def query_action_log(paths):
    """Read-only. Never modifies the log."""
    if not os.path.exists(paths.action_log_path):
        return []
    records = []
    with open(paths.action_log_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records


# ------------------------------------------------------------------------- list


def list_resource_classes():
    """Returns exactly the configured resource classes -- nothing
    else is ever representable (spec section 19-A)."""
    return list(RESOURCE_CLASSES)


# WSP2-P2: deterministic bounds on ordinary list results. An unbounded
# `sorted(os.listdir(...))` used to flow verbatim into
# last_workspace_observation -> json.dumps(...) -> the next roaming-
# choice prompt, with no entry-count or aggregate-character ceiling.
# The current production library holds one file, so no live failure
# has occurred yet, but the workspace is meant to grow. These bounds
# apply uniformly to every ordinary WSP1/WSP2 list result (library,
# music, photographs, journal) since all four share this same code --
# never special-cased per resource class. WSP3 private listing does
# NOT go through this code at all (workspace_private.py's own
# execute_private_action() calls os.listdir() directly, independently,
# per PATHWAYS NOT ORGANS) and is untouched by this gate.
MAX_LIST_ENTRIES = 100
MAX_LIST_AGGREGATE_CHARS = 6000
LIST_CURSOR_VERSION = 1
LIST_INVALID_CURSOR = "LIST_INVALID_CURSOR"
LIST_NOT_DIRECTORY = "LIST_NOT_DIRECTORY"


def _list_raw_entries(paths: WorkspacePaths, resource_class):
    """Unbounded, sorted, truthful directory listing -- the one shared
    source of truth for both list_contents() and list_journal_
    entries() to bound independently afterward. Bounding must happen
    AFTER any resource-specific filtering (journal's .json filter),
    never before -- otherwise total_count/truncated would describe the
    wrong set of items."""
    root_dir = paths.dir_for(resource_class)
    if not os.path.isdir(root_dir):
        return []
    return sorted(name for name in os.listdir(root_dir) if not name.startswith(".anaxi_"))


def _bound_entries(entries, start_index=0):
    """Deterministic host-side bound over an already-sorted, complete
    list of strings. Never randomly samples -- always the alphabetical
    prefix that fits. A single pathological long filename can never
    defeat MAX_LIST_AGGREGATE_CHARS: if even the next entry would push
    the running total over the bound, the result stops before it
    (never partially rewritten/truncated to force a fit -- no such
    convention exists for filenames, unlike bounded text content).

    Returns {"entries", "returned_count", "total_count", "truncated"}.
    `total_count` is mechanically the true length of `entries` as
    passed in -- never estimated. `truncated` is only ever True when
    strictly fewer entries were returned than actually exist; a
    genuinely empty or genuinely-fully-returned list is truncated=False."""
    total_count = len(entries)
    bounded = []
    aggregate_chars = 0
    for name in entries[start_index:]:
        if len(bounded) >= MAX_LIST_ENTRIES:
            break
        if aggregate_chars + len(name) > MAX_LIST_AGGREGATE_CHARS:
            break
        bounded.append(name)
        aggregate_chars += len(name)
    return {
        "entries": bounded,
        "returned_count": len(bounded),
        "total_count": total_count,
        "truncated": start_index > 0 or start_index + len(bounded) < total_count,
        "has_more": start_index + len(bounded) < total_count,
        "start_index": start_index,
    }


def _encode_list_cursor(resource_class, directory, query, next_index):
    payload = json.dumps({
        "v": LIST_CURSOR_VERSION,
        "resource_class": resource_class,
        "directory": directory,
        "query": query,
        "next_index": next_index,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_list_cursor(cursor, resource_class, directory, query):
    if cursor in (None, ""):
        return 0
    if not isinstance(cursor, str):
        raise ValueError(LIST_INVALID_CURSOR)
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise ValueError(LIST_INVALID_CURSOR) from exc
    expected = {
        "v": LIST_CURSOR_VERSION,
        "resource_class": resource_class,
        "directory": directory,
        "query": query,
    }
    if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError(LIST_INVALID_CURSOR)
    next_index = payload.get("next_index")
    if not isinstance(next_index, int) or isinstance(next_index, bool) or next_index < 0:
        raise ValueError(LIST_INVALID_CURSOR)
    return next_index


def _browse_entries(paths, resource_class, directory="", query=None):
    root = paths.dir_for(resource_class)
    if root is None:
        raise PathEscapeError(f"Unknown resource class: {resource_class!r}")
    if directory:
        current = resolve_workspace_path(paths, resource_class, directory)
    else:
        current = os.path.realpath(root)
    if not os.path.isdir(current):
        raise NotADirectoryError(LIST_NOT_DIRECTORY)

    root_real = os.path.realpath(root)
    if query:
        folded = query.casefold()
        entries = []
        for walk_root, dirnames, filenames in os.walk(current, followlinks=False):
            dirnames[:] = sorted(name for name in dirnames if not name.startswith(".anaxi_"))
            for name in sorted(filenames):
                if name.startswith(".anaxi_"):
                    continue
                real = os.path.realpath(os.path.join(walk_root, name))
                if not (real == root_real or real.startswith(root_real + os.sep)):
                    continue
                relative = os.path.relpath(real, root_real).replace(os.sep, "/")
                if folded in relative.casefold():
                    entries.append(relative)
        return sorted(entries, key=lambda value: (value.casefold(), value))

    entries = []
    for name in sorted(os.listdir(current), key=lambda value: (value.casefold(), value)):
        if name.startswith(".anaxi_"):
            continue
        real = os.path.realpath(os.path.join(current, name))
        if not (real == root_real or real.startswith(root_real + os.sep)):
            continue
        entries.append(name + "/" if os.path.isdir(real) else name)
    return entries


def list_contents(paths: WorkspacePaths, resource_class, requester_actor_id="clark", *,
                  directory="", cursor=None, query=None):
    """Bounded/truthful directory listing (spec WSP2-P2). Returns
    (result, failure) where a successful result is the bounded dict
    {"entries", "returned_count", "total_count", "truncated"} -- never
    a bare list -- so truncation is always mechanically visible to
    every caller, including the roaming observation pathway, with no
    separate/second truncation layer required anywhere downstream."""
    allowed, boundary_id, rationale = check_permission(resource_class, LIST)
    if not allowed:
        _log_action(paths, resource_class, LIST, None, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    if not isinstance(directory, str) or not isinstance(query, (str, type(None))):
        _log_action(paths, resource_class, LIST, directory, requester_actor_id, "denied", boundary_id, LIST_INVALID_CURSOR)
        return None, {"boundary_id": boundary_id, "rationale": LIST_INVALID_CURSOR}
    query = query.strip() if isinstance(query, str) else None
    query = query or None
    try:
        entries = _browse_entries(paths, resource_class, directory, query)
        start_index = _decode_list_cursor(cursor, resource_class, directory, query)
    except (PathEscapeError, NotADirectoryError, ValueError) as exc:
        failure_kind = str(exc) or LIST_INVALID_CURSOR
        _log_action(paths, resource_class, LIST, directory, requester_actor_id, "denied", boundary_id, failure_kind)
        return None, {"boundary_id": boundary_id, "rationale": failure_kind}
    if start_index > len(entries):
        _log_action(paths, resource_class, LIST, directory, requester_actor_id, "denied", boundary_id, LIST_INVALID_CURSOR)
        return None, {"boundary_id": boundary_id, "rationale": LIST_INVALID_CURSOR}
    bounded_result = _bound_entries(entries, start_index)
    next_index = start_index + bounded_result["returned_count"]
    bounded_result.update({
        "directory": directory,
        "query": query,
        "next_cursor": (
            _encode_list_cursor(resource_class, directory, query, next_index)
            if bounded_result["has_more"] else None
        ),
    })
    next_payload = {"cursor": bounded_result["next_cursor"]}
    if query is not None:
        next_payload["query"] = query
    bounded_result["next_request"] = (
        json.dumps(next_payload, separators=(",", ":"))
        if bounded_result["next_cursor"] is not None else None
    )
    if bounded_result["has_more"] and not bounded_result["entries"]:
        _log_action(paths, resource_class, LIST, directory, requester_actor_id, "denied", boundary_id, LIST_INVALID_CURSOR)
        return None, {"boundary_id": boundary_id, "rationale": LIST_INVALID_CURSOR}
    _log_action(paths, resource_class, LIST, None, requester_actor_id, "performed", boundary_id)
    return bounded_result, None


def inspect_metadata(paths: WorkspacePaths, resource_class, relative_path, requester_actor_id="clark"):
    allowed, boundary_id, rationale = check_permission(resource_class, INSPECT_METADATA)
    if not allowed:
        _log_action(paths, resource_class, INSPECT_METADATA, relative_path, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    try:
        real_path = resolve_workspace_path(paths, resource_class, relative_path)
    except PathEscapeError as exc:
        _log_action(paths, resource_class, INSPECT_METADATA, relative_path, requester_actor_id, "denied", boundary_id, str(exc))
        return None, {"boundary_id": boundary_id, "rationale": str(exc)}

    if not os.path.isfile(real_path):
        _log_action(paths, resource_class, INSPECT_METADATA, relative_path, requester_actor_id, "denied", boundary_id, "not found")
        return None, {"boundary_id": boundary_id, "rationale": "Resource not found."}

    stat = os.stat(real_path)
    metadata = {
        "name": os.path.basename(real_path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.timezone.utc).isoformat(),
        "extension": os.path.splitext(real_path)[1].lstrip("."),
    }
    _log_action(paths, resource_class, INSPECT_METADATA, relative_path, requester_actor_id, "performed", boundary_id)
    return metadata, None


# ---------------------------------------------------------------------- library

PDF_EXTENSION = ".pdf"

# Distinct bounded PDF-read failure kinds (spec sections 7/8) -- never a
# raw-byte UTF-8 fallback, never a claim the document is empty, never a
# claim Clark visually inspected anything, never OCR.
PDF_TEXT_UNAVAILABLE = "PDF_TEXT_UNAVAILABLE"
PDF_MALFORMED = "PDF_MALFORMED"
PDF_ENCRYPTED = "PDF_ENCRYPTED"
PDF_PAGE_OUT_OF_RANGE = "PDF_PAGE_OUT_OF_RANGE"
PDF_RENDER_UNAVAILABLE = "PDF_RENDER_UNAVAILABLE"
PDF_PAGE_MAX_LONG_SIDE = 1600
PDF_PAGE_RENDERER_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv-macos", "bin", "anaxi-pdf-page-renderer")
PDF_PAGE_RENDERER_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_page_renderer.swift")


def _is_pdf(real_path):
    return os.path.splitext(real_path)[1].lower() == PDF_EXTENSION


def _extract_pdf_bounded(real_path, offset, max_chars):
    """Local, offline PDF text extraction only (pypdf) -- no OCR, no
    network, no embedded-action/JavaScript/link execution (pypdf's
    extract_text() never does any of those). Reads the file in binary
    mode via pypdf itself; never falls back to a text-mode/UTF-8 read.

    Builds the logical text stream as page-1-text, "\\n\\n", page-2-text,
    ... (spec section 5) and stops accumulating pages as soon as enough
    text has been gathered to answer the [offset, offset+max_chars)
    window AND to know for certain whether more text remains beyond it
    -- never forces extraction of an entire large document when the
    bounded range can be established without doing so. Never silently
    drops an empty-text page from the sequence (it is still appended,
    preserving page ordering/separators).

    Returns (content, has_more, pages_examined, page_count, failure_kind).
    failure_kind is one of PDF_MALFORMED / PDF_ENCRYPTED /
    PDF_TEXT_UNAVAILABLE / None (success)."""
    try:
        reader = pypdf.PdfReader(real_path)
    except Exception:
        return None, False, 0, 0, PDF_MALFORMED

    if reader.is_encrypted:
        # Only ever attempts the empty/blank password -- some PDFs are
        # nominally "encrypted" with no real access restriction. Never
        # guesses or prompts for a real password (spec section 8).
        try:
            decrypt_result = reader.decrypt("")
        except Exception:
            decrypt_result = 0
        if not decrypt_result:
            return None, False, 0, 0, PDF_ENCRYPTED

    try:
        page_count = len(reader.pages)
    except Exception:
        return None, False, 0, 0, PDF_MALFORMED

    needed = offset + max_chars
    parts = []
    accumulated_len = 0
    pages_examined = 0
    broke_early = False
    try:
        for i in range(page_count):
            page_text = reader.pages[i].extract_text() or ""
            pages_examined += 1
            if parts:
                accumulated_len += 2  # the "\n\n" separator
            parts.append(page_text)
            accumulated_len += len(page_text)
            if accumulated_len > needed:
                broke_early = True
                break
    except Exception:
        return None, False, 0, 0, PDF_MALFORMED

    full_text_so_far = "\n\n".join(parts)

    if not broke_early and full_text_so_far.strip() == "":
        # All pages were examined (never stopped early) and none of
        # them yielded meaningful text -- scanned/image-only PDF, or a
        # PDF with no text layer. Fail distinctly rather than
        # returning an empty-but-"performed" read.
        return None, False, pages_examined, page_count, PDF_TEXT_UNAVAILABLE

    content = full_text_so_far[offset:offset + max_chars]
    has_more = broke_early
    return content, has_more, pages_examined, page_count, None


def read_library_bounded(paths: WorkspacePaths, relative_path, offset=0, max_chars=4000, requester_actor_id="clark"):
    """Bounded/paged text read (spec section 9) -- never dumps an
    entire book into one call. Returns (result, failure) where result
    is {"content", "next_offset", "has_more"} (plus, for a PDF source,
    the additive keys "document_type", "pages_examined", "page_count")
    or None on failure."""
    allowed, boundary_id, rationale = check_permission(LIBRARY, READ)
    if not allowed:
        _log_action(paths, LIBRARY, READ, relative_path, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    try:
        real_path = resolve_workspace_path(paths, LIBRARY, relative_path)
    except PathEscapeError as exc:
        _log_action(paths, LIBRARY, READ, relative_path, requester_actor_id, "denied", boundary_id, str(exc))
        return None, {"boundary_id": boundary_id, "rationale": str(exc)}

    if not os.path.isfile(real_path):
        _log_action(paths, LIBRARY, READ, relative_path, requester_actor_id, "denied", boundary_id, "not found")
        return None, {"boundary_id": boundary_id, "rationale": "Resource not found."}

    if _is_pdf(real_path):
        content, has_more, pages_examined, page_count, failure_kind = _extract_pdf_bounded(real_path, offset, max_chars)
        if failure_kind is not None:
            _log_action(paths, LIBRARY, READ, relative_path, requester_actor_id, "denied", boundary_id, failure_kind)
            if failure_kind == PDF_TEXT_UNAVAILABLE:
                return None, {
                    "boundary_id": boundary_id,
                    "rationale": (
                        f"{PDF_TEXT_UNAVAILABLE}; this {page_count}-page PDF is available through "
                        f"the library {VIEW_PAGE} action with content {{\"page\":1}} (choose any page 1-{page_count})."
                    ),
                    "available_action": VIEW_PAGE,
                    "page_count": page_count,
                }
            return None, {"boundary_id": boundary_id, "rationale": failure_kind}

        _log_action(paths, LIBRARY, READ, relative_path, requester_actor_id, "performed", boundary_id,
                     {"offset": offset, "chars_returned": len(content), "document_type": "pdf",
                      "pages_examined": pages_examined, "page_count": page_count})
        return {
            "content": content,
            "next_offset": offset + len(content),
            "has_more": has_more,
            "next_request": (
                json.dumps({"offset": offset + len(content), "max_chars": max_chars}, separators=(",", ":"))
                if has_more else None
            ),
            "document_type": "pdf",
            "pages_examined": pages_examined,
            "page_count": page_count,
        }, None

    with open(real_path, "r", encoding="utf-8", errors="replace") as f:
        # ``offset`` counts CHARACTERS (next_offset = offset + len(content)).
        # A text-mode ``f.seek(offset)`` is a byte/opaque-cookie position, so
        # for any file containing a multi-byte character (curly quotes, accents)
        # every continuation window overlapped or skipped text. Skip characters.
        remaining = offset
        while remaining > 0:
            skipped = f.read(min(remaining, 1 << 16))
            if not skipped:
                break
            remaining -= len(skipped)
        content = f.read(max_chars)
        has_more = f.read(1) != ""

    _log_action(paths, LIBRARY, READ, relative_path, requester_actor_id, "performed", boundary_id, {"offset": offset, "chars_returned": len(content)})
    return {
        "content": content,
        "next_offset": offset + len(content),
        "has_more": has_more,
        "next_request": (
            json.dumps({"offset": offset + len(content), "max_chars": max_chars}, separators=(",", ":"))
            if has_more else None
        ),
    }, None


def _render_pdf_page_with_pdfkit(real_path, page_index):
    """Render one page through macOS PDFKit, never through PDF actions.

    The production launcher precompiles the fixed Swift helper.  The source
    fallback exists for isolated development worktrees and uses bounded module
    caches under the OS temp directory.  No filename or PDF content is ever
    interpolated into a shell command.
    """
    output_fd, output_path = tempfile.mkstemp(prefix="anaxi-pdf-page-", suffix=".png")
    os.close(output_fd)
    try:
        if os.path.isfile(PDF_PAGE_RENDERER_BIN) and os.access(PDF_PAGE_RENDERER_BIN, os.X_OK):
            command = [PDF_PAGE_RENDERER_BIN, real_path, str(page_index), output_path, str(PDF_PAGE_MAX_LONG_SIDE)]
        elif os.path.isfile(PDF_PAGE_RENDERER_SOURCE) and os.path.exists("/usr/bin/xcrun"):
            command = [
                "/usr/bin/xcrun", "swift", PDF_PAGE_RENDERER_SOURCE,
                real_path, str(page_index), output_path, str(PDF_PAGE_MAX_LONG_SIDE),
            ]
        else:
            return None
        environment = dict(os.environ)
        cache_root = os.path.join(tempfile.gettempdir(), "anaxi-swift-module-cache")
        environment["CLANG_MODULE_CACHE_PATH"] = os.path.join(cache_root, "clang")
        environment["SWIFT_MODULECACHE_PATH"] = os.path.join(cache_root, "swift")
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, check=False, env=environment,
        )
        if completed.returncode != 0:
            return None
        with open(output_path, "rb") as handle:
            return handle.read()
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(output_path)
        except FileNotFoundError:
            pass


def pdf_page_count(paths: WorkspacePaths, relative_path):
    """Number of pages of an existing library PDF, or None when it cannot be established
    (not a PDF, missing, encrypted, malformed). Reads only the page tree -- no text
    extraction, no rendering, no logging, never raises."""
    try:
        real_path = resolve_workspace_path(paths, LIBRARY, relative_path)
        if not os.path.isfile(real_path) or not _is_pdf(real_path):
            return None
        reader = pypdf.PdfReader(real_path)
        if reader.is_encrypted and not reader.decrypt(""):
            return None
        return len(reader.pages)
    except Exception:
        return None


def render_library_pdf_page(paths: WorkspacePaths, relative_path, page_number,
                            requester_actor_id="clark"):
    """Return one bounded rendered PDF page as genuine pixels.

    This is the truthful delivery route for scanned/image-only PDFs.  The
    result identifies the source PDF and rendered representation separately;
    it is never labeled OCR or extracted original text.
    """
    allowed, boundary_id, rationale = check_permission(LIBRARY, VIEW_PAGE)
    if not allowed:
        return None, {"boundary_id": boundary_id, "rationale": rationale}
    try:
        real_path = resolve_workspace_path(paths, LIBRARY, relative_path)
    except PathEscapeError as exc:
        _log_action(paths, LIBRARY, VIEW_PAGE, relative_path, requester_actor_id, "denied", boundary_id, str(exc))
        return None, {"boundary_id": boundary_id, "rationale": str(exc)}
    if not os.path.isfile(real_path) or not _is_pdf(real_path):
        return None, {"boundary_id": boundary_id, "rationale": "Resource is not a PDF file."}
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        return None, {"boundary_id": boundary_id, "rationale": PDF_PAGE_OUT_OF_RANGE}
    try:
        reader = pypdf.PdfReader(real_path)
        if reader.is_encrypted and not reader.decrypt(""):
            return None, {"boundary_id": boundary_id, "rationale": PDF_ENCRYPTED}
        page_count = len(reader.pages)
    except Exception:
        return None, {"boundary_id": boundary_id, "rationale": PDF_MALFORMED}
    if page_number > page_count:
        return None, {"boundary_id": boundary_id, "rationale": PDF_PAGE_OUT_OF_RANGE}

    rendered = _render_pdf_page_with_pdfkit(real_path, page_number - 1)
    if not rendered:
        _log_action(paths, LIBRARY, VIEW_PAGE, relative_path, requester_actor_id, "denied", boundary_id, PDF_RENDER_UNAVAILABLE)
        return None, {"boundary_id": boundary_id, "rationale": PDF_RENDER_UNAVAILABLE}
    try:
        with PILImage.open(io.BytesIO(rendered)) as image:
            image.verify()
        with PILImage.open(io.BytesIO(rendered)) as image:
            width, height = image.size
    except Exception:
        return None, {"boundary_id": boundary_id, "rationale": PDF_RENDER_UNAVAILABLE}
    if max(width, height) > PDF_PAGE_MAX_LONG_SIDE or len(rendered) > MAX_IMAGE_BYTES:
        return None, {"boundary_id": boundary_id, "rationale": PDF_RENDER_UNAVAILABLE}

    source_digest = hashlib.sha256()
    with open(real_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            source_digest.update(chunk)
    source_sha256 = source_digest.hexdigest()
    representation_sha256 = hashlib.sha256(rendered).hexdigest()
    result = {
        "name": os.path.basename(real_path),
        "document_type": "pdf_page_image",
        "page_number": page_number,
        "page_count": page_count,
        "width": width,
        "height": height,
        "size_bytes": len(rendered),
        "source_sha256": source_sha256,
        "sha256": representation_sha256,
        "renderer": "macos_pdfkit_bounded_page_v1",
        "image_bytes": rendered,
        "note": "Rendered source page pixels; not OCR and not a claim of a text layer.",
    }
    if page_number < page_count:   # continuation: the same action with the next page
        result["next_request"] = json.dumps({"page": page_number + 1}, separators=(",", ":"))
    _log_action(paths, LIBRARY, VIEW_PAGE, relative_path, requester_actor_id, "performed", boundary_id, {
        "page_number": page_number,
        "page_count": page_count,
        "source_sha256": source_sha256,
        "representation_sha256": representation_sha256,
    })
    return result, None


def attempt_library_mutation(paths: WorkspacePaths, action, relative_path, requester_actor_id="clark"):
    """Any of write/rename/delete/move_outside_workspace/external_share
    against library -- always denied, always logged (spec section 19-C)."""
    allowed, boundary_id, rationale = check_permission(LIBRARY, action)
    _log_action(paths, LIBRARY, action, relative_path, requester_actor_id, "denied" if not allowed else "performed", boundary_id, rationale)
    if not allowed:
        return None, {"boundary_id": boundary_id, "rationale": rationale}
    return {"status": "performed"}, None  # unreachable given current descriptors; kept for symmetry/testability


# ------------------------------------------------------------------------ music


def access_music_file(paths: WorkspacePaths, relative_path, requester_actor_id="clark"):
    """'read' for music means: confirm the file is present and
    byte-readable, and make it available for a future audio-capable
    pathway. NEVER a claim that Clark heard anything (spec section 10 --
    load-bearing). audio_pathway_available is always False in this
    codebase today, reported truthfully, not fabricated."""
    allowed, boundary_id, rationale = check_permission(MUSIC, READ)
    if not allowed:
        _log_action(paths, MUSIC, READ, relative_path, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    try:
        real_path = resolve_workspace_path(paths, MUSIC, relative_path)
    except PathEscapeError as exc:
        _log_action(paths, MUSIC, READ, relative_path, requester_actor_id, "denied", boundary_id, str(exc))
        return None, {"boundary_id": boundary_id, "rationale": str(exc)}

    if not os.path.isfile(real_path):
        _log_action(paths, MUSIC, READ, relative_path, requester_actor_id, "denied", boundary_id, "not found")
        return None, {"boundary_id": boundary_id, "rationale": "Resource not found."}

    stat = os.stat(real_path)
    result = {
        "name": os.path.basename(real_path),
        "size_bytes": stat.st_size,
        "extension": os.path.splitext(real_path)[1].lstrip("."),
        "audio_pathway_available": MUSIC_AUDIO_PATHWAY_AVAILABLE,
        "note": "File is confirmed present and available; no audio-perception pathway currently exists to let Clark actually hear this.",
    }
    _log_action(paths, MUSIC, READ, relative_path, requester_actor_id, "performed", boundary_id)
    return result, None


def attempt_music_mutation(paths: WorkspacePaths, action, relative_path, requester_actor_id="clark"):
    allowed, boundary_id, rationale = check_permission(MUSIC, action)
    _log_action(paths, MUSIC, action, relative_path, requester_actor_id, "denied" if not allowed else "performed", boundary_id, rationale)
    if not allowed:
        return None, {"boundary_id": boundary_id, "rationale": rationale}
    return {"status": "performed"}, None


def _resolve_and_decode_music(paths, relative_path, start_seconds=None, end_seconds=None):
    """Shared path-containment + decode step for LISTEN and
    INSPECT_AUDIO -- mirrors deliver_photograph_bytes()'s own
    resolve-then-decode order exactly. Returns (decoded, extension,
    failure_kind) where failure_kind is None on success. "path existed"
    is never conflated with "audio was successfully decoded" (CAP2F
    section 10) -- decode_audio_bounded() itself re-checks existence
    and only returns success after scipy has actually parsed real PCM
    data."""
    # A target defect is a different fact from a damaged audio file: report
    # which one it was instead of calling every missing/empty target
    # "malformed audio".
    try:
        real_path = resolve_workspace_path(paths, MUSIC, relative_path)
    except EmptyTargetError:
        return None, None, TARGET_NOT_SPECIFIED
    except PathEscapeError:
        return None, None, TARGET_OUTSIDE_ROOT
    if os.path.isdir(real_path):
        return None, None, TARGET_IS_DIRECTORY
    if not os.path.exists(real_path):
        return None, None, TARGET_NOT_FOUND
    extension = os.path.splitext(real_path)[1].lstrip(".").lower()
    decoded, failure_kind = wa.decode_audio_bounded(
        real_path, start_seconds=start_seconds, end_seconds=end_seconds,
    )
    return decoded, extension, failure_kind


def access_music_listen(paths: WorkspacePaths, relative_path, requester_actor_id="clark"):
    """CAP2F: LISTEN -- resolves a contained public music resource,
    genuinely decodes it, and returns a bounded NEUTRAL orientation
    (source facts + available view/estimator names) -- never a
    semantic reading of the music (spec section 3). Returns (result,
    failure) where a successful result is workspace_audio's own
    AUDIO_SOURCE_V1 dict."""
    allowed, boundary_id, rationale = check_permission(MUSIC, LISTEN)
    if not allowed:
        _log_action(paths, MUSIC, LISTEN, relative_path, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    # LISTEN establishes genuine decode with a one-second bounded probe while
    # retaining truthful full-source duration/format metadata.
    decoded, extension, failure_kind = _resolve_and_decode_music(paths, relative_path, 0.0, 1.0)
    if failure_kind is not None:
        _log_action(paths, MUSIC, LISTEN, relative_path, requester_actor_id, "denied", boundary_id, failure_kind)
        return None, {"boundary_id": boundary_id, "rationale": failure_kind}

    result = wa.render_audio_source(os.path.basename(relative_path), extension, decoded)
    _log_action(paths, MUSIC, LISTEN, relative_path, requester_actor_id, "performed", boundary_id,
                {"sha256": decoded["sha256"], "duration_seconds": decoded["duration_seconds"]})
    return result, None


def access_music_inspect_audio(paths: WorkspacePaths, relative_path, content, requester_actor_id="clark"):
    """CAP2F: INSPECT_AUDIO -- Clark selects exactly ONE acoustic view
    (and optional bounded interval) via `content`'s own strict,
    machine-readable JSON payload (never free-prose parsing -- spec
    section 3). Re-decodes the source fresh every call (no cross-call
    PCM cache) -- deterministic and reproducible, and never conflates a
    stale earlier decode with this call's own request."""
    allowed, boundary_id, rationale = check_permission(MUSIC, INSPECT_AUDIO)
    if not allowed:
        _log_action(paths, MUSIC, INSPECT_AUDIO, relative_path, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    payload, payload_failure = wa.parse_inspect_audio_payload(content)
    if payload_failure is not None:
        _log_action(paths, MUSIC, INSPECT_AUDIO, relative_path, requester_actor_id, "denied", boundary_id, payload_failure)
        return None, {"boundary_id": boundary_id, "rationale": payload_failure}

    decoded, extension, failure_kind = _resolve_and_decode_music(
        paths, relative_path, payload["start_seconds"], payload["end_seconds"],
    )
    if failure_kind is not None:
        _log_action(paths, MUSIC, INSPECT_AUDIO, relative_path, requester_actor_id, "denied", boundary_id, failure_kind)
        return None, {"boundary_id": boundary_id, "rationale": failure_kind}

    if payload["view"] == wa.SEGMENT_OBSERVATION_VIEW:
        # V1: the interval observation view is implemented by the
        # observation module (workspace_audio.py's import purity is tested),
        # routed here before run_inspect_audio.
        result, run_failure = wao.run_segment_observation(
            decoded, payload["start_seconds"], payload["end_seconds"],
        )
    else:
        result, run_failure = wa.run_inspect_audio(decoded, payload["view"], payload["start_seconds"], payload["end_seconds"])
    if run_failure is not None:
        _log_action(paths, MUSIC, INSPECT_AUDIO, relative_path, requester_actor_id, "denied", boundary_id, run_failure)
        return None, {"boundary_id": boundary_id, "rationale": run_failure}

    _log_action(paths, MUSIC, INSPECT_AUDIO, relative_path, requester_actor_id, "performed", boundary_id,
                {"sha256": decoded["sha256"], "view": payload["view"], "interval_seconds": result["interval_seconds"]})
    return result, None


def access_music_observe(paths: WorkspacePaths, relative_path, requester_actor_id="clark"):
    """V1 audio observation: OBSERVE -- resolves a contained public music
    resource, genuinely decodes a probe for source facts/digest, then
    runs the whole-source acoustic observation (streaming profile +
    bounded temporal map + spectral/harmonic/pitch-class detail
    on the leading segments within the analysis budget). The result is a
    bounded, JSON-safe mechanical measurement -- never audio bytes, never
    a semantic reading, never a claim that Clark heard anything. Returns
    (result, failure) where a successful result is workspace_audio_
    observation's own AUDIO_OBSERVATION_V1 dict."""
    allowed, boundary_id, rationale = check_permission(MUSIC, OBSERVE)
    if not allowed:
        _log_action(paths, MUSIC, OBSERVE, relative_path, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    real_path = None
    try:
        real_path = resolve_workspace_path(paths, MUSIC, relative_path)
    except EmptyTargetError:
        _log_action(paths, MUSIC, OBSERVE, relative_path, requester_actor_id, "denied", boundary_id, TARGET_NOT_SPECIFIED)
        return None, {"boundary_id": boundary_id, "rationale": TARGET_NOT_SPECIFIED}
    except PathEscapeError:
        _log_action(paths, MUSIC, OBSERVE, relative_path, requester_actor_id, "denied", boundary_id, TARGET_OUTSIDE_ROOT)
        return None, {"boundary_id": boundary_id, "rationale": TARGET_OUTSIDE_ROOT}

    # The probe establishes genuine decode + the file's own sha256; the
    # whole-source streaming analysis then measures duration from the
    # samples its OWN reader genuinely returned, never the probe's window.
    decoded, extension, failure_kind = _resolve_and_decode_music(paths, relative_path)
    if failure_kind is not None:
        _log_action(paths, MUSIC, OBSERVE, relative_path, requester_actor_id, "denied", boundary_id, failure_kind)
        return None, {"boundary_id": boundary_id, "rationale": failure_kind}

    result, observation_failure = wao.observe_audio_source(
        real_path, os.path.basename(relative_path), extension, decoded,
    )
    if observation_failure is not None:
        _log_action(paths, MUSIC, OBSERVE, relative_path, requester_actor_id, "denied", boundary_id, observation_failure)
        return None, {"boundary_id": boundary_id, "rationale": observation_failure}

    _log_action(paths, MUSIC, OBSERVE, relative_path, requester_actor_id, "performed", boundary_id,
                {"sha256": decoded["sha256"], "duration_seconds": result["duration_seconds"],
                 "segments_planned": result["analysis"]["segments_planned"]})
    return result, None


# ------------------------------------------------------------------ photographs


def access_photograph_file(paths: WorkspacePaths, relative_path, requester_actor_id="clark"):
    """Same honesty boundary as access_music_file, for vision instead
    of audio (spec section 11 -- load-bearing)."""
    allowed, boundary_id, rationale = check_permission(PHOTOGRAPHS, READ)
    if not allowed:
        _log_action(paths, PHOTOGRAPHS, READ, relative_path, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    try:
        real_path = resolve_workspace_path(paths, PHOTOGRAPHS, relative_path)
    except PathEscapeError as exc:
        _log_action(paths, PHOTOGRAPHS, READ, relative_path, requester_actor_id, "denied", boundary_id, str(exc))
        return None, {"boundary_id": boundary_id, "rationale": str(exc)}

    if not os.path.isfile(real_path):
        _log_action(paths, PHOTOGRAPHS, READ, relative_path, requester_actor_id, "denied", boundary_id, "not found")
        return None, {"boundary_id": boundary_id, "rationale": "Resource not found."}

    stat = os.stat(real_path)
    result = {
        "name": os.path.basename(real_path),
        "size_bytes": stat.st_size,
        "extension": os.path.splitext(real_path)[1].lstrip("."),
        "vision_pathway_available": PHOTOGRAPHS_VISION_PATHWAY_AVAILABLE,
        "note": "File is confirmed present. Use VIEW for bounded genuine pixel delivery through the supervised vision pathway; this metadata-only READ action does not itself carry pixels.",
    }
    _log_action(paths, PHOTOGRAPHS, READ, relative_path, requester_actor_id, "performed", boundary_id)
    return result, None


def deliver_photograph_bytes(paths: WorkspacePaths, relative_path, requester_actor_id="clark"):
    """Deliver genuine pixels while preserving source/derivative identity.

    Sources already inside the per-call byte/dimension bounds are returned
    unchanged. Larger/oriented photographs and GIFs receive an in-memory,
    orientation-corrected, aspect-preserving static derivative; originals are
    untouched. `source_sha256` identifies the original and `sha256` identifies
    the exact bytes handed downstream. Callers must never log `image_bytes`.
    """
    # Lazy so non-image ordinary paths remain loadable in minimal/test-safe
    # runtimes that provide the Image interface but never invoke VIEW.
    from PIL import ImageOps

    allowed, boundary_id, rationale = check_permission(PHOTOGRAPHS, VIEW)
    if not allowed:
        _log_action(paths, PHOTOGRAPHS, VIEW, relative_path, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    try:
        real_path = resolve_workspace_path(paths, PHOTOGRAPHS, relative_path)
    except PathEscapeError as exc:
        _log_action(paths, PHOTOGRAPHS, VIEW, relative_path, requester_actor_id, "denied", boundary_id, str(exc))
        return None, {"boundary_id": boundary_id, "rationale": str(exc)}

    if not os.path.isfile(real_path):
        _log_action(paths, PHOTOGRAPHS, VIEW, relative_path, requester_actor_id, "denied", boundary_id, "not found")
        return None, {"boundary_id": boundary_id, "rationale": "Resource not found."}

    extension = os.path.splitext(real_path)[1].lstrip(".").lower()
    if extension not in SUPPORTED_IMAGE_EXTENSIONS:
        _log_action(paths, PHOTOGRAPHS, VIEW, relative_path, requester_actor_id, "denied", boundary_id, IMAGE_UNSUPPORTED_FORMAT)
        return None, {"boundary_id": boundary_id, "rationale": IMAGE_UNSUPPORTED_FORMAT}

    source_size_bytes = os.stat(real_path).st_size
    if source_size_bytes > MAX_SOURCE_IMAGE_BYTES:
        _log_action(paths, PHOTOGRAPHS, VIEW, relative_path, requester_actor_id, "denied", boundary_id, IMAGE_TOO_LARGE)
        return None, {"boundary_id": boundary_id, "rationale": IMAGE_TOO_LARGE}

    with open(real_path, "rb") as f:
        data = f.read()

    try:
        with PILImage.open(io.BytesIO(data)) as probe:
            source_width, source_height = probe.size
            if source_width * source_height > MAX_SOURCE_IMAGE_PIXELS:
                raise ValueError(IMAGE_DIMENSION_EXCEEDED)
            source_orientation = probe.getexif().get(274, 1)
            if getattr(probe, "n_frames", 1) > 1:
                probe.seek(0)
            corrected = ImageOps.exif_transpose(probe).copy()
            corrected.load()
    except ValueError as exc:
        failure_kind = str(exc) if str(exc) == IMAGE_DIMENSION_EXCEEDED else IMAGE_MALFORMED
        _log_action(paths, PHOTOGRAPHS, VIEW, relative_path, requester_actor_id, "denied", boundary_id, failure_kind)
        return None, {"boundary_id": boundary_id, "rationale": failure_kind}
    except Exception:
        _log_action(paths, PHOTOGRAPHS, VIEW, relative_path, requester_actor_id, "denied", boundary_id, IMAGE_MALFORMED)
        return None, {"boundary_id": boundary_id, "rationale": IMAGE_MALFORMED}

    width, height = corrected.size
    needs_derivative = (
        source_size_bytes > MAX_IMAGE_BYTES
        or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION
        or extension == "gif" or source_orientation not in (None, 1)
    )
    if needs_derivative:
        corrected.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), PILImage.LANCZOS)
        width, height = corrected.size
        buffer = io.BytesIO()
        if corrected.mode in ("RGBA", "LA"):
            corrected.save(buffer, format="PNG", optimize=True)
            representation_extension = "png"
        else:
            if corrected.mode != "RGB":
                corrected = corrected.convert("RGB")
            corrected.save(buffer, format="JPEG", quality=90, optimize=True)
            representation_extension = "jpg"
        representation = buffer.getvalue()
        if len(representation) > MAX_IMAGE_BYTES:
            # A second, lower-quality encoding preserves the per-call byte
            # bound without touching the original.  Current intended public
            # photographs fit comfortably after this bounded conversion.
            buffer = io.BytesIO()
            if corrected.mode != "RGB":
                background = PILImage.new("RGB", corrected.size, "white")
                if "A" in corrected.getbands():
                    background.paste(corrected, mask=corrected.getchannel("A"))
                corrected = background
            corrected.save(buffer, format="JPEG", quality=75, optimize=True)
            representation = buffer.getvalue()
            representation_extension = "jpg"
        if len(representation) > MAX_IMAGE_BYTES:
            _log_action(paths, PHOTOGRAPHS, VIEW, relative_path, requester_actor_id,
                        "denied", boundary_id, IMAGE_TOO_LARGE)
            return None, {"boundary_id": boundary_id, "rationale": IMAGE_TOO_LARGE}
    else:
        representation = data
        representation_extension = extension

    source_digest = hashlib.sha256(data).hexdigest()
    digest = hashlib.sha256(representation).hexdigest()
    _log_action(paths, PHOTOGRAPHS, VIEW, relative_path, requester_actor_id, "performed", boundary_id,
                {"source_size_bytes": source_size_bytes, "size_bytes": len(representation),
                 "width": width, "height": height, "source_sha256": source_digest,
                 "sha256": digest, "derivative": needs_derivative})
    result = {
        "name": os.path.basename(real_path),
        "extension": representation_extension,
        "source_extension": extension,
        "size_bytes": len(representation),
        "source_size_bytes": source_size_bytes,
        "source_width": source_width,
        "source_height": source_height,
        "width": width,
        "height": height,
        "sha256": digest,
        "source_sha256": source_digest,
        "derivative": needs_derivative,
        "derivative_policy": "bounded_orientation_corrected_static_frame_v1" if needs_derivative else "source_bytes_within_bounds",
        "image_bytes": representation,
    }
    return result, None


# Default bound for derive_bounded_vision_representation() -- see that
# function's own docstring for why 1024 (CAP2E-P1: this is the
# measured plateau boundary for qwen3-vl:4b's own image-token cost,
# not an arbitrary round number).
DEFAULT_VISION_REPRESENTATION_MAX_LONG_SIDE = 1024


def derive_bounded_vision_representation(image_bytes, max_long_side=DEFAULT_VISION_REPRESENTATION_MAX_LONG_SIDE):
    """CAP2E-P1: given already-validated, already-bounded ORIGINAL
    photograph bytes (see deliver_photograph_bytes()'s own contract --
    this function never independently re-validates format/size/
    dimension bounds; that already happened), returns an aspect-
    preserving, NEVER-UPSCALING bounded representation suitable for
    delivery to a downstream vision-capable model whose own real
    image-token cost scales with resolution above a working plateau
    smaller than this module's own MAX_IMAGE_DIMENSION permits.

    Deliberately generic (no model name here) -- this is a mechanical
    image-bounding utility, not a model-routing decision; that
    decision belongs entirely to the caller (workspace_supervisor.py).

    NEVER upscales: an image already <= max_long_side on BOTH axes is
    returned byte-identical and unresized, so a small photograph's own
    real pixels are never resampled/blurred for no reason -- the
    returned `source_and_representation_identical` flag makes this
    mechanically observable rather than merely inferred by comparing
    bytes. Never writes anything to disk and never mutates the
    original resource in any way -- this function only ever reads the
    bytes it is given and returns new, purely in-memory bytes; the
    original public photograph on disk is completely untouched by this
    or any other function in this module.

    Returns {"representation_bytes", "width", "height", "sha256",
    "source_and_representation_identical"}. Raises no new exception
    class -- if `image_bytes` is somehow not a decodable image (should
    be unreachable in practice, since the caller must already have
    validated it via deliver_photograph_bytes()), the underlying PIL
    exception propagates uncaught, exactly like any other genuinely
    unexpected mechanical failure in this module."""
    with PILImage.open(io.BytesIO(image_bytes)) as probe:
        width, height = probe.size
        if width <= max_long_side and height <= max_long_side:
            return {
                "representation_bytes": image_bytes,
                "width": width,
                "height": height,
                "sha256": hashlib.sha256(image_bytes).hexdigest(),
                "source_and_representation_identical": True,
            }
        scale = max_long_side / float(max(width, height))
        new_width = max(1, round(width * scale))
        new_height = max(1, round(height * scale))
        rgb_image = probe.convert("RGB") if probe.mode != "RGB" else probe.copy()
        resized = rgb_image.resize((new_width, new_height), PILImage.LANCZOS)
    buf = io.BytesIO()
    resized.save(buf, format="PNG")
    representation_bytes = buf.getvalue()
    return {
        "representation_bytes": representation_bytes,
        "width": new_width,
        "height": new_height,
        "sha256": hashlib.sha256(representation_bytes).hexdigest(),
        "source_and_representation_identical": False,
    }


def attempt_photograph_mutation_or_share(paths: WorkspacePaths, action, relative_path, requester_actor_id="clark"):
    allowed, boundary_id, rationale = check_permission(PHOTOGRAPHS, action)
    _log_action(paths, PHOTOGRAPHS, action, relative_path, requester_actor_id, "denied" if not allowed else "performed", boundary_id, rationale)
    if not allowed:
        return None, {"boundary_id": boundary_id, "rationale": rationale}
    return {"status": "performed"}, None


# --------------------------------------------------------------------- journal


JOURNAL_OPENING_CHARS = 120
JOURNAL_THREAD_MAX_MEMBERS = 20
JOURNAL_THREAD_MEMBER_CHARS = 600
JOURNAL_REFERENCE_NOT_FOUND = "JOURNAL_REFERENCE_NOT_FOUND"
JOURNAL_REFERENCE_AMBIGUOUS = "JOURNAL_REFERENCE_AMBIGUOUS"


def _load_journal_entry(paths, filename):
    try:
        with open(os.path.join(paths.journal_dir, filename), "r", encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    return entry if isinstance(entry, dict) else None


def _all_journal_entries(paths):
    entries = []
    for name in _list_raw_entries(paths, JOURNAL):
        if name.endswith(".json"):
            entry = _load_journal_entry(paths, name)
            if entry is not None and isinstance(entry.get("entry_id"), str):
                entries.append(entry)
    return entries


def _journal_thread_id(entry):
    return entry.get("thread_root_id") or entry.get("entry_id")


def _journal_entry_summary(entry):
    """Verbatim opening of the entry's own stored text plus host-recorded facts.
    Nothing here is authored by the host as prose."""
    text = " ".join(str(entry.get("content", "")).split())
    summary = {
        "created_at": entry.get("created_at"),
        "opening": text[:JOURNAL_OPENING_CHARS] + ("..." if len(text) > JOURNAL_OPENING_CHARS else ""),
    }
    if entry.get("continues_entry_id"):
        summary["continues"] = entry["continues_entry_id"]
        summary["thread"] = _journal_thread_id(entry)
    return summary


def _journal_thread_members(paths, thread_id):
    members = [e for e in _all_journal_entries(paths) if _journal_thread_id(e) == thread_id]
    return sorted(members, key=lambda e: (str(e.get("created_at", "")), e["entry_id"]))


def _journal_norm(text):
    return " ".join(str(text).split()).casefold()


def clean_journal_reference(reference):
    """The words a subject used to name an entry, without the ellipsis/quotes/markdown a
    truncated host pointer or a quotation leaves on them."""
    ref = " ".join(str(reference or "").split())
    ref = ref.strip("\"'`*- ")
    for tail in ("...", "\u2026"):
        if ref.endswith(tail):
            ref = ref[: -len(tail)]
    return ref.strip("\"'`*- ")


def resolve_journal_reference(paths, reference, prefer="latest"):
    """Mechanically resolve how a subject named an earlier journal entry: an exact entry id,
    or words from the entry's own text. Returns (entry_id, None) or (None, failure) where a
    failure is NOT_FOUND or AMBIGUOUS (with the candidates). Several entries of ONE thread
    resolve to that thread's latest entry; entries of different threads are never guessed
    between."""
    ref = clean_journal_reference(reference)
    if ref.endswith(".json"):
        ref = ref[:-5]
    if not ref:
        return None, {"code": JOURNAL_REFERENCE_NOT_FOUND, "candidates": []}
    entries = _all_journal_entries(paths)
    for entry in entries:
        if entry["entry_id"] == ref:
            return ref, None
    needle = _journal_norm(ref)
    matches = [e for e in entries if needle in _journal_norm(e.get("content", ""))]
    if not matches:
        return None, {"code": JOURNAL_REFERENCE_NOT_FOUND, "candidates": []}
    threads = {_journal_thread_id(e) for e in matches}
    if len(threads) > 1:
        return None, {"code": JOURNAL_REFERENCE_AMBIGUOUS, "candidates": [
            dict(entry_id=e["entry_id"], **_journal_entry_summary(e)) for e in matches[:10]
        ]}
    members = _journal_thread_members(paths, threads.pop())
    return (members[0] if prefer == "root" else members[-1])["entry_id"], None


def _attach_journal_thread(paths, entry):
    """A read of an entry that belongs to a thread also delivers the thread: every
    contribution in order, each with its author and time, its own stored text bounded per
    member (``content_truncated`` says so; read that entry for the rest)."""
    members = _journal_thread_members(paths, _journal_thread_id(entry))
    if len(members) < 2:
        return entry
    shown = []
    for member in members[:JOURNAL_THREAD_MAX_MEMBERS]:
        text = str(member.get("content", ""))
        item = {
            "entry_id": member["entry_id"], "created_at": member.get("created_at"),
            "author_actor_id": member.get("author_actor_id"),
            "content": text[:JOURNAL_THREAD_MEMBER_CHARS],
        }
        if len(text) > JOURNAL_THREAD_MEMBER_CHARS:
            item["content_truncated"] = True
            item["total_chars"] = len(text)
        shown.append(item)
    entry = dict(entry, thread_root_id=_journal_thread_id(entry), thread=shown)
    if len(members) > len(shown):
        entry["thread_more_entries"] = len(members) - len(shown)
    return entry


def journal_thread_index(paths, limit=3, chars=40):
    """Read-only pointer (no action-log record): the verbatim openings of the ``limit`` journal
    threads with the most recent activity, newest first. A pointer to what exists, not content."""
    latest = {}
    root_entries = {}
    for entry in _all_journal_entries(paths):
        thread = _journal_thread_id(entry)
        stamp = str(entry.get("created_at", ""))
        if stamp >= latest.get(thread, ""):
            latest[thread] = stamp
        if entry["entry_id"] == thread:
            root_entries[thread] = entry
    ordered = sorted((t for t in latest if t in root_entries), key=lambda t: latest[t], reverse=True)
    openings = []
    for thread in ordered[:limit]:
        text = " ".join(str(root_entries[thread].get("content", "")).split()).strip("-* ")
        openings.append(text[:chars] + ("..." if len(text) > chars else ""))
    return openings


def list_journal_entries(paths: WorkspacePaths, requester_actor_id="clark", *, cursor=None, query=None):
    """Bounded (spec WSP2-P2), like list_contents() -- deliberately
    does NOT call list_contents() itself, because bounding must apply
    AFTER the .json filter below, not before: bounding the raw
    directory listing first (which could in principle contain non-
    .json files) would make total_count/truncated describe the wrong
    set. Permission check/logging mirror list_contents()'s own exactly
    (same boundary_id, same one "list" log entry) for behavioral
    parity with the pre-WSP2-P2 implementation, which delegated both
    to list_contents()."""
    allowed, boundary_id, rationale = check_permission(JOURNAL, LIST)
    if not allowed:
        _log_action(paths, JOURNAL, LIST, None, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    query = query.strip() if isinstance(query, str) else query
    if query is not None and not isinstance(query, str):
        return None, {"boundary_id": boundary_id, "rationale": LIST_INVALID_CURSOR}
    json_entries = [e for e in _list_raw_entries(paths, JOURNAL) if e.endswith(".json")]
    summaries = {}
    if query:
        # A journal entry has no filename a subject could know, so a query matches the
        # entry's own stored text as well as its id (plain case-insensitive substring;
        # never semantic, never a guess).
        matched = []
        for entry in json_entries:
            stored = _load_journal_entry(paths, entry)
            if query.casefold() in entry.casefold() or (
                stored is not None and _journal_norm(query) in _journal_norm(stored.get("content", ""))
            ):
                matched.append(entry)
        json_entries = matched
    try:
        start_index = _decode_list_cursor(cursor, JOURNAL, "", query or None)
    except ValueError:
        return None, {"boundary_id": boundary_id, "rationale": LIST_INVALID_CURSOR}
    if start_index > len(json_entries):
        return None, {"boundary_id": boundary_id, "rationale": LIST_INVALID_CURSOR}
    bounded_result = _bound_entries(json_entries, start_index)
    next_index = start_index + bounded_result["returned_count"]
    bounded_result.update({
        "directory": "",
        "query": query or None,
        "next_cursor": (
            _encode_list_cursor(JOURNAL, "", query or None, next_index)
            if bounded_result["has_more"] else None
        ),
    })
    next_payload = {"cursor": bounded_result["next_cursor"]}
    if query:
        next_payload["query"] = query
    bounded_result["next_request"] = (
        json.dumps(next_payload, separators=(",", ":"))
        if bounded_result["next_cursor"] is not None else None
    )
    # Entries are opaque ids; the subject can only choose one it recognizes. Show each
    # listed entry's own stored opening, verbatim, with host facts (when, thread).
    for name in bounded_result["entries"]:
        stored = _load_journal_entry(paths, name)
        if stored is not None:
            summaries[name] = _journal_entry_summary(stored)
    if summaries:
        bounded_result["entry_summaries"] = summaries
    _log_action(paths, JOURNAL, LIST, None, requester_actor_id, "performed", boundary_id)
    return bounded_result, None


def read_journal_entry(paths: WorkspacePaths, entry_id, requester_actor_id="clark", *, offset=0, max_chars=None):
    """Read one journal entry. ``max_chars=None`` returns the whole entry
    (the historical behavior). Given ``max_chars`` the entry's ``content``
    is delivered as a window ``[offset, offset+max_chars)`` with a
    ``content_window`` continuation block, so a long entry can be read
    progressively rather than only in one piece."""
    allowed, boundary_id, rationale = check_permission(JOURNAL, READ)
    filename = f"{entry_id}.json"
    if not allowed:
        _log_action(paths, JOURNAL, READ, filename, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    try:
        real_path = resolve_workspace_path(paths, JOURNAL, filename)
    except PathEscapeError as exc:
        _log_action(paths, JOURNAL, READ, filename, requester_actor_id, "denied", boundary_id, str(exc))
        return None, {"boundary_id": boundary_id, "rationale": str(exc)}

    if not os.path.isfile(real_path):
        _log_action(paths, JOURNAL, READ, filename, requester_actor_id, "denied", boundary_id, "not found")
        return None, {"boundary_id": boundary_id, "rationale": "Entry not found."}

    with open(real_path, "r", encoding="utf-8") as f:
        entry = json.load(f)
    full_entry = entry
    if max_chars is not None and isinstance(entry.get("content"), str):
        total = len(entry["content"])
        if offset > total:
            _log_action(paths, JOURNAL, READ, filename, requester_actor_id, "denied", boundary_id, "offset beyond entry")
            return None, {"boundary_id": boundary_id, "rationale": "Offset is beyond the end of this journal entry."}
        window = entry["content"][offset:offset + max_chars]
        end = offset + len(window)
        entry = dict(entry, content=window)
        if offset > 0 or end < total:
            entry["content_window"] = {
                "offset": offset, "chars": len(window), "total_chars": total,
                "has_more": end < total,
                "next_request": (
                    json.dumps({"offset": end, "max_chars": max_chars}, separators=(",", ":"))
                    if end < total else None
                ),
            }
    entry = _attach_journal_thread(paths, entry) if isinstance(full_entry, dict) and full_entry.get("entry_id") else entry
    _log_action(paths, JOURNAL, READ, filename, requester_actor_id, "performed", boundary_id)
    return entry, None


def append_journal_entry(paths: WorkspacePaths, clark_actor_id, content, session_id=None, source_event_id=None, requester_actor_id="clark", continues=None):
    """The only journal write operation. A direct/supervised call creates
    a new host-generated unique filename. An ordinary-waking call binds
    its append identity to the canonical source H so an exact recovery
    replay returns the already-created entry rather than duplicating the
    side effect. No path overwrites an existing entry. Clark-authored text here is
    evidence Clark wrote it at this time -- never automatically
    canonical belief, never automatically long-term memory (spec
    section 12) -- this function does not touch hippocampal storage,
    Kardia, or anaxi_provenance.db in any way.

    ``continues`` (optional) names an EARLIER entry -- by id or by words from it -- that this
    new entry continues. The earlier entry is never touched: the continuation is its own
    append-only entry carrying host-recorded ``continues_entry_id``/``thread_root_id``
    links, so one logical thread accumulates entries with each entry's own author, time and
    text. Nothing is written when the named entry does not resolve to exactly one thread."""
    allowed, boundary_id, rationale = check_permission(JOURNAL, APPEND)
    if not allowed:
        _log_action(paths, JOURNAL, APPEND, None, requester_actor_id, "denied", boundary_id, rationale)
        return None, {"boundary_id": boundary_id, "rationale": rationale}

    if not isinstance(content, str) or not content.strip():
        _log_action(paths, JOURNAL, APPEND, None, requester_actor_id, "denied", boundary_id, "empty content")
        return None, {"boundary_id": boundary_id, "rationale": "Journal entry content must be a non-empty string."}

    # An authenticated ordinary-waking occurrence supplies its canonical
    # H event id here. Derive a stable append identity from that event so
    # a failure after the write but before canonical X cannot append the
    # same Clark-authored action twice on a lawful continuation. The
    # legacy/direct-supervised path keeps its fresh-UUID behavior.
    if source_event_id is not None and (not isinstance(source_event_id, str) or not source_event_id):
        _log_action(paths, JOURNAL, APPEND, None, requester_actor_id, "denied", boundary_id, "invalid source event id")
        return None, {"boundary_id": boundary_id, "rationale": "source_event_id must be nonempty text when supplied."}
    entry_id = (
        "entry-" + hashlib.sha256(("journal_append\x00" + source_event_id).encode("utf-8")).hexdigest()[:16]
        if source_event_id is not None else f"entry-{uuid.uuid4().hex[:16]}"
    )
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    entry = {
        "entry_id": entry_id,
        "created_at": created_at,
        "author_actor_id": clark_actor_id,
        "content": content,
    }
    if isinstance(continues, str) and continues.strip():
        target_id, reference_failure = resolve_journal_reference(paths, continues)
        if reference_failure is not None:
            detail = reference_failure["code"]
            _log_action(paths, JOURNAL, APPEND, None, requester_actor_id, "denied", boundary_id, detail)
            rationale_text = f"{detail}: nothing was written; no earlier journal entry was continued."
            if reference_failure["candidates"]:
                rationale_text += " Entries that match: " + json.dumps(reference_failure["candidates"]) + \
                    " -- name one by its entry_id (or leave relative_path empty for a new entry)."
            return None, {"boundary_id": boundary_id, "rationale": rationale_text}
        target = _load_journal_entry(paths, f"{target_id}.json")
        entry["continues_entry_id"] = target_id
        entry["thread_root_id"] = _journal_thread_id(target)
    if session_id is not None:
        entry["session_id"] = session_id
    if source_event_id is not None:
        entry["source_event_id"] = source_event_id

    os.makedirs(paths.journal_dir, exist_ok=True)
    real_path = os.path.join(paths.journal_dir, f"{entry_id}.json")
    try:
        # Exclusive create makes the stable-id path race-safe: two
        # processes replaying one H can never both perform the append.
        with open(real_path, "x", encoding="utf-8") as f:
            json.dump(entry, f, indent=2)
    except FileExistsError:
        if source_event_id is None:
            _log_action(paths, JOURNAL, APPEND, None, requester_actor_id, "denied", boundary_id, "entry_id collision")
            return None, {"boundary_id": boundary_id, "rationale": "Internal error: entry_id collision."}
        try:
            with open(real_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, ValueError, TypeError):
            _log_action(paths, JOURNAL, APPEND, None, requester_actor_id, "denied", boundary_id, "unreadable idempotency collision")
            return None, {"boundary_id": boundary_id, "rationale": "Existing journal occurrence is unreadable; append refused."}
        if (
            existing.get("source_event_id") == source_event_id
            and existing.get("author_actor_id") == clark_actor_id
            and existing.get("content") == content
            and existing.get("continues_entry_id") == entry.get("continues_entry_id")
        ):
            # Exact replay: return the already-performed result without a
            # second file or a second action-log occurrence.
            return existing, None
        _log_action(paths, JOURNAL, APPEND, None, requester_actor_id, "denied", boundary_id, "conflicting idempotent replay")
        return None, {
            "boundary_id": boundary_id,
            "rationale": "This source occurrence is already bound to a different journal append.",
        }

    _log_action(paths, JOURNAL, APPEND, f"{entry_id}.json", requester_actor_id, "performed", boundary_id)
    return entry, None


def attempt_journal_mutation(paths: WorkspacePaths, action, relative_path, requester_actor_id="clark"):
    """write (arbitrary overwrite)/rename/delete/move/share against an
    existing journal entry -- always denied, always logged."""
    allowed, boundary_id, rationale = check_permission(JOURNAL, action)
    _log_action(paths, JOURNAL, action, relative_path, requester_actor_id, "denied" if not allowed else "performed", boundary_id, rationale)
    if not allowed:
        return None, {"boundary_id": boundary_id, "rationale": rationale}
    return {"status": "performed"}, None
