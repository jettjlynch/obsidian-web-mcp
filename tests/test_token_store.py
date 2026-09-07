"""Tests for token_store.py -- the fresh, uncached, cross-process token
lookup app-bridge uses (2026-09-07, SECURITY.md H-2's phone-rollout gap).

The one thing worth locking in here that test_token_scope.py's own
oauth-module-level tests can't: this module reads the FILE fresh on every
call rather than trusting oauth.py's in-memory _issued_tokens dict. Proven
by writing a token to disk WITHOUT going through oauth._issue_token at all
(simulating "a different process minted this") and confirming it's still
found -- if this module accidentally started reusing oauth.py's cached
dict, that write would be invisible and this test would fail.
"""

import json
import time

from obsidian_vault_mcp import token_store


def _write_tokens_file(path, data: dict) -> None:
    path.write_text(json.dumps(data))


def test_lookup_reads_a_token_written_by_a_different_process(monkeypatch, tmp_path):
    """Simulates the real scenario this module exists for: obsidian-web-mcp's
    process mints a token; app-bridge's SEPARATE process must see it without
    restarting. No import of oauth.py's _issued_tokens dict involved at all
    -- the file is the only shared thing."""
    tokens_file = tmp_path / "oauth_tokens.json"
    monkeypatch.setattr(token_store, "_TOKENS_FILE", tokens_file)
    _write_tokens_file(tokens_file, {
        "a-bridge-token": {
            "client_id": "jarvis-app",
            "expires_at": time.time() + 3600,
            "scope": "read",
            "audience": "bridge",
        }
    })

    entry = token_store.lookup_issued_token("a-bridge-token")
    assert entry is not None
    assert entry["audience"] == "bridge"
    assert entry["client_id"] == "jarvis-app"


def test_lookup_sees_a_token_added_after_this_module_was_first_imported(monkeypatch, tmp_path):
    """The actual staleness bug this module exists to avoid: a lookup must
    reflect a file write that happens AFTER import time, not a snapshot
    frozen at process start (which is exactly what importing oauth.py's
    _issued_tokens directly would give app-bridge)."""
    tokens_file = tmp_path / "oauth_tokens.json"
    monkeypatch.setattr(token_store, "_TOKENS_FILE", tokens_file)
    _write_tokens_file(tokens_file, {})

    assert token_store.lookup_issued_token("late-arrival") is None

    _write_tokens_file(tokens_file, {
        "late-arrival": {
            "client_id": "jarvis-app",
            "expires_at": time.time() + 3600,
            "scope": "write",
            "audience": "bridge",
        }
    })

    assert token_store.lookup_issued_token("late-arrival") is not None


def test_lookup_rejects_an_expired_token(monkeypatch, tmp_path):
    tokens_file = tmp_path / "oauth_tokens.json"
    monkeypatch.setattr(token_store, "_TOKENS_FILE", tokens_file)
    _write_tokens_file(tokens_file, {
        "stale": {
            "client_id": "jarvis-app",
            "expires_at": time.time() - 1,
            "scope": "read",
            "audience": "bridge",
        }
    })
    assert token_store.lookup_issued_token("stale") is None


def test_lookup_unknown_token_is_none(monkeypatch, tmp_path):
    tokens_file = tmp_path / "oauth_tokens.json"
    monkeypatch.setattr(token_store, "_TOKENS_FILE", tokens_file)
    _write_tokens_file(tokens_file, {})
    assert token_store.lookup_issued_token("never-issued") is None


def test_lookup_fails_closed_on_a_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(token_store, "_TOKENS_FILE", tmp_path / "does-not-exist.json")
    assert token_store.lookup_issued_token("anything") is None


def test_lookup_fails_closed_on_a_corrupt_file(monkeypatch, tmp_path):
    tokens_file = tmp_path / "oauth_tokens.json"
    tokens_file.write_text("{not valid json")
    monkeypatch.setattr(token_store, "_TOKENS_FILE", tokens_file)
    assert token_store.lookup_issued_token("anything") is None
