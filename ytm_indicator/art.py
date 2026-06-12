"""Album art fetch + on-disk cache (PNG download + ARGB persistent store)."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ytm-indicator"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

YOUTUBE_THUMB = "https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
MAX_ART_BYTES = 5 * 1024 * 1024  # 5 MB sanity cap
CACHE_TTL_S = 7 * 24 * 3600  # 7 days


def evict_old_cache() -> None:
    """Remove ARGB cache entries older than CACHE_TTL_S.

    Called once at startup so stale art from old video IDs doesn't
    accumulate indefinitely.
    """
    cutoff = time.time() - CACHE_TTL_S
    try:
        for argb_path in CACHE_DIR.glob("art_*.argb"):
            try:
                if os.path.getmtime(argb_path) < cutoff:
                    argb_path.unlink(missing_ok=True)
                    argb_path.with_suffix(".meta").unlink(missing_ok=True)
                    log.debug("evicted old art cache: %s", argb_path.name)
            except OSError as e:
                log.debug("evict: could not remove %s: %s", argb_path, e)
    except OSError as e:
        log.debug("evict: cache dir scan failed: %s", e)


def _argb_cache_path(video_id: str) -> Path:
    return CACHE_DIR / f"art_{video_id}.argb"


def _meta_cache_path(video_id: str) -> Path:
    return CACHE_DIR / f"art_{video_id}.meta"


def _load_argb_cache(video_id: str) -> tuple[int, int, bytes] | None:
    """Return (width, height, argb_bytes) from disk cache, or None on miss."""
    argb = _argb_cache_path(video_id)
    meta = _meta_cache_path(video_id)
    if not argb.exists() or not meta.exists():
        return None
    try:
        w, h = (int(x) for x in meta.read_text().strip().split())
        data = argb.read_bytes()
        if len(data) != w * h * 4:
            log.debug("art cache corrupt for %s; ignoring", video_id)
            return None
        return w, h, data
    except (OSError, ValueError) as e:
        log.debug("art cache load failed for %s: %s", video_id, e)
        return None


def _save_argb_cache(video_id: str, width: int, height: int, argb_data: bytes) -> None:
    """Persist ARGB bytes and dimensions sidecar to disk cache."""
    try:
        _argb_cache_path(video_id).write_bytes(argb_data)
        _meta_cache_path(video_id).write_text(f"{width} {height}\n")
        log.debug("persisted ARGB cache for %s (%dx%d)", video_id, width, height)
    except OSError as e:
        log.debug("art cache write failed for %s: %s", video_id, e)


def _build_png_from_argb(video_id: str, width: int, height: int, argb_data: bytes) -> Path | None:
    """Reconstruct a PNG from ARGB cache bytes and write it as the .img file."""
    from PIL import Image

    try:
        # ARGB → RGBA channel swap for PIL
        raw = bytearray(len(argb_data))
        for i in range(0, len(argb_data), 4):
            raw[i] = argb_data[i + 1]  # R
            raw[i + 1] = argb_data[i + 2]  # G
            raw[i + 2] = argb_data[i + 3]  # B
            raw[i + 3] = argb_data[i]  # A
        img = Image.frombytes("RGBA", (width, height), bytes(raw))
        dest = CACHE_DIR / f"{video_id}.img"
        img.save(dest, format="PNG")
        return dest
    except Exception as e:
        log.debug("failed to rebuild PNG from ARGB cache for %s: %s", video_id, e)
        return None


def _rgba_to_argb(rgba: bytes) -> bytes:
    """Convert RGBA byte sequence to ARGB32 for SNI / disk cache."""
    out = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        out[i] = rgba[i + 3]  # A
        out[i + 1] = rgba[i]  # R
        out[i + 2] = rgba[i + 1]  # G
        out[i + 3] = rgba[i + 2]  # B
    return bytes(out)


async def fetch_art(
    session: aiohttp.ClientSession,
    video_id: str,
    image_src: str | None,
) -> Path | None:
    """Return a path to album art PNG for `video_id`.

    Check order:
    1. ARGB persistent cache (validated by video_id) → rebuild PNG.
    2. Existing .img file (backward-compat / same session).
    3. Fetch from network → decode + cache ARGB → write .img.
    Returns None on failure.
    """
    if not video_id:
        return None

    # 1. ARGB persistent cache hit.
    cached = _load_argb_cache(video_id)
    if cached is not None:
        w, h, argb_data = cached
        dest = CACHE_DIR / f"{video_id}.img"
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        return _build_png_from_argb(video_id, w, h, argb_data)

    # 2. Legacy .img file (prior session, no ARGB sidecar).
    dest = CACHE_DIR / f"{video_id}.img"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    # 3. Network fetch.
    urls = [u for u in (image_src, YOUTUBE_THUMB.format(video_id=video_id)) if u]
    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.debug("art fetch %s -> HTTP %d", url, resp.status)
                    continue
                body = await resp.read()
                if len(body) > MAX_ART_BYTES:
                    log.warning("art for %s too large (%d bytes); skipping", video_id, len(body))
                    continue
                dest.write_bytes(body)
                log.info("cached art for %s from %s (%d bytes)", video_id, url, len(body))
                _persist_argb_from_png(video_id, dest)
                return dest
        except (aiohttp.ClientError, TimeoutError) as e:
            log.debug("art fetch %s failed: %s", url, e)
            continue
    return None


def _persist_argb_from_png(video_id: str, png_path: Path) -> None:
    """Decode PNG → ARGB and write persistent sidecar cache."""
    from PIL import Image

    try:
        with Image.open(png_path) as im:
            im = im.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
            argb = _rgba_to_argb(im.tobytes())
            _save_argb_cache(video_id, im.width, im.height, argb)
    except Exception as e:
        log.debug("ARGB persist failed for %s: %s", video_id, e)
