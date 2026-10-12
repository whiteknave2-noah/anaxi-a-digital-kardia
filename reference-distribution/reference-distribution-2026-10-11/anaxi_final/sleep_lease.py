"""SLP1-C: durable, fenced single-flight lease for the dormant Sleep
cycle orchestrator.

Timeout-only lease ownership is NOT sufficient (a stale owner whose
clock or process merely paused past expiry could still race a new
owner's writes). This module implements a monotonic FENCING GENERATION
on top of the lease row, in the same anchored `anaxi_provenance.db`
every other canonical writer uses (sleep_c_schema.NEW_TABLES_DDL's own
`sleep_v1_lease` table) -- SQLite single-host transaction semantics are
sufficient; no distributed consensus is needed.

Rules (frozen):
  1. An unowned or expired lease can be acquired by anyone.
  2. Every acquisition increments `generation` monotonically.
  3. The caller receives (owner_id, generation) as its `LeaseHandle`.
  4. Every final state-changing commit must verify, INSIDE that same
     commit's own transaction: current owner_id == caller owner_id,
     current generation == caller generation, and the lease has not
     since expired -- see `verify_lease_for_commit()`, which performs
     only the read+compare; the caller is responsible for running it
     inside its own already-open transaction, never a separate one.
  5. Once a newer owner acquires (because the old one expired), the old
     generation is fenced forever -- it can never again pass a
     commit's verification, even if its own clock thinks it is still
     "within its lease."
  6. A stale owner cannot advance the watermark, persist derivations,
     or clear a newer owner's lease.
  7. Release is compare-and-clear: it only clears a row that still
     matches the caller's own (owner_id, generation).

Every function here takes an already-open `sqlite3.Connection` to the
anchored provenance DB and manages its OWN short transaction boundary
(`acquire_lease`/`renew_lease`/`release_lease`) -- except
`verify_lease_for_commit`, a pure read+compare meant to run inside a
LARGER caller-owned transaction (sleep_cycle.py's own final atomic
commit). An injectable `now` (an integer epoch-seconds clock value, not
a live `time.time()` call baked into this module) makes every rule
above deterministically testable without real sleeping.
"""

DEFAULT_LEASE_DURATION_SECONDS = 300


class LeaseHandle:
    def __init__(self, owner_id, generation, expires_at):
        self.owner_id = owner_id
        self.generation = generation
        self.expires_at = expires_at

    def __repr__(self):
        return f"LeaseHandle(owner_id={self.owner_id!r}, generation={self.generation}, expires_at={self.expires_at})"


def acquire_lease(conn, *, owner_id, now, duration_seconds=DEFAULT_LEASE_DURATION_SECONDS):
    """Atomic acquire in its own immediate transaction. Returns a
    LeaseHandle on success, or None if the lease is currently held by a
    still-unexpired owner (never raises for that ordinary case)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT owner_id, generation, expires_at FROM sleep_v1_lease WHERE id = 1"
        ).fetchone()
        if row is None:
            new_generation = 1
        else:
            _existing_owner, existing_generation, expires_at = row
            if now < expires_at:
                conn.execute("ROLLBACK")
                return None
            new_generation = existing_generation + 1
        expires_at_new = now + duration_seconds
        conn.execute(
            "INSERT INTO sleep_v1_lease (id, owner_id, generation, expires_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET owner_id = excluded.owner_id, "
            "generation = excluded.generation, expires_at = excluded.expires_at",
            (owner_id, new_generation, expires_at_new),
        )
        conn.execute("COMMIT")
        return LeaseHandle(owner_id=owner_id, generation=new_generation, expires_at=expires_at_new)
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def renew_lease(conn, *, owner_id, generation, now, duration_seconds=DEFAULT_LEASE_DURATION_SECONDS):
    """Extends expiry ONLY if owner_id and generation still match the
    current row -- a stale owner cannot renew. Returns True/False."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT owner_id, generation FROM sleep_v1_lease WHERE id = 1"
        ).fetchone()
        if row is None or row[0] != owner_id or row[1] != generation:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "UPDATE sleep_v1_lease SET expires_at = ? WHERE id = 1",
            (now + duration_seconds,),
        )
        conn.execute("COMMIT")
        return True
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def release_lease(conn, *, owner_id, generation):
    """Compare-and-clear: only clears the row if it still matches
    (owner_id, generation) -- a stale owner can never clear a newer
    owner's lease. Returns True/False."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT owner_id, generation FROM sleep_v1_lease WHERE id = 1"
        ).fetchone()
        if row is None or row[0] != owner_id or row[1] != generation:
            conn.execute("ROLLBACK")
            return False
        conn.execute("DELETE FROM sleep_v1_lease WHERE id = 1")
        conn.execute("COMMIT")
        return True
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def verify_lease_for_commit(conn, *, owner_id, generation, now):
    """Pure read+compare -- performs NO transaction management of its
    own. Must be called from inside the caller's own already-open
    transaction (sleep_cycle.py's final atomic commit) so the check and
    the commit it gates are part of the SAME atomic unit. Returns True
    only if the current row still matches (owner_id, generation) and
    has not expired as of `now`."""
    row = conn.execute(
        "SELECT owner_id, generation, expires_at FROM sleep_v1_lease WHERE id = 1"
    ).fetchone()
    if row is None:
        return False
    current_owner, current_generation, expires_at = row
    if current_owner != owner_id or current_generation != generation:
        return False
    if now >= expires_at:
        return False
    return True
