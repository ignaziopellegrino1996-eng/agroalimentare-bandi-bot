from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import yaml

from .models import Source


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_ids: list[str]

    def token_resolved(self) -> str:
        return _resolve_env(self.token)

    def chat_ids_resolved(self) -> list[str]:
        return [_resolve_env(c) for c in self.chat_ids if _resolve_env(c)]


@dataclass(frozen=True)
class ScheduleConfig:
    timezone: str = "Europe/Rome"
    daily_time: str = "08:00"
    weekly_day: str = "mon"
    weekly_time: str = "08:05"
    tolerance_minutes: int = 90


@dataclass(frozen=True)
class DbConfig:
    path: str = "data/state.sqlite3"


@dataclass(frozen=True)
class HttpConfig:
    timeout_s: float = 25.0
    max_retries: int = 3
    backoff_base_s: float = 0.6
    concurrency: int = 6
    rate_limit_rps: float = 1.0
    user_agent: str = "AgroBandiBot/1.0"


@dataclass(frozen=True)
class FilteringConfig:
    min_score: int = 2
    prefetch_detail_if_score_at_least: int = 1
    max_detail_fetch_per_source: int = 10
    max_published_age_days: int = 365
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WeeklyConfig:
    lookback_days: int = 7
    due_soon_days: int = 14
    max_items: int = 50


@dataclass(frozen=True)
class AppConfig:
    telegram: TelegramConfig
    schedule: ScheduleConfig
    db: DbConfig
    http: HttpConfig
    filtering: FilteringConfig
    weekly: WeeklyConfig

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.schedule.timezone)

    def should_run_now(
        self,
        expect_local_time: Optional[str] = None,
        expect_weekday: Optional[str] = None,
    ) -> bool:
        if expect_local_time is None and expect_weekday is None:
            return True
        now = datetime.now(self.tz())
        tolerance = self.schedule.tolerance_minutes
        if expect_weekday:
            days = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
            if now.weekday() != days.get(expect_weekday.lower(), -1):
                return False
        if expect_local_time:
            h, m = map(int, expect_local_time.split(":"))
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            diff = abs((now - target).total_seconds())
            if diff > tolerance * 60:
                return False
        return True


def _resolve_env(value: str) -> str:
    match = re.fullmatch(r"\$\{([^}]+)}", value.strip())
    if match:
        return os.environ.get(match.group(1), "")
    return value


def _deep_get(d: dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, {})
    return d if d != {} else default


def load_config(path: Path) -> AppConfig:
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)

    tg = data.get("telegram", {})
    sched = data.get("schedule", {})
    db = data.get("db", {})
    http = data.get("http", {})
    filt = data.get("filtering", {})
    weekly = data.get("weekly", {})

    return AppConfig(
        telegram=TelegramConfig(
            token=tg.get("token", "${TELEGRAM_BOT_TOKEN}"),
            chat_ids=tg.get("chat_ids", ["${TELEGRAM_CHAT_ID}"]),
        ),
        schedule=ScheduleConfig(
            timezone=sched.get("timezone", "Europe/Rome"),
            daily_time=sched.get("daily_time", "08:00"),
            weekly_day=sched.get("weekly_day", "mon"),
            weekly_time=sched.get("weekly_time", "08:05"),
            tolerance_minutes=sched.get("tolerance_minutes", 90),
        ),
        db=DbConfig(path=db.get("path", "data/state.sqlite3")),
        http=HttpConfig(
            timeout_s=http.get("timeout_s", 25.0),
            max_retries=http.get("max_retries", 3),
            backoff_base_s=http.get("backoff_base_s", 0.6),
            concurrency=http.get("concurrency", 6),
            rate_limit_rps=http.get("rate_limit_rps", 1.0),
            user_agent=http.get("user_agent", "AgroBandiBot/1.0"),
        ),
        filtering=FilteringConfig(
            min_score=filt.get("min_score", 2),
            prefetch_detail_if_score_at_least=filt.get("prefetch_detail_if_score_at_least", 1),
            max_detail_fetch_per_source=filt.get("max_detail_fetch_per_source", 10),
            max_published_age_days=filt.get("max_published_age_days", 365),
            include_keywords=filt.get("include_keywords", []),
            exclude_keywords=filt.get("exclude_keywords", []),
        ),
        weekly=WeeklyConfig(
            lookback_days=weekly.get("lookback_days", 7),
            due_soon_days=weekly.get("due_soon_days", 14),
            max_items=weekly.get("max_items", 50),
        ),
    )


def load_sources(path: Path) -> list[Source]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    sources = []
    for s in data.get("sources", []):
        sources.append(Source(
            id=s["id"],
            name=s["name"],
            level=s["level"],
            kind=s["kind"],
            url=s["url"],
            enabled=s.get("enabled", True),
            parser=s.get("parser"),
        ))
    return sources
