"""Regression tests for the 2026-10-03 OAuth hardening pass:

1. Access-token lifetime 30 days -> 7 days, plus an `issued_at` max-age check
   so a future TTL cut also applies to tokens already in the store.
2. PKCE REQUIRED, S256 only (OAuth 2.1 §4.1.1 / §7.5.2): /oauth/authorize
   rejects a request with no code_challenge or a non-S256 method, and
   /oauth/token always requires a matching code_verifier.
3. PIN brute-force lockout: 5 consecutive wrong PINs -> 15-minute lockout
   (temporary, never permanent), persisted across restarts, reset on success,
   generic "try later" error while locked.
"""

import base64
import hashlib
import json
import logging
import secrets
import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from obsidian_vault_mcp import config, oauth, token_store


PIN = "1234"
JARVIS_REDIRECT = "jarvisapp://oauth/callback"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "VAULT_OAUTH_AUTHORIZE_PIN", PIN)
    monkeypatch.setattr(oauth, "_LOCKOUT_FILE", tmp_path / "oauth_pin_lockout.json")
    monkeypatch.setattr(oauth, "_pin_lockout", {"failures": 0, "locked_until": 0.0})
    oauth._register_client("jarvis-app", [JARVIS_REDIRECT], "jarvis-app (static, pre-registered)")
    app = Starlette(routes=oauth.oauth_routes)
    return TestClient(app, follow_redirects=False)


def _pkce_pair():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _authz_params(challenge, method="S256", **extra):
    p = {
        "response_type": "code",
        "client_id": "jarvis-app",
        "redirect_uri": JARVIS_REDIRECT,
        "state": "st8",
    }
    if challenge is not None:
        p["code_challenge"] = challenge
    if method is not None:
        p["code_challenge_method"] = method
    p.update(extra)
    return p


# --- 1. Token lifetime ------------------------------------------------------

def test_token_ttl_is_seven_days():
    assert oauth._TOKEN_TTL_SECONDS == 7 * 86400


def test_issued_token_records_issued_at_and_7_day_expiry():
    before = time.time()
    tok = oauth._issue_token("jarvis-app", "read", audience="vault")
    entry = oauth._issued_tokens[tok]
    assert before <= entry["issued_at"] <= time.time()
    assert entry["expires_at"] == pytest.approx(entry["issued_at"] + 7 * 86400, abs=1)


def test_token_older_than_max_age_rejected_even_if_expires_at_in_future():
    """A token with issued_at recorded is capped at the current TTL, so
    lowering the constant later also shortens already-issued tokens."""
    tok = oauth._issue_token("jarvis-app", "write", audience="vault")
    oauth._issued_tokens[tok]["issued_at"] = time.time() - 8 * 86400
    oauth._issued_tokens[tok]["expires_at"] = time.time() + 22 * 86400
    assert oauth.get_issued_token_scope(tok) is None
    assert not oauth.is_valid_issued_token(tok)


def test_legacy_token_without_issued_at_still_valid_until_expires_at():
    """Pre-2026-10-03 entries carry no issued_at; they expire naturally."""
    oauth._issued_tokens["legacy"] = {
        "client_id": "jarvis-app", "expires_at": time.time() + 3600,
        "scope": "read", "audience": "vault",
    }
    assert oauth.get_issued_token_scope("legacy") == "read"


def test_bridge_token_store_enforces_max_age(tmp_path, monkeypatch):
    f = tmp_path / "oauth_tokens.json"
    monkeypatch.setattr(token_store, "_TOKENS_FILE", f)
    f.write_text(json.dumps({
        "old": {"client_id": "c", "issued_at": time.time() - 8 * 86400,
                "expires_at": time.time() + 3600, "scope": "read", "audience": "bridge"},
        "new": {"client_id": "c", "issued_at": time.time() - 60,
                "expires_at": time.time() + 3600, "scope": "read", "audience": "bridge"},
    }))
    assert token_store.lookup_issued_token("old") is None
    assert token_store.lookup_issued_token("new") is not None


# --- 2. PKCE required, S256 only -------------------------------------------

def test_authorize_get_without_code_challenge_rejected(client):
    resp = client.get("/oauth/authorize", params=_authz_params(None, method=None))
    assert resp.status_code in (302, 400)
    assert "PIN" not in resp.text
    if resp.status_code == 302:
        loc = resp.headers["location"]
        assert loc.startswith(JARVIS_REDIRECT)
        assert "error=invalid_request" in loc and "code=" not in loc
        assert "state=st8" in loc


def test_authorize_get_with_plain_method_rejected(client):
    _, challenge = _pkce_pair()
    resp = client.get("/oauth/authorize", params=_authz_params(challenge, method="plain"))
    assert resp.status_code in (302, 400)
    assert "PIN" not in resp.text


def test_authorize_get_with_missing_method_rejected(client):
    """RFC 7636: absent method means "plain" -- not acceptable here."""
    _, challenge = _pkce_pair()
    resp = client.get("/oauth/authorize", params=_authz_params(challenge, method=None))
    assert resp.status_code in (302, 400)
    assert "PIN" not in resp.text


def test_authorize_get_with_malformed_challenge_rejected(client):
    resp = client.get("/oauth/authorize", params=_authz_params("x"))
    assert resp.status_code in (302, 400)
    assert "PIN" not in resp.text


def test_authorize_get_with_s256_reaches_pin_form(client):
    _, challenge = _pkce_pair()
    resp = client.get("/oauth/authorize", params=_authz_params(challenge))
    assert resp.status_code == 200
    assert "PIN" in resp.text


def test_authorize_post_without_challenge_rejected_even_with_correct_pin(client):
    resp = client.post("/oauth/authorize", data={**_authz_params(None, method=None), "pin": PIN})
    assert resp.status_code in (302, 400)
    if resp.status_code == 302:
        assert "code=" not in resp.headers["location"]


def test_token_requires_code_verifier(client):
    _, challenge = _pkce_pair()
    authz = client.post("/oauth/authorize", data={**_authz_params(challenge), "pin": PIN})
    code = authz.headers["location"].split("code=")[1].split("&")[0]
    resp = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": JARVIS_REDIRECT,
    })
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_token_rejects_wrong_verifier(client):
    _, challenge = _pkce_pair()
    authz = client.post("/oauth/authorize", data={**_authz_params(challenge), "pin": PIN})
    code = authz.headers["location"].split("code=")[1].split("&")[0]
    resp = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": JARVIS_REDIRECT,
        "code_verifier": secrets.token_urlsafe(32),
    })
    assert resp.status_code == 400


def test_token_rejects_non_ascii_verifier_without_500(client):
    _, challenge = _pkce_pair()
    authz = client.post("/oauth/authorize", data={**_authz_params(challenge), "pin": PIN})
    code = authz.headers["location"].split("code=")[1].split("&")[0]
    resp = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": JARVIS_REDIRECT,
        "code_verifier": "é" * 50,
    })
    assert resp.status_code == 400


def test_full_s256_flow_succeeds_with_7_day_expires_in(client):
    verifier, challenge = _pkce_pair()
    authz = client.post("/oauth/authorize", data={**_authz_params(challenge), "pin": PIN})
    assert authz.status_code == 302
    code = authz.headers["location"].split("code=")[1].split("&")[0]
    resp = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": JARVIS_REDIRECT,
        "code_verifier": verifier,
    })
    assert resp.status_code == 200
    assert resp.json()["expires_in"] == 7 * 86400


# --- 3. PIN lockout ---------------------------------------------------------

def _post_pin(client, pin):
    _, challenge = _pkce_pair()
    return client.post("/oauth/authorize", data={**_authz_params(challenge), "pin": pin})


def test_four_wrong_pins_then_correct_succeeds_and_resets(client):
    for _ in range(4):
        assert _post_pin(client, "0000").status_code == 401
    assert _post_pin(client, PIN).status_code == 302
    assert oauth._pin_lockout["failures"] == 0
    # Counter reset: another 4 wrong + correct still works.
    for _ in range(4):
        assert _post_pin(client, "0000").status_code == 401
    assert _post_pin(client, PIN).status_code == 302


def test_five_wrong_pins_locks_out_even_correct_pin(client):
    for _ in range(5):
        _post_pin(client, "0000")
    resp = _post_pin(client, PIN)
    assert resp.status_code == 429
    assert "code=" not in resp.headers.get("location", "")
    assert "try again later" in resp.text.lower()
    # No oracle: the locked response doesn't say whether the PIN was right.
    assert "incorrect" not in resp.text.lower()


def test_lockout_is_15_minutes_and_expires(client, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(oauth.time, "time", lambda: now[0])
    for _ in range(5):
        _post_pin(client, "0000")
    assert oauth._pin_lockout["locked_until"] == pytest.approx(now[0] + 15 * 60)
    now[0] += 14 * 60
    assert _post_pin(client, PIN).status_code == 429
    now[0] += 2 * 60
    assert _post_pin(client, PIN).status_code == 302


def test_lockout_never_permanent_even_if_state_file_tampered(client, monkeypatch, tmp_path):
    """A corrupt or far-future locked_until is clamped to at most 15 min."""
    now = 2_000_000.0
    monkeypatch.setattr(oauth.time, "time", lambda: now)
    oauth._LOCKOUT_FILE.write_text(json.dumps({"failures": 0, "locked_until": now + 10 * 365 * 86400}))
    state = oauth._load_lockout()
    assert state["locked_until"] <= now + 15 * 60


def test_lockout_persisted_across_restart(client):
    for _ in range(5):
        _post_pin(client, "0000")
    on_disk = json.loads(oauth._LOCKOUT_FILE.read_text())
    assert on_disk["locked_until"] > time.time()
    # Simulate restart: reload from disk.
    reloaded = oauth._load_lockout()
    assert reloaded["locked_until"] == pytest.approx(on_disk["locked_until"])
    assert oct(oauth._LOCKOUT_FILE.stat().st_mode & 0o777) == "0o600"


def test_corrupt_lockout_file_starts_clean(client):
    oauth._LOCKOUT_FILE.write_text("{not json")
    assert oauth._load_lockout() == {"failures": 0, "locked_until": 0.0}


def test_failures_and_lockout_audit_logged_without_pin(client, caplog):
    caplog.set_level(logging.WARNING, logger="obsidian_vault_mcp.audit")
    for _ in range(5):
        _post_pin(client, "9876")
    audit = [r.getMessage() for r in caplog.records if r.name == "obsidian_vault_mcp.audit"]
    assert sum("oauth_pin_failure" in m for m in audit) == 5
    assert any("oauth_pin_lockout" in m for m in audit)
    assert not any("9876" in m or PIN in m for m in audit)
