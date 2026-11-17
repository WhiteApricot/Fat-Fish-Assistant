"""Durable correlation of authenticated OneBot recalls with admitted QQ input."""
import asyncio
from contextlib import closing
import hashlib
import logging
import re
import sqlite3
import time

from fat_fish.communication.model import Account, ChatKind, Conversation, MessageRetraction, key


logger = logging.getLogger(__name__)
RECALL_TYPES = {"friend_recall": ChatKind.PRIVATE, "group_recall": ChatKind.GROUP}
RETENTION = 30 * 86400


def qq_id(value):
    value = str(value)
    return value if re.fullmatch(r"[1-9][0-9]{0,18}", value) else None


def signed_id(value):
    value = str(value)
    return value if re.fullmatch(r"-?[1-9][0-9]{0,18}", value) and -(2**31) <= int(value) < 2**31 else None


class OneBotRetractions:
    def __init__(self, address, bot, context):
        self.address, self.bot, self.context = address, bot, context
        self.path = context.private_dir / "ffa-qq-retractions.sqlite3"
        self.verified = set()
        self.detach = None
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS qq_retractions (
                room TEXT NOT NULL, sender TEXT NOT NULL, message TEXT NOT NULL,
                tracked INTEGER NOT NULL, recalled INTEGER NOT NULL, applied INTEGER NOT NULL,
                observed REAL NOT NULL, context_digest TEXT, PRIMARY KEY(room,sender,message))""")
            db.execute("DELETE FROM qq_retractions WHERE recalled=0 AND observed<?", (time.time() - RETENTION,))

    def connect(self):
        return sqlite3.connect(self.path, timeout=3)

    def attach(self, adapter):
        original = adapter.handle_msg
        prior = vars(adapter).get("handle_msg")

        async def on_message(message):
            raw = getattr(message, "raw_message", None)
            if isinstance(raw, dict) and raw.get("post_type") == "notice" and raw.get("notice_type") in RECALL_TYPES:
                try:
                    await self.receive(raw)
                except (ValueError, PermissionError, sqlite3.Error, OSError, RuntimeError, TimeoutError) as exc:
                    logger.info("FFA QQ recall deferred (%s)", type(exc).__name__)
                return
            await original(message)

        def detach():
            if adapter.handle_msg is on_message:
                if prior is None:
                    del adapter.handle_msg
                else:
                    adapter.handle_msg = prior

        self.detach = detach
        adapter.handle_msg = on_message

    def track(self, message):
        if signed_id(message.message_id) is None:
            return True
        keys = message.conversation.storage_key, message.sender.subject, message.message_id
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO qq_retractions(room,sender,message,tracked,recalled,applied,observed) VALUES (?,?,?,1,0,0,?)
                ON CONFLICT(room,sender,message) DO UPDATE SET tracked=1""", (*keys, time.time()))
            return not db.execute("""SELECT recalled FROM qq_retractions
                WHERE room=? AND sender=? AND message=?""", keys).fetchone()[0]

    @staticmethod
    def _context_digest(message):
        content = message.content
        if isinstance(content, list):
            content = "\n".join(text for part in content if isinstance(text := getattr(part, "text", None), str))
        return hashlib.sha256(content.encode()).hexdigest() if isinstance(content, str) and content else None

    def filter_history(self, room, inputs, messages):
        has_current = bool(messages and messages[-1].role == "user")
        current = messages[-1:] if has_current else []
        digest = self._context_digest(current[0]) if current else None
        with closing(self.connect()) as db, db:
            if digest:
                db.executemany("""UPDATE qq_retractions SET context_digest=? WHERE room=? AND sender=? AND message=?""",
                               ((digest, room.storage_key, item.sender.subject, item.message_id) for item in inputs))
            withdrawn = {row[0] for row in db.execute("""SELECT context_digest FROM qq_retractions
                WHERE room=? AND recalled=1 AND context_digest IS NOT NULL""", (room.storage_key,))}
        if not withdrawn:
            return messages
        history = []
        skip = False
        for message in messages[:-1] if has_current else messages:
            if message.role == "user":
                skip = self._context_digest(message) in withdrawn
            if not skip or message.role == "system":
                history.append(message)
        return history + current

    def is_recalled(self, room, author, message_id):
        with closing(self.connect()) as db:
            return bool(db.execute("""SELECT 1 FROM qq_retractions WHERE room=?
                AND sender=? AND message=? AND recalled=1""", (room.storage_key, author, message_id)).fetchone())

    def _mark(self, notice):
        keys = notice.conversation.storage_key, notice.actor.subject, notice.message_id
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO qq_retractions(room,sender,message,tracked,recalled,applied,observed) VALUES (?,?,?,0,1,0,?)
                ON CONFLICT(room,sender,message) DO UPDATE SET recalled=1""", (*keys, time.time()))
            tracked, applied = db.execute("""SELECT tracked,applied FROM qq_retractions
                WHERE room=? AND sender=? AND message=?""", keys).fetchone()
        return tracked, applied

    async def receive(self, raw):
        if raw.get("post_type") != "notice":
            return
        kind = RECALL_TYPES.get(raw.get("notice_type"))
        sender = qq_id(raw.get("user_id"))
        ident = signed_id(raw.get("message_id"))
        if not kind or not sender or not ident or sender == self.bot or qq_id(raw.get("self_id")) != self.bot:
            return
        peer = sender if kind is ChatKind.PRIVATE else qq_id(raw.get("group_id"))
        if not peer or (kind is ChatKind.GROUP and not qq_id(raw.get("operator_id"))):
            return
        notice = MessageRetraction(Conversation(self.address, self.bot, kind, peer, peer),
                                   Account(self.address, sender), ident)
        await self.apply(notice)

    async def apply(self, notice):
        tracked, applied = await asyncio.to_thread(self._mark, notice)
        if applied or notice in self.verified:
            return
        prior = self.context.retraction_source
        if not tracked and (prior is None or not await prior(notice)):
            return
        if notice in self.verified:
            return
        self.verified.add(notice)
        try:
            await self.context.retract_native(self.address, notice)
            await asyncio.to_thread(self._finish, notice)
        finally:
            self.verified.discard(notice)

    def _finish(self, notice):
        with closing(self.connect()) as db, db:
            db.execute("""UPDATE qq_retractions SET applied=1 WHERE room=? AND sender=? AND message=?""",
                       (notice.conversation.storage_key, notice.actor.subject, notice.message_id))

    async def reconcile(self, message):
        room = message.conversation

        def pending():
            with closing(self.connect()) as db:
                return db.execute("""SELECT sender,message FROM qq_retractions WHERE room=?
                    AND recalled=1 AND applied=0 ORDER BY observed DESC LIMIT 16""",
                                  (room.storage_key,)).fetchall()

        for author, ident in await asyncio.to_thread(pending):
            notice = MessageRetraction(room, Account(self.address, author), ident)
            try:
                await self.apply(notice)
            except (PermissionError, OSError, RuntimeError, TimeoutError):
                continue
