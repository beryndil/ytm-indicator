"""Tests for ytm_indicator.state."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from ytm_indicator.state import SongState, State

# ── SongState ────────────────────────────────────────────────────────────────


def test_song_state_defaults() -> None:
    s = SongState()
    assert s.title == ""
    assert s.artist == ""
    assert s.album is None
    assert s.video_id == ""
    assert s.image_src is None
    assert s.is_paused is True
    assert s.duration_s == 0.0
    assert s.elapsed_s == 0.0
    assert s.like == "INDIFFERENT"
    assert s.online is False


def test_has_song_false_when_no_video_id() -> None:
    assert SongState().has_song is False


def test_has_song_true_with_video_id() -> None:
    s = SongState(video_id="abc123")
    assert s.has_song is True


def test_song_state_equality_same_values() -> None:
    a = SongState(title="T", artist="A", video_id="v1")
    b = SongState(title="T", artist="A", video_id="v1")
    assert a == b


def test_song_state_inequality_different_field() -> None:
    a = SongState(title="T", artist="A", video_id="v1")
    b = SongState(title="T", artist="A", video_id="v2")
    assert a != b


def test_song_state_is_frozen() -> None:
    s = SongState(title="T")
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.title = "changed"  # type: ignore[misc]


# ── State ────────────────────────────────────────────────────────────────────


def test_state_default_current_is_default_song() -> None:
    state = State()
    assert state.current == SongState()


def test_state_update_same_value_returns_false() -> None:
    state = State()
    assert state.update(SongState()) is False


def test_state_update_new_value_returns_true() -> None:
    state = State()
    new = SongState(video_id="xyz", online=True)
    assert state.update(new) is True
    assert state.current == new


def test_state_update_sets_event() -> None:
    state = State()
    new = SongState(video_id="xyz")
    state.update(new)
    assert state._changed.is_set()


def test_state_update_same_does_not_set_event() -> None:
    state = State()
    state.update(SongState())
    # event was not set (same value as default)
    assert not state._changed.is_set()


def test_state_patch_changes_one_field() -> None:
    state = State()
    state.update(SongState(title="Old", video_id="v1", is_paused=True))
    changed = state.patch(is_paused=False)
    assert changed is True
    assert state.current.is_paused is False
    assert state.current.title == "Old"


def test_state_patch_same_value_returns_false() -> None:
    state = State()
    state.update(SongState(is_paused=True))
    assert state.patch(is_paused=True) is False


# ── State.wait_change ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_change_returns_new_state() -> None:
    state = State()
    new = SongState(video_id="vid", online=True)

    async def _updater() -> None:
        await asyncio.sleep(0)  # yield once
        state.update(new)

    task = asyncio.create_task(_updater())
    result = await asyncio.wait_for(state.wait_change(), timeout=1.0)
    await task
    assert result == new


@pytest.mark.asyncio
async def test_wait_change_clears_event() -> None:
    state = State()

    async def _updater() -> None:
        await asyncio.sleep(0)
        state.update(SongState(video_id="v1"))

    task = asyncio.create_task(_updater())
    await asyncio.wait_for(state.wait_change(), timeout=1.0)
    await task
    assert not state._changed.is_set()


# ── online / offline transitions ──────────────────────────────────────────────


def test_online_transition() -> None:
    state = State()
    state.update(SongState(online=False))
    state.patch(online=True)
    assert state.current.online is True


def test_song_change_detection() -> None:
    state = State()
    first = SongState(title="Song A", video_id="v1", online=True)
    second = SongState(title="Song B", video_id="v2", online=True)
    state.update(first)
    assert state.update(second) is True
    assert state.current.video_id == "v2"


def test_play_pause_transition() -> None:
    state = State()
    state.update(SongState(video_id="v1", is_paused=False, online=True))
    assert state.patch(is_paused=True) is True
    assert state.current.is_paused is True
