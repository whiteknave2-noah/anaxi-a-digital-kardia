"""Regression: the owner's logical source folder of a selected item survives
selection -> resolution -> view -> waking delivery.

Live photograph checkout (2026-09-19): the pixels of ``lp_image.JPG`` reached the
subject and he described them, but asked whose folder it came from he could only
say the system exposed a file name. Family photographs are filed in per-person
folders on purpose. The ingest manifest knows the source is
``Pictures/Family/<Name>/lp_image.JPG``, but that source's bytes were already present
at the photographs root, so it was bound to that copy and the workspace path lost the
folder. Only the file name was ever delivered.
"""
import json
import os
from pathlib import Path

import pytest
from PIL import Image

import workspace_capability as wc
import workspace_direction as wd
import workspace_provenance as wprov
from test_wsp1_production_hard_floor import PRODUCTION_HUMAN_BYTES, _build, _message

MANIFEST = wprov.MANIFEST_NAME


def _manifest(paths, entries):
    resources = {
        key: {"resource_class": cls, "destination_relative_path": dest, "sha256": "x"}
        for key, (cls, dest) in entries.items()
    }
    Path(paths.root, MANIFEST).write_text(json.dumps({"version": 1, "resources": resources}), encoding="utf-8")


def _image(path, color=(200, 30, 30)):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(path)


def test_root_copy_bound_to_a_family_folder_reports_that_folder(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    paths.ensure_exists()
    _image(Path(paths.photographs_dir, "lp_image.png"))
    _image(Path(paths.photographs_dir, "Family", "Alex", "me.png"), (1, 2, 3))
    _manifest(paths, {
        "Pictures/Family/Blair/lp_image.png": ("photographs", "lp_image.png"),
        "Pictures/Family/Alex/me.png": ("photographs", "Family/Alex/me.png"),
        "Pictures/Family/Alex/me copy.png": ("photographs", "Family/Alex/me.png"),
    })
    block = wprov.resource_provenance(paths, "photographs", "lp_image.png")
    assert block["source_folder"] == "Family/Blair" and block["folder_name"] == "Blair"
    assert "not an identification" in block["note"]
    nested = wprov.resource_provenance(paths, "photographs", "Family/Alex/me.png")
    assert nested["folder_name"] == "Alex" and nested["also_filed_as"] == ["me copy.png"]
    assert wprov.resource_provenance(paths, "library", "welcome.txt") is None      # flat item: nothing to add


def test_identical_bytes_filed_under_two_folders_report_both(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    paths.ensure_exists()
    _image(Path(paths.photographs_dir, "shared.png"))
    _manifest(paths, {"Pictures/Family/A/shared.png": ("photographs", "shared.png"),
                      "Pictures/Family/B/shared.png": ("photographs", "shared.png")})
    assert wprov.resource_provenance(paths, "photographs", "shared.png")["source_folder"] == ["Family/A", "Family/B"]


def test_listing_shows_the_filing_folder_of_an_entry_whose_path_hides_it(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    paths.ensure_exists()
    _image(Path(paths.photographs_dir, "lp_image.png"))
    _image(Path(paths.photographs_dir, "Family", "Alex", "me.png"))
    _manifest(paths, {"Pictures/Family/Blair/lp_image.png": ("photographs", "lp_image.png"),
                      "Pictures/Family/Alex/me.png": ("photographs", "Family/Alex/me.png")})
    listing, performed = wd.execute_workspace_action(
        paths, {"resource_class": "photographs", "action": "list", "relative_path": "", "content": ""}, "actor")
    assert performed and listing["result"]["entry_folders"] == {"lp_image.png": "Family/Blair"}
    inside, _ = wd.execute_workspace_action(
        paths, {"resource_class": "photographs", "action": "list", "relative_path": "Family/Alex", "content": ""}, "actor")
    assert "entry_folders" not in inside["result"]                    # path already says it


def test_view_result_carries_the_provenance_and_never_the_image_bytes(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    paths.ensure_exists()
    _image(Path(paths.photographs_dir, "lp_image.png"))
    _manifest(paths, {"Pictures/Family/Blair/lp_image.png": ("photographs", "lp_image.png")})
    boundary, performed = wd.execute_workspace_action(
        paths, {"resource_class": "photographs", "action": "view", "relative_path": "lp_image.png", "content": ""}, "actor")
    assert performed
    assert boundary["result"]["resource_provenance"]["source_folder"] == "Family/Blair"
    json.dumps(boundary)                                              # still JSON-safe for roaming
    wd.get_and_clear_last_view_image_bytes()


def test_ordinary_waking_delivers_the_folder_to_the_subject_with_the_pixels(monkeypatch, tmp_path):
    """The real seam: ordinary waking -> photograph view -> Pass 2 prompt. The folder is in what
    the subject is actually given, next to the real pixels."""
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action={
        "resource_class": "photographs", "action": "view", "relative_path": "lp_image.png", "content": "",
    })
    _image(Path(h.paths.photographs_dir, "lp_image.png"))
    _manifest(h.paths, {"Pictures/Family/Blair/lp_image.png": ("photographs", "lp_image.png")})
    images = []
    inner = h.la.ollama.chat

    def capture(model, messages, **kwargs):
        images.extend(m["images"] for m in messages if m.get("images"))
        return inner(model, messages, **kwargs)

    h.la.ollama.chat = capture
    result = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation")

    assert images, "real pixels must still reach the vision model"
    pass2 = "\n".join(m["content"] for m in h.calls[-1]["messages"])
    assert "'source_folder': 'Family/Blair'" in pass2 and "'folder_name': 'Blair'" in pass2
    assert "not an identification of anyone" in pass2
    assert result["boundary_result"]["result"]["resource_provenance"]["folder_name"] == "Blair"


def test_real_public_manifest_resolves_the_live_case_when_present():
    root = Path(__file__).resolve().parent / "workspace"
    if not (root / MANIFEST).is_file() or not (root / "photographs" / "lp_image.JPG").is_file():
        pytest.skip("live public manifest not present")
    paths = wc.WorkspacePaths(str(root))
    assert wprov.resource_provenance(paths, "photographs", "lp_image.JPG")["source_folder"] == "Family/Blair"


def test_the_folder_survives_into_the_next_waking_turns_encounter_continuity(monkeypatch, tmp_path):
    """The live shape: turn 1 views the photograph; the person's NEXT message (turn 2) asks about
    it. Turn 2's waking is only told what the encounter-continuity carries, and that used to be
    class/path/action -- 'lp_image.JPG' and nothing about the folder."""
    import workspace_episode_provenance as wep

    real_recorder = wep.record_resource_encounter          # _build stubs it; this test needs the real one
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action={
        "resource_class": "photographs", "action": "view", "relative_path": "lp_image.png", "content": "",
    })
    monkeypatch.setattr(wep, "record_resource_encounter", real_recorder)
    import od1_schema_migration  # noqa: F401  (schema parity with production DBs)
    _image(Path(h.paths.photographs_dir, "lp_image.png"))
    _manifest(h.paths, {"Pictures/Family/Blair/lp_image.png": ("photographs", "lp_image.png")})

    h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation")

    inner = h.la.ollama.chat
    plain_act = {"act": "develop_current", "thread": "t", "direction_request": "none", "relinquish_direction": False}

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        probe = bool(options and options.get("num_predict") == 1)
        if isinstance(format, dict) and "act" in format.get("properties", {}) and not probe:
            return {"message": {"content": json.dumps(plain_act)}, "prompt_eval_count": 1, "done": True,
                    "done_reason": "stop", "eval_count": 20}
        if format is None and not probe:
            h.calls.append({"format": None, "options": options, "messages": [dict(m) for m in messages]})
            return {"message": {"content": "Understood."}, "prompt_eval_count": 1,
                    "done": True, "done_reason": "stop", "eval_count": 5}
        return inner(model, messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat
    h.la.run_waking_turn(
        h.la.AnaxiOrchestrator(), "Do you know which folder that photograph is filed under?", interaction_mode="conversation",
    )
    text = "\n".join(m["content"] for m in h.calls[-1]["messages"])
    assert "photographs/lp_image.png" in text                              # the pre-existing continuity fact
    assert "under the folder Family/Blair" in text
    assert "not an identification of anyone or anything in it" in text
