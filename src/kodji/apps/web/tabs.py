"""Registry of the /s/{ticker}/{tab} tabs.

Kept in one place so the tab bar + router + placeholders all agree on
the canonical set and each tab's Phase-of-arrival. Every tab declares
which kinds it's hidden for; the visible-for-kind projection is what
`security.html` renders on the tabbar.

Bond tabs (`overview` / `cashflow` / `yield` / `related`) live in Phase
8c. Bonds also see `chart` and `news`; equity-only concerns (Peers,
Corporate actions, Financials, Ownership, Segments, Analyst, plain
Description) are hidden.
"""

from __future__ import annotations

from dataclasses import dataclass

from kodji.store.accounts import FREE_PLAN, PAID_PLAN


@dataclass(frozen=True)
class TabSpec:
    key: str
    label: str
    template: str                       # renders inside {% block tab_content %}
    phase: str = ""                     # when the real content lands ("" == already live)
    hidden_for_kinds: tuple[str, ...] = ()  # security kinds where this tab is not shown
    min_plan: str = FREE_PLAN           # PAID_PLAN hides the tab and blocks the route

    @property
    def is_paid(self) -> bool:
        return self.min_plan == PAID_PLAN


# Every non-equity, non-index kind. Adding a kind here doesn't
# regress the existing tabs — the existing hidden_for_kinds tuples
# still stand; this is just a convenience for the (equity, index)
# case that most bond tabs mirror.
_EQUITY_ONLY_KINDS = ("index", "bond")
_BOND_ONLY_HIDDEN = ("equity", "index")

TABS: tuple[TabSpec, ...] = (
    # Cross-kind tabs first.
    # Chart is history + plotting — our work over the raw closes, so paid.
    TabSpec("chart", "Chart", "_tab/chart.html", min_plan=PAID_PLAN),
    # Bond-only landing.
    TabSpec("overview", "Overview", "_tab/bond_overview.html",
            hidden_for_kinds=_BOND_ONLY_HIDDEN),
    TabSpec("cashflow", "Cash flow", "_tab/bond_cashflow.html",
            hidden_for_kinds=_BOND_ONLY_HIDDEN, min_plan=PAID_PLAN),
    TabSpec("yield", "Yield & Duration", "_tab/bond_yield.html",
            hidden_for_kinds=_BOND_ONLY_HIDDEN, min_plan=PAID_PLAN),
    TabSpec("related", "Related bonds", "_tab/bond_related.html",
            hidden_for_kinds=_BOND_ONLY_HIDDEN),
    # Equity-only tabs.
    TabSpec("description", "Description", "_tab/description.html",
            hidden_for_kinds=_EQUITY_ONLY_KINDS),
    TabSpec("peers", "Peers", "_tab/peers.html",
            hidden_for_kinds=_EQUITY_ONLY_KINDS, min_plan=PAID_PLAN),
    # News is cross-kind (bonds get an issuer-name fallback in `bonds.list_issuer_news`).
    TabSpec("news", "News", "_tab/news.html"),
    TabSpec("corporate-actions", "Corporate actions", "_tab/corporate_actions.html",
            hidden_for_kinds=_EQUITY_ONLY_KINDS),
    # Ratios live on this tab, and ratios are the computed layer.
    TabSpec("financials", "Financials", "_tab/financials.html",
            hidden_for_kinds=_EQUITY_ONLY_KINDS, min_plan=PAID_PLAN),
    TabSpec("ownership", "Ownership", "_tab/ownership.html",
            hidden_for_kinds=_EQUITY_ONLY_KINDS),
    TabSpec("segments", "Segments", "_tab/segments.html",
            hidden_for_kinds=_EQUITY_ONLY_KINDS),
    TabSpec("analyst", "Analyst view", "_tab/analyst.html",
            hidden_for_kinds=_EQUITY_ONLY_KINDS, min_plan=PAID_PLAN),
)

_BY_KEY = {t.key: t for t in TABS}


def get(key: str) -> TabSpec | None:
    return _BY_KEY.get(key)


def visible_for(kind: str | None, plan: str = PAID_PLAN) -> tuple[TabSpec, ...]:
    """The ordered tabs to show for `kind` on `plan`.

    `plan` defaults to paid so every existing caller that has no notion of
    a plan — the TUI, tests written before PR-Y — keeps seeing the full
    set. The web tabbar passes the real plan.

    Hiding a tab is presentation, not access control: `/s/SNTS/yield`
    stays reachable by URL, and so do the HTMX fragments and the JSON
    API. `require_plan_for_tab` is the enforcement; this is the tabbar.
    """
    return tuple(
        t for t in TABS
        if kind not in t.hidden_for_kinds
        and (plan == PAID_PLAN or not t.is_paid)
    )


def paid_keys() -> frozenset[str]:
    """Tab keys that require a paid plan — the set the leak test walks."""
    return frozenset(t.key for t in TABS if t.is_paid)


def default_tab_for(kind: str | None, plan: str = PAID_PLAN) -> str:
    """The tab a bare `/s/{ticker}` should redirect to for this kind.

    Bonds land on `overview` (that's the DES-equivalent screen);
    everything else lands on `chart` for continuity with the existing
    equity/index flow — except on the free plan, where `chart` is paid
    and would bounce the reader straight into an upgrade wall on every
    `/s/{ticker}` click. Free lands on `news`, which is free for every
    kind.
    """
    if kind == "bond":
        return "overview"
    return "chart" if plan == PAID_PLAN else "news"
