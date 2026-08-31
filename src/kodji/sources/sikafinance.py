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

from kodji.models import (
    CorporateAction,
    DailyBar,
    IndexLevel,
    NewsItem,
    Quote,
    Security,
)
from kodji.sources._dedupe import news_hash
from kodji.sources._http import make_client
from kodji.sources._num import parse_number

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


def parse_aaz(
    html: str, *, session_date: date | None = None
) -> tuple[list[Security], list[Quote], list[IndexLevel]]:
    """Parse the A-to-Z listing page.

    Returns:
      securities: canonical equities + indices (name, kind, country, source_url)
      quotes: latest-cotation snapshot for every equity (with volume/turnover)
      index_levels: latest-cotation level for every index / composite gauge

    F-11: `session_date` defaults to `clock.last_completed_session_date()`
    — the Abidjan calendar date of the most recent trading day that has
    actually happened. Weekend and pre-open polls used to stamp
    `date.today()` (server-local) which both mis-attributed the level
    to Sat/Sun rows and, on a Montreal server before the Abidjan
    midnight roll, stamped a date one behind the true Abidjan day.
    """
    from kodji.clock import last_completed_session_date
    if session_date is None:
        session_date = last_completed_session_date()
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
                        session_date=session_date,
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


# ---------- news feed / communiqués / dividends calendar ----------


def _absolute(href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return f"{BASE}{href}"


def _parse_dt_abidjan(iso_local: str) -> str | None:
    """Sikafinance datetime attrs are Africa/Abidjan wall clock (UTC+0, no DST).

    Return an ISO-8601 UTC string, or None if unparseable.
    """
    try:
        dt = datetime.fromisoformat(iso_local)
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ddmmyyyy(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def parse_news_feed(html: str) -> list[NewsItem]:
    """Parse `/marches/actualites_bourse_brvm`.

    Structure: `ul.news-feed > li.news-item` with `a.news-title`,
    `div.news-chapeau`, `time.news-date[datetime="ISO"]`.
    """
    tree = HTMLParser(html)
    items: list[NewsItem] = []
    seen: set[str] = set()

    feed = tree.css_first("ul.news-feed")
    if feed is None:
        return items

    for li in feed.css("li.news-item"):
        a = li.css_first("a.news-title")
        if a is None:
            continue
        href = _href(a) or ""
        title = a.text(strip=True)
        if not href or not title:
            continue
        url = _absolute(href)
        h = news_hash(url, title)
        if h in seen:
            continue
        seen.add(h)

        chapeau_el = li.css_first("div.news-chapeau")
        chapeau = chapeau_el.text(strip=True) if chapeau_el is not None else None

        published_at: str | None = None
        t = li.css_first("time.news-date")
        if t is not None:
            iso = t.attributes.get("datetime")
            if iso:
                published_at = _parse_dt_abidjan(iso)

        items.append(
            NewsItem(
                source=SOURCE_NAME,
                kind="news",
                url=url,
                url_hash=h,
                title=title,
                chapeau=chapeau or None,
                published_at=published_at,
            )
        )
    return items


_COMMUNIQUE_ISSUER_RE = re.compile(r"^\s*(?P<issuer>[^:]+?)\s*:\s*(?P<title>.+?)\s*$")


def parse_communiques(html: str) -> list[NewsItem]:
    """Parse `/marches/communiques_brvm`.

    Structure: `table.tbl100_6.tablesorter > tbody > tr` with two cells:
    date (DD/MM/YYYY) and an anchor to a /docs/*.pdf whose link text is
    `COMPANY NAME : TITLE`.
    """
    tree = HTMLParser(html)
    items: list[NewsItem] = []
    seen: set[str] = set()

    tbl = tree.css_first("table.tbl100_6")
    if tbl is None:
        return items

    for tr in tbl.css("tbody tr"):
        tds = tr.css("td")
        if len(tds) < 2:
            continue
        d = _parse_ddmmyyyy(tds[0].text(strip=True))
        a = tds[1].css_first("a")
        if a is None:
            continue
        href = _href(a) or ""
        raw_title = a.text(strip=True)
        if not href or not raw_title:
            continue

        m = _COMMUNIQUE_ISSUER_RE.match(raw_title)
        issuer = m.group("issuer").strip() if m else None
        title = m.group("title").strip() if m else raw_title

        url = _absolute(href)
        h = news_hash(url, raw_title)
        if h in seen:
            continue
        seen.add(h)

        published_at = f"{d.isoformat()}T00:00:00Z" if d else None
        items.append(
            NewsItem(
                source=SOURCE_NAME,
                kind="communique",
                url=url,
                url_hash=h,
                title=title,
                issuer_name=issuer,
                published_at=published_at,
            )
        )
    return items


def parse_dividendes(html: str) -> list[CorporateAction]:
    """Parse `/marches/dividendes` upcoming table (id=`tbdDiv`).

    Columns: Date détachement (DD/MM/YYYY or "A préciser"), Nom (anchor to
    /marches/cotation_TICKER.cc), Montant, Rendement (with %).
    """
    tree = HTMLParser(html)
    out: list[CorporateAction] = []

    tbl = tree.css_first("table#tbdDiv")
    if tbl is None:
        return out

    for tr in tbl.css("tbody tr"):
        tds = tr.css("td")
        if len(tds) < 4:
            continue
        raw_date = tds[0].text(strip=True)
        ex_date = _parse_ddmmyyyy(raw_date)  # None for "A préciser"

        a = tds[1].css_first("a")
        if a is None:
            continue
        href = _href(a) or ""
        try:
            ticker, _cc = _ticker_from_href(href)
        except ValueError:
            continue

        try:
            amount = parse_number(tds[2].text(strip=True))
        except ValueError:
            amount = None
        try:
            yield_pct = parse_number(tds[3].text(strip=True))
        except ValueError:
            yield_pct = None

        note = None if ex_date else raw_date or None
        out.append(
            CorporateAction(
                ticker=ticker,
                kind="dividend",
                ex_date=ex_date,
                amount=amount,
                currency="XOF",
                yield_pct=yield_pct,
                note=note,
                source=SOURCE_NAME,
                source_url=f"{BASE}/marches/dividendes",
            )
        )
    return out


def fetch_news_feed(client: httpx.Client | None = None) -> list[NewsItem]:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}/marches/actualites_bourse_brvm")
        r.raise_for_status()
        return parse_news_feed(r.text)
    finally:
        if close:
            client.close()


def fetch_communiques(client: httpx.Client | None = None) -> list[NewsItem]:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}/marches/communiques_brvm")
        r.raise_for_status()
        return parse_communiques(r.text)
    finally:
        if close:
            client.close()


def fetch_dividendes(client: httpx.Client | None = None) -> list[CorporateAction]:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}/marches/dividendes")
        r.raise_for_status()
        return parse_dividendes(r.text)
    finally:
        if close:
            client.close()


# ---------- palmarès (gainers / losers / most active) --------------------

# Sikafinance query-param values for the "Variation" dropdown on
# `/marches/palmares`. Only one variation renders per fetch; the caller
# chains three fetches to fill the full gainers/losers/most-active
# trio into a `Palmares` view.
PALMARES_VARIATIONS = {
    "gainers": "h",       # Hausses
    "losers": "b",        # Baisses
    "most_active": "c",   # Capitaux échangés (turnover-ranked)
    "top_volume": "v",    # Volumes en séance
}


def parse_palmares(html: str) -> list[Quote]:
    """Parse one palmarès table into `Quote` rows.

    The page shows a single ranking per fetch (Hausses / Baisses /
    Volumes / Capitaux), always in the same `#tabQuotes` table with the
    columns: Nom · Haut · Bas · Dernier · Volume · Variation jour ·
    Variation (hidden). Turnover is not published on this view, so
    `Quote.turnover` stays None; volume + last + change_pct are the
    fields the caller can rank on.
    """
    tree = HTMLParser(html)
    tbl = tree.css_first("table#tabQuotes")
    if tbl is None:
        return []
    out: list[Quote] = []
    for tr in tbl.css("tbody tr"):
        a = tr.css_first("td.allf a")
        if a is None:
            continue
        href = _href(a) or ""
        # Palmarès hrefs are relative (`cotation_XXX.cc`) without the
        # `/marches/` prefix — normalise before feeding the shared
        # `_ticker_from_href` regex.
        if href and not href.startswith("/"):
            href = "/marches/" + href
        try:
            ticker, _cc = _ticker_from_href(href)
        except ValueError:
            continue
        cells = _row_cells_text(tr)
        if len(cells) < 6:
            continue
        try:
            high = parse_number(cells[1])
            low = parse_number(cells[2])
            last = parse_number(cells[3])
            volume = int(parse_number(cells[4]))
            change_pct = parse_number(cells[5])
        except ValueError:
            continue
        out.append(
            Quote(
                ticker=ticker,
                source=SOURCE_NAME,
                last=last,
                high=high,
                low=low,
                volume=volume,
                change_pct=change_pct,
            )
        )
    return out


def fetch_palmares(
    variation: str = "gainers",
    client: httpx.Client | None = None,
) -> list[Quote]:
    """`variation` is one of the `PALMARES_VARIATIONS` keys. Unknown
    values fall back to gainers rather than raising so a stale caller
    still gets a usable list."""
    q = PALMARES_VARIATIONS.get(variation, "h")
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}/marches/palmares?dlVariation={q}")
        r.raise_for_status()
        return parse_palmares(r.text)
    finally:
        if close:
            client.close()
