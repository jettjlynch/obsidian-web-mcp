"""Port of upstream f7a7bd9 (#26): atomic-rename writes surface as MOVED events.

write_file_atomic (mkstemp + os.replace) and vault_move (shutil.move) are
reported by watchdog as FileMovedEvent, not created/modified. Without
on_moved the frontmatter index (and the retrieval change listeners that
hang off it) never saw vault_write output until the next restart.
"""

from watchdog.events import FileMovedEvent, DirMovedEvent

from obsidian_vault_mcp.frontmatter_index import FrontmatterIndex, _VaultEventHandler


def _scheduled(idx):
    return set(idx._pending_paths)


def _no_timer(idx):
    # Capture scheduling without starting a real debounce timer thread.
    idx._schedule_debounce = lambda p: idx._pending_paths.add(p)


def test_on_moved_schedules_src_and_dest(vault_dir):
    idx = FrontmatterIndex()
    _no_timer(idx)
    h = _VaultEventHandler(idx)
    src = str(vault_dir / "old.md")
    dest = str(vault_dir / "new.md")
    h.on_moved(FileMovedEvent(src, dest))
    assert _scheduled(idx) == {src, dest}


def test_on_moved_atomic_tmp_to_md_only_schedules_md(vault_dir):
    idx = FrontmatterIndex()
    _no_timer(idx)
    h = _VaultEventHandler(idx)
    tmp = str(vault_dir / ".tmpabc123")
    dest = str(vault_dir / "note.md")
    h.on_moved(FileMovedEvent(tmp, dest))
    assert _scheduled(idx) == {dest}


def test_on_moved_ignores_directories_and_excluded(vault_dir):
    idx = FrontmatterIndex()
    _no_timer(idx)
    h = _VaultEventHandler(idx)
    h.on_moved(DirMovedEvent(str(vault_dir / "a"), str(vault_dir / "b")))
    h.on_moved(FileMovedEvent(str(vault_dir / ".obsidian" / "x.md"), str(vault_dir / ".trash" / "x.md")))
    assert _scheduled(idx) == set()


def test_on_moved_flush_updates_index_and_listeners(vault_dir):
    idx = FrontmatterIndex()
    seen = []
    idx.add_change_listener(lambda rel, exists: seen.append((rel, exists)))
    _no_timer(idx)
    h = _VaultEventHandler(idx)
    (vault_dir / "fresh.md").write_text("---\nstatus: new\n---\nbody\n")
    h.on_moved(FileMovedEvent(str(vault_dir / ".tmpzz"), str(vault_dir / "fresh.md")))
    idx._flush_pending()
    assert idx._index.get("fresh.md", {}).get("status") == "new"
    assert ("fresh.md", True) in seen
