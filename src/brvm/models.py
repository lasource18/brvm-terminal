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
