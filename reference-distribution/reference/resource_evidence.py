"""A2: public-safe resource-assay evidence over the synthetic collection.

Runs the UNMODIFIED production ``public_resource_ingest.sync_public_resources``,
``public_resource_completion_assay.run_assay`` and
``public_resource_delivery_assay.run_assay`` (imported from ANAXI_DIR) against
a copy of the synthetic collection and a fresh isolated workspace under a
temporary root. The only post-processing is replacing the two temporary root
paths with fixed placeholders, so the evidence carries no host path.

    python a2_resource_evidence.py ANAXI_DIR COLLECTION_DIR OUT_DIR
Writes OUT_DIR/synthetic_public_resources.json and
OUT_DIR/synthetic_public_resource_delivery.json; exits non-zero unless both
whole-resource results are COMPLETE.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SOURCE_PLACEHOLDER = "<synthetic-public-source-root>"
WORKSPACE_PLACEHOLDER = "<synthetic-isolated-workspace-root>"
OUTPUTS = {
    "completion": "synthetic_public_resources.json",
    "delivery": "synthetic_public_resource_delivery.json",
}


def _render(report: dict, roots: dict[str, str]) -> str:
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    for real, placeholder in sorted(roots.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(real, placeholder)
    return text


def main(argv: list[str]) -> int:
    anaxi_dir, collection_dir, out_dir = (Path(a).resolve() for a in argv)
    sys.path.insert(0, str(anaxi_dir))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    tmp = Path(tempfile.mkdtemp(prefix="anaxi-a2-evidence-"))
    try:
        source, workspace = tmp / "source", tmp / "workspace"
        # any code path that asks for the production Workspace is redirected here
        os.environ["ANAXI_TEST_WORKSPACE_ROOT"] = str(tmp / "redirected")
        shutil.copytree(collection_dir, source)
        import public_resource_ingest as ingest
        import public_resource_completion_assay as completion
        import public_resource_delivery_assay as delivery
        sync = ingest.sync_public_resources(source, workspace)
        if not sync.get("ok", True):
            print(json.dumps(sync, indent=1), file=sys.stderr)
            return 1
        roots = {}
        for p, ph in ((source, SOURCE_PLACEHOLDER), (workspace, WORKSPACE_PLACEHOLDER)):
            roots[str(p)] = ph
            roots[os.path.realpath(p)] = ph
        reports = {"completion": completion.run_assay(source, workspace),
                   "delivery": delivery.run_assay(source, workspace)}
        out_dir.mkdir(parents=True, exist_ok=True)
        ok = True
        for kind, report in reports.items():
            (out_dir / OUTPUTS[kind]).write_text(_render(report, roots), encoding="utf-8")
            status = report["whole_resource_result"]["status"]
            print(f"{OUTPUTS[kind]}: {status}")
            ok &= status == "COMPLETE"
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
