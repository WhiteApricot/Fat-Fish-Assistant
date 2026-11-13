"""Local-only identity administration inside the protected FFA instance."""
import argparse
import json
from pathlib import Path

from fat_fish.communication.model import Account, ChatKind, Conversation, ProviderId
from fat_fish.communication.registry import ProviderRegistry
from fat_fish.core.identity import IdentityDirectory, Role


def account(values):
    return Account(ProviderId(*values[:2]), values[2])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("owner-providers")
    provision = commands.add_parser("provision")
    provision.add_argument("account", nargs=3, metavar="ACCOUNT", help="PLATFORM INSTANCE SUBJECT")
    provision.add_argument("role", choices=("member", "admin", "blocked"))
    invite = commands.add_parser("invite")
    invite.add_argument("existing", nargs=3)
    invite.add_argument("target", nargs=3)
    unlink = commands.add_parser("unlink")
    unlink.add_argument("account", nargs=3)
    room = commands.add_parser("room")
    room.add_argument("address", nargs=5, metavar="ADDRESS", help="PLATFORM INSTANCE BOT GROUP THREAD")
    room.add_argument("mode", choices=("mention", "smart", "open", "revoke"))
    room.add_argument("--member", action="append", default=[])
    commands.add_parser("memory-status")
    erasures = commands.add_parser("export-memory-erasures")
    erasures.add_argument("destination", type=Path)
    args = parser.parse_args()
    path = args.private.resolve(strict=True)
    if args.command == "memory-status":
        from fat_fish.gateway.platform_gateway import PlatformGateway
        bridge = PlatformGateway(private=path).memory_bridge
        options = bridge.options()
        status = {"enabled": bool(options), "embedding": "disabled"}
        space = None
        if options and options.get("embedding_space"):
            try:
                provider = bridge.embedding.provider(options)
                status["embedding"] = "configured" if provider else "unavailable"
                if provider:
                    space = provider.space(options["embedding_space"])
                    status.update(embedding_revision=provider.revision, embedding_dimensions=provider.dimensions)
            except (OSError, ValueError) as exc:
                status.update(embedding="invalid", embedding_error=type(exc).__name__)
        if (path / "ffa-memory.sqlite3").is_file():
            status.update(bridge.memory().status(space))
        print(json.dumps(status, ensure_ascii=False))
        return
    if args.command == "owner-providers":
        registry = ProviderRegistry(path / "ffa-communication.json")
        providers = {address: definition for address, (definition, _, _) in registry.configured().items()
                     if definition.verification_capabilities}
        print(json.dumps([
            {"platform": address.platform, "instance": address.instance, "provider": definition.provider_id,
             "verification": sorted(definition.verification_capabilities)}
            for address, definition in providers.items()
        ], ensure_ascii=False))
        return
    directory = IdentityDirectory(path / "ffa-identities.sqlite3")
    owner = directory.owner_account()
    if owner is None:
        parser.error("Set up the owner first")
    if args.command == "provision":
        directory.provision(owner, account(args.account), Role[args.role.upper()])
    elif args.command == "invite":
        print(directory.invite(owner, account(args.existing), account(args.target)))
    elif args.command == "unlink":
        directory.unlink(owner, account(args.account))
    elif args.command == "export-memory-erasures":
        from fat_fish.memory.service import MemoryService

        database = path / "ffa-memory.sqlite3"
        entries = MemoryService(database).export_erasures() if database.is_file() else []
        with args.destination.open("x", encoding="utf-8") as file:
            json.dump({"schema": 2, "owner_principal": directory.principal_id(owner), "entries": entries},
                      file, ensure_ascii=False)
    elif args.command == "room":
        platform, instance, bot, group, thread = args.address
        provider = ProviderId(platform, instance)
        conversation = Conversation(provider, bot, ChatKind.GROUP, group, thread)
        directory.set_room(owner, conversation, None if args.mode == "revoke" else args.mode,
                           [Account(provider, member) for member in args.member])
    else:
        parser.error("Command unavailable for this instance")


if __name__ == "__main__":
    main()
