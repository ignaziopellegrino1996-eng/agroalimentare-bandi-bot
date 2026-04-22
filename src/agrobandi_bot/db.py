from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from .models import Item, SearchFilters

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    item_id     TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    source_name TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    level       TEXT NOT NULL,
    published   TEXT,
    deadline    TEXT,
    summary     TEXT,
    relevance_score INTEGER DEFAULT 0,
    recipient_tags  TEXT DEFAULT '[]',
    first_seen  TEXT NOT NULL,
    meta        TEXT
);

CREATE TABLE IF NOT EXISTS delivered_items (
    chat_id  TEXT NOT NULL,
    item_id  TEXT NOT NULL,
    sent_at  TEXT NOT NULL,
    PRIMARY KEY (chat_id, item_id),
    FOREIGN KEY (item_id) REFERENCES seen_items(item_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    total_candidates INTEGER DEFAULT 0,
    new_items   INTEGER DEFAULT 0,
    sent_items  INTEGER DEFAULT 0,
    error_summary TEXT
);

CREATE TABLE IF NOT EXISTS run_sources (
    run_id      INTEGER NOT NULL,
    source_id   TEXT NOT NULL,
    ok          INTEGER DEFAULT 1,
    fetched_count INTEGER DEFAULT 0,
    error_msg   TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_seen_items_deadline ON seen_items(deadline);
CREATE INDEX IF NOT EXISTS idx_seen_items_first_seen ON seen_items(first_seen);
CREATE INDEX IF NOT EXISTS idx_seen_items_level ON seen_items(level);
CREATE INDEX IF NOT EXISTS idx_seen_items_score ON seen_items(relevance_score);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_item_id(source_id: str, url: str, title: str, external_id: Optional[str] = None) -> str:
    if external_id:
        key = f"{source_id}:{external_id}"
    else:
        # canonicalize URL by stripping query/fragment for dedup
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        canon = urlunparse(parsed._replace(query="", fragment=""))
        key = f"{source_id}:{canon}" if canon else f"{source_id}:{title[:120]}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> "Database":
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        return self

    async def __aexit__(self, *_) -> None:
        if self._db:
            await self._db.close()

    async def init(self) -> None:
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    # ── Items ─────────────────────────────────────────────────────────────────

    async def upsert_seen_item(self, item: Item, source_name: str) -> str:
        item_id = stable_item_id(item.source_id, item.url, item.title, item.external_id)
        await self._db.execute(
            """
            INSERT INTO seen_items
                (item_id, source_id, source_name, title, url, canonical_url,
                 level, published, deadline, summary, relevance_score,
                 recipient_tags, first_seen, meta)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET
                relevance_score = excluded.relevance_score,
                deadline = COALESCE(excluded.deadline, seen_items.deadline),
                summary  = COALESCE(excluded.summary,  seen_items.summary)
            """,
            (
                item_id, item.source_id, source_name, item.title, item.url,
                item.canonical_url, item.level, item.published, item.deadline,
                item.summary, item.relevance_score,
                json.dumps(list(item.recipient_tags)),
                _now_iso(), json.dumps(item.meta) if item.meta else None,
            ),
        )
        await self._db.commit()
        return item_id

    async def has_seen(self, item_id: str) -> bool:
        cur = await self._db.execute("SELECT 1 FROM seen_items WHERE item_id=?", (item_id,))
        return await cur.fetchone() is not None

    async def has_delivered(self, chat_id: str, item_id: str) -> bool:
        cur = await self._db.execute(
            "SELECT 1 FROM delivered_items WHERE chat_id=? AND item_id=?", (chat_id, item_id)
        )
        return await cur.fetchone() is not None

    async def mark_delivered(self, chat_id: str, item_id: str) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO delivered_items (chat_id, item_id, sent_at) VALUES (?,?,?)",
            (chat_id, item_id, _now_iso()),
        )
        await self._db.commit()

    # ── Search ────────────────────────────────────────────────────────────────

    async def search_items(self, filters: SearchFilters) -> tuple[list[dict], int]:
        """Returns (items, total_count) applying the given filters."""
        where: list[str] = []
        params: list = []

        if filters.level:
            where.append("level = ?")
            params.append(filters.level)

        if filters.relevance == "alta":
            where.append("relevance_score >= 6")
        elif filters.relevance == "media":
            where.append("relevance_score >= 3")

        if filters.recipient and filters.recipient != "tutti":
            where.append("recipient_tags LIKE ?")
            params.append(f'%"{filters.recipient}"%')

        if filters.status == "aperto":
            today = datetime.now(timezone.utc).date().isoformat()
            where.append("(deadline IS NULL OR deadline >= ?)")
            params.append(today)
        elif filters.status == "in_scadenza":
            today = datetime.now(timezone.utc).date().isoformat()
            limit = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
            where.append("deadline BETWEEN ? AND ?")
            params.extend([today, limit])
        elif filters.status == "atteso":
            today = datetime.now(timezone.utc).date().isoformat()
            where.append("(published IS NULL OR published > ?)")
            params.append(today)

        if filters.keyword:
            kw = f"%{filters.keyword}%"
            where.append("(title LIKE ? OR summary LIKE ?)")
            params.extend([kw, kw])

        where_clause = " AND ".join(where) if where else "1"
        count_sql = f"SELECT COUNT(*) FROM seen_items WHERE {where_clause}"
        cur = await self._db.execute(count_sql, params)
        row = await cur.fetchone()
        total = row[0]

        sql = (
            f"SELECT * FROM seen_items WHERE {where_clause} "
            f"ORDER BY first_seen DESC LIMIT ? OFFSET ?"
        )
        cur = await self._db.execute(sql, params + [filters.page_size, filters.page * filters.page_size])
        rows = await cur.fetchall()
        return [dict(r) for r in rows], total

    async def list_last_n_items(self, n: int = 10) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM seen_items ORDER BY first_seen DESC LIMIT ?", (n,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def list_expiring_items(self, days: int = 30) -> list[dict]:
        today = datetime.now(timezone.utc).date().isoformat()
        limit = (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()
        cur = await self._db.execute(
            "SELECT * FROM seen_items WHERE deadline BETWEEN ? AND ? ORDER BY deadline ASC",
            (today, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def list_items_for_weekly(self, lookback_days: int, due_soon_days: int, max_items: int) -> tuple[list[dict], list[dict]]:
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        cur = await self._db.execute(
            "SELECT * FROM seen_items WHERE first_seen >= ? ORDER BY relevance_score DESC, first_seen DESC LIMIT ?",
            (since, max_items),
        )
        items = [dict(r) for r in await cur.fetchall()]

        today = datetime.now(timezone.utc).date().isoformat()
        limit = (datetime.now(timezone.utc) + timedelta(days=due_soon_days)).date().isoformat()
        cur = await self._db.execute(
            "SELECT * FROM seen_items WHERE deadline BETWEEN ? AND ? ORDER BY deadline ASC LIMIT 15",
            (today, limit),
        )
        due_soon = [dict(r) for r in await cur.fetchall()]
        return items, due_soon

    # ── Sources list ──────────────────────────────────────────────────────────

    async def list_sources_stats(self) -> list[dict]:
        cur = await self._db.execute(
            "SELECT source_id, source_name, level, COUNT(*) as count FROM seen_items "
            "GROUP BY source_id, source_name, level ORDER BY level, source_name"
        )
        return [dict(r) for r in await cur.fetchall()]

    # ── Runs ──────────────────────────────────────────────────────────────────

    async def already_ran_today(self, *, kind: str, local_date: str) -> bool:
        """
        True se esiste un run `kind` già completato (finished_at non NULL) nella
        data locale indicata (YYYY-MM-DD). Usato per idempotency: GitHub Actions
        pianifica due cron UTC per coprire DST; senza questo check entrambi
        manderebbero messaggi nella finestra di tolleranza.
        """
        cur = await self._db.execute(
            """
            SELECT 1 FROM runs
            WHERE kind = ? AND finished_at IS NOT NULL
              AND date(finished_at) = ?
            LIMIT 1
            """,
            (kind, local_date),
        )
        return (await cur.fetchone()) is not None

    async def start_run(self, kind: str) -> int:
        cur = await self._db.execute(
            "INSERT INTO runs (kind, started_at) VALUES (?,?)", (kind, _now_iso())
        )
        await self._db.commit()
        return cur.lastrowid

    async def finish_run(
        self, run_id: int, total: int, new: int, sent: int, errors: dict[str, str]
    ) -> None:
        err_summary = json.dumps(errors) if errors else None
        await self._db.execute(
            "UPDATE runs SET finished_at=?, total_candidates=?, new_items=?, sent_items=?, error_summary=? WHERE id=?",
            (_now_iso(), total, new, sent, err_summary, run_id),
        )
        await self._db.commit()

    async def log_source_result(self, run_id: int, source_id: str, ok: bool, count: int, error: Optional[str]) -> None:
        await self._db.execute(
            "INSERT INTO run_sources (run_id, source_id, ok, fetched_count, error_msg) VALUES (?,?,?,?,?)",
            (run_id, source_id, int(ok), count, error),
        )
        await self._db.commit()
