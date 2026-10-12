"""CAP2-D: non-destructive public organizational overlay.

Pathways, not organs: this module never touches library/music/
photographs/journal source files. A container (collection/shelf/
playlist/album -- one generic shape, labeled by `container_type`) holds
only REFERENCES -- (resource_class, relative_path) pointers -- to
resources that already exist under one of workspace_capability.py's
four resource roots. Deleting a container deletes exactly one JSON file
of its own, in its own `organization/` directory, and nothing else;
removing a reference edits that same JSON file's own reference list and
never touches the pointed-to resource.

Every reference is validated at add-time against workspace_capability's
own authoritative permission/path-containment machinery (check_permission
+ resolve_workspace_path + a real existence check) -- a container can
never hold a reference to something that doesn't exist, escapes the
workspace, or belongs to an unrecognized resource class. Host validator
remains authoritative throughout: no normalization of malformed input,
no semantic retry, fail closed on any invalid request.

Deliberately NOT wired into the live Pass-1/Pass-2 typed-action schema
this gate (workspace_direction.STAGE2_ACTION_SCHEMA / LIVE_ALLOWED_
SURFACE / execute_workspace_action) -- mirrors workspace_capability.py's
OWN original WSP1-S1 shape exactly (a pure, directly-callable, host-
operated capability surface, with the conversational integration seam
left for a following gate; see that module's own docstring for the
precedent). This keeps CAP2 bounded: the typed-action wiring touches
the Pass-1 JSON schema, the live allowed-surface table, budget
composition, and their existing test suites across three other files,
which is real, separable follow-on work, not required to deliver
genuine, tested, host-validated organization operations now.
"""
import datetime
import json
import os
import uuid

import workspace_capability as wc

CONTAINER_TYPES = frozenset({"collection", "shelf", "playlist", "album"})
MAX_NAME_LENGTH = 200
MAX_REFERENCES_PER_CONTAINER = 500

LIST = "list"
CREATE = "create"
INSPECT = "inspect"
RENAME = "rename"
ADD_REFERENCE = "add_reference"
REMOVE_REFERENCE = "remove_reference"
DELETE_CONTAINER = "delete_container"


class OrganizationError(Exception):
    """Raised only for a genuine host-side bug (e.g. a container_id
    collision on creation) -- never for an ordinary validation failure,
    which always returns (None, failure) instead."""


def _org_dir(paths: wc.WorkspacePaths):
    return os.path.join(paths.root, "organization")


def _org_log_path(paths: wc.WorkspacePaths):
    return os.path.join(paths.root, "organization_action_log.jsonl")


def _log_action(paths, action, container_id, requester_actor_id, result, detail=None):
    """Append-only JSONL, mirroring workspace_capability._log_action's
    own established convention exactly, in a dedicated log file (never
    workspace_action_log.jsonl -- organization is not one of
    workspace_capability.RESOURCE_CLASSES)."""
    record = {
        "action_id": f"orgaction-{uuid.uuid4().hex[:16]}",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "requester_actor_id": requester_actor_id,
        "action": action,
        "container_id": container_id,
        "result": result,  # "performed" | "denied"
        "detail": detail,
    }
    log_path = _org_log_path(paths)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record


def query_action_log(paths):
    """Read-only. Never modifies the log."""
    log_path = _org_log_path(paths)
    if not os.path.exists(log_path):
        return []
    records = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records


def _container_path(paths, container_id):
    """Container IDs are always host-minted uuids (see create_container)
    -- never taken as a caller-supplied path component -- so no separate
    traversal check is needed here, unlike workspace_capability's own
    resolve_workspace_path() (which guards genuinely caller-supplied
    relative paths). A caller-supplied container_id is only ever used
    as an exact-match dictionary/filename key, never path-joined with
    unsanitized segments beyond a plain f-string of the id itself."""
    return os.path.join(_org_dir(paths), f"{container_id}.json")


def _load_container(paths, container_id):
    if not isinstance(container_id, str) or not container_id or "/" in container_id or "\\" in container_id or ".." in container_id:
        return None
    path = _container_path(paths, container_id)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_container(paths, container):
    os.makedirs(_org_dir(paths), exist_ok=True)
    path = _container_path(paths, container["container_id"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(container, f, indent=2)


def list_containers(paths: wc.WorkspacePaths, requester_actor_id="clark"):
    """Bounded, deterministic listing (mirrors workspace_capability's
    own WSP2-P2 bounds/discipline) of every container's own
    {"container_id","container_type","name","reference_count"} summary
    -- never full reference lists (see inspect_container for those)."""
    org_dir = _org_dir(paths)
    if not os.path.isdir(org_dir):
        entries = []
    else:
        entries = sorted(f for f in os.listdir(org_dir) if f.endswith(".json"))
    bounded = wc._bound_entries(entries)
    summaries = []
    for filename in bounded["entries"]:
        container_id = filename[:-5]
        container = _load_container(paths, container_id)
        if container is not None:
            summaries.append({
                "container_id": container["container_id"],
                "container_type": container["container_type"],
                "name": container["name"],
                "reference_count": len(container["resource_references"]),
            })
    _log_action(paths, LIST, None, requester_actor_id, "performed")
    return {
        "entries": summaries,
        "returned_count": bounded["returned_count"],
        "total_count": bounded["total_count"],
        "truncated": bounded["truncated"],
    }, None


def create_container(paths: wc.WorkspacePaths, container_type, name, requester_actor_id="clark"):
    if container_type not in CONTAINER_TYPES:
        _log_action(paths, CREATE, None, requester_actor_id, "denied", "unknown container_type")
        return None, {"rationale": f"Unknown container_type. Must be one of: {sorted(CONTAINER_TYPES)}"}
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LENGTH:
        _log_action(paths, CREATE, None, requester_actor_id, "denied", "invalid name")
        return None, {"rationale": f"name must be a non-empty string of at most {MAX_NAME_LENGTH} characters."}

    container_id = f"org-{uuid.uuid4().hex[:16]}"
    if os.path.exists(_container_path(paths, container_id)):
        _log_action(paths, CREATE, container_id, requester_actor_id, "denied", "container_id collision")
        raise OrganizationError("Internal error: container_id collision.")

    container = {
        "container_id": container_id,
        "container_type": container_type,
        "name": name.strip(),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "resource_references": [],
    }
    _save_container(paths, container)
    _log_action(paths, CREATE, container_id, requester_actor_id, "performed")
    return dict(container), None


def inspect_container(paths: wc.WorkspacePaths, container_id, requester_actor_id="clark"):
    container = _load_container(paths, container_id)
    if container is None:
        _log_action(paths, INSPECT, container_id, requester_actor_id, "denied", "not found")
        return None, {"rationale": "Container not found."}
    _log_action(paths, INSPECT, container_id, requester_actor_id, "performed")
    return dict(container), None


def rename_container(paths: wc.WorkspacePaths, container_id, new_name, requester_actor_id="clark"):
    container = _load_container(paths, container_id)
    if container is None:
        _log_action(paths, RENAME, container_id, requester_actor_id, "denied", "not found")
        return None, {"rationale": "Container not found."}
    if not isinstance(new_name, str) or not new_name.strip() or len(new_name) > MAX_NAME_LENGTH:
        _log_action(paths, RENAME, container_id, requester_actor_id, "denied", "invalid name")
        return None, {"rationale": f"new_name must be a non-empty string of at most {MAX_NAME_LENGTH} characters."}
    container["name"] = new_name.strip()
    _save_container(paths, container)
    _log_action(paths, RENAME, container_id, requester_actor_id, "performed")
    return dict(container), None


def _reference_target_exists(paths, resource_class, relative_path):
    """Fail-closed existence + permission check, reusing workspace_
    capability's own authoritative machinery -- never a second,
    independently-implemented containment/permission decision. A
    reference may only ever point at something Clark is currently
    permitted to LIST within that resource class."""
    if resource_class not in wc.RESOURCE_CLASSES:
        return False
    allowed, _boundary_id, _rationale = wc.check_permission(resource_class, wc.LIST)
    if not allowed:
        return False
    try:
        real_path = wc.resolve_workspace_path(paths, resource_class, relative_path)
    except wc.PathEscapeError:
        return False
    return os.path.isfile(real_path)


def add_reference(paths: wc.WorkspacePaths, container_id, resource_class, relative_path, requester_actor_id="clark"):
    container = _load_container(paths, container_id)
    if container is None:
        _log_action(paths, ADD_REFERENCE, container_id, requester_actor_id, "denied", "not found")
        return None, {"rationale": "Container not found."}
    if not _reference_target_exists(paths, resource_class, relative_path):
        _log_action(paths, ADD_REFERENCE, container_id, requester_actor_id, "denied", "invalid or nonexistent reference target")
        return None, {"rationale": "Reference target does not exist or is not a recognized, permitted resource."}
    if len(container["resource_references"]) >= MAX_REFERENCES_PER_CONTAINER:
        _log_action(paths, ADD_REFERENCE, container_id, requester_actor_id, "denied", "reference limit reached")
        return None, {"rationale": f"Container already holds the maximum of {MAX_REFERENCES_PER_CONTAINER} references."}

    already_present = any(
        r["resource_class"] == resource_class and r["relative_path"] == relative_path
        for r in container["resource_references"]
    )
    if not already_present:
        container["resource_references"].append({
            "resource_class": resource_class,
            "relative_path": relative_path,
            "added_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        _save_container(paths, container)
    _log_action(paths, ADD_REFERENCE, container_id, requester_actor_id, "performed",
                {"resource_class": resource_class, "relative_path": relative_path, "already_present": already_present})
    return dict(container), None


def remove_reference(paths: wc.WorkspacePaths, container_id, resource_class, relative_path, requester_actor_id="clark"):
    """Removes a reference ONLY -- never touches the source resource
    file (spec: deleting/removing an organizational reference must not
    delete the source)."""
    container = _load_container(paths, container_id)
    if container is None:
        _log_action(paths, REMOVE_REFERENCE, container_id, requester_actor_id, "denied", "not found")
        return None, {"rationale": "Container not found."}

    original_count = len(container["resource_references"])
    container["resource_references"] = [
        r for r in container["resource_references"]
        if not (r["resource_class"] == resource_class and r["relative_path"] == relative_path)
    ]
    if len(container["resource_references"]) == original_count:
        _log_action(paths, REMOVE_REFERENCE, container_id, requester_actor_id, "denied", "reference not found")
        return None, {"rationale": "Reference not found in this container."}

    _save_container(paths, container)
    _log_action(paths, REMOVE_REFERENCE, container_id, requester_actor_id, "performed",
                {"resource_class": resource_class, "relative_path": relative_path})
    return dict(container), None


def delete_container(paths: wc.WorkspacePaths, container_id, requester_actor_id="clark"):
    """Deletes exactly one file: this container's own JSON record.
    Never deletes, moves, or modifies any referenced source resource --
    there is no code path here that opens anything under library_dir/
    music_dir/photographs_dir/journal_dir at all."""
    container = _load_container(paths, container_id)
    if container is None:
        _log_action(paths, DELETE_CONTAINER, container_id, requester_actor_id, "denied", "not found")
        return None, {"rationale": "Container not found."}
    os.remove(_container_path(paths, container_id))
    _log_action(paths, DELETE_CONTAINER, container_id, requester_actor_id, "performed")
    return {"status": "deleted", "container_id": container_id}, None
