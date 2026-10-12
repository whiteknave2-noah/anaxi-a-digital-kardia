"""A2: deterministic synthetic public-resource collection.

Replaces the owner's production public-resource collection as the input of
``public_resource_completion_assay`` / ``public_resource_delivery_assay``.
Everything is generated programmatically: no file is fetched, copied, or
derived from the owner's collection, and no production name or count is
reproduced.

Only the properties the bound resource claims need are present:
  library      long multi-window UTF-8 text with multi-byte characters (followed
               to its exact end), a nested markdown note, a text PDF (READ
               windows), an image-only PDF of 3 pages (VIEW_PAGE first/middle/last)
  photographs  every supported photo extension (.jpg .jpeg .png .webp .gif),
               nested folders
  music        WAV, FLAC and WMA (ASF/wmav2 via PyAV, the same stack the decoder
               uses) tracks long enough for a genuine late/end window, nested
  root file    ANAXI_CAPABILITIES_AND_PATHWAYS.md (required by the source layout)

    python packaging/reference_distribution/a2_synthetic_collection.py OUT_DIR
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import struct
import sys
import wave
from pathlib import Path

GENERATOR_ID = "anaxi-reference-distribution/a2_synthetic_collection.py"
CONFIG = {
    "version": 1,
    "long_text_lines": 900,
    "text_pdf_pages": 3,
    "image_pdf_pages": 3,
    "photo_size": [640, 480],
    "audio_seconds": 6,
    "audio_rates": {"wav": 8000, "flac": 16000, "wma": 44100},
}


def config_sha256() -> str:
    """Hash of the deterministic inputs: this generator's source plus CONFIG."""
    h = hashlib.sha256()
    h.update(Path(__file__).read_bytes())
    h.update(json.dumps(CONFIG, sort_keys=True).encode())
    return h.hexdigest()


# --- library ---------------------------------------------------------------

def _long_text() -> str:
    words = ("river", "lantern", "orchard", "meridian", "harbour", "quartz", "willow", "comet")
    lines = []
    for i in range(CONFIG["long_text_lines"]):
        w = " ".join(words[(i + k) % len(words)] for k in range(6))
        # multi-byte characters: offsets must count characters, not bytes
        lines.append(f"Line {i:04d} — café naïve résumé Ω≈ç: {w}.")
    return "Synthetic reading sample for the ANAXI reference distribution.\n" + "\n".join(lines) + "\nEND OF SAMPLE\n"


def _pdf(objects: list[bytes]) -> bytes:
    """Serialize numbered PDF objects (1..n; object 1 is the catalog) with an exact xref."""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _text_pdf() -> bytes:
    pages = CONFIG["text_pdf_pages"]
    # 1 catalog, 2 pages, 3 font, then (page, content) pairs
    kids = " ".join(f"{4 + 2 * p} 0 R" for p in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for p in range(pages):
        ops = ["BT", "/F1 10 Tf", "12 TL", "50 750 Td"]
        for n in range(48):
            ops.append(f"(Page {p + 1} line {n:02d}: synthetic public document text for window checks.) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode()
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * p} 0 R >>".encode())
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    return _pdf(objects)


def _image_pdf() -> bytes:
    from PIL import Image, ImageDraw
    frames = []
    for p in range(CONFIG["image_pdf_pages"]):
        img = Image.new("RGB", (850, 1100), (250, 250, 245))
        draw = ImageDraw.Draw(img)
        for y in range(80, 1020, 36):  # ruled "scanned" lines, no text layer
            draw.rectangle([70, y, 780 - 40 * p, y + 14], fill=(40 + 30 * p, 40, 60))
        frames.append(img)
    buf = io.BytesIO()
    frames[0].save(buf, format="PDF", save_all=True, append_images=frames[1:], resolution=100.0,
                   title="synthetic scanned sample", author="synthetic", producer="synthetic",
                   creator="synthetic", creationDate="D:20260101000000Z", modDate="D:20260101000000Z")
    return buf.getvalue()


# --- photographs -----------------------------------------------------------

def _photo(fmt: str, seed: int) -> bytes:
    from PIL import Image
    w, h = CONFIG["photo_size"]
    img = Image.new("RGB", (w, h))
    img.putdata([((x * 3 + seed * 40) % 256, (y * 2 + seed * 70) % 256, ((x + y) + seed * 90) % 256)
                 for y in range(h) for x in range(w)])
    buf = io.BytesIO()
    if fmt == "GIF":
        img = img.convert("P", palette=Image.Palette.ADAPTIVE, colors=64)
    kwargs = {"quality": 90} if fmt in ("JPEG", "WEBP") else {}
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


# --- music -----------------------------------------------------------------

def _samples(rate: int, freq: float) -> list[float]:
    n = rate * CONFIG["audio_seconds"]
    # a rising tone with a decaying envelope: causal, non-silent late window
    return [0.5 * math.sin(2 * math.pi * (freq + 40 * i / n) * i / rate) * (1 - 0.6 * i / n) for i in range(n)]


def _wav(freq: float) -> bytes:
    rate = CONFIG["audio_rates"]["wav"]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", int(32767 * s)) for s in _samples(rate, freq)))
    return buf.getvalue()


def _encode_av(fmt: str, codec: str, rate: int, freq: float, sample_format: str, frame: int) -> bytes:
    import av
    import numpy as np
    mono = np.asarray(_samples(rate, freq), dtype="float32")
    stereo = np.vstack([mono, mono])
    if sample_format == "s16":
        stereo = (stereo * 32767).astype("int16")
    buf = io.BytesIO()
    container = av.open(buf, "w", format=fmt, options={"fflags": "+bitexact"})
    stream = container.add_stream(codec, rate=rate, layout="stereo")
    ctx = stream.codec_context
    ctx.format = sample_format
    ctx.flags |= av.codec.context.Flags.bitexact
    if codec == "wmav2":
        ctx.bit_rate = 128000
    planar = sample_format.endswith("p")
    for start in range(0, stereo.shape[1] - frame + 1, frame):
        chunk = stereo[:, start:start + frame]
        arr = np.ascontiguousarray(chunk if planar else chunk.T.reshape(1, -1))
        af = av.AudioFrame.from_ndarray(arr, format=sample_format, layout="stereo")
        af.sample_rate = rate
        af.pts = start
        for packet in stream.encode(af):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return buf.getvalue()


def _capabilities_md() -> bytes:
    body = ["# ANAXI capabilities and pathways (synthetic reference copy)", ""]
    for i, topic in enumerate(("Library", "Photographs", "Music", "Journal", "Notes", "Sleep")):
        body.append(f"## {topic}")
        body.extend(f"Synthetic description paragraph {i}.{k} for the reference distribution." for k in range(8))
        body.append("")
    return "\n".join(body).encode()


def collection() -> dict[str, bytes]:
    """{relative path: bytes} -- the whole collection, in memory, deterministic."""
    rates = CONFIG["audio_rates"]
    files = {
        "Books/long_reading_sample.txt": _long_text().encode("utf-8"),
        "Books/notes/nested_notes.md": ("# Nested notes\n\n" + "A nested synthetic note. " * 60 + "\n").encode(),
        "Books/text_sample.pdf": _text_pdf(),
        "Books/scans/scanned_sample.pdf": _image_pdf(),
        "Pictures/Landscapes/gradient_a.jpg": _photo("JPEG", 1),
        "Pictures/Landscapes/gradient_b.jpeg": _photo("JPEG", 2),
        "Pictures/Patterns/pattern_c.png": _photo("PNG", 3),
        "Pictures/Patterns/nested/pattern_d.webp": _photo("WEBP", 4),
        "Pictures/pattern_e.gif": _photo("GIF", 5),
        "Music/Album One/tone_a.wav": _wav(330.0),
        "Music/Album One/tone_b.flac": _encode_av("flac", "flac", rates["flac"], 440.0, "s16", 4096),
        "Music/Album Two/tone_c.wma": _encode_av("asf", "wmav2", rates["wma"], 550.0, "fltp", 2048),
        "ANAXI_CAPABILITIES_AND_PATHWAYS.md": _capabilities_md(),
    }
    return dict(sorted(files.items()))


def build(out_dir: Path) -> dict:
    """Write the collection into a fresh OUT_DIR; return a manifest of sha256s."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"refusing to write into non-empty {out_dir}")
    files = collection()
    for rel, data in files.items():
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return {
        "generator": GENERATOR_ID,
        "config_sha256": config_sha256(),
        "files": {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()},
    }


if __name__ == "__main__":
    print(json.dumps(build(Path(sys.argv[1])), indent=1))
