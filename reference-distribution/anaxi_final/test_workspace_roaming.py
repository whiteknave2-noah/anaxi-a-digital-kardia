"""WSP2-S1 acceptance tests. Zero real Ollama/model calls -- Pass-1
calls into the roaming loop are always a fake, injected
`ask_llama_for_json` callable, never the real ollama-backed function.
Zero real workspace actions against the production `anaxi_final/
workspace/` tree -- every test uses a fresh temp WorkspacePaths.

End-to-end worker-thread tests monkeypatch `workspace_roaming.
SECONDS_PER_MINUTE` down to a tiny value so a "5-minute wait" resolves
in milliseconds instead of actually blocking -- see that module
attribute's own docstring for why this is a safe, production-inert
testability seam.
"""
import json
import os
import sys
import tempfile
import time
import traceback

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import workspace_capability as wc
import workspace_direction as wd
import workspace_roaming as wr
import workspace_private as wpriv
import workspace_episode_provenance as wep
import workspace_public_continuity as wpc
import provenance_schema
import context_budget

CLARK_ACTOR_ID = "actor-80eee447ad46e1a1e9b2ea65b5"
TEST_ROOT = tempfile.mkdtemp(prefix="wsp2s1_test_")


def fresh_paths(name):
    root = os.path.join(TEST_ROOT, name)
    paths = wc.WorkspacePaths(root=root)
    paths.ensure_exists()
    return paths


def fresh_private_paths(name):
    root = os.path.join(TEST_ROOT, name + "_private")
    private_paths = wpriv.PrivatePaths(root=root)
    private_paths.ensure_exists()
    return private_paths


def sequenced_ask_llama_for_json(responses):
    """Returns a callable that yields each of `responses` in order
    (as JSON strings, matching the real ask_llama_for_json's return
    type), then raises if called more times than provided -- so an
    unbounded/looping test fails loudly instead of hanging.

    WSP2-MA2: accepts an optional `structured_schema` kwarg, matching
    the real ask_llama_for_json()'s own new (additive) signature --
    Stage-2 call sites now pass one; this fake records it (bounded, so
    a test can assert exactly which schema a given call received) but
    never lets it affect which canned response is returned."""
    state = {"i": 0, "calls": [], "schemas": []}

    def _fn(messages, structured_schema=None):
        state["calls"].append(messages)
        state["schemas"].append(structured_schema)
        if state["i"] >= len(responses):
            raise AssertionError(f"ask_llama_for_json called more times ({state['i'] + 1}) than the test provided responses for")
        value = responses[state["i"]]
        state["i"] += 1
        return json.dumps(value) if not isinstance(value, str) else value

    _fn.calls = state["calls"]
    _fn.schemas = state["schemas"]
    _fn.call_count = lambda: state["i"]
    return _fn


def snapshot_capturing_ask_llama_for_json(responses):
    """Like sequenced_ask_llama_for_json, but also records a snapshot
    of wr.get_roaming_state()["last_workspace_observation"] (via a
    json round-trip, so later in-place mutation can never retroactively
    alter an earlier snapshot) at the moment each call is made -- i.e.
    exactly the observation that was rendered into that call's own
    `messages`. Lets tests directly inspect what a given roaming-choice
    or workspace-action prompt actually saw, even for a run that later
    clears the observation (stop/wait_for_human) before the test can
    inspect it after the run has fully ended."""
    state = {"i": 0, "calls": [], "observation_snapshots": [], "schemas": []}

    def _fn(messages, structured_schema=None):
        state["calls"].append(messages)
        state["schemas"].append(structured_schema)
        obs = wr.get_roaming_state().get("last_workspace_observation")
        state["observation_snapshots"].append(json.loads(json.dumps(obs)) if obs is not None else None)
        if state["i"] >= len(responses):
            raise AssertionError(f"ask_llama_for_json called more times ({state['i'] + 1}) than the test provided responses for")
        value = responses[state["i"]]
        state["i"] += 1
        return json.dumps(value) if not isinstance(value, str) else value

    _fn.calls = state["calls"]
    _fn.schemas = state["schemas"]
    _fn.call_count = lambda: state["i"]
    _fn.observation_snapshots = state["observation_snapshots"]
    return _fn


def wait_until_stopped(timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not wr.is_worker_running():
            return True
        time.sleep(0.01)
    return False


def setup_method():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 60  # restored default; individual tests override as needed
    wpriv.reset_private_state()


# ------------------------------------------------------- A: typed choice --


def test_validate_roaming_choice_accepts_valid():
    for act in (wr.ROAMING_ACT_ACT, wr.ROAMING_ACT_WAIT, wr.ROAMING_ACT_WAIT_FOR_HUMAN, wr.ROAMING_ACT_STOP):
        for minutes in (5, 15, 30, 60):
            validated, failure = wr.validate_roaming_choice({"roaming_act": act, "wait_minutes": minutes})
            assert failure is None
            assert validated == {"roaming_act": act, "wait_minutes": minutes}


def test_validate_roaming_choice_rejects_invalid_act():
    validated, failure = wr.validate_roaming_choice({"roaming_act": "explore_forever", "wait_minutes": 15})
    assert validated is None
    assert failure == wr.RoamingFailure.INVALID_ROAMING_ACT


def test_validate_roaming_choice_rejects_invalid_wait_minutes():
    for bad in (1, 10, 45, 90, 0, -5, "15", None):
        validated, failure = wr.validate_roaming_choice({"roaming_act": "wait", "wait_minutes": bad})
        assert validated is None
        assert failure == wr.RoamingFailure.INVALID_WAIT_MINUTES, f"bad={bad!r}"


def test_validate_roaming_choice_malformed_and_leakage():
    validated, failure = wr.validate_roaming_choice("not json {{{")
    assert failure == wr.RoamingFailure.MALFORMED_CHOICE
    validated, failure = wr.validate_roaming_choice({"roaming_act": "wait"})
    assert failure == wr.RoamingFailure.MALFORMED_CHOICE
    validated, failure = wr.validate_roaming_choice({"roaming_act": "wait", "wait_minutes": 15, "extra": "leak"})
    assert failure == wr.RoamingFailure.PROTOCOL_LEAKAGE


# --------------------------------------------------- B: authorization -----


def test_fresh_roaming_state_off():
    state = wr.get_roaming_state()
    assert state["authorized"] is False


def test_authorized_human_control_sets_true():
    new_val, failure = wr.apply_human_roaming_authorization("human-actor-x", "human-actor-x", True)
    assert failure is None
    assert new_val is True


def test_authorized_human_control_sets_false():
    new_val, failure = wr.apply_human_roaming_authorization("human-actor-x", "human-actor-x", False)
    assert failure is None
    assert new_val is False


def test_unauthorized_source_fails_closed():
    for bad_source in (None, "", "some-other-actor", 42):
        new_val, failure = wr.apply_human_roaming_authorization(bad_source, "human-actor-x", True)
        assert new_val is None
        assert failure == wr.AuthorizationFailure.UNAUTHORIZED_HUMAN_CONTROL


def test_clark_model_path_cannot_reach_authorization_function():
    with open(os.path.join(ANAXI_FINAL, "workspace_roaming.py"), encoding="utf-8") as f:
        source = f.read()
    loop_body = source[source.index("def _roaming_loop"):source.index("def start_roaming_worker")]
    assert "apply_human_roaming_authorization" not in loop_body


# ------------------------------------------------- C: no-obligation -------


def test_authorized_clark_may_choose_wait_without_action():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "wait", "wait_minutes": 5},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    paths = fresh_paths("no_obligation_wait")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-1", trace_path=os.path.join(TEST_ROOT, "wr_trace_wait.jsonl"))
    assert wait_until_stopped()
    assert fake.call_count() == 2
    assert wc.query_action_log(paths) == []  # zero workspace actions taken


def test_authorized_clark_may_stop_immediately():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    paths = fresh_paths("no_obligation_stop")
    trace_path = os.path.join(TEST_ROOT, "wr_trace_stop.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-2", trace_path=trace_path)
    assert wait_until_stopped()
    assert fake.call_count() == 1
    records = wr.query_roaming_trace(trace_path)
    assert records[-1]["run_status"] == wr.RUN_STATUS_STOPPED_BY_CLARK
    assert wc.query_action_log(paths) == []


# -------------------------------------------------------------- D: wait ---


def test_wait_causes_no_action_and_offers_next_opportunity():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "wait", "wait_minutes": 5},
        {"roaming_act": "wait", "wait_minutes": 5},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    paths = fresh_paths("wait_then_offer_again")
    trace_path = os.path.join(TEST_ROOT, "wr_trace_wait2.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-3", trace_path=trace_path)
    assert wait_until_stopped()
    assert fake.call_count() == 3
    records = wr.query_roaming_trace(trace_path)
    statuses = [r["run_status"] for r in records]
    assert statuses.count(wr.RUN_STATUS_WAITING) == 2
    assert statuses[-1] == wr.RUN_STATUS_STOPPED_BY_CLARK


def test_no_overlapping_cycle_duplicate_start_rejected():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.05  # long enough that the first cycle is still "waiting" when we try a duplicate start
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "wait", "wait_minutes": 5}, {"roaming_act": "stop", "wait_minutes": 5}])
    paths = fresh_paths("no_overlap")
    started = wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                                       session_id="sess-4", trace_path=os.path.join(TEST_ROOT, "wr_trace_overlap.jsonl"))
    assert started is True
    time.sleep(0.02)
    started_again = wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                                             session_id="sess-4", trace_path=os.path.join(TEST_ROOT, "wr_trace_overlap.jsonl"))
    assert started_again is False  # rejected -- one cycle already in flight
    wr.stop_roaming_worker()
    assert wait_until_stopped()


# ------------------------------------------------------------ E: action ---


def test_act_routes_through_existing_wsp1_validator_and_executor():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("act_library_list")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("hello")
    fake = sequenced_ask_llama_for_json([
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    # First call must be the ROAMING choice (act), not the workspace
    # action directly -- so the sequence here is [roaming choice=act
    # producing a workspace-action call, then stop]. Correct the
    # sequence to reflect the real two-call design:
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_action.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-5", trace_path=trace_path)
    assert wait_until_stopped()
    log = wc.query_action_log(paths)
    assert len(log) == 1
    assert log[0]["action"] == "list"
    assert log[0]["resource_class"] == "library"
    assert log[0]["result"] == "performed"
    assert log[0]["requester_actor_id"] == CLARK_ACTOR_ID  # canonical, never literal "clark"


def _build_minimal_compressed_pdf(text):
    """A single-page, standards-compliant PDF with a real FlateDecode-
    compressed content stream -- just enough to prove roaming ACT
    routes a `.pdf` library.read through the existing WSP1 executor
    into WSP2-S2's real PDF extraction path, with no parallel
    PDF-specific roaming executor of any kind."""
    import zlib
    stream = f"BT /F1 24 Tf 20 100 Td ({text}) Tj ET".encode()
    compressed = zlib.compress(stream)
    buf = bytearray(b"%PDF-1.4\n")
    offsets = {}

    def add_obj(num, content_bytes):
        offsets[num] = len(buf)
        buf.extend(f"{num} 0 obj\n".encode())
        buf.extend(content_bytes)
        buf.extend(b"\nendobj\n")

    add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add_obj(3, b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 5 0 R >> >> "
                b"/MediaBox [0 0 300 144] /Contents 4 0 R >>")
    header = f"<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode()
    add_obj(4, header + compressed + b"\nendstream")
    add_obj(5, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    xref_offset = len(buf)
    buf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for num in range(1, 6):
        buf.extend(f"{offsets[num]:010d} 00000 n \n".encode())
    buf.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode())
    return bytes(buf)


def test_roaming_act_reads_local_pdf_through_existing_executor():
    # WSP2-S2: proves the ONLY thing this gate promised for roaming --
    # improving workspace_capability.read_library_bounded() to handle
    # .pdf automatically makes PDFs readable through unattended
    # roaming, with zero changes to workspace_roaming.py and no
    # parallel PDF-specific roaming code path.
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("act_library_pdf_read")
    with open(os.path.join(paths.library_dir, "roaming_doc.pdf"), "wb") as f:
        f.write(_build_minimal_compressed_pdf("Roaming PDF Text"))
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "roaming_doc.pdf", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_pdf_action.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-pdf", trace_path=trace_path)
    assert wait_until_stopped()
    log = wc.query_action_log(paths)
    assert len(log) == 1
    assert log[0]["action"] == "read"
    assert log[0]["resource_class"] == "library"
    assert log[0]["result"] == "performed"
    assert log[0]["requester_actor_id"] == CLARK_ACTOR_ID  # canonical, never literal "clark"
    assert log[0]["detail"]["document_type"] == "pdf"
    assert log[0]["detail"]["chars_returned"] == len("Roaming PDF Text")
    with open(os.path.join(ANAXI_FINAL, "workspace_roaming.py"), encoding="utf-8") as f:
        roaming_source = f.read()
    # No parallel PDF-specific roaming code was added -- checked
    # structurally (actual import/constant/branch markers), not by a
    # bare "pdf" substring scan, which would false-match this module's
    # own legitimate prose explaining that no such handling exists.
    for marker in ("import pypdf", "_is_pdf", "PDF_EXTENSION", "PDF_TEXT_UNAVAILABLE", "PDF_MALFORMED", "PDF_ENCRYPTED"):
        assert marker not in roaming_source, marker


# ------------------------------------------- E2: bounded observation (WSP2-S3) -


def test_library_list_result_becomes_next_observation():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_library_list")
    with open(os.path.join(paths.library_dir, "alpha.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    fake = snapshot_capturing_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs1", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs1.jsonl"))
    assert wait_until_stopped()
    assert fake.call_count() == 3
    # snapshot[0]: before the very first choice -- no prior observation.
    assert fake.observation_snapshots[0] is None
    # snapshot[2]: the THIRD call (the roaming choice offered after the
    # library.list action completed) must have seen that action's own
    # actual bounded result as the observation.
    obs = fake.observation_snapshots[2]
    assert obs is not None
    assert obs["resource_class"] == "library" and obs["action"] == "list" and obs["status"] == "performed"
    # bounded_result is execute_workspace_action()'s own boundary-result
    # envelope (action/scope/boundary/consequence/rationale/result) --
    # the same object WSP1's Pass 2 already renders -- with the real
    # mechanical host result nested under "result".
    # WSP2-P2: list results are now the bounded dict shape.
    assert obs["bounded_result"]["result"]["entries"] == ["alpha.txt"]  # the real host result, not a summary
    assert obs["bounded_result"]["result"]["truncated"] is False


def test_large_library_list_reaches_roaming_prompt_bounded_not_unbounded():
    # WSP2-P2: proves the fix at the exact pathway the original
    # inspection flagged -- a large directory listing can no longer
    # enter the next roaming-choice prompt unbounded. No change to
    # workspace_roaming.py itself was needed for this: the bound is
    # inherited automatically because it is applied at the source
    # (workspace_capability.list_contents()), and everything from
    # there to the rendered prompt is a pure pass-through.
    #
    # OWC9-P1 UPDATE: the source-of-truth for "bounded" is now the
    # canonical roaming_state itself (still exactly MAX_LIST_ENTRIES
    # entries, still flagged truncated=True -- unchanged from WSP2-P2,
    # asserted below via the observation snapshot), not necessarily
    # the NEXT rendered Stage-1 call's own text. A maximal, MAX_LIST_
    # ENTRIES-sized listing (~2KB rendered) plus roaming's own
    # mandatory hard control context (~2KB) together approach the
    # 4096-token ceiling closely enough that the aggregate compositor
    # (context_budget.py, spec OWC9-P1) may deterministically and
    # honestly drop the observation from THIS PARTICULAR call rather
    # than let it silently starve the protected generation reserve --
    # exactly the governing invariant this gate exists to enforce
    # (spec section 11: report what survives, never silently drop
    # merely because something is large, but also never let it eat the
    # reserve). Whichever happens, two things must always be true:
    # (a) the canonical observation itself remains exactly as bounded
    # as before (proven below via the snapshot, untouched by this
    # gate), and (b) if it DOES reach the prompt, it is still capped at
    # MAX_LIST_ENTRIES with its truncation flag visible.
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_large_library")
    names = [f"book_{i:04d}.txt" for i in range(150)]
    for name in names:
        with open(os.path.join(paths.library_dir, name), "w", encoding="utf-8") as f:
            f.write("x")
    fake = snapshot_capturing_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs-large", trace_path=os.path.join(TEST_ROOT, "wr_trace_obslarge.jsonl"))
    assert wait_until_stopped()

    obs = fake.observation_snapshots[2]
    result = obs["bounded_result"]["result"]
    assert result["total_count"] == 150
    assert result["returned_count"] == wc.MAX_LIST_ENTRIES
    assert len(result["entries"]) == wc.MAX_LIST_ENTRIES
    assert result["truncated"] is True

    # The actual rendered text handed to the next roaming-choice model
    # call -- not just the internal snapshot -- must reflect the same
    # bound whenever aggregate pressure allows it to survive at all:
    # at most MAX_LIST_ENTRIES filenames present, and the truncation
    # flag visible as text. If aggregate pressure dropped it entirely
    # (OWC9-P1's own, disclosed capacity finding for this exact
    # scenario), the rendered text simply omits it -- book_ count of 0
    # still satisfies "at most MAX_LIST_ENTRIES."
    rendered = "\n".join(m["content"] for m in fake.calls[2])
    assert rendered.count("book_") <= wc.MAX_LIST_ENTRIES
    if "book_" in rendered:
        assert "truncated" in rendered and "true" in rendered


def test_library_read_bounded_text_becomes_next_observation():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_library_read")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("Hello Bounded Text")
    fake = snapshot_capturing_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "book.txt", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs2", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs2.jsonl"))
    assert wait_until_stopped()
    obs = fake.observation_snapshots[2]
    assert obs["bounded_result"]["result"]["content"] == "Hello Bounded Text"
    rendered = json.dumps(fake.calls[2])
    assert "Hello Bounded Text" in rendered  # the model literally receives this text


def test_pdf_library_read_becomes_next_observation():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_pdf_read")
    with open(os.path.join(paths.library_dir, "doc.pdf"), "wb") as f:
        f.write(_build_minimal_compressed_pdf("Observation PDF Text"))
    fake = snapshot_capturing_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "doc.pdf", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs3", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs3.jsonl"))
    assert wait_until_stopped()
    obs = fake.observation_snapshots[2]
    assert obs["bounded_result"]["result"]["content"] == "Observation PDF Text"
    assert obs["bounded_result"]["result"]["document_type"] == "pdf"
    assert "%PDF" not in obs["bounded_result"]["result"]["content"]  # extracted text, not raw PDF bytes


def test_music_and_photo_metadata_become_next_observation():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_music_photo")
    with open(os.path.join(paths.music_dir, "song.mp3"), "wb") as f:
        f.write(b"ID3fixture")
    with open(os.path.join(paths.photographs_dir, "family.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff")
    fake = snapshot_capturing_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "music", "action": "inspect_metadata", "relative_path": "song.mp3", "content": ""},
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "photographs", "action": "inspect_metadata", "relative_path": "family.jpg", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs4", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs4.jsonl"))
    assert wait_until_stopped()
    music_obs = fake.observation_snapshots[2]  # roaming choice offered right after the music action
    assert music_obs["resource_class"] == "music"
    assert music_obs["bounded_result"]["result"]["name"] == "song.mp3"
    photo_obs = fake.observation_snapshots[4]  # roaming choice offered right after the photo action
    assert photo_obs["resource_class"] == "photographs"
    assert photo_obs["bounded_result"]["result"]["name"] == "family.jpg"


def test_observation_is_exact_host_result_no_summary_keys():
    # Direct structural comparison against workspace_capability's own
    # real return value for the same input -- proves the observation
    # is the literal host result, never a model-generated summary/
    # reasoning field.
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_exact_match")
    with open(os.path.join(paths.library_dir, "ref.txt"), "w", encoding="utf-8") as f:
        f.write("reference text")
    expected_result, expected_failure = wc.read_library_bounded(paths, "ref.txt", requester_actor_id=CLARK_ACTOR_ID)
    assert expected_failure is None
    fake = snapshot_capturing_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "ref.txt", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs5", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs5.jsonl"))
    assert wait_until_stopped()
    obs = fake.observation_snapshots[2]
    assert obs["bounded_result"]["result"] == expected_result
    assert set(obs.keys()) == {"resource_class", "action", "relative_path", "status", "bounded_result", "action_id"}
    # The envelope itself is exactly execute_workspace_action()'s own
    # mechanical boundary-result shape -- no extra summary/reasoning key.
    assert set(obs["bounded_result"].keys()) == {"action", "scope", "boundary", "consequence", "rationale", "result"}


def test_second_action_replaces_first_observation_no_accumulation():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_replacement")
    with open(os.path.join(paths.library_dir, "first.txt"), "w", encoding="utf-8") as f:
        f.write("FIRST_MARKER")
    with open(os.path.join(paths.library_dir, "second.txt"), "w", encoding="utf-8") as f:
        f.write("SECOND_MARKER")
    fake = snapshot_capturing_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "first.txt", "content": ""},
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "second.txt", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs6", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs6.jsonl"))
    assert wait_until_stopped()
    after_first = fake.observation_snapshots[2]
    assert after_first["bounded_result"]["result"]["content"] == "FIRST_MARKER"
    after_second = fake.observation_snapshots[4]
    assert after_second["bounded_result"]["result"]["content"] == "SECOND_MARKER"
    # Replacement, not accumulation: the second observation contains no
    # trace of the first, and only ONE observation exists at a time --
    # there is no list/history field anywhere on the state.
    assert "FIRST_MARKER" not in json.dumps(after_second)
    assert "last_workspace_observations" not in wr.get_roaming_state()  # no plural/history field exists


def test_wait_preserves_observation_across_the_interval():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_wait_preserve")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("PRESERVED_MARKER")
    fake = snapshot_capturing_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "book.txt", "content": ""},
        {"roaming_act": "wait", "wait_minutes": 5},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs7", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs7.jsonl"))
    assert wait_until_stopped()
    assert fake.call_count() == 4  # no model call was skipped or added by the wait itself
    before_wait = fake.observation_snapshots[2]
    after_wait = fake.observation_snapshots[3]
    assert before_wait == after_wait  # identical, unduplicated -- wait neither clears nor expands it
    assert after_wait["bounded_result"]["result"]["content"] == "PRESERVED_MARKER"


def test_fresh_run_starts_with_observation_none():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_fresh_run")
    fake = snapshot_capturing_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs8", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs8.jsonl"))
    assert wait_until_stopped()
    assert fake.observation_snapshots[0] is None


def test_clark_stop_clears_observation():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_clark_stop_clears")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("about to be cleared")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "book.txt", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs9", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs9.jsonl"))
    assert wait_until_stopped()
    assert wr.get_roaming_state()["last_workspace_observation"] is None


def test_human_stop_clears_observation():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.05
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_human_stop_clears")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("about to be cleared by human stop")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "book.txt", "content": ""},
        {"roaming_act": "wait", "wait_minutes": 60},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs10", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs10.jsonl"))
    time.sleep(0.03)  # let the action complete and enter the long wait
    assert wr.get_roaming_state()["last_workspace_observation"] is not None  # sanity: it was actually set
    wr.stop_roaming_worker()
    assert wait_until_stopped()
    assert wr.get_roaming_state()["last_workspace_observation"] is None


def test_process_reset_clears_observation():
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["last_workspace_observation"] = {"resource_class": "library", "action": "read", "relative_path": "x",
                                            "status": "performed", "bounded_result": {}, "action_id": "wraction-x"}
    wr.reset_roaming_state()
    assert wr.get_roaming_state()["last_workspace_observation"] is None


# ---------------------------------------------- E3: journal reader (WSP2-S3) --


def test_journal_write_then_deliberate_read_reaches_next_observation():
    # Section 7's own required proof: the unattended pathway is not
    # write-only. journal.append succeeds, and a LATER, separately
    # chosen roaming action (journal.list) can read it back -- with
    # that read's own actual result reaching the subsequent roaming
    # choice as the observation.
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_journal_reader")
    fake = snapshot_capturing_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "journal", "action": "append", "relative_path": "", "content": "Journal Entry For Observation Test"},
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "journal", "action": "list", "relative_path": "", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs11", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs11.jsonl"))
    assert wait_until_stopped()

    after_append = fake.observation_snapshots[2]
    assert after_append["action"] == "append"
    assert after_append["bounded_result"]["result"]["content"] == "Journal Entry For Observation Test"

    after_list = fake.observation_snapshots[4]
    assert after_list["action"] == "list"
    entries_on_disk = sorted(f for f in os.listdir(paths.journal_dir) if f.endswith(".json"))
    # WSP2-P2: list results are now the bounded dict shape.
    assert after_list["bounded_result"]["result"]["entries"] == entries_on_disk  # the deliberate read's real result reached the next choice
    assert after_list["bounded_result"]["result"]["truncated"] is False


def test_no_automatic_journal_read_after_append():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_no_auto_journal_read")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "journal", "action": "append", "relative_path": "", "content": "no auto read of this"},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs12", trace_path=os.path.join(TEST_ROOT, "wr_trace_obs12.jsonl"))
    assert wait_until_stopped()
    assert fake.call_count() == 3  # exactly: choice, append, stop -- no surprise journal-read call was ever made
    log = wc.query_action_log(paths)
    assert [r["action"] for r in log] == ["append"]  # only the deliberately chosen action ever ran


# ------------------------------------------------- E4: wait_for_human (WSP2-S3) -


def test_wait_for_human_sets_unauthorized_clears_observation_and_stops_worker():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("wfh_basic")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("some text")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "book.txt", "content": ""},
        {"roaming_act": "wait_for_human", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-wfh1", trace_path=os.path.join(TEST_ROOT, "wr_trace_wfh1.jsonl"))
    assert wait_until_stopped()
    assert wr.get_roaming_state()["authorized"] is False
    assert wr.get_roaming_state()["last_workspace_observation"] is None
    assert fake.call_count() == 3  # no further roaming-choice call was made after wait_for_human


def test_wait_for_human_emits_waiting_for_human_trace_status():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("wfh_trace")
    fake = sequenced_ask_llama_for_json([{"roaming_act": "wait_for_human", "wait_minutes": 15}])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_wfh2.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-wfh2", trace_path=trace_path)
    assert wait_until_stopped()
    records = wr.query_roaming_trace(trace_path)
    assert records[-1]["run_status"] == wr.RUN_STATUS_WAITING_FOR_HUMAN
    assert records[-1]["run_status"] != wr.RUN_STATUS_STOPPED_BY_CLARK  # distinct audit reason from stop


def test_wait_for_human_no_further_model_call_and_no_automatic_restart():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("wfh_no_restart")
    fake = sequenced_ask_llama_for_json([{"roaming_act": "wait_for_human", "wait_minutes": 5}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-wfh3", trace_path=os.path.join(TEST_ROOT, "wr_trace_wfh3.jsonl"))
    assert wait_until_stopped()
    assert fake.call_count() == 1
    # No automatic restart: the worker stays stopped and the state
    # stays unauthorized indefinitely -- only a fresh, explicit human
    # authorization (start_roaming_worker with authorized=True set by
    # the caller) can begin another run.
    time.sleep(0.05)
    assert wr.is_worker_running() is False
    assert wr.get_roaming_state()["authorized"] is False


def test_wait_for_human_requires_valid_wait_minutes_placeholder():
    # No fake wait duration is invented -- the field is structurally
    # required (matching 'stop', which is likewise unused) but any
    # invalid value still fails validation the same as for every other
    # act; there is no special-casing that lets wait_for_human bypass
    # the shared wait_minutes check.
    validated, failure = wr.validate_roaming_choice({"roaming_act": "wait_for_human", "wait_minutes": 45})
    assert validated is None
    assert failure == wr.RoamingFailure.INVALID_WAIT_MINUTES
    validated2, failure2 = wr.validate_roaming_choice({"roaming_act": "wait_for_human", "wait_minutes": 30})
    assert failure2 is None
    assert validated2 == {"roaming_act": "wait_for_human", "wait_minutes": 30}


# ---------------------------------------------- E4b: private roaming (WSP3-S1) -


def test_roaming_may_choose_private_act_and_executes_via_private_pathway():
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("private_roaming_ordinary")
    private_paths = fresh_private_paths("private_roaming")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "write", "relative_path": "note.txt", "content": "Clark's private note",
         "destination_relative_path": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-priv1", trace_path=os.path.join(TEST_ROOT, "wr_trace_priv1.jsonl"),
                             private_paths=private_paths)
    assert wait_until_stopped()
    assert fake.call_count() == 3
    # The file was actually written through the dedicated private
    # pathway, never through the ordinary WSP1 executor.
    assert os.path.isfile(os.path.join(private_paths.root, "note.txt"))
    with open(os.path.join(private_paths.root, "note.txt"), encoding="utf-8") as f:
        assert f.read() == "Clark's private note"
    # No ordinary workspace action of any kind occurred.
    assert wc.query_action_log(paths) == []
    # Ordinary last_workspace_observation was never touched by the
    # private_act cycle (spec section 12).
    assert wr.get_roaming_state()["last_workspace_observation"] is None


def test_private_act_without_private_paths_fails_closed_no_crash():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("private_act_unwired")
    fake = sequenced_ask_llama_for_json([{"roaming_act": "private_act", "wait_minutes": 5}])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_priv2.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-priv2", trace_path=trace_path)  # no private_paths passed
    assert wait_until_stopped()
    assert wr.get_roaming_state()["authorized"] is False
    records = wr.query_roaming_trace(trace_path)
    assert records[-1]["run_status"] == wr.RUN_STATUS_FAILED


def test_private_act_run_end_clears_private_observation():
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("private_clear_ordinary")
    private_paths = fresh_private_paths("private_clear")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "write", "relative_path": "note.md", "content": "temporary", "destination_relative_path": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-priv3", trace_path=os.path.join(TEST_ROOT, "wr_trace_priv3.jsonl"),
                             private_paths=private_paths)
    assert wait_until_stopped()
    assert wpriv.get_private_state()["last_private_observation"] is None


def test_ordinary_choice_prompt_never_contains_private_content():
    # Section 12's own separation requirement: even though a private
    # write happened, the NEXT roaming-choice prompt (the ordinary,
    # top-level act/private_act/wait/wait_for_human/stop decision) must
    # never embed the private content -- only workspace_private's own
    # dedicated build_private_action_messages() may ever render it.
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("private_leak_check_ordinary")
    private_paths = fresh_private_paths("private_leak_check")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "write", "relative_path": "diary.txt", "content": "SECRET_PRIVATE_MARKER",
         "destination_relative_path": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-priv4", trace_path=os.path.join(TEST_ROOT, "wr_trace_priv4.jsonl"),
                             private_paths=private_paths)
    assert wait_until_stopped()
    third_call_messages = fake.calls[2]  # the ordinary roaming choice offered after the private write
    assert "SECRET_PRIVATE_MARKER" not in json.dumps(third_call_messages)


def test_private_act_failure_records_only_failure_class_never_content():
    # WSP3-P2: superseded property -- not even the branch-specific
    # PrivateFailure CLASS is recorded any more (that was itself a
    # leak vector; see WSP3-P2's own module comment on
    # _generic_execution_failure_code()). The durable trace now
    # carries only the shared generic execution-failure code, with
    # roaming_act/wait_minutes/workspace_action_id all redacted.
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("private_fail_ordinary")
    private_paths = fresh_private_paths("private_fail")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "read", "relative_path": "../escape_attempt_SECRET.txt", "content": "",
         "destination_relative_path": ""},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_priv5.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-priv5", trace_path=trace_path, private_paths=private_paths)
    assert wait_until_stopped()
    assert wr.get_roaming_state()["authorized"] is False
    records = wr.query_roaming_trace(trace_path)
    assert records[-1]["run_status"] == wr.RUN_STATUS_FAILED
    assert records[-1]["workspace_action_status"] == wr.EXECUTION_FAILURE_GENERIC_CODE
    assert records[-1]["workspace_action_status"] != wpriv.PrivateFailure.PATH_ESCAPE
    assert records[-1]["roaming_act"] is None
    assert records[-1]["wait_minutes"] is None
    with open(trace_path, encoding="utf-8") as f:
        raw_trace_text = f.read()
    assert "escape_attempt_SECRET" not in raw_trace_text
    assert "private_act" not in raw_trace_text
    assert wpriv.PrivateFailure.PATH_ESCAPE not in raw_trace_text


def test_successful_private_action_leaves_no_durable_trace_row():
    # WSP3-P1 section 4: a successful private_act cycle must write
    # ZERO roaming-trace rows -- not even a content-free marker row.
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("private_trace_ordinary")
    private_paths = fresh_private_paths("private_trace")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "write", "relative_path": "special_filename_marker.txt", "content": "PRIVATE_TRACE_MARKER",
         "destination_relative_path": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_priv6.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-priv6", trace_path=trace_path, private_paths=private_paths)
    assert wait_until_stopped()
    records = wr.query_roaming_trace(trace_path)
    # Exactly one durable record exists for the whole run: the final
    # STOP. Nothing marks that a private_act cycle happened in between.
    assert len(records) == 1
    assert records[0]["run_status"] == wr.RUN_STATUS_STOPPED_BY_CLARK
    assert records[0]["roaming_act"] == "stop"
    for record in records:
        assert record["roaming_act"] != "private_act"
    with open(trace_path, encoding="utf-8") as f:
        raw_trace_text = f.read()
    assert "PRIVATE_TRACE_MARKER" not in raw_trace_text
    assert "special_filename_marker.txt" not in raw_trace_text
    assert "private_act" not in raw_trace_text  # not even the roaming_act label survives


def test_repeated_successful_private_actions_produce_no_usage_count():
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("private_repeat_ordinary")
    private_paths = fresh_private_paths("private_repeat")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "write", "relative_path": "a.txt", "content": "one", "destination_relative_path": ""},
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "write", "relative_path": "b.txt", "content": "two", "destination_relative_path": ""},
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "write", "relative_path": "c.txt", "content": "three", "destination_relative_path": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_priv7.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-priv7", trace_path=trace_path, private_paths=private_paths)
    assert wait_until_stopped()
    # All three private writes genuinely happened...
    assert sorted(os.listdir(private_paths.root)) == ["a.txt", "b.txt", "c.txt"]
    # ...but the durable trace shows no count, history, or evidence of
    # any of it: still exactly one record (the final STOP), and its own
    # roaming_action_count reads 0 -- not "3" -- since private
    # successes are never counted into the shared ordinary counter
    # either (that would itself be a detectable usage signal).
    records = wr.query_roaming_trace(trace_path)
    assert len(records) == 1
    assert records[0]["roaming_action_count"] == 0


def test_private_success_does_not_create_a_countable_gap_against_ordinary_actions():
    # Guards the specific leak this patch closes: if roaming_action_count
    # were bumped for an invisible private success, a LATER ordinary
    # ACT's own trace row would show a count inconsistent with the
    # number of visible ACT rows, revealing a private action happened
    # by arithmetic even without naming it.
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("private_gap_ordinary")
    private_paths = fresh_private_paths("private_gap")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("public")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "write", "relative_path": "hidden.txt", "content": "hidden", "destination_relative_path": ""},
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_priv8.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-priv8", trace_path=trace_path, private_paths=private_paths)
    assert wait_until_stopped()
    records = wr.query_roaming_trace(trace_path)
    action_records = [r for r in records if r["run_status"] == wr.RUN_STATUS_ACTION]
    assert len(action_records) == 1
    # The one visible ordinary ACT's own recorded count is exactly 1 --
    # matching the number of visible ACT rows, with no invisible jump
    # from the private write that preceded it.
    assert action_records[0]["roaming_action_count"] == 1


def test_roaming_source_never_calls_ordinary_pass2():
    # Structural proof for spec section 11: roaming (ordinary OR
    # private) never routes through workspace_direction's Pass-2
    # expression pathway or workspace_supervisor's conversational
    # persistence -- a private read can never become a reply to Alex.
    with open(os.path.join(ANAXI_FINAL, "workspace_roaming.py"), encoding="utf-8") as f:
        source = f.read()
    # "workspace_supervisor" itself is not checked here as a bare
    # substring -- this module's own docstring legitimately names it in
    # prose (explaining why it deliberately reuses workspace_direction's
    # lower-level functions instead); its absence from the actual
    # import surface is already proven by test_no_rem_sleep_import_or_
    # reference's AST-based exact-import-set assertion.
    for forbidden in ("build_pass2_interface", "validate_pass2_expression"):
        assert forbidden not in source, forbidden


# --------------------------------------------- E5: observation memory boundary -


def test_observation_bounded_result_not_persisted_in_trace_action_record():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("obs_trace_boundary")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("TRACE_BOUNDARY_MARKER_TEXT")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "book.txt", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_obs_boundary.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-obs13", trace_path=trace_path)
    assert wait_until_stopped()
    records = wr.query_roaming_trace(trace_path)
    action_record = next(r for r in records if r["run_status"] == wr.RUN_STATUS_ACTION)
    # Exactly the existing, pre-WSP2-S3 trace schema -- no new
    # observation/bounded_result field was added to the durable trace.
    assert set(action_record.keys()) == {
        "trace_id", "timestamp", "session_id", "clark_actor_id", "authorization_state",
        "roaming_act", "wait_minutes", "workspace_action_id", "workspace_action_status",
        "roaming_action_count", "run_status",
    }
    with open(trace_path, encoding="utf-8") as f:
        raw_trace_text = f.read()
    assert "TRACE_BOUNDARY_MARKER_TEXT" not in raw_trace_text  # full bounded_result content never lands in the trace


def test_journal_append_succeeds_through_existing_pathway():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("act_journal_append")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "journal", "action": "append", "relative_path": "", "content": "A quiet observation."},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_journal.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-6", trace_path=trace_path)
    assert wait_until_stopped()
    entries = [f for f in os.listdir(paths.journal_dir) if f.endswith(".json")]
    assert len(entries) == 1
    with open(os.path.join(paths.journal_dir, entries[0]), encoding="utf-8") as f:
        entry = json.load(f)
    assert entry["author_actor_id"] == CLARK_ACTOR_ID
    assert entry["content"] == "A quiet observation."


def test_music_and_photos_surface_bounded_and_shared_with_roaming():
    # LIVE_ALLOWED_SURFACE itself (unchanged, reused directly) already
    # excludes anything else for music -- no genuine audio pathway
    # exists (AUDIO BLOCKED BY CURRENT RUNTIME; see the CAP2 final
    # report) -- proven here by confirming the roaming pathway routes
    # through the SAME constant, not a roaming-specific broadened one.
    #
    # Pixel-bearing actions are deliberately absent from unattended roaming:
    # roaming has no image-model pass and must not degrade VIEW/view_page to
    # metadata while calling the capability performed. The surface is derived
    # from the shared live table rather than hand-copied.
    #
    # CAP2F: music legitimately gained LISTEN/INSPECT_AUDIO (genuine
    # bounded acoustic decode/measurement -- NOT model-level audio
    # perception; no model call is ever made with these actions). Both
    # results are always plain, already-bounded JSON-safe dicts (see
    # workspace_audio.py's own render_audio_source()/render_audio_view()
    # docstrings) -- no raw audio bytes, no unbounded arrays -- so
    # roaming's own json.dumps(boundary_result) handling stays exactly
    # as safe as it already was for every other action.
    assert wd.LIVE_ALLOWED_SURFACE[wc.MUSIC] == {wc.LIST, wc.INSPECT_METADATA, wc.LISTEN, wc.INSPECT_AUDIO, wc.OBSERVE}
    assert wd.LIVE_ALLOWED_SURFACE[wc.PHOTOGRAPHS] == {wc.LIST, wc.INSPECT_METADATA, wc.VIEW}
    assert wc.VIEW not in wr.ROAMING_ALLOWED_SURFACE[wc.PHOTOGRAPHS]
    assert wc.VIEW_PAGE not in wr.ROAMING_ALLOWED_SURFACE[wc.LIBRARY]
    assert wr.ROAMING_ALLOWED_SURFACE[wc.MUSIC] == wd.LIVE_ALLOWED_SURFACE[wc.MUSIC]


def test_forbidden_action_fails_closed_and_ends_run():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("forbidden_action")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "music", "action": "play", "relative_path": "", "content": ""},  # not in allowed surface
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_forbidden.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-7", trace_path=trace_path)
    assert wait_until_stopped()
    records = wr.query_roaming_trace(trace_path)
    assert records[-1]["run_status"] == wr.RUN_STATUS_FAILED
    assert wr.get_roaming_state()["authorized"] is False  # run ended, no silent capability expansion


def test_traversal_attempt_fails_closed_and_ends_run():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("traversal_attempt")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "read", "relative_path": "../../etc/passwd", "content": ""},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_traversal.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-8", trace_path=trace_path)
    assert wait_until_stopped()
    log = wc.query_action_log(paths)
    assert len(log) == 1
    assert log[0]["result"] == "denied"  # workspace_action_log.jsonl -- ordinary-only, untouched by WSP3-P2
    records = wr.query_roaming_trace(trace_path)
    assert records[-1]["run_status"] == wr.RUN_STATUS_FAILED
    # WSP3-P2: the roaming trace's own workspace_action_status is now
    # the shared generic execution-failure code, not the literal
    # "denied" string -- see _generic_execution_failure_code()'s
    # module comment (symmetric with the private_act execution-failure
    # branch, spec section 14's deliberate tradeoff).
    assert records[-1]["workspace_action_status"] == wr.EXECUTION_FAILURE_GENERIC_CODE
    assert records[-1]["roaming_act"] is None
    assert records[-1]["wait_minutes"] is None


# --------------------------------------------------------- F: attribution -


def test_every_action_uses_canonical_clark_actor_never_literal_clark():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("attribution")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_attribution.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-9", trace_path=trace_path)
    assert wait_until_stopped()
    log = wc.query_action_log(paths)
    assert all(r["requester_actor_id"] == CLARK_ACTOR_ID for r in log)
    assert all(r["requester_actor_id"] != "clark" for r in log)
    records = wr.query_roaming_trace(trace_path)
    assert all(r["clark_actor_id"] == CLARK_ACTOR_ID for r in records)


# --------------------------------------------------------------- G: stop --


def test_clark_stop_ends_run_no_next_call():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    paths = fresh_paths("clark_stop")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-10", trace_path=os.path.join(TEST_ROOT, "wr_trace_cstop.jsonl"))
    assert wait_until_stopped()
    assert fake.call_count() == 1
    assert wr.get_roaming_state()["authorized"] is False


def test_human_stop_ends_run_and_prevents_next_call():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.05
    state = wr.get_roaming_state()
    state["authorized"] = True
    # A long wait -- if human stop didn't take effect promptly, a
    # second call would occur after the (short, test-scaled) interval.
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "wait", "wait_minutes": 60},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    paths = fresh_paths("human_stop")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-11", trace_path=os.path.join(TEST_ROOT, "wr_trace_hstop.jsonl"))
    time.sleep(0.02)  # let it enter the wait phase
    wr.stop_roaming_worker()
    assert wait_until_stopped()
    assert fake.call_count() == 1  # the second (post-wait) call never happened
    assert wr.get_roaming_state()["authorized"] is False


def test_currently_executing_action_finishes_before_stop_takes_effect():
    # stop_roaming_worker() only sets an Event checked at loop-iteration
    # boundaries -- it cannot and does not interrupt execute_workspace_
    # action() mid-call. Proven here: stop is requested immediately
    # after starting, yet the already-in-flight action still completes
    # and is durably logged.
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("finish_in_flight")
    with open(os.path.join(paths.library_dir, "book.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-12", trace_path=os.path.join(TEST_ROOT, "wr_trace_inflight.jsonl"))
    wr.stop_roaming_worker()  # requested essentially immediately
    assert wait_until_stopped()
    log = wc.query_action_log(paths)
    assert len(log) == 1
    assert log[0]["result"] == "performed"  # completed safely despite the stop request


# ---------------------------------------------------- H: waking substrate -


def test_unattended_calls_use_injected_waking_function_only():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    paths = fresh_paths("waking_substrate")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-13", trace_path=os.path.join(TEST_ROOT, "wr_trace_waking.jsonl"))
    assert wait_until_stopped()
    assert fake.call_count() == 1  # the ONLY call surface the loop has


def test_no_rem_sleep_import_or_reference():
    # The meaningful, non-fragile check is the import list -- module
    # docstring prose legitimately says what this module is NOT
    # (including the word "REM" itself, e.g. "It is explicitly NOT:
    # REM, sleep..."), which is not a functional reference.
    with open(os.path.join(ANAXI_FINAL, "workspace_roaming.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("llama_sleep", "sleep_receipts", "reflection_pathway"):
        assert forbidden not in source, f"unexpected reference to {forbidden!r}"
    import ast
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    # WSP2-P3: three deliberate, narrow additions --
    # collections (stdlib, the bounded recent-public-decisions deque),
    # workspace_episode_provenance (canonical episode recording, itself
    # independently checked to import none of Kardia/hippocampus/
    # direction/Sleep-REM -- see test_workspace_episode_provenance.py),
    # and session_dialogue_window (Bridge A's departure-handoff SOURCE
    # dialogue ONLY -- collect_session_dialogue_pairs()/DEFAULT_MAX_TURNS,
    # used exclusively inside build_departure_handles()/
    # _perform_departure_handoff(), never inside build_roaming_choice_
    # messages() -- see test_ordinary_roaming_choice_prompt_still_excludes_
    # dialogue_content below, which proves the ORIGINAL frozen principle
    # this test used to enforce -- ordinary roaming decisions still never
    # see dialogue history -- remains true despite this import existing).
    assert imported_names.issubset({
        "datetime", "json", "os", "threading", "uuid", "collections",
        "workspace_direction", "workspace_private", "workspace_episode_provenance",
        "workspace_capability",
        "session_dialogue_window",
        # WSP2-P3-P1: one further deliberate, narrow addition -- sqlite3
        # (stdlib), used exclusively by _canonical_preflight_ok()'s own
        # read-only connection to check the provenance DB/pipeline/actor
        # rows are usable BEFORE any episode write is attempted (spec
        # section 1: "a preflight check ... is strongly preferred").
        "sqlite3",
        # WSP2-P4: one further deliberate, narrow addition --
        # workspace_public_continuity, itself independently checked to
        # import no workspace_private (see
        # test_workspace_public_continuity.py's own import audit) --
        # used exclusively by _roaming_loop()'s LIVE_WAKING_CONTINUITY_V1
        # refresh, immediately before each build_roaming_choice_messages()
        # call.
        "workspace_public_continuity",
        # OWC9-P1: one further deliberate, narrow addition --
        # context_budget, the SAME central aggregate prompt-budget
        # authority OWC9 already wired into ordinary waking (llama_
        # anaxi.py) -- reused here unchanged, never forked, for roaming
        # Stage-1's own aggregate composition. Independently checked to
        # import nothing beyond stdlib (see context_budget.py's own
        # module contents -- no imports at all).
        "context_budget",
        # Pure windowed-delivery helper (imports only context_budget and
        # workspace_capability): a truthful window instead of a dropped result.
        "workspace_delivery",
    })


# ------------------------------------------------------ I: memory boundary


def test_roaming_module_never_imports_kardia_hippocampus_journal_dialogue():
    # The AST import-list check above (test_no_rem_sleep_import_or_
    # reference) already proves this module imports nothing beyond
    # {datetime, json, os, threading, uuid, workspace_direction,
    # workspace_private} -- so it has no CAPABILITY to reach any of the
    # following, regardless of how their names appear in its own
    # explanatory prose (e.g. this module's docstring legitimately
    # discusses conversation_direction.py and workspace_supervisor.py's
    # "orchestration layer" by name, as design comparisons, without
    # importing either). workspace_private itself is independently
    # checked by test_workspace_private.py's own equivalent import test.
    with open(os.path.join(ANAXI_FINAL, "workspace_roaming.py"), encoding="utf-8") as f:
        source = f.read()
    import ast
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    assert imported_names.issubset({
        "datetime", "json", "os", "threading", "uuid", "collections",
        "workspace_direction", "workspace_private", "workspace_episode_provenance",
        "workspace_capability",
        "session_dialogue_window",
        # WSP2-P3-P1: one further deliberate, narrow addition -- sqlite3
        # (stdlib), used exclusively by _canonical_preflight_ok()'s own
        # read-only connection to check the provenance DB/pipeline/actor
        # rows are usable BEFORE any episode write is attempted (spec
        # section 1: "a preflight check ... is strongly preferred").
        "sqlite3",
        # WSP2-P4: one further deliberate, narrow addition --
        # workspace_public_continuity, itself independently checked to
        # import no workspace_private (see
        # test_workspace_public_continuity.py's own import audit) --
        # used exclusively by _roaming_loop()'s LIVE_WAKING_CONTINUITY_V1
        # refresh, immediately before each build_roaming_choice_messages()
        # call.
        "workspace_public_continuity",
        # OWC9-P1: one further deliberate, narrow addition --
        # context_budget, the SAME central aggregate prompt-budget
        # authority OWC9 already wired into ordinary waking (llama_
        # anaxi.py) -- reused here unchanged, never forked, for roaming
        # Stage-1's own aggregate composition. Independently checked to
        # import nothing beyond stdlib (see context_budget.py's own
        # module contents -- no imports at all).
        "context_budget",
        # Pure windowed-delivery helper (imports only context_budget and
        # workspace_capability): a truthful window instead of a dropped result.
        "workspace_delivery",
    })
    for forbidden_import_line in (
        "import hippocampus_store", "import clark_journal",
        "import native_provenance_writer", "import conversation_direction", "import orchestration",
        "import ollama",
    ):
        assert forbidden_import_line not in source


def test_no_automatic_journal_write_on_wait_or_stop():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("no_auto_journal")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "wait", "wait_minutes": 5},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-14", trace_path=os.path.join(TEST_ROOT, "wr_trace_nojournal.jsonl"))
    assert wait_until_stopped()
    assert not os.path.isdir(paths.journal_dir) or os.listdir(paths.journal_dir) == []


# --------------------------------------------------- J: UI/shared backend -


def test_roaming_state_is_single_process_global_not_per_surface():
    wr.reset_roaming_state()
    state_a = wr.get_roaming_state()
    state_a["authorized"] = True
    state_b = wr.get_roaming_state()
    assert state_a is state_b  # same object -- one backend, one state, any number of UI surfaces


# ------------------------------------------------------------ K: shutdown -


def test_worker_thread_is_daemon():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.05
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "wait", "wait_minutes": 5}, {"roaming_act": "stop", "wait_minutes": 5}])
    paths = fresh_paths("daemon_check")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-15", trace_path=os.path.join(TEST_ROOT, "wr_trace_daemon.jsonl"))
    thread = wr.get_roaming_state()["worker_thread"]
    assert thread is not None
    assert thread.daemon is True
    wr.stop_roaming_worker()
    assert wait_until_stopped()


def test_no_external_scheduler_or_task_creation():
    # Narrow, functional checks -- not a bare "Scheduler" substring
    # scan, since this module's own docstring legitimately explains
    # (in prose) that it creates no "Windows Task Scheduler entry".
    with open(os.path.join(ANAXI_FINAL, "workspace_roaming.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("schtasks", "subprocess", "win32", "crontab", "at.exe", "os.system", "os.startfile"):
        assert forbidden not in source


# ------------------------------------------------------------------ misc --


def test_no_free_form_reasoning_persisted_in_trace():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("no_free_form")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "journal", "action": "append", "relative_path": "", "content": "SECRET_FREEFORM_TEXT_MARKER"},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_freeform.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-16", trace_path=trace_path)
    assert wait_until_stopped()
    with open(trace_path, encoding="utf-8") as f:
        raw_trace_text = f.read()
    assert "SECRET_FREEFORM_TEXT_MARKER" not in raw_trace_text


def test_api1_api2_source_hashes_unchanged():
    import hashlib
    expected_prefixes = {
        "api1_control_plane.py": "b98c648c28168e69",
        "api2_control_plane.py": "b40aed1b38989bb9",
        "api2_supervisor.py": "74b48d59a052689b",
        "decision_path_core.py": "c8ecbb58fd4db63c",
    }
    for fn, prefix in expected_prefixes.items():
        with open(os.path.join(ANAXI_FINAL, fn), "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        assert actual.startswith(prefix), f"{fn} hash changed: {actual}"


# ==================================================== WSP2-P3: departure handoff


HUMAN_ACTOR_ID = "human-actor-test-canonical"


def fresh_provenance_dir(name, seed_human=False, seed_host=True, seed_clark=True, seed_pipeline=True, create_db=True):
    """WSP2-P3-P1: every seeding step is now individually toggleable
    so canonical-failure tests can construct exactly the missing-
    prerequisite scenario they need to prove (missing DB entirely,
    missing pipeline row, missing actor rows) without duplicating this
    whole fixture per scenario."""
    test_dir = os.path.join(TEST_ROOT, name + "_prov")
    os.makedirs(test_dir, exist_ok=True)
    if not create_db:
        return test_dir, "anaxi_orchestration_lineage_a"
    db_path = os.path.join(test_dir, "anaxi_provenance.db")
    conn = provenance_schema.create_provenance_db(db_path)
    pipeline_key = "anaxi_orchestration_lineage_a"
    if seed_pipeline:
        conn.execute(
            "INSERT INTO pipelines (pipeline_id, pipeline_key, legacy_substrate_label, description) "
            "VALUES ('pipe-test-1', ?, 'llama', 'test')", (pipeline_key,),
        )
    if seed_clark:
        conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'clark_agent', 'clark', 1000)", (CLARK_ACTOR_ID,))
    if seed_host:
        host_actor_id = wep.host_actor_id()
        conn.execute("INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'host_system', 'bounded_clause_renderer', 1000)", (host_actor_id,))
        conn.execute("INSERT INTO actor_host_system (actor_id, subsystem_key) VALUES (?, 'bounded_clause_renderer')", (host_actor_id,))
    if seed_human:
        conn.execute(
            "INSERT INTO actors (actor_id, actor_type, stable_key, created_at) VALUES (?, 'human_person', ?, 1000)",
            (HUMAN_ACTOR_ID, HUMAN_ACTOR_ID),
        )
    conn.commit()
    conn.close()
    return test_dir, pipeline_key


def _fake_pair(event_id, prompt, clark_text):
    return {"event_id": event_id, "occurred_at": 1000, "prompt": prompt, "clark_text": clark_text}


def test_ordinary_roaming_choice_prompt_still_excludes_dialogue_content():
    # The original WSP2-S1 frozen principle (build_roaming_choice_
    # messages() never pulls dialogue history) remains true even
    # though session_dialogue_window is now a legitimate import for
    # Bridge A specifically -- proven directly against the actual
    # rendered ordinary-choice prompt, not just the import list.
    secret_prompt = "SECRET_BEDTIME_CONVERSATION_MARKER"
    handles, ordered = wr.build_departure_handles([_fake_pair("evt-1", secret_prompt, "reply")])
    assert secret_prompt in handles["d1"]["text"]  # confirms the fixture is meaningful
    messages = wr.build_roaming_choice_messages(wr.empty_roaming_state())
    rendered = json.dumps(messages)
    assert secret_prompt not in rendered


def test_build_departure_handles_empty_pairs():
    handles, ordered = wr.build_departure_handles([])
    assert handles == {} and ordered == []


def test_build_departure_handles_assigns_handles_in_chronological_order():
    pairs = [_fake_pair("evt-1", "first prompt", "first reply"), _fake_pair("evt-2", "second prompt", "second reply")]
    handles, ordered = wr.build_departure_handles(pairs)
    assert ordered == ["d1", "d2", "d3", "d4"]
    assert handles["d1"] == {"author": "human", "text": "first prompt", "event_id": "evt-1", "sequence": None}
    assert handles["d2"] == {"author": "clark", "text": "first reply", "event_id": "evt-1", "sequence": 1}


def test_build_departure_handles_respects_pair_count_bound():
    pairs = [_fake_pair(f"evt-{i}", f"prompt{i}", f"reply{i}") for i in range(20)]
    handles, ordered = wr.build_departure_handles(pairs, max_pairs=8)
    assert len(ordered) == 16  # 8 pairs * 2 units


def test_build_departure_handles_aggregate_char_bound_keeps_newest():
    big_text = "x" * 4000
    pairs = [_fake_pair("evt-old", big_text, big_text), _fake_pair("evt-new", "short new prompt", "short new reply")]
    handles, ordered = wr.build_departure_handles(pairs, aggregate_max_chars=1000)
    texts = [handles[h]["text"] for h in ordered]
    assert "short new prompt" in texts and "short new reply" in texts
    assert big_text not in texts  # oldest, over-budget unit dropped whole, never spliced


def test_handoff_choice_zero_items_valid():
    handles, ordered = wr.build_departure_handles([_fake_pair("evt-1", "p", "c")])
    resolved, failure = wr.validate_and_resolve_handoff_choice({"carry_forward": []}, handles)
    assert failure is None and resolved == []


def test_handoff_choice_valid_selection_resolves_source_and_note():
    handles, ordered = wr.build_departure_handles([_fake_pair("evt-1", "Would you like to roam?", "Yes, I would.")])
    raw = {"carry_forward": [{"source_handles": ["d1"], "note": "Alex's roaming offer."}]}
    resolved, failure = wr.validate_and_resolve_handoff_choice(raw, handles)
    assert failure is None
    assert resolved[0]["note"] == "Alex's roaming offer."
    assert resolved[0]["excerpts"][0]["text"] == "Would you like to roam?"
    assert resolved[0]["excerpts"][0]["author"] == "human"


def test_handoff_choice_more_than_three_items_rejected():
    handles, ordered = wr.build_departure_handles([_fake_pair(f"evt-{i}", f"p{i}", f"c{i}") for i in range(4)])
    raw = {"carry_forward": [{"source_handles": [h], "note": "n"} for h in ordered[:4]]}
    resolved, failure = wr.validate_and_resolve_handoff_choice(raw, handles)
    assert failure == wr.HandoffFailure.TOO_MANY_ITEMS


def test_handoff_choice_unknown_handle_rejected():
    handles, ordered = wr.build_departure_handles([_fake_pair("evt-1", "p", "c")])
    raw = {"carry_forward": [{"source_handles": ["d99"], "note": "n"}]}
    resolved, failure = wr.validate_and_resolve_handoff_choice(raw, handles)
    assert failure == wr.HandoffFailure.UNKNOWN_SOURCE_HANDLE


def test_handoff_choice_model_cannot_bypass_with_a_fabricated_canonical_id():
    handles, ordered = wr.build_departure_handles([_fake_pair("evt-1", "p", "c")])
    raw = {"carry_forward": [{"source_handles": ["evt-1"], "note": "n"}]}  # a real event_id, not a handle
    resolved, failure = wr.validate_and_resolve_handoff_choice(raw, handles)
    assert failure == wr.HandoffFailure.UNKNOWN_SOURCE_HANDLE


def test_handoff_choice_malformed_json_contained():
    handles, ordered = wr.build_departure_handles([_fake_pair("evt-1", "p", "c")])
    resolved, failure = wr.validate_and_resolve_handoff_choice("not json at all {{{", handles)
    assert failure == wr.HandoffFailure.MALFORMED_HANDOFF


def test_handoff_choice_bounds_enforced():
    handles, ordered = wr.build_departure_handles([_fake_pair("evt-1", "p" * 2000, "c")])
    raw = {"carry_forward": [{"source_handles": ["d1"], "note": "n" * 500}]}
    resolved, failure = wr.validate_and_resolve_handoff_choice(raw, handles)
    assert failure is None
    assert len(resolved[0]["note"]) == wr.HANDOFF_NOTE_MAX_CHARS
    assert len(resolved[0]["excerpts"][0]["text"]) == wr.HANDOFF_SOURCE_EXCERPT_MAX_CHARS


def test_empty_departure_handoff_zero_model_calls_and_episode_recorded():
    data_dir, pipeline_key = fresh_provenance_dir("empty_handoff")
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("empty_handoff_ws"),
        session_id="sess-empty-handoff", trace_path=os.path.join(TEST_ROOT, "wr_trace_empty_handoff.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
    )
    assert ok
    assert wait_until_stopped()
    assert fake.call_count() == 1  # zero model calls for the handoff itself, exactly one for the stop choice
    run_id = wr.get_roaming_state().get("run_id") or _last_started_run_id(data_dir)
    pending = wep.find_pending_episode_run_ids(data_dir)
    assert len(pending) == 1


def _last_started_run_id(data_dir):
    conn = __import__("sqlite3").connect(os.path.join(data_dir, "anaxi_provenance.db"))
    row = conn.execute(
        "SELECT component_text FROM event_components WHERE component_kind = ? ORDER BY rowid DESC LIMIT 1",
        (wep.RUN_ID_LINK_COMPONENT_KIND,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def test_full_roaming_run_with_data_dir_records_canonical_episode_end_to_end():
    data_dir, pipeline_key = fresh_provenance_dir("full_episode")
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "journal", "action": "append", "relative_path": "", "content": "Noticed the quiet."},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.SECONDS_PER_MINUTE = 0.001
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("full_episode_ws"),
        session_id="sess-full-episode", trace_path=os.path.join(TEST_ROOT, "wr_trace_full_episode.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
    )
    assert ok
    assert wait_until_stopped()
    run_id = _last_started_run_id(data_dir)
    episode = wep.query_episode(data_dir, run_id)
    event_types = [e["event_type"] for e in episode]
    assert event_types[0] == wep.EVENT_EPISODE_STARTED
    assert wep.EVENT_HANDOFF_SELECTED in event_types
    assert wep.EVENT_PUBLIC_ACTION in event_types
    assert event_types[-1] == wep.EVENT_EPISODE_ENDED
    action_event = next(e for e in episode if e["event_type"] == wep.EVENT_PUBLIC_ACTION)
    journal_text = next(c for c in action_event["components"] if c["component_kind"] == "roaming_journal_text")
    assert journal_text["component_text"] == "Noticed the quiet."
    assert journal_text["creator_actor_id"] == CLARK_ACTOR_ID
    ended_event = next(e for e in episode if e["event_type"] == wep.EVENT_EPISODE_ENDED)
    term = next(c for c in ended_event["components"] if c["component_kind"] == "roaming_termination_fact")
    assert json.loads(term["component_text"])["termination_class"] == wep.TERMINATION_CLARK_STOP
    # Not yet delivered to any waking turn -- still pending, ready for
    # workspace_episode_context.build_workspace_episode_context().
    assert run_id in wep.find_pending_episode_run_ids(data_dir)


def test_private_act_never_appends_to_recent_public_window():
    data_dir, pipeline_key = fresh_provenance_dir("private_no_leak")
    private_paths = fresh_private_paths("private_no_leak")
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "append", "content": "a private thought"},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.SECONDS_PER_MINUTE = 0.001
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("private_no_leak_ws"),
        session_id="sess-private-no-leak", trace_path=os.path.join(TEST_ROOT, "wr_trace_private_no_leak.jsonl"),
        private_paths=private_paths, data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
    )
    assert ok
    assert wait_until_stopped()
    assert len(wr.get_roaming_state()["recent_public_decisions"]) == 0


def test_handoff_structural_failure_prevents_ordinary_loop_from_starting():
    data_dir, pipeline_key = fresh_provenance_dir("handoff_fail")
    wr.reset_roaming_state()
    # First call (the handoff-selection call) returns garbage; the
    # ordinary roaming loop must never even attempt a second call.
    fake = sequenced_ask_llama_for_json(["not valid json for the handoff"])
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("handoff_fail_ws"),
        session_id="sess-handoff-fail", trace_path=os.path.join(TEST_ROOT, "wr_trace_handoff_fail.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key,
        dialogue_pairs=[_fake_pair("evt-1", "prompt", "reply")],
    )
    assert ok
    assert wait_until_stopped()
    assert fake.call_count() == 1  # never reached the ordinary decision loop
    assert wr.get_roaming_state()["authorized"] is False


# =================================================== WSP2-P3-P1: canonical fail-closed


def test_canonical_missing_db_prevents_ordinary_roaming_loop():
    # No anaxi_provenance.db exists at all at data_dir -- the preflight
    # must catch this BEFORE any write is even attempted.
    data_dir, pipeline_key = fresh_provenance_dir("missing_db", create_db=False)
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("missing_db_ws"),
        session_id="sess-missing-db", trace_path=os.path.join(TEST_ROOT, "wr_trace_missing_db.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
    )
    assert ok
    assert wait_until_stopped()
    assert fake.call_count() == 0  # ordinary roaming loop never entered
    assert wr.get_roaming_state()["authorized"] is False


def test_canonical_missing_pipeline_prevents_ordinary_roaming_loop():
    data_dir, pipeline_key = fresh_provenance_dir("missing_pipeline", seed_pipeline=False)
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("missing_pipeline_ws"),
        session_id="sess-missing-pipeline", trace_path=os.path.join(TEST_ROOT, "wr_trace_missing_pipeline.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
    )
    assert ok
    assert wait_until_stopped()
    assert fake.call_count() == 0
    assert wr.get_roaming_state()["authorized"] is False


def test_canonical_missing_host_actor_prevents_ordinary_roaming_loop():
    data_dir, pipeline_key = fresh_provenance_dir("missing_host_actor", seed_host=False)
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("missing_host_actor_ws"),
        session_id="sess-missing-host", trace_path=os.path.join(TEST_ROOT, "wr_trace_missing_host.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
    )
    assert ok
    assert wait_until_stopped()
    assert fake.call_count() == 0
    assert wr.get_roaming_state()["authorized"] is False


def test_canonical_missing_clark_actor_prevents_ordinary_roaming_loop():
    data_dir, pipeline_key = fresh_provenance_dir("missing_clark_actor", seed_clark=False)
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("missing_clark_actor_ws"),
        session_id="sess-missing-clark", trace_path=os.path.join(TEST_ROOT, "wr_trace_missing_clark.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
    )
    assert ok
    assert wait_until_stopped()
    assert fake.call_count() == 0
    assert wr.get_roaming_state()["authorized"] is False


def test_canonical_handoff_persistence_failure_prevents_ordinary_roaming_loop():
    # Preconditions are otherwise fine (DB/pipeline/host/clark actors
    # all seeded), but the selected excerpt is human-authored and no
    # human actor row exists -- record_episode_handoff() fails closed
    # (workspace_episode_provenance.EpisodeProvenanceError), which
    # must prevent ordinary roaming exactly like a structurally
    # invalid CHOICE would.
    data_dir, pipeline_key = fresh_provenance_dir("handoff_persist_fail", seed_human=False)
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    fake = sequenced_ask_llama_for_json([
        {"carry_forward": [{"source_handles": ["d1"], "note": "keep this"}]},
        {"roaming_act": "stop", "wait_minutes": 5},  # must never be reached
    ])
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("handoff_persist_fail_ws"),
        session_id="sess-handoff-persist-fail", trace_path=os.path.join(TEST_ROOT, "wr_trace_handoff_persist_fail.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key,
        dialogue_pairs=[_fake_pair("evt-1", "Would you like to roam?", "Yes, I would.")],
    )
    assert ok
    assert wait_until_stopped()
    assert fake.call_count() == 1  # the handoff call itself, never the ordinary roaming-choice call
    assert wr.get_roaming_state()["authorized"] is False


def test_mid_run_canonical_public_action_failure_stops_further_roaming():
    # Preflight/episode-start/handoff all succeed; the FIRST public
    # action's own canonical persistence then fails (simulated by
    # monkeypatching wep.record_public_action) -- roaming must stop
    # immediately, never offering a next decision, even though the
    # workspace action itself already, mechanically, succeeded.
    data_dir, pipeline_key = fresh_provenance_dir("mid_run_fail")
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "journal", "action": "append", "relative_path": "", "content": "Noticed the quiet."},
        {"roaming_act": "stop", "wait_minutes": 5},  # must never be reached
    ])
    original_record_public_action = wep.record_public_action
    wep.record_public_action = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated canonical failure"))
    try:
        ok = wr.start_roaming_worker(
            ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("mid_run_fail_ws"),
            session_id="sess-mid-run-fail", trace_path=os.path.join(TEST_ROOT, "wr_trace_mid_run_fail.jsonl"),
            data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
        )
        assert ok
        assert wait_until_stopped()  # process/thread survives -- contained, not crashed
    finally:
        wep.record_public_action = original_record_public_action
    assert fake.call_count() == 2  # the roaming-choice call + the workspace-action call; never a 3rd
    assert wr.get_roaming_state()["authorized"] is False
    # The action itself already, mechanically happened -- its
    # noncanonical evidence is retained, not falsely erased:
    action_log_records = wr.query_roaming_trace(os.path.join(TEST_ROOT, "wr_trace_mid_run_fail.jsonl"))
    assert any(r.get("workspace_action_status") == "performed" for r in action_log_records)


def test_mid_run_canonical_wait_failure_stops_further_roaming():
    data_dir, pipeline_key = fresh_provenance_dir("mid_run_wait_fail")
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "wait", "wait_minutes": 5},
        {"roaming_act": "stop", "wait_minutes": 5},  # must never be reached
    ])
    original_record_public_wait = wep.record_public_wait
    wep.record_public_wait = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated canonical failure"))
    try:
        ok = wr.start_roaming_worker(
            ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("mid_run_wait_fail_ws"),
            session_id="sess-mid-run-wait-fail", trace_path=os.path.join(TEST_ROOT, "wr_trace_mid_run_wait_fail.jsonl"),
            data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
        )
        assert ok
        assert wait_until_stopped()
    finally:
        wep.record_public_wait = original_record_public_wait
    assert fake.call_count() == 1  # only the roaming-choice call that selected "wait"
    assert wr.get_roaming_state()["authorized"] is False


def test_jsonl_availability_cannot_rescue_a_canonical_failure():
    # The noncanonical trace/action-log path is fully functional and
    # writable throughout -- proving that canonical failure alone,
    # with JSONL perfectly healthy, still stops the run. This is the
    # exact "JSONL availability cannot turn a canonical failure into
    # continued roaming" requirement.
    data_dir, pipeline_key = fresh_provenance_dir("jsonl_cannot_rescue")
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    trace_path = os.path.join(TEST_ROOT, "wr_trace_jsonl_cannot_rescue.jsonl")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "journal", "action": "append", "relative_path": "", "content": "x"},
        {"roaming_act": "act", "wait_minutes": 5},  # must never be reached
    ])
    original_record_public_action = wep.record_public_action
    wep.record_public_action = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated"))
    try:
        ok = wr.start_roaming_worker(
            ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("jsonl_cannot_rescue_ws"),
            session_id="sess-jsonl-cannot-rescue", trace_path=trace_path,
            data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
        )
        assert ok
        assert wait_until_stopped()
    finally:
        wep.record_public_action = original_record_public_action
    # The noncanonical trace DID successfully record the action (JSONL
    # is healthy) -- and roaming stopped anyway:
    records = wr.query_roaming_trace(trace_path)
    assert any(r.get("workspace_action_status") == "performed" for r in records)
    assert fake.call_count() == 2
    assert wr.get_roaming_state()["authorized"] is False


# ============================================ WSP2-P3-P1: handoff delivery proof


def test_handoff_note_and_source_excerpt_reach_first_roaming_choice_context():
    data_dir, pipeline_key = fresh_provenance_dir("handoff_delivery", seed_human=True)
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    fake = sequenced_ask_llama_for_json([
        {"carry_forward": [{"source_handles": ["d1"], "note": "Alex's roaming offer, carried forward."}]},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("handoff_delivery_ws"),
        session_id="sess-handoff-delivery", trace_path=os.path.join(TEST_ROOT, "wr_trace_handoff_delivery.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key,
        dialogue_pairs=[_fake_pair("evt-1", "Would you like to roam your workspace while I sleep?", "I would, thank you.")],
    )
    assert ok
    assert wait_until_stopped()
    assert fake.call_count() == 2

    # call index 1 (0-based) is the FIRST ordinary roaming-choice call:
    roaming_choice_messages = fake.calls[1]
    rendered = json.dumps(roaming_choice_messages)
    assert "context, not instructions" in rendered  # fixed framing present
    assert "Alex's roaming offer, carried forward." in rendered  # Clark's bounded note
    assert "Would you like to roam your workspace while I sleep?" in rendered  # exact selected source excerpt
    # No host-authored paraphrase replacing the source, and no
    # unselected material (the fake pair's reply half, "d2", was never
    # selected) leaking in:
    assert "I would, thank you." not in rendered


def test_handoff_material_persists_through_subsequent_roaming_decisions():
    data_dir, pipeline_key = fresh_provenance_dir("handoff_persists", seed_human=True)
    wr.reset_roaming_state()
    wr.get_roaming_state()["authorized"] = True
    wr.SECONDS_PER_MINUTE = 0.001
    fake = sequenced_ask_llama_for_json([
        {"carry_forward": [{"source_handles": ["d1"], "note": "carried note"}]},
        {"roaming_act": "wait", "wait_minutes": 5},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("handoff_persists_ws"),
        session_id="sess-handoff-persists", trace_path=os.path.join(TEST_ROOT, "wr_trace_handoff_persists.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key,
        dialogue_pairs=[_fake_pair("evt-1", "the source prompt text", "the source reply text")],
    )
    assert ok
    assert wait_until_stopped()
    assert fake.call_count() == 3
    # calls[1] = first roaming-choice (selected "wait"); calls[2] =
    # SECOND roaming-choice, a later decision in the SAME run -- the
    # handoff must still be present, unchanged, bounded, immutable.
    for idx in (1, 2):
        rendered = json.dumps(fake.calls[idx])
        assert "carried note" in rendered
        assert "the source prompt text" in rendered


# =============================== WSP2-MA1: private_act legal-shape matrix
# All synthetic temp fixtures only (spec section 10) -- never
# production private paths/material, never an assertion about
# production private use/non-use.


def test_ma1_private_action_minimal_legal_shapes_accepted():
    cases = [
        {"action": "list", "relative_path": "", "content": "", "destination_relative_path": ""},
        {"action": "read", "relative_path": "note.txt", "content": "", "destination_relative_path": ""},
        {"action": "write", "relative_path": "note.txt", "content": "text", "destination_relative_path": ""},
        {"action": "append", "relative_path": "note.txt", "content": "more", "destination_relative_path": ""},
        {"action": "rename", "relative_path": "a.txt", "content": "", "destination_relative_path": "b.txt"},
        {"action": "delete", "relative_path": "a.txt", "content": "", "destination_relative_path": ""},
    ]
    for raw in cases:
        validated, failure = wpriv.validate_private_action(raw)
        assert failure is None, raw
        assert validated == raw


def test_ma1_private_action_all_fields_required():
    base = {"action": "list", "relative_path": "", "content": "", "destination_relative_path": ""}
    for missing in ("action", "relative_path", "content", "destination_relative_path"):
        raw = dict(base)
        del raw[missing]
        _, failure = wpriv.validate_private_action(raw)
        assert failure == wpriv.PrivateFailure.MALFORMED_ACTION, missing


def test_ma1_private_action_forbidden_extra_field_rejected():
    raw = {"action": "list", "relative_path": "", "content": "", "destination_relative_path": "", "extra": ""}
    _, failure = wpriv.validate_private_action(raw)
    assert failure == wpriv.PrivateFailure.PROTOCOL_LEAKAGE


def test_ma1_private_action_wrong_types_rejected():
    base = {"action": "list", "relative_path": "", "content": "", "destination_relative_path": ""}
    for field in ("relative_path", "content", "destination_relative_path"):
        for bad_value in (5, None, ["x"]):
            raw = dict(base)
            raw[field] = bad_value
            _, failure = wpriv.validate_private_action(raw)
            assert failure == wpriv.PrivateFailure.MALFORMED_ACTION, (field, bad_value)


def test_ma1_private_action_invalid_discriminator_rejected():
    _, failure = wpriv.validate_private_action(
        {"action": "execute", "relative_path": "", "content": "", "destination_relative_path": ""}
    )
    assert failure == wpriv.PrivateFailure.INVALID_ACTION


# ===================================== WSP2-MA1: private-safe generic telemetry


def test_ma1_generic_structural_failure_mapping_covers_all_known_codes():
    for code in (
        wd.DirectionFailure.MALFORMED_ACTION, wd.DirectionFailure.PROTOCOL_LEAKAGE,
        wd.DirectionFailure.UNKNOWN_RESOURCE_CLASS, wd.DirectionFailure.NOT_IN_ALLOWED_SURFACE,
        wd.DirectionFailure.INVALID_RELATIVE_PATH, wd.DirectionFailure.INVALID_CONTENT,
        wpriv.PrivateFailure.MALFORMED_ACTION, wpriv.PrivateFailure.PROTOCOL_LEAKAGE,
        wpriv.PrivateFailure.INVALID_ACTION,
    ):
        generic = wr._generic_structural_failure_code(code)
        assert generic in wr._GENERIC_STRUCTURAL_FAILURE_CODES, (code, generic)
    # Never silently pass a branch-specific code through unrecognized.
    assert wr._generic_structural_failure_code("SOME_UNKNOWN_CODE") in wr._GENERIC_STRUCTURAL_FAILURE_CODES


def test_ma1_ordinary_structural_failure_trace_is_redacted_and_generic():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("ma1_ordinary_structural")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "list"},  # missing relative_path/content -- MALFORMED_ACTION
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_ma1_ordinary.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-ma1-ord", trace_path=trace_path)
    assert wait_until_stopped()
    records = wr.query_roaming_trace(trace_path)
    last = records[-1]
    assert last["run_status"] == wr.RUN_STATUS_FAILED
    assert last["roaming_act"] is None
    assert last["wait_minutes"] is None
    assert last["workspace_action_status"] in wr._GENERIC_STRUCTURAL_FAILURE_CODES
    assert last["workspace_action_status"] != wd.DirectionFailure.MALFORMED_ACTION  # specific code never reaches the trace


def test_ma1_private_paths_none_trace_is_redacted_and_generic():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("ma1_private_unwired")
    fake = sequenced_ask_llama_for_json([{"roaming_act": "private_act", "wait_minutes": 5}])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_ma1_priv_unwired.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-ma1-priv-unwired", trace_path=trace_path)  # no private_paths
    assert wait_until_stopped()
    records = wr.query_roaming_trace(trace_path)
    last = records[-1]
    assert last["run_status"] == wr.RUN_STATUS_FAILED
    assert last["roaming_act"] is None
    assert last["wait_minutes"] is None
    assert last["workspace_action_status"] in wr._GENERIC_STRUCTURAL_FAILURE_CODES


def test_ma1_private_choice_failure_trace_is_redacted_and_generic():
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("ma1_private_structural_ws")
    private_paths = fresh_private_paths("ma1_private_structural")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "read"},  # missing relative_path/content/destination_relative_path -- MALFORMED_ACTION
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_ma1_priv_structural.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-ma1-priv-structural", trace_path=trace_path, private_paths=private_paths)
    assert wait_until_stopped()
    records = wr.query_roaming_trace(trace_path)
    last = records[-1]
    assert last["run_status"] == wr.RUN_STATUS_FAILED
    assert last["roaming_act"] is None
    assert last["wait_minutes"] is None
    assert last["workspace_action_status"] in wr._GENERIC_STRUCTURAL_FAILURE_CODES
    assert last["workspace_action_status"] != wpriv.PrivateFailure.MALFORMED_ACTION  # specific code never reaches the trace
    with open(trace_path, encoding="utf-8") as f:
        raw_trace_text = f.read()
    assert "private_act" not in raw_trace_text  # the branch identity itself never reaches the durable trace


def test_ma1_ordinary_and_private_structural_failures_produce_identical_trace_shape():
    # The core privacy property (spec section 2/15): a trace reader
    # must not be able to tell, from the failure record's shape alone,
    # whether a structural control-validation failure came from the
    # ordinary act branch or the private_act branch.
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    ordinary_paths = fresh_paths("ma1_indistinguishable_ordinary")
    fake_ordinary = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library"},  # structurally malformed
    ])
    trace_path_ordinary = os.path.join(TEST_ROOT, "wr_trace_ma1_indist_ordinary.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake_ordinary, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=ordinary_paths, session_id="sess-ma1-indist-a", trace_path=trace_path_ordinary)
    assert wait_until_stopped()
    ordinary_record = wr.query_roaming_trace(trace_path_ordinary)[-1]

    wr.reset_roaming_state()
    wpriv.reset_private_state()
    state = wr.get_roaming_state()
    state["authorized"] = True
    private_ws_paths = fresh_paths("ma1_indistinguishable_private_ws")
    private_paths = fresh_private_paths("ma1_indistinguishable_private")
    fake_private = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "write"},  # structurally malformed
    ])
    trace_path_private = os.path.join(TEST_ROOT, "wr_trace_ma1_indist_private.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake_private, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=private_ws_paths, session_id="sess-ma1-indist-b",
                             trace_path=trace_path_private, private_paths=private_paths)
    assert wait_until_stopped()
    private_record = wr.query_roaming_trace(trace_path_private)[-1]

    # Same shape for the fields that must never distinguish the two --
    # only session_id/timestamp/trace_id legitimately differ.
    for field in ("roaming_act", "wait_minutes", "workspace_action_id", "run_status"):
        assert ordinary_record[field] == private_record[field], field
    # And the failure code itself is drawn from the SAME shared
    # vocabulary in both cases -- not proof they'd always be textually
    # identical (different specific defects can map to different
    # generic classes), but proof neither ever leaks a branch-specific
    # code name.
    assert ordinary_record["workspace_action_status"] in wr._GENERIC_STRUCTURAL_FAILURE_CODES
    assert private_record["workspace_action_status"] in wr._GENERIC_STRUCTURAL_FAILURE_CODES


# ========================== WSP3-P2: private execution-failure telemetry
# Synthetic temp fixtures only (spec section 2/10) -- never production
# private paths/material, never an assertion about production private
# use/non-use.


def test_wsp3p2_generic_execution_mapping_is_single_shared_value():
    for code in (
        wpriv.PrivateFailure.PATH_ESCAPE, wpriv.PrivateFailure.NOT_FOUND,
        wpriv.PrivateFailure.UNSUPPORTED_EXTENSION, wpriv.PrivateFailure.IO_ERROR,
        wpriv.PrivateFailure.INVALID_ACTION, None, "anything",
    ):
        assert wr._generic_execution_failure_code(code) == wr.EXECUTION_FAILURE_GENERIC_CODE


def _run_private_execution_failure(name, second_call, private_paths=None):
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths(name + "_ws")
    private_paths = private_paths or fresh_private_paths(name)
    fake = sequenced_ask_llama_for_json([{"roaming_act": "private_act", "wait_minutes": 5}, second_call])
    trace_path = os.path.join(TEST_ROOT, f"wr_trace_{name}.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id=f"sess-{name}", trace_path=trace_path, private_paths=private_paths)
    assert wait_until_stopped()
    return wr.query_roaming_trace(trace_path), trace_path


def test_wsp3p2_A_private_execution_still_fails_exactly_as_before_path_escape():
    # A: private execution still fails (fail-closed behavior itself
    # unchanged) -- proven directly against execute_private_action(),
    # not through the roaming loop, so this test cannot be confused
    # with the telemetry-shape assertions below.
    private_paths = fresh_private_paths("wsp3p2_exec_unchanged")
    validated, _ = wpriv.validate_private_action(
        {"action": "read", "relative_path": "../escape.txt", "content": "", "destination_relative_path": ""}
    )
    result, failure = wpriv.execute_private_action(private_paths, validated)
    assert result is None
    assert failure == {"failure_class": wpriv.PrivateFailure.PATH_ESCAPE}


def test_wsp3p2_C_D_durable_telemetry_generic_for_path_escape():
    records, trace_path = _run_private_execution_failure(
        "wsp3p2_path_escape",
        {"action": "read", "relative_path": "../escape_SECRET.txt", "content": "", "destination_relative_path": ""},
    )
    last = records[-1]
    assert last["workspace_action_status"] == wr.EXECUTION_FAILURE_GENERIC_CODE
    assert last["roaming_act"] is None
    assert last["wait_minutes"] is None
    assert last["workspace_action_id"] is None
    with open(trace_path, encoding="utf-8") as f:
        raw = f.read()
    for forbidden in ("private_act", wpriv.PrivateFailure.PATH_ESCAPE, "escape_SECRET", "../escape"):
        assert forbidden not in raw


def test_wsp3p2_C_D_durable_telemetry_generic_for_not_found():
    records, trace_path = _run_private_execution_failure(
        "wsp3p2_not_found",
        {"action": "delete", "relative_path": "does_not_exist_SECRET.txt", "content": "", "destination_relative_path": ""},
    )
    last = records[-1]
    assert last["workspace_action_status"] == wr.EXECUTION_FAILURE_GENERIC_CODE
    assert last["roaming_act"] is None
    with open(trace_path, encoding="utf-8") as f:
        raw = f.read()
    for forbidden in ("private_act", wpriv.PrivateFailure.NOT_FOUND, "does_not_exist_SECRET"):
        assert forbidden not in raw


def test_wsp3p2_C_D_durable_telemetry_generic_for_unsupported_extension():
    records, trace_path = _run_private_execution_failure(
        "wsp3p2_unsupported_ext",
        {"action": "read", "relative_path": "note_SECRET.exe", "content": "", "destination_relative_path": ""},
    )
    last = records[-1]
    assert last["workspace_action_status"] == wr.EXECUTION_FAILURE_GENERIC_CODE
    assert last["roaming_act"] is None
    with open(trace_path, encoding="utf-8") as f:
        raw = f.read()
    for forbidden in ("private_act", wpriv.PrivateFailure.UNSUPPORTED_EXTENSION, "note_SECRET"):
        assert forbidden not in raw


def test_wsp3p2_B_no_private_success_persisted():
    # B: confirms the pre-existing WSP3-P1 invariant remains true --
    # this gate must not have accidentally introduced any success-path
    # trace row while touching only failure-path recording.
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("wsp3p2_success_ws")
    private_paths = fresh_private_paths("wsp3p2_success")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "write", "relative_path": "note.txt", "content": "hello", "destination_relative_path": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_wsp3p2_success.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-wsp3p2-success", trace_path=trace_path, private_paths=private_paths)
    assert wait_until_stopped()
    records = wr.query_roaming_trace(trace_path)
    # Only the final 'stop' row exists -- zero rows for the successful
    # write in between.
    assert len(records) == 1
    assert records[0]["run_status"] == wr.RUN_STATUS_STOPPED_BY_CLARK


def test_wsp3p2_E_no_canonical_public_event_fabricated_on_private_execution_failure():
    data_dir, pipeline_key = fresh_provenance_dir("wsp3p2_no_fabrication")
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.get_roaming_state()["authorized"] = True
    private_paths = fresh_private_paths("wsp3p2_no_fabrication")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "read", "relative_path": "missing_SECRET.txt", "content": "", "destination_relative_path": ""},
    ])
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("wsp3p2_no_fabrication_ws"),
        session_id="sess-wsp3p2-no-fab", trace_path=os.path.join(TEST_ROOT, "wr_trace_wsp3p2_no_fab.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[], private_paths=private_paths,
    )
    assert ok
    assert wait_until_stopped()
    conn = provenance_schema.sqlite3.connect(f"file:{data_dir}/anaxi_provenance.db?mode=ro", uri=True)
    try:
        public_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type IN (?, ?)",
            (wep.EVENT_PUBLIC_ACTION, wep.EVENT_PUBLIC_WAIT),
        ).fetchone()[0]
    finally:
        conn.close()
    assert public_events == 0  # no fabricated public activity for a failed private action


def test_wsp3p2_F_no_public_continuity_renderer_can_receive_it_source_audit():
    # The generic execution-failure code is written ONLY to
    # workspace_roaming_trace.jsonl via _record() -- never through
    # workspace_episode_provenance.record_public_action()/
    # record_public_wait() (the only writers ACTIVE_WORKSPACE_
    # CONTINUITY_V1/Bridge C ever read from), so there is no code path
    # by which it could reach either.
    with open(os.path.join(ANAXI_FINAL, "workspace_roaming.py"), encoding="utf-8") as f:
        source = f.read()
    execution_failure_region_start = source.index("if private_failure is not None:")
    execution_failure_region_end = source.index("workspace_private.set_last_observation(private_result)")
    region = source[execution_failure_region_start:execution_failure_region_end]
    assert "record_public_action" not in region
    assert "record_public_wait" not in region


def test_wsp3p2_indistinguishability_ordinary_vs_private_execution_failure():
    # The core privacy property (spec section 12): an ordinary
    # execution-time failure ("not performed"/denied) and a private
    # execution-time failure must be recorded with an IDENTICAL,
    # branch-indistinguishable durable trace shape.
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    ordinary_paths = fresh_paths("wsp3p2_indist_ordinary")
    fake_ordinary = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        # Structurally VALID (read is in library's allowed_surface) --
        # denied at EXECUTION time by path containment, exactly like
        # test_traversal_attempt_fails_closed_and_ends_run's own
        # fixture. A "not in allowed surface" fixture would instead hit
        # MA1's STRUCTURAL-failure branch, not this one.
        {"resource_class": "library", "action": "read", "relative_path": "../../etc/passwd", "content": ""},
    ])
    trace_path_ordinary = os.path.join(TEST_ROOT, "wr_trace_wsp3p2_indist_ordinary.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake_ordinary, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=ordinary_paths, session_id="sess-wsp3p2-indist-a", trace_path=trace_path_ordinary)
    assert wait_until_stopped()
    ordinary_record = wr.query_roaming_trace(trace_path_ordinary)[-1]

    wr.reset_roaming_state()
    wpriv.reset_private_state()
    state = wr.get_roaming_state()
    state["authorized"] = True
    private_ws_paths = fresh_paths("wsp3p2_indist_private_ws")
    private_paths = fresh_private_paths("wsp3p2_indist_private")
    fake_private = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "read", "relative_path": "missing.txt", "content": "", "destination_relative_path": ""},  # not found
    ])
    trace_path_private = os.path.join(TEST_ROOT, "wr_trace_wsp3p2_indist_private.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake_private, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=private_ws_paths, session_id="sess-wsp3p2-indist-b",
                             trace_path=trace_path_private, private_paths=private_paths)
    assert wait_until_stopped()
    private_record = wr.query_roaming_trace(trace_path_private)[-1]

    for field in ("roaming_act", "wait_minutes", "workspace_action_id", "workspace_action_status", "run_status"):
        assert ordinary_record[field] == private_record[field], field
    # Explicit inference-by-elimination check: neither record, nor its
    # counterpart from the earlier MA1 structural-failure indistinguish-
    # ability test, nor a successful-action record, can be told apart
    # by shape alone.
    assert ordinary_record["workspace_action_status"] == wr.EXECUTION_FAILURE_GENERIC_CODE


# ========================== WSP2-MA2: Stage-2 structured control envelopes


def test_ma2_ordinary_stage2_call_receives_exact_ordinary_schema():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("ma2_ordinary_schema")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library", "action": "list", "relative_path": "", "content": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-ma2-ord-schema", trace_path=os.path.join(TEST_ROOT, "wr_trace_ma2_ord.jsonl"))
    assert wait_until_stopped()
    # Stage-1 (call 0) and the final 'stop' decision (call 2) receive
    # NO schema (Stage-1 remains unchanged, spec section 7); Stage-2
    # (call 1) receives exactly the ordinary action schema.
    assert fake.schemas[0] is None
    assert fake.schemas[1] == wd.STAGE2_ACTION_SCHEMA
    assert fake.schemas[2] is None


def test_ma2_private_stage2_call_receives_exact_private_schema():
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("ma2_private_schema_ws")
    private_paths = fresh_private_paths("ma2_private_schema")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "list", "relative_path": "", "content": "", "destination_relative_path": ""},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-ma2-priv-schema", trace_path=os.path.join(TEST_ROOT, "wr_trace_ma2_priv.jsonl"),
                             private_paths=private_paths)
    assert wait_until_stopped()
    assert fake.schemas[0] is None
    assert fake.schemas[1] == wpriv.STAGE2_PRIVATE_ACTION_SCHEMA
    assert fake.schemas[2] is None


def test_ma2_public_private_symmetry_both_schema_constrained():
    # Section 8: both branches receive equally strict structural
    # treatment -- neither is left freeform while the other is
    # constrained.
    assert wd.STAGE2_ACTION_SCHEMA["additionalProperties"] is False
    assert wpriv.STAGE2_PRIVATE_ACTION_SCHEMA["additionalProperties"] is False
    assert all(spec == {"type": "string"} for spec in wd.STAGE2_ACTION_SCHEMA["properties"].values())
    assert all(spec == {"type": "string"} for spec in wpriv.STAGE2_PRIVATE_ACTION_SCHEMA["properties"].values())


# ============================================ OWC9-P4: Stage-2 aggregate budgeting


def test_owc9p4_stage2_public_budget_composition_fits():
    messages, result = wr.compose_workspace_action_stage2_budget()
    assert result.fits is True
    assert result.final_prompt_cost <= context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET
    assert result.final_prompt_cost + context_budget.WSP1_PASS1_GENERATION_RESERVE + context_budget.SAFETY_MARGIN <= context_budget.CONTEXT_CEILING
    assert messages == wr.build_workspace_action_messages()  # nothing appended after preflight


def test_owc9p4_stage2_public_hard_overflow_no_model_call():
    # section 2/6: a hard-only overflow (an artificially tiny budget --
    # Stage-2 public's own real content is entirely fixed and always
    # fits under the real budget, see the composition-fits test above)
    # must fail closed before ask_llama_for_json() is ever invoked for
    # Stage-2 -- Stage-1's own call still happens normally.
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("owc9p4_stage2_public_overflow")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        # no second response provided -- a Stage-2 model call would raise
    ])
    original_budget = context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET
    context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET = 10
    trace_path = os.path.join(TEST_ROOT, "wr_trace_stage2_public_overflow.jsonl")
    try:
        wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                                 session_id="sess-stage2-public-overflow", trace_path=trace_path)
        assert wait_until_stopped()
    finally:
        context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET = original_budget
    assert fake.call_count() == 1  # Stage-1's own call happened; Stage-2's did not
    records = wr.query_roaming_trace(trace_path)
    assert any(r.get("workspace_action_status") == context_budget.BUDGET_EXCEEDED for r in records)
    assert wr.get_roaming_state()["authorized"] is False
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_BACKOFF


def test_owc9p4_stage2_private_budget_composition_fits_empty_observation():
    state = wpriv.empty_private_state()
    result = wpriv.compose_private_action_budget(state)
    assert result.fits is True
    assert result.final_prompt_cost <= context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET
    messages = wpriv.private_action_messages_from_composition(state, result)
    assert messages == wpriv.build_private_action_messages(state)  # nothing appended after preflight


def test_owc9p4_stage2_private_oversized_observation_drops_not_truncates():
    # section 3: the last-private-observation is SOFT/droppable
    # (mirrors Stage-1's own LAST_WORKSPACE_OBSERVATION pattern
    # exactly) -- an oversized one is dropped ENTIRELY, never
    # re-truncated mid-content, and the sent message falls back to the
    # same honest "none" default text build_private_action_messages()
    # already renders for a genuinely empty observation. SOURCE
    # CONTRACT ONLY -- this synthetic observation is test-constructed,
    # never read from a real private file.
    state = dict(wpriv.empty_private_state())
    state["last_private_observation"] = {
        "action": "read", "relative_path": "x" * 200, "content": "x" * wpriv.DEFAULT_MAX_CHARS,
    }
    result = wpriv.compose_private_action_budget(state)
    assert result.fits is True
    assert result.included_kind(context_budget.LAST_WORKSPACE_OBSERVATION) is None  # dropped
    messages = wpriv.private_action_messages_from_composition(state, result)
    empty_state = wpriv.empty_private_state()
    assert messages == wpriv.build_private_action_messages(empty_state)  # honest "none" fallback, not a partial real one


def test_owc9p4_stage2_private_hard_overflow_no_model_call():
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("owc9p4_stage2_private_overflow_ws")
    private_paths = fresh_private_paths("owc9p4_stage2_private_overflow")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        # no second response provided -- a Stage-2 model call would raise
    ])
    original_budget = context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET
    context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET = 10
    trace_path = os.path.join(TEST_ROOT, "wr_trace_stage2_private_overflow.jsonl")
    try:
        wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                                 session_id="sess-stage2-private-overflow", trace_path=trace_path,
                                 private_paths=private_paths)
        assert wait_until_stopped()
    finally:
        context_budget.WSP1_PASS1_MAX_PROMPT_BUDGET = original_budget
    assert fake.call_count() == 1  # Stage-1's own call happened; Stage-2 private's did not
    records = wr.query_roaming_trace(trace_path)
    assert any(r.get("workspace_action_status") == context_budget.BUDGET_EXCEEDED for r in records)
    assert wr.get_roaming_state()["authorized"] is False
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_BACKOFF


def test_ma2_departure_handoff_and_stage1_remain_unschema_constrained_source_audit():
    # Section 7: MA2 targets ONLY the two Stage-2 call sites -- the
    # Stage-1 roaming-choice call and the departure-handoff-selection
    # call must not have been touched.
    #
    # OWC9-P1: the Stage-1 call site itself changed shape -- it now
    # calls ask_llama_for_json(stage1_messages), where stage1_messages
    # is built from the (possibly aggregate-budget-trimmed) composition
    # via _roaming_stage1_messages_from_composition(), rather than the
    # literal ask_llama_for_json(build_roaming_choice_messages(state))
    # this test used to look for. build_roaming_choice_messages() itself
    # is unchanged (still the sole renderer, still schema-free) --
    # only WHERE/how its result reaches ask_llama_for_json() moved.
    with open(os.path.join(ANAXI_FINAL, "workspace_roaming.py"), encoding="utf-8") as f:
        source = f.read()
    assert "raw_choice = ask_llama_for_json(stage1_messages)" in source
    stage1_call_idx = source.index("raw_choice = ask_llama_for_json(stage1_messages)")
    assert "structured_schema" not in source[:stage1_call_idx]
    handoff_call_idx = source.index("raw_choice = ask_llama_for_json(build_departure_handoff_messages")
    handoff_call_line_end = source.index("\n", handoff_call_idx)
    assert "structured_schema" not in source[handoff_call_idx:handoff_call_line_end]


def test_ma2_no_fallback_retry_on_schema_failure_source_audit():
    # Section 9: no automatic retry, no repair prompt, no fallback from
    # schema mode to freeform JSON around either Stage-2 call site --
    # tested structurally (call counts / exception handling), not by
    # scanning for the word "retry", which this exact region's own
    # PRE-EXISTING, legitimate comment already contains ("no retry
    # with a different action") to describe what does NOT happen.
    with open(os.path.join(ANAXI_FINAL, "workspace_roaming.py"), encoding="utf-8") as f:
        source = f.read()
    stage2_region_start = source.index("if roaming_act == ROAMING_ACT_PRIVATE_ACT:")
    stage2_region_end = source.index("if _stop_event.wait(timeout=post_action_wait", source.index("# roaming_act == ROAMING_ACT_ACT"))
    region = source[stage2_region_start:stage2_region_end]
    # Exactly one ask_llama_for_json() call per branch (private, then
    # ordinary) -- a second call to either would be an automatic retry.
    assert region.count("ask_llama_for_json(") == 2
    # Neither Stage-2 call site is wrapped in its own try/except that
    # could catch a schema/API failure and fall back to something else.
    assert "except" not in region
    assert "format=\"json\"" not in region  # no fallback from schema mode to loose JSON here
    assert "structured_schema=None" not in region  # neither call site ever omits its schema


def test_ma2_telemetry_does_not_reveal_which_schema_was_selected():
    # Section 8/14: the durable trace must not expose schema
    # name/branch identity -- re-confirms MA1/WSP3-P2's own property
    # still holds with schema-constrained generation wired in.
    wr.reset_roaming_state()
    wpriv.reset_private_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("ma2_telemetry_ws")
    private_paths = fresh_private_paths("ma2_telemetry")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "read"},  # structurally malformed -> generic structural failure
    ])
    trace_path = os.path.join(TEST_ROOT, "wr_trace_ma2_telemetry.jsonl")
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-ma2-telemetry", trace_path=trace_path, private_paths=private_paths)
    assert wait_until_stopped()
    with open(trace_path, encoding="utf-8") as f:
        raw = f.read()
    for forbidden in ("STAGE2_PRIVATE_ACTION_SCHEMA", "STAGE2_ACTION_SCHEMA", "additionalProperties", "private_act", "destination_relative_path"):
        assert forbidden not in raw
    records = wr.query_roaming_trace(trace_path)
    assert records[-1]["workspace_action_status"] in wr._GENERIC_STRUCTURAL_FAILURE_CODES


# ============================================ WSP2-P5: background-activity lifecycle


def test_p5_fresh_process_background_activity_enabled_by_default():
    # Section 19A/19B: a fresh process starts IDLE (no worker yet) but
    # immediately ELIGIBLE for auto-start -- no human "enable" action
    # is required, only readiness/preflight (spec sections 2/5).
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    assert state["background_activity_state"] == wr.BACKGROUND_STATE_IDLE
    assert state["consecutive_technical_failures"] == 0
    assert state["backoff_until"] is None
    assert wr.is_background_activity_auto_start_eligible() is True


def test_p5_auto_start_reaches_existing_worker_start_path_no_new_loop():
    # Section 6/19C: maybe_auto_start_background_activity() reuses
    # start_roaming_worker() completely unchanged -- proven the same
    # structural way the GUI's own zero-model-call test proves reuse:
    # patch start_roaming_worker and confirm it is the function
    # actually invoked, with the exact kwargs passed through.
    wr.reset_roaming_state()
    captured = {}
    real_start = wr.start_roaming_worker

    def fake_start(**kwargs):
        captured.update(kwargs)
        return True

    wr.start_roaming_worker = fake_start
    try:
        started = wr.maybe_auto_start_background_activity(
            ask_llama_for_json=lambda *a, **k: "{}", clark_actor_id=CLARK_ACTOR_ID,
            workspace_paths=fresh_paths("p5_autostart_reuse"), session_id="sess-p5-reuse",
        )
    finally:
        wr.start_roaming_worker = real_start
    assert started is True
    assert captured["clark_actor_id"] == CLARK_ACTOR_ID
    assert wr.get_roaming_state()["authorized"] is True
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_ACTIVE


def test_p5_auto_start_no_op_when_worker_already_running():
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "wait", "wait_minutes": 60}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5_already_running"),
                             session_id="sess-p5-already", trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_already.jsonl"))
    deadline = time.time() + 2.0
    while time.time() < deadline and not wr.is_worker_running():
        time.sleep(0.01)
    assert wr.is_worker_running() is True
    started = wr.maybe_auto_start_background_activity(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
        workspace_paths=fresh_paths("p5_already_running2"), session_id="sess-p5-already",
    )
    assert started is False
    wr.stop_roaming_worker()
    assert wait_until_stopped()


def test_p5_auto_start_bridge_a_still_occurs():
    # Section 19D: the departure handoff still runs first through the
    # SAME start_roaming_worker() path when auto-started with data_dir/
    # pipeline_key/dialogue_pairs wired -- mirrors test_empty_departure_
    # handoff_zero_model_calls_and_episode_recorded's own proof for a
    # human-initiated start.
    data_dir, pipeline_key = fresh_provenance_dir("p5_bridge_a")
    wr.reset_roaming_state()
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    started = wr.maybe_auto_start_background_activity(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("p5_bridge_a_ws"),
        session_id="sess-p5-bridge-a", trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_bridge_a.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
    )
    assert started is True
    assert wait_until_stopped()
    assert fake.call_count() == 1  # zero model calls for the handoff itself
    pending = wep.find_pending_episode_run_ids(data_dir)
    assert len(pending) == 1


def test_p5_ordinary_workspace_capability_independent_of_roaming_source_audit():
    # Section 3/19E: confirms current source truth directly -- ordinary/
    # supervised workspace capability (workspace_supervisor.py) has
    # zero references to workspace_roaming, roaming authorization, or
    # background activity state; nothing needed broadening here.
    with open(os.path.join(ANAXI_FINAL, "workspace_supervisor.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("workspace_roaming", "roaming_authorized", "background_activity_state"):
        assert forbidden not in source


def test_p5_private_contract_functions_unchanged():
    # Section 19F: this gate touches zero lines of workspace_private.py
    # -- confirmed here by presence/name of the exact functions this
    # module's private_act branch depends on (deep behavior is
    # test_workspace_private.py's own unmodified regression suite).
    assert hasattr(wpriv, "build_private_action_messages")
    assert hasattr(wpriv, "validate_private_action")
    assert hasattr(wpriv, "execute_private_action")
    assert hasattr(wpriv, "STAGE2_PRIVATE_ACTION_SCHEMA")


def test_p5_malformed_choice_executes_nothing_and_enters_backoff():
    # Section 20A/B/E: a structurally invalid Stage-1 choice executes
    # no action, closes the episode, and enters BACKOFF -- not a
    # permanent revocation.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "not_a_real_act", "wait_minutes": 5}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5_malformed"), session_id="sess-p5-malformed",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_malformed.jsonl"))
    assert wait_until_stopped()
    state = wr.get_roaming_state()
    assert state["background_activity_state"] == wr.BACKGROUND_STATE_BACKOFF
    assert state["consecutive_technical_failures"] == 1
    assert state["backoff_until"] is not None and state["backoff_until"] > time.time()


def test_p5_backoff_is_temporary_not_permanent():
    # Section 20C: is_background_activity_auto_start_eligible() is
    # False while inside the backoff window, but True again once it
    # has elapsed -- proving BACKOFF differs mechanically from a
    # permanent disablement.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_BACKOFF
    state["backoff_until"] = time.time() + 900
    assert wr.is_background_activity_auto_start_eligible() is False
    state["backoff_until"] = time.time() - 1  # window already elapsed
    assert wr.is_background_activity_auto_start_eligible() is True


def test_p5_no_immediate_automatic_retry_after_failure():
    # Section 20D: nothing inside workspace_roaming.py itself starts a
    # new worker after a failure ends one -- only an external caller
    # (llama_gui.py, at specific disclosed trigger points) does.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "bogus", "wait_minutes": 5}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5_no_retry"), session_id="sess-p5-no-retry",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_no_retry.jsonl"))
    assert wait_until_stopped()
    time.sleep(0.1)
    assert wr.is_worker_running() is False  # stays stopped -- no self-restart
    assert fake.call_count() == 1  # no retry consumed a second canned response


def test_p5_new_episode_may_begin_after_backoff_elapses():
    # Section 20F: once backoff_until has passed, maybe_auto_start_
    # background_activity() succeeds again -- a genuinely NEW episode
    # opportunity, not a retry of the old failed choice.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_BACKOFF
    state["backoff_until"] = time.time() - 1
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    started = wr.maybe_auto_start_background_activity(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
        workspace_paths=fresh_paths("p5_post_backoff"), session_id="sess-p5-post-backoff",
        trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_post_backoff.jsonl"),
    )
    assert started is True
    assert wait_until_stopped()


def test_p5_bounded_backoff_policy_escalates_then_fault_pauses():
    # Section 20G/H/8: exact policy -- 1st->15min BACKOFF, 2nd->30min
    # BACKOFF, 3rd->FAULT_PAUSED (no further automatic backoff/retry).
    wr.SECONDS_PER_MINUTE = 60
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    before = time.time()
    wr._record_technical_failure(state)
    assert state["background_activity_state"] == wr.BACKGROUND_STATE_BACKOFF
    assert state["consecutive_technical_failures"] == 1
    assert abs((state["backoff_until"] - before) - 15 * 60) < 5
    wr._record_technical_failure(state)
    assert state["background_activity_state"] == wr.BACKGROUND_STATE_BACKOFF
    assert state["consecutive_technical_failures"] == 2
    assert abs((state["backoff_until"] - before) - 30 * 60) < 5
    wr._record_technical_failure(state)
    assert state["background_activity_state"] == wr.BACKGROUND_STATE_FAULT_PAUSED
    assert state["consecutive_technical_failures"] == 3
    assert state["backoff_until"] is None
    assert wr.is_background_activity_auto_start_eligible(state) is False


def test_p5_valid_progress_resets_failure_counter():
    # Section 20I/8: successful structurally-valid roaming progress
    # (an ordinary act, a private act, or a wait) resets the count.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    wr._record_technical_failure(state)
    wr._record_technical_failure(state)
    assert state["consecutive_technical_failures"] == 2
    wr._reset_technical_failure_count(state)
    assert state["consecutive_technical_failures"] == 0


def test_p5_lifecycle_state_identical_for_ordinary_vs_private_structural_failure():
    # Section 20J: no private branch information leaks through the
    # lifecycle state -- an ordinary act's Stage-2 structural failure
    # and a private_act's Stage-2 structural failure both leave
    # background_activity_state/consecutive_technical_failures in the
    # IDENTICAL shape, mirroring MA1's own durable-trace
    # indistinguishability guarantee one layer up.
    wr.reset_roaming_state()
    fake_ordinary = sequenced_ask_llama_for_json([
        {"roaming_act": "act", "wait_minutes": 5},
        {"resource_class": "library"},  # structurally malformed Stage-2
    ])
    state_a = wr.get_roaming_state()
    state_a["authorized"] = True
    wr.start_roaming_worker(ask_llama_for_json=fake_ordinary, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5_symmetry_ordinary"), session_id="sess-p5-sym-a",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_sym_a.jsonl"))
    assert wait_until_stopped()
    ordinary_snapshot = (
        wr.get_roaming_state()["background_activity_state"],
        wr.get_roaming_state()["consecutive_technical_failures"],
    )

    wr.reset_roaming_state()
    private_paths = fresh_private_paths("p5_symmetry_private")
    fake_private = sequenced_ask_llama_for_json([
        {"roaming_act": "private_act", "wait_minutes": 5},
        {"action": "read"},  # structurally malformed Stage-2
    ])
    state_b = wr.get_roaming_state()
    state_b["authorized"] = True
    wr.start_roaming_worker(ask_llama_for_json=fake_private, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5_symmetry_private_ws"), session_id="sess-p5-sym-b",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_sym_b.jsonl"), private_paths=private_paths)
    assert wait_until_stopped()
    private_snapshot = (
        wr.get_roaming_state()["background_activity_state"],
        wr.get_roaming_state()["consecutive_technical_failures"],
    )
    assert ordinary_snapshot == private_snapshot == (wr.BACKGROUND_STATE_BACKOFF, 1)


def test_p5_wait_sets_waiting_then_active_and_stays_available():
    # Section 21 WAIT: a wait is a nonfailure -- never treated as a
    # technical failure; background_activity_state cycles WAITING ->
    # ACTIVE as the interval elapses, never BACKOFF/PAUSED.
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.2  # wait_minutes=5 -> 1.0s
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "wait", "wait_minutes": 5},
        {"roaming_act": "stop", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5_wait_available"), session_id="sess-p5-wait",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_wait.jsonl"))
    time.sleep(0.1)
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_WAITING
    assert wait_until_stopped(timeout=3.0)
    assert wr.get_roaming_state()["consecutive_technical_failures"] == 0
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_CLARK


def test_p5_wait_for_human_enters_waiting_state():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "wait_for_human", "wait_minutes": 5}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5_wfh"), session_id="sess-p5-wfh",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_wfh.jsonl"))
    assert wait_until_stopped()
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_WAITING_FOR_HUMAN
    assert wr.is_background_activity_auto_start_eligible() is False


def test_p5_wait_for_human_timer_alone_never_releases_it():
    # Section 10/21: only release_wait_for_human_and_maybe_resume()
    # clears WAITING_FOR_HUMAN -- passage of time alone, with nobody
    # calling it, never does (no presence detection, no timer).
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_WAITING_FOR_HUMAN
    time.sleep(0.05)
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_WAITING_FOR_HUMAN
    assert wr.is_background_activity_auto_start_eligible() is False


def test_p5_release_wait_for_human_allows_new_episode():
    # Section 10/21: the canonical trigger is an explicit call (in
    # production, made by llama_gui.py right after a successful
    # CONVERSATION_MODE waking turn) -- never a toggle, never a timer.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_WAITING_FOR_HUMAN
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    released = wr.release_wait_for_human_and_maybe_resume(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
        workspace_paths=fresh_paths("p5_wfh_release"), session_id="sess-p5-wfh-release",
        trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_wfh_release.jsonl"),
    )
    assert released is True
    assert wait_until_stopped()


def test_p5_release_wait_for_human_is_no_op_when_not_waiting():
    wr.reset_roaming_state()
    released = wr.release_wait_for_human_and_maybe_resume(
        ask_llama_for_json=lambda *a, **k: "{}", clark_actor_id=CLARK_ACTOR_ID,
        workspace_paths=fresh_paths("p5_wfh_noop"), session_id="sess-p5-wfh-noop",
    )
    assert released is False
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_IDLE


def test_p5_clark_stop_distinguishable_and_never_auto_resumes():
    # Section 11/21 STOP: preserves Clark's genuine choice -- PAUSED_
    # BY_CLARK is a distinct constant from PAUSED_BY_HUMAN/FAULT_PAUSED,
    # and unlike BACKOFF, no amount of elapsed time makes it eligible
    # again on its own.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5_clark_stop"), session_id="sess-p5-clark-stop",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_clark_stop.jsonl"))
    assert wait_until_stopped()
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    assert wr.BACKGROUND_STATE_PAUSED_BY_CLARK != wr.BACKGROUND_STATE_PAUSED_BY_HUMAN
    assert wr.BACKGROUND_STATE_PAUSED_BY_CLARK != wr.BACKGROUND_STATE_FAULT_PAUSED
    assert wr.is_background_activity_auto_start_eligible() is False
    time.sleep(0.05)
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    assert wr.is_background_activity_auto_start_eligible() is False


def test_p5_human_pause_stops_worker_and_marks_paused_by_human():
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.05  # wait_minutes=60 -> 3s, ample pause window
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "wait", "wait_minutes": 60}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5_human_pause"), session_id="sess-p5-human-pause",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_human_pause.jsonl"))
    time.sleep(0.1)
    wr.pause_background_activity_by_human()
    assert wr.get_roaming_state()["authorized"] is False
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_HUMAN
    assert wait_until_stopped()


def test_p5_human_resume_permits_new_episode():
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_PAUSED_BY_HUMAN
    wr.resume_background_activity_by_human()
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_IDLE
    assert wr.is_background_activity_auto_start_eligible() is True


def test_p5_human_resume_also_clears_fault_paused():
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_FAULT_PAUSED
    state["consecutive_technical_failures"] = 3
    wr.resume_background_activity_by_human()
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_IDLE
    assert wr.is_background_activity_auto_start_eligible() is True


def test_p5_human_resume_is_no_op_when_not_paused():
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_WAITING_FOR_HUMAN
    wr.resume_background_activity_by_human()
    # WAITING_FOR_HUMAN is a distinct lifecycle, not a human pause --
    # resume_background_activity_by_human() must not conflate them.
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_WAITING_FOR_HUMAN


def test_p5_conversation_direction_ownership_untouched_source_audit():
    # Section 22F/1: this module still has zero CAPABILITY to touch
    # conversation direction ownership -- proven the same way test_
    # no_rem_sleep_import_or_reference proves the module has no
    # capability to reach Sleep/REM: an AST import-list check, not a
    # raw substring search (this module's own docstrings legitimately
    # name conversation_direction.py/apply_human_direction_control() as
    # design comparisons, e.g. line ~43/507, without importing either).
    with open(os.path.join(ANAXI_FINAL, "workspace_roaming.py"), encoding="utf-8") as f:
        source = f.read()
    import ast
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    assert "conversation_direction" not in imported_names
    assert "direction_control" not in imported_names


def test_p5_new_episode_after_backoff_still_uses_p4_shared_continuity_mechanism():
    # Section 17/23: a new episode begun via maybe_auto_start_
    # background_activity() after a simulated backoff still goes
    # through the SAME start_roaming_worker() -> Bridge A -> canonical
    # episode-start path as any other episode -- P4/P4-P1 are frozen,
    # not bypassed by this gate's new entrypoint.
    data_dir, pipeline_key = fresh_provenance_dir("p5_post_backoff_continuity")
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_BACKOFF
    state["backoff_until"] = time.time() - 1
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    started = wr.maybe_auto_start_background_activity(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("p5_post_backoff_cont_ws"),
        session_id="sess-p5-post-backoff-cont", trace_path=os.path.join(TEST_ROOT, "wr_trace_p5_post_backoff_cont.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
    )
    assert started is True
    assert wait_until_stopped()
    pending = wep.find_pending_episode_run_ids(data_dir)
    assert len(pending) == 1  # same canonical episode-start bookkeeping as an ordinary run


def test_p5_ma2_schemas_unchanged_still_wired():
    # Section 16: MA2 remains frozen -- the strict Stage-2 schemas are
    # unaffected by this gate's lifecycle wrapper, which never itself
    # calls ask_llama_for_json.
    assert wd.STAGE2_ACTION_SCHEMA["additionalProperties"] is False
    assert wpriv.STAGE2_PRIVATE_ACTION_SCHEMA["additionalProperties"] is False


# ==================================== WSP2-P5-P1: Clark-owned pause/self-resume


def test_p5p1_clark_stop_records_durable_pause_when_data_dir_given():
    # Section 19A: a valid Clark stop, inside a canonical-episode run,
    # creates durable Clark-owned pause truth -- not merely a process-
    # local flag.
    data_dir, pipeline_key = fresh_provenance_dir("p5p1_stop_durable")
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5p1_stop_durable_ws"), session_id="sess-p5p1-stop",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5p1_stop.jsonl"),
                             data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[])
    assert wait_until_stopped()
    assert wep.find_latest_background_lifecycle_control(data_dir) == wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK


def test_p5p1_stop_without_data_dir_no_durable_write_no_crash():
    # Section 8/19: `data_dir=None` (no canonical wiring for this run,
    # e.g. an older/simpler caller) must not crash the stop branch --
    # it simply skips the durable write, exactly like every other
    # data_dir-gated canonical write in this loop.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5p1_stop_no_data_dir"), session_id="sess-p5p1-stop-none",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5p1_stop_none.jsonl"))
    assert wait_until_stopped()
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_CLARK


def test_p5p1_process_local_state_is_reset_but_durable_truth_survives():
    # Section 19B: reset_roaming_state() (the process-local reset every
    # fresh launch performs implicitly by starting from empty_roaming_
    # state()) discards the in-memory PAUSED_BY_CLARK -- but the
    # DURABLE record, being canonical/on-disk, is unaffected by it.
    data_dir, pipeline_key = fresh_provenance_dir("p5p1_reset_survives")
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5p1_reset_survives_ws"), session_id="sess-p5p1-reset",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5p1_reset.jsonl"),
                             data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[])
    assert wait_until_stopped()
    wr.reset_roaming_state()  # simulates the in-memory wipe of a fresh process
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_IDLE  # process memory forgot
    assert wep.find_latest_background_lifecycle_control(data_dir) == wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK  # canon didn't


def test_p5p1_fresh_launch_reconstructs_paused_by_clark():
    # Section 5/19C/19D: the required precedence -- reconstruct BEFORE
    # auto-start eligibility is ever evaluated.
    data_dir, pipeline_key = fresh_provenance_dir("p5p1_reconstruct")
    wep.record_background_lifecycle_control(
        data_dir, actor_id=CLARK_ACTOR_ID, pipeline_key=pipeline_key, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK,
    )
    wr.reset_roaming_state()
    assert wr.is_background_activity_auto_start_eligible() is True  # BEFORE reconstruction, wrongly eligible
    authority = wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir)
    assert authority == wr.CLARK_PAUSE_AUTHORITY_KNOWN_PAUSED
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    assert wr.is_background_activity_auto_start_eligible() is False  # AFTER reconstruction, correctly ineligible


def test_p5p1_fresh_launch_no_reconstruction_when_no_prior_control_event():
    data_dir, pipeline_key = fresh_provenance_dir("p5p1_no_reconstruct")
    wr.reset_roaming_state()
    authority = wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir)
    assert authority == wr.CLARK_PAUSE_AUTHORITY_KNOWN_NOT_PAUSED
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_IDLE
    assert wr.is_background_activity_auto_start_eligible() is True  # ordinary P5 default applies


def test_p5p1_fresh_launch_no_reconstruction_when_latest_control_is_resumed():
    # Section 5/14: a later resume cancels the earlier pause for
    # reconstruction purposes.
    data_dir, pipeline_key = fresh_provenance_dir("p5p1_reconstruct_resumed")
    wep.record_background_lifecycle_control(
        data_dir, actor_id=CLARK_ACTOR_ID, pipeline_key=pipeline_key, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK,
    )
    wep.record_background_lifecycle_control(
        data_dir, actor_id=CLARK_ACTOR_ID, pipeline_key=pipeline_key, occurred_at=2000,
        control=wep.BACKGROUND_CONTROL_RESUMED,
    )
    wr.reset_roaming_state()
    authority = wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir)
    assert authority == wr.CLARK_PAUSE_AUTHORITY_KNOWN_NOT_PAUSED
    assert wr.is_background_activity_auto_start_eligible() is True


def test_p5p2a_missing_provenance_directory_is_authority_unknown():
    # WSP2-P5-P2a (spec sections 1B/1D/6/18): a missing DB is a genuine
    # READ FAILURE, never collapsed with "known empty" -- an
    # established installation's real canonical history must not be
    # silently invisible merely because the expected store cannot be
    # found (e.g. wrong working directory, deleted/moved file).
    # Superseded WSP2-P5-P2's own test_p5p1_missing_provenance_
    # directory_is_known_not_paused_not_unknown, which asserted the
    # OPPOSITE (missing == known-not-paused) -- that was exactly the
    # narrow defect this gate repairs; a hypothetical genuinely virgin
    # installation failing closed here is accepted (spec section 21:
    # explicit bootstrap/first-run semantics remain future work).
    wr.reset_roaming_state()
    authority = wr.reconstruct_clark_owned_pause_from_canonical_truth(os.path.join(TEST_ROOT, "no_such_db_dir_p5p1"))
    assert authority == wr.CLARK_PAUSE_AUTHORITY_UNKNOWN
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN
    assert wr.is_background_activity_auto_start_eligible() is False


def test_p5p1_pass1_can_select_resume_own_pause():
    # Section 20A: waking typed control can select resume_own_pause.
    import conversation_direction as cd
    validated, failure = cd.validate_pass1_conversation_act({
        "act": cd.DEVELOP_CURRENT, "thread": "", "direction_request": cd.REQUEST_NONE,
        "relinquish_direction": False, "background_activity_request": cd.BACKGROUND_ACTIVITY_REQUEST_RESUME_OWN_PAUSE,
    })
    assert failure is None
    assert validated["background_activity_request"] == cd.BACKGROUND_ACTIVITY_REQUEST_RESUME_OWN_PAUSE


def test_p5p1_apply_self_resume_takes_no_free_text_no_authorization():
    # Section 20B/G: structural proof that ordinary prose/free text has
    # no code path to self-resume, and no human authorization is
    # required for Clark's own choice -- the function reads only the
    # existing process-local lifecycle state, nothing else.
    import inspect
    sig = inspect.signature(wr.apply_clark_self_resume_if_valid)
    assert len(sig.parameters) == 0


def test_p5p1_self_resume_clears_paused_by_clark():
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    cleared = wr.apply_clark_self_resume_if_valid()
    assert cleared is True
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_IDLE


def test_p5p1_self_resume_makes_new_episode_eligible_via_existing_path():
    # Section 20D/E: eligibility flips true, and the SAME existing
    # start_roaming_worker() path (via maybe_auto_start_background_
    # activity()) can then begin a genuinely NEW episode.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    assert wr.apply_clark_self_resume_if_valid() is True
    assert wr.is_background_activity_auto_start_eligible() is True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    started = wr.maybe_auto_start_background_activity(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
        workspace_paths=fresh_paths("p5p1_self_resume_new_episode"), session_id="sess-p5p1-new-ep",
    )
    assert started is True
    assert wait_until_stopped()


def test_p5p1_self_resume_cannot_clear_paused_by_human():
    # Section 8/10/21A: PAUSED_BY_HUMAN remains a human-owned authority
    # Clark's own resume_own_pause cannot reach.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_PAUSED_BY_HUMAN
    cleared = wr.apply_clark_self_resume_if_valid()
    assert cleared is False
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_HUMAN


def test_p5p1_self_resume_cannot_clear_fault_paused():
    # Section 8/10/21B: FAULT_PAUSED remains a host-owned authority.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_FAULT_PAUSED
    cleared = wr.apply_clark_self_resume_if_valid()
    assert cleared is False
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_FAULT_PAUSED


def test_p5p1_self_resume_idempotent_second_call_is_noop():
    # Section 22D: replay/restart calling this twice never applies a
    # harmful second resume -- the second call finds nothing to clear.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    assert wr.apply_clark_self_resume_if_valid() is True
    assert wr.apply_clark_self_resume_if_valid() is False  # already IDLE -- nothing left to clear


def test_p5p1_self_resume_no_op_when_already_active_idle_waiting_backoff():
    # Section 8: "If already active/idle/waiting/backoff: choose the
    # narrowest deterministic no-harm behavior" -- idempotent no-op was
    # the chosen option (see apply_clark_self_resume_if_valid()'s own
    # docstring), proven across every other lifecycle state.
    for other_state in (
        wr.BACKGROUND_STATE_IDLE, wr.BACKGROUND_STATE_ACTIVE, wr.BACKGROUND_STATE_WAITING,
        wr.BACKGROUND_STATE_BACKOFF, wr.BACKGROUND_STATE_WAITING_FOR_HUMAN,
    ):
        wr.reset_roaming_state()
        state = wr.get_roaming_state()
        state["background_activity_state"] = other_state
        assert wr.apply_clark_self_resume_if_valid() is False
        assert wr.get_roaming_state()["background_activity_state"] == other_state


def test_p5p1_human_resume_still_clears_paused_by_human_and_fault_paused():
    # Section 21C: human Resume remains fully functional for its
    # original two states, unaffected by this gate's PAUSED_BY_CLARK
    # addition.
    for prior_state in (wr.BACKGROUND_STATE_PAUSED_BY_HUMAN, wr.BACKGROUND_STATE_FAULT_PAUSED):
        wr.reset_roaming_state()
        state = wr.get_roaming_state()
        state["background_activity_state"] = prior_state
        wr.resume_background_activity_by_human()
        assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_IDLE


def test_p5p1_human_resume_also_clears_paused_by_clark():
    # Section 9: human Resume remains ABLE to clear a Clark-owned pause
    # -- no longer the sole way, but still a valid way.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    wr.resume_background_activity_by_human()
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_IDLE


def test_p5p1_human_resume_reconciles_durable_truth_so_restart_cannot_resurrect_it():
    # Section 9/21D: llama_gui.py's resume_background_activity() writes
    # a durable 'resumed' reconciliation event when it clears a live
    # PAUSED_BY_CLARK state (see that function's own source) -- proven
    # here at the mechanism level: after that write, reconstruction on
    # a simulated restart correctly finds no unresolved pause.
    data_dir, pipeline_key = fresh_provenance_dir("p5p1_human_reconcile", seed_human=True)
    human_actor_id = HUMAN_ACTOR_ID
    wep.record_background_lifecycle_control(
        data_dir, actor_id=CLARK_ACTOR_ID, pipeline_key=pipeline_key, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK,
    )
    wr.reset_roaming_state()
    wr.get_roaming_state()["background_activity_state"] = wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    wr.resume_background_activity_by_human()  # the live clear
    wep.record_background_lifecycle_control(  # the durable reconciliation llama_gui.py also performs
        data_dir, actor_id=human_actor_id, pipeline_key=pipeline_key, occurred_at=2000,
        control=wep.BACKGROUND_CONTROL_RESUMED,
    )
    wr.reset_roaming_state()  # simulate a restart
    authority = wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir)
    assert authority == wr.CLARK_PAUSE_AUTHORITY_KNOWN_NOT_PAUSED  # restart does NOT resurrect the stale pause
    assert wr.is_background_activity_auto_start_eligible() is True


def test_p5p1_durable_write_site_strictly_after_persistence_in_source():
    # Section 11/22B: structural proof that llama_anaxi.py's own
    # durable background-lifecycle-control write is placed AFTER the
    # canonical persistence call succeeds -- a Pass-2 failure (which
    # raises before ever reaching that call) can never accidentally
    # resume background activity, since the write is textually and
    # controlflow-strictly downstream of it.
    with open(os.path.join(ANAXI_FINAL, "llama_anaxi.py"), encoding="utf-8") as f:
        source = f.read()
    persistence_index = source.index("native_provenance_writer.stage_and_record_native_waking_turn")
    write_index = source.index("workspace_episode_provenance.record_background_lifecycle_control")
    assert write_index > persistence_index


def test_p5p1_new_episode_after_self_resume_gets_a_fresh_run_id_never_reused():
    # Section 20F: a later episode is a new decision opportunity, never
    # a retry/reopening of the stopped one -- proven the same way P5's
    # own post-backoff test proves it: start_roaming_worker() mints a
    # fresh run_id every time it is called (generate_episode_run_id()
    # is called fresh inside it), never reusing a prior run's id.
    data_dir, pipeline_key = fresh_provenance_dir("p5p1_fresh_run_id")
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["authorized"] = True
    fake1 = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    wr.start_roaming_worker(ask_llama_for_json=fake1, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5p1_fresh_run_id_ws1"), session_id="sess-p5p1-fresh-1",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5p1_fresh1.jsonl"),
                             data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[])
    assert wait_until_stopped()
    first_run_id = wr.get_roaming_state().get("run_id")

    assert wr.apply_clark_self_resume_if_valid() is True
    fake2 = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    started = wr.maybe_auto_start_background_activity(
        ask_llama_for_json=fake2, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("p5p1_fresh_run_id_ws2"),
        session_id="sess-p5p1-fresh-2", trace_path=os.path.join(TEST_ROOT, "wr_trace_p5p1_fresh2.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
    )
    assert started is True
    assert wait_until_stopped()
    second_run_id = wr.get_roaming_state().get("run_id")
    assert first_run_id is not None and second_run_id is not None
    assert first_run_id != second_run_id


def test_p5p1_full_restart_sequence():
    # Section 23: the exact 12-step required sequence, with NO human
    # Resume action anywhere in it.
    data_dir, pipeline_key = fresh_provenance_dir("p5p1_full_sequence")
    # 1. normal background activity active (simulated directly).
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_ACTIVE
    # 2/3. Clark validly selects stop -> state becomes PAUSED_BY_CLARK.
    state["authorized"] = True
    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID,
                             workspace_paths=fresh_paths("p5p1_full_seq_ws1"), session_id="sess-p5p1-seq",
                             trace_path=os.path.join(TEST_ROOT, "wr_trace_p5p1_seq.jsonl"),
                             data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[])
    assert wait_until_stopped()
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    # 4/5. application process ends / fresh process starts (simulated).
    wr.reset_roaming_state()
    # 6. durable state reconstructs PAUSED_BY_CLARK.
    assert wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir) == wr.CLARK_PAUSE_AUTHORITY_KNOWN_PAUSED
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    # 7. no background worker auto-starts.
    assert wr.is_background_activity_auto_start_eligible() is False
    assert wr.is_worker_running() is False
    # 8/9. Clark has ordinary waking conversation; typed resume_own_pause
    # selected successfully (the waking-turn/typed-control layer itself
    # is exercised in test_conversation_direction.py/test_p5p1_pass1_
    # can_select_resume_own_pause -- here, the HOST-SIDE application
    # this waking turn would trigger is exercised directly).
    assert wr.apply_clark_self_resume_if_valid() is True
    # 10. durable pause clears (a real waking turn would durably record
    # BACKGROUND_CONTROL_RESUMED at this point; simulated directly here
    # since no live model call is permitted in this gate). occurred_at
    # must be a real, later wall-clock value -- step 2/3's own durable
    # write above (made by the actual roaming loop) used a real
    # datetime.now() timestamp, not a small synthetic one.
    wep.record_background_lifecycle_control(
        data_dir, actor_id=CLARK_ACTOR_ID, pipeline_key=pipeline_key, occurred_at=int(time.time()) + 60,
        control=wep.BACKGROUND_CONTROL_RESUMED,
    )
    # 11. background activity becomes eligible.
    assert wr.is_background_activity_auto_start_eligible() is True
    # 12. new episode may auto-start.
    fake2 = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    started = wr.maybe_auto_start_background_activity(
        ask_llama_for_json=fake2, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("p5p1_full_seq_ws2"),
        session_id="sess-p5p1-seq2", trace_path=os.path.join(TEST_ROOT, "wr_trace_p5p1_seq2.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, dialogue_pairs=[],
    )
    assert started is True
    assert wait_until_stopped()
    # Confirms a genuinely NEW restart cannot resurrect the resolved pause.
    wr.reset_roaming_state()
    assert wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir) == wr.CLARK_PAUSE_AUTHORITY_KNOWN_NOT_PAUSED


# ================================== WSP2-P5-P2: lifecycle-authority fail-closed


def _write_corrupt_db(name):
    # A genuinely UNREADABLE canonical store -- a PRESENT, unparseable
    # file, distinct from a missing one (both now correctly resolve to
    # AUTHORITY_UNKNOWN as of WSP2-P5-P2a -- see test_p5p2a_missing_
    # provenance_directory_is_authority_unknown below -- but are kept
    # source-distinguishable via ProvenanceStoreMissingError vs. a
    # plain sqlite3 exception, per that gate's own section 18).
    # sqlite3.connect() itself succeeds lazily on any path; the failure
    # surfaces only once a real query is attempted, exactly matching
    # what disk corruption/a truncated write looks like from this
    # module's own perspective.
    data_dir = os.path.join(TEST_ROOT, name)
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "anaxi_provenance.db"), "w", encoding="utf-8") as f:
        f.write("this is not a sqlite database file")
    return data_dir


def test_p5p2_determine_authority_known_paused():
    # Section 16A.
    data_dir, pipeline_key = fresh_provenance_dir("p5p2_authority_paused")
    wep.record_background_lifecycle_control(
        data_dir, actor_id=CLARK_ACTOR_ID, pipeline_key=pipeline_key, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK,
    )
    assert wr.determine_clark_pause_authority(data_dir) == wr.CLARK_PAUSE_AUTHORITY_KNOWN_PAUSED


def test_p5p2_determine_authority_known_resumed():
    # Section 16B.
    data_dir, pipeline_key = fresh_provenance_dir("p5p2_authority_resumed")
    wep.record_background_lifecycle_control(
        data_dir, actor_id=CLARK_ACTOR_ID, pipeline_key=pipeline_key, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_RESUMED,
    )
    assert wr.determine_clark_pause_authority(data_dir) == wr.CLARK_PAUSE_AUTHORITY_KNOWN_NOT_PAUSED


def test_p5p2_determine_authority_empty_ledger_known_not_paused():
    # Section 13/16C: a readable DB with zero lifecycle-control rows is
    # a KNOWN state, never AUTHORITY_UNKNOWN -- preserves normal P5
    # first-use/default behavior.
    data_dir, pipeline_key = fresh_provenance_dir("p5p2_authority_empty")
    assert wr.determine_clark_pause_authority(data_dir) == wr.CLARK_PAUSE_AUTHORITY_KNOWN_NOT_PAUSED


def test_p5p2_determine_authority_corrupt_db_is_unknown():
    # Section 16D: a present but unreadable/corrupt DB is a genuine
    # READ FAILURE, never collapsed with "no record" (spec section 3).
    data_dir = _write_corrupt_db("p5p2_corrupt_db")
    assert wr.determine_clark_pause_authority(data_dir) == wr.CLARK_PAUSE_AUTHORITY_UNKNOWN


def test_p5p2_determine_authority_query_exception_is_unknown():
    # Section 16E: any exception during the underlying lookup (not
    # just corruption) is fail-closed the same way -- proven directly
    # against a monkeypatched find_latest_background_lifecycle_control
    # that raises, so this test does not depend on any particular
    # on-disk corruption mechanism to exercise the exception path.
    real_find = wep.find_latest_background_lifecycle_control

    def _poison(data_dir):
        raise RuntimeError("simulated connection/query failure")

    wep.find_latest_background_lifecycle_control = _poison
    try:
        assert wr.determine_clark_pause_authority("irrelevant") == wr.CLARK_PAUSE_AUTHORITY_UNKNOWN
    finally:
        wep.find_latest_background_lifecycle_control = real_find


def test_p5p2_determine_authority_unrecognized_value_is_unknown():
    # Section 16G/3: a malformed/unrecognized stored control value
    # fails closed rather than being silently treated as "not paused"
    # -- this module's own write path can never produce one, but this
    # function does not trust that invariant blindly.
    real_find = wep.find_latest_background_lifecycle_control

    def _garbled(data_dir):
        return "some_unexpected_future_value"

    wep.find_latest_background_lifecycle_control = _garbled
    try:
        assert wr.determine_clark_pause_authority("irrelevant") == wr.CLARK_PAUSE_AUTHORITY_UNKNOWN
    finally:
        wep.find_latest_background_lifecycle_control = real_find


def test_p5p2_reconstruction_sets_lifecycle_uncertain_on_corrupt_db():
    # Section 4/16F: no exception escapes -- reconstruction completes
    # cleanly and lands in the new, distinct, honestly-labeled state.
    data_dir = _write_corrupt_db("p5p2_reconstruct_corrupt")
    wr.reset_roaming_state()
    authority = wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir)
    assert authority == wr.CLARK_PAUSE_AUTHORITY_UNKNOWN
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN
    # Never fabricates a Clark choice that never happened (spec section 6).
    assert wr.get_roaming_state()["background_activity_state"] != wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    assert wr.get_roaming_state()["background_activity_state"] != wr.BACKGROUND_STATE_FAULT_PAUSED


def test_p5p2_lifecycle_uncertain_blocks_auto_start_eligibility():
    # Section 5/17C.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN
    assert wr.is_background_activity_auto_start_eligible() is False


def test_p5p2_auto_start_no_worker_no_episode_no_model_call_under_uncertainty():
    # Section 17C/D/E/F: the full negative proof -- no worker thread,
    # no episode_started persisted, and (structurally, since maybe_
    # auto_start_background_activity() returns before ever reaching
    # start_roaming_worker()) no model call path reached at all. A
    # poison ask_llama_for_json proves the last point directly: if it
    # were ever invoked, this test would raise. Uses a genuinely valid,
    # empty, readable DB (not a corrupted file) so the follow-up
    # find_pending_episode_run_ids() check below can itself cleanly
    # confirm "nothing was written" -- the uncertainty itself is
    # injected via a monkeypatched lookup, exactly like test_p5p2_
    # determine_authority_query_exception_is_unknown does.
    data_dir, pipeline_key = fresh_provenance_dir("p5p2_no_autostart")
    wr.reset_roaming_state()
    real_find = wep.find_latest_background_lifecycle_control

    def _poison_find(dd):
        raise RuntimeError("simulated read failure")

    wep.find_latest_background_lifecycle_control = _poison_find
    try:
        wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir)
    finally:
        wep.find_latest_background_lifecycle_control = real_find
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN

    def _poison_ask(*a, **k):
        raise AssertionError("ask_llama_for_json must never be called under LIFECYCLE_UNCERTAIN")

    started = wr.maybe_auto_start_background_activity(
        ask_llama_for_json=_poison_ask, clark_actor_id=CLARK_ACTOR_ID,
        workspace_paths=fresh_paths("p5p2_no_autostart_ws"), session_id="sess-p5p2-no-autostart",
        data_dir=data_dir,
    )
    assert started is False
    assert wr.is_worker_running() is False
    pending = wep.find_pending_episode_run_ids(data_dir)
    assert pending == []  # no episode_started was ever written


def test_p5p2_known_not_paused_auto_start_eligibility_unchanged():
    # Section 17A: ordinary P5 behavior for the KNOWN_NOT_PAUSED_BY_
    # CLARK result is exactly the pre-P5-P2 default -- unaffected by
    # this gate's addition.
    wr.reset_roaming_state()
    assert wr.is_background_activity_auto_start_eligible() is True


def test_p5p2_ordinary_conversation_direction_unaffected_by_uncertainty():
    # Section 18: AUTHORITY_UNKNOWN/LIFECYCLE_UNCERTAIN is a workspace_
    # roaming-only concept -- conversation_direction.py has no
    # capability to even observe it (proven the same way test_p5_
    # conversation_direction_ownership_untouched_source_audit proves
    # the absence of the reverse coupling): direction_owner resolution
    # does not read background_activity_state at all.
    import conversation_direction as cd
    ws = cd.empty_working_set()
    ws["direction_owner"] = cd.DIRECTION_CLARK
    resolution = cd.resolve_clark_direction_control(cd.DIRECTION_CLARK, cd.REQUEST_NONE, False)
    assert resolution["direction_owner_after"] == cd.DIRECTION_CLARK  # entirely unaffected


def test_p5p2_clark_self_resume_no_op_under_lifecycle_uncertain():
    # Section 9/19A: resume_own_pause must not clear AUTHORITY_UNKNOWN
    # merely because the typed field was selected -- reuses the
    # existing P5-P1 idempotent/no-op guard (LIFECYCLE_UNCERTAIN !=
    # PAUSED_BY_CLARK), no new logic needed.
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN
    cleared = wr.apply_clark_self_resume_if_valid()
    assert cleared is False
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN


def test_p5p2_reconstruction_reread_clears_uncertain_once_db_becomes_readable():
    # Section 7/8: the narrow, already-natural re-establishment path --
    # a later call (here, simulating either a fresh relaunch or human
    # Resume's own one-shot re-read) with the SAME data_dir now healthy
    # correctly clears LIFECYCLE_UNCERTAIN back to ordinary
    # availability, with no polling/retry infrastructure involved.
    data_dir, pipeline_key = fresh_provenance_dir("p5p2_rereconstruct")
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN
    authority = wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir)
    assert authority == wr.CLARK_PAUSE_AUTHORITY_KNOWN_NOT_PAUSED
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_IDLE
    assert wr.is_background_activity_auto_start_eligible() is True


def test_p5p2_reconstruction_reread_finds_real_pause_once_db_becomes_readable():
    data_dir, pipeline_key = fresh_provenance_dir("p5p2_rereconstruct_paused")
    wep.record_background_lifecycle_control(
        data_dir, actor_id=CLARK_ACTOR_ID, pipeline_key=pipeline_key, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK,
    )
    wr.reset_roaming_state()
    state = wr.get_roaming_state()
    state["background_activity_state"] = wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN
    authority = wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir)
    assert authority == wr.CLARK_PAUSE_AUTHORITY_KNOWN_PAUSED
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    assert wr.is_background_activity_auto_start_eligible() is False


def test_p5p2_full_restart_sequence_with_temporary_unreadability():
    # Section 20: the exact 9-step required sequence.
    data_dir, pipeline_key = fresh_provenance_dir("p5p2_full_sequence")
    # 1. canonical pause exists.
    wep.record_background_lifecycle_control(
        data_dir, actor_id=CLARK_ACTOR_ID, pipeline_key=pipeline_key, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK,
    )
    # 2. process exits (simulated).
    wr.reset_roaming_state()
    # 3. canonical DB is temporarily unreadable on next launch
    # (simulated via a monkeypatched lookup rather than actually
    # corrupting the real seeded DB, so step 8/9 below can cleanly
    # observe the SAME real data once "healthy" again).
    real_find = wep.find_latest_background_lifecycle_control

    def _temporarily_unreadable(dd):
        raise RuntimeError("simulated temporary read failure")

    wep.find_latest_background_lifecycle_control = _temporarily_unreadable
    try:
        # 4. reconstruction = AUTHORITY_UNKNOWN.
        authority = wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir)
        assert authority == wr.CLARK_PAUSE_AUTHORITY_UNKNOWN
    finally:
        wep.find_latest_background_lifecycle_control = real_find
    # 5. background does NOT auto-start.
    assert wr.is_background_activity_auto_start_eligible() is False
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_LIFECYCLE_UNCERTAIN
    # 6. ordinary waking remains available (workspace_roaming state is
    # entirely independent of conversation_direction's own state --
    # see test_p5p2_ordinary_conversation_direction_unaffected_by_
    # uncertainty for the direct proof; nothing here could disable it).
    # 7. no fake resumed/paused event is written.
    assert wep.find_latest_background_lifecycle_control(data_dir) == wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK
    # 8/9. later simulated healthy reconstruction sees the original
    # paused_by_clark, restored correctly.
    authority2 = wr.reconstruct_clark_owned_pause_from_canonical_truth(data_dir)
    assert authority2 == wr.CLARK_PAUSE_AUTHORITY_KNOWN_PAUSED
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_CLARK


# ============================== WSP2-P5-P2a: CWD-independent canonical location


def test_p5p2a_cwd_misdirection_does_not_affect_reconstruction_result():
    # Section 17: (1) canonical DB exists at an "anchored application
    # location" (simulated by a fresh, seeded provenance dir); (2)
    # process CWD is changed to an unrelated temp directory; (3)
    # reconstruction is given the correct anchored data_dir explicitly
    # -- exactly as llama_gui.py's own launch_production_backend()
    # always passes PROVENANCE_DB_DIR, never os.getcwd(); (4) the
    # anchored real DB location is still selected -- the true history
    # is found regardless of the process's actual CWD at call time;
    # (5) the unrelated CWD never receives its own anaxi_provenance.db
    # -- nothing in this call path writes relative to os.getcwd(); (6)
    # the authority result comes from the anchored store, not CWD.
    anchored_dir, pipeline_key = fresh_provenance_dir("p5p2a_anchored_location")
    wep.record_background_lifecycle_control(
        anchored_dir, actor_id=CLARK_ACTOR_ID, pipeline_key=pipeline_key, occurred_at=1000,
        control=wep.BACKGROUND_CONTROL_PAUSED_BY_CLARK,
    )
    unrelated_cwd = tempfile.mkdtemp(prefix="p5p2a_unrelated_cwd_")
    original_cwd = os.getcwd()
    wr.reset_roaming_state()
    try:
        os.chdir(unrelated_cwd)
        authority = wr.reconstruct_clark_owned_pause_from_canonical_truth(anchored_dir)
    finally:
        os.chdir(original_cwd)
    assert authority == wr.CLARK_PAUSE_AUTHORITY_KNOWN_PAUSED
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_PAUSED_BY_CLARK
    assert not os.path.exists(os.path.join(unrelated_cwd, "anaxi_provenance.db"))


def test_p5p2a_reconstruction_never_creates_the_missing_db():
    # Section 7/16J: lifecycle reconstruction MUST NOT manufacture a
    # provenance DB merely because one is missing.
    missing_dir = os.path.join(TEST_ROOT, "p5p2a_never_created")
    assert not os.path.exists(missing_dir)
    wr.reset_roaming_state()
    authority = wr.reconstruct_clark_owned_pause_from_canonical_truth(missing_dir)
    assert authority == wr.CLARK_PAUSE_AUTHORITY_UNKNOWN
    assert not os.path.exists(missing_dir)  # not even the directory was created
    assert not os.path.exists(os.path.join(missing_dir, "anaxi_provenance.db"))


# ============================= OWC9-P1: roaming Stage-1 context budget ===
# Reuses context_budget.py's central authority unchanged (spec section 3).
# See _compose_roaming_stage1_budget()/_roaming_stage1_messages_from_
# composition()/_roaming_context_budget_diagnostics() in workspace_roaming.py.


def _big_recent_public(n, size=2000):
    return [{"kind": "library_read", "preview": "x" * size} for _ in range(n)]


def test_owc9_p1_no_pressure_all_kinds_survive_untouched():
    # Model-free. Typical, modest content: nothing should be trimmed or
    # dropped, and the composition must fit comfortably within budget
    # (spec section 13's "prove soft context is mechanically trimmed"
    # implies the converse must also hold -- no pressure, no trimming).
    state = wr.empty_roaming_state()
    state["authorized"] = True
    state["recent_public_decisions"] = [{"kind": "library_read", "preview": "short preview"}] * 3
    state["departure_handoff"] = {"note": "a short note", "excerpts": [{"author": "book.txt", "text": "a short excerpt"}]}
    state["last_workspace_observation"] = {
        "resource_class": "library", "action": "read", "relative_path": "book.txt",
        "status": "ok", "action_id": "abc", "bounded_result": "a short bounded result",
    }
    live_units = [{"event_id": f"ev{i}", "prompt": "short human message", "clark_text": "short clark reply"} for i in range(4)]

    result = wr._compose_roaming_stage1_budget(state, live_units)
    assert result.fits
    assert result.dropped_kinds == []
    assert result.trimmed_kinds == []
    for kind in (context_budget.RECENT_PUBLIC_ROAMING, context_budget.ROAMING_HANDOFF,
                 context_budget.LIVE_WAKING_CONTINUITY, context_budget.LAST_WORKSPACE_OBSERVATION):
        assert result.included_kind(kind) is not None
    # Generation reserve stays fully protected regardless.
    assert result.final_prompt_cost + context_budget.ROAMING_GENERATION_RESERVE + context_budget.SAFETY_MARGIN <= context_budget.CONTEXT_CEILING

    messages, surviving = wr._roaming_stage1_messages_from_composition(state, result)
    assert len(surviving) == len(live_units)  # nothing trimmed -- all survive
    rendered = json.dumps(messages)
    assert "short preview" in rendered and "a short note" in rendered and "a short bounded result" in rendered


def test_owc9_p1_soft_pressure_trims_mechanically_reserve_protected_no_model_call():
    # Model-free starvation reproduction (spec section 13): synthetic
    # contributors sized to consume close to the full 4096-token
    # ceiling. Proves soft context is mechanically trimmed, the
    # protected reserve remains intact, the final prompt fits, and (by
    # construction -- these are pure functions, no ask_llama_for_json
    # is ever passed in) no model inference occurs.
    state = wr.empty_roaming_state()
    state["authorized"] = True
    state["recent_public_decisions"] = _big_recent_public(10)
    state["departure_handoff"] = {"note": "y" * 1500, "excerpts": [{"author": "book", "text": "y" * 1500}]}
    live_units = [{"event_id": f"ev{i}", "prompt": "z" * 400, "clark_text": "z" * 400} for i in range(10)]

    result = wr._compose_roaming_stage1_budget(state, live_units)
    assert result.fits  # never silently overflows -- deterministically trimmed/dropped instead
    assert result.final_prompt_cost <= context_budget.ROAMING_MAX_PROMPT_BUDGET
    assert result.final_prompt_cost + context_budget.ROAMING_GENERATION_RESERVE + context_budget.SAFETY_MARGIN <= context_budget.CONTEXT_CEILING
    # Under this much pressure, at least something had to give.
    assert result.dropped_kinds or result.trimmed_kinds


def test_owc9_p1_hard_overflow_fails_closed_no_model_call():
    # Spec sections 6/13/14: mandatory roaming control context (HARD)
    # cannot fit under an artificially tiny budget -> BUDGET_EXCEEDED,
    # no model call, no fabricated roaming_act -- routed through the
    # SAME bounded technical-failure/backoff bookkeeping an ordinary
    # structural-validation failure already uses (spec section 14 does
    # not forbid backoff treatment for a host-side budget failure).
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("hard_overflow")
    fake = sequenced_ask_llama_for_json([])  # any call at all would be a bug
    original_budget = context_budget.ROAMING_MAX_PROMPT_BUDGET
    context_budget.ROAMING_MAX_PROMPT_BUDGET = 10  # far below even the fixed HARD control text
    trace_path = os.path.join(TEST_ROOT, "wr_trace_hardoverflow.jsonl")
    try:
        wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                                 session_id="sess-hardoverflow", trace_path=trace_path)
        assert wait_until_stopped()
    finally:
        context_budget.ROAMING_MAX_PROMPT_BUDGET = original_budget
    assert fake.call_count() == 0  # no model call was ever made
    records = wr.query_roaming_trace(trace_path)
    assert any(r.get("workspace_action_status") == context_budget.BUDGET_EXCEEDED for r in records)
    assert wr.get_roaming_state()["authorized"] is False
    assert wr.get_roaming_state()["background_activity_state"] == wr.BACKGROUND_STATE_BACKOFF


def test_owc9_p1_wait_stop_wait_for_human_semantics_unaffected():
    # Section 14: normal-pressure decisions still resolve to exactly
    # the typed act the model chose -- budgeting changes nothing about
    # wait/stop/wait_for_human/act/private_act semantics when
    # composition fits without any trimming pressure at all.
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True
    paths = fresh_paths("owc9p1_semantics")
    fake = sequenced_ask_llama_for_json([
        {"roaming_act": "wait", "wait_minutes": 5},
        {"roaming_act": "wait_for_human", "wait_minutes": 5},
    ])
    wr.start_roaming_worker(ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=paths,
                             session_id="sess-owc9p1-semantics", trace_path=os.path.join(TEST_ROOT, "wr_trace_owc9p1sem.jsonl"))
    assert wait_until_stopped()
    assert fake.call_count() == 2
    assert wr.get_roaming_state()["last_roaming_act"] == wr.ROAMING_ACT_WAIT_FOR_HUMAN
    assert wr.get_roaming_state()["authorized"] is False


def test_owc9_p1_delivery_accounting_only_surviving_units_marked_delivered(monkeypatch):
    # Load-bearing (spec section 8): "ELIGIBLE != DELIVERED." Ten
    # synthetic live-waking units, sized so the compositor can only
    # keep a few of them under aggregate pressure from a maxed-out
    # recent-public window and handoff -- record_live_waking_delivery()
    # must be called with exactly the surviving subset, never the full
    # originally-eligible list.
    data_dir, pipeline_key = fresh_provenance_dir("owc9p1_delivery", seed_human=True)
    wr.reset_roaming_state()
    wr.SECONDS_PER_MINUTE = 0.001
    state = wr.get_roaming_state()
    state["authorized"] = True

    all_units = [{"event_id": f"live-ev-{i}", "prompt": "z" * 400, "clark_text": "z" * 400} for i in range(10)]
    monkeypatch.setattr(wpc, "collect_live_waking_units", lambda *a, **k: list(all_units))
    monkeypatch.setattr(wr, "wpc", wpc)  # ensure workspace_roaming's own bound name sees the patched function

    delivery_calls = []
    original_record = wep.record_live_waking_delivery

    def spy_record(data_dir, *, run_id, pipeline_key, occurred_at, delivered_event_ids):
        delivery_calls.append(list(delivered_event_ids))
        return original_record(data_dir, run_id=run_id, pipeline_key=pipeline_key,
                                occurred_at=occurred_at, delivered_event_ids=delivered_event_ids)
    monkeypatch.setattr(wep, "record_live_waking_delivery", spy_record)
    monkeypatch.setattr(wr, "wep", wep)

    # Aggregate pressure heavy enough that not all 10 units survive.
    state["recent_public_decisions"] = _big_recent_public(10)

    fake = sequenced_ask_llama_for_json([{"roaming_act": "stop", "wait_minutes": 5}])
    ok = wr.start_roaming_worker(
        ask_llama_for_json=fake, clark_actor_id=CLARK_ACTOR_ID, workspace_paths=fresh_paths("owc9p1_delivery_ws"),
        session_id="sess-owc9p1-delivery", trace_path=os.path.join(TEST_ROOT, "wr_trace_owc9p1delivery.jsonl"),
        data_dir=data_dir, pipeline_key=pipeline_key, staging_path="dummy_staging",
    )
    assert ok
    assert wait_until_stopped()
    assert fake.call_count() == 1

    assert len(delivery_calls) == 1
    delivered_ids = delivery_calls[0]
    all_ids = [u["event_id"] for u in all_units]
    assert delivered_ids, "at least the newest units should have survived"
    assert len(delivered_ids) <= len(all_ids)
    assert set(delivered_ids).issubset(set(all_ids))
    # Oldest-first dropping (spec section 7/9): whatever survived must
    # be the NEWEST contiguous tail of the original list, never a gap.
    assert delivered_ids == all_ids[len(all_ids) - len(delivered_ids):]


def test_owc9_p1_diagnostics_generic_only_no_branch_identity():
    # Section 18: bounded, generic/structural diagnostics only -- never
    # rendered content, never which branch (public/private) was chosen.
    state = wr.empty_roaming_state()
    state["authorized"] = True
    result = wr._compose_roaming_stage1_budget(state, [])
    diag = wr._roaming_context_budget_diagnostics(result)
    assert set(diag.keys()) == {
        "context_ceiling", "reserve", "max_prompt_budget", "final_prompt_cost",
        "fits", "included_kinds", "trimmed_kinds", "dropped_kinds", "estimator_method",
    }
    # No content, no private/public branch marker of any kind.
    blob = json.dumps(diag)
    for forbidden in ("private_act", "SECRET", "roaming_act", "content"):
        assert forbidden not in blob


ALL_TESTS = [
    test_validate_roaming_choice_accepts_valid,
    test_validate_roaming_choice_rejects_invalid_act,
    test_validate_roaming_choice_rejects_invalid_wait_minutes,
    test_validate_roaming_choice_malformed_and_leakage,
    test_fresh_roaming_state_off,
    test_authorized_human_control_sets_true,
    test_authorized_human_control_sets_false,
    test_unauthorized_source_fails_closed,
    test_clark_model_path_cannot_reach_authorization_function,
    test_authorized_clark_may_choose_wait_without_action,
    test_authorized_clark_may_stop_immediately,
    test_wait_causes_no_action_and_offers_next_opportunity,
    test_no_overlapping_cycle_duplicate_start_rejected,
    test_act_routes_through_existing_wsp1_validator_and_executor,
    test_roaming_act_reads_local_pdf_through_existing_executor,
    test_library_list_result_becomes_next_observation,
    test_large_library_list_reaches_roaming_prompt_bounded_not_unbounded,
    test_library_read_bounded_text_becomes_next_observation,
    test_pdf_library_read_becomes_next_observation,
    test_music_and_photo_metadata_become_next_observation,
    test_observation_is_exact_host_result_no_summary_keys,
    test_second_action_replaces_first_observation_no_accumulation,
    test_wait_preserves_observation_across_the_interval,
    test_fresh_run_starts_with_observation_none,
    test_clark_stop_clears_observation,
    test_human_stop_clears_observation,
    test_process_reset_clears_observation,
    test_journal_write_then_deliberate_read_reaches_next_observation,
    test_no_automatic_journal_read_after_append,
    test_wait_for_human_sets_unauthorized_clears_observation_and_stops_worker,
    test_wait_for_human_emits_waiting_for_human_trace_status,
    test_wait_for_human_no_further_model_call_and_no_automatic_restart,
    test_wait_for_human_requires_valid_wait_minutes_placeholder,
    test_roaming_may_choose_private_act_and_executes_via_private_pathway,
    test_private_act_without_private_paths_fails_closed_no_crash,
    test_private_act_run_end_clears_private_observation,
    test_ordinary_choice_prompt_never_contains_private_content,
    test_private_act_failure_records_only_failure_class_never_content,
    test_successful_private_action_leaves_no_durable_trace_row,
    test_repeated_successful_private_actions_produce_no_usage_count,
    test_private_success_does_not_create_a_countable_gap_against_ordinary_actions,
    test_roaming_source_never_calls_ordinary_pass2,
    test_observation_bounded_result_not_persisted_in_trace_action_record,
    test_journal_append_succeeds_through_existing_pathway,
    test_music_and_photos_surface_bounded_and_shared_with_roaming,
    test_forbidden_action_fails_closed_and_ends_run,
    test_traversal_attempt_fails_closed_and_ends_run,
    test_every_action_uses_canonical_clark_actor_never_literal_clark,
    test_clark_stop_ends_run_no_next_call,
    test_human_stop_ends_run_and_prevents_next_call,
    test_currently_executing_action_finishes_before_stop_takes_effect,
    test_unattended_calls_use_injected_waking_function_only,
    test_no_rem_sleep_import_or_reference,
    test_roaming_module_never_imports_kardia_hippocampus_journal_dialogue,
    test_no_automatic_journal_write_on_wait_or_stop,
    test_roaming_state_is_single_process_global_not_per_surface,
    test_worker_thread_is_daemon,
    test_no_external_scheduler_or_task_creation,
    test_no_free_form_reasoning_persisted_in_trace,
    test_api1_api2_source_hashes_unchanged,
    # WSP2-P3
    test_ordinary_roaming_choice_prompt_still_excludes_dialogue_content,
    test_build_departure_handles_empty_pairs,
    test_build_departure_handles_assigns_handles_in_chronological_order,
    test_build_departure_handles_respects_pair_count_bound,
    test_build_departure_handles_aggregate_char_bound_keeps_newest,
    test_handoff_choice_zero_items_valid,
    test_handoff_choice_valid_selection_resolves_source_and_note,
    test_handoff_choice_more_than_three_items_rejected,
    test_handoff_choice_unknown_handle_rejected,
    test_handoff_choice_model_cannot_bypass_with_a_fabricated_canonical_id,
    test_handoff_choice_malformed_json_contained,
    test_handoff_choice_bounds_enforced,
    test_empty_departure_handoff_zero_model_calls_and_episode_recorded,
    test_full_roaming_run_with_data_dir_records_canonical_episode_end_to_end,
    test_private_act_never_appends_to_recent_public_window,
    test_handoff_structural_failure_prevents_ordinary_loop_from_starting,
    # WSP2-P3-P1
    test_canonical_missing_db_prevents_ordinary_roaming_loop,
    test_canonical_missing_pipeline_prevents_ordinary_roaming_loop,
    test_canonical_missing_host_actor_prevents_ordinary_roaming_loop,
    test_canonical_missing_clark_actor_prevents_ordinary_roaming_loop,
    test_canonical_handoff_persistence_failure_prevents_ordinary_roaming_loop,
    test_mid_run_canonical_public_action_failure_stops_further_roaming,
    test_mid_run_canonical_wait_failure_stops_further_roaming,
    test_jsonl_availability_cannot_rescue_a_canonical_failure,
    test_handoff_note_and_source_excerpt_reach_first_roaming_choice_context,
    test_handoff_material_persists_through_subsequent_roaming_decisions,
    # WSP2-MA1
    test_ma1_private_action_minimal_legal_shapes_accepted,
    test_ma1_private_action_all_fields_required,
    test_ma1_private_action_forbidden_extra_field_rejected,
    test_ma1_private_action_wrong_types_rejected,
    test_ma1_private_action_invalid_discriminator_rejected,
    test_ma1_generic_structural_failure_mapping_covers_all_known_codes,
    test_ma1_ordinary_structural_failure_trace_is_redacted_and_generic,
    test_ma1_private_paths_none_trace_is_redacted_and_generic,
    test_ma1_private_choice_failure_trace_is_redacted_and_generic,
    test_ma1_ordinary_and_private_structural_failures_produce_identical_trace_shape,
    # WSP3-P2
    test_wsp3p2_generic_execution_mapping_is_single_shared_value,
    test_wsp3p2_A_private_execution_still_fails_exactly_as_before_path_escape,
    test_wsp3p2_C_D_durable_telemetry_generic_for_path_escape,
    test_wsp3p2_C_D_durable_telemetry_generic_for_not_found,
    test_wsp3p2_C_D_durable_telemetry_generic_for_unsupported_extension,
    test_wsp3p2_B_no_private_success_persisted,
    test_wsp3p2_E_no_canonical_public_event_fabricated_on_private_execution_failure,
    test_wsp3p2_F_no_public_continuity_renderer_can_receive_it_source_audit,
    test_wsp3p2_indistinguishability_ordinary_vs_private_execution_failure,
    # WSP2-MA2
    test_ma2_ordinary_stage2_call_receives_exact_ordinary_schema,
    test_ma2_private_stage2_call_receives_exact_private_schema,
    test_ma2_public_private_symmetry_both_schema_constrained,
    test_ma2_departure_handoff_and_stage1_remain_unschema_constrained_source_audit,
    test_ma2_no_fallback_retry_on_schema_failure_source_audit,
    test_ma2_telemetry_does_not_reveal_which_schema_was_selected,
    # WSP2-P5
    test_p5_fresh_process_background_activity_enabled_by_default,
    test_p5_auto_start_reaches_existing_worker_start_path_no_new_loop,
    test_p5_auto_start_no_op_when_worker_already_running,
    test_p5_auto_start_bridge_a_still_occurs,
    test_p5_ordinary_workspace_capability_independent_of_roaming_source_audit,
    test_p5_private_contract_functions_unchanged,
    test_p5_malformed_choice_executes_nothing_and_enters_backoff,
    test_p5_backoff_is_temporary_not_permanent,
    test_p5_no_immediate_automatic_retry_after_failure,
    test_p5_new_episode_may_begin_after_backoff_elapses,
    test_p5_bounded_backoff_policy_escalates_then_fault_pauses,
    test_p5_valid_progress_resets_failure_counter,
    test_p5_lifecycle_state_identical_for_ordinary_vs_private_structural_failure,
    test_p5_wait_sets_waiting_then_active_and_stays_available,
    test_p5_wait_for_human_enters_waiting_state,
    test_p5_wait_for_human_timer_alone_never_releases_it,
    test_p5_release_wait_for_human_allows_new_episode,
    test_p5_release_wait_for_human_is_no_op_when_not_waiting,
    test_p5_clark_stop_distinguishable_and_never_auto_resumes,
    test_p5_human_pause_stops_worker_and_marks_paused_by_human,
    test_p5_human_resume_permits_new_episode,
    test_p5_human_resume_also_clears_fault_paused,
    test_p5_human_resume_is_no_op_when_not_paused,
    test_p5_conversation_direction_ownership_untouched_source_audit,
    test_p5_new_episode_after_backoff_still_uses_p4_shared_continuity_mechanism,
    test_p5_ma2_schemas_unchanged_still_wired,
    # WSP2-P5-P1
    test_p5p1_clark_stop_records_durable_pause_when_data_dir_given,
    test_p5p1_stop_without_data_dir_no_durable_write_no_crash,
    test_p5p1_process_local_state_is_reset_but_durable_truth_survives,
    test_p5p1_fresh_launch_reconstructs_paused_by_clark,
    test_p5p1_fresh_launch_no_reconstruction_when_no_prior_control_event,
    test_p5p1_fresh_launch_no_reconstruction_when_latest_control_is_resumed,
    test_p5p2a_missing_provenance_directory_is_authority_unknown,
    test_p5p1_pass1_can_select_resume_own_pause,
    test_p5p1_apply_self_resume_takes_no_free_text_no_authorization,
    test_p5p1_self_resume_clears_paused_by_clark,
    test_p5p1_self_resume_makes_new_episode_eligible_via_existing_path,
    test_p5p1_self_resume_cannot_clear_paused_by_human,
    test_p5p1_self_resume_cannot_clear_fault_paused,
    test_p5p1_self_resume_idempotent_second_call_is_noop,
    test_p5p1_self_resume_no_op_when_already_active_idle_waiting_backoff,
    test_p5p1_human_resume_still_clears_paused_by_human_and_fault_paused,
    test_p5p1_human_resume_also_clears_paused_by_clark,
    test_p5p1_human_resume_reconciles_durable_truth_so_restart_cannot_resurrect_it,
    test_p5p1_durable_write_site_strictly_after_persistence_in_source,
    test_p5p1_new_episode_after_self_resume_gets_a_fresh_run_id_never_reused,
    test_p5p1_full_restart_sequence,
    # WSP2-P5-P2
    test_p5p2_determine_authority_known_paused,
    test_p5p2_determine_authority_known_resumed,
    test_p5p2_determine_authority_empty_ledger_known_not_paused,
    test_p5p2_determine_authority_corrupt_db_is_unknown,
    test_p5p2_determine_authority_query_exception_is_unknown,
    test_p5p2_determine_authority_unrecognized_value_is_unknown,
    test_p5p2_reconstruction_sets_lifecycle_uncertain_on_corrupt_db,
    test_p5p2_lifecycle_uncertain_blocks_auto_start_eligibility,
    test_p5p2_auto_start_no_worker_no_episode_no_model_call_under_uncertainty,
    test_p5p2_known_not_paused_auto_start_eligibility_unchanged,
    test_p5p2_ordinary_conversation_direction_unaffected_by_uncertainty,
    test_p5p2_clark_self_resume_no_op_under_lifecycle_uncertain,
    test_p5p2_reconstruction_reread_clears_uncertain_once_db_becomes_readable,
    test_p5p2_reconstruction_reread_finds_real_pause_once_db_becomes_readable,
    test_p5p2_full_restart_sequence_with_temporary_unreadability,
    test_p5p2a_cwd_misdirection_does_not_affect_reconstruction_result,
    test_p5p2a_reconstruction_never_creates_the_missing_db,
    # OWC9-P1
    test_owc9_p1_no_pressure_all_kinds_survive_untouched,
    test_owc9_p1_soft_pressure_trims_mechanically_reserve_protected_no_model_call,
    test_owc9_p1_hard_overflow_fails_closed_no_model_call,
    test_owc9_p1_wait_stop_wait_for_human_semantics_unaffected,
    test_owc9_p1_diagnostics_generic_only_no_branch_identity,
    # OWC9-P4
    test_owc9p4_stage2_public_budget_composition_fits,
    test_owc9p4_stage2_public_hard_overflow_no_model_call,
    test_owc9p4_stage2_private_budget_composition_fits_empty_observation,
    test_owc9p4_stage2_private_oversized_observation_drops_not_truncates,
    test_owc9p4_stage2_private_hard_overflow_no_model_call,
]


def main():
    passed, failed = 0, 0
    failures = []
    for t in ALL_TESTS:
        try:
            setup_method()
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
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
