"""Regression: unquoted YAML dates in frontmatter must not crash JSON output.

PyYAML (via python-frontmatter) parses `created: 2026-10-04` as datetime.date and
`updated: 2026-10-04 10:30:00` as datetime.datetime. json.dumps has no encoder for
either, so vault_search (frontmatter_excerpt), vault_read and vault_batch_read used
to return {"error": "Object of type date is not JSON serializable"} for any hit on
such a note. Live: 40+ failures in vault-mcp-error.log, bug fixed 2026-10-04.
"""

import json

from obsidian_vault_mcp.tools.read import vault_batch_read, vault_read
from obsidian_vault_mcp.tools.search import _get_frontmatter_excerpt, vault_search

DATED = (
    "---\ncreated: 2026-10-04\nupdated: 2026-10-04 10:30:00\ntype: note\n"
    "tags: [a, b]\n---\n\nDatebugmarker body line.\n"
)


def _write(vault_dir):
    (vault_dir / "dated-note.md").write_text(DATED)


def test_vault_search_hit_on_dated_note_returns_results(vault_dir):
    _write(vault_dir)
    out = json.loads(vault_search("Datebugmarker"))
    assert "error" not in out, out
    assert out["total_matches"] == 1
    fm = out["results"][0]["frontmatter_excerpt"]
    assert fm["created"] == "2026-10-04"
    assert fm["updated"] == "2026-10-04T10:30:00"
    assert fm["type"] == "note"


def test_excerpt_keeps_native_json_types(vault_dir):
    (vault_dir / "plain.md").write_text("---\nn: 3\nok: true\nlst: [1, x]\n---\nbody\n")
    fm = _get_frontmatter_excerpt(vault_dir / "plain.md")
    assert fm == {"n": 3, "ok": True, "lst": [1, "x"]}


def test_nested_dates_are_converted(vault_dir):
    (vault_dir / "nested.md").write_text(
        "---\nevents:\n  - when: 2026-01-02\n    what: x\nmeta: {d: 2025-12-31}\n---\nbody\n"
    )
    out = json.loads(vault_read("nested.md"))
    assert "error" not in out, out
    assert out["frontmatter"]["events"][0]["when"] == "2026-01-02"
    assert out["frontmatter"]["meta"]["d"] == "2025-12-31"


def test_vault_read_dated_note(vault_dir):
    _write(vault_dir)
    out = json.loads(vault_read("dated-note.md"))
    assert "error" not in out, out
    assert out["frontmatter"]["created"] == "2026-10-04"
    assert out["frontmatter"]["tags"] == ["a", "b"]


def test_vault_batch_read_dated_note(vault_dir):
    _write(vault_dir)
    out = json.loads(vault_batch_read(["dated-note.md", "test-note.md"]))
    assert "error" not in out, out
    assert out["found"] == 2 and out["missing"] == 0
    by_path = {r["path"]: r for r in out["files"]}
    assert by_path["dated-note.md"]["frontmatter"]["updated"] == "2026-10-04T10:30:00"


def test_vault_search_frontmatter_dated_note(monkeypatch):
    """The in-memory index keeps raw YAML values (so `exact` matching via str() is
    unchanged); only the JSON output is normalised."""
    import datetime as dt

    from obsidian_vault_mcp import server
    from obsidian_vault_mcp.tools.search import vault_search_frontmatter

    class _Idx:
        def search_by_field(self, **kw):
            return [{"path": "dated-note.md",
                     "frontmatter": {"created": dt.date(2026, 10, 4),
                                     "updated": dt.datetime(2026, 10, 4, 10, 30),
                                     "type": "note"}}]

    monkeypatch.setattr(server, "frontmatter_index", _Idx(), raising=False)
    out = json.loads(vault_search_frontmatter(field="type", value="note"))
    assert "error" not in out, out
    fm = out["results"][0]["frontmatter"]
    assert fm == {"created": "2026-10-04", "updated": "2026-10-04T10:30:00", "type": "note"}
