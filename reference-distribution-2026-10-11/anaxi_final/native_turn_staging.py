"""
Anaxi -- Durable staging envelope for native (post-cutover) waking turns.
Plays the same role for the native write path that
anaxi_log.jsonl plays for historical migration: an append-only, durable,
external source that restart/reconciliation can recover from -- but for
turns that have not yet been written anywhere else at all, not a replay
of something that already exists.

Three hard requirements this module exists to satisfy, all directly
tied to correctness claims made about crash recovery and concurrent
writers, not stylistic preferences:

1. Non-circular, verifiable ID derivation. `staging_id` must be a
   function of the payload's own bytes, and the payload those bytes
   are derived from must NOT include `staging_id` itself -- otherwise
   the "ID" isn't actually a content hash, it's decoration. Every read
   (normal or crash-recovery) recomputes `staging_id` from the stored
   payload and requires exact equality before trusting the record; any
   mismatch is a hard stop, never repaired or ignored. This module
   does NOT derive an event_id from prompt/response/content anywhere
   -- native event_id remains a separately-minted random ULID
   (native_provenance_writer.generate_native_ulid()), untouched by
   this module. `staging_id` is a distinct identity space for a
   distinct purpose (verifying one staged line's own bytes), reusing
   the same deterministic derivation historical migration already
   uses for a different, unrelated reason (resumable replay of
   external content) -- the two ID spaces never collide and are never
   substituted for one another.

2. Durability ordering, now atomic against BOTH crash and concurrent
   writers. `append_staging_entry()` no longer accepts a caller-supplied
   `line_index` -- an earlier revision required every caller to compute
   `_next_staging_line_index()` independently, which meant two
   processes racing to append could both count the same existing line
   total and both believe they owned the next index (mechanically
   confirmed: the physically-second writer's stored staging_id, derived
   from its OWN claimed index, disagreed with the index recomputed from
   its true physical file position on the next read, raising
   StagingIntegrityError only when read back -- not prevented, only
   detected after the fact). This function now owns the ENTIRE
   sequence -- existing-file integrity validation, next-index
   determination, staging-ID derivation, append, and fsync -- as one
   operation serialized by a REAL cross-process exclusive lock (see
   `_StagingLock` below), so two processes can never race on the same
   index at all.

3. A failed append has an explicit, durable outcome -- never merely
   "maybe wrote something, who knows." While holding the lock, the
   pre-append byte length is captured before any write is attempted.
   If the write or the final fsync fails, this module attempts to
   restore the file to EXACTLY that pre-append length (`os.ftruncate`)
   and fsync the truncated result before releasing the lock:
     - If that rollback succeeds: raises `StagingDurabilityError`.
       No staged entry remains on disk for this attempt -- the turn
       simply did not happen yet, and the caller's documented
       "retry whole" is genuinely safe, because there is nothing left
       over to collide or duplicate with a retry.
     - If the rollback itself cannot be durably completed: raises
       `StagingIndeterminateStateError` instead -- a DISTINCT
       exception, deliberately not a subclass of StagingDurabilityError,
       so a caller cannot accidentally catch-and-retry it via a broad
       `except StagingDurabilityError`. This state means the file's
       tail is of UNKNOWN content (it may be genuinely intact, may
       contain a partial line, or worse) -- automatic "retry whole" is
       explicitly FORBIDDEN here, because a retry that assumes a clean
       pre-append length and blindly appends again could corrupt or
       silently coexist with unknown trailing bytes. No canonical
       transaction may begin. Operator inspection of the raw file is
       required before any further write is attempted against this
       staging_path.
   A caller must never infer durability merely from reading the file
   back afterward in the same process: `os.write()` can make bytes
   immediately visible to a same-machine reader without them being
   durable against a crash -- that gap is exactly what `os.fsync()`
   closes, and exactly why a failed fsync is treated as gravely as a
   failed write, never as "probably fine, it's right there."

Locking mechanism, and why it must be cross-process: a `threading.Lock`
only serializes threads within one Python process -- it does nothing
at all for two separate OS processes (e.g. two independent invocations
of llama_anaxi.py, or a live process racing a crash-recovery/reconcile
run against the same staging file) racing to append to the same
physical file. `_StagingLock` instead opens a dedicated lock file
(`<staging_path>.lock`) and takes a real, OS-enforced, cross-process
exclusive lock on it: `msvcrt.locking()` on Windows (this project's
production host) -- a MANDATORY lock the OS itself enforces against
any other process attempting the same region, not merely an advisory
convention two well-behaved callers must remember to honor -- and
`fcntl.flock()` on POSIX platforms, for the same purpose, for
portability. No third-party dependency; both are standard library.

Reader is also atomic. `read_verified_staging_entries()` acquires the
SAME lock (so it never observes a write in progress) and validates the
ENTIRE file before yielding a single entry to its caller -- an earlier
revision yielded lazily, line by line, and a defect on a LATER line
(a genuinely different, separate defect: a malformed/corrupted trailing
record, distinct from anything the writer-side hardening above
addresses) would only surface after already handing the caller
whatever earlier, individually-valid entries came before it -- a
partial, silently-incomplete view for crash-recovery code that expects
either the complete, verified set or a hard stop, never something in
between. A malformed (non-JSON) line is now translated to
`StagingIntegrityError`, the module's own documented, catchable
contract -- never left to leak a raw `json.JSONDecodeError` a caller
has no reason to expect from this module's interface.
"""

import json
import os
import sys

from provenance_schema import derive_historical_event_id

STAGING_SOURCE_ID = "native_turn_staging.jsonl"


class StagingDurabilityError(Exception):
    """Raised when an append could not be durably completed AND the
    attempted rollback to the pre-append byte length succeeded. No
    staged entry remains on disk for this attempt -- the turn simply
    did not happen yet, and the caller's documented "retry whole" is
    genuinely safe: there is nothing left over on disk to collide or
    duplicate with the retry."""
    pass


class StagingIndeterminateStateError(Exception):
    """Raised when an append failed AND the attempted rollback (restoring
    the file to its pre-append byte length via os.ftruncate, then
    fsync) could NOT itself be durably completed. Deliberately NOT a
    subclass of StagingDurabilityError, so a caller cannot silently
    catch-and-retry it via a broad `except StagingDurabilityError`.

    The staging file's tail is in an UNKNOWN state relative to this
    attempt -- it may be genuinely unaffected, may contain a partial
    trailing record, or worse. Automatic "retry whole" is explicitly
    FORBIDDEN in response to this exception: a retry that assumes a
    clean pre-append length and simply appends again could produce a
    corrupt or ambiguous file. No canonical transaction may begin.
    Operator inspection of the raw staging file is required before any
    further write is attempted against this staging_path."""
    pass


class StagingIntegrityError(Exception):
    """Raised when a stored staging record's `staging_id` does not
    match the ID recomputed from its own payload bytes, or when a
    staging line is not valid JSON, or is otherwise structurally
    malformed. Never repaired or ignored -- the record is untrustworthy
    and the read must stop, not silently proceed with a best-effort
    guess. Raised before any entry -- including earlier, individually-
    valid ones -- is exposed to the caller (see read_verified_staging_entries())."""
    pass


class _StagingLock:
    """Real, OS-enforced, cross-process exclusive lock scoped to one
    staging_path, via a dedicated `<staging_path>.lock` file -- never
    the staging file itself, so the lock's own bookkeeping never
    shares byte offsets with the growing data file it protects.
    Windows (msvcrt.locking, mandatory, this project's production
    host) and POSIX (fcntl.flock, advisory but universally honored by
    every writer in this codebase, since they all go through this same
    class) are both covered; no third-party dependency."""

    def __init__(self, staging_path: str):
        self._lock_path = staging_path + ".lock"
        self._fd = None

    def __enter__(self) -> "_StagingLock":
        self._fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            # msvcrt.locking() requires at least one byte to lock; ensure
            # the lock file is never empty, exactly once, ever (it is
            # never truncated afterward -- only ever (re)used as a mutex).
            # Deliberately no fsync here: the lock file's own byte content
            # has no durability requirement (it exists solely as a mutex
            # target, not as data), and fsync'ing it here would make this
            # setup step indistinguishable, to any test mocking os.fsync,
            # from the actual staging-entry write/rollback fsync calls
            # this module's durability guarantees are actually about.
            if os.fstat(self._fd).st_size < 1:
                os.write(self._fd, b"\0")
            os.lseek(self._fd, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_EX)
        except Exception:
            os.close(self._fd)
            self._fd = None
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._fd is None:
            return
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def serialize_canonical_payload(payload: dict) -> bytes:
    """The one canonical serialization used both to derive
    `staging_id` at write time and to re-verify it at read time.
    Deterministic: sorted keys, fixed separators, ASCII-only escaping
    (so encoding is never a source of byte-level ambiguity across
    platforms/locales)."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def compute_staging_id(payload: dict, line_index: int) -> str:
    """`payload` must NOT contain `staging_id` -- enforced by the
    caller's contract, not re-checked here, since this function is
    also what verification re-derives from a stored record with
    `staging_id` already stripped out. Reuses the existing
    domain-separated, length-prefixed derivation used for historical
    event IDs -- same non-collision guarantee, different source_id so
    the two ID spaces can never collide with each other. Unchanged by
    the locking hardening below: `line_index` is still always the
    entry's own validated physical position among prior entries, only
    now determined atomically under the lock rather than passed in
    from an unsynchronized caller."""
    payload_bytes = serialize_canonical_payload(payload)
    return derive_historical_event_id(STAGING_SOURCE_ID, payload_bytes, line_index)


def _write_all_bytes(fd: int, data: bytes) -> None:
    """Guarantees the COMPLETE buffer is written to the raw file
    descriptor, or raises OSError (the caller -- append_staging_entry()
    -- is responsible for durability-error translation and rollback;
    this function only performs the write-loop itself). os.write()'s
    contract does NOT guarantee a single call writes every requested
    byte (a 'short write') -- loops, feeding the remainder back in,
    until every byte is confirmed written. A non-positive return from
    os.write() is treated as a failed/stalled write, never retried
    forever, and raised as OSError so the caller's rollback path
    handles it uniformly with a write that raised OSError directly."""
    total_written = 0
    length = len(data)
    while total_written < length:
        n = os.write(fd, data[total_written:])
        if n <= 0:
            raise OSError(
                f"os.write() returned {n} while appending a staging entry (after "
                f"{total_written} of {length} bytes written) -- treating as a failed/stalled "
                f"write, not retrying forever."
            )
        total_written += n


def _validate_and_collect_entries(staging_path: str) -> list:
    """Lock-free core validation logic, shared by append_staging_entry()
    (which already holds the lock when it calls this, to determine the
    next index) and read_verified_staging_entries() (which acquires the
    lock itself, then calls this). Never called directly by external
    code -- always go through one of those two public entry points, so
    the lock is never bypassed.

    Returns a list of (line_index, payload_without_staging_id,
    staging_id) for every entry, in order, after recomputing and
    verifying each line's staging_id against its own payload bytes and
    confirming every line is valid JSON. Raises StagingIntegrityError
    immediately on the first defect found anywhere in the file --
    never skips, never repairs, never returns a partial list."""
    if not os.path.isfile(staging_path):
        return []
    with open(staging_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    entries = []
    for line_index, line in enumerate(lines):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            raise StagingIntegrityError(
                f"staging line {line_index} in {staging_path!r} is not valid JSON ({e}) -- "
                f"refusing to trust a malformed record, never skipping or repairing it."
            ) from e
        if "staging_id" not in record:
            raise StagingIntegrityError(
                f"staging line {line_index} in {staging_path!r} has no 'staging_id' field -- "
                f"cannot verify, refusing to trust it."
            )
        stored_staging_id = record["staging_id"]
        payload_without_id = {k: v for k, v in record.items() if k != "staging_id"}
        recomputed_id = compute_staging_id(payload_without_id, line_index)
        if recomputed_id != stored_staging_id:
            raise StagingIntegrityError(
                f"staging line {line_index} in {staging_path!r}: stored staging_id "
                f"{stored_staging_id!r} does not match staging_id {recomputed_id!r} recomputed "
                f"from its own payload bytes -- refusing to trust a record that fails its own "
                f"integrity check, never repairing or ignoring the mismatch."
            )
        entries.append((line_index, payload_without_id, stored_staging_id))
    return entries


def append_staging_entry(staging_path: str, payload: dict) -> str:
    """Writes one durable staging line for `payload` (which must NOT
    contain a `staging_id` key -- raises ValueError if it does, since
    accepting one would make the derivation circular) and returns the
    resulting `staging_id`. Callers no longer determine or pass a
    line_index -- this function owns that, atomically, as part of the
    same locked operation as the append itself (see module docstring,
    requirement 2).

    Sequence, exactly, all under one real cross-process exclusive lock
    (_StagingLock): validate the existing file's integrity and derive
    the next physical index from it -> compute this entry's staging_id
    -> capture the pre-append byte length -> write the COMPLETE encoded
    line -> fsync. Any failure in the write/fsync step attempts a
    rollback (ftruncate back to the captured pre-append length, then
    fsync) before releasing the lock -- see StagingDurabilityError and
    StagingIndeterminateStateError for the two possible outcomes of
    that attempt. The caller never receives a staging_id for a turn
    that is not actually, completely, durable on disk."""
    if "staging_id" in payload:
        raise ValueError(
            "payload must not already contain 'staging_id' -- the ID is derived FROM the "
            "payload, including it would make the derivation circular and self-referential."
        )

    with _StagingLock(staging_path):
        existing_entries = _validate_and_collect_entries(staging_path)
        line_index = len(existing_entries)

        staging_id = compute_staging_id(payload, line_index)
        record = dict(payload)
        record["staging_id"] = staging_id
        line_bytes = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")

        try:
            fd = os.open(staging_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0))
        except OSError as e:
            raise StagingDurabilityError(
                f"could not open {staging_path!r} for durable append (line_index={line_index}): {e}. "
                f"No canonical transaction may begin for this turn."
            ) from e

        try:
            pre_append_length = os.fstat(fd).st_size
            write_error = None
            try:
                _write_all_bytes(fd, line_bytes)
                os.fsync(fd)
            except OSError as e:
                write_error = e

            if write_error is not None:
                rollback_error = None
                try:
                    os.ftruncate(fd, pre_append_length)
                    os.fsync(fd)
                except OSError as e:
                    rollback_error = e

                if rollback_error is None:
                    raise StagingDurabilityError(
                        f"append failed for {staging_path!r} (line_index={line_index}): "
                        f"{write_error!r}. Rolled back to the pre-append length "
                        f"({pre_append_length} bytes) and fsync'd the rollback successfully -- "
                        f"no staged entry remains on disk for this attempt. No canonical "
                        f"transaction may begin for this turn; retry-whole is safe."
                    ) from write_error
                else:
                    raise StagingIndeterminateStateError(
                        f"append failed for {staging_path!r} (line_index={line_index}): "
                        f"{write_error!r} -- AND the attempted rollback to the pre-append "
                        f"length ({pre_append_length} bytes) itself failed: {rollback_error!r}. "
                        f"The file's tail is in an UNKNOWN state. Do NOT retry-whole "
                        f"automatically. No canonical transaction may begin. Operator "
                        f"inspection of {staging_path!r} is required before any further write "
                        f"is attempted against this staging_path."
                    ) from rollback_error
        finally:
            os.close(fd)

    return staging_id


def read_verified_staging_entries(staging_path: str):
    """Yields (line_index, payload_without_staging_id, staging_id) for
    every line in the staging file, in order. Acquires the same
    cross-process lock append_staging_entry() uses, so a torn/
    in-progress concurrent write is never observed, and validates the
    ENTIRE file (see _validate_and_collect_entries()) BEFORE yielding
    the first entry -- a defect anywhere in the file raises
    StagingIntegrityError before any entry, even an earlier,
    individually-valid one, is exposed to the caller. Still a
    generator (existing callers iterate it the same way as before);
    the change is that all validation work, and any exception it can
    raise, happens before the first `yield` executes, not interleaved
    with yielding."""
    with _StagingLock(staging_path):
        entries = _validate_and_collect_entries(staging_path)
    for entry in entries:
        yield entry
