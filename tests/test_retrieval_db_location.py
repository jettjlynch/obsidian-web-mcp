"""2026-10-04: retrieval index default moved out of the Prouds-named directory.

Default is now ~/.config/vault-mcp/retrieval.sqlite; RETRIEVAL_DB_PATH still
wins; if only the legacy ~/.config/prouds-mcp/retrieval.sqlite exists, startup
refuses instead of silently creating an empty index.
"""

import os
import subprocess
import sys
from pathlib import Path

from obsidian_vault_mcp import config


def test_default_is_vault_mcp_dir_not_prouds():
    assert config.RETRIEVAL_DB_PATH_DEFAULT == Path(os.path.expanduser("~/.config/vault-mcp/retrieval.sqlite"))
    assert "prouds" not in str(config.RETRIEVAL_DB_PATH_DEFAULT)


def _config_in_subprocess(home: Path, extra_env: dict) -> str:
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    env.update(extra_env)
    code = "from obsidian_vault_mcp import config; print(config.RETRIEVAL_DB_PATH, config.RETRIEVAL_DB_PATH_FROM_ENV)"
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout.strip()


def test_default_resolves_under_home(tmp_path):
    out = _config_in_subprocess(tmp_path, {})
    assert out == f"{tmp_path}/.config/vault-mcp/retrieval.sqlite False"


def test_env_override_wins(tmp_path):
    out = _config_in_subprocess(tmp_path, {"RETRIEVAL_DB_PATH": str(tmp_path / "x.sqlite")})
    assert out == f"{tmp_path}/x.sqlite True"


def test_error_when_only_legacy_exists(tmp_path):
    new, legacy = tmp_path / "vault-mcp" / "r.sqlite", tmp_path / "prouds-mcp" / "r.sqlite"
    legacy.parent.mkdir()
    legacy.write_bytes(b"x")
    msg = config.retrieval_db_location_error(new, legacy, from_env=False)
    assert msg and str(legacy) in msg and "mv " in msg and "Refusing" in msg


def test_no_error_when_new_exists_or_fresh_install_or_env(tmp_path):
    new, legacy = tmp_path / "n.sqlite", tmp_path / "l.sqlite"
    assert config.retrieval_db_location_error(new, legacy, from_env=False) is None  # fresh install
    legacy.write_bytes(b"x")
    assert config.retrieval_db_location_error(new, legacy, from_env=True) is None   # explicit env
    new.write_bytes(b"x")
    assert config.retrieval_db_location_error(new, legacy, from_env=False) is None  # migrated


def test_server_startup_refuses_with_only_legacy_index(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "prouds-mcp").mkdir(parents=True)
    (home / ".config" / "prouds-mcp" / "retrieval.sqlite").write_bytes(b"legacy")
    vault = tmp_path / "vault"
    vault.mkdir()
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "VAULT_PATH": str(vault),
           "VAULT_MCP_PORT": "8467", "VAULT_MCP_TOKEN": "t", "RETRIEVAL_ENABLED": "true",
           "VAULT_OAUTH_STATE_DIR": str(tmp_path / "state"), "DATALAYER_DIR": str(tmp_path / "nodl")}
    (tmp_path / "state").mkdir()
    p = subprocess.run([sys.executable, "-c", "from obsidian_vault_mcp.server import main; main()"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0
    assert "Refusing to start with an empty index" in (p.stderr + p.stdout)
    assert not (home / ".config" / "vault-mcp" / "retrieval.sqlite").exists()
