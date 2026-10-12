"""Sleep/REM mechanical budget behavior at PRODUCTION-SIZED material.

The real subject-chosen Sleep event is live-gated; the engineering around it is
not. Selection and Transformation run llama3.2:3b under a 4096-token ceiling and
admitted every prompt on the byte upper bound alone. Transformation's input
budget is 2432 with ~1250 bytes of fixed instructions, so ONE ordinary exchange
(a 1310-character human message plus a 1580-character reply, both observed in
production) exceeded it -- OversizeWmuError failed the ENTIRE cycle, atomically
and forever (the watermark never advances, the same window is retried). Nothing
here performs a real Sleep cycle or calls a model: chats and the measurer are
scripted fakes; canonical databases are synthetic.
"""
import json
import math
import os
import sys
import time

import pytest

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
if ANAXI_FINAL not in sys.path:
    sys.path.insert(0, ANAXI_FINAL)

import context_budget
import llama_sleep
import sleep_selection as ss
import sleep_transformation as st

HUMAN_CHARS, REPLY_CHARS = 1310, 1580     # observed production exchange sizes


def exchange_wmu(index, human_chars=HUMAN_CHARS, reply_chars=REPLY_CHARS):
    return {
        "wmu_id": f"wmu-{index:026x}", "primary_event_id": f"01EVENT{index:019d}",
        "selection_bearing": [
            {"memory_kind": "human_expression", "expression": ("Alex wrote about the day. " * 3000)[:human_chars]},
            {"memory_kind": "clark_expression", "expression": ("Clark answered at length. " * 3000)[:reply_chars]},
        ],
        "supporting_provenance": [{"note": "must never reach the model"}],
    }


class Measurer:
    """A provider that counts ~4 bytes per token (real prose) and records use."""
    def __init__(self, bytes_per_token=4):
        self.calls = 0
        self.bytes_per_token = bytes_per_token

    def __call__(self, messages):
        self.calls += 1
        return math.ceil(sum(len(m["content"].encode()) for m in messages) / self.bytes_per_token) + 8


def first_id_chat(messages):
    prompt = messages[0]["content"]
    ids = [line.split()[1].rstrip(":") for line in prompt.splitlines() if line.startswith("WMU wmu-")]
    return json.dumps({"selected_wmu_ids": ids[:1]})


def derive_chat(messages):
    prompt = messages[0]["content"]
    ids = [line.split()[1].rstrip(":") for line in prompt.splitlines() if line.startswith("WMU wmu-")]
    return json.dumps({"derivations": [{"source_wmu_ids": ids[:1], "derived_text": "A tentative association."}]})


# ------------------------------------------------------------ the failure --

def test_one_ordinary_exchange_failed_the_whole_cycle_on_the_byte_bound_alone():
    wmu = exchange_wmu(1)
    with pytest.raises(st.OversizeWmuError):
        st.pack_whole_wmus([wmu])                      # estimator-only: the historical behavior
    with pytest.raises(ss.OversizeCandidateError):
        ss._pack_whole_candidates([exchange_wmu(2, 1800, 2000)], ss.MODE_INITIAL)


def test_measured_admission_lets_ordinary_exchanges_through_selection_and_transformation():
    measure = Measurer()
    wmus = [exchange_wmu(i) for i in range(1, 9)]
    selection = ss.select_wmus(wmus, measure=measure, chat=first_id_chat)
    assert selection.selected_wmu_ids                       # a real, valid selection
    derivations = st.transform(wmus[:4], measure=measure, chat=derive_chat)
    assert derivations and derivations[0].derived_text == "A tentative association."
    assert measure.calls > 0


def test_every_selected_wmu_is_offered_whole_never_clipped():
    seen = []

    def chat(messages):
        seen.append(messages[0]["content"])
        return derive_chat(messages)

    wmu = exchange_wmu(3)
    st.transform([wmu], measure=Measurer(), chat=chat)
    for entry in wmu["selection_bearing"]:
        assert repr(entry["expression"]) in seen[0]        # the full text, byte for byte


def test_true_overflow_is_still_a_closed_failure_and_calls_no_model():
    huge = exchange_wmu(4, 30000, 30000)                   # real cost far beyond the window
    measure = Measurer()
    with pytest.raises(st.OversizeWmuError):
        st.pack_whole_wmus([huge], measure=measure)
    with pytest.raises(ss.OversizeCandidateError):
        ss._pack_whole_candidates([huge], ss.MODE_INITIAL, measure)
    assert measure.calls == 0                              # hopeless: never probed, never generated


def test_a_wmu_whose_real_cost_does_not_fit_is_not_rescued_by_measurement():
    borderline = exchange_wmu(5, 4800, 4800)               # ~9600 bytes: ~2400 real tokens + prompt > 2432? measured says no
    measure = Measurer(bytes_per_token=3)                  # denser text (non-English/code)
    with pytest.raises(st.OversizeWmuError):
        st.pack_whole_wmus([borderline], measure=measure)


def test_an_untrustworthy_or_failed_probe_leaves_the_rejection_in_force():
    for probe in (lambda m: None, lambda m: 0, lambda m: -5, lambda m: "fits", lambda m: 10 ** 9):
        with pytest.raises(st.OversizeWmuError):
            st.pack_whole_wmus([exchange_wmu(6)], measure=probe)


def test_a_full_day_of_exchanges_completes_with_bounded_probing():
    """~400 production-sized exchanges (the real conversation length) select
    without a permanent Oversize failure, and probing is bounded by the count of
    admissions the byte bound could not decide."""
    measure = Measurer()
    wmus = [exchange_wmu(i) for i in range(1, 401)]
    result = ss.select_wmus(wmus, measure=measure, chat=first_id_chat)
    assert result.batch_count >= 1 and result.selected_wmu_ids
    assert measure.calls <= 3 * len(wmus)


# ------------------------------------------------- the model wrapper itself --

class FakeOllama:
    def __init__(self, **response):
        self.requests, self.response = [], response

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        return dict({"message": {"content": "{\"selected_wmu_ids\": []}"}, "done": True,
                     "done_reason": "stop", "prompt_eval_count": 900, "eval_count": 10}, **self.response)


def test_production_generation_is_bounded_and_pinned_to_the_budgeted_ceiling(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr(llama_sleep, "ollama", fake)
    llama_sleep.selection_chat([{"role": "user", "content": "x"}])
    options = fake.requests[0]["options"]
    assert options["num_predict"] == context_budget.SLEEP_SELECTION_GENERATION_RESERVE
    assert options["num_ctx"] == context_budget.SLEEP_LLAMA_CONTEXT_CEILING
    llama_sleep.transformation_chat([{"role": "user", "content": "x"}])
    assert fake.requests[1]["options"]["num_predict"] == context_budget.SLEEP_TRANSFORMATION_GENERATION_RESERVE


def test_a_generation_cut_off_at_its_bound_is_never_parsed_as_an_answer(monkeypatch):
    monkeypatch.setattr(llama_sleep, "ollama", FakeOllama(done_reason="length"))
    with pytest.raises(llama_sleep.SleepGenerationTruncated):
        llama_sleep.selection_chat([{"role": "user", "content": "x"}])


def test_the_measurement_probe_generates_nothing_and_rejects_untrustworthy_counts(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr(llama_sleep, "ollama", fake)
    assert llama_sleep.measure_prompt_tokens([{"role": "user", "content": "x"}]) == 900
    assert fake.requests[0]["options"]["num_predict"] == 1
    for bad in (0, -1, 4095, 4096, None, "n"):
        monkeypatch.setattr(llama_sleep, "ollama", FakeOllama(prompt_eval_count=bad))
        assert llama_sleep.measure_prompt_tokens([{"role": "user", "content": "x"}]) is None

    class Boom:
        def chat(self, **kwargs):
            raise RuntimeError("server down")

    monkeypatch.setattr(llama_sleep, "ollama", Boom())
    assert llama_sleep.measure_prompt_tokens([{"role": "user", "content": "x"}]) is None


def test_ordinary_callers_keep_the_estimator_only_no_model_default(monkeypatch):
    """Tests (and any caller not opting in) never probe a model."""
    monkeypatch.setattr(llama_sleep, "ollama", None)       # any use would raise
    with pytest.raises(st.OversizeWmuError):
        st.pack_whole_wmus([exchange_wmu(7)])
    assert st.pack_whole_wmus([exchange_wmu(8, 200, 200)])  # small units still pack without a model


# ------------------------------------------- the whole cycle, synthetic DB --

def _seed_window(name, turns, chars):
    import test_sleep_cycle as tsc
    conn, ids = tsc.fresh_db(name)
    for i in range(turns):
        tsc.add_eligible_turn(conn, ids, f"01EVT{i:021d}", ("Clark's reflective reply. " * 2000)[:chars], occurred_at=100 + i)
    import sleep_c_schema
    sleep_c_schema.ensure_schema(conn)
    return conn


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_a_production_sized_window_completes_atomically_and_never_reprocesses():
    import sleep_cycle as cycle
    import sleep_watermark as watermark

    conn = _seed_window("prod_sized_ok", turns=40, chars=2600)
    measure = Measurer()
    before = watermark.read_watermark_from_conn(conn)["last_processed_component_id"]
    result = cycle.run_sleep_cycle(conn, measure=measure, selection_chat=first_id_chat, transformation_chat=derive_chat)
    assert result.status == "completed" and result.selected_count >= 1 and result.derivation_count >= 1
    after = watermark.read_watermark_from_conn(conn)["last_processed_component_id"]
    assert after == result.window_end_component_id > before
    assert _count(conn, "sleep_cycles") == 1 and _count(conn, "sleep_derivations") == result.derivation_count
    again = cycle.run_sleep_cycle(conn, measure=measure, selection_chat=first_id_chat, transformation_chat=derive_chat)
    # A cycle covers one bounded window. The next starts exactly where the last
    # ended, so no source is ever selected twice and none is skipped.
    assert again.status == "completed"
    assert again.window_start_component_id == result.window_end_component_id
    assert again.window_end_component_id > result.window_end_component_id
    assert _count(conn, "sleep_cycles") == 2


def test_a_true_overflow_fails_closed_with_no_partial_state_and_no_fabricated_sleep():
    import sleep_cycle as cycle
    import sleep_watermark as watermark

    conn = _seed_window("prod_sized_overflow", turns=3, chars=2600)
    import test_sleep_cycle as tsc
    tsc.add_eligible_turn(conn, {"pipeline_id": "pipe-llama-1", "clark_id": "actor-clark-1"},
                          "01EVTHUGE00000000000000001", "x" * 60000, occurred_at=500)
    before = watermark.read_watermark_from_conn(conn)["last_processed_component_id"]
    with pytest.raises(ss.SleepSelectionError):
        cycle.run_sleep_cycle(conn, measure=Measurer(), selection_chat=first_id_chat, transformation_chat=derive_chat)
    assert watermark.read_watermark_from_conn(conn)["last_processed_component_id"] == before
    assert _count(conn, "sleep_cycles") == 0 and _count(conn, "sleep_derivations") == 0


def test_the_production_entrypoint_supplies_measurement_and_bounded_generation(monkeypatch):
    """The owner-surface Sleep executes run_sleep_cycle_with_production_defaults;
    it must opt into measured admission and the budgeted generation bound."""
    import sleep_cycle as cycle

    captured = {}

    def fake_run(conn, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop before touching anything")

    monkeypatch.setattr(cycle, "run_sleep_cycle", fake_run)
    monkeypatch.setattr(cycle.schema.SleepCPaths, "production_defaults",
                        classmethod(lambda cls: type("P", (), {"provenance_db_path": ":memory:"})()))
    with pytest.raises(RuntimeError):
        cycle.run_sleep_cycle_with_production_defaults()
    assert captured["measure"] is llama_sleep.measure_prompt_tokens
    assert captured["selection_chat"] is llama_sleep.selection_chat
    assert captured["transformation_chat"] is llama_sleep.transformation_chat
