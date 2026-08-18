"""Regression test for the vault_search ripgrep argv-injection RCE.

Emergency fix, 2026-08-18: the user-supplied `query` used to be appended to
the ripgrep argv as a bare positional argument. A query beginning with "-"
was therefore parsed by ripgrep as an OPTION rather than a search pattern.
In particular vault_search(query="--pre=/bin/sh") sets ripgrep's --pre
preprocessor, which runs an arbitrary program against every searched file --
remote code execution reachable from the search tool's query argument.
Root-caused while comparing against jimprosser/obsidian-web-mcp's
independent fix for the identical bug (their commit a4cf931).

Two tests, matching the shape of upstream's own fix verification:
1. A deterministic argv-shape test -- proves `query` is always passed via
   `-e` (forcing pattern interpretation), regardless of whether ripgrep is
   installed in the test environment.
2. An rg-gated end-to-end test using a harmless preprocessor probe
   (/usr/bin/id -- read-only, no side effects, always present on macOS/Linux)
   that proves a "--pre=..." query is never actually executed.
"""

import shutil
import subprocess

import pytest

from obsidian_vault_mcp.tools.search import _search_ripgrep


def test_query_is_always_passed_via_dash_e(monkeypatch, tmp_path):
    """The argv-shape proof: query must never appear as a bare positional."""
    captured = {}

    class _FakeResult:
        stdout = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_run)

    _search_ripgrep("--pre=/bin/sh", tmp_path, "*.md", 20, 2)

    cmd = captured["cmd"]
    assert "-e" in cmd
    e_index = cmd.index("-e")
    # The query must be the argument immediately after -e ...
    assert cmd[e_index + 1] == "--pre=/bin/sh"
    # ... and must not appear anywhere else in the argv (i.e. not ALSO
    # present as a bare positional the way the vulnerable version had it).
    assert cmd.count("--pre=/bin/sh") == 1


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_pre_flag_query_is_never_actually_executed(tmp_path):
    """Live fire, but with a harmless probe: `id` has no side effects."""
    (tmp_path / "note.md").write_text("hello world\n")

    matches = _search_ripgrep("--pre=/usr/bin/id", tmp_path, "*.md", 20, 2)

    # If --pre were honored, ripgrep would run `id <file>` as a preprocessor
    # and its stdout (containing "uid=") would appear somewhere in the
    # matched output. It must not -- the query is a literal search pattern
    # that simply doesn't match "hello world", so there should be zero hits.
    assert matches == []
    assert not any("uid=" in m.get("match_context", "") for m in matches)
