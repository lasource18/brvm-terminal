"""brvm.org — bond listings (Phase 8 primary source).

Sikafinance does not publish a consolidated bond table (as of 2026-08),
so bonds come from the official exchange instead. Three category pages
share the same table shape:

* `/fr/cours-obligations/20` — Obligations d'Etat
* `/fr/cours-obligations/21` — Obligations régionales
* `/fr/cours-obligations/55` — Obligations privées

Each row: `Code obligation | Nom | Date émission | Date maturité |
Cours du jour en valeur | Coupon Couru | Dernier paiement`. Date
maturité is empty in every current row — the maturity year is embedded
in `Nom` as `YYYY-YYYY` and Phase 8b lifts it out along with the coupon
rate and the issuer name. Price lands in `daily_bars.close`; accrued
coupon + last-payment fields land in `bond_snapshots` (see Phase 8b).

Parsers are pure (HTML in → models out) for fixture testability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx
from selectolax.parser import HTMLParser

from brvm.models import BondSnapshot, DailyBar, Security
from brvm.sources._http import make_client
from brvm.sources._num import parse_number


def _parse_ddmmyyyy(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        return None

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


# Nom cell shapes on the category pages, all trailing `COUPON% YYYY-YYYY`:
#
#   BIDC-EBID 6,10% 2017-2027
#   ETAT DU MALI 6,20% 2022-2029
#   ETAT DU MALI 3,00 % 2024-2031         (space before %)
#   BHB 6.25% 2012-2017                   (period-decimal coupon)
#   SOCIAL BOND CRRH-UEMOA 6,00% 2025-2040
#   GENDER BOND ECOBANK CI 6,50% 2024-2029
#   DIASPORA BONDS BHS 6,25% 2019-2024
#   KEUR SAMBA NSIA BQE CI 7% 2025-2030
#   TPCI 5,95% 2017-2024 - A              (trailing tranche suffix, F-30)
#   TPBF 6.50% 2011 - 2016                (spaced year range, F-30)
#
# We capture the prefix, coupon rate, issue year, and maturity year in one
# regex. U+2013 (en-dash) is accepted alongside U+002D (hyphen-minus)
# because brvm.org has used both on freshly-loaded rows. F-30: optional
# whitespace around the year-range dash and an optional trailing
# `- <tranche>` suffix (single letter or one-digit series id) let the
# TPCI/TPBF fixture rows enrich rather than land as NULL coupon/maturity.
_NOM_RE = re.compile(
    r"""^
        (?P<issuer>.+?)\s+
        (?P<coupon>\d{1,3}(?:[.,]\d{1,3})?)\s*%\s+
        (?P<iyear>\d{4})\s*[-\u2013]\s*(?P<myear>\d{4})
        (?:\s*[-\u2013]\s*(?P<tranche>[A-Z0-9]{1,3}))?
        \s*$
    """,
    re.VERBOSE,
)

# Bond-type labels the exchange staples in front of the actual issuer name.
# Stripping them lets `list_by_issuer` group `SOCIAL BOND CRRH-UEMOA 6,00%
# 2025-2040` with plain `CRRH-UEMOA 6.10% 2012-2022`, which is what a user
# scanning "all CRRH-UEMOA bonds" would expect.
_ISSUER_PREFIX_STRIPS: tuple[str, ...] = (
    "SOCIAL BOND ",
    "GENDER BOND ",
    "DIASPORA BONDS ",
    "KEUR SAMBA ",
    "GSS BAOBAB ",
    "GSS ",
)


def _strip_issuer_prefix(raw: str) -> str:
    up = raw.upper()
    for pfx in _ISSUER_PREFIX_STRIPS:
        if up.startswith(pfx):
            return raw[len(pfx):].strip()
    return raw


@dataclass(frozen=True)
class ParsedNom:
    issuer_name: str
    coupon_rate: float
    issue_year: int
    maturity_year: int


def parse_nom(name: str) -> ParsedNom | None:
    """Extract structured fields from a bond's `Nom` cell.

    Returns None when the name doesn't match the trailing `COUPON% YYYY-
    YYYY` shape — a graceful signal to the caller rather than an
    exception because brvm.org occasionally publishes rows with
    non-conforming names (e.g. "à préciser" placeholders on freshly-
    admitted bonds). We keep the ingest running and leave enrichment
    NULL on those rows.
    """
    m = _NOM_RE.match(name.strip())
    if m is None:
        return None
    try:
        coupon = float(m.group("coupon").replace(",", "."))
    except ValueError:
        return None
    return ParsedNom(
        issuer_name=_strip_issuer_prefix(m.group("issuer").strip()),
        coupon_rate=coupon,
        issue_year=int(m.group("iyear")),
        maturity_year=int(m.group("myear")),
    )


# The last-payment cell shape is `DD/MM/YYYY / N,NN`, i.e. date + amount
# separated by a spaced slash. The date itself contains slashes without
# surrounding whitespace, so the split pattern requires at least one
# whitespace char on either side — otherwise "16/06/2026" would split.
_LAST_PAYMENT_SPLIT_RE = re.compile(r"\s+/\s+")


def parse_last_payment(cell: str) -> tuple[date | None, float | None]:
    """Parse "DD/MM/YYYY / N,NN" → (date, amount). Missing / malformed
    halves come back as None each so the caller can still surface one
    when the other is unreadable (which happens when a new bond hasn't
    paid its first coupon yet — the cell is just "-")."""
    if not cell or cell.strip() in {"-", "—", ""}:
        return None, None
    parts = _LAST_PAYMENT_SPLIT_RE.split(cell.strip(), maxsplit=1)
    date_part = parts[0] if parts else ""
    amount_part = parts[1] if len(parts) > 1 else ""
    d = _parse_ddmmyyyy(date_part)
    a: float | None
    try:
        a = parse_number(amount_part)
    except ValueError:
        a = None
    return d, a


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
) -> tuple[list[Security], list[DailyBar], list[BondSnapshot]]:
    """Parse one bond-category page.

    Returns:
      securities: one `Security(kind="bond")` per row, with the four
        bond-only reference fields populated when `Nom` matches the
        canonical `COUPON% YYYY-YYYY` shape (else left NULL and a
        follow-up run can fill them in without wiping other data).
      bars: one `DailyBar` per row (session_date = today, close = price).
      snapshots: one `BondSnapshot` per row (accrued coupon +
        last-payment date/amount). Same session_date as the bar.

    Rows whose price cell doesn't parse are dropped (the parser stays
    strict so a source change surfaces as a failing test rather than a
    silent NULL). Duplicate tickers within one page are ignored.
    """
    tree = HTMLParser(html)
    securities: list[Security] = []
    bars: list[DailyBar] = []
    snapshots: list[BondSnapshot] = []
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

            issue_date = _parse_ddmmyyyy(cells[2]) if len(cells) > 2 else None
            parsed = parse_nom(name)

            try:
                accrued = parse_number(cells[5]) if len(cells) > 5 else None
            except ValueError:
                accrued = None
            last_date, last_amount = (
                parse_last_payment(cells[6]) if len(cells) > 6 else (None, None)
            )

            securities.append(
                Security(
                    ticker=ticker,
                    name=name,
                    kind="bond",
                    country=country,
                    sector=category.sector,
                    currency="XOF",
                    source_url=category.url,
                    coupon_rate=parsed.coupon_rate if parsed else None,
                    maturity_year=parsed.maturity_year if parsed else None,
                    issue_date=issue_date,
                    issuer_name=parsed.issuer_name if parsed else None,
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
            snapshots.append(
                BondSnapshot(
                    ticker=ticker,
                    session_date=session,
                    accrued_coupon=accrued,
                    last_coupon_date=last_date,
                    last_coupon_amount=last_amount,
                    source=SOURCE_NAME,
                )
            )
            seen.add(ticker)

        break  # only the first bonds table matters

    return securities, bars, snapshots


def fetch_bonds(
    category: BondCategory,
    client: httpx.Client | None = None,
    today: date | None = None,
) -> tuple[list[Security], list[DailyBar], list[BondSnapshot]]:
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
) -> tuple[list[Security], list[DailyBar], list[BondSnapshot]]:
    """Fetch all three brvm.org bond categories, concatenated. Duplicate
    tickers across categories (shouldn't happen — categories are disjoint —
    but the exchange has been sloppy before) fall through the seen-set."""
    close = client is None
    client = client or make_client()
    try:
        all_secs: list[Security] = []
        all_bars: list[DailyBar] = []
        all_snaps: list[BondSnapshot] = []
        seen: set[str] = set()
        for cat in BOND_CATEGORIES:
            secs, bars, snaps = fetch_bonds(cat, client=client, today=today)
            for s, b, sn in zip(secs, bars, snaps, strict=True):
                if s.ticker in seen:
                    continue
                seen.add(s.ticker)
                all_secs.append(s)
                all_bars.append(b)
                all_snaps.append(sn)
        return all_secs, all_bars, all_snaps
    finally:
        if close:
            client.close()
