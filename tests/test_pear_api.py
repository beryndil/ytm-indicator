"""Tests for ytm_indicator.pear_api."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import ytm_indicator.pear_api as pear_api_mod
from ytm_indicator.pear_api import (
    PearClient,
    PearError,
    PearOfflineError,
    PearPairingRejectedError,
    PearUnauthorizedError,
)

# ── mock helpers ─────────────────────────────────────────────────────────────


def _resp(
    status: int = 200,
    json_data: object = None,
    content_type: str = "application/json",
) -> MagicMock:
    r = MagicMock()
    r.status = status
    r.content_type = content_type
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value="ok")
    r.read = AsyncMock(return_value=b"")
    r.raise_for_status = MagicMock()
    return r


def _cm(resp: MagicMock) -> MagicMock:
    """Wrap *resp* in an async context-manager mock."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _session() -> MagicMock:
    s = MagicMock()
    s.close = AsyncMock()
    return s


def _client(
    token: str | None = None,
    session: MagicMock | None = None,
) -> PearClient:
    """PearClient backed by *session* (or a fresh one); token set if given."""
    c = PearClient(session if session is not None else _session())
    if token is not None:
        c._token = token
    return c


# ── autouse: redirect TOKEN_PATH to a safe temp location ─────────────────────


@pytest.fixture(autouse=True)
def _patch_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pear_api_mod, "TOKEN_PATH", tmp_path / "token.json")


# ── token persistence ────────────────────────────────────────────────────────


def test_load_token_from_disk() -> None:
    pear_api_mod.TOKEN_PATH.write_text(json.dumps({"accessToken": "saved_tok"}))
    c = _client()
    assert c._token == "saved_tok"


def test_load_token_missing_file() -> None:
    assert _client()._token is None


def test_load_token_bad_json() -> None:
    pear_api_mod.TOKEN_PATH.write_text("not {{ valid json")
    assert _client()._token is None


def test_load_token_empty_string() -> None:
    pear_api_mod.TOKEN_PATH.write_text(json.dumps({"accessToken": ""}))
    assert _client()._token is None


def test_save_token() -> None:
    c = _client()
    c._save_token("brand_new")
    assert c._token == "brand_new"
    assert json.loads(pear_api_mod.TOKEN_PATH.read_text())["accessToken"] == "brand_new"


def test_drop_token() -> None:
    pear_api_mod.TOKEN_PATH.write_text(json.dumps({"accessToken": "x"}))
    c = _client(token="x")
    c._drop_token()
    assert c._token is None
    assert not pear_api_mod.TOKEN_PATH.exists()


def test_auth_headers_with_token() -> None:
    c = _client(token="mytoken")
    assert c._auth_headers() == {"Authorization": "Bearer mytoken"}


def test_auth_headers_no_token() -> None:
    c = _client()
    assert c._auth_headers() == {}


# ── pairing ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pair_success() -> None:
    sess = _session()
    sess.post = MagicMock(return_value=_cm(_resp(200, {"accessToken": "fresh"})))
    c = PearClient(sess)
    await c.pair()
    assert c._token == "fresh"


@pytest.mark.asyncio
async def test_pair_rejected_403() -> None:
    sess = _session()
    sess.post = MagicMock(return_value=_cm(_resp(403)))
    c = PearClient(sess)
    with pytest.raises(PearPairingRejectedError):
        await c.pair()


@pytest.mark.asyncio
async def test_pair_server_error_raises_pear_error() -> None:
    sess = _session()
    sess.post = MagicMock(return_value=_cm(_resp(500)))
    c = PearClient(sess)
    with pytest.raises(PearError):
        await c.pair()


@pytest.mark.asyncio
async def test_pair_offline_raises_offline_error() -> None:
    sess = _session()
    sess.post = MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    c = PearClient(sess)
    with pytest.raises(PearOfflineError):
        await c.pair()


# ── _request ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_returns_json() -> None:
    data = {"title": "Song", "artist": "Artist"}
    sess = _session()
    sess.request = MagicMock(return_value=_cm(_resp(200, data)))
    assert (await _client(token="tok", session=sess).get_song())["title"] == "Song"


@pytest.mark.asyncio
async def test_request_returns_text_for_non_json() -> None:
    r = _resp(200, content_type="text/plain")
    r.text = AsyncMock(return_value="pong")
    sess = _session()
    sess.request = MagicMock(return_value=_cm(r))
    # toggle_play returns text; just verify no exception
    await _client(token="tok", session=sess).toggle_play()


@pytest.mark.asyncio
async def test_request_401_raises_unauthorized() -> None:
    sess = _session()
    sess.request = MagicMock(return_value=_cm(_resp(401)))
    with pytest.raises(PearUnauthorizedError):
        await _client(token="stale", session=sess).get_song()


@pytest.mark.asyncio
async def test_request_connection_error_raises_offline() -> None:
    error_cm = MagicMock()
    error_cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientConnectionError("no route"))
    error_cm.__aexit__ = AsyncMock(return_value=False)
    sess = _session()
    sess.request = MagicMock(return_value=error_cm)
    with pytest.raises(PearOfflineError):
        await _client(token="tok", session=sess).get_song()


@pytest.mark.asyncio
async def test_request_timeout_raises_offline() -> None:
    timeout_cm = MagicMock()
    timeout_cm.__aenter__ = AsyncMock(side_effect=TimeoutError("timed out"))
    timeout_cm.__aexit__ = AsyncMock(return_value=False)
    sess = _session()
    sess.request = MagicMock(return_value=timeout_cm)
    with pytest.raises(PearOfflineError):
        await _client(token="tok", session=sess).get_song()


# ── ensure_paired ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_paired_no_token_triggers_pair() -> None:
    sess = _session()
    sess.post = MagicMock(return_value=_cm(_resp(200, {"accessToken": "new_tok"})))
    c = PearClient(sess)
    assert c._token is None
    await c.ensure_paired()
    assert c._token == "new_tok"


@pytest.mark.asyncio
async def test_ensure_paired_401_drops_and_re_pairs() -> None:
    sess = _session()
    sess.request = MagicMock(return_value=_cm(_resp(401)))
    sess.post = MagicMock(return_value=_cm(_resp(200, {"accessToken": "refreshed"})))
    c = _client(token="stale", session=sess)
    await c.ensure_paired()
    assert c._token == "refreshed"


@pytest.mark.asyncio
async def test_ensure_paired_already_valid_does_not_pair() -> None:
    song_data = {"title": "T", "artist": "A"}
    sess = _session()
    sess.request = MagicMock(return_value=_cm(_resp(200, song_data)))
    c = _client(token="valid", session=sess)
    await c.ensure_paired()
    sess.post.assert_not_called()


# ── endpoint smoke ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_like_state_returns_string() -> None:
    sess = _session()
    sess.request = MagicMock(return_value=_cm(_resp(200, {"state": "LIKE"})))
    assert await _client(token="tok", session=sess).get_like_state() == "LIKE"


@pytest.mark.asyncio
async def test_get_like_state_missing_returns_indifferent() -> None:
    sess = _session()
    sess.request = MagicMock(return_value=_cm(_resp(200, {})))
    assert await _client(token="tok", session=sess).get_like_state() == "INDIFFERENT"


@pytest.mark.asyncio
async def test_next_track() -> None:
    sess = _session()
    sess.request = MagicMock(return_value=_cm(_resp(200, content_type="text/plain")))
    await _client(token="tok", session=sess)._request("POST", "/api/v1/next")


# ── create / aclose ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_aclose() -> None:
    c = await PearClient.create()
    await c.aclose()


@pytest.mark.asyncio
async def test_async_context_manager() -> None:
    async with await PearClient.create() as c:
        assert c._token is None
