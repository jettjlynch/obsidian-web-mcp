"""Test fixtures for the Obsidian vault MCP server."""

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_oauth_state_files(tmp_path, monkeypatch):
    """Every test gets its own oauth client-registry/token-store files and a
    fresh in-memory copy of both dicts -- never the real ones beside the
    server.

    Found 2026-08-30 while adding S1's per-client token store: /oauth/register
    started persisting to disk (needed for the redirect_uri allowlist fix),
    and the pre-existing test_oauth_register_leak.py tests call it several
    times per run with no isolation of their own -- every `pytest` invocation
    was appending "attacker"/"Obsidian Vault MCP Client" test registrations
    into the real oauth_clients.json a running server would load on restart.
    Autouse so this can't be forgotten by a future test file the same way.
    """
    import obsidian_vault_mcp.oauth as oauth

    monkeypatch.setattr(oauth, "_CLIENTS_FILE", tmp_path / "oauth_clients.json")
    monkeypatch.setattr(oauth, "_TOKENS_FILE", tmp_path / "oauth_tokens.json")
    monkeypatch.setattr(oauth, "_registered_clients", {})
    monkeypatch.setattr(oauth, "_issued_tokens", {})
    monkeypatch.setattr(oauth, "_auth_codes", {})
    # 2026-10-03: PIN lockout state -- never the real file, fresh counter.
    monkeypatch.setattr(oauth, "_LOCKOUT_FILE", tmp_path / "oauth_pin_lockout.json")
    monkeypatch.setattr(oauth, "_pin_lockout", {"failures": 0, "locked_until": 0.0})
    # 2026-10-04: /oauth/register brake is module-global; fresh per test.
    monkeypatch.setattr(
        oauth, "_registrations",
        oauth._SlidingLimit(oauth.REGISTRATION_LIMIT, oauth.REGISTRATION_WINDOW_SECONDS),
    )
    yield


@pytest.fixture(autouse=True)
def _default_full_token_scope():
    """Default every test to a fully-authorized (write) token-scope context.

    Added 2026-09-05 alongside the C-2 read/write scope split: most existing
    tests exercise tool business logic or rate-limiting, not auth, and
    predate the concept of a per-request granted scope entirely. Without
    this, audit.py's new scope gate would fail-closed on every one of them
    (token_scope.current_scope defaults to None -- "no scope determined" --
    outside of a real HTTP request through auth.py's middleware), which
    isn't what those tests are checking and would be a false regression, not
    a real one. Tests that actually exercise scope enforcement (see
    test_token_scope.py) override this explicitly within their own body.
    """
    from obsidian_vault_mcp import token_scope

    reset_token = token_scope.current_scope.set("write")
    yield
    token_scope.current_scope.reset(reset_token)


@pytest.fixture
def vault_dir(tmp_path, monkeypatch):
    """Create a temporary vault directory with sample files."""
    vault = tmp_path / "test-vault"
    vault.mkdir()

    # test-note.md with frontmatter
    (vault / "test-note.md").write_text(
        "---\nstatus: active\ntype: note\n---\n\nThis is a test note with some content.\n"
    )

    # subfolder/nested-note.md with frontmatter
    subfolder = vault / "subfolder"
    subfolder.mkdir()
    (subfolder / "nested-note.md").write_text(
        "---\nstatus: draft\ntype: client-hub\nclient: TestCorp\n---\n\nNested note content.\n"
    )

    # no-frontmatter.md
    (vault / "no-frontmatter.md").write_text("Just plain text, no frontmatter here.\n")

    # .obsidian/config.json (should be excluded)
    obsidian_dir = vault / ".obsidian"
    obsidian_dir.mkdir()
    (obsidian_dir / "config.json").write_text('{"theme": "dark"}')

    # Set environment variable for config module
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("VAULT_MCP_TOKEN", "test-token-12345")

    # Reload config to pick up new env var
    import obsidian_vault_mcp.config as config
    config.VAULT_PATH = Path(str(vault))

    yield vault
