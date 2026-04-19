import os
import sqlite3
import datetime
import asyncio
import logging

log = logging.getLogger(__name__)

# Per-cache-key asyncio.Lock to prevent concurrent same-track downloads.
_cache_locks: dict[str, asyncio.Lock] = {}


def get_cache_lock(key: str) -> asyncio.Lock:
    """Return a singleton Lock for the given cache key."""
    if key not in _cache_locks:
        _cache_locks[key] = asyncio.Lock()
    return _cache_locks[key]


class Music:
    def createdb(self):
        con = sqlite3.connect(os.path.join('db','music.db'))
        cur = con.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS music(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT,
            file_id TEXT
            )
        ''')
        con.commit()

    def add_data(self, video_id, file_id):
        con = sqlite3.connect(os.path.join('db','music.db'))
        cur = con.cursor()
        cur.execute('INSERT INTO music(video_id, file_id) VALUES(?, ?)',
                    (video_id, file_id))
        con.commit()

    def remove_data(self, video_id):
        con = sqlite3.connect(os.path.join('db', 'music.db'))
        cur = con.cursor()
        cur.execute('DELETE FROM music WHERE video_id=?', (video_id,)).fetchone()
        con.commit()

    def get_file_id(self, video_id):
        """Look up a cached file_id by video_id.

        Supports legacy YouTube entries stored as raw 11-char IDs:
        if the prefixed key (e.g. 'youtube:dQw4w9WgXcQ') yields nothing
        and the key starts with 'youtube:', also try the raw tail.
        """
        con = sqlite3.connect(os.path.join('db','music.db'))
        cur = con.cursor()
        value = cur.execute('SELECT file_id FROM music WHERE video_id=?', (video_id,)).fetchone()
        if value:
            return value[0]

        # Legacy fallback: raw YouTube ID stored without prefix.
        if video_id.startswith("youtube:"):
            raw_id = video_id[len("youtube:"):]
            # Only attempt for 11-char YouTube-like IDs.
            if len(raw_id) == 11 and raw_id.isascii():
                value = cur.execute('SELECT file_id FROM music WHERE video_id=?', (raw_id,)).fetchone()
                if value:
                    log.info("legacy cache hit: %s → %s", video_id, raw_id)
                    return value[0]

        return None

class Analytics:
    def createdb(self):
        con = sqlite3.connect(os.path.join('db', 'analytics.db'))
        cur = con.cursor()
        cur.execute('''
                    CREATE TABLE IF NOT EXISTS users(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER
                    )
                ''')
        cur.execute('''
                    CREATE TABLE IF NOT EXISTS total_use_count(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    use_count INTEGER
                    )
                ''')
        cur.execute('INSERT INTO total_use_count(use_count) VALUES(?)', (0,))
        con.commit()

    def get_user_count(self):
        con = sqlite3.connect(os.path.join('db', 'analytics.db'))
        cur = con.cursor()
        return cur.execute('SELECT COUNT(*) FROM users').fetchone()[0]

    def add_user(self, user_id) -> bool:
        con = sqlite3.connect(os.path.join('db', 'analytics.db'))
        cur = con.cursor()
        if not cur.execute('SELECT user_id FROM users WHERE user_id=?', (user_id,)).fetchone():
            cur.execute('INSERT INTO users(user_id) VALUES(?)',(user_id,))
            con.commit()
            return True
        return False

    def get_total_use_count(self):
        con = sqlite3.connect(os.path.join('db', 'analytics.db'))
        cur = con.cursor()
        return cur.execute('SELECT use_count FROM total_use_count').fetchone()[0]

    def increment_use_count(self):
        con = sqlite3.connect(os.path.join('db', 'analytics.db'))
        cur = con.cursor()
        cur.execute('UPDATE total_use_count SET use_count=use_count+1 WHERE id=1').fetchone()
        con.commit()