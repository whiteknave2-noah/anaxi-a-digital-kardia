"""Targeted synthetic Sleep seams; never invokes production defaults or a model."""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import context_budget as cb
import sleep_selection as ss
import sleep_transformation as st
import sleep_cycle as sc
import test_sleep_v1_acceptance as accepted


def wmu(size):
    return {'wmu_id': 'wmu-' + 'a' * 26, 'primary_event_id': 'synthetic-a',
            'selection_bearing': [{'memory_kind': 'clark_expression', 'expression': 'x' * size}]}


class SleepReadinessTests(unittest.TestCase):
    def test_retry_accounting_covers_every_message_in_both_stages(self):
        for compose, budget in ((ss._compose_selection_budget, cb.SLEEP_SELECTION_MAX_PROMPT_BUDGET),
                                (st._compose_transformation_budget, cb.SLEEP_TRANSFORMATION_MAX_PROMPT_BUDGET)):
            result = compose([{'role': 'user', 'content': 'x' * (budget - 5)},
                              {'role': 'user', 'content': 'correction'}])
            self.assertFalse(result.fits)

    def test_whole_unit_packing_reserves_correction_and_bounded_retry(self):
        for module, target, run, empty, compose in (
            (ss, '_selection_chat', ss.select_wmus, {'selected_wmu_ids': []}, ss._compose_selection_budget),
            (st, '_transformation_chat', st.transform, {'derivations': []}, st._compose_transformation_budget)):
            with self.subTest(stage=target):
                seen = []
                def chat(messages):
                    self.assertTrue(compose(messages).fits)
                    seen.append(messages)
                    return 'invalid' if len(seen) == 1 else json.dumps(empty)
                with patch.object(module, target, chat):
                    run([wmu(100)])
                self.assertEqual(len(seen), 2)
                self.assertEqual(len(seen[-1]), 2)

    def test_unpacked_retry_overflow_never_reaches_second_inference(self):
        for module, target, build, compose, invoke, error in (
            (ss, '_selection_chat', lambda w: ss._build_messages([ss._render_candidate(w)], 1, ss.MODE_INITIAL),
             ss._compose_selection_budget, lambda w: ss._call_selection_once([w], 1, ss.MODE_INITIAL), ss.OversizeCandidateError),
            (st, '_transformation_chat', lambda w: st._build_messages([ss._render_candidate(w)], st.MAX_DERIVATIONS_PER_CALL, [w['wmu_id']]),
             st._compose_transformation_budget, lambda w: st._call_transformation_once([w], 0), st.OversizeWmuError)):
            with self.subTest(stage=target):
                size = 0
                while compose(build(wmu(size + 1))).fits:
                    size += 1
                with patch.object(module, target, return_value='invalid') as chat:
                    with self.assertRaises(error):
                        invoke(wmu(size))
                    self.assertEqual(chat.call_count, 1)

    def test_expired_lease_after_transformation_cannot_commit(self):
        conn, _, ids = accepted.fresh_prov_conn('readiness_expiry')
        accepted.add_eligible_turn(conn, ids, 'evt-expiry', 'A synthetic waking sentence.', occurred_at=100)
        conn.commit()
        clock = [1000]
        def transform(_):
            clock[0] = 1400  # exceeds the 300-second renewed lease
            return json.dumps({'derivations': []})
        with patch.object(ss, '_selection_chat', accepted.selection_selects_all), \
             patch.object(st, '_transformation_chat', transform), \
             patch.object(sc.time, 'time', lambda: clock[0]):
            with self.assertRaises(sc.SleepCycleFailure):
                sc.run_sleep_cycle(conn, owner_id='synthetic-expiry')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sleep_cycles').fetchone()[0], 0)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sleep_derivations').fetchone()[0], 0)
        self.assertEqual(accepted.watermark_mod.read_watermark_from_conn(conn)['last_processed_component_id'], 0)
        conn.close()

    def test_existing_positive_pipeline_and_zero_paths(self):
        def check(name, condition):
            self.assertTrue(condition, name)
        accepted.test_full_positive_pipeline(check)
        accepted.test_zero_content_paths(check)
        # Carry the accepted synthetic Landing result through the CURRENT
        # ordinary waking driver, including b2bd1e7 and 8b4c88e.
        from test_owc5_s2_integration import fresh_llama_anaxi, _is_json_format
        retrieval = accepted.hr.retrieve_hippocampal_context(
            os.path.join(accepted.TEST_ROOT, 'full_pipeline', 'anaxi_hippocampus.db'),
            'morning routine attentiveness')
        sleep_only = accepted.hr.RetrievalResult(query_terms=retrieval.query_terms,
            items=tuple(i for i in retrieval.items if i.source_store == 'sleep_derivations'))
        la, calls, _, _, _ = fresh_llama_anaxi(
            pass1_value={'act': 'develop_current', 'thread': 'morning routine',
                         'direction_request': 'none', 'relinquish_direction': False},
            pass2_value={'expression': 'A synthetic waking response.'},
            hippocampal_retrieval_result=sleep_only)
        la.run_waking_turn(la.AnaxiOrchestrator(), 'morning routine attentiveness', interaction_mode='conversation')
        messages = next(c['messages'] for c in calls if not _is_json_format(c['format']))
        self.assertEqual(messages[0]['role'], 'system')
        self.assertIn(accepted.hr.SLEEP_DERIVED_LABEL, messages[0]['content'])
        self.assertIn('"source_store": "sleep_derivations"', messages[0]['content'])
        self.assertEqual(messages[-1], {'role': 'user', 'content': 'morning routine attentiveness'})

    def test_first_run_initializes_schema_only_when_explicitly_called(self):
        with tempfile.TemporaryDirectory(prefix='sleep-first-schema-') as tmp:
            path = os.path.join(tmp, 'anaxi_provenance.db')
            accepted.create_provenance_db(path).close()
            with patch.object(sc.schema.SleepCPaths, 'production_defaults',
                              return_value=sc.schema.SleepCPaths(provenance_db_path=path)), \
                 patch.object(ss, '_selection_chat', side_effect=AssertionError('No model call expected')):
                result = sc.run_sleep_cycle_with_production_defaults(now=1000)
            self.assertEqual(result.status, 'no_work')
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM sleep_cycles').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
            conn.close()


if __name__ == '__main__':
    unittest.main()
