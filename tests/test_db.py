from __future__ import annotations

import pytest
import pytest_asyncio
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agrobandi_bot.db import Database, stable_item_id
from agrobandi_bot.models import Item


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.sqlite3")
    async with d:
        await d.init()
        yield d


@pytest.mark.asyncio
async def test_upsert_and_has_seen(db):
    item = Item(
        source_id="masaf", title="Bando Test", url="https://example.com/bando",
        canonical_url="https://example.com/bando", level="IT",
        published="2026-01-01", deadline="2026-06-01", summary="Test bando agricoltura"
    )
    item_id = await db.upsert_seen_item(item, "MASAF Test")
    assert await db.has_seen(item_id)


@pytest.mark.asyncio
async def test_not_seen_initially(db):
    item_id = stable_item_id("src", "https://example.com/x", "Title")
    assert not await db.has_seen(item_id)


@pytest.mark.asyncio
async def test_deliver_and_check(db):
    item = Item(
        source_id="ismea", title="ISMEA Investe", url="https://ismea.it/bando1",
        canonical_url="https://ismea.it/bando1", level="IT",
        published=None, deadline=None, summary="Agevolazioni per agricoltura"
    )
    item_id = await db.upsert_seen_item(item, "ISMEA")
    assert not await db.has_delivered("chat123", item_id)
    await db.mark_delivered("chat123", item_id)
    assert await db.has_delivered("chat123", item_id)


@pytest.mark.asyncio
async def test_list_last_n(db):
    for i in range(5):
        item = Item(
            source_id="test", title=f"Bando {i}", url=f"https://example.com/{i}",
            canonical_url=f"https://example.com/{i}", level="SICILIA",
            published=None, deadline=None, summary=f"Summary {i}"
        )
        await db.upsert_seen_item(item, "Test Source")
    rows = await db.list_last_n_items(3)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_search_by_level(db):
    for level in ["EU", "IT", "SICILIA"]:
        item = Item(
            source_id=f"src_{level}", title=f"Bando {level}",
            url=f"https://example.com/{level}", canonical_url=f"https://example.com/{level}",
            level=level, published=None, deadline=None, summary=f"Bando livello {level}"
        )
        await db.upsert_seen_item(item, f"Source {level}")

    from agrobandi_bot.models import SearchFilters
    filters = SearchFilters(level="EU")
    rows, total = await db.search_items(filters)
    assert total == 1
    assert rows[0]["level"] == "EU"
