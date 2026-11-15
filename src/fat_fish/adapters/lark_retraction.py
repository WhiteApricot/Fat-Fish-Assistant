"""Correlate authenticated Lark recalls with original, admitted messages."""
import asyncio
from collections import OrderedDict
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
RECALL_APPLIED_WITH_REPLY = 3


class LarkRetractions:
    def __init__(self, address, bot, path, retract):
        self.address, self.bot, self.path, self.retract = address, bot, path, retract
        self.dispatcher = self.processor = None
        self.tasks = set()
        self.verified = set()
        self.history_checks = OrderedDict()
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
        self.history_checks.clear()

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
            await self.apply_recall(event.event.chat_id, event.event.message_id)
        except Exception as exc:
            logger.warning("FFA Lark recall could not be applied (%s)", type(exc).__name__)

    async def reconcile(self, event, message, *, force=False):
        """Recover missed callbacks from explicitly deleted rows in this native chat."""
        chat = getattr(getattr(event.message_obj, "raw_message", None), "chat_id", None)
        if self.dispatcher is None or self.message_key(chat, message.message_id) is None:
            return
        if message.conversation.kind is ChatKind.GROUP and chat != message.conversation.peer:
            return
        from lark_oapi.api.im.v1 import ListMessageRequest
        now = time.monotonic()
        if not force and now - self.history_checks.get(chat, -30) < 30:
            return
        self.history_checks[chat] = now
        self.history_checks.move_to_end(chat)
        if len(self.history_checks) > 256:
            self.history_checks.popitem(last=False)

        try:
            api = event.bot.im.v1.message
            page = None
            for _ in range(2):
                builder = (ListMessageRequest.builder().container_id_type("chat").container_id(chat)
                           .sort_type("ByCreateTimeDesc").page_size(50))
                if page:
                    builder.page_token(page)
                result = await asyncio.wait_for(api.alist(builder.build()), 3 if force else 1.5)
                if not result.success() or not result.data:
                    return
                for row in result.data.items or ():
                    if row.chat_id == chat and row.deleted is True:
                        await self.apply_recall(chat, row.message_id, tracked_only=True)
                page = result.data.page_token
                if not result.data.has_more or not page:
                    break
        except Exception as exc:
            logger.info("FFA Lark recall history check unavailable (%s)", type(exc).__name__)

    async def apply_recall(self, chat, message_id, *, tracked_only=False):
        ident = self.message_key(chat, message_id)
        if ident is None or self.dispatcher is None:
            return
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM lark_retractions WHERE observed < ?", (now - RETENTION,))
            original = db.execute("SELECT sender, kind, peer, recalled FROM lark_retractions WHERE id=?",
                                  (ident,)).fetchone()
            if (tracked_only and not original) or (original and original[3] >= RECALL_APPLIED_WITH_REPLY):
                return
            db.execute("""INSERT INTO lark_retractions VALUES (?, NULL, NULL, NULL, 1, ?)
                          ON CONFLICT(id) DO UPDATE SET recalled=1, observed=excluded.observed""", (ident, now))
        if not original or original[0] is None:
            return
        sender, kind, peer, _ = original
        room = Conversation(self.address, self.bot, ChatKind(kind), peer, peer)
        notice = MessageRetraction(room, Account(self.address, sender), message_id)
        if notice in self.verified:
            return
        self.verified.add(notice)
        try:
            await self.retract(self.address, notice)
            with closing(self.connect()) as db, db:
                db.execute("UPDATE lark_retractions SET recalled=? WHERE id=?", (RECALL_APPLIED_WITH_REPLY, ident))
        finally:
            self.verified.discard(notice)
