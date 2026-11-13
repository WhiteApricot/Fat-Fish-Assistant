"""Resolve a host-local embedding endpoint independently of memory plugins."""

import asyncio
from dataclasses import dataclass, field
import hashlib
from http.client import HTTPConnection, HTTPException
import json
from urllib.parse import urlsplit

from fat_fish.communication.model import key


@dataclass(frozen=True)
class LocalEmbeddingProvider:
    base_url: str
    model: str
    revision: str
    dimensions: int
    api_key: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.base_url, str):
            raise ValueError("Invalid embedding URL")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.rstrip("/") != "/v1" or parsed.port is None):
            raise ValueError("Embedding endpoint must be an explicit loopback /v1 URL")
        if (not isinstance(self.model, str) or not self.model.strip() or
                not isinstance(self.revision, str) or not self.revision.strip() or
                type(self.dimensions) is not int or not 1 <= self.dimensions <= 4096):
            raise ValueError("Invalid embedding model space")
        if not isinstance(self.api_key, str) or any(char in self.api_key for char in "\r\n"):
            raise ValueError("Invalid embedding credential")

    def space(self, label):
        model_space = key(label, self.base_url, self.model, self.revision, self.dimensions)
        return hashlib.sha256(model_space.encode()).hexdigest()

    async def get_embedding(self, text):
        return await asyncio.to_thread(self._request, text[:300])

    def _request(self, text):
        endpoint = urlsplit(self.base_url)
        connection = HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        try:
            connection.request("POST", "/v1/embeddings",
                               json.dumps({"model": self.model, "input": text}).encode(), headers)
            response = connection.getresponse()
            if response.status != 200:
                raise OSError(f"Embedding HTTP status {response.status}")
            payload = response.read(1_048_577)
            if len(payload) > 1_048_576:
                raise ValueError("Embedding response too large")
            result = json.loads(payload)
            vector = result["data"][0]["embedding"]
            if (not isinstance(vector, list) or len(vector) != self.dimensions
                    or result.get("model", self.model) != self.model):
                raise ValueError("Embedding response has the wrong model or dimension")
            return vector
        except (HTTPException, TypeError, KeyError, IndexError) as exc:
            raise OSError("Invalid embedding protocol response") from exc
        finally:
            connection.close()


class EmbeddingService:
    def __init__(self, private):
        self.private = private

    def provider(self, options):
        explicit = options.get("embedding")
        if explicit is not None:
            credential = self.private / "local-embedding-key.txt"
            secret = credential.read_text(encoding="utf-8-sig").strip() if credential.is_file() else ""
            return LocalEmbeddingProvider(explicit["base_url"], explicit["model"], explicit["revision"],
                                          explicit["dimensions"], secret)
        config = self.private.parent / "data" / "cmd_config.json"
        if not config.is_file():
            return None
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        providers = data.get("provider", ()) if isinstance(data, dict) else ()
        if not isinstance(providers, (list, tuple)):
            raise ValueError("Invalid local model provider configuration")
        matches = [row for row in providers if isinstance(row, dict)
                   and row.get("id") == "feiyu-local-embedding" and row.get("enable")]
        if len(matches) != 1:
            return None
        row = matches[0]
        if row.get("embedding_model") != "feiyu-bge-small-zh" or row.get("embedding_dimensions") != 512:
            raise ValueError("Configured local model requires an explicit embedding revision and dimension")
        return LocalEmbeddingProvider(row.get("embedding_api_base"), row["embedding_model"],
                                      "bge-small-zh-v1.5-gguf@5bf683e6", 512,
                                      row.get("embedding_api_key", ""))
