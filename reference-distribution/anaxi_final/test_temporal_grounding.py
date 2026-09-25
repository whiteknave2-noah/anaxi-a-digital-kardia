"""Frozen clocks, synthetic canonical metadata, and captured real pass wiring."""
from contextlib import closing
from datetime import timezone
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import temporal_grounding as tg
import context_budget as cb


class TemporalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'canonical.db')
        self.log = os.path.join(self.tmp.name, 'diagnostics.jsonl')
        self.now = 1788703200
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript('CREATE TABLE events(event_id TEXT,event_type TEXT,occurred_at INTEGER,pipeline_id TEXT); CREATE TABLE event_components(event_id TEXT,sequence INTEGER,component_kind TEXT,component_text TEXT,creator_actor_id TEXT,authorship_resolution TEXT); CREATE TABLE sleep_cycles(started_at INTEGER,completed_at INTEGER);')
        self.h('h0', self.now-3000)
        self.x('x0', 'h0', self.now-2820)
        self.h('current', self.now)

    def h(self, ident, epoch, actor='human'):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('INSERT INTO events VALUES (?,?,?,?)', (ident,'human_waking_input',epoch,'llama'))
            c.execute('INSERT INTO event_components VALUES (?,0,?,?,?,?)', (ident,'human_input','UNREAD_SYNTHETIC_PROSE',actor,'resolved'))

    def x(self, ident, h, epoch):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('INSERT INTO events VALUES (?,?,?,?)', (ident,'waking_turn',epoch,'llama'))
            c.execute('INSERT INTO event_components VALUES (?,1,?,?,?,?)', (ident,'human_input_event_id',h,'host','resolved'))

    def render(self, **kw):
        return tg.snapshot(self.db, 'current', now=self.now, process_started=self.now-100, diagnostics_path=self.log, tz=timezone.utc, **kw)

    def test_short_long_current_and_unknown_boundaries(self):
        text = self.render()
        self.assertIn('47 minutes ago', text)
        self.assertIn(tg.local_time(self.now, timezone.utc), text)
        self.assertIn('Previous waking-process end: not established', text)
        self.assertIn('Dormant interval: not established', text)
        self.assertIn('Sleep occurrence during that interval: not established', text)
        self.assertNotIn('earlier authenticated human inputs', text)
        for seconds, expected in [(40,'less than 2 minutes'),(8280,'2 hours 18 minutes'),(259200,'3 days')]:
            self.assertEqual(tg.elapsed(seconds), expected)
        self.assertIn('not established', tg.elapsed(-1))
        self.assertLess(len(text.encode()),tg.MAX_BLOCK_BYTES)

    def test_incomplete_and_success_clear_interval_without_false_delivery(self):
        self.h('failed', self.now-50)
        text = self.render()
        self.assertIn('1 earlier authenticated human inputs', text)
        self.assertIn('47 minutes ago', text)
        self.assertIn('generation status: not established', text)
        self.x('newx', 'failed', self.now-20)
        text = self.render()
        self.assertNotIn('earlier authenticated human inputs',text)
        self.assertIn('Generated and persisted; delivery not established', text)

    def test_exact_diagnostics_stage_and_continuation(self):
        import ui_turn_diagnostics as d
        self.h('failed', self.now-50)
        def fail():
            d.record_human_input_event_id('failed')
            raise RuntimeError('synthetic host failure')
        with patch.object(d.time, 'time', return_value=self.now-10):
            with self.assertRaises(RuntimeError):
                d.wrap_conversation_callback('raw', [], fail, log_path=self.log)
        self.assertIn('failed before Clark generation began',self.render())
        for stages, expected in [([{'purpose':'pass2','success':False}],'during model generation'),([{'purpose':'pass2','success':True}],'Model generation returned')]:
            with open(self.log,'w') as f:
                f.write(json.dumps(dict(human_input_event_id='failed',timestamp_end=self.now-1,waking_turn_success=False,stages=stages)))
            self.assertIn(expected,self.render())
        with open(self.log,'w') as f:
            f.write(json.dumps(dict(human_input_event_id='current',timestamp_end=self.now-1,waking_turn_success=False,stages=[])))
        self.assertIn('continuation of an earlier incomplete attempt', self.render())
        with open(self.log,'w') as f:
            f.write(json.dumps(dict(timestamp_end=self.now-1,waking_turn_success=False,stages=[])))
        self.assertNotIn('failed before', self.render())
        with open(self.log,'w') as f:
            f.write(json.dumps(dict(human_input_event_id='failed',timestamp_end=self.now-1,waking_turn_success=False)))
        self.assertNotIn('failed before', self.render())

    def test_known_sleep_none_unknown_and_cadence(self):
        start = self.now-100
        end = start-30240
        text = self.render(previous_end=end)
        self.assertIn('8 hours 24 minutes',text)
        self.assertIn('Sleep occurrence during that interval: not established',text)
        self.assertIn('No authorized Sleep cycle',self.render(previous_end=end,sleep_ledger_exhaustive=True))
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('INSERT INTO sleep_cycles VALUES (?,?)',(end+10,start-10))
        self.assertIn('1 authorized Sleep cycle(s) completed',self.render(previous_end=end))
        self.assertNotIn('Sleep',self.render(previous_end=end,lifecycle_notice=False))
        self.assertNotIn('Dormant',self.render(lifecycle_notice=False))

    def test_privacy_scope_and_missing_store(self):
        before = self.render()
        self.h('other',self.now-5,actor='other-human')
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript("CREATE TABLE synthetic_private(payload TEXT); INSERT INTO synthetic_private VALUES ('irrelevant activity'); CREATE TABLE sleep_derivations(payload TEXT);")
        self.assertEqual(before,self.render())
        self.assertNotIn('UNREAD_SYNTHETIC_PROSE', before)
        unknown=tg.snapshot(self.db+'.missing','current',now=self.now,tz=timezone.utc)
        self.assertIn('Earlier incomplete interaction history: not established',unknown)
        self.assertFalse(os.path.exists(self.db+'.missing'))
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("DELETE FROM events WHERE event_type='waking_turn'")
        self.assertIn('No linked canonical human→Clark reply in available history',self.render())

    def test_budget_hard_floor_preserves_protected_pair(self):
        from session_dialogue_window import dialogue_contribution, dialogue_messages
        pair=[{'role':'user','content':'prior human'}, {'role':'assistant','content':'prior reply'}]
        dialogue=dialogue_contribution(pair)
        temporal=cb.Contribution(cb.MECHANICAL_STATE,self.render(),True)
        human=cb.Contribution(cb.CURRENT_HUMAN_MESSAGE,'current human',True)
        for budget in (cb.PASS1_MAX_PROMPT_BUDGET,cb.PASS2_MAX_PROMPT_BUDGET,1):
            result=cb.compose_within_budget([human,temporal,dialogue],budget)
            self.assertEqual(dialogue_messages(dialogue),pair)
            self.assertEqual(result.fits,budget>1)
            self.assertEqual(temporal.rendered_text,self.render())


class PassTests(unittest.TestCase):
    def test_actual_two_pass_snapshot_and_canonical_cadence(self):
        from test_owc5_s2_integration import fresh_llama_anaxi
        w,calls,*_=fresh_llama_anaxi(pass1_value={'act':'develop_current','thread':'','direction_request':'none','relinquish_direction':False},pass2_value={'expression':'Synthetic reply.'})
        w._temporal_process_started=1788703100
        with patch.object(tg.time,'time',return_value=1788703200), patch.object(tg,'snapshot',wraps=tg.snapshot) as snapshot:
            w.run_waking_turn(w.AnaxiOrchestrator(),'Synthetic fresh human.',interaction_mode='conversation')
            self.assertEqual(snapshot.call_count,1)
            self.assertTrue(snapshot.call_args.kwargs['lifecycle_notice'])
            self.assertEqual(len(calls),2)
            blocks=[]
            for call in calls:
                system=call['messages'][0]['content']
                self.assertEqual(system.count('Temporal grounding — host facts:'),1)
                block=system[system.index('Temporal grounding — host facts:'):]
                blocks.append(block.split('\n\n')[0])
                self.assertEqual(call['messages'][-1]['content'],'Synthetic fresh human.')
                if 'num_predict' in call['options']:
                    self.assertLessEqual(call['options']['num_predict'], cb.PASS2_GENERATION_RESERVE)
            self.assertEqual(blocks[0],blocks[1])
            calls.clear()
            w.run_waking_turn(w.AnaxiOrchestrator(),'Next synthetic input.',interaction_mode='conversation')
            self.assertFalse(snapshot.call_args.kwargs['lifecycle_notice'])


if __name__=='__main__':
    unittest.main()
