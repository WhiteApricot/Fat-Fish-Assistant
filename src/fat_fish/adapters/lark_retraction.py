"""Correlate authenticated Lark recalls with original, admitted messages."""
import asyncio
from contextlib import closing
import hashlib
import logging
import re
import sqlite3
import time

from fat_fish.communication.model import Account, ChatKind, Conversation, MessageRetraction, key


logger = logging.getLogger(__name__)
EVENT = "p2.im.message.recalled_v1"
RETENTION = 30 * 86400


class LarkRetractions:
    def __init__(self, address, bot, path, retract):
        self.address, self.bot, self.path, self.retract = address, bot, path, retract
        self.dispatcher = self.processor = None
        self.tasks = set()
        self.verified = set()
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS lark_retractions (
                id TEXT PRIMARY KEY, sender TEXT, kind TEXT, peer TEXT,
                recalled INTEGER NOT NULL, observed REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS lark_retractions_observed ON lark_retractions(observed)")
            db.execute("DELETE FROM lark_retractions WHERE observed < ?", (time.time() - RETENTION,))

    def connect(self):
        return sqlite3.connect(self.path, timeout=3)

    def attach(self, adapter):
        if getattr(adapter, "connection_mode", None) != "socket":
            self.detach()
            return
        dispatcher = adapter.event_handler._processorMap
        if self.dispatcher is dispatcher:
            return
        self.detach()
        if EVENT in dispatcher:
            raise ValueError("Lark recall processor already registered")
        from lark_oapi import EventDispatcherHandler

        native = EventDispatcherHandler.builder("", "").register_p2_im_message_recalled_v1(self.receive).build()
        self.processor = native._processorMap[EVENT]
        dispatcher[EVENT] = self.processor
        self.dispatcher = dispatcher

    def detach(self):
        if self.dispatcher is not None and self.dispatcher.get(EVENT) is self.processor:
            del self.dispatcher[EVENT]
        self.dispatcher = self.processor = None
        for task in tuple(self.tasks):
            task.cancel()
        self.verified.clear()

    def message_key(self, chat, message):
        if not isinstance(chat, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat):
            return None
        if not isinstance(message, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]+", message):
            return None
        return hashlib.sha256(key(self.address.platform, self.address.instance, self.bot, chat, message).encode()).hexdigest()

    def track(self, event, message):
        if self.dispatcher is None:
            return True
        chat = getattr(getattr(event.message_obj, "raw_message", None), "chat_id", None)
        ident = self.message_key(chat, message.message_id)
        if ident is None:
            return True
        room = message.conversation
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM lark_retractions WHERE observed < ?", (now - RETENTION,))
            db.execute("INSERT OR IGNORE INTO lark_retractions VALUES (?, ?, ?, ?, 0, ?)",
                       (ident, message.sender.subject, room.kind.value, room.peer, now))
            return not db.execute("SELECT recalled FROM lark_retractions WHERE id=?", (ident,)).fetchone()[0]

    def receive(self, event):
        task = asyncio.create_task(self.apply(event))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def apply(self, event):
        try:
            await self.apply_recall(event.event)
        except Exception as exc:
            logger.warning("FFA Lark recall could not be applied (%s)", type(exc).__name__)

    async def apply_recall(self, event):
        ident = self.message_key(event.chat_id, event.message_id)
        if ident is None or self.dispatcher is None:
            return
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM lark_retractions WHERE observed < ?", (now - RETENTION,))
            db.execute("""INSERT INTO lark_retractions VALUES (?, NULL, NULL, NULL, 1, ?)
                          ON CONFLICT(id) DO UPDATE SET recalled=1, observed=excluded.observed""", (ident, now))
            sender, kind, peer = db.execute("SELECT sender, kind, peer FROM lark_retractions WHERE id=?",
                                            (ident,)).fetchone()
        if sender is None:
            return
        room = Conversation(self.address, self.bot, ChatKind(kind), peer, peer)
        notice = MessageRetraction(room, Account(self.address, sender), event.message_id)
        if notice in self.verified:
            return
        self.verified.add(notice)
        try:
            await self.retract(self.address, notice)
        finally:
            self.verified.discard(notice)
