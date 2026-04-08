from __future__ import annotations

import asyncio
import json
import logging
from datetime import time as dt_time
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

from .config import AppConfig
from .db import Database
from .filtering import relevance_stars
from .formatting import format_item, chunk_messages
from .models import Item, SearchFilters, Source
from .runner import run_daily_check_once, run_weekly_report_once
from .http_client import HttpClient

log = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
FILTER_LEVEL, FILTER_RELEVANCE, FILTER_RECIPIENT, FILTER_STATUS, FILTER_KEYWORD = range(5)

_LEVEL_OPTIONS = [("🇪🇺 Europeo", "EU"), ("🇮🇹 Nazionale", "IT"), ("🏴 Sicilia", "SICILIA"), ("🌍 Tutti", "ALL")]
_RELEVANCE_OPTIONS = [("⭐⭐⭐ Alta", "alta"), ("⭐⭐ Media", "media"), ("⭐ Tutte", "ALL")]
_RECIPIENT_OPTIONS = [
    ("🤝 Cooperative", "cooperative"),
    ("🏢 PMI", "pmi"),
    ("👩 Giovani/Donne", "giovani_donne"),
    ("🐟 Pesca", "pesca"),
    ("👥 Tutti", "ALL"),
]
_STATUS_OPTIONS = [
    ("✅ Aperto", "aperto"),
    ("⏳ In scadenza", "in_scadenza"),
    ("📅 Atteso", "atteso"),
    ("📋 Tutti", "ALL"),
]

_WELCOME = """
🌾 <b>Agroalimentare — Bandi e Avvisi</b>
<i>Monitoraggio automatico per Legacoop Sicilia</i>

Monitoro <b>29 fonti</b> tra portali europei, nazionali e siciliani per trovare bandi, avvisi e opportunità nel settore agroalimentare.

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
/fonti — Lista delle 29 fonti monitorate
/test_daily — Esegue manualmente il controllo giornaliero

<b>Come funziona la ricerca:</b>
1. Avvia /cerca
2. Seleziona i filtri con i pulsanti
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


def _filters_to_dict(f: SearchFilters) -> dict:
    return {"l": f.level, "r": f.relevance, "rc": f.recipient, "s": f.status, "k": f.keyword}


def _dict_to_filters(d: dict, page: int = 0) -> SearchFilters:
    return SearchFilters(
        level=d.get("l"), relevance=d.get("r"), recipient=d.get("rc"),
        status=d.get("s"), keyword=d.get("k"), page=page,
    )


def _user_key(update: Update) -> str:
    user = update.effective_user
    return str(user.id) if user else "0"


async def _send_paginated_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    filters_obj: SearchFilters,
    edit: bool = False,
) -> None:
    # Persist filters in per-user storage so concurrent users don't overwrite each other
    context.user_data["pg_filters"] = _filters_to_dict(filters_obj)

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
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Nuova ricerca", callback_data="restart_search")
        ]])
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

    # Bug #8 fix: callback_data uses short "pg:{N}" format (always ≤8 chars)
    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀ Prec.", callback_data=f"pg:{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Succ. ▶", callback_data=f"pg:{page + 1}"))

    kb: list[list[InlineKeyboardButton]] = []
    if nav_buttons:
        kb.append(nav_buttons)
    kb.append([InlineKeyboardButton("🔄 Nuova ricerca", callback_data="restart_search")])
    markup = InlineKeyboardMarkup(kb)

    chunks = chunk_messages(lines)
    text = chunks[0] if chunks else ""
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )


def _row_to_item(row: dict) -> tuple[Item, str]:
    import json as _json
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
        recipient_tags=tuple(_json.loads(row.get("recipient_tags") or "[]")),  # Bug #2 fix
    ), row.get("source_name", row["source_id"])


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(_WELCOME, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(_HELP, parse_mode=ParseMode.HTML)


async def cmd_ultimi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    rows = await db.list_last_n_items(10)
    if not rows:
        await update.effective_message.reply_text("ℹ️ Nessun bando nel database ancora.")
        return
    lines = ["📋 <b>Ultimi 10 bandi aggiunti:</b>", ""]
    for row in rows:
        item, src_name = _row_to_item(row)
        lines.append(format_item(item, src_name))
        lines.append("─" * 18)
    for chunk in chunk_messages(lines):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_scadenze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    rows = await db.list_expiring_items(30)
    if not rows:
        await update.effective_message.reply_text("ℹ️ Nessun bando in scadenza nei prossimi 30 giorni.")
        return
    lines = ["⏳ <b>Bandi in scadenza entro 30 giorni:</b>", ""]
    for row in rows:
        item, src_name = _row_to_item(row)
        lines.append(format_item(item, src_name))
        lines.append("─" * 18)
    for chunk in chunk_messages(lines):
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_fonti(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sources: list[Source] = context.bot_data["sources"]
    db: Database = context.bot_data["db"]
    stats = await db.list_sources_stats()
    stats_map = {s["source_id"]: s["count"] for s in stats}

    lines = ["📡 <b>Fonti monitorate:</b>", ""]
    by_level: dict[str, list[Source]] = {}
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

    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_test_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: AppConfig = context.bot_data["cfg"]
    allowed = set(cfg.telegram.chat_ids_resolved())
    user_id = str(update.effective_user.id) if update.effective_user else None
    chat_id = str(update.effective_chat.id)
    if user_id not in allowed and chat_id not in allowed:
        await update.effective_message.reply_text("⛔ Non autorizzato.")
        return

    sources: list[Source] = context.bot_data["sources"]
    db: Database = context.bot_data["db"]

    await update.effective_message.reply_text("🔄 Avvio controllo giornaliero manuale…")
    try:
        async with HttpClient(cfg.http) as httpc:
            await run_daily_check_once(cfg, sources, db, httpc, chat_id)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Errore: {e}")
        log.exception("test_daily error")


# ── /cerca conversation ────────────────────────────────────────────────────────

async def cerca_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["filters"] = {}
    kb = [[InlineKeyboardButton(label, callback_data=f"lvl:{val}")] for label, val in _LEVEL_OPTIONS]
    await update.effective_message.reply_text(
        "🔍 <b>Ricerca Bandi</b>\n\n1️⃣ Seleziona il <b>livello territoriale</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return FILTER_LEVEL


async def cerca_start_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point per il ConversationHandler da callback 'restart_search'."""
    q = update.callback_query
    await q.answer()
    context.user_data["filters"] = {}
    kb = [[InlineKeyboardButton(label, callback_data=f"lvl:{val}")] for label, val in _LEVEL_OPTIONS]
    await q.edit_message_text(
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

    kb = [[InlineKeyboardButton(label, callback_data=f"rel:{val2}")] for label, val2 in _RELEVANCE_OPTIONS]
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

    kb = [[InlineKeyboardButton(label, callback_data=f"rec:{val2}")] for label, val2 in _RECIPIENT_OPTIONS]
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

    kb = [[InlineKeyboardButton(label, callback_data=f"sta:{val2}")] for label, val2 in _STATUS_OPTIONS]
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
        InlineKeyboardButton("✏️ Parola chiave", callback_data="kw:ASK"),
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
        "✏️ Scrivi la parola chiave (o /annulla per saltare):",
        parse_mode=ParseMode.HTML,
    )
    return FILTER_KEYWORD


async def filter_keyword_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    if not text.startswith("/"):
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


# Bug #8 fix: pagination uses short "pg:{N}" callback_data, filters stored in bot_data
async def pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    try:
        page = int(q.data.split(":")[1])
        f = context.user_data.get("pg_filters", {})
        filters_obj = _dict_to_filters(f, page=page)
        db: Database = context.bot_data["db"]
        await _send_paginated_results(update, context, db, filters_obj, edit=True)
    except Exception as e:
        log.error("Pagination callback error: %s", e)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("❌ Ricerca annullata.")
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


# ── Application factory (Bug #6 fix: sync, PTB v22 manages event loop) ────────

def run_bot_polling(cfg: AppConfig, sources: list[Source], db_path: Path) -> None:
    """Synchronous entry point — PTB v22 manages the event loop via app.run_polling()."""

    # Bug #7 fix: use post_init/post_shutdown for proper DB lifecycle
    async def post_init(app: Application) -> None:
        db = Database(db_path)
        await db.__aenter__()
        await db.init()
        app.bot_data["cfg"] = cfg
        app.bot_data["sources"] = sources
        app.bot_data["db"] = db
        log.info("Database initialized at %s", db_path)

    async def post_shutdown(app: Application) -> None:
        db: Database = app.bot_data.get("db")
        if db:
            await db.__aexit__(None, None, None)
            log.info("Database closed.")

    app = (
        Application.builder()
        .token(cfg.telegram.token_resolved())
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # /cerca conversation — Bug #8: restart_search is an entry_point
    cerca_conv = ConversationHandler(
        entry_points=[
            CommandHandler("cerca", cerca_start),
            CallbackQueryHandler(cerca_start_from_callback, pattern=r"^restart_search$"),
        ],
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
    # Bug #8 fix: pattern matches short "pg:{N}" format
    app.add_handler(CallbackQueryHandler(pagination_callback, pattern=r"^pg:\d+$"))

    # Bug #17 fix: use datetime.time() with explicit tzinfo instead of timetz()
    tz = cfg.tz()
    h_daily, m_daily = map(int, cfg.schedule.daily_time.split(":"))
    h_weekly, m_weekly = map(int, cfg.schedule.weekly_time.split(":"))
    _weekday_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    weekly_weekday = _weekday_map.get(cfg.schedule.weekly_day.lower(), 0)

    jq = app.job_queue
    jq.run_daily(job_daily, time=dt_time(hour=h_daily, minute=m_daily, tzinfo=tz))
    jq.run_daily(
        job_weekly,
        time=dt_time(hour=h_weekly, minute=m_weekly, tzinfo=tz),
        days=(weekly_weekday,),
    )

    log.info(
        "Bot polling started. Daily: %s, Weekly: %s %s",
        cfg.schedule.daily_time, cfg.schedule.weekly_day, cfg.schedule.weekly_time,
    )
    app.run_polling(drop_pending_updates=True)
