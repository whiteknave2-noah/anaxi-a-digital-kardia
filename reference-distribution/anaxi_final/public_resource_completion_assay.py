"""Exhaustive read-only assay of every owner-defined public resource.

The source inventory is derived independently from the production ingest
module.  Production coverage comes from its durable manifest.  Every mapped
item is then exercised through ``workspace_direction.execute_workspace_action``
with the real production resource directories.  Only the action log is
redirected to a temporary path; source files, Workspace resources, canonical
history, and Private Space are never written or traversed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

import completion_evidence
from provenance_schema import derive_stable_id
import workspace_capability as wc
import workspace_direction as wd


OWNER_SOURCE_LAYOUT = {
    "Books": ("library", frozenset({".pdf", ".txt", ".md", ".rtf"})),
    "Pictures": ("photographs", frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})),
    "Music": ("music", frozenset({".wav", ".flac", ".mp3", ".wma"})),
}
OWNER_ROOT_FILES = {
    "ANAXI_CAPABILITIES_AND_PATHWAYS.md": ("library", "ANAXI_CAPABILITIES_AND_PATHWAYS.md"),
}
MANIFEST_NAME = ".anaxi_public_resource_manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_owner_required_resources(source_root: Path) -> list[dict]:
    """Independent authoritative inventory from the owner's classified tree."""
    if not source_root.is_dir():
        raise FileNotFoundError(f"owner public source root unavailable: {source_root}")
    resources = []
    for source_name, (resource_class, extensions) in OWNER_SOURCE_LAYOUT.items():
        class_root = source_root / source_name
        if not class_root.is_dir():
            raise FileNotFoundError(f"required owner source class unavailable: {class_root}")
        for directory, dirnames, filenames in os.walk(class_root, followlinks=False):
            dirnames[:] = sorted(
                name for name in dirnames
                if not name.startswith(".") and not (Path(directory) / name).is_symlink()
            )
            for filename in sorted(filenames):
                source = Path(directory) / filename
                if source.is_symlink() or source.suffix.lower() not in extensions:
                    continue
                relative = source.relative_to(class_root).as_posix()
                resources.append({
                    "source_key": f"{source_name}/{relative}",
                    "source_path": str(source),
                    "resource_class": resource_class,
                })
    for filename, (resource_class, _destination) in OWNER_ROOT_FILES.items():
        source = source_root / filename
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(f"required owner public root file unavailable: {source}")
        resources.append({
            "source_key": filename, "source_path": str(source),
            "resource_class": resource_class,
        })
    return sorted(resources, key=lambda item: item["source_key"].casefold())


def _action(resource_class: str, action: str, relative_path: str, content: str = "") -> dict:
    return {
        "resource_class": resource_class, "action": action,
        "relative_path": relative_path, "content": content,
    }


def _assay_library(paths: wc.WorkspacePaths, relative_path: str) -> dict:
    boundary, performed = wd.execute_workspace_action(
        paths, _action(wc.LIBRARY, wc.READ, relative_path,
                       json.dumps({"offset": 0, "max_chars": 4000})),
        derive_stable_id("actor", "clark"),
    )
    if performed:
        result = boundary["result"]
        if not isinstance(result.get("content"), str) or not result["content"].strip():
            raise RuntimeError("ordinary READ returned no substantive content")
        return {
            "operation": "read", "chars": len(result["content"]),
            "has_more": result["has_more"],
            "document_type": result.get("document_type", "text"),
        }
    rationale = boundary.get("rationale") or ""
    if not rationale.startswith(wc.PDF_TEXT_UNAVAILABLE):
        raise RuntimeError(f"ordinary READ refused: {rationale}")
    page_count = int(rationale.split("choose any page 1-")[1].split(")")[0])
    page = max(1, (page_count + 1) // 2)
    viewed, viewed_performed = wd.execute_workspace_action(
        paths, _action(wc.LIBRARY, wc.VIEW_PAGE, relative_path, json.dumps({"page": page})),
        derive_stable_id("actor", "clark"),
    )
    pixels = wd.get_and_clear_last_view_image_bytes()
    if not viewed_performed or not pixels:
        raise RuntimeError(f"ordinary VIEW_PAGE did not deliver pixels: {viewed.get('rationale')}")
    return {
        "operation": "view_page", "page": page, "page_count": page_count,
        "delivered_bytes": len(pixels), "document_type": viewed["result"]["document_type"],
    }


def _assay_photo(paths: wc.WorkspacePaths, relative_path: str) -> dict:
    boundary, performed = wd.execute_workspace_action(
        paths, _action(wc.PHOTOGRAPHS, wc.VIEW, relative_path),
        derive_stable_id("actor", "clark"),
    )
    pixels = wd.get_and_clear_last_view_image_bytes()
    if not performed or not pixels:
        raise RuntimeError(f"ordinary VIEW did not deliver pixels: {boundary.get('rationale')}")
    result = boundary["result"]
    return {
        "operation": "view", "delivered_bytes": len(pixels),
        "width": result["width"], "height": result["height"],
        "derivative": result["derivative"],
    }


def _assay_music(paths: wc.WorkspacePaths, relative_path: str) -> dict:
    opening, performed = wd.execute_workspace_action(
        paths, _action(wc.MUSIC, wc.LISTEN, relative_path),
        derive_stable_id("actor", "clark"),
    )
    if not performed:
        raise RuntimeError(f"ordinary LISTEN refused: {opening.get('rationale')}")
    duration = float(opening["result"]["duration_seconds"])
    if duration <= 0:
        raise RuntimeError("ordinary LISTEN established no positive duration")
    # LISTEN intentionally renders duration to millisecond precision.  A
    # rounded-up value can be a fraction beyond the decoder's exact sample
    # duration, so select a genuinely late window just inside the established
    # source instead of asking for a mathematically impossible endpoint.
    end = max(0.001, duration - 0.1)
    start = max(0.0, end - min(5.0, end))
    late_payload = json.dumps({
        "view": "dynamics", "start_seconds": start, "end_seconds": end,
    })
    late, late_performed = wd.execute_workspace_action(
        paths, _action(wc.MUSIC, wc.INSPECT_AUDIO, relative_path, late_payload),
        derive_stable_id("actor", "clark"),
    )
    if not late_performed:
        raise RuntimeError(f"ordinary late/end INSPECT_AUDIO refused: {late.get('rationale')}")
    return {
        "operation": "listen_and_late_dynamics", "duration_seconds": duration,
        "opening_interval_seconds": opening["result"]["decoded_interval_seconds"],
        "late_interval_seconds": late["result"]["interval_seconds"],
        "decoder": opening["result"].get("decoder"),
    }


def run_assay(source_root: Path, workspace_root: Path) -> dict:
    try:
        authoritative = discover_owner_required_resources(source_root)
        authoritative_established = True
    except Exception as exc:
        authoritative = []
        authoritative_established = False
        discovery_error = f"{type(exc).__name__}: {exc}"

    manifest_path = workspace_root / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_resources = manifest["resources"]
        if not isinstance(manifest_resources, dict):
            raise TypeError("manifest resources is not an object")
    except Exception as exc:
        manifest_resources = {}
        manifest_error = f"{type(exc).__name__}: {exc}"

    paths = wc.WorkspacePaths(str(workspace_root))
    temporary_log = tempfile.NamedTemporaryFile(prefix="anaxi-resource-assay-", suffix=".jsonl", delete=False)
    temporary_log.close()
    paths.action_log_path = temporary_log.name
    results_by_class = {"library": {}, "photographs": {}, "music": {}}
    operation_receipts = {}
    try:
        for item in authoritative:
            key = item["source_key"]
            resource_class = item["resource_class"]
            manifest_entry = manifest_resources.get(key)
            if manifest_entry is None:
                continue
            try:
                source = Path(item["source_path"])
                destination = workspace_root / resource_class / manifest_entry["destination_relative_path"]
                expected_hash = manifest_entry["sha256"]
                if _sha256(source) != expected_hash:
                    raise RuntimeError("source hash does not match production manifest")
                if not destination.is_file() or _sha256(destination) != expected_hash:
                    raise RuntimeError("production destination missing or hash-mismatched")
                relative_path = manifest_entry["destination_relative_path"]
                if resource_class == "library":
                    receipt = _assay_library(paths, relative_path)
                elif resource_class == "photographs":
                    receipt = _assay_photo(paths, relative_path)
                else:
                    receipt = _assay_music(paths, relative_path)
                results_by_class[resource_class][key] = {
                    "status": completion_evidence.PASS,
                    "evidence": receipt,
                }
                operation_receipts[key] = receipt
            except Exception as exc:
                results_by_class[resource_class][key] = {
                    "status": completion_evidence.FAIL,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
    finally:
        try:
            os.unlink(temporary_log.name)
        except OSError:
            pass

    records = []
    for resource_class in ("library", "photographs", "music"):
        required = [
            item["source_key"] for item in authoritative
            if item["resource_class"] == resource_class
        ] if authoritative_established else None
        discovered = [
            key for key, entry in manifest_resources.items()
            if entry.get("resource_class") == resource_class
        ]
        record = completion_evidence.build_completion_record(
            capability_id=f"public_resources.{resource_class}",
            authoritative_items=required,
            production_discovered_items=discovered,
            item_results=results_by_class[resource_class],
            nonempty_expected=True,
        )
        records.append(record)

    report = {
        "assay": "owner_public_resources_v1",
        "source_root": str(source_root), "workspace_root": str(workspace_root),
        "private_space_traversed": False,
        "canonical_production_history_mutated": False,
        "production_resource_files_mutated": False,
        "ordinary_route": "workspace_direction.execute_workspace_action",
        "authoritative_discovery_error": None if authoritative_established else discovery_error,
        "production_manifest_error": locals().get("manifest_error"),
        "records": records,
    }
    report["whole_resource_result"] = completion_evidence.build_whole_system_record(records)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_assay(args.source_root, args.workspace_root)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["whole_resource_result"]["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
