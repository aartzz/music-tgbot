"""
Odesli (song.link) client.

Primary: https://api.song.link/v1-alpha.1/links?url=<quoted>
Rate-limited globally to 10 req/min via aiolimiter. On 429 or exhausted limiter,
falls back through a rotating pool of proxy endpoints.

get_links(url) -> dict  (linksByPlatform, empty dict on total failure)
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any, Dict
from urllib.parse import quote

import aiohttp
from aiolimiter import AsyncLimiter

log = logging.getLogger(__name__)

_OFFICIAL = "https://api.song.link/v1-alpha.1/links?url="
_PROXIES = (
    "https://tidal.qqdl.site/api/songlink?url=",
    "https://tidal.squid.wtf/api/songlink?url=",
    "https://spotubedl.com/api/songlink?url=",
)

# 10 requests per 60s on the official endpoint (public limit is 10/min/IP).
_limiter = AsyncLimiter(10, 60)

# Round-robin across proxies to spread load.
_proxy_cycle = itertools.cycle(_PROXIES)
_proxy_lock = asyncio.Lock()

_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


async def _next_proxy() -> str:
    async with _proxy_lock:
        return next(_proxy_cycle)


def _extract_links(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize link container across Odesli variants.

    - Official api.song.link: `{linksByPlatform: {platform: {url, ...}}}`
    - spotubedl.com proxy: `{links: {platform: {url}}}`
    - qqdl/squid proxies: mirror official shape (`linksByPlatform`).

    Returns the platform→info dict (possibly empty).
    """
    if not data:
        return {}
    return data.get("linksByPlatform") or data.get("links") or {}


async def _fetch(session: aiohttp.ClientSession, endpoint: str, url: str) -> Dict[str, Any] | None:
    """Fetch links from a single endpoint. Return parsed JSON or None on failure."""
    full = endpoint + quote(url, safe="")
    try:
        async with session.get(full, timeout=_TIMEOUT) as resp:
            if resp.status == 429:
                log.debug("odesli 429 from %s", endpoint)
                return None
            if resp.status >= 400:
                log.debug("odesli %d from %s", resp.status, endpoint)
                return None
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.debug("odesli error from %s: %s", endpoint, e)
        return None


async def get_links(url: str) -> Dict[str, Any]:
    """
    Return a normalized `platform → {url, ...}` dict for the given music URL.
    Returns {} on total failure (never raises).
    """
    if not url:
        return {}

    async with aiohttp.ClientSession() as session:
        # Attempt 1: official endpoint under global limiter.
        # Non-blocking acquire: if limiter is exhausted, skip to proxies instead of stalling.
        acquired = False
        try:
            await asyncio.wait_for(_limiter.acquire(), timeout=0.1)
            acquired = True
        except asyncio.TimeoutError:
            acquired = False

        if acquired:
            data = await _fetch(session, _OFFICIAL, url)
            if data:
                links = _extract_links(data)
                if links:
                    return links

        # Attempt 2+: rotate through proxies, each tried once per call.
        tried: set[str] = set()
        for _ in range(len(_PROXIES)):
            endpoint = await _next_proxy()
            if endpoint in tried:
                continue
            tried.add(endpoint)
            data = await _fetch(session, endpoint, url)
            if data:
                links = _extract_links(data)
                if links:
                    return links

    return {}
