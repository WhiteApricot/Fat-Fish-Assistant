"""State transitions for replies derived from revocable memory and tool evidence."""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Callable

REGENERATION_SECONDS = 6
RECOVERY_SECONDS = 8
MAX_DRAFT_BYTES = 32 * 1024
UNAVAILABLE_REPLY = "刚才引用的资料已不可用。你可以补充需要的信息，我再接着回答。"


@dataclass(frozen=True)
class Dependency:
    item_id: int
    revision: int
    evidence_digest: str
    valid_at: int | None = None
    known_at: int | None = None
    valid_window: tuple[int, int] | None = None


@dataclass(frozen=True)
class ReplyStamp:
    turn_id: str
    destination: str
    authorization_revision: int
    scope_generations: tuple[tuple[int, int], ...]
    ledger_seq: int
    history_revision: int
    dependencies: tuple[Dependency, ...]


class Delivery(str, Enum):
    SUBMITTED = "submitted"
    UNKNOWN = "unknown"
    STALE = "stale"
    REVOKED = "revoked"
    UNAVAILABLE = "unavailable"


class PolicyFence:
    """Serialize local revocation with the adapter's actual handoff.

    Callbacks must be synchronous and bounded by the caller's validation budget.
    The handoff transfers ownership of the payload synchronously. Awaiting a
    transport queue before it consumes the permit is not a handoff.
    """

    def __init__(self):
        self._gate = RLock()

    @contextmanager
    def mutation(self):
        with self._gate:
            yield

    def revoke(self, mark_revoked):
        with self._gate:
            return mark_revoked()

    def validate(self, allowed, valid):
        if not self._gate.acquire(blocking=False):
            return Delivery.STALE
        try:
            if not allowed():
                return Delivery.REVOKED
            return None if valid() else Delivery.STALE
        finally:
            self._gate.release()

    def submit(self, allowed, valid, handoff):
        # Do not block the native event loop on a persistent SQLite commit.
        if not self._gate.acquire(blocking=False):
            return Delivery.STALE
        try:
            if not allowed():
                return Delivery.REVOKED
            if not valid():
                return Delivery.STALE
            return handoff()
        finally:
            self._gate.release()


class DraftBuffer:
    def __init__(self, maximum=MAX_DRAFT_BYTES):
        self.maximum = maximum
        self.parts = []
        self.size = 0

    def add(self, part: str) -> None:
        self.size += len(part.encode())
        if self.size > self.maximum:
            self.parts.clear()
            raise ValueError("Protected reply exceeded the buffer budget")
        self.parts.append(part)

    def finish(self) -> str:
        draft = "".join(self.parts)
        if not draft.strip():
            raise ValueError("Protected reply was empty")
        return draft


class ProtectedReply:
    """Build from original input, and permit at most one fresh generation.

    The caller supplies trusted authorization and a provider that builds fresh,
    tool-free requests on retry. Rebuild must omit stale provider session and
    tool history. Tool receipts are summarized by the caller under current ACL.
    """

    def __init__(self, fence: PolicyFence, allowed: Callable[[], bool], validator):
        self.fence = fence
        self.allowed = allowed
        self.validator = validator

    async def run(self, original, build, generate, submit, *, initial_deadline: float):
        loop = asyncio.get_running_loop()
        recovery_deadline = None
        handoff_started = False

        def guard(stamp):
            def consumption(action):
                def once():
                    nonlocal handoff_started
                    if handoff_started:
                        return Delivery.UNKNOWN
                    handoff_started = True
                    return action()

                return self.fence.submit(
                    self.allowed, lambda: stamp is None or self.validator(stamp), once
                )

            return consumption

        for attempt in range(2):
            if not self.allowed():
                return Delivery.REVOKED
            deadline = initial_deadline if attempt == 0 else min(
                recovery_deadline, loop.time() + REGENERATION_SECONDS
            )
            try:
                async with asyncio.timeout_at(deadline):
                    request, stamp = await build(original, allow_tools=attempt == 0)
                    verdict = self.fence.validate(self.allowed, lambda: self.validator(stamp))
                    if verdict == Delivery.REVOKED:
                        return verdict
                    if verdict != Delivery.STALE:
                        buffer = DraftBuffer()
                        async for part in generate(request):
                            buffer.add(part)
                        # Adapter must call the fenced callback when consuming
                        # the outbound payload, after pacing and serialization.
                        verdict = await submit(buffer.finish(), guard(stamp))
                if verdict != Delivery.STALE:
                    return verdict
            except (TimeoutError, OSError, ValueError):
                if handoff_started:
                    return Delivery.UNKNOWN
                break
            if attempt == 0:
                recovery_deadline = loop.time() + RECOVERY_SECONDS
        if handoff_started:
            return Delivery.UNKNOWN
        fallback_deadline = min(recovery_deadline or float("inf"), loop.time() + 2)
        try:
            async with asyncio.timeout_at(fallback_deadline):
                return await submit(UNAVAILABLE_REPLY, guard(None))
        except (TimeoutError, OSError):
            return Delivery.UNKNOWN if handoff_started else Delivery.UNAVAILABLE
