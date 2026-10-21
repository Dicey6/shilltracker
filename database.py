"""Persistence layer. PostgreSQL (asyncpg) in production, SQLite (stdlib) for local dev/tests.

SQL is written once using $1-style placeholders; the SQLite backend rewrites them.
All timestamps are integer epoch seconds so both engines behave identically.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

SCHEMA_LOCK_KEY = 7_340_101      # serialises migrations across instances
SINGLETON_LOCK_KEY = 7_340_102   # only one instance may run Telethon at a time
_PG_PARAM = re.compile(r"\$\d+")

MIGRATION_TABLE = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INTEGER PRIMARY KEY, applied_at BIGINT NOT NULL)"
)

MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at BIGINT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS watchlist (user_id BIGINT PRIMARY KEY, username TEXT, "
        "display_name TEXT, added_at BIGINT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS detection_patterns (pid TEXT PRIMARY KEY, kind TEXT NOT NULL, "
        "pattern TEXT NOT NULL, created_at BIGINT NOT NULL, UNIQUE (kind, pattern))",
        "CREATE TABLE IF NOT EXISTS monitored_chats (chat_id BIGINT PRIMARY KEY, title TEXT, "
        "enabled INTEGER NOT NULL DEFAULT 1, updated_at BIGINT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS processed_messages (chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, "
        "sig TEXT NOT NULL, seen_at BIGINT NOT NULL, PRIMARY KEY (chat_id, message_id, sig))",
        "CREATE INDEX IF NOT EXISTS idx_processed_seen_at ON processed_messages (seen_at)",
        "CREATE TABLE IF NOT EXISTS operational_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at BIGINT NOT NULL)",
    ]),
]


def _now() -> int:
    return int(time.time())


def pattern_id(kind: str, pattern: str) -> str:
    norm = pattern.strip().lower() if kind == "keyword" else pattern.strip()
    return hashlib.sha1(f"{kind}:{norm}".encode()).hexdigest()[:10]


class Database:
    def __init__(self, url: str):
        self.url = url
        self.is_pg = url.startswith(("postgres://", "postgresql://"))
        self._pool = None
        self._lock_conn = None
        self._sqlite: Optional[sqlite3.Connection] = None
        self._sq_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        if self.is_pg:
            import asyncpg
            self._pool = await asyncpg.create_pool(self.url, min_size=1, max_size=5, command_timeout=30)
        else:
            path = self.url.split("sqlite:///", 1)[-1] or ":memory:"
            self._sqlite = sqlite3.connect(path, check_same_thread=False)
            self._sqlite.row_factory = sqlite3.Row

    async def close(self) -> None:
        if self._lock_conn is not None:
            try:
                await self._lock_conn.close()
            except Exception:
                pass
            self._lock_conn = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        if self._sqlite is not None:
            self._sqlite.close()
            self._sqlite = None

    # ------------------------------------------------------------------ primitives
    async def _sq(self, sql: str, args: tuple) -> list[dict]:
        sql = _PG_PARAM.sub("?", sql)

        def work() -> list[dict]:
            cur = self._sqlite.execute(sql, args)  # type: ignore[union-attr]
            rows = [dict(r) for r in cur.fetchall()]
            self._sqlite.commit()  # type: ignore[union-attr]
            return rows

        async with self._sq_lock:
            return await asyncio.to_thread(work)

    async def execute(self, sql: str, *args: Any) -> None:
        if self.is_pg:
            async with self._pool.acquire() as conn:
                await conn.execute(sql, *args)
        else:
            await self._sq(sql, args)

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        if self.is_pg:
            async with self._pool.acquire() as conn:
                return [dict(r) for r in await conn.fetch(sql, *args)]
        return await self._sq(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict]:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = await self.fetchrow(sql, *args)
        return next(iter(row.values())) if row else None

    async def ping(self) -> bool:
        try:
            return (await self.fetchval("SELECT 1")) == 1
        except Exception:
            return False

    # ------------------------------------------------------------------ schema
    async def init_schema(self) -> None:
        if self.is_pg:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK_KEY)
                    await conn.execute(MIGRATION_TABLE)
                    applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
                    for version, stmts in MIGRATIONS:
                        if version in applied:
                            continue
                        for stmt in stmts:
                            await conn.execute(stmt)
                        await conn.execute(
                            "INSERT INTO schema_migrations (version, applied_at) VALUES ($1, $2)", version, _now())
                        log.info("Applied migration %s", version)
        else:
            await self.execute(MIGRATION_TABLE)
            applied = {r["version"] for r in await self.fetch("SELECT version FROM schema_migrations")}
            for version, stmts in MIGRATIONS:
                if version in applied:
                    continue
                for stmt in stmts:
                    await self.execute(stmt)
                await self.execute("INSERT INTO schema_migrations (version, applied_at) VALUES ($1, $2)", version, _now())

    # ------------------------------------------------------------------ singleton lock
    async def try_singleton_lock(self) -> bool:
        """Postgres advisory lock so two overlapping Render instances never share one Telegram session."""
        if not self.is_pg:
            return True
        import asyncpg
        if self._lock_conn is None or self._lock_conn.is_closed():
            self._lock_conn = await asyncpg.connect(self.url, timeout=15)
        return bool(await self._lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", SINGLETON_LOCK_KEY))

    async def singleton_lock_alive(self) -> bool:
        if not self.is_pg:
            return True
        try:
            await self._lock_conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ settings / state
    async def get_setting(self, key: str) -> Optional[str]:
        return await self.fetchval("SELECT value FROM settings WHERE key = $1", key)

    async def get_all_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in await self.fetch("SELECT key, value FROM settings")}

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, $3) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
            key, value, _now())

    async def get_state(self, key: str) -> Optional[str]:
        return await self.fetchval("SELECT value FROM operational_state WHERE key = $1", key)

    async def set_state(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO operational_state (key, value, updated_at) VALUES ($1, $2, $3) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
            key, value, _now())

    async def delete_state(self, key: str) -> None:
        await self.execute("DELETE FROM operational_state WHERE key = $1", key)

    # ------------------------------------------------------------------ watchlist
    async def add_watch(self, user_id: int, username: Optional[str], display_name: Optional[str]) -> bool:
        """Returns True if newly added, False if an existing entry was refreshed."""
        existed = await self.fetchval("SELECT 1 FROM watchlist WHERE user_id = $1", user_id)
        await self.execute(
            "INSERT INTO watchlist (user_id, username, display_name, added_at) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id) DO UPDATE SET username = COALESCE(EXCLUDED.username, watchlist.username), "
            "display_name = COALESCE(EXCLUDED.display_name, watchlist.display_name)",
            user_id, (username or None), (display_name or None), _now())
        return not existed

    async def remove_watch(self, user_id: int) -> bool:
        existed = await self.fetchval("SELECT 1 FROM watchlist WHERE user_id = $1", user_id)
        await self.execute("DELETE FROM watchlist WHERE user_id = $1", user_id)
        return bool(existed)

    async def find_watch_by_username(self, username: str) -> Optional[dict]:
        return await self.fetchrow(
            "SELECT * FROM watchlist WHERE LOWER(username) = $1", username.lstrip("@").lower())

    async def list_watch(self) -> list[dict]:
        return await self.fetch("SELECT * FROM watchlist ORDER BY added_at, user_id")

    # ------------------------------------------------------------------ detection patterns
    async def add_pattern(self, kind: str, pattern: str) -> bool:
        pattern = pattern.strip()
        row = await self.fetchrow(
            "INSERT INTO detection_patterns (pid, kind, pattern, created_at) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT DO NOTHING RETURNING pid",
            pattern_id(kind, pattern), kind, pattern, _now())
        return row is not None

    async def remove_pattern(self, pid: str) -> bool:
        existed = await self.fetchval("SELECT 1 FROM detection_patterns WHERE pid = $1", pid)
        await self.execute("DELETE FROM detection_patterns WHERE pid = $1", pid)
        return bool(existed)

    async def remove_keyword(self, keyword: str) -> bool:
        return await self.remove_pattern(pattern_id("keyword", keyword))

    async def list_patterns(self) -> list[dict]:
        return await self.fetch("SELECT pid, kind, pattern FROM detection_patterns ORDER BY kind, created_at")

    # ------------------------------------------------------------------ monitored chats
    async def set_chat_enabled(self, chat_id: int, title: str, enabled: bool) -> None:
        await self.execute(
            "INSERT INTO monitored_chats (chat_id, title, enabled, updated_at) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title, enabled = EXCLUDED.enabled, "
            "updated_at = EXCLUDED.updated_at",
            chat_id, title, 1 if enabled else 0, _now())

    async def list_chat_rows(self) -> list[dict]:
        return await self.fetch("SELECT chat_id, title, enabled FROM monitored_chats")

    # ------------------------------------------------------------------ dedup
    async def claim_message(self, chat_id: int, message_id: int, sig: str) -> bool:
        """Atomically claim a (chat, message, match-signature). True only for the first caller."""
        row = await self.fetchrow(
            "INSERT INTO processed_messages (chat_id, message_id, sig, seen_at) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT DO NOTHING RETURNING chat_id",
            chat_id, message_id, sig, _now())
        return row is not None

    async def purge_processed(self, older_than_days: int) -> int:
        cutoff = _now() - older_than_days * 86400
        rows = await self.fetch("DELETE FROM processed_messages WHERE seen_at < $1 RETURNING chat_id", cutoff)
        return len(rows)
