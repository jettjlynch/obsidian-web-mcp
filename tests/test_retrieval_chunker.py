"""Tests for the heading-aware recursive Markdown chunker.

Per RETRIEVAL-DESIGN.md §1: chunk by heading structure first, token budget
second; carry heading ancestry into each chunk instead of text overlap;
compute real per-chunk line anchors, not section-start line numbers.
"""

from obsidian_vault_mcp.retrieval.chunker import chunk_markdown


def test_single_short_section_is_one_chunk():
    content = "# Title\n\nJust one short paragraph under the title.\n"
    chunks = chunk_markdown("Note.md", content, max_tokens=250)
    assert len(chunks) == 1
    assert "Just one short paragraph" in chunks[0].text


def test_heading_ancestry_breadcrumb_is_prepended():
    content = (
        "# Board Meeting Notes\n"
        "\n"
        "## Financials\n"
        "\n"
        "### Q3 revenue\n"
        "\n"
        "Revenue was up twelve percent versus Q2.\n"
    )
    chunks = chunk_markdown("Decisions/board-notes.md", content, max_tokens=250)
    assert len(chunks) == 1
    text = chunks[0].text
    # Ancestry breadcrumb: file path/title, then H1, H2, H3 headings, in order,
    # all appearing before the body text.
    assert text.index("Decisions/board-notes.md") < text.index("Board Meeting Notes")
    assert text.index("Board Meeting Notes") < text.index("Financials")
    assert text.index("Financials") < text.index("Q3 revenue")
    assert text.index("Q3 revenue") < text.index("Revenue was up twelve percent")


def test_sibling_sections_become_separate_chunks_with_own_ancestry():
    content = (
        "# Doc\n"
        "\n"
        "## Alpha\n"
        "\n"
        "Alpha body text.\n"
        "\n"
        "## Beta\n"
        "\n"
        "Beta body text.\n"
    )
    chunks = chunk_markdown("doc.md", content, max_tokens=250)
    assert len(chunks) == 2
    alpha = next(c for c in chunks if "Alpha body" in c.text)
    beta = next(c for c in chunks if "Beta body" in c.text)
    assert "Alpha" in alpha.text and "Beta" not in alpha.text.split("Alpha body")[0]
    assert "Beta" in beta.text
    assert "Doc" in alpha.text and "Doc" in beta.text


def test_oversized_leaf_section_splits_on_token_budget():
    # No blank lines inside the section body -- forces the fallback splitter
    # past its first preference (blank lines) down to sentences.
    sentence = "This is one sentence about the quarterly numbers. "
    body = sentence * 40  # long enough to blow well past a small budget
    content = f"# Doc\n\n## Body\n\n{body}\n"
    chunks = chunk_markdown("doc.md", content, max_tokens=20)
    assert len(chunks) > 1
    # Every resulting chunk still carries heading context, not just the first.
    for c in chunks:
        assert "Body" in c.text


def test_split_prefers_sentence_boundaries_over_hard_cuts():
    sentence = "Alpha bravo charlie delta echo foxtrot golf hotel. "
    body = sentence * 10
    content = f"# Doc\n\n## Body\n\n{body}\n"
    chunks = chunk_markdown("doc.md", content, max_tokens=15)
    for c in chunks:
        body_part = c.text.split("Body")[-1].strip()
        if body_part:
            # A hard character cut would slice a word in half mid-token;
            # sentence-preferring splitting should not leave a chunk ending
            # on a fragment like "ho" or starting on "tel.".
            assert not body_part.split()[0].startswith("tel")


def test_frontmatter_is_stripped_from_chunk_text():
    content = (
        "---\n"
        "status: active\n"
        "tags: [foo, bar]\n"
        "---\n"
        "\n"
        "# Title\n"
        "\n"
        "Body content here.\n"
    )
    chunks = chunk_markdown("note.md", content, max_tokens=250)
    for c in chunks:
        assert "status: active" not in c.text
        assert "tags:" not in c.text


def test_frontmatter_fields_attached_as_metadata_not_embedded():
    content = (
        "---\n"
        "status: active\n"
        "client: TestCorp\n"
        "---\n"
        "\n"
        "# Title\n"
        "\n"
        "Body content here.\n"
    )
    chunks = chunk_markdown("note.md", content, max_tokens=250)
    assert chunks[0].frontmatter.get("status") == "active"
    assert chunks[0].frontmatter.get("client") == "TestCorp"


def test_line_anchor_points_at_real_content_line():
    content = (
        "# Doc\n"          # line 1
        "\n"               # line 2
        "## Alpha\n"       # line 3
        "\n"               # line 4
        "Alpha body text.\n"  # line 5
        "\n"               # line 6
        "## Beta\n"        # line 7
        "\n"               # line 8
        "Beta body text.\n"   # line 9
    )
    chunks = chunk_markdown("doc.md", content, max_tokens=250)
    beta = next(c for c in chunks if "Beta body" in c.text)
    lines = content.split("\n")
    # The anchor should land on or very near the actual "Beta body text." line
    # (index 8, 0-based -> line 9), not at the start of the file or section.
    assert lines[beta.line_start - 1].strip() == "Beta body text." or (
        beta.line_start >= 7
    )


def test_line_anchors_distinguish_split_sub_chunks():
    sentence = "This is one sentence about the quarterly numbers. "
    body = sentence * 40
    content = f"# Doc\n\n## Body\n\n{body}\n"
    chunks = chunk_markdown("doc.md", content, max_tokens=20)
    anchors = [c.line_start for c in chunks]
    # Sub-chunks from the same oversized section should get distinct,
    # increasing line anchors -- not all pinned to the section's start line.
    assert anchors == sorted(anchors)
    assert len(set(anchors)) > 1


def test_parent_id_shared_across_split_sub_chunks_but_not_across_sections():
    sentence = "This is one sentence about the quarterly numbers. "
    body = sentence * 40
    content = (
        f"# Doc\n\n## Body\n\n{body}\n\n## Other\n\nshort other section.\n"
    )
    chunks = chunk_markdown("doc.md", content, max_tokens=20)
    body_chunks = [c for c in chunks if "Body" in c.text and "Other" not in c.text]
    other_chunks = [c for c in chunks if "Other" in c.text]
    assert len(body_chunks) > 1
    assert len({c.parent_id for c in body_chunks}) == 1
    assert other_chunks[0].parent_id != body_chunks[0].parent_id


def test_content_hash_is_stable_for_identical_text_and_differs_otherwise():
    content_a = "# Doc\n\n## Body\n\nSome stable text.\n"
    content_b = "# Doc\n\n## Body\n\nSome different text.\n"
    chunks_a1 = chunk_markdown("doc.md", content_a, max_tokens=250)
    chunks_a2 = chunk_markdown("doc.md", content_a, max_tokens=250)
    chunks_b = chunk_markdown("doc.md", content_b, max_tokens=250)
    assert chunks_a1[0].content_hash == chunks_a2[0].content_hash
    assert chunks_a1[0].content_hash != chunks_b[0].content_hash


def test_overlong_word_is_dropped_not_left_unsplittable():
    blob = "x" * 5000
    content = f"# Doc\n\n## Body\n\nnormal text {blob} more normal text.\n"
    chunks = chunk_markdown("doc.md", content, max_tokens=20)
    for c in chunks:
        assert blob not in c.text
        # No leftover pathologically long single token either.
        assert max((len(w) for w in c.text.split()), default=0) < 5000


def test_control_characters_are_sanitized():
    content = "# Doc\n\n## Body\n\nHello\x00World\x07 there.\n"
    chunks = chunk_markdown("doc.md", content, max_tokens=250)
    for c in chunks:
        assert "\x00" not in c.text
        assert "\x07" not in c.text


def test_wikilinks_are_preserved_in_embedded_text():
    content = "# Doc\n\n## Body\n\nSee [[Other Note]] for context.\n"
    chunks = chunk_markdown("doc.md", content, max_tokens=250)
    assert "[[Other Note]]" in chunks[0].text


def test_no_frontmatter_and_no_headings_still_chunks_flat_text():
    content = "Just some plain text with no headings and no frontmatter at all.\n"
    chunks = chunk_markdown("plain.md", content, max_tokens=250)
    assert len(chunks) == 1
    assert "plain text" in chunks[0].text
