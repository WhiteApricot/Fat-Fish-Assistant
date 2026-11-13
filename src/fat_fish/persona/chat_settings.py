"""Scoped chat preferences and persona instructions."""
from collections import OrderedDict
from copy import deepcopy
import json
from time import monotonic
from threading import Lock

from fat_fish.communication.model import ChatKind
from fat_fish.persona.persona_settings import PERSONA_HELP, edit_persona, effective_notes


class ChatSettings:
    def __init__(self, path):
        self.path = path
        self.lock = Lock()
        self.persona_undo = OrderedDict()
        self.preference_undo = OrderedDict()

    def read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def save(self, config):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)

    def prepare(self, event, message, admission):
        config = self.read()
        private = message.conversation.kind is ChatKind.PRIVATE
        subject = admission.principal_id
        group = "" if private else message.conversation.storage_key
        event.set_extra("_ffa_persona_notes", effective_notes(config, subject, group))
        if private:
            event.set_extra("_ffa_preference", config.get("preferences", {}).get(subject, "标准"))
        event.set_extra("_ffa_webresearch", config.get("webresearch", {}).get(group or subject, True))

    def latest_persona_undo(self, principal, room):
        with self.lock:
            for (actor, conversation, scope), entry in reversed(self.persona_undo.items()):
                if actor == principal and conversation == room and entry[0] > monotonic():
                    return entry[0] - 1800, "globalpersona" if scope == "global" else "persona"
        return None

    def undo_preference(self, command, message, admission, permissions, allowed):
        private = message.conversation.kind is ChatKind.PRIVATE
        if not private and not permissions.can_moderate(admission):
            return "此操作需要本群管理权限。"
        room = message.conversation.storage_key
        key = admission.principal_id if private else room
        field = {"personal": "preferences", "groupsettings": "cooldowns"}.get(command, command)
        with self.lock:
            config = self.read()
            current = config.get(field, {}).get(key)
            undo_key = admission.principal_id, room, command
            entry = self.preference_undo.get(undo_key)
            if (not allowed() or not entry or monotonic() >= entry[0] or current != entry[2]
                    or entry[3] != config.get("preference_revisions", {}).get(field, {}).get(key)):
                return "权限或配置已变化，未覆盖现有状态。"
            if entry[1] is None:
                config[field].pop(key, None)
            else:
                config[field][key] = entry[1]
            config["preference_revisions"][field][key] += 1
            self.save(config)
            self.preference_undo.pop(undo_key)
            return f"已撤销最近的 {command} 操作，恢复了此前设置。"

    def latest_preference_undo(self, principal, room):
        with self.lock:
            for (actor, conversation, command), entry in reversed(self.preference_undo.items()):
                if actor == principal and conversation == room and entry[0] > monotonic():
                    return entry[0] - 1800, command
        return None

    def command(self, event, message, admission, permissions, allowed):
        words = message.text.strip().split(maxsplit=1)
        if not words:
            return None
        name = words[0].lower().removeprefix("/")
        if not words[0].startswith("/") or name not in {
            "persona", "privatepersona", "grouppersona", "globalpersona", "personal", "webresearch",
            "naturalcommand", "groupsettings",
        }:
            return None
        payload = words[1] if len(words) > 1 else ""
        private = message.conversation.kind is ChatKind.PRIVATE
        actor = admission.principal_id
        group = message.conversation.storage_key
        if (name in {"privatepersona", "personal"} and not private
                or name in {"grouppersona", "groupsettings"} and private):
            return "此指令不适用于当前会话。"
        if name == "globalpersona" and (not private or not permissions.is_global_admin(admission)):
            return "请在授权私聊中管理全局人格。"
        if not private and not permissions.can_moderate(admission):
            return "此操作需要本群管理权限。"
        with self.lock:
            if not allowed():
                return "当前会话授权已变化。"
            config = self.read()
            if name == "personal":
                values = config.setdefault("preferences", {})
                field, key = "preferences", actor
                if payload in {"", "status"}:
                    return "个人回复长度：" + values.get(actor, "标准") + "；/personal short|normal|detailed"
                lengths = {"short": "简短", "normal": "标准", "detailed": "详细",
                           "简短": "简短", "标准": "标准", "详细": "详细"}
                if payload not in lengths:
                    return "格式：/personal 简短|标准|详细"
                value = lengths[payload]
                result = "个人回复长度已设为 " + lengths[payload]
            elif name in {"webresearch", "naturalcommand"}:
                values = config.setdefault(name, {})
                field = name
                key = actor if private else group
                label = "自然语言指令" if name == "naturalcommand" else "联网搜索"
                if payload in {"", "help"}:
                    state = "开启" if values.get(key, True) else "关闭"
                    return f"当前作用域{label}：{state}。使用 /{name} on|off。"
                if payload not in {"on", "off"}:
                    return f"用法：/{name} on|off"
                value = payload == "on"
                result = f"已保存当前作用域的{label}设置：" + ("开启" if payload == "on" else "关闭")
            elif name == "groupsettings":
                values = config.setdefault("cooldowns", {})
                field, key = "cooldowns", group
                if not payload:
                    from fat_fish.participation.participation import DEFAULT_COOLDOWN
                    return (f"本群冷却秒数：{values.get(group, DEFAULT_COOLDOWN)}；"
                            f"/groupsettings cooldown {DEFAULT_COOLDOWN}（30至1800秒）")
                args = payload.split()
                if len(args) != 2 or args[0] not in {"cooldown", "冷却"} or not args[1].isdecimal() or not 30 <= int(args[1]) <= 1800:
                    return "格式：/groupsettings 冷却 120，范围30至1800秒"
                value = int(args[1])
                result = "已调整本群智能插话冷却"
            else:
                args = payload.split(maxsplit=1)
                if args == ["help"]:
                    return PERSONA_HELP.format(command=name)
                if args[:1] == ["edit"] and len(args) == 2:
                    args = ["edit", *args[1].split(maxsplit=1)]
                scope = "global" if name == "globalpersona" else ("private:" + actor if private else "group:" + group)
                if scope == "global":
                    target, field = config, "global_persona_notes"
                else:
                    target = config.setdefault("private_persona_notes" if private else "group_persona_notes", {})
                    field = actor if private else group
                entries = config.setdefault("persona_entries", {})
                prior = entries.get(scope)
                note = target.get(field, "")
                undo_key = actor, message.conversation.storage_key, scope
                if args == ["undo"]:
                    saved = self.persona_undo.get(undo_key)
                    if not saved or monotonic() >= saved[0] or prior != saved[3]:
                        return "没有可撤销的人格修改，或人格已被更新。"
                    _, previous, previous_note, _ = saved
                    target[field] = previous_note
                    entries[scope] = {
                        "rows": previous["rows"] if previous else [],
                        "note": previous_note,
                        "next_id": prior["next_id"],
                        "revision": prior["revision"] + 1,
                    }
                    self.save(config)
                    self.persona_undo.pop(undo_key)
                    return "已恢复上一次修改前的人格要求。"
                entry, result = edit_persona(config, scope, note, args)
                if entry is None:
                    return result
                entry["revision"] = (prior or {}).get("revision", 0) + 1
                target[field] = entry["note"]
                entries[scope] = entry
                self.save(config)
                self.persona_undo[undo_key] = (monotonic() + 1800, deepcopy(prior), note, deepcopy(entry))
                self.persona_undo.move_to_end(undo_key)
                if len(self.persona_undo) > 512:
                    self.persona_undo.popitem(last=False)
                return result
            previous = values.get(key)
            if previous != value:
                values[key] = value
                revisions = config.setdefault("preference_revisions", {}).setdefault(field, {})
                revision = revisions.get(key, 0) + 1
                revisions[key] = revision
                self.save(config)
                undo_key = actor, group, name
                self.preference_undo[undo_key] = monotonic() + 1800, previous, value, revision
                self.preference_undo.move_to_end(undo_key)
                if len(self.preference_undo) > 512:
                    self.preference_undo.popitem(last=False)
            return result
