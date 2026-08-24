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


# Deliberately loose: brvm.org filenames + sikafinance titles carry a mix of
# labels ("Etats financiers", "Rapport d'activités annuel", etc.) and we
# want a Small classifier here rather than a leaky Literal that has to grow
# every time a new title shape shows up.
FilingDocType = Literal[
    "etats_financiers",     # États financiers (annual or interim)
    "rapport_annuel",       # Rapport annuel / Rapport d'activités annuel
    "rapport_activites",    # Rapport d'activités (quarter, semester)
    "resultats",            # "Résultats", "Communiqué de résultats"
    "rse",                  # Rapport RSE
    "assemblee",            # Convocations, projets de résolutions, procurations
    "autre",
]

FilingPeriodKind = Literal["annual", "H1", "Q1", "Q3", "other"]


class Filing(BaseModel):
    """One PDF report ingested from a public source.

    Storage-shape only — the view layer gets a separate model in Phase 4b.
    `url_hash` is the dedupe key so we never re-download; `sha256` is the
    hash of the actual bytes and lets us notice if the same URL later serves
    different content.
    """

    ticker: str
    issuer_name: str | None = None
    doc_type: FilingDocType = "autre"
    period_kind: FilingPeriodKind | None = None
    period_year: int | None = None
    period_label: str | None = None
    source: str                              # 'brvm_org' | 'sikafinance'
    source_url: str
    url_hash: str
    published_date: date | None = None
    file_path: str                           # relative to project root
    size_bytes: int
    sha256: str
    page_count: int | None = None


# --- Alerts (Phase 6a) -----------------------------------------------------

AlertKind = Literal["price_move", "new_filing", "news"]
AlertDeliveryStatus = Literal["ok", "failed", "skipped"]


class AlertRule(BaseModel):
    """One user-configured trigger.

    `ticker=None` means "any security" (watchlist-wide). Only the fields
    relevant to `kind` are read — the rest are ignored at eval time, but
    kept on the row so the /alerts page can round-trip a rule verbatim.
    """

    id: int | None = None
    kind: AlertKind
    ticker: str | None = None
    threshold_pct: float | None = None       # price_move: |change_pct| trigger
    min_relevance: int | None = None         # news: Haiku relevance floor
    doc_types: str | None = None             # new_filing: CSV of FilingDocType
    label: str | None = None
    enabled: bool = True


class AlertEvent(BaseModel):
    """One fired alert. `dedupe_key` is the natural identity of the *thing*
    that fired — a snapshot id, filing id, news id, etc. — so a rule that
    keeps matching only produces one row per underlying event."""

    id: int | None = None
    rule_id: int
    kind: AlertKind
    ticker: str | None = None
    subject: str
    body: str
    payload_json: str | None = None
    dedupe_key: str
    fired_utc: str | None = None             # populated by the store on insert
    delivered_utc: str | None = None
    delivery_status: AlertDeliveryStatus | None = None
