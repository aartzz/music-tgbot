"""
URL classification + resolution for multi-source music bot.

classify_url(url) → (source, kind, id) | None
resolve(url)      → ResolvedItem                  (implemented in Phase 4)

Supported sources: tidal, youtube, spotify, apple, deezer, ytmusic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from . import odesli, tidal
from .downloader import make_ydl_opts, yt_extract

log = logging.getLogger(__name__)

# ---- URL patterns -----------------------------------------------------------

# Tidal: numeric track/album id, UUID playlist. Accepts tidal.com, listen.tidal.com,
# optional /browse/ prefix.
_TIDAL_TRACK = re.compile(
    r"(?:https?://)?(?:www\.|listen\.)?tidal\.com/(?:browse/)?track/(\d+)",
    re.IGNORECASE,
)
_TIDAL_ALBUM = re.compile(
    r"(?:https?://)?(?:www\.|listen\.)?tidal\.com/(?:browse/)?album/(\d+)",
    re.IGNORECASE,
)
_TIDAL_PLAYLIST = re.compile(
    r"(?:https?://)?(?:www\.|listen\.)?tidal\.com/(?:browse/)?playlist/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

# YouTube: 11-char video id (youtube.com/watch?v=, youtu.be/, /embed/, /v/, m.youtube.com, music.youtube.com).
_YT_VIDEO = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)"
    r"/(?:watch\?(?:[^#]*&)?v=|embed/|v/|shorts/)?([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_YT_PLAYLIST = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)"
    r".*?[?&]list=([A-Za-z0-9_-]{13,})",
    re.IGNORECASE,
)

# Spotify: open.spotify.com/track|album|playlist/<22-char base62> OR spotify:track:<id> URI.
_SPOTIFY = re.compile(
    r"(?:https?://)?open\.spotify\.com/(?:intl-[a-z]{2}/)?"
    r"(track|album|playlist)/([A-Za-z0-9]{22})",
    re.IGNORECASE,
)
_SPOTIFY_URI = re.compile(
    r"spotify:(track|album|playlist):([A-Za-z0-9]{22})",
    re.IGNORECASE,
)

# Apple Music: music.apple.com/<storefront>/(album|playlist)/<slug>/<id>[?i=<trackId>]
# Track form: ?i=<numeric trackId> on an album URL → classify as track.
_APPLE_WITH_I = re.compile(
    r"(?:https?://)?music\.apple\.com/[a-z]{2}/(?:album|playlist)/[^/]+/\d+\?[^#]*?[?&]?i=(\d+)",
    re.IGNORECASE,
)
_APPLE_ALBUM = re.compile(
    r"(?:https?://)?music\.apple\.com/[a-z]{2}/album/[^/]+/(\d+)",
    re.IGNORECASE,
)
_APPLE_PLAYLIST = re.compile(
    r"(?:https?://)?music\.apple\.com/[a-z]{2}/playlist/[^/]+/(pl\.[A-Za-z0-9]+)",
    re.IGNORECASE,
)

# Deezer: deezer.com/(en/|xx/)?(track|album|playlist)/<numeric>
_DEEZER = re.compile(
    r"(?:https?://)?(?:www\.)?deezer\.com/(?:[a-z]{2}/)?(track|album|playlist)/(\d+)",
    re.IGNORECASE,
)

# SoundCloud: soundcloud.com/<user>/<track> or soundcloud.com/<user>/sets/<playlist>
_SC_TRACK = re.compile(
    r"(?:https?://)?(?:www\.)?soundcloud\.com/([\w-]+)/([\w-]+)(?:\?.*)?$",
    re.IGNORECASE,
)
_SC_PLAYLIST = re.compile(
    r"(?:https?://)?(?:www\.)?soundcloud\.com/([\w-]+)/sets/([\w-]+)",
    re.IGNORECASE,
)

# Odesli-only platforms: domains we recognise but don't download from directly.
# Odesli resolves these to Tidal/YouTube links. Grouped by domain.
_ODESLI_DOMAINS: dict[str, str] = {
    "music.amazon": "amazonMusic",
    "amazon.com/music": "amazonMusic",
    "play.google": "google",
    "napster.com": "napster",
    "music.yandex": "yandex",
    "audius.co": "audius",
    "anghami.com": "anghami",
    "boomplay.com": "boomplay",
    "audiomack.com": "audiomack",
    "bandcamp.com": "bandcamp",
    "spinrilla.com": "spinrilla",
    "pandora.com": "pandora",
    "itunes.apple.com": "itunes",
}


# ---- Public dataclass -------------------------------------------------------


@dataclass
class ResolvedItem:
    source: str  # 'tidal' | 'youtube'
    kind: str  # 'track' | 'album' | 'playlist'
    id: str  # numeric, UUID, or 11-char YT
    cache_key: str  # f"{source}:{id}"
    title: str = ""  # for track; for album/playlist set from items
    artist: str = ""
    album: Optional[str] = None
    cover_url: Optional[str] = None
    duration: Optional[int] = None
    stream_url: Optional[str] = None  # Tidal tracks only, populated lazily
    codec: str = "mp3"  # 'flac' | 'mp3'
    filesize_estimate: Optional[int] = None
    items: Optional[list] = None  # for album/playlist: list[ResolvedItem]
    original_url: str = ""
    isrc: Optional[str] = None  # International Standard Recording Code for lyrics matching


# ---- classify_url -----------------------------------------------------------


def classify_url(url: str) -> Optional[Tuple[str, str, str]]:
    """
    Match a URL against known patterns. Returns (source, kind, id) or None.

    Order matters: Apple `?i=` (track-on-album) checked before bare album pattern,
    YouTube playlist `list=` checked before bare video to preserve playlist kind
    when both are present.
    """
    if not url:
        return None

    # Tidal
    if m := _TIDAL_TRACK.search(url):
        return ("tidal", "track", m.group(1))
    if m := _TIDAL_ALBUM.search(url):
        return ("tidal", "album", m.group(1))
    if m := _TIDAL_PLAYLIST.search(url):
        return ("tidal", "playlist", m.group(1))

    # Spotify
    if m := _SPOTIFY.search(url):
        return ("spotify", m.group(1).lower(), m.group(2))
    if m := _SPOTIFY_URI.search(url):
        return ("spotify", m.group(1).lower(), m.group(2))

    # Apple — order: ?i= first (track), then album, then playlist.
    if m := _APPLE_WITH_I.search(url):
        return ("apple", "track", m.group(1))
    if m := _APPLE_ALBUM.search(url):
        return ("apple", "album", m.group(1))
    if m := _APPLE_PLAYLIST.search(url):
        return ("apple", "playlist", m.group(1))

    # Deezer
    if m := _DEEZER.search(url):
        return ("deezer", m.group(1).lower(), m.group(2))

    # SoundCloud — playlist (sets/) first, then track.
    if m := _SC_PLAYLIST.search(url):
        return ("soundcloud", "playlist", m.group(2))
    if m := _SC_TRACK.search(url):
        # Filter out non-track pages (e.g. /user/likes, /user/tracks)
        slug = m.group(2).lower()
        if slug not in ("likes", "tracks", "reposts", "followers", "following"):
            return ("soundcloud", "track", m.group(2))

    # Odesli-only platforms (amazon, yandex, audius, anghami, boomplay, audiomack,
    # bandcamp, spinrilla, pandora, napster, google, itunes).
    # These are recognised by domain but resolved via Odesli → Tidal/YouTube.
    url_lower = url.lower()
    for domain, platform_key in _ODESLI_DOMAINS.items():
        if domain in url_lower:
            # Kind is hard to determine from URL alone for these platforms;
            # Odesli will resolve it. Default to 'track'.
            return ("odesli", "track", platform_key)

    # YouTube — playlist first (list= param), then video id.
    if m := _YT_PLAYLIST.search(url):
        return ("youtube", "playlist", m.group(1))
    if m := _YT_VIDEO.search(url):
        return ("youtube", "track", m.group(1))

    return None


# ---- resolve ----------------------------------------------------------------


def _tidal_track_from_info(info: dict, original_url: str = "") -> ResolvedItem:
    """Build a ResolvedItem for a single Tidal track from hifi-api /info/ data.

    stream_url is intentionally left None — it's short-lived and fetched
    lazily at download time by handlers.
    """
    album = info.get("album") or {}
    artist = info.get("artist") or {}
    artists = info.get("artists") or []

    # Multi-artist join (order-preserving)
    if artists:
        artist_name = ", ".join(a.get("name", "") for a in artists if a.get("name"))
    else:
        artist_name = artist.get("name", "")

    # Version suffix (e.g. "Remastered 2009") merged into title
    title = info.get("title", "")
    version = info.get("version")
    if version:
        title = f"{title} ({version})"

    cover_uuid = album.get("cover") or info.get("cover")
    return ResolvedItem(
        source="tidal",
        kind="track",
        id=str(info["id"]),
        cache_key=f"tidal:{info['id']}",
        title=title,
        artist=artist_name,
        album=album.get("title"),
        cover_url=tidal.cover_url(cover_uuid) if cover_uuid else None,
        duration=info.get("duration"),
        stream_url=None,
        codec="flac",
        original_url=original_url,
        isrc=info.get("isrc"),
    )


def _youtube_from_info(info: dict, original_url: str = "") -> ResolvedItem:
    """Build a ResolvedItem for a YouTube track from yt-dlp extract_info output."""
    # yt-dlp puts artist in 'artist' or 'uploader' depending on video metadata
    artist = info.get("artist") or info.get("uploader") or ""
    return ResolvedItem(
        source="youtube",
        kind="track",
        id=info["id"],
        cache_key=f"youtube:{info['id']}",
        title=info.get("title", ""),
        artist=artist,
        album=info.get("album"),
        cover_url=info.get("thumbnail"),
        duration=info.get("duration"),
        stream_url=None,
        codec="mp3",
        filesize_estimate=info.get("filesize") or info.get("filesize_approx"),
        original_url=original_url,
    )


async def resolve(url: str) -> Optional[ResolvedItem]:
    """
    Resolve any supported URL to a ResolvedItem.

    Flow:
      - tidal track → hifi-api /info/
      - tidal album/playlist → hifi-api /album/ or /playlist/, items = list[ResolvedItem]
      - youtube → yt-dlp extract_info(download=False)
      - spotify/apple/deezer → odesli.get_links → recurse into tidal URL (preferred)
        or youtube URL (fallback)
      - unrecognized → None
    """
    classified = classify_url(url)
    if not classified:
        return None
    source, kind, item_id = classified

    # --- Tidal direct ---
    if source == "tidal":
        if kind == "track":
            info = await tidal.get_track_info(item_id)
            return _tidal_track_from_info(info, original_url=url)
        if kind == "album":
            data = await tidal.get_album(item_id)
            items = [
                _tidal_track_from_info(t, original_url=f"https://tidal.com/track/{t['id']}")
                for t in (data.get("items") or [])
                if t and t.get("id")
            ]
            return ResolvedItem(
                source="tidal",
                kind="album",
                id=item_id,
                cache_key=f"tidal:album:{item_id}",
                title=data.get("title", ""),
                artist=(data.get("artist") or {}).get("name", ""),
                cover_url=tidal.cover_url(data["cover"]) if data.get("cover") else None,
                codec="flac",
                items=items,
                original_url=url,
            )
        if kind == "playlist":
            data = await tidal.get_playlist(item_id)
            items = [
                _tidal_track_from_info(t, original_url=f"https://tidal.com/track/{t['id']}")
                for t in (data.get("items") or [])
                if t and t.get("id")
            ]
            return ResolvedItem(
                source="tidal",
                kind="playlist",
                id=item_id,
                cache_key=f"tidal:playlist:{item_id}",
                title=data.get("title", ""),
                artist=(data.get("creator") or {}).get("name", ""),
                codec="flac",
                items=items,
                original_url=url,
            )

    # --- YouTube → Odesli first (Tidal priority for ALL YouTube links, not just YT Music) ---
    if source == "youtube" and kind == "track":
        links = await odesli.get_links(url)
        tidal_link = (links.get("tidal") or {}).get("url")
        if tidal_link:
            log.info("odesli youtube %s → tidal: %s", url, tidal_link)
            resolved = await resolve(tidal_link)
            if resolved is not None:
                resolved.original_url = url
                return resolved
        if not tidal_link:
            log.info("odesli youtube %s → no tidal mapping, using yt-dlp", url)
        # No Tidal on Odesli → fall through to yt-dlp below

    # --- YouTube / SoundCloud direct (yt-dlp) ---
    if source in ("youtube", "soundcloud"):
        # download=False → metadata only. Uses same ydl opts (no progress hook since no id yet).
        info = await yt_extract(url, make_ydl_opts(), download=False)
        if not info:
            return None
        # Playlist: info has 'entries' list
        if kind == "playlist" or info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
            items = [_youtube_from_info(e, original_url=e.get("webpage_url") or e.get("url") or url) for e in entries]
            return ResolvedItem(
                source=source,
                kind="playlist",
                id=item_id,
                cache_key=f"{source}:playlist:{item_id}",
                title=info.get("title", ""),
                artist="",
                codec="mp3",
                items=items,
                original_url=url,
            )
        item = _youtube_from_info(info, original_url=url)
        item.source = source  # preserve 'soundcloud' not 'youtube'
        item.cache_key = f"{source}:{item.id}"
        return item

    # --- Spotify / Apple / Deezer / Odesli-only → Odesli ---
    if source in ("spotify", "apple", "deezer", "odesli"):
        links = await odesli.get_links(url)
        if not links:
            log.warning("odesli returned no links for %s", url)
            return None

        # Prefer Tidal mapping
        tidal_link = (links.get("tidal") or {}).get("url")
        if tidal_link:
            log.info("odesli %s → tidal: %s", url, tidal_link)
            resolved = await resolve(tidal_link)
            if resolved is not None:
                resolved.original_url = url
                return resolved

        # Fallback to YouTube (prefer music.youtube.com for cleaner artist/title)
        yt_link = (links.get("youtubeMusic") or {}).get("url") or (
            links.get("youtube") or {}
        ).get("url")
        if yt_link:
            log.info("odesli %s → youtube fallback: %s", url, yt_link)
            resolved = await resolve(yt_link)
            if resolved is not None:
                resolved.original_url = url
                return resolved

        # Last resort: try the original URL directly with yt-dlp.
        # Works for platforms like Bandcamp, Audiomack that yt-dlp supports natively.
        log.info("odesli gave no tidal/youtube for %s, trying original URL via yt-dlp", url)
        try:
            info = await yt_extract(url, make_ydl_opts(), download=False)
            if info and info.get("id"):
                item = _youtube_from_info(info, original_url=url)
                # Mark source as the Odesli platform key (e.g. 'bandcamp')
                item.source = source if source == "odesli" else source
                item.cache_key = f"{source}:{item.id}"
                return item
        except Exception as e:
            log.debug("yt-dlp fallback for %s failed: %s", url, e)

        log.warning("odesli gave no tidal/youtube mapping for %s", url)
        return None

    return None
