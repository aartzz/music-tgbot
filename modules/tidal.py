"""
Tidal hifi-api client with dynamic instance rotation.

On startup, `init_instances()` fetches live instance lists from a public uptime
worker. Each request rotates through healthy instances and falls through on
5xx/timeout/network failure. Stream URLs are resolved from the Tidal bts
manifest (base64-encoded JSON) or DASH manifest (base64-encoded XML MPD).

Public API:
    init_instances()         - populate internal instance lists (call once at startup)
    get_track_info(id)       - track metadata dict (title/artist/album/cover/...)
    get_stream_url(id, q)    - signed FLAC stream URL (BTS manifests only, backward compat)
    get_stream_info(id, q)   - structured StreamInfo for both BTS and DASH manifests
    get_album(id)            - album info + items (tracks)
    get_playlist(uuid)       - playlist info + items (tracks)
    cover_url(uuid)          - build 1280x1280 cover URL from Tidal cover UUID

Design notes:
    - Instances are re-sorted by semantic version (desc) so newer APIs win.
    - A single successful response ends the rotation loop.
    - Failures are DEBUG-logged; the caller only sees the final
      ``RuntimeError('tidal exhausted')`` when every instance failed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

log = logging.getLogger(__name__)

_UPTIME_PRIMARY = "https://tidal-uptime.jiffy-puffs-1j.workers.dev/"
_UPTIME_FALLBACK = "https://tidal-uptime.props-76styles.workers.dev/"
_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Populated by init_instances(). Module-private; callers use the helper funcs.
_api_instances: List[str] = []
_streaming_instances: List[str] = []


def _version_key(ver: str) -> tuple:
    """Parse '2.9' / '2.10.1' into a comparable tuple; unknowns sort last."""
    try:
        return tuple(int(x) for x in ver.split("."))
    except (ValueError, AttributeError):
        return (0,)


async def init_instances() -> None:
    """Fetch live Tidal instance lists and cache them in module state.

    Idempotent: safe to call multiple times (overwrites previous lists).
    On total failure (both uptime workers unreachable) leaves lists empty;
    subsequent API calls will raise RuntimeError.
    """
    data: Optional[Dict[str, Any]] = None
    for endpoint in (_UPTIME_PRIMARY, _UPTIME_FALLBACK):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, timeout=_TIMEOUT) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        break
                    log.warning("tidal uptime %s returned %d", endpoint, resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("tidal uptime %s failed: %s", endpoint, e)

    # Mutate module-level lists in-place so `from modules.tidal import _api_instances`
    # in tests sees the populated state (re-binding with `=` would leave import aliases stale).
    _api_instances.clear()
    _streaming_instances.clear()

    if not data:
        log.error("tidal: no uptime data, instance lists empty")
        return

    api_list = sorted(
        data.get("api", []),
        key=lambda x: _version_key(x.get("version", "")),
        reverse=True,
    )
    stream_list = sorted(
        data.get("streaming", []),
        key=lambda x: _version_key(x.get("version", "")),
        reverse=True,
    )
    _api_instances.extend(i["url"].rstrip("/") for i in api_list if i.get("url"))
    _streaming_instances.extend(i["url"].rstrip("/") for i in stream_list if i.get("url"))

    log.info("tidal: %d api instances, %d streaming", len(_api_instances), len(_streaming_instances))


async def _request(instances: List[str], path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Rotate through `instances`, first 2xx response wins. Raises on total failure."""
    if not instances:
        raise RuntimeError("tidal: no instances loaded (call init_instances first)")

    last_err: Optional[str] = None
    async with aiohttp.ClientSession() as session:
        for base in instances:
            try:
                async with session.get(f"{base}{path}", params=params, timeout=_TIMEOUT) as resp:
                    if resp.status == 200:
                        body = await resp.json(content_type=None)
                        return body
                    if resp.status == 404:
                        # Legit "not found" — no reason to rotate, the other instances
                        # will give the same verdict.
                        body = await resp.json(content_type=None)
                        raise RuntimeError(f"tidal 404: {body}")
                    last_err = f"{base} -> {resp.status}"
                    log.debug("tidal %s", last_err)
            except RuntimeError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = f"{base}: {e}"
                log.debug("tidal %s", last_err)

    raise RuntimeError(f"tidal exhausted (last: {last_err})")


async def get_track_info(track_id: str) -> Dict[str, Any]:
    """Return the `data` block of /info/ (title/artist/album/duration/...)."""
    body = await _request(_api_instances, "/info/", {"id": track_id})
    return body.get("data", {}) or {}


# ---- Stream resolution (BTS + DASH) ------------------------------------------

@dataclass
class StreamInfo:
    """Structured result from get_stream_info()."""
    type: str             # "bts" (direct URL) or "dash" (MPD XML for ffmpeg)
    url: Optional[str]    # direct stream URL (bts only)
    mpd_xml: Optional[str]  # raw MPD XML string (dash only)
    quality: str          # actual quality that succeeded
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    size_estimate: Optional[int] = None  # estimated bytes (from MPD bandwidth/duration or Content-Length)


def _parse_mpd_size(mpd_xml: str) -> Optional[int]:
    """Estimate file size from MPD bandwidth × duration (bits → bytes)."""
    try:
        bandwidth = int(re.search(r'bandwidth="(\d+)"', mpd_xml).group(1))
        dur_match = re.search(r'mediaPresentationDuration="PT(?:(\d+)H)?(?:(\d+)M)?([\d.]+)S"', mpd_xml)
        if dur_match:
            h = int(dur_match.group(1) or 0)
            m = int(dur_match.group(2) or 0)
            s = float(dur_match.group(3) or 0)
            total_s = h * 3600 + m * 60 + s
            return int(bandwidth * total_s / 8)
    except (AttributeError, ValueError):
        pass
    return None


async def get_stream_info(track_id: str, quality: str = "HI_RES_LOSSLESS") -> StreamInfo:
    """Resolve a track's stream info, handling both BTS and DASH manifests.

    Tries the requested quality first. For BTS manifests (LOSSLESS), returns
    a direct stream URL. For DASH manifests (HI_RES_LOSSLESS on free instances
    that support it), returns the raw MPD XML for ffmpeg-based download.
    Falls back to LOSSLESS if the requested quality fails.
    """
    for q in (quality, "LOSSLESS"):
        if q != quality:
            log.info("tidal: retrying track %s with quality=%s", track_id, q)
        body = await _request(_streaming_instances, "/track/", {"id": track_id, "quality": q})
        data = body.get("data", {}) or {}

        mime = data.get("manifestMimeType", "")
        mani_b64 = data.get("manifest")
        if not mani_b64:
            if q == quality:
                continue
            raise RuntimeError(f"tidal: no manifest (quality={q}, mime={mime})")

        # --- DASH manifest (base64 XML → MPD) ---
        if "dash" in mime.lower():
            try:
                mpd_xml = base64.b64decode(mani_b64).decode("utf-8")
            except Exception as e:
                log.warning("tidal: DASH manifest decode failed for track %s quality=%s: %s", track_id, q, e)
                if q == quality:
                    continue
                raise RuntimeError(f"tidal: DASH manifest decode failed: {e}")

            size_est = _parse_mpd_size(mpd_xml)
            return StreamInfo(
                type="dash",
                url=None,
                mpd_xml=mpd_xml,
                quality=q,
                bit_depth=data.get("bitDepth"),
                sample_rate=data.get("sampleRate"),
                size_estimate=size_est,
            )

        # --- BTS manifest (base64 JSON → direct URL) ---
        try:
            manifest = json.loads(base64.b64decode(mani_b64))
        except (ValueError, json.JSONDecodeError) as e:
            log.warning("tidal: manifest decode failed for track %s quality=%s: %s", track_id, q, e)
            if q == quality:
                continue
            raise RuntimeError(f"tidal: manifest decode failed: {e}")

        urls = manifest.get("urls") or manifest.get("Urls") or []
        if not urls:
            if q == quality:
                continue
            raise RuntimeError(f"tidal: manifest has no urls (keys={list(manifest.keys())})")

        return StreamInfo(
            type="bts",
            url=urls[0],
            mpd_xml=None,
            quality=q,
            bit_depth=data.get("bitDepth"),
            sample_rate=data.get("sampleRate"),
        )

    raise RuntimeError(f"tidal: no usable stream for track {track_id}")


async def get_album(album_id: str, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """Return /album/ payload with items unwrapped.

    Album items are wrapped ``{item: track, type, cut}`` — same as playlists.
    We flatten them to bare track dicts so callers don't need to branch.
    """
    body = await _request(_api_instances, "/album/", {"id": album_id, "limit": limit, "offset": offset})
    data = body.get("data", {}) or {}
    raw_items = data.get("items", []) or []
    # Each entry may be {item: track, type, cut} or already flat (version-dependent).
    items = [entry.get("item", entry) if isinstance(entry, dict) else entry for entry in raw_items]
    data["items"] = items
    return data


async def get_playlist(playlist_uuid: str, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """Return /playlist/ payload.

    Playlist endpoint has a different shape than /album/: the wrapper is
    ``{version, playlist: {meta}, items: [{item: track, type, cut}, ...]}``
    (items live at top level, not inside ``data``). We unwrap the items to
    match get_album's flat ``items`` convention so callers don't branch.
    """
    body = await _request(_api_instances, "/playlist/", {"id": playlist_uuid, "limit": limit, "offset": offset})
    meta = body.get("playlist", {}) or {}
    raw_items = body.get("items", []) or []
    # Each entry is {item: track, type, cut}; flatten to just the track dicts.
    items = [entry.get("item", entry) for entry in raw_items if isinstance(entry, dict)]
    return {**meta, "items": items}


def cover_url(uuid: str) -> str:
    """Build Tidal's 1280x1280 CDN URL from a cover UUID.

    Tidal stores covers under a slash-delimited path built from the UUID, e.g.::

        'abcd1234-5678-90ab-cdef-1234567890ab'
         -> https://resources.tidal.com/images/abcd1234/5678/90ab/cdef/1234567890ab/1280x1280.jpg
    """
    if not uuid:
        return ""
    return f"https://resources.tidal.com/images/{uuid.replace('-', '/')}/1280x1280.jpg"


async def search(query: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Search Tidal catalog. Returns list of track dicts with full metadata."""
    body = await _request(_api_instances, "/search/", {"s": query, "limit": str(limit), "offset": str(offset)})
    data = body.get("data") or {}
    # Shape varies by version: some have data.items, others data is the list
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []
    return items


async def get_lyrics(track_id: str) -> str:
    """Return plain-text lyrics from Tidal (no timestamps). Empty string on failure."""
    try:
        body = await _request(_api_instances, "/lyrics/", {"id": track_id})
        return (body.get("lyrics") or {}).get("lyrics", "") or ""
    except Exception:
        return ""
