"""Verified source envelopes shared by external content and Work connectors."""

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import ipaddress
import json
import re
from urllib.parse import urlsplit

from fat_fish.memory.experience import sensitive_information
from fat_fish.memory.policy import ScopeKind, ScopeRef
from fat_fish.memory.records import EventRecord, ItemRecord, SourceRecord


class ContentAction(str, Enum):
    OPENED = "opened"
    READ = "read"
    ENDORSED = "endorsed"
    RETRIEVED = "retrieved"


class WorkPhase(str, Enum):
    PLAN = "planned"
    STARTED = "running"
    SUBMITTED = "submitted"
    TOOL_SUCCEEDED = "tool_succeeded"
    VERIFIED = "verified"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ContentExperience:
    platform: str
    content_id: str
    url: str
    author: str
    published_at: int | None
    retrieved_at: int
    revision: str
    summary: str
    action: ContentAction


@dataclass(frozen=True)
class WorkReceipt:
    operation_id: str
    project: str
    phase: WorkPhase
    occurred_at: int
    summary: str
    artifact_ids: tuple[str, ...] = ()
    approval_id: str | None = None


@dataclass(frozen=True)
class PublishedReceipt:
    scope: ScopeRef
    source_key: str
    item_id: int
    revision: int


def _clean(value, maximum=160):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or sensitive_information(value)):
        raise ValueError("Invalid source field")
    return value


def _public_host(host):
    if not host or host.endswith("."):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return bool(re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}", host)
                    and not host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".onion")))
    return address.is_global and not address.is_multicast


def _fingerprint(*values):
    return sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _timestamp(value, now):
    if type(value) is not int or not 0 <= value <= now:
        raise ValueError("Invalid source time")
    return value


class SourceIngress:
    """Connectors attest the receipt. Models are downstream consumers."""

    def __init__(self, repository, agent):
        self.repository = repository
        self.agent = _clean(agent)

    def content(self, value: ContentExperience, *, receipt, verify, now, principal=None, expires_at=None):
        if not isinstance(value.action, ContentAction):
            raise ValueError("Unknown content action")
        url = urlsplit(_clean(value.url, 500))
        if (url.scheme != "https" or not _public_host(url.hostname)
                or url.port not in (None, 443) or url.username or url.password or url.query or url.fragment):
            raise ValueError("Content URL must be a canonical public HTTPS URL")
        now = _timestamp(now, now)
        observed = _timestamp(value.retrieved_at, now)
        published = None if value.published_at is None else _timestamp(value.published_at, observed)
        if expires_at is not None and (type(expires_at) is not int or expires_at <= now):
            raise ValueError("Content receipt has expired")
        if value.action is ContentAction.RETRIEVED:
            if principal is not None:
                raise ValueError("A shareable retrieval cannot carry a requester identity")
            scope = ScopeRef(self.agent, ScopeKind.PUBLIC, self.agent)
            actor, kind = "bot", "public_attestation"
        else:
            scope = ScopeRef(self.agent, ScopeKind.PRIVATE, _clean(principal))
            actor, kind = "user", "content_interaction"
        metadata = {"platform": _clean(value.platform), "content_id": _clean(value.content_id),
                    "url": value.url, "author": _clean(value.author), "published_at": published,
                    "retrieved_at": observed, "revision": _clean(value.revision),
                    "action": value.action.value}
        body = f"{value.action.value}: {_clean(value.summary, 800)}"
        identity = _fingerprint("content", _clean(receipt))
        digest = _fingerprint(identity, metadata, body, expires_at)
        source = SourceRecord(scope, identity, kind, metadata["platform"], "verified_connector",
                              digest, observed, principal, expires_at)
        event = EventRecord(source, metadata["content_id"], "content_experience", "observed",
                            actor, observed, body, principal, metadata)
        return self._publish(event, now, verify, metadata, identity)

    def work(self, value: WorkReceipt, *, principal, producer, receipt, verify, now):
        if not isinstance(value.phase, WorkPhase):
            raise ValueError("Unknown work phase")
        scope = ScopeRef(self.agent, ScopeKind.PRIVATE, _clean(principal))
        observed = _timestamp(value.occurred_at, now)
        operation = _clean(value.operation_id)
        if len(value.artifact_ids) > 16:
            raise ValueError("Too many work artifact references")
        metadata = {"operation_id": operation, "project": _clean(value.project), "phase": value.phase.value,
                    "artifacts": [_clean(item, 200) for item in value.artifact_ids],
                    "approval": None if value.approval_id is None else _clean(value.approval_id)}
        kind = "work_plan" if value.phase is WorkPhase.PLAN else "work_receipt"
        trust = "owner_confirmed" if value.phase is WorkPhase.PLAN else "verified_connector"
        body = f"{value.phase.value}: {_clean(value.summary, 800)}"
        phase = {"tool_succeeded": "submitted", "verified": "succeeded"}.get(value.phase.value, value.phase.value)
        identity = _fingerprint("work", _clean(producer), _clean(receipt))
        digest = _fingerprint(identity, metadata, body, observed)
        source = SourceRecord(scope, identity, kind, producer, trust, digest, observed, principal)
        event = EventRecord(source, operation, kind, phase, "agent", observed, body, principal, metadata)
        return self._publish(event, now, verify, metadata, identity)

    def _publish(self, event, now, verify, metadata, slot):
        source = event.source
        def item(proof):
            return ItemRecord(source.scope, self.agent, source.author or self.agent, event.kind, slot,
                              event.phase, event.snippet, metadata, (proof,), source.trust, 1.0, .5)
        item_id, revision = self.repository.publish_verified_event(event, item, now, authorize=lambda: verify(event))
        return PublishedReceipt(source.scope, source.key, item_id, revision)

    def withdraw(self, published: PublishedReceipt, *, generation, verify, now):
        if published.scope.agent != self.agent:
            raise PermissionError("Receipt belongs to another installation audience")
        with self.repository.fence.mutation():
            if not verify():
                raise PermissionError("Receipt withdrawal is not authorized")
            return self.repository.erase("source", published.scope, published.source_key, generation, now)




    def withdraw_public_content(self, platform, content_id, *, generation, verify, now):
        """Revoke every published version and reject future replay of the asset."""
        scope = ScopeRef(self.agent, ScopeKind.PUBLIC, self.agent)
        target = self.repository.public_asset_key(_clean(platform), _clean(content_id))
        with self.repository.fence.mutation():
            if not verify():
                raise PermissionError("Public content withdrawal is not authorized")
            return self.repository.erase("public_asset", scope, target, generation, now)
