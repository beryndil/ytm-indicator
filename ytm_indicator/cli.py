"""Entry point — wires state, Pear client, SNI service, popover, poller."""

from __future__ import annotations

import asyncio
import logging
import os
import signal as sig
import subprocess
import sys

import aiohttp
from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

from ytm_indicator import config as _config
from ytm_indicator.art import evict_old_cache, fetch_art
from ytm_indicator.pear_api import (
    PearClient,
    PearError,
    PearOfflineError,
    PearPairingRejectedError,
)
from ytm_indicator.sni import (
    SNI_PATH,
    SNIInterface,
    register_with_watcher,
    watch_and_reregister,
)
from ytm_indicator.state import SongState, State

log = logging.getLogger("ytm_indicator")

# Config is loaded once at import time so constants are available module-wide.
# Individual instances can override via the Indicator constructor.
_cfg = _config.load()

POLL_INTERVAL_S = _cfg.poll_interval_s
OFFLINE_BACKOFF_S = _cfg.offline_backoff_s
PEAR_LAUNCH_CMD = ["pear-desktop"]
# Popover is a separate GTK4 process — keeps GTK's main loop out of our
# asyncio loop, and a rendering bug there can't take the tray icon down.
POPOVER_CMD = [sys.executable, "-m", "ytm_indicator.popover"]
# gtk4-layer-shell has to interpose before libwayland-client, so it must be
# LD_PRELOADed. Hand the child the preload directly so there's no re-exec.
LAYER_SHELL_LIB = "/usr/lib/libgtk4-layer-shell.so"


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


def _spawn_popover(song: SongState) -> None:
    """Launch the GTK4 now-playing popover as a detached subprocess.

    Current song state is passed on the command line so the popover can
    render immediately without a round-trip to Pear's API.
    """
    argv = [
        *POPOVER_CMD,
        "--title",
        song.title,
        "--artist",
        song.artist,
        "--album",
        song.album or "",
        "--video-id",
        song.video_id,
        "--paused",
        "true" if song.is_paused else "false",
        "--elapsed",
        f"{song.elapsed_s:.0f}",
        "--duration",
        f"{song.duration_s:.0f}",
        "--like",
        song.like,
    ]
    env = os.environ.copy()
    existing = env.get("LD_PRELOAD", "")
    env["LD_PRELOAD"] = f"{LAYER_SHELL_LIB}:{existing}" if existing else LAYER_SHELL_LIB
    env["_YTM_LAYER_SHELL_PRELOADED"] = "1"
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
        log.info("context-menu: spawned popover")
    except OSError as e:
        log.warning("context-menu: popover spawn failed: %s", e)


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
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        # MPRIS fast-path: set when we have an active listener so _poll_once
        # can be woken early from the wait.
        self._mpris_trigger: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        self.pear = await PearClient.create(
            host=_cfg.pear_host,
            port=_cfg.pear_port,
        )
        self.art_session = aiohttp.ClientSession()
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()

        self.sni = SNIInterface(
            self.state,
            on_activate=_open_pear,
            on_context_menu=lambda _x, _y: _spawn_popover(self.state.current),
        )
        self.bus.export(SNI_PATH, self.sni)

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

        await self._setup_mpris_listener()

    async def run(self) -> None:
        while True:
            interval = await self._poll_once()
            # Wait for the timer OR an early MPRIS wakeup, whichever fires first.
            try:
                await asyncio.wait_for(
                    self._wait_for_mpris_trigger(),
                    timeout=interval,
                )
                log.debug("MPRIS fast-path triggered early poll")
            except TimeoutError:
                pass  # normal timer expiry

    async def _wait_for_mpris_trigger(self) -> None:
        """Wait until the MPRIS listener fires an early-poll signal."""
        await self._mpris_trigger.wait()
        self._mpris_trigger.clear()

    async def _poll_once(self) -> float:
        """One poll cycle. Returns the interval to wait before the next poll."""
        assert self.pear and self.sni and self.art_session
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

    async def _setup_mpris_listener(self) -> None:
        """Subscribe to MPRIS PropertiesChanged as a supplemental fast-path.

        Tracks Chromium-family bus names via NameOwnerChanged and subscribes
        to org.freedesktop.DBus.Properties.PropertiesChanged on the
        org.mpris.MediaPlayer2.Player interface. On relevant changes
        (PlaybackStatus or Metadata) the MPRIS trigger event is set so the
        poll loop wakes early and fetches fresh state from Pear.

        Wrapped in try/except — any D-Bus failure downgrades to poll-only
        mode transparently.
        """
        try:
            assert self.bus
            await _subscribe_mpris_changes(self.bus, self._mpris_trigger)
            log.info("MPRIS fast-path listener active")
        except Exception as e:
            log.debug("MPRIS listener setup failed, poll-only mode: %s", e)

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
        assert self.sni
        cur = self.state.current
        if any(getattr(prev, f) != getattr(cur, f) for f in self._TRAY_TITLE_FIELDS):
            self.sni.song_changed()
        if prev.online != cur.online:
            self.sni.status_changed()

    async def aclose(self) -> None:
        if self.pear:
            await self.pear.aclose()
        if self.art_session:
            await self.art_session.close()
        if self.bus:
            self.bus.disconnect()


_MPRIS_PATH = "/org/mpris/MediaPlayer2"
_PROPS_IFACE = "org.freedesktop.DBus.Properties"
_PROPS_CHANGED = "PropertiesChanged"
_MPRIS_PREFIX = "org.mpris.MediaPlayer2."
# Chromium-family name fragments — Pear Desktop is Electron-based.
_CHROMIUM_FRAGS = ("chromium", "chrome", "pear", "electron")


async def _subscribe_mpris_changes(bus: MessageBus, trigger: asyncio.Event) -> None:
    """Wire MPRIS PropertiesChanged to the fast-path trigger.

    Subscribes to org.freedesktop.DBus.Properties.PropertiesChanged on the
    org.mpris.MediaPlayer2.Player interface. Only signals from known
    Chromium-family MPRIS players (tracked via NameOwnerChanged) are
    forwarded to the trigger; if none are known yet, any MPRIS player is
    accepted so we don't miss early signals from Pear.
    """
    introspect = await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
    dbus_proxy = bus.get_proxy_object("org.freedesktop.DBus", "/org/freedesktop/DBus", introspect)
    dbus_iface = dbus_proxy.get_interface("org.freedesktop.DBus")

    chromium_owners: set[str] = set()

    try:
        names: list[str] = await dbus_iface.call_list_names()
        for name in names:
            if not name.startswith(_MPRIS_PREFIX):
                continue
            lower = name[len(_MPRIS_PREFIX) :].lower()
            if any(frag in lower for frag in _CHROMIUM_FRAGS):
                try:
                    owner: str = await dbus_iface.call_get_name_owner(name)
                    chromium_owners.add(owner)
                    log.debug("MPRIS: existing player %s owner=%s", name, owner)
                except Exception:
                    pass
    except Exception as e:
        log.debug("MPRIS: could not enumerate existing names: %s", e)

    def _on_owner_changed(name: str, old_owner: str, new_owner: str) -> None:
        if not name.startswith(_MPRIS_PREFIX):
            return
        lower = name[len(_MPRIS_PREFIX) :].lower()
        if not any(frag in lower for frag in _CHROMIUM_FRAGS):
            return
        if old_owner:
            chromium_owners.discard(old_owner)
        if new_owner:
            chromium_owners.add(new_owner)
            log.debug("MPRIS: player appeared %s owner=%s", name, new_owner)
        else:
            log.debug("MPRIS: player left %s", name)

    dbus_iface.on_name_owner_changed(_on_owner_changed)

    try:
        await dbus_iface.call_add_match(
            "type='signal',"
            "interface='org.freedesktop.DBus.Properties',"
            "member='PropertiesChanged',"
            "path='/org/mpris/MediaPlayer2',"
            "arg0='org.mpris.MediaPlayer2.Player'"
        )
    except Exception as e:
        log.debug("MPRIS: AddMatch failed (may still work on session bus): %s", e)

    def _on_message(msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.interface != _PROPS_IFACE or msg.member != _PROPS_CHANGED:
            return
        if msg.path != _MPRIS_PATH:
            return
        if chromium_owners and msg.sender not in chromium_owners:
            return
        try:
            changed: dict = msg.body[1] if len(msg.body) > 1 else {}
            if "PlaybackStatus" in changed or "Metadata" in changed:
                log.debug("MPRIS fast-path: %s changed", list(changed.keys()))
                trigger.set()
        except Exception as e:
            log.debug("MPRIS: bad PropertiesChanged body: %s", e)

    bus.add_message_handler(_on_message)


async def _run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    evict_old_cache()
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
