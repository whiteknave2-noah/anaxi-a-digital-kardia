"""
ANAXI SLP1-C -- behavioral regression suite for dormant Sleep
Transformation (sleep_transformation.py).

No real model calls anywhere in this file -- sleep_transformation._transformation_chat
is monkeypatched to a scripted fake for every test. No database: this
module operates purely on WMU dicts and in-memory Derivation objects.

Run:
    python test_sleep_transformation.py
"""

import ast
import json
import os
import sys

import context_budget
import sleep_transformation as st
import test_owc9p4_bypass_guard as bypass_guard
import budget_compliance_registry as registry


def make_wmu(wmu_id, primary_event_id, expression="hello there", memory_kind="clark_expression"):
    return {
        "wmu_id": wmu_id,
        "primary_event_id": primary_event_id,
        "selection_bearing": [{"memory_kind": memory_kind, "expression": expression}],
        "supporting_provenance": [{"note": "must never reach the model"}],
    }


class QueueChat:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        return self.responses.pop(0)


class ScriptedChat:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        return self.script(messages, len(self.calls))


def install(fake):
    original = st._transformation_chat
    st._transformation_chat = fake
    return original


def restore(original):
    st._transformation_chat = original


def run_test_suite():
    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(bool(cond))

    # === 1. TRANSFORMATION CONTRACT: table-driven validator proof ===
    offered = ["wmu-a", "wmu-b"]

    valid_case = json.dumps({"derivations": [{"source_wmu_ids": ["wmu-a"], "derived_text": "an association"}]})
    check("1. a valid single derivation parses correctly",
          st._validate_transformation_response(valid_case, offered, 4) == [(["wmu-a"], "an association")])

    zero_case = json.dumps({"derivations": []})
    check("1. zero derivations is a valid parse",
          st._validate_transformation_response(zero_case, offered, 4) == [])

    invalid_cases = [
        ("malformed JSON", "not json at all"),
        ("extra semantic field on the derivation item",
         json.dumps({"derivations": [{"source_wmu_ids": ["wmu-a"], "derived_text": "x", "confidence": 0.9}]})),
        ("extra top-level field",
         json.dumps({"derivations": [], "note": "hi"})),
        ("missing top-level key",
         json.dumps({"other": []})),
        ("unoffered source id",
         json.dumps({"derivations": [{"source_wmu_ids": ["wmu-not-offered"], "derived_text": "x"}]})),
        ("empty source list",
         json.dumps({"derivations": [{"source_wmu_ids": [], "derived_text": "x"}]})),
        ("empty derived_text",
         json.dumps({"derivations": [{"source_wmu_ids": ["wmu-a"], "derived_text": ""}]})),
        ("overlength derived_text",
         json.dumps({"derivations": [{"source_wmu_ids": ["wmu-a"], "derived_text": "z" * (st.MAX_DERIVATION_CHARS + 1)}]})),
        ("too many derivations",
         json.dumps({"derivations": [{"source_wmu_ids": ["wmu-a"], "derived_text": "x"}] * (st.MAX_DERIVATIONS_PER_CALL + 1)})),
        ("wrong type for source_wmu_ids",
         json.dumps({"derivations": [{"source_wmu_ids": "wmu-a", "derived_text": "x"}]})),
        ("wrong type for derived_text",
         json.dumps({"derivations": [{"source_wmu_ids": ["wmu-a"], "derived_text": 5}]})),
        ("derivations is not a list",
         json.dumps({"derivations": {"source_wmu_ids": ["wmu-a"], "derived_text": "x"}})),
    ]
    for label, raw in invalid_cases:
        check(f"1. validation table rejects: {label}",
              st._validate_transformation_response(raw, offered, st.MAX_DERIVATIONS_PER_CALL) is None)

    dup_case = json.dumps({"derivations": [{"source_wmu_ids": ["wmu-b", "wmu-a", "wmu-a"], "derived_text": "x"}]})
    check("1. exact duplicate source ids inside one candidate dedupe and restore canonical (offered) order",
          st._validate_transformation_response(dup_case, offered, 4) == [(["wmu-a", "wmu-b"], "x")])

    # === 2. CONTRADICTIONS COEXIST ===
    original = st._transformation_chat
    try:
        wmus = [make_wmu("wmu-a", "01EVTA", "The garden was watered today."),
                make_wmu("wmu-b", "01EVTB", "The garden was never watered.")]
        contradictory = json.dumps({"derivations": [
            {"source_wmu_ids": ["wmu-a", "wmu-b"], "derived_text": "P: the garden was watered."},
            {"source_wmu_ids": ["wmu-a", "wmu-b"], "derived_text": "not-P: the garden was not watered."},
        ]})
        install(QueueChat([contradictory]))
        derivations = st.transform(wmus)
        check("2. both contradictory derivations are returned -- no host resolution, no suppression",
              {d.derived_text for d in derivations} == {
                  "P: the garden was watered.", "not-P: the garden was not watered."
              })
        check("2. both cite the same offered source WMUs, unchanged",
              all(d.source_wmu_ids == ["wmu-a", "wmu-b"] for d in derivations))
    finally:
        restore(original)

    # === 3. WHOLE-WMU BATCHING (+ oversize) ===
    original = st._transformation_chat
    original_budget = context_budget.SLEEP_TRANSFORMATION_MAX_PROMPT_BUDGET
    try:
        wmus = [make_wmu(f"wmu-{i}", f"01EVT{i}", expression="pad" * 20) for i in range(3)]
        from sleep_selection import _render_candidate
        one_text = _render_candidate(wmus[0])
        msg_one = st._build_messages([one_text], st.MAX_DERIVATIONS_PER_CALL, [wmus[0]["wmu_id"]])
        msg_one.append({"role": "user", "content": st._retry_correction(st.MAX_DERIVATIONS_PER_CALL)})
        cost_one = st._compose_transformation_budget(msg_one).final_prompt_cost
        two_texts = [_render_candidate(wmus[0]), _render_candidate(wmus[1])]
        msg_two = st._build_messages(two_texts, st.MAX_DERIVATIONS_PER_CALL, [wmus[0]["wmu_id"], wmus[1]["wmu_id"]])
        msg_two.append({"role": "user", "content": st._retry_correction(st.MAX_DERIVATIONS_PER_CALL)})
        cost_two = st._compose_transformation_budget(msg_two).final_prompt_cost

        context_budget.SLEEP_TRANSFORMATION_MAX_PROMPT_BUDGET = cost_one + 1
        assert cost_two > cost_one + 1, "fixture too small to prove batching -- widen the padding"

        offered_per_call = []

        def record_and_return_none(messages, attempt_no):
            content = messages[0]["content"]
            offered_per_call.append([wid for wid in ("wmu-0", "wmu-1", "wmu-2") if wid in content])
            return json.dumps({"derivations": []})

        install(ScriptedChat(record_and_return_none))
        derivations = st.transform(wmus)
        check("3. every whole WMU lands in its own batch when only one fits per call",
              len(offered_per_call) == 3)
        check("3. every selected WMU receives exactly one Transformation opportunity, in canonical order",
              offered_per_call == [["wmu-0"], ["wmu-1"], ["wmu-2"]])
        check("3. zero derivations across all batches is a valid successful result",
              derivations == [])
        restore(original)

        context_budget.SLEEP_TRANSFORMATION_MAX_PROMPT_BUDGET = cost_one - 1
        fake2 = QueueChat([json.dumps({"derivations": []})] * 5)
        install(fake2)
        oversize_raised = False
        try:
            st.transform(wmus)
        except st.OversizeWmuError:
            oversize_raised = True
        check("3. a WMU that cannot fit alone raises OversizeWmuError, no partial/complete-cycle success",
              oversize_raised and len(fake2.calls) == 0)
    finally:
        restore(original)
        context_budget.SLEEP_TRANSFORMATION_MAX_PROMPT_BUDGET = original_budget

    # === retry law ===
    original = st._transformation_chat
    try:
        wmus = [make_wmu("wmu-a", "01EVTA")]
        fake = QueueChat(["garbage", json.dumps({"derivations": [{"source_wmu_ids": ["wmu-a"], "derived_text": "ok"}]})])
        install(fake)
        derivations = st.transform(wmus)
        check("retry: an invalid response followed by a valid retry succeeds using exactly two attempts",
              len(derivations) == 1 and derivations[0].derived_text == "ok" and len(fake.calls) == 2)
        restore(original)

        fake2 = QueueChat(["garbage", "still garbage"])
        install(fake2)
        raised = False
        try:
            st.transform(wmus)
        except st.TransformationValidationError:
            raised = True
        check("retry: two consecutive invalid responses fail the whole Transformation stage",
              raised and len(fake2.calls) == 2)
    finally:
        restore(original)

    # === dedupe_derivations (cycle-level exact-duplicate-tuple collapse) ===
    d1 = st.Derivation(["wmu-a", "wmu-b"], "same text", batch_index=0, item_index=0)
    d2 = st.Derivation(["wmu-b", "wmu-a"], "same text", batch_index=1, item_index=0)  # same set, different order -> same tuple
    d3 = st.Derivation(["wmu-a"], "different text", batch_index=1, item_index=1)
    deduped = st.dedupe_derivations([d1, d2, d3])
    check("dedupe: exact duplicate (derived_text, canonical source set) tuples collapse with no added weight",
          len(deduped) == 2 and deduped[0] is d1 and deduped[1] is d3)

    # === no source reopening / ephemerality (static scan) ===
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sleep_transformation.py"),
              "r", encoding="utf-8") as f:
        transformation_source = f.read()
    forbidden_tokens = [
        "sqlite3", "prov_conn", "native_provenance_writer", "hippocampus",
        "sleep_receipts", "kardia", "mind_graph", "workspace_episode_provenance",
        "qwen", "requests.get", "urlopen", "active_kardia", "govern_identity_revision",
    ]
    present = [tok for tok in forbidden_tokens if tok in transformation_source]
    check("no source reopening / no identity or Kardia surface imported or used -- "
          f"found: {present!r}", present == [])

    # === OWC9 admission + bypass proof ===
    check("OWC9: sleep_transformation is registered in the compliance registry",
          any(row["path_id"] == "sleep_transformation" for row in registry.ALL_PATHS))
    row = next(r for r in registry.ALL_PATHS if r["path_id"] == "sleep_transformation")
    check("OWC9: registered against the existing, already-approved llama_sleep wrapper call site",
          row["wrapper_call_site"] == ("llama_sleep.py", "ask_llama_for_json"))
    check("OWC9: the registered wrapper call site is one of the bypass guard's approved sites",
          row["wrapper_call_site"] in bypass_guard.ALLOWED_WRAPPER_CALL_SITES)

    transformation_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sleep_transformation.py")
    discovered_calls = bypass_guard._find_chat_calls(transformation_path)
    check("OWC9: the bypass guard's own AST scan finds zero direct ollama.chat() calls in sleep_transformation.py",
          discovered_calls == [])

    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")
    return sum(results) == len(results)


if __name__ == "__main__":
    success = run_test_suite()
    sys.exit(0 if success else 1)
