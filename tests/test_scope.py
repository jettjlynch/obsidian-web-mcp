"""Tests for VAULT_SCOPE_ROOT folder-scoping.

Proves a scoped instance treats its sub-folder as the vault root and refuses any
path resolving outside it (direct, ../, absolute), while an unscoped instance can
reach everything in the same vault.
"""

import json
from pathlib import Path

import pytest

import obsidian_vault_mcp.config as config
from obsidian_vault_mcp.vault import resolve_vault_path
from obsidian_vault_mcp.tools.read import vault_read
from obsidian_vault_mcp.tools.manage import vault_list


@pytest.fixture
def multi_folder_vault(tmp_path, monkeypatch):
    """A vault with two folders, each holding a file. Resets scope to unset."""
    vault = tmp_path / "vault"
    (vault / "Governance").mkdir(parents=True)
    (vault / "CostCommittee").mkdir(parents=True)

    (vault / "Governance" / "policy.md").write_text(
        "---\ntype: governance\n---\n\nGovernance policy body.\n"
    )
    (vault / "CostCommittee" / "secret.md").write_text(
        "---\ntype: confidential\n---\n\nSECRET-COSTCOMMITTEE-7F3A\n"
    )

    monkeypatch.setattr(config, "VAULT_PATH", Path(str(vault)))
    monkeypatch.setattr(config, "VAULT_SCOPE_ROOT", "")  # default: full access
    return vault


def _scope_to(monkeypatch, folder: str):
    monkeypatch.setattr(config, "VAULT_SCOPE_ROOT", folder)


# --- unscoped (full access) ---

def test_unscoped_reads_everything(multi_folder_vault, monkeypatch):
    """With no scope, both folders are reachable."""
    gov = json.loads(vault_read("Governance/policy.md"))
    cost = json.loads(vault_read("CostCommittee/secret.md"))
    assert "error" not in gov and "Governance policy body" in gov["content"]
    assert "error" not in cost and "SECRET-COSTCOMMITTEE-7F3A" in cost["content"]


# --- scoped: can reach its own folder ---

def test_scoped_reads_own_folder(multi_folder_vault, monkeypatch):
    """Scoped to Governance, the folder is the root: 'policy.md' resolves inside it."""
    _scope_to(monkeypatch, "Governance")
    res = json.loads(vault_read("policy.md"))
    assert "error" not in res
    assert "Governance policy body" in res["content"]


def test_scoped_effective_root_is_subfolder(multi_folder_vault, monkeypatch):
    _scope_to(monkeypatch, "Governance")
    assert config.effective_vault_path() == (multi_folder_vault / "Governance").resolve()


# --- scoped: cannot reach outside (the proof) ---

def test_scoped_blocks_direct_outside_path(multi_folder_vault, monkeypatch):
    """A sibling folder named directly resolves inside scope and simply isn't there."""
    _scope_to(monkeypatch, "Governance")
    res = json.loads(vault_read("CostCommittee/secret.md"))
    assert "error" in res  # resolves to Governance/CostCommittee/... which doesn't exist


def test_scoped_blocks_parent_traversal(multi_folder_vault, monkeypatch):
    """../ escape is refused."""
    _scope_to(monkeypatch, "Governance")
    res = json.loads(vault_read("../CostCommittee/secret.md"))
    assert "error" in res
    with pytest.raises(ValueError):
        resolve_vault_path("../CostCommittee/secret.md")


def test_scoped_blocks_absolute_path(multi_folder_vault, monkeypatch):
    """An absolute path pointing outside scope is refused, not clamped."""
    _scope_to(monkeypatch, "Governance")
    abs_outside = str(multi_folder_vault / "CostCommittee" / "secret.md")
    res = json.loads(vault_read(abs_outside))
    assert "error" in res
    with pytest.raises(ValueError):
        resolve_vault_path(abs_outside)


def test_scoped_list_only_sees_scope(multi_folder_vault, monkeypatch):
    """Listing the scoped root shows its files, never the sibling folder."""
    _scope_to(monkeypatch, "Governance")
    listing = json.loads(vault_list(""))
    names = json.dumps(listing)
    assert "policy.md" in names
    assert "CostCommittee" not in names
    assert "secret.md" not in names


def test_scope_config_rejects_escape(multi_folder_vault, monkeypatch):
    """A scope value that escapes the vault is rejected outright."""
    _scope_to(monkeypatch, "../evil")
    with pytest.raises(ValueError):
        config.effective_vault_path()
