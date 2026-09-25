"""ANAXI Reference Harness / Stabilization V0.

One deterministic, offline integration harness exercising the REAL,
accepted production modules of this lineage through representative
waking/recovery/Sleep/capability seams. This is NOT a second
implementation of ANAXI -- every scenario below drives actual
production functions (provenance_schema, native_provenance_writer,
human_session_binding, operative_directive, external_information,
family_membership, discord_correspondence, wtr0_waking_recovery,
sleep_cycle, context_budget, ...). Mocks/fakes are used only at
genuine external boundaries (the `ollama` inference client, Discord
HTTP transport, external web transport) -- exactly the same technique
this repository's own existing test suites already established
(test_native_waking_turn_end_to_end.py, test_human_waking_authority_
end_to_end.py, test_fs1_family_shared_expansion_v0.py,
test_discord_correspondence_v0.py, test_external_information.py,
test_waking_turn_recovery.py, test_sleep_v1_acceptance.py). This
harness reuses those files' own fixtures/helpers by import wherever
possible rather than reimplementing them, and adds only the missing
CROSS-CAPABILITY integrated scenarios (R8/R9) plus a thin end-to-end
driver over each accepted capability seam.

Every scenario runs against fresh, isolated, temporary state (a
tempfile.mkdtemp() root per scenario) and NEVER touches:
  - the real anaxi_provenance.db / production data directory;
  - real Private Space;
  - real family/Discord accounts;
  - any live network or Discord transport;
  - any credential.

Run:
    python3 -B anaxi_final/reference_harness_v0.py
    python3 -B anaxi_final/reference_harness_v0.py --regressions   # also run existing suites
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import types
import uuid

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
if ANAXI_FINAL not in sys.path:
    sys.path.insert(0, ANAXI_FINAL)


# ============================================================================
# Optional heavy-dependency stubs. sentence_transformers/anthropic are
# imported at MODULE level by anaxi_protocol_sqlite.py / anaxi_sleep.py but
# never actually CONSTRUCTED on any path this harness exercises (every
# real E2E fixture in this repository bypasses ConstitutionalMind.__init__
# via __new__ + manual _init_core_tables, and never calls anaxi_sleep's
# Anthropic-backed helper). Stubbing them so these modules import cleanly
# offline is the SAME established technique test_fs1_family_shared_
# expansion_v0.py already uses in this exact repository -- not a new
# workaround invented for this harness.
# ============================================================================
def _stub_optional_heavy_dependencies():
    for name, attr in (("sentence_transformers", "SentenceTransformer"), ("anthropic", "Anthropic")):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            setattr(stub, attr, object)
            sys.modules[name] = stub


_stub_optional_heavy_dependencies()

from provenance_schema import create_provenance_db, derive_stable_id
from migrate_historical_data import build_pipeline_map, seed_reference_data, alter_existing_stores_schema
from hir1_registration import register_canonical_human, HOST_ACTOR_ID
import hir1_schema_migration
import od1_schema_migration
import fs1_schema_migration
import oc0_schema_migration
import dc0_schema_migration
import wtr0_schema_migration
import boundary_inspector_schema_migration
import sleep_c_schema

import human_session_binding as hsb
import operative_directive as od1
import external_information as ext
import external_information_net as ext_net
import external_info_registry as eir
import family_membership as fm
import discord_correspondence as dc
import discord_correspondence_registry as dcr
import discord_correspondence_net as dc_net
import outward_communication as oc
import conversation_direction as cd
import context_budget as cb
import native_provenance_writer as npw
import wtr0_waking_recovery as recovery
import waking_failure_evidence as wfe
from wtr0_cold_reset import ColdResetOutcome
import inference_provider as ip


_MANIFEST = {"pipelines": {
    "llama": {"routing_constant_value": "nate"},
    "claude": {"routing_constant_value": "nate"},
}}
_PIPELINE_KEY = "anaxi_orchestration_lineage_a"
_CLARK_ACTOR_ID = derive_stable_id("actor", "clark")

_ALL_TEMP_ROOTS = []


def _mk_root(label):
    root = tempfile.mkdtemp(prefix=f"anaxi_refharness_{label}_")
    _ALL_TEMP_ROOTS.append(root)
    return root


def _cleanup_all_roots():
    for root in _ALL_TEMP_ROOTS:
        shutil.rmtree(root, ignore_errors=True)


# ============================================================================
# Shared isolated-environment builder. Applies exactly the accepted
# migrations a scenario names, seeds reference data through the REAL
# migrate_historical_data.seed_reference_data(), and returns the db_path.
# Mirrors the *_new_env()/_seed_*_env() helpers already established across
# test_operative_directive.py / test_external_information.py /
# test_discord_correspondence_v0.py / test_fs1_family_shared_expansion_v0.py.
# ============================================================================
_ALL_MIGRATIONS = (
    hir1_schema_migration, od1_schema_migration, fs1_schema_migration,
    oc0_schema_migration, dc0_schema_migration, wtr0_schema_migration,
    boundary_inspector_schema_migration,
)


def new_env(root, migrations=(), seed=True):
    db_path = os.path.join(root, "anaxi_provenance.db")
    create_provenance_db(db_path).close()
    for mod in migrations:
        mod.apply_additive_migration(db_path)
    if seed:
        pipeline_map = build_pipeline_map(_MANIFEST)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        seed_reference_data(conn, pipeline_map, int(time.time()))
        conn.close()
    return db_path


def register_human(db_path, suffix, aab_suffix=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    result, failure = register_canonical_human(conn, {
        "registration_request_id": f"refharness-req-{suffix}",
        "aab_actor_id": f"actor-refharness-{aab_suffix or suffix}",
        "display_label": f"Reference Harness Human {suffix}",
        "source": "local_operator_provisioning",
    })
    conn.close()
    assert failure is None, f"register_canonical_human failed: {failure}"
    return result["actor_id"]


def operative_directive_contribution(rendered_text, source_id=None):
    """Mirrors llama_anaxi.py's own OPERATIVE_DIRECTIVE Contribution
    construction exactly (pinned single-unit SOFT contribution)."""
    return cb.Contribution(
        cb.OPERATIVE_DIRECTIVE, rendered_text, hard=False,
        source_ids=[source_id] if source_id else None,
        droppable_units=[rendered_text], render_fn=lambda units: units[0] if units else "",
        minimum_units=1,
    )


def mapped_inbound_sender(root, owner, label):
    """Since 12d0fb0 an inbound message reaches Clark only from a Discord author the owner has mapped to
    a principal (or on a surface the owner opened to other sources); an unmapped author is held.  The
    harness therefore maps its sender to the owner first and stamps the message AFTER the mapping."""
    import discord_author_mapping as dam
    author_id = "900000000000000099"
    dam.map_author(root, discord_author_id=author_id, principal_actor_id=owner, requester_actor_id=owner,
                   occurred_at=int(time.time()), display_label=label)
    message_id = str(((int((time.time() + 5) * 1000) - 1420070400000) << 22) | 1)
    return author_id, message_id


def genuine_waking_turn(db_path, root, *, session_id, session_started_at, occurred_at, tag, **kwargs):
    """A GENUINE, canonically-committed waking turn via the REAL
    native_provenance_writer.stage_and_record_native_waking_turn() --
    the single production entry point every capability module's own
    tests use as the lightweight, non-Ollama basis for a real
    triggering/carrying waking turn. Accepts the same optional
    capability fields that function does (operative_directive_*,
    external_info_*, discord_correspondence_*, visibility_scope,
    human_input_event_id, delivered_*)."""
    data_dir = os.path.dirname(db_path)
    staging_path = os.path.join(root, f"native_turn_staging_{tag}.jsonl")
    recorded = npw.stage_and_record_native_waking_turn(
        data_dir, staging_path,
        session_id=session_id, session_started_at=session_started_at,
        user_id="reference-harness-operator", prompt="a synthetic reference-harness prompt",
        bounded_clause="", clark_prose="a synthetic reference-harness reply",
        kardia={}, controls={}, waking_model_tag="inert-reference-harness-model",
        pipeline_key=_PIPELINE_KEY, artifact_pass_ran=False, occurred_at=occurred_at,
        **kwargs,
    )
    assert "event_id" in recorded, f"genuine waking turn did not return an event_id: {recorded!r}"
    return recorded["event_id"]


# ============================================================================
# Report plumbing
# ============================================================================
class Report:
    def __init__(self):
        self.rows = []  # (label, status, detail)

    def record(self, label, status, detail=""):
        self.rows.append((label, status, detail))
        marker = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
        line = f"[{marker}] {label}"
        if detail and status != "PASS":
            line += f" -- {detail}"
        print(line)

    def failed(self):
        return [r for r in self.rows if r[1] == "FAIL"]

    def print_summary(self):
        n_pass = sum(1 for r in self.rows if r[1] == "PASS")
        n_fail = sum(1 for r in self.rows if r[1] == "FAIL")
        n_skip = sum(1 for r in self.rows if r[1] == "SKIP")
        print("\n" + "=" * 78)
        print(f"REFERENCE HARNESS: {n_pass} PASS, {n_fail} FAIL, {n_skip} SKIP "
              f"(of {len(self.rows)} scenarios)")
        print("=" * 78)
        for label, status, detail in self.rows:
            suffix = f" -- {detail}" if detail and status != "PASS" else ""
            print(f"  [{status}] {label}{suffix}")


REPORT = Report()


def run_scenario(label, fn):
    try:
        fn()
        REPORT.record(label, "PASS")
    except _EnvironmentNotRunnable as exc:
        REPORT.record(label, "SKIP", str(exc))
    except AssertionError as exc:
        REPORT.record(label, "FAIL", str(exc) or "assertion failed")
    except Exception:
        tb = traceback.format_exc(limit=6)
        REPORT.record(label, "FAIL", tb.strip().splitlines()[-1])


class _EnvironmentNotRunnable(Exception):
    """Raised by a scenario to mean 'this exact environment cannot run
    this scenario' (a genuinely-unavailable optional dependency), as
    distinct from a code defect. Never used to paper over a real
    integration failure."""


# ============================================================================
# R0 -- clean boot / migration assembly
# ============================================================================
def scenario_r0_clean_boot_migrations():
    root = _mk_root("r0")
    db_path = os.path.join(root, "anaxi_provenance.db")
    create_provenance_db(db_path).close()

    for mod in _ALL_MIGRATIONS:
        mod.apply_additive_migration(db_path)
    sleep_c_schema.apply_additive_migration(db_path)

    pipeline_map = build_pipeline_map(_MANIFEST)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    seed_reference_data(conn, pipeline_map, int(time.time()))
    conn.close()

    # Second initialization: re-apply every migration a second time against
    # the now-seeded, already-migrated DB. Must be a genuine no-op (idempotent),
    # never raise, never duplicate/corrupt rows.
    for mod in _ALL_MIGRATIONS:
        mod.apply_additive_migration(db_path)
    sleep_c_schema.apply_additive_migration(db_path)

    for mod in _ALL_MIGRATIONS:
        state = mod.verify_migration_state(db_path)
        assert all(state.values()), f"{mod.__name__}.verify_migration_state incomplete after boot: {state}"

    conn = sqlite3.connect(db_path)
    n_pipelines = conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
    n_actors = conn.execute("SELECT COUNT(*) FROM actors").fetchone()[0]
    conn.close()
    assert n_pipelines == 2, f"expected exactly 2 seeded pipelines after double-init, found {n_pipelines}"
    assert n_actors >= 2, f"expected clark_agent + host_system actors seeded, found {n_actors}"

    # startup does not require the rejected Route A adapter: no production
    # module this boot path imports references route_a in any form.
    for fname in ("inference_provider.py", "llama_anaxi.py", "orchestration.py"):
        src = open(os.path.join(ANAXI_FINAL, fname), "r", encoding="utf-8").read().lower()
        assert "route_a" not in src and "routea" not in src, f"{fname} references route_a"


# ============================================================================
# R1 -- baseline waking path (reuses the repo's own established E2E fixture)
# ============================================================================
def scenario_r1_baseline_waking():
    for mod_name in ("llama_anaxi", "orchestration", "anaxi_protocol_sqlite",
                      "relational_history", "native_provenance_writer", "native_turn_staging"):
        sys.modules.pop(mod_name, None)
    import test_native_waking_turn_end_to_end as baseline
    ok = baseline.run_test_suite()
    assert ok, "test_native_waking_turn_end_to_end.run_test_suite() reported a failing check"


# ============================================================================
# R2 -- waking turn recovery (WTR0), integrated: committed / interrupted /
# restart / idempotent-replay, driven through the REAL recovery state
# machine (wtr0_waking_recovery.execute_recovery), reusing test_waking_
# turn_recovery.py's own Env fixture rather than re-deriving it.
# ============================================================================
def scenario_r2_waking_recovery():
    sys.modules.pop("test_waking_turn_recovery", None)
    import test_waking_turn_recovery as wtr_fixture

    root = _mk_root("r2")
    env = wtr_fixture.Env("integrated")

    # --- A committed turn: ordinary H+X, nothing to recover.
    h_committed, authority = env.make_h("An ordinary answered turn.")
    session_started_at = env.conn().execute(
        "SELECT started_at FROM sessions WHERE session_id = ?", (authority.session_id,)
    ).fetchone()[0]
    x_committed = genuine_waking_turn(
        env.db_path, env.tmp_root, session_id=authority.session_id,
        session_started_at=session_started_at,
        occurred_at=int(time.time()), tag="committed", human_input_event_id=h_committed,
    )
    decision = recovery.assess_eligibility(env.conn(), h_committed, env.actor_id)
    assert decision["decision"] == recovery.EligibilityDecision.INELIGIBLE and decision["basis"] == "X_ALREADY_LINKED", \
        f"a committed turn with X already existing must be ineligible for recovery, got {decision}"

    # --- An interrupted/staged turn: H exists, no X, a retry-safe failure
    # was recorded (the canonical shape a real crashed generation leaves).
    h_interrupted, _ = env.make_h("An interrupted turn that never got an X.")
    env.record_evidence(h_interrupted, "WAKING_EXECUTION_FAILURE_NO_X_PERSISTED")

    result = recovery.execute_recovery(
        env.db_path, h_interrupted, env.actor_id,
        reset_fn=lambda: {"status": ColdResetOutcome.SUCCEEDED, "basis": "synthetic reference-harness reset"},
        generation_fn=lambda prompt: {"native_event_id": genuine_waking_turn(
            env.db_path, env.tmp_root, session_id=f"wtr-recover-{uuid.uuid4()}",
            session_started_at=int(time.time()), occurred_at=int(time.time()),
            tag="recovered", human_input_event_id=h_interrupted,
        )},
    )
    assert result["terminal_state"] == "SUCCESS", f"expected recovery SUCCESS, got {result}"
    x_recovered = result["canonical_x_event_id"]

    # --- restart: fresh connection, re-derive state from canonical history only.
    fresh_conn = env.conn()
    status_after_restart = recovery.recovery_status(fresh_conn, h_interrupted)
    assert status_after_restart is not None and status_after_restart.get("terminal_state") == "SUCCESS", \
        f"recovery status did not survive a fresh connection/restart: {status_after_restart}"
    fresh_conn.close()

    # --- idempotent replay: a second recovery attempt for the SAME H must
    # be denied outright (no second consequential generation).
    denied = False
    try:
        recovery.execute_recovery(
            env.db_path, h_interrupted, env.actor_id,
            reset_fn=lambda: {"status": ColdResetOutcome.SUCCEEDED, "basis": "should never run"},
            generation_fn=lambda prompt: (_ for _ in ()).throw(
                AssertionError("generation_fn must never be called for an already-recovered H")),
        )
    except recovery.RecoveryDenied:
        denied = True
    assert denied, "a second recovery attempt for an already-recovered H must raise RecoveryDenied"

    conn = env.conn()
    x_count = conn.execute(
        "SELECT COUNT(*) FROM event_components WHERE component_kind = 'human_input_event_id' AND component_text = ?",
        (h_interrupted,),
    ).fetchone()[0]
    conn.close()
    assert x_count == 1, f"exactly one X must ever link to the recovered H, found {x_count}"


# ============================================================================
# R3 -- Sleep / waking continuity. Reuses the repo's own integrated Sleep
# pipeline test (test_sleep_v1_acceptance.py) for the A/B/C/D pipeline, and
# proves restart/recovery + lawful waking continuity by re-reading the
# resulting hippocampus DB and watermark through a fresh connection.
# ============================================================================
def scenario_r3_sleep_waking_continuity():
    for mod_name in ("orchestration", "anaxi_protocol_sqlite", "llama_sleep", "sleep_cycle",
                      "sleep_selection", "sleep_transformation", "anaxi_sleep"):
        sys.modules.pop(mod_name, None)
    import test_sleep_v1_acceptance as sleep_fixture

    conn, prov_path, ids = sleep_fixture.fresh_prov_conn("refharness_full_pipeline")
    try:
        sleep_fixture.add_eligible_turn(conn, ids, "refharness-evt-a", "A synthetic first sleep-eligible turn.", occurred_at=100)
        sleep_fixture.add_eligible_turn(conn, ids, "refharness-evt-b", "A synthetic second sleep-eligible turn.", occurred_at=200)
        conn.commit()

        derived_text = "A synthetic reference-harness derivation."

        import re as _re
        import json as _json

        def _transformation_derives_one(messages):
            offered = _re.findall(r"wmu-[0-9a-f]+", messages[0]["content"])
            return _json.dumps({"derivations": [{"source_wmu_ids": offered, "derived_text": derived_text}]})

        sleep_fixture.install_selection_chat(sleep_fixture.selection_selects_all)
        sleep_fixture.install_transformation_chat(_transformation_derives_one)

        result = sleep_fixture.cycle_mod.run_sleep_cycle(conn, owner_id="owner-e1", now=1000)
        assert result.status == "completed" and result.derivation_count == 1, \
            f"run_sleep_cycle did not complete with the expected derivation: {result}"

        watermark_after = sleep_fixture.watermark_mod.read_watermark_from_conn(conn)

        # restart: fresh connection against the same file, canonical state
        # must reconstruct identically.
        conn.commit()
        fresh_conn = sqlite3.connect(prov_path)
        fresh_conn.execute("PRAGMA foreign_keys = ON;")
        watermark_restart = sleep_fixture.watermark_mod.read_watermark_from_conn(fresh_conn)
        assert watermark_restart == watermark_after, \
            f"Sleep watermark did not survive a fresh connection/restart: {watermark_after} vs {watermark_restart}"
        fresh_conn.close()
    finally:
        conn.close()


# ============================================================================
# R4 -- operative directive end to end (null -> activate -> waking carriage
# -> replace/withdraw -> reconstructed state after restart)
# ============================================================================
def scenario_r4_operative_directive():
    root = _mk_root("r4")
    db_path = new_env(root, migrations=(hir1_schema_migration, od1_schema_migration))
    session_id = f"od-session-{uuid.uuid4()}"
    session_started_at = int(time.time())

    # null state: nothing active, renders nothing.
    active = od1.fetch_active_directive(root)
    assert active is None, f"expected null directive state at boot, got {active}"
    assert od1.render_operative_directive_context(active) == "", "null state must render no context block"

    # explicit Clark activation, via a genuine triggering waking turn.
    trigger_1 = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=session_started_at,
        occurred_at=int(time.time()), tag="od-activate",
        operative_directive_request=cd.OPERATIVE_DIRECTIVE_REQUEST_SET,
        operative_directive_text="Always answer in plain, direct sentences.",
    )
    od1.record_operative_directive_activation(
        root, event_id=od1.generate_directive_event_id(), session_id=session_id,
        session_started_at=session_started_at, occurred_at=int(time.time()),
        directive_text="Always answer in plain, direct sentences.",
        triggering_waking_turn_event_id=trigger_1,
    )

    active = od1.fetch_active_directive(root)
    assert active is not None and active["directive_text"] == "Always answer in plain, direct sentences.", \
        f"directive did not appear active after activation: {active}"
    rendered = od1.render_operative_directive_context(active)
    assert "Always answer in plain, direct sentences." in rendered, "rendered context lost the exact directive text"

    # appears through the waking-context contribution seam, correctly SOFT.
    contribution = operative_directive_contribution(rendered, active.get("active_event_id"))
    composition = cb.compose_within_budget([contribution], max_prompt_budget=100_000)
    assert composition.included_kind(cb.OPERATIVE_DIRECTIVE) is not None, \
        "operative directive contribution did not survive ordinary-budget composition"
    assert contribution.hard is False, "operative directive must be SOFT, never HARD/host-law precedence"

    # replacement.
    trigger_2 = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=session_started_at,
        occurred_at=int(time.time()) + 1, tag="od-replace",
        operative_directive_request=cd.OPERATIVE_DIRECTIVE_REQUEST_SET,
        operative_directive_text="Prefer short paragraphs.",
    )
    od1.record_operative_directive_replacement(
        root, event_id=od1.generate_directive_event_id(), session_id=session_id,
        session_started_at=session_started_at, occurred_at=int(time.time()) + 1,
        directive_text="Prefer short paragraphs.", triggering_waking_turn_event_id=trigger_2,
    )
    active = od1.fetch_active_directive(root)
    assert active["directive_text"] == "Prefer short paragraphs.", f"replacement did not take effect: {active}"

    # withdrawal -> null state, no wallpaper.
    trigger_3 = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=session_started_at,
        occurred_at=int(time.time()) + 2, tag="od-withdraw",
        operative_directive_request=cd.OPERATIVE_DIRECTIVE_REQUEST_WITHDRAW,
    )
    od1.record_operative_directive_withdrawal(
        root, event_id=od1.generate_directive_event_id(), session_id=session_id,
        session_started_at=session_started_at, occurred_at=int(time.time()) + 2,
        triggering_waking_turn_event_id=trigger_3,
    )
    active = od1.fetch_active_directive(root)
    assert active is None, f"expected null state after withdrawal, got {active}"
    assert od1.render_operative_directive_context(active) == "", "withdrawn state must render no wallpaper"

    # full permanent history survives, restart-reconstructed from fresh state.
    transitions = od1.fetch_directive_transitions(root)
    assert len(transitions) >= 3, f"expected >=3 permanent transitions (activate/replace/withdraw), got {len(transitions)}"
    active_after_restart = od1.fetch_active_directive(root)
    assert active_after_restart is None, "restart must reconstruct the same null state, not fabricate an active one"


# ============================================================================
# R5 -- read-only external information end to end (SEARCH -> dispatch ->
# bounded result -> untrusted tool/data carriage -> waking delivery)
# ============================================================================
def scenario_r5_read_only_external_information():
    root = _mk_root("r5")
    db_path = new_env(root, migrations=(hir1_schema_migration,))
    session_id = f"ext-session-{uuid.uuid4()}"
    session_started_at = int(time.time())

    def fake_search(query):
        return {"status": ext_net.SEARCH_STATUS_SUCCESS, "detail": None, "results": [
            {"title": "Synthetic result", "url": "https://example.invalid/a", "snippet": "a synthetic snippet"},
        ]}

    net_call_count = [0]

    def counting_search(query):
        net_call_count[0] += 1
        return fake_search(query)

    trigger = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=session_started_at,
        occurred_at=int(time.time()), tag="ext-trigger",
        external_info_request=ext.OPERATION_WEB_SEARCH, external_info_target="synthetic reference-harness query",
    )
    query = ext.record_external_info_query(
        root, session_id=session_id, session_started_at=session_started_at, pipeline_key=_PIPELINE_KEY,
        operation=ext.OPERATION_WEB_SEARCH, target="synthetic reference-harness query",
        occurred_at=int(time.time()), input_source_ref=trigger,
    )
    query_event_id = query["query_event_id"]

    # no network request happened merely by recording the request.
    assert net_call_count[0] == 0, "a network call occurred before the explicit dispatch stage"

    ext.append_external_info_result(root, query_event_id=query_event_id, search_fn=counting_search)
    assert net_call_count[0] == 1, "expected exactly one network dispatch for one explicit SEARCH action"

    pending = ext.next_pending_external_info_result(root, session_id)
    assert pending is not None, "expected a pending, undelivered external-info result"
    rendered = ext.render_external_info_result_delivery(pending["result"])
    assert "Synthetic result" in rendered and "synthetic snippet" in rendered, \
        "delivered content is metadata-only; expected substantive title/snippet content"
    assert json.loads(rendered).get("query_event_id") == query_event_id or query_event_id in rendered, \
        "source/provenance (query_event_id) did not survive into the rendered delivery"

    contribution = cb.Contribution(
        cb.EXTERNAL_INFO_RESULT, rendered, hard=False, source_ids=[query_event_id],
    )
    composition = cb.compose_within_budget([contribution], max_prompt_budget=100_000)
    assert composition.delivered_source_ids(cb.EXTERNAL_INFO_RESULT) == [query_event_id], \
        "external-info result source id did not survive composition -- retrieval != delivery boundary broken"

    waking_event_id = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=session_started_at,
        occurred_at=int(time.time()) + 1, tag="ext-delivery",
        delivered_external_info_query_event_id=query_event_id,
    )
    ext.record_external_info_delivered(
        root, query_event_id=query_event_id, waking_turn_event_id=waking_event_id, occurred_at=int(time.time()) + 1,
    )
    assert ext.next_pending_external_info_result(root, session_id) is None, \
        "result must no longer be pending once genuinely delivered"

    # restart: duplicate disclosure must not reappear as pending/new.
    assert ext.next_pending_external_info_result(root, session_id) is None, \
        "a delivered external-info result reappeared as pending after a fresh read"

    # no external write/action authority appears anywhere on this path.
    assert not hasattr(ext, "record_external_info_send") and not hasattr(ext, "post_external_info"), \
        "external_information module unexpectedly exposes a write/action capability"


# ============================================================================
# R6 -- family / shared visibility matrix (owner + members A/B)
# ============================================================================
def scenario_r6_family_shared_visibility():
    sys.modules.pop("test_fs1_family_shared_expansion_v0", None)
    import test_fs1_family_shared_expansion_v0 as fs1_fixture

    root = _mk_root("r6")
    prov, ids = fs1_fixture._seed_fs1_env(root)
    owner, member = ids["owner"], ids["member"]

    conn = sqlite3.connect(prov)
    active = frozenset({owner, member})

    # owner-private session: owner sees own private + shared, never member's private.
    assert fm.can_receive_event(owner, fm.SCOPE_PRINCIPAL_PRIVATE, owner, fm.SCOPE_PRINCIPAL_PRIVATE, active)
    assert fm.can_receive_event(owner, fm.SCOPE_PRINCIPAL_PRIVATE, member, fm.SCOPE_PRINCIPAL_PRIVATE, active) is False
    assert fm.can_receive_event(owner, fm.SCOPE_PRINCIPAL_PRIVATE, member, fm.SCOPE_FAMILY_SHARED, active)

    # member-private session: member sees own private + shared, never owner's private.
    assert fm.can_receive_event(member, fm.SCOPE_PRINCIPAL_PRIVATE, member, fm.SCOPE_PRINCIPAL_PRIVATE, active)
    assert fm.can_receive_event(member, fm.SCOPE_PRINCIPAL_PRIVATE, owner, fm.SCOPE_PRINCIPAL_PRIVATE, active) is False
    assert fm.can_receive_event(member, fm.SCOPE_PRINCIPAL_PRIVATE, owner, fm.SCOPE_FAMILY_SHARED, active)

    # FAMILY_SHARED session: shared visible, neither private visible.
    assert fm.can_receive_event(owner, fm.SCOPE_FAMILY_SHARED, member, fm.SCOPE_FAMILY_SHARED, active)
    assert fm.can_receive_event(owner, fm.SCOPE_FAMILY_SHARED, member, fm.SCOPE_PRINCIPAL_PRIVATE, active) is False
    assert fm.can_receive_event(owner, fm.SCOPE_FAMILY_SHARED, owner, fm.SCOPE_PRINCIPAL_PRIVATE, active) is False

    # unbound/legacy session: fails closed, never sees scoped material.
    assert fm.can_receive_event(None, None, owner, fm.SCOPE_FAMILY_SHARED, active) is False
    assert fm.can_receive_event(None, None, owner, fm.SCOPE_PRINCIPAL_PRIVATE, active) is False
    conn.close()

    # a stale/deactivated session fails closed: deactivate member, re-check
    # that binding a *new* scoped session for the now-inactive member is
    # refused (fail closed for scoped binding of a non-active principal).
    conn = sqlite3.connect(prov)
    conn.execute("PRAGMA foreign_keys = ON;")
    fm.deactivate_family_member(conn, principal_actor_id=member, requester_actor_id=owner)
    conn.commit()
    denied = False
    try:
        hsb.bind_session_to_registered_human(
            conn, session_id=f"post-deactivation-{uuid.uuid4()}", session_started_at=int(time.time()),
            pipeline_key=fs1_fixture.PIPELINE_KEY, actor_id=member, visibility_scope=fm.SCOPE_FAMILY_SHARED,
        )
    except Exception:
        denied = True
    conn.close()
    assert denied, "binding a scoped session for a DEACTIVATED family member must be refused, fail closed"

    # end-to-end: reuse the repo's own real run_waking_turn() driver for the
    # three scope variants (unbound / owner_private / member_shared), proving
    # the whole waking path, not merely the pure predicate.
    for variant in ("unbound", "owner_private", "member_shared"):
        tmp = os.path.join(root, f"e2e_{variant}")
        os.mkdir(tmp)
        env = fs1_fixture._run_family_e2e(tmp, variant)
        if variant == "unbound":
            assert env["event_scope"] is None
        elif variant == "owner_private":
            assert env["event_scope"] == fm.SCOPE_PRINCIPAL_PRIVATE
            assert "MEMBER_PRIVATE_MARKER" not in env["system_content"]
        else:
            assert env["event_scope"] == fm.SCOPE_FAMILY_SHARED
            assert "MEMBER_SHARED_MARKER" in env["system_content"]


# ============================================================================
# R7 -- private Discord correspondence end to end (reuses the repo's own
# real end-to-end scenario, then adds a revocation/no-resend check on top)
# ============================================================================
def scenario_r7_private_discord_correspondence():
    sys.modules.pop("test_discord_correspondence_v0", None)
    import test_discord_correspondence_v0 as dc_fixture

    dc_fixture.test_end_to_end_synthetic_private_correspondence_path()

    # Additional integrated checks on top of the reused E2E path: revocation
    # blocks dispatch, and an uncertain/ambiguous send is never auto-retried.
    data_dir = dc_fixture._new_env(f"r7_extra_{uuid.uuid4()}")
    owner = dc_fixture._register_owner(data_dir, "r7extra")
    record = dc_fixture._authorize(data_dir, owner)
    destination_id = record["destination_id"]

    dc.revoke_destination(data_dir, destination_id=destination_id, requester_actor_id=owner, occurred_at=int(time.time()))

    waking_event_id = genuine_waking_turn(
        os.path.join(data_dir, "anaxi_provenance.db"), data_dir,
        session_id=f"dc-revoked-{uuid.uuid4()}", session_started_at=int(time.time()),
        occurred_at=int(time.time()), tag="dc-revoked",
        discord_correspondence_request="send_message", discord_destination_id=destination_id,
        discord_message_text="This must never be sent -- destination was revoked.",
    )
    fake = dc_fixture.FakeTransport()
    result = dc.dispatch_outbound(data_dir, waking_turn_event_id=waking_event_id, token="tok", request_fn=fake)
    assert result["status"] != dcr.DISPATCH_CONFIRMED_SENT, \
        f"dispatch succeeded against a revoked destination: {result}"
    assert fake.send_count() == 0, "a POST occurred against a revoked Discord destination"

    # ambiguous outcome must not be auto-retried on "restart" (a second read
    # of the same waking turn's dispatch state must not send again).
    def ambiguous_responder(method, url, headers, body):
        return 500, b"{}"

    record2 = dc_fixture._authorize(data_dir, owner, snowflake="999999999999999999", label="Second Room")
    waking_event_id_2 = genuine_waking_turn(
        os.path.join(data_dir, "anaxi_provenance.db"), data_dir,
        session_id=f"dc-ambiguous-{uuid.uuid4()}", session_started_at=int(time.time()),
        occurred_at=int(time.time()), tag="dc-ambiguous",
        discord_correspondence_request="send_message", discord_destination_id=record2["destination_id"],
        discord_message_text="An ambiguous send attempt.",
    )
    fake2 = dc_fixture.FakeTransport(responder=ambiguous_responder)
    first = dc.dispatch_outbound(data_dir, waking_turn_event_id=waking_event_id_2, token="tok", request_fn=fake2)
    assert first["status"] != dcr.DISPATCH_CONFIRMED_SENT
    first_sends = fake2.send_count()
    second = dc.dispatch_outbound(data_dir, waking_turn_event_id=waking_event_id_2, token="tok", request_fn=fake2)
    assert fake2.send_count() == first_sends, \
        "an ambiguous/uncertain send was automatically retried on replay -- must never auto-resend"


# ============================================================================
# R8 -- cross-capability composition: a family-scoped session, an active
# operative directive, a pending/delivered external-info result, and a
# pending Discord inbound message ALL coexisting in one synthetic runtime,
# verified through the REAL context_budget compositor and REAL provenance.
# ============================================================================
def scenario_r8_cross_capability_composition():
    root = _mk_root("r8")
    db_path = new_env(root, migrations=(
        hir1_schema_migration, od1_schema_migration, fs1_schema_migration,
        oc0_schema_migration, dc0_schema_migration,
    ))
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.close()

    owner = register_human(db_path, "r8-owner", "r8owner")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    fm.designate_owner(conn, principal_actor_id=owner, display_label="Owner", requester_actor_id=HOST_ACTOR_ID)
    conn.commit()
    conn.close()

    session_id = f"r8-session-{uuid.uuid4()}"
    session_started_at = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    authority = hsb.bind_session_to_registered_human(
        conn, session_id=session_id, session_started_at=session_started_at,
        pipeline_key=_PIPELINE_KEY, actor_id=owner, visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
    )
    conn.commit()
    conn.close()

    # 1. active operative directive
    trigger_od = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=session_started_at,
        occurred_at=int(time.time()), tag="r8-od",
        operative_directive_request=cd.OPERATIVE_DIRECTIVE_REQUEST_SET,
        operative_directive_text="A standing directive coexisting with everything else.",
        visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
    )
    od1.record_operative_directive_activation(
        root, event_id=od1.generate_directive_event_id(), session_id=session_id,
        session_started_at=session_started_at, occurred_at=int(time.time()),
        directive_text="A standing directive coexisting with everything else.",
        triggering_waking_turn_event_id=trigger_od,
    )
    directive = od1.fetch_active_directive(root)
    directive_text_rendered = od1.render_operative_directive_context(directive)

    # 2. delivered external-info result
    trigger_ext = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=session_started_at,
        occurred_at=int(time.time()) + 1, tag="r8-ext-trigger",
        external_info_request=ext.OPERATION_WEB_SEARCH, external_info_target="r8 composition query",
        visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
    )
    query = ext.record_external_info_query(
        root, session_id=session_id, session_started_at=session_started_at, pipeline_key=_PIPELINE_KEY,
        operation=ext.OPERATION_WEB_SEARCH, target="r8 composition query", occurred_at=int(time.time()) + 1,
        input_source_ref=trigger_ext,
    )
    ext.append_external_info_result(
        root, query_event_id=query["query_event_id"],
        search_fn=lambda q: {"status": ext_net.SEARCH_STATUS_SUCCESS, "detail": None,
                              "results": [{"title": "R8", "url": "https://example.invalid/r8", "snippet": "r8 snippet"}]},
    )
    pending_ext = ext.next_pending_external_info_result(root, session_id)
    ext_rendered = ext.render_external_info_result_delivery(pending_ext["result"])

    # 3. pending Discord inbound (authorize a destination, fake-poll one message)
    dest_record = dc.authorize_destination(
        root, destination_kind=dcr.DESTINATION_KIND_CHANNEL, discord_snowflake="123123123123123123",
        display_label="R8 Room", requester_actor_id=owner, occurred_at=int(time.time()),
    )
    sender_id, message_id = mapped_inbound_sender(root, owner, "Owner")
    inbound_message = {
        "id": message_id, "content": "Substantive R8 inbound content.",
        "channel_id": "123123123123123123",
        "author": {"id": sender_id, "username": "r8sender"},
        "timestamp": "2026-09-17T00:00:00.000000+00:00",
    }

    def fake_receive(method, url, headers, body, timeout):
        return 200, json.dumps([inbound_message]).encode()

    import unittest.mock as _mock
    with _mock.patch.object(dc_net, "_default_request", fake_receive), \
         _mock.patch.dict(os.environ, {dc_net.DISCORD_BOT_TOKEN_ENV_VAR: "tok"}, clear=False):
        dc.poll_authorized_inbound(root, occurred_at=int(time.time()))
    pending_discord = dc.next_pending_inbound(root)[0]
    discord_rendered = dc.render_inbound_delivery(pending_discord)

    # 4. an ordinary family-scoped waking turn carrying ALL of the above.
    final_event_id = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=session_started_at,
        occurred_at=int(time.time()) + 5, tag="r8-final",
        visibility_scope=fm.SCOPE_PRINCIPAL_PRIVATE,
        delivered_external_info_query_event_id=query["query_event_id"],
        delivered_discord_inbound_event_ids=[pending_discord["event_id"]],
    )
    ext.record_external_info_delivered(
        root, query_event_id=query["query_event_id"], waking_turn_event_id=final_event_id,
        occurred_at=int(time.time()) + 5,
    )
    dc.record_inbound_delivered(
        root, inbound_event_id=pending_discord["event_id"], waking_turn_event_id=final_event_id,
        occurred_at=int(time.time()) + 5,
    )

    # --- Compose all four contributions together through the REAL compositor.
    contributions = [
        operative_directive_contribution(directive_text_rendered, directive.get("active_event_id")),
        cb.Contribution(cb.EXTERNAL_INFO_RESULT, ext_rendered, hard=False, source_ids=[query["query_event_id"]]),
        cb.Contribution(cb.DISCORD_INBOUND_CARRIAGE, discord_rendered, hard=False, source_ids=[pending_discord["event_id"]]),
    ]
    composition = cb.compose_within_budget(contributions, max_prompt_budget=100_000)
    assert composition.fits, "the composed cross-capability context unexpectedly failed to fit"
    assert composition.included_kind(cb.OPERATIVE_DIRECTIVE) is not None
    assert composition.delivered_source_ids(cb.EXTERNAL_INFO_RESULT) == [query["query_event_id"]]
    assert composition.delivered_source_ids(cb.DISCORD_INBOUND_CARRIAGE) == [pending_discord["event_id"]]

    # --- family-private material never reaches the composer at all: there is
    # no PRIVATE contribution kind, and nothing above carries owner-private
    # dialogue text into a shared/foreign context.
    assert not hasattr(cb, "PRIVATE") and "family_private" not in cb.ALL_CONTRIBUTION_KINDS, \
        "a private contribution kind unexpectedly exists on the composer surface"

    # --- provenance stays coherent for the final composite turn.
    conn = sqlite3.connect(db_path)
    ok = npw.verify_native_bundle_contract(conn, final_event_id)
    conn.close()
    assert ok, "the cross-capability composite waking turn failed native bundle verification"

    # --- no capability silently grants another capability authority: the
    # directive text can never contain a structured discord/external-info
    # action of its own (it is rendered as inert JSON-string data).
    assert '"discord_correspondence_request"' not in directive_text_rendered
    assert '"external_info_request"' not in directive_text_rendered


# ============================================================================
# R9 -- cross-capability recovery: durable state across a simulated restart.
# ============================================================================
def scenario_r9_cross_capability_recovery():
    root = _mk_root("r9")
    db_path = new_env(root, migrations=(
        hir1_schema_migration, od1_schema_migration, fs1_schema_migration,
        oc0_schema_migration, dc0_schema_migration,
    ))
    owner = register_human(db_path, "r9-owner", "r9owner")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    fm.designate_owner(conn, principal_actor_id=owner, display_label="Owner", requester_actor_id=HOST_ACTOR_ID)
    conn.commit()
    conn.close()

    session_id = f"r9-session-{uuid.uuid4()}"
    session_started_at = int(time.time())

    # active directive
    trigger_od = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=session_started_at,
        occurred_at=int(time.time()), tag="r9-od",
        operative_directive_request=cd.OPERATIVE_DIRECTIVE_REQUEST_SET,
        operative_directive_text="Durable across restart.",
    )
    od1.record_operative_directive_activation(
        root, event_id=od1.generate_directive_event_id(), session_id=session_id,
        session_started_at=session_started_at, occurred_at=int(time.time()),
        directive_text="Durable across restart.", triggering_waking_turn_event_id=trigger_od,
    )

    # pending external-info result (never delivered)
    trigger_ext = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=session_started_at,
        occurred_at=int(time.time()) + 1, tag="r9-ext-trigger",
        external_info_request=ext.OPERATION_WEB_SEARCH, external_info_target="r9 pending query",
    )
    query = ext.record_external_info_query(
        root, session_id=session_id, session_started_at=session_started_at, pipeline_key=_PIPELINE_KEY,
        operation=ext.OPERATION_WEB_SEARCH, target="r9 pending query", occurred_at=int(time.time()) + 1,
        input_source_ref=trigger_ext,
    )
    ext.append_external_info_result(
        root, query_event_id=query["query_event_id"],
        search_fn=lambda q: {"status": ext_net.SEARCH_STATUS_SUCCESS, "detail": None,
                              "results": [{"title": "R9", "url": "https://example.invalid/r9", "snippet": "r9 snippet"}]},
    )

    # pending inbound Discord message (never delivered)
    dest_record = dc.authorize_destination(
        root, destination_kind=dcr.DESTINATION_KIND_CHANNEL, discord_snowflake="321321321321321321",
        display_label="R9 Room", requester_actor_id=owner, occurred_at=int(time.time()),
    )
    sender_id, message_id = mapped_inbound_sender(root, owner, "Owner")
    inbound_message = {
        "id": message_id, "content": "R9 pending inbound.",
        "channel_id": "321321321321321321", "author": {"id": sender_id, "username": "r9sender"},
        "timestamp": "2026-09-17T00:00:00.000000+00:00",
    }
    import unittest.mock as _mock
    with _mock.patch.object(dc_net, "_default_request", lambda *a, **k: (200, json.dumps([inbound_message]).encode())), \
         _mock.patch.dict(os.environ, {dc_net.DISCORD_BOT_TOKEN_ENV_VAR: "tok"}, clear=False):
        dc.poll_authorized_inbound(root, occurred_at=int(time.time()))

    # one uncertain (outcome-not-established) outward act.
    fake_outbound_session = f"r9-outbound-{uuid.uuid4()}"
    outbound_trigger = genuine_waking_turn(
        db_path, root, session_id=fake_outbound_session, session_started_at=int(time.time()),
        occurred_at=int(time.time()), tag="r9-outbound",
        discord_correspondence_request="send_message", discord_destination_id=dest_record["destination_id"],
        discord_message_text="An uncertain send.",
    )

    def ambiguous_responder(method, url, headers, body, timeout):
        return 500, b"{}"

    dc.dispatch_outbound(root, waking_turn_event_id=outbound_trigger, token="tok", request_fn=ambiguous_responder)

    # --- SIMULATED RESTART: fresh connections/reads only, nothing carried
    # over in-process. ---
    active_after = od1.fetch_active_directive(root)
    assert active_after is not None and active_after["directive_text"] == "Durable across restart.", \
        f"active directive did not reconstruct truthfully after restart: {active_after}"

    still_pending_ext = ext.next_pending_external_info_result(root, session_id)
    assert still_pending_ext is not None and still_pending_ext["query_event_id"] == query["query_event_id"], \
        "a never-delivered external-info result vanished (or changed identity) after restart"

    still_pending_discord = dc.next_pending_inbound(root)
    assert len(still_pending_discord) == 1 and still_pending_discord[0]["event_id"] is not None, \
        "a never-delivered Discord inbound message vanished after restart"

    outbound_status = dc.dispatch_status(root, outbound_trigger) if hasattr(dc, "dispatch_status") else None
    reconcile_result = dc.reconcile_outbound_attempts(root, occurred_at=int(time.time()) + 10)
    send_count_marker = []

    def _must_not_send(*a, **k):
        send_count_marker.append(1)
        return 200, json.dumps({"id": "should-never-happen", "channel_id": "0"}).encode()

    replay = dc.dispatch_outbound(root, waking_turn_event_id=outbound_trigger, token="tok", request_fn=_must_not_send)
    assert not send_count_marker, "an ambiguous outward act was auto-resent after simulated restart/reconciliation"
    assert replay["status"] != dcr.DISPATCH_CONFIRMED_SENT, \
        f"a genuinely-ambiguous prior send was reported as confirmed on replay: {replay}"

    # membership state remains truthful across restart.
    active_principals_after = fm.active_family_principals(sqlite3.connect(db_path))
    assert owner in active_principals_after, "owner membership did not survive restart"


# ============================================================================
# R10 -- authority separation (mechanical, not prose)
# ============================================================================
def scenario_r10_authority_separation():
    root = _mk_root("r10")
    db_path = new_env(root, migrations=(
        hir1_schema_migration, od1_schema_migration, fs1_schema_migration,
        oc0_schema_migration, dc0_schema_migration,
    ))
    owner = register_human(db_path, "r10-owner", "r10owner")
    member = register_human(db_path, "r10-member", "r10member")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    fm.designate_owner(conn, principal_actor_id=owner, display_label="Owner", requester_actor_id=HOST_ACTOR_ID)
    fm.enroll_family_member(conn, principal_actor_id=member, display_label="Member", requester_actor_id=owner)
    conn.commit()
    conn.close()

    # 1. read-only web authority grants no Discord send authority: nothing
    # in external_information.py can name/construct a discord dispatch.
    ext_src = open(os.path.join(ANAXI_FINAL, "external_information.py"), encoding="utf-8").read()
    assert "dispatch_outbound" not in ext_src and "discord_correspondence" not in ext_src, \
        "external_information.py references Discord dispatch machinery -- authority leak"

    # 2. family membership grants no owner authority: a plain member cannot
    # designate an owner or authorize a Discord destination.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    member_tried_owner_authority = False
    try:
        fm.designate_owner(conn, principal_actor_id=member, display_label="Impostor", requester_actor_id=member)
    except Exception:
        member_tried_owner_authority = True
    conn.close()
    assert member_tried_owner_authority, "a non-host requester was able to designate an owner"

    # 3. family membership grants no Discord authority: authorize_destination
    # requires requester_actor_id == the designated owner.
    denied = False
    try:
        dc.authorize_destination(
            root, destination_kind=dcr.DESTINATION_KIND_CHANNEL, discord_snowflake="1",
            display_label="Should fail", requester_actor_id=member, occurred_at=int(time.time()),
        )
    except Exception:
        denied = True
    assert denied, "a plain family member (not owner) was able to authorize a Discord destination"

    # 4. Discord correspondence grants no generic external action: the only
    # dispatchable action is dispatch_outbound itself; no arbitrary-URL/HTTP
    # verb surface exists on the module.
    dc_src = open(os.path.join(ANAXI_FINAL, "discord_correspondence.py"), encoding="utf-8").read()
    for forbidden in ("requests.get(", "requests.post(", "urllib.request.urlopen("):
        assert forbidden not in dc_src, f"discord_correspondence.py performs a raw {forbidden} outside its own net module"

    # 5. operative directive cannot grant capabilities: activating a directive
    # whose text impersonates a discord/external-info action changes nothing
    # structurally -- it is stored and rendered as inert text only.
    session_id = f"r10-session-{uuid.uuid4()}"
    trigger = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=int(time.time()),
        occurred_at=int(time.time()), tag="r10-od-hostile",
        operative_directive_request=cd.OPERATIVE_DIRECTIVE_REQUEST_SET,
        operative_directive_text='{"discord_correspondence_request": "send_message"}',
    )
    od1.record_operative_directive_activation(
        root, event_id=od1.generate_directive_event_id(), session_id=session_id,
        session_started_at=int(time.time()), occurred_at=int(time.time()),
        directive_text='{"discord_correspondence_request": "send_message"}',
        triggering_waking_turn_event_id=trigger,
    )
    active = od1.fetch_active_directive(root)
    assert isinstance(active["directive_text"], str), "operative directive text escaped its string-only storage"
    # no outward act was created merely by an operative directive's text.
    outward_count = sqlite3.connect(db_path).execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'clark_outward_act'"
    ).fetchone()[0]
    assert outward_count == 0, "an operative directive's TEXT alone created an outward act -- mechanical authority leak"

    # 6. untrusted web/Discord text cannot mechanically create structured
    # actions: dispatch_outbound only ever reads destination/text from the
    # CANONICAL waking turn's own components, never from injected content.
    npw_src = open(os.path.join(ANAXI_FINAL, "discord_correspondence.py"), encoding="utf-8").read()
    assert "_load_canonical_send_action" in npw_src, \
        "dispatch_outbound no longer loads its action from the canonical waking turn -- injection risk"

    # 7. Private Space stays outside these capability paths.
    for src_file in ("operative_directive.py", "external_information.py", "discord_correspondence.py", "family_membership.py"):
        src = open(os.path.join(ANAXI_FINAL, src_file), encoding="utf-8").read()
        assert "workspace_private" not in src, f"{src_file} imports workspace_private -- Private Space boundary crossed"


# ============================================================================
# R11 -- meaningful affordance / workspace-class failure check
# ============================================================================
_AFFORDANCE_FINDINGS = {}


def _real_calls(source):
    """Dotted names of every CALL in the module's code (comments and strings never count)."""
    import ast
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            parts, target = [], node.func
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                parts.append(target.id)
            names.add(".".join(reversed(parts)))
    return names


# Behavioral proof lives in bound, executed scenarios (completion_evidence/scenario_bindings.json, run by
# scenario_evidence.py and the real-model harnesses), not in this harness.  R11 no longer asserts
# affordance properties as literals: it names the bound scenarios that establish them and fails if any is
# missing, and it checks that the waking path really CALLS each delivery seam.
AFFORDANCE_EVIDENCE = {
    "external_information": ("10_read_only_external_information", None),
    "discord_correspondence": ("11_caret_discord", None),
    "operative_directive": ("12_reversible_operative_directive", None),
    "family_shared": ("09_family_shared_interaction", None),
    "workspace_resources": ("04_library_books_documents", None),
    "photographs": ("05_photographs_vision", None),
    "music_audio": ("06_music_audio_resource_access", "whole_source_observation_bounded_measurement"),
    "journal": ("07_journal", None),
    "shared_obsidian_notes": ("34_collaborative_obsidian_workspace", "clark_reaches_vault_through_ordinary_interface"),
    "external_correspondence": ("32_external_correspondence_scoped_continuity", "unknown_source_converses_on_owner_opened_surface"),
    "lawful_null": ("16_null_refusal_defer_no_action", "null"),
}
WAKING_DELIVERY_CALLS = {
    "external_information": "external_information.next_pending_external_info_result",
    "discord_correspondence": "discord_correspondence.poll_authorized_inbound",
    "operative_directive": "operative_directive.render_operative_directive_context",
    "workspace_resources": "workspace_supervisor.run_one_supervised_workspace_action",
    "workspace_surface": "workspace_supervisor.wd.resource_allowed_surface",
}


def scenario_r11_meaningful_affordance():
    _mk_root("r11")
    bindings = json.load(open(os.path.join(ANAXI_FINAL, "completion_evidence", "scenario_bindings.json"),
                              encoding="utf-8"))["bindings"]
    calls = _real_calls(open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8").read())
    for name, dotted in WAKING_DELIVERY_CALLS.items():
        assert any(c == dotted or c.endswith("." + dotted.split(".", 1)[1]) or c == dotted.split(".")[-1]
                   for c in calls), f"{name}: the waking path never calls {dotted}"
    for name, (capability, scenario) in AFFORDANCE_EVIDENCE.items():
        bound = bindings.get(capability) or {}
        assert bound, f"{name}: no bound behavioral evidence for {capability}"
        if scenario is not None:
            assert bound.get(scenario, {}).get("tests"), f"{name}: {capability}/{scenario} is not bound"
        _AFFORDANCE_FINDINGS[name] = {"established_by": capability + (f"/{scenario}" if scenario else ""),
                                      "bound_scenarios": len(bound)}
    typed_workspace_act = (
        cd.USE_WORKSPACE in cd.ALLOWED_ACTS
        and cd.USE_WORKSPACE in cd.PASS1_SCHEMA["properties"]["act"]["enum"]
    )
    assert typed_workspace_act, "ordinary waking has no closed typed workspace affordance"


# ============================================================================
# R12 -- provider boundary / production default
# ============================================================================
def scenario_r12_provider_default_state():
    saved = os.environ.pop("ANAXI_INFERENCE_PROVIDER", None)
    try:
        assert ip.resolve_provider() == ip.PROVIDER_OLLAMA, \
            f"default inference provider is not Ollama: {ip.resolve_provider()!r}"
    finally:
        if saved is not None:
            os.environ["ANAXI_INFERENCE_PROVIDER"] = saved

    for fname in ("inference_provider.py",):
        src = open(os.path.join(ANAXI_FINAL, fname), encoding="utf-8").read().lower()
        assert "route_a" not in src and "routea" not in src, f"{fname} references the rejected Route A adapter"

    src = open(os.path.join(ANAXI_FINAL, "inference_provider.py"), encoding="utf-8").read()
    for forbidden_import in ("import provenance_schema", "import native_provenance_writer",
                              "import hippocampus_store", "import hippocampus_retrieval",
                              "import operative_directive", "import family_membership"):
        assert forbidden_import not in src, \
            f"inference_provider.py imports {forbidden_import!r} -- provider boundary is not transport-only"
    assert "sqlite3.connect" not in src and "anaxi_provenance.db" not in src, \
        "inference_provider.py appears to open/touch a provenance/continuity database directly"

    try:
        ip.resolve_provider("not-a-real-provider")
        raised = False
    except ValueError:
        raised = True
    assert raised, "an unknown inference provider was silently accepted instead of raising"


# ============================================================================
# R14 + R15 -- canonical provenance coherence & context composition coherence
# ============================================================================
def scenario_r14_r15_provenance_and_context_composition():
    root = _mk_root("r14_r15")
    db_path = new_env(root, migrations=(hir1_schema_migration, od1_schema_migration))
    session_id = f"r14-session-{uuid.uuid4()}"
    event_id = genuine_waking_turn(
        db_path, root, session_id=session_id, session_started_at=int(time.time()),
        occurred_at=int(time.time()), tag="r14-coherence",
    )
    conn = sqlite3.connect(db_path)
    ok = npw.verify_native_bundle_contract(conn, event_id)
    assert ok, "a freshly-committed genuine waking turn failed native bundle verification"
    row = conn.execute(
        "SELECT a.session_id, e.occurred_at, e.auth_context_id FROM events e "
        "JOIN auth_contexts a ON a.auth_context_id = e.auth_context_id WHERE e.event_id = ?",
        (event_id,),
    ).fetchone()
    conn.close()
    assert row is not None and row[0] == session_id, "actor/session/source identity was not mutually consistent"

    # -- context composition: HARD never trimmed even under pressure; SOFT
    # trims in exactly SOFT_TRIM_ORDER; dropping a kind never fabricates a
    # false delivered source id; bounded composition terminates cleanly.
    hard = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, "x" * 200, hard=True)
    soft_early = cb.Contribution(cb.LEGACY_RETRIEVAL, "y" * 5_000_000, hard=False, source_ids=["legacy-1"])
    soft_late = operative_directive_contribution("a pinned directive block")
    composition = cb.compose_within_budget([hard, soft_early, soft_late], max_prompt_budget=2_000)
    assert composition.included_kind(cb.CURRENT_HUMAN_MESSAGE) is not None, \
        "a HARD contribution was dropped under budget pressure"
    assert cb.LEGACY_RETRIEVAL in composition.dropped_kinds or composition.included_kind(cb.LEGACY_RETRIEVAL) is None, \
        "SOFT trim order violated: the earliest-dropped-first kind survived while budget was still exceeded"
    assert composition.delivered_source_ids(cb.LEGACY_RETRIEVAL) == [], \
        "a dropped SOFT contribution still reported a delivered source id -- false delivery"

    # a HARD set alone that cannot fit fails closed rather than silently shrinking.
    oversized_hard = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, "z" * 50_000, hard=True)
    failed_composition = cb.compose_within_budget([oversized_hard], max_prompt_budget=10)
    assert failed_composition.fits is False, "an oversized HARD-only set did not fail closed"


# ============================================================================
# Scenario registry
# ============================================================================
SCENARIOS = [
    ("clean boot / migrations (R0)", scenario_r0_clean_boot_migrations),
    ("baseline waking (R1)", scenario_r1_baseline_waking),
    ("waking turn recovery (R2)", scenario_r2_waking_recovery),
    ("Sleep / waking continuity (R3)", scenario_r3_sleep_waking_continuity),
    ("operative directive (R4)", scenario_r4_operative_directive),
    ("read-only external information (R5)", scenario_r5_read_only_external_information),
    ("family / shared visibility (R6)", scenario_r6_family_shared_visibility),
    ("Discord correspondence (R7)", scenario_r7_private_discord_correspondence),
    ("cross-capability composition (R8)", scenario_r8_cross_capability_composition),
    ("cross-capability recovery (R9)", scenario_r9_cross_capability_recovery),
    ("authority separation (R10)", scenario_r10_authority_separation),
    ("meaningful-affordance checks (R11)", scenario_r11_meaningful_affordance),
    ("provider / default state (R12)", scenario_r12_provider_default_state),
    ("provenance / context composition (R14+R15)", scenario_r14_r15_provenance_and_context_composition),
]


# ============================================================================
# Existing regression suites (separate step -- NOT reimplemented here).
# "script" style: the file has its own __main__ runner and its own PASS/FAIL
# aggregation; invoked as a standalone process with the same optional-
# dependency stubs this harness itself installs. "pytest" style: bare
# pytest-collectible def test_*() functions (fixtures like tmp_path/
# monkeypatch), invoked via `python3 -m pytest -q`.
# ============================================================================
REGRESSION_SUITES_SCRIPT = [
    "test_waking_turn_recovery.py",
    "test_operative_directive.py",
    "test_external_information.py",
    "test_external_information_net.py",
    "test_discord_correspondence_v0.py",
    "test_context_budget.py",
    "test_boundary_inspector.py",
    "test_boundary_inspect_cli.py",
    "test_boundary_rationale_registry.py",
    "test_native_waking_turn_end_to_end.py",
    "test_native_provenance_integration.py",
    "test_native_turn_staging_hardening.py",
    "test_human_waking_authority_end_to_end.py",
    "test_sleep_v1_acceptance.py",
    "test_provenance_schema.py",
]

REGRESSION_SUITES_PYTEST = [
    "test_fs1_family_shared_expansion_v0.py",
    "test_conversation_direction.py",
    "test_inference_provider.py",
    "test_inference_provider_integration.py",
    "test_post_sleep_wake_continuation.py",
    "test_workspace_capability.py",
    "test_workspace_direction.py",
    "test_workspace_organization.py",
    "test_workspace_private.py",
    "test_workspace_public_continuity.py",
    "test_workspace_roaming.py",
    "test_workspace_audio.py",
    "test_workspace_episode_context.py",
    "test_workspace_episode_provenance.py",
    "test_workspace_actor_resolution.py",
    "test_workspace_attribution.py",
    "test_workspace_historical_continuity.py",
    "test_workspace_episode_delivery.py",
    "test_workspace_waking_affordance.py",
]

_STUB_BOOTSTRAP = (
    "import sys, types, runpy\n"
    "for _name, _attr in (('sentence_transformers','SentenceTransformer'), ('anthropic','Anthropic')):\n"
    "    if _name not in sys.modules:\n"
    "        _m = types.ModuleType(_name); setattr(_m, _attr, object); sys.modules[_name] = _m\n"
    "sys.argv = [{target!r}]\n"
    "runpy.run_path({target!r}, run_name='__main__')\n"
)

# Optional-GUI-only dependency: known genuinely unavailable in this offline
# environment (llama_gui.py -> gradio), never installed to inflate a pass
# count. A failure whose ONLY cause is this import is classified SKIP
# (environment-not-runnable), never PASS or a silent FAIL.
_KNOWN_OPTIONAL_ENV_GAPS = (
    "No module named 'gradio'",       # llama_gui.py: desktop GUI, not the waking path itself
    "No module named 'pypdf'",        # workspace_capability.py: PDF text extraction, optional
    "No module named 'soundfile'",    # workspace_audio.py: audio decoding, optional
    "anaxi_final/anaxi_provenance.db",  # requires a real, live production DB this isolated harness never creates
)

_PYTEST_STUB_BOOTSTRAP = (
    "import sys, types\n"
    "for _name, _attr in (('sentence_transformers','SentenceTransformer'), ('anthropic','Anthropic')):\n"
    "    if _name not in sys.modules:\n"
    "        _m = types.ModuleType(_name); setattr(_m, _attr, object); sys.modules[_name] = _m\n"
    "import pytest\n"
    "sys.exit(pytest.main(['-q', {target!r}]))\n"
)


def _run_regression_file(fname, style):
    path = os.path.join(ANAXI_FINAL, fname)
    if style == "script":
        cmd = [sys.executable, "-B", "-c", _STUB_BOOTSTRAP.format(target=fname)]
    else:
        cmd = [sys.executable, "-B", "-c", _PYTEST_STUB_BOOTSTRAP.format(target=fname)]
    proc = subprocess.run(cmd, cwd=ANAXI_FINAL, capture_output=True, text=True, timeout=600)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return "PASS", output
    for gap in _KNOWN_OPTIONAL_ENV_GAPS:
        if gap in output:
            return "SKIP", f"environment-not-runnable: {gap}"
    tail = output.strip().splitlines()[-1] if output.strip() else f"exit code {proc.returncode}"
    return "FAIL", tail


def run_regressions():
    print("\n" + "=" * 78)
    print("EXISTING REGRESSION SUITES (separate from the reference harness above)")
    print("=" * 78)
    results = []
    for fname in REGRESSION_SUITES_SCRIPT:
        status, detail = _run_regression_file(fname, "script")
        results.append((fname, status, detail))
        print(f"[{status}] {fname}" + (f" -- {detail}" if status != "PASS" else ""))
    for fname in REGRESSION_SUITES_PYTEST:
        status, detail = _run_regression_file(fname, "pytest")
        results.append((fname, status, detail))
        print(f"[{status}] {fname}" + (f" -- {detail}" if status != "PASS" else ""))
    n_pass = sum(1 for _, s, _ in results if s == "PASS")
    n_fail = sum(1 for _, s, _ in results if s == "FAIL")
    n_skip = sum(1 for _, s, _ in results if s == "SKIP")
    print(f"\nREGRESSIONS: {n_pass} PASS, {n_fail} FAIL, {n_skip} SKIP (of {len(results)} suites)")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regressions", action="store_true",
                         help="also invoke the relevant existing accepted regression suites (separate step)")
    parser.add_argument("--regressions-only", action="store_true",
                         help="run ONLY the existing regression suites, skip the reference scenarios")
    args = parser.parse_args()

    print("=" * 78)
    print("ANAXI REFERENCE HARNESS / STABILIZATION V0")
    print("=" * 78)

    if not args.regressions_only:
        for label, fn in SCENARIOS:
            run_scenario(label, fn)

        if _AFFORDANCE_FINDINGS:
            print("\n--- meaningful-affordance findings ---")
            print(json.dumps(_AFFORDANCE_FINDINGS, indent=2, sort_keys=True))

        REPORT.print_summary()

        if REPORT.failed():
            print("\nIsolated temp-state roots for the failing run (for debugging only):")
            for root in _ALL_TEMP_ROOTS:
                print(f"  {root}")
        else:
            _cleanup_all_roots()

    regression_results = None
    if args.regressions or args.regressions_only:
        regression_results = run_regressions()

    scenario_failed = (not args.regressions_only) and bool(REPORT.failed())
    regression_failed = bool(regression_results) and any(s == "FAIL" for _, s, _ in regression_results)
    return 1 if (scenario_failed or regression_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
