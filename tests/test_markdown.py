"""Unit tests for the markdown structure + diff helpers."""

from obsidian_vault_mcp.markdown import (
    extract_tags,
    find_heading_section,
    heading_level,
    link_target_key,
    parse_wikilinks,
    split_frontmatter,
    unified_diff,
)


def test_heading_level():
    assert heading_level("# Title") == 1
    assert heading_level("## Status") == 2
    assert heading_level("###### deep") == 6
    assert heading_level("   ## indented") == 2
    assert heading_level("not a heading") == 0
    assert heading_level("#tag") == 0  # no space -> inline tag, not heading
    assert heading_level("####### too deep") == 0


def test_find_heading_section_stops_at_same_level():
    text = "## A\nbody a1\nbody a2\n### sub\nsub body\n## B\nbody b\n"
    lines = text.split("\n")
    res = find_heading_section(lines, "## A")
    assert res is not None
    h, start, end = res
    assert lines[h].strip() == "## A"
    # Section includes the ### sub subsection but stops at "## B"
    assert lines[start:end] == ["body a1", "body a2", "### sub", "sub body"]


def test_find_heading_section_to_eof():
    text = "# Top\n## Last\nx\ny\n"
    lines = text.split("\n")
    res = find_heading_section(lines, "## Last")
    h, start, end = res
    assert lines[start:end] == ["x", "y", ""]  # trailing newline -> final ""


def test_find_heading_section_missing():
    assert find_heading_section(["# A", "body"], "## Nope") is None


def test_split_frontmatter_roundtrip():
    text = "---\ntitle: T\ntags: [a, b]\n---\nbody line\nmore\n"
    fm, body = split_frontmatter(text)
    assert fm == "---\ntitle: T\ntags: [a, b]\n---\n"
    assert body == "body line\nmore\n"
    assert fm + body == text


def test_split_frontmatter_none():
    text = "no frontmatter here\nline 2\n"
    fm, body = split_frontmatter(text)
    assert fm == ""
    assert body == text


def test_split_frontmatter_unterminated():
    text = "---\nlooks like fm but never closes\n"
    fm, body = split_frontmatter(text)
    assert fm == ""
    assert body == text


def test_parse_wikilinks():
    text = "See [[Note A]] and [[Folder/Note B|alias]] and [[Note C#Heading]].\n[[Note A]] again."
    links = parse_wikilinks(text)
    targets = [l["target"] for l in links]
    assert targets == ["Note A", "Folder/Note B", "Note C", "Note A"]
    assert links[1]["alias"] == "alias"
    assert links[2]["subpath"] == "Heading"


def test_link_target_key():
    assert link_target_key("Note A") == "note a"
    assert link_target_key("Folder/Note B") == "note b"
    assert link_target_key("Daily/2026-06-17.md") == "2026-06-17"


def test_extract_tags_frontmatter_and_inline():
    md = {"tags": ["project", "avanti"]}
    body = "Working on #phoenix3 and #ai/agents today. Not a ## heading.\n"
    tags = extract_tags(md, body)
    assert tags == ["ai/agents", "avanti", "phoenix3", "project"]


def test_extract_tags_string_frontmatter():
    assert extract_tags({"tags": "a, b c"}, "") == ["a", "b", "c"]


def test_extract_tags_ignores_headings():
    assert extract_tags(None, "## Heading\n### Another\n") == []


def test_unified_diff_empty_when_same():
    assert unified_diff("x.md", "same\n", "same\n") == ""
    d = unified_diff("x.md", "old\n", "new\n")
    assert "-old" in d and "+new" in d
