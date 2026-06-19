"""Tests for vault_read_section."""

import json

from obsidian_vault_mcp.tools.read import vault_read_section


def test_read_section_returns_body_only(vault_dir):
    (vault_dir / "doc.md").write_text(
        "# Title\n## Status\nactive\nblocked: no\n## Next\nshipping\n"
    )
    r = json.loads(vault_read_section("doc.md", "## Status"))
    assert "error" not in r
    assert r["heading"] == "## Status"
    assert r["content"] == "active\nblocked: no"


def test_read_section_includes_subsections(vault_dir):
    (vault_dir / "doc.md").write_text("## A\nx\n### sub\ny\n## B\nz\n")
    r = json.loads(vault_read_section("doc.md", "## A"))
    assert r["content"] == "x\n### sub\ny"


def test_read_section_missing_heading(vault_dir):
    (vault_dir / "doc.md").write_text("## A\nx\n")
    r = json.loads(vault_read_section("doc.md", "## Z"))
    assert "error" in r


def test_read_section_missing_file(vault_dir):
    r = json.loads(vault_read_section("nope.md", "## A"))
    assert "error" in r
