from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import AppConfig, load_sources
from .db import Database, stable_item_id
from .filtering import relevance_label, relevance_stars
from .formatting import format_item, chunk_messages
from .http_client import HttpClient
from .models import Item, SearchFilters, Source
from .runner import run_daily_check_once, run_weekly_report_once

log = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
FILTER_LEVEL, FILTER_RELEVANCE, FILTER_RECIPIENT, FILTER_STATUS, FILTER_KEYWORD, SHOW_RESULTS = range(6)

_LEVEL_OPTIONS = [("🇪🇺 Europeo", "EU"), ("🇮🇹 Nazionale", "IT"), ("🏴 Sicilia", "SICILIA"), ("🌍 Tutti", None)]
_RELEVANCE_OPTIONS = [("⭐⭐⭐ Alta", "alta"), ("⭐⭐ Media", "media"), ("⭐ Tutte", None)]
_RECIPIENT_OPTIONS = [
    ("🤝 Cooperative", "cooperative"),
    ("🏢 PMI", "pmi"),
    ("👩 Giovani/Donne", "giovani_donne"),
    ("🐟 Pesca", "pesca"),
    ("👥 Tutti", None),
]
_STATUS_OPTIONS = [
    ("✅ Aperto", "aperto"),
    ("⏳ In scadenza", "in_scadenza"),
    ("📅 Atteso", "atteso"),
    ("📋 Tutti", None),
]

_WELCOME = """
🌾 <b>Agroalimentare — Bandi e Avvisi</b>
<i>Monitoraggio automatico per Legacoop Sicilia</i>

Monitoro <b>25 fonti</b> tra portali europei, nazionali e siciliani per trovare bandi, avvisi e opportunità nel settore agroalimentare.

<b>Comandi disponibili:</b>
/cerca — Cerca bandi con filtri
/ultimi — Ultimi 10 bandi aggiunti
/scadenze — Bandi in scadenza entro 30 giorni
/fonti — Elenco fonti monitorate
/help — Guida completa
"""

_HELP = """
📚 <b>Guida Comandi</b>

/start — Messaggio di benvenuto
/cerca — Ricerca con filtri (livello, rilevanza, destinatari, stato, parola chiave)
/ultimi — Mostra gli ultimi 10 bandi inseriti nel database
/scadenze — Bandi in scadenza nei prossimi 30 giorni
/fonti — Lista delle 25 fonti monitorate
/test_daily — Esegue manualmente il controllo giornaliero

<b>Come funziona la ricerca:</b>
1. Avvia /cerca
2. Seleziona i filtri desiderati con i pulsanti
3. Naviga i risultati con i tasti ◀ ▶

<b>Rilevanza cooperativa:</b>
⭐⭐⭐ Alta — Bando molto rilevante per cooperative
⭐⭐ Media — Rilevanza discreta
⭐ Bassa — Meno specifico per il settore

📡 <i>Aggiornamento: ogni giorno alle 08:00</i>
📡 <i>Report settimanale: ogni lunedì</i>
"""


def _level_emoji(level: str) -> str:
    return {"EU": "🇪🇺", "IT": "🇮🇹", "SICILIA": "🏴"}.get(level, "📋")


def _filters_summary(f: SearchFilters) -> str:
    parts = []
    if f.level:
        parts.append(f"Livello: {_level_emoji(f.level)} {f.level}")
    if f.relevance:
        parts.append(f"Rilevanza: {f.relevance}")
    if f.recipient:
        parts.append(f"Destinatari: {f.recipient}")
    if f.status:
        parts.append(f"Stato: {f.status}")
    if f.keyword:
        parts.append(f"Parola chiave: '{f.keyword}'")
    return " | ".join(parts) if parts else "Nessun filtro"


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
    ), row.get("source_name", row["source_id"])


async def _send_paginated_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    filters_obj: SearchFilters,
    edit: bool = False,
) -> None:
    rows, total = await db.search_items(filters_obj)
    page = filters_obj.page
    page_size = filters_obj.page_size
    total_pages = max(1, (total + page_size - 1) // page_size)

    if not rows:
        text = (
            f"🔍 <b>Risultati ricerca</b>\n"
            f"<i>{_filters_summary(filters_obj)}</i>\n\n"
            "ℹ️ Nessun bando trovato con questi filtri.\n"
            "Prova ad allargare i criteri di ricerca."
        )
        kb = [[InlineKeyboardButton("🔄 Nuova ricerca", callback_data="restart_search")]]
        markup = InlineKeyboardMarkup(kb)
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        else:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    lines: list[str] = [
        f"🔍 <b>Risultati ricerca</b> (pag. {page + 1}/{total_pages}, totale: {total})",
        f"<i>{_filters_summary(filters_obj)}</i>",
        "",
    ]
    for row in rows:
        item, src_name = _row_to_item(row)
        lines.append(format_item(item, src_name))
        lines.append("─" * 18)

    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        prev_data = json.dumps({"a": "page", "p": page - 1, "f": _filters_to_dict(filters_obj)})
        nav_buttons.append(InlineKeyboardButton("◀ Prec.", callback_data=prev_data[:64]))
    if page < total_pages - 1:
        next_data = json.dumps({"a": "page", "p": page + 1, "f": _filters_to_dict(filters_obj)})
        nav_buttons.append(InlineKeyboardButton("Succ. ▶", callback_data=next_data[:64]))

    kb: list[list[InlineKeyboardButton]] = []
    if nav_buttons:
        kb.append(nav_buttons)
    kb.append([InlineKeyboardButton("🔄 Nuova ricerca", callback_data="restart_search")])
    markup = InlineKeyboardMarkup(kb)

    text = "\n".join(lines)[:3800]
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )


def _filters_to_dict(f: SearchFilters) -> dict:
    return {
        "l": f.level, "r": f.relevance, "rc": f.recipient,
        "s": f.status, "k": f.keyword,
    }


def _dict_to_filters(d: dict, page: int = 0) -> SearchFilters:
    return SearchFilters(
        level=d.get("l"), relevance=d.get("r"), recipient=d.get("rc"),
        status=d.get("s"), keyword=d.get("k"), page=page,
    )


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_WELCOME, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP, parse_mode=ParseMode.HTML)


async def cmd_ultimi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    rows = await db.list_last_n_items(10)
    if not rows:
        await update.message.reply_text("ℹ️ Nessun bando nel database ancora.")
        return
    lines = ["📋 <b>Ultimi 10 bandi aggiunti:</b>", ""]
    for row in rows:
        item, src_name = _row_to_item(row)
        lines.append(format_item(item, src_name))
        lines.append("─" * 18)
    for chunk in chunk_messages(lines):
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_scadenze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    rows = await db.list_expiring_items(30)
    if not rows:
        await update.message.reply_text("ℹ️ Nessun bando in scadenza nei prossimi 30 giorni.")
        return
    lines = ["⏳ <b>Bandi in scadenza entro 30 giorni:</b>", ""]
    for row in rows:
        item, src_name = _row_to_item(row)
        lines.append(format_item(item, src_name))
        lines.append("─" * 18)
    for chunk in chunk_messages(lines):
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_fonti(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sources: list[Source] = context.bot_data["sources"]
    db: Database = context.bot_data["db"]
    stats = await db.list_sources_stats()
    stats_map = {s["source_id"]: s["count"] for s in stats}

    lines = ["📡 <b>Fonti monitorate (25 totali):</b>", ""]
    by_level = {"EU": [], "IT": [], "SICILIA": []}
    for s in sources:
        by_level.setdefault(s.level, []).append(s)

    emojis = {"EU": "🇪🇺", "IT": "🇮🇹", "SICILIA": "🏴"}
    labels = {"EU": "Europeo", "IT": "Nazionale", "SICILIA": "Sicilia"}

    for level in ["EU", "IT", "SICILIA"]:
        lvl_sources = by_level.get(level, [])
        if not lvl_sources:
            continue
        lines.append(f"{emojis[level]} <b>{labels[level]} ({len(lvl_sources)} fonti)</b>")
        for s in lvl_sources:
            count = stats_map.get(s.id, 0)
            status = "✅" if s.enabled else "⏸"
            lines.append(f"  {status} {s.name}" + (f" ({count} bandi)" if count else ""))
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_test_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: AppConfig = context.bot_data["cfg"]
    sources: list[Source] = context.bot_data["sources"]
    db: Database = context.bot_data["db"]
    chat_id = str(update.effective_chat.id)

    await update.message.reply_text("🔄 Avvio controllo giornaliero manuale…")
    try:
        async with HttpClient(cfg.http) as httpc:
            await run_daily_check_once(cfg, sources, db, httpc, chat_id)
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")
        log.exception("test_daily error")


# ── /cerca conversation ────────────────────────────────────────────────────────

async def cerca_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["filters"] = {}
    kb = [[InlineKeyboardButton(label, callback_data=f"lvl:{val or 'ALL'}")] for label, val in _LEVEL_OPTIONS]
    await update.message.reply_text(
        "🔍 <b>Ricerca Bandi</b>\n\n1️⃣ Seleziona il <b>livello territoriale</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return FILTER_LEVEL


async def filter_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    val = q.data.replace("lvl:", "")
    context.user_data["filters"]["level"] = None if val == "ALL" else val

    kb = [[InlineKeyboardButton(label, callback_data=f"rel:{val2 or 'ALL'}")] for label, val2 in _RELEVANCE_OPTIONS]
    await q.edit_message_text(
        "2️⃣ Filtra per <b>rilevanza cooperativa</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return FILTER_RELEVANCE


async def filter_relevance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    val = q.data.replace("rel:", "")
    context.user_data["filters"]["relevance"] = None if val == "ALL" else val

    kb = [[InlineKeyboardButton(label, callback_data=f"rec:{val2 or 'ALL'}")] for label, val2 in _RECIPIENT_OPTIONS]
    await q.edit_message_text(
        "3️⃣ Filtra per <b>destinatari</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return FILTER_RECIPIENT


async def filter_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    val = q.data.replace("rec:", "")
    context.user_data["filters"]["recipient"] = None if val == "ALL" else val

    kb = [[InlineKeyboardButton(label, callback_data=f"sta:{val2 or 'ALL'}")] for label, val2 in _STATUS_OPTIONS]
    await q.edit_message_text(
        "4️⃣ Filtra per <b>stato del bando</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return FILTER_STATUS


async def filter_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    val = q.data.replace("sta:", "")
    context.user_data["filters"]["status"] = None if val == "ALL" else val

    kb = [[
        InlineKeyboardButton("🔍 Cerca subito", callback_data="kw:SKIP"),
        InlineKeyboardButton("✏️ Inserisci parola chiave", callback_data="kw:ASK"),
    ]]
    await q.edit_message_text(
        "5️⃣ Vuoi cercare per <b>parola chiave</b>?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return FILTER_KEYWORD


async def filter_keyword_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "kw:SKIP":
        return await _execute_search(update, context, edit=True)
    await q.edit_message_text(
        "✏️ Scrivi la parola chiave da cercare (o /annulla per saltare):",
        parse_mode=ParseMode.HTML,
    )
    return FILTER_KEYWORD


async def filter_keyword_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.startswith("/"):
        context.user_data["filters"]["keyword"] = None
    else:
        context.user_data["filters"]["keyword"] = text
    return await _execute_search(update, context, edit=False)


async def _execute_search(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool) -> int:
    db: Database = context.bot_data["db"]
    f = context.user_data.get("filters", {})
    filters_obj = SearchFilters(
        level=f.get("level"),
        relevance=f.get("relevance"),
        recipient=f.get("recipient"),
        status=f.get("status"),
        keyword=f.get("keyword"),
        page=0,
    )
    await _send_paginated_results(update, context, db, filters_obj, edit=edit)
    return ConversationHandler.END


async def pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    try:
        data = json.loads(q.data)
        if data.get("a") == "page":
            filters_obj = _dict_to_filters(data.get("f", {}), page=data.get("p", 0))
            db: Database = context.bot_data["db"]
            await _send_paginated_results(update, context, db, filters_obj, edit=True)
    except Exception as e:
        log.error("Pagination callback error: %s", e)


async def restart_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data["filters"] = {}
    kb = [[InlineKeyboardButton(label, callback_data=f"lvl:{val or 'ALL'}")] for label, val in _LEVEL_OPTIONS]
    await q.edit_message_text(
        "🔍 <b>Ricerca Bandi</b>\n\n1️⃣ Seleziona il <b>livello territoriale</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return FILTER_LEVEL


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Ricerca annullata.")
    return ConversationHandler.END


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def job_daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: AppConfig = context.bot_data["cfg"]
    sources: list[Source] = context.bot_data["sources"]
    db: Database = context.bot_data["db"]
    chat_ids = cfg.telegram.chat_ids_resolved()
    async with HttpClient(cfg.http) as httpc:
        for chat_id in chat_ids:
            try:
                await run_daily_check_once(cfg, sources, db, httpc, chat_id)
            except Exception as e:
                log.exception("Daily job error for chat %s: %s", chat_id, e)


async def job_weekly(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: AppConfig = context.bot_data["cfg"]
    sources: list[Source] = context.bot_data["sources"]
    db: Database = context.bot_data["db"]
    chat_ids = cfg.telegram.chat_ids_resolved()
    async with HttpClient(cfg.http) as httpc:
        for chat_id in chat_ids:
            try:
                await run_weekly_report_once(cfg, sources, db, httpc, chat_id)
            except Exception as e:
                log.exception("Weekly job error for chat %s: %s", chat_id, e)


# ── Application factory ────────────────────────────────────────────────────────

async def run_bot_polling(cfg: AppConfig, sources: list[Source], db_path: Path) -> None:
    from telegram.ext import JobQueue
    from zoneinfo import ZoneInfo

    tz = cfg.tz()

    db = Database(db_path)
    await db.__aenter__()
    await db.init()

    app = (
        Application.builder()
        .token(cfg.telegram.token_resolved())
        .build()
    )

    app.bot_data["cfg"] = cfg
    app.bot_data["sources"] = sources
    app.bot_data["db"] = db

    # /cerca conversation
    cerca_conv = ConversationHandler(
        entry_points=[CommandHandler("cerca", cerca_start)],
        states={
            FILTER_LEVEL: [CallbackQueryHandler(filter_level, pattern=r"^lvl:")],
            FILTER_RELEVANCE: [CallbackQueryHandler(filter_relevance, pattern=r"^rel:")],
            FILTER_RECIPIENT: [CallbackQueryHandler(filter_recipient, pattern=r"^rec:")],
            FILTER_STATUS: [CallbackQueryHandler(filter_status, pattern=r"^sta:")],
            FILTER_KEYWORD: [
                CallbackQueryHandler(filter_keyword_choice, pattern=r"^kw:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, filter_keyword_text),
            ],
        },
        fallbacks=[CommandHandler("annulla", cancel), CommandHandler("start", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ultimi", cmd_ultimi))
    app.add_handler(CommandHandler("scadenze", cmd_scadenze))
    app.add_handler(CommandHandler("fonti", cmd_fonti))
    app.add_handler(CommandHandler("test_daily", cmd_test_daily))
    app.add_handler(cerca_conv)
    app.add_handler(CallbackQueryHandler(pagination_callback, pattern=r"^\{"))
    app.add_handler(CallbackQueryHandler(restart_search_callback, pattern=r"^restart_search$"))

    # Schedule jobs
    h_daily, m_daily = map(int, cfg.schedule.daily_time.split(":"))
    h_weekly, m_weekly = map(int, cfg.schedule.weekly_time.split(":"))
    _weekday_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    weekly_weekday = _weekday_map.get(cfg.schedule.weekly_day.lower(), 0)

    jq = app.job_queue
    jq.run_daily(job_daily, time=datetime.now(tz).replace(hour=h_daily, minute=m_daily, second=0).timetz())
    jq.run_daily(
        job_weekly,
        time=datetime.now(tz).replace(hour=h_weekly, minute=m_weekly, second=0).timetz(),
        days=(weekly_weekday,),
    )

    log.info("Bot polling started. Daily: %s, Weekly: %s %s", cfg.schedule.daily_time, cfg.schedule.weekly_day, cfg.schedule.weekly_time)

    try:
        await app.run_polling(drop_pending_updates=True)
    finally:
        await db.__aexit__(None, None, None)
