# ytm-indicator — Outstanding Items

## v0.1.0 — shipped 2026-04-23

### Shipping

- [x] Project scaffold (pyproject, LICENSE, CLAUDE.md, TODO.md, README).
- [x] Pear API client with JWT pairing + token persistence (`pear_api.py`).
- [x] StatusNotifierItem D-Bus service (`sni.py`).
- [x] DBusMenu D-Bus service (`menu.py`).
- [x] Album art fetch → PNG cache (`art.py`).
- [x] Main loop wiring poll → state → SNI updates (`cli.py`).
- [x] systemd `--user` unit (`config/ytm-indicator.service`).
- [x] First smoke test: watcher registers us, Patina picks up the item,
      menu GetLayout returns the full tree, status toggles Active↔Passive
      as Pear comes/goes. Visual confirmation that the icon renders in
      Patina's header slot still pending — first time Dave looks at the
      tray he either sees it or files an issue.
- [x] git init, first commit, push to `Beryndil/ytm-indicator` org repo,
      tag `v0.1.0`.

### Post-ship follow-ups (still v0.1.x)

- **2026-04-23 fix** — `cli.Indicator._push_updates` no longer fires
  `NewTitle`/`NewToolTip`/`NewStatus` unconditionally. It now takes
  `prev` and emits `song_changed()` only when one of
  `(online, video_id, title, artist, album)` differs from the previous
  snapshot, and `status_changed()` only when `online` transitions.
  Reason: every SNI signal makes every host (e.g. Patina) call
  `Properties.GetAll` on the full interface, which PyGObject hosts
  unpack in pure-Python GVariant code — the 16 KB `IconPixmap` unpack
  alone is ~250 ms per round-trip. Before the fix, the 3 s poll plus
  the unconditional offline-backoff push burned ~60–77% of a core on
  Patina's main thread. `elapsed_s`/`duration_s` tick every poll and
  never alter a tray-visible field, so they no longer trigger signals.
  Verified with 60 SIGPROF samples on Patina post-fix: main thread
  idle in `Gio.py:138 run` on every sample.
- End-to-end test with a song actually playing in Pear — validates title
  updates, album art fetch, like/dislike toggles. Dave hasn't signed into
  YouTube Music inside Pear yet, so this is blocked on that.
- `ytm_indicator/sni.py` + `ytm_indicator/menu.py` use `@method()` without
  `-> None` return annotations as a workaround for dbus-fast's
  parse_annotation bug (it turns the string "None" back into Python None
  under `from __future__ import annotations`, then crashes
  get_signature_tree). If dbus-fast fixes this upstream, re-add the
  annotations and drop the comments.

### Deferred to v0.2.0

- MPRIS `PropertiesChanged` listener to replace polling. Pear exposes
  MPRIS via Chromium's MediaSession bridge; current plan is poll every
  3 s because that's less code and more robust to bus-name rotation.
- Configurable poll interval + endpoint via `~/.config/ytm-indicator/config.toml`.
- Pytest suite (currently no tests — ship working first, test on v0.2).
- Icon disk cache survives restart (v0.1 re-fetches on boot).
- Tooltip with full "title — artist — album" (SNI ToolTip property).

### Known gotchas to handle in v0.1

- Pear not running / API server off → indicator displays "offline" icon
  and disabled menu, retries every 10 s.
- JWT expired or invalidated → drop token, re-pair on next request.
- Chromium bus name rotation (when we go MPRIS in v0.2): re-resolve on
  `NameOwnerChanged`.
- `/api/v1/song` can return `null` between tracks; treat as "nothing
  playing," don't crash.
