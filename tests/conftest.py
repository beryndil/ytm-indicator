"""Shared fixtures for the ytm-indicator test suite."""

from __future__ import annotations

import pytest

from ytm_indicator.state import SongState, State


@pytest.fixture
def song_online() -> SongState:
    """A SongState representing an online, actively-playing song."""
    return SongState(
        title="Starlight",
        artist="Muse",
        album="Black Holes and Revelations",
        video_id="vid_abc123",
        is_paused=False,
        online=True,
    )


@pytest.fixture
def empty_state() -> State:
    """A freshly constructed State (default SongState, offline)."""
    return State()


@pytest.fixture
def playing_state(song_online: SongState) -> State:
    """A State seeded with an online playing song."""
    s = State()
    s.update(song_online)
    return s
