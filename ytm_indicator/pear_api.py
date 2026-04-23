"""Pear Desktop HTTP client with JWT pairing + persistence."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Self

import aiohttp

log = logging.getLogger(__name__)

CLIENT_ID = "ytm-indicator"
BASE_URL = "http://127.0.0.1:26538"
TOKEN_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "ytm-indicator"
    / "token.json"
)
REQUEST_TIMEOUT_S = 5.0


class PearError(Exception):
    """Base for all Pear client errors."""


class PearOfflineError(PearError):
    """Pear not reachable on 127.0.0.1:26538."""


class PearUnauthorizedError(PearError):
    """Pear returned 401; token missing, expired, or revoked."""


class PearPairingRejectedError(PearError):
    """User denied the pairing dialog."""


class PearClient:
    """Async client for Pear's API Server plugin."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._token: str | None = None
        self._load_token()

    @classmethod
    async def create(cls) -> Self:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
        session = aiohttp.ClientSession(base_url=BASE_URL, timeout=timeout)
        return cls(session)

    async def aclose(self) -> None:
        await self._session.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # --- auth ---------------------------------------------------------------

    def _load_token(self) -> None:
        try:
            data = json.loads(TOKEN_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return
        tok = data.get("accessToken")
        if isinstance(tok, str) and tok:
            self._token = tok
            log.debug("loaded persisted token")

    def _save_token(self, token: str) -> None:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(json.dumps({"accessToken": token}))
        TOKEN_PATH.chmod(0o600)
        self._token = token
        log.info("persisted new token to %s", TOKEN_PATH)

    def _drop_token(self) -> None:
        self._token = None
        TOKEN_PATH.unlink(missing_ok=True)

    async def pair(self) -> None:
        """POST /auth/{id} — Pear pops a dialog; blocks until user decides."""
        log.info("requesting pairing from Pear (awaiting user confirmation)")
        try:
            async with self._session.post(
                f"/auth/{CLIENT_ID}",
                timeout=aiohttp.ClientTimeout(total=300),  # user may be slow
            ) as resp:
                if resp.status == 403:
                    raise PearPairingRejectedError("user denied pairing")
                if resp.status >= 400:
                    raise PearError(f"pair failed: HTTP {resp.status}")
                body = await resp.json()
        except aiohttp.ClientConnectionError as e:
            raise PearOfflineError(str(e)) from e
        token = body.get("accessToken")
        if not isinstance(token, str) or not token:
            raise PearError("pair response missing accessToken")
        self._save_token(token)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    # --- request helper -----------------------------------------------------

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        headers = {**self._auth_headers(), **kw.pop("headers", {})}
        try:
            async with self._session.request(method, path, headers=headers, **kw) as resp:
                if resp.status == 401:
                    raise PearUnauthorizedError(path)
                resp.raise_for_status()
                if resp.content_type == "application/json":
                    return await resp.json()
                return await resp.text()
        except aiohttp.ClientConnectionError as e:
            raise PearOfflineError(str(e)) from e
        except TimeoutError as e:
            raise PearOfflineError("timeout") from e

    async def ensure_paired(self) -> None:
        """Make one authenticated call; pair if needed."""
        if self._token is None:
            await self.pair()
            return
        try:
            await self._request("GET", "/api/v1/song")
        except PearUnauthorizedError:
            self._drop_token()
            await self.pair()

    # --- endpoints ----------------------------------------------------------

    async def get_song(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/song")  # type: ignore[no-any-return]

    async def get_like_state(self) -> str:
        data = await self._request("GET", "/api/v1/like-state")
        state = data.get("state")
        return state if isinstance(state, str) else "INDIFFERENT"

    async def toggle_play(self) -> None:
        await self._request("POST", "/api/v1/toggle-play")

    async def next_track(self) -> None:
        await self._request("POST", "/api/v1/next")

    async def previous_track(self) -> None:
        await self._request("POST", "/api/v1/previous")

    async def like(self) -> None:
        await self._request("POST", "/api/v1/like")

    async def dislike(self) -> None:
        await self._request("POST", "/api/v1/dislike")
