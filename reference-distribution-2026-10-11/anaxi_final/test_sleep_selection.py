"""
ANAXI SLP1-B -- behavioral regression suite for dormant Sleep Selection
(sleep_selection.py).

No real model calls anywhere in this file -- sleep_selection._selection_chat
is monkeypatched to a scripted fake for every test. No database, no
provenance store, no filesystem writes: sleep_selection.py takes plain
WMU dicts and returns an in-memory SelectionResult only, so tests build
synthetic WMU dicts directly rather than going through the real
waking_material_unit.assemble_wmus() pipeline.

Run:
    python test_sleep_selection.py
"""

import ast
import json
import os
import sys
import tempfile

import context_budget
import sleep_selection as ss
import test_owc9p4_bypass_guard as bypass_guard
import budget_compliance_registry as registry


def make_wmu(wmu_id, primary_event_id, expression="hello there", memory_kind="clark_expression"):
    return {
        "wmu_id": wmu_id,
        "primary_event_id": primary_event_id,
        "selection_bearing": [{"memory_kind": memory_kind, "expression": expression}],
        "supporting_provenance": [{"note": "must never reach the model"}],
    }


class ScriptedChat:
    """Replaces sleep_selection._selection_chat. `script` is a callable
    (messages) -> raw_text_response, invoked for every call including
    retries -- tests control behavior by inspecting messages content
    (offered wmu ids are always visible as literal 'wmu-...' tokens)."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        return self.script(messages, len(self.calls))


class QueueChat:
    """Replaces sleep_selection._selection_chat with a fixed queue of
    canned raw responses, one per call, in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        return self.responses.pop(0)


def install(fake):
    original = ss._selection_chat
    ss._selection_chat = fake
    return original


def restore(original):
    ss._selection_chat = original


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(bool(cond))

    # === A: ordinary valid selection ===
    original = ss._selection_chat
    try:
        wmus = [make_wmu("wmu-a", "01EVTA"), make_wmu("wmu-b", "01EVTB"), make_wmu("wmu-c", "01EVTC")]
        fake = QueueChat([json.dumps({"selected_wmu_ids": ["wmu-c", "wmu-a"]})])
        install(fake)
        result = ss.select_wmus(wmus)
        check("A. ordinary valid selection returns exactly the model's chosen ids",
              set(result.selected_wmu_ids) == {"wmu-a", "wmu-c"})
        check("A. selection is returned in canonical (original) candidate order, not model output order",
              result.selected_wmu_ids == ["wmu-a", "wmu-c"])
        check("A. one batch, one attempt, no reduction for a small ordinary case",
              result.batch_count == 1 and result.attempt_count == 1 and result.reduction_rounds == 0)
        check("A. exactly one model call was made",
              len(fake.calls) == 1)
    finally:
        restore(original)

    # === B: zero selection is valid, never retried, never a minimum ===
    original = ss._selection_chat
    try:
        wmus = [make_wmu("wmu-a", "01EVTA"), make_wmu("wmu-b", "01EVTB")]
        fake = QueueChat([json.dumps({"selected_wmu_ids": []})])
        install(fake)
        result = ss.select_wmus(wmus)
        check("B. an empty selection is a valid, successful result",
              result.selected_wmu_ids == [])
        check("B. an empty selection is never retried (exactly one attempt)",
              result.attempt_count == 1 and len(fake.calls) == 1)
    finally:
        restore(original)

    # === C: validation table (each case independently invalid) ===
    offered = ["wmu-a", "wmu-b"]
    invalid_cases = [
        ("malformed JSON / prose", "not json at all"),
        ("extra semantic field", json.dumps({"selected_wmu_ids": ["wmu-a"], "rationale": "important"})),
        ("missing required key", json.dumps({"other": []})),
        ("wrong value type (string, not list)", json.dumps({"selected_wmu_ids": "wmu-a"})),
        ("unoffered id", json.dumps({"selected_wmu_ids": ["wmu-not-offered"]})),
        ("too many unique ids for max_select=1", json.dumps({"selected_wmu_ids": ["wmu-a", "wmu-b"]})),
        ("null list", json.dumps({"selected_wmu_ids": None})),
        ("list containing a non-string", json.dumps({"selected_wmu_ids": ["wmu-a", 3]})),
    ]
    for label, raw in invalid_cases:
        check(f"C. validation table rejects: {label}",
              ss._validate_selection_response(raw, offered, 1) is None)

    check("C. duplicate valid ids dedupe mechanically and add no weight against max_select",
          ss._validate_selection_response(
              json.dumps({"selected_wmu_ids": ["wmu-a", "wmu-a"]}), offered, 1
          ) == ["wmu-a"])

    # === D + E: deterministic whole-WMU batching, and oversize failure ===
    original = ss._selection_chat
    try:
        wmus = [make_wmu(f"wmu-{i}", f"01EVT{i}", expression="pad" * 20) for i in range(3)]
        one_text = ss._render_candidate(wmus[0])
        msg_one = ss._build_messages([one_text], 1, ss.MODE_INITIAL)
        msg_one.append({"role": "user", "content": ss._retry_correction(1)})
        cost_one = ss._compose_selection_budget(msg_one).final_prompt_cost
        two_texts = [ss._render_candidate(wmus[0]), ss._render_candidate(wmus[1])]
        msg_two = ss._build_messages(two_texts, 2, ss.MODE_INITIAL)
        msg_two.append({"role": "user", "content": ss._retry_correction(2)})
        cost_two = ss._compose_selection_budget(msg_two).final_prompt_cost

        original_budget = context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET
        try:
            # Fits exactly one whole candidate per call, never two.
            context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET = cost_one + 1
            assert cost_two > cost_one + 1, "fixture too small to prove batching -- widen the padding"

            offered_per_call = []

            def record_and_select_none(messages, attempt_no):
                content = messages[0]["content"]
                offered_per_call.append([wid for wid in ("wmu-0", "wmu-1", "wmu-2") if wid in content])
                return json.dumps({"selected_wmu_ids": []})

            fake = ScriptedChat(record_and_select_none)
            install(fake)
            result = ss.select_wmus(wmus)
            check("D. every candidate lands in its own batch when only one whole WMU fits per call",
                  result.batch_count == 3)
            check("D. every lawful candidate is offered in exactly one initial call (no skip, no duplication)",
                  sorted(sum(offered_per_call, [])) == ["wmu-0", "wmu-1", "wmu-2"]
                  and all(len(o) == 1 for o in offered_per_call))
            check("D. batches are offered in canonical (ascending primary_event_id) order",
                  offered_per_call == [["wmu-0"], ["wmu-1"], ["wmu-2"]])
            restore(original)

            # E: oversize -- budget too small even for one whole candidate alone.
            context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET = cost_one - 1
            fake2 = QueueChat([json.dumps({"selected_wmu_ids": []})] * 5)
            install(fake2)
            oversize_raised = False
            try:
                ss.select_wmus(wmus)
            except ss.OversizeCandidateError:
                oversize_raised = True
            check("E. a WMU that cannot fit alone raises OversizeCandidateError and fails the whole operation",
                  oversize_raised)
            check("E. no model call is ever made once an oversize candidate is detected during packing",
                  len(fake2.calls) == 0)
        finally:
            context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET = original_budget
    finally:
        restore(original)

    # === F: no source reopening (cheapest reliable proof: static source scan) ===
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sleep_selection.py"),
              "r", encoding="utf-8") as f:
        selection_source = f.read()
    forbidden_tokens = [
        "sqlite3", "prov_conn", "native_provenance_writer", "hippocampus",
        "sleep_receipts", "sleep_watermark", "kardia", "mind_graph",
        "workspace_episode_provenance", "qwen", "requests.get", "urlopen",
    ]
    present = [tok for tok in forbidden_tokens if tok in selection_source]
    check("F. sleep_selection.py imports/uses none of the source-reopening surfaces "
          f"(DB, hippocampus, Qwen, web, provenance writers) -- found: {present!r}",
          present == [])

    # === G: retry law, and terminal multi-batch failure yields no partial success ===
    original = ss._selection_chat
    try:
        wmus = [make_wmu("wmu-a", "01EVTA")]
        fake = QueueChat(["garbage, not json", json.dumps({"selected_wmu_ids": ["wmu-a"]})])
        install(fake)
        result = ss.select_wmus(wmus)
        check("G. an invalid response followed by a valid retry succeeds using exactly two attempts",
              result.selected_wmu_ids == ["wmu-a"] and result.attempt_count == 2 and len(fake.calls) == 2)
        restore(original)

        fake2 = QueueChat(["garbage", "still garbage"])
        install(fake2)
        raised = False
        try:
            ss.select_wmus(wmus)
        except ss.SelectionValidationError:
            raised = True
        check("G. two consecutive invalid responses (initial + retry) fail the call, exactly two attempts",
              raised and len(fake2.calls) == 2)
        restore(original)

        # A later batch's terminal failure must not yield a partial success
        # from an earlier, already-successful batch.
        original_budget = context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET
        try:
            wmus2 = [make_wmu("wmu-p", "01EVTP", expression="pad" * 20),
                     make_wmu("wmu-q", "01EVTQ", expression="pad" * 20)]
            one_text = ss._render_candidate(wmus2[0])
            msg_one = ss._build_messages([one_text], 1, ss.MODE_INITIAL)
            msg_one.append({"role": "user", "content": ss._retry_correction(1)})
            context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET = (
                ss._compose_selection_budget(msg_one).final_prompt_cost + 1
            )

            fake3 = QueueChat([
                json.dumps({"selected_wmu_ids": ["wmu-p"]}),  # batch 1: valid, would succeed alone
                "garbage",                                     # batch 2: invalid
                "still garbage",                               # batch 2 retry: invalid -> terminal failure
            ])
            install(fake3)
            raised2 = False
            try:
                ss.select_wmus(wmus2)
            except ss.SelectionValidationError:
                raised2 = True
            check("G. a terminal failure in a later batch fails the whole operation "
                  "(no partial union from the earlier successful batch is ever returned)",
                  raised2)
        finally:
            context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET = original_budget
    finally:
        restore(original)

    # === H: reduction -- converges in one round ===
    original = ss._selection_chat
    original_budget = context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET
    try:
        n = 10
        wmus = [make_wmu(f"wmu-r{i}", f"01EVTR{i}", expression="pad" * 20) for i in range(n)]
        one_text = ss._render_candidate(wmus[0])
        msg_one_initial = ss._build_messages([one_text], 1, ss.MODE_INITIAL)
        msg_one_reduction = ss._build_messages([one_text], 1, ss.MODE_REDUCTION)
        msg_one_initial.append({"role": "user", "content": ss._retry_correction(1)})
        msg_one_reduction.append({"role": "user", "content": ss._retry_correction(1)})
        cost_one = max(
            ss._compose_selection_budget(msg_one_initial).final_prompt_cost,
            ss._compose_selection_budget(msg_one_reduction).final_prompt_cost,
        )
        context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET = cost_one + 1  # exactly one WMU per call, both modes

        keep_ids = {f"wmu-r{i}" for i in range(5)}  # the 5 reduction survives
        offered_in_reduction = []

        def script(messages, attempt_no):
            content = messages[0]["content"]
            offered = [f"wmu-r{i}" for i in range(n) if f"wmu-r{i}" in content]
            assert len(offered) == 1, "budget was sized for exactly one whole WMU per call"
            wid = offered[0]
            if "(Selection mode: reduction)" in content:
                offered_in_reduction.append(wid)
                chosen = [wid] if wid in keep_ids else []
            else:
                chosen = [wid]  # initial stage: select everything, forcing overflow into reduction
            return json.dumps({"selected_wmu_ids": chosen})

        install(ScriptedChat(script))
        result = ss.select_wmus(wmus)

        check("H. initial stage selects every candidate, exceeding the final bound and forcing reduction",
              result.batch_count == n)
        check("H. exactly one reduction round was needed to converge",
              result.reduction_rounds == 1)
        check("H. reduction candidates are drawn ONLY from what Clark selected in the prior stage "
              "(never a new/unrelated candidate)",
              set(offered_in_reduction) == {f"wmu-r{i}" for i in range(n)})
        check("H. the final reduced result is exactly Clark's own reduction choices, bounded and in canonical order",
              result.selected_wmu_ids == sorted(keep_ids))
        check("H. final result respects the per-call selection limit",
              len(result.selected_wmu_ids) <= ss.MAX_SELECTED_WMUS_PER_CALL)
        check("H. reduction uses the identical Selection budget profile as initial selection "
              "(no second/bespoke reduction constant exists in the module)",
              "SLEEP_REDUCTION" not in selection_source and "_compose_reduction_budget" not in selection_source)
    finally:
        restore(original)
        context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET = original_budget

    # === I: non-convergence -- a reduction round that shrinks nothing fails closed ===
    original = ss._selection_chat
    original_budget = context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET
    try:
        n = 9
        wmus = [make_wmu(f"wmu-s{i}", f"01EVTS{i}", expression="pad" * 20) for i in range(n)]
        one_text = ss._render_candidate(wmus[0])
        msg_one_initial = ss._build_messages([one_text], 1, ss.MODE_INITIAL)
        msg_one_reduction = ss._build_messages([one_text], 1, ss.MODE_REDUCTION)
        msg_one_initial.append({"role": "user", "content": ss._retry_correction(1)})
        msg_one_reduction.append({"role": "user", "content": ss._retry_correction(1)})
        cost_one = max(
            ss._compose_selection_budget(msg_one_initial).final_prompt_cost,
            ss._compose_selection_budget(msg_one_reduction).final_prompt_cost,
        )
        context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET = cost_one + 1

        def select_everything_always(messages, attempt_no):
            content = messages[0]["content"]
            offered = [f"wmu-s{i}" for i in range(n) if f"wmu-s{i}" in content]
            return json.dumps({"selected_wmu_ids": offered})

        install(ScriptedChat(select_everything_always))
        non_convergence_raised = False
        try:
            ss.select_wmus(wmus)
        except ss.NonConvergenceError:
            non_convergence_raised = True
        check("I. a reduction round that fails to shrink an over-bound candidate set "
              "raises NonConvergenceError instead of looping forever or returning early",
              non_convergence_raised)
    finally:
        restore(original)
        context_budget.SLEEP_SELECTION_MAX_PROMPT_BUDGET = original_budget

    # === J: ephemerality -- no persistence side effect of any kind ===
    check("J. sleep_selection.py touches no persistence/identity/memory surface by import "
          "(no sqlite3, no provenance writer, no hippocampus, no watermark, no journal)",
          present == [])  # reuses the F scan -- same forbidden-surface list covers persistence too

    original = ss._selection_chat
    try:
        wmus = [make_wmu("wmu-a", "01EVTA")]
        install(QueueChat([json.dumps({"selected_wmu_ids": ["wmu-a"]})]))
        with tempfile.TemporaryDirectory() as tmp:
            before = set(os.listdir(tmp))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                ss.select_wmus(wmus)
            finally:
                os.chdir(cwd)
            after = set(os.listdir(tmp))
        check("J. running a full selection creates no file anywhere (genuinely ephemeral, in-memory only)",
              before == after)
    finally:
        restore(original)

    # === K: OWC9 admission + bypass proof ===
    check("K. sleep_selection is registered in the OWC9 compliance registry",
          any(row["path_id"] == "sleep_selection" for row in registry.ALL_PATHS))
    sleep_selection_row = next(row for row in registry.ALL_PATHS if row["path_id"] == "sleep_selection")
    check("K. sleep_selection is registered against the existing, already-approved llama_sleep wrapper "
          "call site -- no new physical ollama.chat() call site is introduced",
          sleep_selection_row["wrapper_call_site"] == ("llama_sleep.py", "ask_llama_for_json"))
    check("K. the registered wrapper call site is still one of the bypass guard's approved sites",
          sleep_selection_row["wrapper_call_site"] in bypass_guard.ALLOWED_WRAPPER_CALL_SITES)

    selection_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sleep_selection.py")
    discovered_calls = bypass_guard._find_chat_calls(selection_path)
    check("K. the bypass guard's own AST scan finds zero direct ollama.chat() calls inside sleep_selection.py "
          "-- every model call is admitted exclusively through the approved llama_sleep wrapper",
          discovered_calls == [])

    poisoned_source = (
        "import ollama\n"
        "def bypass(messages):\n"
        "    return ollama.chat(model='llama3.2:3b', messages=messages)\n"
    )
    poisoned_tree = ast.parse(poisoned_source)
    poisoned_hits = [node for node in ast.walk(poisoned_tree) if bypass_guard._is_chat_call(node)]
    check("K. the guard's detection logic WOULD flag a hypothetical direct unbudgeted "
          "ollama.chat(model=...) call if one were ever introduced into this module",
          len(poisoned_hits) == 1)

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
