"""brvm.org — bond listings (Phase 8 primary source).

Sikafinance does not publish a consolidated bond table (as of 2026-08),
so bonds come from the official exchange instead. Three category pages
share the same table shape:

* `/fr/cours-obligations/20` — Obligations d'Etat
* `/fr/cours-obligations/21` — Obligations régionales
* `/fr/cours-obligations/55` — Obligations privées

Each row: `Code obligation | Nom | Date émission | Date maturité |
Cours du jour en valeur | Coupon Couru | Dernier paiement`. Date
maturité is often blank (the maturity year is instead embedded in the
`Nom` as `YYYY-YYYY`, but we leave name-derived enrichment for a
follow-up). The daily price lands in `daily_bars.close` so bonds feed
the same directory / period-return SQL as equities and indices.

Parsers are pure (HTML in → models out) for fixture testability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import httpx
from selectolax.parser import HTMLParser

from brvm.models import DailyBar, Security
from brvm.sources._http import make_client
from brvm.sources._num import parse_number

SOURCE_NAME = "brvm_org"
BASE = "https://www.brvm.org"


@dataclass(frozen=True)
class BondCategory:
    """One of the three brvm.org bond category pages."""

    id: int
    sector: str          # French label used as `securities.sector`

    @property
    def path(self) -> str:
        return f"/fr/cours-obligations/{self.id}"

    @property
    def url(self) -> str:
        return f"{BASE}{self.path}"


BOND_CATEGORIES: tuple[BondCategory, ...] = (
    BondCategory(20, "Obligations d'Etat"),
    BondCategory(21, "Obligations régionales"),
    BondCategory(55, "Obligations privées"),
)


# ETAT DU / DE ... → ISO-2 country code. Regional and private issuers don't
# map to a single country, so they're left as None and a future enrichment
# pass can revisit (many private tickers embed a country hint — BHSN.O1 is
# Senegal, ECOC.O1 is CI — but the mapping is issuer-specific and better
# handled once we're wiring per-issuer metadata rather than guessing here).
_STATE_COUNTRY_MAP: dict[str, str] = {
    "MALI": "ML",
    "SENEGAL": "SN",
    "BENIN": "BJ",
    "COTE D'IVOIRE": "CI",
    "COTE D IVOIRE": "CI",
    "BURKINA": "BF",
    "BURKINA FASO": "BF",
    "NIGER": "NE",
    "TOGO": "TG",
    "GUINEE BISSAU": "GW",
    "GUINEE-BISSAU": "GW",
}

_ETAT_PREFIX_RE = re.compile(r"^\s*ETAT\s+(?:DU|DE|DE LA|DES|D')\s+(.+?)\s+\d", re.IGNORECASE)


def _country_from_name(name: str) -> str | None:
    """Return ISO-2 for `ETAT DU X ...` names; None otherwise."""
    m = _ETAT_PREFIX_RE.match(name)
    if not m:
        return None
    key = m.group(1).upper().strip()
    return _STATE_COUNTRY_MAP.get(key)


def _row_cells_text(row) -> list[str]:  # type: ignore[no-untyped-def]
    return [c.text(strip=True) for c in row.css("td")]


# The bond table is `<table class="table table-hover table-striped sticky-enabled">`
# with a `<thead>` whose first `<th>` is exactly "Code obligation". The
# category pages also carry a smaller "Activités du marché" table whose
# header shape is different — we key off the first-`<th>` text so ordering
# in the page (which brvm.org has rearranged before) doesn't matter.
_BOND_HEADER = "Code obligation"


def parse_bonds(
    html: str, category: BondCategory, today: date | None = None
) -> tuple[list[Security], list[DailyBar]]:
    """Parse one bond-category page.

    Returns:
      securities: one `Security(kind="bond")` per row
      bars: one `DailyBar` per row (session_date = today, close = price)

    Rows whose price cell doesn't parse are dropped (the parser stays
    strict so a source change surfaces as a failing test rather than a
    silent NULL). Duplicate tickers within one page are ignored.
    """
    tree = HTMLParser(html)
    securities: list[Security] = []
    bars: list[DailyBar] = []
    session = today or date.today()
    seen: set[str] = set()

    for tbl in tree.css("table"):
        thead = tbl.css_first("thead")
        if thead is None:
            continue
        first_th = thead.css_first("th")
        if first_th is None or first_th.text(strip=True) != _BOND_HEADER:
            continue

        for tr in tbl.css("tbody tr"):
            cells = _row_cells_text(tr)
            if len(cells) < 5:
                continue
            ticker = cells[0].strip()
            name = cells[1].strip()
            if not ticker or not name or ticker in seen:
                continue

            try:
                price = parse_number(cells[4])
            except ValueError:
                continue
            # A zero price means the bond hasn't traded (freshly-admitted or
            # long-matured with no cross). Persisting a 0 close would sink
            # period-return columns; skip instead.
            if price <= 0:
                continue

            country = _country_from_name(name)

            securities.append(
                Security(
                    ticker=ticker,
                    name=name,
                    kind="bond",
                    country=country,
                    sector=category.sector,
                    currency="XOF",
                    source_url=category.url,
                )
            )
            bars.append(
                DailyBar(
                    ticker=ticker,
                    session_date=session,
                    close=price,
                    source=SOURCE_NAME,
                )
            )
            seen.add(ticker)

        break  # only the first bonds table matters

    return securities, bars


def fetch_bonds(
    category: BondCategory,
    client: httpx.Client | None = None,
    today: date | None = None,
) -> tuple[list[Security], list[DailyBar]]:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(category.url)
        r.raise_for_status()
        return parse_bonds(r.text, category, today=today)
    finally:
        if close:
            client.close()


def fetch_all_bonds(
    client: httpx.Client | None = None, today: date | None = None
) -> tuple[list[Security], list[DailyBar]]:
    """Fetch all three brvm.org bond categories, concatenated. Duplicate
    tickers across categories (shouldn't happen — categories are disjoint —
    but the exchange has been sloppy before) fall through the seen-set in
    each `parse_bonds` call; a later category will overwrite a duplicate."""
    close = client is None
    client = client or make_client()
    try:
        all_secs: list[Security] = []
        all_bars: list[DailyBar] = []
        seen: set[str] = set()
        for cat in BOND_CATEGORIES:
            secs, bars = fetch_bonds(cat, client=client, today=today)
            for s, b in zip(secs, bars, strict=True):
                if s.ticker in seen:
                    continue
                seen.add(s.ticker)
                all_secs.append(s)
                all_bars.append(b)
        return all_secs, all_bars
    finally:
        if close:
            client.close()
