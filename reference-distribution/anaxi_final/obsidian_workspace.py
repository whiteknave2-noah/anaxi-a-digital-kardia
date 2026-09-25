"""OBSIDIAN-COLLABORATIVE-WORKSPACE-V0: the Obsidian vault as a
collaborative / durable working surface for ANAXI.

This is the candidate -- NON-PRODUCTION. It is a host-operated
capability surface over the SAME vault root ANAXI already establishes
for Clark's artifact journal (`ANAXI_OBSIDIAN_WORKSPACE_ROOT`, the
"Clark Kara Other" Obsidian workspace -- see llama_anaxi.py's
OBSIDIAN_WORKSPACE_ROOT and clark_journal.py). Creation of durable
artifacts stays where it already is: clark_journal.py's
write_journal_entry / prepare_artifact_decision / finalize_artifact_-
write (Q-05: canonical-before-artifact ordering is the caller's
responsibility). This module supplies the rest of an ordinary
collaborative workflow: bounded listing, reading, and provenance/
authorship legibility over the preserved material, within ANAXI's
privacy and authority model.

WHAT THE WORKSPACE IS (and is not), stated as a frozen boundary:

- The workspace PRESERVES material, PROVIDES ACCESS to material,
  SUPPORTS COLLABORATION, and makes durable artifacts available to the
  participants. That is the whole claim. It is a place, never:
    an agent brain, a hidden thought store, a canonical consciousness
    store, proof of memory, proof of preference, proof of subjective
    experience, or a mechanism for manufacturing autobiographical
    continuity.
- The existence of a note is never evidence that the subject authored,
  understood, remembered, endorsed, preferred, or emotionally valued
  its contents. Authorship is whatever the note's own provenance
  frontmatter explicitly records -- and nothing more. A note with no
  author record is AUTHORSHIP_NOT_ESTABLISHED, never "clark".
- Reading a note never rewrites it. This surface performs NO write,
  NO edit, NO deletion, NO rename. Corrections and later
  interpretations are distinct NEW artifacts; the historical artifact
  is preserved with its occurrence/provenance intact.
- This module is deliberately separate from workspace_capability.py
  (the in-repo library/music/photographs/journal) and from
  workspace_private.py (Private Space). Nothing here can address a
  path outside the vault root, into a dot-directory (Obsidian's own
  internal store, e.g. `.obsidian/`, `.trash/`), or into ANAXI Private
  Space. PATHWAYS NOT ORGANS was the established pattern; this module
  follows it.

truthful-state discipline (mirrors the project-wide rule that
ABSENT / EMPTY / UNAVAILABLE / FAILED / NOT_ESTABLISHED stay distinct):
  - a note that is not there is VAULT_NOTE_NOT_FOUND (a failure);
  - a note that is there but has an empty body is a SUCCESS whose
    `content` is "" with `empty` True;
  - a note whose file cannot be read is VAULT_NOTE_UNREADABLE;
  - a vault root that does not exist is `vault_exists: False`, never
    "an empty vault";
  - a field absent from frontmatter is NOT_ESTABLISHED, never
    fabricated.

MODEL-VISIBLE ACCESS (final completion build, 2026-09-24): the vault is the workspace resource class
``notes`` (workspace_capability.NOTES), reached through Clark's ordinary typed ``use_workspace`` act and
the supervised workspace pathway (discovery, bounded delivery, canonical persistence) like every other
collection -- list, read (windowed), and APPEND = create one new note of his own.  Offered only in the
owner's principal-private session and only when the configured vault exists; never in unattended
roaming.  CREATION is the one write this module performs (create_note): exclusive-create, never an
overwrite or edit of any note, Clark's included.

AUTHORSHIP BY PROVENANCE (verify_authorship): a note's own ``author:`` line is its CLAIM.  ANAXI
confirms Clark's authorship only when the note carries a canonical event id that ANAXI's own
provenance database holds (``event_id`` = the waking turn that wrote an artifact; ``source_event_id`` =
the canonical message that occasioned a created note) AND the body still matches the recorded
``content_sha256``.  Everything else is reported as what it is: edited since, marked clark but
unconfirmed, attributed to someone (their claim), shared, marked host, or no author recorded.
"""
import datetime
import hashlib
import json
import os
import re
from typing import Optional

import workspace_private


# ------------------------------------------------------------------ bounds

# Mirrors workspace_capability.py's own deterministic bound discipline
# (MAX_LIST_ENTRIES / MAX_LIST_AGGREGATE_CHARS). Kept as independent
# constants here, not imported from workspace_capability, because this
# module deliberately imports no sibling capability substrate at all.
MAX_LIST_ENTRIES = 100
MAX_LIST_AGGREGATE_CHARS = 6000
MAX_FRONTMATTER_PEEK = 4096

# Any path segment starting with "." is Obsidian-internal machinery,
# never collaborative material (`.obsidian`, `.trash`, `.DS_Store`...).
DOT_PREFIX = "."

NOTE_EXTENSION = ".md"

# ----------------------------------------------------------- failure codes

VAULT_TARGET_EMPTY = "VAULT_TARGET_EMPTY"
VAULT_TARGET_NOT_MARKDOWN = "VAULT_TARGET_NOT_MARKDOWN"
VAULT_TARGET_OUTSIDE_ROOT = "VAULT_TARGET_OUTSIDE_ROOT"
VAULT_TARGET_IS_OBSIDIAN_INTERNAL = "VAULT_TARGET_IS_OBSIDIAN_INTERNAL"
VAULT_PRIVATE_SPACE_REFUSED = "VAULT_PRIVATE_SPACE_REFUSED"
VAULT_NOTE_NOT_FOUND = "VAULT_NOTE_NOT_FOUND"
VAULT_NOTE_IS_DIRECTORY = "VAULT_NOTE_IS_DIRECTORY"
VAULT_NOTE_UNREADABLE = "VAULT_NOTE_UNREADABLE"
VAULT_INVALID_CURSOR = "VAULT_INVALID_CURSOR"

# ------------------------------------------------------------ authorship

# The only value this module ever maps to "clark": an EXPLICIT
# `author:` field whose value is exactly clark/Clark. Nothing is ever
# inferred from content, filename, absence, or delivery success.
CLARK_AUTHOR_VALUES = frozenset({"clark"})

AUTHOR_CLARK = "clark"
AUTHOR_EXPLICIT_NONCLARK = "explicit_nonclark"
AUTHOR_NOT_ESTABLISHED = "not_established"

PROVENANCE_EXPLICIT = "explicit"
PROVENANCE_NOT_ESTABLISHED = "not_established"

FRONTMATTER_OK = "ok"
FRONTMATTER_MALFORMED = "malformed"
FRONTMATTER_ABSENT = "absent"


class ObsidianWorkspaceConfigurationError(Exception):
    """Raised only for a genuinely invalid configuration -- the vault
    root resolving inside ANAXI Private Space (or being Private Space
    itself). Ordinary user errors (missing file, empty target, escape)
    are never raised; they return (None, failure) like every other
    capability surface."""


class ObsidianPathEscapeError(Exception):
    pass


# ----------------------------------------------------------------- parsing


def _split_frontmatter(text: str):
    """Returns (frontmatter_dict_or_None, body, parse_state).

    frontmatter_dict is None when there is no `---`-delimited block.
    parse_state is FRONTMATTER_OK / FRONTMATTER_ABSENT / FRONTMATTER_MALFORMED.
    Only simple `key: value` lines are recognized -- anything else in
    the delimited block marks it malformed rather than being guessed.
    The body is delivered verbatim in every state; a malformed block is
    surfaced, never silently dropped or reinterpreted."""
    if not isinstance(text, str):
        return None, "", FRONTMATTER_ABSENT
    lines = text.split("\n")
    if not lines or not lines[0].strip().startswith("---"):
        return None, text, FRONTMATTER_ABSENT
    closing = -1
    for i in range(1, len(lines)):
        if lines[i].strip().startswith("---"):
            closing = i
            break
    if closing < 0:
        # A lone opening fence with no body/close: not a real frontmatter
        # block -- treat the text as its own body verbatim (never guess).
        return None, text, FRONTMATTER_ABSENT
    block = lines[1:closing]
    body = "\n".join(lines[closing + 1:])
    fields = {}
    for line in block:
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^([^:]+):\s*(.*)$", stripped)
        if match is None:
            return None, body, FRONTMATTER_MALFORMED
        key = match.group(1).strip()
        value = match.group(2).strip()
        fields[key] = value
    return fields, body, FRONTMATTER_OK


def authorship_status(frontmatter: Optional[dict]):
    """Canonical authorship resolution. Returns
    (kind, author_value_or_None). Never infers; absent author is
    AUTHOR_NOT_ESTABLISHED. An explicit non-clark author (alex, host,
    collaborative, anyone) is preserved verbatim as
    AUTHOR_EXPLICIT_NONCLARK -- never renamed, never upgraded to clark,
    never collapsed into a generic "memory" label."""
    if not frontmatter:
        return AUTHOR_NOT_ESTABLISHED, None
    raw = frontmatter.get("author")
    if raw is None:
        return AUTHOR_NOT_ESTABLISHED, None
    if str(raw).strip().lower() in CLARK_AUTHOR_VALUES:
        return AUTHOR_CLARK, str(raw).strip()
    return AUTHOR_EXPLICIT_NONCLARK, str(raw).strip()


def provenance_status(frontmatter: Optional[dict]):
    """The `provenance:` line verbatim (model_generated, host, external,
    derived, ...) or PROVENANCE_NOT_ESTABLISHED. Never a value of its
    own invention."""
    if not frontmatter:
        return PROVENANCE_NOT_ESTABLISHED, None
    raw = frontmatter.get("provenance")
    if raw is None:
        return PROVENANCE_NOT_ESTABLISHED, None
    return PROVENANCE_EXPLICIT, str(raw).strip()


def _summarize_frontmatter(peeked: str):
    """Cheap provenance hint for list entries: author + created_at only,
    from the first bytes of a note. Returns {} when nothing legible."""
    fields, _body, state = _split_frontmatter(peeked)
    if state != FRONTMATTER_OK:
        return {}
    hint = {}
    _kind, author = authorship_status(fields)
    if author is not None:
        hint["frontmatter_author"] = author
    if fields.get("created_at"):
        hint["frontmatter_created_at"] = fields["created_at"]
    return hint


# --------------------------------------------------------------- workspace


class ObsidianWorkspace:
    """One configured vault root. Resolution and containment always use
    realpath -- never a naive prefix check -- so '..', symlinks, and
    absolute paths are all caught by the same mechanism.

    ``private_space_root=None`` resolves ANAXI Private Space through
    workspace_private.PrivatePaths.production_defaults() (the canonical
    location), so under pytest isolation it follows the redirected
    workspace root the same way the rest of the suite does."""

    def __init__(self, vault_root: str, private_space_root: Optional[str] = None):
        self.vault_root: str = os.path.realpath(os.path.abspath(vault_root))
        if private_space_root is None:
            private_space_root = workspace_private.PrivatePaths.production_defaults().root
        self.private_space_root: str = os.path.realpath(os.path.abspath(private_space_root))
        if self._is_within(self.vault_root, self.private_space_root):
            raise ObsidianWorkspaceConfigurationError(
                "Refusing to configure: the Obsidian vault root resolves inside ANAXI "
                "Private Space. The collaborative workspace must never address Private Space."
            )
        self._private_root_real = os.path.realpath(self.private_space_root)

    @staticmethod
    def production_defaults():
        """Exactly the established resolution order for
        llama_anaxi.OBSIDIAN_WORKSPACE_ROOT, without importing llama_anaxi
        (which would pull the runtime client into a pure surface module):
        ANAXI_OBSIDIAN_WORKSPACE_ROOT, else the original Windows-derived
        default path. A default path that does not exist is a truthful
        `vault_exists: False` fact, never an invented empty vault."""
        configured = os.environ.get("ANAXI_OBSIDIAN_WORKSPACE_ROOT")
        if configured:
            vault_root = configured
        else:
            from pathlib import Path
            vault_root = str(Path.home() / "OneDrive" / "Desktop" / "Clark Kara Other")
        return ObsidianWorkspace(vault_root=vault_root)

    # ---------------------------------------------------------- containment

    @staticmethod
    def _is_within(candidate_real: str, root_real: str) -> bool:
        return candidate_real == root_real or candidate_real.startswith(root_real + os.sep)

    def _normalize_relative_path(self, relative_path: str):
        """Portable normalization: never joins a raw caller string into a
        path. Returns the cleaned relative path (posix-style segments) or
        "" for an empty target."""
        if not isinstance(relative_path, str):
            return ""
        cleaned = relative_path.strip().replace("\\", "/")
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        return cleaned

    def _has_dot_segment(self, relative_path: str) -> bool:
        segments = [s for s in relative_path.split("/") if s not in ("", ".")]
        return any(s.startswith(DOT_PREFIX) for s in segments)

    def resolve_note_path(self, relative_path, *, create=False):
        """Returns (real_path, None) or (None, failure_dict). The single
        authoritative resolver for every addressing operation. `create`
        is reserved for callers that create (none exist in this module)
        -- containment is identical either way; a missing trailing file
        is the caller's business once containment passes."""
        cleaned = self._normalize_relative_path(relative_path)
        if cleaned == "":
            return None, {"code": VAULT_TARGET_EMPTY, "rationale": "No note path was supplied."}
        if "." in cleaned.split("/") or ".." in cleaned.split("/"):
            return None, {"code": VAULT_TARGET_OUTSIDE_ROOT, "rationale": f"Path traversal is refused: {relative_path!r}"}
        if self._has_dot_segment(cleaned):
            return None, {
                "code": VAULT_TARGET_IS_OBSIDIAN_INTERNAL,
                "rationale": "Dot-paths are Obsidian's own internal store, not collaborative material.",
            }
        if not cleaned.lower().endswith(NOTE_EXTENSION):
            return None, {
                "code": VAULT_TARGET_NOT_MARKDOWN,
                "rationale": "Only .md notes are addressable in the Obsidian workspace.",
            }
        candidate = os.path.join(self.vault_root, cleaned)
        real = os.path.realpath(candidate)
        # Private Space is the more authoritative boundary: if a path
        # resolves there it is refused as Private Space (never as a mere
        # outside-root case), so the failure name says what it is.
        if self._is_within(real, self._private_root_real):
            return None, {
                "code": VAULT_PRIVATE_SPACE_REFUSED,
                "rationale": "Refused: that path resolves inside ANAXI Private Space.",
            }
        if not self._is_within(real, self.vault_root):
            return None, {
                "code": VAULT_TARGET_OUTSIDE_ROOT,
                "rationale": f"{relative_path!r} resolved outside the vault root.",
            }
        return real, None

    # ------------------------------------------------------------ boundary

    def boundary_report(self):
        """Distinct, interpretation-free facts about the configured
        boundary. No content is read; only existence and containment."""
        return {
            "vault_root": self.vault_root,
            "vault_exists": os.path.isdir(self.vault_root),
            "private_space_root": self._private_root_real,
            "vault_resolves_inside_private_space": self._is_within(self.vault_root, self._private_root_real),
            "dot_entries_excluded_deterministically": True,
            "note_extension": NOTE_EXTENSION,
        }

    # ---------------------------------------------------------------- list

    def list_notes(self, cursor=None, query=None, folder=""):
        """Bounded deterministic listing of every .md note under the
        vault root, excluding dot-directories/dot-files. No content is
        loaded -- only a bounded frontmatter peek for a provenance hint.
        Supports page continuation via cursor == json.dumps({"start": N})."""
        if cursor is not None:
            try:
                parsed = json.loads(cursor)
                start_index = int(parsed.get("start", 0))
            except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                return None, {"code": VAULT_INVALID_CURSOR, "rationale": "Unrecognized listing cursor."}
            if start_index < 0:
                return None, {"code": VAULT_INVALID_CURSOR, "rationale": "Unrecognized listing cursor."}
        else:
            start_index = 0

        all_notes, failure = self._filtered_notes(query, folder)
        if failure is not None:
            return None, failure
        bounded = []
        aggregate = 0
        for name in all_notes[start_index:]:
            if len(bounded) >= MAX_LIST_ENTRIES:
                break
            if aggregate + len(name) > MAX_LIST_AGGREGATE_CHARS:
                break
            bounded.append(name)
            aggregate += len(name)

        entries = []
        for rel in bounded:
            entry = {"relative_path": rel, "name": rel.split("/")[-1]}
            real = os.path.join(self.vault_root, rel)
            try:
                with open(real, "r", encoding="utf-8") as f:
                    peek = f.read(MAX_FRONTMATTER_PEEK)
            except OSError:
                peek = ""
            entry.update(_summarize_frontmatter(peek))
            # A listing shows the note's own CLAIM only; confirmation happens when it is read.
            entry["recorded_author"] = entry.get("frontmatter_author") or "none recorded"
            entries.append(entry)

        next_index = start_index + len(bounded)
        has_more = next_index < len(all_notes)
        result = {
            "entries": entries,
            "returned_count": len(bounded),
            "total_count": len(all_notes),
            "truncated": len(bounded) < len(all_notes),
            "has_more": has_more,
            "next_cursor": json.dumps({"start": next_index}, separators=(",", ":")) if has_more else None,
        }
        result["next_request"] = result["next_cursor"]
        return result, None

    def _filtered_notes(self, query, folder):
        all_notes = self._collect_notes()
        folder = self._normalize_relative_path(folder or "").strip("/")
        if folder:
            if ".." in folder.split("/") or self._has_dot_segment(folder):
                return None, {"code": VAULT_TARGET_OUTSIDE_ROOT, "rationale": f"Folder {folder!r} is not addressable."}
            all_notes = [n for n in all_notes if n.startswith(folder + "/")]
        if isinstance(query, str) and query.strip():
            # Plain case-insensitive substring over the note's path (never semantic, never a guess).
            all_notes = [n for n in all_notes if query.strip().casefold() in n.casefold()]
        return all_notes, None

    def _peek_author(self, rel):
        try:
            with open(os.path.join(self.vault_root, rel), "r", encoding="utf-8") as f:
                return _summarize_frontmatter(f.read(MAX_FRONTMATTER_PEEK)).get("frontmatter_author")
        except OSError:
            return None

    def list_notes_compact(self, *, start_index=0, query=None, folder=""):
        """The listing in the workspace's standard shape -- what the model is shown when choosing.
        ``entries`` are plain relative paths; ``entry_summaries`` gives each listed note's recorded
        author CLAIM ("author: X" / "no author recorded") and is windowed together with the entries
        by workspace_delivery.  Real-model finding (live, 2026-09-24): the per-entry dict form (name,
        created_at, author twice) was ~6 KB for 23 notes, whole-entry windowing left ZERO entries in the
        selection prompt, and Clark wandered to the library.  Cursor/continuation is the caller's."""
        paths, failure = self._filtered_notes(query, folder)
        if failure is not None:
            return None, failure
        if not isinstance(start_index, int) or start_index < 0 or start_index > max(len(paths), 0):
            return None, {"code": VAULT_INVALID_CURSOR, "rationale": "Unrecognized listing cursor."}
        window, aggregate = [], 0
        for rel in paths[start_index:]:
            if len(window) >= MAX_LIST_ENTRIES or aggregate + len(rel) > MAX_LIST_AGGREGATE_CHARS:
                break
            window.append(rel)
            aggregate += len(rel)
        authors = {rel: self._peek_author(rel) for rel in window}
        return {
            "directory": folder, "query": query or None, "start_index": start_index,
            "entries": window,
            "entry_summaries": {rel: (f"author: {authors[rel]}" if authors.get(rel) else "no author recorded")
                                for rel in window},
            "returned_count": len(window), "total_count": len(paths),
            "truncated": len(window) < len(paths), "has_more": start_index + len(window) < len(paths),
            "authorship_note": "authors shown are each note's own claim; reading a note shows what ANAXI confirms",
        }, None

    def _collect_notes(self):
        if not os.path.isdir(self.vault_root):
            return []
        found = []
        for root, dirs, files in os.walk(self.vault_root, topdown=True):
            dirs[:] = sorted(d for d in dirs if not d.startswith(DOT_PREFIX))
            for name in sorted(files):
                if name.startswith(DOT_PREFIX):
                    continue
                if name.lower().endswith(NOTE_EXTENSION):
                    rel = os.path.relpath(os.path.join(root, name), self.vault_root)
                    found.append(rel)
        return sorted(found)

    # ---------------------------------------------------------------- read

    def read_note(self, relative_path, *, offset=0, max_chars=None, event_lookup=None, owner_note_lookup=None):
        """Read one note. max_chars=None returns the whole body (the
        historical convention, mirroring workspace_capability.read_
        journal_entry); given max_chars, the body is delivered as a
        window [offset, offset+max_chars) with a content_window
        continuation block, so long notes are read progressively.

        The result always carries provenance: frontmatter fields
        verbatim, authorship_status, provenance_status, and a
        frontmatter parse state. An absent author is
        AUTHOR_NOT_ESTABLISHED -- never clark. This function never
        mutates the note."""
        real, failure = self.resolve_note_path(relative_path)
        if failure is not None:
            return None, failure

        if os.path.isdir(real):
            return None, {"code": VAULT_NOTE_IS_DIRECTORY, "rationale": f"{relative_path!r} is a directory, not a note."}
        if not os.path.isfile(real):
            return None, {"code": VAULT_NOTE_NOT_FOUND, "rationale": f"No note at {relative_path!r}."}
        try:
            with open(real, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            return None, {
                "code": VAULT_NOTE_UNREADABLE,
                "rationale": f"Could not read {relative_path!r}: {exc}",
            }

        frontmatter, body, parse_state = _split_frontmatter(text)
        raw_remaining = body

        if not isinstance(offset, int) or offset < 0:
            return None, {"code": VAULT_INVALID_CURSOR, "rationale": "offset must be a non-negative integer."}
        if max_chars is None:
            window = raw_remaining
            start = 0
            end = len(raw_remaining)
        else:
            if not isinstance(max_chars, int) or max_chars < 0:
                return None, {"code": VAULT_INVALID_CURSOR, "rationale": "max_chars must be a non-negative integer."}
            if offset > len(raw_remaining):
                return None, {"code": VAULT_INVALID_CURSOR, "rationale": "Offset is beyond the end of this note."}
            window = raw_remaining[offset:offset + max_chars]
            start = offset
            end = offset + len(window)

        result = {
            "relative_path": relative_path,
            "name": real.split("/")[-1],
            "frontmatter_parse": parse_state,
            "frontmatter": dict(frontmatter) if frontmatter else None,
            "authorship_kind": authorship_status(frontmatter)[0],
            "authorship_author": authorship_status(frontmatter)[1],
            "provenance_kind": provenance_status(frontmatter)[0],
            "provenance_value": provenance_status(frontmatter)[1],
            "content": window,
            "empty": len(raw_remaining) == 0,
            "total_chars": len(raw_remaining),
        }
        # Provenance decides authorship over the WHOLE body (never the delivered window).
        result["authorship"], result["authorship_text"] = verify_authorship(frontmatter, raw_remaining, event_lookup,
                                                                             owner_note_lookup)
        if max_chars is not None:
            if start > 0 or end < len(raw_remaining):
                result["content_window"] = {
                    "offset": start,
                    "chars": len(window),
                    "total_chars": len(raw_remaining),
                    "has_more": end < len(raw_remaining),
                    "next_request": (
                        json.dumps({"offset": end, "max_chars": max_chars}, separators=(",", ":"))
                        if end < len(raw_remaining) else None
                    ),
                }
        return result, None


def now_utc_iso():
    """Small helper for fixtures/drivers -- hosts may create explicit
    notes with real timestamps without duplicating datetime knowledge."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

# ------------------------------------------------------ authorship verification

VERIFIED_CLARK = "yours_confirmed"               # written by Clark through ANAXI; unchanged since
VERIFIED_CLARK_EDITED = "yours_edited_since"      # written by Clark through ANAXI; body changed since
CLARK_UNCONFIRMED = "marked_clark_unconfirmed"    # says author: clark, but ANAXI cannot confirm it
ATTRIBUTED = "attributed"                         # someone's name on it -- the note's own claim
ATTRIBUTED_SHARED = "attributed_shared"           # several names -- the note's own claim
MARKED_HOST = "marked_host"                       # says the host/ANAXI generated it -- a claim
NO_AUTHOR = "no_author_recorded"                  # available to Clark; authorship not established
OWNER_CONFIRMED = "owner_confirmed"              # written by the owner through ANAXI; unchanged since
OWNER_EDITED = "owner_edited_since"              # written by the owner through ANAXI; body changed since
OWNER_PROVENANCE = "owner_authored_via_anaxi"

HOST_AUTHOR_VALUES = frozenset({"host", "anaxi", "system"})

AUTHORSHIP_TEXT = {
    VERIFIED_CLARK: "yours: written by you through ANAXI (canonical record {event}); unchanged since",
    VERIFIED_CLARK_EDITED: "written by you through ANAXI (canonical record {event}), but its text has "
                           "changed since you wrote it (edited outside ANAXI)",
    CLARK_UNCONFIRMED: "marked author: clark, but ANAXI holds no record confirming you wrote it",
    ATTRIBUTED: "attributed to {author} (the note's own claim; ANAXI has not verified it)",
    ATTRIBUTED_SHARED: "attributed to {author} jointly (the note's own claim; ANAXI has not verified it)",
    MARKED_HOST: "marked as host-generated ({author}); not your words",
    NO_AUTHOR: "no author recorded: available to you; whose it is is not established",
    OWNER_CONFIRMED: "written by {author} (the owner) through ANAXI (canonical record {event}); unchanged since",
    OWNER_EDITED: "written by {author} (the owner) through ANAXI (canonical record {event}), but its text has "
                  "changed since it was written (edited outside ANAXI)",
}


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def verify_authorship(frontmatter, body, event_lookup=None, owner_note_lookup=None):
    """(kind, text).  ``event_lookup(event_id) -> bool`` answers whether ANAXI's canonical provenance
    holds that event; None (no database reachable) means nothing can be confirmed.  Never infers from
    content, filename, folder or delivery success.

    ``owner_note_lookup(event_id) -> {actor_id, label, content_sha256} | None`` reads the canonical
    owner_vault_note record.  A note is the owner's (confirmed) only when that record exists, was authored
    by the canonical owner, names the same actor and the same written body hash as the note's frontmatter;
    the owner's name shown is the canonical actor's label, never the note's own claim.  Otherwise the note's
    author line stays exactly what it was: a claim (attributed)."""
    fm = frontmatter or {}
    raw = (fm.get("author") or "").strip()
    if not raw:
        return NO_AUTHOR, AUTHORSHIP_TEXT[NO_AUTHOR]
    if fm.get("provenance") == OWNER_PROVENANCE and owner_note_lookup is not None and fm.get("event_id"):
        record = owner_note_lookup(fm.get("event_id"))
        if (record and record.get("actor_id") == fm.get("anaxi_actor_id")
                and record.get("content_sha256") and record.get("content_sha256") == fm.get("content_sha256")):
            kind = OWNER_CONFIRMED if record["content_sha256"] == body_sha256(_written_body(body)) else OWNER_EDITED
            return kind, AUTHORSHIP_TEXT[kind].format(author=record.get("label"), event=fm.get("event_id"))
    names = [n.strip() for n in re.split(r",|;|\band\b|&", raw) if n.strip()]
    if len(names) > 1:
        return ATTRIBUTED_SHARED, AUTHORSHIP_TEXT[ATTRIBUTED_SHARED].format(author=", ".join(names))
    if raw.lower() in HOST_AUTHOR_VALUES:
        return MARKED_HOST, AUTHORSHIP_TEXT[MARKED_HOST].format(author=raw)
    if raw.lower() not in CLARK_AUTHOR_VALUES:
        return ATTRIBUTED, AUTHORSHIP_TEXT[ATTRIBUTED].format(author=raw)
    event = fm.get("event_id") or fm.get("source_event_id")
    recorded_sha = fm.get("content_sha256")
    if not event or event_lookup is None or not event_lookup(event):
        return CLARK_UNCONFIRMED, AUTHORSHIP_TEXT[CLARK_UNCONFIRMED]
    if recorded_sha and recorded_sha == body_sha256(_written_body(body)):
        return VERIFIED_CLARK, AUTHORSHIP_TEXT[VERIFIED_CLARK].format(event=event)
    return VERIFIED_CLARK_EDITED, AUTHORSHIP_TEXT[VERIFIED_CLARK_EDITED].format(event=event)


def _written_body(body: str) -> str:
    """Both writers put exactly one blank line after the closing fence; the hash is over what followed."""
    return body[1:] if body.startswith("\n") else body


# ---------------------------------------------------------------- creation

VAULT_NOTE_EXISTS = "VAULT_NOTE_EXISTS"
VAULT_NOTE_EMPTY_CONTENT = "VAULT_NOTE_EMPTY_CONTENT"
VAULT_REPLAY_CONFLICT = "VAULT_REPLAY_CONFLICT"
MAX_NOTE_NAME_CHARS = 120
_UNSAFE_NAME = re.compile(r"[\x00-\x1f<>:\"|?*]")


def _note_name(relative_path, content, source_event_id):
    cleaned = (relative_path or "").strip().replace("\\", "/")
    if not cleaned:
        digest = hashlib.sha256((source_event_id or content).encode("utf-8")).hexdigest()[:8]
        cleaned = f"Clark note {datetime.date.today().isoformat()} {digest}"
    if _UNSAFE_NAME.search(cleaned) or len(cleaned) > MAX_NOTE_NAME_CHARS:
        return None
    return cleaned if cleaned.lower().endswith(NOTE_EXTENSION) else cleaned + NOTE_EXTENSION


def owner_note_name(title):
    """The vault-relative name of an owner note: the title as given (a required name -- never generated)."""
    cleaned = (title or "").strip().replace("\\", "/")
    if not cleaned or _UNSAFE_NAME.search(cleaned) or len(cleaned) > MAX_NOTE_NAME_CHARS:
        return None
    return cleaned if cleaned.lower().endswith(NOTE_EXTENSION) else cleaned + NOTE_EXTENSION


def create_owner_note(workspace, name, content, *, owner_actor_id, owner_label, event_id):
    """Write ONE new owner-authored note, bound to its already-committed canonical owner_vault_note event.
    Exclusive create: never overwrites or edits anything (an existing name is VAULT_NOTE_EXISTS, nothing
    written).  Returns (result, None) or (None, failure)."""
    real, failure = workspace.resolve_note_path(name)
    if failure is not None:
        return None, failure
    frontmatter = (
        "---\n"
        f"author: {owner_label}\n"
        f"provenance: {OWNER_PROVENANCE}\n"
        f"created_at: {now_utc_iso()}\n"
        f"event_id: {event_id}\n"
        f"anaxi_actor_id: {owner_actor_id}\n"
        f"content_sha256: {body_sha256(content)}\n"
        "---\n\n"
    )
    os.makedirs(os.path.dirname(real), exist_ok=True)
    try:
        with open(real, "x", encoding="utf-8") as fh:
            fh.write(frontmatter + content)
    except FileExistsError:
        return None, {"code": VAULT_NOTE_EXISTS,
                      "rationale": f"A note named {name!r} already exists; nothing was overwritten or written."}
    return {"relative_path": name, "created": True}, None


def author_owner_note(data_dir, *, workspace, authority, pipeline_key, title, text):
    """The owner panel's "Write a note to the shared workspace": (ok, message).  Order: validate, refuse an
    existing name (nothing recorded), commit the canonical owner_vault_note event (path + body hash, authored
    by the owner's re-validated authority), then exclusive-create the note bound to that event.  If the name
    was taken in between, the event stays as the owner's recorded act and the message says the note was not
    written -- nothing is overwritten, deleted or claimed."""
    import sqlite3
    import time as _time
    import human_session_binding as hsb
    if authority is None:
        return False, "No bound owner for this process; nothing was written."
    if not isinstance(text, str) or not text.strip():
        return False, "A note needs text; nothing was written."
    name = owner_note_name(title)
    if name is None:
        return False, "That title is not usable as a note name (empty, control characters, reserved symbols or too long)."
    if not os.path.isdir(workspace.vault_root):
        return False, "The shared vault does not exist; nothing was written."
    real, failure = workspace.resolve_note_path(name)
    if failure is not None:
        return False, failure.get("rationale") or "That note name is not addressable; nothing was written."
    if os.path.exists(real):
        return False, f"A note named {name!r} already exists; nothing was overwritten, written or recorded."
    try:
        event_id = hsb.record_owner_vault_note(data_dir, pipeline_key=pipeline_key, authority=authority,
                                               relative_path=name, content_sha256=body_sha256(text),
                                               occurred_at=int(_time.time()))
    except hsb.HumanWakingInputWriteError as exc:
        return False, f"Not written: {exc}"
    conn = sqlite3.connect(f"file:{data_dir}/anaxi_provenance.db?mode=ro", uri=True)
    try:
        record = hsb.owner_vault_note_record(conn, event_id)
    finally:
        conn.close()
    result, failure = create_owner_note(workspace, name, text, owner_actor_id=authority.authenticated_actor_id,
                                        owner_label=(record or {}).get("label") or authority.authenticated_actor_id,
                                        event_id=event_id)
    if failure is not None:
        return False, (f"Your note was recorded (canonical record {event_id}) but not written: "
                       f"{failure.get('rationale')}")
    return True, f"Wrote {name!r} to the shared workspace as your note (canonical record {event_id})."


def create_note(workspace, relative_path, content, *, clark_actor_id, source_event_id=None):
    """Create ONE new Clark-authored note.  Never overwrites or edits anything: an existing name is
    VAULT_NOTE_EXISTS (nothing written).  Bound to the canonical message that occasioned it, so an exact
    replay of that message returns the already-created note instead of writing twice.  Returns
    (result, None) or (None, failure)."""
    if not isinstance(content, str) or not content.strip():
        return None, {"code": VAULT_NOTE_EMPTY_CONTENT, "rationale": "A note needs text; nothing was written."}
    name = _note_name(relative_path, content, source_event_id)
    if name is None:
        return None, {"code": VAULT_TARGET_OUTSIDE_ROOT,
                      "rationale": "That note name is not usable (control characters, reserved symbols or too long)."}
    real, failure = workspace.resolve_note_path(name)
    if failure is not None:
        return None, failure
    if not os.path.isdir(workspace.vault_root):
        return None, {"code": VAULT_NOTE_NOT_FOUND, "rationale": "The shared vault does not exist; nothing was written."}
    body = content
    frontmatter = (
        "---\n"
        "author: clark\n"
        "provenance: clark_authored_via_anaxi\n"
        f"created_at: {now_utc_iso()}\n"
        + (f"source_event_id: {source_event_id}\n" if source_event_id else "")
        + f"anaxi_actor_id: {clark_actor_id}\n"
        f"content_sha256: {body_sha256(body)}\n"
        "kardia_linked: false\n"
        "---\n\n"
    )
    os.makedirs(os.path.dirname(real), exist_ok=True)
    try:
        with open(real, "x", encoding="utf-8") as fh:
            fh.write(frontmatter + body)
    except FileExistsError:
        existing, read_failure = workspace.read_note(name)
        fm = (existing or {}).get("frontmatter") or {}
        if (source_event_id and read_failure is None and fm.get("source_event_id") == source_event_id
                and fm.get("content_sha256") == body_sha256(body)):
            return dict(existing, created=False, replayed=True), None     # exact replay: nothing new
        if source_event_id and fm.get("source_event_id") == source_event_id:
            return None, {"code": VAULT_REPLAY_CONFLICT,
                          "rationale": "This message already created a different note; nothing was written."}
        return None, {"code": VAULT_NOTE_EXISTS,
                      "rationale": f"A note named {name!r} already exists; nothing was overwritten or written."}
    result, _ = workspace.read_note(name)
    return dict(result, created=True, replayed=False), None
