"""Bounded synthetic ordinary waking completion and projection proof.

No real inference, production databases, production H/X replay or private Space.
"""
import os
import sqlite3
import unittest

import context_budget as cb
import native_provenance_writer as real_npw
from conversation_projection import project_waking_conversation
from provenance_schema import create_provenance_db
from migrate_historical_data import build_pipeline_map, seed_reference_data
from test_owc5_s2_integration import fresh_llama_anaxi, _is_json_format


class CompletionBoundaryTests(unittest.TestCase):
    def fixture(self, prose, metadata, *, completion_marker=True, pass1_value=None):
        la, calls, _, _, _ = fresh_llama_anaxi(
            pass1_value=pass1_value or {
                'act': 'develop_current', 'thread': 'family conversation',
                'direction_request': 'none', 'relinquish_direction': False,
            },
            pass2_value={'expression': prose} if completion_marker else prose)
        la.native_provenance_writer = real_npw
        db = os.path.join(la.PROVENANCE_DB_DIR, 'anaxi_provenance.db')
        create_provenance_db(db).close()
        conn = sqlite3.connect(db)
        manifest = {'pipelines': {k: {'routing_constant_value': 'synthetic_user'}
                                 for k in ('llama', 'claude')}}
        seed_reference_data(conn, build_pipeline_map(manifest), 1000)
        conn.close()
        fake_chat = la.ollama.chat
        def chat(**kwargs):
            response = fake_chat(**kwargs)
            if not _is_json_format(kwargs.get('format')):
                response.update(metadata)
            return response
        la.ollama.chat = chat
        return la, calls, db

    def run_turn(self, la):
        return la.run_waking_turn(la.AnaxiOrchestrator(),
                                 'A short synthetic family message.', interaction_mode='conversation')

    def test_longer_complete_reply_survives_canonical_and_ui_projection(self):
        prose = ('The children chose their own designs, and each choice made the afternoon memorable. ' * 20).strip()
        la, calls, db = self.fixture(prose, {'done': True, 'done_reason': 'stop', 'eval_count': 450})
        result = self.run_turn(la)
        self.assertEqual(result['reply'], prose)
        self.assertEqual(project_waking_conversation(db)[-1]['content'], prose)
        pass1_call = next(c for c in calls if _is_json_format(c['format']))
        call = next(c for c in calls if not _is_json_format(c['format']))
        self.assertEqual(pass1_call['options']['num_predict'], cb.PASS1_GENERATION_RESERVE)
        self.assertEqual(pass1_call['options']['num_ctx'], cb.CONTEXT_CEILING)
        self.assertEqual(call['options']['num_predict'], cb.PASS2_GENERATION_RESERVE)
        self.assertEqual(call['options']['num_ctx'], cb.CONTEXT_CEILING)
        # SHARED ORDINARY EXPRESSION SEAM: a plain chat completion -- no grammar container.
        self.assertIsNone(call['format'])
        self.assertEqual(call['messages'][-1]['content'], 'A short synthetic family message.')
        self.assertNotIn(la.PASS2_COMPLETION_MARKER, result['reply'])

    def test_a_normal_stop_is_the_completed_reply_nothing_typed_back(self):
        # SHARED ORDINARY EXPRESSION SEAM: completion is the provider's own stop within the decode
        # reserve; no model-typed marker is required, and nothing inside the words is judged.
        prose = 'A synthetic reply begins. And then we have'
        la, calls, db = self.fixture(prose, {'done': True, 'done_reason': 'stop', 'eval_count': 73})
        self.assertEqual(self.run_turn(la)['reply'], prose)
        self.assertEqual(project_waking_conversation(db)[-1]['content'], prose)
        self.assertEqual(len(calls), 2)

    def test_a_blank_normal_stop_is_expression_not_established(self):
        la, calls, db = self.fixture(
            '   ', {'done': True, 'done_reason': 'stop', 'eval_count': 2}, completion_marker=False)
        with self.assertRaises(la.ConversationDirectionFailure) as caught:
            self.run_turn(la)
        self.assertEqual(caught.exception.failure_code, 'EMPTY_EXPRESSION')
        self.assertEqual(project_waking_conversation(db), [])
        self.assertEqual(len(calls), 2)  # no continuation/retry hidden in the turn

    def test_a_reply_cannot_hide_exhaustion_or_interruption(self):
        cases = [
            ({'done': True, 'done_reason': 'length', 'eval_count': 768}, 'COMPLETION_LIMIT_REACHED'),
            ({'done': False, 'done_reason': 'stop', 'eval_count': 73}, 'INCOMPLETE_MODEL_COMPLETION'),
            ({'done': True, 'done_reason': None, 'eval_count': 73}, 'INCOMPLETE_MODEL_COMPLETION'),
            ({'done': True, 'done_reason': 'stop', 'eval_count': 769}, 'COMPLETION_LIMIT_EXCEEDED'),
        ]
        for metadata, code in cases:
            with self.subTest(metadata=metadata):
                la, calls, db = self.fixture('A reply, but not a completed transport.', metadata)
                with self.assertRaises(la.ConversationDirectionFailure) as caught:
                    self.run_turn(la)
                self.assertEqual(caught.exception.failure_code, code)
                self.assertEqual(project_waking_conversation(db), [])
                self.assertEqual(len(calls), 2)  # one Pass 1, one Pass 2; no retry
                self.assertEqual(la.get_last_conversation_direction_trace()['pass2_status'], code)

    def test_pass1_structured_output_cannot_hide_length_exhaustion(self):
        la, calls, db = self.fixture('Pass 2 must never run.', {})
        fake_chat = la.ollama.chat

        def length_limited_pass1(**kwargs):
            response = fake_chat(**kwargs)
            if _is_json_format(kwargs.get('format')):
                response.update(done=True, done_reason='length',
                                eval_count=cb.PASS1_GENERATION_RESERVE)
            return response

        la.ollama.chat = length_limited_pass1
        with self.assertRaises(la.ConversationDirectionFailure) as caught:
            self.run_turn(la)
        self.assertEqual(caught.exception.failure_code, 'COMPLETION_LIMIT_REACHED')
        self.assertEqual(caught.exception.stage, 'pass1')
        self.assertEqual(project_waking_conversation(db), [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['options']['num_predict'], cb.PASS1_GENERATION_RESERVE)
        self.assertEqual(calls[0]['options']['num_ctx'], cb.CONTEXT_CEILING)

    def test_incomplete_reply_cannot_duplicate_deferred_external_effect(self):
        pass1 = {
            'act': 'develop_current', 'thread': 'family conversation',
            'direction_request': 'none', 'relinquish_direction': False,
            'external_info_request': 'web_search',
            'external_info_target': 'synthetic query that must not dispatch',
        }
        la, calls, db = self.fixture(
            '   ',   # expression not established (blank completion)
            {'done': True, 'done_reason': 'stop', 'eval_count': 40},
            completion_marker=False, pass1_value=pass1,
        )
        side_effect_calls = []
        # The request is performed before Pass 2 (so a reply can be composed with its outcome) but is
        # RECORDED only after the turn commits: an incomplete reply persists nothing. Hermetic fake.
        original_search = la.external_information.net.search_public
        self.addCleanup(setattr, la.external_information.net, 'search_public', original_search)
        la.external_information.net.search_public = lambda q, **k: {
            'status': 'success', 'detail': None, 'provider': 'wikipedia_search', 'results': []}
        original_dispatch = la.external_information.record_external_info_query_and_result
        self.addCleanup(
            setattr, la.external_information, 'record_external_info_query_and_result', original_dispatch,
        )
        la.external_information.record_external_info_query_and_result = (
            lambda *args, **kwargs: side_effect_calls.append((args, kwargs))
        )
        with self.assertRaises(la.ConversationDirectionFailure):
            self.run_turn(la)
        self.assertEqual(side_effect_calls, [])
        self.assertEqual(project_waking_conversation(db), [])
        self.assertEqual(len(calls), 2)

    def test_previous_observation_only_boundary_accepts_length_limited_json(self):
        prose = 'A syntactically valid envelope can contain an unfinished'
        la, _, db = self.fixture(prose, {'done': True, 'done_reason': 'length', 'eval_count': 768})
        call = la.call_llama
        def previous_boundary(*args, **kwargs):
            # Reproduce the previous driver: it supplied no generation reserve,
            # so completion metadata was recorded but not used for admission.
            kwargs.pop('generation_reserve', None)
            return call(*args, **kwargs)
        la.call_llama = previous_boundary
        self.assertEqual(self.run_turn(la)['reply'], prose)
        self.assertEqual(project_waking_conversation(db)[-1]['content'], prose)

    def test_genuinely_excessive_generation_has_a_finite_request_bound(self):
        # A deterministic backend models demand above the allowance, emits
        # only the requested number of tokens, and reports length exhaustion.
        la, _, db = self.fixture('Unused.', {})
        fake_chat = la.ollama.chat
        generated = []
        def capped_chat(**kwargs):
            if _is_json_format(kwargs.get('format')):
                return fake_chat(**kwargs)
            bound = kwargs['options']['num_predict']
            self.assertEqual(bound, cb.PASS2_GENERATION_RESERVE)
            generated.extend(range(min(10_000, bound)))
            return {'message': {'content': '{"expression":"bounded partial"}'},
                    'done': True, 'done_reason': 'length', 'eval_count': len(generated)}
        la.ollama.chat = capped_chat
        with self.assertRaises(la.ConversationDirectionFailure):
            self.run_turn(la)
        self.assertEqual(len(generated), 768)
        self.assertEqual(project_waking_conversation(db), [])

    def test_normal_stop_exactly_at_reserve_is_allowed(self):
        prose = 'A complete response that legitimately lands at the configured boundary.'
        la, _, db = self.fixture(
            prose, {'done': True, 'done_reason': 'stop', 'eval_count': cb.PASS2_GENERATION_RESERVE},
        )
        self.assertEqual(self.run_turn(la)['reply'], prose)
        self.assertEqual(project_waking_conversation(db)[-1]['content'], prose)


if __name__ == '__main__':
    unittest.main()
