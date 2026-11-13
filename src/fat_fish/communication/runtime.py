"""FFA ingress and outbound dispatch. Each registered connection owns its lifecycle."""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

from fat_fish.communication.model import (
    Account, AdapterManifest, Capability, ChatKind, Conversation, NotSubmitted, Outgoing, ProviderId, identifier,
)
from fat_fish.gateway.dedup import SeenMessages
from fat_fish.permissions.service import PermissionService


logger = logging.getLogger(__name__)


class CommunicationProvider(Protocol):
    address: ProviderId
    bot: str
    capabilities: frozenset[Capability]

    async def start(self): ...
    async def stop(self): ...
    async def healthy(self) -> bool: ...
    async def send_checked(self, target: Conversation, message: Outgoing, allowed) -> None: ...


@dataclass
class Connection:
    provider: CommunicationProvider
    manifest: AdapterManifest | None = None
    state: str = "disabled"
    enabled: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CommunicationRuntime:
    def __init__(self, identities, dedup_path, *, on_delivery=None, on_retraction=None):
        self.identities = identities
        self.permissions = PermissionService(identities)
        self.seen = SeenMessages(dedup_path)
        self.connections = {}
        self.on_delivery = on_delivery
        self.on_retraction = on_retraction
        self._inflight = set()

    def register(self, provider):
        if provider.address in self.connections:
            raise ValueError("Provider already registered")
        manifest = getattr(provider, "manifest", None)
        if manifest is not None and (
                manifest.address != provider.address or manifest.capabilities != provider.capabilities):
            raise ValueError("Adapter manifest does not match its provider")
        self.connections[provider.address] = Connection(provider, manifest)

    async def enable(self, address):
        slot = self.connections[address]
        async with slot.lock:
            if slot.enabled:
                return True
            if slot.state == "stop_failed":
                return False
            try:
                await slot.provider.start()
            except (Exception, asyncio.CancelledError) as exc:
                slot.state = "start_failed"
                try:
                    await slot.provider.stop()
                except Exception as cleanup_error:
                    slot.state = "stop_failed"
                    logger.warning("FFA provider startup cleanup failed (%s)", type(cleanup_error).__name__)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                logger.warning("FFA provider start failed (%s)", type(exc).__name__)
                return False
            slot.enabled = True
            slot.state = "running"
            return True

    async def disable(self, address):
        slot = self.connections[address]
        slot.enabled = False
        async with slot.lock:
            if slot.state == "disabled":
                return
            slot.enabled = False
            try:
                await slot.provider.stop()
            except (Exception, asyncio.CancelledError) as exc:
                slot.state = "stop_failed"
                logger.warning("FFA provider stop failed (%s)", type(exc).__name__)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return
            slot.state = "disabled"

    async def health(self, address):
        slot = self.connections[address]
        async with slot.lock:
            if slot.enabled:
                try:
                    healthy = await slot.provider.healthy()
                except Exception:
                    healthy = False
                slot.state = "running" if healthy else "unhealthy"
            return "unhealthy" if slot.state in {"start_failed", "stop_failed"} else slot.state

    async def stop_all(self):
        await asyncio.gather(*(self.disable(address) for address in self.connections))
        return all(slot.state == "disabled" for slot in self.connections.values())

    def accepts(self, event):
        room = event.conversation
        slot = self.connections.get(room.provider)
        return bool(slot and slot.enabled and room.bot == slot.provider.bot and event.sender.subject != room.bot)

    async def verify_account(self, event):
        if not self.accepts(event):
            return None
        slot = self.connections[event.conversation.provider]
        if not getattr(slot.provider, "verification_capabilities", ()):
            return None
        async with slot.lock:
            if not self.accepts(event):
                return None
            try:
                account = await slot.provider.verify_account(event)
            except Exception as exc:
                slot.state = "unhealthy"
                logger.warning("FFA account verification failed (%s)", type(exc).__name__)
                return None
            return account if self.accepts(event) and account == event.sender else None

    def authorize(self, event):
        room = event.conversation
        if not self.accepts(event):
            return None
        return self.permissions.conversation(event.sender, room, mentions=event.mentions)

    def admit(self, event):
        permission = self.authorize(event)
        if permission and self.seen.begin_delivery("ingress", event.event_id):
            return permission
        return None

    def finish_receive(self, event, state):
        return self.seen.finish_delivery("ingress", event.event_id, state)

    async def receive(self, event, handler):
        decision = self.admit(event)
        if decision is None:
            return False
        try:
            await handler(event, decision)
        except BaseException:
            self.finish_receive(event, "unknown")
            raise
        self.finish_receive(event, "completed")
        return True

    async def retract(self, notice):
        """Accept a withdrawal only from the active provider's verification boundary."""
        room = notice.conversation
        slot = self.connections.get(room.provider)
        if not slot or not slot.enabled or slot.provider.bot != room.bot:
            raise PermissionError("Inactive retraction provider")
        verifier = getattr(slot.provider, "verify_retraction", None)
        if Capability.RETRACT not in slot.provider.capabilities or not callable(verifier) or self.on_retraction is None:
            raise NotImplementedError("Message retraction is unavailable")
        async with slot.lock:
            if not slot.enabled or not await verifier(notice):
                raise PermissionError("Retraction was not verified")

            def admission():
                return self.permissions.conversation(notice.actor, room) if slot.enabled else None

            current = admission()
            if current is None:
                raise PermissionError("Retraction actor is outside the conversation")

            def allowed():
                latest = admission()
                return latest is not None and latest.principal_id == current.principal_id

            return await self.on_retraction(notice, current.principal_id, allowed)

    async def identity_reply(self, event, account, submit):
        """Send a response to an authenticated private identity operation."""
        target = event.conversation
        slot = self.connections[target.provider]
        async with slot.lock:
            if (not self.accepts(event) or target.kind is not ChatKind.PRIVATE
                    or event.sender != account or target.peer != account.subject):
                return None
            return await submit()

    async def reply(self, event, admission, sequence, submit, *, check_on_submit=False):
        """Dispatch a native envelope without losing its media or quote information."""
        target = event.conversation
        slot = self.connections[target.provider]
        scope = "reply:" + target.storage_key
        operation = event.event_id + ":" + str(sequence)
        if not self.seen.begin_delivery(scope, operation):
            return None
        submitted = False

        def authorize():
            nonlocal submitted
            current = self.authorize(event)
            if current is None or current.principal_id != admission.principal_id:
                raise asyncio.CancelledError("FFA reply authorization revoked")
            submitted = True

        try:
            if not check_on_submit:
                authorize()
            result = await submit(authorize)
        except BaseException:
            self.seen.finish_delivery(scope, operation, "unknown" if submitted else "retryable")
            if submitted:
                slot.state = "unhealthy"
            raise
        self.seen.finish_delivery(scope, operation, "submitted" if submitted else "retryable")
        return result

    async def send(self, actor: Account, target: Conversation, message: Outgoing, operation_id, *,
                   proactive=False, recheck=None):
        identifier(operation_id)
        slot = self.connections.get(target.provider)
        if not slot:
            raise LookupError("Unknown communication provider")
        async with slot.lock:
            if not slot.enabled or target.bot != slot.provider.bot:
                raise ConnectionError("Provider unavailable")
            admission = self.permissions.conversation(actor, target)
            if admission is None or (recheck is not None and not recheck()):
                raise PermissionError("Target outside actor scope")
            required = message.requirements | ({Capability.PUSH} if proactive else set())
            if not required <= slot.provider.capabilities:
                raise ValueError("Provider capability unavailable")
            scope = "outbound:" + target.storage_key
            if not self.seen.begin_delivery(scope, operation_id):
                return False
            def allowed():
                current = self.permissions.conversation(actor, target)
                return (slot.enabled and current is not None and current.principal_id == admission.principal_id
                        and (recheck is None or recheck()))

            if not allowed():
                self.seen.finish_delivery(scope, operation_id, "retryable")
                raise PermissionError("Target authorization revoked")
            checked = getattr(slot.provider, "send_checked", None)
            if not callable(checked):
                self.seen.finish_delivery(scope, operation_id, "retryable")
                raise PermissionError("Provider has no last-mile authorization contract")
            authorized = False

            def before_submit():
                nonlocal authorized
                if authorized:
                    raise PermissionError("Submission authorization already used")
                authorized = allowed()
                return authorized

            claimed = scope, operation_id
            self._inflight.add(claimed)
            try:
                await checked(target, message, before_submit)
                if not authorized:
                    raise RuntimeError("Provider returned without a successful submission check")
            except NotSubmitted:
                self.seen.finish_delivery(scope, operation_id, "unknown" if authorized else "retryable")
                raise
            except BaseException:
                slot.state = "unhealthy"
                self.seen.finish_delivery(scope, operation_id, "unknown")
                raise
            finally:
                self._inflight.discard(claimed)
            self.seen.finish_delivery(scope, operation_id, "submitted")
        if self.on_delivery is not None:
            try:
                await self.on_delivery(self, actor, target, operation_id, proactive=proactive)
            except Exception as exc:
                logger.warning("FFA delivery evidence skipped (%s)", type(exc).__name__)
        return True

    def reconcile_send(self, actor, target, operation_id, evidence, *, verify):
        """Trusted host only: verifier returns delivered, rejected or no conclusive evidence."""
        admission = self.permissions.conversation(actor, target)
        if admission is None:
            raise PermissionError("Target outside actor scope")
        state = verify(target, operation_id, evidence)
        if state not in {"delivered", "rejected"}:
            raise ValueError("Conclusive provider evidence required")
        current = self.permissions.conversation(actor, target)
        if current is None or current.principal_id != admission.principal_id:
            raise PermissionError("Target authorization revoked")
        scope = "outbound:" + target.storage_key
        if (scope, operation_id) in self._inflight:
            raise RuntimeError("Cannot reconcile a live submission")
        current = self.seen.delivery(scope, operation_id)
        return current in {"processing", "unknown", "submitted"} and self.seen.finish_delivery(
            scope, operation_id, state, expected=current)
