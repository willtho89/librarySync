import base64
import hashlib
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from librarysync import config
from librarysync.connectors.services import trakt as trakt_module
from librarysync.connectors.services.trakt import TraktClient, generate_pkce_pair
from librarysync.core import watch_pipeline
from librarysync.core.watch_pipeline import TraktSyncStrategy

TOKEN_RESPONSE = {"access_token": "access", "refresh_token": "refresh", "expires_in": 604800}
REDIRECT_URI = "http://localhost:8000/api/integrations/trakt/callback"


def _capture_post(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def fake_post_json(self, url, payload):
        sent.append(payload)
        return TOKEN_RESPONSE

    monkeypatch.setattr(trakt_module.TraktClient, "_post_json", fake_post_json)
    return sent


def test_generate_pkce_pair_uses_s256():
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert generate_pkce_pair()[0] != verifier


def test_authorize_url_includes_challenge_only_when_given():
    client = TraktClient(client_id="cid")
    with_pkce = parse_qs(urlparse(client.build_authorize_url(REDIRECT_URI, "st", "chal")).query)
    assert with_pkce["code_challenge"] == ["chal"]
    assert with_pkce["code_challenge_method"] == ["S256"]
    without = parse_qs(urlparse(client.build_authorize_url(REDIRECT_URI, "st")).query)
    assert "code_challenge" not in without


@pytest.mark.asyncio
async def test_exchange_code_pkce_without_secret(monkeypatch):
    sent = _capture_post(monkeypatch)
    token = await TraktClient(client_id="cid").exchange_code("code", REDIRECT_URI, "verifier")
    assert token.access_token == "access"
    assert sent[0]["code_verifier"] == "verifier"
    assert "client_secret" not in sent[0]


@pytest.mark.asyncio
async def test_exchange_code_with_secret_keeps_legacy_payload(monkeypatch):
    sent = _capture_post(monkeypatch)
    await TraktClient(client_id="cid", client_secret="sec").exchange_code("code", REDIRECT_URI)
    assert sent[0]["client_secret"] == "sec"
    assert "code_verifier" not in sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [None, ""])
async def test_refresh_omits_missing_secret(monkeypatch, secret):
    sent = _capture_post(monkeypatch)
    await TraktClient(client_id="cid", client_secret=secret).refresh_access_token("old")
    assert sent[0]["refresh_token"] == "old"
    assert "client_secret" not in sent[0]


@pytest.mark.parametrize(("client_id", "ready"), [("cid", True), (None, False)])
def test_trakt_sync_ready_without_secret(monkeypatch, client_id, ready):
    updated = replace(config.settings, trakt_client_id=client_id, trakt_client_secret=None)
    monkeypatch.setattr(watch_pipeline, "settings", updated)
    assert TraktSyncStrategy()._settings_ready() is ready
