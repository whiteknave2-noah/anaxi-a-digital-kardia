"""Regression: an ordinary request that names no item ("read something from
the library", "view a photograph", "listen to some music") must complete
through the ordinary waking path WITHOUT the owner supplying a raw host path.

Live incident (2026-09-19 ~02:34Z, session 01M2VR48E8RDCKM4Q9QM74A7Q4, three
consecutive turns): the subject's workspace Pass 1 answered ``read`` /
``view`` / ``listen`` with an EMPTY ``relative_path`` -- it had never been shown
what the collections hold -- and the host refused ("Empty path."). Music was
additionally mislabeled ``AUDIO_MALFORMED`` although no audio file had been
opened at all. The lower layers (dispatcher, reader, decoder) all worked on a
VALID path; the defect lived at the seam before that. These tests therefore
start at ordinary waking and follow the real chain: entry -> typed act ->
workspace Pass 1 (empty target) -> host listing -> subject selection -> real
operation -> Pass 2 delivery -> canonical X.
"""
import json
import os
from pathlib import Path

import numpy as np
import pytest

import workspace_capability as wc
import workspace_discovery
import workspace_direction as wd
from test_wsp1_production_hard_floor import PRODUCTION_HUMAN_BYTES, _build, _message

EMPTY = {"resource_class": "library", "action": "read", "relative_path": "", "content": ""}


def _script(h, choices):
    """Serve ``choices`` (dicts) in order to the structured-control calls the
    workspace stage makes (Pass 1, then any selections); everything else keeps
    the production-shaped fake."""
    inner = h.la.ollama.chat
    remaining = list(choices)
    served = []

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        is_probe = bool(options and options.get("num_predict") == 1)
        if format == "json" and not is_probe and remaining:
            value = remaining.pop(0)
            if callable(value):
                value = value([dict(m) for m in messages])
            h.calls.append({"format": format, "options": options, "messages": [dict(m) for m in messages]})
            served.append(value)
            return {"message": {"content": json.dumps(value)}, "prompt_eval_count": 1,
                    "done": True, "done_reason": "stop", "eval_count": 20}
        return inner(model, messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat
    h.served = served
    return h


def _run(h, human_bytes=PRODUCTION_HUMAN_BYTES):
    return h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(human_bytes), interaction_mode="conversation")


def _log(h):
    return wc.query_action_log(h.paths)


def _pass2_text(h):
    return "\n".join(m["content"] for m in h.calls[-1]["messages"])


def test_empty_library_target_completes_by_listing_then_subject_choice(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, action=EMPTY)
    Path(h.paths.library_dir, "another.txt").write_text("Not chosen.", encoding="utf-8")
    _script(h, [EMPTY, {"resource_class": "library", "action": "read", "relative_path": "chosen.txt", "content": ""}])

    result = _run(h)

    assert result["reply"] == h.expression["expression"]
    assert result["boundary_result"]["result"]["content"] == h.source_text
    assert h.source_text in _pass2_text(h)
    # The subject was actually SHOWN the listing before it chose.
    selection_prompt = "\n".join(m["content"] for m in h.workspace_calls()[1]["messages"])
    assert "chosen.txt" in selection_prompt and "another.txt" in selection_prompt
    # The host listed (read-only) and then performed the read; it never read anything itself.
    actions = [(r["action"], r["result"]) for r in _log(h)]
    assert ("list", "performed") in actions and ("read", "performed") in actions
    assert not [r for r in _log(h) if r["result"] == "denied"]
    assert "the item acted on is the one you chose" in _pass2_text(h)
    assert h.budget_results["wsp1_select"].fits and h.budget_results["wsp1_pass2"].fits
    trace = h.la.get_last_conversation_direction_trace()
    assert trace["pass1_status"] == trace["pass2_status"] == "ok"


def test_host_never_chooses_when_the_subject_still_names_nothing(monkeypatch, tmp_path):
    """Selection returns an empty target again: nothing is opened, no item is
    substituted, and the subject still receives the real listing plus a
    truthful receipt -- the turn completes, it is not a system error."""
    h = _build(monkeypatch, tmp_path, real_writer=True, action=EMPTY)
    _script(h, [EMPTY, EMPTY])

    result = _run(h)

    assert result["reply"] == h.expression["expression"]
    assert not [r for r in _log(h) if r["action"] == "read"]
    text = _pass2_text(h)
    assert "Action requested (not performed)" in text
    assert "TARGET_NOT_SELECTED" in text and "No item was opened" in text
    assert "chosen.txt" in text                     # the listing was still delivered
    assert h.source_text not in text                # the host never opened a file for the subject
    assert result["boundary_result"]["consequence"] == "No item was opened; source remains unchanged."


def test_malformed_selection_ends_truthfully_without_fabricated_state(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, action=EMPTY)
    _script(h, [EMPTY, {"resource_class": "library", "action": "read"}])   # missing fields

    result = _run(h)

    assert not [r for r in _log(h) if r["action"] == "read"]
    assert "TARGET_NOT_SELECTED (MALFORMED_ACTION)" in _pass2_text(h)
    assert result["reply"] == h.expression["expression"]


def test_invented_path_is_treated_as_unknown_and_goes_to_discovery(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=False, action=EMPTY)
    invented = {"resource_class": "library", "action": "read", "relative_path": "a book i imagined.pdf", "content": ""}
    _script(h, [invented, {"resource_class": "library", "action": "read", "relative_path": "chosen.txt", "content": ""}])

    result = _run(h)

    assert result["boundary_result"]["result"]["content"] == h.source_text
    assert "the named item does not exist" in "\n".join(m["content"] for m in h.workspace_calls()[1]["messages"])


def test_class_prefixed_path_resolves_without_any_discovery(monkeypatch, tmp_path):
    """"library/chosen.txt" names the item; the redundant class segment must not
    make it unopenable, and it needs no listing step."""
    h = _build(monkeypatch, tmp_path, real_writer=False, action={
        "resource_class": "library", "action": "read", "relative_path": "library/chosen.txt", "content": "",
    })

    result = _run(h)

    assert result["boundary_result"]["result"]["content"] == h.source_text
    assert len(h.workspace_calls()) == 1
    assert not [r for r in _log(h) if r["action"] == "list"]


def test_listing_the_class_by_its_own_name_lists_the_root(monkeypatch, tmp_path):
    """A live roaming action listed directory "library" and failed
    LIST_NOT_DIRECTORY; the class name is not a subdirectory."""
    h = _build(monkeypatch, tmp_path, real_writer=False, action={
        "resource_class": "library", "action": "list", "relative_path": "library", "content": "",
    })

    result = _run(h)

    assert result["boundary_result"]["result"]["entries"] == ["chosen.txt"]


def test_path_escape_is_refused_and_never_redirected_to_discovery(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=False, action={
        "resource_class": "library", "action": "read", "relative_path": "../../etc/hosts", "content": "",
    })

    result = _run(h)

    assert len(h.workspace_calls()) == 1
    assert not [r for r in _log(h) if r["action"] == "list"]
    assert [r for r in _log(h) if r["result"] == "denied"]
    assert result["boundary_result"]["result"] is None


def test_photograph_with_no_target_is_chosen_from_a_listing_and_pixels_arrive(monkeypatch, tmp_path):
    from PIL import Image

    h = _build(monkeypatch, tmp_path, real_writer=False, compress_probes=True, action={
        "resource_class": "photographs", "action": "view", "relative_path": "", "content": "",
    })
    Path(h.paths.photographs_dir, "Family").mkdir()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(Path(h.paths.photographs_dir, "Family", "red.png"))
    view_empty = {"resource_class": "photographs", "action": "view", "relative_path": "", "content": ""}
    _script(h, [
        view_empty,
        {"resource_class": "photographs", "action": "list", "relative_path": "Family/", "content": ""},
        {"resource_class": "photographs", "action": "view", "relative_path": "Family/red.png", "content": ""},
    ])
    images = []
    inner = h.la.ollama.chat

    def capture(model, messages, **kwargs):
        for m in messages:
            if m.get("images"):
                images.append(m["images"])
        return inner(model, messages, **kwargs)

    h.la.ollama.chat = capture

    result = _run(h)

    assert result["boundary_result"]["result"]["name"] == "red.png"
    assert images, "real pixels must reach the vision model"
    selection_prompts = ["\n".join(m["content"] for m in c["messages"]) for c in h.workspace_calls()[1:]]
    assert "Family/" in selection_prompts[0]                 # a directory was offered
    assert "red.png" in selection_prompts[1]                 # and navigated into by the subject
    assert [r["relative_path"] for r in _log(h) if r["action"] == "view"] == ["Family/red.png"]


def test_music_with_no_target_is_chosen_from_a_listing_and_decodes(monkeypatch, tmp_path):
    import soundfile as sf

    h = _build(monkeypatch, tmp_path, real_writer=False, action={
        "resource_class": "music", "action": "listen", "relative_path": "", "content": "",
    })
    tone = np.sin(2 * np.pi * 440 * np.arange(22050 * 2) / 22050).astype("float32") * 0.4
    sf.write(os.path.join(h.paths.music_dir, "tone.wav"), tone, 22050, format="WAV")
    _script(h, [
        {"resource_class": "music", "action": "listen", "relative_path": "", "content": ""},
        {"resource_class": "music", "action": "listen", "relative_path": "tone.wav", "content": ""},
    ])

    result = _run(h)

    listen = result["boundary_result"]["result"]
    assert listen["duration_seconds"] > 1.9 and listen["sample_rate_hz"] == 22050
    assert not [r for r in _log(h) if r["result"] == "denied"]


def test_music_target_defects_are_never_reported_as_malformed_audio(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    paths.ensure_exists()
    Path(paths.music_dir, "Album").mkdir()
    for relative_path, expected in (
        ("", wc.TARGET_NOT_SPECIFIED),
        ("missing.mp3", wc.TARGET_NOT_FOUND),
        ("Album", wc.TARGET_IS_DIRECTORY),
        ("../escape.mp3", wc.TARGET_OUTSIDE_ROOT),
    ):
        result, failure = wc.access_music_listen(paths, relative_path)
        assert result is None and failure["rationale"] == expected, relative_path
    # A damaged file that DOES exist is still (truthfully) a decode failure.
    Path(paths.music_dir, "broken.mp3").write_bytes(b"this is not audio at all")
    _result, failure = wc.access_music_listen(paths, "broken.mp3")
    assert failure["rationale"] in (wc.wa.AUDIO_MALFORMED, wc.wa.AUDIO_DECODER_UNAVAILABLE)


def test_large_collection_selection_is_windowed_and_pageable(monkeypatch, tmp_path):
    """151 long track names cannot ride one selection prompt. The subject is
    shown a real window that says how many exist, can turn the page with the
    continuation the host actually delivered, and can choose from page two --
    the first window is not a permanent hidden subset."""
    import re

    h = _build(monkeypatch, tmp_path, real_writer=False, compress_probes=True, action={
        "resource_class": "music", "action": "listen", "relative_path": "", "content": "",
    })
    names = [f"{i:03d}. Some Composer - A Reasonably Long Track Title Number {i:03d}.mp3" for i in range(151)]
    for name in names:
        Path(h.paths.music_dir, name).write_bytes(b"")

    def turn_the_page(messages):
        prompt = "\n".join(m["content"] for m in messages)
        request = re.search(r"'next_request': '(\{[^']*\})'", prompt).group(1)
        return {"resource_class": "music", "action": "list", "relative_path": "", "content": request}

    def pick_from_page_two(messages):
        prompt = "\n".join(m["content"] for m in messages)
        entry = re.search(r"'entries': \['([^']*)'", prompt).group(1)
        return {"resource_class": "music", "action": "listen", "relative_path": entry, "content": ""}

    _script(h, [
        {"resource_class": "music", "action": "listen", "relative_path": "", "content": ""},
        turn_the_page, pick_from_page_two,
    ])
    _run(h)

    first_prompt = "\n".join(m["content"] for m in h.workspace_calls()[1]["messages"])
    assert "'total_count': 151" in first_prompt and "next_request" in first_prompt
    assert h.budget_results["wsp1_select"].fits
    shown_first = re.search(r"'entries': \[(.*?)\]", first_prompt).group(1).count(".mp3")
    assert 0 < shown_first < 151                       # a real, truthfully partial window
    listened = [r["relative_path"] for r in _log(h) if r["action"] == "listen"]
    assert len(listened) == 1 and listened[0] in names
    assert names.index(listened[0]) >= shown_first     # chosen from the second page


def test_selection_is_never_asked_when_it_cannot_be_made_honestly(monkeypatch, tmp_path):
    """If even the fixed text of the selection call cannot fit, the subject is
    not asked to choose from nothing, and the host chooses nothing."""
    h = _build(monkeypatch, tmp_path, real_writer=False, action=EMPTY)
    import context_budget
    monkeypatch.setattr(context_budget, "WSP1_PASS1_MAX_PROMPT_BUDGET", 40)
    listing = {
        "action": "list", "scope": "local_workspace/library", "boundary": "b", "consequence": "c", "rationale": "r",
        "result": {"entries": ["a"], "returned_count": 1, "total_count": 1, "truncated": False,
                   "has_more": False, "start_index": 0},
    }
    with pytest.raises(workspace_discovery.SelectionUnavailable):
        h.supervisor._ask_target_selection(
            {}, "core " * 50, [], "hello", None, listing, EMPTY, wc.TARGET_NOT_SPECIFIED, wd.LIVE_ALLOWED_SURFACE,
        )


def test_discovery_needed_matrix(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    paths.ensure_exists()
    Path(paths.library_dir, "book.txt").write_text("x", encoding="utf-8")
    Path(paths.library_dir, "Shelf").mkdir()
    (Path(paths.journal_dir) / "entry-1.json").write_text('{"content":"c"}', encoding="utf-8")

    def needed(resource_class, action, path):
        return workspace_discovery.discovery_needed(
            paths, {"resource_class": resource_class, "action": action, "relative_path": path, "content": ""},
        )[:2]

    assert needed("library", "read", "") == (True, wc.TARGET_NOT_SPECIFIED)
    assert needed("library", "read", "   ") == (True, wc.TARGET_NOT_SPECIFIED)
    assert needed("library", "read", "nope.pdf") == (True, wc.TARGET_NOT_FOUND)
    assert needed("library", "read", "Shelf") == (True, wc.TARGET_IS_DIRECTORY)
    assert needed("library", "read", "book.txt")[0] is False
    assert needed("library", "read", "library/book.txt")[0] is False
    assert needed("library", "read", "../book.txt") == (False, wc.TARGET_OUTSIDE_ROOT)
    assert needed("library", "list", "")[0] is False            # lists name a directory, not an item
    assert needed("journal", "read", "entry-1")[0] is False
    assert needed("journal", "read", "entry-1.json")[0] is False
    assert needed("journal", "read", "")[0] is True
    assert needed("journal", "append", "")[0] is False


def test_subject_is_never_asked_to_choose_from_a_listing_it_was_not_shown(monkeypatch, tmp_path):
    """Worst case: real measurement is unavailable (probes assume no token
    compression), so the selection prompt has no room for even one entry. The
    subject must not be asked to choose from a withheld listing, the host must
    not choose for it, and the turn still ends truthfully."""
    h = _build(monkeypatch, tmp_path, real_writer=False, action={
        "resource_class": "music", "action": "listen", "relative_path": "", "content": "",
    })
    for i in range(151):
        Path(h.paths.music_dir, f"{i:03d}. Some Composer - A Reasonably Long Track Title Number {i:03d}.mp3").write_bytes(b"")
    _script(h, [{"resource_class": "music", "action": "listen", "relative_path": "", "content": ""}])

    result = _run(h)

    assert len(h.workspace_calls()) == 1                    # no selection call was made
    assert not [r for r in _log(h) if r["action"] == "listen"]
    assert "TARGET_NOT_SELECTED (LISTING_NOT_DELIVERED)" in _pass2_text(h)
    assert result["reply"] == h.expression["expression"]


def _scanned_pdf(h, name="scan.pdf"):
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(os.path.join(h.paths.library_dir, name), "wb") as f:
        writer.write(f)
    return name


@pytest.mark.skipif(not os.path.exists(wc.PDF_PAGE_RENDERER_BIN), reason="PDF page renderer not built")
def test_scanned_pdf_read_offers_the_page_image_route_and_pixels_arrive(monkeypatch, tmp_path):
    """A text-less PDF cannot be read as text. The subject is offered one
    neutral follow-up (view a page, or stop) instead of the turn ending on the
    failure; it chooses the page and real pixels reach the vision model."""
    h = _build(monkeypatch, tmp_path, real_writer=False, compress_probes=True)
    name = _scanned_pdf(h)
    _script(h, [
        {"resource_class": "library", "action": "read", "relative_path": name, "content": ""},
        {"resource_class": "library", "action": "view_page", "relative_path": name, "content": '{"page": 1}'},
    ])
    images = []
    inner = h.la.ollama.chat

    def capture(model, messages, **kwargs):
        images.extend(m["images"] for m in messages if m.get("images"))
        return inner(model, messages, **kwargs)

    h.la.ollama.chat = capture

    result = _run(h)

    assert images, "real page pixels must reach the vision model"
    assert result["boundary_result"]["action"] == "view_page"
    assert "found no text layer" in _pass2_text(h)
    offered = "\n".join(m["content"] for m in h.workspace_calls()[1]["messages"])
    assert "PDF_TEXT_UNAVAILABLE" in offered and "view_page" in offered
    assert [r["action"] for r in _log(h) if r["resource_class"] == "library"] == ["read", "view_page"]


def test_scanned_pdf_subject_may_stop_and_receives_the_truthful_failure(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=False)
    name = _scanned_pdf(h)
    _script(h, [
        {"resource_class": "library", "action": "read", "relative_path": name, "content": ""},
        {"resource_class": "library", "action": "none", "relative_path": "", "content": ""},
    ])

    result = _run(h)

    assert result["boundary_result"]["action"] == "read" and result["boundary_result"]["result"] is None
    assert "PDF_TEXT_UNAVAILABLE" in _pass2_text(h)
    assert [r["action"] for r in _log(h) if r["resource_class"] == "library"] == ["read"]
    assert result["reply"] == h.expression["expression"]


def test_subject_listing_is_followed_by_a_neutral_choice_and_may_stop(monkeypatch, tmp_path):
    """The subject chose ``list`` itself (the real model's usual first move).
    It is offered the chance to act on an item -- and may equally stop; a
    stop is a lawful outcome, not an error, and nothing is opened for it."""
    h = _build(monkeypatch, tmp_path, real_writer=True, action={
        "resource_class": "library", "action": "list", "relative_path": "", "content": "",
    })
    _script(h, [
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
        {"resource_class": "library", "action": "none", "relative_path": "", "content": ""},
    ])

    result = _run(h)

    assert result["boundary_result"]["result"]["entries"] == ["chosen.txt"]
    assert not [r for r in _log(h) if r["action"] == "read"]
    assert "To stop here" in "\n".join(m["content"] for m in h.workspace_calls()[1]["messages"])
    assert result["reply"] == h.expression["expression"]


def test_subject_listing_then_choosing_an_item_reads_it(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True)
    _script(h, [
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
        {"resource_class": "library", "action": "read", "relative_path": "chosen.txt", "content": ""},
    ])

    result = _run(h)

    assert result["boundary_result"]["result"]["content"] == h.source_text
    assert "You listed this resource earlier in this turn and then chose the item acted on" in _pass2_text(h)


def _plain_pass2(h, text):
    """Answer the workspace narration call (plain generation, not JSON-format,
    not a measurement probe) with ``text``."""
    inner = h.la.ollama.chat

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        if format is None and not (options and options.get("num_predict") == 1) and not any(m.get("images") for m in messages):
            return {"message": {"content": text}, "prompt_eval_count": 1, "done": True,
                    "done_reason": "stop", "eval_count": 20}
        return inner(model, messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat


def test_workspace_narration_accepts_plain_prose_with_quotes_and_the_marker(monkeypatch, tmp_path):
    """Real model: a JSON envelope around free prose died as MALFORMED_EXPRESSION in ~1 of 8
    turns (an unescaped quotation mark), after the action had already run."""
    h = _build(monkeypatch, tmp_path, real_writer=True, action={
        "resource_class": "library", "action": "read", "relative_path": "chosen.txt", "content": "",
    })
    prose = 'I "read" the file and it said what it said.'
    _plain_pass2(h, prose)

    result = _run(h)

    assert result["reply"] == prose
    assert h.la.PASS2_COMPLETION_MARKER not in result["reply"]


def test_a_blank_workspace_narration_is_expression_not_established(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, action={
        "resource_class": "library", "action": "read", "relative_path": "chosen.txt", "content": "",
    })
    _plain_pass2(h, "   ")
    import conversation_direction as cd_

    with pytest.raises(cd_.ConversationDirectionFailure) as caught:
        _run(h)
    assert caught.value.failure_code == "EMPTY_EXPRESSION"


def _multi_page_pdf(h, name="scan3.pdf", pages=3):
    import pypdf

    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(os.path.join(h.paths.library_dir, name), "wb") as f:
        writer.write(f)
    return name


def _capture_images(h):
    images = []
    inner = h.la.ollama.chat

    def capture(model, messages, **kwargs):
        images.extend(m["images"] for m in messages if m.get("images"))
        return inner(model, messages, **kwargs)

    h.la.ollama.chat = capture
    return images


@pytest.mark.skipif(not os.path.exists(wc.PDF_PAGE_RENDERER_BIN), reason="PDF page renderer not built")
@pytest.mark.parametrize("first_content", ["", "{}", '{"page": 99}', "not json"])
def test_view_page_without_a_valid_page_is_completed_by_the_subjects_choice_and_pixels_arrive(monkeypatch, tmp_path, first_content):
    """Live scanned-PDF checkout: the subject selected the document and chose view_page but left the page
    out; the executor called that a malformed payload and no page image reached waking. The host now
    states the facts (how many pages) and the SUBJECT chooses the page. Real renderer, real pixels."""
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    name = _multi_page_pdf(h)
    images = _capture_images(h)
    _script(h, [
        {"resource_class": "library", "action": "view_page", "relative_path": name, "content": first_content},
        {"resource_class": "library", "action": "view_page", "relative_path": name, "content": '{"page": 2}'},
    ])

    result = _run(h)

    chosen = result["boundary_result"]["result"]
    assert chosen["page_number"] == 2 and chosen["page_count"] == 3          # the SUBJECT's page, not the host's
    assert json.loads(chosen["next_request"]) == {"page": 3}                  # continuation offered
    assert images, "real page pixels must reach the vision model"
    asked = "\n".join(m["content"] for m in h.workspace_calls()[1]["messages"])
    assert "'page_count': 3" in asked and "PARAMETER_NOT_CHOSEN" not in result["boundary_result"]["rationale"]
    assert [(r["action"], r["result"]) for r in _log(h) if r["resource_class"] == "library"] == [("view_page", "performed")]


@pytest.mark.skipif(not os.path.exists(wc.PDF_PAGE_RENDERER_BIN), reason="PDF page renderer not built")
def test_a_page_choice_that_leaves_relative_path_blank_stays_on_the_item_the_subject_chose(monkeypatch, tmp_path):
    """Live real-model finding ("next page"): the subject answered the page question with
    {"relative_path": "", "content": "2"}. The question asks only for the page and the item was the
    subject's own earlier choice, so the blank names no other item -- it must not end as a malformed payload."""
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    name = _multi_page_pdf(h)
    images = _capture_images(h)
    _script(h, [
        {"resource_class": "library", "action": "view_page", "relative_path": name, "content": ""},
        {"resource_class": "library", "action": "view_page", "relative_path": "", "content": "2"},
    ])

    result = _run(h)

    assert result["boundary_result"]["result"]["page_number"] == 2 and images
    assert [(r["action"], r["result"]) for r in _log(h) if r["resource_class"] == "library"] == [("view_page", "performed")]


def test_a_page_choice_naming_a_different_item_is_still_not_accepted(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    name = _multi_page_pdf(h)
    _script(h, [
        {"resource_class": "library", "action": "view_page", "relative_path": name, "content": ""},
        {"resource_class": "library", "action": "view_page", "relative_path": "someother.pdf", "content": "2"},
    ])

    result = _run(h)

    assert result["boundary_result"]["result"] is None
    assert not [r for r in _log(h) if r["result"] == "performed" and r["action"] == "view_page"]


@pytest.mark.skipif(not os.path.exists(wc.PDF_PAGE_RENDERER_BIN), reason="PDF page renderer not built")
def test_the_subject_may_decline_to_choose_a_page_and_receives_the_truthful_failure(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    name = _multi_page_pdf(h)
    images = _capture_images(h)
    _script(h, [
        {"resource_class": "library", "action": "view_page", "relative_path": name, "content": ""},
        {"resource_class": "library", "action": "none", "relative_path": "", "content": ""},
    ])

    result = _run(h)

    assert not images and result["boundary_result"]["result"] is None
    assert [r for r in _log(h) if r["action"] == "view_page" and r["result"] == "performed"] == []


def test_the_host_never_picks_the_page_when_the_subject_answers_wrongly(monkeypatch, tmp_path):
    """A changed action, or still no page, is not rescued by the host choosing one."""
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    name = _multi_page_pdf(h)
    _script(h, [
        {"resource_class": "library", "action": "view_page", "relative_path": name, "content": ""},
        {"resource_class": "library", "action": "view_page", "relative_path": name, "content": ""},
    ])

    result = _run(h)

    assert result["boundary_result"]["result"] is None
    assert not [r for r in _log(h) if r["result"] == "performed" and r["action"] == "view_page"]


def test_inspect_audio_without_a_view_is_completed_by_the_subjects_choice(monkeypatch, tmp_path):
    import soundfile as sf

    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    tone = np.sin(2 * np.pi * 440 * np.arange(22050 * 2) / 22050).astype("float32") * 0.4
    sf.write(os.path.join(h.paths.music_dir, "tone.wav"), tone, 22050, format="WAV")
    _script(h, [
        {"resource_class": "music", "action": "inspect_audio", "relative_path": "tone.wav", "content": ""},
        {"resource_class": "music", "action": "inspect_audio", "relative_path": "tone.wav", "content": '{"view": "dynamics"}'},
    ])

    result = _run(h)

    assert result["boundary_result"]["result"]["view"] == "dynamics"
    assert "available_views" in "\n".join(m["content"] for m in h.workspace_calls()[1]["messages"])


@pytest.mark.skipif(not os.path.exists(wc.PDF_PAGE_RENDERER_BIN), reason="PDF page renderer not built")
def test_a_bare_page_number_is_the_page_the_real_model_actually_writes(monkeypatch, tmp_path):
    """Real model, asked for a page, answers content "1" -- not {"page": 1}. That is unambiguous and must
    work, both as the first choice (no discovery step at all) and as the answer to the page question."""
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    name = _multi_page_pdf(h)
    images = _capture_images(h)
    _script(h, [{"resource_class": "library", "action": "view_page", "relative_path": name, "content": "2"}])

    result = _run(h)

    assert result["boundary_result"]["result"]["page_number"] == 2 and images
    assert len(h.workspace_calls()) == 1                       # no extra question was needed


def test_page_and_view_payload_parsers_accept_only_unambiguous_bare_forms():
    import workspace_audio as wa_

    assert wd.parse_pdf_page_content("3") == ({"page": 3}, None)
    assert wd.parse_pdf_page_content('{"page": 3}') == ({"page": 3}, None)
    for bad in ("0", "-1", "true", '"3"', "page 3", "3.5", "[3]", ""):
        assert wd.parse_pdf_page_content(bad)[1] is not None, bad
    assert wa_.parse_inspect_audio_payload("dynamics")[0]["view"] == "dynamics"
    for bad in ("Dynamics", "the dynamics view", "loud", ""):
        assert wa_.parse_inspect_audio_payload(bad)[1] is not None, bad


@pytest.mark.skipif(not os.path.exists(wc.PDF_PAGE_RENDERER_BIN), reason="PDF page renderer not built")
def test_continuation_the_next_turn_is_told_which_page_was_delivered(monkeypatch, tmp_path):
    """A later 'the next page, please' needs to know where the last delivery stopped."""
    import workspace_episode_provenance as wep

    real_recorder = wep.record_resource_encounter
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    monkeypatch.setattr(wep, "record_resource_encounter", real_recorder)
    name = _multi_page_pdf(h)
    _script(h, [{"resource_class": "library", "action": "view_page", "relative_path": name, "content": "2"}])
    _run(h)

    inner = h.la.ollama.chat
    plain_act = {"act": "develop_current", "thread": "t", "direction_request": "none", "relinquish_direction": False}

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        probe = bool(options and options.get("num_predict") == 1)
        if isinstance(format, dict) and "act" in format.get("properties", {}) and not probe:
            return {"message": {"content": json.dumps(plain_act)}, "prompt_eval_count": 1, "done": True,
                    "done_reason": "stop", "eval_count": 20}
        if format is None and not probe:
            h.calls.append({"format": None, "options": options, "messages": [dict(m) for m in messages]})
            return {"message": {"content": "Yes."}, "prompt_eval_count": 1,
                    "done": True, "done_reason": "stop", "eval_count": 5}
        return inner(model, messages, format=format, options=options, think=think, **kwargs)

    h.la.ollama.chat = chat
    h.la.run_waking_turn(h.la.AnaxiOrchestrator(), "Please show the next page.", interaction_mode="conversation")
    text = "\n".join(m["content"] for m in h.calls[-1]["messages"])
    assert f"library/{name}" in text and "PDF page 2 of 3" in text


def test_a_named_but_unresolved_item_narrows_the_listing_to_what_was_named(tmp_path):
    """Live 2026-09-24: "255520" (the start of one scanned book's file name) did not resolve; the host listed
    the whole library and the subject picked the book the conversation was about (3/3).  The listing is now
    narrowed to entries containing what was named; the subject still chooses.  Countermodels: nothing named
    and a name matching nothing both list exactly as before."""
    import workspace_discovery as disc
    paths = wc.WorkspacePaths(str(tmp_path / "ws"))
    paths.ensure_exists()
    for name in ("255520-little-house.pdf", "The Hobbit.pdf", "Other.pdf"):
        Path(paths.library_dir, name).write_text("x", encoding="utf-8")
    act = disc.discovery_list_action(paths, {"resource_class": "library", "action": "read", "relative_path": "255520", "content": ""})
    assert json.loads(act["content"]) == {"query": "255520"}
    listing, ok = wd.execute_workspace_action(paths, act, "actor-clark")
    assert ok and listing["result"]["entries"] == ["255520-little-house.pdf"]
    for named in ("", "zzz-no-such-item"):
        act = disc.discovery_list_action(paths, {"resource_class": "library", "action": "read", "relative_path": named, "content": ""})
        assert act["content"] == ""


def test_an_entry_chosen_exactly_as_listed_inside_a_folder_resolves_against_that_folder(tmp_path):
    """Live 2026-09-24: inside photographs/Family/ the subject chose "Blair/" exactly as listed; it was
    refused LIST_NOT_DIRECTORY and no photograph was viewed.  Countermodels: a root-relative path is left as
    is, and a name existing nowhere stays unresolved."""
    import workspace_discovery as disc
    paths = wc.WorkspacePaths(str(tmp_path / "ws"))
    paths.ensure_exists()
    Path(paths.photographs_dir, "Family", "Blair").mkdir(parents=True)
    Path(paths.photographs_dir, "Family", "Blair", "a.jpg").write_text("x", encoding="utf-8")
    listing = {"resource_class": "photographs", "action": "list", "relative_path": "Family", "content": ""}
    surface = {"photographs": {"list", "view", "inspect_metadata"}}
    outcome, action, _ = disc.interpret_selection(paths, listing, listing,
                                                  json.dumps({"resource_class": "photographs", "action": "list", "relative_path": "Blair/", "content": ""}), surface)
    assert outcome == disc.NAVIGATE and action["relative_path"].rstrip("/") == "Family/Blair"
    listed, ok = wd.execute_workspace_action(paths, action, "actor-clark")
    assert ok and listed["result"]["entries"] == ["a.jpg"]
    inner = dict(listing, relative_path="Family/Blair")
    outcome, action, _ = disc.interpret_selection(paths, inner, inner,
                                                  json.dumps({"resource_class": "photographs", "action": "view", "relative_path": "a.jpg", "content": ""}), surface)
    assert outcome == disc.RESOLVED and action["relative_path"] == "Family/Blair/a.jpg"
    outcome, action, failure = disc.interpret_selection(paths, listing, listing,
                                                        json.dumps({"resource_class": "photographs", "action": "view", "relative_path": "Nobody.jpg", "content": ""}), surface)
    assert outcome == disc.UNRESOLVED
