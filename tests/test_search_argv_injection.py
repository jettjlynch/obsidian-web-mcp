"""Regression tests for the vault_search ripgrep argv-injection RCE.

Emergency fix, 2026-08-18: the user-supplied `query` used to be appended to the
ripgrep argv as a bare positional argument. A query beginning with "-" was
therefore parsed by ripgrep as an OPTION rather than a search pattern. In
particular vault_search(query="--pre=/bin/sh") sets ripgrep's --pre
preprocessor, which runs an arbitrary program against every searched file --
remote code execution reachable from the search tool's query argument. Fixed
by passing the query with `-e`, which forces ripgrep to treat it as a search
pattern regardless of a leading "-". Root-caused independently on both sides
of this merge: locally, and upstream in jimprosser/obsidian-web-mcp's commit
a4cf931 -- both fixes land on the same `-e` guard, so both regression suites
are kept here for coverage of each side's specific assertions.
"""

import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from obsidian_vault_mcp.tools import search as search_mod
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


def test_query_is_passed_with_dash_e(monkeypatch, tmp_path):
    """The query must be guarded by `-e` so a leading-dash value can't be a flag."""
    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(search_mod.subprocess, "run", fake_run)

    malicious = "--pre=/bin/sh"
    search_mod._search_ripgrep(
        malicious, tmp_path, file_pattern="*.md", max_results=10, context_lines=2
    )

    cmd = captured["cmd"]
    # The query must appear immediately after a `-e`, never as a bare token.
    assert "-e" in cmd, f"query not guarded by -e: {cmd}"
    assert cmd[cmd.index(malicious) - 1] == "-e", (
        f"query must be preceded by -e to neutralize leading-dash flags: {cmd}"
    )
    # And the search path stays a positional after the guarded query.
    assert cmd[-1] == str(tmp_path)


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


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_leading_dash_query_is_treated_as_literal_pattern(tmp_path, monkeypatch):
    """End-to-end: a '--pre=...'-style query is searched literally, not executed."""
    monkeypatch.setattr(search_mod.config, "VAULT_PATH", tmp_path)
    sentinel = tmp_path / "PWNED"
    (tmp_path / "note.md").write_text("a line containing --pre=/bin/sh literally\n")

    # Reuse the real builder so we exercise the exact argv the server sends.
    matches = search_mod._search_ripgrep(
        "--pre=/bin/sh", tmp_path, file_pattern="*.md", max_results=10, context_lines=1
    )

    # No preprocessor program ran...
    assert not sentinel.exists(), "ripgrep executed the query as a --pre program"
    # ...and the query matched as a literal substring of the note.
    assert any("--pre=/bin/sh" in m.get("match_context", "") for m in matches), (
        f"leading-dash query was not treated as a literal pattern: {matches}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
