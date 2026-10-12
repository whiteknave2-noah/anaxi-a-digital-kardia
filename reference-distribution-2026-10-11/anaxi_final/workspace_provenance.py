"""Resource-identity provenance for public workspace items.

The owner's public collections are organized on purpose: each family member's
photographs live in their own named folder, and that logical source is
meaningful context. The workspace copy does not always preserve it in its own
path -- the public ingest binds a source whose bytes already exist elsewhere in
the workspace to that existing copy ("represented once"), so a photograph filed
under ``Pictures/Family/<Name>/`` can sit at the photographs root under just its
file name. The subject then saw only ``lp_image.JPG`` and truthfully could not say
whose folder it came from.

The authority for "where did this come from" is the ingest manifest
(``.anaxi_public_resource_manifest.json``: source key -> workspace destination),
plus the workspace path itself. This module resolves that authoritative logical
source for an item the subject selected and attaches it to what is delivered. It
reports the FILING (which named folder the owner keeps it in); it never infers
who or what is in the resource, and it says so.

Read-only. Never touches Private Space (it is not a public class and has no
manifest entries). No model call.
"""
import json
import os

MANIFEST_NAME = ".anaxi_public_resource_manifest.json"
# The ingest's source class directories (public_resource_ingest.PUBLIC_SOURCE_LAYOUT).
SOURCE_CLASS_DIRS = frozenset({"Books", "Pictures", "Music"})
PUBLIC_CLASSES = frozenset({"library", "photographs", "music"})

FILING_NOTE = (
    "source_folder is the owner's filing of this item, not an identification of anyone or anything in it."
)

_cache = {}


def _load(paths):
    path = os.path.join(paths.root, MANIFEST_NAME)
    try:
        stat = os.stat(path)
    except OSError:
        return {}
    key = (path, stat.st_mtime_ns, stat.st_size)
    if _cache.get("key") == key:
        return _cache["by_destination"]
    by_destination = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            resources = json.load(handle).get("resources", {})
        for source_key, entry in resources.items():
            if not isinstance(entry, dict):
                continue
            destination = entry.get("destination_relative_path")
            resource_class = entry.get("resource_class")
            if isinstance(destination, str) and isinstance(resource_class, str):
                by_destination.setdefault((resource_class, destination), []).append(source_key)
    except (OSError, ValueError, TypeError, AttributeError):
        by_destination = {}
    _cache.clear()
    _cache.update(key=key, by_destination=by_destination)
    return by_destination


def _logical_folder(source_key):
    """'Pictures/Family/Blair/x.JPG' -> 'Family/Blair'; a flat or root source -> ''."""
    parts = [p for p in source_key.replace("\\", "/").split("/") if p]
    if parts and parts[0] in SOURCE_CLASS_DIRS:
        parts = parts[1:]
    return "/".join(parts[:-1])


def source_folders(paths, resource_class, relative_path):
    """Sorted distinct logical folders the owner files this item under (usually one;
    several when identical bytes are filed under more than one folder), from the
    ingest manifest; falls back to the workspace directory when the manifest has no
    record. Empty list when the item is not filed under any folder."""
    if resource_class not in PUBLIC_CLASSES or not isinstance(relative_path, str) or not relative_path:
        return []
    relative_path = relative_path.replace("\\", "/")
    folders = {
        _logical_folder(source_key)
        for source_key in _load(paths).get((resource_class, relative_path), [])
    }
    folders.discard("")
    if not folders:
        directory = os.path.dirname(relative_path)
        if directory:
            folders.add(directory)
    return sorted(folders)


def resource_provenance(paths, resource_class, relative_path):
    """The provenance block for one selected item, or None when it has no folder
    identity to report."""
    folders = source_folders(paths, resource_class, relative_path)
    if not folders:
        return None
    block = {"source_folder": folders[0] if len(folders) == 1 else folders, "note": FILING_NOTE}
    if len(folders) == 1:
        block["folder_name"] = folders[0].rsplit("/", 1)[-1]
    names = sorted({
        key.replace("\\", "/").rsplit("/", 1)[-1]
        for key in _load(paths).get((resource_class, relative_path.replace("\\", "/")), [])
    } - {os.path.basename(relative_path)})
    if names:   # the same bytes are also filed under another file name
        block["also_filed_as"] = names
    return block


def attach_resource_provenance(paths, resource_class, relative_path, result):
    """Copy of ``result`` with its provenance block, when there is one. Never
    replaces or reorders the substantive content."""
    if not isinstance(result, dict):
        return result
    block = resource_provenance(paths, resource_class, relative_path)
    if block is None:
        return result
    return dict(result, resource_provenance=block)


def attach_entry_folders(paths, resource_class, directory, listing):
    """For a listing: the logical folder of each listed FILE whose filing folder is
    not the directory it is listed in (e.g. a photograph bound to a root copy), so
    the subject choosing from the list can see it. Absent when there is nothing to add."""
    if not isinstance(listing, dict) or resource_class not in PUBLIC_CLASSES:
        return listing
    # A search (query) lists paths relative to the class root; a browse lists names in ``directory``.
    base = "" if listing.get("query") else (directory or "").strip("/")
    extra = {}
    for name in listing.get("entries", []):
        if not isinstance(name, str) or name.endswith("/"):
            continue
        relative = f"{base}/{name}" if base else name
        folders = source_folders(paths, resource_class, relative)
        if folders and folders != [os.path.dirname(relative)]:
            extra[name] = folders[0] if len(folders) == 1 else folders
    return dict(listing, entry_folders=extra) if extra else listing
