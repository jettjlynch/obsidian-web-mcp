"""Tests for rate_limit.py (vault-mcp audit backlog item #2)."""

import json

import pytest

from obsidian_vault_mcp import config, rate_limit
from obsidian_vault_mcp.rate_limit import rate_limited


@pytest.fixture(autouse=True)
def _clean_rate_limit_state():
    """Each test starts with an empty sliding window and restores config limits after."""
    rate_limit.reset()
    orig_read, orig_write = config.RATE_LIMIT_READ, config.RATE_LIMIT_WRITE
    yield
    config.RATE_LIMIT_READ, config.RATE_LIMIT_WRITE = orig_read, orig_write
    rate_limit.reset()


def test_check_allows_calls_under_the_limit():
    config.RATE_LIMIT_READ = 3
    rate_limit.check("read")
    rate_limit.check("read")
    rate_limit.check("read")  # 3rd call, still within limit


def test_check_raises_once_limit_exceeded():
    config.RATE_LIMIT_READ = 2
    rate_limit.check("read")
    rate_limit.check("read")
    with pytest.raises(rate_limit.RateLimitExceeded):
        rate_limit.check("read")


def test_read_and_write_limits_are_independent():
    config.RATE_LIMIT_READ = 1
    config.RATE_LIMIT_WRITE = 5
    rate_limit.check("read")
    with pytest.raises(rate_limit.RateLimitExceeded):
        rate_limit.check("read")
    # write budget is untouched by read exhaustion
    rate_limit.check("write")
    rate_limit.check("write")


def test_zero_limit_disables_enforcement():
    config.RATE_LIMIT_READ = 0
    for _ in range(50):
        rate_limit.check("read")  # never raises


def test_rate_limited_short_circuits_and_logs_without_calling_fn(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="obsidian_vault_mcp.rate_limit")
    config.RATE_LIMIT_READ = 1

    calls = []

    @rate_limited(kind="read")
    def fake_tool(path: str) -> str:
        calls.append(path)
        return json.dumps({"ok": True})

    fake_tool(path="a.md")
    assert calls == ["a.md"]

    result = json.loads(fake_tool(path="b.md"))
    assert "error" in result
    assert calls == ["a.md"]  # second call never reached fn

    assert any("Rate limit rejected" in r.message for r in caplog.records)


def test_rate_limited_tool_not_wrapped_is_never_rate_limited():
    """A tool with no @rate_limited decorator at all is simply never checked --
    the old `audited(..., kind=None)` escape hatch had no other purpose than this
    once rate limiting and audit logging split into separate decorators/call sites
    (2026-08-18 upstream merge); a tool that wants no rate limiting just omits the
    decorator rather than applying it with a null kind."""
    config.RATE_LIMIT_READ = 1
    config.RATE_LIMIT_WRITE = 1

    def fake_tool() -> str:
        return json.dumps({"ok": True})

    for _ in range(10):
        result = json.loads(fake_tool())
        assert "error" not in result
