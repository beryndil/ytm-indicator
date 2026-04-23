"""StatusNotifierItem D-Bus service."""

from __future__ import annotations

import asyncio
import logging
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

# 22x22 fallback PNG embedded — gray circle, shown when no art yet.
# Generated once at module import; cheap.
_FALLBACK_SIZE = 22


def _fallback_pixmap() -> tuple[int, int, bytes]:
    """Build a small gray placeholder ARGB32 pixmap."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (_FALLBACK_SIZE, _FALLBACK_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((1, 1, _FALLBACK_SIZE - 2, _FALLBACK_SIZE - 2), fill=(90, 90, 90, 255))
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

    def __init__(self, state: State) -> None:
        super().__init__(SNI_IFACE)
        self._state = state
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
        # Left-click = no-op for v0.1; transport via right-click menu.
        pass

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

    def on_name_owner_changed(name: str, old_owner: str, new_owner: str) -> None:
        if name != WATCHER_NAME or not new_owner:
            return
        log.info("watcher owner changed (%s → %s); re-registering", old_owner, new_owner)
        loop.create_task(_safe_register(bus, bus_name))

    dbus_iface.on_name_owner_changed(on_name_owner_changed)


async def _safe_register(bus: MessageBus, bus_name: str) -> None:
    try:
        await register_with_watcher(bus, bus_name)
    except Exception as e:
        log.warning("re-registration failed: %s", e)
