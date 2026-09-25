"""Tests for ui_turn_diagnostics.py -- the observability-only per-turn
instrumentation added for the recurrent turn-accumulation UI failure
gate. Every test uses a temporary log path. No model call, no real
Gradio server, no production log file touched anywhere in this file.
"""
import json
import os
import shutil
import sys
import tempfile
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import ui_turn_diagnostics as utd

TEST_ROOT = tempfile.mkdtemp(prefix="ui_turn_diag_test_")


def fresh_log_path(name):
    return os.path.join(TEST_ROOT, name, "ui_turn_diagnostics.jsonl")


def read_records(log_path):
    if not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def setup_method():
    utd.reset_turn_counter()


# ================================================================= A: success


def test_successful_turn_records_full_shape():
    setup_method()
    log_path = fresh_log_path("success")

    def fake_run():
        raw1 = utd.timed_model_call("pass1", lambda: "act-json")
        raw2 = utd.timed_model_call("pass2", lambda: "expression-json")
        native = utd.timed_stage("persistence", lambda: {"event_id": "evt-1"})
        return {"reply": "Hello Alex.", "_diagnostic_typed_act": "develop_current", "native_event_id": native["event_id"]}

    result = utd.wrap_conversation_callback(
        "hi there", [{"role": "user", "content": "hi"}], run_fn=fake_run,
        log_path=log_path, session_id_fn=lambda: "sess-abc",
    )
    assert result["reply"] == "Hello Alex."

    records = read_records(log_path)
    assert len(records) == 1
    r = records[0]
    assert r["callback_success"] is True
    assert r["turn_ordinal"] == 1
    assert r["session_id"] == "sess-abc"
    assert r["incoming_message_char_count"] == len("hi there")
    assert r["visible_history_item_count"] == 1
    assert r["visible_history_json_serializable"] is True
    assert r["response_char_count"] == len("Hello Alex.")
    assert r["typed_act"] == "develop_current"
    assert {"purpose": "pass1", "duration_ms": r["stages"][0]["duration_ms"], "success": True} == r["stages"][0]
    assert r["stages"][1]["purpose"] == "pass2"
    assert r["stages"][2]["purpose"] == "persistence"
    assert r["total_model_call_duration_ms"] >= 0
    assert r["total_persistence_duration_ms"] >= 0
    assert r["callback_duration_ms"] >= 0
    assert "timestamp_start" in r and "timestamp_end" in r
    assert "resource_start" in r and "resource_end" in r


def test_turn_ordinal_increments_across_calls():
    setup_method()
    log_path = fresh_log_path("ordinal")
    for _ in range(3):
        utd.wrap_conversation_callback("hi", [], run_fn=lambda: {"reply": "ok"}, log_path=log_path)
    records = read_records(log_path)
    assert [r["turn_ordinal"] for r in records] == [1, 2, 3]


# ============================================================ B/C/D: staged exceptions


class FakeConversationDirectionFailure(Exception):
    def __init__(self, stage, code):
        self.stage = stage
        self.failure_code = code
        super().__init__(f"conversation direction failed at {stage}: {code}")


FakeConversationDirectionFailure.__name__ = "ConversationDirectionFailure"


class FakeStagingDurabilityError(Exception):
    pass


FakeStagingDurabilityError.__name__ = "StagingDurabilityError"


def test_pass1_exception_recorded_with_correct_stage_and_reraised():
    setup_method()
    log_path = fresh_log_path("pass1_exc")

    def fake_run():
        utd.timed_model_call("pass1", lambda: (_ for _ in ()).throw(FakeConversationDirectionFailure("pass1", "MALFORMED_ACT")))

    try:
        utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
        assert False, "expected exception to propagate"
    except FakeConversationDirectionFailure as exc:
        assert exc.stage == "pass1"  # existing failure semantics preserved exactly

    records = read_records(log_path)
    assert len(records) == 1
    assert records[0]["callback_success"] is False
    assert records[0]["pipeline_stage"] == "pass1"
    assert records[0]["exception_type"] == "ConversationDirectionFailure"
    assert records[0]["stages"][0]["purpose"] == "pass1"
    assert records[0]["stages"][0]["success"] is False


def test_pass2_exception_recorded_with_correct_stage_and_reraised():
    setup_method()
    log_path = fresh_log_path("pass2_exc")

    def fake_run():
        utd.timed_model_call("pass1", lambda: "ok")
        utd.timed_model_call("pass2", lambda: (_ for _ in ()).throw(FakeConversationDirectionFailure("pass2", "MALFORMED_EXPRESSION")))

    try:
        utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
        assert False, "expected exception to propagate"
    except FakeConversationDirectionFailure as exc:
        assert exc.stage == "pass2"

    records = read_records(log_path)
    assert records[0]["pipeline_stage"] == "pass2"
    assert [s["purpose"] for s in records[0]["stages"]] == ["pass1", "pass2"]
    assert records[0]["stages"][1]["success"] is False


def test_persistence_exception_recorded_with_correct_stage_and_reraised():
    setup_method()
    log_path = fresh_log_path("persist_exc")

    def fake_run():
        utd.timed_model_call("pass1", lambda: "ok")
        utd.timed_model_call("pass2", lambda: "ok")
        utd.timed_stage("persistence", lambda: (_ for _ in ()).throw(FakeStagingDurabilityError("disk full")))

    try:
        utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
        assert False, "expected exception to propagate"
    except FakeStagingDurabilityError:
        pass

    records = read_records(log_path)
    assert records[0]["pipeline_stage"] == "persistence"
    assert records[0]["exception_type"] == "StagingDurabilityError"


# ================================================================= E: outer


def test_unrecognized_outer_exception_still_captured_and_reraised():
    setup_method()
    log_path = fresh_log_path("outer_exc")

    def fake_run():
        raise ValueError("something entirely unexpected")

    try:
        utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
        assert False, "expected exception to propagate"
    except ValueError as exc:
        assert str(exc) == "something entirely unexpected"  # unchanged

    records = read_records(log_path)
    assert len(records) == 1
    assert records[0]["callback_success"] is False
    assert records[0]["pipeline_stage"] == "unknown"  # honest -- never a guessed stage
    assert records[0]["exception_type"] == "ValueError"
    assert "something entirely unexpected" in records[0]["exception_traceback"]


def test_bounded_traceback_never_unbounded():
    setup_method()
    log_path = fresh_log_path("bounded_tb")

    def fake_run():
        raise RuntimeError("x" * 50000)

    try:
        utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    except RuntimeError:
        pass
    records = read_records(log_path)
    assert len(records[0]["exception_message"]) <= utd.MAX_EXCEPTION_MESSAGE_CHARS
    assert len(records[0]["exception_traceback"]) <= utd.MAX_TRACEBACK_CHARS + len("...[truncated]")


def test_stage_classification_covers_known_types():
    assert utd._classify_stage(FakeConversationDirectionFailure("pass1", "X")) == "pass1"
    assert utd._classify_stage(FakeConversationDirectionFailure("pass2", "X")) == "pass2"
    assert utd._classify_stage(FakeStagingDurabilityError("x")) == "persistence"

    class FakeStagingIndeterminateStateError(Exception):
        pass
    FakeStagingIndeterminateStateError.__name__ = "StagingIndeterminateStateError"
    assert utd._classify_stage(FakeStagingIndeterminateStateError("x")) == "persistence"

    class FakeStaleSchemaError(Exception):
        pass
    FakeStaleSchemaError.__name__ = "StaleSchemaError"
    assert utd._classify_stage(FakeStaleSchemaError("x")) == "artifact_construction"

    post_commit_exc = RuntimeError("post-canonical-commit artifact write failed for event evt-1: reason")
    assert utd._classify_stage(post_commit_exc) == "post_commit_artifact_write"

    assert utd._classify_stage(ValueError("totally unrelated")) == "unknown"


# ============================================================== F: history growth


def test_increasing_history_produces_increasing_recorded_sizes():
    setup_method()
    log_path = fresh_log_path("history_growth")
    for n in (0, 1, 2, 4, 8):
        history = [{"role": "user", "content": f"message {i}"} for i in range(n)]
        utd.wrap_conversation_callback("hi", history, run_fn=lambda: {"reply": "ok"}, log_path=log_path)
    records = read_records(log_path)
    counts = [r["visible_history_item_count"] for r in records]
    sizes = [r["visible_history_char_count"] for r in records]
    assert counts == [0, 1, 2, 4, 8]
    assert sizes == sorted(sizes)  # monotonically non-decreasing with history length
    assert sizes[0] < sizes[-1]


def test_non_serializable_history_flagged_not_crashed():
    setup_method()
    log_path = fresh_log_path("history_nonserializable")

    class Unserializable:
        pass

    result = utd.wrap_conversation_callback(
        "hi", [Unserializable()], run_fn=lambda: {"reply": "ok"}, log_path=log_path,
    )
    assert result["reply"] == "ok"  # instrumentation never breaks a successful turn
    records = read_records(log_path)
    assert records[0]["visible_history_json_serializable"] is False
    assert records[0]["visible_history_char_count"] is None


# ================================================================== G: no leak


def test_no_content_leak_only_counts_sizes_timings():
    setup_method()
    log_path = fresh_log_path("no_leak")
    secret_message = "SECRET_PROMPT_MARKER_never_should_appear_in_the_log"
    secret_response = "SECRET_RESPONSE_MARKER_never_should_appear_in_the_log"
    secret_history_text = "SECRET_HISTORY_MARKER_never_should_appear_in_the_log"

    def fake_run():
        return {"reply": secret_response, "_diagnostic_typed_act": "develop_current"}

    utd.wrap_conversation_callback(
        secret_message, [{"role": "user", "content": secret_history_text}],
        run_fn=fake_run, log_path=log_path,
    )
    with open(log_path, encoding="utf-8") as f:
        raw_text = f.read()
    assert secret_message not in raw_text
    assert secret_response not in raw_text
    assert secret_history_text not in raw_text

    record = read_records(log_path)[0]
    allowed_top_level_keys = {
        "timestamp_start", "timestamp_end", "turn_ordinal", "session_id",
        "human_input_event_id",  # canonical mechanical ID only, never H text
        "incoming_message_char_count", "visible_history_item_count",
        "visible_history_char_count", "visible_history_json_serializable",
        "resource_start", "resource_end", "callback_duration_ms", "callback_success",
        "waking_turn_success", "control_failure_contained",
        "stages", "total_model_call_duration_ms", "total_persistence_duration_ms",
        "host_transition_duration_ms", "typed_act", "response_type",
        "response_char_count", "response_json_serializable",
        "pass1_response_content_chars", "pass1_done", "pass1_done_reason",
        "pass1_eval_count", "pass1_prompt_eval_count", "pass1_total_duration",
        "pass1_load_duration", "pass1_eval_duration", "pass1_thinking_chars",
        "pass2_response_content_chars", "pass2_done", "pass2_done_reason",
        "pass2_eval_count", "pass2_prompt_eval_count", "pass2_total_duration",
        "pass2_load_duration", "pass2_eval_duration", "pass2_thinking_chars",
        # OWC9: bounded, generic compositor diagnostics only -- kind
        # names/counts/costs, never rendered content (see
        # test_owc9_context_budget_diagnostics_never_leak_content below
        # for the direct proof of that property).
        "context_budget_results",
    }
    assert set(record.keys()) <= allowed_top_level_keys


def test_no_full_prompt_dumped_even_on_failure():
    setup_method()
    log_path = fresh_log_path("no_leak_failure")
    secret_message = "SECRET_PROMPT_ON_FAILURE_PATH"

    def fake_run():
        raise RuntimeError("boom")

    try:
        utd.wrap_conversation_callback(secret_message, [], run_fn=fake_run, log_path=log_path)
    except RuntimeError:
        pass
    with open(log_path, encoding="utf-8") as f:
        raw_text = f.read()
    assert secret_message not in raw_text


def test_owc9_context_budget_diagnostics_never_leak_content():
    # Section 15: bounded, generic diagnostics only -- kind names,
    # counts, costs, the fixed budget constants -- never the actual
    # rendered content of any contribution.
    import context_budget as cb
    setup_method()
    log_path = fresh_log_path("owc9_no_leak")
    secret = "SECRET_DIALOGUE_CONTENT_never_in_diagnostics"

    def fake_run():
        utd.start_model_call_tracking()
        hard = cb.Contribution(cb.CORE_SYSTEM_CONTROL, "core", hard=True)
        units = [secret]
        soft = cb.Contribution(
            cb.RECENT_DIALOGUE, secret, hard=False,
            droppable_units=list(units), render_fn=lambda u: "".join(u),
        )
        result = cb.compose_within_budget([hard, soft], 1000)
        utd.record_context_budget_result("pass1", result)
        return {"reply": "ordinary reply", "_diagnostic_typed_act": "develop_current"}

    utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    with open(log_path, encoding="utf-8") as f:
        raw_text = f.read()
    assert secret not in raw_text

    record = read_records(log_path)[0]
    assert "context_budget_results" in record
    entry = record["context_budget_results"][0]
    assert entry["pass"] == "pass1"
    assert entry["fits"] is True
    assert cb.RECENT_DIALOGUE in entry["kinds_included"]
    # Only mechanical facts -- no rendered text/content anywhere in the entry.
    for value in entry.values():
        assert secret not in json.dumps(value)


def test_owc9_context_budget_results_absent_when_never_recorded():
    # A turn that never calls record_context_budget_result() (e.g. TASK
    # mode, which has no Pass-1/Pass-2 compositor calls at all) still
    # produces a valid record with an empty list, never a missing key
    # or a stale value from an earlier turn/thread.
    setup_method()
    log_path = fresh_log_path("owc9_absent")

    def fake_run():
        return {"reply": "ordinary reply", "_diagnostic_typed_act": None}

    utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    record = read_records(log_path)[0]
    assert record["context_budget_results"] == []


def test_module_imports_nothing_beyond_minimal_stdlib():
    import ast
    with open(os.path.join(ANAXI_FINAL, "ui_turn_diagnostics.py"), encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert names.issubset({"json", "os", "threading", "time", "traceback", "resource"})
    for forbidden in ("hippocampus_store", "hippocampus_retrieval", "workspace_private",
                       "conversation_direction", "orchestration", "ollama", "anthropic"):
        assert forbidden not in names


# ============================================================ H: no semantic change


def test_instrumentation_never_swallows_a_return_value_change():
    # The wrapper must return EXACTLY what run_fn() returned -- object
    # identity, not a copy/reconstruction -- so no caller downstream of
    # respond() can observe any difference.
    setup_method()
    log_path = fresh_log_path("identity")
    sentinel = {"reply": "identical object", "marker": object()}
    result = utd.wrap_conversation_callback("hi", [], run_fn=lambda: sentinel, log_path=log_path)
    assert result is sentinel


def test_append_only_never_truncates_prior_records():
    setup_method()
    log_path = fresh_log_path("append_only")
    utd.wrap_conversation_callback("hi", [], run_fn=lambda: {"reply": "one"}, log_path=log_path)
    utd.wrap_conversation_callback("hi", [], run_fn=lambda: {"reply": "two"}, log_path=log_path)
    records = read_records(log_path)
    assert len(records) == 2  # both survive -- never overwritten


def test_logging_failure_itself_never_breaks_a_successful_turn():
    setup_method()
    # An unwritable log path (a directory where a file is expected)
    # forces _append_record's own internal write to fail -- the turn
    # itself must still succeed and return normally.
    bad_log_path = os.path.join(TEST_ROOT, "bad_log_dir")
    os.makedirs(bad_log_path, exist_ok=True)  # a directory, not a file path
    result = utd.wrap_conversation_callback("hi", [], run_fn=lambda: {"reply": "still works"}, log_path=bad_log_path)
    assert result["reply"] == "still works"


# ========================================= OWC7-P2: control-failure containment


def test_diagnostic_honesty_fields_success_vs_failure():
    setup_method()
    log_path = fresh_log_path("semantic_split")
    utd.wrap_conversation_callback("hi", [], run_fn=lambda: {"reply": "ok"}, log_path=log_path)
    try:
        utd.wrap_conversation_callback(
            "hi", [],
            run_fn=lambda: (_ for _ in ()).throw(FakeConversationDirectionFailure("pass1", "MALFORMED_ACT")),
            log_path=log_path,
        )
    except FakeConversationDirectionFailure:
        pass
    records = read_records(log_path)
    assert records[0]["callback_success"] is True
    assert records[0]["waking_turn_success"] is True
    assert records[0]["control_failure_contained"] is False
    assert records[1]["callback_success"] is False
    assert records[1]["waking_turn_success"] is False
    assert records[1]["control_failure_contained"] is True  # duck-typed by exception class name only


def test_control_failure_contained_false_for_non_control_exceptions():
    setup_method()
    log_path = fresh_log_path("non_control_exc")
    try:
        utd.wrap_conversation_callback("hi", [], run_fn=lambda: (_ for _ in ()).throw(ValueError("boom")), log_path=log_path)
    except ValueError:
        pass
    record = read_records(log_path)[0]
    assert record["waking_turn_success"] is False
    assert record["control_failure_contained"] is False  # not the ConversationDirectionFailure family


def test_bounded_pass1_failure_preview_short_text_unbounded():
    preview, total_chars, truncated = utd.bounded_pass1_failure_preview("short malformed text")
    assert preview == "short malformed text"
    assert total_chars == len("short malformed text")
    assert truncated is False


def test_bounded_pass1_failure_preview_enforces_bound():
    long_text = "y" * (utd.MAX_RAW_PASS1_FAILURE_PREVIEW_CHARS + 300)
    preview, total_chars, truncated = utd.bounded_pass1_failure_preview(long_text)
    assert len(preview) == utd.MAX_RAW_PASS1_FAILURE_PREVIEW_CHARS
    assert preview == long_text[: utd.MAX_RAW_PASS1_FAILURE_PREVIEW_CHARS]
    assert total_chars == len(long_text)
    assert truncated is True


def test_attach_pass1_failure_preview_then_recorded_on_wrap():
    setup_method()
    log_path = fresh_log_path("raw_preview_recorded")

    def fake_run():
        exc = FakeConversationDirectionFailure("pass1", "MALFORMED_ACT")
        utd.attach_pass1_failure_preview(exc, "the model's actual malformed reply")
        raise exc

    try:
        utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    except FakeConversationDirectionFailure:
        pass
    record = read_records(log_path)[0]
    assert record["raw_pass1_output_preview"] == "the model's actual malformed reply"
    assert record["raw_pass1_output_total_chars"] == len("the model's actual malformed reply")
    assert record["raw_pass1_output_truncated"] is False


def test_raw_preview_absent_when_not_attached():
    # Test G (spec section 19): a successful turn -- and a control
    # failure where the caller never attached a preview (e.g.
    # UNAUTHORIZED_RELINQUISH) -- must not fabricate one.
    setup_method()
    log_path = fresh_log_path("raw_preview_absent")

    def fake_run():
        raise FakeConversationDirectionFailure("pass1", "UNAUTHORIZED_RELINQUISH")  # no preview attached

    try:
        utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    except FakeConversationDirectionFailure:
        pass
    record = read_records(log_path)[0]
    assert record["raw_pass1_output_preview"] is None
    assert record["raw_pass1_output_total_chars"] is None
    assert record["raw_pass1_output_truncated"] is None


def test_successful_pass1_creates_no_raw_shadow_log():
    # Test G (spec section 19): an ordinary successful turn's record
    # has no raw-preview fields at all -- no unnecessary enlargement of
    # observability for the success path.
    setup_method()
    log_path = fresh_log_path("no_shadow_log")
    utd.wrap_conversation_callback("hi", [], run_fn=lambda: {"reply": "ok"}, log_path=log_path)
    record = read_records(log_path)[0]
    assert "raw_pass1_output_preview" not in record
    assert "raw_pass1_output_total_chars" not in record
    assert "raw_pass1_output_truncated" not in record


def test_raw_preview_bound_causes_no_jsonl_corruption():
    # Test F (spec section 19).
    setup_method()
    log_path = fresh_log_path("raw_preview_bound_jsonl")
    long_text = "z" * 5000

    def fake_run():
        exc = FakeConversationDirectionFailure("pass1", "MALFORMED_ACT")
        utd.attach_pass1_failure_preview(exc, long_text)
        raise exc

    try:
        utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    except FakeConversationDirectionFailure:
        pass
    utd.wrap_conversation_callback("hi", [], run_fn=lambda: {"reply": "next turn still fine"}, log_path=log_path)
    records = read_records(log_path)  # read_records() itself proves valid JSONL, line by line
    assert len(records) == 2
    assert records[0]["raw_pass1_output_truncated"] is True
    assert len(records[0]["raw_pass1_output_preview"]) == utd.MAX_RAW_PASS1_FAILURE_PREVIEW_CHARS
    assert records[1]["callback_success"] is True  # second record intact -- no corruption from the first


# ============================================== OWC5-P4: completion metadata


def test_pass1_completion_metadata_recorded_when_supplied():
    setup_method()
    log_path = fresh_log_path("pass1_completion")
    fake_response = {
        "message": {"content": '{"act": "develop_current"}', "thinking": "some hidden reasoning text"},
        "done": True, "done_reason": "stop", "eval_count": 55, "prompt_eval_count": 3664,
        "total_duration": 123456789, "load_duration": 1000, "eval_duration": 987654321,
    }

    def fake_pass1_call():
        utd.record_pass1_completion_metadata(fake_response)
        return fake_response["message"]["content"]

    def fake_run():
        utd.timed_model_call("pass1", fake_pass1_call)
        return {"reply": "ok", "_diagnostic_typed_act": "develop_current"}

    utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    record = read_records(log_path)[0]
    assert record["pass1_response_content_chars"] == len('{"act": "develop_current"}')
    assert record["pass1_done"] is True
    assert record["pass1_done_reason"] == "stop"
    assert record["pass1_eval_count"] == 55
    assert record["pass1_prompt_eval_count"] == 3664
    assert record["pass1_total_duration"] == 123456789
    assert record["pass1_load_duration"] == 1000
    assert record["pass1_eval_duration"] == 987654321
    assert record["pass1_thinking_chars"] == len("some hidden reasoning text")


def test_pass1_thinking_content_never_written_only_length():
    setup_method()
    log_path = fresh_log_path("thinking_no_leak")
    secret_thinking = "SECRET_HIDDEN_REASONING_MARKER_must_never_appear_in_log"
    fake_response = {"message": {"content": "x", "thinking": secret_thinking}}

    def fake_run():
        utd.record_pass1_completion_metadata(fake_response)
        return {"reply": "ok"}

    utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    with open(log_path, encoding="utf-8") as f:
        raw_text = f.read()
    assert secret_thinking not in raw_text
    record = read_records(log_path)[0]
    assert record["pass1_thinking_chars"] == len(secret_thinking)
    assert isinstance(record["pass1_thinking_chars"], int)


def test_pass1_completion_metadata_absent_when_not_supplied():
    # Item 10 (spec section A9): missing optional metadata does not
    # break ordinary turns -- every field simply reads None.
    setup_method()
    log_path = fresh_log_path("pass1_completion_absent")
    utd.wrap_conversation_callback("hi", [], run_fn=lambda: {"reply": "ok"}, log_path=log_path)
    record = read_records(log_path)[0]
    for field in (
        "pass1_response_content_chars", "pass1_done", "pass1_done_reason",
        "pass1_eval_count", "pass1_prompt_eval_count", "pass1_total_duration",
        "pass1_load_duration", "pass1_eval_duration", "pass1_thinking_chars",
    ):
        assert record[field] is None, field


def test_pass1_completion_metadata_recorded_on_failure_branch_too():
    setup_method()
    log_path = fresh_log_path("pass1_completion_failure")
    fake_response = {
        "message": {"content": "", "thinking": "reasoning that consumed the remaining budget"},
        "done": True, "done_reason": "length",
    }

    def fake_run():
        def fake_pass1():
            utd.record_pass1_completion_metadata(fake_response)
            raise FakeConversationDirectionFailure("pass1", "MALFORMED_ACT")
        utd.timed_model_call("pass1", fake_pass1)

    try:
        utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    except FakeConversationDirectionFailure:
        pass
    record = read_records(log_path)[0]
    assert record["pass1_done_reason"] == "length"
    assert record["pass1_thinking_chars"] == len("reasoning that consumed the remaining budget")
    assert record["control_failure_contained"] is True  # unaffected by the added metadata fields


# ============================================== OWC8-S1: Pass-2 completion metadata


def test_pass2_completion_metadata_recorded_when_supplied():
    setup_method()
    log_path = fresh_log_path("pass2_completion")
    fake_response = {
        "message": {"content": '{"expression": "hi"}', "thinking": "some hidden reasoning text"},
        "done": True, "done_reason": "length", "eval_count": 5, "prompt_eval_count": 4091,
        "total_duration": 12096910000, "load_duration": 1000, "eval_duration": 301940000,
    }

    def fake_pass2_call():
        utd.record_pass2_completion_metadata(fake_response)
        return fake_response["message"]["content"]

    def fake_run():
        utd.timed_model_call("pass2", fake_pass2_call)
        return {"reply": "ok", "_diagnostic_typed_act": "ask_human"}

    utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    record = read_records(log_path)[0]
    assert record["pass2_response_content_chars"] == len('{"expression": "hi"}')
    assert record["pass2_done"] is True
    assert record["pass2_done_reason"] == "length"
    assert record["pass2_eval_count"] == 5
    assert record["pass2_prompt_eval_count"] == 4091
    assert record["pass2_total_duration"] == 12096910000
    assert record["pass2_load_duration"] == 1000
    assert record["pass2_eval_duration"] == 301940000
    assert record["pass2_thinking_chars"] == len("some hidden reasoning text")


def test_pass2_thinking_content_never_written_only_length():
    setup_method()
    log_path = fresh_log_path("pass2_thinking_no_leak")
    secret_thinking = "SECRET_HIDDEN_REASONING_MARKER_must_never_appear_in_log"
    fake_response = {"message": {"content": "x", "thinking": secret_thinking}}

    def fake_run():
        utd.record_pass2_completion_metadata(fake_response)
        return {"reply": "ok"}

    utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    with open(log_path, encoding="utf-8") as f:
        raw_text = f.read()
    assert secret_thinking not in raw_text
    record = read_records(log_path)[0]
    assert record["pass2_thinking_chars"] == len(secret_thinking)
    assert isinstance(record["pass2_thinking_chars"], int)


def test_pass2_no_expression_content_leaked_only_length():
    setup_method()
    log_path = fresh_log_path("pass2_no_leak")
    secret_expression = "SECRET_PASS2_EXPRESSION_MARKER_never_should_appear_in_the_log"
    fake_response = {"message": {"content": json.dumps({"expression": secret_expression})}}

    def fake_run():
        utd.record_pass2_completion_metadata(fake_response)
        return {"reply": "ok"}

    utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    with open(log_path, encoding="utf-8") as f:
        raw_text = f.read()
    assert secret_expression not in raw_text
    record = read_records(log_path)[0]
    assert record["pass2_response_content_chars"] == len(json.dumps({"expression": secret_expression}))


def test_pass2_completion_metadata_absent_when_not_supplied():
    setup_method()
    log_path = fresh_log_path("pass2_completion_absent")
    utd.wrap_conversation_callback("hi", [], run_fn=lambda: {"reply": "ok"}, log_path=log_path)
    record = read_records(log_path)[0]
    for field in utd._PASS2_COMPLETION_FIELDS:
        assert record[field] is None, field


def test_pass2_completion_metadata_recorded_on_failure_branch_too():
    # Mirrors the real MALFORMED_EXPRESSION shape from the live
    # OWC/WSP2-P3 forensic: call_llama() (and its telemetry capture)
    # completes successfully -- validate_pass2_expression() is what
    # fails afterward, on the truncated JSON, and raises. Telemetry
    # must still be present on that failure branch.
    setup_method()
    log_path = fresh_log_path("pass2_completion_failure")
    fake_response = {
        "message": {"content": '{"expres', "thinking": None},
        "done": True, "done_reason": "length", "eval_count": 5, "prompt_eval_count": 4091,
    }

    def fake_run():
        def fake_pass2():
            utd.record_pass2_completion_metadata(fake_response)
            return fake_response["message"]["content"]
        utd.timed_model_call("pass2", fake_pass2)
        raise FakeConversationDirectionFailure("pass2", "MALFORMED_EXPRESSION")

    try:
        utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    except FakeConversationDirectionFailure:
        pass
    record = read_records(log_path)[0]
    assert record["pass2_done_reason"] == "length"
    assert record["pass2_eval_count"] == 5
    assert record["pass2_prompt_eval_count"] == 4091
    assert record["control_failure_contained"] is True  # unaffected by the added metadata fields


def test_pass2_completion_metadata_does_not_alter_returned_value():
    # Telemetry capture must never change what the wrapped call
    # returns -- object identity, same as the existing H-section check.
    setup_method()
    log_path = fresh_log_path("pass2_completion_identity")
    fake_response = {"message": {"content": "some expression json"}}

    def fake_run():
        def fake_pass2():
            utd.record_pass2_completion_metadata(fake_response)
            return fake_response["message"]["content"]
        raw2 = utd.timed_model_call("pass2", fake_pass2)
        return {"reply": raw2}

    result = utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    assert result["reply"] == "some expression json"


ALL_TESTS = [
    test_successful_turn_records_full_shape,
    test_turn_ordinal_increments_across_calls,
    test_pass1_exception_recorded_with_correct_stage_and_reraised,
    test_pass2_exception_recorded_with_correct_stage_and_reraised,
    test_persistence_exception_recorded_with_correct_stage_and_reraised,
    test_unrecognized_outer_exception_still_captured_and_reraised,
    test_bounded_traceback_never_unbounded,
    test_stage_classification_covers_known_types,
    test_increasing_history_produces_increasing_recorded_sizes,
    test_non_serializable_history_flagged_not_crashed,
    test_no_content_leak_only_counts_sizes_timings,
    test_no_full_prompt_dumped_even_on_failure,
    test_owc9_context_budget_diagnostics_never_leak_content,
    test_owc9_context_budget_results_absent_when_never_recorded,
    test_module_imports_nothing_beyond_minimal_stdlib,
    test_instrumentation_never_swallows_a_return_value_change,
    test_append_only_never_truncates_prior_records,
    test_logging_failure_itself_never_breaks_a_successful_turn,
    test_diagnostic_honesty_fields_success_vs_failure,
    test_control_failure_contained_false_for_non_control_exceptions,
    test_bounded_pass1_failure_preview_short_text_unbounded,
    test_bounded_pass1_failure_preview_enforces_bound,
    test_attach_pass1_failure_preview_then_recorded_on_wrap,
    test_raw_preview_absent_when_not_attached,
    test_successful_pass1_creates_no_raw_shadow_log,
    test_raw_preview_bound_causes_no_jsonl_corruption,
    test_pass1_completion_metadata_recorded_when_supplied,
    test_pass1_thinking_content_never_written_only_length,
    test_pass1_completion_metadata_absent_when_not_supplied,
    test_pass1_completion_metadata_recorded_on_failure_branch_too,
    test_pass2_completion_metadata_recorded_when_supplied,
    test_pass2_thinking_content_never_written_only_length,
    test_pass2_no_expression_content_leaked_only_length,
    test_pass2_completion_metadata_absent_when_not_supplied,
    test_pass2_completion_metadata_recorded_on_failure_branch_too,
    test_pass2_completion_metadata_does_not_alter_returned_value,
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
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


def test_pass2_marker_shape_is_recorded_on_wrap_content_free():
    setup_method()
    log_path = fresh_log_path("pass2_marker_shape_recorded")

    def fake_run():
        exc = FakeConversationDirectionFailure("pass2", "INCOMPLETE_EXPRESSION_BOUNDARY")
        exc.pass2_marker_shape = {"marker_count": 0, "ends_with_marker": False}
        raise exc

    try:
        utd.wrap_conversation_callback("hi", [], run_fn=fake_run, log_path=log_path)
    except FakeConversationDirectionFailure:
        pass
    assert read_records(log_path)[0]["pass2_marker_shape"] == {"marker_count": 0, "ends_with_marker": False}
