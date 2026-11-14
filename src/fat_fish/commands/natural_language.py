"""Context and classification for management requests from any provider."""
import logging

from fat_fish.commands.intents import IntentUnavailable, classify, contains_external_material
from fat_fish.communication.model import ChatKind
from fat_fish.gateway.media_ingress import UNSUPPORTED_MEDIA
from fat_fish.memory.experience import contains_secret
from fat_fish.persona.chat_settings import feature_enabled
from fat_fish.persona.persona_settings import persona_catalog


logger = logging.getLogger(__name__)


class NaturalLanguageManager:
    def __init__(self, registry):
        self.registry = registry

    async def classify(self, context, event, message, admission, permissions, read_settings, *,
                       recent, pending, reminders):
        private = message.conversation.kind is ChatKind.PRIVATE
        room = message.conversation.storage_key
        principal = admission.principal_id
        setting = principal if private else room
        text = message.text.strip()
        if (context is None
                or (not private and message.conversation.bot not in message.mentions)
                or not text or len(text) > 2000 or text.startswith("/") or message.media
                or message.reply_to or contains_external_material(event)
                or any(type(part).__name__ in UNSUPPORTED_MEDIA for part in event.get_messages())):
            return None
        config = read_settings()
        if not feature_enabled(config, "naturalcommand", setting):
            return None
        scope = ("private:" if private else "group:") + setting
        notes = config.get("private_persona_notes" if private else "group_persona_notes", {})
        persona_numbers = {"persona": [row[0] for row in persona_catalog(
            config, scope, notes.get(setting, ""))["rows"]]}
        if permissions.is_global_admin(admission):
            persona_numbers["globalpersona"] = [row[0] for row in persona_catalog(
                config, "global", config.get("global_persona_notes", ""))["rows"]]
        reminder_context = [{"number": row["number"], "body": row["body"][:200]}
                            for row in (reminders.rows() if reminders else ())
                            if row["creator_principal"] == principal and row["scope"] == room
                            and row["state"] not in {"deleted", "done"} and not contains_secret(row["body"])][:20]
        try:
            return await classify(
                context, event, text, "" if private else message.conversation.peer,
                [subject for subject in message.mentions if subject != message.conversation.bot],
                recent=recent, pending=pending, reminders=reminder_context, persona_numbers=persona_numbers,
                registry=self.registry,
            )
        except IntentUnavailable as exc:
            logger.info("FFA intent classification unavailable (%s)", exc)
            return None
