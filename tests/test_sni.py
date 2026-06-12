"""Smoke tests for ytm_indicator.sni — property getters and signal triggers."""

from __future__ import annotations

from ytm_indicator.sni import SNIInterface
from ytm_indicator.state import SongState, State

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_sni(state: State) -> SNIInterface:
    return SNIInterface(state, on_activate=lambda: None)


# ── property getters: no song ─────────────────────────────────────────────────


def test_title_no_song_is_fallback(empty_state: State) -> None:
    sni = _make_sni(empty_state)
    assert sni.Title == "YouTube Music"


def test_status_offline_is_passive(empty_state: State) -> None:
    sni = _make_sni(empty_state)
    assert sni.Status == "Passive"


def test_tooltip_no_song_title_is_fallback(empty_state: State) -> None:
    sni = _make_sni(empty_state)
    icon_name, icon_pixmap, title, _desc = sni.ToolTip
    assert title == "YouTube Music"
    assert isinstance(icon_name, str)
    assert isinstance(icon_pixmap, list)


# ── property getters: with song ───────────────────────────────────────────────


def test_title_with_song(playing_state: State) -> None:
    sni = _make_sni(playing_state)
    s = playing_state.current
    assert sni.Title == f"{s.title} — {s.artist}"


def test_status_online_is_active(playing_state: State) -> None:
    sni = _make_sni(playing_state)
    assert sni.Status == "Active"


def test_tooltip_with_song_and_album(playing_state: State) -> None:
    sni = _make_sni(playing_state)
    s = playing_state.current
    _, _, title, desc = sni.ToolTip
    assert title == s.title
    assert s.artist in desc
    assert s.album in desc


def test_tooltip_artist_only_no_album() -> None:
    state = State()
    state.update(SongState(title="T", artist="A", video_id="v1", online=True))
    sni = _make_sni(state)
    _, _, title, desc = sni.ToolTip
    assert title == "T"
    assert desc == "A"


def test_tooltip_no_artist_empty_desc() -> None:
    state = State()
    state.update(SongState(title="T", video_id="v1", online=True))
    sni = _make_sni(state)
    _, _, title, desc = sni.ToolTip
    assert title == "T"
    assert desc == ""


# ── static properties ─────────────────────────────────────────────────────────


def test_category(empty_state: State) -> None:
    assert _make_sni(empty_state).Category == "ApplicationStatus"


def test_id(empty_state: State) -> None:
    assert _make_sni(empty_state).Id == "ytm-indicator"


def test_icon_name_is_empty_string(empty_state: State) -> None:
    assert _make_sni(empty_state).IconName == ""


def test_item_is_menu_false(empty_state: State) -> None:
    assert _make_sni(empty_state).ItemIsMenu is False


def test_menu_is_no_menu_path(empty_state: State) -> None:
    from ytm_indicator.sni import NO_MENU_PATH

    assert _make_sni(empty_state).Menu == NO_MENU_PATH


def test_window_id_is_zero(empty_state: State) -> None:
    assert _make_sni(empty_state).WindowId == 0


def test_icon_pixmap_is_list(empty_state: State) -> None:
    pmap = _make_sni(empty_state).IconPixmap
    assert isinstance(pmap, list)
    assert len(pmap) == 1
    w, h, data = pmap[0]
    assert isinstance(w, int) and w > 0
    assert isinstance(h, int) and h > 0
    assert isinstance(data, bytes) and len(data) == w * h * 4


# ── signal-emitting methods (no bus → signals are no-ops) ────────────────────


def test_song_changed_does_not_raise(playing_state: State) -> None:
    sni = _make_sni(playing_state)
    sni.song_changed()  # should not raise even without a real D-Bus bus


def test_status_changed_does_not_raise(playing_state: State) -> None:
    sni = _make_sni(playing_state)
    sni.status_changed()


def test_status_changed_offline_state(empty_state: State) -> None:
    sni = _make_sni(empty_state)
    sni.status_changed()  # online=False → "Passive"; no exception


# ── callback routing ─────────────────────────────────────────────────────────


def test_activate_callback_called(playing_state: State) -> None:
    fired: list[bool] = []
    sni = SNIInterface(playing_state, on_activate=lambda: fired.append(True))
    sni.Activate(0, 0)
    assert fired == [True]


def test_context_menu_callback_called(playing_state: State) -> None:
    coords: list[tuple[int, int]] = []
    sni = SNIInterface(
        playing_state,
        on_activate=lambda: None,
        on_context_menu=lambda x, y: coords.append((x, y)),
    )
    sni.ContextMenu(10, 20)
    assert coords == [(10, 20)]


def test_context_menu_no_handler_does_not_raise(playing_state: State) -> None:
    sni = SNIInterface(playing_state, on_activate=lambda: None, on_context_menu=None)
    sni.ContextMenu(0, 0)


def test_activate_exception_does_not_propagate(playing_state: State) -> None:
    def _bad() -> None:
        raise RuntimeError("boom")

    sni = SNIInterface(playing_state, on_activate=_bad)
    sni.Activate(0, 0)  # should log but not re-raise
