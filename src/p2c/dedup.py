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
