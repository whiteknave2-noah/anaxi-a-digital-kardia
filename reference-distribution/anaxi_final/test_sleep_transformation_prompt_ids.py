"""The Transformation prompt states the exact wmu_ids valid in THIS call (real llama3.2:3b
invented a non-existent id in ~1 of 6 attempts; two in a row fail the whole cycle)."""
import sleep_transformation as st


def _wmu(i):
    return {"wmu_id": f"wmu-{i:026x}", "primary_event_id": f"01E{i}",
            "selection_bearing": [{"memory_kind": "clark_expression", "expression": "hello"}],
            "supporting_provenance": []}


def test_call_prompt_names_exactly_the_offered_ids():
    seen = []

    def chat(messages):
        seen.append(messages[0]["content"])
        return '{"derivations": []}'

    batch = [_wmu(1), _wmu(2)]
    st.transform(batch, chat=chat)
    prompt = seen[0]
    assert "The ONLY valid wmu_id values in this call are: " + ", ".join(w["wmu_id"] for w in batch) + "." in prompt
    assert _wmu(3)["wmu_id"] not in prompt
