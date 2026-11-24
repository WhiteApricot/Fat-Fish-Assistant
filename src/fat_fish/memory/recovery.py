"""Connect recovery to separately managed installation keys and watermarks."""

import hmac
import json
from pathlib import Path

from fat_fish.memory.erasure import ErasureLedger, LedgerError, Watermark
from fat_fish.memory.repository import MemoryRepository


def replay_snapshot(database: Path, controls: Path, snapshot_root: Path) -> Watermark:
    """Open a restored database only using independently retained controls.

    The control directory must be supplied by the deployment, never extracted
    from an instance snapshot. Its checkpoint is the deployment's minimum
    independently retained watermark.
    """
    controls = controls.resolve(strict=True)
    if controls.is_relative_to(snapshot_root.resolve()) or controls.is_relative_to(database.parent.parent.resolve()):
        raise LedgerError("Erasure controls must be outside the snapshot and restored instance")
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
    return MemoryRepository(database, ledger).open(mark)
