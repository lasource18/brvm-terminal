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


class CorporateActionRow(BaseModel):
    id: int
    ticker: str
    name: str | None = None      # joined from securities.name for display
    kind: str
    ex_date: date | None = None
    pay_date: date | None = None
    amount: float | None = None
    currency: str | None = None
    yield_pct: float | None = None
    note: str | None = None
    source: str | None = None
    source_url: str | None = None


class Overview(BaseModel):
    indices: list[IndexTile] = Field(default_factory=list)
    gainers: list[QuoteRow] = Field(default_factory=list)
    losers: list[QuoteRow] = Field(default_factory=list)
    turnover_leaders: list[QuoteRow] = Field(default_factory=list)
    upcoming_actions: list[CorporateActionRow] = Field(default_factory=list)
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
    # Period returns vs the close/level on-or-before the reference date.
    # None when the reference bar isn't available (short-history tickers,
    # brand-new indices captured only from `just snapshot` runs). Zero
    # would be misleading — the template renders "—" instead.
    change_1w_pct: float | None = None
    change_1m_pct: float | None = None
    change_3m_pct: float | None = None
    change_ytd_pct: float | None = None
    change_1y_pct: float | None = None
    # All-time return vs the *earliest* recorded close/level for the
    # ticker. Meaningful once we've accumulated enough history — a
    # brand-new ticker whose earliest bar is also today returns 0%.
    change_all_pct: float | None = None


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
    # Phase 4d — headline ratios for cross-ticker comparison. Sourced from
    # `services/ratios.get_latest_ratios(ticker)`; None when the peer
    # hasn't been through fundamentals extraction yet (or when the ratio
    # couldn't be computed for the usual missing-data reasons).
    pe: float | None = None
    roe: float | None = None            # percentage points, e.g. 12.3
    net_margin: float | None = None     # percentage points
    # True for the "self" row appended to the peers list so the currently-
    # viewed company shows up in the comparison table (rendered at the
    # bottom, visually distinguished by the template).
    is_self: bool = False


class PeerStats(BaseModel):
    """Median + mean of a single ratio across the non-self peer list.

    Both are None when fewer than 2 peers reported the field (a single
    sample is not a stable central tendency). `n` is the sample size so
    the UI can dim the row / show a tooltip when the stats are thin.
    """

    median: float | None = None
    mean: float | None = None
    n: int = 0


class PeersView(BaseModel):
    sector: str | None = None
    source: str
    peers: list[PeerRow] = Field(default_factory=list)
    # Phase 8g: per-ratio median + mean across the peer set (self row
    # excluded). Sonnet has been reading these for a while via
    # analyst-notes' `_peer_medians`; the Peers tab now surfaces them too
    # so a reader can eyeball the same comparison the model sees.
    stats: dict[str, PeerStats] = Field(default_factory=dict)


class NewsRow(BaseModel):
    """One news / communiqué row as rendered by the UI.

    `tickers` is the union of `ticker_hint` (fast pre-filter) and
    `tickers_llm` (LLM attribution), deduped in insertion order — the
    template only cares about the display set.
    """

    id: int
    source: str
    kind: str          # 'news' | 'communique'
    url: str
    title: str
    chapeau: str | None = None
    issuer_name: str | None = None
    tickers: list[str] = Field(default_factory=list)
    relevance: int | None = None
    category: str | None = None
    summary_fr: str | None = None
    summary_en: str | None = None
    published_at: str | None = None
    fetched_utc: str | None = None

    @property
    def is_tagged(self) -> bool:
        return self.category is not None or bool(self.tickers) or self.relevance is not None


class NewsFeed(BaseModel):
    items: list[NewsRow] = Field(default_factory=list)
    total: int = 0
    limit: int = 25
    offset: int = 0
    filters: dict = Field(default_factory=dict)

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    @property
    def next_offset(self) -> int:
        return self.offset + self.limit

    @property
    def page(self) -> int:
        return (self.offset // self.limit) + 1 if self.limit else 1

    @property
    def total_pages(self) -> int:
        if not self.limit:
            return 1
        return max(1, (self.total + self.limit - 1) // self.limit)
