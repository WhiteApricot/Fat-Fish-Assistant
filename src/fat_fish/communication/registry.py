"""Discover trusted provider definitions and configure independent runtime instances."""
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from importlib import import_module
from importlib.metadata import entry_points
from importlib.util import find_spec
import json
import logging
from pathlib import Path

from fat_fish.communication.model import Capability, ProviderId, identifier


logger = logging.getLogger(__name__)
BUILTINS = Path(__file__).with_name("providers")
PACKAGE_GROUP = "fat_fish.communication_providers"


@dataclass(frozen=True)
class ProviderContext:
    platform_instance: Callable[[], object]
    send_native: Callable[[str, object], Awaitable[bool]]
    speech_provider: Callable[[str], Awaitable[object]] | None = None


@dataclass(frozen=True)
class ProviderDefinition:
    provider_id: str
    name: str
    version: str
    entrypoint: str
    supported_platforms: frozenset[str]
    capabilities: frozenset[Capability]
    configuration_schema: dict
    required_dependencies: tuple[str, ...]
    verification_capabilities: frozenset[str]
    lifecycle_capabilities: frozenset[str]

    @classmethod
    def read(cls, data):
        module, separator, name = data["entrypoint"].partition(":")
        if not separator or not name.isidentifier() or not all(p.isidentifier() for p in module.split(".")):
            raise ValueError("Invalid provider entrypoint")
        schema = data["configuration_schema"]
        if not isinstance(schema, dict) or any(t not in {"str", "int", "bool"} for t in schema.values()):
            raise ValueError("Invalid provider configuration schema")

        def strings(field):
            values = data[field]
            if not isinstance(values, list):
                raise ValueError("Expected a provider manifest list")
            return tuple(identifier(value) for value in values)

        platforms = frozenset(strings("supported_platforms"))
        if not platforms:
            raise ValueError("Provider must declare a platform")
        return cls(
            identifier(data["provider_id"]), identifier(data["name"]), identifier(data["version"]),
            data["entrypoint"], platforms, frozenset(Capability(value) for value in strings("capabilities")),
            schema, strings("required_dependencies"), frozenset(strings("verification_capabilities")),
            frozenset(strings("lifecycle_capabilities")),
        )

    def validate_config(self, config):
        types = {"str": str, "int": int, "bool": bool}
        if not isinstance(config, dict) or config.keys() != self.configuration_schema.keys() or any(
            type(config[name]) is not types[expected] for name, expected in self.configuration_schema.items()
        ):
            raise ValueError("Invalid provider instance configuration")
        for dependency in self.required_dependencies:
            if find_spec(dependency) is None:
                raise ImportError("Provider dependency unavailable")

    def create(self, address, bot, config, context):
        module, _, symbol = self.entrypoint.partition(":")
        factory = getattr(import_module(module), symbol)
        provider = factory(address, bot, context, config)
        if provider.address != address or provider.bot != bot or provider.capabilities != self.capabilities:
            raise ValueError("Provider instance does not match its manifest or configuration")
        for method in ("start", "stop", "healthy", "send_checked"):
            if not callable(getattr(provider, method, None)):
                raise ValueError("Provider runtime interface is incomplete")
        if self.verification_capabilities and (
            getattr(provider, "verification_capabilities", None) != self.verification_capabilities
            or not callable(getattr(provider, "verify_account", None))
        ):
            raise ValueError("Provider verification interface unavailable")
        if Capability.RETRACT in self.capabilities and any(
            not callable(getattr(provider, method, None))
            for method in ("convert_retraction", "verify_retraction")
        ):
            raise ValueError("Provider retraction interface unavailable")
        return provider


class ProviderRegistry:
    def __init__(self, path):
        self.path = path
        self._stamp = None
        self._instances = {}

    def discover(self, trusted_packages=()):
        definitions = {}
        conflicts = set()

        def register(definition):
            provider_id = definition.provider_id
            if provider_id in definitions or provider_id in conflicts:
                definitions.pop(provider_id, None)
                conflicts.add(provider_id)
                logger.warning("FFA provider identifier is ambiguous")
                return
            definitions[provider_id] = definition

        for manifest in sorted(BUILTINS.glob("*.json")):
            try:
                register(ProviderDefinition.read(json.loads(manifest.read_text(encoding="utf-8"))))
            except (OSError, KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("FFA built-in provider discovery failed (%s)", type(exc).__name__)
        for entry in entry_points(group=PACKAGE_GROUP):
            if entry.dist is None or entry.dist.name not in trusted_packages:
                continue
            try:
                definition = ProviderDefinition.read(entry.load()())
                if definition.provider_id != entry.name:
                    raise ValueError("Provider entry point name mismatch")
                register(definition)
            except Exception as exc:
                logger.warning("FFA installed provider discovery failed (%s)", type(exc).__name__)
        return definitions

    def _configuration(self):
        if not self.path.is_file():
            return [], {}
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 2:
            raise ValueError("Unsupported communication configuration")
        rows = data["providers"]
        if not isinstance(rows, list):
            raise ValueError("Expected provider instance list")
        trusted = data.get("trusted_packages", [])
        if not isinstance(trusted, list) or any(not isinstance(name, str) for name in trusted):
            raise ValueError("Invalid trusted provider package list")
        return rows, self.discover(trusted)

    def configured(self):
        try:
            metadata = self.path.stat()
        except FileNotFoundError:
            self._stamp = None
            self._instances = {}
            return {}
        stamp = metadata.st_mtime_ns, metadata.st_size
        if stamp == self._stamp:
            return self._instances.copy()
        rows, definitions = self._configuration()
        seen = set()
        instances = {}
        for row in rows:
            try:
                address = ProviderId(row["platform"], row["instance"])
                if address in seen:
                    instances.pop(address, None)
                    raise ValueError("Duplicate provider address")
                seen.add(address)
                if row.get("enabled", True) is not True:
                    continue
                definition = definitions[row["provider"]]
                if address.platform not in definition.supported_platforms:
                    raise ValueError("Platform unavailable for provider")
                config = row.get("config", {})
                definition.validate_config(config)
                instances[address] = (definition, identifier(row["bot"]), config)
            except (ImportError, KeyError, TypeError, ValueError) as exc:
                logger.warning("FFA provider configuration rejected (%s)", type(exc).__name__)
        self._stamp, self._instances = stamp, instances
        return instances.copy()

    async def ensure(self, runtime, address, bot, context):
        if address in runtime.connections:
            return
        instance = self.configured().get(address)
        if instance is None:
            return
        if instance[1] != bot:
            return
        await self.start(runtime, address, instance, context)

    async def start(self, runtime, address, instance, context):
        if address in runtime.connections:
            return
        definition, bot, config = instance
        try:
            provider = definition.create(address, bot, config.copy(), context)
            runtime.register(provider)
        except Exception as exc:
            logger.warning("FFA provider registration failed (%s)", type(exc).__name__)
            return
        await runtime.enable(address)
