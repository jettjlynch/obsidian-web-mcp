"""Regression: RetrievalStore must be usable from threads other than the one
that created it.

Live bug (2026-10-03): server.py builds the store on the main thread at
startup, but frontmatter_index fires the retrieval change listener from a
threading.Timer thread (_flush_pending), and sync MCP tools can run on a
worker thread. With sqlite3's default check_same_thread=True every listener
call raised "ProgrammingError: SQLite objects created in a thread can only be
used in that same thread" -- 9,044 times in vault-mcp-error.log, so edited
notes were never re-indexed for semantic search.
"""

import threading

from obsidian_vault_mcp.retrieval.store import RetrievalStore

from .test_retrieval_store import DIM, _chunk, _vec


def _run_in_thread(fn):
    box = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 -- surface to the test
            box["error"] = e

    t = threading.Thread(target=target)
    t.start()
    t.join(10)
    assert not t.is_alive(), "worker thread hung"
    if "error" in box:
        raise box["error"]
    return box.get("result")


def test_store_created_on_one_thread_is_usable_from_another(tmp_path):
    store = RetrievalStore(tmp_path / "r.sqlite", embedding_dim=DIM)
    try:
        chunks = [_chunk("alpha", content_hash="ha")]
        # The exact listener path that failed live: diff + apply from a non-creator thread.
        _run_in_thread(lambda: store.diff("note.md", chunks))
        _run_in_thread(lambda: store.apply("note.md", chunks, {"ha": _vec(1, 0)}))
        assert _run_in_thread(lambda: store.existing_hashes("note.md")) == {"ha"}
        hits = _run_in_thread(lambda: store.query(_vec(1, 0), top_k=5))
        assert [h.file_path for h in hits] == ["note.md"]
        _run_in_thread(lambda: store.delete_file("note.md"))
        assert store.existing_hashes("note.md") == set()
    finally:
        store.close()


def test_concurrent_writers_and_readers_do_not_corrupt_or_raise(tmp_path):
    store = RetrievalStore(tmp_path / "r.sqlite", embedding_dim=DIM)
    errors: list[BaseException] = []

    def writer(i):
        try:
            for n in range(20):
                h = f"h{i}-{n}"
                store.apply(f"f{i}.md", [_chunk(f"t{i}-{n}", content_hash=h)], {h: _vec(1, i)})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        try:
            for _ in range(40):
                store.query(_vec(1, 0), top_k=5)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    try:
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == []
        # Each file ends reconciled to exactly its last chunk.
        for i in range(4):
            assert store.existing_hashes(f"f{i}.md") == {f"h{i}-19"}
    finally:
        store.close()
