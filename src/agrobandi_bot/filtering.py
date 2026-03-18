from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import FilteringConfig

_RECIPIENT_KEYWORDS: dict[str, list[str]] = {
    "cooperative": ["cooperativa", "cooperativismo", "cooperativ", "legacoop", "confcooperative"],
    "pmi": ["PMI", "piccola impresa", "media impresa", "microimpresa", "MPMI", "impresa agricola"],
    "giovani_donne": ["giovani", "giovane imprenditore", "donne", "imprenditoria femminile", "under 40", "under40"],
    "pesca": ["pesca", "acquacoltura", "pescatore", "pescatori", "flotta peschereccia", "FEAMP"],
}

_BANDO_SIGNALS = [
    "bando", "avviso", "contribut", "finanziament", "agevolazion", "incentiv",
    "misura", "programma", "call", "open call", "sostegno", "voucher",
]


@dataclass
class ScoreResult:
    score: int
    matched_keywords: list[str]
    excluded: bool
    ok: bool
    recipient_tags: list[str]


def score_item(cfg: FilteringConfig, title: str, summary: str, url: str) -> ScoreResult:
    text = f"{title} {summary} {url}".lower()

    # Check exclude keywords first
    for kw in cfg.exclude_keywords:
        if kw.lower() in text:
            return ScoreResult(score=0, matched_keywords=[], excluded=True, ok=False, recipient_tags=[])

    score = 0
    matched: list[str] = []
    for kw in cfg.include_keywords:
        if kw.lower() in text:
            score += 2 if len(kw) >= 6 else 1
            matched.append(kw)

    # Detect recipient tags
    recipient_tags: list[str] = []
    for tag, keywords in _RECIPIENT_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text:
                recipient_tags.append(tag)
                break

    return ScoreResult(
        score=score,
        matched_keywords=matched,
        excluded=False,
        ok=score >= cfg.min_score,
        recipient_tags=list(set(recipient_tags)),
    )


def looks_like_call(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(sig in text for sig in _BANDO_SIGNALS)


def relevance_label(score: int) -> str:
    if score >= 6:
        return "⭐⭐⭐ Alta"
    if score >= 3:
        return "⭐⭐ Media"
    return "⭐ Bassa"


def relevance_stars(score: int) -> str:
    if score >= 6:
        return "⭐⭐⭐"
    if score >= 3:
        return "⭐⭐"
    return "⭐"
