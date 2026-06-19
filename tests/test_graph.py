"""Tests for the graph + daily tools (Tier 3)."""

import json

from obsidian_vault_mcp.tools.graph import (
    _london_today,
    vault_backlinks,
    vault_daily,
    vault_links,
    vault_tags,
)


# ---------- vault_links ----------

def test_vault_links(vault_dir):
    (vault_dir / "src.md").write_text(
        "Links to [[Note A]] and [[Folder/Note B|alias]] and [[Note A]].\n"
    )
    r = json.loads(vault_links("src.md"))
    assert r["count"] == 3
    assert r["unique_targets"] == ["Note A", "Folder/Note B"]


def test_vault_links_none(vault_dir):
    (vault_dir / "src.md").write_text("no links here\n")
    r = json.loads(vault_links("src.md"))
    assert r["count"] == 0


# ---------- vault_backlinks ----------

def test_vault_backlinks(vault_dir):
    (vault_dir / "target.md").write_text("I am the target.\n")
    (vault_dir / "one.md").write_text("see [[target]] here\n")
    (vault_dir / "two.md").write_text("also [[target|aliased]] and [[target]]\n")
    (vault_dir / "three.md").write_text("no link to it\n")

    r = json.loads(vault_backlinks("target"))
    paths = [b["path"] for b in r["backlinks"]]
    assert "one.md" in paths
    assert "two.md" in paths
    assert "three.md" not in paths
    two = next(b for b in r["backlinks"] if b["path"] == "two.md")
    assert two["count"] == 2


def test_vault_backlinks_with_md_suffix_and_path(vault_dir):
    (vault_dir / "one.md").write_text("link [[Daily/2026-06-17]]\n")
    r = json.loads(vault_backlinks("2026-06-17.md"))
    assert any(b["path"] == "one.md" for b in r["backlinks"])


# ---------- vault_tags ----------

def test_vault_tags_all_counts(vault_dir):
    (vault_dir / "t1.md").write_text("---\ntags: [project, avanti]\n---\nbody #phoenix3\n")
    (vault_dir / "t2.md").write_text("body with #project inline\n")
    r = json.loads(vault_tags())
    counts = {entry["tag"]: entry["count"] for entry in r["tags"]}
    assert counts["project"] == 2  # frontmatter in t1 + inline in t2
    assert counts["avanti"] == 1
    assert counts["phoenix3"] == 1


def test_vault_tags_filter(vault_dir):
    (vault_dir / "t1.md").write_text("---\ntags: [project]\n---\nx\n")
    (vault_dir / "t2.md").write_text("y #project\n")
    (vault_dir / "t3.md").write_text("nothing\n")
    r = json.loads(vault_tags("#project"))
    assert set(r["notes"]) == {"t1.md", "t2.md"}
    assert r["total"] == 2


# ---------- vault_daily ----------

def test_vault_daily_creates_when_missing(vault_dir):
    date_str = _london_today()
    r = json.loads(vault_daily())
    assert r["created"] is True
    f = vault_dir / "Daily" / f"{date_str}.md"
    assert f.exists()
    text = f.read_text()
    assert f"title: Daily Log — {date_str}" in text
    assert "type: daily" in text
    assert f"permalink: jj-second-brain/daily/{date_str}" in text


def test_vault_daily_returns_content_when_exists(vault_dir):
    date_str = _london_today()
    daily_dir = vault_dir / "Daily"
    daily_dir.mkdir()
    (daily_dir / f"{date_str}.md").write_text("existing daily content\n")
    r = json.loads(vault_daily())
    assert r["created"] is False
    assert r["content"] == "existing daily content\n"


def test_vault_daily_appends_content(vault_dir):
    date_str = _london_today()
    daily_dir = vault_dir / "Daily"
    daily_dir.mkdir()
    (daily_dir / f"{date_str}.md").write_text("# header\n")
    r = json.loads(vault_daily(content="### 14:30 entry"))
    assert r["appended"] is True
    assert "### 14:30 entry" in (daily_dir / f"{date_str}.md").read_text()


def test_vault_daily_append_under_heading(vault_dir):
    date_str = _london_today()
    daily_dir = vault_dir / "Daily"
    daily_dir.mkdir()
    (daily_dir / f"{date_str}.md").write_text("## Log\nfirst\n## Other\nx\n")
    json.loads(vault_daily(content="second", heading="## Log"))
    text = (daily_dir / f"{date_str}.md").read_text()
    assert text == "## Log\nfirst\n\nsecond\n## Other\nx\n"


def test_vault_daily_dry_run(vault_dir):
    date_str = _london_today()
    r = json.loads(vault_daily(content="entry", dry_run=True))
    assert r["dry_run"] is True
    assert r["would_create"] is True
    assert not (vault_dir / "Daily" / f"{date_str}.md").exists()
