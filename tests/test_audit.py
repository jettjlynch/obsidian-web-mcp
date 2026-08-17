"""Tests for the audit-trail decorator (vault-mcp audit backlog item #1)."""

import json
import logging

import pytest

from obsidian_vault_mcp.audit import audited


@pytest.fixture
def caught_logs(caplog):
    caplog.set_level(logging.INFO, logger="obsidian_vault_mcp.audit")
    return caplog


def _entries(caught_logs):
    return [json.loads(r.message) for r in caught_logs.records]


def test_audited_logs_success_with_identifying_args(caught_logs):
    @audited("fake_tool")
    def fake_tool(path: str, content: str) -> str:
        return json.dumps({"ok": True})

    fake_tool(path="a.md", content="super secret note body")

    entries = _entries(caught_logs)
    assert len(entries) == 1
    assert entries[0]["tool"] == "fake_tool"
    assert entries[0]["ok"] is True
    assert entries[0]["args"] == {"path": "a.md"}  # content body dropped
    assert "super secret note body" not in caught_logs.text


def test_audited_logs_json_error_result(caught_logs):
    @audited("fake_tool")
    def fake_tool(path: str) -> str:
        return json.dumps({"error": "boom", "path": path})

    fake_tool(path="a.md")

    entries = _entries(caught_logs)
    assert entries[0]["ok"] is False
    assert entries[0]["error"] == "boom"


def test_audited_logs_and_reraises_exception(caught_logs):
    @audited("fake_tool")
    def fake_tool(path: str) -> str:
        raise FileNotFoundError(f"missing: {path}")

    with pytest.raises(FileNotFoundError):
        fake_tool(path="missing.md")

    entries = _entries(caught_logs)
    assert entries[0]["ok"] is False
    assert "missing.md" in entries[0]["error"]


def test_audited_redacts_batch_update_field_values(caught_logs):
    @audited("vault_batch_frontmatter_update")
    def fake_batch(updates: list) -> str:
        return json.dumps({"ok": True})

    fake_batch(updates=[{"path": "a.md", "fields": {"secret": "value"}}])

    entries = _entries(caught_logs)
    assert entries[0]["args"]["updates"] == ["a.md"]
    assert "secret" not in caught_logs.text
