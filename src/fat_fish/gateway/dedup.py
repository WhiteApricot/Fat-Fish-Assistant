"""Bounded, persistent delivery deduplication; no message bodies stored."""
from contextlib import closing
import hashlib
import sqlite3
import time

class SeenMessages:
    def __init__(self,path,clock=time.time,ttl=86400):
        self.path,self.clock,self.ttl=str(path),clock,ttl
    def claim(self,scope,ident):
        if not ident:return True
        now=self.clock();key=hashlib.sha256((scope+'|'+str(ident)).encode()).hexdigest()
        with closing(sqlite3.connect(self.path,timeout=3)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, expires REAL NOT NULL)')
            db.execute('DELETE FROM seen WHERE expires <= ?',(now,))
            if db.execute('SELECT 1 FROM seen WHERE id=?',(key,)).fetchone():return False
            db.execute('INSERT INTO seen VALUES (?,?)',(key,now+self.ttl))
            db.execute('DELETE FROM seen WHERE id IN (SELECT id FROM seen ORDER BY expires DESC LIMIT -1 OFFSET 20000)')
        return True

    @staticmethod
    def _delivery_table(db):
        db.execute("""CREATE TABLE IF NOT EXISTS delivery (
            id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL
        )""")

    @staticmethod
    def _delivery_id(scope, ident):
        return hashlib.sha256((scope + "|" + ident).encode()).hexdigest()

    def begin_delivery(self, scope, ident):
        key = self._delivery_id(scope, ident)
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._delivery_table(db)
            db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, expires REAL NOT NULL)")
            if db.execute("SELECT 1 FROM seen WHERE id=?", (key,)).fetchone():
                return False
            return db.execute("INSERT OR IGNORE INTO delivery VALUES (?,'processing',?)",
                              (key, self.clock())).rowcount == 1

    def delivery(self, scope, ident):
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            self._delivery_table(db)
            row = db.execute("SELECT state FROM delivery WHERE id=?",
                             (self._delivery_id(scope, ident),)).fetchone()
            return row[0] if row else None

    def finish_delivery(self, scope, ident, state, *, expected="processing"):
        key = self._delivery_id(scope, ident)
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            self._delivery_table(db)
            if state == "retryable":
                return db.execute("DELETE FROM delivery WHERE id=? AND state=?",
                                  (key, expected)).rowcount == 1
            return db.execute("UPDATE delivery SET state=?,updated=? WHERE id=? AND state=?",
                              (state, self.clock(), key, expected)).rowcount == 1
