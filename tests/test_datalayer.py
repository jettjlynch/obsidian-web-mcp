"""data-layer P3 hook (datalayer.py): flag off = no behaviour change; flag on = reorder only.

Uses the real ranking library at datalayer.DATALAYER_DIR (Jett's
~/work/Scripts/data-layer); skipped where that build is not present. The flag
is driven by DATALAYER_RERANK_VAULT_MCP so the real flags.json is never read,
and the recall log is redirected to tmp_path so the real one is never written.
"""

import json

import pytest

from obsidian_vault_mcp import config, datalayer
from obsidian_vault_mcp.retrieval import query as rquery
from obsidian_vault_mcp.retrieval.query import SemanticSearchResponse, SemanticSearchResult
import obsidian_vault_mcp.server as server
from obsidian_vault_mcp.tools import search as search_tools
from obsidian_vault_mcp.tools.read import vault_batch_read, vault_read

pytestmark = pytest.mark.skipif(not datalayer.DATALAYER_DIR.is_dir(), reason="data-layer build not present")


@pytest.fixture
def typed_vault(vault_dir, monkeypatch, tmp_path):
    (vault_dir / "A ref.md").write_text("---\nmemory_type: reference\ndate: '2026-10-01'\n---\nalpha\n")
    (vault_dir / "B dec.md").write_text("---\nmemory_type: decision\ndate: '2026-10-01'\n---\nalpha\n")
    (vault_dir / "Core").mkdir()
    (vault_dir / "Core" / "Identity.md").write_text('---\npinned: true\ndescription: "Who"\n---\nx\n')
    monkeypatch.setattr(config, "VAULT_SCOPE_ROOT", "")
    recall = datalayer._lib("recall")
    monkeypatch.setattr(recall, "LOG_PATH", tmp_path / "recalls.jsonl")
    rr = datalayer._lib("rerank")
    monkeypatch.setitem(rr.DEFAULTS, "pinned_roots", ["Core/"])
    monkeypatch.setattr(rr, "RANKING_PATH", tmp_path / "no-ranking.json")  # pure defaults
    monkeypatch.setattr(rr, "RECALLS_PATH", tmp_path / "recalls.jsonl")
    return vault_dir


def _flag(monkeypatch, on):
    monkeypatch.setenv("DATALAYER_RERANK_VAULT_MCP", "1" if on else "0")


def _fake_semantic(results, no_good=False):
    def fake(**kw):
        return SemanticSearchResponse(results=results, query_text=kw["query"], no_good_answer=no_good,
                                      searched_summary="s")
    return fake


def _res(path, distance):
    return SemanticSearchResult(file_path=path, text="t", heading_path=(path,), line_start=1,
                                parent_id=path, distance=distance)


@pytest.fixture
def fake_store(monkeypatch):
    monkeypatch.setattr(server, "retrieval_store", object())
    monkeypatch.setattr(server, "retrieval_embedder", object())


def test_semantic_flag_off_is_raw_order_and_no_pinned_key(typed_vault, fake_store, monkeypatch):
    _flag(monkeypatch, False)
    monkeypatch.setattr(rquery, "semantic_search", _fake_semantic([_res("A ref.md", 0.70), _res("B dec.md", 0.75)]))
    out = json.loads(search_tools.vault_search_semantic(query="alpha"))
    assert [r["path"] for r in out["results"]] == ["A ref.md", "B dec.md"]
    assert "pinned_core" not in out


def test_semantic_flag_on_reorders_but_keeps_distance_and_confidence(typed_vault, fake_store, monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(rquery, "semantic_search", _fake_semantic([_res("A ref.md", 0.70), _res("B dec.md", 0.75)]))
    out = json.loads(search_tools.vault_search_semantic(query="alpha"))
    assert [r["path"] for r in out["results"]] == ["B dec.md", "A ref.md"]
    assert {r["path"]: r["distance"] for r in out["results"]} == {"A ref.md": 0.70, "B dec.md": 0.75}
    assert out["no_good_answer"] is False
    assert [p["path"] for p in out["pinned_core"]] == ["Core/Identity.md"]


def test_no_good_answer_unchanged_with_flag_on(typed_vault, fake_store, monkeypatch):
    for on in (False, True):
        _flag(monkeypatch, on)
        monkeypatch.setattr(rquery, "semantic_search", _fake_semantic([], no_good=True))
        out = json.loads(search_tools.vault_search_semantic(query="nothing"))
        assert out["no_good_answer"] is True and out["results"] == [] and "note" in out


def test_text_search_order_flag_off_unchanged_flag_on_typed(typed_vault, monkeypatch):
    _flag(monkeypatch, False)
    off = json.loads(search_tools.vault_search(query="alpha"))
    _flag(monkeypatch, True)
    on = json.loads(search_tools.vault_search(query="alpha"))
    assert sorted(r["path"] for r in off["results"]) == sorted(r["path"] for r in on["results"])
    assert [r["path"] for r in on["results"]].index("B dec.md") < [r["path"] for r in on["results"]].index("A ref.md")
    assert "pinned_core" not in off and "pinned_core" in on


def test_read_logs_recall_only_when_flag_on(typed_vault, monkeypatch, tmp_path):
    log = tmp_path / "recalls.jsonl"
    _flag(monkeypatch, False)
    vault_read("A ref.md")
    assert not log.exists()
    _flag(monkeypatch, True)
    vault_read("A ref.md")
    vault_read("missing.md")
    vault_batch_read(["B dec.md", "nope.md"])
    vault_batch_read(["A ref.md"], include_content=False)
    paths = [json.loads(line)["path"] for line in log.read_text().splitlines()]
    assert paths == ["A ref.md", "B dec.md"]


def test_missing_datalayer_dir_means_off(typed_vault, monkeypatch, tmp_path):
    _flag(monkeypatch, True)
    monkeypatch.setattr(datalayer, "DATALAYER_DIR", tmp_path / "absent")
    monkeypatch.setattr(datalayer, "_lib", lambda name="rerank": None)
    assert datalayer.enabled() is False
    assert datalayer.pinned_core() is None
    xs = [_res("A ref.md", 0.7)]
    assert datalayer.rerank_semantic(xs) is xs


def test_semantic_flag_on_fetches_deeper_and_truncates(typed_vault, fake_store, monkeypatch):
    seen = {}

    def fake(**kw):
        seen["top_k"] = kw["top_k"]
        res = [_res("A ref.md", 0.50), _res("A ref.md", 0.55), _res("B dec.md", 0.60)]
        return SemanticSearchResponse(results=res, query_text=kw["query"], no_good_answer=False, searched_summary="s")

    monkeypatch.setattr(rquery, "semantic_search", fake)
    _flag(monkeypatch, False)
    search_tools.vault_search_semantic(query="alpha", max_results=2)
    assert seen["top_k"] == 2
    _flag(monkeypatch, True)
    out = json.loads(search_tools.vault_search_semantic(query="alpha", max_results=2))
    assert seen["top_k"] == 2 * datalayer.DEPTH_FACTOR
    assert len(out["results"]) == 2
    assert out["results"][0]["path"] == "B dec.md"  # promoted from rank 3 of the deep fetch


def test_text_search_flag_on_with_unquoted_yaml_dates(typed_vault, monkeypatch):
    """Regression 2026-10-04: unquoted YAML dates (PyYAML -> datetime.date) used to make
    vault_search return a JSON-serialisation error, which also hid the re-rank path live.
    Flag on: still reorders (decision above reference), same result set, dates as ISO strings."""
    (typed_vault / "C ref.md").write_text("---\nmemory_type: reference\ndate: 2026-10-01\n---\nbeta\n")
    (typed_vault / "D dec.md").write_text("---\nmemory_type: decision\ndate: 2026-10-01 09:00:00\n---\nbeta\n")
    _flag(monkeypatch, False)
    off = json.loads(search_tools.vault_search(query="beta"))
    _flag(monkeypatch, True)
    on = json.loads(search_tools.vault_search(query="beta"))
    assert "error" not in off and "error" not in on, (off, on)
    paths = [r["path"] for r in on["results"]]
    assert sorted(paths) == sorted(r["path"] for r in off["results"]) == ["C ref.md", "D dec.md"]
    assert paths.index("D dec.md") < paths.index("C ref.md")
    by = {r["path"]: r["frontmatter_excerpt"] for r in on["results"]}
    assert by["C ref.md"]["date"] == "2026-10-01"
    assert by["D dec.md"]["date"] == "2026-10-01T09:00:00"
    assert "pinned_core" in on
