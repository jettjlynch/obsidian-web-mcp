"""dry_run coverage for existing write tools (global safety requirement)."""

import json

from obsidian_vault_mcp.tools.write import (
    vault_append,
    vault_batch_frontmatter_update,
    vault_write,
)


def test_vault_write_dry_run_no_write(vault_dir):
    r = json.loads(vault_write("new.md", "hello\n", dry_run=True))
    assert r["dry_run"] is True
    assert r["would_create"] is True
    assert "+hello" in r["diff"]
    assert not (vault_dir / "new.md").exists()  # nothing written


def test_vault_write_dry_run_overwrite_diff(vault_dir):
    (vault_dir / "exist.md").write_text("old content\n")
    r = json.loads(vault_write("exist.md", "new content\n", dry_run=True))
    assert r["would_change"] is True
    assert "-old content" in r["diff"] and "+new content" in r["diff"]
    assert (vault_dir / "exist.md").read_text() == "old content\n"  # untouched


def test_vault_append_dry_run(vault_dir):
    (vault_dir / "log.md").write_text("line 1\n")
    r = json.loads(vault_append("log.md", "line 2", dry_run=True))
    assert r["dry_run"] is True
    assert "+line 2" in r["diff"]
    assert (vault_dir / "log.md").read_text() == "line 1\n"


def test_batch_frontmatter_dry_run(vault_dir):
    r = json.loads(vault_batch_frontmatter_update(
        [{"path": "test-note.md", "fields": {"priority": "high"}}],
        dry_run=True,
    ))
    assert r["dry_run"] is True
    entry = r["results"][0]
    assert entry["would_change"] is True
    assert "priority" in entry["diff"]
    # original frontmatter untouched
    assert "priority" not in (vault_dir / "test-note.md").read_text()
