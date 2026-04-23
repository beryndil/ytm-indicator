# ytm-indicator — Tray control for Pear Desktop

## Project
- **Platform**: Python 3.12+, async, no GUI framework
- **Purpose**: Standalone StatusNotifierItem daemon that shows a
  now-playing icon in any SNI host (primary target: Patina) and controls
  Pear Desktop (YouTube Music) via MPRIS + Pear's API Server plugin
- **License**: Unlicense
- **Status**: Pre-release (0.x.x development). v0.1.0 polls; v0.2.0 signals.

## Tech Stack
- Python 3.12 floor
- uv + `pyproject.toml` (hatchling backend)
- `dbus-fast` for all D-Bus (SNI service export, DBusMenu, MPRIS listen)
- `aiohttp` for Pear API calls
- `Pillow` for album art → PNG at tray-icon sizes
- `ruff` lint + format, `mypy --strict`, `pytest` + `pytest-asyncio`

## Layout

```
ytm-indicator/
├── ytm_indicator/      # Python package
│   ├── __init__.py
│   ├── __main__.py     # python -m ytm_indicator
│   ├── cli.py          # entry point (ytm-indicator script)
│   ├── state.py        # central async-safe state store
│   ├── pear_api.py     # Pear Desktop HTTP + JWT pairing
│   ├── mpris.py        # MPRIS listener (v0.2+)
│   ├── sni.py          # StatusNotifierItem D-Bus service
│   ├── menu.py         # DBusMenu D-Bus service
│   └── art.py          # album-art download + resize + cache
├── tests/              # pytest suite
├── config/             # systemd --user unit
├── assets/             # fallback icons
└── pyproject.toml
```

## Why standalone (not a Patina widget)

Patina is mid-leak-hunt (see Patina's TODO.md §NEW LEAK). Adding a new
widget with D-Bus signals + HTTP + image decode into that process right
now would contaminate the investigation. This daemon runs in its own
process and registers as an SNI item; Patina hosts it via the
`StatusNotifierWatcher` it already owns. Kill it independently if it
misbehaves.

## Pear prerequisites

- `pear-desktop` from CachyOS repo (or AUR `pear-desktop-bin`)
- `~/.config/YouTube Music/config.json` must have:
  ```json
  "plugins": {
    "api-server": {
      "enabled": true,
      "hostname": "127.0.0.1",
      "port": 26538,
      "authStrategy": "AUTH_AT_FIRST"
    }
  }
  ```
- Pear must be running. No fallback when Pear is down — indicator shows
  a disabled icon and degraded menu.

## Auth flow

On first run the daemon calls `POST /auth/{id}` with a stable
`id = "ytm-indicator"`. Pear pops a confirm dialog. On accept, the
returned JWT is saved to `~/.config/ytm-indicator/token.json` and used
as `Authorization: Bearer <jwt>` for subsequent calls.

## Development workflow

```bash
uv sync
uv run ytm-indicator          # run in foreground for testing
uv run ruff check .
uv run ruff format --check .
uv run mypy ytm_indicator
uv run pytest
```

## Beryndil Development Standards

Follow `~/.claude/CLAUDE.md` and `~/.claude/rules/`. Project-specific:

- Functions max 40 lines, files max 400 lines, no magic numbers.
- No hardcoded secrets. The JWT is obtained at runtime via the pairing
  flow — never committed.
- Localhost-only. The daemon must refuse to talk to any Pear instance
  not on 127.0.0.1 / localhost, even if config says otherwise.
- No `requests` library — stay async, use `aiohttp`.
- One async `asyncio.TaskGroup` at the top of `cli.main()`; every
  long-running coroutine is a child task so a crash tears everything
  down cleanly.
