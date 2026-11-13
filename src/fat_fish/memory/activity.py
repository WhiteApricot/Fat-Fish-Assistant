"""Evidence-bearing, scoped activity records shared by chat and Agent providers."""
from dataclasses import dataclass
from enum import Enum
import math
from typing import Callable

from fat_fish.communication.model import identifier, key
from fat_fish.memory.experience import contains_secret


class ActivityKind(str, Enum):
    MESSAGE_RECEIVED = "message_received"
    MESSAGE_SENT = "message_sent"
    LEGACY_GROUP_SUMMARY = "legacy_group_summary"
    BROWSE = "browse"
    READ = "read"
    COMMENT = "comment"
    TOOL = "tool"
    REMINDER = "reminder"
    TASK = "task"


class ActivityState(str, Enum):
    OBSERVED = "observed"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TRANSITIONS = {
    None: {ActivityState.PLANNED},
    ActivityState.PLANNED: {ActivityState.AWAITING_APPROVAL, ActivityState.RUNNING},
    ActivityState.AWAITING_APPROVAL: {ActivityState.APPROVED},
    ActivityState.APPROVED: {ActivityState.RUNNING},
    ActivityState.RUNNING: {ActivityState.SUCCEEDED, ActivityState.FAILED},
}


@dataclass(frozen=True)
class Source:
    platform: str
    provider: str
    bot: str
    account: str
    conversation: str

    def __post_init__(self):
        for value in (self.platform, self.provider, self.bot, self.account):
            identifier(value)
        if not isinstance(self.conversation, str) or not 0 < len(self.conversation) <= 2048:
            raise ValueError("Invalid source conversation")


@dataclass(frozen=True)
class Scope:
    kind: str
    subject: str

    def __post_init__(self):
        if self.kind not in {"private", "group"} or not isinstance(self.subject, str) or not 0 < len(self.subject) <= 2048:
            raise ValueError("Invalid memory scope")

    @property
    def storage_key(self):
        return key(self.kind, self.subject)


@dataclass(frozen=True)
class Access:
    """Created by the trusted FFA host. It is revalidated on every use."""
    principal: str
    scope: Scope
    source: Source
    verify: Callable[[], bool]
    may_write: bool = True
    audience: str = "conversation"
    purpose: str = "chat"
    moderate: Callable[[], bool] | None = None
    authenticated_chat: bool = False
    source_scope_allowed: Callable[[str], bool] | None = None

    def require_recall(self):
        self.require()
        if (self.audience != "conversation" or self.purpose not in {"chat", "operator"}
                or (not self.authenticated_chat and self.source.platform not in {"qq", "lark"})):
            raise PermissionError("Scoped recall is restricted to authenticated chat destinations")

    def require(self, *, write=False, moderate=False):
        if ((self.scope.kind == "private" and self.scope.subject != self.principal)
                or (write and not self.may_write) or not self.verify()
                or (moderate and (self.scope.kind != "group" or not self.moderate or not self.moderate()))):
            raise PermissionError("Memory authorization revoked or missing")


@dataclass(frozen=True)
class Activity:
    event_id: str
    kind: ActivityKind
    state: ActivityState
    occurred: float
    summary: str
    evidence: str = ""
    operation_id: str | None = None
    intent: str = ""

    def __post_init__(self):
        identifier(self.event_id)
        if not isinstance(self.kind, ActivityKind) or not isinstance(self.state, ActivityState):
            raise ValueError("Invalid activity kind or state")
        if not isinstance(self.occurred, (int, float)) or not math.isfinite(self.occurred) or not 0 <= self.occurred <= 253402300799:
            raise ValueError("Invalid activity time")
        if self.state is ActivityState.OBSERVED:
            if self.operation_id or self.intent:
                raise ValueError("Observations cannot advance an operation")
        else:
            identifier(self.operation_id)
            identifier(self.intent)
        if self.state in {ActivityState.SUCCEEDED, ActivityState.FAILED} and not self.evidence:
            raise ValueError("Terminal outcome requires evidence")
        if not isinstance(self.summary, str) or len(self.summary) > 2048:
            raise ValueError("Invalid activity summary")
        if not isinstance(self.evidence, str) or len(self.evidence) > 512:
            raise ValueError("Invalid evidence reference")
        if contains_secret([self.summary, self.evidence]):
            raise ValueError("Credentials cannot be stored in activity")
