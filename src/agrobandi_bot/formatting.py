from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .filtering import relevance_stars
from .models import Item, Level

_MAX_MSG = 3800


def _esc(s: str) -> str:
    return html.escape(s or "")


def _level_emoji(level: str) -> str:
    try:
        return Level(level).emoji
    except ValueError:
        return "📋"


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y")
    except Exception:
        return iso[:10] if len(iso) >= 10 else iso


def format_item(item: Item, source_name: str) -> str:
    stars = relevance_stars(item.relevance_score)
    level_emoji = _level_emoji(item.level)
    title = _esc(item.title[:120])
    source = _esc(source_name)
    pub = _fmt_date(item.published)
    deadline = _fmt_date(item.deadline)
    summary = _esc((item.summary or "")[:300])
    url = html.escape(item.url or "", quote=True)

    lines = [
        f"{level_emoji} {stars} <b>{title}</b>",
        f"📌 <i>{source}</i>",
    ]
    if item.published:
        lines.append(f"📅 Pubblicato: {pub}")
    if item.deadline:
        lines.append(f"⏳ Scadenza: {deadline}")
    if summary:
        lines.append(f"📝 {summary}")
    lines.append(f'🔗 <a href="{url}">Apri bando</a>')
    return "\n".join(lines)


def format_daily_header(new_count: int) -> str:
    return (
        "🌾 <b>Agroalimentare — Nuovi Bandi e Aggiornamenti</b>\n"
        f"📊 {new_count} nuov{'o' if new_count == 1 else 'i'} bando/i trovato/i nelle ultime 24h\n"
    )


def format_daily_footer() -> str:
    return "\n📡 <i>Monitoraggio automatico — Legacoop Sicilia</i>"


def format_no_news() -> str:
    return (
        "🌾 <b>Agroalimentare — Aggiornamento Giornaliero</b>\n\n"
        "ℹ️ Nessun nuovo bando nelle ultime 24h.\n\n"
        "📡 <i>Monitoraggio automatico — Legacoop Sicilia</i>"
    )


def format_weekly_report(
    items: list[tuple[Item, str]],
    due_soon: list[tuple[Item, str]],
    lookback_days: int,
) -> list[str]:
    lines: list[str] = []
    lines.append(f"📊 <b>Report Settimanale Bandi Agroalimentare</b>")
    lines.append(f"🗓 Ultimi {lookback_days} giorni — {len(items)} bandi trovati\n")

    if due_soon:
        lines.append("⚠️ <b>In scadenza a breve:</b>")
        for item, src_name in due_soon[:10]:
            stars = relevance_stars(item.relevance_score)
            lvl = _level_emoji(item.level)
            lines.append(
                f"{lvl} {stars} <a href=\"{item.url}\">{_esc(item.title[:80])}</a>"
                f" — scade {_fmt_date(item.deadline)}"
            )
        lines.append("")

    if items:
        lines.append("📋 <b>Tutti i nuovi bandi:</b>")
        for item, src_name in items[:40]:
            stars = relevance_stars(item.relevance_score)
            lvl = _level_emoji(item.level)
            lines.append(
                f"{lvl} {stars} <a href=\"{item.url}\">{_esc(item.title[:80])}</a>"
                f" ({_esc(src_name)})"
            )

    lines.append("\n📡 <i>Legacoop Sicilia — Report automatico settimanale</i>")
    return chunk_messages(lines)


def chunk_messages(lines: list[str], max_chars: int = _MAX_MSG) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > max_chars and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)
