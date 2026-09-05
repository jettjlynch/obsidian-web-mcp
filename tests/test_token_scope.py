"""Tests for the C-2 read/write token-scope split.

SECURITY.md C-2: prior to this, any token that authenticated at all (static
or per-client) could call every tool, including
vault_write/append/delete/move/batch_frontmatter -- no split existed. This
file locks in:

1. The audit.py-layer gate (`_scope_allows` / `audited()`'s scope check) --
   unit-level, against fake tools, mirroring test_audit.py/test_rate_limit.py's
   own style.
2. Every REAL registered tool in server.py, read and write alike, driven off
   their actual `audit_kind` (not a hand-maintained parallel list -- see
   `_all_kinded_tools` below) -- so this suite can't silently miss a new tool.
3. The OAuth consent-page + issuance side: default-to-read, explicit write
   checkbox, scope in the token response, and the pre-existing-token
   migration (unscoped tokens are purged, not grandfathered to write).

Nothing here touches H-1, H-2, or L-2 -- those are separate, still-open
findings. The legacy static token is deliberately left grandfathered to
"write" throughout (see oauth.get_token_scope's docstring) -- retiring it is
H-2/S2's job.
"""

import json
import logging

import pytest

from obsidian_vault_mcp import config, oauth, token_scope
from obsidian_vault_mcp.audit import audited, _scope_allows


# --- _scope_allows / audited() gate, unit-level (fake tools) --------------

def test_scope_allows_matrix():
    assert _scope_allows("read", "read") is True
    assert _scope_allows("read", "write") is True   # write implies read
    assert _scope_allows("read", None) is False
    assert _scope_allows("write", "write") is True
    assert _scope_allows("write", "read") is False  # the actual C-2 fix
    assert _scope_allows("write", None) is False


def test_audited_write_tool_denied_under_read_scope(caplog):
    caplog.set_level(logging.INFO, logger="obsidian_vault_mcp.audit")
    token_scope.current_scope.set("read")

    calls = []

    @audited("fake_write_tool", kind="write")
    def fake_write(path: str) -> str:
        calls.append(path)
        return json.dumps({"ok": True})

    result = json.loads(fake_write(path="a.md"))
    assert "error" in result
    assert "requires write-scope" in result["error"]
    assert calls == []  # fn never called

    entries = [json.loads(r.message) for r in caplog.records]
    assert entries[-1]["ok"] is False
    assert entries[-1]["scope_denied"] is True
    assert entries[-1]["granted_scope"] == "read"
    # Distinct from rate-limiting's own event marker (item 5: a distinct
    # event type, not folded into a generic "denied").
    assert "rate_limited" not in entries[-1]


def test_audited_write_tool_denied_with_no_scope_at_all(caplog):
    caplog.set_level(logging.INFO, logger="obsidian_vault_mcp.audit")
    token_scope.current_scope.set(None)

    @audited("fake_write_tool", kind="write")
    def fake_write() -> str:
        return json.dumps({"ok": True})

    result = json.loads(fake_write())
    assert "error" in result

    entries = [json.loads(r.message) for r in caplog.records]
    assert entries[-1]["scope_denied"] is True
    assert entries[-1]["granted_scope"] is None


def test_audited_write_tool_allowed_under_write_scope():
    token_scope.current_scope.set("write")

    @audited("fake_write_tool", kind="write")
    def fake_write() -> str:
        return json.dumps({"ok": True})

    assert json.loads(fake_write())["ok"] is True


def test_audited_read_tool_allowed_under_either_scope():
    @audited("fake_read_tool", kind="read")
    def fake_read() -> str:
        return json.dumps({"ok": True})

    token_scope.current_scope.set("read")
    assert json.loads(fake_read())["ok"] is True

    token_scope.current_scope.set("write")
    assert json.loads(fake_read())["ok"] is True


def test_audited_read_tool_denied_with_no_scope_at_all():
    token_scope.current_scope.set(None)

    @audited("fake_read_tool", kind="read")
    def fake_read() -> str:
        return json.dumps({"ok": True})

    result = json.loads(fake_read())
    assert "error" in result
    assert "requires read-scope" in result["error"]


def test_audited_without_kind_ignores_scope_entirely():
    """kind=None already means 'skip rate limiting' (rate_limit.py) -- scope
    enforcement piggybacks on the same escape hatch, deliberately, so tools
    that are neither read nor write (none exist today) aren't forced into
    the split. Also protects test_audit.py's pre-existing kind-less tests
    from this change."""
    token_scope.current_scope.set(None)

    @audited("fake_tool")  # no kind
    def fake_tool() -> str:
        return json.dumps({"ok": True})

    assert json.loads(fake_tool())["ok"] is True


# --- Every real registered tool, driven off server.py's own annotations ---

import obsidian_vault_mcp.server as server  # noqa: E402  (after fixtures import cleanly)

# One entry per tool that declares a kind in server.py. Write-class calls use
# dry_run=True (or, for vault_delete, confirm's default False) wherever the
# tool supports it, so this suite never mutates vault_dir regardless of
# whether the scope gate lets the call through.
_MINIMAL_ARGS = {
    "vault_read": {"path": "test-note.md"},
    "vault_batch_read": {"paths": ["test-note.md"]},
    "vault_search": {"query": "test"},
    "vault_search_frontmatter": {"field": "status", "value": "active"},
    "vault_list": {},
    "vault_read_section": {"path": "test-note.md", "heading": "Nonexistent"},
    "vault_links": {"path": "test-note.md"},
    "vault_backlinks": {"target": "test-note.md"},
    "vault_tags": {},
    "vault_write": {"path": "scope-probe.md", "content": "x", "dry_run": True},
    "vault_append": {"path": "scope-probe.md", "content": "x", "dry_run": True},
    "vault_batch_frontmatter_update": {
        "updates": [{"path": "test-note.md", "fields": {"x": "y"}}],
        "dry_run": True,
    },
    "vault_move": {"source": "test-note.md", "destination": "moved.md", "dry_run": True},
    "vault_delete": {"path": "test-note.md"},  # confirm defaults False -- never deletes
    "vault_find_replace": {"path": "test-note.md", "find": "test", "replace": "TEST", "dry_run": True},
    "vault_insert_at": {
        "path": "test-note.md",
        "content": "x",
        "after_line": "This is a test note with some content.",
        "dry_run": True,
    },
    "vault_replace_section": {"path": "test-note.md", "heading": "Nonexistent", "new_content": "x", "dry_run": True},
    "vault_append_under_heading": {"path": "test-note.md", "heading": "Nonexistent", "content": "x", "dry_run": True},
    "vault_prepend": {"path": "test-note.md", "content": "x", "dry_run": True},
    "vault_daily": {"dry_run": True},
}


def _all_kinded_tools() -> dict[str, str]:
    """Every server.py attribute carrying a real audit_kind ("read"/"write"),
    keyed by name. Derived from the live module, not hand-copied, so a tool
    added to server.py without a matching _MINIMAL_ARGS entry fails the
    coverage test below instead of silently not being exercised.
    """
    found = {}
    for name in dir(server):
        kind = getattr(getattr(server, name), "audit_kind", None)
        if kind in ("read", "write"):
            found[name] = kind
    return found


def test_minimal_args_cover_every_kinded_tool():
    assert set(_all_kinded_tools()) == set(_MINIMAL_ARGS), (
        "A read/write tool was added to or removed from server.py without "
        "updating _MINIMAL_ARGS above -- keep this test suite in sync."
    )


def test_read_scope_rejected_on_every_write_class_tool(vault_dir):
    """The literal C-2 requirement: a read-scope token must be REJECTED on
    every write-class tool."""
    token_scope.current_scope.set("read")
    for name, kind in _all_kinded_tools().items():
        if kind != "write":
            continue
        fn = getattr(server, name)
        result = json.loads(fn(**_MINIMAL_ARGS[name]))
        assert "error" in result, f"{name}: expected a scope-denial error, got {result}"
        assert "requires write-scope access" in result["error"], f"{name}: {result}"


def test_read_scope_accepted_on_every_read_class_tool(vault_dir):
    """The literal C-2 requirement: a read-scope token must be ACCEPTED
    (i.e. reach real tool logic, not the scope gate) on every read-class
    tool. Some may still return an unrelated business-logic error (e.g. a
    heading that doesn't exist) -- that's fine, it proves the gate let the
    call through; only a scope-denial specifically is a failure here."""
    token_scope.current_scope.set("read")
    for name, kind in _all_kinded_tools().items():
        if kind != "read":
            continue
        fn = getattr(server, name)
        result = json.loads(fn(**_MINIMAL_ARGS[name]))
        error = result.get("error", "") or ""
        assert "requires read-scope access" not in error, f"{name} was scope-denied: {result}"


def test_write_scope_accepted_on_every_write_class_tool(vault_dir):
    """Round out the matrix: write scope must still work for write-class
    tools -- this task is a split, not a lockout."""
    token_scope.current_scope.set("write")
    for name, kind in _all_kinded_tools().items():
        if kind != "write":
            continue
        fn = getattr(server, name)
        result = json.loads(fn(**_MINIMAL_ARGS[name]))
        error = result.get("error", "") or ""
        assert "requires write-scope access" not in error, f"{name} was scope-denied: {result}"


# --- OAuth issuance side: default read, explicit write checkbox -----------

PIN = "1234"
STATIC_TOKEN = "the-real-shared-static-token"
JARVIS_REDIRECT = "jarvisapp://oauth/callback"


def _isolate_oauth_state(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "VAULT_OAUTH_AUTHORIZE_PIN", PIN)
    monkeypatch.setattr(config, "VAULT_MCP_TOKEN", STATIC_TOKEN)
    monkeypatch.setattr(config, "VAULT_OAUTH_STATIC_CLIENT_ID", "jarvis-app")
    monkeypatch.setattr(config, "VAULT_OAUTH_REDIRECT_URIS", [JARVIS_REDIRECT])

    monkeypatch.setattr(oauth, "_CLIENTS_FILE", tmp_path / "oauth_clients.json")
    monkeypatch.setattr(oauth, "_TOKENS_FILE", tmp_path / "oauth_tokens.json")
    monkeypatch.setattr(oauth, "_registered_clients", {})
    monkeypatch.setattr(oauth, "_issued_tokens", {})
    monkeypatch.setattr(oauth, "_auth_codes", {})

    oauth._register_client("jarvis-app", [JARVIS_REDIRECT], "jarvis-app (static, pre-registered)")


def _client(monkeypatch, tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    _isolate_oauth_state(monkeypatch, tmp_path)
    app = Starlette(routes=oauth.oauth_routes)
    return TestClient(app, follow_redirects=False)


def test_consent_form_write_checkbox_unchecked_by_default(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "jarvis-app",
        "redirect_uri": JARVIS_REDIRECT,
        "code_challenge": "x",
    })
    assert resp.status_code == 200
    assert 'name="scope" value="write"' in resp.text
    assert 'name="scope" value="write" checked' not in resp.text


def test_consent_form_prechecks_write_when_client_hints_it(monkeypatch, tmp_path):
    """A client CAN hint scope=write to pre-check the box -- pure UX, still
    requires the human to submit with the correct PIN to mean anything."""
    client = _client(monkeypatch, tmp_path)
    resp = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "jarvis-app",
        "redirect_uri": JARVIS_REDIRECT,
        "code_challenge": "x",
        "scope": "write",
    })
    assert resp.status_code == 200
    assert 'name="scope" value="write" checked' in resp.text


def _pkce_pair():
    import base64
    import hashlib
    import secrets as _secrets

    verifier = _secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _run_pkce_flow(client, *, tick_write: bool):
    verifier, challenge = _pkce_pair()
    data = {
        "response_type": "code",
        "client_id": "jarvis-app",
        "redirect_uri": JARVIS_REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "pin": PIN,
    }
    if tick_write:
        data["scope"] = "write"  # a real browser only sends this when checked
    authz = client.post("/oauth/authorize", data=data)
    assert authz.status_code == 302
    code = authz.headers["location"].split("code=")[1].split("&")[0]

    token_resp = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": JARVIS_REDIRECT,
        "code_verifier": verifier,
    })
    assert token_resp.status_code == 200
    return token_resp.json()


def test_pkce_flow_without_checkbox_issues_read_only_token(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    body = _run_pkce_flow(client, tick_write=False)
    assert body["scope"] == "read"
    assert oauth.get_issued_token_scope(body["access_token"]) == "read"


def test_pkce_flow_with_checkbox_ticked_issues_write_token(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    body = _run_pkce_flow(client, tick_write=True)
    assert body["scope"] == "write"
    assert oauth.get_issued_token_scope(body["access_token"]) == "write"


def test_client_credentials_grant_reports_write_scope(monkeypatch, tmp_path):
    """Unchanged behavior (H-2's territory) -- just confirms the response now
    honestly reports what it already grants."""
    _isolate_oauth_state(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "VAULT_OAUTH_CLIENT_ID", "creds-client")
    monkeypatch.setattr(config, "VAULT_OAUTH_CLIENT_SECRET", "creds-secret")

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    app = Starlette(routes=oauth.oauth_routes)
    client = TestClient(app)
    resp = client.post("/oauth/token", data={
        "grant_type": "client_credentials",
        "client_id": "creds-client",
        "client_secret": "creds-secret",
    })
    assert resp.status_code == 200
    assert resp.json()["scope"] == "write"


# --- get_token_scope: static-token grandfather + invalid tokens -----------

def test_get_token_scope_static_token_is_write(monkeypatch, tmp_path):
    _isolate_oauth_state(monkeypatch, tmp_path)
    assert oauth.get_token_scope(STATIC_TOKEN) == "write"


def test_get_token_scope_unknown_token_is_none(monkeypatch, tmp_path):
    _isolate_oauth_state(monkeypatch, tmp_path)
    assert oauth.get_token_scope("not-a-real-token") is None


def test_get_token_scope_returns_issued_tokens_own_scope(monkeypatch, tmp_path):
    _isolate_oauth_state(monkeypatch, tmp_path)
    read_tok = oauth._issue_token("jarvis-app", "read")
    write_tok = oauth._issue_token("jarvis-app", "write")
    assert oauth.get_token_scope(read_tok) == "read"
    assert oauth.get_token_scope(write_tok) == "write"


def test_issue_token_rejects_invalid_scope_value(monkeypatch, tmp_path):
    _isolate_oauth_state(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        oauth._issue_token("jarvis-app", "admin")


# --- Migration: pre-scope tokens are purged, not grandfathered to write ---

def test_purge_unscoped_tokens_removes_pre_migration_entries(monkeypatch, tmp_path):
    _isolate_oauth_state(monkeypatch, tmp_path)

    # Simulate a token issued by the pre-C-2 code: no "scope" key at all.
    legacy_token = "legacy-pre-scope-token"
    oauth._issued_tokens[legacy_token] = {
        "client_id": "jarvis-app",
        "expires_at": __import__("time").time() + 3600,
    }
    scoped_token = oauth._issue_token("jarvis-app", "write")

    purged = oauth._purge_unscoped_tokens()

    assert purged == 1
    assert legacy_token not in oauth._issued_tokens
    assert scoped_token in oauth._issued_tokens  # untouched -- it has a scope
    # Not silently grandfathered to write, and not still readable either --
    # gone entirely, per the "require re-approval" decision.
    assert oauth.get_token_scope(legacy_token) is None


def test_purge_unscoped_tokens_is_a_noop_when_already_migrated(monkeypatch, tmp_path):
    _isolate_oauth_state(monkeypatch, tmp_path)
    oauth._issue_token("jarvis-app", "read")
    assert oauth._purge_unscoped_tokens() == 0
