"""Entry point — wires state, Pear client, SNI service, DBusMenu, poller."""

from __future__ import annotations

import asyncio
import logging
import os
import signal as sig
import subprocess
import sys

import aiohttp
from dbus_fast import BusType
from dbus_fast.aio import MessageBus

from ytm_indicator.art import fetch_art
from ytm_indicator.menu import Actions, DBusMenuInterface
from ytm_indicator.pear_api import (
    PearClient,
    PearError,
    PearOfflineError,
    PearPairingRejectedError,
)
from ytm_indicator.sni import (
    MENU_PATH,
    SNI_PATH,
    SNIInterface,
    register_with_watcher,
    watch_and_reregister,
)
from ytm_indicator.state import SongState, State

log = logging.getLogger("ytm_indicator")

POLL_INTERVAL_S = 3.0
OFFLINE_BACKOFF_S = 10.0
PEAR_LAUNCH_CMD = ["pear-desktop"]


def _open_pear() -> None:
    """Launch Pear Desktop, or focus the existing window if it's already up.

    Electron's built-in single-instance lock routes a second invocation to
    the already-running process (which focuses its window) so running the
    same command covers both cases. stdin/stdout/stderr are detached and
    the child runs in a new session so it outlives this indicator.
    """
    try:
        subprocess.Popen(
            PEAR_LAUNCH_CMD,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        log.info("activate: spawned %s", PEAR_LAUNCH_CMD[0])
    except FileNotFoundError:
        log.error("activate: %s not on PATH", PEAR_LAUNCH_CMD[0])
    except OSError as e:
        log.warning("activate: failed to spawn %s: %s", PEAR_LAUNCH_CMD[0], e)


def _parse_song(payload: dict[str, object], like: str) -> SongState:
    if not payload or not payload.get("videoId"):
        return SongState(online=True)
    return SongState(
        title=str(payload.get("title", "")),
        artist=str(payload.get("artist", "")),
        album=payload.get("album") if isinstance(payload.get("album"), str) else None,
        video_id=str(payload.get("videoId", "")),
        image_src=payload.get("imageSrc") if isinstance(payload.get("imageSrc"), str) else None,
        is_paused=bool(payload.get("isPaused", True)),
        duration_s=float(payload.get("songDuration", 0) or 0),
        elapsed_s=float(payload.get("elapsedSeconds", 0) or 0),
        like=like,
        online=True,
    )


class Indicator:
    """Owns the bus connection, services, and the main polling loop."""

    def __init__(self) -> None:
        self.state = State()
        self.bus: MessageBus | None = None
        self.pear: PearClient | None = None
        self.art_session: aiohttp.ClientSession | None = None
        self.sni: SNIInterface | None = None
        self.menu: DBusMenuInterface | None = None
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

    async def start(self) -> None:
        self.pear = await PearClient.create()
        self.art_session = aiohttp.ClientSession()
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()

        actions = Actions(
            toggle_play=self._safe_call(self.pear.toggle_play),
            next_track=self._safe_call(self.pear.next_track),
            previous_track=self._safe_call(self.pear.previous_track),
            like=self._safe_call(self.pear.like),
            dislike=self._safe_call(self.pear.dislike),
        )

        self.sni = SNIInterface(self.state, on_activate=_open_pear)
        self.menu = DBusMenuInterface(self.state, actions)
        self.bus.export(SNI_PATH, self.sni)
        self.bus.export(MENU_PATH, self.menu)

        await self.bus.request_name(self._bus_name)
        log.info("bus name acquired: %s", self._bus_name)

        # Subscribe to NameOwnerChanged BEFORE the first register attempt so
        # we catch the case where the watcher comes online between the two
        # calls. If registration fails now (no watcher yet), the subscription
        # will fire as soon as one shows up.
        await watch_and_reregister(self.bus, self._bus_name)
        try:
            await register_with_watcher(self.bus, self._bus_name)
        except Exception as e:
            log.warning("initial watcher registration failed: %s (will retry on owner change)", e)

    def _safe_call(self, coro_fn):  # type: ignore[no-untyped-def]
        async def wrapped() -> None:
            try:
                await coro_fn()
                # Refresh state quickly after an action.
                await self._poll_once()
            except PearError as e:
                log.warning("action failed: %s", e)

        return wrapped

    async def run(self) -> None:
        while True:
            interval = await self._poll_once()
            await asyncio.sleep(interval)

    async def _poll_once(self) -> float:
        """One poll cycle. Returns the interval to wait before the next poll."""
        assert self.pear and self.sni and self.menu and self.art_session
        prev = self.state.current
        try:
            try:
                await self.pear.ensure_paired()
            except PearPairingRejectedError:
                log.error("Pear rejected pairing; cannot continue")
                self.state.patch(online=False)
                self._push_updates(prev)
                return OFFLINE_BACKOFF_S
            payload = await self.pear.get_song()
            like = await self.pear.get_like_state()
        except PearOfflineError as e:
            if self.state.current.online:
                log.info("Pear offline: %s", e)
            self.state.update(SongState(online=False))
            self._push_updates(prev)
            return OFFLINE_BACKOFF_S
        except PearError as e:
            log.warning("Pear error: %s", e)
            return OFFLINE_BACKOFF_S

        new = _parse_song(payload if isinstance(payload, dict) else {}, like)
        changed = self.state.update(new)
        if not changed:
            return POLL_INTERVAL_S

        self._push_updates(prev)
        if new.has_song and new.video_id != prev.video_id:
            await self._refresh_art(new)
        elif not new.has_song:
            self.sni.reset_icon()
        return POLL_INTERVAL_S

    async def _refresh_art(self, song: SongState) -> None:
        assert self.art_session and self.sni
        path = await fetch_art(self.art_session, song.video_id, song.image_src)
        if path is None:
            self.sni.reset_icon()
            return
        try:
            self.sni.set_pixmap_from_png(path)
        except Exception as e:
            log.warning("failed to load art %s: %s", path, e)
            self.sni.reset_icon()

    # Tray-visible SongState fields — only changes to these warrant an SNI
    # signal. elapsed_s/duration_s/is_paused/like are NOT here: they tick
    # (or change silently) without altering Title/IconPixmap/ToolTip/Status,
    # and each emitted signal makes every SNI host GetAll the full property
    # set — including a 16 KB IconPixmap — which PyGObject hosts like Patina
    # unpack in pure-Python GVariant code at ~90% of a core.
    _TRAY_TITLE_FIELDS = ("online", "video_id", "title", "artist", "album")

    def _push_updates(self, prev: SongState) -> None:
        assert self.sni and self.menu
        cur = self.state.current
        if any(getattr(prev, f) != getattr(cur, f) for f in self._TRAY_TITLE_FIELDS):
            self.sni.song_changed()
        if prev.online != cur.online:
            self.sni.status_changed()
        self.menu.bump()

    async def aclose(self) -> None:
        if self.pear:
            await self.pear.aclose()
        if self.art_session:
            await self.art_session.close()
        if self.bus:
            self.bus.disconnect()


async def _run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ind = Indicator()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for s in (sig.SIGINT, sig.SIGTERM):
        loop.add_signal_handler(s, stop.set)

    await ind.start()
    runner = asyncio.create_task(ind.run())
    try:
        await stop.wait()
    finally:
        runner.cancel()
        await ind.aclose()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        sys.exit(0)
