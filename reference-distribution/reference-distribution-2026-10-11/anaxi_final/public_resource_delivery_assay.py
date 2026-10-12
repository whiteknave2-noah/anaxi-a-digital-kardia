"""Exhaustive assay that every owner-defined public resource can actually be
USED BY THE SUBJECT through the ordinary route -- not merely opened by the host.

``public_resource_completion_assay`` proves the host can open every resource.
That is necessary and was, historically, mistaken for sufficient: at production
prompt sizes the workspace Pass 2 dropped a library page or a collection
listing whole, so the host "succeeded" and the subject received nothing. This
assay therefore takes every item through the same ordinary dispatcher AND the
delivery stage Pass 2 applies (``workspace_delivery.fit_boundary_result``,
sized to the conservative production Pass-2 room), and requires:

  * the collection is REACHABLE by following delivered continuations, with
    every authoritative item appearing (no first-N truncation, no omission);
  * a library text/PDF item delivers substantive text whose delivered windows
    are contiguous, exact, and consistent with an independent read; a text
    item is followed to its true end;
  * a scanned/image-only PDF delivers real pixels for first/middle/last page;
  * a photograph delivers real pixels in an admissible bounded representation;
  * a music item delivers its mechanical listen result and a genuine late-end
    window, each fitting the prompt room whole.

The room is the estimator-only (worst case) Pass-2 room at the measured
production floor; a real provider measurement can only enlarge it. Read-only:
sources, workspace resources, canonical history and Private Space are never
written or traversed; only the action log is redirected to a temp file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

import completion_evidence
import context_budget
import public_resource_completion_assay as base
from provenance_schema import derive_stable_id
import workspace_capability as wc
import workspace_delivery
import workspace_direction as wd

# Measured production Pass-2 hard floor (live diagnostics, conversation-mode
# core with calibrated framing, an ordinary 456-byte human message):
# 1030 identity + 156 framing + 456 human + 460 task/framing text.
PRODUCTION_PASS2_HARD_FLOOR = 2102
ROOM = context_budget.WSP1_PASS2_MAX_PROMPT_BUDGET - PRODUCTION_PASS2_HARD_FLOOR
MIN_SUBSTANTIVE_CHARS = 200
MAX_TEXT_WINDOWS = 1500      # a plain-text item is followed to its true end within this
PDF_CHAIN_WINDOWS = 8        # a PDF's windows are chained this far and cross-checked
CLARK = derive_stable_id("actor", "clark")


def _act(paths, resource_class, action, relative_path="", payload=None):
    content = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    boundary, performed = wd.execute_workspace_action(
        paths, base._action(resource_class, action, relative_path, content), CLARK,
    )
    return boundary, performed


def _delivered(boundary, performed):
    """What the subject actually receives for this executed action."""
    if not performed:
        return boundary, {"delivery": "none"}
    fitted, info = workspace_delivery.fit_boundary_result(boundary, ROOM)
    if info["delivery"] == "withheld":
        raise RuntimeError("result was withheld from the subject (does not fit the prompt room)")
    return fitted, info


def walk_collection(paths, resource_class):
    """Every file reachable by following delivered listings and continuations,
    recursing into directories, exactly as a subject browsing would."""
    seen, pending_dirs = [], [""]
    while pending_dirs:
        directory = pending_dirs.pop(0)
        payload = None
        for _ in range(10_000):
            boundary, performed = _act(paths, resource_class, wc.LIST, directory, payload)
            if not performed:
                raise RuntimeError(f"ordinary LIST refused: {boundary.get('rationale')}")
            fitted, _info = _delivered(boundary, performed)
            page = fitted["result"]
            if not page["entries"] and page.get("has_more"):
                raise RuntimeError("a delivered listing page was empty while more remained")
            for name in page["entries"]:
                path = f"{directory}{name}" if not directory else f"{directory.rstrip('/')}/{name}"
                if name.endswith("/"):
                    pending_dirs.append(path)
                else:
                    seen.append(path)
            if not page.get("has_more"):
                break
            payload = json.loads(page["next_request"])
        else:
            raise RuntimeError("listing did not terminate")
    return seen


def _text_windows(paths, relative_path, limit):
    """Follow delivered read windows; return (collected_text, windows, reached_end)."""
    offset, collected, windows = 0, "", 0
    while windows < limit:
        boundary, performed = _act(paths, wc.LIBRARY, wc.READ, relative_path, {"offset": offset, "max_chars": 4000})
        if not performed:
            return boundary, None, None
        fitted, _info = _delivered(boundary, performed)
        result = fitted["result"]
        content = result["content"]
        if not content:
            raise RuntimeError("a delivered read window was empty")
        if result["next_offset"] != offset + len(content):
            raise RuntimeError("delivered window's continuation offset does not match what was delivered")
        collected += content
        windows += 1
        offset = result["next_offset"]
        if not result["has_more"]:
            return collected, windows, True
        if json.loads(result["next_request"])["offset"] != offset:
            raise RuntimeError("delivered next_request does not continue where the window ended")
    return collected, windows, False


def assay_library_item(paths, relative_path):
    is_pdf = relative_path.lower().endswith(".pdf")
    first = _text_windows(paths, relative_path, PDF_CHAIN_WINDOWS if is_pdf else MAX_TEXT_WINDOWS)
    if first[1] is None:                       # refused: scanned / image-only PDF
        boundary = first[0]
        rationale = boundary.get("rationale") or ""
        if not rationale.startswith(wc.PDF_TEXT_UNAVAILABLE):
            raise RuntimeError(f"ordinary READ refused: {rationale}")
        page_count = int(rationale.split("choose any page 1-")[1].split(")")[0])
        pages = sorted({1, max(1, (page_count + 1) // 2), page_count})
        delivered = []
        for page in pages:
            viewed, performed = _act(paths, wc.LIBRARY, wc.VIEW_PAGE, relative_path, {"page": page})
            pixels = wd.get_and_clear_last_view_image_bytes()
            if not performed or not pixels:
                raise RuntimeError(f"page {page} did not deliver pixels: {viewed.get('rationale')}")
            _delivered(viewed, performed)
            rep = wc.derive_bounded_vision_representation(pixels)
            cost = context_budget.qwen_image_admission_cost(rep["width"], rep["height"])
            if cost > context_budget.QWEN_IMAGE_PLATEAU_TOKEN_COST:
                raise RuntimeError(f"page {page} representation costs {cost}, above the bounded plateau")
            delivered.append({"page": page, "representation": [rep["width"], rep["height"]], "cost": cost})
        return {"operation": "view_page", "page_count": page_count, "pages_delivered": delivered}

    collected, windows, reached_end = first
    if len(collected.strip()) < MIN_SUBSTANTIVE_CHARS and reached_end is False:
        raise RuntimeError("delivered text is not substantive")
    independent, failure = wc.read_library_bounded(paths, relative_path, offset=0, max_chars=len(collected))
    if failure is not None or independent["content"] != collected:
        raise RuntimeError("delivered windows disagree with an independent read of the same range")
    if not is_pdf and not reached_end:
        raise RuntimeError(f"plain-text item not followed to its end within {MAX_TEXT_WINDOWS} windows")
    return {
        "operation": "read_windows", "windows": windows, "delivered_chars": len(collected),
        "reached_end": reached_end, "document_type": "pdf" if is_pdf else "text",
    }


def assay_photo_item(paths, relative_path):
    boundary, performed = _act(paths, wc.PHOTOGRAPHS, wc.VIEW, relative_path)
    pixels = wd.get_and_clear_last_view_image_bytes()
    if not performed or not pixels:
        raise RuntimeError(f"ordinary VIEW did not deliver pixels: {boundary.get('rationale')}")
    _delivered(boundary, performed)
    rep = wc.derive_bounded_vision_representation(pixels)
    cost = context_budget.qwen_image_admission_cost(rep["width"], rep["height"])
    if cost > context_budget.QWEN_IMAGE_PLATEAU_TOKEN_COST:
        raise RuntimeError(f"representation costs {cost}, above the bounded plateau")
    return {"operation": "view", "representation": [rep["width"], rep["height"]], "image_admission_cost": cost,
            "delivered_source_bytes": len(pixels)}


def assay_music_item(paths, relative_path):
    opening, performed = _act(paths, wc.MUSIC, wc.LISTEN, relative_path)
    if not performed:
        raise RuntimeError(f"ordinary LISTEN refused: {opening.get('rationale')}")
    fitted, info = _delivered(opening, performed)
    if info["delivery"] != "whole":
        raise RuntimeError(f"LISTEN result did not reach the subject whole: {info}")
    duration = float(fitted["result"]["duration_seconds"])
    end = max(0.001, duration - 0.1)
    start = max(0.0, end - min(5.0, end))
    late, late_performed = _act(paths, wc.MUSIC, wc.INSPECT_AUDIO, relative_path,
                                {"view": "dynamics", "start_seconds": start, "end_seconds": end})
    if not late_performed:
        raise RuntimeError(f"late INSPECT_AUDIO refused: {late.get('rationale')}")
    _fitted, late_info = _delivered(late, late_performed)
    if late_info["delivery"] != "whole":
        raise RuntimeError(f"late INSPECT_AUDIO result did not reach the subject whole: {late_info}")
    return {"operation": "listen_and_late_dynamics", "duration_seconds": duration,
            "listen_delivery": info["delivery"], "late_delivery": late_info["delivery"]}


def opaque_id(resource_class: str, source_key: str) -> str:
    """Stable, non-reversible identifier so evidence carries no resource names."""
    return "item:" + hashlib.sha256(f"{resource_class}\x00{source_key}".encode("utf-8")).hexdigest()[:16]


ASSAYS = {"library": assay_library_item, "photographs": assay_photo_item, "music": assay_music_item}


def run_assay(source_root: Path, workspace_root: Path) -> dict:
    discovery_error = manifest_error = None
    try:
        authoritative = base.discover_owner_required_resources(source_root)
    except Exception as exc:
        authoritative, discovery_error = [], f"{type(exc).__name__}: {exc}"
    try:
        manifest = json.loads((workspace_root / base.MANIFEST_NAME).read_text(encoding="utf-8"))["resources"]
    except Exception as exc:
        manifest, manifest_error = {}, f"{type(exc).__name__}: {exc}"

    paths = wc.WorkspacePaths(str(workspace_root))
    log = tempfile.NamedTemporaryFile(prefix="anaxi-delivery-assay-", suffix=".jsonl", delete=False)
    log.close()
    paths.action_log_path = log.name
    per_class = {"library": {}, "photographs": {}, "music": {}}
    walks, walk_errors = {}, {}
    try:
        for resource_class in per_class:
            try:
                walks[resource_class] = set(walk_collection(paths, resource_class))
            except Exception as exc:
                walks[resource_class], walk_errors[resource_class] = set(), f"{type(exc).__name__}: {exc}"
        for item in authoritative:
            key, resource_class = item["source_key"], item["resource_class"]
            entry = manifest.get(key)
            if entry is None:
                continue
            relative = entry["destination_relative_path"]
            oid = opaque_id(resource_class, key)
            try:
                if resource_class in walk_errors:
                    raise RuntimeError(f"collection walk failed: {walk_errors[resource_class]}")
                if relative not in walks[resource_class]:
                    raise RuntimeError("not reachable by following delivered listings and continuations")
                receipt = ASSAYS[resource_class](paths, relative)
                per_class[resource_class][oid] = {"status": completion_evidence.PASS, "evidence": receipt}
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                for name in {key, relative, os.path.basename(relative)}:
                    reason = reason.replace(name, oid)          # evidence never names a resource
                per_class[resource_class][oid] = {"status": completion_evidence.FAIL, "reason": reason}
    finally:
        try:
            os.unlink(log.name)
        except OSError:
            pass

    records = []
    for resource_class, results in per_class.items():
        required = [opaque_id(resource_class, i["source_key"]) for i in authoritative if i["resource_class"] == resource_class] \
            if discovery_error is None else None
        discovered = [opaque_id(resource_class, k) for k, e in manifest.items() if e.get("resource_class") == resource_class]
        records.append(completion_evidence.build_completion_record(
            capability_id=f"public_resource_delivery.{resource_class}",
            authoritative_items=required, production_discovered_items=discovered,
            item_results=results, nonempty_expected=True,
        ))
    report = {
        "assay": "owner_public_resource_delivery_v1",
        "source_root": str(source_root), "workspace_root": str(workspace_root),
        "pass2_room_estimator_tokens": ROOM, "production_pass2_hard_floor": PRODUCTION_PASS2_HARD_FLOOR,
        "private_space_traversed": False, "canonical_production_history_mutated": False,
        "production_resource_files_mutated": False,
        "ordinary_route": "workspace_direction.execute_workspace_action + workspace_delivery.fit_boundary_result",
        "identifier_scheme": "item:<first 16 hex of sha256(resource_class NUL source_key)>; no resource names or contents are recorded",
        "authoritative_discovery_error": discovery_error, "production_manifest_error": manifest_error,
        "collection_walk_errors": walk_errors,
        "collection_walk_counts": {k: len(v) for k, v in walks.items()},
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
    summary = {r["capability_id"]: (r["attempted_total"], r["authoritative_expected_total"], r["failed_total"])
               for r in report["records"]}
    print(json.dumps(summary, indent=1))
    return 0 if report["whole_resource_result"]["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
