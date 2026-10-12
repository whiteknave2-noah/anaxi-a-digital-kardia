"""BOUNDARY INSPECTOR v1 permanent evaluation-engine suite.

Zero Ollama calls, zero inference, zero external network, zero real
Workspace/Private Space access. Every DB-backed test runs against a
FRESH SYNTHETIC anaxi_provenance.db built by
provenance_schema.create_provenance_db() (+ real additive migrations)
in a disposable temp directory -- never the live file, never
production data.

Capability substrate: this sandbox intentionally has none of pypdf /
PIL / numpy / soundfile / scipy installed, so the REAL closed
capability substrate (workspace_capability.py / workspace_audio.py)
cannot be imported natively here. To exercise the REAL descriptors and
closed action domain (not a fabricated mirror), the handful of pure
third-party import-time names are stubbed in sys.modules before the
substrate's first lazy import -- the real constants and real
check_permission() logic then run unmodified. The ImportError ->
OBSERVATION_INCOMPLETE path is separately verified in a clean
subprocess that carries NO stubs.

Run from repository root:
    python3 -B anaxi_final/test_boundary_inspector.py
(also pytest-compatible: bare def test_*() functions).
"""
import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import types

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)


def _install_capability_substrate_stubs():
    """Inert import-time names only -- never callable behavior. Lets
    the REAL workspace_capability / workspace_audio modules resolve
    their third-party imports so their genuine constants and genuine
    check_permission() logic run in this suite."""
    for name in ("pypdf", "numpy", "soundfile", "PIL.Image", "scipy.fft", "scipy.signal"):
        try:
            importlib.import_module(name)
        except ImportError:
            sys.modules.setdefault(name, types.ModuleType(name))
    pil = sys.modules.setdefault("PIL", types.ModuleType("PIL"))
    pil.Image = sys.modules["PIL.Image"]
    scipy = sys.modules.setdefault("scipy", types.ModuleType("scipy"))
    scipy.fft = sys.modules["scipy.fft"]
    scipy_signal = sys.modules["scipy.signal"]
    if not hasattr(scipy_signal, "find_peaks"):
        scipy_signal.find_peaks = lambda *a, **k: ((), {})
    scipy.signal = scipy_signal


_install_capability_substrate_stubs()

import boundary_inspector as bi
import boundary_inspector_schema_migration
import boundary_rationale_registry as brr
import context_budget
import conversation_direction as cd
import conversation_direction_trace as cdt
import migrate_historical_data
import native_provenance_writer
import oc0_schema_migration
import outward_communication
import provenance_schema
import slp2_schema_migration
import wtr0_schema_migration

import workspace_capability

TEST_DIR = tempfile.mkdtemp(prefix="boundary_inspector_test_")
_MANIFEST = {"pipelines": {
    "llama": {"routing_constant_value": "nate"},
    "claude": {"routing_constant_value": "nate"},
}}
_COUNTER = [0]
PIPELINE_KEY = "anaxi_orchestration_lineage_a"


def _next_id(prefix="sess"):
    _COUNTER[0] += 1
    return f"{prefix}-{_COUNTER[0]}-{int(time.time() * 1000)}"


def _now():
    return int(time.time())


def _new_env_unseeded(name):
    """Fresh synthetic data_dir with ONLY the canonical schema -- no
    reference data, no additive migrations."""
    data_dir = os.path.join(TEST_DIR, name)
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    provenance_schema.create_provenance_db(db_path).close()
    return data_dir


def _new_env_base(name):
    """Fresh synthetic data_dir + full canonical schema + reference
    data (pipelines + clark/host actors) seeded. No additive
    migrations -- the structural-negative fixture."""
    data_dir = os.path.join(TEST_DIR, name)
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    provenance_schema.create_provenance_db(db_path).close()
    pipeline_map = migrate_historical_data.build_pipeline_map(_MANIFEST)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    migrate_historical_data.seed_reference_data(conn, pipeline_map, _now())
    conn.close()
    return data_dir


def _new_env(name):
    """Structural-positive fixture: full canonical schema + reference
    data + the three real additive migrations (oc0 / slp2 / wtr0)."""
    data_dir = _new_env_base(name)
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    oc0_schema_migration.apply_additive_migration(db_path)
    slp2_schema_migration.apply_additive_migration(db_path)
    wtr0_schema_migration.apply_additive_migration(db_path)
    return data_dir


def _table_rows(data_dir, table):
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


def _record_trace(trace_path, session_id, pass1_status="ok", pass2_status="ok"):
    cdt.record_trace(
        session_id=session_id, raw_act={"act": "develop_current", "thread": ""},
        validated_act={"act": "develop_current", "direction_request": "none"},
        thread="", pass1_status=pass1_status,
        working_set_before=[], working_set_after=[],
        direction_owner_before=cd.DIRECTION_UNKNOWN, direction_owner_after=cd.DIRECTION_UNKNOWN,
        direction_request="none", relinquish_direction=False, pass2_status=pass2_status,
        trace_path=trace_path,
    )


def _outward_act(data_dir, session_id, session_started_at, content="a boundary-inspector synthetic outward act"):
    event_id = outward_communication.generate_outward_event_id()
    outward_communication.record_clark_outward_act(
        data_dir, event_id=event_id, session_id=session_id,
        session_started_at=session_started_at, content=content,
        recipient_reference="test-operator-synthetic-reference", occurred_at=_now(),
    )
    return event_id


def _wake_turn(data_dir, session_id, session_started_at, occurred_at, tag,
               delivered_boundary_query_event_id=None):
    """A GENUINE, canonically-committed waking turn in the same session
    (same seeded pipeline), via the real native provenance writer -- the
    only legitimate basis for an acknowledged boundary delivery in v1."""
    staging_path = os.path.join(data_dir, f"native_turn_staging_{tag}.jsonl")
    recorded = native_provenance_writer.stage_and_record_native_waking_turn(
        data_dir, staging_path,
        session_id=session_id, session_started_at=session_started_at,
        user_id="test-operator", prompt="a test prompt for a genuine waking turn",
        bounded_clause="", clark_prose="a test reply",
        kardia={}, controls={}, waking_model_tag="inert-test-model",
        pipeline_key=PIPELINE_KEY, artifact_pass_ran=False, occurred_at=occurred_at,
        delivered_boundary_query_event_id=delivered_boundary_query_event_id,
    )
    if "event_id" not in recorded:
        raise AssertionError(f"native waking turn did not return an event_id: {recorded!r}")
    return recorded["event_id"]


def _insert_raw_event(data_dir, *, session_id, event_type, pipeline_key, occurred_at):
    """A canonically-shaped event of an arbitrary type in an EXISTING
    session, for negative-path fixtures (wrong type / foreign pipeline).
    Not a waking turn -- used only to prove such events are refused."""
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        pipeline_id = conn.execute(
            "SELECT pipeline_id FROM pipelines WHERE pipeline_key = ?", (pipeline_key,)).fetchone()[0]
        event_id = "evt-" + native_provenance_writer.generate_native_ulid()
        auth_context_id = "auth-" + native_provenance_writer.generate_native_ulid()
        conn.execute(
            "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
            "VALUES (?, ?, 'unknown', ?)", (auth_context_id, session_id, occurred_at))
        conn.execute(
            "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
            "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
            "VALUES (?, ?, ?, 'known', NULL, ?, NULL, ?, ?)",
            (event_id, event_type, pipeline_id, auth_context_id, occurred_at, occurred_at))
        conn.commit()
    finally:
        conn.close()
    return event_id


def _carry_and_deliver(data_dir, query_event_id, waking_turn_event_id, occurred_at):
    """Acknowledge the carriage already recorded atomically by the real
    canonical waking-turn writer."""
    return bi.record_boundary_inspection_delivered(
        data_dir, query_event_id=query_event_id,
        waking_turn_event_id=waking_turn_event_id, occurred_at=occurred_at)


# ---------------------------------------------------------- architecture ---


def test_private_space_zero_runtime_import_dependency():
    before = set(sys.modules)
    import boundary_inspector
    assert "workspace_private" not in (set(sys.modules) - before)
    assert boundary_inspector.PRIVATE_WORKSPACE_IMPORT_DEPENDENCY is None
    with open(boundary_inspector.__file__, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.lstrip()
            assert not stripped.startswith("import workspace_private"), line
            assert not stripped.startswith("from workspace_private"), line


def test_registry_locks_capability_descriptor_boundary_ids():
    for cls in workspace_capability.RESOURCE_CLASSES:
        descriptor = workspace_capability.get_capability_descriptor(cls)
        assert descriptor is not None
        assert descriptor["boundary_id"] in brr.BOUNDARY_DEFINITIONS, descriptor["boundary_id"]
    for action in sorted(workspace_capability.ALL_KNOWN_ACTIONS):
        _, boundary_id, _ = workspace_capability.check_permission("library", action)
        assert boundary_id in (None, "workspace.library.read_only")


# ------------------------------------------------------------ validation ---


def test_validate_boundary_query_accepts_every_closed_target():
    for kind in cd.BOUNDARY_INQUIRY_QUERY_KINDS:
        targets = {
            bi.QUERY_KIND_CAPABILITY: cd.BOUNDARY_CAPABILITY_TARGETS,
            bi.QUERY_KIND_BOUNDARY_ID: cd.BOUNDARY_BOUNDARY_ID_TARGETS,
            bi.QUERY_KIND_HOST_RULE: cd.BOUNDARY_HOST_RULE_TARGETS,
            bi.QUERY_KIND_RECENT_REJECTED_ACTION: cd.BOUNDARY_RECENT_REJECTED_ACTION_TARGETS,
        }[kind]
        for target in targets:
            query = {"query_kind": kind, "query_target": target}
            assert bi.validate_boundary_query(query) == query
    outward = {"query_kind": bi.QUERY_KIND_RECENT_REJECTED_ACTION,
               "query_target": f"outward.{outward_communication.generate_outward_event_id()}"}
    assert bi.validate_boundary_query(outward) == outward


def test_validate_boundary_query_rejects_malformed():
    for query in (None, "capability:library", {}, {"query_kind": "capability"},
                  {"query_kind": "capability", "query_target": "library", "extra": 1},
                  {"query_kind": "nonsense", "query_target": "x"},
                  {"query_kind": "capability", "query_target": "nonsense"},
                  {"query_kind": "boundary_id", "query_target": "nonsense"},
                  {"query_kind": "host_rule", "query_target": "nonsense"},
                  {"query_kind": "recent_rejected_action", "query_target": "nonsense"},
                  {"query_kind": "recent_rejected_action", "query_target": "budget.most_recent.extra"},
                  {"query_kind": "recent_rejected_action", "query_target": "outward.not-a-ulid"},
                  {"query_kind": "recent_rejected_action", "query_target": ""}):
        try:
            bi.validate_boundary_query(query)
        except bi.BoundaryInspectionError:
            continue
        raise AssertionError(f"accepted malformed query: {query!r}")


# -------------------------------------------------------------- capability ---


def test_capability_four_resource_classes_positively_governed():
    for target in workspace_capability.RESOURCE_CLASSES:
        env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "capability-resource"))
        result = bi.evaluate_boundary_query(env, {"query_kind": "capability", "query_target": target})
        descriptor = workspace_capability.get_capability_descriptor(target)
        assert result["classification"] == bi.CLASS_ACTUAL_HOST_BOUNDARY, (target, result["classification"])
        assert result["evidence_complete"] is True
        assert len(result["boundaries"]) == 1
        boundary = result["boundaries"][0]
        assert boundary["boundary_id"] == descriptor["boundary_id"]
        assert boundary["boundary_type"] == "workspace_capability"
        assert boundary["architectural_rationale"] != "not recorded"
        assert all(boundary[k] for k in ("operational_why", "what_changes_it"))
        assert bi.validate_result(result) is result


def test_capability_outbound_messaging_positive_non_existence():
    env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "capability-outbound"))
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "capability", "query_target": "outbound_messaging"})
    assert result["classification"] == bi.CLASS_CAPABILITY_NOT_PRESENT
    assert result["evidence_complete"] is True
    assert result["boundaries"] == []
    assert len(result["checks"]) == 3


def test_capability_substrate_importable_in_supported_environment():
    """A clean production-shaped subprocess loads the installed capability
    substrate and returns complete host-boundary evidence."""
    data_dir = os.path.join(TEST_DIR, "capability-incomplete-subproc")
    os.makedirs(data_dir, exist_ok=True)
    code = (
        "import sys; sys.path.insert(0, %r); "
        "import boundary_inspector as bi; "
        "env = bi.BoundaryInspectionEnv(%r); "
        "r = bi.evaluate_boundary_query(env, "
        "{'query_kind': 'capability', 'query_target': 'library'}); "
        "print(r['classification']); print(r['evidence_complete']); "
        "print(r['checks'][0]['evidence_source_id'])"
    ) % (ANAXI_FINAL, data_dir)
    proc = subprocess.run([sys.executable, "-B", "-c", code],
                          capture_output=True, text=True, cwd=os.path.dirname(ANAXI_FINAL))
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[0] == bi.CLASS_ACTUAL_HOST_BOUNDARY
    assert lines[1] == "True"
    assert lines[2] == "workspace.capability.descriptors"


def test_capability_unknown_class_fails_validation():
    env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "capability-unknown"))
    try:
        bi.evaluate_boundary_query(env, {"query_kind": "capability", "query_target": "appdata"})
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("unknown capability target accepted")


# ---------------------------------------------------------------- host_rule ---


def test_host_rule_all_targets_no_boundary_found():
    """Ground truth (traced against conversation_direction.py /
    interaction_mode.py): the closed act schema has no reason field,
    no allowed act is ever rejected for direction-ownership reasons,
    apply_conversation_act() never branches on direction_owner, and
    neither interaction mode suppresses generation. All four
    participation.* host_rule targets must therefore resolve as a
    genuine, complete NO_HOST_BOUNDARY_FOUND -- never a fabricated
    ACTUAL_HOST_BOUNDARY for a rule that does not exist."""
    for target in cd.BOUNDARY_HOST_RULE_TARGETS:
        env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "host-rule"))
        result = bi.evaluate_boundary_query(env, {"query_kind": "host_rule", "query_target": target})
        assert result["classification"] == bi.CLASS_NO_HOST_BOUNDARY_FOUND, (target, result["classification"])
        assert result["evidence_complete"] is True
        assert result["boundaries"] == []
        assert len(result["checks"]) == 1
        assert result["checks"][0]["result"] == bi.CHECK_BOUNDARY_NOT_ESTABLISHED
        assert bi.validate_result(result) is result


def test_host_rule_requires_reason_established_when_field_present():
    """Proves the ACTUAL_HOST_BOUNDARY branch is real, not dead code:
    if the closed act schema ever gained a reason-shaped field, the
    query must positively establish the boundary, not silently miss it."""
    env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "host-rule-reason-present"))
    original = bi._REASON_FIELD_VOCABULARY
    try:
        bi._REASON_FIELD_VOCABULARY = frozenset({"thread"})  # "thread" is a real allowed field
        result = bi.evaluate_boundary_query(
            env, {"query_kind": "host_rule", "query_target": "participation.requires_reason"})
    finally:
        bi._REASON_FIELD_VOCABULARY = original
    assert result["classification"] == bi.CLASS_ACTUAL_HOST_BOUNDARY
    assert result["evidence_complete"] is True
    boundary = result["boundaries"][0]
    assert boundary["boundary_id"] == "host_rule.participation.requires_reason"
    assert boundary["boundary_type"] == "conversation_participation_rule"


def test_host_rule_gated_by_direction_owner_established_when_act_varies():
    """Proves gated_by_direction_owner's ACTUAL_HOST_BOUNDARY branch is
    reachable: if apply_conversation_act() ever produced an owner-
    dependent outcome, the query must catch it."""
    env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "host-rule-owner-gated"))
    original = cd.apply_conversation_act
    try:
        def gated(working_set, validated_act):
            new_ws = original(working_set, validated_act)
            if working_set.get("direction_owner") == cd.DIRECTION_HUMAN:
                new_ws["active_thread"] = "owner-dependent-injected-value"
            return new_ws
        cd.apply_conversation_act = gated
        result = bi.evaluate_boundary_query(
            env, {"query_kind": "host_rule", "query_target": "participation.gated_by_direction_owner"})
    finally:
        cd.apply_conversation_act = original
    assert result["classification"] == bi.CLASS_ACTUAL_HOST_BOUNDARY
    assert result["boundaries"][0]["boundary_id"] == "host_rule.participation.gated_by_direction_owner"


def test_host_rule_conflicting_checker_yields_cause_not_established():
    env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "host-rule-conflict"))
    original = bi._check_requires_permission_to_speak
    try:
        bi._check_requires_permission_to_speak = lambda observed_at: (
            bi._check("host_rule.validator.validate_pass1_closed_domain",
                      bi.CHECK_CONFLICTING,
                      "host_rule.participation.requires_permission_to_speak", observed_at),
            "injected contradiction",
        )
        result = bi.evaluate_boundary_query(
            env, {"query_kind": "host_rule", "query_target": "participation.requires_permission_to_speak"})
    finally:
        bi._check_requires_permission_to_speak = original
    assert result["classification"] == bi.CLASS_CAUSE_NOT_ESTABLISHED
    assert result["evidence_complete"] is True
    assert result["boundaries"] == []


# -------------------------------------------------------------- boundary_id ---


def test_boundary_id_private_rule_actual():
    data_dir = _new_env_base("boundary_id_private_actual")
    env = bi.BoundaryInspectionEnv(data_dir)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "boundary_id", "query_target": "private_space.public_rule"})
    assert result["classification"] == bi.CLASS_ACTUAL_HOST_BOUNDARY
    assert result["evidence_complete"] is True
    boundary = result["boundaries"][0]
    assert boundary["boundary_id"] == "private_space.public_rule"
    assert boundary["boundary_type"] == "redacted_private_material_at_host_boundary"
    assert len(result["checks"]) == 2


def test_boundary_id_private_rule_conflicting_with_dependency():
    data_dir = _new_env_base("boundary_id_private_conflict")
    env = bi.BoundaryInspectionEnv(data_dir)
    original = bi.PRIVATE_WORKSPACE_IMPORT_DEPENDENCY
    try:
        bi.PRIVATE_WORKSPACE_IMPORT_DEPENDENCY = object()
        result = bi.evaluate_boundary_query(
            env, {"query_kind": "boundary_id", "query_target": "private_space.public_rule"})
    finally:
        bi.PRIVATE_WORKSPACE_IMPORT_DEPENDENCY = original
    assert result["classification"] == bi.CLASS_CAUSE_NOT_ESTABLISHED
    assert result["evidence_complete"] is True
    assert result["boundaries"] == []


def test_boundary_id_private_rule_incomplete_without_declaration():
    data_dir = _new_env_base("boundary_id_private_incomplete")
    env = bi.BoundaryInspectionEnv(data_dir)
    original = brr.PRIVATE_SPACE_PUBLIC_RULE
    try:
        brr.PRIVATE_SPACE_PUBLIC_RULE = "   "
        result = bi.evaluate_boundary_query(
            env, {"query_kind": "boundary_id", "query_target": "private_space.public_rule"})
    finally:
        brr.PRIVATE_SPACE_PUBLIC_RULE = original
    assert result["classification"] == bi.CLASS_OBSERVATION_INCOMPLETE
    assert result["evidence_complete"] is False


def test_boundary_id_missing_db_incomplete():
    data_dir = os.path.join(TEST_DIR, "boundary_id_no_db")
    os.makedirs(data_dir, exist_ok=True)
    env = bi.BoundaryInspectionEnv(data_dir)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "boundary_id", "query_target": "private_space.public_rule"})
    assert result["classification"] == bi.CLASS_OBSERVATION_INCOMPLETE
    assert result["evidence_complete"] is False


def test_boundary_id_sleep_and_wtr0_ceilings_present():
    data_dir = _new_env("boundary_id_structural_present")
    env = bi.BoundaryInspectionEnv(data_dir)
    expected = {
        "sleep.one_unresolved_root": "sleep.open_request_uniqueness",
        "sleep.authorize_execute_decoupling": "sleep.execution_requires_authorization",
        "wtr0.recovery_ceiling": "wtr0.recovery_uniqueness",
    }
    for target, evidence_source in expected.items():
        result = bi.evaluate_boundary_query(env, {"query_kind": "boundary_id", "query_target": target})
        assert result["classification"] == bi.CLASS_ACTUAL_HOST_BOUNDARY, (target, result["classification"])
        assert result["evidence_complete"] is True
        boundary = result["boundaries"][0]
        assert boundary["boundary_id"] == target
        assert boundary["architectural_rationale"] == "not recorded"
        assert evidence_source in boundary["evidence_source_ids"]


def test_boundary_id_sleep_and_wtr0_ceilings_absent():
    data_dir = _new_env_base("boundary_id_structural_absent")
    env = bi.BoundaryInspectionEnv(data_dir)
    for target in ("sleep.one_unresolved_root", "sleep.authorize_execute_decoupling",
                   "wtr0.recovery_ceiling"):
        result = bi.evaluate_boundary_query(env, {"query_kind": "boundary_id", "query_target": target})
        assert result["classification"] == bi.CLASS_NO_HOST_BOUNDARY_FOUND, (target, result["classification"])
        assert result["evidence_complete"] is True
        assert result["boundaries"] == []


# ------------------------------------------------- recent_rejected_action: budget


def test_budget_requires_session_id():
    env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "budget-no-session"))
    try:
        bi.evaluate_boundary_query(
            env, {"query_kind": "recent_rejected_action", "query_target": "budget.most_recent"})
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("session-less budget query accepted")


def test_budget_no_trace_incomplete():
    env = bi.BoundaryInspectionEnv(
        os.path.join(TEST_DIR, "budget-no-trace"),
        trace_path=os.path.join(TEST_DIR, "budget-no-trace", "missing.jsonl"))
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "recent_rejected_action", "query_target": "budget.most_recent"},
        session_id=_next_id())
    assert result["classification"] == bi.CLASS_OBSERVATION_INCOMPLETE
    assert result["evidence_complete"] is False


def test_budget_trace_unreadable_incomplete():
    trace_path = os.path.join(TEST_DIR, "budget-bad-trace.jsonl")
    with open(trace_path, "w", encoding="utf-8") as f:
        f.write("{not valid json\n")
    env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "budget-bad"), trace_path=trace_path)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "recent_rejected_action", "query_target": "budget.most_recent"},
        session_id=_next_id())
    assert result["classification"] == bi.CLASS_OBSERVATION_INCOMPLETE
    assert result["evidence_complete"] is False


def test_budget_other_session_records_incomplete():
    trace_path = os.path.join(TEST_DIR, "budget-other-session.jsonl")
    _record_trace(trace_path, "session-other-page")
    env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "budget-other"), trace_path=trace_path)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "recent_rejected_action", "query_target": "budget.most_recent"},
        session_id=_next_id())
    assert result["classification"] == bi.CLASS_OBSERVATION_INCOMPLETE
    assert result["evidence_complete"] is False


def test_budget_rejection_recorded_positively():
    trace_path = os.path.join(TEST_DIR, "budget-rejected.jsonl")
    session_id = _next_id("budget")
    _record_trace(trace_path, session_id, pass1_status="BUDGET_EXCEEDED")
    env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "budget-pos"), trace_path=trace_path)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "recent_rejected_action", "query_target": "budget.most_recent"},
        session_id=session_id)
    assert result["classification"] == bi.CLASS_ACTUAL_HOST_BOUNDARY
    assert result["evidence_complete"] is True
    boundary = result["boundaries"][0]
    assert boundary["boundary_id"] == "budget.exhaustion"
    assert boundary["boundary_type"] == "resource_budget"


def test_budget_no_rejection_no_boundary():
    trace_path = os.path.join(TEST_DIR, "budget-no-rejection.jsonl")
    session_id = _next_id("budget")
    _record_trace(trace_path, session_id, pass1_status="ok")
    env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "budget-neg"), trace_path=trace_path)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "recent_rejected_action", "query_target": "budget.most_recent"},
        session_id=session_id)
    assert result["classification"] == bi.CLASS_NO_HOST_BOUNDARY_FOUND
    assert result["evidence_complete"] is True
    assert result["boundaries"] == []


# ------------------------------------------- recent_rejected_action: outward ---


def _outward_result(data_dir, session_id, status, attempted_at=None):
    event_id = _outward_act(data_dir, session_id, _now())
    if status is not None:
        outward_communication.record_projection_attempt(
            data_dir, outward_event_id=event_id, status=status,
            attempted_at=attempted_at or _now(), observed_at=_now(),
        )
    return event_id


def test_outward_requires_session_id():
    data_dir = _new_env("outward-no-session")
    session_id = _next_id("out")
    event_id = _outward_result(data_dir, session_id, "failed")
    env = bi.BoundaryInspectionEnv(data_dir)
    try:
        bi.evaluate_boundary_query(
            env, {"query_kind": "recent_rejected_action",
                  "query_target": f"outward.{event_id}"})
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("session-less outward query accepted")


def test_outward_failed_projection_actual_boundary():
    data_dir = _new_env("outward-failed")
    session_id = _next_id("out")
    event_id = _outward_result(data_dir, session_id, "failed")
    env = bi.BoundaryInspectionEnv(data_dir)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "recent_rejected_action", "query_target": f"outward.{event_id}"},
        session_id=session_id)
    assert result["classification"] == bi.CLASS_ACTUAL_HOST_BOUNDARY
    assert result["evidence_complete"] is True
    boundary = result["boundaries"][0]
    assert boundary["boundary_id"] == "outward.projection_failure"
    assert boundary["boundary_type"] == "projection_failure_boundary"


def test_outward_succeeded_attempt_no_boundary():
    data_dir = _new_env("outward-succeeded")
    session_id = _next_id("out")
    event_id = _outward_result(data_dir, session_id, "succeeded")
    env = bi.BoundaryInspectionEnv(data_dir)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "recent_rejected_action", "query_target": f"outward.{event_id}"},
        session_id=session_id)
    assert result["classification"] == bi.CLASS_NO_HOST_BOUNDARY_FOUND
    assert result["evidence_complete"] is True
    assert result["boundaries"] == []
    assert "never establishes delivery" in result["scope_note"]


def test_outward_not_established_attempt_no_boundary():
    data_dir = _new_env("outward-not-established")
    session_id = _next_id("out")
    event_id = _outward_result(data_dir, session_id, "not_established")
    env = bi.BoundaryInspectionEnv(data_dir)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "recent_rejected_action", "query_target": f"outward.{event_id}"},
        session_id=session_id)
    # A not_established projection is an UNRESOLVED attempt, not a
    # confirmed absence: the honest outcome is an incomplete observation,
    # never a complete NO_HOST_BOUNDARY_FOUND (defect R1).
    assert result["classification"] == bi.CLASS_OBSERVATION_INCOMPLETE
    assert result["evidence_complete"] is False
    assert result["boundaries"] == []
    assert [c["evidence_source_id"] for c in result["checks"]] == [
        "outward.canonical_occurrence", "outward.projection_attempts"]


def test_outward_attempted_incomplete():
    data_dir = _new_env("outward-attempted")
    session_id = _next_id("out")
    event_id = _outward_result(data_dir, session_id, "attempted")
    env = bi.BoundaryInspectionEnv(data_dir)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "recent_rejected_action", "query_target": f"outward.{event_id}"},
        session_id=session_id)
    assert result["classification"] == bi.CLASS_OBSERVATION_INCOMPLETE
    assert result["evidence_complete"] is False


def test_outward_zero_attempts_no_boundary():
    data_dir = _new_env("outward-zero-attempts")
    session_id = _next_id("out")
    event_id = _outward_result(data_dir, session_id, None)
    env = bi.BoundaryInspectionEnv(data_dir)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "recent_rejected_action", "query_target": f"outward.{event_id}"},
        session_id=session_id)
    assert result["classification"] == bi.CLASS_NO_HOST_BOUNDARY_FOUND
    assert result["evidence_complete"] is True
    assert result["boundaries"] == []


def test_outward_foreign_session_refused():
    data_dir = _new_env("outward-foreign-session")
    session_a = _next_id("out")
    session_b = _next_id("out")
    event_id = _outward_result(data_dir, session_a, "failed")
    env = bi.BoundaryInspectionEnv(data_dir)
    try:
        bi.evaluate_boundary_query(
            env, {"query_kind": "recent_rejected_action", "query_target": f"outward.{event_id}"},
            session_id=session_b)
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("foreign-session outward reference accepted")


def test_outward_nonexistent_reference_refused():
    data_dir = _new_env("outward-nonexistent")
    env = bi.BoundaryInspectionEnv(data_dir)
    try:
        bi.evaluate_boundary_query(
            env, {"query_kind": "recent_rejected_action",
                  "query_target": f"outward.{outward_communication.generate_outward_event_id()}"},
            session_id=_next_id("out"))
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("nonexistent outward reference accepted")


def test_outward_reference_to_non_outward_event_refused():
    data_dir = _new_env("outward-non-outward")
    session_id = _next_id("out")
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        pipeline_id = conn.execute(
            "SELECT pipeline_id FROM pipelines WHERE pipeline_key = ?", (PIPELINE_KEY,)
        ).fetchone()[0]
        occurred_at = _now()
        auth_context_id = outward_communication.generate_outward_event_id()
        conn.execute(
            "INSERT INTO sessions (session_id, pipeline_id, started_at) VALUES (?, NULL, ?)",
            (session_id, occurred_at))
        conn.execute(
            "INSERT INTO auth_contexts (auth_context_id, session_id, auth_state, established_at) "
            "VALUES (?, ?, 'unknown', ?)", (auth_context_id, session_id, occurred_at))
        event_id = outward_communication.generate_outward_event_id()
        conn.execute(
            "INSERT INTO events (event_id, event_type, pipeline_id, pipeline_provenance_status, "
            "epoch_id, auth_context_id, input_source_ref, occurred_at, record_created_at) "
            "VALUES (?, 'human_waking_input', ?, 'known', NULL, ?, NULL, ?, ?)",
            (event_id, pipeline_id, auth_context_id, occurred_at, occurred_at))
        conn.commit()
    finally:
        conn.close()
    env = bi.BoundaryInspectionEnv(data_dir)
    try:
        bi.evaluate_boundary_query(
            env, {"query_kind": "recent_rejected_action", "query_target": f"outward.{event_id}"},
            session_id=session_id)
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("non-outward reference accepted")


# ------------------------------------------------------------- persistence ---


def test_record_query_and_result_two_stage():
    data_dir = _new_env("persist-two-stage")
    session_id = _next_id("persist")
    occurred_at = _now()
    query = {"query_kind": "host_rule", "query_target": "participation.requires_reason"}
    outcome = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query=query, occurred_at=occurred_at,
    )
    assert outcome["result_persisted"] is True
    assert outcome["classification"] == bi.CLASS_NO_HOST_BOUNDARY_FOUND
    qid = outcome["query_event_id"]

    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    try:
        event = conn.execute(
            "SELECT e.event_type, e.pipeline_id, e.pipeline_provenance_status, a.session_id "
            "FROM events e JOIN auth_contexts a ON a.auth_context_id = e.auth_context_id "
            "WHERE e.event_id = ?", (qid,)).fetchone()
        assert event is not None
        assert event[0] == brr.BOUNDARY_INSPECTION_EVENT_TYPE
        assert event[2] == "known"
        assert event[3] == session_id
        pipeline_id = conn.execute(
            "SELECT pipeline_id FROM pipelines WHERE pipeline_key = ?", (PIPELINE_KEY,)).fetchone()[0]
        assert event[1] == pipeline_id
        components = conn.execute(
            "SELECT sequence, creator_actor_id, component_kind, component_text "
            "FROM event_components WHERE event_id = ? ORDER BY sequence", (qid,)).fetchall()
        assert [c[2] for c in components] == [
            brr.BOUNDARY_INSPECTION_QUERY_SPEC_COMPONENT_KIND,
            brr.BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND,
        ]
        spec = json.loads(components[0][3])
        assert spec["query_target"] == "participation.requires_reason"
        assert spec["session_id"] == session_id
        persisted = json.loads(components[1][3])
        assert persisted["classification"] == bi.CLASS_NO_HOST_BOUNDARY_FOUND
        assert bi.validate_result(persisted) is persisted
        host_actor = provenance_schema.derive_stable_id("actor", "bounded_clause_renderer")
        assert components[1][1] == host_actor
    finally:
        conn.close()


def test_stage1_query_without_pipeline_refused_before_any_write():
    data_dir = _new_env_unseeded("persist-unseeded")
    session_id = _next_id("persist")
    try:
        bi.record_boundary_query(
            data_dir, session_id=session_id, session_started_at=_now(),
            pipeline_key=PIPELINE_KEY,
            query={"query_kind": "host_rule", "query_target": "participation.requires_reason"},
            occurred_at=_now())
    except bi.BoundaryInspectionError:
        pass
    else:
        raise AssertionError("unseeded pipeline accepted")
    assert _table_rows(data_dir, "events") == []


def test_append_result_idempotent_replay_and_conflict():
    data_dir = _new_env("persist-replay")
    session_id = _next_id("persist")
    occurred_at = _now()
    qid = bi.record_boundary_query(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY,
        query={"query_kind": "host_rule", "query_target": "participation.requires_reason"},
        occurred_at=occurred_at)["query_event_id"]
    first = bi.append_boundary_inspection_result(data_dir, query_event_id=qid)
    assert first == {"query_event_id": qid, "result_persisted": True,
                     "already_present": False, "classification": bi.CLASS_NO_HOST_BOUNDARY_FOUND}
    replay = bi.append_boundary_inspection_result(data_dir, query_event_id=qid)
    assert replay == {"query_event_id": qid, "result_persisted": True,
                      "already_present": True, "classification": bi.CLASS_NO_HOST_BOUNDARY_FOUND}
    # The unsafe API is gone: a caller cannot inject or rebind a result.
    try:
        bi.append_boundary_inspection_result(data_dir, query_event_id=qid, result={})
    except TypeError:
        pass
    else:
        raise AssertionError("caller-supplied result authority still exists")


def test_stage2_failure_never_erases_query_occurrence():
    data_dir = _new_env("persist-stage2-failure")
    session_id = _next_id("persist")
    occurred_at = _now()
    qid = bi.record_boundary_query(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY,
        query={"query_kind": "host_rule", "query_target": "participation.requires_reason"},
        occurred_at=occurred_at)["query_event_id"]
    original = bi.evaluate_boundary_query
    try:
        def fail_evaluation(*_args, **_kwargs):
            raise bi.BoundaryInspectionError("synthetic stage-2 failure")
        bi.evaluate_boundary_query = fail_evaluation
        try:
            bi.append_boundary_inspection_result(data_dir, query_event_id=qid)
        except bi.BoundaryInspectionError:
            pass
        else:
            raise AssertionError("synthetic stage-2 failure did not propagate")
    finally:
        bi.evaluate_boundary_query = original
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM event_components WHERE event_id = ?", (qid,)).fetchone()[0]
        assert count == 1
        leftover = conn.execute(
            "SELECT COUNT(*) FROM event_components WHERE event_id = ? AND component_kind = ?",
            (qid, brr.BOUNDARY_INSPECTION_RESULT_COMPONENT_KIND)).fetchone()[0]
        assert leftover == 0
    finally:
        conn.close()


def test_next_pending_fifo_and_delivered_discipline():
    data_dir = _new_env("persist-fifo")
    session_id = _next_id("persist")
    occurred_at = _now()
    # Runtime order: the driving pipeline's waking turn mints the
    # session first; boundary queries then ride that same session.
    _wake_turn(data_dir, session_id, occurred_at, occurred_at, "est")
    q1 = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query={"query_kind": "host_rule", "query_target": "participation.requires_reason"},
        occurred_at=occurred_at)["query_event_id"]
    q2 = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query={"query_kind": "host_rule", "query_target": "participation.requires_permission_to_speak"},
        occurred_at=occurred_at + 1)["query_event_id"]

    first = bi.next_pending_boundary_result(data_dir, session_id)
    assert first is not None and first["query_event_id"] == q1
    assert first["query"]["query_target"] == "participation.requires_reason"
    assert bi.validate_result(first["result"]) is first["result"]

    wake1 = _wake_turn(data_dir, session_id, occurred_at, occurred_at + 2, "w1",
                       delivered_boundary_query_event_id=q1)
    assert _carry_and_deliver(data_dir, q1, wake1, occurred_at + 2)["already_present"] is False
    assert _carry_and_deliver(data_dir, q1, wake1, occurred_at + 2)["already_present"] is True

    second = bi.next_pending_boundary_result(data_dir, session_id)
    assert second is not None and second["query_event_id"] == q2

    wake2 = _wake_turn(data_dir, session_id, occurred_at, occurred_at + 3, "w2",
                       delivered_boundary_query_event_id=q2)
    _carry_and_deliver(data_dir, q2, wake2, occurred_at + 3)
    assert bi.next_pending_boundary_result(data_dir, session_id) is None


def test_next_pending_session_isolation():
    data_dir = _new_env("persist-isolation")
    session_a = _next_id("persist")
    occurred_at = _now()
    bi.record_boundary_query_and_result(
        data_dir, session_id=session_a, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query={"query_kind": "host_rule", "query_target": "participation.requires_reason"},
        occurred_at=occurred_at)
    session_b = _next_id("persist")
    assert bi.next_pending_boundary_result(data_dir, session_b) is None
    assert bi.next_pending_boundary_result(data_dir, session_a) is not None


def test_delivered_marker_requires_query_and_resists_rewrite():
    data_dir = _new_env("persist-delivered")
    session_id = _next_id("persist")
    occurred_at = _now()

    try:
        bi.record_boundary_inspection_delivered(
            data_dir, query_event_id="bq-" + "A" * 26,
            waking_turn_event_id="wake-x", occurred_at=occurred_at)
    except bi.BoundaryInspectionError:
        pass
    else:
        raise AssertionError("delivery marker written for a nonexistent query event")

    # Runtime order: establish the session with a genuine waking turn
    # first; boundary queries then ride that same session.
    _wake_turn(data_dir, session_id, occurred_at, occurred_at, "est")

    qid = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query={"query_kind": "host_rule", "query_target": "participation.requires_reason"},
        occurred_at=occurred_at)["query_event_id"]

    # A delivery by a FORGED waking-turn event (no canonical event
    # behind it) must be rejected outright -- defect R6 (b): a marker
    # claimed for a turn that never carried it.
    try:
        bi.record_boundary_inspection_delivered(
            data_dir, query_event_id=qid,
            waking_turn_event_id="wake-forged", occurred_at=occurred_at)
    except bi.BoundaryInspectionError:
        pass
    else:
        raise AssertionError("delivery marker written for a nonexistent waking turn event")

    wake_a = _wake_turn(data_dir, session_id, occurred_at, occurred_at + 1, "wa",
                        delivered_boundary_query_event_id=qid)
    first = _carry_and_deliver(data_dir, qid, wake_a, occurred_at + 1)
    assert first["already_present"] is False
    same = bi.record_boundary_inspection_delivered(
        data_dir, query_event_id=qid,
        waking_turn_event_id=wake_a, occurred_at=occurred_at + 1)
    assert same["already_present"] is True

    # Rewriting delivery history through a DIFFERENT genuine turn --
    # even one whose OWN real composition result genuinely names this
    # exact query as surviving -- must still be refused: the delivered
    # marker is written once (bound to wake_a) and resists rewrite by
    # any other turn.
    wake_b = _wake_turn(data_dir, session_id, occurred_at, occurred_at + 2, "wb",
                        delivered_boundary_query_event_id=qid)
    try:
        bi.record_boundary_inspection_delivered(
            data_dir, query_event_id=qid,
            waking_turn_event_id=wake_b, occurred_at=occurred_at + 2)
    except bi.BoundaryInspectionError:
        pass
    else:
        raise AssertionError("delivery history rewritten")


# ---------------------------------------------------------------- delivery ---


def test_record_query_round_trips_input_source_ref():
    data_dir = _new_env("persist-input-source-ref")
    session_id = _next_id("persist")
    occurred_at = _now()
    source_ref = "wake-urn-native-02cx6d"
    qid = bi.record_boundary_query(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY,
        query={"query_kind": "host_rule", "query_target": "participation.requires_reason"},
        occurred_at=occurred_at, input_source_ref=source_ref)["query_event_id"]
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    try:
        stored = conn.execute(
            "SELECT input_source_ref FROM events WHERE event_id = ?", (qid,)).fetchone()[0]
    finally:
        conn.close()
    assert stored == source_ref


def test_render_boundary_result_delivery_host_framed_and_bounded():
    data_dir = _new_env("deliver-render")
    session_id = _next_id("persist")
    occurred_at = _now()
    outcome = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query={"query_kind": "host_rule", "query_target": "participation.requires_reason"},
        occurred_at=occurred_at)
    pending = bi.next_pending_boundary_result(data_dir, session_id)
    assert pending["query_event_id"] == outcome["query_event_id"]
    text = bi.render_boundary_result_delivery(pending["result"])
    assert text.startswith("Host-established boundary report")
    assert "not your own conclusion" in text
    assert len(text) <= bi.MAX_DELIVERY_TEXT_CHARS
    assert data_dir not in text
    assert "/" not in text, "delivery text must never expose a file path"


# --------------------------------------------------------------- regressions ---
#
# Permanent, byte-exact encodings of the two Codex-blocking defect
# clusters corrector 1 was authorized to fix. Each assertion is the
# OPPOSITE of the confirmed pre-correction behavior (see
# .anaxi-dev/evidence/boundary_inspector_v1_codex_blocking_correction_1_pre_reproduction.json),
# so any regression re-introducing the defect fails loudly.

_QUERY = {"query_kind": "host_rule", "query_target": "participation.requires_reason"}


def _validate_query():
    return {"query_kind": "host_rule", "query_target": "participation.requires_reason"}


def _chk(evidence_source_id, result, boundary_id=None):
    return {"evidence_source_id": evidence_source_id, "result": result,
            "boundary_id": boundary_id, "observed_at": _now()}


def _bentry(boundary_id="host_rule.participation.requires_reason"):
    return {"boundary_id": boundary_id, "boundary_type": "conversation_participation_rule",
            "operational_why": "why", "architectural_rationale": "rationale",
            "what_changes_it": "change", "evidence_source_ids": ["synthetic"]}


def _result_for_validate(classification, evidence_complete, boundaries=(), checks=(),
                         scope_note="a synthetic result"):
    return {"query": _validate_query(), "classification": classification,
            "evidence_complete": evidence_complete, "boundaries": list(boundaries),
            "checks": list(checks), "evaluated_at": _now(), "scope_note": scope_note}


def _assert_rejected(result, label):
    try:
        bi.validate_result(result)
    except bi.BoundaryInspectionError:
        return
    raise AssertionError(f"validate_result accepted {label}")


def test_validate_result_rejects_evidence_free_complete_negative():
    """Defect R4: an evidence_complete=True finding with NO checks."""
    _assert_rejected(_result_for_validate(
        bi.CLASS_NO_HOST_BOUNDARY_FOUND, True, checks=[]), "an evidence-free complete negative")


def test_validate_result_rejects_complete_with_unavailable_check():
    _assert_rejected(_result_for_validate(
        bi.CLASS_NO_HOST_BOUNDARY_FOUND, True, checks=[_chk("s", bi.CHECK_UNAVAILABLE)]),
        "a complete result carrying an unavailable check")


def test_validate_result_rejects_no_host_with_boundary_entries():
    _assert_rejected(_result_for_validate(
        bi.CLASS_NO_HOST_BOUNDARY_FOUND, True,
        checks=[_chk("s", bi.CHECK_BOUNDARY_NOT_ESTABLISHED)], boundaries=[_bentry()]),
        "NO_HOST_BOUNDARY_FOUND with boundary entries")


def test_validate_result_rejects_no_host_with_established_or_conflicting_check():
    for state in (bi.CHECK_BOUNDARY_ESTABLISHED, bi.CHECK_CONFLICTING):
        _assert_rejected(_result_for_validate(
            bi.CLASS_NO_HOST_BOUNDARY_FOUND, True,
            checks=[_chk("authoritative", state,
                         "host_rule.participation.requires_reason")]),
            f"NO_HOST_BOUNDARY_FOUND with {state}")


def test_validate_result_rejects_actual_host_without_boundaries():
    _assert_rejected(_result_for_validate(
        bi.CLASS_ACTUAL_HOST_BOUNDARY, True,
        checks=[_chk("s", bi.CHECK_BOUNDARY_ESTABLISHED)], boundaries=[]),
        "ACTUAL_HOST_BOUNDARY with no boundary entry")


def test_validate_result_rejects_observation_incomplete_with_complete_evidence():
    _assert_rejected(_result_for_validate(
        bi.CLASS_OBSERVATION_INCOMPLETE, True,
        checks=[_chk("s", bi.CHECK_BOUNDARY_NOT_ESTABLISHED)]),
        "OBSERVATION_INCOMPLETE with evidence_complete=True")


def test_validate_result_rejects_cause_not_established_without_unresolved_check():
    _assert_rejected(_result_for_validate(
        bi.CLASS_CAUSE_NOT_ESTABLISHED, False,
        checks=[_chk("s", bi.CHECK_BOUNDARY_NOT_ESTABLISHED)]),
        "CAUSE_NOT_ESTABLISHED(False) with only settled checks")


def test_validate_result_accepts_consistent_shapes():
    """Positive control: the semantics bind, they do not over-reject."""
    for result in (
        _result_for_validate(bi.CLASS_NO_HOST_BOUNDARY_FOUND, True,
                             checks=[_chk("s", bi.CHECK_BOUNDARY_NOT_ESTABLISHED)]),
        _result_for_validate(bi.CLASS_ACTUAL_HOST_BOUNDARY, True,
                             checks=[_chk("s", bi.CHECK_BOUNDARY_ESTABLISHED)], boundaries=[_bentry()]),
        _result_for_validate(bi.CLASS_OBSERVATION_INCOMPLETE, False,
                             checks=[_chk("s", bi.CHECK_UNAVAILABLE)]),
        _result_for_validate(bi.CLASS_CAUSE_NOT_ESTABLISHED, True,
                             checks=[_chk("s", bi.CHECK_CONFLICTING)]),
    ):
        assert bi.validate_result(result) is result


class _CatalogErrorConn:
    def execute(self, *_a, **_k):
        raise sqlite3.OperationalError("synthetic catalog I/O failure")


def test_catalog_read_error_fails_closed():
    """Defect R2: a catalog read error must raise (fail-closed), never
    be coerced into False (a verified structural absence)."""
    try:
        bi._has_unique_index_on(_CatalogErrorConn(), "sleep_open_requests", {"actor_id"})
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("catalog read error was coerced into a boolean")


def test_sleep_and_wtr0_catalog_error_downgrade_to_incomplete():
    """Defect R2 (consumer side): the fail-closed BoundaryInspectionError
    becomes OBSERVATION_INCOMPLETE with CHECK_UNAVAILABLE, never a
    definite NO_HOST_BOUNDARY_FOUND."""
    data_dir = _new_env("catalog-error")
    env = bi.BoundaryInspectionEnv(data_dir)
    original = bi._has_unique_index_on
    try:
        def boom(*_a, **_k):
            raise bi.BoundaryInspectionError("synthetic unreadable catalog")
        bi._has_unique_index_on = boom
        for target, source in (("sleep.one_unresolved_root", "sleep.open_request_uniqueness"),
                               ("wtr0.recovery_ceiling", "wtr0.recovery_uniqueness")):
            result = bi.evaluate_boundary_query(
                env, {"query_kind": "boundary_id", "query_target": target})
            assert result["classification"] == bi.CLASS_OBSERVATION_INCOMPLETE, (target, result)
            assert result["evidence_complete"] is False
            assert result["boundaries"] == []
            assert [c["evidence_source_id"] for c in result["checks"]] == [source]
            assert result["checks"][0]["result"] == bi.CHECK_UNAVAILABLE
            assert bi.validate_result(result) is result
    finally:
        bi._has_unique_index_on = original


def test_budget_trace_missing_status_keys_downgrades_to_incomplete():
    """Defect R3: a session trace record missing pass1_status/pass2_status
    makes the rejection state unobservable -- OBSERVATION_INCOMPLETE,
    never a complete negative."""
    trace_path = os.path.join(TEST_DIR, "budget-missing-status.jsonl")
    session_id = _next_id("budget")
    with open(trace_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"session_id": session_id, "pass2_status": "ok"}) + "\n")
    env = bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, "budget-missing"), trace_path=trace_path)
    result = bi.evaluate_boundary_query(
        env, {"query_kind": "recent_rejected_action", "query_target": "budget.most_recent"},
        session_id=session_id)
    assert result["classification"] == bi.CLASS_OBSERVATION_INCOMPLETE
    assert result["evidence_complete"] is False
    assert result["boundaries"] == []
    assert result["checks"][0]["result"] == bi.CHECK_UNAVAILABLE


def test_budget_trace_null_and_unknown_statuses_downgrade_to_incomplete():
    query = {"query_kind": "recent_rejected_action", "query_target": "budget.most_recent"}
    for label, pass1_status, pass2_status in (
        ("null", None, None),
        ("unknown", "FUTURE_UNKNOWN_STATUS", "ALIEN_STATUS"),
    ):
        trace_path = os.path.join(TEST_DIR, f"budget-{label}-status.jsonl")
        session_id = _next_id("budget")
        with open(trace_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "session_id": session_id,
                "pass1_status": pass1_status,
                "pass2_status": pass2_status,
            }) + "\n")
        result = bi.evaluate_boundary_query(
            bi.BoundaryInspectionEnv(os.path.join(TEST_DIR, f"budget-{label}"), trace_path=trace_path),
            query, session_id=session_id)
        assert result["classification"] == bi.CLASS_OBSERVATION_INCOMPLETE, (label, result)
        assert result["evidence_complete"] is False
        assert result["checks"][0]["result"] == bi.CHECK_UNAVAILABLE


def test_append_result_binds_to_the_recorded_query():
    """R6: stage 2 derives session/query/time from the exact occurrence;
    no result from another same-kind/target occurrence can be supplied."""
    data_dir = _new_env("append-binding")
    session_a = _next_id("bind-a")
    session_b = _next_id("bind-b")
    occurred_at = _now()
    query = {"query_kind": "recent_rejected_action", "query_target": "budget.most_recent"}
    trace_path = os.path.join(TEST_DIR, "append-binding-trace.jsonl")
    _record_trace(trace_path, session_a, pass1_status="ok", pass2_status="ok")
    _record_trace(trace_path, session_b, pass1_status="BUDGET_EXCEEDED", pass2_status=None)
    qa = bi.record_boundary_query(
        data_dir, session_id=session_a, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query=query, occurred_at=occurred_at)["query_event_id"]
    qb = bi.record_boundary_query(
        data_dir, session_id=session_b, session_started_at=occurred_at + 1,
        pipeline_key=PIPELINE_KEY, query=query, occurred_at=occurred_at + 1)["query_event_id"]
    appended_b = bi.append_boundary_inspection_result(
        data_dir, query_event_id=qb, trace_path=trace_path)
    assert appended_b["classification"] == bi.CLASS_ACTUAL_HOST_BOUNDARY
    pending_b = bi.next_pending_boundary_result(data_dir, session_b)
    try:
        bi.append_boundary_inspection_result(
            data_dir, query_event_id=qa, result=pending_b["result"], trace_path=trace_path)
    except TypeError:
        pass
    else:
        raise AssertionError("a foreign occurrence result was accepted")
    appended = bi.append_boundary_inspection_result(
        data_dir, query_event_id=qa, trace_path=trace_path)
    assert appended["query_event_id"] == qa
    assert appended["classification"] == bi.CLASS_NO_HOST_BOUNDARY_FOUND


def test_append_result_rejects_nonexistent_and_corrupt_occurrence_identity():
    data_dir = _new_env("append-invalid-occurrence")
    try:
        bi.append_boundary_inspection_result(data_dir, query_event_id="bq-" + "A" * 26)
    except bi.BoundaryInspectionError:
        pass
    else:
        raise AssertionError("nonexistent query occurrence was accepted")

    session_id = _next_id("bind")
    occurred_at = _now()
    _wake_turn(data_dir, session_id, occurred_at, occurred_at, "est-invalid")
    qid = _insert_raw_event(
        data_dir, session_id=session_id,
        event_type=brr.BOUNDARY_INSPECTION_EVENT_TYPE,
        pipeline_key=PIPELINE_KEY, occurred_at=occurred_at)
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    try:
        corrupt = json.dumps({
            "query_kind": "host_rule",
            "query_target": "not.a.real.target",
            "session_id": "different-session",
            "occurred_at": occurred_at,
        }, sort_keys=True)
        conn.execute(
            "INSERT INTO event_components (event_id, sequence, creator_actor_id, component_kind, "
            "authorship_resolution, component_text, content_sha256, model_revision_id, "
            "span_start, span_end) VALUES (?, 0, ?, ?, 'resolved', ?, ?, NULL, NULL, NULL)",
            (qid, provenance_schema.derive_stable_id("actor", "clark"),
             brr.BOUNDARY_INSPECTION_QUERY_SPEC_COMPONENT_KIND, corrupt,
             hashlib.sha256(corrupt.encode("utf-8")).hexdigest()),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        bi.append_boundary_inspection_result(data_dir, query_event_id=qid)
    except bi.BoundaryInspectionError:
        pass
    else:
        raise AssertionError("corrupt target/session occurrence identity was accepted")


def test_wake_turn_wrong_type_rejected():
    data_dir = _new_env("wake-wrong-type")
    session_id = _next_id("wake")
    occurred_at = _now()
    _wake_turn(data_dir, session_id, occurred_at, occurred_at, "est")
    qid = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query=_QUERY, occurred_at=occurred_at)["query_event_id"]
    wrong = _insert_raw_event(
        data_dir, session_id=session_id, event_type="human_waking_input",
        pipeline_key=PIPELINE_KEY, occurred_at=occurred_at + 1)
    try:
        bi.record_boundary_inspection_delivered(
            data_dir, query_event_id=qid,
            waking_turn_event_id=wrong, occurred_at=occurred_at + 1)
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("a non-waking event was accepted as the carrying turn")


def test_wake_turn_wrong_session_rejected():
    data_dir = _new_env("wake-wrong-session")
    query_session = _next_id("wake")
    occurred_at = _now()
    _wake_turn(data_dir, query_session, occurred_at, occurred_at, "est")
    qid = bi.record_boundary_query_and_result(
        data_dir, session_id=query_session, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query=_QUERY, occurred_at=occurred_at)["query_event_id"]
    other_session = _next_id("wake")
    other_wake = _wake_turn(data_dir, other_session, occurred_at, occurred_at + 1, "other")
    try:
        bi.record_boundary_inspection_delivered(
            data_dir, query_event_id=qid,
            waking_turn_event_id=other_wake, occurred_at=occurred_at + 1)
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("a waking turn from a foreign session was accepted")


def test_wake_turn_different_pipeline_rejected():
    data_dir = _new_env("wake-wrong-pipeline")
    session_id = _next_id("wake")
    occurred_at = _now()
    _wake_turn(data_dir, session_id, occurred_at, occurred_at, "est")
    qid = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query=_QUERY, occurred_at=occurred_at)["query_event_id"]
    foreign = _insert_raw_event(
        data_dir, session_id=session_id, event_type="waking_turn",
        pipeline_key="anaxi_orchestration_lineage_b", occurred_at=occurred_at + 1)
    try:
        bi.record_boundary_inspection_delivered(
            data_dir, query_event_id=qid,
            waking_turn_event_id=foreign, occurred_at=occurred_at + 1)
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("a waking turn on a different pipeline was accepted")


def test_wake_turn_temporal_backward_rejected():
    data_dir = _new_env("wake-temporal")
    session_id = _next_id("wake")
    occurred_at = _now()
    before = _wake_turn(data_dir, session_id, occurred_at, occurred_at - 10, "before")
    qid = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query=_QUERY, occurred_at=occurred_at)["query_event_id"]
    try:
        bi.record_boundary_inspection_delivered(
            data_dir, query_event_id=qid,
            waking_turn_event_id=before, occurred_at=occurred_at - 10)
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("a waking turn that preceded the query was accepted")


def test_wake_turn_timestamp_mismatch_rejected():
    data_dir = _new_env("wake-timestamp")
    session_id = _next_id("wake")
    occurred_at = _now()
    _wake_turn(data_dir, session_id, occurred_at, occurred_at, "est")
    qid = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query=_QUERY, occurred_at=occurred_at)["query_event_id"]
    wake = _wake_turn(data_dir, session_id, occurred_at, occurred_at + 5, "late")
    try:
        bi.record_boundary_inspection_delivered(
            data_dir, query_event_id=qid,
            waking_turn_event_id=wake, occurred_at=occurred_at + 6)
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("a caller-supplied fabricated occurred_at was accepted")


def test_delivered_requires_actual_composition_inclusion():
    """R7 / defect #4: a genuine waking turn whose REAL composition
    result does not show this query surviving must never have delivery
    acknowledged for it -- a caller cannot assert carriage merely by
    naming two real event ids."""
    data_dir = _new_env("wake-not-composed")
    session_id = _next_id("wake")
    occurred_at = _now()
    _wake_turn(data_dir, session_id, occurred_at, occurred_at, "est")
    qid = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query=_QUERY, occurred_at=occurred_at)["query_event_id"]
    bare = _wake_turn(data_dir, session_id, occurred_at, occurred_at + 1, "bare")
    # The composition result genuinely produced by this turn never
    # included the boundary result at all (e.g. dropped for budget, or
    # simply an unrelated later turn) -- delivered_source_ids is empty.
    try:
        bi.record_boundary_inspection_delivered(
            data_dir, query_event_id=qid,
            waking_turn_event_id=bare, occurred_at=occurred_at + 1)
    except bi.BoundaryInspectionError:
        pass
    else:
        raise AssertionError("delivery was acknowledged for a turn that did not carry the result")
    assert bi.next_pending_boundary_result(data_dir, session_id) is not None


def test_real_composition_canonical_waking_delivery_whole_path():
    """R6-R8 integration: real query/evaluation, real compositor source
    survival, canonical waking transaction carriage, delivery, FIFO."""
    data_dir = _new_env("whole-path")
    session_id = _next_id("whole")
    occurred_at = _now()
    _wake_turn(data_dir, session_id, occurred_at, occurred_at, "est")
    outcome = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query=_QUERY, occurred_at=occurred_at)
    pending = bi.next_pending_boundary_result(data_dir, session_id)
    assert pending["query_event_id"] == outcome["query_event_id"]
    contribution = context_budget.Contribution(
        context_budget.BOUNDARY_INSPECTION_RESULT,
        bi.render_boundary_result_delivery(pending["result"]),
        hard=False, source_ids=[pending["query_event_id"]],
    )
    composed = context_budget.compose_within_budget([contribution], 100000)
    included_ids = composed.delivered_source_ids(context_budget.BOUNDARY_INSPECTION_RESULT)
    assert included_ids == [pending["query_event_id"]]
    wake = _wake_turn(
        data_dir, session_id, occurred_at, occurred_at + 1, "included",
        delivered_boundary_query_event_id=included_ids[0],
    )
    conn = sqlite3.connect(os.path.join(data_dir, "anaxi_provenance.db"))
    try:
        canonical = conn.execute(
            "SELECT component_text FROM event_components WHERE event_id = ? AND component_kind = ?",
            (wake, brr.BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND),
        ).fetchall()
    finally:
        conn.close()
    assert canonical == [(pending["query_event_id"],)]
    delivered = bi.record_boundary_inspection_delivered(
        data_dir, query_event_id=pending["query_event_id"],
        waking_turn_event_id=wake, occurred_at=occurred_at + 1)
    assert delivered["already_present"] is False
    assert bi.next_pending_boundary_result(data_dir, session_id) is None


def test_delivered_rejects_bare_event_id_assertion_without_composition_object():
    """R7's central attack: a caller must not be able to fabricate
    delivery merely by naming a real query_event_id and a real,
    genuinely-unrelated waking_turn_event_id in the same session --
    there is no independently-callable carriage entry point, and
    passing anything other than the real composition object (i.e. an
    object that does not even expose delivered_source_ids) must be
    refused outright, never treated as an implicit true/false claim."""
    data_dir = _new_env("wake-bare-assertion")
    session_id = _next_id("wake")
    occurred_at = _now()
    _wake_turn(data_dir, session_id, occurred_at, occurred_at, "est")
    qid = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query=_QUERY, occurred_at=occurred_at)["query_event_id"]
    unrelated_turn = _wake_turn(data_dir, session_id, occurred_at, occurred_at + 1, "unrelated")
    # The API accepts no composition object at all; duck-typed and even
    # genuine/reconstructed CompositionResult instances have no authority.
    real_lookalike = context_budget.CompositionResult()
    for forged_claim in (True, "carried", qid, object(), real_lookalike):
        try:
            bi.record_boundary_inspection_delivered(
                data_dir, query_event_id=qid, waking_turn_event_id=unrelated_turn,
                occurred_at=occurred_at + 1, composition_result=forged_claim)
        except TypeError:
            continue
        raise AssertionError(f"a bare non-composition claim ({forged_claim!r}) forged delivery")
    try:
        bi.record_boundary_inspection_delivered(
            data_dir, query_event_id=qid, waking_turn_event_id=unrelated_turn,
            occurred_at=occurred_at + 1)
    except bi.BoundaryInspectionError:
        pass
    else:
        raise AssertionError("an unrelated waking turn without canonical carriage forged delivery")
    assert bi.next_pending_boundary_result(data_dir, session_id) is not None


def test_carriage_conflicting_rewrite_rejected():
    data_dir = _new_env("wake-carriage-conflict")
    session_id = _next_id("wake")
    occurred_at = _now()
    _wake_turn(data_dir, session_id, occurred_at, occurred_at, "est")
    q1 = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY, query=_QUERY, occurred_at=occurred_at)["query_event_id"]
    q2 = bi.record_boundary_query_and_result(
        data_dir, session_id=session_id, session_started_at=occurred_at,
        pipeline_key=PIPELINE_KEY,
        query={"query_kind": "host_rule", "query_target": "participation.requires_permission_to_speak"},
        occurred_at=occurred_at + 1)["query_event_id"]
    wake = _wake_turn(data_dir, session_id, occurred_at, occurred_at + 2, "c",
                      delivered_boundary_query_event_id=q1)
    _carry_and_deliver(data_dir, q1, wake, occurred_at + 2)
    assert _carry_and_deliver(data_dir, q1, wake, occurred_at + 2)["already_present"] is True
    try:
        bi.record_boundary_inspection_delivered(
            data_dir, query_event_id=q2,
            waking_turn_event_id=wake, occurred_at=occurred_at + 2)
    except bi.BoundaryInspectionError:
        return
    raise AssertionError("carriage history was rewritten for the same waking turn")


# ------------------------------------------------------------------- runner ---

ALL_TESTS = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def main():
    failed = 0
    for test in ALL_TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(ALL_TESTS)} tests, {len(ALL_TESTS) - failed} passed, {failed} failed")
    import shutil
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
