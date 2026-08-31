"""afx.kwayisi.org/brvm parsers.

Used as a cross-check for sikafinance snapshots and (per-ticker) a source
of the last ~10 daily bars in tidy English formatting.

Verified page structures (fixtures captured 2026-08-18):

* /brvm/ — one big table under `<h2>Listed companies/securities</h2>`.
  Columns: Ticker (link), Name (link), Volume, Price, Change (with
  class=hi / class=lo hint). English numbers (comma thousands).

* /brvm/<ticker>.html — top block has "SNTS \u2022 32,500 \u25b4 600 (1.88%)"
  in a `.h2` div; a "Last Trading Results" table; a `<table data-hist>`
  with columns Date (YYYY-MM-DD), Volume, Close, Change, Change%.
"""

from __future__ import annotations

import contextlib
import re
from datetime import date, datetime

import httpx
from selectolax.parser import HTMLParser

from kodji.models import DailyBar, Quote
from kodji.sources._http import make_client
from kodji.sources._num import parse_en_number

SOURCE_NAME = "afx_kwayisi"
BASE = "https://afx.kwayisi.org"


_TICKER_HREF_RE = re.compile(r"/brvm/([a-z0-9-]+)\.html$")


def _ticker_from_href(href: str) -> str | None:
    m = _TICKER_HREF_RE.search(href)
    return m.group(1).upper() if m else None


def parse_home(html: str) -> list[Quote]:
    """Return a Quote per listed security from the front-page table."""
    tree = HTMLParser(html)
    quotes: list[Quote] = []
    # The listing table lives in the section right after the H2. Grab every
    # table on the page and pick the one whose header cells are exactly
    # Ticker / Name / Volume / Price / Change.
    for tbl in tree.css("table"):
        headers = [th.text(strip=True) for th in tbl.css("thead th")]
        if headers != ["Ticker", "Name", "Volume", "Price", "Change"]:
            continue
        for r in tbl.css("tbody tr"):
            cells = r.css("td")
            if len(cells) < 5:
                continue
            a = cells[0].css_first("a")
            if a is None:
                continue
            ticker = _ticker_from_href(a.attributes.get("href") or "")
            if ticker is None:
                continue
            try:
                volume = int(parse_en_number(cells[2].text(strip=True)))
                last = parse_en_number(cells[3].text(strip=True))
            except ValueError:
                continue
            change_txt = cells[4].text(strip=True)
            try:
                change_abs = parse_en_number(change_txt) if change_txt else None
            except ValueError:
                change_abs = None
            quotes.append(
                Quote(
                    ticker=ticker,
                    source=SOURCE_NAME,
                    last=last,
                    volume=volume,
                    change_abs=change_abs,
                )
            )
        break
    return quotes


def parse_ticker_page(html: str, ticker: str) -> tuple[Quote, list[DailyBar]]:
    """Parse a single-security page: current-day quote + last-N daily bars."""
    tree = HTMLParser(html)

    # --- top-line quote from the .h2 div ---
    last: float | None = None
    change_abs: float | None = None
    change_pct: float | None = None
    top = tree.css_first("div.h2")
    if top is not None:
        text = top.text(strip=True)
        # e.g. "SNTS \u2022 32,500 \u25b4 600 (1.88%) 8 hours ago"
        m = re.search(
            r"([0-9][\d,]*(?:\.\d+)?)\s*[\u25b4\u25be\u2022]?\s*"
            r"([+-]?[\d,]+(?:\.\d+)?)?\s*"
            r"\(([+-]?\d+(?:\.\d+)?)%\)",
            text,
        )
        if m:
            with contextlib.suppress(ValueError):
                last = parse_en_number(m.group(1))
            if m.group(2):
                with contextlib.suppress(ValueError):
                    change_abs = parse_en_number(m.group(2))
            with contextlib.suppress(ValueError):
                change_pct = parse_en_number(m.group(3))

    # --- Last Trading Results ---
    open_ = high = low = None
    volume: int | None = None
    turnover_raw: str | None = None
    for tbl in tree.css("table"):
        thead = tbl.css_first("thead")
        if thead is None:
            continue
        head_text = thead.text(strip=True)
        if "Last Trading Results" not in head_text:
            continue
        for r in tbl.css("tbody tr"):
            cells = r.css("td")
            if len(cells) != 2:
                continue
            label = cells[0].text(strip=True)
            val = cells[1].text(strip=True)
            if not val:
                continue
            try:
                if label == "Opening Price":
                    open_ = parse_en_number(val)
                elif label == "Day\u2019s Low Price" or label == "Day's Low Price":
                    low = parse_en_number(val)
                elif label == "Day\u2019s High Price" or label == "Day's High Price":
                    high = parse_en_number(val)
                elif label == "Traded Volume":
                    volume = int(parse_en_number(val))
                elif label == "Gross Turnover":
                    turnover_raw = val
            except ValueError:
                continue
        break

    quote = Quote(
        ticker=ticker,
        source=SOURCE_NAME,
        last=last,
        open=open_,
        high=high,
        low=low,
        volume=volume,
        change_abs=change_abs,
        change_pct=change_pct,
        turnover=_parse_short_money(turnover_raw) if turnover_raw else None,
    )

    # --- daily bars from data-hist ---
    bars: list[DailyBar] = []
    hist = tree.css_first("table[data-hist]")
    if hist is not None:
        for r in hist.css("tbody tr"):
            cells = [c.text(strip=True) for c in r.css("td")]
            if len(cells) < 3:
                continue
            try:
                session = _parse_iso_date(cells[0])
                v = int(parse_en_number(cells[1]))
                close = parse_en_number(cells[2])
            except ValueError:
                continue
            bars.append(
                DailyBar(
                    ticker=ticker,
                    session_date=session,
                    close=close,
                    volume=v,
                    source=SOURCE_NAME,
                )
            )

    return quote, bars


def parse_factsheet(html: str) -> dict:
    """Extract the `<div data-fact>` block on a single-ticker page.

    Returns keys among: sector, industry, address, telephone, email, website.
    Values are strings; missing / dash-only fields are omitted.
    """
    tree = HTMLParser(html)
    block = tree.css_first("div[data-fact]")
    if block is None:
        return {}
    out: dict = {}
    # The <dl> mixes single-item <div><dt><dd> rows with pair rows
    # (`<div><div><dt><dd></div><div><dt><dd></div></div>`). Iterate every
    # <dt> under the block and pair with the following <dd>.
    for dt in block.css("dt"):
        parent = dt.parent
        if parent is None:
            continue
        dd = parent.css_first("dd")
        if dd is None:
            continue
        label = dt.text(strip=True).lower()
        value = dd.text(strip=True)
        if not value or value in {"—", "-"}:
            continue
        out[label] = value
    return out


def parse_competitors(html: str, exclude_ticker: str | None = None) -> list[dict]:
    """Extract the `<table data-comp>` peers block.

    Returns rows like `{"ticker","name","market_cap","last","change_ytd_pct"}`.
    Market cap is normalised to a float in XOF via `_parse_short_money`.
    """
    tree = HTMLParser(html)
    tbl = tree.css_first("table[data-comp]")
    if tbl is None:
        return []
    rows: list[dict] = []
    for r in tbl.css("tbody tr"):
        cells = r.css("td")
        if len(cells) < 5:
            continue
        a = cells[0].css_first("a")
        if a is None:
            continue
        ticker = _ticker_from_href(a.attributes.get("href") or "")
        if ticker is None or ticker == exclude_ticker:
            continue
        rows.append(
            {
                "ticker": ticker,
                "name": cells[1].text(strip=True),
                "market_cap": _parse_short_money(cells[2].text(strip=True)),
                "last": _safe_en_num(cells[3].text(strip=True)),
                "change_ytd_pct": _safe_en_num(cells[4].text(strip=True)),
            }
        )
    return rows


def _safe_en_num(s: str) -> float | None:
    try:
        return parse_en_number(s)
    except ValueError:
        return None


_SUFFIX_MULTIPLIER = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _parse_short_money(s: str) -> float | None:
    """afx uses "96.2M" / "3.25T" style. Return absolute XOF as float."""
    s = s.strip()
    m = re.match(r"^([\d.,]+)\s*([KMBT])$", s)
    if not m:
        try:
            return parse_en_number(s)
        except ValueError:
            return None
    try:
        return parse_en_number(m.group(1)) * _SUFFIX_MULTIPLIER[m.group(2)]
    except ValueError:
        return None


def _parse_iso_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# ---------- fetchers ----------


def fetch_home(client: httpx.Client | None = None) -> list[Quote]:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}/brvm/")
        r.raise_for_status()
        return parse_home(r.text)
    finally:
        if close:
            client.close()


def fetch_ticker(
    ticker: str, client: httpx.Client | None = None
) -> tuple[Quote, list[DailyBar]]:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}/brvm/{ticker.lower()}.html")
        r.raise_for_status()
        return parse_ticker_page(r.text, ticker.upper())
    finally:
        if close:
            client.close()
