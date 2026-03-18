#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import argparse
import asyncio
import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agrobandi_bot.config import load_config, load_sources
from agrobandi_bot.db import Database
from agrobandi_bot.http_client import HttpClient
from agrobandi_bot.runner import run_daily_check_once, run_weekly_report_once
from agrobandi_bot.telegram_app import run_bot_polling


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Agroalimentare Bandi Bot — Legacoop Sicilia")
    p.add_argument("--config", default="config.example.yaml")
    p.add_argument("--sources", default="sources.yaml")
    p.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    p.add_argument("--once", action="store_true")
    p.add_argument("--weekly-once", action="store_true")
    p.add_argument("--expect-local-time", default=None)
    p.add_argument("--expect-weekday", default=None)
    p.add_argument("--db-path", default=None)
    return p.parse_args()


async def _run_once_async(
    args: argparse.Namespace,
    cfg,
    srcs,
    db_path: Path,
) -> int:
    logger = logging.getLogger("run")

    if not cfg.should_run_now(
        expect_local_time=args.expect_local_time,
        expect_weekday=args.expect_weekday,
    ):
        logger.info(
            "Skipping: current time does not match --expect-local-time=%s "
            "--expect-weekday=%s (tolerance ±%d min)",
            args.expect_local_time,
            args.expect_weekday,
            cfg.schedule.tolerance_minutes,
        )
        return 0

    chat_ids = cfg.telegram.chat_ids_resolved()
    if not chat_ids:
        raise SystemExit("No TELEGRAM_CHAT_ID configured.")

    async with Database(db_path) as db:
        await db.init()
        async with HttpClient(cfg.http) as httpc:
            if args.weekly_once:
                for chat_id in chat_ids:
                    await run_weekly_report_once(cfg, srcs, db, httpc, chat_id)
            else:
                for chat_id in chat_ids:
                    await run_daily_check_once(cfg, srcs, db, httpc, chat_id)
    return 0


def main() -> None:
    args = _parse_args()
    _setup_logging(args.log_level)

    cfg = load_config(Path(args.config))
    srcs = load_sources(Path(args.sources))
    db_path = Path(args.db_path) if args.db_path else Path(cfg.db.path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.once or args.weekly_once:
        # One-shot mode: asyncio.run() manages the event loop
        raise SystemExit(asyncio.run(_run_once_async(args, cfg, srcs, db_path)))

    # Long-running polling: PTB v22 manages its own event loop via app.run_polling()
    run_bot_polling(cfg, srcs, db_path)


if __name__ == "__main__":
    main()
