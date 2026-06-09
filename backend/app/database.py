"""SQLite storage."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiosqlite

from app.config import settings


class Database:
    def __init__(self, db_path: str = settings.DATABASE_PATH):
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            # Migration: add is_allowed column if upgrading from older schema
            try:
                await db.execute("ALTER TABLE users ADD COLUMN is_allowed INTEGER NOT NULL DEFAULT 0")
                await db.commit()
            except Exception:
                pass  # Column already exists
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    api_key TEXT,
                    is_allowed INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    provider_job_id TEXT,
                    model TEXT,
                    source_prompt TEXT,
                    source_negative_prompt TEXT,
                    prompt TEXT NOT NULL,
                    negative_prompt TEXT,
                    image_paths TEXT,
                    references_count INTEGER DEFAULT 0,
                    duration INTEGER DEFAULT 5,
                    resolution TEXT DEFAULT "720p",
                    ratio TEXT DEFAULT "16:9",
                    quality TEXT DEFAULT "std",
                    video_reference_mode TEXT DEFAULT "motion",
                    seed INTEGER,
                    with_audio INTEGER DEFAULT 0,
                    status TEXT DEFAULT "pending",
                    result_url TEXT,
                    error_message TEXT,
                    credits_spent REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS reference_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT,
                    path TEXT NOT NULL,
                    remote_url TEXT,
                    size INTEGER DEFAULT 0,
                    sha256 TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            await self._ensure_generation_columns(db)
            await self._ensure_reference_asset_columns(db)
            await db.commit()

    async def _ensure_generation_columns(self, db: aiosqlite.Connection):
        async with db.execute("PRAGMA table_info(generations)") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}

        columns = {
            "model": "TEXT",
            "source_prompt": "TEXT",
            "source_negative_prompt": "TEXT",
            "negative_prompt": "TEXT",
            "references_count": "INTEGER DEFAULT 0",
            "quality": "TEXT DEFAULT 'std'",
            "video_reference_mode": "TEXT DEFAULT 'motion'",
            "seed": "INTEGER",
            "reference_asset_ids": "TEXT",
        }
        for name, definition in columns.items():
            if name not in existing:
                await db.execute(f"ALTER TABLE generations ADD COLUMN {name} {definition}")

    async def _ensure_reference_asset_columns(self, db: aiosqlite.Connection):
        async with db.execute("PRAGMA table_info(reference_assets)") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}

        columns = {
            "content_type": "TEXT",
            "remote_url": "TEXT",
            "size": "INTEGER DEFAULT 0",
            "sha256": "TEXT",
        }
        for name, definition in columns.items():
            if name not in existing:
                await db.execute(f"ALTER TABLE reference_assets ADD COLUMN {name} {definition}")

    async def get_user(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def create_user(self, telegram_id: int, username: Optional[str], full_name: Optional[str]):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (telegram_id, username, full_name) VALUES (?, ?, ?)",
                (telegram_id, username, full_name),
            )
            await db.commit()

    async def set_user_api_key(self, telegram_id: int, api_key: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE users SET api_key = ? WHERE telegram_id = ?", (api_key, telegram_id))
            await db.commit()

    async def set_user_allowed(self, telegram_id: int, allowed: bool):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET is_allowed = ? WHERE telegram_id = ?",
                (1 if allowed else 0, telegram_id),
            )
            await db.commit()

    async def is_user_allowed(self, telegram_id: int) -> bool:
        user = await self.get_user(telegram_id)
        if not user:
            return False
        return bool(user.get("is_allowed"))

    async def get_all_users(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users ORDER BY created_at DESC") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def create_generation(
        self,
        *,
        user_db_id: int,
        model: str,
        source_prompt: str,
        source_negative_prompt: Optional[str],
        prompt: str,
        negative_prompt: Optional[str],
        image_paths: Optional[str],
        references_count: int,
        duration: int,
        resolution: str,
        ratio: str,
        quality: str,
        video_reference_mode: str,
        seed: Optional[int],
        reference_asset_ids: Optional[str],
        with_audio: bool,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO generations (
                    user_id, model, source_prompt, source_negative_prompt, prompt, negative_prompt, image_paths, references_count,
                    duration, resolution, ratio, quality, video_reference_mode, seed, reference_asset_ids, with_audio, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_db_id,
                    model,
                    source_prompt,
                    source_negative_prompt,
                    prompt,
                    negative_prompt,
                    image_paths,
                    references_count,
                    duration,
                    resolution,
                    ratio,
                    quality,
                    video_reference_mode,
                    seed,
                    reference_asset_ids,
                    1 if with_audio else 0,
                    "pending",
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def update_generation_status(
        self,
        gen_id: int,
        status: str,
        result_url: Optional[str] = None,
        error_message: Optional[str] = None,
        credits_spent: Optional[float] = None,
    ):
        completed = datetime.now().isoformat() if status in {"completed", "failed"} else None
        fields = ["status = ?"]
        values: List[Any] = [status]

        if result_url is not None:
            fields.append("result_url = ?")
            values.append(result_url)
        if error_message is not None:
            fields.append("error_message = ?")
            values.append(error_message)
        if credits_spent is not None:
            fields.append("credits_spent = ?")
            values.append(credits_spent)
        if completed is not None:
            fields.append("completed_at = ?")
            values.append(completed)

        values.append(gen_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE generations SET {', '.join(fields)} WHERE id = ?", values)
            await db.commit()

    async def get_generation(self, gen_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM generations WHERE id = ?", (gen_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_user_generations(self, telegram_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT g.*
                FROM generations g
                JOIN users u ON g.user_id = u.id
                WHERE u.telegram_id = ?
                ORDER BY g.created_at DESC
                LIMIT ?
                """,
                (telegram_id, limit),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def create_reference_asset(
        self,
        *,
        user_db_id: int,
        kind: str,
        filename: str,
        content_type: Optional[str],
        path: str,
        remote_url: Optional[str],
        size: int,
        sha256: str,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, remote_url FROM reference_assets
                WHERE user_id = ? AND kind = ? AND sha256 = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_db_id, kind, sha256),
            ) as cursor:
                existing = await cursor.fetchone()
                if existing:
                    if remote_url and not existing["remote_url"]:
                        await db.execute(
                            "UPDATE reference_assets SET remote_url = ? WHERE id = ?",
                            (remote_url, int(existing["id"])),
                        )
                        await db.commit()
                    return int(existing["id"])

            cursor = await db.execute(
                """
                INSERT INTO reference_assets (
                    user_id, kind, filename, content_type, path, remote_url, size, sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_db_id, kind, filename, content_type, path, remote_url, size, sha256),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def get_user_reference_assets(self, telegram_id: int, limit: int = 80) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT a.*
                FROM reference_assets a
                JOIN users u ON a.user_id = u.id
                WHERE u.telegram_id = ?
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT ?
                """,
                (telegram_id, limit),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def get_reference_assets_by_ids(self, user_db_id: int, asset_ids: List[int]) -> List[Dict[str, Any]]:
        if not asset_ids:
            return []
        placeholders = ",".join("?" for _ in asset_ids)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""
                SELECT *
                FROM reference_assets
                WHERE user_id = ? AND id IN ({placeholders})
                """,
                (user_db_id, *asset_ids),
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
        by_id = {int(row["id"]): row for row in rows}
        return [by_id[asset_id] for asset_id in asset_ids if asset_id in by_id]


db = Database()
