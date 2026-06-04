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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    api_key TEXT,
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
                    prompt TEXT NOT NULL,
                    negative_prompt TEXT,
                    image_paths TEXT,
                    references_count INTEGER DEFAULT 0,
                    duration INTEGER DEFAULT 5,
                    resolution TEXT DEFAULT "720p",
                    ratio TEXT DEFAULT "16:9",
                    quality TEXT DEFAULT "std",
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
            await self._ensure_generation_columns(db)
            await db.commit()

    async def _ensure_generation_columns(self, db: aiosqlite.Connection):
        async with db.execute("PRAGMA table_info(generations)") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}

        columns = {
            "model": "TEXT",
            "negative_prompt": "TEXT",
            "references_count": "INTEGER DEFAULT 0",
            "quality": "TEXT DEFAULT 'std'",
            "seed": "INTEGER",
        }
        for name, definition in columns.items():
            if name not in existing:
                await db.execute(f"ALTER TABLE generations ADD COLUMN {name} {definition}")

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

    async def create_generation(
        self,
        *,
        user_db_id: int,
        model: str,
        prompt: str,
        negative_prompt: Optional[str],
        image_paths: Optional[str],
        references_count: int,
        duration: int,
        resolution: str,
        ratio: str,
        quality: str,
        seed: Optional[int],
        with_audio: bool,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO generations (
                    user_id, model, prompt, negative_prompt, image_paths, references_count,
                    duration, resolution, ratio, quality, seed, with_audio, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_db_id,
                    model,
                    prompt,
                    negative_prompt,
                    image_paths,
                    references_count,
                    duration,
                    resolution,
                    ratio,
                    quality,
                    seed,
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


db = Database()
