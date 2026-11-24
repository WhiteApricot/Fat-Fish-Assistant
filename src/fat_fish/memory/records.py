"""Inputs accepted from trusted memory capture and consolidation callers."""

from dataclasses import dataclass
from typing import Any

from fat_fish.memory.policy import ScopeRef


@dataclass(frozen=True)
class SourceRecord:
    scope: ScopeRef
    key: str
    kind: str
    producer: str
    trust: str
    receipt_digest: str
    observed_at: int
    author: str | None = None
    expires_at: int | None = None


@dataclass(frozen=True)
class EventRecord:
    source: SourceRecord
    key: str
    kind: str
    phase: str
    actor_kind: str
    time: int
    snippet: str | None = None
    actor_ref: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class EvidenceRecord:
    source_id: int
    event_id: int | None = None
    grant_id: int | None = None
    role: str = "support"
    independence_key: str = ""
    weight: float = 1.0


@dataclass(frozen=True)
class ItemRecord:
    scope: ScopeRef
    observer: str
    subject: str
    kind: str
    slot: str
    variant: str
    body: str
    value: dict[str, Any]
    evidence: tuple[EvidenceRecord, ...]
    trust: str
    confidence: float
    importance: float
    valid_from: int | None = None
    valid_to: int | None = None
