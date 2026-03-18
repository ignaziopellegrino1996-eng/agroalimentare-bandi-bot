from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agrobandi_bot.config import FilteringConfig
from agrobandi_bot.filtering import score_item, looks_like_call

_CFG = FilteringConfig(
    min_score=2,
    include_keywords=[
        "agroalimentare", "cooperativa", "bando", "agricoltura",
        "pesca", "ISMEA", "PSR", "sviluppo rurale",
    ],
    exclude_keywords=["borsa di studio", "selezione personale", "tirocinio"],
)


def test_high_relevance_agro():
    r = score_item(_CFG, "Bando ISMEA per cooperative agroalimentari", "Contributi per agricoltura", "https://ismea.it")
    assert r.ok
    assert r.score >= 4


def test_exclude_keyword_blocks():
    r = score_item(_CFG, "Bando per borsa di studio", "Agroalimentare e agricoltura", "https://example.com")
    assert r.excluded
    assert not r.ok


def test_cooperative_recipient_tag():
    r = score_item(_CFG, "Bando per cooperative siciliane", "Sviluppo rurale PSR Sicilia", "https://sicilia.it")
    assert "cooperative" in r.recipient_tags


def test_looks_like_call():
    assert looks_like_call("Avviso pubblico per contributi", "Bando per finanziamento")
    assert not looks_like_call("Notizie dal settore", "Aggiornamenti mercato")


def test_fishing_recipient_tag():
    r = score_item(_CFG, "Bando pesca e acquacoltura FEAMP", "Misure per pescatori", "https://example.com")
    assert "pesca" in r.recipient_tags
