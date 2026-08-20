"""sikafinance.com parsers and fetchers.

Parsers are pure functions (HTML str -> models). Fetchers wrap them with
network I/O via the shared httpx client.

Verified page structures (fixtures captured 2026-08-18):

* /marches/aaz — two tables:
    - Indices table: rows with links like `/marches/cotation_BRVMC` (no
      country suffix). Columns: name, ouv, +haut, +bas, dernier, variation.
    - Equities table `#tblShare`: rows link `/marches/cotation_XXX.cc`.
      Columns: name, ouv, +haut, +bas, vol titres, vol XOF, dernier, variation.

* /marches/cotation_<TICKER>[.cc] — header block `.quotebarE` has H1 name,
  country flag `<img src="/i/<cc>.png">`, and the ISIN+ticker text. Big
  price is inside `.cot1u`; details in `table.tbl100_4`.

* /marches/historiques/<TICKER>.cc — table `#tblhistos`. Columns:
  Date DD/MM/YYYY, Clôture, Plus bas, Plus haut, Ouverture, Volume Titres,
  Volume FCFA, Variation %.
"""

from __future__ import annotations

import contextlib
import re
from datetime import date, datetime
from urllib.parse import urlparse

import httpx
from selectolax.parser import HTMLParser, Node

from brvm.models import DailyBar, IndexLevel, Quote, Security
from brvm.sources._http import make_client
from brvm.sources._num import parse_number

SOURCE_NAME = "sikafinance"
BASE = "https://www.sikafinance.com"

_COTATION_RE = re.compile(r"^/marches/cotation_([A-Z0-9-]+)(?:\.([a-z]{2}))?$")
_FLAG_RE = re.compile(r"/i/([a-z]{2})\.png")


def _row_cells_text(row: Node) -> list[str]:
    return [c.text(strip=True) for c in row.css("td")]


def _href(a: Node | None) -> str | None:
    if a is None:
        return None
    return a.attributes.get("href")


def _ticker_from_href(href: str) -> tuple[str, str | None]:
    """Return (ticker, country_code_or_None). Raises on malformed href."""
    m = _COTATION_RE.match(href)
    if not m:
        raise ValueError(f"not a cotation href: {href!r}")
    return m.group(1), m.group(2)


def parse_aaz(html: str) -> tuple[list[Security], list[Quote], list[IndexLevel]]:
    """Parse the A-to-Z listing page.

    Returns:
      securities: canonical equities + indices (name, kind, country, source_url)
      quotes: today's snapshot for every equity (with volume/turnover)
      index_levels: today's level for every index / composite gauge
    """
    tree = HTMLParser(html)
    securities: list[Security] = []
    quotes: list[Quote] = []
    index_levels: list[IndexLevel] = []

    # --- indices table (id="tabQuotes2") ---
    seen = set()
    for tbl in tree.css("table#tabQuotes2"):
        rows = tbl.css("tbody tr")
        for r in rows:
            a = r.css_first("td.allf a")
            if not a:
                continue
            href = _href(a) or ""
            try:
                ticker, cc = _ticker_from_href(href)
            except ValueError:
                continue
            if ticker in seen:
                continue
            cells = _row_cells_text(r)
            if len(cells) < 6:
                continue

            if cc is None:
                # Index row: cells = [name, ouv, +haut, +bas, dernier, variation]
                try:
                    level = parse_number(cells[4])
                    change_pct = parse_number(cells[5])
                except ValueError:
                    continue
                securities.append(
                    Security(
                        ticker=ticker,
                        name=a.text(strip=True),
                        kind="index",
                        source_url=f"{BASE}{href}",
                    )
                )
                index_levels.append(
                    IndexLevel(
                        ticker=ticker,
                        session_date=date.today(),
                        level=level,
                        change_pct=change_pct,
                        source=SOURCE_NAME,
                    )
                )
                seen.add(ticker)

    # --- equities table ---
    tbl = tree.css_first("table#tblShare")
    if tbl is not None:
        for r in tbl.css("tbody tr"):
            a = r.css_first("td.allf a")
            if not a:
                continue
            href = _href(a) or ""
            try:
                ticker, cc = _ticker_from_href(href)
            except ValueError:
                continue
            if ticker in seen or cc is None:
                continue
            cells = _row_cells_text(r)
            if len(cells) < 8:
                continue
            try:
                open_ = parse_number(cells[1])
                high = parse_number(cells[2])
                low = parse_number(cells[3])
                volume = int(parse_number(cells[4]))
                turnover = parse_number(cells[5])
                last = parse_number(cells[6])
                change_pct = parse_number(cells[7])
            except ValueError:
                continue
            securities.append(
                Security(
                    ticker=ticker,
                    name=a.text(strip=True),
                    kind="equity",
                    country=cc.upper() if cc else None,
                    source_url=f"{BASE}{href}",
                )
            )
            quotes.append(
                Quote(
                    ticker=ticker,
                    source=SOURCE_NAME,
                    last=last,
                    open=open_,
                    high=high,
                    low=low,
                    volume=volume,
                    turnover=turnover,
                    change_pct=change_pct,
                )
            )
            seen.add(ticker)

    return securities, quotes, index_levels


_COTATION_FIELDS = {
    "Volume (titres)": "volume",
    "Volume (XOF )": "turnover",
    "Volume (XOF)": "turnover",
    "Ouverture": "open",
    "Plus haut": "high",
    "Plus bas": "low",
    "Clôture veille": "prev_close",
}


def parse_cotation(html: str, ticker: str) -> Quote:
    """Parse a per-ticker cotation page. Ticker is passed in because the
    page's canonical form isn't always in a stable spot; we already know
    which URL we requested."""
    tree = HTMLParser(html)

    last: float | None = None
    change_pct: float | None = None
    price_block = tree.css_first("div.cot1u")
    if price_block is not None:
        # Equity form: "32 500 XOF +1,88%"; index form: "507,13 " (no XOF).
        # Get the full decoded text, subtract the change span's text, drop
        # "XOF", parse what's left as a number.
        raw = price_block.text(separator=" ").replace("XOF", "")
        span = price_block.css_first("span")
        if span is not None:
            change_text = span.text(strip=True)
            raw = raw.replace(change_text, "")
            with contextlib.suppress(ValueError):
                change_pct = parse_number(change_text)
        with contextlib.suppress(ValueError):
            last = parse_number(raw)

    fields: dict[str, float] = {}
    for tbl in tree.css("table.tbl100_4"):
        for tr in tbl.css("tr"):
            tds = tr.css("td")
            if len(tds) != 2:
                continue
            label = tds[0].text(strip=True)
            key = _COTATION_FIELDS.get(label)
            if key is None:
                continue
            try:
                fields[key] = parse_number(tds[1].text(strip=True))
            except ValueError:
                continue

    volume = int(fields["volume"]) if "volume" in fields else None
    return Quote(
        ticker=ticker,
        source=SOURCE_NAME,
        last=last,
        change_pct=change_pct,
        open=fields.get("open"),
        high=fields.get("high"),
        low=fields.get("low"),
        prev_close=fields.get("prev_close"),
        volume=volume,
        turnover=fields.get("turnover"),
    )


def parse_cotation_meta(html: str) -> dict[str, str | None]:
    """Extract ISIN + country code + display name from a cotation page.
    Returns keys: name, isin, country (ISO2 upper), ticker (uppercase)."""
    tree = HTMLParser(html)
    out: dict[str, str | None] = {"name": None, "isin": None, "country": None, "ticker": None}
    h1 = tree.css_first("div.quotebarE h1")
    if h1 is not None:
        out["name"] = h1.text(strip=True)
    inner = tree.css_first("div.innerUpu")
    if inner is not None:
        text = inner.text(strip=True)
        m = re.search(r"([A-Z]{2}\d{10})\s*-\s*([A-Z0-9-]+)", text)
        if m:
            out["isin"] = m.group(1)
            out["ticker"] = m.group(2)
        flag = inner.css_first("img")
        if flag is not None:
            src = flag.attributes.get("src") or ""
            fm = _FLAG_RE.search(src)
            if fm:
                out["country"] = fm.group(1).upper()
    return out


def parse_historique(html: str, ticker: str) -> list[DailyBar]:
    """Parse `#tblhistos` -> DailyBar list (newest first).

    Table columns: Date (DD/MM/YYYY), Clôture, Plus bas, Plus haut,
    Ouverture, Volume Titres, Volume FCFA, Variation %.
    """
    tree = HTMLParser(html)
    tbl = tree.css_first("table#tblhistos")
    if tbl is None:
        return []
    bars: list[DailyBar] = []
    for r in tbl.css("tbody tr"):
        cells = _row_cells_text(r)
        if len(cells) < 7:
            continue
        try:
            session = datetime.strptime(cells[0], "%d/%m/%Y").date()
            close = parse_number(cells[1])
            low = parse_number(cells[2])
            high = parse_number(cells[3])
            open_ = parse_number(cells[4])
            volume = int(parse_number(cells[5]))
            turnover = parse_number(cells[6])
        except ValueError:
            continue
        bars.append(
            DailyBar(
                ticker=ticker,
                session_date=session,
                close=close,
                open=open_,
                high=high,
                low=low,
                volume=volume,
                turnover=turnover,
                source=SOURCE_NAME,
            )
        )
    return bars


# ---------- company profile (societe) + sector peers (secteur) ----------


_SOCIETE_LABELS = {
    "La société": "description",
    "Téléphone": "phone",
    "Fax": "fax",
    "Adresse": "address",
    "Dirigeants": "leadership",
    "Nombre de titres": "shares_outstanding",
    "Flottant": "float_pct",
    "Valorisation de la société": "market_cap_mxof",
}


def _parse_shareholders(raw: str) -> list[tuple[str, float]]:
    """Parse the pipe-delimited shareholder blob.

    Format: `NAME*PCT;NAME*PCT;...`, e.g.
    "FRANCE TELECOM*42,3;ETAT DU SENEGAL*27,7". Percentages use comma decimal.
    """
    out: list[tuple[str, float]] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "*" not in chunk:
            continue
        name, _, pct = chunk.rpartition("*")
        try:
            out.append((name.strip(), parse_number(pct)))
        except ValueError:
            continue
    return out


def parse_societe(html: str) -> dict:
    """Extract fields from `/marches/societe/<T>.<cc>`.

    Returns a plain dict so the service layer can decide which fields are
    surfaced. Missing fields are simply absent from the dict.
    """
    tree = HTMLParser(html)
    out: dict = {}

    # Sector name from the first h1 (breadcrumb-style).
    h1 = tree.css_first("div.quotebarE h1")
    if h1 is not None:
        out["title"] = h1.text(strip=True)

    # ISIN + ticker from innerUpu.
    inner = tree.css_first("div.innerUpu")
    if inner is not None:
        text = inner.text(strip=True)
        m = re.search(r"([A-Z]{2}\d{10})\s*-\s*([A-Z0-9-]+)", text)
        if m:
            out["isin"] = m.group(1)
            out["ticker"] = m.group(2)

    # Label/value paragraphs in the left column.
    col = tree.css_first("div.soc_col1")
    if col is not None:
        for p in col.css("p"):
            b = p.css_first("b")
            if b is None:
                continue
            label = b.text(strip=True).rstrip(":").strip()
            key = _SOCIETE_LABELS.get(label)
            if key is None:
                continue
            # Value is the paragraph text minus the label text.
            full = p.text(separator=" ").strip()
            # Strip the "Label :" prefix (colon may or may not be present).
            value = re.sub(r"^\s*" + re.escape(label) + r"\s*:\s*", "", full).strip()
            if key == "leadership":
                # Split on the <br> separators the source uses.
                value = re.sub(r"\s+", " ", value).strip()
            out[key] = value

    # Shareholder blob (hidden span).
    span = tree.css_first("#lstActionnaires")
    if span is not None:
        out["shareholders"] = _parse_shareholders(span.text())

    return out


def parse_secteur(html: str) -> dict:
    """Parse `/marches/secteur/<T>.<cc>`.

    Returns `{"sector": <name>, "peers": [{"ticker","country","name","last",
    "change_day_pct","change_ytd_pct","volume"}, ...]}`.
    """
    tree = HTMLParser(html)
    out: dict = {"sector": None, "peers": []}

    h1 = tree.css_first("h1")
    if h1 is not None:
        text = h1.text(strip=True)
        m = re.search(r"secteur\s+(.+?)\s*$", text)
        if m:
            out["sector"] = m.group(1)

    tbl = tree.css_first("table#tabQuotes")
    if tbl is None:
        return out

    for r in tbl.css("tbody tr"):
        a = r.css_first("td.allf a")
        if not a:
            continue
        try:
            ticker, cc = _ticker_from_href(a.attributes.get("href") or "")
        except ValueError:
            continue
        cells = _row_cells_text(r)
        if len(cells) < 8:
            continue
        try:
            volume = int(parse_number(cells[4]))
            last = parse_number(cells[5])
            chg_day = parse_number(cells[6])
            chg_ytd = parse_number(cells[7])
        except ValueError:
            continue
        out["peers"].append(
            {
                "ticker": ticker,
                "country": cc.upper() if cc else None,
                "name": a.text(strip=True),
                "last": last,
                "change_day_pct": chg_day,
                "change_ytd_pct": chg_ytd,
                "volume": volume,
            }
        )
    return out


# ---------- fetchers (network I/O) ----------


def fetch_aaz(client: httpx.Client | None = None) -> tuple[list[Security], list[Quote], list[IndexLevel]]:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}/marches/aaz")
        r.raise_for_status()
        return parse_aaz(r.text)
    finally:
        if close:
            client.close()


def _cotation_path(ticker: str, country: str | None) -> str:
    if country:
        return f"/marches/cotation_{ticker}.{country.lower()}"
    return f"/marches/cotation_{ticker}"


def fetch_cotation(
    ticker: str, country: str | None, client: httpx.Client | None = None
) -> Quote:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(BASE + _cotation_path(ticker, country))
        r.raise_for_status()
        return parse_cotation(r.text, ticker)
    finally:
        if close:
            client.close()


def fetch_historique(
    ticker: str, country: str | None, client: httpx.Client | None = None
) -> list[DailyBar]:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}/marches/historiques/{ticker}.{(country or '').lower()}")
        r.raise_for_status()
        return parse_historique(r.text, ticker)
    finally:
        if close:
            client.close()


def fetch_societe(
    ticker: str, country: str | None, client: httpx.Client | None = None
) -> dict:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}/marches/societe/{ticker}.{(country or '').lower()}")
        r.raise_for_status()
        return parse_societe(r.text)
    finally:
        if close:
            client.close()


def fetch_secteur(
    ticker: str, country: str | None, client: httpx.Client | None = None
) -> dict:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}/marches/secteur/{ticker}.{(country or '').lower()}")
        r.raise_for_status()
        return parse_secteur(r.text)
    finally:
        if close:
            client.close()


def canonical_url(ticker: str, country: str | None) -> str:
    """Fully-qualified sikafinance URL for a security. Used as source_url."""
    return f"{BASE}{_cotation_path(ticker, country)}"


def is_cotation_url(url: str) -> bool:
    p = urlparse(url)
    return bool(_COTATION_RE.match(p.path))
