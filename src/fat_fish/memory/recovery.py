"""Connect recovery to separately managed installation keys and watermarks."""

import json
import os
import uuid
from pathlib import Path

from fat_fish.memory.erasure import ErasureLedger, LedgerError, Watermark
from fat_fish.memory.repository import MemoryRepository


def initialize_controls(controls: Path) -> Watermark:
    """Provision a new independently retained control directory, exclusively."""
    controls = controls.resolve()
    controls.mkdir(mode=0o700, parents=False, exist_ok=False)
    installation, key_id = uuid.uuid4().hex, "initial"
    key = os.urandom(32)
    payload = {"installation": installation, "first_key": key_id, "keys": {key_id: key.hex()}}
    _write_control(controls / "keys.json", json.dumps(payload, separators=(",", ":")).encode())
    ledger = ErasureLedger(controls / "erasure.jsonl", installation, {key_id: key}, key_id)
    mark = ledger.create()
    _write_control(controls / "trusted.watermark", ledger.anchor_path.read_bytes())
    ledger._sync_parent()
    return mark


def _write_control(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def open_controlled_repository(database: Path, controls: Path):
    controls = controls.resolve(strict=True)
    if controls.is_relative_to(database.parent.parent.resolve()):
        raise LedgerError("Erasure controls must be outside the live instance")
    return _open(database, controls)


def replay_snapshot(database: Path, controls: Path, snapshot_root: Path) -> Watermark:
    """Open a restored database only using independently retained controls.

    The control directory must be supplied by the deployment, never extracted
    from an instance snapshot. Its checkpoint is the deployment's minimum
    independently retained watermark.
    """
    controls = controls.resolve(strict=True)
    if controls.is_relative_to(snapshot_root.resolve()) or controls.is_relative_to(database.parent.parent.resolve()):
        raise LedgerError("Erasure controls must be outside the snapshot and restored instance")
    _, mark = _open(database, controls)
    return mark


def _open(database, controls):
    try:
        keys = json.loads((controls / "keys.json").read_text(encoding="utf-8"))
        independent = (controls / "trusted.watermark").read_bytes()
        metadata = json.loads(independent)
        mark = Watermark(metadata["installation"], metadata["seq"], metadata["tail_mac"])
        ledger = ErasureLedger(controls / "erasure.jsonl", keys["installation"],
                               {k: bytes.fromhex(v) for k, v in keys["keys"].items()}, keys["first_key"])
        actual = ledger.authenticate_watermark(independent)
        if actual != mark:
            raise LedgerError("Trusted erasure watermark is invalid")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise LedgerError("Erasure controls could not be loaded") from exc
    repository = MemoryRepository(database, ledger)
    return repository, repository.open(mark)
