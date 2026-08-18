"""Heartbeat: validated config (fail-closed), capped/no-redirect ping, redacted logs."""

import logging

import pytest

from obsidian_vault_mcp import config, server


# --- the ping itself -------------------------------------------------------

def test_ping_caps_read_and_uses_no_redirect_opener(monkeypatch):
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            seen["read_n"] = n
            return b""

    def fake_open(url, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return FakeResp()

    # _heartbeat_ping must go through the no-redirect opener, not bare urlopen.
    monkeypatch.setattr(server._heartbeat_opener, "open", fake_open)
    server._heartbeat_ping("http://monitor.example/push")

    assert seen["url"] == "http://monitor.example/push"
    assert seen["read_n"] == server._HEARTBEAT_MAX_BYTES  # body read is capped


def test_no_redirect_handler_refuses_to_follow():
    # redirect_request returning None makes urllib raise instead of following.
    assert server._NoRedirect().redirect_request("a", "b", 302, "c", {}, "http://evil") is None


def test_loop_swallows_errors(monkeypatch):
    """A failing ping is logged and the loop proceeds to sleep, never propagating."""
    def boom(url):
        raise OSError("down")

    def stop(_):
        raise KeyboardInterrupt  # break out of the otherwise-infinite loop

    monkeypatch.setattr(server, "_heartbeat_ping", boom)
    monkeypatch.setattr(server.time, "sleep", stop)

    try:
        server._heartbeat_forever("http://x", 1)
    except KeyboardInterrupt:
        pass  # reaching sleep proves the OSError from the ping was swallowed


def test_failure_log_redacts_capability_url(monkeypatch, caplog):
    """The capability URL (secret in the path) must never reach the logs."""
    def boom(url):
        raise OSError("connection refused to http://mon.example/ping/SECRET123")

    def stop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(server, "_heartbeat_ping", boom)
    monkeypatch.setattr(server.time, "sleep", stop)

    with caplog.at_level(logging.WARNING, logger="obsidian_vault_mcp.server"):
        try:
            server._heartbeat_forever("http://mon.example/ping/SECRET123", 1)
        except KeyboardInterrupt:
            pass

    assert "SECRET123" not in caplog.text       # secret path never logged
    assert "mon.example" in caplog.text         # host is fine to log
    assert "OSError" in caplog.text             # exception type, not its message


# --- startup validation (fail closed) --------------------------------------

def test_validate_disabled_when_no_url(monkeypatch):
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", "")
    assert config.validate_heartbeat() is None


def test_validate_returns_interval_when_valid(monkeypatch):
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", "https://hc-ping.com/abc")
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_INTERVAL", "30")
    assert config.validate_heartbeat() == 30


def test_validate_allows_private_lan_target(monkeypatch):
    """A self-hosted monitor on a LAN/loopback address is the common case, not an error."""
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", "http://192.168.1.10:3001/api/push/x")
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_INTERVAL", "60")
    assert config.validate_heartbeat() == 60


@pytest.mark.parametrize("url", ["ftp://x/y", "file:///etc/passwd", "gopher://x"])
def test_validate_rejects_non_http_scheme(monkeypatch, url):
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", url)
    with pytest.raises(ValueError):
        config.validate_heartbeat()


@pytest.mark.parametrize("interval", ["abc", "", "1.5", "0", "-5"])
def test_validate_rejects_bad_interval(monkeypatch, interval):
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", "https://hc-ping.com/abc")
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_INTERVAL", interval)
    with pytest.raises(ValueError):
        config.validate_heartbeat()


@pytest.mark.parametrize("url", ["http://", "https://", "http:///path", "http://h:notaport/"])
def test_validate_rejects_hostless_or_malformed_url(monkeypatch, url):
    """A scheme alone isn't enough -- a hostless or malformed URL must fail closed."""
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", url)
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_INTERVAL", "60")
    with pytest.raises(ValueError):
        config.validate_heartbeat()


def test_validation_errors_never_echo_raw_values(monkeypatch):
    """Error messages must not contain the raw URL/interval (could be a secret)."""
    secret = "SECRETCAP123"
    # operator swaps env vars: a capability URL lands in the interval slot
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", "https://hc-ping.com/abc")
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_INTERVAL", f"http://mon/{secret}")
    with pytest.raises(ValueError) as exc:
        config.validate_heartbeat()
    assert secret not in str(exc.value)


def test_main_fails_closed_and_redacts_on_swapped_env(monkeypatch, tmp_path, caplog):
    """main() exits 1 on a bad heartbeat config and never logs the swapped capability URL."""
    secret = "SECRETCAP456"
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)             # pass is_dir() gate
    monkeypatch.setattr(server.frontmatter_index, "start", lambda: None)
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", "https://hc-ping.com/abc")
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_INTERVAL", f"http://mon/{secret}")
    with caplog.at_level(logging.ERROR, logger="obsidian_vault_mcp.server"):
        with pytest.raises(SystemExit) as exc:
            server.main()
    assert exc.value.code == 1
    assert secret not in caplog.text
