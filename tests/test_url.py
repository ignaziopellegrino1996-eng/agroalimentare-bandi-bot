from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agrobandi_bot.sources import canonicalize_url
from agrobandi_bot.db import stable_item_id


def test_canonicalize_removes_utm():
    url = "https://masaf.gov.it/bando?utm_source=newsletter&utm_medium=email"
    clean = canonicalize_url(url)
    assert "utm_source" not in clean
    assert "masaf.gov.it" in clean


def test_canonicalize_removes_fragment():
    url = "https://ismea.it/bandi#sezione-avvisi"
    clean = canonicalize_url(url)
    assert "#" not in clean


def test_canonicalize_lowercase_domain():
    url = "https://MASAF.GOV.IT/bando"
    clean = canonicalize_url(url)
    assert clean.startswith("https://masaf.gov.it/")


def test_stable_id_with_external_id():
    id1 = stable_item_id("masaf", "https://masaf.gov.it/bando/1", "Bando Test", "ext-001")
    id2 = stable_item_id("masaf", "https://masaf.gov.it/bando/1", "Bando Test", "ext-001")
    assert id1 == id2


def test_stable_id_different_sources():
    id1 = stable_item_id("masaf", "https://example.com/bando", "Bando Test")
    id2 = stable_item_id("ismea", "https://example.com/bando", "Bando Test")
    assert id1 != id2


def test_stable_id_same_url_no_external_id():
    id1 = stable_item_id("masaf", "https://masaf.gov.it/bando?page=1", "Bando")
    id2 = stable_item_id("masaf", "https://masaf.gov.it/bando?page=1", "Bando")
    assert id1 == id2
