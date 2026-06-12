"""Tests for ytm_indicator.art."""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import ytm_indicator.art as art_mod
from ytm_indicator.art import (
    CACHE_TTL_S,
    _build_png_from_argb,
    _load_argb_cache,
    _rgba_to_argb,
    _save_argb_cache,
    evict_old_cache,
    fetch_art,
)

# ── autouse: isolate CACHE_DIR to a temp directory ────────────────────────────


@pytest.fixture(autouse=True)
def _patch_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "art-cache"
    cache.mkdir()
    monkeypatch.setattr(art_mod, "CACHE_DIR", cache)
    return cache


# ── ARGB conversion ───────────────────────────────────────────────────────────


def test_rgba_to_argb_single_pixel() -> None:
    # RGBA pixel: R=0x10, G=0x20, B=0x30, A=0xFF
    rgba = bytes([0x10, 0x20, 0x30, 0xFF])
    argb = _rgba_to_argb(rgba)
    assert argb == bytes([0xFF, 0x10, 0x20, 0x30])


def test_rgba_to_argb_multiple_pixels() -> None:
    rgba = bytes([0xAA, 0xBB, 0xCC, 0xDD]) * 4
    argb = _rgba_to_argb(rgba)
    assert len(argb) == len(rgba)
    # Each pixel: A=0xDD, R=0xAA, G=0xBB, B=0xCC
    for i in range(4):
        base = i * 4
        assert argb[base] == 0xDD
        assert argb[base + 1] == 0xAA
        assert argb[base + 2] == 0xBB
        assert argb[base + 3] == 0xCC


def test_rgba_to_argb_roundtrip_length() -> None:
    rgba = bytes(range(256)) * 4
    assert len(_rgba_to_argb(rgba)) == len(rgba)


# ── ARGB disk cache ───────────────────────────────────────────────────────────


def test_load_argb_cache_miss() -> None:
    assert _load_argb_cache("nonexistent_vid") is None


def test_save_and_load_argb_cache() -> None:
    vid = "test_vid_01"
    w, h = 4, 4
    argb = bytes([0xFF, 0x80, 0x40, 0x20]) * (w * h)
    _save_argb_cache(vid, w, h, argb)

    result = _load_argb_cache(vid)
    assert result is not None
    rw, rh, rdata = result
    assert rw == w
    assert rh == h
    assert rdata == argb


def test_load_argb_cache_corrupt_data() -> None:
    """Wrong byte count returns None (corrupt detection)."""
    vid = "corrupt_vid"
    w, h = 2, 2
    # Write meta saying 2x2 but write wrong number of bytes
    cache_dir = art_mod.CACHE_DIR
    (cache_dir / f"art_{vid}.argb").write_bytes(b"\x00" * 5)  # should be 16
    (cache_dir / f"art_{vid}.meta").write_text(f"{w} {h}\n")

    assert _load_argb_cache(vid) is None


def test_load_argb_cache_missing_meta() -> None:
    vid = "no_meta_vid"
    (art_mod.CACHE_DIR / f"art_{vid}.argb").write_bytes(b"\x00" * 16)
    # No .meta file
    assert _load_argb_cache(vid) is None


# ── evict_old_cache ───────────────────────────────────────────────────────────


def test_evict_removes_old_files() -> None:
    cache_dir = art_mod.CACHE_DIR
    old_argb = cache_dir / "art_oldvid.argb"
    old_meta = cache_dir / "art_oldvid.meta"
    old_argb.write_bytes(b"\x00")
    old_meta.write_text("1 1\n")

    # Back-date to older than CACHE_TTL_S
    old_time = time.time() - CACHE_TTL_S - 100
    os.utime(old_argb, (old_time, old_time))
    os.utime(old_meta, (old_time, old_time))

    evict_old_cache()

    assert not old_argb.exists()
    assert not old_meta.exists()


def test_evict_keeps_fresh_files() -> None:
    cache_dir = art_mod.CACHE_DIR
    fresh = cache_dir / "art_freshvid.argb"
    fresh.write_bytes(b"\x00")

    evict_old_cache()

    assert fresh.exists()


# ── fetch_art ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_art_empty_video_id_returns_none() -> None:
    sess = MagicMock()
    result = await fetch_art(sess, "", None)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_art_argb_cache_hit_no_http() -> None:
    """ARGB cache hit: fetch_art must not make any HTTP call."""
    from PIL import Image

    vid = "cached_vid"
    w, h = 8, 8
    # Build valid ARGB data (8x8 opaque red)
    argb = bytes([0xFF, 0xFF, 0x00, 0x00]) * (w * h)
    _save_argb_cache(vid, w, h, argb)

    # Also write the .img so it looks like a valid cached file
    img = Image.new("RGB", (w, h), (255, 0, 0))
    img_path = art_mod.CACHE_DIR / f"{vid}.img"
    img.save(img_path, format="PNG")

    sess = MagicMock()
    result = await fetch_art(sess, vid, None)

    assert result == img_path
    sess.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_art_network_fetch_on_cache_miss() -> None:
    """No cache → fetch from network, write .img and ARGB sidecar."""
    from PIL import Image

    vid = "fresh_vid"
    # Build a minimal real PNG
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 200, 100)).save(buf, format="PNG")
    png_bytes = buf.getvalue()

    resp = MagicMock()
    resp.status = 200
    resp.read = AsyncMock(return_value=png_bytes)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    sess = MagicMock()
    sess.get = MagicMock(return_value=cm)

    result = await fetch_art(sess, vid, None)

    assert result is not None
    assert result.exists()
    assert result.suffix == ".img"
    # ARGB sidecar must have been written too
    assert (art_mod.CACHE_DIR / f"art_{vid}.argb").exists()


@pytest.mark.asyncio
async def test_fetch_art_network_failure_returns_none() -> None:
    import aiohttp

    vid = "fail_vid"
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("boom"))
    cm.__aexit__ = AsyncMock(return_value=False)
    sess = MagicMock()
    sess.get = MagicMock(return_value=cm)

    result = await fetch_art(sess, vid, "http://example.com/art.jpg")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_art_non_200_continues_to_fallback() -> None:
    """A 404 on image_src should fall through to the YouTube thumbnail URL."""
    from PIL import Image

    vid = "partial_vid"
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (0, 0, 255)).save(buf, format="PNG")
    png_bytes = buf.getvalue()

    not_found_resp = MagicMock()
    not_found_resp.status = 404
    ok_resp = MagicMock()
    ok_resp.status = 200
    ok_resp.read = AsyncMock(return_value=png_bytes)

    # First call (image_src) → 404; second call (youtube thumb) → 200
    call_count = 0

    def _get(url: str, **_kw: object) -> MagicMock:
        nonlocal call_count
        resp = not_found_resp if call_count == 0 else ok_resp
        call_count += 1
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    sess = MagicMock()
    sess.get = MagicMock(side_effect=_get)

    result = await fetch_art(sess, vid, "http://custom-art/pic.jpg")
    assert result is not None


# ── _build_png_from_argb ──────────────────────────────────────────────────────


def test_build_png_from_argb_creates_img_file() -> None:
    vid = "rebuild_vid"
    w, h = 2, 2
    # 4 pixels of valid ARGB data (A, R, G, B per pixel)
    argb = bytes([0xFF, 0x10, 0x20, 0x30]) * (w * h)
    result = _build_png_from_argb(vid, w, h, argb)
    assert result is not None
    assert result.exists()
    assert result.stat().st_size > 0


def test_build_png_from_argb_invalid_data_returns_none() -> None:
    # Wrong byte count for dimensions → Image.frombytes raises
    result = _build_png_from_argb("bad_vid", 100, 100, b"\x00" * 4)
    assert result is None


# ── fetch_art: ARGB cache hit, .img absent → rebuild ─────────────────────────


@pytest.mark.asyncio
async def test_fetch_art_argb_cache_hit_img_missing_rebuilds() -> None:
    """ARGB cache hit but no .img file yet → _build_png_from_argb is called."""
    vid = "rebuild_fetch_vid"
    w, h = 4, 4
    argb = bytes([0xFF, 0x80, 0x80, 0x80]) * (w * h)
    _save_argb_cache(vid, w, h, argb)
    # Do NOT create the .img file

    sess = MagicMock()
    result = await fetch_art(sess, vid, None)

    assert result is not None
    assert result.exists()
    sess.get.assert_not_called()


# ── fetch_art: legacy .img (no ARGB sidecar) ─────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_art_legacy_img_file_returned_directly() -> None:
    """Pre-existing .img without ARGB sidecar is returned without HTTP."""
    from PIL import Image

    vid = "legacy_vid"
    img_path = art_mod.CACHE_DIR / f"{vid}.img"
    Image.new("RGB", (8, 8), (100, 100, 100)).save(img_path, format="PNG")

    sess = MagicMock()
    result = await fetch_art(sess, vid, None)

    assert result == img_path
    sess.get.assert_not_called()
