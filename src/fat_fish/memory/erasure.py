"""Authenticated deletion records, independent of SQLite database snapshots."""

from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

FORMAT = 1
GENESIS = "0" * 64
TARGETS = {"source", "item", "scope", "grant", "public_asset"}


class LedgerError(RuntimeError):
    """An erasure history that cannot safely authorize memory reads."""


def _encode(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(secret: bytes, domain: str, payload: bytes) -> str:
    return hmac.new(secret, domain.encode() + b"\0" + payload, hashlib.sha256).hexdigest()


def target_digest(secret: bytes, installation: str, kind: str, *parts: str) -> str:
    value = _encode({"installation": installation, "kind": kind, "parts": parts})
    return _digest(secret, "memory-erasure-target", value)


@dataclass(frozen=True)
class Watermark:
    installation: str
    seq: int
    tail_mac: str


@dataclass(frozen=True)
class Erasure:
    installation: str
    format: int
    seq: int
    key_id: str
    previous_mac: str
    kind: str
    scope_hmac: str
    target_hmac: str
    generation: int
    requested_at: int
    next_key_id: str | None = None
    next_key_proof: str | None = None
    mac: str = ""

    def payload(self) -> dict:
        fields = asdict(self)
        del fields["mac"]
        return fields


class ErasureLedger:
    """Access a ledger using the control plane's keys and trusted starting key ID.

    Call under the instance's exclusive write ownership. A restore additionally
    needs a checkpoint obtained outside the snapshot being restored.
    """

    def __init__(self, path: Path, installation: str, keys: Mapping[str, bytes], first_key: str):
        self.path = path
        self.anchor_path = path.with_name(path.name + ".watermark")
        self.installation = installation
        self.keys = dict(keys)
        self.first_key = first_key
        self._key(first_key)

    def _key(self, name: str) -> bytes:
        key = self.keys.get(name)
        if not isinstance(key, bytes) or len(key) < 32:
            raise LedgerError("Required erasure key is unavailable")
        return key

    def _anchor_bytes(self, mark: Watermark, key_id: str) -> bytes:
        payload = {"format": FORMAT, "key_id": key_id, **asdict(mark)}
        return _encode({**payload, "mac": _digest(self._key(key_id), "memory-erasure-watermark",
                                                  _encode(payload))})

    def authenticate_watermark(self, raw: bytes) -> Watermark:
        try:
            data = json.loads(raw)
            mark = Watermark(data["installation"], data["seq"], data["tail_mac"])
            expected = self._anchor_bytes(mark, data["key_id"])
            if mark.installation != self.installation or not hmac.compare_digest(raw, expected):
                raise LedgerError("Erasure watermark authentication failed")
            return mark
        except (TypeError, ValueError, KeyError) as exc:
            raise LedgerError("Invalid erasure watermark") from exc

    def _persist_anchor(self, mark: Watermark, key_id: str) -> None:
        fd, name = tempfile.mkstemp(prefix=".watermark-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(self._anchor_bytes(mark, key_id))
                stream.flush()
                os.fsync(stream.fileno())
            self._replace(Path(name), self.anchor_path)
            self._sync_parent()
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def _replace(source: Path, destination: Path) -> None:
        if os.name != "nt":
            os.replace(source, destination)
            return
        import ctypes
        from ctypes import wintypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        replace_existing, write_through = 0x1, 0x8
        if not move(str(source), str(destination), replace_existing | write_through):
            raise ctypes.WinError(ctypes.get_last_error())

    def _sync_parent(self) -> None:
        if os.name == "nt":
            # The deployment's independent watermark is required to protect
            # against rollback of the volume and directory metadata.
            return
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def create(self) -> Watermark:
        if self.anchor_path.exists():
            raise LedgerError("An erasure watermark already exists")
        mark = Watermark(self.installation, 0, GENESIS)
        with self.path.open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        self._persist_anchor(mark, self.first_key)
        return mark

    def read(self, expected: Watermark | None = None) -> tuple[tuple[Erasure, ...], Watermark, str]:
        rows = []
        tail = GENESIS
        key_id = self.first_key
        try:
            with self.path.open("rb") as stream:
                for seq, line in enumerate(iter(lambda: stream.readline(4097), b""), 1):
                    if not line.endswith(b"\n") or len(line) > 4096:
                        raise LedgerError("Invalid erasure record boundary")
                    row = Erasure(**json.loads(line))
                    if (row.installation != self.installation or row.format != FORMAT
                            or row.seq != seq or row.previous_mac != tail or row.key_id != key_id):
                        raise LedgerError("Erasure record is out of sequence")
                    mac = _digest(self._key(key_id), "memory-erasure-record", _encode(row.payload()))
                    if not hmac.compare_digest(mac, row.mac):
                        raise LedgerError("Erasure record authentication failed")
                    if row.kind == "rotate":
                        if not row.next_key_id or row.next_key_id == key_id or not row.next_key_proof:
                            raise LedgerError("Invalid erasure key rotation")
                        proof = _digest(self._key(row.next_key_id), "memory-erasure-rotation",
                                        _encode({"installation": self.installation, "seq": seq,
                                                 "previous_mac": tail, "key_id": row.next_key_id}))
                        if not hmac.compare_digest(proof, row.next_key_proof):
                            raise LedgerError("Erasure key rotation authentication failed")
                        key_id = row.next_key_id
                    elif row.kind not in TARGETS or row.next_key_id or row.next_key_proof:
                        raise LedgerError("Invalid erasure target")
                    rows.append(row)
                    tail = mac
            mark = Watermark(self.installation, len(rows), tail)
            # A crash after append but before publication can leave a valid tail.
            # Keep reads closed until the control plane recovers that state.
            if not hmac.compare_digest(self._anchor_bytes(mark, key_id), self.anchor_path.read_bytes()):
                raise LedgerError("Ledger and local watermark disagree")
            if expected is not None:
                if expected.installation != self.installation or not 0 <= expected.seq <= mark.seq:
                    raise LedgerError("Erasure ledger is older than the trusted watermark")
                check = GENESIS if expected.seq == 0 else rows[expected.seq - 1].mac
                if not hmac.compare_digest(check, expected.tail_mac):
                    raise LedgerError("Erasure ledger diverges from the trusted watermark")
            return tuple(rows), mark, key_id
        except (OSError, TypeError, ValueError) as exc:
            raise LedgerError("Could not validate the erasure ledger") from exc

    def _append(self, row: Erasure, active_key: str) -> Watermark:
        fields = row.payload()
        fields["mac"] = _digest(self._key(row.key_id), "memory-erasure-record", _encode(fields))
        with self.path.open("ab") as stream:
            stream.write(_encode(fields) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        mark = Watermark(self.installation, row.seq, fields["mac"])
        self._persist_anchor(mark, active_key)
        return mark

    def append(self, kind: str, scope: str, target: str, generation: int, requested_at: int) -> Watermark:
        if kind not in TARGETS or not scope or not target or generation < 0:
            raise ValueError("Invalid erasure target")
        _, mark, key_id = self.read()
        key = self._key(key_id)
        row = Erasure(self.installation, FORMAT, mark.seq + 1, key_id, mark.tail_mac, kind,
                      target_digest(key, self.installation, "scope", scope),
                      target_digest(key, self.installation, kind, scope, target),
                      generation, requested_at)
        return self._append(row, key_id)

    def rotate(self, key_id: str, requested_at: int) -> Watermark:
        _, mark, old_key = self.read()
        if key_id == old_key:
            raise ValueError("New erasure key ID must differ from the active key")
        new_key = self._key(key_id)
        seq = mark.seq + 1
        proof = _digest(new_key, "memory-erasure-rotation",
                        _encode({"installation": self.installation, "seq": seq,
                                 "previous_mac": mark.tail_mac, "key_id": key_id}))
        row = Erasure(self.installation, FORMAT, seq, old_key, mark.tail_mac, "rotate",
                      "", "", 0, requested_at, key_id, proof)
        return self._append(row, key_id)
