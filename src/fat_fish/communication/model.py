"""Transport-independent addresses and message envelope."""
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid identifier")
    return value


def key(*fields):
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))


class NotSubmitted(PermissionError):
    """The trusted adapter confirms it stopped before external dispatch."""


class ChatKind(str, Enum):
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"


class Capability(str, Enum):
    TEXT = "text"
    MEDIA = "media"
    QUOTE = "quote"
    PUSH = "push"
    DIRECT_MESSAGE = "direct_message"
    GROUP_MESSAGE = "group_message"
    CHANNEL_MESSAGE = "channel_message"
    IMAGE = "image"
    FILE = "file"
    VOICE = "voice"
    EDIT = "edit"
    RETRACT = "retract"


@dataclass(frozen=True)
class ProviderId:
    platform: str
    instance: str

    def __post_init__(self):
        identifier(self.platform)
        identifier(self.instance)


@dataclass(frozen=True)
class AdapterManifest:
    adapter_id: str
    version: str
    configuration_schema: int
    address: ProviderId
    capabilities: frozenset[Capability]

    def __post_init__(self):
        identifier(self.adapter_id)
        identifier(self.version)
        if self.configuration_schema < 1:
            raise ValueError("Invalid adapter configuration schema")


@dataclass(frozen=True)
class Account:
    provider: ProviderId
    subject: str

    def __post_init__(self):
        identifier(self.subject)

    @property
    def storage_key(self):
        return key(self.provider.platform, self.provider.instance, self.subject)


@dataclass(frozen=True)
class Conversation:
    provider: ProviderId
    bot: str
    kind: ChatKind
    peer: str
    thread: str

    def __post_init__(self):
        for value in (self.bot, self.peer, self.thread):
            identifier(value)
        if not isinstance(self.kind, ChatKind):
            raise ValueError("Invalid conversation kind")

    @property
    def storage_key(self):
        return key(self.provider.platform, self.provider.instance, self.bot,
                   self.kind.value, self.peer, self.thread)


@dataclass(frozen=True)
class Media:
    kind: str
    reference: str | None = None
    content_type: str | None = None
    filename: str | None = None


@dataclass(frozen=True)
class MessageEvent:
    conversation: Conversation
    sender: Account
    message_id: str
    timestamp: float
    text: str
    mentions: tuple[str, ...] = ()
    reply_to: str | None = None
    media: tuple[Media, ...] = ()
    quoted_sender: str | None = None
    adapter_id: str | None = None
    raw_payload_reference: str | None = None

    def __post_init__(self):
        identifier(self.message_id)
        if self.sender.provider != self.conversation.provider:
            raise ValueError("Provider mismatch")
        if self.conversation.kind is ChatKind.PRIVATE and self.sender.subject != self.conversation.peer:
            raise ValueError("Private peer mismatch")
        if not math.isfinite(self.timestamp) or self.timestamp < 0:
            raise ValueError("Invalid timestamp")
        if self.reply_to is not None:
            identifier(self.reply_to)
        if self.quoted_sender is not None:
            identifier(self.quoted_sender)
        if self.adapter_id is not None:
            identifier(self.adapter_id)
        for mention in self.mentions:
            identifier(mention)

    @property
    def event_id(self):
        return hashlib.sha256(key(self.conversation.storage_key, self.message_id).encode()).hexdigest()


@dataclass(frozen=True)
class MessageRetraction:
    """A provider-confirmed withdrawal of an inbound message."""
    conversation: Conversation
    actor: Account
    message_id: str

    def __post_init__(self):
        identifier(self.message_id)
        if self.actor.provider != self.conversation.provider:
            raise ValueError("Provider mismatch")
        if self.conversation.kind is ChatKind.PRIVATE and self.actor.subject != self.conversation.peer:
            raise ValueError("Private peer mismatch")

    @property
    def event_id(self):
        return hashlib.sha256(key(self.conversation.storage_key, self.message_id).encode()).hexdigest()


@dataclass(frozen=True)
class Outgoing:
    text: str
    media: tuple[Media, ...] = ()
    reply_to: str | None = None
    mentions: tuple[str, ...] = ()

    @property
    def requirements(self):
        requirements = {Capability.TEXT}
        if self.media:
            requirements.add(Capability.MEDIA)
        if self.reply_to:
            requirements.add(Capability.QUOTE)
        if self.mentions:
            requirements.add(Capability.GROUP_MESSAGE)
        return requirements
