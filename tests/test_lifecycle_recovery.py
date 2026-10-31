"""Synthetic recovery checks; run with TEMP/TMP directed to local workspace."""
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import manage
from lifecycle_lock import exclusive

class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        (self.root/'private').mkdir();(self.root/'data').mkdir()
        (self.root/'private/RUNNING').write_text('99999999')
        release=self.root/'release';release.mkdir();(release/'payload-manifest.json').write_text('[]')
        (self.root/'instance.json').write_text(json.dumps({'schema':1,'release':str(release)}))
        with closing(sqlite3.connect(self.root/'data/test.db')) as db:
            db.execute('create table chats(text)');db.execute("insert into chats values ('synthetic')")
    def tearDown(self):self.temp.cleanup()
    def recover(self):
        with exclusive(self.root),patch.object(manage.psutil,'pid_exists',return_value=False):manage.recover_stale(self.root)
    def test_reported_stale_marker(self):
        self.recover();self.assertFalse((self.root/'private/RUNNING').exists())
        self.assertEqual(len(list((self.root/'private/lifecycle-history').glob('*/RUNNING'))),1)
    def test_dirty_without_vector_state(self):
        (self.root/'private/DIRTY').write_text('interrupted');self.recover()
        self.assertFalse((self.root/'private/DIRTY').exists())
    def test_reused_pid_never_controlled(self):
        with exclusive(self.root),patch.object(manage.psutil,'pid_exists',return_value=True),self.assertRaises(ValueError):manage.recover_stale(self.root)
        self.assertTrue((self.root/'private/RUNNING').exists())
    def test_corrupt_sqlite_retains_evidence(self):
        (self.root/'data/test.db').write_bytes(b'corrupt')
        with self.assertRaises(sqlite3.DatabaseError):self.recover()
        self.assertTrue((self.root/'private/RUNNING').exists())
    def test_upgrade_is_separate(self):
        (self.root/'private/upgrade-journal.json').write_text('{}')
        with self.assertRaises(ValueError):self.recover()
    def test_missing_metadata_retains_markers(self):
        (self.root/'instance.json').unlink()
        with self.assertRaises(ValueError):self.recover()
        self.assertTrue((self.root/'private/RUNNING').exists())
    def test_os_lock_excludes_recovery(self):
        with exclusive(self.root),self.assertRaises(RuntimeError):
            with exclusive(self.root):pass
    def test_start_lock_excludes_parallel_start(self):
        with exclusive(self.root,'.start-lock'),self.assertRaises(RuntimeError):manage.start(self.root,Path(sys.executable))
    def test_unverified_vector_memory_retains_markers(self):
        path=self.root/'data/plugin_data/astrbot_plugin_livingmemory';path.mkdir(parents=True)
        with closing(sqlite3.connect(path/'livingmemory.db')) as db:db.execute('create table test(id)')
        with self.assertRaises(ValueError):self.recover()
        self.assertTrue((self.root/'private/RUNNING').exists())

    def test_native_historical_backups_are_not_active_indexes(self):
        history=self.root/'data/plugin_data/astrbot_plugin_livingmemory/backups/vunknown'
        history.mkdir(parents=True)
        (history/'livingmemory.index').write_bytes(b'opaque old recovery material')
        (history/'livingmemory.db').write_bytes(b'opaque old recovery database')
        self.recover()
        self.assertFalse((self.root/'private/RUNNING').exists())
        self.assertEqual((history/'livingmemory.index').read_bytes(),b'opaque old recovery material')

if __name__=='__main__':unittest.main(verbosity=2)
