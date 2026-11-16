"""Built-in and extension dispatch for platform-neutral management commands."""
import asyncio
from dataclasses import dataclass
from typing import Callable

from fat_fish.persona.persona_settings import PERSONA_ROUTES


# Allowed argument counts include the subcommand itself.
ROUTES = {
    'help': {'': {0}, '*': {1}},
    'role': {'': {0}, 'help': {1}, 'list': {1, 2}, 'add': {2, 3}, 'remove': {2, 3},
             'set': {3, 4}, 'grant': {5}, 'moderator-add': {2}, 'moderator-remove': {2}, 'global': {2, 3}, 'undo': {1}},
    'globaladmin': {'list': {1}, 'add': {2}, 'remove': {2}},
    'history': {'': {0}, 'all': {1}},
    'confirm': {'': {0}},
    'cancel': {'': {0}},
    'undo': {'': {0}, '*': {1}},
    'new': {'': {0}},
    'naturalcommand': {'': {0}, 'on': {1}, 'off': {1}, 'global': {1, 2}},
    'websearch': {'': {0}, 'on': {1}, 'off': {1}, 'global': {1, 2}},
    'memory': {'': {0}, 'help': {1}, 'reset': {1}, 'search': {2}, 'remember': {2},
               'update': {4}, 'delete': {3}, 'archive': {2}, 'history': {1, 2}, 'card': {1, 2},
               'card-archive': {1, 2}, 'status': {1}, 'on': {1}, 'off': {1}, 'confirm': {1}, 'cancel': {1}},
    'groupmemory': {'': {0}, 'help': {1}, 'reset': {1}, 'search': {2}, 'remember': {2},
                    'update': {3}, 'delete': {2}, 'status': {1}, 'on': {1}, 'off': {1}, 'cancel': {1}},
    'reminder': {'': {0}, 'help': {1}, 'add': {3}, 'addto': {4}, 'add-exact': {4}, 'addto-exact': {5},
                 'list': {1}, 'show': {2},
                 'delete': {2}, 'pause': {2}, 'resume': {2}, 'time': {3}, 'text': {3},
                 'select': {2, 3}, 'audio': {1}},
    'groupmode': {'': {0}, 'help': {1}, 'mention': {1}, 'smart': {1}},
    'groupsettings': {'': {0}, 'help': {1}, 'cooldown': {2}},
    'whitelist': {'': {0}, 'help': {1}, 'add': {2}, 'remove': {2}, 'list': {1}},
    'blacklist': {'': {0}, 'help': {1}, 'add': {2}, 'remove': {2}, 'list': {1}},
    'persona': PERSONA_ROUTES,
    'globalpersona': PERSONA_ROUTES,
    'privatepersona': {'': {0}, 'help': {1}, 'set': {2}, 'reset': {1}, 'undo': {1}},
    'grouppersona': {'': {0}, 'help': {1}, 'set': {2}, 'reset': {1}},
    'personal': {'': {0}, 'help': {1}, '简短': {1}, '标准': {1}, '详细': {1}},
}

LOOKUP_ACTIONS = {
    "help": {"", "*"}, "history": {"", "all"}, "reminder": {"", "list", "show", "audio"},
    "persona": {"", "list"}, "globalpersona": {"", "list"},
    "role": {"", "list"}, "globaladmin": {"list"}, "blacklist": {"", "list"}, "whitelist": {"", "list"},
    "memory": {"search", "archive", "history", "status"}, "groupmemory": {"search", "status"},
    "personal": {""}, "groupmode": {""}, "groupsettings": {""},
    "naturalcommand": {""}, "websearch": {""},
}


def is_lookup(command):
    if command.name not in ROUTES:
        return False
    if command.args == ("help",) or command.name == "help":
        return True
    if command.name in {"naturalcommand", "websearch"} and command.args == ("global",):
        return True
    return (command.args[0] if command.args else "") in LOOKUP_ACTIONS.get(command.name, ())


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class CommandRegistration:
    routes: dict
    handle: Callable
    help: str = ""
    authorize: Callable | None = None


class CommandRegistry:
    def __init__(self, builtins=None):
        self.registrations = {}
        if builtins is not None and builtins.keys() != ROUTES.keys():
            raise ValueError("Built-in command dispatch differs from the intent routes")
        for name, handler in (builtins or {}).items():
            self.registrations[name] = CommandRegistration(ROUTES[name], handler)

    def register(self, name, *, routes, help, authorize, handle):
        if (not name.isascii() or not name.isalnum() or name != name.lower()
                or name in {"chat", "comfirm", "remind", "webresearch"} or name in self.routes()):
            raise ValueError("Invalid or duplicate command name")
        self.registrations[name] = CommandRegistration(routes, handle, help, authorize)

    def routes(self):
        return {**ROUTES, **{name: entry.routes for name, entry in self.registrations.items()}}

    def help(self, admission):
        return {name: entry.help for name, entry in self.registrations.items()
                if entry.authorize is not None and entry.authorize(admission)}

    def find(self, name):
        return self.registrations.get({"remind": "reminder", "comfirm": "confirm", "webresearch": "websearch"}.get(name, name))

    def proposal(self, name, args):
        if not isinstance(name, str) or name not in self.registrations or not isinstance(args, list):
            return None
        if len(args) > 12 or any(not isinstance(arg, str) or not arg.strip() or len(arg) > 2000
                                 or any(ch in arg for ch in "\r\n\x00") for arg in args):
            return None
        routes = self.registrations[name].routes
        if len(args) not in routes.get(args[0] if args else "", routes.get("*", ())):
            return None
        return Command(name, tuple(arg.strip() for arg in args))

    async def dispatch(self, command, admission, allowed, *, builtin_args):
        entry = self.find(command.name)
        if entry is None:
            return None
        if admission is None or not await asyncio.to_thread(allowed):
            return "当前会话权限不足。"
        if entry.authorize is None:
            # Built-in handlers preserve the legacy slash syntax and validate each mutation.
            return await entry.handle(*builtin_args)
        if not entry.authorize(admission):
            return "当前会话权限不足。"
        selector = command.args[0] if command.args else ""
        if len(command.args) not in entry.routes.get(selector, entry.routes.get("*", ())):
            return entry.help
        return await entry.handle(command, admission, allowed)
