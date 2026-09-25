"""Deterministic transport segmentation of ONE canonical Clark reply into Discord-sized parts.

Pure and dependency-free.  This is a transport projection, never authorship: the parts, concatenated
in order, are exactly the input string (no inserted markers, no dropped or duplicated whitespace, no
normalization).  Lengths are measured in Python code points, the same unit the canonical writer and
the single-message transport already use.

Boundary preference inside the window that keeps every remaining part feasible:
  1. a paragraph boundary (the last blank-line break),
  2. a newline,
  3. ordinary whitespace,
  4. an exact hard slice, only when none of the above exists in the window.
A split falls AFTER the boundary character, so the whitespace stays in the preceding part.
The minimum part count is always used: ceil(len / limit).
"""

DEFAULT_PART_LIMIT = 2000
DEFAULT_MAX_PARTS = 3
PLAN_VERSION = 1


class TransportSegmentationError(ValueError):
    """The reply cannot be segmented within the transport bounds (never truncated instead)."""


def segment_offsets(text, *, limit=DEFAULT_PART_LIMIT, max_parts=DEFAULT_MAX_PARTS):
    """[(start, end), ...] covering `text` exactly, in order, each part <= `limit` code points."""
    if not isinstance(text, str) or not text.strip():
        raise TransportSegmentationError("reply must be a nonblank string")
    n = len(text)
    if n > limit * max_parts:
        raise TransportSegmentationError(
            f"reply is {n} characters; transport cap is {limit * max_parts} ({max_parts} x {limit})"
        )
    if n <= limit:
        return [(0, n)]
    count = -(-n // limit)
    offsets, start = [], 0
    for remaining in range(count, 1, -1):
        hi = min(start + limit, n)
        lo = max(start + 1, n - (remaining - 1) * limit)   # the remainder must still fit its parts
        cut = _choose_cut(text, lo, hi)
        offsets.append((start, cut))
        start = cut
    offsets.append((start, n))
    for a, b in offsets:
        if not text[a:b].strip():
            raise TransportSegmentationError("a transport part would carry no visible text")
    return offsets


def _choose_cut(text, lo, hi):
    """Cut index c in [lo, hi] (part = text[start:c]); prefers paragraph, newline, whitespace."""
    for j in range(hi - 1, lo - 2, -1):                 # paragraph: a newline preceded by a newline
        if text[j] == "\n" and j > 0 and text[j - 1] == "\n":
            return j + 1
    for j in range(hi - 1, lo - 2, -1):
        if text[j] == "\n":
            return j + 1
    for j in range(hi - 1, lo - 2, -1):
        if text[j].isspace():
            return j + 1
    return hi


def segment_reply(text, *, limit=DEFAULT_PART_LIMIT, max_parts=DEFAULT_MAX_PARTS):
    return [text[a:b] for a, b in segment_offsets(text, limit=limit, max_parts=max_parts)]
