from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

from .config import AppConfig, FilteringConfig
from .db import Database, stable_item_id
from .filtering import looks_like_call, score_item
from .formatting import (
    chunk_messages,
    format_daily_footer,
    format_daily_header,
    format_item,
    format_no_news,
    format_weekly_report,
)
from .http_client import HttpClient
from .models import Item, Source
from .sources import enrich_item_from_detail, fetch_items_for_source

log = logging.getLogger(__name__)

_SOURCE_TIMEOUTS = {"rss": 60, "html": 75}


def _source_name_map(sources: list[Source]) -> dict[str, str]:
    return {s.id: s.name for s in sources}


def _row_to_item(row: dict) -> tuple[Item, str]:
    return Item(
        source_id=row["source_id"],
        title=row["title"],
        url=row["url"],
        canonical_url=row["canonical_url"],
        level=row["level"],
        published=row.get("published"),
        deadline=row.get("deadline"),
        summary=row.get("summary", ""),
        relevance_score=row.get("relevance_score", 0),
        recipient_tags=tuple(json.loads(row.get("recipient_tags") or "[]")),
    ), row.get("source_name", row["source_id"])


async def _fetch_and_filter(
    source: Source,
    httpc: HttpClient,
    cfg: FilteringConfig,
    now: datetime,
) -> tuple[list[Item], Optional[str]]:
    http_timeout = _SOURCE_TIMEOUTS.get(source.kind, 75)
    # wait_for uses 1.5x the HTTP timeout so it acts as a real outer guard
    # (not just a duplicate of the inner httpx timeout)
    outer_timeout = http_timeout * 1.5
    try:
        raw_items = await asyncio.wait_for(
            fetch_items_for_source(source, httpc, now),
            timeout=outer_timeout,
        )
    except asyncio.TimeoutError:
        return [], f"Timeout after {outer_timeout:.0f}s"
    except Exception as e:
        return [], str(e)

    filtered: list[Item] = []
    enriched_count = 0

    for item in raw_items:
        result = score_item(cfg, item.title, item.summary, item.url)
        if result.excluded:
            continue

        scored = Item(
            source_id=item.source_id,
            title=item.title,
            url=item.url,
            canonical_url=item.canonical_url,
            level=item.level,
            published=item.published,
            deadline=item.deadline,
            summary=item.summary,
            external_id=item.external_id,
            relevance_score=result.score,
            recipient_tags=tuple(result.recipient_tags),
            meta=item.meta,
        )

        if not result.ok and looks_like_call(item.title, item.summary):
            if result.score >= cfg.prefetch_detail_if_score_at_least and enriched_count < cfg.max_detail_fetch_per_source:
                enriched_count += 1
                scored = await enrich_item_from_detail(source, httpc, scored)
                re_result = score_item(cfg, scored.title, scored.summary, scored.url)
                if re_result.excluded:
                    continue
                scored = Item(
                    source_id=scored.source_id, title=scored.title, url=scored.url,
                    canonical_url=scored.canonical_url, level=scored.level,
                    published=scored.published, deadline=scored.deadline,
                    summary=scored.summary, external_id=scored.external_id,
                    relevance_score=re_result.score,
                    recipient_tags=tuple(re_result.recipient_tags),
                    meta=scored.meta,
                )
                if not re_result.ok:
                    continue
            else:
                continue
        elif not result.ok:
            continue

        filtered.append(scored)

    return filtered, None


async def run_daily_check_once(
    cfg: AppConfig,
    sources: list[Source],
    db: Database,
    httpc: HttpClient,
    chat_id: str,
) -> None:
    log.info("Starting daily check for chat %s", chat_id)
    now = datetime.now(timezone.utc)
    run_id = await db.start_run("daily")
    src_name = _source_name_map(sources)

    active = [s for s in sources if s.enabled]
    total_candidates = 0
    new_items = 0
    sent_items = 0
    errors: dict[str, str] = {}
    to_send: list[tuple[Item, str]] = []

    tasks = {
        s.id: asyncio.create_task(_fetch_and_filter(s, httpc, cfg.filtering, now))
        for s in active
    }

    for source in active:
        items, error = await tasks[source.id]
        if error:
            errors[source.id] = error
            log.warning("Source %s error: %s", source.id, error)
            await db.log_source_result(run_id, source.id, False, 0, error)
            continue

        total_candidates += len(items)
        ok_count = 0

        for item in items:
            # Age filter
            if item.published:
                try:
                    pub_date = date.fromisoformat(item.published)
                    age = (now.date() - pub_date).days
                    if age > cfg.filtering.max_published_age_days:
                        if not item.deadline or item.deadline < now.date().isoformat():
                            continue
                except Exception:
                    pass

            item_id = stable_item_id(source.id, item.url, item.title, item.external_id)
            if await db.has_delivered(chat_id, item_id):
                continue

            new_items += 1
            await db.upsert_seen_item(item, src_name.get(source.id, source.id))
            await db.mark_delivered(chat_id, item_id)
            to_send.append((item, src_name.get(source.id, source.id)))
            ok_count += 1

        await db.log_source_result(run_id, source.id, True, ok_count, None)

    # Use async with Bot for PTB v22 compatibility
    async with Bot(token=cfg.telegram.token_resolved()) as bot:
        if not to_send:
            try:
                await bot.send_message(
                    chat_id=chat_id, text=format_no_news(), parse_mode=ParseMode.HTML
                )
            except Exception as e:
                log.error("Failed to send no-news message: %s", e)
            await db.finish_run(run_id, total_candidates, 0, 0, errors)
            return

        to_send.sort(key=lambda x: x[0].published or "0000", reverse=True)
        sent_items = len(to_send)

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=format_daily_header(len(to_send)),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.error("Failed to send daily header: %s", e)

        item_lines: list[str] = []
        for item, name in to_send:
            item_lines.append(format_item(item, name))
            item_lines.append("─" * 20)
        item_lines.append(format_daily_footer())

        for chunk in chunk_messages(item_lines):
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                await asyncio.sleep(0.5)
            except Exception as e:
                log.error("Failed to send chunk: %s", e)

    await db.finish_run(run_id, total_candidates, new_items, sent_items, errors)
    log.info("Daily check done: %d new items sent for chat %s", sent_items, chat_id)


async def run_weekly_report_once(
    cfg: AppConfig,
    sources: list[Source],
    db: Database,
    httpc: HttpClient,
    chat_id: str,
) -> None:
    log.info("Starting weekly report for chat %s", chat_id)
    run_id = await db.start_run("weekly")

    items_raw, due_soon_raw = await db.list_items_for_weekly(
        cfg.weekly.lookback_days, cfg.weekly.due_soon_days, cfg.weekly.max_items
    )

    items = [_row_to_item(r) for r in items_raw]
    due_soon = [_row_to_item(r) for r in due_soon_raw]

    async with Bot(token=cfg.telegram.token_resolved()) as bot:
        for chunk in format_weekly_report(items, due_soon, cfg.weekly.lookback_days):
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                await asyncio.sleep(0.5)
            except Exception as e:
                log.error("Weekly report send error: %s", e)

    await db.finish_run(run_id, len(items), 0, len(items), {})
    log.info("Weekly report done for chat %s: %d items", chat_id, len(items))
