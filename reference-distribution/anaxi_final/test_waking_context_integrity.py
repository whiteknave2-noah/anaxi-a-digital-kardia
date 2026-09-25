"""Canonical cross-session referents and structural OWC9 payload proof only."""
from contextlib import closing
import os
import sqlite3
import tempfile
import unittest

import context_budget as cb
from session_dialogue_window import dialogue_contribution, dialogue_messages


class PairBudgetTests(unittest.TestCase):
    def test_trim_and_refill_preserve_whole_pairs_and_pin_newest(self):
        messages = [{'role':r, 'content':t} for r,t in (
            ('user','old human ' * 20), ('assistant','old answer ' * 20),
            ('user','prior question'), ('assistant','The cobalt lantern is lit.'))]
        for refill in (False, True):
            with self.subTest(refill=refill):
                dialogue = dialogue_contribution(messages)
                hard = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, 'You said "cobalt lantern"?', hard=True)
                retrieval = cb.Contribution(cb.RETRIEVED_HISTORY, 'retrieval ' * 400, hard=False)
                budget = hard.cost + cb.estimate_tokens(''.join(m['content'] for m in messages[-2:]))
                result = cb.compose_within_budget([hard, dialogue, retrieval], budget, refill_unused_capacity=refill)
                self.assertTrue(result.fits)
                self.assertEqual(dialogue_messages(dialogue), messages[-2:])

                self.assertEqual(messages[0]['content'], 'old human ' * 20)
                # Even when nothing else remains to evict, fail instead of
                # dropping or clipping the newest pair to squeeze through.
                result = cb.compose_within_budget([hard, dialogue], budget - 1, refill_unused_capacity=refill)
                self.assertFalse(result.fits)
                self.assertEqual(dialogue_messages(dialogue), messages[-2:])

        dialogue = dialogue_contribution(messages)
        full_budget = hard.cost + dialogue.cost
        result = cb.compose_within_budget([hard, dialogue, retrieval], full_budget, refill_unused_capacity=True)
        self.assertTrue(result.fits)
        self.assertEqual(dialogue_messages(dialogue), messages)

    def test_malformed_role_sequence_is_rejected(self):
        for messages in ([{'role':'assistant','content':'orphan'}],
                         [{'role':'user','content':'h'},{'role':'user','content':'mis-roled x'}]):
            with self.assertRaises(ValueError):
                dialogue_contribution(messages)


class WakingPayloadTests(unittest.TestCase):
    def test_fresh_session_quotes_previous_canonical_x_in_both_real_passes(self):
        import test_human_waking_authority_end_to_end as fixture
        with tempfile.TemporaryDirectory() as tmp:
            env = fixture._fresh_env(tmp)
            try:
                import test_owc5_s2_integration as fake_model
                waking = env['llama_anaxi']
                waking.CONVERSATION_DIRECTION_TRACE_PATH = os.path.join(tmp,'direction.jsonl')
                waking.reset_working_set()
                calls = []
                answer = {'value': {'expression':'The cobalt lantern is lit.'}}
                waking.ollama = fake_model.build_fake_ollama(calls,
                    {'value': {'act':'yield_direction','thread':'','direction_request':'none','relinquish_direction':False}}, answer)
                waking._ollama_environment_cache.update(checked=True, model_digest=fake_model._CALIBRATED_MODEL_DIGEST,
                    server_version=fake_model._CALIBRATED_SERVER_VERSION, chat_template_sha256=fake_model._CALIBRATED_CHAT_TEMPLATE_SHA256)
                original_prepare = env['orch'].prepare_context
                def prepare(user, prompt):
                    p = original_prepare(user,prompt)
                    core,style = fake_model._calibration_identity_preamble_and_style()
                    p['core_system_text']=core; p['messages'][0]['content']=core; p['controls']['style_instruction']=style
                    p['messages'].append({'role':'user','content':prompt})
                    return p
                env['orch'].prepare_context=prepare
                def bind():
                    with closing(sqlite3.connect(env['provenance_db_path'])) as c, c:
                        c.execute('PRAGMA foreign_keys=ON')
                        return env['human_session_binding'].bind_session_to_registered_human(c,
                            session_id=waking.get_current_session_id(), session_started_at=waking.get_current_session_started_at(),
                            pipeline_key=waking.PIPELINE_KEY, actor_id=env['registered_actor_id'])
                first = waking.run_waking_turn(env['orch'], 'Good morning! ', interaction_mode=waking.CONVERSATION_MODE,
                                              human_input_authority=bind())
                first_session=waking.get_current_session_id()
                waking._native_session_state.update(session_id=None,session_started_at=None)
                waking.reset_working_set()
                second_authority=bind()
                self.assertNotEqual(first_session,waking.get_current_session_id())
                calls.clear()
                fresh='You said "the cobalt lantern is lit". Was that your wording?'
                answer['value']={'expression':'A synthetic reply without an understanding assertion.'}
                second = waking.run_waking_turn(env['orch'], fresh, interaction_mode=waking.CONVERSATION_MODE,
                                               human_input_authority=second_authority)
                passes=[c for c in calls if c.get('options',{}).get('num_predict') in (
                    cb.PASS1_GENERATION_RESERVE, cb.PASS2_GENERATION_RESERVE)]
                self.assertEqual(len(passes),2)
                expected=[{'role':'user','content':'Good morning! '},
                          {'role':'assistant','content':'The cobalt lantern is lit.'},
                          {'role':'user','content':fresh}]
                # Both passes show the prior reply exactly as persisted (shared expression seam).
                for call,want in zip(passes,(expected,expected)):
                    self.assertEqual(call['messages'][1:],want)
                    self.assertEqual(call['messages'][0]['role'],'system')
                    self.assertNotIn('The cobalt lantern',call['messages'][0]['content'])
                self.assertIn("direction_owner='unknown'",passes[0]['messages'][0]['content'])
                self.assertEqual(passes[1]['messages'][0]['content'].count('direction_owner: unknown'),1)
                self.assertNotIn('direction_owner: human',passes[1]['messages'][0]['content'])
                self.assertIn('You did not request a change in directional ownership.',passes[1]['messages'][0]['content'])
                with closing(sqlite3.connect(env['provenance_db_path'])) as c:
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'").fetchone()[0],2)
                    for x in (first['native_event_id'],second['native_event_id']):
                        self.assertEqual(c.execute("SELECT COUNT(*) FROM event_components WHERE event_id=? AND component_kind='human_input_event_id'",(x,)).fetchone()[0],1)
            finally:
                fixture._teardown_env(env)


if __name__ == '__main__':
    unittest.main()
