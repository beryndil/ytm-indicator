"""Configurable runtime settings — reads ~/.config/ytm-indicator/config.toml."""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ytm-indicator"
_CONFIG_PATH = _CONFIG_DIR / "config.toml"

_DEFAULT_TOML = """\
# ytm-indicator configuration
# https://github.com/Beryndil/ytm-indicator

[poll]
# How often to query the Pear API when playing (seconds).
poll_interval_s = 3.0

# How long to wait before retrying when Pear is offline (seconds).
offline_backoff_s = 10.0

[pear]
# Pear API Server hostname (localhost only — do not change to a remote host).
host = "127.0.0.1"

# Pear API Server port — must match api-server.port in Pear's config.json.
port = 26538
"""


class Config:
    """Flat config bag loaded from config.toml.

    All values fall back to defaults on missing key or parse failure.
    The file is created with defaults if it does not exist.
    """

    def __init__(
        self,
        poll_interval_s: float = 3.0,
        offline_backoff_s: float = 10.0,
        pear_host: str = "127.0.0.1",
        pear_port: int = 26538,
    ) -> None:
        self.poll_interval_s = poll_interval_s
        self.offline_backoff_s = offline_backoff_s
        self.pear_host = pear_host
        self.pear_port = pear_port


def load() -> Config:
    """Load config from disk, creating the file with defaults if absent."""
    _ensure_default()
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            log.warning("no TOML library available; using defaults")
            return Config()

    try:
        data = tomllib.loads(_CONFIG_PATH.read_text())
    except Exception as e:
        log.warning("failed to read %s: %s; using defaults", _CONFIG_PATH, e)
        return Config()

    poll = data.get("poll", {}) if isinstance(data.get("poll"), dict) else {}
    pear = data.get("pear", {}) if isinstance(data.get("pear"), dict) else {}

    return Config(
        poll_interval_s=_float(poll.get("poll_interval_s"), 3.0),
        offline_backoff_s=_float(poll.get("offline_backoff_s"), 10.0),
        pear_host=_str(pear.get("host"), "127.0.0.1"),
        pear_port=_int(pear.get("port"), 26538),
    )


def _ensure_default() -> None:
    if _CONFIG_PATH.exists():
        return
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(_DEFAULT_TOML)
        log.info("created default config at %s", _CONFIG_PATH)
    except OSError as e:
        log.warning("could not create default config: %s", e)


def _float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _str(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
