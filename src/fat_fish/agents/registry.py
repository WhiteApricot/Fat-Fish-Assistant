"""Minimal trusted-host Agent lifecycle, capabilities and durable operation claims."""
import asyncio
from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
import math
import sqlite3
import time

from fat_fish.communication.model import identifier, key
from fat_fish.memory.activity import Access, Activity, ActivityKind, ActivityState
from fat_fish.memory.experience import contains_secret
from fat_fish.memory.service import encode
from fat_fish.agents.output import OutputAuthorization, check_external_output


@dataclass(frozen=True)
class ApprovalEvent:
    """Verified by the trusted host against the exact operation and payload."""
    event_id: str
    occurred: float
    access: Access

    def __post_init__(self):
        identifier(self.event_id)
        if not isinstance(self.occurred, (int, float)) or not math.isfinite(self.occurred):
            raise ValueError("Invalid trusted approval time")


class Risk(str, Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class Tool:
    name: str
    kind: ActivityKind
    risk: Risk

    def __post_init__(self):
        identifier(self.name)
        if not isinstance(self.kind, ActivityKind) or not isinstance(self.risk, Risk):
            raise ValueError("Invalid tool kind or risk")


@dataclass(frozen=True)
class Manifest:
    plugin_id: str
    environment: str
    dependencies: tuple[str, ...]
    tools: tuple[Tool, ...]
    revision: str = "local"
    repository: str = "local"

    @property
    def fingerprint(self):
        return hashlib.sha256(encode(asdict(self)).encode()).hexdigest()

    def __post_init__(self):
        identifier(self.plugin_id)
        identifier(self.environment)
        identifier(self.revision)
        identifier(self.repository)
        for dependency in self.dependencies:
            identifier(dependency)
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("Duplicate tool declaration")


@dataclass(frozen=True)
class ToolResult:
    succeeded: bool
    summary: str
    evidence: str

    def __post_init__(self):
        if type(self.succeeded) is not bool or not isinstance(self.summary, str) or not isinstance(self.evidence, str):
            raise ValueError("Malformed connector result")
        if not self.evidence:
            raise ValueError("Connector outcome requires evidence")


@dataclass(frozen=True)
class WriteLimits:
    target_argument: str
    maximum: int
    window: int
    cooldown: int

    def __post_init__(self):
        identifier(self.target_argument)
        if self.maximum < 1 or self.window < 1 or self.cooldown < 0:
            raise ValueError("Invalid external write limits")

    def for_arguments(self, arguments):
        target = identifier(arguments.get(self.target_argument))
        return target, self.maximum, self.window, self.cooldown


@dataclass
class _Plugin:
    manifest: Manifest
    connector: object
    access: object
    source: object
    allowed: frozenset[str]
    write_limits: WriteLimits | None
    handle: object = field(default_factory=object)
    state: str = "disabled"
    in_flight: asyncio.Task | None = None
    enabled: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AgentRegistry:
    def __init__(self, memory, *, environment, dependency_available, clock=time.time, timeout=30,
                 authenticate_approval=None, authorize_output=None, verify_outcome=None, protected_identities=None):
        # These callbacks belong to the trusted host. A connector never supplies them.
        self.authenticate_approval = authenticate_approval
        self.authorize_output = authorize_output
        self.protected_identities = protected_identities
        self.verify_outcome = verify_outcome
        self._live_approvers = {}
        self.memory = memory
        self.environment = environment
        self.dependency_available = dependency_available
        self.clock = clock
        self.timeout = timeout
        self.plugins = {}

    def register(self, manifest, connector, access, source, *, allowed=(), approved_manifest=None, write_limits=None):
        access.require(write=True)
        if manifest.plugin_id in self.plugins:
            raise ValueError("Plugin already registered")
        allowed = frozenset(allowed)
        if not allowed <= {tool.name for tool in manifest.tools}:
            raise ValueError("Grant references undeclared tool")
        writes = {tool.name for tool in manifest.tools if tool.risk is Risk.WRITE}
        if allowed & writes:
            if approved_manifest != manifest.fingerprint:
                raise PermissionError("Write grant requires explicit approval of the exact manifest")
            if not callable(getattr(connector, "invoke_checked", None)):
                raise ValueError("Write connectors require a last-mile authorization callback")
        slot = _Plugin(manifest, connector, access, source, allowed, write_limits)
        self.plugins[manifest.plugin_id] = slot
        return slot.handle

    def _slot(self, handle):
        for slot in self.plugins.values():
            if slot.handle is handle:
                return slot
        raise PermissionError("Unregistered plugin identity")

    async def enable(self, handle):
        slot = self._slot(handle)
        async with slot.lock:
            slot.access.require(write=True)
            if slot.enabled:
                return True
            try:
                if slot.state == "stop_failed":
                    return False
                if (slot.manifest.environment != self.environment or
                        not all(self.dependency_available(item) for item in slot.manifest.dependencies)):
                    slot.state = "dependency_missing"
                    return False
                await asyncio.wait_for(slot.connector.start(), self.timeout)
            except Exception:
                slot.state = "unhealthy"
                return False
            slot.enabled = True
            slot.state = "running"
            return True

    async def disable(self, handle):
        slot = self._slot(handle)
        # Revoke first. Existing operations with unknown outcomes remain claimed.
        slot.enabled = False
        for approval in tuple(self._live_approvers):
            if approval[0] is handle:
                self._live_approvers.pop(approval)
        if slot.in_flight is not None:
            slot.in_flight.cancel()
        async with slot.lock:
            slot.enabled = False
            try:
                await asyncio.wait_for(slot.connector.stop(), self.timeout)
            except Exception:
                slot.state = "stop_failed"
            else:
                slot.state = "disabled"

    async def health(self, handle):
        slot = self._slot(handle)
        async with slot.lock:
            if slot.enabled:
                try:
                    ready = await asyncio.wait_for(slot.connector.healthy(), self.timeout)
                except Exception:
                    ready = False
                slot.state = "running" if ready else "unhealthy"
            return slot.state

    def _tool(self, slot, name):
        slot.access.require(write=True)
        if not slot.enabled or slot.state != "running" or name not in slot.allowed:
            raise PermissionError("Plugin or tool is not authorized")
        return next(tool for tool in slot.manifest.tools if tool.name == name)

    @staticmethod
    def _intent(tool, arguments, manifest=None, source=None):
        if not isinstance(arguments, dict) or contains_secret(arguments):
            raise ValueError("Invalid or sensitive tool arguments")
        serialized = encode([tool.name, arguments, manifest.fingerprint if manifest else None,
                             asdict(source) if source else None])
        if len(serialized) > 8192:
            raise ValueError("Tool arguments are too large")
        return hashlib.sha256(serialized.encode()).hexdigest()

    @staticmethod
    def _legacy_intent(tool, arguments):
        return hashlib.sha256(encode([tool.name, arguments]).encode()).hexdigest()

    @staticmethod
    def _event_id(operation_id, state):
        return hashlib.sha256(key(operation_id, state.value).encode()).hexdigest()

    def _producer(self, slot):
        return "plugin:" + slot.manifest.plugin_id

    def _operation(self, slot, operation_id):
        operation = self.memory.operation(slot.access, self._producer(slot), operation_id)
        if operation and operation["source"] != encode(asdict(slot.source)):
            raise PermissionError("Plugin operation belongs to a different source")
        return operation

    def _write(self, slot, tool, operation_id, intent, state, summary, evidence="", **options):
        activity = Activity(self._event_id(operation_id, state), tool.kind, state, self.clock(),
                            summary, evidence, operation_id, intent)
        return self.memory.record(slot.access, activity, producer=self._producer(slot), source=slot.source, **options)

    def observe(self, handle, tool_name, *, event_id, occurred, summary, evidence):
        slot = self._slot(handle)
        tool = self._tool(slot, tool_name)
        if tool.risk is not Risk.READ or not evidence:
            raise PermissionError("Observation requires an authorized read tool and evidence")
        activity = Activity("observed:" + event_id, tool.kind, ActivityState.OBSERVED,
                            occurred, summary, evidence)
        return self.memory.record(slot.access, activity, producer=self._producer(slot), source=slot.source)

    def plan(self, handle, tool_name, operation_id, arguments, summary):
        slot = self._slot(handle)
        tool = self._tool(slot, tool_name)
        intent = self._intent(tool, arguments, slot.manifest, slot.source)
        current = self._operation(slot, operation_id)
        if current is None:
            self._write(slot, tool, operation_id, intent, ActivityState.PLANNED, summary)
            current = self._operation(slot, operation_id)
        if current["intent"] != intent:
            raise ValueError("Operation ID belongs to a different request")
        if tool.risk is Risk.WRITE and current["state"] == ActivityState.PLANNED:
            self._write(slot, tool, operation_id, intent, ActivityState.AWAITING_APPROVAL,
                        "等待操作审批")
            return ActivityState.AWAITING_APPROVAL
        return ActivityState(current["state"])

    def _output(self, slot, tool, arguments):
        authorization = (self.authorize_output(slot.source, tool.name, arguments)
                         if self.authorize_output is not None else None)
        if not isinstance(authorization, OutputAuthorization) or not authorization.matches(slot.source.account, arguments):
            raise PermissionError("Outbound destination or provenance is not authorized by the host")
        protected = self.protected_identities(slot.access) if self.protected_identities else ()
        check_external_output(self.memory, slot.access, arguments, protected_ids=protected)

    def approve(self, handle, tool_name, operation_id, arguments, event):
        """Consume an authenticated user action furnished by the host, with exact intent binding."""
        slot = self._slot(handle)
        tool = self._tool(slot, tool_name)
        intent = self._intent(tool, arguments, slot.manifest, slot.source)
        authenticated = (self.authenticate_approval(event, slot.source, tool_name, operation_id, intent)
                         if self.authenticate_approval is not None else None)
        if not isinstance(authenticated, ApprovalEvent):
            raise PermissionError("Trusted approval event required")
        event_id, approver = authenticated.event_id, authenticated.access
        now = self.clock()
        expires = authenticated.occurred + 300
        if authenticated.occurred > now or expires <= now:
            raise PermissionError("Approval event is stale or in the future")
        approver.require_recall()
        approver.require(write=True)
        if (approver.scope.kind != "private" or approver.scope != slot.access.scope
                or approver.principal != slot.access.principal):
            raise PermissionError("Approver is outside the operation scope")
        if tool.risk is not Risk.WRITE:
            raise ValueError("Only write tools require approval")
        current = self._operation(slot, operation_id)
        if not current or current["intent"] != intent or current["state"] != ActivityState.AWAITING_APPROVAL:
            raise ValueError("No matching operation awaiting approval")
        self._output(slot, tool, arguments)
        approver.require(write=True)
        receipt = "approval:" + hashlib.sha256(event_id.encode()).hexdigest()
        provenance = encode([approver.principal, asdict(approver.source), tool.name, asdict(slot.source),
                             authenticated.occurred])
        try:
            self._write(slot, tool, operation_id, intent, ActivityState.APPROVED, "操作已获授权", receipt,
                        approval=(event_id, provenance, expires))
        except sqlite3.IntegrityError as exc:
            raise PermissionError("Approval event already used") from exc
        self._live_approvers[(slot.handle, operation_id)] = approver, expires

    async def _invoke(self, slot, tool, arguments, approval):
        self._tool(slot, tool.name)
        if tool.risk is Risk.READ:
            return await slot.connector.invoke(tool.name, arguments)
        approver, expires = approval
        intent = self._intent(tool, arguments, slot.manifest, slot.source)
        checked = False

        def authorize():
            nonlocal checked
            if checked:
                raise PermissionError('External submission already authorized for this operation')
            if expires <= self.clock():
                raise PermissionError("Approval expired before dispatch")
            if self._intent(tool, arguments, slot.manifest, slot.source) != intent:
                raise PermissionError("Connector changed the approved request")
            approver.require(write=True)
            self._output(slot, tool, arguments)
            approver.require(write=True)
            self._tool(slot, tool.name)
            checked = True

        result = await slot.connector.invoke_checked(tool.name, arguments, authorize)
        if not checked or self._intent(tool, arguments, slot.manifest, slot.source) != intent:
            raise ValueError("Write connector skipped authorization or changed the request")
        return result

    def _verified(self, slot, tool, operation_id, arguments, result):
        if not isinstance(result, ToolResult):
            raise ValueError("Connector did not supply evidence")
        if tool.risk is Risk.WRITE and (self.verify_outcome is None or not
                self.verify_outcome(slot.source, tool.name, operation_id, arguments, result)):
            raise ValueError("External write requires independent host verification")

    async def execute(self, handle, tool_name, operation_id, arguments):
        slot = self._slot(handle)
        async with slot.lock:
            tool = self._tool(slot, tool_name)
            current = self._operation(slot, operation_id)
            intent = self._intent(tool, arguments, slot.manifest, slot.source)
            arguments = json.loads(encode(arguments))
            if not current:
                raise ValueError("Unknown operation")
            state = ActivityState(current["state"])
            claimed_states = {ActivityState.RUNNING, ActivityState.SUCCEEDED, ActivityState.FAILED}
            legacy_claim = state in claimed_states and current["intent"] == self._legacy_intent(tool, arguments)
            if current["intent"] != intent and not legacy_claim:
                raise ValueError("Changed operation intent")
            if state in claimed_states:
                return state
            required = ActivityState.APPROVED if tool.risk is Risk.WRITE else ActivityState.PLANNED
            if state is not required:
                raise PermissionError("Operation has no required approval")
            approval_key = (slot.handle, operation_id)
            approval = self._live_approvers.get(approval_key)
            if tool.risk is Risk.WRITE:
                if approval is None or self.verify_outcome is None:
                    raise PermissionError("Live host approval and verifier required")
                if approval[1] <= self.clock():
                    self._live_approvers.pop(approval_key, None)
                    raise PermissionError("Approval expired before claim")
                approval[0].require(write=True)
                self._output(slot, tool, arguments)
            limits = (slot.write_limits.for_arguments(arguments)
                      if tool.risk is Risk.WRITE and slot.write_limits else None)
            try:
                _, claimed = self._write(slot, tool, operation_id, intent,
                                         ActivityState.RUNNING, "开始执行操作", consume_approval=tool.risk is Risk.WRITE,
                                         write_limit=limits)
            except ValueError:
                latest = self._operation(slot, operation_id)
                if latest and latest["intent"] == intent and latest["state"] in {
                    ActivityState.RUNNING, ActivityState.SUCCEEDED, ActivityState.FAILED,
                }:
                    return ActivityState(latest["state"])
                raise
            self._live_approvers.pop(approval_key, None)
            if not claimed:
                return ActivityState.RUNNING
            try:
                slot.in_flight = asyncio.create_task(self._invoke(slot, tool, arguments, approval))
                result = await asyncio.wait_for(slot.in_flight, self.timeout)
                self._verified(slot, tool, operation_id, arguments, result)
                state = ActivityState.SUCCEEDED if result.succeeded else ActivityState.FAILED
                self._write(slot, tool, operation_id, intent, state, result.summary, result.evidence)
            except BaseException:
                # A transport error does not prove the side effect failed. Preserve
                # the claim for reconciliation and prevent automatic replay.
                slot.state = "unhealthy"
                raise
            finally:
                slot.in_flight = None
            return state

    def reconcile(self, handle, tool_name, operation_id, arguments, approver, result):
        slot = self._slot(handle)
        approver.require(write=True)
        if approver.scope != slot.access.scope or approver.principal != slot.access.principal:
            raise PermissionError("Reconciliation outside the operation scope")
        if not isinstance(result, ToolResult):
            raise ValueError("Reconciliation requires verified connector evidence")
        if tool_name not in slot.allowed:
            raise PermissionError("Tool is not authorized")
        tool = next(tool for tool in slot.manifest.tools if tool.name == tool_name)
        current = self._operation(slot, operation_id)
        intent = self._intent(tool, arguments, slot.manifest, slot.source)
        if (not current or current["state"] != ActivityState.RUNNING
                or current["kind"] != tool.kind.value
                or current["intent"] not in {intent, self._legacy_intent(tool, arguments)}):
            raise ValueError("No matching operation pending reconciliation")
        intent = current["intent"]
        self._verified(slot, tool, operation_id, arguments, result)
        state = ActivityState.SUCCEEDED if result.succeeded else ActivityState.FAILED
        self._write(slot, tool, operation_id, intent, state, result.summary, result.evidence)
        return state
