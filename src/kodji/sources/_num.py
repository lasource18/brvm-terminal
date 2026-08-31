"""Number parsing helpers for French / mixed sources.

BRVM sources print numbers in a few flavours we need to normalise:

* sikafinance aaz table: `2&#xA0;600` (nbsp thousands), percentages `-3.70%` (period)
* sikafinance cotation header: `+1,88%` (comma decimal), `32&#xA0;500` prices
* sikafinance historique: prices integer-nbsp, `1,88%` change
* afx.kwayisi: English — `32,500` (comma thousands), `1.88%` (period decimal)

Rule: strip all whitespace (including U+00A0) and `%`, then decide comma-vs-
period by structural cues (single comma near the end == decimal, otherwise
comma is a thousands separator in English input).
"""

from __future__ import annotations

_WHITESPACE = "".join(chr(c) for c in (0x20, 0x09, 0x0A, 0x0D, 0xA0, 0x202F))
_TRANS = str.maketrans({c: None for c in _WHITESPACE})


def _clean(s: str) -> str:
    return s.translate(_TRANS).replace("%", "").replace("+", "").strip()


def parse_fr_number(s: str) -> float:
    """Parse a French-formatted number: nbsp/space thousands, comma decimal.

    Also accepts pre-cleaned integers (no comma). Empty / '-' returns NaN via
    ValueError to the caller — we raise so bugs surface loudly.
    """
    x = _clean(s)
    if not x or x in {"-", "—"}:
        raise ValueError(f"empty number: {s!r}")
    x = x.replace(",", ".")
    return float(x)


def parse_en_number(s: str) -> float:
    """Parse an English-formatted number: comma thousands, period decimal."""
    x = _clean(s).replace(",", "")
    if not x or x in {"-", "—"}:
        raise ValueError(f"empty number: {s!r}")
    return float(x)


def parse_number(s: str) -> float:
    """Parse a sikafinance-flavoured number.

    Sikafinance uses nbsp/space thousands and mixes period-decimal
    percentages (aaz page) with comma-decimal ones (cotation, historique).
    It never uses comma-as-thousands, so treating every comma as a decimal
    point (after stripping whitespace) covers both cases safely.

    For English (comma-thousands) sources like afx.kwayisi, callers must
    use :func:`parse_en_number` explicitly instead.
    """
    x = _clean(s)
    if not x or x in {"-", "—"}:
        raise ValueError(f"empty number: {s!r}")
    return float(x.replace(",", "."))
