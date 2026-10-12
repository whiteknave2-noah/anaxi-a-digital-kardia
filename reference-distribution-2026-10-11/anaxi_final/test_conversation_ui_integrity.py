"""Synthetic UI projection, canonical times, and raw submission boundary."""
from datetime import datetime, timedelta, timezone
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import conversation_display as display
import conversation_projection as projection
import ui_turn_diagnostics as diagnostics


def seed_projection_db(path):
    with closing(sqlite3.connect(path)) as c, c:
        c.executescript('''
            CREATE TABLE events (event_id TEXT, event_type TEXT, occurred_at INTEGER);
            CREATE TABLE event_components (event_id TEXT, sequence INTEGER, component_kind TEXT,
                                           component_text TEXT, authorship_resolution TEXT);
        ''')


def add_event(path, event_id, role, text, stamp, linked_h=None):
    with closing(sqlite3.connect(path)) as c, c:
        c.execute('INSERT INTO events VALUES (?,?,?)',
                  (event_id,'human_waking_input' if role=='user' else 'waking_turn',stamp))
        c.execute('INSERT INTO event_components VALUES (?,?,?,?,?)',
                  (event_id,0,'human_conversational_input' if role=='user' else 'conversational_prose',text,'resolved'))
        if linked_h:
            c.execute('INSERT INTO event_components VALUES (?,?,?,?,?)',
                      (event_id,1,'human_input_event_id',linked_h,'resolved'))


class ProjectionTests(unittest.TestCase):
    def test_delayed_recovered_x_is_projected_directly_beneath_its_h(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'anaxi_provenance.db')
            seed_projection_db(db)
            add_event(db, 'h-recovered', 'user', 'Recovered H', 1000)
            add_event(db, 'h-later', 'user', 'Later H', 2000)
            add_event(db, 'x-later', 'assistant', 'Later X', 2001, 'h-later')
            # Persistence occurs later, but the canonical link—not wall-clock
            # adjacency—determines the conversation layout.
            add_event(db, 'x-recovered', 'assistant', 'Recovered X', 3000, 'h-recovered')
            rows = projection.project_waking_conversation(db, include_event_metadata=True)
            self.assertEqual(
                [row['event_id'] for row in rows],
                ['h-recovered', 'x-recovered', 'h-later', 'x-later'],
            )
            self.assertEqual(rows[1]['occurred_at'], 3000)

    def test_canonical_historical_new_timestamps_local_day_order_and_exact_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp,'anaxi_provenance.db')
            seed_projection_db(db)
            tz = timezone(timedelta(hours=-7))
            before_midnight = int(datetime(2026,9,6,6,59,tzinfo=timezone.utc).timestamp())
            # Deliberately reverse insertion order; occurrence time remains authoritative.
            add_event(db,'x1','assistant','An unfinished Markdown fence: ```',before_midnight+120,'h1')
            add_event(db,'h1','user','Exact H with trailing space. ',before_midnight)
            raw = projection.project_waking_conversation(db)
            self.assertEqual([m['role'] for m in raw],['user','assistant'])
            records = projection.project_waking_conversation(db,include_event_metadata=True)
            self.assertEqual([m['occurred_at'] for m in records],[before_midnight,before_midnight+120])
            rendered = display.display_messages(records,tz=tz)
            self.assertEqual(rendered[0]['content'][0]['text'],raw[0]['content'])
            self.assertEqual(rendered[1]['content'][0]['text'],raw[1]['content'])
            self.assertIn('Sep 5, 2026 · 11:59 PM',rendered[0]['content'][1]['text'])
            self.assertIn('Sep 6, 2026 · 12:01 AM',rendered[1]['content'][1]['text'])
            add_event(db,'h2','user','New H.',before_midnight+180)
            add_event(db,'x2','assistant','New X.',before_midnight+240,'h2')
            fresh = display.display_messages(projection.project_waking_conversation(db,include_event_metadata=True),tz=tz)
            self.assertEqual(fresh[:2],rendered)
            self.assertIn('12:02 AM',fresh[2]['content'][1]['text'])
            self.assertNotIn('Sep ',fresh[2]['content'][1]['text'])
            self.assertEqual(fresh,display.display_messages(projection.project_waking_conversation(db,include_event_metadata=True),tz=tz))
            self.assertNotIn('anaxi-message-time',str(projection.project_waking_conversation(db)))

    def test_host_timestamp_comes_from_existing_callback_and_does_not_make_x(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fail():
                raise ValueError('Synthetic host failure')
            path = os.path.join(tmp,'diagnostics.jsonl')
            with self.assertRaises(ValueError):
                diagnostics.wrap_conversation_callback('Synthetic H',[],fail,log_path=path)
            import json
            record = json.loads(Path(path).read_text().strip())
            metadata = diagnostics.get_last_callback_display_metadata()
            self.assertEqual(metadata['timestamp_end'],record['timestamp_end'])
            status = display.display_host_status('Host failure',metadata['timestamp_end'],tz=timezone.utc)
            self.assertIn('Host · ',status)
            self.assertNotIn('event_id',status)
            self.assertEqual(display.display_host_status('Host failure',None),'Host failure')


class UiAdapterTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ,{'ANAXI_BOUND_HUMAN_ACTOR_ID':'','GRADIO_ANALYTICS_ENABLED':'False'})
        env.start(); self.addCleanup(env.stop)

    def test_submit_rehydrates_canonical_times_without_sending_display_text_to_driver(self):
        import test_llama_gui_conversation_mode as gui_fixture
        gui, w, _, _, _, _, _, _, _ = gui_fixture.fresh_gui(['llama_gui.py','--conversation'])
        control_names = {'take_direction','give_direction_to_clark','open_direction','submit_waking_projection'}
        queued = [fn for fn in gui.demo.fns.values() if getattr(fn.fn,'__name__','') in control_names]
        self.assertEqual(len(queued),4)
        self.assertEqual(len({fn.concurrency_id for fn in queued}),1)
        self.assertTrue(all(fn.concurrency_limit==1 for fn in queued))
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp,'anaxi_provenance.db')
            seed_projection_db(db)
            add_event(db,'h1','user','Prior raw H',1000)
            add_event(db,'x1','assistant','Prior raw X',1001,'h1')
            gui.PROVENANCE_DB_DIR = tmp
            raw_input = 'Exact new input. '
            def respond(message,history):
                self.assertEqual(message,raw_input)
                self.assertEqual(history,projection.project_waking_conversation(db))
                self.assertNotIn('anaxi-message-time',str(history))
                add_event(db,'h2','user',message,2000)
                add_event(db,'x2','assistant','New raw X',2001,'h2')
                return 'New raw X',''
            with patch.object(gui,'respond',side_effect=respond):
                messages,status,lead,request = gui.submit_waking_projection(raw_input)
            self.assertEqual(status,'')
            self.assertEqual(messages,gui.refresh_waking_conversation())
            self.assertEqual(messages[-2]['content'][0]['text'],raw_input)
            self.assertIn('anaxi-message-time',messages[-1]['content'][1]['text'])
            with patch.object(gui,'respond',return_value=('Host diagnostic','Host failure')):
                failed,status,_,_ = gui.submit_waking_projection('Synthetic failure')
            self.assertEqual(failed,messages)  # host status never enters an assistant bubble
            self.assertIn('Host failure',status)
            # Page refresh does not change Open; a fresh session's accepted default is Open.
            before = w.get_working_set()
            gui.refresh_waking_conversation(); gui.refresh_conversation_lead()
            self.assertEqual(w.get_working_set(),before)
            w.reset_working_set()
            self.assertIn('Conversation lead: Open',gui.refresh_conversation_lead())

    def test_supported_gradio_keeps_footer_in_separate_message_block(self):
        import gradio as gr
        raw = 'An unclosed code fence:\n```python\nprint(1)'
        rendered = display.display_messages([{'role':'assistant','content':raw,'occurred_at':1000}],tz=timezone.utc)
        chatbot = gr.Chatbot(group_consecutive_messages=False)
        data = chatbot.postprocess(rendered).model_dump()
        self.assertEqual(data[0]['content'][0]['text'],raw)
        self.assertEqual(len(data[0]['content']),2)
        self.assertIn('anaxi-message-time',data[0]['content'][1]['text'])


if __name__ == '__main__':
    unittest.main()
