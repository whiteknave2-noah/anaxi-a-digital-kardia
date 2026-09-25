"""Synthetic source-window boundaries; no production DB or model calls."""
import hashlib
import json
import unittest
from unittest.mock import patch

from provenance_schema import create_provenance_db
import sleep_cycle as sc
import sleep_selection as ss
import waking_material_unit as wm


class WindowIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.c = create_provenance_db(':memory:')
        self.addCleanup(self.c.close)
        c = self.c
        c.execute("INSERT INTO pipelines VALUES ('p','anaxi_orchestration_lineage_a','llama','test')")
        for actor, kind in [('host', 'host_system'), ('clark', 'clark_agent'), ('human', 'human_person')]:
            c.execute("INSERT INTO actors (actor_id,actor_type,stable_key,created_at) VALUES (?, ?, ?, 1)", (actor, kind, actor))
        c.execute("INSERT INTO actor_host_system (actor_id,subsystem_key) VALUES ('host','test')")
        c.execute("INSERT INTO actor_clark_agent (actor_id,canonical_key) VALUES ('clark','clark')")
        c.execute("INSERT INTO persons (person_id,created_at) VALUES ('person',1)")
        c.execute("INSERT INTO actor_human_person (actor_id,person_id,canonical_name,relationship_established_at) VALUES ('human','person','Human',1)")
        c.execute("INSERT INTO sessions (session_id,pipeline_id,started_at) VALUES ('s','p',1)")
        c.execute("INSERT INTO auth_contexts (auth_context_id,session_id,auth_state,established_at) VALUES ('u','s','unknown',1)")
        c.execute("INSERT INTO auth_contexts (auth_context_id,session_id,authenticated_actor_id,auth_state,auth_method,assurance_level,established_at) VALUES ('a','s','human','authenticated','local_session_start_binding','low',1)")

    def event(self, eid, typ='waking_turn'):
        self.c.execute("INSERT INTO events (event_id,event_type,pipeline_id,pipeline_provenance_status,auth_context_id,input_source_ref,occurred_at,record_created_at) VALUES (?,?,'p','known',?,?,1,1)",
                       (eid, typ, 'a' if typ == 'human_waking_input' else 'u', eid))

    def component(self, eid, text, actor='clark', kind='conversational_prose'):
        seq = self.c.execute('SELECT COUNT(*) FROM event_components WHERE event_id=?', (eid,)).fetchone()[0]
        return self.c.execute("INSERT INTO event_components (event_id,sequence,creator_actor_id,component_kind,authorship_resolution,component_text,content_sha256) VALUES (?,?,?,?,'resolved',?,?)",
                              (eid, seq, actor, kind, text, hashlib.sha256(text.encode()).hexdigest())).lastrowid

    def pair(self, suffix=''):
        h, x = 'H' + suffix, 'X' + suffix
        self.event(h, 'human_waking_input')
        hc = self.component(h, 'Kit and Remy chose Clark/Kara and stickers for the host machine.', 'human', 'human_conversational_input')
        self.event(x)
        xc = self.component(x, 'The stickers sound colorful.')
        self.component(x, h, 'host', 'human_input_event_id')
        return hc, xc

    def gather(self, after=0, through=None, **kw):
        if through is None:
            through = sc._read_max_component_id(self.c)
        return sc.gather_window_evidence(self.c, after_component_id=after, through_component_id=through, max_segments=1, **kw)

    def test_boundary_preserves_pair_and_model_facing_roles(self):
        self.event('prior'); self.component('prior', 'Earlier synthetic turn.')
        hc, xc = self.pair()
        items, end = self.gather()
        self.assertEqual(end, xc)
        unit = next(w for w in wm.assemble_wmus(self.c, items) if w['primary_event_id'] == 'H')
        self.assertEqual([e['component_id'] for e in unit['selection_bearing']], [hc, xc])
        rendered = ss._render_candidate(unit)
        self.assertIn('[authenticated human expression]', rendered)
        self.assertIn('Kit and Remy chose Clark/Kara', rendered)
        self.assertIn('[Clark expression]', rendered)
        self.assertEqual([e['event_id'] for e in unit['selection_bearing']], ['H', 'X'])
        with patch.object(ss, '_selection_chat', return_value=json.dumps({'selected_wmu_ids': [unit['wmu_id']]})) as chat:
            self.assertEqual(ss.select_wmus([unit]).selected_wmu_ids, [unit['wmu_id']])
            self.assertIn(rendered, chat.call_args.args[0][0]['content'])

    def test_segmented_human_is_never_delivered_without_its_existing_answer(self):
        hc, xc = self.pair()
        items, end = self.gather(max_chars_per_segment=9)
        unit, = wm.assemble_wmus(self.c, items)
        self.assertEqual(end, xc)
        self.assertEqual([e['component_id'] for e in unit['selection_bearing']], [hc, xc])
        self.assertGreater(len(unit['selection_bearing'][0]['segments']), 1)

    def test_closure_finishes_interleaved_units_and_delivered_resource(self):
        self.event('H', 'human_waking_input')
        self.component('H', 'Human introduction.', 'human', 'human_conversational_input')
        h2, x2 = self.pair('2')
        self.event('X'); self.component('X', 'First answer.')
        self.component('X', 'H', 'host', 'human_input_event_id')
        self.event('Y', 'workspace_resource_encounter')
        text = 'Actually delivered synthetic library text.'
        fact = dict(resource_class='library', relative_path='notes.txt', modality='text', delivered_portion='all',
                    content_sha256=hashlib.sha256(text.encode()).hexdigest(), source_action='read',
                    target_model_pathway='gemma4:e4b', genuinely_delivered=True)
        self.component('Y', json.dumps(fact), 'host', 'resource_encounter_fact')
        yc = self.component('Y', text, 'host', 'resource_encounter_delivered_text')
        self.component('Y', 'X', 'host', 'triggering_waking_event_id')
        items, end = self.gather()
        self.assertEqual(end, yc)
        units = {w['primary_event_id']: w for w in wm.assemble_wmus(self.c, items)}
        self.assertEqual([e['event_id'] for e in units['H']['selection_bearing']], ['H', 'X', 'Y'])
        self.assertEqual([e['component_id'] for e in units['H2']['selection_bearing']], [h2, x2])

    def test_snapshot_excludes_later_answer_and_processed_source_is_not_replayed(self):
        hc, xc = self.pair()
        items, end = self.gather(through=hc)
        self.assertEqual(end, hc)
        self.assertEqual([i['event_id'] for i in items], ['H'])
        with self.assertRaisesRegex(sc.SleepCycleFailure, 'crosses the processed watermark'):
            self.gather(after=hc)

    def test_oversize_whole_pair_fails_before_inference_without_clipping(self):
        hc, xc = self.pair()
        self.component('H', 'Large but exact human expression. ' * 300, 'human', 'human_conversational_input')
        items, end = self.gather()
        unit, = wm.assemble_wmus(self.c, items)
        self.assertEqual(len(unit['selection_bearing']), 3)
        with patch.object(ss, '_selection_chat', side_effect=AssertionError('no inference')) as chat:
            with self.assertRaises(ss.OversizeCandidateError):
                ss.select_wmus([unit])
            chat.assert_not_called()

    def test_late_answer_fails_before_selection_and_leaves_watermark_unchanged(self):
        self.event('H', 'human_waking_input')
        hc = self.component('H', 'An unanswered human address.', 'human', 'human_conversational_input')
        self.c.isolation_level = None
        with patch.object(ss, '_selection_chat', return_value='{"selected_wmu_ids": []}'):
            first = sc.run_sleep_cycle(self.c, owner_id='first', now=100)
        self.assertEqual(first.window_end_component_id, hc)
        self.event('X'); self.component('X', 'A later answer.')
        self.component('X', 'H', 'host', 'human_input_event_id')
        before = sc.watermark_mod.read_watermark_from_conn(self.c)
        with patch.object(ss, '_selection_chat', side_effect=AssertionError('no inference')) as chat:
            with self.assertRaisesRegex(sc.SleepCycleFailure, 'crosses the processed watermark'):
                sc.run_sleep_cycle(self.c, owner_id='second', now=101)
            chat.assert_not_called()
        self.assertEqual(sc.watermark_mod.read_watermark_from_conn(self.c), before)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM sleep_cycles').fetchone()[0], 1)

    def test_later_misattribution_remains_a_separate_clark_occurrence(self):
        self.pair()
        self.event('H2', 'human_waking_input')
        self.component('H2', 'You asked me about those stickers.', 'human', 'human_conversational_input')
        self.event('X2')
        mistaken = 'I originally introduced the stickers.'
        self.component('X2', mistaken)
        self.component('X2', 'H2', 'host', 'human_input_event_id')
        items, end = sc.gather_window_evidence(self.c, after_component_id=0,
                                              through_component_id=sc._read_max_component_id(self.c))
        units = {w['primary_event_id']: w for w in wm.assemble_wmus(self.c, items)}
        self.assertEqual(set(units), {'H', 'H2'})
        self.assertIn('Kit and Remy', units['H']['selection_bearing'][0]['expression'])
        self.assertEqual(units['H2']['selection_bearing'][1]['expression'], mistaken)
        self.assertEqual(units['H2']['selection_bearing'][1]['memory_kind'], 'clark_expression')


if __name__ == '__main__':
    unittest.main()
