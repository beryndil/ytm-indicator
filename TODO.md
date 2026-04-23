# ytm-indicator — Outstanding Items

## v0.1.0 — initial build (2026-04-23, in progress)

### Shipping

- [x] Project scaffold (pyproject, LICENSE, CLAUDE.md, TODO.md, README).
- [ ] Pear API client with JWT pairing + token persistence (`pear_api.py`).
- [ ] StatusNotifierItem D-Bus service (`sni.py`).
- [ ] DBusMenu D-Bus service (`menu.py`).
- [ ] Album art fetch → PNG cache (`art.py`).
- [ ] Main loop wiring poll → state → SNI updates (`cli.py`).
- [ ] systemd `--user` unit (`config/ytm-indicator.service`).
- [ ] First smoke test: icon appears in Patina tray, play/pause works.
- [ ] git init, first commit, push to `Beryndil/ytm-indicator` org repo.

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
