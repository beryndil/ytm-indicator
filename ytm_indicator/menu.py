"""DBusMenu (com.canonical.dbusmenu) service for the tray icon's right-click menu."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from dbus_fast.constants import PropertyAccess
from dbus_fast.service import ServiceInterface, dbus_property, method, signal
from dbus_fast.signature import Variant

from ytm_indicator.state import State

log = logging.getLogger(__name__)

DBUSMENU_IFACE = "com.canonical.dbusmenu"

# Stable item IDs (root is 0; children start at 1).
ID_ROOT = 0
ID_PLAY_PAUSE = 1
ID_NEXT = 2
ID_PREV = 3
ID_SEP1 = 4
ID_LIKE = 5
ID_DISLIKE = 6
ID_SEP2 = 7
ID_NOW_PLAYING = 8
ID_ARTIST = 9
ID_ALBUM = 10

Action = Callable[[], Awaitable[None]]


@dataclass
class Actions:
    toggle_play: Action
    next_track: Action
    previous_track: Action
    like: Action
    dislike: Action


def _v(sig: str, value: object) -> Variant:
    return Variant(sig, value)


@dataclass
class _ItemProps:
    props: dict[str, Variant] = field(default_factory=dict)


class DBusMenuInterface(ServiceInterface):
    """Minimum-viable com.canonical.dbusmenu implementation."""

    def __init__(self, state: State, actions: Actions) -> None:
        super().__init__(DBUSMENU_IFACE)
        self._state = state
        self._actions = actions
        self._revision = 1

    # --- layout builder -----------------------------------------------------

    def _item_props(self, item_id: int) -> dict[str, Variant]:
        s = self._state.current
        if item_id == ID_ROOT:
            return {"children-display": _v("s", "submenu")}
        if item_id == ID_PLAY_PAUSE:
            label = "Play" if s.is_paused or not s.has_song else "Pause"
            return {
                "label": _v("s", label),
                "enabled": _v("b", s.online),
            }
        if item_id == ID_NEXT:
            return {"label": _v("s", "Next"), "enabled": _v("b", s.online and s.has_song)}
        if item_id == ID_PREV:
            return {
                "label": _v("s", "Previous"),
                "enabled": _v("b", s.online and s.has_song),
            }
        if item_id in (ID_SEP1, ID_SEP2):
            return {"type": _v("s", "separator")}
        if item_id == ID_LIKE:
            return {
                "label": _v("s", "Like"),
                "enabled": _v("b", s.online and s.has_song),
                "toggle-type": _v("s", "checkmark"),
                "toggle-state": _v("i", 1 if s.like == "LIKE" else 0),
            }
        if item_id == ID_DISLIKE:
            return {
                "label": _v("s", "Dislike"),
                "enabled": _v("b", s.online and s.has_song),
                "toggle-type": _v("s", "checkmark"),
                "toggle-state": _v("i", 1 if s.like == "DISLIKE" else 0),
            }
        if item_id == ID_NOW_PLAYING:
            text = s.title if s.has_song else ("(offline)" if not s.online else "(nothing playing)")
            return {
                "label": _v("s", text),
                "enabled": _v("b", False),
                "disposition": _v("s", "informative"),
            }
        if item_id == ID_ARTIST:
            return {
                "label": _v("s", s.artist),
                "enabled": _v("b", False),
                "visible": _v("b", bool(s.artist)),
                "disposition": _v("s", "informative"),
            }
        if item_id == ID_ALBUM:
            return {
                "label": _v("s", s.album or ""),
                "enabled": _v("b", False),
                "visible": _v("b", bool(s.album)),
                "disposition": _v("s", "informative"),
            }
        return {}

    def _child_ids(self, parent: int) -> list[int]:
        if parent == ID_ROOT:
            return [
                ID_PLAY_PAUSE,
                ID_NEXT,
                ID_PREV,
                ID_SEP1,
                ID_LIKE,
                ID_DISLIKE,
                ID_SEP2,
                ID_NOW_PLAYING,
                ID_ARTIST,
                ID_ALBUM,
            ]
        return []

    def _build_layout(
        self,
        item_id: int,
        depth: int,
    ) -> tuple[int, dict[str, Variant], list[Variant]]:
        props = self._item_props(item_id)
        if depth == 0:
            return (item_id, props, [])
        children = [
            _v("(ia{sv}av)", self._build_layout(cid, depth - 1)) for cid in self._child_ids(item_id)
        ]
        return (item_id, props, children)

    # --- DBusMenu properties ------------------------------------------------

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []

    # --- DBusMenu methods ---------------------------------------------------

    @method()
    def GetLayout(
        self,
        parent_id: "i",
        recursion_depth: "i",
        property_names: "as",
    ) -> "u(ia{sv}av)":
        depth = 10 if recursion_depth < 0 else recursion_depth
        layout = self._build_layout(parent_id, depth)
        return [self._revision, layout]

    @method()
    def GetGroupProperties(
        self,
        ids: "ai",
        property_names: "as",
    ) -> "a(ia{sv})":
        target = ids or [ID_ROOT, *self._child_ids(ID_ROOT)]
        return [[i, self._item_props(i)] for i in target]

    @method()
    def GetProperty(self, id: "i", name: "s") -> "v":
        props = self._item_props(id)
        return props.get(name, _v("s", ""))

    # Note: `-> None` is deliberately omitted on void @method() bodies. With
    # `from __future__ import annotations`, dbus-fast's parse_annotation
    # turns the string "None" back into Python None (not "") and crashes
    # _Method.__init__. Omitting the annotation keeps dbus-fast happy.

    @method()
    async def Event(
        self,
        id: "i",
        event_id: "s",
        data: "v",
        timestamp: "u",
    ):
        if event_id != "clicked":
            return
        log.info("menu event: id=%d clicked", id)
        try:
            if id == ID_PLAY_PAUSE:
                await self._actions.toggle_play()
            elif id == ID_NEXT:
                await self._actions.next_track()
            elif id == ID_PREV:
                await self._actions.previous_track()
            elif id == ID_LIKE:
                await self._actions.like()
            elif id == ID_DISLIKE:
                await self._actions.dislike()
        except Exception as e:
            log.warning("action for id=%d failed: %s", id, e)

    @method()
    def EventGroup(
        self,
        events: "a(isvu)",
    ) -> "ai":
        # Fire-and-forget; proper handling would dispatch each. Return empty = no failures.
        return []

    @method()
    def AboutToShow(self, id: "i") -> "b":
        return False  # No per-open update needed; we push via LayoutUpdated.

    @method()
    def AboutToShowGroup(self, ids: "ai") -> "aiai":
        return [[], []]

    # --- signals ------------------------------------------------------------

    @signal()
    def ItemsPropertiesUpdated(
        self,
        updated_props: "a(ia{sv})",
        removed_props: "a(ias)",
    ) -> "a(ia{sv})a(ias)":
        return [updated_props, removed_props]

    @signal()
    def LayoutUpdated(
        self,
        revision: "u",
        parent: "i",
    ) -> "ui":
        return [revision, parent]

    @signal()
    def ItemActivationRequested(
        self,
        id: "i",
        timestamp: "u",
    ) -> "iu":
        return [id, timestamp]

    # --- public mutator -----------------------------------------------------

    def bump(self) -> None:
        """Tell clients the layout needs re-fetching."""
        self._revision += 1
        self.LayoutUpdated(self._revision, ID_ROOT)
