from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

import feedparser
from bs4 import BeautifulSoup, Tag
from dateutil import parser as dateparser

from .http_client import HttpClient
from .models import Item, Source

log = logging.getLogger(__name__)

# ── URL utilities ──────────────────────────────────────────────────────────────

_UTM_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}


def canonicalize_url(url: str) -> str:
    try:
        p = urlparse(url)
        qs = {k: v for k, v in parse_qs(p.query).items() if k not in _UTM_PARAMS}
        clean_qs = urlencode({k: v[0] for k, v in qs.items()}, quote_via=lambda s, *_: s)
        return urlunparse(p._replace(netloc=p.netloc.lower(), query=clean_qs, fragment=""))
    except Exception:
        return url


def _shorten(text: str, max_len: int = 400) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len] + "…" if len(text) > max_len else text


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


# ── Date extraction ────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")
_DEADLINE_HINTS = re.compile(
    r"(?:scadenza|entro il|termine|entro|deadline|chiusura)[:\s]*"
    r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4})",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _iso_or_none(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = text.strip()
    if _ISO_DATE_RE.fullmatch(text[:10]):
        return text[:10]
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            pass
    try:
        parsed = dateparser.parse(text, dayfirst=True, fuzzy=True)
        if parsed is not None:  # Bug #18 fix: guard against None before .date()
            return parsed.date().isoformat()
    except Exception:
        pass
    return None


def _extract_deadline(text: str) -> Optional[str]:
    m = _DEADLINE_HINTS.search(text)
    if m:
        return _iso_or_none(m.group(1))
    return None


def _extract_first_date(text: str) -> Optional[str]:
    m = _DATE_RE.search(text)
    if m:
        return _iso_or_none(f"{m.group(1)}/{m.group(2)}/{m.group(3)}")
    return None


def _extract_published_from_page(soup: BeautifulSoup) -> Optional[str]:
    # Try <time datetime=...>
    for tag in soup.find_all("time", attrs={"datetime": True}):
        d = _iso_or_none(tag["datetime"])
        if d:
            return d
    # Try meta tags
    for attr in ("article:published_time", "datePublished", "DC.date"):
        meta = soup.find("meta", attrs={"property": attr}) or soup.find("meta", attrs={"name": attr})
        if meta and meta.get("content"):
            d = _iso_or_none(meta["content"])
            if d:
                return d
    return None


def _best_summary(soup: BeautifulSoup, hint_keywords: list[str] | None = None) -> str:
    for selector in ["article", "main", ".content", "#content", ".entry-content", ".post-content"]:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(" ", strip=True)
            if len(text) > 80:
                return _shorten(text)
    return _shorten(soup.get_text(" ", strip=True))


# ── Generic HTML parser ────────────────────────────────────────────────────────

_BANDO_TITLE_RE = re.compile(
    r"(bando|avviso|contribut|finanziament|agevolazion|incentiv|misura|call|psr|psr sicilia|gal|leader)",
    re.IGNORECASE,
)
_MIN_LINK_TEXT = 20


def _is_bando_link(text: str, href: str) -> bool:
    if len(text) < _MIN_LINK_TEXT:
        return False
    if _BANDO_TITLE_RE.search(text):
        return True
    return False


def parse_generic_links(html_text: str, base_url: str) -> list[dict]:
    soup = _soup(html_text)
    results: list[dict] = []
    seen_hrefs: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        abs_url = urljoin(base_url, href)
        if abs_url in seen_hrefs:
            continue
        text = a.get_text(" ", strip=True)
        if not text or len(text) < _MIN_LINK_TEXT:
            continue

        # Get surrounding context for summary
        parent = a.find_parent(["li", "div", "article", "section", "tr"])
        context = parent.get_text(" ", strip=True) if parent else text
        deadline = _extract_deadline(context)
        published = None
        for tag in (parent.find_all("time") if parent is not None else []):  # Bug #11 fix
            published = _iso_or_none(tag.get("datetime", ""))
            if published:
                break

        seen_hrefs.add(abs_url)
        results.append({
            "title": text[:200],
            "url": abs_url,
            "summary": _shorten(context, 400),
            "published": published,
            "deadline": deadline,
        })
    return results


# ── Domain-specific parsers ────────────────────────────────────────────────────

def parse_masaf_bandi(html_text: str, base_url: str) -> list[dict]:
    soup = _soup(html_text)
    results: list[dict] = []
    # Try article/news listing patterns
    for item in soup.select(".views-row, .field-item, article.node, .content-item, li.item"):
        a = item.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        url = urljoin(base_url, a["href"])
        context = item.get_text(" ", strip=True)
        results.append({
            "title": title[:200],
            "url": url,
            "summary": _shorten(context),
            "published": _extract_first_date(context),
            "deadline": _extract_deadline(context),
        })
    if not results:
        return parse_generic_links(html_text, base_url)
    return results


def parse_ismea_bandi(html_text: str, base_url: str) -> list[dict]:
    soup = _soup(html_text)
    results: list[dict] = []
    for item in soup.select(".bando-item, .views-row, .item-list li, article, .node"):
        a = item.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        url = urljoin(base_url, a["href"])
        context = item.get_text(" ", strip=True)
        results.append({
            "title": title[:200],
            "url": url,
            "summary": _shorten(context),
            "published": _extract_first_date(context),
            "deadline": _extract_deadline(context),
        })
    if not results:
        return parse_generic_links(html_text, base_url)
    return results


def parse_invitalia_bandi(html_text: str, base_url: str) -> list[dict]:
    soup = _soup(html_text)
    results: list[dict] = []
    for item in soup.select(".incentivo-item, .card, .views-row, article, .incentivo"):
        a = item.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        url = urljoin(base_url, a["href"])
        context = item.get_text(" ", strip=True)
        results.append({
            "title": title[:200],
            "url": url,
            "summary": _shorten(context),
            "published": None,
            "deadline": _extract_deadline(context),
        })
    if not results:
        return parse_generic_links(html_text, base_url)
    return results


def parse_sicilia_bandi(html_text: str, base_url: str) -> list[dict]:
    soup = _soup(html_text)
    results: list[dict] = []
    for item in soup.select(".views-row, .bando, article, .field-content, li.views-row"):
        a = item.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        url = urljoin(base_url, a["href"])
        context = item.get_text(" ", strip=True)
        deadline = _extract_deadline(context)
        results.append({
            "title": title[:200],
            "url": url,
            "summary": _shorten(context),
            "published": _extract_first_date(context),
            "deadline": deadline,
        })
    if not results:
        return parse_generic_links(html_text, base_url)
    return results


def parse_sicilia_regione_bandi(html_text: str, base_url: str) -> list[dict]:
    return parse_sicilia_bandi(html_text, base_url)


def parse_fasi_bandi(html_text: str, base_url: str) -> list[dict]:
    soup = _soup(html_text)
    results: list[dict] = []
    for item in soup.select(".bando-row, .bando-item, .agevolazione, tr, .row-bando"):
        a = item.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        url = urljoin(base_url, a["href"])
        context = item.get_text(" ", strip=True)
        results.append({
            "title": title[:200],
            "url": url,
            "summary": _shorten(context),
            "published": _extract_first_date(context),
            "deadline": _extract_deadline(context),
        })
    if not results:
        return parse_generic_links(html_text, base_url)
    return results


def parse_europafacile_bandi(html_text: str, base_url: str) -> list[dict]:
    soup = _soup(html_text)
    results: list[dict] = []
    for item in soup.select(".bando, .bando-item, .result-item, article, .views-row"):
        a = item.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        url = urljoin(base_url, a["href"])
        context = item.get_text(" ", strip=True)
        results.append({
            "title": title[:200],
            "url": url,
            "summary": _shorten(context),
            "published": _extract_first_date(context),
            "deadline": _extract_deadline(context),
        })
    if not results:
        return parse_generic_links(html_text, base_url)
    return results


def parse_feampa_bandi(html_text: str, base_url: str) -> list[dict]:
    """Parser specifico per feampabandionline.it — struttura con .bandi-archive-title e card bandi."""
    soup = _soup(html_text)
    results: list[dict] = []
    seen: set[str] = set()

    # Cerca i blocchi bando: ogni articolo/card ha un titolo linkato
    for card in soup.select("article, .bando-item, .post, .entry"):
        a = card.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        url = urljoin(base_url, a["href"])
        if url in seen:
            continue
        seen.add(url)
        context = card.get_text(" ", strip=True)
        results.append({
            "title": title[:200],
            "url": url,
            "summary": _shorten(context),
            "published": _extract_first_date(context),
            "deadline": _extract_deadline(context),
        })

    # Fallback: cerca titoli con classe specifica
    if not results:
        for heading in soup.select(".bandi-archive-title, h2 a, h3 a, .entry-title a"):
            if heading.name == "a":
                a = heading
            else:
                a = heading.find("a", href=True)
            if not a:
                continue
            title = a.get_text(strip=True)
            if not title or len(title) < 10:
                continue
            url = urljoin(base_url, a["href"])
            if url in seen:
                continue
            seen.add(url)
            parent = a.find_parent(["article", "div", "li", "section"])
            context = parent.get_text(" ", strip=True) if parent else title
            results.append({
                "title": title[:200],
                "url": url,
                "summary": _shorten(context),
                "published": _extract_first_date(context),
                "deadline": _extract_deadline(context),
            })

    if not results:
        return parse_generic_links(html_text, base_url)
    return results


def parse_wordpress_news(html_text: str, base_url: str) -> list[dict]:
    """Parser generico per siti WordPress con articoli news (es. OCM Vino)."""
    soup = _soup(html_text)
    results: list[dict] = []
    seen: set[str] = set()

    for article in soup.select("article, .post, .entry, .et_pb_post"):
        a = article.find("a", href=True)
        if not a:
            continue
        # Prefer heading link
        h = article.find(["h1", "h2", "h3"])
        if h:
            ha = h.find("a", href=True)
            if ha:
                a = ha
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        url = urljoin(base_url, a["href"])
        if url in seen:
            continue
        seen.add(url)
        context = article.get_text(" ", strip=True)
        results.append({
            "title": title[:200],
            "url": url,
            "summary": _shorten(context),
            "published": _extract_first_date(context),
            "deadline": _extract_deadline(context),
        })

    if not results:
        return parse_generic_links(html_text, base_url)
    return results


_HTML_PARSERS: dict = {
    "generic_links": parse_generic_links,
    "masaf_bandi": parse_masaf_bandi,
    "ismea_bandi": parse_ismea_bandi,
    "invitalia_bandi": parse_invitalia_bandi,
    "sicilia_bandi": parse_sicilia_bandi,
    "sicilia_regione_bandi": parse_sicilia_regione_bandi,
    "fasi_bandi": parse_fasi_bandi,
    "europafacile_bandi": parse_europafacile_bandi,
    "feampa_bandi": parse_feampa_bandi,
    "wordpress_news": parse_wordpress_news,
}


# ── Fetch functions ────────────────────────────────────────────────────────────

async def fetch_rss(source: Source, httpc: HttpClient) -> list[Item]:
    try:
        text = await httpc.get_text(source.url, timeout=60)
        feed = feedparser.parse(text)
        items: list[Item] = []
        for entry in feed.entries[:30]:
            title = entry.get("title", "").strip()
            url = entry.get("link", "").strip()
            if not title or not url:
                continue
            summary = entry.get("summary", "")
            summary = re.sub(r"<[^>]+>", " ", summary)
            summary = _shorten(summary)

            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    published = datetime(*entry.published_parsed[:3]).date().isoformat()
                except Exception:
                    pass

            deadline = _extract_deadline(f"{title} {summary}")
            canon = canonicalize_url(url)

            items.append(Item(
                source_id=source.id,
                title=title,
                url=url,
                canonical_url=canon,
                level=source.level,
                published=published,
                deadline=deadline,
                summary=summary,
            ))
        return items
    except Exception as e:
        log.error("RSS fetch failed for %s: %s", source.id, e)
        return []


async def fetch_html(source: Source, httpc: HttpClient) -> list[Item]:
    try:
        html_text = await httpc.get_text(source.url, timeout=75)
        parser_fn = _HTML_PARSERS.get(source.parser or "generic_links", parse_generic_links)
        raw_items = parser_fn(html_text, source.url)
        items: list[Item] = []
        for r in raw_items[:50]:
            title = r.get("title", "").strip()
            url = r.get("url", "").strip()
            if not title or not url:
                continue
            canon = canonicalize_url(url)
            items.append(Item(
                source_id=source.id,
                title=title,
                url=url,
                canonical_url=canon,
                level=source.level,
                published=r.get("published"),
                deadline=r.get("deadline"),
                summary=r.get("summary", ""),
            ))
        return items
    except Exception as e:
        log.error("HTML fetch failed for %s (%s): %s", source.id, source.url, e)
        return []


async def enrich_item_from_detail(source: Source, httpc: HttpClient, item: Item) -> Item:
    try:
        html_text = await httpc.get_text(item.url, timeout=20)
        soup = _soup(html_text)
        summary = _best_summary(soup)
        published = item.published or _extract_published_from_page(soup)
        deadline = item.deadline or _extract_deadline(soup.get_text(" ", strip=True))
        return Item(
            source_id=item.source_id,
            title=item.title,
            url=item.url,
            canonical_url=item.canonical_url,
            level=item.level,
            published=published,
            deadline=deadline,
            summary=summary,
            external_id=item.external_id,
            relevance_score=item.relevance_score,
            recipient_tags=item.recipient_tags,
            meta=item.meta,
        )
    except Exception as e:
        log.debug("Detail enrich failed for %s: %s", item.url, e)
        return item


async def fetch_items_for_source(source: Source, httpc: HttpClient, now: datetime) -> list[Item]:
    if source.kind == "rss":
        return await fetch_rss(source, httpc)
    return await fetch_html(source, httpc)
