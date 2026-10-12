"""Synthetic receipts and ordinary waking transport; no production data/model calls."""
import os
import sqlite3
import unittest

import context_budget as cb
import workspace_episode_provenance as wep
import workspace_public_continuity as wpc
import native_provenance_writer as real_npw
from provenance_schema import create_provenance_db, derive_stable_id
from migrate_historical_data import build_pipeline_map, seed_reference_data


class ContinuityCapacityTests(unittest.TestCase):
    def test_refill_preserves_priority_budget_and_exact_delivery_ids(self):
        def compose(refill):
            units = [{'event_id': 'old', 'text': 'a' * 100}, {'event_id': 'new', 'text': 'b' * 100}]
            render = lambda us: ''.join(u['text'] for u in us)
            return cb.compose_within_budget([
                cb.Contribution(cb.CORE_SYSTEM_CONTROL, 'h' * 100, True),
                cb.Contribution(cb.ACTIVE_WORKSPACE_CONTINUITY, render(units), False,
                                units, render, ['old', 'new']),
                cb.Contribution(cb.RECENT_DIALOGUE, 'd' * 400, False,
                                ['d' * 400], lambda us: ''.join(us)),
            ], 250, refill_unused_capacity=refill)
        before = compose(False)
        self.assertIsNone(before.included_kind(cb.ACTIVE_WORKSPACE_CONTINUITY))
        after = compose(True)
        self.assertTrue(after.fits)
        self.assertEqual(after.final_prompt_cost, 200)
        self.assertEqual(after.delivered_source_ids(cb.ACTIVE_WORKSPACE_CONTINUITY), ['new'])
        self.assertIn(cb.ACTIVE_WORKSPACE_CONTINUITY, after.trimmed_kinds)

    def test_partial_trim_acknowledges_only_surviving_source(self):
        c = cb.Contribution(cb.ACTIVE_WORKSPACE_CONTINUITY, 'aabb', False,
                            ['aa', 'bb'], lambda us: ''.join(us), ['old', 'new'])
        result = cb.compose_within_budget([c], 2)
        self.assertEqual(result.delivered_source_ids(cb.ACTIVE_WORKSPACE_CONTINUITY), ['new'])

    def test_synthetic_receipt_reaches_actual_ordinary_ollama_messages(self):
        # Existing harness replaces heavy dependencies before importing the
        # real waking driver and redirects every runtime output to temp paths.
        from test_owc5_s2_integration import fresh_llama_anaxi, _is_json_format
        la, calls, _, _, _ = fresh_llama_anaxi(
            pass1_value={'act': 'develop_current', 'thread': 'recent activity',
                         'direction_request': 'none', 'relinquish_direction': False},
            pass2_value={'expression': 'A synthetic response.'})
        la.native_provenance_writer = real_npw
        path = os.path.join(la.PROVENANCE_DB_DIR, 'anaxi_provenance.db')
        create_provenance_db(path).close()
        conn = sqlite3.connect(path)
        manifest = {'pipelines': {k: {'routing_constant_value': 'synthetic_user'} for k in ('llama', 'claude')}}
        seed_reference_data(conn, build_pipeline_map(manifest), 1000)
        conn.close()
        run_id = wep.generate_episode_run_id()
        actor = derive_stable_id('actor', 'clark')
        wep.record_episode_started(la.PROVENANCE_DB_DIR, run_id=run_id,
            clark_actor_id=actor, pipeline_key=la.PIPELINE_KEY, occurred_at=1001)
        wep.record_public_action(la.PROVENANCE_DB_DIR, run_id=run_id,
            clark_actor_id=actor, pipeline_key=la.PIPELINE_KEY, occurred_at=1002,
            resource_class='library', action='list', relative_path='', success=True,
            action_id_backlink='synthetic-action', model_revision_id=None)
        # Force the exact failure shape: an oversized, indivisible dialogue
        # unit is dropped AFTER public continuity. Real compositor/assembly run.
        original_compose = cb.compose_within_budget
        observations = []
        def compose(contributions, budget, **kwargs):
            if kwargs.get('refill_unused_capacity'):
                dialogue = next(c for c in contributions if c.kind == cb.RECENT_DIALOGUE)
                dialogue.droppable_units = [[{'role': 'assistant', 'content': 'x' * 5000}]]
                dialogue.rendered_text = 'x' * 5000
                dialogue.cost = 5000
            result = original_compose(contributions, budget, **kwargs)
            if kwargs.get('refill_unused_capacity'):
                observations.append(result)
            return result
        from unittest.mock import patch
        with patch.object(cb, 'compose_within_budget', compose):
            la.run_waking_turn(la.AnaxiOrchestrator(), 'How was the interval?', interaction_mode='conversation')
        sent = next(c for c in calls if not _is_json_format(c['format']) and c['messages'][0]['content'] != 'You are a helpful assistant.')
        self.assertEqual(sent['model'], 'gemma4:e4b')
        system = sent['messages'][0]
        self.assertEqual(system['role'], 'system')
        self.assertIn('Your recent public Space activity', system['content'])
        self.assertIn('usable without a body', system['content'])
        self.assertIn('availability alone does not prove use', system['content'])
        self.assertIn('library.list: ok', system['content'])
        self.assertIn("not Alex's speech or instructions", system['content'])
        self.assertEqual(sent['messages'][-1], {'role': 'user', 'content': 'How was the interval?'})
        self.assertTrue(observations[0].fits)
        self.assertIn(cb.RECENT_DIALOGUE, observations[0].dropped_kinds)
        self.assertTrue(observations[0].delivered_source_ids(cb.ACTIVE_WORKSPACE_CONTINUITY))
        self.assertEqual(
            wpc.find_delivered_public_event_ids(la.PROVENANCE_DB_DIR),
            set(observations[0].delivered_source_ids(cb.ACTIVE_WORKSPACE_CONTINUITY)))

    def test_refill_never_displaces_surviving_higher_priority_context(self):
        result = cb.compose_within_budget([
            cb.Contribution(cb.RECENT_DIALOGUE, 'd' * 200, False),
            cb.Contribution(cb.ACTIVE_WORKSPACE_CONTINUITY, 'a' * 100, False),
        ], 250, refill_unused_capacity=True)
        self.assertEqual(result.included_kind(cb.RECENT_DIALOGUE).rendered_text, 'd' * 200)
        self.assertIsNone(result.included_kind(cb.ACTIVE_WORKSPACE_CONTINUITY))
        overflow = cb.compose_within_budget([
            cb.Contribution(cb.CORE_SYSTEM_CONTROL, 'h' * 300, True),
        ], 250, refill_unused_capacity=True)
        self.assertFalse(overflow.fits)


if __name__ == '__main__':
    unittest.main()
