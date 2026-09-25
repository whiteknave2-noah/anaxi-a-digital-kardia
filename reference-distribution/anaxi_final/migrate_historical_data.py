"""
Anaxi -- Real implementation of the historical migration described in
the frozen spec (DESIGN_NOTE_identity_provenance_schema.md, SHA-256
7761d132aded85f18ca519aac6b189d541d22eee336755f8b8b3e02c885751c2),
section 7. Populates a fresh anaxi_provenance.db from anaxi_log.jsonl,
and additively alters + backfills relational_events / turn_generation_log
/ nodes / active_kardia / kardia_history / proposals.

STRUCTURALLY REHEARSAL-ONLY -- there is no live-mode flag anywhere in
this module. Every entry point calls assert_is_rehearsal_dir() first,
which refuses to proceed without a correctly-contented marker file
AND refuses outright if the path resolves to this module's own
(live) directory, regardless of any marker present.

Run (rehearsal only):
    python migrate_historical_data.py <rehearsal_dir>
"""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime

from provenance_schema import derive_historical_event_id, derive_stable_id


class MigrationStopCondition(Exception):
    """Raised when the migration encounters data it is not authorized
    to silently absorb, or a detected conflict in existing non-NULL
    provenance. Stop and report, never guess or overwrite."""
    pass


class NotARehearsalDirectoryError(Exception):
    """Raised by assert_is_rehearsal_dir() -- the structural guard
    that keeps this module rehearsal-only."""
    pass


class RehearsalSafetyError(Exception):
    """Raised by create_rehearsal_dir() when a destination, source, or
    child file fails one of the aliasing/traversal/symlink safety
    checks -- these are hard refusals, not warnings."""
    pass


class IncompleteEventBundleError(Exception):
    """Raised when a partial (some-but-not-all-parts-present) event
    bundle is detected -- this should be structurally impossible under
    the one-transaction write path, so its presence indicates real
    corruption that must be reported, never silently patched over."""
    pass


# =============================================================================
# Rehearsal-only structural enforcement (no live mode exists anywhere
# in this module -- there is no parameter, flag, or environment
# variable that bypasses this check).
# =============================================================================
REHEARSAL_MARKER_FILENAME = ".rehearsal_marker"
REHEARSAL_MARKER_CONTENT = "ANAXI-PROVENANCE-REHEARSAL-ONLY-V1"


def _live_dir() -> str:
    return os.path.realpath(os.path.dirname(os.path.abspath(__file__)))


def _assert_no_symlink_in_ancestry(path: str) -> None:
    """Refuses if any component of path's ancestry is a symlink --
    catches a rehearsal directory (or one of its parents) being itself
    an alias for somewhere else, not just individual file children."""
    real = os.path.realpath(path)
    absolute = os.path.abspath(path)
    normalized_real = os.path.normcase(os.path.normpath(real))
    normalized_absolute = os.path.normcase(os.path.normpath(absolute))
    if normalized_real == normalized_absolute:
        return

    # macOS exposes its conventional temporary root lexically through
    # /var while the filesystem canonical path is /private/var.  That
    # OS-owned prefix alias is not a caller-controlled rehearsal alias.
    # Permit only the exact canonical rewrite of tempfile.gettempdir();
    # any additional symlink below that root still changes the expected
    # suffix and is refused.
    temp_absolute = os.path.abspath(tempfile.gettempdir())
    try:
        under_temp_root = os.path.commonpath((absolute, temp_absolute)) == temp_absolute
    except ValueError:
        under_temp_root = False
    if under_temp_root:
        expected_real = os.path.join(
            os.path.realpath(temp_absolute), os.path.relpath(absolute, temp_absolute),
        )
        if normalized_real == os.path.normcase(os.path.normpath(expected_real)):
            return

    if normalized_real != normalized_absolute:
        raise RehearsalSafetyError(
            f"{path!r} resolves through a symlink somewhere in its ancestry "
            f"(realpath={real!r} != abspath={absolute!r}) -- refusing."
        )


def _validate_dest_name(dest_name: str) -> None:
    """dest_name must be a bare filename -- no path separators, no
    '..' traversal, not absolute. Rejects any attempt to escape the
    rehearsal directory via a path-bearing destination name."""
    if os.path.isabs(dest_name):
        raise RehearsalSafetyError(f"destination name {dest_name!r} must not be an absolute path")
    if dest_name in ("", ".", ".."):
        raise RehearsalSafetyError(f"destination name {dest_name!r} is not a valid filename")
    normalized = dest_name.replace("\\", "/")
    if "/" in normalized:
        raise RehearsalSafetyError(f"destination name {dest_name!r} must be a bare filename, no path separators")


def _write_all_bytes(fd: int, data: bytes) -> None:
    """Guarantees the COMPLETE buffer is written. os.write()'s API
    contract does NOT guarantee a single call writes every requested
    byte (a 'short write') -- the earlier one-shot os.write(fd, data)
    silently trusted this never to happen. Loops, feeding the
    remainder back in, until every byte is confirmed written; a
    non-positive return from os.write() is treated as a failed/stalled
    write, not silently retried forever."""
    total_written = 0
    length = len(data)
    while total_written < length:
        n = os.write(fd, data[total_written:])
        if n <= 0:
            raise RehearsalSafetyError(
                f"os.write() returned {n} while copying -- treating as a failed write, not retrying forever."
            )
        total_written += n


def create_rehearsal_dir(dest_dir: str, source_files: dict) -> None:
    """The only sanctioned way to produce a directory this module's
    functions will operate on. source_files: {dest_filename: source_path}.

    Hardened against aliasing a live file, and against a silently
    incomplete or corrupted copy:
      - requires dest_dir to be genuinely FRESH (does not exist at all);
      - rejects any dest_name that carries a path (traversal/escape);
      - rejects any source_path that is itself a symlink or non-regular file;
      - creates each destination with O_EXCL, which fails on any
        pre-existing path (including a pre-placed symlink) rather than
        following or overwriting it;
      - writes via _write_all_bytes(), guaranteed-complete, in the
        correct binary mode;
      - verifies SHA-256(source) == SHA-256(destination) BEFORE the
        rehearsal marker is created -- a corrupted or short-written
        copy must never be blessed as a valid rehearsal directory;
      - as a final, independent check, verifies the newly-created copy
        is NOT samefile() as its source -- catches hardlinks and any
        other inode-level aliasing a symlink check alone would miss.
    """
    if os.path.lexists(dest_dir):
        raise RehearsalSafetyError(
            f"{dest_dir!r} already exists -- create_rehearsal_dir requires a fresh "
            f"destination. Remove it first if you intend to rebuild it."
        )
    os.makedirs(dest_dir)
    _assert_no_symlink_in_ancestry(dest_dir)

    for dest_name, source_path in source_files.items():
        _validate_dest_name(dest_name)
        dest_path = os.path.join(dest_dir, dest_name)

        if os.path.islink(source_path):
            raise RehearsalSafetyError(f"source {source_path!r} is a symlink -- refusing to copy through it")
        if not os.path.isfile(source_path):
            raise RehearsalSafetyError(f"source {source_path!r} is not a regular file")

        if os.path.lexists(dest_path):
            raise RehearsalSafetyError(f"destination {dest_path!r} already exists -- refusing")

        with open(source_path, "rb") as f:
            data = f.read()
        source_sha256 = hashlib.sha256(data).hexdigest()

        # os.O_BINARY is required on Windows -- os.open() defaults to
        # TEXT mode there, which silently translates \n -> \r\n even for
        # arbitrary binary bytes (e.g. a SQLite file's internal 0x0A
        # bytes), corrupting the copy. getattr(..., 0) makes this a
        # no-op on POSIX, where O_BINARY doesn't exist. Confirmed as a
        # real bug here, not a theoretical one: a 12288-byte source
        # became a 12299-byte, schema-corrupted destination without
        # this flag.
        binary_flag = getattr(os, "O_BINARY", 0)
        fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | binary_flag)
        try:
            _write_all_bytes(fd, data)
        finally:
            os.close(fd)

        # Verify byte-for-byte correctness BEFORE this copy is trusted
        # -- independent of the write path above, re-reading from disk
        # rather than trusting the in-memory buffer was what actually
        # landed on the filesystem.
        with open(dest_path, "rb") as f:
            dest_data = f.read()
        dest_sha256 = hashlib.sha256(dest_data).hexdigest()
        if dest_sha256 != source_sha256:
            raise RehearsalSafetyError(
                f"{dest_path!r} SHA-256 ({dest_sha256}) does not match source {source_path!r} "
                f"SHA-256 ({source_sha256}) after copy -- refusing to bless a corrupted or "
                f"incomplete copy by creating the rehearsal marker."
            )

        if os.path.samefile(dest_path, source_path):
            raise RehearsalSafetyError(
                f"{dest_path!r} is the SAME FILE as {source_path!r} (hardlink or other "
                f"aliasing) -- refusing. A rehearsal copy must be a genuinely independent file."
            )

    with open(os.path.join(dest_dir, REHEARSAL_MARKER_FILENAME), "w", encoding="utf-8") as f:
        f.write(REHEARSAL_MARKER_CONTENT)


def assert_is_rehearsal_dir(path: str, live_dir: str = None) -> None:
    """Structural guard, not a convention. Refuses unless:
      (a) no symlink anywhere in path's own ancestry;
      (b) a correctly-contented marker file is present (and is not
          itself a symlink);
      (c) the path does not resolve to the live directory;
      (d) no CHILD file inside path is a symlink, or is samefile() as
          a like-named file in the live directory -- covers the case
          where the root directory is entirely legitimate but one
          child (e.g. a .db file) was individually replaced with a
          symlink or hardlink into a live path. FAILS CLOSED: if
          samefile() cannot be evaluated at all (raises for any
          reason), that is treated as "aliasing could not be ruled
          out" and refused -- never silently passed through as safe.
          A live-directory file that is itself a symlink is NOT
          exempted from this comparison; samefile() follows symlinks
          on both sides, so excluding a symlinked live counterpart
          would only create a blind spot, not close one.

    live_dir defaults to this module's own (real production) directory
    -- production callers never pass an override. Tests may pass a
    simulated temporary directory instead, so this check can be
    exercised without ever creating or deleting anything in the real
    module/repository directory."""
    _assert_no_symlink_in_ancestry(path)

    marker_path = os.path.join(path, REHEARSAL_MARKER_FILENAME)
    if os.path.islink(marker_path) or not os.path.isfile(marker_path):
        raise NotARehearsalDirectoryError(
            f"{path!r} has no valid (non-symlink) {REHEARSAL_MARKER_FILENAME} -- refusing to "
            f"treat it as a rehearsal directory. Use create_rehearsal_dir() to build one."
        )
    with open(marker_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if content != REHEARSAL_MARKER_CONTENT:
        raise NotARehearsalDirectoryError(f"{path!r}'s marker file has unexpected content -- refusing.")

    if live_dir is None:
        live_dir = _live_dir()
    else:
        live_dir = os.path.realpath(live_dir)

    real_path = os.path.realpath(path)
    if real_path == live_dir:
        raise NotARehearsalDirectoryError(
            f"{path!r} resolves to the live directory -- refusing regardless of any "
            f"marker file present. There is no live mode."
        )

    for entry in os.listdir(path):
        child_path = os.path.join(path, entry)
        if os.path.islink(child_path):
            raise NotARehearsalDirectoryError(f"{child_path!r} is a symlink -- refusing")
        if not os.path.isfile(child_path):
            continue
        live_candidate = os.path.join(live_dir, entry)
        # os.path.exists() follows symlinks -- a symlinked live
        # counterpart is included here, not exempted, precisely
        # because samefile() below will correctly resolve through it
        # to whatever it actually points at.
        if os.path.exists(live_candidate):
            try:
                aliased = os.path.samefile(child_path, live_candidate)
            except OSError as e:
                raise NotARehearsalDirectoryError(
                    f"Could not verify {child_path!r} is not the same file as "
                    f"{live_candidate!r} ({e}) -- refusing (fail closed: inability to "
                    f"prove non-aliasing is treated as aliasing)."
                ) from e
            if aliased:
                raise NotARehearsalDirectoryError(
                    f"{child_path!r} is the SAME FILE as the live {live_candidate!r} "
                    f"(hardlink, symlink, or other aliasing) -- refusing, even though the "
                    f"rehearsal directory itself is otherwise legitimate."
                )


# =============================================================================
# Private routing manifest (gitignored) -- the real historical routing
# literal never lives in this source file, its comments, or any
# committed test fixture.
# =============================================================================
MANIFEST_FILENAME = "migration_manifest.local.json"

# Structural facts about each pipeline (not personal/private) stay in
# code. Only routing_constant_value (a real historical identifier) is
# private and lives in the gitignored manifest.
PIPELINE_STRUCTURE = {
    "llama": {
        "pipeline_key": "anaxi_orchestration_lineage_a",
        "routing_source": "llama_anaxi.py:USER_ID",
    },
    "claude": {
        "pipeline_key": "anaxi_orchestration_lineage_b",
        "routing_source": "claude_anaxi.py:USER_ID",
    },
}

REQUIRED_AUTHORIZATION_SCOPES = {
    "artifact_persistence": "Authorization scope covering journal/artifact persistence writes.",
}


def load_migration_manifest(manifest_path: str) -> dict:
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(
            f"{manifest_path!r} not found. The private routing manifest is gitignored "
            f"and must exist locally before migration can run."
        )
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_pipeline_map(manifest: dict) -> dict:
    """Merges code-side structural facts with the manifest's private
    routing_constant_value, and computes each pipeline's opaque,
    collision-safe pipeline_id deterministically from its pipeline_key
    -- never a hand-picked human-readable placeholder."""
    pipeline_map = {}
    for legacy_label, structure in PIPELINE_STRUCTURE.items():
        manifest_entry = (manifest.get("pipelines") or {}).get(legacy_label)
        if manifest_entry is None or "routing_constant_value" not in manifest_entry:
            raise MigrationStopCondition(
                f"migration manifest is missing routing_constant_value for {legacy_label!r}"
            )
        pipeline_map[legacy_label] = {
            "pipeline_id": derive_stable_id("pipeline", structure["pipeline_key"]),
            "pipeline_key": structure["pipeline_key"],
            "routing_source": structure["routing_source"],
            "routing_constant_value": manifest_entry["routing_constant_value"],
        }
    return pipeline_map


# =============================================================================
# Timezone-aware timestamp parsing
# =============================================================================
def parse_iso_timestamp_to_unix(ts: str) -> int:
    """Replaces an earlier version's
    time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")), which
    truncated away both the UTC offset and the microseconds and then
    applied mktime's LOCAL-timezone interpretation to a string that was
    actually UTC -- silently shifting every occurred_at by the local
    machine's UTC offset. Real anaxi_log.jsonl timestamps carry an
    explicit offset (e.g. "...+00:00"); fromisoformat parses it
    directly and .timestamp() converts correctly regardless of the
    local machine's timezone.

    Offset-naive input is REJECTED, not silently interpreted as local
    time: datetime.timestamp() on a naive datetime falls back to the
    platform's local timezone, which would reintroduce exactly the
    kind of silent-wrong-value bug this function exists to fix, just
    one step later. Every real anaxi_log.jsonl timestamp carries an
    explicit offset; one that doesn't is malformed or unexpected input,
    not something to guess a timezone for."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise MigrationStopCondition(
            f"Timestamp {ts!r} is offset-naive (no UTC offset) -- refusing to guess a "
            f"timezone. All real anaxi_log.jsonl timestamps carry an explicit offset."
        )
    return int(dt.timestamp())


# =============================================================================
# Reference-data seeding (idempotent, opaque IDs, includes authorization scopes)
# =============================================================================
def _verify_or_raise(actual: dict, expected: dict, context: str) -> None:
    """actual/expected are {field: value}. Raises MigrationStopCondition
    on any mismatch. A deterministic ID that already exists but whose
    stored fields disagree with what this run would have inserted is
    NOT safe to treat as 'already seeded' -- it indicates a collision
    or corruption, not idempotent reuse, and must stop rather than be
    silently accepted."""
    for field, expected_value in expected.items():
        actual_value = actual.get(field)
        if actual_value != expected_value:
            raise MigrationStopCondition(
                f"{context}: existing row has {field}={actual_value!r}, expected {expected_value!r}. "
                f"A deterministic ID with mismatched fields indicates a collision or corruption -- stopping."
            )


def _pipeline_description(legacy_label: str) -> str:
    """The exact deterministic description seed_reference_data would
    insert -- factored out so it can also be used to VERIFY an
    existing row, not only to construct a new one."""
    return f"Migrated reference row for legacy substrate {legacy_label!r}"


def seed_reference_data(conn: sqlite3.Connection, pipeline_map: dict, now: int) -> None:
    for legacy_label, mapping in pipeline_map.items():
        row = conn.execute(
            "SELECT pipeline_key, legacy_substrate_label, description FROM pipelines WHERE pipeline_id=?",
            (mapping["pipeline_id"],),
        ).fetchone()
        if row is not None:
            # description is included here -- an earlier version
            # verified pipeline_key/legacy_substrate_label only,
            # leaving description (also a deterministic, seeder-
            # supplied immutable field) unverified.
            _verify_or_raise(
                {"pipeline_key": row[0], "legacy_substrate_label": row[1], "description": row[2]},
                {"pipeline_key": mapping["pipeline_key"], "legacy_substrate_label": legacy_label,
                 "description": _pipeline_description(legacy_label)},
                f"pipelines[{mapping['pipeline_id']!r}]",
            )
            continue
        conn.execute(
            "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
            "VALUES (?, ?, ?, ?)",
            (mapping["pipeline_id"], mapping["pipeline_key"], legacy_label, _pipeline_description(legacy_label)),
        )

    clark_actor_id = derive_stable_id("actor", "clark")
    row = conn.execute("SELECT actor_type, stable_key FROM actors WHERE actor_id=?", (clark_actor_id,)).fetchone()
    if row is not None:
        _verify_or_raise({"actor_type": row[0], "stable_key": row[1]},
                          {"actor_type": "clark_agent", "stable_key": "clark"},
                          f"actors[{clark_actor_id!r}]")
        # Verify the SUBTYPE linkage also exists AND has the expected
        # canonical_key -- an earlier version only checked existence of
        # the linkage row, not its value, which would have missed a
        # linkage row present under the wrong canonical_key.
        linkage = conn.execute(
            "SELECT canonical_key FROM actor_clark_agent WHERE actor_id=?", (clark_actor_id,)
        ).fetchone()
        if linkage is None:
            raise MigrationStopCondition(
                f"actors[{clark_actor_id!r}] exists but has no actor_clark_agent linkage row -- "
                f"partial initialization detected. Stopping."
            )
        _verify_or_raise({"canonical_key": linkage[0]}, {"canonical_key": "clark"},
                          f"actor_clark_agent[{clark_actor_id!r}]")
    else:
        conn.execute(
            "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', ?)",
            (clark_actor_id, now),
        )
        conn.execute("INSERT INTO actor_clark_agent (actor_id) VALUES (?)", (clark_actor_id,))

    host_actor_id = derive_stable_id("actor", "bounded_clause_renderer")
    row = conn.execute("SELECT actor_type, stable_key FROM actors WHERE actor_id=?", (host_actor_id,)).fetchone()
    if row is not None:
        _verify_or_raise({"actor_type": row[0], "stable_key": row[1]},
                          {"actor_type": "host_system", "stable_key": "bounded_clause_renderer"},
                          f"actors[{host_actor_id!r}]")
        linkage = conn.execute(
            "SELECT subsystem_key FROM actor_host_system WHERE actor_id=?", (host_actor_id,)
        ).fetchone()
        if linkage is None:
            raise MigrationStopCondition(
                f"actors[{host_actor_id!r}] exists but has no actor_host_system linkage row -- "
                f"partial initialization detected. Stopping."
            )
        _verify_or_raise({"subsystem_key": linkage[0]}, {"subsystem_key": "bounded_clause_renderer"},
                          f"actor_host_system[{host_actor_id!r}]")
    else:
        conn.execute(
            "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) "
            "VALUES (?, 'host_system', 'bounded_clause_renderer', ?)",
            (host_actor_id, now),
        )
        conn.execute(
            "INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')",
            (host_actor_id,),
        )

    # Explicitly seeded reference data (not silently absent): the
    # historical waking-turn migration itself never writes to any
    # authorization stream, but a fresh anaxi_provenance.db should be
    # self-consistent reference data for the native write path that
    # will eventually use these scopes.
    for scope_key, description in REQUIRED_AUTHORIZATION_SCOPES.items():
        scope_id = derive_stable_id("scope", scope_key)
        row = conn.execute("SELECT scope_key, description FROM authorization_scopes WHERE scope_id=?", (scope_id,)).fetchone()
        if row is not None:
            _verify_or_raise({"scope_key": row[0], "description": row[1]},
                              {"scope_key": scope_key, "description": description},
                              f"authorization_scopes[{scope_id!r}]")
            continue
        conn.execute(
            "INSERT INTO authorization_scopes (scope_id, scope_key, description) VALUES (?, ?, ?)",
            (scope_id, scope_key, description),
        )
    conn.commit()


def resolve_or_create_model_revision(conn: sqlite3.Connection, tag: str, first_observed_at: int) -> str:
    """Opaque, collision-safe model_revision_id -- replaces an earlier
    version's human-readable f"histmodel-{tag...}" slug, which was
    neither opaque nor guaranteed collision-free. An already-existing
    row under this deterministic ID is verified to have the expected
    tag/identity_confidence, not merely assumed to be the same thing --
    a mismatch is a stop condition, not a silent reuse."""
    model_revision_id = derive_stable_id("modelrev", tag, "tag_only_degraded")
    row = conn.execute("SELECT tag, identity_confidence FROM model_revisions WHERE model_revision_id=?",
                        (model_revision_id,)).fetchone()
    if row is not None:
        _verify_or_raise({"tag": row[0], "identity_confidence": row[1]},
                          {"tag": tag, "identity_confidence": "tag_only_degraded"},
                          f"model_revisions[{model_revision_id!r}]")
        return model_revision_id
    conn.execute(
        "INSERT INTO model_revisions (model_revision_id, tag, identity_confidence, first_observed_at) "
        "VALUES (?, ?, 'tag_only_degraded', ?)",
        (model_revision_id, tag, first_observed_at),
    )
    return model_revision_id


# =============================================================================
# Complete-bundle resume validation
# =============================================================================
_BUNDLE_CHECK_QUERIES = {
    "events": "SELECT 1 FROM events WHERE event_id = ?",
    "event_migration_status": "SELECT 1 FROM event_migration_status WHERE event_id = ?",
    "legacy_routing_provenance": "SELECT 1 FROM legacy_routing_provenance WHERE event_id = ?",
    "event_model_participation": "SELECT 1 FROM event_model_participation WHERE event_id = ?",
    "event_components": "SELECT 1 FROM event_components WHERE event_id = ?",
}


def verify_event_bundle_complete(conn: sqlite3.Connection, event_id: str, expected: dict = None) -> bool:
    """Proves the COMPLETE canonical event bundle exists for event_id
    -- not merely that an events row exists. Returns True if every
    part is present, False if none are. Raises IncompleteEventBundleError
    if some but not all parts exist, which should be structurally
    impossible under the one-transaction write path (§4) and therefore
    indicates real corruption -- reported, never silently resolved by
    re-processing or skipping.

    expected, when given (used during resume -- see migrate_jsonl),
    additionally proves the EXISTING bundle's content/provenance
    actually agrees with what the current source row would produce --
    a "complete" bundle (all five rows present) whose content_sha256,
    pipeline_id, or routing_constant_value disagree with the source
    being processed is content-mismatched, not idempotently
    already-migrated, and is a MigrationStopCondition, distinct from
    IncompleteEventBundleError (which is about missing rows, not
    disagreeing content)."""
    flags = {name: conn.execute(q, (event_id,)).fetchone() is not None
              for name, q in _BUNDLE_CHECK_QUERIES.items()}
    if not any(flags.values()):
        return False
    if not all(flags.values()):
        raise IncompleteEventBundleError(
            f"event_id={event_id!r} has a PARTIAL bundle {flags} -- this should be "
            f"structurally impossible under the one-transaction write path. Stopping."
        )

    if expected:
        if "content_sha256" in expected:
            row = conn.execute("SELECT content_sha256 FROM event_components WHERE event_id = ?", (event_id,)).fetchone()
            if row[0] != expected["content_sha256"]:
                raise MigrationStopCondition(
                    f"event_id={event_id!r}: existing event_components.content_sha256={row[0]!r} "
                    f"does not match the current source row's expected {expected['content_sha256']!r} -- "
                    f"refusing to treat this as already-migrated."
                )
        if "pipeline_id" in expected:
            row = conn.execute("SELECT pipeline_id FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if row[0] != expected["pipeline_id"]:
                raise MigrationStopCondition(
                    f"event_id={event_id!r}: existing events.pipeline_id={row[0]!r} does not match "
                    f"the current source row's expected {expected['pipeline_id']!r}."
                )
        if "routing_constant_value" in expected:
            row = conn.execute(
                "SELECT routing_constant_value FROM legacy_routing_provenance WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row[0] != expected["routing_constant_value"]:
                raise MigrationStopCondition(
                    f"event_id={event_id!r}: existing legacy_routing_provenance.routing_constant_value="
                    f"{row[0]!r} does not match the current source row's expected "
                    f"{expected['routing_constant_value']!r}."
                )
    return True


# Fixed literal values migrate_jsonl writes for every historical row --
# factored out so verify_historical_bundle_contract can check them
# exactly rather than trusting they haven't silently drifted between
# the write path and the resume/verify path.
HISTORICAL_MIGRATION_BASIS = (
    "anaxi_log.jsonl predates any authentication concept; confirmed by direct "
    "inspection of llama_anaxi.py/claude_anaxi.py -- neither implements auth."
)
HISTORICAL_PARTICIPATION_NOTE = (
    "Model backing this waking turn per anaxi_log.jsonl's own recorded 'model' field."
)


def verify_historical_bundle_contract(conn: sqlite3.Connection, event_id: str, entry: dict, mapping: dict) -> bool:
    """The EXACT historical bundle contract, replacing the earlier
    five-table-presence + three-field check for migrate_jsonl's resume
    path. Verifies expected CARDINALITY (exactly one row where the
    contract requires exactly one -- never merely "at least one"), and
    every field the migration itself is responsible for setting, not a
    sampled subset:
      - events: event_type, pipeline_id, pipeline_provenance_status,
        auth_context_id, epoch_id, input_source_ref, occurred_at
        (identity/timestamp/status/input ref);
      - event_migration_status: migration_status AND migration_basis
        (migration status, and the fixed rationale string migrate_jsonl
        actually writes -- not just the status code);
      - legacy_routing_provenance: routing_constant_value,
        routing_mechanism, source_code_reference, and the deterministic
        legacy_routing_id itself (source/mechanism/value/id), and
        exactly one row;
      - event_model_participation: model_revision_id derived exactly
        from entry["model"] AND participation_note (exact tag-derived
        model participation), and exactly one row;
      - model_revisions: the row referenced by model_revision_id
        actually EXISTS with tag==entry["model"] and
        identity_confidence=='tag_only_degraded' -- the foreign-key ID
        matching is not trusted alone, since a deterministic ID could in
        principle be satisfied by a row seeded with different fields;
      - event_components: exactly one row, sequence=0,
        authorship_resolution='unresolved_mixed_historical',
        component_kind='assembled_reply_unsplit_historical',
        creator_actor_id/model_revision_id/span_start/span_end all NULL,
        component_text and content_sha256 both agreeing with
        entry["response"].

    Returns False only if the event is entirely absent (safe to
    insert fresh). Raises IncompleteEventBundleError for a
    structurally-impossible partial bundle (some but not all of the
    five tables have any row). Raises MigrationStopCondition for any
    cardinality or field disagreement with what this exact source row
    would produce -- a "bundle exists" is never enough on its own."""
    event_row = conn.execute(
        "SELECT event_type, pipeline_id, pipeline_provenance_status, auth_context_id, "
        "epoch_id, input_source_ref, occurred_at FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    status_row = conn.execute(
        "SELECT migration_status, migration_basis FROM event_migration_status WHERE event_id = ?", (event_id,)
    ).fetchone()
    routing_rows = conn.execute(
        "SELECT legacy_routing_id, routing_constant_value, routing_mechanism, source_code_reference "
        "FROM legacy_routing_provenance WHERE event_id = ?", (event_id,)
    ).fetchall()
    participation_rows = conn.execute(
        "SELECT model_revision_id, participation_note FROM event_model_participation WHERE event_id = ?", (event_id,)
    ).fetchall()
    component_rows = conn.execute(
        "SELECT sequence, creator_actor_id, component_kind, authorship_resolution, "
        "component_text, content_sha256, model_revision_id, span_start, span_end "
        "FROM event_components WHERE event_id = ?",
        (event_id,)
    ).fetchall()

    presence = {
        "events": event_row is not None,
        "event_migration_status": status_row is not None,
        "legacy_routing_provenance": len(routing_rows) > 0,
        "event_model_participation": len(participation_rows) > 0,
        "event_components": len(component_rows) > 0,
    }
    if not any(presence.values()):
        return False
    if not all(presence.values()):
        raise IncompleteEventBundleError(
            f"event_id={event_id!r} has a PARTIAL bundle {presence} -- this should be "
            f"structurally impossible under the one-transaction write path. Stopping."
        )

    if len(routing_rows) != 1:
        raise MigrationStopCondition(
            f"event_id={event_id!r}: expected exactly 1 legacy_routing_provenance row, found {len(routing_rows)}.")
    if len(participation_rows) != 1:
        raise MigrationStopCondition(
            f"event_id={event_id!r}: expected exactly 1 event_model_participation row, found {len(participation_rows)}.")
    if len(component_rows) != 1:
        raise MigrationStopCondition(
            f"event_id={event_id!r}: expected exactly 1 event_components row, found {len(component_rows)}.")

    def _check(actual_fields: dict, expected_fields: dict, table: str) -> None:
        for field, expected_value in expected_fields.items():
            actual_value = actual_fields[field]
            if actual_value != expected_value:
                raise MigrationStopCondition(
                    f"event_id={event_id!r}: {table}.{field}={actual_value!r}, expected {expected_value!r} "
                    f"-- refusing to treat this as already-migrated."
                )

    event_type, pipeline_id, pipeline_provenance_status, auth_context_id, epoch_id, input_source_ref, occurred_at = event_row
    _check(
        {"event_type": event_type, "pipeline_id": pipeline_id, "pipeline_provenance_status": pipeline_provenance_status,
         "auth_context_id": auth_context_id, "epoch_id": epoch_id, "input_source_ref": input_source_ref,
         "occurred_at": occurred_at},
        {"event_type": "waking_turn", "pipeline_id": mapping["pipeline_id"], "pipeline_provenance_status": "known",
         "auth_context_id": None, "epoch_id": None, "input_source_ref": event_id,
         "occurred_at": parse_iso_timestamp_to_unix(entry["timestamp"])},
        "events",
    )

    migration_status, migration_basis = status_row
    _check(
        {"migration_status": migration_status, "migration_basis": migration_basis},
        {"migration_status": "pre_authentication_layer", "migration_basis": HISTORICAL_MIGRATION_BASIS},
        "event_migration_status",
    )

    actual_legacy_routing_id, routing_constant_value, routing_mechanism, source_code_reference = routing_rows[0]
    expected_legacy_routing_id = derive_stable_id("legrt", event_id)
    _check(
        {"legacy_routing_id": actual_legacy_routing_id, "routing_constant_value": routing_constant_value,
         "routing_mechanism": routing_mechanism, "source_code_reference": source_code_reference},
        {"legacy_routing_id": expected_legacy_routing_id, "routing_constant_value": mapping["routing_constant_value"],
         "routing_mechanism": "hardcoded_module_constant", "source_code_reference": mapping["routing_source"]},
        "legacy_routing_provenance",
    )

    actual_model_revision_id, participation_note = participation_rows[0]
    expected_model_revision_id = derive_stable_id("modelrev", entry["model"], "tag_only_degraded")
    _check(
        {"model_revision_id": actual_model_revision_id, "participation_note": participation_note},
        {"model_revision_id": expected_model_revision_id, "participation_note": HISTORICAL_PARTICIPATION_NOTE},
        "event_model_participation",
    )

    # The referenced model_revisions row must actually EXIST with the
    # expected tag/identity_confidence -- matching the deterministic ID
    # alone is not sufficient, since that ID could in principle be
    # satisfied by a row some other code path seeded with different
    # fields (e.g. a genuine tag collision or corruption).
    model_revision_row = conn.execute(
        "SELECT tag, identity_confidence FROM model_revisions WHERE model_revision_id = ?",
        (actual_model_revision_id,),
    ).fetchone()
    if model_revision_row is None:
        raise MigrationStopCondition(
            f"event_id={event_id!r}: event_model_participation references model_revision_id="
            f"{actual_model_revision_id!r}, but no such row exists in model_revisions -- "
            f"refusing to treat this as already-migrated."
        )
    model_revision_tag, model_revision_identity_confidence = model_revision_row
    _check(
        {"tag": model_revision_tag, "identity_confidence": model_revision_identity_confidence},
        {"tag": entry["model"], "identity_confidence": "tag_only_degraded"},
        f"model_revisions[{actual_model_revision_id!r}]",
    )

    (sequence, creator_actor_id, component_kind, authorship_resolution, component_text, content_sha256,
     comp_model_revision_id, span_start, span_end) = component_rows[0]
    expected_content_sha256 = hashlib.sha256(entry["response"].encode("utf-8")).hexdigest()
    _check(
        {"sequence": sequence, "creator_actor_id": creator_actor_id, "component_kind": component_kind,
         "authorship_resolution": authorship_resolution, "component_text": component_text,
         "content_sha256": content_sha256, "model_revision_id": comp_model_revision_id,
         "span_start": span_start, "span_end": span_end},
        {"sequence": 0, "creator_actor_id": None, "component_kind": "assembled_reply_unsplit_historical",
         "authorship_resolution": "unresolved_mixed_historical", "component_text": entry["response"],
         "content_sha256": expected_content_sha256, "model_revision_id": None,
         "span_start": None, "span_end": None},
        "event_components",
    )

    return True


# =============================================================================
# JSONL -> anaxi_provenance.db migration
# =============================================================================
def migrate_jsonl(provenance_conn: sqlite3.Connection, jsonl_path: str, pipeline_map: dict) -> dict:
    migration_run_at = int(time.time())
    counts = {"processed": 0, "skipped_already_migrated": 0, "inserted": 0}

    with open(jsonl_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for line_index, line in enumerate(lines):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        counts["processed"] += 1
        entry = json.loads(line)
        substrate = entry["substrate"]

        mapping = pipeline_map.get(substrate)
        if mapping is None:
            raise MigrationStopCondition(
                f"Unmapped substrate value {substrate!r} encountered at line {line_index} "
                f"with timestamp {entry.get('timestamp')!r}. Halting -- add an explicit "
                f"mapping entry before re-running, do not default to any existing pipeline."
            )

        event_id = derive_historical_event_id("anaxi_log.jsonl", line.encode("utf-8"), line_index)

        if verify_historical_bundle_contract(provenance_conn, event_id, entry, mapping):
            counts["skipped_already_migrated"] += 1
            continue

        occurred_at = parse_iso_timestamp_to_unix(entry["timestamp"])

        provenance_conn.execute("BEGIN")
        try:
            provenance_conn.execute(
                "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
                "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
                "VALUES (?, 'waking_turn', ?, 'known', NULL, NULL, ?, ?, ?)",
                (event_id, mapping["pipeline_id"], event_id, occurred_at, migration_run_at),
            )
            provenance_conn.execute(
                "INSERT INTO event_migration_status (event_id, migration_status, migrated_at, migration_basis) "
                "VALUES (?, 'pre_authentication_layer', ?, ?)",
                (event_id, migration_run_at, HISTORICAL_MIGRATION_BASIS),
            )
            legacy_routing_id = derive_stable_id("legrt", event_id)
            provenance_conn.execute(
                "INSERT INTO legacy_routing_provenance (legacy_routing_id, event_id, routing_constant_value, "
                "routing_mechanism, source_code_reference, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (legacy_routing_id, event_id, mapping["routing_constant_value"],
                 "hardcoded_module_constant", mapping["routing_source"], migration_run_at),
            )
            model_revision_id = resolve_or_create_model_revision(provenance_conn, entry["model"], occurred_at)
            provenance_conn.execute(
                "INSERT INTO event_model_participation (event_id, model_revision_id, participation_note) "
                "VALUES (?, ?, ?)",
                (event_id, model_revision_id, HISTORICAL_PARTICIPATION_NOTE),
            )
            response_text = entry["response"]
            provenance_conn.execute(
                "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
                "authorship_resolution, component_text, content_sha256, model_revision_id) "
                "VALUES (?, 0, NULL, 'assembled_reply_unsplit_historical', 'unresolved_mixed_historical', ?, ?, NULL)",
                (event_id, response_text, hashlib.sha256(response_text.encode("utf-8")).hexdigest()),
            )
            provenance_conn.commit()
        except Exception:
            provenance_conn.rollback()
            raise

        # Post-commit proof, not merely trust: verify the FULL exact
        # historical bundle contract, not just row presence, before
        # counting this row as migrated.
        if not verify_historical_bundle_contract(provenance_conn, event_id, entry, mapping):
            raise MigrationStopCondition(
                f"event_id={event_id!r} bundle incomplete immediately after commit -- should be impossible."
            )
        counts["inserted"] += 1

    return counts


# =============================================================================
# §9 step 6(d): additive schema alteration -- structurally SEPARATE from
# any backfill. Covers every required existing table.
# =============================================================================
RELATIONAL_EVENTS_ADDITIVE_COLUMNS = ["pipeline_id", "model_revision_id", "epoch_id", "auth_context_id", "event_id"]
TURN_GENERATION_LOG_ADDITIVE_COLUMNS = ["pipeline_id", "model_revision_id", "epoch_id", "event_id"]
NODES_ADDITIVE_COLUMNS = ["pipeline_id", "epoch_id", "asserting_event_id", "grounding_assertion_id"]
ACTIVE_KARDIA_ADDITIVE_COLUMNS = ["pipeline_id", "epoch_id", "source_event_id"]
KARDIA_HISTORY_ADDITIVE_COLUMNS = ["pipeline_id", "epoch_id", "source_event_id"]
PROPOSALS_ADDITIVE_COLUMNS = ["pipeline_id", "epoch_id"]


def _add_columns_if_missing(conn: sqlite3.Connection, table: str, columns: list) -> list:
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    added = []
    for col in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            added.append(col)
    return added


def alter_existing_stores_schema(rel_conn: sqlite3.Connection, mind_conn: sqlite3.Connection) -> dict:
    """ONLY adds nullable columns -- never writes a value into them.
    Every table named in the frozen spec's §3 additive-deltas table is
    covered here, not only the two an earlier version handled."""
    report = {"relational_events": _add_columns_if_missing(rel_conn, "relational_events", RELATIONAL_EVENTS_ADDITIVE_COLUMNS)}
    rel_conn.commit()
    report["turn_generation_log"] = _add_columns_if_missing(mind_conn, "turn_generation_log", TURN_GENERATION_LOG_ADDITIVE_COLUMNS)
    report["nodes"] = _add_columns_if_missing(mind_conn, "nodes", NODES_ADDITIVE_COLUMNS)
    report["active_kardia"] = _add_columns_if_missing(mind_conn, "active_kardia", ACTIVE_KARDIA_ADDITIVE_COLUMNS)
    report["kardia_history"] = _add_columns_if_missing(mind_conn, "kardia_history", KARDIA_HISTORY_ADDITIVE_COLUMNS)
    report["proposals"] = _add_columns_if_missing(mind_conn, "proposals", PROPOSALS_ADDITIVE_COLUMNS)
    mind_conn.commit()
    return report


# =============================================================================
# Backfills -- NULL-only, idempotent, stop on conflicting non-NULL
# provenance. Structurally SEPARATE from schema alteration above.
# =============================================================================
def backfill_relational_events(rel_conn: sqlite3.Connection, pipeline_map: dict) -> dict:
    rows = rel_conn.execute("SELECT id, substrate, pipeline_id FROM relational_events").fetchall()
    updated = 0
    already_set = 0
    for row_id, substrate, existing_pipeline_id in rows:
        mapping = pipeline_map.get(substrate)
        if mapping is None:
            raise MigrationStopCondition(f"Unmapped substrate {substrate!r} in relational_events row {row_id}")
        resolved = mapping["pipeline_id"]
        if existing_pipeline_id is not None:
            if existing_pipeline_id != resolved:
                raise MigrationStopCondition(
                    f"relational_events row {row_id} already has pipeline_id={existing_pipeline_id!r}, "
                    f"conflicting with resolved {resolved!r} from substrate={substrate!r}. Stopping -- "
                    f"never silently overwrite existing non-NULL provenance."
                )
            already_set += 1
            continue
        rel_conn.execute(
            "UPDATE relational_events SET pipeline_id = ? WHERE id = ? AND pipeline_id IS NULL",
            (resolved, row_id),
        )
        updated += 1
    rel_conn.commit()
    return {"updated": updated, "already_set": already_set}


def backfill_turn_generation_log(mind_conn: sqlite3.Connection, pipeline_map: dict) -> dict:
    """turn_generation_log has no substrate/model column of its own --
    provenance is DB-file membership (this whole file is exclusively
    llama-pipeline data), never inferred from model_revision_id nullness."""
    llama_pipeline_id = pipeline_map["llama"]["pipeline_id"]
    rows = mind_conn.execute("SELECT id, pipeline_id FROM turn_generation_log").fetchall()
    updated = 0
    already_set = 0
    for row_id, existing in rows:
        if existing is not None:
            if existing != llama_pipeline_id:
                raise MigrationStopCondition(
                    f"turn_generation_log row {row_id} already has pipeline_id={existing!r}, "
                    f"conflicting with expected {llama_pipeline_id!r}. Stopping."
                )
            already_set += 1
            continue
        mind_conn.execute(
            "UPDATE turn_generation_log SET pipeline_id = ? WHERE id = ? AND pipeline_id IS NULL",
            (llama_pipeline_id, row_id),
        )
        updated += 1
    mind_conn.commit()
    return {"updated": updated, "already_set": already_set}


# =============================================================================
# Pipeline-aware legacy-substrate matching shim, as real, importable,
# testable functions (an earlier version only existed as prose /
# inline test-local reimplementation).
# =============================================================================
def resolve_pipeline_id(conn: sqlite3.Connection, pipeline_key: str):
    row = conn.execute("SELECT pipeline_id FROM pipelines WHERE pipeline_key = ?", (pipeline_key,)).fetchone()
    return row[0] if row else None


def resolve_legacy_substrate_label(conn: sqlite3.Connection, pipeline_key: str):
    row = conn.execute("SELECT legacy_substrate_label FROM pipelines WHERE pipeline_key = ?", (pipeline_key,)).fetchone()
    return row[0] if row else None


def matches_legacy_pipeline(conn: sqlite3.Connection, entry: dict, pipeline_key: str) -> bool:
    """Drop-in replacement for a hardcoded entry.get("substrate") ==
    "llama" (or == "claude") check. The fallback substrate literal is
    looked up FROM pipeline_key via pipelines.legacy_substrate_label,
    never hardcoded, so this one function is correct for every
    pipeline."""
    if "pipeline_id" in entry:
        return entry["pipeline_id"] == resolve_pipeline_id(conn, pipeline_key)
    legacy_label = resolve_legacy_substrate_label(conn, pipeline_key)
    return entry.get("substrate") == legacy_label


# =============================================================================
# Orchestration
# =============================================================================
def main(rehearsal_dir: str, manifest_path: str = None) -> None:
    assert_is_rehearsal_dir(rehearsal_dir)

    if manifest_path is None:
        manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), MANIFEST_FILENAME)
    manifest = load_migration_manifest(manifest_path)
    pipeline_map = build_pipeline_map(manifest)

    provenance_path = os.path.join(rehearsal_dir, "anaxi_provenance.db")
    jsonl_path = os.path.join(rehearsal_dir, "anaxi_log.jsonl")
    relational_path = os.path.join(rehearsal_dir, "anaxi_relational_llama.db")
    mind_path = os.path.join(rehearsal_dir, "anaxi_mind_llama.db")

    provenance_conn = sqlite3.connect(provenance_path)
    provenance_conn.execute("PRAGMA foreign_keys = ON;")
    now = int(time.time())

    seed_reference_data(provenance_conn, pipeline_map, now)
    print("Reference data seeded/confirmed (idempotent, opaque IDs).")

    print(f"\nMigrating {jsonl_path} -> {provenance_path} ...")
    counts = migrate_jsonl(provenance_conn, jsonl_path, pipeline_map)
    print(f"  {counts}")

    rel_conn = sqlite3.connect(relational_path)
    mind_conn = sqlite3.connect(mind_path)

    print("\nAltering existing-store schema (additive columns only, structurally separate from backfill) ...")
    alter_report = alter_existing_stores_schema(rel_conn, mind_conn)
    print(f"  {alter_report}")

    print("\nBackfilling relational_events (NULL-only, conflict-detecting) ...")
    rel_report = backfill_relational_events(rel_conn, pipeline_map)
    print(f"  {rel_report}")
    rel_conn.close()

    print("\nBackfilling turn_generation_log (NULL-only, conflict-detecting) ...")
    tgl_report = backfill_turn_generation_log(mind_conn, pipeline_map)
    print(f"  {tgl_report}")
    mind_conn.close()

    provenance_conn.close()
    print("\nMigration against copies complete.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python migrate_historical_data.py <rehearsal_dir>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
