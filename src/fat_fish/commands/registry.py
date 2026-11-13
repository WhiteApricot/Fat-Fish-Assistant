"""Built-in and extension dispatch for platform-neutral management commands."""
from dataclasses import dataclass
import json
from typing import Callable

from fat_fish.commands.intents import ROUTES


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
                or name in {"chat", "comfirm", "remind"} or name in self.routes()):
            raise ValueError("Invalid or duplicate command name")
        self.registrations[name] = CommandRegistration(routes, handle, help, authorize)

    def routes(self):
        return {**ROUTES, **{name: entry.routes for name, entry in self.registrations.items()}}

    def guidance(self, base):
        additions = {name: {arg: sorted(counts) for arg, counts in entry.routes.items()}
                     for name, entry in self.registrations.items() if name not in ROUTES}
        return base + "\n扩展管理命令：" + json.dumps(additions, ensure_ascii=False) if additions else base

    def help(self, admission):
        return {name: entry.help for name, entry in self.registrations.items()
                if entry.authorize is not None and entry.authorize(admission)}

    def find(self, name):
        return self.registrations.get({"remind": "reminder", "comfirm": "confirm"}.get(name, name))

    async def dispatch(self, command, admission, allowed, *, builtin_args):
        entry = self.find(command.name)
        if entry is None:
            return None
        if admission is None or not allowed():
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
