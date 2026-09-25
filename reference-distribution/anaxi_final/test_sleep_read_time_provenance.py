"""Read-time provenance audit: isolated canonical stores and captured fake inference."""
from contextlib import closing
from dataclasses import replace
import json
import os
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

import context_budget as cb
import hippocampus_store as hs
import hippocampus_retrieval as hr
import sleep_c_schema
import test_human_waking_authority_end_to_end as fixture

TEXT = 'SYNTHETIC cobaltquasar: triangular bells might recur together.'
OPPOSITE = 'SYNTHETIC cobaltquasar: triangular bells might not recur together.'


class ReadTimeProvenanceTests(unittest.TestCase):
    def test_canonical_landing_real_prepare_both_passes_and_repeated_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixture._fresh_env(tmp)
            try:
                import test_owc5_s2_integration as fake_model
                import sleep_cycle
                import orchestration
                w = env['llama_anaxi']
                w.CONVERSATION_DIRECTION_TRACE_PATH = os.path.join(tmp, 'direction.jsonl')
                w.reset_working_set()
                calls = []
                w.ollama = fake_model.build_fake_ollama(calls,
                    {'value':{'act':'develop_current','thread':'','direction_request':'none','relinquish_direction':False}},
                    {'value':{'expression':'SYNTHETIC ordinary expression: the bells are separate.'}})
                w._ollama_environment_cache.update(checked=True, model_digest=fake_model._CALIBRATED_MODEL_DIGEST,
                    server_version=fake_model._CALIBRATED_SERVER_VERSION, chat_template_sha256=fake_model._CALIBRATED_CHAT_TEMPLATE_SHA256)
                # A real canonical synthetic X supplies a mechanically linked WMU.
                original = env['orch'].prepare_context
                def initial_prepare(user, prompt):
                    p = original(user, prompt)
                    core, style = fake_model._calibration_identity_preamble_and_style()
                    p['core_system_text'] = core
                    p['messages'][0]['content'] = core
                    p['controls']['style_instruction'] = style
                    p['messages'].append({'role':'user','content':prompt})
                    return p
                env['orch'].prepare_context = initial_prepare
                source = w.run_waking_turn(env['orch'], 'Synthetic source observation.', interaction_mode=w.CONVERSATION_MODE)
                prov = env['provenance_db_path']
                with closing(sqlite3.connect(prov)) as c:
                    component = c.execute("SELECT component_id FROM event_components WHERE event_id=? AND component_kind='conversational_prose'", (source['native_event_id'],)).fetchone()[0]
                    sleep_c_schema.ensure_schema(c)
                    c.execute("INSERT INTO sleep_v1_lease VALUES (1,'synthetic-audit',1,2000)")
                    c.commit()
                    wmu = {'primary_event_id':source['native_event_id'], 'selection_bearing':[
                        {'event_id':source['native_event_id'], 'component_id':component,
                         'segments':[{'segment_id':'synthetic-segment', 'segment_index':0}]}]}
                    # Invoke only the real atomic canonical writer, not a Sleep cycle.
                    sleep_cycle._commit_successful_cycle(c, owner_id='synthetic-audit', generation=1, now=1000,
                        window_start_component_id=0, window_end_component_id=component, selected_count=1,
                        derivations=[SimpleNamespace(source_wmu_ids=['wmu-synthetic-audit'], derived_text=text,
                                                     batch_index=0, item_index=i) for i,text in enumerate((TEXT,OPPOSITE))],
                        id_to_wmu={'wmu-synthetic-audit':wmu})
                    canonical_before = c.execute('SELECT * FROM sleep_derivations ORDER BY derivation_id').fetchall()
                    links_before = c.execute('SELECT * FROM sleep_derivation_sources ORDER BY derivation_id').fetchall()
                    self.assertEqual(len(links_before), 2)
                    self.assertEqual({r[5] for r in links_before}, {component})
                hip = os.path.join(tmp, 'hip.db')
                paths = hs.HippocampusPaths(prov, w.RELATIONAL_DB_PATH, w.LOG_FILE, hip)
                hs.rebuild_hippocampus(hip, paths)
                before = hs.fetch_logical_rows(hip)
                snapshots = []
                for _ in range(3):
                    result = hr.retrieve_hippocampal_context(hip, 'cobaltquasar')
                    snapshots.append(result)
                    self.assertEqual({i.content for i in result.items}, {TEXT, OPPOSITE})
                    self.assertTrue(all(i.memory_kind == 'derived_inference' and i.source_store == 'sleep_derivations' for i in result.items))
                    self.assertEqual(hr.render_hippocampal_context(result).count(hr.SLEEP_DERIVED_LABEL), 2)
                self.assertEqual(snapshots[0], snapshots[1])
                self.assertEqual(snapshots[1], snapshots[2])
                self.assertEqual(before, hs.fetch_logical_rows(hip))
                ordinary = hr.retrieve_hippocampal_context(hip, 'separate')
                self.assertTrue(ordinary.items)
                self.assertNotIn(hr.SLEEP_DERIVED_LABEL, hr.render_hippocampal_context(ordinary))

                # Restore the REAL production prepare_context seam, using only
                # synthetic mind inputs and isolated hippocampal/source paths.
                env['orch'].hippocampus_paths = paths
                env['mind'].retrieve_waking_context = lambda *args: ''
                env['mind'].get_current_kardia = lambda *args: dict(fake_model._CALIBRATION_REFERENCE_KARDIA)
                env['orch'].prepare_context = orchestration.AnaxiOrchestrator.prepare_context.__get__(env['orch'])
                calls.clear()
                w.run_waking_turn(env['orch'], 'Synthetic cobaltquasar inquiry.', interaction_mode=w.CONVERSATION_MODE)
                # Exclude bounded cost probes (baseline system message): measurements, not generation.
                calls[:] = [c for c in calls if c['messages'][0]['content'] != 'You are a helpful assistant.']
                self.assertEqual(len(calls), 2)
                p1, p2 = calls
                p1_text = '\n'.join(m['content'] for m in p1['messages'])
                p2_text = '\n'.join(m['content'] for m in p2['messages'])
                # Frozen Pass 1 is direction-only: no retrieved memory at all.
                self.assertNotIn(TEXT, p1_text)
                self.assertNotIn(OPPOSITE, p1_text)
                self.assertNotIn(hr.CONTEXT_HEADING, p1_text)
                # Every admitted item retains its marker, source identity and kind.
                #
                # META-RESPONSE LEAK repair (production incident, 2026-09-22): d8a0f9d reworded
                # PASS2_TASK_INSTRUCTION/PASS2_IMMUTABLE_TASK_MESSAGE_CLOSING_CUE (fixing a real
                # description-vs-speech ambiguity a live turn fell into), which invalidated the
                # OLD scaffold calibration fingerprint by design and briefly forced MECHANICAL_
                # STATE onto the safe-but-conservative byte estimator, shrinking real Pass-2
                # headroom enough to admit only 1 of these 2 synthetic RETRIEVED_HISTORY items. A
                # fresh, real-model calibration assay against the new exact scaffold text (see
                # llama_anaxi._PASS2_SCAFFOLD_CALIBRATION's own comment) restored -- and slightly
                # improved on -- the original headroom (153 calibrated tokens vs the old 167), so
                # both items are admitted together again, same as before the wording repair.
                system = p2['messages'][0]['content']
                admitted = [json.loads(line) for line in system.splitlines()
                            if line.startswith('{') and '"source_store": "sleep_derivations"' in line]
                self.assertEqual(len(admitted), 2)
                self.assertEqual(p2_text.count(hr.SLEEP_DERIVED_LABEL), len(admitted))
                self.assertTrue(all(i['memory_kind']=='derived_inference' and i['event_id']==i['source_locator'] for i in admitted))
                self.assertTrue(all(i['content'] in (TEXT, OPPOSITE) for i in admitted))
                self.assertIn(hr.EPISTEMIC_STATEMENT, system)
                self.assertEqual(p2['options']['num_predict'], cb.PASS2_GENERATION_RESERVE)
                # Repeated waking delivery cannot disposition the canonical
                # derivation. New ordinary X rows may change corpus statistics;
                # equivalent read-only ranking was checked separately above.
                for _ in range(2):
                    calls.clear()
                    # Equivalent context capacity, rather than accumulating
                    # optional dialogue until ordinary OWC9 eviction applies.
                    w._native_session_state.update(session_id=None,session_started_at=None)
                    w.reset_working_set()
                    w.run_waking_turn(env['orch'], 'Synthetic cobaltquasar inquiry.', interaction_mode=w.CONVERSATION_MODE)
                    text = calls[1]['messages'][0]['content']
                    delivered = [json.loads(line) for line in text.splitlines()
                                 if line.startswith('{') and '"source_store": "sleep_derivations"' in line]
                    # New ordinary X rows legitimately change ranking. Any
                    # derivation still admitted must retain its epistemic
                    # marker and provenance as an indivisible unit; perpetual
                    # top-N residency is not a production invariant.
                    self.assertEqual(text.count(hr.SLEEP_DERIVED_LABEL),len(delivered))
                    self.assertTrue(all(i['memory_kind']=='derived_inference' for i in delivered))
                with closing(sqlite3.connect(prov)) as c:
                    self.assertEqual(canonical_before, c.execute('SELECT * FROM sleep_derivations ORDER BY derivation_id').fetchall())
                    self.assertEqual(links_before, c.execute('SELECT * FROM sleep_derivation_sources ORDER BY derivation_id').fetchall())
                print('PASS1: no retrieved-history block (direction-only).')
                actual_lines = [line for line in system.splitlines() if line in
                    (hr.CONTEXT_HEADING,hr.EPISTEMIC_STATEMENT,hr.SLEEP_DERIVED_LABEL)
                    or (line.startswith('{') and '"source_store": "sleep_derivations"' in line)]
                print('PASS2 captured synthetic representation:\n' + '\n'.join(actual_lines))
            finally:
                fixture._teardown_env(env)

    def test_truncation_and_owc9_refill_keep_marker_attached_or_drop_whole_item(self):
        item = hr.RetrievedMemory('synthetic-item','synthetic-derivation',1000,'sleep_derivations',
            'synthetic-derivation','derived_inference','unknown','unknown',None,None,None,None,'unknown',None,
            TEXT,False,hr.RETRIEVAL_METHOD,-1.0,('cobaltquasar',))
        # Character clipping precedes rendering: epistemic fields are not clipped.
        row = {'content_text':TEXT}
        bounded = hr._select_bounded_items([row],1,18,18)
        clipped = replace(item, content=bounded[0][1], content_truncated=bounded[0][2])
        def render(units):
            return hr.render_hippocampal_context(hr.RetrievalResult(('cobaltquasar',),tuple(units))) if units else ''
        text = render([clipped])
        self.assertIn(hr.SLEEP_DERIVED_LABEL, text)
        self.assertIn('"content_truncated": true', text)
        for budget, fits in ((len(text.encode('utf-8')),True),(20,False)):
            contribution = cb.Contribution(cb.RETRIEVED_HISTORY,text,False,droppable_units=[clipped],render_fn=render,
                                           source_ids=[clipped.event_id])
            result = cb.compose_within_budget([contribution],budget,refill_unused_capacity=True)
            self.assertTrue(result.fits)
            retained = result.included_kind(cb.RETRIEVED_HISTORY)
            self.assertEqual(retained is not None, fits)
            if retained:
                self.assertEqual(retained.rendered_text,text)
                self.assertEqual(result.delivered_source_ids(cb.RETRIEVED_HISTORY),[clipped.event_id])
        for kind in ('clark_expression','derived_inference'):
            ordinary = replace(item,source_store='event_components',memory_kind=kind)
            self.assertNotIn(hr.SLEEP_DERIVED_LABEL,render([ordinary]))


if __name__ == '__main__':
    unittest.main()
