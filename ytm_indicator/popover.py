"""Modern now-playing popover — what the tray shows on right-click.

Intentionally a standalone GTK4 / libadwaita process. The indicator spawns
this as a subprocess on `ContextMenu()`; the popover then talks to Pear's
HTTP API directly using the JWT we persisted in
`~/.config/ytm-indicator/token.json`. Running out-of-process means GTK's
main loop can't fight with the indicator's asyncio loop, and a rendering
bug here can't take the tray icon down.

Layout is deliberately close to GNOME Shell's 46+ media widget and
libadwaita's `About` dialogs: centered square art, title/artist/album
stack, a slim progress bar with cursor times, a three-button transport
row, and a secondary row for like/dislike + "open Pear". The accent
color is sampled from the album art (dominant saturated swatch) and
injected as CSS so the progress highlight and play button match the
artwork. Falls back to libadwaita's default accent when sampling fails.

Positioning uses `gtk4-layer-shell` (OVERLAY layer, anchored top-right)
so it drops in from wherever the tray lives without needing a
Hyprland window rule. Escape or the close button dismisses.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

log = logging.getLogger("ytm_indicator.popover")

BASE_URL = "http://127.0.0.1:26538"
TOKEN_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "ytm-indicator"
    / "token.json"
)
ART_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ytm-indicator"
POLL_S = 1.5
REQUEST_TIMEOUT_S = 3.0

# Fallback accent (libadwaita default blue) if album art sampling fails.
DEFAULT_ACCENT = "#3584e4"

# gtk4-layer-shell works by interposing itself between GTK and libwayland
# at dynamic-link time. Python imports don't satisfy the "before libwayland"
# ordering, so the library asks us to LD_PRELOAD it. If we weren't invoked
# with it, re-exec ourselves with it set.
LAYER_SHELL_LIB = "/usr/lib/libgtk4-layer-shell.so"
_LAYER_SHELL_MARKER = "_YTM_LAYER_SHELL_PRELOADED"


# ─── Pear client (sync, stdlib only) ────────────────────────────────────

def _load_token() -> str | None:
    try:
        data = json.loads(TOKEN_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    tok = data.get("accessToken")
    return tok if isinstance(tok, str) and tok else None


def _request(method: str, path: str, token: str | None) -> Any:
    """Fire one request; return parsed JSON or None on any failure."""
    req = urllib.request.Request(f"{BASE_URL}{path}", method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            body = resp.read()
            if resp.headers.get_content_type() == "application/json" and body:
                return json.loads(body)
            return None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        log.debug("request %s %s failed: %s", method, path, e)
        return None


# ─── Accent extraction ──────────────────────────────────────────────────

def _extract_accent(png_path: Path) -> str | None:
    """Pick the most saturated swatch from album art; None if we can't read it."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(png_path) as im:
            im = im.convert("RGB").resize((96, 96))
            pal = im.quantize(colors=8, method=Image.Quantize.FASTOCTREE)
            palette = pal.getpalette() or []
            counts = sorted(pal.getcolors() or [], reverse=True)
            for _, idx in counts:
                r, g, b = palette[idx * 3 : idx * 3 + 3]
                mx, mn = max(r, g, b), min(r, g, b)
                saturation = (mx - mn) / mx if mx else 0
                # Skip near-grey and near-black swatches; those make a muddy UI.
                if saturation > 0.35 and mx > 60:
                    return f"#{r:02x}{g:02x}{b:02x}"
    except Exception as e:
        log.debug("accent extract failed on %s: %s", png_path, e)
    return None


# ─── Time formatting ────────────────────────────────────────────────────

def _fmt_time(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


# ─── Static CSS (dynamic accent layered on top) ─────────────────────────

BASE_CSS = b"""
window.ytm-popover {
    border-radius: 18px;
}
.ytm-art {
    border-radius: 14px;
    background: alpha(@window_fg_color, 0.08);
}
.ytm-title {
    font-weight: 700;
    font-size: 16pt;
}
.ytm-artist {
    font-size: 12pt;
}
.ytm-album {
    font-size: 10pt;
    opacity: 0.65;
}
.ytm-progress trough {
    min-height: 5px;
    border-radius: 3px;
}
.ytm-progress trough slider {
    min-width: 14px;
    min-height: 14px;
    background: @theme_fg_color;
    border-radius: 9999px;
    border: none;
    margin: -5px 0;
    opacity: 0;
    transition: opacity 120ms ease;
}
.ytm-progress:hover trough slider {
    opacity: 1;
}
.ytm-round {
    border-radius: 9999px;
    padding: 10px;
    min-width: 0;
    min-height: 0;
}
.ytm-playbtn {
    border-radius: 9999px;
    padding: 14px;
    min-width: 0;
    min-height: 0;
}
.ytm-secondary button {
    border-radius: 9999px;
    padding: 6px 14px;
}
.ytm-secondary button.active {
    background: alpha(@accent_bg_color, 0.25);
}
.ytm-time {
    font-size: 9pt;
    opacity: 0.6;
    font-variant-numeric: tabular-nums;
}
"""


# ─── The popover window ─────────────────────────────────────────────────

class Popover(Gtk.Window):
    # Subclassing plain Gtk.Window (not Adw.ApplicationWindow) because
    # gtk4-layer-shell emits "not a layer surface" warnings and silently
    # falls back to a regular toplevel when given an AdwApplicationWindow
    # on current libadwaita. Plain Gtk.Window + libadwaita styling still
    # renders identically — we aren't using AdwApplicationWindow-specific
    # features (no integrated sidebar, etc.).
    def __init__(self, app: Adw.Application, initial: dict[str, Any], token: str | None) -> None:
        super().__init__()
        app.add_window(self)
        self.set_default_size(440, 560)
        self.set_resizable(False)
        self.set_decorated(False)
        self.add_css_class("ytm-popover")

        LayerShell.init_for_window(self)
        LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
        LayerShell.set_anchor(self, LayerShell.Edge.TOP, True)
        LayerShell.set_anchor(self, LayerShell.Edge.RIGHT, True)
        LayerShell.set_margin(self, LayerShell.Edge.TOP, 56)
        LayerShell.set_margin(self, LayerShell.Edge.RIGHT, 12)
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.ON_DEMAND)

        self._token = token
        self._state = initial
        self._accent_provider: Gtk.CssProvider | None = None
        self._stop = threading.Event()

        self._build()
        self._apply_state()

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        threading.Thread(target=self._poll_loop, daemon=True).start()

    # ── layout ──────────────────────────────────────────────────────────

    def _build(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Custom minimal header: just a close button pinned top-right.
        # AdwHeaderBar reaches into AdwApplicationWindow internals we don't
        # have (we subclass Gtk.Window for layer-shell compatibility).
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.set_margin_top(10)
        header.set_margin_end(10)
        header.set_margin_start(10)
        spacer = Gtk.Box(hexpand=True)
        header.append(spacer)
        close_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.add_css_class("circular")
        close_btn.connect("clicked", lambda *_: self.close())
        header.append(close_btn)
        outer.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_start(28)
        body.set_margin_end(28)
        body.set_margin_top(4)
        body.set_margin_bottom(24)
        body.set_halign(Gtk.Align.FILL)

        # Album art
        art_frame = Gtk.Frame()
        art_frame.add_css_class("ytm-art")
        art_frame.set_halign(Gtk.Align.CENTER)
        self.art = Gtk.Picture()
        self.art.set_size_request(220, 220)
        self.art.set_content_fit(Gtk.ContentFit.COVER)
        art_frame.set_child(self.art)
        body.append(art_frame)

        # Title / artist / album
        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meta.set_margin_top(14)
        self.title = Gtk.Label(xalign=0.5)
        self.title.set_wrap(True)
        self.title.set_max_width_chars(28)
        self.title.set_justify(Gtk.Justification.CENTER)
        self.title.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.title.add_css_class("ytm-title")
        meta.append(self.title)
        self.artist = Gtk.Label(xalign=0.5)
        self.artist.set_ellipsize(3)
        self.artist.add_css_class("ytm-artist")
        meta.append(self.artist)
        self.album = Gtk.Label(xalign=0.5)
        self.album.set_ellipsize(3)
        self.album.add_css_class("ytm-album")
        meta.append(self.album)
        body.append(meta)

        # Progress bar row
        progress_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        progress_row.set_margin_top(14)
        self.elapsed_lbl = Gtk.Label(label="0:00")
        self.elapsed_lbl.add_css_class("ytm-time")
        self.remaining_lbl = Gtk.Label(label="0:00")
        self.remaining_lbl.add_css_class("ytm-time")
        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        self.scale.set_range(0, 1)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.add_css_class("ytm-progress")
        self._seek_id = self.scale.connect("change-value", self._on_seek)
        progress_row.append(self.elapsed_lbl)
        progress_row.append(self.scale)
        progress_row.append(self.remaining_lbl)
        body.append(progress_row)

        # Transport row
        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        transport.set_halign(Gtk.Align.CENTER)
        transport.set_margin_top(12)
        self.prev_btn = Gtk.Button.new_from_icon_name("media-skip-backward-symbolic")
        self.prev_btn.add_css_class("flat")
        self.prev_btn.add_css_class("circular")
        self.prev_btn.add_css_class("ytm-round")
        self.prev_btn.connect("clicked", lambda *_: self._fire("POST", "/api/v1/previous"))
        self.play_btn = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self.play_btn.add_css_class("suggested-action")
        self.play_btn.add_css_class("circular")
        self.play_btn.add_css_class("ytm-playbtn")
        self.play_btn.connect("clicked", self._on_toggle)
        self.next_btn = Gtk.Button.new_from_icon_name("media-skip-forward-symbolic")
        self.next_btn.add_css_class("flat")
        self.next_btn.add_css_class("circular")
        self.next_btn.add_css_class("ytm-round")
        self.next_btn.connect("clicked", lambda *_: self._fire("POST", "/api/v1/next"))
        transport.append(self.prev_btn)
        transport.append(self.play_btn)
        transport.append(self.next_btn)
        body.append(transport)

        # Secondary row: like/dislike/open
        secondary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        secondary.set_halign(Gtk.Align.CENTER)
        secondary.set_margin_top(14)
        secondary.add_css_class("ytm-secondary")
        self.like_btn = Gtk.ToggleButton(label="♥  Like")
        self._like_id = self.like_btn.connect("toggled", self._on_like)
        self.dislike_btn = Gtk.ToggleButton(label="♡  Dislike")
        self._dislike_id = self.dislike_btn.connect("toggled", self._on_dislike)
        self.open_btn = Gtk.Button(label="Open Pear")
        self.open_btn.connect("clicked", lambda *_: self._open_pear())
        secondary.append(self.like_btn)
        secondary.append(self.dislike_btn)
        secondary.append(self.open_btn)
        body.append(secondary)

        outer.append(body)
        self.set_child(outer)

    # ── state → UI ─────────────────────────────────────────────────────

    def _apply_state(self) -> None:
        s = self._state
        art_path = _art_path_for(s)
        if art_path is not None:
            self.art.set_filename(str(art_path))
            accent = _extract_accent(art_path) or DEFAULT_ACCENT
            self._set_accent(accent)
        else:
            # Gtk.Picture has no clear-state setter in GTK 4.22; calling
            # set_filename("") is a documented no-op reset.
            self.art.set_filename(None)
            self._set_accent(DEFAULT_ACCENT)

        self.title.set_label(s.get("title") or "Nothing playing")
        self.artist.set_label(s.get("artist") or "")
        album = s.get("album") or ""
        self.album.set_label(album)
        self.album.set_visible(bool(album))

        duration = float(s.get("duration_s") or 0)
        elapsed = float(s.get("elapsed_s") or 0)
        if duration > 0:
            self.scale.handler_block(self._seek_id)
            self.scale.set_range(0, duration)
            self.scale.set_value(min(elapsed, duration))
            self.scale.handler_unblock(self._seek_id)
            self.scale.set_sensitive(True)
            self.elapsed_lbl.set_label(_fmt_time(elapsed))
            self.remaining_lbl.set_label("-" + _fmt_time(max(0, duration - elapsed)))
        else:
            self.scale.set_sensitive(False)
            self.elapsed_lbl.set_label("0:00")
            self.remaining_lbl.set_label("0:00")

        paused = bool(s.get("is_paused", True))
        self.play_btn.set_icon_name(
            "media-playback-start-symbolic" if paused else "media-playback-pause-symbolic"
        )

        like = s.get("like", "INDIFFERENT")
        with _SignalBlock(self.like_btn, self._like_id):
            self.like_btn.set_active(like == "LIKE")
        with _SignalBlock(self.dislike_btn, self._dislike_id):
            self.dislike_btn.set_active(like == "DISLIKE")

        has_song = bool(s.get("title"))
        for b in (self.prev_btn, self.next_btn, self.like_btn, self.dislike_btn):
            b.set_sensitive(has_song)
        self.play_btn.set_sensitive(has_song)

    def _set_accent(self, color: str) -> None:
        css = (
            f".ytm-progress highlight {{ background: {color}; border-radius: 3px; }}"
            f".ytm-playbtn {{ background: {color}; color: #ffffff; }}"
            f".ytm-secondary button.active {{ background: alpha({color}, 0.25); "
            f"color: {color}; }}"
        ).encode()
        # Replace any previously-installed accent provider so we don't stack.
        display = self.get_display()
        if self._accent_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._accent_provider)
        provider = Gtk.CssProvider()
        provider.load_from_data(css, len(css))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 10
        )
        self._accent_provider = provider

    # ── interactions ────────────────────────────────────────────────────

    def _on_key(self, _c: object, keyval: int, _kc: int, _mod: int) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_toggle(self, _btn: Gtk.Button) -> None:
        self._fire("POST", "/api/v1/toggle-play")

    def _on_like(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():
            self._fire("POST", "/api/v1/like")
            with _SignalBlock(self.dislike_btn, self._dislike_id):
                self.dislike_btn.set_active(False)

    def _on_dislike(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():
            self._fire("POST", "/api/v1/dislike")
            with _SignalBlock(self.like_btn, self._like_id):
                self.like_btn.set_active(False)

    def _on_seek(self, _scale: Gtk.Scale, _scroll: object, value: float) -> bool:
        duration = float(self._state.get("duration_s") or 0)
        if duration <= 0:
            return False
        seconds = max(0, min(int(value), int(duration)))
        self._fire("POST", f"/api/v1/seek-to?seconds={seconds}")
        return False

    def _open_pear(self) -> None:
        try:
            subprocess.Popen(
                ["pear-desktop"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as e:
            log.warning("pear-desktop spawn failed: %s", e)

    def _fire(self, method: str, path: str) -> None:
        threading.Thread(
            target=_request, args=(method, path, self._token), daemon=True
        ).start()
        # Refresh quickly so the UI reflects the action even before the poll
        # tick catches up.
        GLib.timeout_add(200, self._refresh_now)

    def _refresh_now(self) -> bool:
        threading.Thread(target=self._refresh_thread, daemon=True).start()
        return False

    def _refresh_thread(self) -> None:
        song = _request("GET", "/api/v1/song", self._token)
        like_data = _request("GET", "/api/v1/like-state", self._token)
        if song is None:
            return
        state = dict(self._state)
        state.update(
            title=song.get("title", state.get("title", "")) or "",
            artist=song.get("artist", state.get("artist", "")) or "",
            album=song.get("album") if isinstance(song.get("album"), str) else None,
            video_id=song.get("videoId", state.get("video_id", "")) or "",
            image_src=song.get("imageSrc") if isinstance(song.get("imageSrc"), str) else None,
            is_paused=bool(song.get("isPaused", True)),
            duration_s=float(song.get("songDuration", 0) or 0),
            elapsed_s=float(song.get("elapsedSeconds", 0) or 0),
            like=(like_data or {}).get("state", state.get("like", "INDIFFERENT")),
        )
        GLib.idle_add(self._set_state, state)

    def _set_state(self, state: dict[str, Any]) -> bool:
        self._state = state
        self._apply_state()
        return False

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(POLL_S):
                break
            self._refresh_thread()

    def do_close_request(self) -> bool:  # type: ignore[override]
        self._stop.set()
        return False  # allow close


class _SignalBlock:
    """Context manager that blocks a GObject signal handler for its lifetime."""

    def __init__(self, obj: Any, handler_id: int) -> None:
        self._obj = obj
        self._id = handler_id

    def __enter__(self) -> None:
        self._obj.handler_block(self._id)

    def __exit__(self, *_: object) -> None:
        self._obj.handler_unblock(self._id)


def _art_path_for(state: dict[str, Any]) -> Path | None:
    vid = state.get("video_id") or ""
    if not vid:
        return None
    path = ART_CACHE / f"{vid}.img"
    return path if path.exists() and path.stat().st_size > 0 else None


# ─── Entry point ────────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> dict[str, Any]:
    ap = argparse.ArgumentParser(description="ytm-indicator now-playing popover")
    ap.add_argument("--title", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--album", default="")
    ap.add_argument("--video-id", default="")
    ap.add_argument("--paused", default="true")
    ap.add_argument("--elapsed", type=float, default=0.0)
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--like", default="INDIFFERENT")
    args = ap.parse_args(argv)
    return {
        "title": args.title,
        "artist": args.artist,
        "album": args.album or None,
        "video_id": args.video_id,
        "is_paused": args.paused.lower() in ("true", "1", "yes"),
        "elapsed_s": args.elapsed,
        "duration_s": args.duration,
        "like": args.like,
    }


def _ensure_layer_shell_preloaded() -> None:
    """Re-exec with LD_PRELOAD=libgtk4-layer-shell.so if not already set.

    gtk4-layer-shell must load before libwayland; dlopen from a Python
    import happens too late. We use a marker env var to detect whether
    we've already re-execed so we don't loop.
    """
    if os.environ.get(_LAYER_SHELL_MARKER):
        return
    if not Path(LAYER_SHELL_LIB).exists():
        log.warning("%s not found — skipping layer-shell preload", LAYER_SHELL_LIB)
        return
    env = dict(os.environ)
    existing = env.get("LD_PRELOAD", "")
    env["LD_PRELOAD"] = f"{LAYER_SHELL_LIB}:{existing}" if existing else LAYER_SHELL_LIB
    env[_LAYER_SHELL_MARKER] = "1"
    os.execvpe(sys.executable, [sys.executable, "-m", "ytm_indicator.popover", *sys.argv[1:]], env)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    _ensure_layer_shell_preloaded()
    initial = _parse_args(sys.argv[1:])
    token = _load_token()

    Adw.init()
    app = Adw.Application(
        application_id="org.beryndil.YtmIndicator.Popover",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )

    def on_activate(application: Adw.Application) -> None:
        display = Gdk.Display.get_default()
        base_provider = Gtk.CssProvider()
        base_provider.load_from_data(BASE_CSS, len(BASE_CSS))
        Gtk.StyleContext.add_provider_for_display(
            display, base_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        win = Popover(application, initial, token)
        win.present()

    app.connect("activate", on_activate)
    app.run([])


if __name__ == "__main__":
    main()
