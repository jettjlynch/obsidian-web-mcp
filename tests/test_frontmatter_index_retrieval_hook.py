"""Tests for FrontmatterIndex's change-listener hook.

Per RETRIEVAL-DESIGN.md §2.3 / §5 build order step 3: the incremental
indexer must "hook into the existing file-watcher layer" -- extend it,
don't stand up a second watchdog.Observer on the same vault path (that
exact collision is the documented 2026-08-03 hang, see
test_frontmatter_index_lifecycle.py). This tests the extension point itself,
not the retrieval indexer's own logic (covered in test_retrieval_indexer.py).

These tests seed `_pending_paths` and call `_flush_pending` directly rather
than going through `_schedule_debounce`'s real timer, so they don't need a
live watchdog.Observer, a multi-second sleep, or a dangling background
timer thread left running after the test returns.
"""

from obsidian_vault_mcp.frontmatter_index import FrontmatterIndex


def test_change_listener_is_called_on_flush_with_relative_path(vault_dir):
    idx = FrontmatterIndex()
    seen = []
    idx.add_change_listener(lambda rel_path, exists: seen.append((rel_path, exists)))

    abs_path = vault_dir / "test-note.md"
    idx._pending_paths.add(str(abs_path))
    idx._flush_pending()

    assert ("test-note.md", True) in seen


def test_change_listener_told_when_file_no_longer_exists(vault_dir):
    idx = FrontmatterIndex()
    seen = []
    idx.add_change_listener(lambda rel_path, exists: seen.append((rel_path, exists)))

    ghost = vault_dir / "never-existed.md"
    idx._pending_paths.add(str(ghost))
    idx._flush_pending()

    assert ("never-existed.md", False) in seen


def test_listener_exception_does_not_break_frontmatter_flush(vault_dir):
    idx = FrontmatterIndex()

    def bad_listener(rel_path, exists):
        raise RuntimeError("boom")

    idx.add_change_listener(bad_listener)

    abs_path = vault_dir / "test-note.md"
    idx._pending_paths.add(str(abs_path))
    idx._flush_pending()  # must not raise

    # The frontmatter index itself still updated despite the listener blowing up.
    assert "test-note.md" in idx._index


def test_multiple_listeners_all_get_called(vault_dir):
    idx = FrontmatterIndex()
    calls_a, calls_b = [], []
    idx.add_change_listener(lambda p, e: calls_a.append(p))
    idx.add_change_listener(lambda p, e: calls_b.append(p))

    abs_path = vault_dir / "test-note.md"
    idx._pending_paths.add(str(abs_path))
    idx._flush_pending()

    assert "test-note.md" in calls_a
    assert "test-note.md" in calls_b
