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
    yield


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
