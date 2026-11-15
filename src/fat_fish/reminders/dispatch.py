"""Reminder authorization and delivery through canonical account and room addresses."""
import json

from fat_fish.communication.model import Account, ChatKind, Conversation, Outgoing, ProviderId


def address(row):
    platform, instance, bot, kind, peer, thread = json.loads(row["scope"])
    provider = ProviderId(platform, instance)
    return Conversation(provider, bot, ChatKind(kind), peer, thread), Account(provider, row["creator"])


def operation_id(row):
    return f"reminder:{row['id']}:{row['due']}"


class ReminderAccess:
    def __init__(self, runtime, room):
        self.permissions = runtime.permissions
        self.room = room

    def admission(self, subject):
        return self.permissions.conversation(Account(self.room.provider, subject), self.room)

    def principal(self, subject):
        admission = self.admission(subject)
        return admission.principal_id if admission else None

    def can_moderate(self, subject):
        admission = self.admission(subject)
        return bool(admission and self.permissions.can_moderate(admission))


class ReminderDispatch:
    def __init__(self, store, runtime, render=None):
        self.store = store
        self.runtime = runtime
        self.render = render

    def allowed(self, row):
        latest = next((item for item in self.store.rows() if item["id"] == row["id"]), None)
        if (latest is None or latest["state"] != "active" or latest["updated"] != row["updated"]
                or latest["due"] != row["due"]):
            return False
        try:
            room, actor = address(row)
        except (TypeError, ValueError):
            return False
        current = self.runtime.permissions.conversation(actor, room)
        if not current or current.principal_id != row["creator_principal"]:
            return False
        if row["target"] == actor.subject:
            return True
        target = Account(room.provider, row["target"])
        recipient = self.runtime.permissions.conversation(target, room)
        return (room.kind is ChatKind.GROUP and self.runtime.permissions.can_moderate(current) and recipient is not None
                and recipient.principal_id == row["target_principal"])

    async def authorize(self, row):
        return self.allowed(row)

    async def ready(self, row):
        room, _ = address(row)
        return await self.runtime.ready_for_push(room)

    def delivery_state(self, row):
        room, _ = address(row)
        operation = operation_id(row)
        return (self.runtime.outbound_state(room, operation)
                or self.runtime.outbound_state(room, operation + ":multipart"))

    async def send(self, row):
        room, actor = address(row)
        mentions = (row["target"],) if room.kind is ChatKind.GROUP else ()
        text = (await self.render(room, row["creator_principal"], row["body"]) if self.render
                else "提醒：" + row["body"])
        operation = operation_id(row)
        if not self.allowed(row):
            return False
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) <= 1:
            await self.send_part(row, actor, room, Outgoing(text, mentions=mentions), operation)
            return None

        # A restart must not regenerate a different set of parts after any of
        # the original parts may have been submitted.
        marker = operation + ":multipart"
        scope = "outbound:" + room.storage_key
        if not self.runtime.seen.begin_delivery(scope, marker):
            if self.delivery_state(row) in {"submitted", "delivered"}:
                return None
            raise ConnectionError("Multipart reminder delivery is unresolved")
        for index, line in enumerate(lines):
            await self.send_part(row, actor, room, Outgoing(line, mentions=mentions if index == 0 else ()),
                                 f"{operation}:part:{index}")
        self.runtime.seen.finish_delivery(scope, marker, "submitted")

    async def send_part(self, row, actor, room, message, operation):
        sent = await self.runtime.send(actor, room, message, operation, proactive=True,
                                       recheck=lambda: self.allowed(row))
        if not sent and self.runtime.outbound_state(room, operation) not in {"submitted", "delivered"}:
            raise ConnectionError("Reminder delivery is unresolved")

    async def tick(self):
        await self.store.tick(self.authorize, self.send, only=lambda row: bool(row["creator_principal"]),
                              ready=self.ready, delivery_state=self.delivery_state)
