"""Opt-in: Sleep Selection and Transformation with the REAL production Sleep
model (llama3.2:3b via Ollama) on synthetic, production-sized waking exchanges.

No database, no canonical write, no watermark, no Private Space: it drives the
two model stages (sleep_selection.select_wmus / sleep_transformation.transform)
directly with the production measurer and chats. Checks that the real model's
output is accepted by the real validators at production sizes -- the class of
failure (format, size, budget) that scripted-fake tests cannot reveal.
Enable with ANAXI_REAL_MODEL_HARNESS=1.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ANAXI_REAL_MODEL_HARNESS") != "1", reason="opt-in real-model harness",
)

HUMAN = [
    "I spent the morning repotting the ferns on the porch, and it got me thinking about how slowly some things change. ",
    "We drove out to the coast yesterday and the fog never lifted, but the tide pools were worth it anyway. ",
    "My sister called about the family reunion; she wants to hold it at the lake house in August if the weather agrees. ",
    "I have been reading an old field guide to birds and keep noticing how many names describe the sound, not the look. ",
]
CLARK = [
    "Repotting sounds like patient work. Roots that have outgrown their pot tell you exactly what they need, if you look. ",
    "Fog on the coast changes what you notice: sound, smell, the edges of things close by rather than the horizon. ",
    "A reunion at the lake house sounds like it could hold a lot of people comfortably, and August is a generous month for it. ",
    "Names built from sounds are a kind of listening preserved in language; a bird known by its call is known by attention. ",
]


def _wmu(i, human_chars=1310, reply_chars=1580):
    human = (HUMAN[i % 4] * 40)[:human_chars]
    reply = (CLARK[i % 4] * 40)[:reply_chars]
    return {
        "wmu_id": f"wmu-{i:026x}", "primary_event_id": f"01EVENT{i:019d}",
        "selection_bearing": [
            {"memory_kind": "human_expression", "expression": human},
            {"memory_kind": "clark_expression", "expression": reply},
        ],
        "supporting_provenance": [{"note": "must never reach the model"}],
    }


def test_real_selection_and_transformation_accept_production_sized_exchanges():
    import llama_sleep
    import sleep_selection as ss
    import sleep_transformation as st

    wmus = [_wmu(i) for i in range(1, 5)]
    selection = ss.select_wmus(wmus, measure=llama_sleep.measure_prompt_tokens, chat=llama_sleep.selection_chat)
    print("\n[sleep] selected", len(selection.selected_wmu_ids), "of", len(wmus))
    assert set(selection.selected_wmu_ids) <= {w["wmu_id"] for w in wmus}      # zero selection is lawful too
    chosen = [w for w in wmus if w["wmu_id"] in selection.selected_wmu_ids] or wmus[:1]
    def chat(messages):
        raw = llama_sleep.transformation_chat(messages)
        print("[sleep] raw transformation output:", repr(raw[:400]), "...", len(raw))
        return raw

    derivations = st.transform(chosen, measure=llama_sleep.measure_prompt_tokens, chat=chat)
    print("[sleep] derivations", len(derivations), [d.derived_text[:70] for d in derivations])
    for d in derivations:                                                        # provenance: only offered sources
        assert set(d.source_wmu_ids) <= {w["wmu_id"] for w in chosen}


def test_full_sleep_cycle_with_the_real_model_on_a_synthetic_canonical_db():
    """The whole lawful cycle -- window, Selection, Transformation, landing, receipt,
    watermark -- with the real llama3.2:3b on a SYNTHETIC canonical DB (never the live
    one). A second cycle finds no new work (no duplicate, watermark advanced once)."""
    import sleep_cycle
    import llama_sleep
    import test_sleep_v1_acceptance as accepted

    conn, _path, ids = accepted.fresh_prov_conn("real_model_full_cycle")
    conn.isolation_level = None
    for i in range(6):
        accepted.add_eligible_turn(
            conn, ids, f"evt-real-{i}", (CLARK[i % 4] * 3)[:420], occurred_at=100 + i,
        )
    conn.commit()
    kwargs = dict(measure=llama_sleep.measure_prompt_tokens, selection_chat=llama_sleep.selection_chat,
                  transformation_chat=llama_sleep.transformation_chat)
    first = sleep_cycle.run_sleep_cycle(conn, owner_id="real-model-owner", now=1000, **kwargs)
    print("\n[sleep] cycle:", first.status, "selected", first.selected_count, "derivations", first.derivation_count)
    assert first.status == "completed"
    rows = conn.execute("SELECT COUNT(*) FROM sleep_derivations").fetchone()[0]
    assert rows == first.derivation_count
    second = sleep_cycle.run_sleep_cycle(conn, owner_id="real-model-owner", now=2000, **kwargs)
    assert second.status == "no_work"
    assert conn.execute("SELECT COUNT(*) FROM sleep_cycles").fetchone()[0] == 1
    conn.close()
