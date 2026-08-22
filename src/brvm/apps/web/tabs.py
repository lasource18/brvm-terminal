"""Registry of the /s/{ticker}/{tab} tabs.

Kept in one place so the tab bar + router + placeholders all agree on
the canonical set and each tab's Phase-of-arrival.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TabSpec:
    key: str
    label: str
    template: str                       # renders inside {% block tab_content %}
    phase: str = ""                     # when the real content lands ("" == already live)
    hidden_for_kinds: tuple[str, ...] = ()  # security kinds where this tab is not shown


_EQUITY_ONLY = ("index",)

TABS: tuple[TabSpec, ...] = (
    TabSpec("chart", "Chart", "_tab/chart.html"),
    TabSpec("description", "Description", "_tab/description.html",
            hidden_for_kinds=_EQUITY_ONLY),
    TabSpec("peers", "Peers", "_tab/peers.html",
            hidden_for_kinds=_EQUITY_ONLY),
    TabSpec("news", "News", "_tab/news.html"),
    TabSpec("corporate-actions", "Corporate actions", "_tab/corporate_actions.html",
            hidden_for_kinds=_EQUITY_ONLY),
    TabSpec("financials", "Financials", "_tab/placeholder.html",
            phase="Phase 4", hidden_for_kinds=_EQUITY_ONLY),
    TabSpec("ownership", "Ownership", "_tab/placeholder.html",
            phase="Phase 4", hidden_for_kinds=_EQUITY_ONLY),
    TabSpec("segments", "Segments", "_tab/placeholder.html",
            phase="Phase 4", hidden_for_kinds=_EQUITY_ONLY),
)

_BY_KEY = {t.key: t for t in TABS}


def get(key: str) -> TabSpec | None:
    return _BY_KEY.get(key)


def visible_for(kind: str | None) -> tuple[TabSpec, ...]:
    """Return the ordered tabs that should be shown for `kind`."""
    return tuple(t for t in TABS if kind not in t.hidden_for_kinds)
