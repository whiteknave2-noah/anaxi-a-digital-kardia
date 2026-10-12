"""
Anaxi -- Llama sleep cycle: memory consolidation + identity reflection,
on the local substrate. Mirrors anaxi_sleep.py; see that file's
docstring for what each phase does.

Reads only "llama"-tagged entries from anaxi_log.jsonl -- Claude's
turns are filtered out on purpose, so the two substrates' histories
never cross into each other through the shared log file. anaxi_sleep.py
has been updated to filter the other way, for the same reason. This
was a latent bug, not a hypothetical one that got preemptively fixed --
harmless while only claude_anaxi.py existed, real starting today, now
that both substrates write to the same file.

Setup: same folder as llama_anaxi.py, same dependencies.
Run llama_anaxi.py a few times first so there's something to consolidate.

Usage:
    python llama_sleep.py                   run a sleep cycle
    python llama_sleep.py --list            show pending proposals
    python llama_sleep.py --resolve 3 accept
    python llama_sleep.py --resolve 3 reject
    python llama_sleep.py --reconsider node_42 delete_node
        Deliberate human action only -- lifts a rejection's
        suppression so this node/proposal-type pair is eligible for
        the ordinary proposal machinery again next cycle. Does not
        itself accept, reject, or delete anything; never modifies the
        original rejection.
"""

import os
import sqlite3
import sys
import json
import re
import time

import ollama
from orchestration import AnaxiOrchestrator
from relational_history import RelationalHistory
from sleep_receipts import SleepReceiptStore
from migrate_historical_data import PIPELINE_STRUCTURE, matches_legacy_pipeline

import context_budget

PIPELINE_KEY = PIPELINE_STRUCTURE["llama"]["pipeline_key"]

MODEL = "llama3.2:3b"
DB_PATH = "anaxi_mind_llama.db"
RELATIONAL_DB_PATH = "anaxi_relational_llama.db"
RECEIPT_DB_PATH = "anaxi_sleep_receipts.db"
USER_ID = "nate"
LOG_FILE = "anaxi_log.jsonl"
PROVENANCE_DB_PATH = "anaxi_provenance.db"
RECENT_TURNS = 20


def to_ollama_format(messages: list[dict]) -> list[dict]:
    return messages


def strip_code_fence(text: str) -> str:
    """Claude Sonnet 5 fenced JSON output intermittently despite being
    told not to -- found on last night's live run, not anticipated in
    advance. A smaller, less heavily instruction-tuned local model
    doing the same or worse isn't a stretch, so this is here from the
    start rather than waited for a second time."""
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1) if match else text


def ask_llama_for_json(messages: list[dict], generation_reserve=None, *, measure_only=False):
    """The one Sleep inference call site.

    ``generation_reserve`` (production Sleep stages pass their budgeted
    reserve) bounds decoding with ``num_predict`` and pins ``num_ctx`` to the
    ceiling the budgets were derived against, so a runaway generation can never
    silently shift the prompt out of the window. A response cut off by that
    bound is not treated as JSON (it fails validation, never a partial success).

    ``measure_only=True`` performs the bounded, discarded-output ``num_predict=1``
    probe and returns the provider's own prompt token count for these exact
    messages (or None if it cannot be trusted): used only to admit a prompt the
    byte upper bound rejected."""
    if measure_only:
        try:
            probe = ollama.chat(
                model=MODEL, messages=to_ollama_format(messages), format="json",
                options={"temperature": 0, "num_predict": 1,
                         "num_ctx": context_budget.SLEEP_LLAMA_CONTEXT_CEILING},
            )
        except Exception:
            return None
        count = probe.get("prompt_eval_count") if hasattr(probe, "get") else None
        if type(count) is int and 0 < count < context_budget.SLEEP_LLAMA_CONTEXT_CEILING - 1:
            return count
        return None
    options = {"temperature": 0.2}
    if generation_reserve is not None:
        options.update({"num_predict": generation_reserve, "num_ctx": context_budget.SLEEP_LLAMA_CONTEXT_CEILING})
    response = ollama.chat(
        model=MODEL,
        messages=to_ollama_format(messages),
        format="json",
        options=options,
    )
    if generation_reserve is not None and response.get("done_reason") == "length":
        raise SleepGenerationTruncated("generation reached its budgeted reserve before completing")
    return strip_code_fence(response["message"]["content"].strip())


class SleepGenerationTruncated(Exception):
    """A Sleep model response ended at its generation bound, not at a complete
    answer. Fails the stage closed rather than parsing a torn object."""


def measure_prompt_tokens(messages):
    return ask_llama_for_json(messages, measure_only=True)


def selection_chat(messages):
    return ask_llama_for_json(messages, context_budget.SLEEP_SELECTION_GENERATION_RESERVE)


def transformation_chat(messages):
    return ask_llama_for_json(messages, context_budget.SLEEP_TRANSFORMATION_GENERATION_RESERVE)


def _compose_rem_budget(messages):
    """OWC9-P4 section 5: aggregate REM-consolidation prompt-budget
    composition -- a production inference call this gate's own source
    audit found completely unbudgeted (no generation-reserve
    protection at all; `options` sets no num_predict). Reuses
    context_budget.py's central authority unchanged, on this model's
    own confirmed budget profile (SLEEP_REM_MAX_PROMPT_BUDGET --
    llama3.2:3b, not gemma4:e4b; see that constant's own comment).

    HARD only: REM_SYSTEM_PROMPT (fixed, small -- 1485 bytes, measured
    to comfortably fit without needing calibration) and the filled-in
    user template, which embeds up to RECENT_TURNS conversation turns
    VERBATIM (genuinely unbounded). No dialogue window exists here to
    trim unit-by-unit (load_recent_conversation() returns one already-
    joined string, unchanged this gate per spec section 5's own "do
    not fix unrelated Sleep semantics") -- an oversized conversation
    log fails the whole HARD set closed rather than being torn
    mid-string."""
    hard_system = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, messages[0]["content"], hard=True)
    hard_user = context_budget.Contribution(context_budget.CURRENT_HUMAN_MESSAGE, messages[1]["content"], hard=True)
    return context_budget.compose_within_budget(
        [hard_system, hard_user], context_budget.SLEEP_REM_MAX_PROMPT_BUDGET,
    )


def _compose_reflection_budget(messages):
    """OWC9-P4 section 5: aggregate identity-reflection prompt-budget
    composition -- same reasoning as _compose_rem_budget() above, on
    SLEEP_REFLECTION_MAX_PROMPT_BUDGET (this schema's own smaller,
    single-record output shape)."""
    hard_system = context_budget.Contribution(context_budget.CORE_SYSTEM_CONTROL, messages[0]["content"], hard=True)
    hard_user = context_budget.Contribution(context_budget.CURRENT_HUMAN_MESSAGE, messages[1]["content"], hard=True)
    return context_budget.compose_within_budget(
        [hard_system, hard_user], context_budget.SLEEP_REFLECTION_MAX_PROMPT_BUDGET,
    )


def load_recent_conversation(n: int, provenance_conn: sqlite3.Connection) -> str:
    """Filtering goes through matches_legacy_pipeline()
    (migrate_historical_data.py, reused unedited) instead of a naive
    `e.get("substrate") == "llama"` check -- correctly separates this
    pipeline's entries from a shared log that may also contain entries
    carrying a pipeline_id key this historical filter never had to
    account for. Falls back to the historical substrate label for
    entries that predate the native write path. Requires an open
    connection to anaxi_provenance.db, since that's where
    pipeline_id/substrate-label resolution lives."""
    if not os.path.exists(LOG_FILE):
        return "(no prior turns logged yet)"
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]
    llama_entries = [e for e in entries if matches_legacy_pipeline(provenance_conn, e, PIPELINE_KEY)]
    recent = llama_entries[-n:]
    if not recent:
        return "(no prior turns logged yet)"
    return "\n\n".join(
        f"User: {e['prompt']}\nLlama: {e['response']}" for e in recent
    )


def run_sleep(orch: AnaxiOrchestrator, receipts: SleepReceiptStore) -> None:
    # SLP1-E correction: disabling only main()'s CLI dispatch left this
    # function itself -- the actual REM/reflection/governance/Kardia
    # semantic pipeline -- directly callable by any Python caller that
    # imports llama_sleep, which does not satisfy "no second live
    # semantic Sleep implementation." The guard belongs HERE, at the
    # true semantic entrypoint, not only at its CLI wrapper. Checked
    # before receipts.new_cycle_id() or any other side effect -- no
    # receipt is ever written for a call that never ran.
    print(LEGACY_SLEEP_DISABLED_MESSAGE)
    raise RuntimeError(LEGACY_SLEEP_DISABLED_MESSAGE)

    cycle_id = receipts.new_cycle_id()
    sleep_start = int(time.time())
    errors: list[str] = []
    sleep_success = False
    identity_success = False
    memories_considered = 0
    memories_retained = 0
    memories_discarded = 0
    kardia_reflections_generated = 0
    sleep_completion = None
    proposals_after = 0
    proposals_created = 0

    print(f"[receipt] cycle {cycle_id} starting")
    proposals_before = len(orch.list_pending_proposals(USER_ID))
    provenance_conn = sqlite3.connect(PROVENANCE_DB_PATH)
    try:
        conversation = load_recent_conversation(RECENT_TURNS, provenance_conn)
    finally:
        provenance_conn.close()

    print("--- asking Llama to propose memory updates (REM) ---")
    rem_json = json.dumps({"upsert_nodes": [], "add_edges": [], "delete_nodes": []})
    try:
        rem_messages = orch.build_rem_prompt(conversation, "(none yet)")
        rem_budget = _compose_rem_budget(rem_messages)
        if not rem_budget.fits:
            # OWC9-P4 section 5: aggregate preflight before this model
            # call -- a hard-only overflow never reaches ask_llama_for_
            # json() at all. Routed through the SAME try/except this
            # step already had for any other failure -- zero fabricated
            # output (rem_json stays the empty-lists default set above),
            # zero model call, no new failure surface introduced.
            raise RuntimeError(
                f"{context_budget.BUDGET_EXCEEDED}: REM prompt cost {rem_budget.final_prompt_cost} "
                f"exceeds SLEEP_REM_MAX_PROMPT_BUDGET {context_budget.SLEEP_REM_MAX_PROMPT_BUDGET}"
            )
        rem_json = ask_llama_for_json(rem_messages)
        print(rem_json)
        rem_data = json.loads(rem_json)
        memories_retained = len(rem_data.get("upsert_nodes", []))
        memories_discarded = len(rem_data.get("delete_nodes", []))
        memories_considered = memories_retained + memories_discarded
    except Exception as e:
        errors.append(f"REM proposal step: {type(e).__name__}: {e}")

    print("\n--- asking Llama to propose an identity reflection ---")
    reflection_json = None
    try:
        reflection_messages = orch.build_reflection_prompt(
            conversation_summary=conversation,
            identity_signals="(none noted)",
            user_id=USER_ID,
        )
        reflection_budget = _compose_reflection_budget(reflection_messages)
        if not reflection_budget.fits:
            raise RuntimeError(
                f"{context_budget.BUDGET_EXCEEDED}: reflection prompt cost {reflection_budget.final_prompt_cost} "
                f"exceeds SLEEP_REFLECTION_MAX_PROMPT_BUDGET {context_budget.SLEEP_REFLECTION_MAX_PROMPT_BUDGET}"
            )
        reflection_json = ask_llama_for_json(reflection_messages)
        print(reflection_json)
        kardia_reflections_generated = 1
    except Exception as e:
        errors.append(f"Reflection proposal step: {type(e).__name__}: {e}")

    print("\n--- running both through governance ---")
    try:
        result = orch.run_sleep_cycle(
            user_id=USER_ID,
            rem_json_payload=rem_json,
            reflection_json_payload=reflection_json,
        )
        sleep_success = result["sleep_status"] == "success"
        # Governance ran to completion without raising -- distinct from
        # whether it applied, deferred, or blocked a Kardia change, all
        # three of which are legitimate, non-error outcomes.
        identity_success = True
        sleep_completion = int(time.time())

        print(f"\nMemory consolidation: {result['sleep_status']}")
        if result["sleep_status"] == "failed":
            print(f"  reason: {result['reason']}")
            errors.append(f"Memory consolidation failed: {result['reason']}")
        print(f"Identity: {result['identity_status']}")

        # If the reflection actually produced a constitutional_argument,
        # that's the model's own stated reasoning about itself -- real
        # content for agent_observation, not manufactured to fill the
        # field. Waking couldn't honestly populate this; sleep can.
        try:
            argument = json.loads(reflection_json).get("constitutional_argument")
        except (json.JSONDecodeError, TypeError, AttributeError):
            argument = None
        if argument:
            history = RelationalHistory(RELATIONAL_DB_PATH)
            history.record_event(
                user_id=USER_ID,
                substrate="llama",
                agent_observation=argument,
            )
            history.close()

        proposals = result["pending_proposals"]
        proposals_after = len(proposals)
        proposals_created = max(0, proposals_after - proposals_before)

        if proposals:
            print(f"\n{len(proposals)} pending proposal(s) waiting on you:")
            for p in proposals:
                print(f"  #{p.get('id')} [{p.get('proposal_type')}] {p.get('reason')}")
            print("Resolve with: python llama_sleep.py --resolve <id> accept|reject")
        else:
            print("\nNo pending proposals.")
    except Exception as e:
        errors.append(f"Governance step: {type(e).__name__}: {e}")
        proposals_after = proposals_before

    cycle_end = int(time.time())
    receipts.record_receipt(
        cycle_id=cycle_id,
        user_id=USER_ID,
        sleep_start_timestamp=sleep_start,
        sleep_success=sleep_success,
        identity_success=identity_success,
        memories_considered=memories_considered,
        memories_retained=memories_retained,
        memories_discarded=memories_discarded,
        kardia_reflections_generated=kardia_reflections_generated,
        proposals_created=proposals_created,
        proposals_pending=proposals_after,
        errors=errors,
        sleep_completion_timestamp=sleep_completion,
        cycle_end_timestamp=cycle_end,
    )
    print(f"[receipt] cycle {cycle_id} recorded"
          f" ({'no errors' if not errors else f'{len(errors)} error(s)'})")


def list_proposals(orch: AnaxiOrchestrator) -> None:
    proposals = orch.list_pending_proposals(USER_ID)
    if not proposals:
        print("No pending proposals.")
        return
    for p in proposals:
        print(json.dumps(p, indent=2, default=str))


def resolve(orch: AnaxiOrchestrator, proposal_id: int, accept: bool) -> None:
    print(orch.resolve_proposal(USER_ID, proposal_id, accept))


def reconsider(orch: AnaxiOrchestrator, node_id: str, proposal_type: str) -> None:
    print(orch.reconsider_proposal(USER_ID, node_id, proposal_type))


# SLP1-E: mirrors anaxi_sleep.py's own already-established
# LEGACY_SLEEP_DISABLED_MESSAGE disposition (SLP1-D1B) -- this local-
# Llama pathway's own CLI (`python llama_sleep.py` with no args, or any
# of --list/--resolve/--reconsider) is the one remaining way a human or
# a scheduled task could trigger the OLD REM/reflection/governance
# pipeline (run_sleep() -> AnaxiOrchestrator.run_sleep_cycle() ->
# Kardia/mind-graph mutation) as an independently executable alternative
# to the new dormant Sleep v1 pipeline (sleep_cycle.run_sleep_cycle()).
# Guarded here, once, ahead of every branch -- not one guard per branch --
# exactly like anaxi_sleep.py's own guard placement, so there is no
# public invocation path through this file's CLI that can reach
# AnaxiOrchestrator, ask_llama_for_json's REM/reflection callers, or any
# legacy Sleep write.
#
# run_sleep()/list_proposals()/resolve()/reconsider() themselves are
# UNCHANGED and remain directly callable by anything that imports this
# module under its own control (e.g. test_sleep_receipts.py's existing
# OWC9 budget-overflow regression coverage, which calls run_sleep()
# directly against fully faked ollama/orchestrator/receipts dependencies
# and touches no real data) -- only the CLI dispatch that would run
# real legacy Sleep against real production state is disabled. The
# already-approved ask_llama_for_json() JSON transport wrapper is
# untouched and remains the physical call site B/C's OWC9-budgeted
# sleep_selection/sleep_transformation pathways reuse.
LEGACY_SLEEP_DISABLED_MESSAGE = (
    "LEGACY_SLEEP_DISABLED\n"
    "This local-Llama Sleep pathway's CLI (llama_sleep.py's own "
    "run_sleep()/REM/reflection/governance cycle) has been mechanically "
    "disabled -- SLP1-E established that the only authoritative Sleep "
    "pathway is the new dormant Sleep v1 orchestrator "
    "(sleep_cycle.run_sleep_cycle()); running this file's CLI too would "
    "be a second, uncoordinated Sleep authority. No model call was made, "
    "nothing was written. The source remains on disk for historical "
    "reference and existing regression coverage only."
)


def main():
    # Checked before AnaxiOrchestrator is even constructed, and before
    # any branch below -- see the comment above.
    print(LEGACY_SLEEP_DISABLED_MESSAGE)
    sys.exit(1)


if __name__ == "__main__":
    main()
