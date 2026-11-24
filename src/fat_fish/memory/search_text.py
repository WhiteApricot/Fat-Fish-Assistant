"""Normalized terms shared by cognitive-memory indexing and querying."""

from collections import Counter
import re
import unicodedata

TOKENIZER_VERSION = 1
TOKENS = re.compile(r"[\u3400-\u9fff]+|[^\W\d_]+|\d+")


def tokens(text: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    result = Counter()
    for match in TOKENS.finditer(normalized):
        token = match.group()
        if "\u3400" <= token[0] <= "\u9fff":
            result.update(token)
            result.update(token[i:i + 2] for i in range(len(token) - 1))
        else:
            result[token] += 1
    return result
