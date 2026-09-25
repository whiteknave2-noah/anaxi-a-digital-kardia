"""OC0/OC1 focused outward-foundation regression suite. Zero Ollama calls, zero inference, zero
external network. Every test operates against a FRESH SYNTHETIC
anaxi_provenance.db built by provenance_schema.create_provenance_db()
in a disposable temp directory -- never the live file, never
production data.

Run from repository root: python3 -B anaxi_final/test_outward_communication.py
"""
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

from provenance_schema import create_provenance_db, derive_stable_id
from migrate_historical_data import build_pipeline_map, seed_reference_data
import hir1_schema_migration
from hir1_registration import register_canonical_human
import human_session_binding
import oc0_schema_migration
import outward_communication as oc

TEST_DIR = tempfile.mkdtemp(prefix="oc0_test_")
_MANIFEST = {"pipelines": {
    "llama": {"routing_constant_value": "nate"},
    "claude": {"routing_constant_value": "nate"},
}}
_PIPELINE_KEY = "anaxi_orchestration_lineage_a"


def _new_env(name):
    """Fresh synthetic data_dir with the full canonical schema, OC0's
    additive migration, and reference-data (pipelines + clark_agent +
    host_system actors) seeded -- no human registered yet."""
    data_dir = os.path.join(TEST_DIR, name)
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    create_provenance_db(db_path).close()
    oc0_schema_migration.apply_additive_migration(db_path)
    hir1_schema_migration.apply_additive_migration(db_path)
    pipeline_map = build_pipeline_map(_MANIFEST)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    seed_reference_data(conn, pipeline_map, int(time.time()))
    conn.close()
    return data_dir


def _register_and_bind_human(data_dir, suffix):
    """Registers a genuine synthetic human via the REAL production
    writer (hir1_registration.py) and binds a session to it via the
    REAL production writer (human_session_binding.py). Returns a
    HumanInputAuthority. Never fabricates a human row directly."""
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    reg_conn = sqlite3.connect(db_path)
    reg_conn.row_factory = sqlite3.Row
    reg_conn.execute("PRAGMA foreign_keys = ON;")
    result, failure = register_canonical_human(reg_conn, {
        "registration_request_id": f"oc0-test-reg-{suffix}",
        "aab_actor_id": f"actor-oc0-test-human-{suffix}",
        "display_label": f"OC0 Test Human {suffix}",
        "source": "local_operator_provisioning",
    })
    reg_conn.close()
    assert failure is None, failure
    actor_id = result["actor_id"]

    bind_conn = sqlite3.connect(db_path)
    bind_conn.execute("PRAGMA foreign_keys = ON;")
    authority = human_session_binding.bind_session_to_registered_human(
        bind_conn, session_id=f"oc0-test-session-{suffix}", session_started_at=int(time.time()),
        pipeline_key=_PIPELINE_KEY, actor_id=actor_id,
    )
    bind_conn.close()
    return authority


def _record_human_event(data_dir, authority, message, occurred_at=None):
    return human_session_binding.record_human_waking_input(
        data_dir, pipeline_key=_PIPELINE_KEY, authority=authority,
        message=message, occurred_at=occurred_at if occurred_at is not None else int(time.time()),
    )


def _count(data_dir, sql, params=()):
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


_PRE_CORRECTION_2_MIGRATION_COMMIT = "e90325b876e0c0cb3935376f5f629051c683cc89"


def _load_pre_correction2_schema_migration_module():
    """Loads the ACTUAL historical anaxi_final/oc0_schema_migration.py
    as committed at _PRE_CORRECTION_2_MIGRATION_COMMIT (before
    chronology enforcement existed at all -- correction #2, added
    after this commit, put it directly in
    trg_outward_reply_links_refs_exist's own body; that trigger is the
    exact one this upgrade-path regression must prove installs the
    NEW, separately-named chronology trigger on top of). Reconstructed
    via `git show` against this checkout's own history -- never a
    hand-retyped approximation -- and loaded as a distinctly-named
    module so it never collides with (or shadows) the current
    oc0_schema_migration module already imported above."""
    source = subprocess.run(
        ["git", "show", f"{_PRE_CORRECTION_2_MIGRATION_COMMIT}:anaxi_final/oc0_schema_migration.py"],
        cwd=ANAXI_FINAL, capture_output=True, text=True, check=True,
    ).stdout
    tmp_path = os.path.join(TEST_DIR, f"_pre_correction2_oc0_schema_migration_{uuid.uuid4().hex}.py")
    with open(tmp_path, "w") as f:
        f.write(source)
    spec = importlib.util.spec_from_file_location(
        f"_pre_correction2_oc0_schema_migration_{uuid.uuid4().hex}", tmp_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# =============================================================================
# 1. No preceding human required
# =============================================================================
def test_outward_act_persists_without_any_preceding_human_event():
    data_dir = _new_env("no_h")
    now = int(time.time())
    event_id = oc.generate_outward_event_id()
    act = oc.record_clark_outward_act(
        data_dir, event_id=event_id, session_id="oc0-s1", session_started_at=now,
        content="Checking in -- nothing needed, just saying hello.",
        recipient_reference="anaxi-local-recipient:household-primary",
        occurred_at=now,
    )
    assert act["event_id"] == event_id
    assert act["event_type"] == oc.CLARK_OUTWARD_ACT_EVENT_TYPE
    assert act["actor_id"] == derive_stable_id("actor", "clark")

    human_event_count = _count(
        data_dir, "SELECT COUNT(*) FROM events WHERE event_type = 'human_waking_input'"
    )
    assert human_event_count == 0, (
        f"expected zero fabricated human_waking_input events, found {human_event_count}"
    )
    total_events = _count(data_dir, "SELECT COUNT(*) FROM events")
    assert total_events == 1, f"expected exactly one event (the outward act), found {total_events}"


# =============================================================================
# 2. Identity/actor/recipient/chronology/content survive a fresh read
# =============================================================================
def test_outward_act_survives_a_fresh_read():
    data_dir = _new_env("fresh_read")
    now = int(time.time())
    event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=event_id, session_id="oc0-s2", session_started_at=now,
        content="A note left for later, unprompted.",
        recipient_reference="anaxi-local-recipient:nate",
        occurred_at=now,
    )
    reread = oc.fetch_outward_act(data_dir, event_id)
    assert reread is not None
    assert reread["event_id"] == event_id
    assert reread["content"] == "A note left for later, unprompted."
    assert reread["recipient_reference"] == "anaxi-local-recipient:nate"
    assert reread["occurred_at"] == now
    assert reread["actor_id"] == derive_stable_id("actor", "clark")
    assert reread["record_created_at"] >= reread["occurred_at"]


def test_nonexistent_outward_act_fetch_returns_none():
    data_dir = _new_env("fetch_missing")
    assert oc.fetch_outward_act(data_dir, "does-not-exist") is None


# =============================================================================
# 3. Projection is subordinate to the canonical act
# =============================================================================
def test_projection_attempt_against_nonexistent_event_is_rejected():
    data_dir = _new_env("projection_nonexistent")
    try:
        oc.record_projection_attempt(
            data_dir, outward_event_id="never-recorded", status="attempted",
            attempted_at=int(time.time()), observed_at=int(time.time()),
        )
        assert False, "expected OutwardCommunicationError"
    except oc.OutwardCommunicationError:
        pass
    count = _count(data_dir, "SELECT COUNT(*) FROM outward_projection_attempts")
    assert count == 0, f"expected zero projection attempt rows, found {count}"


def test_projection_attempt_rejects_invalid_status():
    data_dir = _new_env("projection_bad_status")
    now = int(time.time())
    event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=event_id, session_id="oc0-s3", session_started_at=now,
        content="content", recipient_reference="anaxi-local-recipient:x", occurred_at=now,
    )
    try:
        oc.record_projection_attempt(
            data_dir, outward_event_id=event_id, status="delivered_and_read",
            attempted_at=now, observed_at=now,
        )
        assert False, "expected OutwardCommunicationError for invalid status"
    except oc.OutwardCommunicationError:
        pass


# =============================================================================
# 4. Failed projection leaves the canonical act intact
# =============================================================================
def test_failed_projection_leaves_canonical_act_intact():
    data_dir = _new_env("failed_projection")
    now = int(time.time())
    event_id = oc.generate_outward_event_id()
    before = oc.record_clark_outward_act(
        data_dir, event_id=event_id, session_id="oc0-s4", session_started_at=now,
        content="Trying to reach out.", recipient_reference="anaxi-local-recipient:y",
        occurred_at=now,
    )
    oc.record_projection_attempt(
        data_dir, outward_event_id=event_id, status="failed",
        attempted_at=now, observed_at=now + 1, detail="synthetic transport unavailable",
    )
    after = oc.fetch_outward_act(data_dir, event_id)
    assert after == before, "canonical act mutated by a failed projection attempt"
    events_count = _count(data_dir, "SELECT COUNT(*) FROM events WHERE event_id = ?", (event_id,))
    assert events_count == 1


# =============================================================================
# 5. Retry does not duplicate the canonical act; attempts are distinguishable
# =============================================================================
def test_retry_does_not_duplicate_canonical_act():
    data_dir = _new_env("retry_no_dup")
    now = int(time.time())
    event_id = oc.generate_outward_event_id()
    kwargs = dict(
        event_id=event_id, session_id="oc0-s5", session_started_at=now,
        content="Same content, retried.", recipient_reference="anaxi-local-recipient:z",
        occurred_at=now,
    )
    first = oc.record_clark_outward_act(data_dir, **kwargs)
    second = oc.record_clark_outward_act(data_dir, **kwargs)
    assert first == second
    assert _count(data_dir, "SELECT COUNT(*) FROM events WHERE event_id = ?", (event_id,)) == 1
    assert _count(data_dir, "SELECT COUNT(*) FROM event_components WHERE event_id = ?", (event_id,)) == 2


def test_retry_with_different_content_is_refused_not_silently_merged():
    data_dir = _new_env("retry_diff_content")
    now = int(time.time())
    event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=event_id, session_id="oc0-s6", session_started_at=now,
        content="Original content.", recipient_reference="anaxi-local-recipient:w",
        occurred_at=now,
    )
    try:
        oc.record_clark_outward_act(
            data_dir, event_id=event_id, session_id="oc0-s6", session_started_at=now,
            content="Materially different content.", recipient_reference="anaxi-local-recipient:w",
            occurred_at=now,
        )
        assert False, "expected OutwardCommunicationError: content must not silently change under the same event_id"
    except oc.OutwardCommunicationError:
        pass


def test_repeated_projection_attempts_are_mechanically_distinguishable():
    data_dir = _new_env("repeated_attempts")
    now = int(time.time())
    event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=event_id, session_id="oc0-s7", session_started_at=now,
        content="Attempt this a few times.", recipient_reference="anaxi-local-recipient:v",
        occurred_at=now,
    )
    canonical_hash = oc.fetch_outward_act(data_dir, event_id)["content_sha256"]

    a1 = oc.record_projection_attempt(data_dir, outward_event_id=event_id, status="failed",
                                       attempted_at=now, observed_at=now + 1)
    a2 = oc.record_projection_attempt(data_dir, outward_event_id=event_id, status="failed",
                                       attempted_at=now + 2, observed_at=now + 3)
    a3 = oc.record_projection_attempt(data_dir, outward_event_id=event_id, status="succeeded",
                                       attempted_at=now + 4, observed_at=now + 5)
    assert [a1["attempt_sequence"], a2["attempt_sequence"], a3["attempt_sequence"]] == [1, 2, 3]
    assert len({a1["projection_attempt_id"], a2["projection_attempt_id"], a3["projection_attempt_id"]}) == 3
    for attempt in (a1, a2, a3):
        assert attempt["content_sha256"] == canonical_hash

    attempts = oc.fetch_projection_attempts(data_dir, event_id)
    assert [a["status"] for a in attempts] == ["failed", "failed", "succeeded"]
    # only one canonical act, regardless of how many attempts were made
    assert _count(data_dir, "SELECT COUNT(*) FROM events WHERE event_id = ?", (event_id,)) == 1


def test_projection_succeeded_status_never_implies_human_attention():
    """Mechanical distinctness proof: 'succeeded' means the projection
    mechanism reported success -- it carries no field for human
    attention/reading/agreement, and this module records none."""
    data_dir = _new_env("no_overclaim")
    now = int(time.time())
    event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=event_id, session_id="oc0-s8", session_started_at=now,
        content="Sent successfully, mechanically.", recipient_reference="anaxi-local-recipient:u",
        occurred_at=now,
    )
    attempt = oc.record_projection_attempt(
        data_dir, outward_event_id=event_id, status="succeeded", attempted_at=now, observed_at=now,
    )
    assert set(attempt.keys()) == {
        "projection_attempt_id", "outward_event_id", "attempt_sequence", "content_sha256",
        "status", "attempted_at", "observed_at", "detail",
    }, "projection attempt record carries only mechanical fields, no attention/read/agreement claim"


# =============================================================================
# 6. Genuine later human reply can explicitly reference the outward act
# =============================================================================
def test_genuine_human_reply_can_reference_outward_act():
    data_dir = _new_env("reply_link")
    now = int(time.time())
    outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=outward_event_id, session_id="oc0-s9", session_started_at=now,
        content="Reaching out first.", recipient_reference="anaxi-local-recipient:nate",
        occurred_at=now,
    )
    authority = _register_and_bind_human(data_dir, "reply1")
    human_event_id = _record_human_event(data_dir, authority, "Replying to your message.")

    link = oc.record_reply_link(
        data_dir, outward_event_id=outward_event_id, human_event_id=human_event_id,
        linked_at=int(time.time()),
    )
    assert link["outward_event_id"] == outward_event_id
    assert link["human_event_id"] == human_event_id

    fetched = oc.fetch_reply_link_for_human_event(data_dir, human_event_id)
    assert fetched["outward_event_id"] == outward_event_id

    # H remains a real, independent human_waking_input event -- unmodified in shape.
    h_row = _count(
        data_dir,
        "SELECT COUNT(*) FROM events WHERE event_id = ? AND event_type = 'human_waking_input'",
        (human_event_id,),
    )
    assert h_row == 1


def test_relinking_same_pair_is_idempotent():
    data_dir = _new_env("relink_idempotent")
    now = int(time.time())
    outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=outward_event_id, session_id="oc0-s10", session_started_at=now,
        content="Reaching out again.", recipient_reference="anaxi-local-recipient:nate",
        occurred_at=now,
    )
    authority = _register_and_bind_human(data_dir, "reply2")
    human_event_id = _record_human_event(data_dir, authority, "Replying again.")

    first = oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                                  human_event_id=human_event_id, linked_at=now)
    second = oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                                   human_event_id=human_event_id, linked_at=now)
    assert first["reply_link_id"] == second["reply_link_id"]
    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links") == 1


def test_replay_with_different_linked_at_reports_canonical_chronology():
    """Verifier-reported regression: a replay of an already-existing
    reply link with a DIFFERENT caller-supplied linked_at must return
    the ORIGINAL canonical linked_at in its receipt, never the new
    caller-supplied value -- and must not mutate stored chronology or
    create a duplicate row."""
    data_dir = _new_env("relink_chronology")
    now = int(time.time())

    outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=outward_event_id, session_id="oc0-s10b", session_started_at=now,
        content="Reaching out, replayed later.", recipient_reference="anaxi-local-recipient:nate",
        occurred_at=now,
    )
    authority = _register_and_bind_human(data_dir, "reply2b")
    human_event_id = _record_human_event(data_dir, authority, "Replying, replayed later.")
    # Taken AFTER the human event exists: linked_at may not predate it, and a clock-second rollover
    # between an earlier `now` and the event made this test fail intermittently under suite load.
    t1 = int(time.time())
    t2 = t1 + 1
    assert t1 != t2

    first = oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                                  human_event_id=human_event_id, linked_at=t1)
    assert first["linked_at"] == t1

    replay = oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                                   human_event_id=human_event_id, linked_at=t2)
    assert replay["reply_link_id"] == first["reply_link_id"], \
        "replay must return the same reply-link identity"
    assert replay["linked_at"] == t1, \
        f"replay receipt must report canonical linked_at={t1!r}, not caller-supplied {t2!r}"

    fresh = oc.fetch_reply_link_for_human_event(data_dir, human_event_id)
    assert fresh["linked_at"] == t1, "canonical storage must be unchanged by the replay"

    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links") == 1, \
        "replay must not create a duplicate link row"
    stored_linked_at = _count(
        data_dir, "SELECT linked_at FROM outward_act_reply_links WHERE human_event_id = ?",
        (human_event_id,),
    )
    assert stored_linked_at == t1, "stored row must not have been mutated"


# =============================================================================
# 6b. Reply-link chronology: a link must not assert an impossible sequence
# =============================================================================
def test_reply_link_rejects_canonically_earlier_human_event():
    """A. Earlier-H rejection: H occurred strictly before the outward
    act. The link must be refused, and neither referenced canonical
    event may be mutated, and no fabricated replacement event may be
    created."""
    data_dir = _new_env("chrono_earlier_h")
    h1 = int(time.time())
    o1 = h1 + 100
    authority = _register_and_bind_human(data_dir, "chrono_earlier")
    human_event_id = _record_human_event(data_dir, authority, "An earlier message.", occurred_at=h1)

    outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=outward_event_id, session_id="oc0-chrono1", session_started_at=o1,
        content="A later outward act.", recipient_reference="anaxi-local-recipient:nate",
        occurred_at=o1,
    )

    human_before = _count(data_dir, "SELECT occurred_at FROM events WHERE event_id = ?", (human_event_id,))
    outward_before = oc.fetch_outward_act(data_dir, outward_event_id)

    try:
        oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                              human_event_id=human_event_id, linked_at=o1 + 200)
        assert False, "expected OutwardCommunicationError: H is canonically earlier than the outward act"
    except oc.OutwardCommunicationError:
        pass

    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links") == 0, \
        "no reply-link row may be recorded for a proven-impossible chronology"
    assert _count(data_dir, "SELECT occurred_at FROM events WHERE event_id = ?", (human_event_id,)) == human_before, \
        "the referenced human event must not be mutated"
    assert oc.fetch_outward_act(data_dir, outward_event_id) == outward_before, \
        "the referenced outward act must not be mutated"
    assert _count(data_dir, "SELECT COUNT(*) FROM events WHERE event_type = 'human_waking_input'") == 1, \
        "no fabricated replacement human event may be created"


def test_reply_link_allows_canonically_later_human_event():
    """B. Lawful later-H link: the outward act occurs first, the human
    event occurs strictly later. The link must succeed and survive a
    fresh read."""
    data_dir = _new_env("chrono_later_h")
    o1 = int(time.time())
    h1 = o1 + 100

    outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=outward_event_id, session_id="oc0-chrono2", session_started_at=o1,
        content="An earlier outward act.", recipient_reference="anaxi-local-recipient:nate",
        occurred_at=o1,
    )
    authority = _register_and_bind_human(data_dir, "chrono_later")
    human_event_id = _record_human_event(data_dir, authority, "A genuinely later reply.", occurred_at=h1)

    link = oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                                 human_event_id=human_event_id, linked_at=h1 + 1)
    assert link["outward_event_id"] == outward_event_id
    assert link["human_event_id"] == human_event_id

    fresh = oc.fetch_reply_link_for_human_event(data_dir, human_event_id)
    assert fresh is not None and fresh["outward_event_id"] == outward_event_id
    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links") == 1


def test_reply_link_allows_equal_canonical_timestamps():
    """C. Boundary/equal-time behavior: occurred_at is second-resolution
    and is the only canonical chronology field on events (no monotonic
    secondary ordering signal exists -- event_id ULIDs here carry a
    random, not counter-based, tail). A reply link where the human
    event and outward act share the SAME occurred_at is therefore
    accepted: the stored evidence does not (and cannot) prove the human
    event earlier, and this module never invents artificial sub-second
    precision to reject it. This is a deliberate, documented choice,
    not silent leniency: only a PROVEN-earlier human event is
    rejected."""
    data_dir = _new_env("chrono_equal")
    shared_timestamp = int(time.time())

    outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=outward_event_id, session_id="oc0-chrono3", session_started_at=shared_timestamp,
        content="Same-second outward act.", recipient_reference="anaxi-local-recipient:nate",
        occurred_at=shared_timestamp,
    )
    authority = _register_and_bind_human(data_dir, "chrono_equal")
    human_event_id = _record_human_event(data_dir, authority, "Same-second reply.",
                                          occurred_at=shared_timestamp)

    link = oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                                 human_event_id=human_event_id, linked_at=shared_timestamp)
    assert link["outward_event_id"] == outward_event_id
    fresh = oc.fetch_reply_link_for_human_event(data_dir, human_event_id)
    assert fresh is not None and fresh["outward_event_id"] == outward_event_id


def test_reply_link_rejects_linked_at_predating_either_referenced_event():
    """D. Link-recording chronology: linked_at must not claim the link
    was recorded before either canonical event it references. No
    receipt may claim an impossible canonical sequence."""
    data_dir = _new_env("chrono_linked_at")
    o1 = int(time.time())
    h1 = o1 + 50

    outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=outward_event_id, session_id="oc0-chrono4", session_started_at=o1,
        content="Outward act.", recipient_reference="anaxi-local-recipient:nate", occurred_at=o1,
    )
    authority = _register_and_bind_human(data_dir, "chrono_linked_at")
    human_event_id = _record_human_event(data_dir, authority, "Later reply.", occurred_at=h1)

    # linked_at predates the outward act (and therefore the human event too).
    try:
        oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                              human_event_id=human_event_id, linked_at=o1 - 10)
        assert False, "expected OutwardCommunicationError: linked_at predates the outward act"
    except oc.OutwardCommunicationError:
        pass
    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links") == 0

    # linked_at is after the outward act but still predates the human event.
    try:
        oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                              human_event_id=human_event_id, linked_at=o1 + 1)
        assert False, "expected OutwardCommunicationError: linked_at predates the human event"
    except oc.OutwardCommunicationError:
        pass
    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links") == 0

    # A truthful linked_at (at or after both) succeeds.
    link = oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                                 human_event_id=human_event_id, linked_at=h1)
    assert link["outward_event_id"] == outward_event_id
    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links") == 1


# =============================================================================
# 6c. Migration upgrade-safety: chronology enforcement must install on
# BOTH a fresh database and a database already migrated by the
# pre-chronology schema, not only the former.
# =============================================================================
def test_migration_installs_chronology_trigger_on_fresh_database():
    """1. Fresh database: applying the current migration must install
    the separately-named chronology trigger and it must actually
    enforce chronology."""
    data_dir = _new_env("migration_fresh")
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    state = oc0_schema_migration.verify_migration_state(db_path)
    assert state["has_reply_link_chronology_trigger"] is True

    h1 = int(time.time())
    o1 = h1 + 100
    authority = _register_and_bind_human(data_dir, "migfresh")
    human_event_id = _record_human_event(data_dir, authority, "Earlier message.", occurred_at=h1)
    outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=outward_event_id, session_id="oc0-migfresh", session_started_at=o1,
        content="Later outward act.", recipient_reference="anaxi-local-recipient:nate", occurred_at=o1,
    )
    try:
        oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                              human_event_id=human_event_id, linked_at=o1 + 1)
        assert False, "expected rejection: fresh-database chronology enforcement must be active"
    except oc.OutwardCommunicationError:
        pass


def test_migration_upgrade_path_from_pre_correction2_schema():
    """Reproduces the verifier's exact defect and proves the fix.

    2. Real upgrade path: build a database using the historical
    PRE-CORRECTION-2 migration (commit e90325b, reconstructed via `git
    show`, before any chronology trigger existed), seed lawful
    historical OC0 data under that old schema, then reproduce the
    verifier's defect (an illegal-chronology reply link inserts and
    persists because the old schema has no chronology trigger at all).
    Snapshot every existing row. Apply the CURRENT (corrected)
    migration on top -- the actual upgrade. Require: every existing
    table/row (including the historically-permitted illegal-chronology
    row -- OC0 never rewrites history) is byte/value-identical
    afterward; the new chronology trigger is now installed; a NEW
    earlier-H attempt is rejected; a NEW linked_at-predates-either-event
    attempt is rejected; a NEW lawful later-H link succeeds; the
    documented equal-timestamp case remains accepted.

    3. Migration idempotency: apply the corrected migration a SECOND
    time. Require no error, chronology enforcement remains intact, and
    every row (old and newly added) is unchanged by the re-migration.

    4. Existing historical data: the lawful pre-existing outward act
    and its lawful pre-existing reply link, seeded before the upgrade,
    must read back with every field byte/value-identical afterward --
    proving the upgrade never rewrites historical chronology or
    content."""
    data_dir = os.path.join(TEST_DIR, "migration_upgrade")
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")

    # --- Build the historical PRE-CORRECTION-2 schema/state. ---
    create_provenance_db(db_path).close()
    old_migration = _load_pre_correction2_schema_migration_module()
    old_migration.apply_additive_migration(db_path)
    hir1_schema_migration.apply_additive_migration(db_path)
    pipeline_map = build_pipeline_map(_MANIFEST)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    seed_reference_data(conn, pipeline_map, int(time.time()))
    conn.close()

    pre_state = old_migration.verify_migration_state(db_path)
    assert pre_state["has_outward_projection_attempts"] is True
    assert pre_state["has_outward_act_reply_links"] is True
    assert "has_reply_link_chronology_trigger" not in pre_state, (
        "the historical pre-correction-2 module has no notion of a chronology trigger at all"
    )

    # --- Seed LAWFUL pre-existing historical OC0 data under the old schema. ---
    lawful_o1 = int(time.time())
    lawful_h1 = lawful_o1 + 500
    lawful_outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=lawful_outward_event_id, session_id="oc0-mig-lawful",
        session_started_at=lawful_o1, content="Lawful pre-existing outward act.",
        recipient_reference="anaxi-local-recipient:nate", occurred_at=lawful_o1,
    )
    lawful_authority = _register_and_bind_human(data_dir, "miglawful")
    lawful_human_event_id = _record_human_event(data_dir, lawful_authority,
                                                  "Lawful pre-existing reply.", occurred_at=lawful_h1)
    lawful_link = oc.record_reply_link(data_dir, outward_event_id=lawful_outward_event_id,
                                        human_event_id=lawful_human_event_id, linked_at=lawful_h1 + 1)

    # --- Reproduce the verifier's exact defect under the OLD schema: an
    # illegal-chronology reply link (H before O, linked_at predating
    # both) inserts and persists, because the old schema's trigger
    # never checks chronology at all. ---
    bad_o1 = int(time.time()) + 1000
    bad_h1 = bad_o1 - 100  # H strictly earlier than O
    bad_outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=bad_outward_event_id, session_id="oc0-mig-bad", session_started_at=bad_o1,
        content="Outward act for the reproduced defect.", recipient_reference="anaxi-local-recipient:nate",
        occurred_at=bad_o1,
    )
    bad_authority = _register_and_bind_human(data_dir, "migbad")
    bad_human_event_id = _record_human_event(data_dir, bad_authority, "Earlier message (illegal chronology).",
                                              occurred_at=bad_h1)
    bad_link = oc.record_reply_link(data_dir, outward_event_id=bad_outward_event_id,
                                     human_event_id=bad_human_event_id, linked_at=bad_h1 - 50)
    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links WHERE reply_link_id = ?",
                  (bad_link["reply_link_id"],)) == 1, (
        "reproduction check: the old (pre-correction-2) schema must accept the illegal-chronology "
        "link -- if this assertion fails, the defect could not be reproduced as reported"
    )

    # --- Snapshot every row that exists before the upgrade. ---
    def _snapshot():
        c = sqlite3.connect(db_path)
        try:
            events = c.execute("SELECT event_id, event_type, occurred_at, record_created_at FROM events "
                                "ORDER BY event_id").fetchall()
            components = c.execute("SELECT event_id, sequence, component_kind, component_text, content_sha256 "
                                    "FROM event_components ORDER BY event_id, sequence").fetchall()
            reply_links = c.execute("SELECT reply_link_id, outward_event_id, human_event_id, linked_at "
                                     "FROM outward_act_reply_links ORDER BY reply_link_id").fetchall()
            return events, components, reply_links
        finally:
            c.close()

    before_events, before_components, before_reply_links = _snapshot()
    assert len(before_reply_links) == 2, "expected the lawful link and the reproduced bad-chronology link"

    # --- THE UPGRADE: apply the corrected migration on top of the
    # already-populated, old-schema database. ---
    oc0_schema_migration.apply_additive_migration(db_path)

    upgraded_state = oc0_schema_migration.verify_migration_state(db_path)
    assert upgraded_state["has_reply_link_chronology_trigger"] is True, (
        "the corrected migration must install chronology enforcement on an already-migrated database, "
        "not only on a fresh one"
    )

    after_events, after_components, after_reply_links = _snapshot()
    assert after_events == before_events, "upgrade must not rewrite any existing canonical event"
    assert after_components == before_components, "upgrade must not rewrite any existing event component"
    assert after_reply_links == before_reply_links, (
        "upgrade must not rewrite any existing reply-link row -- including the historically-permitted "
        "illegal-chronology one; OC0 never rewrites history"
    )

    # --- New chronology violations are now rejected (post-upgrade). ---
    new_o1 = int(time.time()) + 2000
    new_h1 = new_o1 - 100
    new_outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=new_outward_event_id, session_id="oc0-mig-postupgrade",
        session_started_at=new_o1, content="Post-upgrade outward act.",
        recipient_reference="anaxi-local-recipient:nate", occurred_at=new_o1,
    )
    new_authority = _register_and_bind_human(data_dir, "migpostupgrade")
    new_human_event_id = _record_human_event(data_dir, new_authority, "Earlier message (post-upgrade).",
                                              occurred_at=new_h1)
    try:
        oc.record_reply_link(data_dir, outward_event_id=new_outward_event_id,
                              human_event_id=new_human_event_id, linked_at=new_o1 + 1)
        assert False, "post-upgrade: earlier-H reply link must now be rejected"
    except oc.OutwardCommunicationError:
        pass

    # linked_at predating either referenced event is now rejected too.
    later_human_event_id = _record_human_event(data_dir, new_authority,
                                                "Genuinely later reply (post-upgrade).",
                                                occurred_at=new_o1 + 50)
    try:
        oc.record_reply_link(data_dir, outward_event_id=new_outward_event_id,
                              human_event_id=later_human_event_id, linked_at=new_o1 - 1)
        assert False, "post-upgrade: linked_at predating the outward act must now be rejected"
    except oc.OutwardCommunicationError:
        pass

    # A lawful later-H link still succeeds post-upgrade.
    lawful_post_upgrade_link = oc.record_reply_link(
        data_dir, outward_event_id=new_outward_event_id, human_event_id=later_human_event_id,
        linked_at=new_o1 + 51,
    )
    assert lawful_post_upgrade_link["outward_event_id"] == new_outward_event_id

    # The documented equal-timestamp acceptance still holds post-upgrade.
    equal_ts = int(time.time()) + 3000
    equal_outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=equal_outward_event_id, session_id="oc0-mig-equalts",
        session_started_at=equal_ts, content="Same-second post-upgrade outward act.",
        recipient_reference="anaxi-local-recipient:nate", occurred_at=equal_ts,
    )
    equal_authority = _register_and_bind_human(data_dir, "migequalts")
    equal_human_event_id = _record_human_event(data_dir, equal_authority, "Same-second reply (post-upgrade).",
                                                occurred_at=equal_ts)
    equal_link = oc.record_reply_link(data_dir, outward_event_id=equal_outward_event_id,
                                       human_event_id=equal_human_event_id, linked_at=equal_ts)
    assert equal_link["outward_event_id"] == equal_outward_event_id

    # --- 3. Migration idempotency: apply the corrected migration a
    # SECOND time on the now-upgraded database. ---
    before_second_events, before_second_components, before_second_reply_links = _snapshot()
    oc0_schema_migration.apply_additive_migration(db_path)  # must not error
    still_upgraded_state = oc0_schema_migration.verify_migration_state(db_path)
    assert still_upgraded_state["has_reply_link_chronology_trigger"] is True
    after_second_events, after_second_components, after_second_reply_links = _snapshot()
    assert after_second_events == before_second_events
    assert after_second_components == before_second_components
    assert after_second_reply_links == before_second_reply_links, (
        "re-applying the migration must not duplicate, delete, or rewrite any reply-link row"
    )

    # Chronology enforcement still rejects a fresh violation after the
    # second (idempotent) migration application.
    try:
        oc.record_reply_link(data_dir, outward_event_id=new_outward_event_id,
                              human_event_id=new_human_event_id, linked_at=new_o1 + 1)
        assert False, "chronology enforcement must remain intact after a second migration application"
    except oc.OutwardCommunicationError:
        pass

    # --- 4. Existing historical data: the lawful pre-existing outward
    # act and reply link, seeded before the upgrade, are byte/value-
    # identical to what fetch_outward_act/fetch_reply_link_for_human_event
    # return now. ---
    reread_lawful_act = oc.fetch_outward_act(data_dir, lawful_outward_event_id)
    assert reread_lawful_act["content"] == "Lawful pre-existing outward act."
    assert reread_lawful_act["occurred_at"] == lawful_o1
    reread_lawful_link = oc.fetch_reply_link_for_human_event(data_dir, lawful_human_event_id)
    assert reread_lawful_link == lawful_link, (
        "the lawful pre-existing reply link must be byte/value-identical after the upgrade"
    )


# =============================================================================
# 7. Unrelated later human event is not automatically linked
# =============================================================================
def test_unrelated_human_event_is_not_automatically_linked():
    data_dir = _new_env("unrelated_human")
    now = int(time.time())
    outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=outward_event_id, session_id="oc0-s11", session_started_at=now,
        content="An outward act nobody replies to yet.", recipient_reference="anaxi-local-recipient:nate",
        occurred_at=now,
    )
    authority = _register_and_bind_human(data_dir, "unrelated1")
    unrelated_human_event_id = _record_human_event(data_dir, authority, "Something unrelated entirely.")

    assert oc.fetch_reply_link_for_human_event(data_dir, unrelated_human_event_id) is None
    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links") == 0


# =============================================================================
# 8. Unanswered outward act creates no obligation
# =============================================================================
def test_unanswered_outward_act_creates_no_synthetic_h_or_obligation():
    data_dir = _new_env("unanswered")
    now = int(time.time())
    event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=event_id, session_id="oc0-s12", session_started_at=now,
        content="Said something; nobody answered.", recipient_reference="anaxi-local-recipient:nate",
        occurred_at=now,
    )
    human_events = _count(data_dir, "SELECT COUNT(*) FROM events WHERE event_type = 'human_waking_input'")
    assert human_events == 0
    reply_links = _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links")
    assert reply_links == 0
    # absence of any projection attempt row IS "not attempted" -- not a failure state.
    assert oc.fetch_projection_attempts(data_dir, event_id) == []


# =============================================================================
# 9. Malformed / nonexistent references for reply linkage are rejected
# =============================================================================
def test_reply_link_rejects_nonexistent_outward_event():
    data_dir = _new_env("reply_bad_outward")
    authority = _register_and_bind_human(data_dir, "badref1")
    human_event_id = _record_human_event(data_dir, authority, "A message.")
    try:
        oc.record_reply_link(data_dir, outward_event_id="does-not-exist",
                              human_event_id=human_event_id, linked_at=int(time.time()))
        assert False, "expected OutwardCommunicationError"
    except oc.OutwardCommunicationError:
        pass
    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links") == 0


def test_reply_link_rejects_nonexistent_human_event():
    data_dir = _new_env("reply_bad_human")
    now = int(time.time())
    event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=event_id, session_id="oc0-s13", session_started_at=now,
        content="content", recipient_reference="anaxi-local-recipient:nate", occurred_at=now,
    )
    try:
        oc.record_reply_link(data_dir, outward_event_id=event_id,
                              human_event_id="does-not-exist", linked_at=now)
        assert False, "expected OutwardCommunicationError"
    except oc.OutwardCommunicationError:
        pass
    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links") == 0


def test_reply_link_rejects_wrong_event_type_on_both_sides():
    """A syntactically real event_id of the WRONG type must be
    rejected, not merely a bare unknown string -- proves the trigger
    checks event_type, not just row existence."""
    data_dir = _new_env("reply_wrong_type")
    now = int(time.time())
    outward_event_id = oc.generate_outward_event_id()
    oc.record_clark_outward_act(
        data_dir, event_id=outward_event_id, session_id="oc0-s14", session_started_at=now,
        content="content", recipient_reference="anaxi-local-recipient:nate", occurred_at=now,
    )
    authority = _register_and_bind_human(data_dir, "wrongtype1")
    human_event_id = _record_human_event(data_dir, authority, "A message.")

    # human_event_id used as the outward_event_id slot -- wrong type.
    try:
        oc.record_reply_link(data_dir, outward_event_id=human_event_id,
                              human_event_id=human_event_id, linked_at=now)
        assert False, "expected OutwardCommunicationError: human event used as outward event"
    except oc.OutwardCommunicationError:
        pass

    # outward_event_id used as the human_event_id slot -- wrong type.
    try:
        oc.record_reply_link(data_dir, outward_event_id=outward_event_id,
                              human_event_id=outward_event_id, linked_at=now)
        assert False, "expected OutwardCommunicationError: outward event used as human event"
    except oc.OutwardCommunicationError:
        pass

    assert _count(data_dir, "SELECT COUNT(*) FROM outward_act_reply_links") == 0


# =============================================================================
# 10. Validation: content / recipient must be genuinely nonempty
# =============================================================================
def test_empty_content_is_refused():
    data_dir = _new_env("empty_content")
    now = int(time.time())
    try:
        oc.record_clark_outward_act(
            data_dir, event_id=oc.generate_outward_event_id(), session_id="oc0-s15",
            session_started_at=now, content="   ", recipient_reference="anaxi-local-recipient:nate",
            occurred_at=now,
        )
        assert False, "expected OutwardCommunicationError for empty content"
    except oc.OutwardCommunicationError:
        pass
    assert _count(data_dir, "SELECT COUNT(*) FROM events") == 0


def test_empty_recipient_reference_is_refused():
    data_dir = _new_env("empty_recipient")
    now = int(time.time())
    try:
        oc.record_clark_outward_act(
            data_dir, event_id=oc.generate_outward_event_id(), session_id="oc0-s16",
            session_started_at=now, content="Real content.", recipient_reference="",
            occurred_at=now,
        )
        assert False, "expected OutwardCommunicationError for empty recipient_reference"
    except oc.OutwardCommunicationError:
        pass
    assert _count(data_dir, "SELECT COUNT(*) FROM events") == 0


# =============================================================================
# 11. Private Space is never imported by this module
# =============================================================================
def test_module_never_imports_private_space():
    import outward_communication as _oc_mod
    import oc0_schema_migration as _oc0_mod
    assert "workspace_private" not in dir(_oc_mod)
    assert "workspace_private" not in dir(_oc0_mod)
    for filename in ("outward_communication.py", "oc0_schema_migration.py"):
        with open(os.path.join(ANAXI_FINAL, filename)) as f:
            for line in f:
                stripped = line.strip()
                assert not (stripped.startswith("import workspace_private")
                            or stripped.startswith("from workspace_private")), (
                    f"{filename} must never import workspace_private (Private Space): {line!r}"
                )


# OC1 stabilization: all fixtures below are disposable and use real SQLite.
def _rows(data_dir):
    """All persisted values, including reference and SQLite sequence rows."""
    conn = sqlite3.connect(os.path.join(data_dir, 'anaxi_provenance.db'))
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: sorted(conn.execute('SELECT * FROM "' + name + '"').fetchall(), key=repr) for name in names}
    finally:
        conn.close()


def _objects(data_dir):
    conn = sqlite3.connect(os.path.join(data_dir, 'anaxi_provenance.db'))
    try:
        return conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
    finally:
        conn.close()


def _sql(data_dir, sql, args=()):
    conn = sqlite3.connect(os.path.join(data_dir, 'anaxi_provenance.db'))
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        result = conn.execute(sql, args).fetchall()
        conn.commit()
        return result
    finally:
        conn.close()


def _act(data_dir, **overrides):
    args = dict(event_id='oc1-outward', session_id='oc1-session', session_started_at=100,
                content='Stable payload π', recipient_reference='anaxi-local-recipient:test', occurred_at=100)
    args.update(overrides)
    return oc.record_clark_outward_act(data_dir, **args)


def _raises(error_type, text, call):
    try:
        call()
    except error_type as exc:
        assert text in str(exc), str(exc)
        return exc
    raise AssertionError('Expected ' + error_type.__name__ + ': ' + text)


def test_oc1_canonical_failure_rolls_back_all_rows():
    # Fail after each prior INSERT, including the final component write.
    for table, condition in [('auth_contexts', '1'), ('events', '1'),
                             ('event_components', 'NEW.sequence=0'), ('event_components', 'NEW.sequence=1')]:
        data_dir = _new_env('oc1_atomic_' + table + condition[-1])
        _sql(data_dir, f"CREATE TRIGGER injected_failure BEFORE INSERT ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        before = _rows(data_dir)
        _raises(sqlite3.IntegrityError, 'injected failure', lambda: _act(data_dir))
        assert _rows(data_dir) == before
        assert oc.fetch_outward_act(data_dir, 'oc1-outward') is None


def test_oc1_canonical_replay_checks_session_start():
    data_dir = _new_env('oc1_replay_start')
    first = _act(data_dir)
    before = _rows(data_dir)
    assert _act(data_dir) == first == oc.fetch_outward_act(data_dir, first['event_id'])
    _raises(oc.OutwardCommunicationError, 'started_at', lambda: _act(data_dir, session_started_at=99))
    assert _rows(data_dir) == before


def test_oc1_canonical_conflicts_preserve_all_rows():
    data_dir = _new_env('oc1_conflicts')
    first = _act(data_dir)
    before = _rows(data_dir)
    for change in [dict(content='different'), dict(recipient_reference='different'),
                   dict(occurred_at=101), dict(session_id='different')]:
        _raises(oc.OutwardCommunicationError, 'already exists', lambda: _act(data_dir, **change))
        assert _rows(data_dir) == before
    assert oc.fetch_outward_act(data_dir, first['event_id']) == first


def test_oc1_timestamp_inputs_fail_closed():
    data_dir = _new_env('oc1_timestamp_inputs')
    before = _rows(data_dir)
    for field in ('occurred_at', 'session_started_at'):
        for invalid in ('100', 'not-a-time', 100.5, True, None, 2**63, -(2**63)-1):
            _raises(oc.OutwardCommunicationError, field, lambda: _act(data_dir, **{field: invalid}))
            assert _rows(data_dir) == before
    act = _act(data_dir)
    before = _rows(data_dir)
    for field in ('attempted_at', 'observed_at'):
        for invalid in ('100', 'not-a-time', 100.5, True, None, 2**63, -(2**63)-1):
            args = dict(outward_event_id=act['event_id'], status='attempted', attempted_at=100, observed_at=100)
            args[field] = invalid
            _raises(oc.OutwardCommunicationError, field, lambda: oc.record_projection_attempt(data_dir, **args))
            assert _rows(data_dir) == before
    authority = _register_and_bind_human(data_dir, 'oc1badtime')
    h = _record_human_event(data_dir, authority, 'Reply', occurred_at=101)
    before = _rows(data_dir)
    for invalid in ('102', 'not-a-time', 102.5, True, None, 2**63, -(2**63)-1):
        _raises(oc.OutwardCommunicationError, 'linked_at', lambda: oc.record_reply_link(data_dir, outward_event_id=act['event_id'], human_event_id=h, linked_at=invalid))
        assert _rows(data_dir) == before


def test_oc1_projection_failure_and_receipt_truthfulness():
    data_dir = _new_env('oc1_projection_atomic')
    act = _act(data_dir)
    _sql(data_dir, "CREATE TRIGGER injected_failure AFTER INSERT ON outward_projection_attempts BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    before = _rows(data_dir)
    _raises(sqlite3.IntegrityError, 'injected failure', lambda: oc.record_projection_attempt(data_dir, outward_event_id=act['event_id'], status='succeeded', attempted_at=101, observed_at=102))
    assert _rows(data_dir) == before
    assert oc.fetch_projection_attempts(data_dir, act['event_id']) == []
    _sql(data_dir, 'DROP TRIGGER injected_failure')
    for seq, status in enumerate(oc.PROJECTION_STATUSES, 1):
        receipt = oc.record_projection_attempt(data_dir, outward_event_id=act['event_id'], status=status, attempted_at=101, observed_at=102, detail='mechanical only')
        fresh = oc.fetch_projection_attempts(data_dir, act['event_id'])[-1]
        assert receipt == dict(fresh, outward_event_id=act['event_id'])
        assert receipt['attempt_sequence'] == seq
        assert receipt['content_sha256'] == act['content_sha256']
    assert oc.fetch_outward_act(data_dir, act['event_id']) == act


def test_oc1_reply_failure_replay_and_independent_links():
    data_dir = _new_env('oc1_reply_atomic')
    act = _act(data_dir)
    second = _act(data_dir, event_id='second')
    authority = _register_and_bind_human(data_dir, 'oc1replyatomic')
    h1 = _record_human_event(data_dir, authority, 'Reply one', occurred_at=101)
    h2 = _record_human_event(data_dir, authority, 'Reply two', occurred_at=102)
    _sql(data_dir, "CREATE TRIGGER injected_failure AFTER INSERT ON outward_act_reply_links BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    before = _rows(data_dir)
    exc = _raises(oc.OutwardCommunicationError, 'injected failure', lambda: oc.record_reply_link(data_dir, outward_event_id=act['event_id'], human_event_id=h1, linked_at=103))
    assert isinstance(exc.__cause__, sqlite3.IntegrityError)
    assert _rows(data_dir) == before
    assert oc.fetch_reply_link_for_human_event(data_dir, h1) is None
    _sql(data_dir, 'DROP TRIGGER injected_failure')
    first = oc.record_reply_link(data_dir, outward_event_id=act['event_id'], human_event_id=h1, linked_at=103)
    other = oc.record_reply_link(data_dir, outward_event_id=second['event_id'], human_event_id=h2, linked_at=104)
    before = _rows(data_dir)
    for caller_time in (0, 103, 999):
        assert oc.record_reply_link(data_dir, outward_event_id=act['event_id'], human_event_id=h1, linked_at=caller_time) == first == oc.fetch_reply_link_for_human_event(data_dir, h1)
        assert oc.fetch_reply_link_for_human_event(data_dir, h2) == other
        assert _rows(data_dir) == before
    _raises(oc.OutwardCommunicationError, 'different', lambda: oc.record_reply_link(data_dir, outward_event_id=second['event_id'], human_event_id=h1, linked_at=105))
    assert _rows(data_dir) == before


def test_oc1_append_only_boundaries():
    data_dir = _new_env('oc1_append_only')
    act = _act(data_dir)
    oc.record_projection_attempt(data_dir, outward_event_id=act['event_id'], status='attempted', attempted_at=101, observed_at=101)
    authority = _register_and_bind_human(data_dir, 'oc1append')
    h = _record_human_event(data_dir, authority, 'Reply', occurred_at=101)
    oc.record_reply_link(data_dir, outward_event_id=act['event_id'], human_event_id=h, linked_at=102)
    before = _rows(data_dir)
    for table, column in [('events','occurred_at'), ('event_components','component_text'),
                          ('auth_contexts','established_at'), ('outward_projection_attempts','status'),
                          ('outward_act_reply_links','linked_at')]:
        for sql in (f'UPDATE {table} SET {column}={column}', f'DELETE FROM {table}'):
            _raises(sqlite3.IntegrityError, 'append-only', lambda: _sql(data_dir, sql))
            assert _rows(data_dir) == before
    # The accepted session lifecycle permits one ended_at transition; preserve it.
    _sql(data_dir, 'UPDATE sessions SET ended_at=200 WHERE session_id=?', ('oc1-session',))
    _raises(sqlite3.IntegrityError, 'exactly once', lambda: _sql(data_dir, 'UPDATE sessions SET ended_at=201 WHERE session_id=?', ('oc1-session',)))


def test_oc1_concurrent_projection_sequences():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    data_dir = _new_env('oc1_projection_concurrent')
    act = _act(data_dir)
    barrier = Barrier(2)
    def write():
        barrier.wait(timeout=5)
        return oc.record_projection_attempt(data_dir, outward_event_id=act['event_id'], status='attempted', attempted_at=101, observed_at=101)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=10) for f in [pool.submit(write), pool.submit(write)]]
    assert sorted(r['attempt_sequence'] for r in results) == [1, 2]
    assert len({r['projection_attempt_id'] for r in results}) == 2
    assert oc.fetch_outward_act(data_dir, act['event_id']) == act
    assert [r['attempt_sequence'] for r in oc.fetch_projection_attempts(data_dir, act['event_id'])] == [1, 2]
    _raises(sqlite3.IntegrityError, 'UNIQUE', lambda: _sql(data_dir, "INSERT INTO outward_projection_attempts SELECT 'duplicate',outward_event_id,attempt_sequence,content_sha256,status,attempted_at,observed_at,detail,created_at FROM outward_projection_attempts LIMIT 1"))


def test_oc1_concurrent_reply_uniqueness():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    data_dir = _new_env('oc1_reply_concurrent')
    first = _act(data_dir)
    second = _act(data_dir, event_id='other')
    authority = _register_and_bind_human(data_dir, 'oc1concurrent')
    h = _record_human_event(data_dir, authority, 'Reply', occurred_at=101)
    before = _rows(data_dir)
    barrier = Barrier(2)
    def write(event_id):
        barrier.wait(timeout=5)
        try:
            return oc.record_reply_link(data_dir, outward_event_id=event_id, human_event_id=h, linked_at=102)
        except oc.OutwardCommunicationError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=10) for f in [pool.submit(write, first['event_id']), pool.submit(write, second['event_id'])]]
    successes = [r for r in results if isinstance(r, dict)]
    assert len(successes) == 1
    assert len([r for r in results if isinstance(r, oc.OutwardCommunicationError)]) == 1
    assert successes[0] == oc.fetch_reply_link_for_human_event(data_dir, h)
    after = _rows(data_dir)
    assert len(after.pop('outward_act_reply_links')) == 1
    before.pop('outward_act_reply_links')
    assert after == before
    _raises(sqlite3.IntegrityError, 'UNIQUE', lambda: _sql(data_dir, "INSERT INTO outward_act_reply_links VALUES ('duplicate',?,?,102)", (first['event_id'],h)))


def test_oc1_migration_failure_is_atomic():
    from unittest.mock import patch
    data_dir = os.path.join(TEST_DIR, 'oc1_migration_atomic')
    os.makedirs(data_dir)
    path = os.path.join(data_dir, 'anaxi_provenance.db')
    create_provenance_db(path).close()
    before, objects = _rows(data_dir), _objects(data_dir)
    with patch.object(oc0_schema_migration, 'NEW_TABLES_DDL', oc0_schema_migration.NEW_TABLES_DDL + '\nCREATE INDEX injected_failure ON nonexistent_table(no_column);'):
        _raises(sqlite3.OperationalError, 'no such table', lambda: oc0_schema_migration.apply_additive_migration(path))
    assert _objects(data_dir) == objects
    assert _rows(data_dir) == before
    assert oc0_schema_migration.apply_additive_migration(path)['new_tables_created'] == ['outward_act_reply_links', 'outward_projection_attempts']
    assert oc0_schema_migration.apply_additive_migration(path)['new_tables_created'] == []


def test_oc1_unanswered_write_has_only_canonical_effects():
    data_dir = _new_env('oc1_unanswered_delta')
    before = _rows(data_dir)
    _act(data_dir)
    after = _rows(data_dir)
    changed = {table for table in before if before[table] != after[table]}
    assert changed == {'sessions', 'auth_contexts', 'events', 'event_components', 'sqlite_sequence'}
    assert _sql(data_dir, 'SELECT event_type FROM events') == [('clark_outward_act',)]
    assert _sql(data_dir, 'SELECT component_kind FROM event_components ORDER BY sequence') == [('outward_content',), ('outward_recipient_reference',)]
    assert oc.fetch_projection_attempts(data_dir, 'oc1-outward') == []


def test_oc1_transitive_import_boundary():
    import ast
    # The entire OC0 reachable local import graph terminates at this schema.
    expected = {'outward_communication.py': {'hashlib','secrets','sqlite3','time','provenance_schema'},
                'oc0_schema_migration.py': {'sqlite3'},
                'provenance_schema.py': {'hashlib','sqlite3','sys'}}
    for filename, allowed in expected.items():
        with open(os.path.join(ANAXI_FINAL, filename)) as stream:
            tree = ast.parse(stream.read())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {'eval', 'exec', '__import__'}
        assert imports == allowed, (filename, imports)


def test_narrow_hx_real_writers():
    """Reconstructs the previously untracked OC0 NumPy-free H -> X probe."""
    from native_provenance_writer import record_native_waking_turn
    data_dir = _new_env('oc1_hx')
    authority = _register_and_bind_human(data_dir, 'oc1hx')
    h = _record_human_event(data_dir, authority, 'Synthetic human input', occurred_at=100)
    conn = sqlite3.connect(os.path.join(data_dir, 'anaxi_provenance.db'))
    started = conn.execute('SELECT started_at FROM sessions WHERE session_id=?', (authority.session_id,)).fetchone()[0]
    assert human_session_binding.validate_human_input_authority(conn, authority, session_id=authority.session_id)
    conn.close()
    x = oc.generate_outward_event_id()
    receipt = record_native_waking_turn(
        data_dir, event_id=x, staging_id='synthetic-staging-id',
        session_id=authority.session_id, session_started_at=started,
        auth_context_id=oc.generate_outward_event_id(), prompt='Synthetic human input',
        bounded_clause='', clark_prose='Synthetic reply', pipeline_key=_PIPELINE_KEY,
        waking_model_tag='synthetic:test', artifact_pass_ran=False, occurred_at=101,
        human_input_event_id=h,
    )
    assert receipt['reassembled_reply'] == 'Synthetic reply'
    conn = sqlite3.connect(os.path.join(data_dir, 'anaxi_provenance.db'))
    try:
        assert conn.execute("SELECT event_type FROM events WHERE event_id=?", (h,)).fetchone() == ('human_waking_input',)
        assert conn.execute("SELECT event_type FROM events WHERE event_id=?", (x,)).fetchone() == ('waking_turn',)
        assert conn.execute("SELECT component_text FROM event_components WHERE event_id=? AND component_kind='human_input_event_id'", (x,)).fetchall() == [(h,)]
        assert conn.execute("SELECT creator_actor_id FROM event_components WHERE event_id=? AND component_kind='conversational_prose'", (x,)).fetchone() == (derive_stable_id('actor', 'clark'),)
        assert conn.execute("SELECT auth_state FROM auth_contexts WHERE auth_context_id=?", (receipt['auth_context_id'],)).fetchone() == ('unknown',)
        assert conn.execute('SELECT COUNT(*) FROM outward_act_reply_links').fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='clark_outward_act'").fetchone() == (0,)
    finally:
        conn.close()


def _historical_migration(head):
    """Use committed historical code, never an approximated schema fixture."""
    import types
    source = subprocess.run(['git','show',head + ':anaxi_final/oc0_schema_migration.py'], cwd=ANAXI_FINAL, capture_output=True, text=True, check=True).stdout
    module = types.ModuleType('historical_oc0_' + head)
    exec(compile(source, head + ':oc0_schema_migration.py', 'exec'), module.__dict__)
    return module


def test_oc1_historical_migration_matrix():
    from unittest.mock import patch
    versions = [None, 'a3a853c3d1ff2558058e0fbf62e4822361e2a375',
                '82543a319649eac81dae680585b57a08a13205e6',
                '6bf5a679296b56ab1c4690a423d456aa2d6eef69']
    for index, version in enumerate(versions):
        data_dir = os.path.join(TEST_DIR, 'oc1_matrix_' + str(index))
        os.makedirs(data_dir)
        db_path = os.path.join(data_dir, 'anaxi_provenance.db')
        create_provenance_db(db_path).close()
        hir1_schema_migration.apply_additive_migration(db_path)
        conn = sqlite3.connect(db_path)
        seed_reference_data(conn, build_pipeline_map(_MANIFEST), 100)
        conn.close()
        if version is not None:
            _historical_migration(version).apply_additive_migration(db_path)
        act = _act(data_dir)
        authority = _register_and_bind_human(data_dir, 'oc1matrix' + str(index))
        h = _record_human_event(data_dir, authority, 'Lawful reply', occurred_at=101)
        early = _record_human_event(data_dir, authority, 'Earlier input', occurred_at=99)
        if version is not None:
            oc.record_projection_attempt(data_dir, outward_event_id=act['event_id'], status='not_established', attempted_at=101, observed_at=102)
            oc.record_reply_link(data_dir, outward_event_id=act['event_id'], human_event_id=h, linked_at=102)
            if index == 1:
                # Historical permitted row must survive without retroactive rewrite.
                _sql(data_dir, "INSERT INTO outward_act_reply_links VALUES ('historical',?,?,98)", (act['event_id'],early))
        before, objects = _rows(data_dir), _objects(data_dir)
        with patch.object(oc0_schema_migration, 'NEW_TABLES_DDL', oc0_schema_migration.NEW_TABLES_DDL + '\nCREATE INDEX injected_failure ON missing_table(missing_column);'):
            _raises(sqlite3.OperationalError, 'no such table', lambda: oc0_schema_migration.apply_additive_migration(db_path))
        assert _rows(data_dir) == before
        assert _objects(data_dir) == objects
        receipt = oc0_schema_migration.apply_additive_migration(db_path)
        assert receipt['new_tables_created'] == ([] if version else ['outward_act_reply_links','outward_projection_attempts'])
        after = _rows(data_dir)
        assert {table: after[table] for table in before} == before
        assert set(objects) <= set(_objects(data_dir)), 'No historical object definition may be overwritten'
        state = oc0_schema_migration.verify_migration_state(db_path)
        assert all(state.values())
        if index == 1:
            assert oc.record_reply_link(data_dir, outward_event_id=act['event_id'], human_event_id=early, linked_at=999)['linked_at'] == 98
        post, post_objects = _rows(data_dir), _objects(data_dir)
        assert oc0_schema_migration.apply_additive_migration(db_path)['new_tables_created'] == []
        assert _rows(data_dir) == post
        assert _objects(data_dir) == post_objects
        for human, linked_at, diagnostic in [(early,102,'canonically earlier'), (h,100,'must not predate')]:
            _raises(sqlite3.IntegrityError, diagnostic, lambda: _sql(data_dir, "INSERT INTO outward_act_reply_links VALUES ('violation',?,?,?)", (act['event_id'],human,linked_at)))
            assert _rows(data_dir) == post
        equal = _record_human_event(data_dir, authority, 'Equal second', occurred_at=100)
        lawful = oc.record_reply_link(data_dir, outward_event_id=act['event_id'], human_event_id=equal, linked_at=100)
        assert lawful == oc.fetch_reply_link_for_human_event(data_dir, equal)


def test_oc1_migration_object_evolution_audit():
    versions = ['a3a853c3d1ff2558058e0fbf62e4822361e2a375',
                '82543a319649eac81dae680585b57a08a13205e6',
                '6bf5a679296b56ab1c4690a423d456aa2d6eef69']
    snapshots = []
    for index, version in enumerate(versions):
        data_dir = os.path.join(TEST_DIR, 'oc1_objects_' + str(index))
        os.makedirs(data_dir)
        db_path = os.path.join(data_dir, 'anaxi_provenance.db')
        create_provenance_db(db_path).close()
        _historical_migration(version).apply_additive_migration(db_path)
        snapshots.append({r[1]:r for r in _objects(data_dir) if r[2] in ('outward_projection_attempts','outward_act_reply_links')})
    original, correction2, accepted = snapshots
    assert original.keys() == correction2.keys()
    assert {name for name in original if original[name] != correction2[name]} == {'trg_outward_reply_links_refs_exist'}
    assert accepted.keys() - original.keys() == {'trg_outward_reply_links_chronology'}
    assert all(accepted[name] == row for name,row in original.items())
    data_dir = _new_env('oc1_current_objects')
    current = {r[1]:r for r in _objects(data_dir) if r[2] in ('outward_projection_attempts','outward_act_reply_links')}
    assert current == accepted, 'OC1 changes transaction application, not accepted schema definitions'


def test_oc1_invalid_text_inputs_leave_no_state():
    data_dir = _new_env('oc1_invalid_text')
    before = _rows(data_dir)
    for field in ('event_id','session_id','content','recipient_reference'):
        for invalid in (None, '', '  ', 42, b'bytes'):
            _raises(oc.OutwardCommunicationError, field, lambda: _act(data_dir, **{field:invalid}))
            assert _rows(data_dir) == before
    for invalid in (None, '', '  ', 42, b'bytes'):
        for fetch, field in [(oc.fetch_outward_act,'event_id'), (oc.fetch_projection_attempts,'outward_event_id'), (oc.fetch_reply_link_for_human_event,'human_event_id')]:
            _raises(oc.OutwardCommunicationError, field, lambda: fetch(data_dir,invalid))
            assert _rows(data_dir) == before
    act = _act(data_dir)
    before = _rows(data_dir)
    _raises(oc.OutwardCommunicationError, 'detail', lambda: oc.record_projection_attempt(data_dir, outward_event_id=act['event_id'], status='attempted', attempted_at=100, observed_at=100, detail=42))
    assert _rows(data_dir) == before


def test_oc1_locked_projection_fails_without_false_receipt():
    from unittest.mock import patch
    data_dir = _new_env('oc1_locked')
    act = _act(data_dir)
    before = _rows(data_dir)
    connect = sqlite3.connect
    holder = connect(os.path.join(data_dir,'anaxi_provenance.db'))
    holder.execute('BEGIN IMMEDIATE')
    def no_wait(*args, **kwargs):
        return connect(*args, **dict(kwargs, timeout=0))
    try:
        with patch.object(oc.sqlite3, 'connect', no_wait):
            _raises(sqlite3.OperationalError, 'locked', lambda: oc.record_projection_attempt(data_dir,outward_event_id=act['event_id'],status='attempted',attempted_at=100,observed_at=100))
    finally:
        holder.rollback()
        holder.close()
    assert _rows(data_dir) == before
    assert oc.fetch_projection_attempts(data_dir, act['event_id']) == []


def test_oc1_direct_schema_constraints_and_error_distinctions():
    data_dir = _new_env('oc1_direct_constraints')
    act = _act(data_dir)
    authority = _register_and_bind_human(data_dir,'oc1direct')
    h = _record_human_event(data_dir,authority,'Reply',occurred_at=101)
    before = _rows(data_dir)
    _raises(oc.OutwardCommunicationError, 'event_type', lambda: oc.fetch_outward_act(data_dir,h))
    for outward, human, diagnostic in [('missing',h,'outward_event_id'), (h,h,'outward_event_id'),
                                        (act['event_id'],'missing','human_event_id'), (act['event_id'],act['event_id'],'human_event_id')]:
        _raises(sqlite3.IntegrityError, diagnostic, lambda: _sql(data_dir,"INSERT INTO outward_act_reply_links VALUES ('invalid',?,?,102)",(outward,human)))
        assert _rows(data_dir) == before
    for outward, seq, status, diagnostic in [('missing',1,'attempted','existing clark_outward_act'),
                                             (h,1,'attempted','existing clark_outward_act'),
                                             (act['event_id'],0,'attempted','CHECK'),
                                             (act['event_id'],1,'read','CHECK')]:
        _raises(sqlite3.IntegrityError,diagnostic,lambda: _sql(data_dir,"INSERT INTO outward_projection_attempts VALUES ('invalid',?,?,?, ?,101,102,NULL,102)",(outward,seq,act['content_sha256'],status)))
        assert _rows(data_dir) == before


ALL_TESTS = [
    test_oc1_historical_migration_matrix,
    test_oc1_migration_object_evolution_audit,
    test_oc1_invalid_text_inputs_leave_no_state,
    test_oc1_locked_projection_fails_without_false_receipt,
    test_oc1_direct_schema_constraints_and_error_distinctions,

    test_oc1_canonical_failure_rolls_back_all_rows,
    test_oc1_canonical_replay_checks_session_start,
    test_oc1_canonical_conflicts_preserve_all_rows,
    test_oc1_timestamp_inputs_fail_closed,
    test_oc1_projection_failure_and_receipt_truthfulness,
    test_oc1_reply_failure_replay_and_independent_links,
    test_oc1_append_only_boundaries,
    test_oc1_concurrent_projection_sequences,
    test_oc1_concurrent_reply_uniqueness,
    test_oc1_migration_failure_is_atomic,
    test_oc1_unanswered_write_has_only_canonical_effects,
    test_oc1_transitive_import_boundary,
    test_narrow_hx_real_writers,

    test_outward_act_persists_without_any_preceding_human_event,
    test_outward_act_survives_a_fresh_read,
    test_nonexistent_outward_act_fetch_returns_none,
    test_projection_attempt_against_nonexistent_event_is_rejected,
    test_projection_attempt_rejects_invalid_status,
    test_failed_projection_leaves_canonical_act_intact,
    test_retry_does_not_duplicate_canonical_act,
    test_retry_with_different_content_is_refused_not_silently_merged,
    test_repeated_projection_attempts_are_mechanically_distinguishable,
    test_projection_succeeded_status_never_implies_human_attention,
    test_genuine_human_reply_can_reference_outward_act,
    test_relinking_same_pair_is_idempotent,
    test_replay_with_different_linked_at_reports_canonical_chronology,
    test_reply_link_rejects_canonically_earlier_human_event,
    test_reply_link_allows_canonically_later_human_event,
    test_reply_link_allows_equal_canonical_timestamps,
    test_reply_link_rejects_linked_at_predating_either_referenced_event,
    test_migration_installs_chronology_trigger_on_fresh_database,
    test_migration_upgrade_path_from_pre_correction2_schema,
    test_unrelated_human_event_is_not_automatically_linked,
    test_unanswered_outward_act_creates_no_synthetic_h_or_obligation,
    test_reply_link_rejects_nonexistent_outward_event,
    test_reply_link_rejects_nonexistent_human_event,
    test_reply_link_rejects_wrong_event_type_on_both_sides,
    test_empty_content_is_refused,
    test_empty_recipient_reference_is_refused,
    test_module_never_imports_private_space,
]


def main():
    passed, failed = 0, 0
    failures = []
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            tb = traceback.format_exc()
            failures.append((t.__name__, str(exc), tb))
            print(f"FAIL {t.__name__}: {exc}")

    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    print(f"test_dir={TEST_DIR}")
    if failures:
        print()
        print("=== FAILURE DETAILS ===")
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
