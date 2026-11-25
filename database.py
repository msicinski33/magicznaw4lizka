"""SQLite persistence. Mutations which decide a winner use BEGIN IMMEDIATE."""
from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.executescript("""
        CREATE TABLE IF NOT EXISTS users(discord_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, total_captures INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS creatures(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, aliases TEXT NOT NULL DEFAULT '[]', rarity TEXT NOT NULL, image_path TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN(0,1)));
        CREATE TABLE IF NOT EXISTS user_creatures(discord_id INTEGER NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE, creature_id INTEGER NOT NULL REFERENCES creatures(id) ON DELETE CASCADE, quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity>=0), first_caught_at TEXT NOT NULL, last_caught_at TEXT NOT NULL, PRIMARY KEY(discord_id,creature_id));
        CREATE TABLE IF NOT EXISTS server_settings(guild_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, allowed_channels TEXT NOT NULL DEFAULT '[]', spawn_min_messages INTEGER NOT NULL DEFAULT 20, spawn_max_messages INTEGER NOT NULL DEFAULT 50, encounter_duration INTEGER NOT NULL DEFAULT 120, allow_parallel INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS encounter_history(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, creature_id INTEGER NOT NULL REFERENCES creatures(id), caught_by INTEGER, spawned_at TEXT NOT NULL, caught_at TEXT, expires_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN('active','caught','expired','closed')));
        CREATE INDEX IF NOT EXISTS idx_history_active ON encounter_history(guild_id,channel_id,status);
        CREATE INDEX IF NOT EXISTS idx_user_creatures_user ON user_creatures(discord_id);
        CREATE INDEX IF NOT EXISTS idx_history_caught ON encounter_history(caught_by);
        """)
        # Encounters from a previous process are deliberately closed to avoid accepting stale guesses.
        await self.db.execute("UPDATE encounter_history SET status='closed' WHERE status='active'")
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    async def execute(self, sql: str, params: tuple = ()):
        assert self.db
        async with self._lock:
            cur = await self.db.execute(sql, params)
            await self.db.commit()
            return cur

    async def one(self, sql: str, params: tuple = ()):
        assert self.db
        async with self._lock:
            cur = await self.db.execute(sql, params)
            return await cur.fetchone()

    async def all(self, sql: str, params: tuple = ()):
        assert self.db
        async with self._lock:
            cur = await self.db.execute(sql, params)
            return await cur.fetchall()

    async def load_creatures(self, path: Path) -> int:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        ids: set[int] = set()
        names: set[str] = set()
        for item in data:
            creature_id = int(item["id"])
            name = item["name"].strip()
            rarity = item.get("rarity", "common").lower()
            if creature_id < 1 or creature_id in ids:
                raise ValueError(f"Powtórzony lub nieprawidłowy identyfikator stworzenia: {creature_id}")
            if not name or name.casefold() in names:
                raise ValueError(f"Pusta lub powtórzona nazwa stworzenia: {name!r}")
            if rarity not in {"common", "uncommon", "rare", "epic", "legendary", "mythical"}:
                raise ValueError(f"Nieprawidłowa rzadkość stworzenia {name}: {rarity}")
            ids.add(creature_id)
            names.add(name.casefold())

        assert self.db
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                for item in data:
                    await self.db.execute("INSERT INTO creatures(id,name,aliases,rarity,image_path,enabled) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,aliases=excluded.aliases,rarity=excluded.rarity,image_path=excluded.image_path,enabled=excluded.enabled",
                        (int(item["id"]), item["name"].strip(), json.dumps(item.get("aliases", []), ensure_ascii=False), item.get("rarity", "common").lower(), item["image"], int(item.get("enabled", True))))
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        return len(data)

    async def settings(self, guild_id: int):
        row = await self.one("SELECT * FROM server_settings WHERE guild_id=?", (guild_id,))
        if row is None:
            await self.execute("INSERT OR IGNORE INTO server_settings(guild_id) VALUES(?)", (guild_id,))
            row = await self.one("SELECT * FROM server_settings WHERE guild_id=?", (guild_id,))
        return row

    async def spawn(self, guild_id: int, channel_id: int, creature_id: int, duration: int, allow_parallel: bool = False):
        assert self.db
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                if not allow_parallel:
                    cur = await self.db.execute("SELECT id FROM encounter_history WHERE guild_id=? AND channel_id=? AND status='active'", (guild_id, channel_id))
                    if await cur.fetchone():
                        await self.db.rollback()
                        return None
                cur = await self.db.execute("INSERT INTO encounter_history(guild_id,channel_id,creature_id,spawned_at,expires_at,status) VALUES(?,?,?,?,?,'active')",
                    (guild_id, channel_id, creature_id, now(), datetime.now(timezone.utc).timestamp() + duration))
                await self.db.commit()
                return cur.lastrowid
            except Exception:
                await self.db.rollback()
                raise

    async def catch(self, encounter_id: int, user_id: int) -> bool:
        """Atomically claim an active encounter and increment the user's species stack."""
        assert self.db
        stamp = now()
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = await self.db.execute("UPDATE encounter_history SET status='caught',caught_by=?,caught_at=? WHERE id=? AND status='active' AND CAST(expires_at AS REAL)>?",
                                            (user_id, stamp, encounter_id, datetime.now(timezone.utc).timestamp()))
                if cur.rowcount != 1:
                    await self.db.rollback()
                    return False
                row_cur = await self.db.execute("SELECT creature_id FROM encounter_history WHERE id=?", (encounter_id,))
                creature_id = (await row_cur.fetchone())[0]
                await self.db.execute("INSERT INTO users(discord_id,created_at,total_captures) VALUES(?,?,1) ON CONFLICT(discord_id) DO UPDATE SET total_captures=total_captures+1", (user_id, stamp))
                await self.db.execute("INSERT INTO user_creatures(discord_id,creature_id,quantity,first_caught_at,last_caught_at) VALUES(?,?,1,?,?) ON CONFLICT(discord_id,creature_id) DO UPDATE SET quantity=quantity+1,last_caught_at=excluded.last_caught_at", (user_id, creature_id, stamp, stamp))
                await self.db.commit()
                return True
            except Exception:
                await self.db.rollback()
                raise

