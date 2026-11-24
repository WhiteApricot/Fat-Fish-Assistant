"""Admission and native receipt mapping for the replacement memory path."""

from hashlib import sha256
import sqlite3
from time import time

from fat_fish.communication.model import ChatKind, MessageEvent, MessageRetraction, key
from fat_fish.memory.capture import CaptureQueue
from fat_fish.memory.erasure import LedgerError
from fat_fish.memory.experience import sensitive_information
from fat_fish.memory.policy import Audience, ScopeKind, ScopeRef
from fat_fish.memory.records import EventRecord, SourceRecord
from fat_fish.memory.reply_guard import Dependency, ProtectedReply, ReplyStamp


class CognitiveMemoryGateway:
    def __init__(self, agent: str, queue: CaptureQueue):
        self.agent = agen
        self.queue = queue

    def _scope(self, conversation, principal: str) -> ScopeRef:
        if not principal:
            raise PermissionError("A memory turn requires an admitted principal")
        if conversation.kind == ChatKind.PRIVATE:
            return ScopeRef(self.agent, ScopeKind.PRIVATE, principal)
        if conversation.kind == ChatKind.GROUP:
            return ScopeRef(self.agent, ScopeKind.GROUP, conversation.storage_key)
        raise PermissionError("Unsupported memory conversation")

    def audience(self, event: MessageEvent, principal: str, authorization_revision: int) -> Audience:
        return Audience(self.agent, principal, self._scope(event.conversation, principal),
                        authorization_revision)

    def on_inbound(self, event: MessageEvent, principal: str, verify) -> bool:
        scope = self._scope(event.conversation, principal)
        occurred = int(event.timestamp * 1000)
        provider = event.conversation.provider
        source = SourceRecord(scope, event.event_id, "platform_message",
                              key(provider.platform, provider.instance), "direct_self",
                              event.event_id, occurred, author=principal)
        text = event.text[:1500] if event.text and not sensitive_information(event.text) else None
        return self.queue.offer(EventRecord(source, event.event_id, "inbound", "observed", "user",
                                            occurred, text, actor_ref=principal), verify)

    async def on_receipt(self, event: MessageEvent, principal: str, receipt: str, state: str,
                         occurred: int, verify) -> int:
        if state not in {"submitted", "failed", "unknown"} or not receipt:
            raise ValueError("A delivery transition requires a native result")
        scope = self._scope(event.conversation, principal)
        provider = event.conversation.provider
        digest = sha256(key(event.event_id, receipt, state).encode()).hexdigest()
        source = SourceRecord(scope, digest, "platform_delivery",
                              key(provider.platform, provider.instance), "verified_connector",
                              digest, occurred)
        record = EventRecord(source, event.event_id, "delivery", state, "bot", occurred)
        return await self.queue.persist(record, verify)

    def on_retraction(self, notice: MessageRetraction, principal: str, generation: int):
        scope = self._scope(notice.conversation, principal)
        return self.queue.repository.erase("source", scope, notice.event_id, generation, int(time() * 1000))


    def protected_reply(self, event: MessageEvent, principal: str, admission, history_revision,
                        *, clock=None):
        """Construct a per-turn guard before handing context to the model provider."""
        repository = self.queue.repository
        now = clock or (lambda: int(time() * 1000))
        address = event.conversation.storage_key

        def active():
            identity = admission()
            return identity is not None and identity[0] == principal

        def current_dependencies(stamp):
            identity = admission()
            if (identity is None or identity[0] != principal or stamp.destination != address
                    or stamp.turn_id != event.event_id or stamp.history_revision != history_revision()):
                return False
            audience = self.audience(event, principal, identity[1])
            try:
                rows = repository.authorized_documents(
                    audience, now(), item_ids=tuple(dep.item_id for dep in stamp.dependencies)
                )
            except (PermissionError, LedgerError, sqlite3.Error):
                return False
            found = {row["id"]: (row["rev"], row["content_hash"]) for row in rows}
            generations = tuple(sorted({(row["scope_id"], row["generation"]) for row in rows}))
            return generations == stamp.scope_generations and all(
                found.get(dep.item_id) == (dep.revision, dep.evidence_digest)
                       for dep in stamp.dependencies)

        return ProtectedReply(repository.fence, active, current_dependencies)


    def prompt_snapshot(self, event: MessageEvent, principal: str, authorization_revision: int,
                        history_revision: int, now: int):
        """Return current snippets with the exact revisions to recheck on handoff."""
        audience = self.audience(event, principal, authorization_revision)
        rows = self.queue.repository.authorized_documents(audience, now)
        stamp = ReplyStamp(event.event_id, event.conversation.storage_key, authorization_revision,
                           tuple(sorted({(row["scope_id"], row["generation"]) for row in rows})),
                           rows[0]["ledger_seq"] if rows else 0, history_revision,
                           tuple(Dependency(row["id"], row["rev"], row["content_hash"]) for row in rows))
        return tuple(row["body"] for row in rows), stamp
