"""Tests for the embedding backends.

Per RETRIEVAL-DESIGN.md §2.1 and PORT-DESIGN.md (amended 2026-09-06 after
hitting a real 3 RPM rate limit on Jett's un-carded Voyage account, and his
explicit preference for local/open-source over a new paid vendor): the
default backend is now a LOCAL model (Ollama + nomic-embed-text) -- zero
cost, zero rate limit, zero vendor relationship, in keeping with every other
piece of this stack running on the same Mac Mini. VoyageEmbedder stays
available (already built, tested, and proven against the real API) as a
non-default option. Both sit behind the same small `Embedder` interface so
the rest of the system never depends on a real network call in tests.
"""

import json

import httpx
import pytest

from obsidian_vault_mcp.retrieval.embeddings import (
    DeterministicFakeEmbedder,
    OllamaEmbedder,
    OllamaEmbeddingError,
    VoyageEmbedder,
    VoyageEmbeddingError,
)


def test_fake_embedder_is_deterministic_for_same_text():
    e = DeterministicFakeEmbedder(dimension=8)
    v1 = e.embed(["hello world"])[0]
    v2 = e.embed(["hello world"])[0]
    assert v1 == v2
    assert len(v1) == 8


def test_fake_embedder_differs_for_different_text():
    e = DeterministicFakeEmbedder(dimension=8)
    v1 = e.embed(["alpha"])[0]
    v2 = e.embed(["beta"])[0]
    assert v1 != v2


def test_fake_embedder_preserves_input_order():
    e = DeterministicFakeEmbedder(dimension=8)
    vecs = e.embed(["one", "two", "three"])
    assert vecs[0] == e.embed(["one"])[0]
    assert vecs[2] == e.embed(["three"])[0]


def _mock_transport(expected_model, expected_dim):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        body = httpx.Request.read(request)
        import json
        payload = json.loads(body)
        assert payload["model"] == expected_model
        n = len(payload["input"])
        return httpx.Response(
            200,
            json={"data": [{"embedding": [float(i)] * expected_dim, "index": i} for i in range(n)]},
        )

    return httpx.MockTransport(handler)


def test_voyage_embedder_sends_bearer_auth_and_model_and_parses_response():
    client = httpx.Client(transport=_mock_transport("voyage-3-lite", 4))
    embedder = VoyageEmbedder(api_key="test-key", model="voyage-3-lite", dimension=4, client=client)

    vecs = embedder.embed(["first text", "second text"])

    assert len(vecs) == 2
    assert vecs[0] == [0.0, 0.0, 0.0, 0.0]
    assert vecs[1] == [1.0, 1.0, 1.0, 1.0]


def test_voyage_embedder_batches_large_requests():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        payload = json.loads(httpx.Request.read(request))
        calls.append(len(payload["input"]))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.0, 0.0], "index": i}
                    for i in range(len(payload["input"]))
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = VoyageEmbedder(api_key="k", model="m", dimension=2, client=client, batch_size=3)

    texts = [f"text {i}" for i in range(7)]
    vecs = embedder.embed(texts)

    assert len(vecs) == 7
    assert calls == [3, 3, 1]


def test_voyage_embedder_raises_clear_error_on_api_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = VoyageEmbedder(api_key="bad-key", model="m", dimension=2, client=client)

    with pytest.raises(VoyageEmbeddingError):
        embedder.embed(["hello"])


def test_voyage_embedder_does_not_retry_on_a_non_transient_error():
    """A 401 (bad key) retrying would just burn time -- only 429/5xx are worth it."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"error": "invalid api key"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps = []
    embedder = VoyageEmbedder(api_key="bad-key", model="m", dimension=2, client=client, sleep_fn=sleeps.append)

    with pytest.raises(VoyageEmbeddingError):
        embedder.embed(["hello"])
    assert len(attempts) == 1
    assert sleeps == []


def test_voyage_embedder_retries_on_429_then_succeeds():
    """Found live against Jett's real Voyage account: no payment method on
    file means a 3 RPM rate limit, and a real full-vault reindex hits 429
    almost immediately. A transient rate limit must not crash the whole run."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, json={"detail": "rate limited"})
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 2.0], "index": 0}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps = []
    embedder = VoyageEmbedder(api_key="k", model="m", dimension=2, client=client, sleep_fn=sleeps.append)

    vecs = embedder.embed(["hello"])

    assert vecs == [[1.0, 2.0]]
    assert len(attempts) == 3
    assert len(sleeps) == 2  # slept before each retry, not before the first attempt
    assert sleeps == sorted(sleeps)  # backoff grows, never shrinks


def test_voyage_embedder_gives_up_after_max_retries_on_persistent_429():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429, json={"detail": "rate limited"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps = []
    embedder = VoyageEmbedder(
        api_key="k", model="m", dimension=2, client=client, sleep_fn=sleeps.append, max_retries=3
    )

    with pytest.raises(VoyageEmbeddingError):
        embedder.embed(["hello"])
    assert len(attempts) == 4  # first attempt + 3 retries


def test_voyage_embedder_retries_on_server_error():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 2:
            return httpx.Response(503, text="upstream unavailable")
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 2.0], "index": 0}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = VoyageEmbedder(api_key="k", model="m", dimension=2, client=client, sleep_fn=lambda s: None)

    vecs = embedder.embed(["hello"])
    assert vecs == [[1.0, 2.0]]
    assert len(attempts) == 2


# --- OllamaEmbedder: local, no API key, no rate limit --------------------

def test_ollama_embedder_defaults_to_local_host():
    e = OllamaEmbedder(model="nomic-embed-text", dimension=768)
    assert e.host == "http://localhost:11434"


def test_ollama_embedder_posts_to_embed_endpoint_and_parses_response():
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        payload = json.loads(httpx.Request.read(request))
        assert payload["model"] == "nomic-embed-text"
        assert payload["input"] == ["first text", "second text"]
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = OllamaEmbedder(model="nomic-embed-text", dimension=2, client=client)

    vecs = embedder.embed(["first text", "second text"])

    assert vecs == [[0.1, 0.2], [0.3, 0.4]]
    assert str(seen_requests[0].url) == "http://localhost:11434/api/embed"
    # No API key, no Authorization header at all -- it's local.
    assert "authorization" not in {h.lower() for h in seen_requests[0].headers}


def test_ollama_embedder_uses_custom_host_when_given():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.0]]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = OllamaEmbedder(model="m", dimension=1, host="http://other-box:11434", client=client)

    embedder.embed(["x"])
    # MockTransport doesn't care about host, but the embedder's own attribute
    # is what actually gets used to build the request -- assert it directly.
    assert embedder.host == "http://other-box:11434"


def test_ollama_embedder_batches_large_requests():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(httpx.Request.read(request))
        calls.append(len(payload["input"]))
        return httpx.Response(200, json={"embeddings": [[0.0] for _ in payload["input"]]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = OllamaEmbedder(model="m", dimension=1, client=client, batch_size=3)

    vecs = embedder.embed([f"text {i}" for i in range(7)])

    assert len(vecs) == 7
    assert calls == [3, 3, 1]


def test_ollama_embedder_raises_clear_error_when_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = OllamaEmbedder(model="m", dimension=1, client=client)

    with pytest.raises(OllamaEmbeddingError, match="(?i)ollama"):
        embedder.embed(["hello"])


def test_ollama_embedder_raises_clear_error_when_model_not_pulled():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": 'model "nomic-embed-text" not found'})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = OllamaEmbedder(model="nomic-embed-text", dimension=768, client=client)

    with pytest.raises(OllamaEmbeddingError, match="not found"):
        embedder.embed(["hello"])
