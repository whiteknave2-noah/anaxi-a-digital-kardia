"""Real legacy schema and synthetic landing; never production data/inference."""
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import hippocampus_store as hs
import test_hippocampus_store as fixtures
import test_sleep_landing as landing
import sleep_c_schema


def legacy_db(path, *, four_sources=False):
    conn = sqlite3.connect(path)
    sql = hs._SCHEMA_SQL if four_sources else hs._SCHEMA_SQL.replace(repr(hs.SOURCE_STORE_VALUES), repr(hs.SOURCE_STORE_VALUES[:3]))
    conn.executescript(sql)
    conn.executemany('INSERT INTO hippocampus_meta VALUES (?,?)', [('schema_version','1'),('item_id_version','1')])
    conn.commit()
    return conn


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fx = fixtures.build_fixture(self.tmp.name)
        self.paths = fixtures.make_paths(self.fx, self.tmp.name)

    def add_sleep(self):
        with closing(sqlite3.connect(self.paths.provenance_db_path)) as c, c:
            sleep_c_schema.ensure_schema(c)
        landing.insert_cycle_and_derivation(self.paths.provenance_db_path, cycle_id='synthetic-cycle',
                                           derivation_id='synthetic-derivation', derived_text='Synthetic tentative continuity.')

    def test_actual_old_check_failure_rolls_back_then_upgrade_accepts_landing(self):
        p = self.paths.hippocampus_db_path
        legacy_db(p).close()
        self.add_sleep()
        # Reproduce the deployed v1 opener which checked version metadata only.
        with patch.object(hs, '_open_hippocampus_writable', side_effect=lambda path: sqlite3.connect(path)):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'source_store IN'):
                hs.sync_hippocampus(p, self.paths)
        with closing(sqlite3.connect(p)) as c, c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM hippocampal_items').fetchone()[0], 0)
        result = hs.sync_hippocampus(p, self.paths)
        self.assertTrue(result.inserted)
        with closing(sqlite3.connect(p)) as c, c:
            self.assertEqual(dict(c.execute('SELECT * FROM hippocampus_meta'))['schema_version'], '2')
            self.assertEqual(set(r[0] for r in c.execute('SELECT DISTINCT source_store FROM hippocampal_items')), set(hs.SOURCE_STORE_VALUES))
            self.assertEqual(c.execute("SELECT memory_kind,source_locator FROM hippocampal_items WHERE source_store='sleep_derivations'").fetchone(), ('derived_inference','synthetic-derivation'))
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE hippocampal_items SET source_store='invented_source'")
        self.assertFalse(hs.sync_hippocampus(p, self.paths).inserted)

    def test_existing_rows_rowids_and_fts_survive_and_triggers_still_work(self):
        p = self.paths.hippocampus_db_path
        legacy_db(p).close()
        with patch.object(hs, '_open_hippocampus_writable', side_effect=lambda path: sqlite3.connect(path)):
            hs.sync_hippocampus(p, self.paths)
        with closing(sqlite3.connect(p)) as c, c:
            before = c.execute('SELECT * FROM hippocampal_items ORDER BY row_id').fetchall()
            fts_before = c.execute("SELECT rowid FROM hippocampal_items_fts WHERE hippocampal_items_fts MATCH 'human' ORDER BY rowid").fetchall()
        c = hs._open_hippocampus_writable(p)
        try:
            self.assertEqual(c.execute('SELECT * FROM hippocampal_items ORDER BY row_id').fetchall(), before)
            self.assertEqual(c.execute("SELECT rowid FROM hippocampal_items_fts WHERE hippocampal_items_fts MATCH 'human' ORDER BY rowid").fetchall(), fts_before)
            c.execute("UPDATE hippocampal_items SET content_text='uniquesyntheticftsprobe' WHERE row_id=?", (before[0][0],))
            self.assertEqual(c.execute("SELECT rowid FROM hippocampal_items_fts WHERE hippocampal_items_fts MATCH 'uniquesyntheticftsprobe'").fetchall(), [(before[0][0],)])
            c.rollback()
            c.execute("INSERT INTO hippocampal_items_fts(hippocampal_items_fts,rank) VALUES ('integrity-check',1)")
        finally:
            c.close()

    def test_failed_ddl_upgrade_is_atomic(self):
        p = self.paths.hippocampus_db_path
        c = legacy_db(p)
        before = c.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
        c.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE else sqlite3.SQLITE_OK)
        with self.assertRaises(sqlite3.DatabaseError):
            hs._migrate_schema(c)
        c.set_authorizer(None)
        self.assertEqual(c.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall(), before)
        self.assertEqual(dict(c.execute('SELECT * FROM hippocampus_meta'))['schema_version'], '1')
        c.close()

    def test_known_four_source_v1_upgrades_but_unknown_shape_fails_closed(self):
        p = self.paths.hippocampus_db_path
        legacy_db(p, four_sources=True).close()
        hs._open_hippocampus_writable(p).close()
        with closing(sqlite3.connect(p)) as c, c:
            c.execute('ALTER TABLE hippocampal_items ADD COLUMN unexpected TEXT')
        with self.assertRaises(hs.HippocampusSchemaError):
            hs._open_hippocampus_writable(p)


if __name__ == '__main__':
    unittest.main()
