"""Real waking/H/X writers and sync, synthetic DBs, fake model only."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import hippocampus_store as hs
import sleep_c_schema
import test_sleep_landing as landing
import test_human_waking_authority_end_to_end as authority_fixture
from test_post_sleep_schema_upgrade import legacy_db
from resume_human_input import read_unanswered_input


class ContinuationTests(unittest.TestCase):
    def test_failed_first_wake_then_same_h_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = authority_fixture._fresh_env(tmp)
            try:
                waking = env['llama_anaxi']
                waking.CONVERSATION_DIRECTION_TRACE_PATH = os.path.join(tmp, 'direction.jsonl')
                waking.reset_working_set()
                import test_owc5_s2_integration as fake_model
                calls = []
                waking.ollama = fake_model.build_fake_ollama(calls,
                    {'value': {'act':'yield_direction','thread':'','direction_request':'none','relinquish_direction':False}},
                    {'value': {'expression':'Good morning. This is a complete synthetic reply.'}})
                waking._ollama_environment_cache.update({
                    'checked':True, 'model_digest':fake_model._CALIBRATED_MODEL_DIGEST,
                    'server_version':fake_model._CALIBRATED_SERVER_VERSION,
                    'chat_template_sha256':fake_model._CALIBRATED_CHAT_TEMPLATE_SHA256,
                })
                prov = env['provenance_db_path']
                with closing(sqlite3.connect(prov)) as c, c:
                    c.execute('PRAGMA foreign_keys=ON')
                    authority = env['human_session_binding'].bind_session_to_registered_human(
                        c, session_id=waking.get_current_session_id(), session_started_at=waking.get_current_session_started_at(),
                        pipeline_key=waking.PIPELINE_KEY, actor_id=env['registered_actor_id'])
                    sleep_c_schema.ensure_schema(c)
                landing.insert_cycle_and_derivation(prov, cycle_id='synthetic-cycle', derivation_id='synthetic-d', derived_text='Synthetic tentative continuity.')
                Path(waking.LOG_FILE).write_text('')
                hip = os.path.join(tmp, 'hip.db'); legacy_db(hip).close()
                paths = hs.HippocampusPaths(prov, waking.RELATIONAL_DB_PATH, waking.LOG_FILE, hip)
                original_prepare = env['orch'].prepare_context
                def prepare(user, prompt):
                    hs.sync_hippocampus(hip, paths)
                    prepared = original_prepare(user, prompt)
                    core, style = fake_model._calibration_identity_preamble_and_style()
                    prepared['core_system_text'] = core
                    prepared['messages'][0]['content'] = core
                    prepared['controls']['style_instruction'] = style
                    return prepared
                env['orch'].prepare_context = prepare
                prompt = 'Good morning! '
                with patch.object(hs, '_open_hippocampus_writable', side_effect=lambda path: sqlite3.connect(path)):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'source_store IN'):
                        waking.run_waking_turn(env['orch'], prompt, interaction_mode=waking.CONVERSATION_MODE, human_input_authority=authority)
                self.assertEqual(calls, [])
                with closing(sqlite3.connect(prov)) as c:
                    h, = c.execute("SELECT event_id FROM events WHERE event_type='human_waking_input'").fetchone()
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE event_type='waking_turn'").fetchone()[0], 0)
                self.assertEqual(read_unanswered_input(prov, h, env['registered_actor_id']), prompt)
                with closing(sqlite3.connect(hip)) as c:
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM hippocampal_items').fetchone()[0], 0)
                with self.assertRaises(ValueError):
                    waking.run_waking_turn(env['orch'], prompt, existing_human_input_event_id=h)
                with self.assertRaises(ValueError):
                    waking.run_waking_turn(env['orch'], prompt.rstrip(), human_input_authority=authority, existing_human_input_event_id=h)
                # The operator continuation runs in a fresh process/session.
                original_session = waking.get_current_session_id()
                waking._native_session_state.update(session_id=None, session_started_at=None)
                with closing(sqlite3.connect(prov)) as c, c:
                    c.execute('PRAGMA foreign_keys=ON')
                    authority = env['human_session_binding'].bind_session_to_registered_human(
                        c, session_id=waking.get_current_session_id(), session_started_at=waking.get_current_session_started_at(),
                        pipeline_key=waking.PIPELINE_KEY, actor_id=env['registered_actor_id'])
                self.assertNotEqual(waking.get_current_session_id(), original_session)
                result = waking.run_waking_turn(env['orch'], prompt, interaction_mode=waking.CONVERSATION_MODE,
                                               human_input_authority=authority, existing_human_input_event_id=h)
                with closing(sqlite3.connect(prov)) as c:
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'").fetchone()[0], 1)
                    self.assertEqual(c.execute("SELECT component_text FROM event_components WHERE event_id=? AND component_kind='human_input_event_id'", (result['native_event_id'],)).fetchone()[0], h)
                    self.assertEqual(c.execute('SELECT a.session_id FROM events e JOIN auth_contexts a ON a.auth_context_id=e.auth_context_id WHERE e.event_id=?', (h,)).fetchone()[0], original_session)
                self.assertGreaterEqual(len(calls), 2)
                count = len(calls)
                with self.assertRaises(ValueError):
                    read_unanswered_input(prov, h, env['registered_actor_id'])
                with self.assertRaises(ValueError):
                    waking.run_waking_turn(env['orch'], prompt, human_input_authority=authority, existing_human_input_event_id=h)
                self.assertEqual(len(calls), count)
            finally:
                authority_fixture._teardown_env(env)


if __name__ == '__main__':
    unittest.main()
