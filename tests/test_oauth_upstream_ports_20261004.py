"""OAuth fixes ported from upstream jimprosser/obsidian-web-mcp on 2026-10-04.

- c385d41 (#4b) / e4924f0: token exchange binds client_id and requires redirect_uri.
- 5bdba5b (#97, register half): /oauth/register capped at 20/hour, globally.
- e975be2 (#41) / 4fa12c1 (#87): OAuth state files written atomically, 0600 from creation.
"""

import base64
import hashlib
import json
import os
import secrets
import stat

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from obsidian_vault_mcp import config, oauth

PIN = "1234"
REDIRECT = "jarvisapp://oauth/callback"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "VAULT_OAUTH_AUTHORIZE_PIN", PIN)
    oauth._register_client("jarvis-app", [REDIRECT], "jarvis-app")
    return TestClient(Starlette(routes=oauth.oauth_routes), follow_redirects=False)


def _code(client):
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = client.post("/oauth/authorize", data={
        "response_type": "code", "client_id": "jarvis-app", "redirect_uri": REDIRECT,
        "state": "s", "code_challenge": challenge, "code_challenge_method": "S256", "pin": PIN,
    })
    assert r.status_code == 302, r.text
    code = r.headers["location"].split("code=")[1].split("&")[0]
    return code, verifier


def _exchange(client, code, verifier, headers=None, **fields):
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    data.update({k: v for k, v in fields.items() if v is not None})
    return client.post("/oauth/token", data=data, headers=headers or {})


# --- client_id / redirect_uri binding ---------------------------------------

def test_exchange_with_matching_client_and_redirect_succeeds(client):
    code, v = _code(client)
    r = _exchange(client, code, v, client_id="jarvis-app", redirect_uri=REDIRECT)
    assert r.status_code == 200 and r.json()["access_token"]


def test_exchange_with_other_client_id_rejected(client):
    oauth._register_client("other", [REDIRECT], "other")
    code, v = _code(client)
    r = _exchange(client, code, v, client_id="other", redirect_uri=REDIRECT)
    assert r.status_code == 400 and r.json()["error_description"] == "client_id mismatch"


def test_exchange_without_client_id_rejected(client):
    code, v = _code(client)
    r = _exchange(client, code, v, redirect_uri=REDIRECT)
    assert r.status_code == 400 and r.json()["error_description"] == "client_id mismatch"


def test_exchange_with_client_id_in_basic_header_succeeds(client):
    code, v = _code(client)
    basic = base64.b64encode(b"jarvis-app:whatever").decode()
    r = _exchange(client, code, v, headers={"Authorization": f"Basic {basic}"}, redirect_uri=REDIRECT)
    assert r.status_code == 200


def test_exchange_without_redirect_uri_rejected(client):
    code, v = _code(client)
    r = _exchange(client, code, v, client_id="jarvis-app")
    assert r.status_code == 400 and r.json()["error_description"] == "redirect_uri mismatch"


def test_rejected_exchange_burns_the_code(client):
    code, v = _code(client)
    assert _exchange(client, code, v, client_id="other", redirect_uri=REDIRECT).status_code == 400
    r = _exchange(client, code, v, client_id="jarvis-app", redirect_uri=REDIRECT)
    assert r.status_code == 400  # single-use


# --- /oauth/register brake ---------------------------------------------------

def test_register_capped_at_20_per_hour_and_saves_nothing_past_cap(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(oauth, "_clock", lambda: now[0])
    for _ in range(oauth.REGISTRATION_LIMIT):
        assert client.post("/oauth/register", json={"redirect_uris": ["https://x/cb"]}).status_code == 201
    before = dict(oauth._registered_clients)
    r = client.post("/oauth/register", json={"redirect_uris": ["https://x/cb"]})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert oauth._registered_clients == before
    now[0] += oauth.REGISTRATION_WINDOW_SECONDS + 1
    assert client.post("/oauth/register", json={"redirect_uris": ["https://x/cb"]}).status_code == 201


def test_register_brake_does_not_touch_token_endpoint(client, monkeypatch):
    monkeypatch.setattr(oauth, "_clock", lambda: 5.0)
    for _ in range(oauth.REGISTRATION_LIMIT + 1):
        client.post("/oauth/register", json={})
    code, v = _code(client)
    assert _exchange(client, code, v, client_id="jarvis-app", redirect_uri=REDIRECT).status_code == 200


# --- atomic 0600 state writes -----------------------------------------------

def test_save_json_creates_owner_only_file_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "state" / "oauth_tokens.json"
    oauth._save_json(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert [x.name for x in p.parent.iterdir()] == ["oauth_tokens.json"]


def test_save_json_never_creates_world_readable_even_with_permissive_umask(tmp_path, monkeypatch):
    old = os.umask(0)
    try:
        created_modes = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *a, **k):
            created_modes.append(mode)
            return real_open(path, flags, mode, *a, **k)

        monkeypatch.setattr(oauth.os, "open", spy)
        oauth._save_json(tmp_path / "f.json", {})
    finally:
        os.umask(old)
    assert created_modes == [0o600]
    assert stat.S_IMODE(os.stat(tmp_path / "f.json").st_mode) == 0o600


def test_save_json_works_without_fchmod(tmp_path, monkeypatch):
    monkeypatch.delattr(oauth.os, "fchmod")
    oauth._save_json(tmp_path / "f.json", {"k": "v"})
    assert json.loads((tmp_path / "f.json").read_text()) == {"k": "v"}


def test_failed_write_keeps_previous_file_intact(tmp_path, monkeypatch):
    p = tmp_path / "f.json"
    oauth._save_json(p, {"old": True})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(oauth.os, "fsync", boom)
    with pytest.raises(OSError):
        oauth._save_json(p, {"new": True})
    assert json.loads(p.read_text()) == {"old": True}
    assert [x.name for x in tmp_path.iterdir()] == ["f.json"]
