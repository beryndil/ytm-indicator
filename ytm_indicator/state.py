"""Shared state between the API poller, SNI service, and DBusMenu."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace


@dataclass(frozen=True, slots=True)
class SongState:
    """Snapshot of what's playing, flattened from Pear's /api/v1/song."""

    title: str = ""
    artist: str = ""
    album: str | None = None
    video_id: str = ""
    image_src: str | None = None
    is_paused: bool = True
    duration_s: float = 0.0
    elapsed_s: float = 0.0
    like: str = "INDIFFERENT"  # DISLIKE | INDIFFERENT | LIKE
    online: bool = False  # False when Pear API unreachable

    @property
    def has_song(self) -> bool:
        return bool(self.video_id)


@dataclass(slots=True)
class State:
    """Async-notifying container for the latest SongState."""

    current: SongState = field(default_factory=SongState)
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    def update(self, new: SongState) -> bool:
        """Return True when state actually changed."""
        if new == self.current:
            return False
        self.current = new
        self._changed.set()
        return True

    def patch(self, **fields: object) -> bool:
        return self.update(replace(self.current, **fields))  # type: ignore[arg-type]

    async def wait_change(self) -> SongState:
        await self._changed.wait()
        self._changed.clear()
        return self.current
