"""Tests for the surgical edit tools (Tier 1 + Tier 2 writes)."""

import json

from obsidian_vault_mcp.tools.edit import (
    vault_append_under_heading,
    vault_find_replace,
    vault_insert_at,
    vault_prepend,
    vault_replace_section,
)


# ---------- vault_find_replace ----------

def test_find_replace_all(vault_dir):
    (vault_dir / "fr.md").write_text("foo bar foo baz foo\n")
    r = json.loads(vault_find_replace("fr.md", "foo", "XXX"))
    assert r["replacements"] == 3
    assert r["occurrences_found"] == 3
    assert (vault_dir / "fr.md").read_text() == "XXX bar XXX baz XXX\n"


def test_find_replace_first(vault_dir):
    (vault_dir / "fr.md").write_text("a a a\n")
    r = json.loads(vault_find_replace("fr.md", "a", "Z", occurrence="first"))
    assert r["replacements"] == 1
    assert (vault_dir / "fr.md").read_text() == "Z a a\n"


def test_find_replace_nth(vault_dir):
    (vault_dir / "fr.md").write_text("a a a\n")
    r = json.loads(vault_find_replace("fr.md", "a", "Z", occurrence=2))
    assert r["replacements"] == 1
    assert (vault_dir / "fr.md").read_text() == "a Z a\n"


def test_find_replace_nth_out_of_range(vault_dir):
    (vault_dir / "fr.md").write_text("a a\n")
    r = json.loads(vault_find_replace("fr.md", "a", "Z", occurrence=5))
    assert "error" in r
    assert (vault_dir / "fr.md").read_text() == "a a\n"  # untouched


def test_find_replace_not_present(vault_dir):
    (vault_dir / "fr.md").write_text("hello\n")
    r = json.loads(vault_find_replace("fr.md", "absent", "x"))
    assert "error" in r
    assert r["occurrences_found"] == 0


def test_find_replace_dry_run_writes_nothing(vault_dir):
    (vault_dir / "fr.md").write_text("foo\n")
    r = json.loads(vault_find_replace("fr.md", "foo", "bar", dry_run=True))
    assert r["dry_run"] is True
    assert r["would_change"] is True
    assert "-foo" in r["diff"] and "+bar" in r["diff"]
    assert (vault_dir / "fr.md").read_text() == "foo\n"  # unchanged


def test_find_replace_missing_file(vault_dir):
    r = json.loads(vault_find_replace("nope.md", "a", "b"))
    assert "error" in r


# ---------- vault_insert_at ----------

def test_insert_after_heading(vault_dir):
    (vault_dir / "i.md").write_text("# Top\n## A\nbody\n## B\n")
    r = json.loads(vault_insert_at("i.md", "INSERTED", after_heading="## A"))
    assert r["changed"] is True
    assert (vault_dir / "i.md").read_text() == "# Top\n## A\nINSERTED\nbody\n## B\n"


def test_insert_before_line(vault_dir):
    (vault_dir / "i.md").write_text("alpha\nbeta\ngamma\n")
    r = json.loads(vault_insert_at("i.md", "NEW", after_line="beta", position="before"))
    assert (vault_dir / "i.md").read_text() == "alpha\nNEW\nbeta\ngamma\n"


def test_insert_at_very_top(vault_dir):
    (vault_dir / "i.md").write_text("first line\nsecond\n")
    r = json.loads(vault_insert_at("i.md", "TOP", after_line="first line", position="before"))
    assert (vault_dir / "i.md").read_text() == "TOP\nfirst line\nsecond\n"


def test_insert_requires_exactly_one_anchor(vault_dir):
    (vault_dir / "i.md").write_text("x\n")
    both = json.loads(vault_insert_at("i.md", "y", after_heading="## A", after_line="x"))
    assert "error" in both
    neither = json.loads(vault_insert_at("i.md", "y"))
    assert "error" in neither


def test_insert_heading_not_found(vault_dir):
    (vault_dir / "i.md").write_text("# Top\n")
    r = json.loads(vault_insert_at("i.md", "y", after_heading="## Missing"))
    assert "error" in r


def test_insert_dry_run(vault_dir):
    (vault_dir / "i.md").write_text("a\nb\n")
    r = json.loads(vault_insert_at("i.md", "X", after_line="a", dry_run=True))
    assert r["dry_run"] is True and r["would_change"] is True
    assert (vault_dir / "i.md").read_text() == "a\nb\n"


# ---------- vault_replace_section ----------

def test_replace_section_swaps_body_keeps_heading(vault_dir):
    (vault_dir / "s.md").write_text("# Doc\n## Status\nold line 1\nold line 2\n## Next\nkeep\n")
    r = json.loads(vault_replace_section("s.md", "## Status", "fresh status"))
    assert r["changed"] is True
    assert (vault_dir / "s.md").read_text() == "# Doc\n## Status\nfresh status\n## Next\nkeep\n"


def test_replace_section_stops_at_same_level_not_subheadings(vault_dir):
    text = "## A\nold\n### sub\nsubbody\n## B\nbkeep\n"
    (vault_dir / "s.md").write_text(text)
    json.loads(vault_replace_section("s.md", "## A", "NEW"))
    assert (vault_dir / "s.md").read_text() == "## A\nNEW\n## B\nbkeep\n"


def test_replace_section_not_found(vault_dir):
    (vault_dir / "s.md").write_text("## A\nx\n")
    r = json.loads(vault_replace_section("s.md", "## Z", "y"))
    assert "error" in r


def test_replace_section_dry_run(vault_dir):
    (vault_dir / "s.md").write_text("## A\nold\n")
    r = json.loads(vault_replace_section("s.md", "## A", "new", dry_run=True))
    assert r["dry_run"] is True
    assert (vault_dir / "s.md").read_text() == "## A\nold\n"


# ---------- vault_append_under_heading ----------

def test_append_under_heading(vault_dir):
    (vault_dir / "a.md").write_text("## Log\nentry 1\n## Other\nx\n")
    json.loads(vault_append_under_heading("a.md", "## Log", "entry 2"))
    assert (vault_dir / "a.md").read_text() == "## Log\nentry 1\n\nentry 2\n## Other\nx\n"


def test_append_under_heading_to_eof_section(vault_dir):
    (vault_dir / "a.md").write_text("# Top\n## Log\nentry 1\n")
    json.loads(vault_append_under_heading("a.md", "## Log", "entry 2"))
    # trailing newline is preserved (the section ran to EOF)
    assert (vault_dir / "a.md").read_text() == "# Top\n## Log\nentry 1\n\nentry 2\n"


def test_append_under_heading_missing(vault_dir):
    (vault_dir / "a.md").write_text("## A\nx\n")
    r = json.loads(vault_append_under_heading("a.md", "## Z", "y"))
    assert "error" in r


# ---------- vault_prepend ----------

def test_prepend_after_frontmatter(vault_dir):
    (vault_dir / "p.md").write_text("---\ntitle: T\n---\nbody\n")
    r = json.loads(vault_prepend("p.md", "TOP NOTE"))
    assert r["after_frontmatter"] is True
    assert (vault_dir / "p.md").read_text() == "---\ntitle: T\n---\nTOP NOTE\nbody\n"


def test_prepend_no_frontmatter(vault_dir):
    (vault_dir / "p.md").write_text("just body\n")
    json.loads(vault_prepend("p.md", "TOP"))
    assert (vault_dir / "p.md").read_text() == "TOP\njust body\n"


def test_prepend_dry_run(vault_dir):
    (vault_dir / "p.md").write_text("body\n")
    r = json.loads(vault_prepend("p.md", "x", dry_run=True))
    assert r["dry_run"] is True
    assert (vault_dir / "p.md").read_text() == "body\n"
