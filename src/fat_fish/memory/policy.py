"""Typed audiences for the memory service, derived from current platform admission."""

from dataclasses import dataclass
from enum import Enum
import json


class ScopeKind(str, Enum):
    PRIVATE = "private"
    SESSION = "session"
    GROUP = "group"
    PUBLIC = "agent_public"
    SHARED = "shared"


@dataclass(frozen=True)
class ScopeRef:
    agent: str
    kind: ScopeKind
    key: str

    @property
    def identity(self) -> str:
        return json.dumps((self.agent, self.kind.value, self.key), separators=(",", ":"))


@dataclass(frozen=True)
class Audience:
    """Issued by the trusted gateway using a current IdentityDirectory admission."""

    agent: str
    principal: str
    conversation_scope: ScopeRef
    authorization_revision: int
    granted_scopes: tuple[ScopeRef, ...] = ()

    def scopes(self) -> tuple[ScopeRef, ...]:
        base = self.conversation_scope
        if base.agent != self.agent or not self.principal:
            raise PermissionError("Invalid memory audience")
        if base.kind == ScopeKind.PRIVATE and base.key != self.principal:
            raise PermissionError("Private memory belongs to another principal")
        for scope in self.granted_scopes:
            if scope.agent != self.agent or scope.kind not in {ScopeKind.SHARED, ScopeKind.SESSION}:
                raise PermissionError("Unexpected memory scope in audience grant")
        public = ScopeRef(self.agent, ScopeKind.PUBLIC, self.agent)
        return tuple(dict.fromkeys((base, public, *self.granted_scopes)))
