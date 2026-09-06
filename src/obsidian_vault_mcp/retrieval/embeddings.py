"""Embedding backends.

Per RETRIEVAL-DESIGN.md §2.1: a single embedding model fixed for the whole
deployment. Per PORT-DESIGN.md: a hosted API (Voyage AI, Anthropic's own
recommended embeddings partner -- there is no first-party Anthropic
embeddings endpoint), called directly over HTTPS, no local model process.

Everything upstream (the indexer, the query path) depends only on the
`embed(texts) -> vectors` shape, never on Voyage specifically -- tests use
`DeterministicFakeEmbedder` and never touch the network.
"""

import hashlib
import json
import time
from typing import Callable, Protocol

import httpx

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
DEFAULT_BATCH_SIZE = 128

OLLAMA_DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MAX_RETRIES = 5
DEFAULT_BASE_DELAY = 1.0
# Status codes worth retrying: 429 (rate limit -- found live against Jett's
# real Voyage account, whose lack of a payment method on file caps it at
# 3 RPM) and 5xx (transient upstream trouble). Never retry 4xx auth/request
# errors (401/400/etc) -- those won't fix themselves by waiting.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class Embedder(Protocol):
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text, in the same order."""
        ...


class VoyageEmbeddingError(RuntimeError):
    """Raised when the Voyage API call fails or returns something we can't parse."""


class DeterministicFakeEmbedder:
    """Test double: no network, deterministic per-text vectors.

    Not a real embedding model -- vectors carry no semantic meaning, only
    determinism (same text -> same vector) and stable dimensionality, which
    is all the store/indexer/query layers need from an `Embedder` in tests.
    """

    def __init__(self, dimension: int = 32):
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            # Cycle the digest's bytes out to `dimension` floats in [0, 1).
            vec = [digest[i % len(digest)] / 255.0 for i in range(self.dimension)]
            vectors.append(vec)
        return vectors


class VoyageEmbedder:
    """Production embedder: calls the Voyage AI embeddings endpoint over HTTPS."""

    def __init__(
        self,
        api_key: str,
        model: str,
        dimension: int,
        client: httpx.Client | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.model = model
        self.dimension = dimension
        self.batch_size = batch_size
        self.max_retries = max_retries
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=30.0)
        self._sleep = sleep_fn

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            results.extend(self._embed_batch(batch))
        return results

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        attempt = 0
        while True:
            try:
                response = self._client.post(
                    VOYAGE_API_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    content=json.dumps({"input": texts, "model": self.model}),
                )
            except httpx.HTTPError as e:
                raise VoyageEmbeddingError(f"Voyage API request failed: {e}") from e

            if response.status_code == 200:
                break

            if response.status_code not in _RETRYABLE_STATUS or attempt >= self.max_retries:
                raise VoyageEmbeddingError(
                    f"Voyage API returned {response.status_code}: {response.text}"
                )

            # Honor a Retry-After header (Voyage sends one on 429) when present,
            # otherwise fall back to exponential backoff.
            retry_after = response.headers.get("retry-after")
            if retry_after is not None:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = DEFAULT_BASE_DELAY * (2 ** attempt)
            else:
                delay = DEFAULT_BASE_DELAY * (2 ** attempt)
            self._sleep(delay)
            attempt += 1

        try:
            payload = response.json()
            ordered = sorted(payload["data"], key=lambda d: d["index"])
            return [d["embedding"] for d in ordered]
        except (KeyError, TypeError, ValueError) as e:
            raise VoyageEmbeddingError(f"Could not parse Voyage API response: {e}") from e


class OllamaEmbeddingError(RuntimeError):
    """Raised when the local Ollama call fails or returns something we can't parse."""


class OllamaEmbedder:
    """Default production embedder (2026-09-06): a local Ollama model.

    Chosen over a hosted API after hitting a real 3 RPM rate limit on an
    un-carded Voyage account and Jett's explicit preference for local/
    open-source (see PORT-DESIGN.md's amendment). No API key, no rate
    limit, no per-token cost, no new vendor relationship -- runs on the
    same Mac Mini as everything else in this stack.
    """

    def __init__(
        self,
        model: str,
        dimension: int,
        host: str = OLLAMA_DEFAULT_HOST,
        client: httpx.Client | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ):
        self.model = model
        self.dimension = dimension
        self.host = host
        self.batch_size = batch_size
        self._client = client or httpx.Client(timeout=120.0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            results.extend(self._embed_batch(batch))
        return results

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            response = self._client.post(
                f"{self.host}/api/embed",
                content=json.dumps({"model": self.model, "input": texts}),
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as e:
            raise OllamaEmbeddingError(
                f"Could not reach Ollama at {self.host}: {e}. "
                f"Is it running? (`brew services start ollama`, or `ollama serve`)"
            ) from e

        if response.status_code != 200:
            raise OllamaEmbeddingError(f"Ollama returned {response.status_code}: {response.text}")

        try:
            payload = response.json()
            return payload["embeddings"]
        except (KeyError, TypeError, ValueError) as e:
            raise OllamaEmbeddingError(f"Could not parse Ollama response: {e}") from e
