"""Admission and native receipt mapping for the replacement memory path."""

import asyncio
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
from fat_fish.memory.retrieval import CognitiveRetrieval
from fat_fish.memory.semantic_query import RecallDecision, select, understand
from fat_fish.memory.structured_memory import extract, infer_observations


class CognitiveMemoryGateway:
    def __init__(self, agent: str, queue: CaptureQueue):
        self.agent = agent
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

    def _incoming(self, event, principal):
        scope = self._scope(event.conversation, principal)
        occurred = int(event.timestamp * 1000)
        provider = event.conversation.provider
        source = SourceRecord(scope, event.event_id, "platform_message",
                              key(provider.platform, provider.instance), "direct_self",
                              event.event_id, occurred, author=principal)
        snippet = event.text[:1500] if event.text and not sensitive_information(event.text) else None
        return EventRecord(source, event.event_id, "inbound", "observed", "user",
                           occurred, snippet, actor_ref=principal)

    def on_inbound(self, event: MessageEvent, principal: str, verify) -> bool:
        return self.queue.offer(self._incoming(event, principal), verify)

    async def capture_inbound(self, event: MessageEvent, principal: str, verify) -> int:
        return (await self.capture_completed_turn((event,), principal, verify))[0]

    async def capture_completed_turn(self, events, principal: str, verify):
        """The caller provides admitted messages only after successful delivery."""
        events = tuple(events)
        if not events or any(event.conversation != events[0].conversation or event.sender != events[0].sender
                             for event in events):
            raise ValueError("Capture inputs must belong to one admitted turn")
        records = tuple(self._incoming(event, principal) for event in events)
        return await asyncio.to_thread(self.queue.repository.record_completed_turn, records,
                                       self.queue.clock(), authorize=verify)

    async def extract_next(self, provider_for, authorize):
        """Process one lease. Provider choice and current admission remain caller-owned."""
        repository = self.queue.repository
        job = await asyncio.to_thread(repository.claim_extraction, self.queue.clock())
        if job is None:
            return False
        stage = "authorization"
        reason = "AuthorizationChanged"
        try:
            if await asyncio.to_thread(authorize, job):
                async with asyncio.timeout(24):
                    stage = "extraction"
                    provider = await provider_for(job)
                    candidates = await asyncio.to_thread(repository.extraction_candidates, job, self.queue.clock())
                    if not await asyncio.to_thread(authorize, job):
                        raise PermissionError("Extraction source is no longer authorized")
                    claims = await extract(provider, job["snippet"], job["occurred_at"], candidates=candidates)
                    observations = ()
                    if claims and len(candidates) >= 2 and await asyncio.to_thread(authorize, job):
                        stage = "observation"
                        observations = await infer_observations(provider, job["snippet"], candidates)
                stage = "publication"
                if await asyncio.to_thread(repository.apply_extraction, job, claims, self.queue.clock(),
                                           authorize=lambda: authorize(job), candidates=candidates,
                                           observations=observations):
                    return True
        except (TimeoutError, ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
            reason = f"{stage}:{type(exc).__name__}"
        await asyncio.to_thread(repository.retry_extraction, job, self.queue.clock(), reason=reason)
        return True

    async def semantic_snapshot(self, event, principal, authorization_revision, history_revision,
                                provider, *, dialogue=(), embedding=None, space=None):
        """Build a bounded, revision-stamped snapshot for an explicit lookback."""
        message = event.text
        empty_stamp = _reply_stamp(event, authorization_revision, history_revision, ())
        if not message or sensitive_information(message):
            return RecallDecision("abstain", (), ()), empty_stamp
        audience = self.audience(event, principal, authorization_revision)
        repository = self.queue.repository
        retrieval = CognitiveRetrieval(repository, embedding, space=space)
        try:
            async with asyncio.timeout(4):
                plan = await understand(provider, message, dialogue)
                if plan.route != "deep":
                    return RecallDecision(plan.route, (), ()), empty_stamp
                candidates = {}
                for target in plan.targets:
                    now = self.queue.clock()
                    result = await retrieval.recall(audience, target, now, deep=True)
                    expected = {hit.item_id: (hit.revision, hit.digest) for hit in result.hits}
                    documents = await asyncio.to_thread(repository.authorized_documents, audience,
                                                        self.queue.clock(), item_ids=tuple(expected))
                    by_id = {row["id"]: row for row in documents
                             if expected[row["id"]] == (row["rev"], row["content_hash"])}
                    ordered = tuple(by_id[item] for item in expected if item in by_id)
                    try:
                        related = await asyncio.to_thread(repository.graph_candidates, audience, self.queue.clock(),
                                                          tuple(row["id"] for row in ordered[:4]), limit=4)
                    except (sqlite3.Error, LedgerError):
                        related = ()
                    candidates[target] = (*ordered, *(row for row in related if row["id"] not in by_id))
                if not all(candidates.values()):
                    available = tuple(row for rows in candidates.values() for row in rows)
                    return RecallDecision("abstain", available, ()), empty_stamp
                decision = await select(provider, message, candidates)
                rows = decision.support
                stamp = _reply_stamp(event, authorization_revision, history_revision, rows)
                return decision, stamp
        except (TimeoutError, ValueError, RuntimeError, OSError, sqlite3.Error):
            return RecallDecision("abstain", (), ()), empty_stamp

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
            if (identity is None or identity[0] != principal or identity[1] != stamp.authorization_revision
                    or stamp.destination != address
                    or stamp.turn_id != event.event_id or stamp.history_revision != history_revision()):
                return False
            audience = self.audience(event, principal, identity[1])
            try:
                rows = repository.authorized_documents(
                    audience, now(), limit=max(12, len(stamp.dependencies)),
                    item_ids=tuple(dep.item_id for dep in stamp.dependencies)
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
        return tuple(row["body"] for row in rows), _reply_stamp(
            event, authorization_revision, history_revision, rows)


def _reply_stamp(event, authorization_revision, history_revision, rows):
    return ReplyStamp(event.event_id, event.conversation.storage_key, authorization_revision,
                      tuple(sorted({(row["scope_id"], row["generation"]) for row in rows})),
                      rows[0]["ledger_seq"] if rows else 0, history_revision,
                      tuple(Dependency(row["id"], row["rev"], row["content_hash"]) for row in rows))
