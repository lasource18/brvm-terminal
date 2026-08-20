"""View models — typed data structures the templates and JSON APIs consume.

Kept separate from `brvm.models` (which represents *storage* shapes) so
UI concerns like formatting hints and derived fields don't leak into the
storage layer.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class IndexTile(BaseModel):
    ticker: str
    name: str
    level: float | None = None
    change_pct: float | None = None
    session_date: date | None = None


class QuoteRow(BaseModel):
    ticker: str
    name: str
    country: str | None = None
    last: float | None = None
    change_pct: float | None = None
    volume: int | None = None
    turnover: float | None = None
    captured_utc: str | None = None


class Overview(BaseModel):
    indices: list[IndexTile] = Field(default_factory=list)
    gainers: list[QuoteRow] = Field(default_factory=list)
    losers: list[QuoteRow] = Field(default_factory=list)
    turnover_leaders: list[QuoteRow] = Field(default_factory=list)
    generated_utc: str
    market_open: bool
    last_snapshot_utc: str | None = None

    @property
    def is_stale(self) -> bool:
        return not self.market_open


class WatchlistView(BaseModel):
    id: int
    slug: str
    name: str
    created_utc: str
    items: list[QuoteRow] = Field(default_factory=list)


class SecurityView(BaseModel):
    ticker: str
    name: str
    kind: str
    country: str | None = None
    isin: str | None = None
    source_url: str | None = None
    quote: QuoteRow | None = None


class SearchHit(BaseModel):
    ticker: str
    name: str
    kind: str
    country: str | None = None


class DirectoryRow(BaseModel):
    ticker: str
    name: str
    kind: str
    country: str | None = None
    sector: str | None = None
    last: float | None = None
    change_pct: float | None = None


class Shareholder(BaseModel):
    name: str
    pct: float


class CompanyProfile(BaseModel):
    """Company profile for the Description tab.

    Fields populated best-effort from whichever source responds. `source`
    identifies which one (sikafinance / afx_kwayisi)."""

    ticker: str
    source: str
    description: str | None = None
    sector: str | None = None
    industry: str | None = None
    address: str | None = None
    phone: str | None = None
    fax: str | None = None
    email: str | None = None
    website: str | None = None
    leadership: str | None = None
    shares_outstanding: str | None = None
    float_pct: str | None = None
    market_cap: str | None = None
    shareholders: list[Shareholder] = Field(default_factory=list)


class PeerRow(BaseModel):
    ticker: str
    name: str
    country: str | None = None
    last: float | None = None
    change_day_pct: float | None = None
    change_ytd_pct: float | None = None
    volume: int | None = None
    market_cap: float | None = None


class PeersView(BaseModel):
    sector: str | None = None
    source: str
    peers: list[PeerRow] = Field(default_factory=list)
