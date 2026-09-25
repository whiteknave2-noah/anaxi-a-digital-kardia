"""Post-completion waking-budget focused regression suite.

Root cause reproduced here: llama_anaxi.py's Pass-1 mechanical-state
contribution (context_budget.MECHANICAL_STATE, HARD -- never trimmed)
unconditionally concatenated six full prose "affordance" paragraphs
(sleep timing, boundary inquiry, operative directive, external info,
Discord correspondence, workspace action) every single turn, each one
re-explaining its own field/enum contract in sentences rather than
naming it once. In a final-integration turn with every capability
actually available (an authorized Discord destination, the default
unbound/owner-workspace-available path, and the always-present
sleep/boundary/operative/external affordances), CORE_SYSTEM_CONTROL
(693, calibrated) plus MECHANICAL_STATE alone already exceeded
PASS1_MAX_PROMPT_BUDGET (3456) -- BUDGET_EXCEEDED before the current
human message was even added, and before any model call.

PASS1-BUDGET-2 (conversation_direction.render_pass1_action_menu(),
wired in by llama_anaxi.py) replaces concatenating six standalone,
independently-boilerplated affordance blocks with ONE compact menu:
the shared "optional/defaults to none/never inferred from prose" fact
is stated once instead of six times, and each capability's free-
string payload-field name (e.g. `discord_destination_id`) is no
longer repeated in prose because it is NOT new information in this
request -- PASS1_SCHEMA's own `properties` dict, passed to the exact
same model call as `format=structured_schema`, already names every
one of those fields and already enforces every closed enum as a real
generation-time grammar constraint. Nothing becomes mechanically
hidden: every field remains selectable and every enum remains enforced.

The final repair deliberately does not claim that the historical
1584-byte H fits the conservative production bound. It establishes a
safe fresh-message envelope instead: topic labels are mechanically
bounded to the 200 characters the existing generation reserve already
assumed; the full space-capability grounding remains charged at its
175-byte conservative cost; and a 400-byte fresh message fits the
measured production-equivalent largest lawful Pass-1/Pass-2 hard state
with positive headroom.

Zero real Ollama/model calls -- reuses test_owc5_s2_integration.py's
established fake-ollama/fake-orchestration harness. A real, synthetic
provenance DB is created in the harness's own temp dir so
discord_correspondence.list_authorized_destinations() sees a genuine
owner-authorized destination, exactly like production.

Run: python3 -B anaxi_final/test_pass1_budget_bugfix_v0.py
"""
import os
import json
import sqlite3
import sys
import time
import traceback
from unittest.mock import patch

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import test_owc5_s2_integration as harness

from provenance_schema import create_provenance_db
from migrate_historical_data import build_pipeline_map, seed_reference_data
import hir1_schema_migration
import dc0_schema_migration
from hir1_registration import register_canonical_human
import discord_correspondence as dc
import discord_correspondence_registry as dcr
import conversation_direction as cd
import context_budget

_MANIFEST = {"pipelines": {
    "llama": {"routing_constant_value": "nate"},
    "claude": {"routing_constant_value": "nate"},
}}
DESTINATION_LABEL = "Family Room"
DESTINATION_SNOWFLAKE = "123456789012345678"

# A representative first-wake human message -- ordinary conversational
# prose, not a synthetic worst-case string, long enough to be a real
# multi-sentence greeting/check-in.
REPRESENTATIVE_FIRST_WAKE_MESSAGE = (
    "Hey, it's Alex. This is the first time I'm properly waking you up -- "
    "I wanted to check in, see how you're doing, and talk through a few "
    "things about how we should get started together now that everything "
    "is wired up. No pressure to have a whole plan -- just curious what's "
    "on your mind and whether anything about this setup feels off to you."
)


def _provision_authorized_discord_destination(data_dir):
    """Builds a real, synthetic provenance DB (never production) in
    `data_dir` with one canonical registered owner and one owner-
    authorized Discord destination -- exactly the shape
    discord_correspondence.list_authorized_destinations() reads in
    production."""
    db_path = os.path.join(data_dir, "anaxi_provenance.db")
    create_provenance_db(db_path).close()
    pipeline_map = build_pipeline_map(_MANIFEST)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    seed_reference_data(conn, pipeline_map, int(time.time()))
    conn.close()
    hir1_schema_migration.apply_additive_migration(db_path)
    dc0_schema_migration.apply_additive_migration(db_path)

    reg_conn = sqlite3.connect(db_path)
    reg_conn.row_factory = sqlite3.Row
    reg_conn.execute("PRAGMA foreign_keys = ON;")
    reg_result, reg_failure = register_canonical_human(reg_conn, {
        "registration_request_id": "pass1-budget-bugfix-owner",
        "aab_actor_id": "actor-pass1-budget-bugfix-owner",
        "display_label": "PASS1-BUDGET-1 Test Owner",
        "source": "local_operator_provisioning",
    })
    reg_conn.close()
    assert reg_failure is None, reg_failure
    owner = reg_result["actor_id"]

    with patch.object(dc.time, "time", return_value=0):
        record = dc.authorize_destination(
            data_dir, destination_kind=dcr.DESTINATION_KIND_CHANNEL,
            discord_snowflake=DESTINATION_SNOWFLAKE,
            display_label=DESTINATION_LABEL, requester_actor_id=owner, occurred_at=0,
        )
    return record


def _fresh_fully_capable_environment(pass1_value=None, pass2_value=None):
    """A fresh llama_anaxi with EVERY Pass-1-visible capability actually
    present: an authorized Discord destination (Discord correspondence
    affordance), the default unbound/legacy path (owner workspace
    affordance available, ordinary/ ""family/session binding"" without
    FS1 ever being engaged), and the always-unconditional sleep-timing/
    boundary-inquiry/operative-directive/external-info affordances."""
    la, call_log, p1, p2, persistence = harness.fresh_llama_anaxi(
        pass1_value=pass1_value or {
            "act": "develop_current", "thread": "", "direction_request": "none",
            "relinquish_direction": False,
        },
        pass2_value=pass2_value or {"expression": "An ordinary reply."},
    )
    destination = _provision_authorized_discord_destination(la.PROVENANCE_DB_DIR)
    return la, call_log, p1, p2, persistence, destination


def _pass1_prompt_text(call_log):
    """The exact text sent to the model for the Pass-1 (format='json')
    call -- system + user content concatenated, mirroring how the model
    itself reads the two messages."""
    for call in call_log:
        if harness._is_json_format(call["format"]):
            return "\n".join(m["content"] for m in call["messages"])
    raise AssertionError("no Pass-1 call was ever sent")


# ------------------------------------------------------------- 1: the fix --


def test_full_capability_turn_with_representative_message_fits_and_reaches_model():
    # THE regression: before PASS1-BUDGET-1, this exact scenario raised
    # ConversationDirectionFailure("pass1", "BUDGET_EXCEEDED") --
    # core_system_control (693, calibrated) + mechanical_state alone
    # already overran PASS1_MAX_PROMPT_BUDGET (3456) with every
    # capability present, before the human message was even added.
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    result = la.run_waking_turn(
        la.AnaxiOrchestrator(), REPRESENTATIVE_FIRST_WAKE_MESSAGE, interaction_mode="conversation",
    )
    assert result["reply"] == "An ordinary reply."
    pass1_calls = [c for c in call_log if harness._is_json_format(c["format"])]
    assert len(pass1_calls) == 1


def _message_of_exact_byte_length(seed_sentence, target_bytes):
    """Builds ordinary repeated conversational prose, then truncates on
    a UTF-8 boundary to EXACTLY `target_bytes`, without hand-counting
    characters."""
    text = ""
    while len(text.encode("utf-8")) < target_bytes:
        text += seed_sentence
    encoded = text.encode("utf-8")[:target_bytes]
    while True:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]  # back off one byte off a split multi-byte char


# The owner-facing fresh-message maximum established by the final
# production-equivalent accounting.  It must pass without relying on a
# live token-measurement recovery probe.
SAFE_FRESH_MESSAGE = _message_of_exact_byte_length(
    "Hey, it's Alex. This is the first time I'm properly waking you up -- I wanted to check in, "
    "see how you're doing, and talk through what we should start with. ",
    400,
)
assert len(SAFE_FRESH_MESSAGE.encode("utf-8")) == 400


def test_safe_fresh_message_pass1_composition_fits_with_headroom():
    # A 400-byte fresh message on top of a fully-loaded
    # mechanical state (Discord destination authorized, workspace
    # available, sleep/boundary/operative/external affordances present).
    # It must fit with residual headroom without a measurement probe.
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    la.run_waking_turn(la.AnaxiOrchestrator(), SAFE_FRESH_MESSAGE, interaction_mode="conversation")
    pass1_calls = [c for c in call_log if harness._is_json_format(c["format"])]
    assert len(pass1_calls) == 1, "Pass-1 must have actually reached the model for this cost class"

    # Direct, non-mocked proof of the exact margin: rebuild the same
    # real Pass-1 contributions llama_anaxi.py itself composes (same
    # calibrated core-scaffold cost, same real discord_correspondence
    # destination lookup, same conversation_direction.render_pass1_
    # action_menu() call) and check the real context_budget arithmetic
    # directly, with the residual headroom the reviewer asked for.
    import context_budget
    import discord_correspondence
    import conversation_direction as cd
    import temporal_grounding

    destinations = discord_correspondence.list_authorized_destinations(la.PROVENANCE_DB_DIR)
    # Mirror production costing exactly: the fixed menu block is its own ACTION_MENU_FIXED
    # contribution at the fingerprint-calibrated cost; head/tail stay byte-costed in the
    # mechanical state (llama_anaxi composes it this way).
    head, fixed, tail = cd.render_pass1_action_menu_parts(discord_destinations=destinations, workspace_available=True)
    menu = "\n".join(part for part in (head, tail) if part)
    temporal_text = temporal_grounding.snapshot(
        os.path.join(la.PROVENANCE_DB_DIR, "anaxi_provenance.db"), None, process_started=None,
        lifecycle_notice=True, diagnostics_path=os.path.join(la.PROVENANCE_DB_DIR, "ui_turn_diagnostics.jsonl"),
    )
    base_state = "Current working state: active_thread=None, active_thread_origin=None, open_threads=[], direction_owner='unknown'"
    mechanical_state_text = base_state + "\n\n" + temporal_text + "\n\n" + menu

    core = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, "x" * 693, hard=True)
    core.cost = 693  # the real calibrated cost this exact scenario measures
    human = context_budget.Contribution(
        context_budget.CURRENT_HUMAN_MESSAGE, SAFE_FRESH_MESSAGE, hard=True,
    )
    mechanical = context_budget.Contribution(
        context_budget.MECHANICAL_STATE, mechanical_state_text, hard=True,
    )
    menu_fixed = context_budget.Contribution(context_budget.ACTION_MENU_FIXED, fixed, hard=True)
    menu_fixed.cost = la._PASS1_MENU_CALIBRATED_COST
    result = context_budget.compose_within_budget(
        [core, human, mechanical, menu_fixed], context_budget.PASS1_MAX_PROMPT_BUDGET,
    )
    assert result.fits is True
    assert result.final_prompt_cost < context_budget.PASS1_MAX_PROMPT_BUDGET, (
        "must land with residual headroom, not exactly on the ceiling"
    )
    headroom = context_budget.PASS1_MAX_PROMPT_BUDGET - result.final_prompt_cost
    assert headroom >= 50, f"headroom too thin: only {headroom} bytes"


# --------------------------------------------- PASS2-BUDGET-1: Pass-2 too --


def test_safe_fresh_message_clears_both_passes_end_to_end():
    # The conservative owner-facing maximum completes both passes and
    # reaches synthetic persistence without measurement recovery.
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    result = la.run_waking_turn(
        la.AnaxiOrchestrator(), SAFE_FRESH_MESSAGE, interaction_mode="conversation",
    )
    assert result["reply"] == "An ordinary reply."
    pass1_calls = [c for c in call_log if harness._is_json_format(c["format"])]
    pass2_calls = [c for c in call_log if not harness._is_json_format(c["format"])]
    assert len(pass1_calls) == 1, "Pass-1 must reach the model"
    assert len(pass2_calls) == 1, "Pass-2 must reach the model"
    assert len(persistence) == 1, "the turn must reach persistence in the synthetic test path"


def test_pass2_space_grounding_is_conservatively_charged_in_core():
    # No unstructured 43-unit calibration may represent the structured
    # production request. The full grounding text remains inside the
    # byte-estimated hard core and therefore costs its complete 175-byte
    # upper bound.
    import workspace_public_continuity as wpc

    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    original = context_budget.compose_within_budget
    captured = []

    def capture(contributions, budget, **kwargs):
        if budget == context_budget.PASS2_MAX_PROMPT_BUDGET:
            captured.append(list(contributions))
        return original(contributions, budget, **kwargs)

    la.context_budget.compose_within_budget = capture
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), "hello", interaction_mode="conversation")
    finally:
        la.context_budget.compose_within_budget = original

    core = next(c for c in captured[-1] if c.kind == context_budget.CORE_SYSTEM_CONTROL)
    assert core.rendered_text.startswith(wpc.SPACE_CAPABILITY_GROUNDING + "\n\n")
    assert core.cost == context_budget.estimate_tokens(core.rendered_text)
    assert context_budget.estimate_tokens(wpc.SPACE_CAPABILITY_GROUNDING) == 175


def test_pass2_final_composition_has_positive_nontrivial_headroom():
    # Direct proof of positive Pass-2 margin for the safe fresh-message
    # class, never a ceiling-exact landing.
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()

    import context_budget
    orig_compose = context_budget.compose_within_budget
    captured = []

    def _capture(contributions, budget, **kw):
        result = orig_compose(contributions, budget, **kw)
        captured.append((budget, result))
        return result

    context_budget.compose_within_budget = _capture
    la.context_budget.compose_within_budget = _capture
    try:
        la.run_waking_turn(la.AnaxiOrchestrator(), SAFE_FRESH_MESSAGE, interaction_mode="conversation")
    finally:
        context_budget.compose_within_budget = orig_compose
        la.context_budget.compose_within_budget = orig_compose

    pass2_budget, pass2_result = [
        (budget, result) for budget, result in captured
        if budget == context_budget.PASS2_MAX_PROMPT_BUDGET
    ][-1]
    assert pass2_result.fits is True
    assert pass2_result.final_prompt_cost < pass2_budget, "must not land exactly on the Pass-2 ceiling"
    headroom = pass2_budget - pass2_result.final_prompt_cost
    assert headroom > 0, f"Pass-2 headroom must be positive; got {headroom}"


# ------------------------------------------------- 2/3: the two budget cases --


def test_mechanical_state_alone_fits_with_empty_or_minimal_message():
    # Governing check from the bug report: core_system_control +
    # mechanical_state must fit even with an effectively empty human
    # message -- the defect must never be "solved" by relying on the
    # human message being short.
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    result = la.run_waking_turn(la.AnaxiOrchestrator(), "hi", interaction_mode="conversation")
    assert result["reply"] == "An ordinary reply."


def test_reasonably_long_ordinary_human_message_still_fits():
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    long_message = (
        "So here's what's been on my mind the last couple of days. " * 10
    ).strip()
    result = la.run_waking_turn(la.AnaxiOrchestrator(), long_message, interaction_mode="conversation")
    assert result["reply"] == "An ordinary reply."


# ------------------------------------------------- 4-6: discoverability intact --


def test_capability_discoverability_every_affordance_field_present():
    # No affordance may become mechanically hidden by the compaction --
    # every optional field name Clark could choose must still be
    # visible in the exact text sent to the model.
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    la.run_waking_turn(la.AnaxiOrchestrator(), REPRESENTATIVE_FIRST_WAKE_MESSAGE, interaction_mode="conversation")
    prompt_text = _pass1_prompt_text(call_log)
    pass1_call = next(c for c in call_log if harness._is_json_format(c["format"]))
    same_request_text = prompt_text + "\n" + json.dumps(pass1_call["format"], sort_keys=True)
    for field in (
        "sleep_timing_request",
        "operative_directive_request", "external_info_request",
        "discord_correspondence_request", "use_workspace",
    ):
        assert field in same_request_text, (
            f"{field!r} must remain discoverable in the Pass-1 message/schema request"
        )
    # Boundary inquiry is WITHDRAWN from the production release contract (owner decision 2026-09-24): the
    # menu states it is not available; it is never offered as a choice.
    assert "boundary_inquiry_request: not available in this release (leave it out)" in prompt_text
    assert "host report later" not in prompt_text


def test_compact_menu_preserves_semantic_authority_and_payload_guidance():
    # A schema proves field/enum availability, but it cannot explain
    # authority, provenance, conditional use, or what a free string
    # means.  Those accepted facts must remain visible in the actual
    # compact hot-path menu, not merely in unused standalone constants.
    menu = cd.render_pass1_action_menu(
        discord_destinations=[{
            "destination_id": "discord_destination-edc8323d752a99983eebabfa35",
            "display_label": "Caret private family channel",
        }],
        workspace_available=True,
    )
    for required in (
        "if none pending", "not available in this release",
        "+operative_directive_text", "reversible subordinate", "+external_info_target", "read-only",
        "+discord_destination_id", "+discord_message_text", "Alex-controlled", "supervised",
        "SEPARATE JSON fields", "never combined with another optional field",
    ):
        assert required in menu, f"semantic guidance missing from compact menu: {required!r}"
    # Every field the menu tells the subject to use exists in the request schema
    # (so a menu edit can never advertise a field the grammar would reject), and
    # every enum value it names is accepted.
    for field in (
        "sleep_timing_request", "boundary_inquiry_request", "operative_directive_request",
        "operative_directive_text", "external_info_request", "external_info_target",
        "discord_correspondence_request", "discord_destination_id", "discord_message_text",
    ):
        assert field in menu and field in cd.PASS1_SCHEMA["properties"], field
    for value in ("request_sleep", "knock_sleep_request", "withdraw_sleep_request", "set_directive",
                  "withdraw_directive", "web_search", "fetch_url", "send_message"):
        assert value in menu
        assert any(value in spec.get("enum", ()) for spec in cd.PASS1_SCHEMA["properties"].values())
    # The illustrative example must itself be a valid request object.
    import re
    for label in ("Example, a web search", "Example, setting a standing directive"):
        example = json.loads(re.search(label + r": (\{.*\})", menu).group(1))
        assert set(example) <= set(cd.PASS1_SCHEMA["properties"]) and set(cd.PASS1_SCHEMA["required"]) <= set(example)
        assert example["act"] in cd.PASS1_SCHEMA["properties"]["act"]["enum"]
        # ...and it must validate as a real request (placeholders aside)
        example.update({k: "x" for k, v in example.items() if isinstance(v, str) and v.startswith("<")})
        assert cd.validate_pass1_conversation_act(example)[1] is None, label
    # Without the workspace / destinations the menu must not advertise them.
    bare = cd.render_pass1_action_menu(workspace_available=False)
    assert "use_workspace" not in bare and "discord_correspondence_request" not in bare
    # Real production destination id/label size class, not the shorter fixture.
    # The menu states each capability's separate field once and now says HOW to
    # use it (live finding: without that the real model answered web/Discord/
    # photo requests with act=use_workspace or claimed it had no access). It is
    # byte-costed only on the fast path; a composition that would shed re-costs
    # it by real measurement (a real tokenizer prices it near a quarter of this),
    # and the fully-capable turns in this module still fit their budget.
    # Size tripwire (re-pinned 2026-09-23 for the lawful-null reply_request line + example, whose
    # wording the real-model menu battery chose; the budget itself is proven by the calibrated
    # composition tests, this only catches silent growth).
    assert len(menu.encode("utf-8")) <= 2050


def test_discord_destination_label_and_stable_id_usable():
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    la.run_waking_turn(la.AnaxiOrchestrator(), REPRESENTATIVE_FIRST_WAKE_MESSAGE, interaction_mode="conversation")
    prompt_text = _pass1_prompt_text(call_log)
    assert destination["destination_id"] in prompt_text
    assert DESTINATION_LABEL in prompt_text
    assert DESTINATION_SNOWFLAKE not in prompt_text  # never the raw transport identity


def test_workspace_action_remains_discoverable():
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    la.run_waking_turn(la.AnaxiOrchestrator(), REPRESENTATIVE_FIRST_WAKE_MESSAGE, interaction_mode="conversation")
    prompt_text = _pass1_prompt_text(call_log)
    assert "use_workspace" in prompt_text


def test_operative_directive_affordance_available_where_lawful():
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    la.run_waking_turn(la.AnaxiOrchestrator(), REPRESENTATIVE_FIRST_WAKE_MESSAGE, interaction_mode="conversation")
    prompt_text = _pass1_prompt_text(call_log)
    for value in cd.VALID_OPERATIVE_DIRECTIVE_REQUESTS:
        assert value in prompt_text


# --------------------------------------------- 7/8: no widened/dropped authority --


def test_family_private_filtering_functions_unchanged():
    # PASS1-BUDGET-1 only compacts affordance TEXT -- it must never touch
    # the FS1 availability predicates themselves. Direct proof the
    # existing owner-private/member-private/shared-scope gating logic is
    # byte-for-byte the same decision it always was.
    assert cd  # module import succeeds; the assertions below are the real proof
    import llama_anaxi as la_module
    assert la_module._workspace_action_available(False, None) is True
    assert la_module._discord_correspondence_available(False, None) is True
    assert la_module._workspace_action_available(True, None) is False
    assert la_module._discord_correspondence_available(True, None) is False


def test_no_authority_widened_affordance_values_match_closed_enums_exactly():
    # Compaction must name exactly the members of each closed enum --
    # never more (a widened surface) and never fewer (a hidden one).
    checks = [
        (cd.SLEEP_TIMING_AFFORDANCE_TEXT, cd.VALID_SLEEP_TIMING_REQUESTS),
        (cd.OPERATIVE_DIRECTIVE_AFFORDANCE_TEXT, cd.VALID_OPERATIVE_DIRECTIVE_REQUESTS),
        (cd.EXTERNAL_INFO_AFFORDANCE_TEXT, cd.VALID_EXTERNAL_INFO_REQUESTS),
        (cd.DISCORD_CORRESPONDENCE_AFFORDANCE_PREFIX, cd.VALID_DISCORD_CORRESPONDENCE_REQUESTS),
    ]
    for text, valid_values in checks:
        for value in valid_values:
            assert value in text, f"{value!r} missing from {text!r}"


def test_current_human_message_delivered_verbatim_never_dropped_for_budget():
    # The fix must never "solve" the budget by shrinking or dropping the
    # current human message -- it remains a HARD, byte-for-byte
    # contribution.
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    la.run_waking_turn(la.AnaxiOrchestrator(), REPRESENTATIVE_FIRST_WAKE_MESSAGE, interaction_mode="conversation")
    prompt_text = _pass1_prompt_text(call_log)
    assert REPRESENTATIVE_FIRST_WAKE_MESSAGE in prompt_text


def test_thread_label_bound_is_enforced_without_relabeling_schema_calibration():
    assert cd.MAX_THREAD_LENGTH == 200
    assert cd.PASS1_SCHEMA["properties"]["thread"] == {"type": "string"}
    base = {
        "act": cd.DEVELOP_CURRENT, "direction_request": cd.REQUEST_NONE,
        "relinquish_direction": False,
    }
    accepted, failure = cd.validate_pass1_conversation_act({**base, "thread": "t" * 200})
    assert failure is None and len(accepted["thread"]) == 200
    rejected, failure = cd.validate_pass1_conversation_act({**base, "thread": "t" * 201})
    assert rejected is None and failure == cd.DirectionFailure.INVALID_THREAD


def test_pass1_calibration_schema_identity_is_frozen_not_self_referential():
    la, call_log, p1, p2, persistence, destination = _fresh_fully_capable_environment()
    expected = la._PASS1_SCAFFOLD_CALIBRATION["pass1_schema_sha256"]
    assert expected == la.PASS1_SCHEMA_SHA256
    assert expected == "e68b413500f0fdf4c6f4a9805db826c3c57f628570ec4742bd5e60b82c1e9b48"   # shared-vault recalibration
    altered = dict(cd.PASS1_SCHEMA)
    altered["additionalProperties"] = True
    import hashlib
    altered_hash = hashlib.sha256(json.dumps(altered, sort_keys=True).encode("utf-8")).hexdigest()
    assert altered_hash != expected


def test_production_equivalent_400_byte_envelope_has_headroom():
    # Read-only production audit values, represented only as sizes so no
    # Kardia or human content is copied into the test.  These are the
    # largest lawful hard states after enforcing MAX_THREAD_LENGTH:
    # Pass 1: calibrated core 693 + fully-loaded mechanical state 2304.
    # Pass 2: byte-estimated Kardia/core with the full space grounding
    # and its delimiter 1207 + calibrated framing 156 + calibrated task
    # 156 + worst directional state 1198.
    target = 400
    pass1_fixed = 693 + 2304
    pass2_fixed = 1207 + 156 + 156 + 1198
    assert context_budget.PASS1_MAX_PROMPT_BUDGET - pass1_fixed - target == 59
    assert context_budget.PASS2_MAX_PROMPT_BUDGET - pass2_fixed - target == 83


ALL_TESTS = [
    test_full_capability_turn_with_representative_message_fits_and_reaches_model,
    test_safe_fresh_message_pass1_composition_fits_with_headroom,
    test_safe_fresh_message_clears_both_passes_end_to_end,
    test_pass2_space_grounding_is_conservatively_charged_in_core,
    test_pass2_final_composition_has_positive_nontrivial_headroom,
    test_mechanical_state_alone_fits_with_empty_or_minimal_message,
    test_reasonably_long_ordinary_human_message_still_fits,
    test_capability_discoverability_every_affordance_field_present,
    test_compact_menu_preserves_semantic_authority_and_payload_guidance,
    test_discord_destination_label_and_stable_id_usable,
    test_workspace_action_remains_discoverable,
    test_operative_directive_affordance_available_where_lawful,
    test_family_private_filtering_functions_unchanged,
    test_no_authority_widened_affordance_values_match_closed_enums_exactly,
    test_current_human_message_delivered_verbatim_never_dropped_for_budget,
    test_thread_label_bound_is_enforced_without_relabeling_schema_calibration,
    test_pass1_calibration_schema_identity_is_frozen_not_self_referential,
    test_production_equivalent_400_byte_envelope_has_headroom,
]


def main():
    passed, failed = 0, 0
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
            traceback.print_exc()
    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


def test_fixed_menu_block_calibration_is_bound_to_its_exact_text():
    """The calibrated (221-token) cost of the fixed action-menu block is honored
    only for the exact text it was measured on: any edit must fall back to the
    byte estimator until someone re-measures it."""
    import hashlib
    import llama_anaxi as la_module
    _head, fixed, _tail = cd.render_pass1_action_menu_parts(workspace_available=True)
    assert la_module._PASS1_MENU_CALIBRATION["scaffold_sha256"] == hashlib.sha256(fixed.encode("utf-8")).hexdigest(), (
        "the fixed menu block changed: re-measure its cost (A/B, real model) and update _PASS1_MENU_CALIBRATION"
    )
    assert la_module._PASS1_MENU_CALIBRATED_COST < context_budget.estimate_tokens(fixed)
    live = la_module._pass1_scaffold_live_fingerprint(fixed + " ", message_structure_signature=("system_merged",))
    assert context_budget.calibrated_or_fallback_cost(
        fixed + " ", live, la_module._PASS1_MENU_CALIBRATION, la_module._PASS1_MENU_CALIBRATED_COST,
    ) == context_budget.estimate_tokens(fixed + " ")
