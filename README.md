# ytm-indicator

Standalone StatusNotifierItem (SNI) tray control for **Pear Desktop**
(formerly `th-ch/youtube-music`).

Pops an album-art icon into any SNI host (Patina, waybar, KDE, etc.) and
exposes play/pause/next/prev/like/dislike through a DBusMenu. No GUI
framework — pure async Python, `dbus-fast` + `aiohttp`.

## Why standalone

Deliberately decoupled from Patina so a bug in this process can't take
Patina down. Patina already owns `org.kde.StatusNotifierWatcher`, so
the icon surfaces there automatically once the daemon registers.

## Requirements

- Python 3.12+
- [`pear-desktop`](https://github.com/pear-devs/pear-desktop) running with the
  **API Server** plugin enabled on `127.0.0.1:26538`
- Any SNI host (Patina's tray slot, waybar `tray` module, etc.)

## Install

```bash
uv sync
uv run ytm-indicator
```

First run pops a confirm dialog inside Pear to approve this client. The
resulting JWT is persisted to `~/.config/ytm-indicator/token.json`.

## systemd user service

```bash
cp config/ytm-indicator.service ~/.config/systemd/user/
systemctl --user enable --now ytm-indicator
```

## Suppressing Pear's own tray icon

Pear gates close-to-tray behavior AND its native tray-icon creation
behind the same `options.tray` flag, so enabling close-to-tray means
two YouTube Music icons in the tray (ours and Pear's). To get one
without the other, patch Pear's `app.asar` so `setUpTray()` becomes a
no-op while leaving the close handler alone:

```bash
sudo install -m 0755 scripts/patch-pear-notray.sh /usr/local/bin/patch-pear-notray
sudo install -m 0644 config/pear-notray.hook /etc/pacman.d/hooks/pear-notray.hook
sudo /usr/local/bin/patch-pear-notray
```

The pacman hook re-runs the patch after every `pear-desktop` upgrade.
The original asar is preserved at
`/usr/lib/pear-desktop/app.asar.pristine.<version>`; remove the hook and
swap that file back to revert.

Requires `nodejs` + `npm` (for `npx @electron/asar`).

## Status

Pre-release (0.x.x). v0.1.0 polls Pear every 3 s for now-playing state;
v0.2.0 will switch to MPRIS `PropertiesChanged` signals for push updates.

## License

Unlicense. See `LICENSE`.
