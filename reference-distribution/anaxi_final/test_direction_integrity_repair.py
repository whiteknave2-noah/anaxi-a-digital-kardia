"""Production failure shape with synthetic dialogue only; no live inference."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import context_budget as cb
from session_dialogue_window import dialogue_contribution, dialogue_messages
import test_human_waking_authority_end_to_end as fixture


class BudgetRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixture._fresh_env(self.tmp.name)
        self.w = self.env['llama_anaxi']

    def tearDown(self):
        fixture._teardown_env(self.env)
        self.tmp.cleanup()

    def shape(self, budget=3200, dialogue_size=1415):
        human = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, 'h' * 95, True)
        core = cb.Contribution(cb.CORE_SYSTEM_CONTROL, 's' * (budget - 1353), True)
        pairs = [{'role':'user', 'content':'prior question'},
                 {'role':'assistant', 'content':'a' * (dialogue_size - 14)}]
        dialogue = dialogue_contribution(pairs)
        contributions = [core, human, dialogue]
        result = cb.compose_within_budget(contributions, budget)
        return human, dialogue, contributions, result, pairs

    def test_actual_overflow_and_adjacent_pass1_recover_without_losing_roles(self):
        for budget in (cb.PASS2_MAX_PROMPT_BUDGET, cb.PASS1_MAX_PROMPT_BUDGET):
            h, d, cs, result, pairs = self.shape(budget)
            self.assertEqual(result.final_prompt_cost, budget + 157)
            self.assertFalse(result.fits)
            with patch.object(self.w.ollama, 'chat', return_value={
                    'done':True, 'prompt_eval_count':480, 'message':{'content':'discard me'}}) as model:
                fixed = self.w._recover_hard_overflow_via_real_measurement(result, cs, budget, h, h.rendered_text, {'type':'object'})
            self.assertTrue(fixed.fits)
            self.assertEqual(fixed.final_prompt_cost, budget - 778)
            self.assertEqual(dialogue_messages(d), pairs)
            self.assertEqual(model.call_count, 1)
            request = model.call_args.kwargs
            self.assertEqual(request['messages'][1:-1], pairs)
            self.assertEqual(request['messages'][-1]['role'], 'user')
            self.assertEqual(request['options']['num_predict'], 1)
            self.assertEqual(request['options']['num_ctx'], cb.CONTEXT_CEILING)
            self.assertFalse(request['think'])
            self.assertEqual(request['format'], {'type':'object'})

    def test_invalid_counts_or_non_helpful_counts_drop_only_soft_dialogue(self):
        for count in (None, -1, 0, True, '480', 4095, 1500):
            with self.subTest(count=count):
                h, d, cs, result, pairs = self.shape()
                with patch.object(self.w.ollama, 'chat', return_value={'done':True, 'prompt_eval_count':count}):
                    fixed = self.w._recover_hard_overflow_via_real_measurement(result, cs, 3200, h, h.rendered_text)
                self.assertTrue(fixed.fits)
                self.assertIsNone(fixed.included_kind(cb.RECENT_DIALOGUE))
                self.assertIn(cb.RECENT_DIALOGUE, fixed.dropped_kinds)
                self.assertIsNotNone(fixed.included_kind(cb.CURRENT_HUMAN_MESSAGE))
                self.assertEqual(pairs[0]['content'], 'prior question')

    def test_live_pass2_hard_overflow_ignores_disproved_ratio_heuristic(self):
        """The second live failure had a 44.6% byte-accounted overflow.

        Its actual current-H cost was ~398 provider tokens, so the hard floor
        fit comfortably. The former 30% plausibility gate made the provider's
        real answer unreachable and stopped ordinary waking before Pass 2.
        """
        human = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, 'h' * 1968, True)
        contributions = [
            cb.Contribution(cb.CORE_SYSTEM_CONTROL, 's' * 1207, True),
            human,
            cb.Contribution(cb.MECHANICAL_STATE, 'm' * 156, True),
            cb.Contribution(cb.WORKING_SET, 'w' * 591, True),
            cb.Contribution(cb.CONVERSATION_FRAMING, 'f' * 156, True),
        ]
        result = cb.compose_within_budget(contributions, cb.PASS2_MAX_PROMPT_BUDGET)
        self.assertEqual(result.final_prompt_cost, 4078)
        self.assertFalse(result.fits)
        self.assertGreater(
            result.final_prompt_cost - cb.PASS2_MAX_PROMPT_BUDGET,
            human.cost * 0.3,
        )

        with patch.object(self.w, '_measure_real_text_cost', return_value=398) as measure:
            fixed = self.w._recover_hard_overflow_via_real_measurement(
                result, contributions, cb.PASS2_MAX_PROMPT_BUDGET,
                human, human.rendered_text, {'type': 'object'},
            )
        measure.assert_called_once_with(human.rendered_text, structured_schema={'type': 'object'})
        self.assertTrue(fixed.fits)
        self.assertEqual(fixed.final_prompt_cost, 2508)
        self.assertEqual(fixed.included_kind(cb.CURRENT_HUMAN_MESSAGE).rendered_text, 'h' * 1968)

    def test_current_h_measurement_is_bounded_and_rejects_invalid_provider_counts(self):
        safe_text = 'ordinary prose ' * 50
        valid = [
            {'done': True, 'prompt_eval_count': 180},
            {'done': True, 'prompt_eval_count': 12},
        ]
        with patch.object(self.w.ollama, 'chat', side_effect=valid) as model:
            self.assertEqual(self.w._measure_real_text_cost(safe_text, {'type': 'object'}), 168)
        self.assertEqual(model.call_count, 2)
        for call in model.call_args_list:
            self.assertEqual(call.kwargs['options']['num_predict'], 1)
            self.assertEqual(call.kwargs['options']['num_ctx'], cb.CONTEXT_CEILING)

        with patch.object(self.w.ollama, 'chat') as model:
            self.assertIsNone(self.w._measure_real_text_cost('x' * cb.CONTEXT_CEILING))
        model.assert_not_called()

        with patch.object(self.w.ollama, 'chat', side_effect=[
                {'done': True, 'prompt_eval_count': 10},
                {'done': True, 'prompt_eval_count': 12},
        ]):
            self.assertIsNone(self.w._measure_real_text_cost(safe_text))

    def test_measured_hard_floor_remains_viable_under_accumulated_soft_state(self):
        """Lawful optional growth cannot veto an actually-fitting hard floor."""
        human_text = 'ordinary current thought ' * 100
        human = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, human_text, True)
        dialogue = dialogue_contribution([
            {'role': 'user', 'content': 'earlier question ' * 100},
            {'role': 'assistant', 'content': 'earlier answer ' * 140},
        ])
        contributions = [
            cb.Contribution(cb.CORE_SYSTEM_CONTROL, 's' * 1207, True),
            human,
            cb.Contribution(cb.MECHANICAL_STATE, 'm' * 156, True),
            cb.Contribution(cb.WORKING_SET, 'w' * 591, True),
            cb.Contribution(cb.CONVERSATION_FRAMING, 'f' * 156, True),
            cb.Contribution(cb.RETRIEVED_HISTORY, 'r' * 6000, False),
            cb.Contribution(cb.EPISODE_CONTEXT, 'e' * 5000, False),
            dialogue,
        ]
        result = cb.compose_within_budget(
            contributions, cb.PASS2_MAX_PROMPT_BUDGET, refill_unused_capacity=True,
        )
        self.assertFalse(result.fits)

        with patch.object(self.w, '_measure_real_text_cost', return_value=520), patch.object(
                self.w, '_measure_retained_dialogue_cost', return_value=None):
            fixed = self.w._recover_hard_overflow_via_real_measurement(
                result, contributions, cb.PASS2_MAX_PROMPT_BUDGET,
                human, human_text, {'type': 'object'},
            )
        self.assertTrue(fixed.fits)
        self.assertEqual(fixed.final_prompt_cost, 2630)
        self.assertEqual(
            fixed.included_kind(cb.CURRENT_HUMAN_MESSAGE).rendered_text,
            human_text,
        )
        self.assertIsNotNone(fixed.included_kind(cb.CORE_SYSTEM_CONTROL))
        self.assertIsNone(fixed.included_kind(cb.RETRIEVED_HISTORY))
        self.assertIsNone(fixed.included_kind(cb.EPISODE_CONTEXT))
        self.assertIsNone(fixed.included_kind(cb.RECENT_DIALOGUE))

    def test_excessive_dialogue_is_bounded_before_probe(self):
        h, d, cs, result, pairs = self.shape(dialogue_size=10000)
        with patch.object(self.w.ollama, 'chat') as model:
            fixed = self.w._recover_hard_overflow_via_real_measurement(result, cs, 3200, h, h.rendered_text)
            self.assertIsNone(self.w._measure_retained_dialogue_cost(pairs))
        self.assertTrue(fixed.fits)
        self.assertIsNone(fixed.included_kind(cb.RECENT_DIALOGUE))
        self.assertIn(cb.RECENT_DIALOGUE, fixed.dropped_kinds)
        model.assert_not_called()

    def test_live_failure_sized_pair_is_measured_in_safe_role_preserving_parts(self):
        """Regression for live H 01M2SF8GTMYWQ9HS49590X08R5's ledger.

        The canonical text itself is deliberately not copied into this test;
        only its mechanical UTF-8 sizes and production budget costs are.
        """
        human = cb.Contribution(cb.CURRENT_HUMAN_MESSAGE, 'h' * 288, True)
        core = cb.Contribution(cb.CORE_SYSTEM_CONTROL, 's' * 693, True)
        mechanical = cb.Contribution(cb.MECHANICAL_STATE, 'm' * 1304, True)
        messages = [
            {'role':'user', 'content':'u' * 1139},
            {'role':'assistant', 'content':'a' * 2858},
        ]
        dialogue = dialogue_contribution(messages)
        contributions = [core, human, mechanical, dialogue]
        result = cb.compose_within_budget(contributions, cb.PASS1_MAX_PROMPT_BUDGET)
        self.assertEqual(result.final_prompt_cost, 6282)
        self.assertFalse(result.fits)

        # Conservative, role-aware isolated measurements.  Their sum keeps
        # the intact pair below the real Pass-1 ceiling without pretending
        # either canonical message was smaller than it was.
        with patch.object(self.w.ollama, 'chat', side_effect=[
                {'done':True, 'prompt_eval_count':320},
                {'done':True, 'prompt_eval_count':760},
        ]) as model:
            self.w.ui_turn_diagnostics.start_model_call_tracking()
            fixed = self.w._recover_hard_overflow_via_real_measurement(
                result, contributions, cb.PASS1_MAX_PROMPT_BUDGET,
                human, human.rendered_text, {'type':'object'},
            )
            stages = self.w.ui_turn_diagnostics.get_and_clear_model_calls()
        self.assertTrue(fixed.fits)
        self.assertEqual(fixed.final_prompt_cost, 3365)
        self.assertEqual(dialogue_messages(dialogue), messages)
        self.assertEqual(model.call_count, 2)
        self.assertEqual([s['purpose'] for s in stages], [
            'retained_dialogue_cost_recovery_probe',
            'retained_dialogue_cost_recovery_probe',
        ])
        user_probe, assistant_probe = [c.kwargs for c in model.call_args_list]
        self.assertEqual(user_probe['messages'][1], messages[0])
        self.assertEqual(user_probe['messages'][-1]['role'], 'user')
        self.assertEqual(assistant_probe['messages'][1], messages[1])
        self.assertEqual(assistant_probe['messages'][-1]['role'], 'user')
        for request in (user_probe, assistant_probe):
            self.assertEqual(request['options']['num_predict'], 1)
            self.assertEqual(request['options']['num_ctx'], cb.CONTEXT_CEILING)
            self.assertFalse(request['think'])

    def test_partition_preflights_every_message_before_any_probe(self):
        messages = [
            {'role':'user', 'content':'u' * 1139},
            {'role':'assistant', 'content':'a' * 5000},
        ]
        with patch.object(self.w.ollama, 'chat') as model:
            self.assertIsNone(self.w._measure_retained_dialogue_cost(messages))
        model.assert_not_called()

    def test_probe_does_not_restore_trimmed_pairs_or_retrieval(self):
        h, d, cs, _, pairs = self.shape()
        d = dialogue_contribution([{'role':'user','content':'old '*600},
                                   {'role':'assistant','content':'old reply '*600}] + pairs)
        retrieval = cb.Contribution(cb.RETRIEVED_HISTORY, 'r' * 6000, False)
        cs = cs[:2] + [d, retrieval]
        result = cb.compose_within_budget(cs, 3200, refill_unused_capacity=True)
        with patch.object(self.w.ollama, 'chat', return_value={'done':True, 'prompt_eval_count':480}):
            fixed = self.w._recover_hard_overflow_via_real_measurement(result, cs, 3200, h, h.rendered_text)
        self.assertTrue(fixed.fits)
        self.assertEqual(dialogue_messages(d), pairs)
        self.assertIn(cb.RETRIEVED_HISTORY, fixed.dropped_kinds)
        self.assertIn(cb.RECENT_DIALOGUE, fixed.trimmed_kinds)
        self.assertIsNone(fixed.included_kind(cb.RETRIEVED_HISTORY))

    def test_failure_then_same_h_in_fresh_open_session_with_real_canonical_writers(self):
        import test_owc5_s2_integration as fake_model
        from resume_human_input import read_unanswered_input
        from conversation_projection import project_waking_conversation
        w, env = self.w, self.env
        w.CONVERSATION_DIRECTION_TRACE_PATH = os.path.join(self.tmp.name, 'direction.jsonl')
        w.reset_working_set()
        calls = []
        act = {'value':{'act':'ask_human', 'thread':'synthetic_topic',
                        'direction_request':'request_human', 'relinquish_direction':False}}
        answer = {'value':{'expression':'Synthetic answer. ' + 'a' * 1212}}
        w.ollama = fake_model.build_fake_ollama(calls, act, answer)
        w._ollama_environment_cache.update(checked=True, model_digest=fake_model._CALIBRATED_MODEL_DIGEST,
            server_version=fake_model._CALIBRATED_SERVER_VERSION, chat_template_sha256=fake_model._CALIBRATED_CHAT_TEMPLATE_SHA256)
        original_prepare = env['orch'].prepare_context
        def prepare(user, prompt):
            p = original_prepare(user, prompt)
            _, style = fake_model._calibration_identity_preamble_and_style()
            # Leave room for Temporal Grounding's required host facts while
            # retaining the original Pass-2-only protected-pair overflow shape.
            # (lawful-null recalibration: the calibrated Pass-1 menu grew 89 tokens, so the protected
            # prior answer below shrank 90 bytes to keep Pass 1 within budget, and this Pass-2-only
            # identity text grew 40 bytes so the shape is still a Pass-2-only protected-pair overflow.)
            p['core_system_text'] = 'Synthetic identity. ' * 42
            p['messages'][0]['content'] = p['core_system_text']
            p['controls']['style_instruction'] = style
            p['messages'].append({'role':'user','content':prompt})
            return p
        env['orch'].prepare_context = prepare
        def bind():
            with closing(sqlite3.connect(env['provenance_db_path'])) as c, c:
                c.execute('PRAGMA foreign_keys=ON')
                return env['human_session_binding'].bind_session_to_registered_human(c,
                    session_id=w.get_current_session_id(), session_started_at=w.get_current_session_started_at(),
                    pipeline_key=w.PIPELINE_KEY, actor_id=env['registered_actor_id'])
        authority = bind()
        prior = 'Synthetic prior question. ' + 'h' * 69
        w.run_waking_turn(env['orch'], prior, interaction_mode=w.CONVERSATION_MODE, human_input_authority=authority)
        current = 'Synthetic current question. ' + 'h' * 67 + ' '
        calls.clear()
        staging_before = Path(w.STAGING_PATH).read_bytes()
        # Reproduce the pre-repair Pass-2-only gate so this test can still
        # establish exact-H retry semantics independently of the new soft-
        # dialogue pressure valve exercised above.
        with patch.object(w, '_recover_hard_overflow_via_real_measurement',
                          side_effect=lambda result, *args, **kwargs: result):
            with self.assertRaises(w.ConversationDirectionFailure) as failure:
                w.run_waking_turn(env['orch'], current, interaction_mode=w.CONVERSATION_MODE, human_input_authority=authority)
        self.assertEqual((failure.exception.stage, failure.exception.failure_code), ('pass2', cb.BUDGET_EXCEEDED))
        # Only the Pass-1 generation: cost probes (baseline system message) are measurements.
        self.assertEqual(len([c for c in calls if c['messages'][0]['content'] != 'You are a helpful assistant.']), 1)
        self.assertEqual(Path(w.STAGING_PATH).read_bytes(), staging_before)
        self.assertEqual(w.get_working_set()['direction_owner'], 'unknown')
        with closing(sqlite3.connect(env['provenance_db_path'])) as c:
            h = c.execute("SELECT event_id FROM event_components WHERE component_kind='human_conversational_input' AND component_text=?", (current,)).fetchone()[0]
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE event_type='waking_turn'").fetchone()[0], 1)
        self.assertEqual(read_unanswered_input(env['provenance_db_path'], h, env['registered_actor_id']), current)
        projection = project_waking_conversation(env['provenance_db_path'])
        self.assertIn({'role':'user','content':current}, projection)
        self.assertEqual(len(projection), 3)  # no host diagnostic becomes X
        prior_pair = [{'role':'user','content':prior}, {'role':'assistant','content':answer['value']['expression']}]
        previous_session = w.get_current_session_id()
        w._native_session_state.update(session_id=None, session_started_at=None)
        w.reset_working_set()
        authority = bind()
        self.assertNotEqual(previous_session, w.get_current_session_id())
        calls.clear()
        answer['value'] = {'expression':'A complete synthetic response after bounded recovery.'}
        original_chat = w.ollama.chat
        probes = []
        def model(**kwargs):
            if kwargs.get('options', {}).get('num_predict') == 1:
                probes.append(kwargs)
                return {'done':True, 'prompt_eval_count':480}
            return original_chat(**kwargs)
        with patch.object(w.ollama, 'chat', side_effect=model):
            result = w.run_waking_turn(env['orch'], current, interaction_mode=w.CONVERSATION_MODE,
                human_input_authority=authority, existing_human_input_event_id=h)
        self.assertGreaterEqual(len(probes), 1)  # bounded measurement, never generation
        self.assertEqual(len(calls), 2)
        # Both passes see the exact stored pair (shared expression seam: no protocol marker
        # is re-attached to Clark's earlier replies).
        for call, expected_pair in zip(calls, (prior_pair, prior_pair)):
            self.assertEqual(call['messages'][1:3], expected_pair)
            self.assertEqual(call['messages'][-1], {'role':'user','content':current})
        self.assertIn('direction_owner: unknown', calls[1]['messages'][0]['content'])
        self.assertIn('This request did not change ownership.', calls[1]['messages'][0]['content'])
        self.assertEqual(w.get_working_set()['direction_owner'], 'unknown')
        with closing(sqlite3.connect(env['provenance_db_path'])) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE event_type='human_waking_input'").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT component_text FROM event_components WHERE event_id=? AND component_kind='human_input_event_id'", (result['native_event_id'],)).fetchone()[0], h)
        with self.assertRaises(ValueError):
            read_unanswered_input(env['provenance_db_path'], h, env['registered_actor_id'])


class OpenControlTests(unittest.TestCase):
    def setUp(self):
        # The real Desktop actor binding must not leak into synthetic GUI DBs.
        env = patch.dict(os.environ, {'ANAXI_BOUND_HUMAN_ACTOR_ID':'', 'GRADIO_ANALYTICS_ENABLED':'False'})
        env.start()
        self.addCleanup(env.stop)

    def test_human_open_clark_open_ui_trace_and_both_passes_agree(self):
        import test_llama_gui_conversation_mode as gui_fixture
        gui, w, calls, _, _, _, _, tmp, trace = gui_fixture.fresh_gui(['llama_gui.py', '--conversation'],
            pass1_value={'act':'ask_human', 'thread':'synthetic', 'direction_request':'request_human', 'relinquish_direction':False},
            pass2_value={'expression':'A complete synthetic response.'})
        gui_fixture.setup_canonical_human_actor(tmp)
        original_chat = w.ollama.chat
        def model(**kwargs):
            response = original_chat(**kwargs)
            response.update(done=True, done_reason='stop', eval_count=100, prompt_eval_count=1000)
            return response
        w.ollama.chat = model
        for action, owner, label in ((gui.take_direction,'human','You'), (gui.open_direction,'unknown','Open'),
                                    (gui.give_direction_to_clark,'clark','Clark'), (gui.open_direction,'unknown','Open')):
            self.assertIn('Conversation lead: ' + label, action())
            self.assertEqual(w.get_working_set()['direction_owner'], owner)
            record = gui.direction_control.query_direction_control_trace(trace)[-1]
            self.assertEqual(record['direction_owner_after'], owner)
            calls.clear()
            reply, status = gui.respond(gui_fixture.NEUTRAL_MESSAGE, [])
            self.assertEqual(reply, 'A complete synthetic response.')
            self.assertEqual(status, '')
            self.assertEqual(len(calls), 2)
            self.assertIn("direction_owner='" + owner + "'", calls[0]['messages'][0]['content'])
            self.assertIn('direction_owner: ' + owner, calls[1]['messages'][0]['content'])
            self.assertEqual(w.get_working_set()['direction_owner'], owner)
            self.assertIn('Conversation lead: ' + label, gui.refresh_conversation_lead())
        marker, status = gui.contain_control_failure(w.ConversationDirectionFailure('pass2', cb.BUDGET_EXCEEDED))
        self.assertIn('Host diagnostic', marker)
        self.assertIn('budget', status)
        self.assertNotIn('could not be validated', status)
        self.assertNotIn('retry your message', marker)

    def test_clark_request_banner_is_truthful_across_every_lead_transition(self):
        """Live 2026-09-20 (session 01M30JMDF3FFJHXFZJA69659ZP): the owner lead was You while 'Clark is asking you
        to lead' (his own earlier request) stayed up. A request is outstanding only until answered: a lead
        control, or the lead already being where it asked. A new turn's request is its own, fresh request."""
        import test_llama_gui_conversation_mode as gui_fixture
        gui, w, calls, _, _, _, _, tmp, trace = gui_fixture.fresh_gui(['llama_gui.py', '--conversation'],
            pass1_value={'act':'ask_human', 'thread':'synthetic', 'direction_request':'request_human', 'relinquish_direction':False},
            pass2_value={'expression':'A complete synthetic response.'})
        gui_fixture.setup_canonical_human_actor(tmp)
        original_chat = w.ollama.chat
        def model(**kwargs):
            response = original_chat(**kwargs)
            response.update(done=True, done_reason='stop', eval_count=100, prompt_eval_count=1000)
            return response
        w.ollama.chat = model
        banner = 'Clark is asking you to lead.'
        self.assertEqual(gui.refresh_conversation_lead_request(), '')
        gui.respond(gui_fixture.NEUTRAL_MESSAGE, [])                 # Clark requests that the person lead; owner unknown
        self.assertIn(banner, gui.refresh_conversation_lead_request())
        for action, owner, label, shown in ((gui.give_direction_to_clark, 'clark', 'Clark', False),
                                            (gui.open_direction, 'unknown', 'Open', False),
                                            (gui.take_direction, 'human', 'You', False)):
            gui.respond(gui_fixture.NEUTRAL_MESSAGE, [])             # a fresh request each turn ...
            self.assertIn('Conversation lead: ' + label, action())   # ... answered by the owner's control
            self.assertEqual(w.get_working_set()['direction_owner'], owner)
            self.assertEqual(gui.refresh_conversation_lead_request(), '')          # never beside the chosen lead
            self.assertIn('Conversation lead: ' + label, gui.refresh_conversation_lead())
        # Lead already with the person: the very same request is moot, not shown, with no click needed.
        gui.respond(gui_fixture.NEUTRAL_MESSAGE, [])
        self.assertEqual(gui.refresh_conversation_lead_request(), '')
        # Open: no directional preference remains and the next turn's request shows again.
        gui.open_direction()
        gui.respond(gui_fixture.NEUTRAL_MESSAGE, [])
        self.assertIn(banner, gui.refresh_conversation_lead_request())

    def test_request_indicator_is_refreshed_by_every_lead_control_click(self):
        source = (Path(__file__).parent / 'llama_gui.py').read_text()
        for control in ('take_direction_btn', 'give_direction_btn', 'open_direction_btn'):
            start = source.index(control + '.click(')
            self.assertIn('refresh_conversation_lead_request', source[start:start + 400])

    def test_trace_write_failure_cannot_change_owner(self):
        import test_llama_gui_conversation_mode as gui_fixture
        gui, w, _, _, _, _, _, tmp, _ = gui_fixture.fresh_gui(['llama_gui.py', '--conversation'])
        gui_fixture.setup_canonical_human_actor(tmp)
        gui.take_direction()
        before = w.get_working_set()
        with patch.object(gui.direction_control, 'record_direction_control_trace', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                gui.open_direction()
        self.assertEqual(w.get_working_set(), before)
        self.assertIn('Conversation lead: You', gui.refresh_conversation_lead())


if __name__ == '__main__':
    unittest.main()
