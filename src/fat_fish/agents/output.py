"""Last-mile checks used by the trusted Agent dispatcher."""
import re
from dataclasses import dataclass
import hashlib

from fat_fish.memory.experience import contains_secret, sensitive_information
from fat_fish.memory.service import encode, normalize


PERSONAL_ID = re.compile(r"(?:qq|群号|微信|手机号|身份证)\s*[:：=]?\s*[0-9]{5,}", re.I)


@dataclass(frozen=True)
class OutputAuthorization:
    origin: str
    account: str
    digest: str
    reference: str

    @classmethod
    def attest(cls, origin, account, arguments, reference):
        return cls(origin, account, hashlib.sha256(encode(arguments).encode()).hexdigest(), reference)

    def matches(self, account, arguments):
        return (self.origin in {"public", "confirmed_user"}
                and bool(self.reference)
                and self == self.attest(self.origin, account, arguments, self.reference))


def _text_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _text_values(item)
    elif isinstance(value, (str, int)):
        yield normalize(str(value))


def check_external_output(memory, access, arguments, *, protected_ids=()):
    access.require(write=True)
    values = tuple(_text_values(arguments))
    identities = {normalize(access.source.account), *(normalize(value) for value in protected_ids)}
    identifiers = [re.escape(value) for value in identities
                   if value.isascii() and value.isdecimal() and len(value) >= 5]
    protected = re.compile(r"(?<![0-9])(?:" + "|".join(sorted(identifiers)) + r")(?![0-9])") if identifiers else None
    if contains_secret(arguments) or any(
        sensitive_information(value) or PERSONAL_ID.search(value) or (protected and protected.search(value))
        for value in values
    ):
        raise PermissionError("External output contains sensitive data or protected identity")
    # The host independently authorizes provenance and knows the exact target
    # account. This check catches verbatim reuse of locally scoped data as well.
    with memory.transaction() as db:
        rows = db.execute("""SELECT summary AS text FROM activities
            WHERE scope=? AND redacted=0 AND producer='core'
              AND kind IN ('message_received','message_sent')
              AND summary NOT IN ('收到消息','通信适配器已返回回复提交')
            UNION ALL
            SELECT v.content FROM fact_versions v
            WHERE v.scope=? AND v.content IS NOT NULL""", (access.scope.storage_key,) * 2)
        for row in rows:
            value = normalize(row["text"])
            if value and any(value in text for text in values):
                raise PermissionError("External output repeats scoped memory")
    access.require(write=True)
