from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Level(str, Enum):
    EU = "EU"
    IT = "IT"
    SICILIA = "SICILIA"

    @property
    def emoji(self) -> str:
        return {"EU": "🇪🇺", "IT": "🇮🇹", "SICILIA": "🏴"}[self.value]

    @property
    def label(self) -> str:
        return {"EU": "Europeo", "IT": "Nazionale", "SICILIA": "Sicilia"}[self.value]


class Relevance(str, Enum):
    HIGH = "alta"
    MEDIUM = "media"
    ALL = "tutte"


class Status(str, Enum):
    OPEN = "aperto"
    EXPIRING = "in_scadenza"
    EXPECTED = "atteso"
    ALL = "tutti"


class Recipient(str, Enum):
    COOPERATIVE = "cooperative"
    SME = "pmi"
    YOUTH_WOMEN = "giovani_donne"
    FISHING = "pesca"
    ALL = "tutti"


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    level: str
    kind: str
    url: str
    enabled: bool
    parser: Optional[str] = None


@dataclass(frozen=True)
class Item:
    source_id: str
    title: str
    url: str
    canonical_url: str
    level: str
    published: Optional[str]
    deadline: Optional[str]
    summary: str
    external_id: Optional[str] = None
    relevance_score: int = 0
    recipient_tags: tuple[str, ...] = field(default_factory=tuple)
    meta: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class RunStats:
    total_candidates: int
    new_items: int
    sent_items: int
    errors_by_source: dict[str, str]


@dataclass
class SearchFilters:
    level: Optional[str] = None          # EU / IT / SICILIA
    relevance: Optional[str] = None      # alta / media
    recipient: Optional[str] = None      # cooperative / pmi / giovani_donne / pesca
    status: Optional[str] = None         # aperto / in_scadenza / atteso
    keyword: Optional[str] = None
    page: int = 0
    page_size: int = 5
