"""Deterministic public-resource ingest for ANAXI's production Workspace.

Only the explicitly public source classes named here are traversed.  Private
Space, Archive, journals, Obsidian internals, shortcuts, URLs, and
unclassified miscellaneous files are structurally unreachable from this
module.  Originals are never modified.  Copies retain a manifest binding the
source-relative path, destination-relative path, size, timestamps, and SHA-256.

Reconciliation is deliberately no-clobber: a destination previously managed
by this manifest may be atomically refreshed only while its bytes still match
the prior manifest.  A human-edited or unrelated collision is reported and
left untouched.  Exact duplicate bytes already present in the Workspace are
represented once and bound to that existing copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import runtime_roots


MANIFEST_VERSION = 1
MANIFEST_NAME = ".anaxi_public_resource_manifest.json"
SOURCE_ROOT_ENV = "ANAXI_PUBLIC_RESOURCE_ROOT"

PUBLIC_SOURCE_LAYOUT = {
    "Books": ("library", frozenset({".pdf", ".txt", ".md", ".rtf"})),
    "Pictures": ("photographs", frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})),
    "Music": ("music", frozenset({".wav", ".flac", ".mp3", ".wma"})),
}
ROOT_PUBLIC_FILES = {
    "ANAXI_CAPABILITIES_AND_PATHWAYS.md": ("library", "ANAXI_CAPABILITIES_AND_PATHWAYS.md"),
}


class PublicResourceIngestError(RuntimeError):
    pass


def default_public_source_root() -> Path:
    configured = os.environ.get(SOURCE_ROOT_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Documents" / "Clark Kara Other" / "Clark Kara Other"


def default_workspace_root() -> Path:
    return Path(runtime_roots.workspace_root())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def discover_public_resources(source_root: Path) -> list[dict]:
    """Return the complete classified public manifest, without mutations."""
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise PublicResourceIngestError(f"Public resource root is unavailable: {source_root}")

    discovered: list[dict] = []
    for source_name, (resource_class, extensions) in PUBLIC_SOURCE_LAYOUT.items():
        class_root = source_root / source_name
        if not class_root.is_dir():
            continue
        class_root_real = class_root.resolve()
        for directory, dirnames, filenames in os.walk(class_root, followlinks=False):
            dirnames[:] = sorted(
                name for name in dirnames
                if not name.startswith(".") and not (Path(directory) / name).is_symlink()
            )
            for filename in sorted(filenames):
                candidate = Path(directory) / filename
                if candidate.is_symlink() or candidate.suffix.lower() not in extensions:
                    continue
                real = candidate.resolve()
                if not _within(class_root_real, real) or not real.is_file():
                    continue
                relative_inside_class = candidate.relative_to(class_root).as_posix()
                discovered.append({
                    "source_key": f"{source_name}/{relative_inside_class}",
                    "source_path": str(real),
                    "resource_class": resource_class,
                    "intended_destination": relative_inside_class,
                })

    for filename, (resource_class, destination) in sorted(ROOT_PUBLIC_FILES.items()):
        candidate = source_root / filename
        if candidate.is_file() and not candidate.is_symlink():
            real = candidate.resolve()
            if _within(source_root, real):
                discovered.append({
                    "source_key": filename,
                    "source_path": str(real),
                    "resource_class": resource_class,
                    "intended_destination": destination,
                })

    return sorted(discovered, key=lambda item: item["source_key"].casefold())


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"version": MANIFEST_VERSION, "resources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise PublicResourceIngestError(f"Public resource manifest is unreadable: {path}") from exc
    if data.get("version") != MANIFEST_VERSION or not isinstance(data.get("resources"), dict):
        raise PublicResourceIngestError(f"Unsupported public resource manifest: {path}")
    return data


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".anaxi-ingest-", dir=str(destination.parent))
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".anaxi-manifest-", dir=str(path.parent))
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _workspace_files(workspace_root: Path) -> list[Path]:
    files: list[Path] = []
    for resource_class in ("library", "photographs", "music"):
        root = workspace_root / resource_class
        if not root.is_dir():
            continue
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
            for filename in sorted(filenames):
                candidate = Path(directory) / filename
                if not candidate.is_symlink() and candidate.is_file() and not filename.startswith("."):
                    files.append(candidate)
    return files


def sync_public_resources(source_root: Path, workspace_root: Path) -> dict:
    """Copy/reconcile the classified public collection into Workspace."""
    source_root = source_root.resolve()
    workspace_root = workspace_root.resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    manifest_path = workspace_root / MANIFEST_NAME
    prior = _load_manifest(manifest_path)
    prior_resources = prior["resources"]
    discovered = discover_public_resources(source_root)

    existing_by_hash: dict[tuple[str, str], list[Path]] = {}
    for existing in _workspace_files(workspace_root):
        resource_class = existing.relative_to(workspace_root).parts[0]
        existing_by_hash.setdefault((resource_class, _sha256(existing)), []).append(existing)
    prior_destination_refs: dict[tuple[str, str], int] = {}
    for prior_entry in prior_resources.values():
        key = (
            prior_entry.get("resource_class", ""),
            prior_entry.get("destination_relative_path", ""),
        )
        prior_destination_refs[key] = prior_destination_refs.get(key, 0) + 1

    resources: dict[str, dict] = {}
    report = {
        "source_root": str(source_root),
        "workspace_root": str(workspace_root),
        "discovered": len(discovered),
        "copied": 0,
        "updated": 0,
        "deduplicated": 0,
        "unchanged": 0,
        "conflicts": [],
        "stale_preserved": sorted(set(prior_resources) - {item["source_key"] for item in discovered}),
    }

    for item in discovered:
        source = Path(item["source_path"])
        source_stat = source.stat()
        prior_entry = prior_resources.get(item["source_key"])
        source_hash = None

        if prior_entry is not None:
            if prior_entry.get("resource_class") != item["resource_class"]:
                raise PublicResourceIngestError(
                    f"Manifest class changed for {item['source_key']}"
                )
            destination = workspace_root / prior_entry["resource_class"] / prior_entry["destination_relative_path"]
            destination = destination.resolve()
            class_root = (workspace_root / prior_entry["resource_class"]).resolve()
            if not _within(class_root, destination):
                raise PublicResourceIngestError("Manifest destination escaped its resource root")
            metadata_unchanged = (
                prior_entry.get("source_size") == source_stat.st_size
                and prior_entry.get("source_mtime_ns") == source_stat.st_mtime_ns
                and destination.is_file()
                and prior_entry.get("destination_size") == destination.stat().st_size
                and prior_entry.get("destination_mtime_ns") == destination.stat().st_mtime_ns
            )
            if metadata_unchanged:
                resources[item["source_key"]] = prior_entry
                report["unchanged"] += 1
                continue
            source_hash = _sha256(source)
            current_hash = _sha256(destination) if destination.is_file() else None
            if current_hash not in (None, prior_entry.get("sha256")):
                report["conflicts"].append({
                    "source_key": item["source_key"],
                    "destination": str(destination),
                    "reason": "managed destination changed outside ingest",
                })
                resources[item["source_key"]] = prior_entry
                continue
            if current_hash != source_hash:
                shared_key = (
                    prior_entry["resource_class"],
                    prior_entry["destination_relative_path"],
                )
                if current_hash is not None and prior_destination_refs.get(shared_key, 0) > 1:
                    intended = (
                        workspace_root / item["resource_class"] / item["intended_destination"]
                    ).resolve()
                    class_root = (workspace_root / item["resource_class"]).resolve()
                    if not _within(class_root, intended):
                        raise PublicResourceIngestError("Intended destination escaped its resource root")
                    destination = intended
                    if destination.exists() and _sha256(destination) != source_hash:
                        suffix = destination.suffix
                        destination = destination.with_name(
                            f"{destination.stem}--{source_hash[:12]}{suffix}"
                        )
                    if destination.exists() and _sha256(destination) != source_hash:
                        report["conflicts"].append({
                            "source_key": item["source_key"],
                            "destination": str(destination),
                            "reason": "shared managed destination diverged and safe alternate exists with different bytes",
                        })
                        resources[item["source_key"]] = prior_entry
                        continue
                _atomic_copy(source, destination)
                existing_by_hash.setdefault((item["resource_class"], source_hash), []).append(destination)
                report["updated"] += 1
            else:
                report["unchanged"] += 1
        else:
            source_hash = _sha256(source)
            duplicates = [
                candidate for candidate in existing_by_hash.get((item["resource_class"], source_hash), [])
                if candidate.is_file() and _sha256(candidate) == source_hash
            ]
            if duplicates:
                destination = sorted(duplicates, key=lambda path: str(path).casefold())[0]
                report["deduplicated"] += 1
            else:
                destination = workspace_root / item["resource_class"] / item["intended_destination"]
                destination = destination.resolve()
                class_root = (workspace_root / item["resource_class"]).resolve()
                if not _within(class_root, destination):
                    raise PublicResourceIngestError("Intended destination escaped its resource root")
                if destination.exists():
                    report["conflicts"].append({
                        "source_key": item["source_key"],
                        "destination": str(destination),
                        "reason": "unmanaged destination has different bytes",
                    })
                    continue
                _atomic_copy(source, destination)
                existing_by_hash.setdefault((item["resource_class"], source_hash), []).append(destination)
                report["copied"] += 1

        destination_stat = destination.stat()
        destination_relative = destination.relative_to(workspace_root / item["resource_class"]).as_posix()
        resources[item["source_key"]] = {
            "resource_class": item["resource_class"],
            "destination_relative_path": destination_relative,
            "sha256": source_hash or prior_entry["sha256"],
            "source_size": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "destination_size": destination_stat.st_size,
            "destination_mtime_ns": destination_stat.st_mtime_ns,
        }

    _atomic_write_json(manifest_path, {
        "version": MANIFEST_VERSION,
        "source_root": str(source_root),
        "resources": resources,
    })
    report["represented"] = len(resources)
    report["ok"] = not report["conflicts"] and report["represented"] == report["discovered"]
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize classified public resources into ANAXI Workspace")
    parser.add_argument("command", choices=("sync", "inspect"))
    parser.add_argument("--source-root", type=Path, default=default_public_source_root())
    parser.add_argument("--workspace-root", type=Path, default=default_workspace_root())
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = {"resources": discover_public_resources(args.source_root)}
        else:
            result = sync_public_resources(args.source_root, args.workspace_root)
    except PublicResourceIngestError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
