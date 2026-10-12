"""Reference-harness finding (R2, red on production master): two exchanges whose H's share a second were
projected H1, H2, X1, X2 -- each reply detached from what it answers -- because the timeline sorted by
role rank before event id.  The GUI projection (and the WTR adjacency verifier that reads it) must keep
every exchange together.  Countermodel: any ordering that sorts all same-second users before assistants."""
import os
import time

import conversation_projection as cp
from native_provenance_writer import stage_and_record_native_waking_turn
from test_waking_turn_recovery import Env


def _x(env, h_id, prose, at):
    return stage_and_record_native_waking_turn(
        env.tmp_root, os.path.join(env.tmp_root, "native_turn_staging.jsonl"),
        session_id="sess-same-second", session_started_at=at, user_id="u", prompt="p", bounded_clause="",
        clark_prose=prose, kardia={}, controls={}, waking_model_tag="gemma4:e4b", pipeline_key=env.pipeline_key,
        artifact_pass_ran=False, occurred_at=at, human_input_event_id=h_id)["event_id"]


def test_same_second_exchanges_stay_adjacent_in_the_projection():
    env = Env("same-second")
    import human_session_binding as hsb
    frozen = int(time.time())
    _h, authority = env.make_h("an earlier exchange opener")      # the session binding to reuse
    h1 = hsb.record_human_waking_input(env.tmp_root, pipeline_key=env.pipeline_key, authority=authority,
                                       message="first message", occurred_at=frozen + 60)
    h2 = hsb.record_human_waking_input(env.tmp_root, pipeline_key=env.pipeline_key, authority=authority,
                                       message="second message", occurred_at=frozen + 60)
    frozen += 60
    x2 = _x(env, h2, "reply to the second", frozen)
    x1 = _x(env, h1, "reply to the first", frozen)
    rows = cp.project_waking_conversation(env.db_path, include_event_metadata=True)[1:]
    assert [(r["event_id"], r["content"]) for r in rows] == [
        (h1, "first message"), (x1, "reply to the first"),
        (h2, "second message"), (x2, "reply to the second")]
