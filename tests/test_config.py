"""Tests for ytm_indicator.config."""

from __future__ import annotations

from pathlib import Path

import pytest

import ytm_indicator.config as config_mod
from ytm_indicator.config import Config, load

# ── fixture: redirect config paths to temp directory ─────────────────────────


@pytest.fixture(autouse=True)
def _patch_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_dir = tmp_path / "ytm-config"
    cfg_file = cfg_dir / "config.toml"
    monkeypatch.setattr(config_mod, "_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_mod, "_CONFIG_PATH", cfg_file)
    return cfg_file


# ── defaults when no file exists ─────────────────────────────────────────────


def test_defaults_when_no_file() -> None:
    cfg = load()
    assert cfg.poll_interval_s == 3.0
    assert cfg.offline_backoff_s == 10.0
    assert cfg.pear_host == "127.0.0.1"
    assert cfg.pear_port == 26538


def test_creates_file_when_absent() -> None:
    assert not config_mod._CONFIG_PATH.exists()
    load()
    assert config_mod._CONFIG_PATH.exists()
    content = config_mod._CONFIG_PATH.read_text()
    assert "poll_interval_s" in content


def test_default_config_object_attributes() -> None:
    cfg = Config()
    assert isinstance(cfg.poll_interval_s, float)
    assert isinstance(cfg.offline_backoff_s, float)
    assert isinstance(cfg.pear_host, str)
    assert isinstance(cfg.pear_port, int)


# ── override from TOML ────────────────────────────────────────────────────────


def test_override_poll_interval() -> None:
    config_mod._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_mod._CONFIG_PATH.write_text(
        "[poll]\npoll_interval_s = 7.5\noffline_backoff_s = 20.0\n"
        '[pear]\nhost = "127.0.0.1"\nport = 26538\n'
    )
    cfg = load()
    assert cfg.poll_interval_s == 7.5
    assert cfg.offline_backoff_s == 20.0


def test_override_pear_port() -> None:
    config_mod._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_mod._CONFIG_PATH.write_text(
        "[poll]\npoll_interval_s = 3.0\noffline_backoff_s = 10.0\n"
        '[pear]\nhost = "127.0.0.1"\nport = 9999\n'
    )
    cfg = load()
    assert cfg.pear_port == 9999


# ── missing / bad keys fall back to default ───────────────────────────────────


def test_missing_pear_section_falls_back() -> None:
    config_mod._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_mod._CONFIG_PATH.write_text("[poll]\npoll_interval_s = 5.0\noffline_backoff_s = 15.0\n")
    cfg = load()
    assert cfg.pear_host == "127.0.0.1"
    assert cfg.pear_port == 26538


def test_missing_poll_section_falls_back() -> None:
    config_mod._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_mod._CONFIG_PATH.write_text('[pear]\nhost = "127.0.0.1"\nport = 26538\n')
    cfg = load()
    assert cfg.poll_interval_s == 3.0
    assert cfg.offline_backoff_s == 10.0


def test_wrong_type_falls_back_to_default() -> None:
    config_mod._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_mod._CONFIG_PATH.write_text(
        '[poll]\npoll_interval_s = "not_a_number"\noffline_backoff_s = 10.0\n'
    )
    cfg = load()
    # _float("not_a_number", 3.0) → 3.0 default  NOTE: float("not_a_number") raises ValueError
    # wait — actually float("not_a_number") raises, so it returns 3.0 default
    assert cfg.poll_interval_s == 3.0


def test_empty_host_string_falls_back() -> None:
    config_mod._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_mod._CONFIG_PATH.write_text(
        '[poll]\npoll_interval_s = 3.0\noffline_backoff_s = 10.0\n[pear]\nhost = ""\nport = 26538\n'
    )
    cfg = load()
    assert cfg.pear_host == "127.0.0.1"


def test_invalid_toml_falls_back_to_defaults() -> None:
    config_mod._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_mod._CONFIG_PATH.write_text("[[[[not valid toml")
    cfg = load()
    assert cfg.poll_interval_s == 3.0
    assert cfg.pear_port == 26538


def test_load_does_not_overwrite_existing_file() -> None:
    config_mod._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    custom = "[poll]\npoll_interval_s = 99.0\noffline_backoff_s = 10.0\n"
    config_mod._CONFIG_PATH.write_text(custom)
    load()
    assert config_mod._CONFIG_PATH.read_text() == custom
