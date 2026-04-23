"""StatusNotifierItem D-Bus service."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from dbus_fast.aio import MessageBus
from dbus_fast.constants import PropertyAccess
from dbus_fast.service import ServiceInterface, dbus_property, method, signal

from ytm_indicator.state import State

log = logging.getLogger(__name__)

SNI_IFACE = "org.kde.StatusNotifierItem"
SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
WATCHER_IFACE = "org.kde.StatusNotifierWatcher"

# Fallback icon: YouTube Music mark — red circle, white play triangle.
# Shown when Pear is offline or between tracks. Rendered once at module
# import; album art replaces it as soon as a song is loaded.
_FALLBACK_SIZE = 64
# YouTube Music red (matches the brand, not clipped from any asset).
_YTM_RED = (255, 0, 0, 255)
_WHITE = (255, 255, 255, 255)


def _fallback_pixmap() -> tuple[int, int, bytes]:
    """Build the YouTube Music mark as an ARGB32 pixmap."""
    from PIL import Image, ImageDraw

    # Render at 4x and downsample — PIL's primitives aren't anti-aliased,
    # so super-sampling is the cleanest way to get a smooth circle + edge
    # on the triangle.
    scale = 4
    size = _FALLBACK_SIZE * scale
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Red circle fills the canvas.
    draw.ellipse((0, 0, size - 1, size - 1), fill=_YTM_RED)

    # White play triangle, visually centered. A triangle's geometric centroid
    # sits 1/3 of the way from the base to the opposite vertex, so we offset
    # the left edge by width/3 to put the centroid at the circle's center.
    cx, cy = size / 2, size / 2
    half_height = size * 0.20  # triangle is ~40 % of the diameter tall
    width = size * 0.34
    left = cx - width / 3
    right = left + width
    draw.polygon(
        [(left, cy - half_height), (left, cy + half_height), (right, cy)],
        fill=_WHITE,
    )

    img = img.resize((_FALLBACK_SIZE, _FALLBACK_SIZE), Image.Resampling.LANCZOS)
    return _FALLBACK_SIZE, _FALLBACK_SIZE, _rgba_to_argb(img.tobytes())


def _rgba_to_argb(rgba: bytes) -> bytes:
    """Convert RGBA byte sequence to ARGB32 (network byte order) for SNI."""
    out = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        out[i] = rgba[i + 3]  # A
        out[i + 1] = rgba[i]  # R
        out[i + 2] = rgba[i + 1]  # G
        out[i + 3] = rgba[i + 2]  # B
    return bytes(out)


class SNIInterface(ServiceInterface):
    """Implements org.kde.StatusNotifierItem."""

    def __init__(self, state: State, on_activate: Callable[[], None]) -> None:
        super().__init__(SNI_IFACE)
        self._state = state
        self._on_activate = on_activate
        self._pixmap: tuple[int, int, bytes] = _fallback_pixmap()

    # --- public mutators ----------------------------------------------------

    def set_pixmap_from_png(self, png_path: Path) -> None:
        from PIL import Image

        with Image.open(png_path) as im:
            im = im.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
            self._pixmap = (im.width, im.height, _rgba_to_argb(im.tobytes()))
        self.NewIcon()
        self.NewToolTip()

    def reset_icon(self) -> None:
        self._pixmap = _fallback_pixmap()
        self.NewIcon()
        self.NewToolTip()

    def song_changed(self) -> None:
        self.NewTitle()
        self.NewToolTip()

    def status_changed(self) -> None:
        s = self._state.current
        status = "Passive" if not s.online else "Active"
        self.NewStatus(status)

    # --- SNI methods --------------------------------------------------------

    # Note: @method()-decorated bodies deliberately OMIT `-> None`. With
    # `from __future__ import annotations`, dbus-fast's parse_annotation
    # turns the string "None" back into Python None (not "") and crashes
    # get_signature_tree(None). Omitting the annotation lets it see
    # inspect.Signature.empty and return "" instead.

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        pass

    @method()
    def Activate(self, x: "i", y: "i"):
        # Left-click: open (or focus) the Pear Desktop window.
        try:
            self._on_activate()
        except Exception as e:
            log.warning("activate handler failed: %s", e)

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        pass

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass

    # --- SNI properties -----------------------------------------------------

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "ytm-indicator"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        s = self._state.current
        if not s.has_song:
            return "YouTube Music"
        return f"{s.title} — {s.artist}"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Passive" if not self._state.current.online else "Active"

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "u":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        w, h, data = self._pixmap
        return [[w, h, data]]

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionMovieName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        s = self._state.current
        if not s.online:
            return ["", [], "YouTube Music", "(Pear offline)"]
        if not s.has_song:
            return ["", [], "YouTube Music", "Nothing playing"]
        desc = s.album or ""
        return ["", [], f"{s.title} — {s.artist}", desc]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return MENU_PATH

    # --- SNI signals --------------------------------------------------------

    @signal()
    def NewTitle(self) -> None:
        pass

    @signal()
    def NewIcon(self) -> None:
        pass

    @signal()
    def NewAttentionIcon(self) -> None:
        pass

    @signal()
    def NewOverlayIcon(self) -> None:
        pass

    @signal()
    def NewToolTip(self) -> None:
        pass

    @signal()
    def NewStatus(self, status: "s") -> "s":
        return status


async def register_with_watcher(bus: MessageBus, bus_name: str) -> None:
    """Tell the SNI host our bus name so it picks up the item."""
    introspect = await bus.introspect(WATCHER_NAME, WATCHER_PATH)
    proxy = bus.get_proxy_object(WATCHER_NAME, WATCHER_PATH, introspect)
    iface = proxy.get_interface(WATCHER_IFACE)
    await iface.call_register_status_notifier_item(bus_name)
    log.info("registered with %s as %s", WATCHER_NAME, bus_name)


async def watch_and_reregister(bus: MessageBus, bus_name: str) -> None:
    """Re-register with the watcher whenever its owner changes.

    The watcher's state (the list of registered items) lives in the
    watcher process's memory. When Patina — or any host — restarts,
    that list is gone and our old registration goes with it. Subscribe
    to NameOwnerChanged so we can re-register the moment a new owner
    appears.
    """
    introspect = await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
    dbus_proxy = bus.get_proxy_object(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        introspect,
    )
    dbus_iface = dbus_proxy.get_interface("org.freedesktop.DBus")

    loop = asyncio.get_running_loop()
    # Holding strong refs so the GC doesn't cancel in-flight re-registers
    # (ruff RUF006) and doesn't drop the signal callback itself.
    bg_tasks: set[asyncio.Task[None]] = set()

    def on_name_owner_changed(name: str, old_owner: str, new_owner: str) -> None:
        if name != WATCHER_NAME or not new_owner:
            return
        log.info("watcher owner changed (%s → %s); re-registering", old_owner, new_owner)
        task = loop.create_task(_safe_register(bus, bus_name))
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

    dbus_iface.on_name_owner_changed(on_name_owner_changed)


async def _safe_register(bus: MessageBus, bus_name: str) -> None:
    try:
        await register_with_watcher(bus, bus_name)
    except Exception as e:
        log.warning("re-registration failed: %s", e)
