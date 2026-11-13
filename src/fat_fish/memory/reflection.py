"""Scoped adapter for the pinned LivingMemory conversation processor."""
import asyncio
from collections import OrderedDict
import logging
import math
import sqlite3

from fat_fish.memory.experience import sensitive_information
from fat_fish.memory.service import normalize

logger = logging.getLogger(__name__)


class Reflection:
    def __init__(self):
        self.windows = OrderedDict()
        self.tasks = {}

    def observe(self, access, ref, text, occurred):
        key = access.scope.storage_key, access.source.conversation
        turns = self.windows.setdefault(key, [])
        if not any(turn[0] == ref for turn in turns):
            turns.append((ref, text[:1000], occurred))
            del turns[:-6]
        self.windows.move_to_end(key)
        if len(self.windows) > 32:
            self.windows.popitem(last=False)

    def schedule(self, service, access, processor, message_type, persona):
        key = access.scope.storage_key, access.source.conversation
        turns = tuple(self.windows.get(key, ()))
        if len(turns) < 3 or key in self.tasks:
            return
        task = asyncio.create_task(self._process(service, access, processor, message_type, persona, turns))
        self.tasks[key] = task

        def complete(done):
            self.tasks.pop(key, None)
            if not done.cancelled() and done.result():
                refs = {turn[0] for turn in turns}
                self.windows[key] = [turn for turn in self.windows.get(key, ()) if turn[0] not in refs]

        task.add_done_callback(complete)

    async def _process(self, service, access, processor, message_type, persona, turns):
        try:
            access.require_recall()
            messages = [message_type(id=ref, session_id=access.source.conversation,
                                     role="user", content=text, sender_id=access.principal,
                                     platform=access.source.platform, timestamp=at)
                        for ref, text, at in turns]
            _, metadata, importance = await asyncio.wait_for(processor.process_conversation(
                messages=messages, is_group_chat=False, persona_id=persona), 20)
            access.require_recall()
            if (metadata.get("summary_quality") == "low" or not isinstance(importance, (float, int))
                    or not math.isfinite(importance) or not .65 <= importance <= 1):
                return True
            facts = metadata.get("key_facts", [])
            if not isinstance(facts, list):
                return True
            for fact in facts[:3]:
                if not isinstance(fact, str) or not 5 <= len(fact.strip()) <= 180 or sensitive_information(fact):
                    continue
                grounded = normalize(fact)
                for ref, text, _ in reversed(turns):
                    if grounded in normalize(text):
                        try:
                            await asyncio.to_thread(service.remember, access, "reflection",
                                                    grounded, fact.strip(), importance, ref)
                        except PermissionError:
                            access.require_recall()
                            logger.info("FFA reflection skipped a revoked evidence item")
                        break
            return True
        except PermissionError:
            logger.info("FFA discarded revoked reflection inputs")
            return True
        except (OSError, ValueError, TypeError, AttributeError, TimeoutError, RuntimeError,
                sqlite3.Error) as exc:
            logger.warning("FFA memory reflection deferred (%s)", type(exc).__name__)
            return False
