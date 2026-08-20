"""Typed models shared by sources, store, and services.

Parsers return these; the store persists them. Kept intentionally small.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

SecurityKind = Literal["equity", "index", "bond"]


class Security(BaseModel):
    ticker: str
    name: str
    kind: SecurityKind
    isin: str | None = None
    country: str | None = None
    sector: str | None = None
    currency: str = "XOF"
    source_url: str | None = None


class Quote(BaseModel):
    ticker: str
    source: str
    last: float | None = None
    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: int | None = None
    turnover: float | None = None
    change_abs: float | None = None
    change_pct: float | None = None
    is_stale: bool = False


class DailyBar(BaseModel):
    ticker: str
    session_date: date
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: int | None = None
    turnover: float | None = None
    source: str


class IndexLevel(BaseModel):
    ticker: str
    session_date: date
    level: float
    change_pct: float | None = None
    source: str


class Palmares(BaseModel):
    gainers: list[Quote] = Field(default_factory=list)
    losers: list[Quote] = Field(default_factory=list)
    most_active: list[Quote] = Field(default_factory=list)


NewsKind = Literal["news", "communique"]


class NewsItem(BaseModel):
    """A single row from a news feed or communiqué listing.

    `url_hash` is the dedupe key (sha256 of normalized url + '|' + title).
    LLM-tagged fields are populated by the Phase 3b worker; they stay None
    at ingest time.
    """

    source: str
    kind: NewsKind
    url: str
    url_hash: str
    title: str
    chapeau: str | None = None
    issuer_name: str | None = None
    ticker_hint: str | None = None
    published_at: str | None = None  # ISO-8601 UTC


CorporateActionKind = Literal[
    "dividend", "agm", "rights", "split", "admission", "other"
]


class CorporateAction(BaseModel):
    ticker: str
    kind: CorporateActionKind
    ex_date: date | None = None
    pay_date: date | None = None
    amount: float | None = None
    currency: str | None = None
    yield_pct: float | None = None
    note: str | None = None
    source: str
    source_url: str | None = None
