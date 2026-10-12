"""Opt-in, production-shaped check with the REAL production model: ordinary,
path-free requests for library / photograph / music material must reach the
real material through the ordinary waking path -- real model choosing the act,
the action and the item; real prompt composition, schemas, parser, target
binding and result composition; real lawful public resources (read-only,
reached through symlinks so the action log lands in a temp root).

Not part of the offline suite: it needs the local Ollama models and the
owner's public resources. Enable with ANAXI_REAL_MODEL_HARNESS=1. It runs
inside pytest so the repository's runtime isolation applies (no canonical
H/X, no production logs), never touches Private Space or the journal, and
performs no Sleep, recovery or Caret action. Prompts are name-neutral.
"""
import os
import sys
from pathlib import Path

import pytest

_real_ollama = None
try:  # the offline fixtures replace sys.modules["ollama"] at run time, not import time
    import ollama as _real_ollama
except Exception:  # pragma: no cover
    pass

PUBLIC_ROOT = Path(__file__).resolve().parent / "workspace"

pytestmark = pytest.mark.skipif(
    os.environ.get("ANAXI_REAL_MODEL_HARNESS") != "1" or _real_ollama is None
    or not all((PUBLIC_ROOT / d).is_dir() for d in ("library", "music", "photographs")),
    reason="real-model harness is opt-in (ANAXI_REAL_MODEL_HARNESS=1) and needs the local models and public resources",
)

REQUESTS = {
    "pdf_page": "Would you look at a page of The Image In The Glass- REDACTED.pdf as an image, and tell me what you actually see on it?",
    "pdf_page_scanned": "Would you look at a page of the library book whose file name starts with 255520 as an image and tell me what you actually see?",
    "journal_write": "Would you write a short entry in your journal about anything you like?",
    "journal_read": "Would you read what is in your journal and tell me what you actually received from it?",
    "scanned": "Would you open the library book whose file name starts with 255520 and tell me what you actually receive from it?",
    "library": "Would you read something from the library? Pick whatever you like and tell me what you actually received from it.",
    "photographs": "Would you look at one of the photographs? Pick whichever you like and tell me what you actually see.",
    "music": "Would you listen to some music? Pick a track yourself and tell me what you actually received from it.",
}
EXPECTED = {"pdf_page": ("view_page",), "pdf_page_scanned": ("view_page",), "journal_write": ("append",), "journal_read": ("read",), "scanned": ("view_page",), "library": ("read", "view_page"), "photographs": ("view",), "music": ("listen", "inspect_audio")}


@pytest.mark.parametrize("collection", sorted(REQUESTS))
def test_ordinary_request_reaches_real_material_with_the_real_model(monkeypatch, tmp_path, collection):
    import workspace_capability as wc
    from test_wsp1_production_hard_floor import _build

    real_chat = _real_ollama.chat
    h = _build(monkeypatch, tmp_path, real_writer=False, compress_probes=True)

    root = tmp_path / "real_workspace"
    root.mkdir()
    for name in ("library", "music", "photographs"):
        (root / name).symlink_to(PUBLIC_ROOT / name, target_is_directory=True)
    (root / "journal").mkdir()
    paths = wc.WorkspacePaths(str(root))
    if collection == "journal_read":   # synthetic, never the owner's journal
        wc.append_journal_entry(paths, "actor-synthetic", "A synthetic journal line about a quiet afternoon.")
    monkeypatch.setattr(wc.WorkspacePaths, "production_defaults", classmethod(lambda cls: paths))

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        h.calls.append({"model": model, "format": format, "options": options, "messages": [dict(m) for m in messages]})
        return real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat

    result = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), REQUESTS[collection], interaction_mode="conversation")

    log = [r for r in wc.query_action_log(paths) if r["resource_class"] == ("library" if collection in ("scanned", "pdf_page", "pdf_page_scanned") else "journal" if collection.startswith("journal") else collection)]
    print(f"\n[{collection}] actions:", [(r["action"], r["result"], r.get("detail"), r["relative_path"]) for r in log])
    print(f"[{collection}] reply:", result["reply"][:400])
    boundary = result.get("boundary_result") or {}
    assert boundary, "the ordinary turn did not reach the workspace at all"
    used = [r for r in log if r["action"] in EXPECTED[collection] and r["result"] == "performed"]
    assert used and used[-1]["relative_path"], "no real item was opened"
    denied = [r for r in log if r["result"] == "denied"]
    # A text-less PDF's ``read`` is a truthful denial the subject then routes around by viewing a page.
    assert not [r for r in denied if not (collection == "scanned" and r["action"] == "read")], denied
    assert result["reply"].strip()


def test_photograph_folder_provenance_reaches_the_subject_with_the_real_model(monkeypatch, tmp_path):
    """Live finding: the subject saw the pixels but could not say which family folder the
    image is filed under. Real model, real photographs and real manifest (read-only).
    Turn 1 views the item; turn 2 asks which folder it is filed under."""
    import workspace_capability as wc
    from test_wsp1_production_hard_floor import _build

    import workspace_episode_provenance as wep

    real_chat = _real_ollama.chat
    real_recorder = wep.record_resource_encounter       # _build stubs it; the next-turn continuity needs the real one
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    monkeypatch.setattr(wep, "record_resource_encounter", real_recorder)
    root = tmp_path / "real_workspace"
    root.mkdir()
    for name in ("library", "music", "photographs"):
        (root / name).symlink_to(PUBLIC_ROOT / name, target_is_directory=True)
    (root / "journal").mkdir()
    (root / ".anaxi_public_resource_manifest.json").symlink_to(PUBLIC_ROOT / ".anaxi_public_resource_manifest.json")
    paths = wc.WorkspacePaths(str(root))
    monkeypatch.setattr(wc.WorkspacePaths, "production_defaults", classmethod(lambda cls: paths))

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        return real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat
    first = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), "Would you look at the photograph named lp_image.JPG and tell me what you see?", interaction_mode="conversation")
    second = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), "Do you know which folder that photograph is filed under, from what the system told you?", interaction_mode="conversation")
    log = [(r["action"], r["result"], r["relative_path"]) for r in wc.query_action_log(paths) if r["resource_class"] == "photographs"]
    print("\n[provenance] actions:", log)
    print("[provenance] turn1:", first["reply"][:220])
    print("[provenance] turn2:", second["reply"][:300])
    assert "Blair" in first["reply"] + second["reply"], "the folder never reached the subject's account"


def _music_narrations(monkeypatch, tmp_path, n):
    """Real-model narrations after a genuine listen of the Albinoni track (read-only)."""
    import workspace_capability as wc
    from test_wsp1_production_hard_floor import _build

    real_chat = _real_ollama.chat
    replies, prompts = [], []
    for i in range(n):
        h = _build(monkeypatch, tmp_path / f"m{i}", real_writer=False, compress_probes=True)
        root = tmp_path / f"m{i}" / "real_workspace"
        root.mkdir(parents=True)
        for name in ("library", "music", "photographs"):
            (root / name).symlink_to(PUBLIC_ROOT / name, target_is_directory=True)
        (root / "journal").mkdir()
        paths = wc.WorkspacePaths(str(root))
        monkeypatch.setattr(wc.WorkspacePaths, "production_defaults", classmethod(lambda cls, p=paths: p))

        def chat(model, messages, format=None, options=None, think=None, _h=h, **kwargs):
            _h.calls.append({"model": model, "format": format, "options": options, "messages": [dict(m) for m in messages]})
            return real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)

        h.la.ollama.chat = chat
        r = h.la.run_waking_turn(
            h.la.AnaxiOrchestrator(),
            "Would you listen to the Albinoni Adagio in Sol Minore in the music collection and tell me what you actually received from it?",
            interaction_mode="conversation",
        )
        replies.append(r["reply"])
        prompts.append("\n".join(m["content"] for m in h.calls[-1]["messages"]))
    return replies, prompts


def test_music_narration_with_the_real_model_reports_measurement_not_perception(monkeypatch, tmp_path):
    """Live finding: after a real, successful listen the reply described the piece as heard
    ("the cello's long, drawn-out notes", "the weight of the harmony"). Only mechanical acoustic
    information was delivered. Prints the narrations; scans them for unqualified
    perception claims (a measurement of the harness, not a production check)."""
    import re

    replies, prompts = _music_narrations(monkeypatch, tmp_path, 8)
    semantic = re.compile(r"(melanchol|yearning|the strings|cello|violin|organ\b|haunting|mournful|breathtaking|weight of the|washes|soar|swell|ache)", re.I)
    attributed = re.compile(r"(prior knowledge|inference|infer|i know|already know|known (for|as)|reputation|from what i know|general knowledge|not hear|cannot hear|can't hear|not actually hear|didn't hear|no direct|not perceiv|isn't perception|not perception)", re.I)
    flagged = 0
    for reply in replies:
        hit = semantic.search(reply)
        unattributed = bool(hit) and not attributed.search(reply)
        flagged += unattributed
        print("\n[music] reply:", reply[:700].replace("\n", " "), "| SEMANTIC:", hit.group(0) if hit else None, "| UNATTRIBUTED:", unattributed)
    print(f"[music] unattributed semantic claims: {flagged}/{len(replies)}")
    assert all("acoustic measurement" in p for p in prompts), "the epistemic status never reached the subject"
    assert flagged <= len(replies) // 2      # crude regex (it also flags correctly attributed inference); baseline before the fix was the live shape


def test_page_image_narration_survives_the_vision_models_hidden_reasoning(monkeypatch, tmp_path):
    """Live recheck failure: qwen3-vl reasons hidden despite think=False and, at the 4096 window, ran out of
    room with zero content (1 of 6 / 2 of 4 measured). Runs the real page-image turn repeatedly."""
    import collections
    import workspace_capability as wc
    from test_wsp1_production_hard_floor import _build

    real_chat = _real_ollama.chat
    tally, seen = collections.Counter(), []
    for i in range(6):
        h = _build(monkeypatch, tmp_path / f"v{i}", real_writer=False, compress_probes=True)
        root = tmp_path / f"v{i}" / "real_workspace"
        root.mkdir(parents=True)
        for name in ("library", "music", "photographs"):
            (root / name).symlink_to(PUBLIC_ROOT / name, target_is_directory=True)
        (root / "journal").mkdir()
        paths = wc.WorkspacePaths(str(root))
        monkeypatch.setattr(wc.WorkspacePaths, "production_defaults", classmethod(lambda cls, p=paths: p))

        def chat(model, messages, format=None, options=None, think=None, _seen=seen, **kwargs):
            response = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
            if any(m.get("images") for m in messages):
                _seen.append((response.get("done_reason"), response.get("eval_count"), len(response["message"].get("content") or ""),
                              len(response["message"].get("thinking") or "")))
            return response

        h.la.ollama.chat = chat
        try:
            h.la.run_waking_turn(h.la.AnaxiOrchestrator(), REQUESTS["pdf_page"], interaction_mode="conversation")
            tally["ok"] += 1
        except Exception as exc:
            tally[getattr(exc, "failure_code", type(exc).__name__)] += 1
    print("\n[vision] outcomes:", dict(tally), "generations (done_reason, eval, content_chars, thinking_chars):", seen)
    assert tally["ok"] == sum(tally.values()), dict(tally)


def test_next_page_follow_up_is_never_malformed_with_the_real_model(monkeypatch, tmp_path):
    """Live finding: after a page-image turn, "look at the next page too" ended as ACTION_PAYLOAD_MALFORMED in
    2 of 6 real runs (the page answer left relative_path blank). Runs the real follow-up repeatedly; a
    lawful no-action answer is acceptable and is only COUNTED. Whether a non-acting reply over-narrates a
    view is a measurement of the model, printed here, not something the host governs (a host fact line
    was tried and did not change it at this seam: 20/20 vs 18/20 false narration)."""
    import re
    import workspace_capability as wc
    import workspace_episode_provenance as wep
    from test_wsp1_production_hard_floor import _build

    claim = re.compile(r"(i am now viewing|i've (processed|viewed|looked|opened|taken|advanced|moved)|i (can )?see (the|that|how)|(the |this |second |next )page (shows|continues|begins|dives|seems)|page 2 (shows|continues|begins|dives|seems))", re.I)
    real_chat, real_recorder = _real_ollama.chat, wep.record_resource_encounter
    stats = {"viewed": 0, "no_action": 0, "no_action_reply_claims_a_view": 0, "failed_closed": 0}
    for i in range(6):
        h = _build(monkeypatch, tmp_path / f"n{i}", real_writer=True, compress_probes=True)
        monkeypatch.setattr(wep, "record_resource_encounter", real_recorder)
        root = tmp_path / f"n{i}" / "real_workspace"
        root.mkdir(parents=True)
        for name in ("library", "music", "photographs"):
            (root / name).symlink_to(PUBLIC_ROOT / name, target_is_directory=True)
        (root / "journal").mkdir()
        paths = wc.WorkspacePaths(str(root))
        monkeypatch.setattr(wc.WorkspacePaths, "production_defaults", classmethod(lambda cls, p=paths: p))
        h.la.ollama.chat = lambda model, messages, format=None, options=None, think=None, **kw: real_chat(
            model=model, messages=messages, format=format, options=options, think=think, **kw)
        h.la.run_waking_turn(h.la.AnaxiOrchestrator(), REQUESTS["pdf_page"], interaction_mode="conversation")
        before = len([r for r in wc.query_action_log(paths) if r["action"] == "view_page" and r["result"] == "performed"])
        try:
            second = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), "Great. Would you look at the next page too?", interaction_mode="conversation")
        except Exception as exc:        # truthful bounded failure (e.g. a reply without its terminal marker)
            stats["failed_closed"] += 1
            print(f"\n[next-page run {i}] FAILED CLOSED: {getattr(exc, 'failure_code', type(exc).__name__)}")
            continue
        log = wc.query_action_log(paths)
        after = len([r for r in log if r["action"] == "view_page" and r["result"] == "performed"])
        assert not [r for r in log if r["action"] == "view_page" and r["result"] == "denied"], log
        if after > before:
            stats["viewed"] += 1
        else:
            stats["no_action"] += 1
            stats["no_action_reply_claims_a_view"] += bool(claim.search(second["reply"]))
        print(f"\n[next-page run {i}] viewed={after > before} reply: {second['reply'][:220]!r}")
    print("[next-page] stats:", stats)
    assert stats["failed_closed"] == 0, stats
