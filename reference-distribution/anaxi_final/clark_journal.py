"""
Anaxi -- Clark's Obsidian journal, Phase 1: isolated write tool only.

Not connected to the live waking loop. Per the explicit project design
principle: Obsidian is a place, not an
extension of REM. Nothing written here ever becomes a Kardia node, REM
candidate, salience-bearing memory, or governance proposal --
confirmed directly against the real code, not assumed: REM's
load_recent_conversation() reads only anaxi_log.jsonl and never scans
any other directory, so there is no mechanical path from a journal
entry to consolidation at all, regardless of what this tool does.

Path containment resolves to the real, canonical path on both sides
before comparing -- not a naive prefix check on the unresolved string,
which would be vulnerable to '..' traversal, symlinks, or an absolute
path silently overriding the intended root (a well-known os.path.join
pitfall). Never raises on an out-of-bounds attempt; returns a clear
rejection instead, so a caller can handle it without a crash.

Provenance frontmatter on every entry, per the design spec, plus one
addition of my own beyond what was explicitly specified:
kardia_linked: false, making the architectural guarantee visible
inside the artifact itself, not only true by virtue of code elsewhere.
"""

import datetime
import hashlib
import json
import os
import re

from signal_matcher import normalize


class StaleSchemaError(Exception):
    """Raised when a decision payload contains 'create_artifact' --
    the field name from the pre-authorization-boundary schema,
    superseded by the deterministic signal gate and construction_status.
    Deliberately NOT a quiet, ordinary failure path: if this ever
    fires, it means the model was given (or is still using) an old
    prompt, or something upstream produced a stale-shaped payload.
    That should be conspicuous -- caught and logged at the call site
    in llama_anaxi.py, never silently absorbed into an ordinary
    'insufficient_information' result, per the explicit requirement
    that a stale field must be loud rather than invisible."""
    pass


MAX_NAME_SUFFIX = 50


def _exclusive_note_path(candidate_real: str):
    """The vault is shared: an existing note (anyone's) is never overwritten.  The first free name of
    '<name>.md', '<name> (2).md', ... is created exclusively; None when none is free."""
    stem, ext = os.path.splitext(candidate_real)
    for n in range(1, MAX_NAME_SUFFIX + 1):
        path = candidate_real if n == 1 else f"{stem} ({n}){ext}"
        try:
            return path, open(path, "x", encoding="utf-8")
        except FileExistsError:
            continue
    return None, None


def write_journal_entry(workspace_root: str, filename: str, content: str,
                         waking_model_tag: str = "unknown", *, event_id: str = None,
                         pipeline_id: str = None) -> dict:
    root_real = os.path.realpath(workspace_root)
    candidate = os.path.join(workspace_root, filename)
    candidate_real = os.path.realpath(candidate)

    if not (candidate_real == root_real or candidate_real.startswith(root_real + os.sep)):
        return {
            "status": "rejected",
            "reason": f"Path escapes the workspace root: {filename!r} resolved outside {workspace_root!r}.",
        }

    if not candidate_real.endswith(".md"):
        return {"status": "rejected", "reason": "Journal entries must be .md files."}

    os.makedirs(os.path.dirname(candidate_real), exist_ok=True)

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    frontmatter = (
        "---\n"
        "author: clark\n"
        # LEGACY PIPELINE DISCRIMINATOR, not exact model provenance --
        # kept literally "llama" on purpose, matching llama_anaxi.py's
        # log_entry() field of the same name and for the same reason.
        # Retained temporarily for backward compatibility; a rename/
        # migration belongs to the forthcoming identity/provenance
        # layer, not this bounded substrate switch.
        "substrate: llama\n"
        f"waking_model_tag: {waking_model_tag}\n"
        f"created_at: {timestamp}\n"
        "provenance: model_generated\n"
        "kardia_linked: false\n"
        # Canonical identity (written only post-commit, Q-05): the waking turn that authored this
        # artifact and its pipeline, plus the body's hash, so a later reader can tell "written by
        # Clark through ANAXI, unchanged" from "edited since" or "merely marked clark".
        + (f"event_id: {event_id}\n" if event_id else "")
        + (f"pipeline_id: {pipeline_id}\n" if pipeline_id else "")
        + f"content_sha256: {hashlib.sha256(content.encode('utf-8')).hexdigest()}\n"
        + "---\n\n"
    )
    path, handle = _exclusive_note_path(candidate_real)
    if handle is None:
        return {"status": "rejected",
                "reason": f"Every name for {filename!r} is already taken in the shared vault; nothing was overwritten."}
    with handle:
        handle.write(frontmatter + content)

    return {
        "status": "success",
        "path": path,
        "bytes_written": len(frontmatter) + len(content),
    }


# ----------------------------------------------------------------------
# Phase 2A -- host-side handling of Pass 1's artifact-judgment decision.
# Parses, validates against the fixed enum, executes if valid, and
# builds the factual context string Pass 2 actually receives. Never
# trusts Pass 1's raw output as already-safe -- confirmed necessary by
# this whole project's own history with this model's JSON reliability.
# ----------------------------------------------------------------------

_VALID_ARTIFACT_TYPES = {"journal"}
_VALID_NAMESPACES = {"journal"}
_VALID_SCOPES = {"personal"}

# journal namespace -> filename prefix. Namespace-to-path mapping lives
# entirely here, host-side -- the model only ever sees the namespace
# string "journal", never a real path.
_NAMESPACE_SUBDIR = {"journal": "Journal"}


def _safe_filename_from_title(title) -> str:
    """Sanitizes a model-supplied title into a safe filename fragment
    -- never passes the raw title through as a path fragment. Falls
    back to a timestamp-based name if title is missing or empty after
    sanitization, so a malformed/empty title can't produce an invalid
    or unintended path."""
    if not title or not isinstance(title, str):
        title = ""
    safe = re.sub(r"[^A-Za-z0-9 _-]", "", title).strip()
    safe = re.sub(r"\s+", "-", safe)
    if not safe:
        safe = datetime.datetime.now(datetime.timezone.utc).strftime("entry-%Y%m%dT%H%M%S")
    return safe[:80]


def _shares_substantial_overlap(content: str, source_text: str, n_gram_size: int = 6) -> bool:
    """General, name-independent overlap check: does content contain any
    contiguous 6+ word sequence also present in the original source
    prompt? Catches near-verbatim copying regardless of which specific
    names or facts are involved -- doesn't require a hardcoded list of
    people/places, so it generalizes to anyone mentioned in the future,
    not just currently-known family members. Known limitation, stated
    plainly rather than overclaimed: this catches blatant or
    near-verbatim reuse, not a genuinely well-paraphrased misattribution
    that shares no long exact word sequence with the source."""
    def normalize(t):
        return re.sub(r"[^a-z0-9\s]", "", t.lower()).split()

    content_words = normalize(content)
    source_words = normalize(source_text)
    if len(content_words) < n_gram_size or len(source_words) < n_gram_size:
        return False

    source_ngrams = {
        tuple(source_words[i:i + n_gram_size])
        for i in range(len(source_words) - n_gram_size + 1)
    }
    content_ngrams = {
        tuple(content_words[i:i + n_gram_size])
        for i in range(len(content_words) - n_gram_size + 1)
    }
    return len(source_ngrams & content_ngrams) > 0


def _claims_same_relationship_as_source(content: str, source_text: str) -> bool:
    """Semantic check, not lexical: does content contain a first-person
    possessive claim about a relationship category ('my daughter', 'my
    wife') that the SOURCE speaker also claimed as their own? Catches
    the actual pattern regardless of paraphrasing or whether any proper
    name is ever used at all -- confirmed necessary directly against a
    real case (entry 85) where neither the source nor the misattributed
    content ever names a specific person, only the relationship word
    itself gets reused. The relationship word list is fixed and general
    -- not tied to any specific person's name -- so this generalizes to
    anyone mentioned in the future, the same way _shares_substantial_overlap
    does for exact phrasing, just at the level of relationship claims
    instead of words.

    Recognizes two general first-person constructions as the SAME claim
    -- "my X" and "I have X" -- checked in both directions, confirmed
    necessary directly: a real case slipped through where the source
    said "I have two daughters" and the misattributed content said "my
    daughters," which share the relationship but not the phrasing, so
    checking only one construction missed it.

    Also recognizes a narrow named third-person source construction
    ("Jordan has two kids") when content re-claims that same relationship
    in first person.  The subject must be one or two proper-name-shaped
    tokens and common sentence determiners are excluded, so generic sources
    such as "the shelter has two dogs" do not become identity claims.
    Correctly attributed third-person content ("Jordan's kids") remains
    allowed because only first-person content claims are rejected."""
    relationship_words = (
        r"daughter|daughters|son|sons|wife|husband|spouse|child|children|kids?|"
        r"family|dog|dogs|cat|cats|pet|pets|mother|father|parent|parents|"
        r"mom|dad|mum"
    )
    my_pattern = re.compile(rf"\bmy\s+(?:\w+\s+){{0,2}}({relationship_words})\b", re.IGNORECASE)
    have_pattern = re.compile(
        rf"\bI\s+(?:have|had|'ve got|have got)\s+(?:\w+\s+){{0,3}}({relationship_words})\b",
        re.IGNORECASE,
    )
    named_third_person_pattern = re.compile(
        rf"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:has|had)\s+"
        rf"(?:\w+\s+){{0,3}}({relationship_words})\b"
    )

    def extract_claims(text):
        return {m.lower() for m in my_pattern.findall(text)} | {m.lower() for m in have_pattern.findall(text)}

    source_claims = extract_claims(source_text)
    content_claims = extract_claims(content)
    excluded_subjects = {"The", "A", "An", "This", "That", "It", "There"}
    third_person_claims = {
        relationship.lower()
        for subject, relationship in named_third_person_pattern.findall(source_text)
        if subject.split()[0] not in excluded_subjects
    }
    return bool((source_claims | third_person_claims) & content_claims)


def prepare_artifact_decision(raw_json_output: str, workspace_root: str, source_prompt: str = "",
                               waking_model_tag: str = "unknown") -> dict:
    """
    Parses the construction model's raw JSON output and validates it --
    but does NOT perform the durable write. Split out of what was
    previously the single process_artifact_decision() function, per
    the human-adopted Q-05 decision: canonical provenance must commit
    before the journal/artifact file becomes durable, but bounded-clause
    rendering (which needs to know whether construction succeeded)
    happens earlier, before any canonical write is even attempted --
    so the DECISION must be available early, while the durable WRITE
    itself must not happen until later. Call finalize_artifact_write()
    on this function's return value, and ONLY after canonical
    provenance has committed, to actually make the artifact durable.

    Per the settled authority-boundary decision: this function no
    longer decides WHETHER to create anything. That's already been
    decided, deterministically, by signal_matcher.py's classify_signal(),
    before this is ever called. This function only validates a
    downstream construction CLAIM -- and does not trust that claim
    outright. In particular, "construction_status": "success" does not
    mean "therefore write it" -- it means "the model claims it
    succeeded," and identified_referent is independently, mechanically
    verified against source_prompt before anything is queued to write.

    Raises StaleSchemaError (not returned as an ordinary result) if the
    payload still contains 'create_artifact' -- the pre-authorization
    -boundary field name. This is deliberate: a stale schema should be
    loud, not silently absorbed into a normal "insufficient_information"
    outcome. Caught and logged at the call site in llama_anaxi.py,
    which has its own responsibility to keep this from crashing an
    ordinary waking turn.

    Otherwise never raises on malformed input: a JSON parse failure, a
    missing field, or an out-of-enum value are all treated as
    "insufficient_information," with the specific reason recorded where
    available, rather than crashing the turn.

    Distinguishes three outcomes, not two: "insufficient_information"
    (construction_status wasn't "success" -- a complete, valid,
    expected result, not a failure) from "failed" (construction_status
    claimed "success" but the referent couldn't be verified, or another
    validation step rejected it) from validation having fully succeeded
    (artifact_created=True here means "ready to write," not yet "written"
    -- see finalize_artifact_write()). Conflating "nothing was attempted"
    with "something was attempted and failed" would let Clark's own
    words in Pass 2 be grounded in a false implication about what
    actually happened -- the same reasoning as the original
    None-vs-FAILED distinction, now applied to the same three-way
    contract.

    Returns a dict always containing:
      - "artifact_created": bool (True here means validation passed and
        a write is queued -- see finalize_artifact_write() for when it
        actually becomes durable)
      - "pass2_context": the exact string Pass 2 should receive
      - "detail": present only for a final (non-pending) outcome
      - "_pending_write": present only when artifact_created is True;
        opaque to every caller except finalize_artifact_write()
    """
    try:
        decision = json.loads(raw_json_output)
    except (json.JSONDecodeError, TypeError) as e:
        return {
            "artifact_created": False,
            "pass2_context": "Artifact action:\nNone.",
            "detail": {"status": "insufficient_information", "reason": f"Pass 1 output was not valid JSON: {e}"},
        }

    if not isinstance(decision, dict):
        return {
            "artifact_created": False,
            "pass2_context": "Artifact action:\nNone.",
            "detail": {"status": "insufficient_information", "reason": "Pass 1 output was not a JSON object."},
        }

    if "create_artifact" in decision:
        raise StaleSchemaError(
            f"Payload contains 'create_artifact' -- pre-authorization-boundary "
            f"schema field, no longer valid. Full payload: {decision!r}"
        )

    construction_status = decision.get("construction_status")
    identified_referent = decision.get("identified_referent")

    if construction_status != "success":
        return {
            "artifact_created": False,
            "pass2_context": "Artifact action:\nNone.",
            "detail": {"status": "insufficient_information", "reason": decision.get("identified_referent")},
        }

    artifact_type = decision.get("artifact_type")
    namespace = decision.get("namespace")
    scope = decision.get("artifact_scope")
    title = decision.get("title")
    content = decision.get("content")

    def _failed(reason: str) -> dict:
        return {
            "artifact_created": False,
            "pass2_context": f"Artifact action:\nFAILED -- {reason}",
            "detail": {"status": "failed", "reason": reason},
        }

    if not identified_referent or not isinstance(identified_referent, str):
        return _failed("construction_status was 'success' but identified_referent was missing -- claim rejected.")
    if source_prompt and normalize(identified_referent) not in normalize(source_prompt):
        return _failed(
            f"identified_referent {identified_referent!r} does not appear in the "
            f"source turn -- construction claimed success but the referent "
            f"could not be verified, not merely trusted."
        )

    if artifact_type not in _VALID_ARTIFACT_TYPES:
        return _failed(f"artifact_type {artifact_type!r} is not a supported type.")
    if namespace not in _VALID_NAMESPACES:
        return _failed(f"namespace {namespace!r} is not a supported namespace.")
    if scope not in _VALID_SCOPES:
        return _failed(f"artifact_scope {scope!r} is not a supported scope.")
    if not content or not isinstance(content, str):
        return _failed("content was missing or empty.")
    if source_prompt and _shares_substantial_overlap(content, source_prompt):
        return _failed(
            "content shares a substantial word sequence with the source "
            "conversation -- rejected as likely misattributed/copied rather "
            "than genuinely Clark's own words."
        )
    if source_prompt and _claims_same_relationship_as_source(content, source_prompt):
        return _failed(
            "content claims the same first-person relationship (e.g. 'my "
            "daughter', 'my wife') that the source speaker claimed as their "
            "own -- rejected as likely claiming someone else's family or "
            "relationships as Clark's own, regardless of exact wording."
        )

    safe_title = _safe_filename_from_title(title)
    subdir = _NAMESPACE_SUBDIR[namespace]
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    filename = f"{subdir}/{timestamp}-{safe_title}.md"

    return {
        "artifact_created": True,
        "pass2_context": f"Artifact action:\nCreated: {filename}\nTitle: {title!r}",
        "artifact_title": title,
        "_pending_write": {
            "workspace_root": workspace_root,
            "filename": filename,
            "content": content,
            "waking_model_tag": waking_model_tag,
            "title": title,
        },
    }


def finalize_artifact_write(prepared_result: dict, *, event_id: str = None, pipeline_id: str = None) -> dict:
    """Call ONLY after canonical provenance has committed, for the
    native waking path (Q-05: canonical-before-artifact ordering -- the
    durable journal/artifact file must not become durable before
    canonical provenance does).

    Takes the return value of prepare_artifact_decision(). If that
    result has no "_pending_write" (every non-success outcome), there
    is nothing to finalize -- returns it unchanged. Otherwise performs
    the actual durable write and returns a result in EXACTLY the shape
    process_artifact_decision() has always returned
    (artifact_created/pass2_context/artifact_title/detail), including
    its ORIGINAL, unchanged handling of a write failure (OSError, or
    write_journal_entry() itself rejecting the path/extension): caught
    and reported as an ordinary "failed" result, exactly as
    process_artifact_decision() has always done -- this function is
    also what process_artifact_decision() itself calls, for every one
    of its other, non-native-waking callers, and their behavior must
    not change.

    This function deliberately does NOT decide what a post-canonical-
    commit write failure should mean for the caller -- a synchronous
    process_artifact_decision() caller (no provenance ordering of its
    own) and the native waking path (which has already committed
    canonical provenance and generated a reply assuming success) need
    different answers to that question. That decision belongs to, and
    is made by, llama_anaxi.py's run_waking_turn() specifically -- see
    its own comment at the call site."""
    pending = prepared_result.get("_pending_write")
    if pending is None:
        return prepared_result

    try:
        result = write_journal_entry(
            pending["workspace_root"], pending["filename"], pending["content"],
            waking_model_tag=pending["waking_model_tag"], event_id=event_id, pipeline_id=pipeline_id,
        )
    except OSError as e:
        return {
            "artifact_created": False,
            "pass2_context": f"Artifact action:\nFAILED -- Unexpected OS error during write: {e}",
            "detail": {"status": "failed", "reason": f"Unexpected OS error during write: {e}"},
        }

    if result["status"] != "success":
        reason = result.get("reason", "write rejected for an unspecified reason.")
        return {
            "artifact_created": False,
            "pass2_context": f"Artifact action:\nFAILED -- {reason}",
            "detail": {"status": "failed", "reason": reason},
        }

    return {
        "artifact_created": True,
        "pass2_context": prepared_result["pass2_context"],
        "artifact_title": prepared_result["artifact_title"],
        "detail": result,
    }


def process_artifact_decision(raw_json_output: str, workspace_root: str, source_prompt: str = "",
                               waking_model_tag: str = "unknown") -> dict:
    """Unchanged, synchronous, single-call contract: validates and, if
    -- and only if -- everything checks out, writes immediately,
    returning a result describing exactly what happened. Every existing
    caller of this exact function (test_clark_journal.py,
    test_gemma_substrate_switch.py, and any future non-native-waking
    caller) keeps this same synchronous behavior verbatim.

    The native Llama/Gemma waking path (llama_anaxi.py) does NOT call
    this function -- per the human-adopted Q-05 decision, it calls
    prepare_artifact_decision() early (to determine the bounded clause)
    and finalize_artifact_write() separately, only after canonical
    provenance commits, so the durable artifact write itself is
    deferred. Both entry points share the exact same validation/write
    logic underneath (prepare_artifact_decision() then
    finalize_artifact_write() immediately, right here) -- nothing about
    what gets validated, rejected, or written has changed; only the
    native waking path's timing of the write itself has."""
    return finalize_artifact_write(
        prepare_artifact_decision(raw_json_output, workspace_root, source_prompt, waking_model_tag)
    )
