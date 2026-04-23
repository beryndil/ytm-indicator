"""Album art fetch + on-disk cache."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ytm-indicator"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

YOUTUBE_THUMB = "https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
MAX_ART_BYTES = 5 * 1024 * 1024  # 5 MB sanity cap


async def fetch_art(
    session: aiohttp.ClientSession,
    video_id: str,
    image_src: str | None,
) -> Path | None:
    """Download album art for `video_id`. Returns cached path or None on failure."""
    if not video_id:
        return None
    dest = CACHE_DIR / f"{video_id}.img"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

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
                return dest
        except (aiohttp.ClientError, TimeoutError) as e:
            log.debug("art fetch %s failed: %s", url, e)
            continue
    return None
