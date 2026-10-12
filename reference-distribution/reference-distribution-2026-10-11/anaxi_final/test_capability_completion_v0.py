import json
import os
import sys
from pathlib import Path

import pypdf
import pytest
from PIL import Image

import workspace_capability as wc
import workspace_direction as wd


def _paths(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    paths.ensure_exists()
    return paths


def test_collection_pagination_reaches_every_item_and_cursor_is_bound(tmp_path):
    paths = _paths(tmp_path)
    expected = []
    for index in range(237):
        name = f"track-{index:03d}.mp3"
        (Path(paths.music_dir) / name).write_bytes(b"audio")
        expected.append(name)

    observed = []
    cursor = None
    pages = 0
    while True:
        result, failure = wc.list_contents(paths, wc.MUSIC, cursor=cursor)
        assert failure is None
        pages += 1
        observed.extend(result["entries"])
        if not result["has_more"]:
            assert result["next_cursor"] is None
            assert result["next_request"] is None
            break
        assert json.loads(result["next_request"])["cursor"] == result["next_cursor"]
        cursor = result["next_cursor"]

    assert pages == 3
    assert observed == sorted(expected)
    assert len(observed) == len(set(observed)) == 237

    _, mismatched = wc.list_contents(paths, wc.MUSIC, cursor=cursor, query="other")
    assert mismatched["rationale"] == wc.LIST_INVALID_CURSOR


def test_nested_navigation_and_recursive_search_are_complete(tmp_path):
    paths = _paths(tmp_path)
    target = Path(paths.photographs_dir) / "Family" / "Dominique" / "day.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"pixels")

    root, failure = wc.list_contents(paths, wc.PHOTOGRAPHS)
    assert failure is None
    assert root["entries"] == ["Family/"]
    nested, failure = wc.list_contents(paths, wc.PHOTOGRAPHS, directory="Family")
    assert failure is None
    assert nested["entries"] == ["Dominique/"]
    search, failure = wc.list_contents(paths, wc.PHOTOGRAPHS, query="day")
    assert failure is None
    assert search["entries"] == ["Family/Dominique/day.jpg"]


def test_search_continuation_carries_its_query_binding(tmp_path):
    paths = _paths(tmp_path)
    for index in range(105):
        (Path(paths.library_dir) / f"matching-{index:03d}.txt").write_text("x", encoding="utf-8")
    (Path(paths.library_dir) / "not-relevant.txt").write_text("x", encoding="utf-8")

    first_action = {
        "resource_class": wc.LIBRARY,
        "action": wc.LIST,
        "relative_path": "",
        "content": json.dumps({"query": "matching"}),
    }
    first, performed = wd.execute_workspace_action(paths, first_action, "actor-clark")
    assert performed is True and first["result"]["has_more"] is True
    continuation = json.loads(first["result"]["next_request"])
    assert continuation["query"] == "matching"

    second, performed = wd.execute_workspace_action(
        paths, dict(first_action, content=first["result"]["next_request"]), "actor-clark",
    )
    assert performed is True
    assert second["result"]["entries"] == [f"matching-{index:03d}.txt" for index in range(100, 105)]


def test_ordinary_dispatch_accepts_list_and_read_continuations(tmp_path):
    paths = _paths(tmp_path)
    for index in range(105):
        (Path(paths.library_dir) / f"book-{index:03d}.txt").write_text("0123456789", encoding="utf-8")

    first_action = {"resource_class": wc.LIBRARY, "action": wc.LIST, "relative_path": "", "content": ""}
    first, performed = wd.execute_workspace_action(paths, first_action, "actor-clark")
    assert performed is True and first["result"]["has_more"] is True
    second_action = dict(first_action, content=first["result"]["next_request"])
    second, performed = wd.execute_workspace_action(paths, second_action, "actor-clark")
    assert performed is True
    assert second["result"]["entries"] == [f"book-{index:03d}.txt" for index in range(100, 105)]

    read_action = {
        "resource_class": wc.LIBRARY,
        "action": wc.READ,
        "relative_path": "book-000.txt",
        "content": json.dumps({"offset": 4, "max_chars": 3}),
    }
    read_result, performed = wd.execute_workspace_action(paths, read_action, "actor-clark")
    assert performed is True
    assert read_result["result"]["content"] == "456"
    assert json.loads(read_result["result"]["next_request"]) == {"offset": 7, "max_chars": 3}


def test_ordinary_dispatch_preserves_specific_runtime_failure_reason(tmp_path):
    paths = _paths(tmp_path)
    action = {
        "resource_class": wc.LIBRARY, "action": wc.READ,
        "relative_path": "missing.pdf", "content": "",
    }
    result, performed = wd.execute_workspace_action(paths, action, "actor-clark")
    assert performed is False
    assert result["rationale"] == "Resource not found."
    assert result["rationale"] != wc.CAPABILITY_DESCRIPTORS[wc.LIBRARY]["rationale"]


@pytest.mark.skipif(sys.platform != "darwin", reason="production page renderer uses macOS PDFKit")
def test_scanned_pdf_page_is_rendered_as_bounded_pixels_through_dispatch(tmp_path):
    paths = _paths(tmp_path)
    pdf_path = Path(paths.library_dir) / "scanned.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with pdf_path.open("wb") as handle:
        writer.write(handle)

    action = {
        "resource_class": wc.LIBRARY,
        "action": wc.VIEW_PAGE,
        "relative_path": "scanned.pdf",
        "content": '{"page":1}',
    }
    result, performed = wd.execute_workspace_action(paths, action, "actor-clark")
    assert performed is True
    assert result["result"]["document_type"] == "pdf_page_image"
    assert result["result"]["page_number"] == 1
    assert result["result"]["page_count"] == 1
    assert result["result"]["source_sha256"] != result["result"]["sha256"]
    pixels = wd.get_and_clear_last_view_image_bytes()
    assert pixels.startswith(b"\x89PNG")
    assert wd.get_and_clear_last_view_image_bytes() is None


def test_large_photo_and_gif_receive_safe_derivatives_with_source_linkage(tmp_path):
    paths = _paths(tmp_path)
    large_path = Path(paths.photographs_dir) / "large.jpg"
    Image.new("RGB", (5000, 3000), (12, 34, 56)).save(large_path, quality=95)
    gif_path = Path(paths.photographs_dir) / "animated.gif"
    frames = [Image.new("RGB", (80, 40), color) for color in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(gif_path, save_all=True, append_images=frames[1:], duration=100, loop=0)

    for name in ("large.jpg", "animated.gif"):
        result, failure = wc.deliver_photograph_bytes(paths, name)
        assert failure is None
        assert result["derivative"] is True
        assert result["source_sha256"] != ""
        assert result["sha256"] != ""
        assert max(result["width"], result["height"]) <= wc.MAX_IMAGE_DIMENSION
        assert result["size_bytes"] <= wc.MAX_IMAGE_BYTES
        assert result["image_bytes"]


def test_launch_prepares_pdf_renderer_and_ingests_before_waking():
    source = (Path(__file__).parent / "Launch Anaxi.command").read_text(encoding="utf-8")
    embedder_position = source.index('prepare_embedding_model.py --check-only')
    compile_position = source.index('/usr/bin/xcrun swiftc')
    ingest_position = source.index('public_resource_ingest.py sync')
    waking_position = source.index('llama_launch.py --conversation')
    assert embedder_position < compile_position < ingest_position < waking_position


def _jpeg_with_exif_orientation(path, size, orientation):
    image = Image.new("RGB", size, (200, 30, 30))
    # mark the top-left corner so a rotation is detectable from pixels
    image.paste((0, 0, 255), (0, 0, size[0] // 4, size[1] // 4))
    exif = Image.Exif()
    exif[274] = orientation
    image.save(path, format="JPEG", exif=exif)


@pytest.mark.parametrize("orientation,swaps_axes", [(1, False), (3, False), (6, True), (8, True)])
def test_photo_exif_orientation_is_corrected_in_the_derivative_and_source_is_untouched(
        tmp_path, orientation, swaps_axes):
    paths = _paths(tmp_path)
    source_path = Path(paths.photographs_dir) / f"oriented-{orientation}.jpg"
    _jpeg_with_exif_orientation(source_path, (400, 200), orientation)
    original_bytes = source_path.read_bytes()

    result, failure = wc.deliver_photograph_bytes(paths, source_path.name)

    assert failure is None
    assert source_path.read_bytes() == original_bytes, "source original must never be rewritten"
    assert (result["source_width"], result["source_height"]) == (400, 200)
    expected = (200, 400) if swaps_axes else (400, 200)
    assert (result["width"], result["height"]) == expected
    assert result["derivative"] is (orientation != 1)
    with Image.open(__import__("io").BytesIO(result["image_bytes"])) as delivered:
        assert delivered.size == expected
        assert delivered.getexif().get(274, 1) in (None, 1)


def test_animated_gif_is_delivered_as_one_bounded_static_frame_with_source_intact(tmp_path):
    import io
    paths = _paths(tmp_path)
    gif_path = Path(paths.photographs_dir) / "two-frames.gif"
    frames = [Image.new("RGB", (60, 30), color) for color in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(gif_path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    original_bytes = gif_path.read_bytes()

    result, failure = wc.deliver_photograph_bytes(paths, gif_path.name)

    assert failure is None
    assert gif_path.read_bytes() == original_bytes
    assert result["derivative"] is True
    assert result["source_extension"] == "gif"
    assert result["derivative_policy"] == "bounded_orientation_corrected_static_frame_v1"
    with Image.open(io.BytesIO(result["image_bytes"])) as delivered:
        assert getattr(delivered, "n_frames", 1) == 1
        # first frame is what is delivered
        assert delivered.convert("RGB").getpixel((5, 5))[0] > 200


def test_completion_assay_and_ingest_layouts_never_name_private_space():
    """The evidence machinery for public resources cannot traverse Private Space."""
    import public_resource_completion_assay as assay
    import public_resource_ingest as ingest
    for layout in (set(assay.OWNER_SOURCE_LAYOUT), set(ingest.PUBLIC_SOURCE_LAYOUT)):
        assert layout == {"Books", "Pictures", "Music"}
        assert not any("private" in name.lower() for name in layout)
    document = json.loads(
        (Path(__file__).with_name("completion_evidence") / "synthetic_public_resources.json").read_text(encoding="utf-8"))
    assert document["private_space_traversed"] is False
    assert document["canonical_production_history_mutated"] is False
    assert document["production_resource_files_mutated"] is False
