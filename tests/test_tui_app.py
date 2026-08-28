"""TUI smoke + interaction tests via Textual's Pilot.

The TUI is a viewer over the same services layer the web app uses; tests
seed a fresh SQLite DB, then boot the app in headless mode and drive it
via keystrokes. Assertions target the widget tree (ids, cell counts,
current tab, `ContentSwitcher.current`) rather than pixel snapshots so
they stay stable across Textual releases.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from textual.widgets import ContentSwitcher, DataTable, Static

from brvm.apps.tui.app import BRVMTerminalApp
from brvm.apps.tui.sidebar import Sidebar
from brvm.apps.tui.views.alerts import AlertsView
from brvm.apps.tui.views.directory import DirectoryView
from brvm.apps.tui.views.news import NewsView
from brvm.apps.tui.views.ticker import TickerView
from brvm.apps.tui.views.watchlists import WatchlistsView
from tests.conftest import apply_migrations, reset_module_state


def _seed(db_path: Path) -> None:
    """Minimal DB seed for TUI tests — securities, snapshots, one index."""
    from brvm.db import connect
    from brvm.models import IndexLevel, Quote, Security
    from brvm.store import quotes as quotes_repo
    from brvm.store import securities as sec_repo

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(
            conn,
            [
                Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
                Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
                Security(ticker="SPHC", name="SAPH CI", kind="equity", country="CI"),
                Security(ticker="BOAC", name="BANK OF AFRICA CI", kind="equity", country="CI"),
                Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
            ],
        )
        quotes_repo.insert_snapshots(
            conn,
            [
                Quote(ticker="SNTS", source="sikafinance", last=32500, change_pct=1.88,
                      volume=3006, turnover=97_695_000),
                Quote(ticker="ORAC", source="sikafinance", last=19000, change_pct=-0.5,
                      volume=1000, turnover=19_000_000),
                Quote(ticker="SPHC", source="sikafinance", last=8990, change_pct=7.02,
                      volume=52971, turnover=476_209_290),
                Quote(ticker="BOAC", source="sikafinance", last=6500, change_pct=-1.23,
                      volume=800, turnover=5_200_000),
            ],
        )
        quotes_repo.upsert_index_levels(
            conn,
            [
                IndexLevel(ticker="BRVMC", session_date=date(2026, 8, 25),
                           level=510.42, change_pct=0.83, source="sikafinance"),
            ],
        )


@pytest.fixture
def tui_db(tmp_path, monkeypatch):
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    reset_module_state()
    _seed(db_path)
    yield db_path
    reset_module_state()


async def test_app_boots_on_home(tui_db):
    """Fresh boot lands on the Home view with the ContentSwitcher pointing at it."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        cs = app.query_one("#right-pane", ContentSwitcher)
        assert cs.current == "home"


async def test_indices_render_on_home(tui_db):
    """The home indices strip picks up BRVMC from the seeded `index_levels`."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        strip = app.query_one("#home-indices", Static)
        assert "BRVMC" in _plain(strip.render())


async def test_sidebar_populates_turnover_leaders_by_default(tui_db):
    """No user watchlists ⇒ sidebar shows the virtual `Turnover leaders`
    with SPHC at the top (highest turnover in the seed)."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sb = app.query_one(Sidebar)
        table = sb.query_one("#sidebar-table", DataTable)
        assert table.row_count >= 4
        # First row is the top turnover leader.
        first_key = table.coordinate_to_cell_key((0, 0)).row_key.value
        assert first_key == "SPHC"


async def test_switch_to_directory_view(tui_db):
    """Pressing `d` swaps the ContentSwitcher to the directory view and it fills."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        cs = app.query_one("#right-pane", ContentSwitcher)
        assert cs.current == "directory"
        dv = app.query_one(DirectoryView)
        table = dv.query_one("#directory-table", DataTable)
        # 4 equities + 1 index seeded.
        assert table.row_count == 5


async def test_alerts_events_cursor_survives_refresh(tui_db, monkeypatch):
    """F-35: the cursor used to snap to row 0 on every 30-second
    refresh because the clamp ran against a stray `limit=1` query
    (`min(cursor, 0)` is always 0). Position the cursor on row 3, hit
    the refresh path, and confirm the cursor stayed put."""
    from brvm.services import alerts as alerts_svc

    class _E:
        def __init__(self, eid: int) -> None:
            self.id = eid
            self.fired_utc = f"2026-08-28T15:1{eid}:00"
            self.kind = "price_move"
            self.ticker = "SNTS"
            self.delivery_status = "sent"
            self.subject = f"synthetic event {eid}"

    events = [_E(eid) for eid in range(5)]
    monkeypatch.setattr(
        alerts_svc, "list_recent_events", lambda *, limit=25: events[:limit]
    )

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        av = app.query_one(AlertsView)
        et = av.query_one("#alerts-events", DataTable)
        assert et.row_count == 5
        et.move_cursor(row=3, animate=False)
        assert et.cursor_row == 3

        av.refresh_data()
        await pilot.pause()
        # Old code always collapsed to 0 because the stray `limit=1`
        # query returned one row, so `min(3, 0) == 0`.
        assert et.cursor_row == 3


async def test_switch_to_alerts_view(tui_db):
    """Pressing `a` swaps to the alerts view; empty seed => 0 events, 0 rules."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        cs = app.query_one("#right-pane", ContentSwitcher)
        assert cs.current == "alerts"
        av = app.query_one(AlertsView)
        events = av.query_one("#alerts-events", DataTable)
        rules = av.query_one("#alerts-rules", DataTable)
        assert events.row_count == 0
        assert rules.row_count == 0


async def test_switch_to_watchlists_view(tui_db):
    """Pressing `w` swaps to the watchlists view; a fresh DB has one
    seeded 'Default' watchlist (see migration 0002)."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        cs = app.query_one("#right-pane", ContentSwitcher)
        assert cs.current == "watchlists"
        wv = app.query_one(WatchlistsView)
        wl = wv.query_one("#wl-list", DataTable)
        assert wl.row_count == 1
        assert wl.coordinate_to_cell_key((0, 0)).row_key.value == "default"


async def test_switch_to_news_view(tui_db):
    """Pressing `F5` opens the news view with 0 rows (empty seed)."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f5")
        await pilot.pause()
        cs = app.query_one("#right-pane", ContentSwitcher)
        assert cs.current == "news"
        nv = app.query_one(NewsView)
        nt = nv.query_one("#news-table", DataTable)
        assert nt.row_count == 0


async def test_open_ticker_from_sidebar(tui_db):
    """Selecting a sidebar row switches to the Ticker view and fills the header."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sb = app.query_one(Sidebar)
        table = sb.query_one("#sidebar-table", DataTable)
        # Cursor is at (0, 0) — SPHC in this seed. Enter selects the row.
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        cs = app.query_one("#right-pane", ContentSwitcher)
        assert cs.current == "ticker"
        tv = app.query_one(TickerView)
        header = tv.query_one("#quote-header", Static)
        rendered = _plain(header.render())
        assert "SPHC" in rendered
        assert "SAPH" in rendered  # company name


async def test_refresh_preserves_sidebar_cursor(tui_db):
    """Move the cursor down, force a refresh — cursor should stay put."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sb = app.query_one(Sidebar)
        table = sb.query_one("#sidebar-table", DataTable)
        table.focus()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()
        assert table.cursor_row == 2
        # `r` triggers a refresh cycle; cursor should survive.
        app.action_refresh_now()
        await pilot.pause()
        assert table.cursor_row == 2


async def test_directory_sort_cycle(tui_db):
    """Pressing `s` cycles the sort column and repaints the table."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        dv = app.query_one(DirectoryView)
        table = dv.query_one("#directory-table", DataTable)
        initial_first = table.coordinate_to_cell_key((0, 0)).row_key.value
        # Ensure focus is on the table (not a stray input) before pressing `s`.
        table.focus()
        await pilot.press("s")
        await pilot.pause()
        assert dv._sort_idx == 0
        # The row set is stable (5 rows); we're only asserting that
        # sorting activated and the table still renders.
        assert table.row_count == 5
        # Cycling until wrap-around comes back to the default (-1).
        for _ in range(len(_directory_cols())):
            await pilot.press("s")
            await pilot.pause()
        assert dv._sort_idx == -1
        final_first = table.coordinate_to_cell_key((0, 0)).row_key.value
        assert final_first == initial_first


async def test_watchlist_create_and_add_ticker(tui_db):
    """Create a watchlist from the watchlists view, add SNTS to it,
    verify it appears in the sidebar's watchlist rotation."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        wv = app.query_one(WatchlistsView)
        name_input = wv.query_one("#wl-new-name")
        name_input.focus()
        # Type + submit.
        await pilot.press(*list("Core"))
        await pilot.press("enter")
        await pilot.pause()
        wl = wv.query_one("#wl-list", DataTable)
        # Default (seeded) + Core (just created) = 2 lists.
        assert wl.row_count == 2

        add_input = wv.query_one("#wl-add-ticker")
        add_input.focus()
        await pilot.press(*list("SNTS"))
        await pilot.press("enter")
        await pilot.pause()
        items = wv.query_one("#wl-items", DataTable)
        assert items.row_count == 1

        # The sidebar should now offer the new list; cycle to it and
        # confirm SNTS lands. Sidebar sources are: [turnover leaders,
        # default (empty), core (has SNTS)] — cycle twice to reach Core.
        sb = app.query_one(Sidebar)
        sb.reload_and_refresh()
        await pilot.pause()
        while sb.active_source.slug != "core":
            sb.action_cycle_watchlist()
            await pilot.pause()
        sb_table = sb.query_one("#sidebar-table", DataTable)
        assert sb_table.row_count == 1
        first_key = sb_table.coordinate_to_cell_key((0, 0)).row_key.value
        assert first_key == "SNTS"


async def test_duplicate_watchlist_name_is_a_validation_error_not_a_crash(tui_db):
    """F-06: a second `Core` used to raise `sqlite3.IntegrityError` up
    through the input handler and take the whole app down. It must
    surface as a validation notify while the app stays running."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        wv = app.query_one(WatchlistsView)
        name_input = wv.query_one("#wl-new-name")

        for _ in range(2):
            name_input.focus()
            await pilot.press(*list("Core"))
            await pilot.press("enter")
            await pilot.pause()

        wl = wv.query_one("#wl-list", DataTable)
        # Default (seeded) + Core (added once) = 2 lists — the second Core
        # was rejected, not silently duplicated or crashed on.
        assert wl.row_count == 2
        # And the app is still alive to answer queries.
        assert app.query_one(WatchlistsView) is wv


async def test_watchlist_remove_uses_x_not_r(tui_db):
    """F-06: `r` was bound to remove-member, shadowing the app-level
    `r` = refresh. A habitual refresh press deleted rows silently. Now
    `x` removes and `r` refreshes."""
    from brvm.services import watchlist as wl_svc

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        wv = app.query_one(WatchlistsView)
        name_input = wv.query_one("#wl-new-name")
        name_input.focus()
        await pilot.press(*list("Core"))
        await pilot.press("enter")
        await pilot.pause()

        add_input = wv.query_one("#wl-add-ticker")
        add_input.focus()
        await pilot.press(*list("SNTS"))
        await pilot.press("enter")
        await pilot.pause()

        items = wv.query_one("#wl-items", DataTable)
        assert items.row_count == 1

        # `r` on the items table used to remove the row. It must NOT any
        # more — the row survives, and the app-level refresh runs
        # instead (verified by asking the service directly).
        items.focus()
        await pilot.press("r")
        await pilot.pause()
        assert items.row_count == 1
        assert wl_svc.get_with_quotes("core").items[0].ticker == "SNTS"

        # `x` is the new remove binding.
        await pilot.press("x")
        await pilot.pause()
        assert items.row_count == 0
        assert wl_svc.get_with_quotes("core").items == []


async def test_escape_blurs_watchlist_input(tui_db):
    """Typing in the watchlist name/ticker inputs used to trap focus —
    the user had to click somewhere else to escape and use `h`/`d`/`w`
    again. Escape now blurs back to the watchlist list."""
    from textual.widgets import Input

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        wv = app.query_one(WatchlistsView)
        name_input = wv.query_one("#wl-new-name", Input)
        name_input.focus()
        await pilot.pause()
        assert app.focused is name_input

        await pilot.press("escape")
        await pilot.pause()
        # Focus is off the input — the app-level shortcuts fire again.
        assert app.focused is not name_input


async def test_escape_blurs_alerts_input(tui_db):
    """Same escape-to-blur affordance in the Alerts view — a focused
    new-rule input used to swallow every app shortcut until the user
    clicked elsewhere."""
    from textual.widgets import Input

    from brvm.apps.tui.views.alerts import AlertsView as _AV

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        av = app.query_one(_AV)
        ticker_input = av.query_one("#new-ticker", Input)
        ticker_input.focus()
        await pilot.pause()
        assert app.focused is ticker_input

        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is not ticker_input


async def test_pressing_t_with_no_ticker_opens_the_palette(tui_db):
    """Pressing `t` on a fresh session used to switch to the Ticker
    view and strand the user on a "Select a ticker…" placeholder. It
    now opens the search palette on top so the user picks one instead
    of having to remember ctrl+k."""
    from brvm.apps.tui.palette import SearchPalette

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # No ticker loaded yet.
        tv = app.query_one(TickerView)
        assert tv.has_ticker is False

        await pilot.press("t")
        await pilot.pause()
        # The Ticker view is switched in AND the palette is on top.
        cs = app.query_one("#right-pane", ContentSwitcher)
        assert cs.current == "ticker"
        assert isinstance(app.screen, SearchPalette)


async def test_pressing_t_with_loaded_ticker_skips_the_palette(tui_db):
    """If a ticker is already loaded, `t` should just re-show the view
    without pushing the palette — otherwise a habitual "back to my
    ticker" press becomes a modal interruption."""
    from brvm.apps.tui.palette import SearchPalette

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        tv = app.query_one(TickerView)
        tv.set_ticker("SNTS")
        await pilot.pause()
        # Switch to a different view first, so `t` has something to do.
        await pilot.press("d")
        await pilot.pause()

        await pilot.press("t")
        await pilot.pause()
        cs = app.query_one("#right-pane", ContentSwitcher)
        assert cs.current == "ticker"
        assert not isinstance(app.screen, SearchPalette)


async def test_ticker_tabs_hide_equity_concerns_for_bonds(tui_db):
    """A bond ticker must not advertise Peers / Financials / Corp actions
    / Analyst — they are all N/A. Bond details (the DES + CSHF + YAS +
    REL composite) is where a bond user actually lands."""
    from textual.widgets import TabbedContent

    from brvm.db import connect
    from brvm.models import Security
    from brvm.store import securities as sec_repo

    with connect(tui_db) as conn:
        sec_repo.upsert(conn, [
            Security(
                ticker="BIDCO4", name="BIDC.O4 SUPRA 6.10% 2027",
                kind="bond", country="CI", coupon_rate=6.10,
                maturity_year=2027, issuer_name="BIDC",
            ),
        ])

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        tv = app.query_one(TickerView)
        tv.set_ticker("BIDCO4")
        await pilot.pause()

        tabs = tv.query_one(TabbedContent)
        # Equity-only tabs are hidden from the strip for bonds. Query
        # the Tab widget (the button in the strip) — TabPane.display is
        # driven by TabbedContent's inner ContentSwitcher and only
        # reflects which pane is currently active, not tab visibility.
        for hidden in ("tab-financials", "tab-peers", "tab-actions", "tab-analyst"):
            assert tabs.get_tab(hidden).display is False, (
                f"expected {hidden} to be hidden for kind='bond'"
            )
        # The bond-only Bond details tab and cross-kind tabs are visible.
        for shown in ("tab-bond", "tab-chart", "tab-news"):
            assert tabs.get_tab(shown).display is True, (
                f"expected {shown} to be visible for kind='bond'"
            )


async def test_ticker_tabs_hide_bond_details_for_equities(tui_db):
    """The Bond details tab is a bond-only composite; it must not
    dangle on equity pages advertising 'This tab is only meaningful
    for bonds.'"""
    from textual.widgets import TabbedContent

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        tv = app.query_one(TickerView)
        tv.set_ticker("SNTS")
        await pilot.pause()

        tabs = tv.query_one(TabbedContent)
        assert tabs.get_tab("tab-bond").display is False
        # The equity-standard tabs stay in the strip.
        for shown in (
            "tab-overview", "tab-chart", "tab-news",
            "tab-financials", "tab-peers", "tab-actions", "tab-analyst",
        ):
            assert tabs.get_tab(shown).display is True, (
                f"expected {shown} to be visible for kind='equity'"
            )


async def test_ticker_tabs_index_shows_only_chart_and_news(tui_db):
    """Indexes have no fundamentals, no peers, no dividends — the only
    tabs with real data are Chart and News. Everything else was
    rendering N/A copy that just added noise to the tab strip."""
    from textual.widgets import TabbedContent

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        tv = app.query_one(TickerView)
        tv.set_ticker("BRVMC")
        await pilot.pause()

        tabs = tv.query_one(TabbedContent)
        for hidden in (
            "tab-overview", "tab-financials", "tab-peers",
            "tab-actions", "tab-analyst", "tab-bond",
        ):
            assert tabs.get_tab(hidden).display is False, (
                f"expected {hidden} to be hidden for kind='index'"
            )
        assert tabs.get_tab("tab-chart").display is True
        assert tabs.get_tab("tab-news").display is True


async def test_ticker_tabs_switch_kind_reflows_visibility(tui_db):
    """Opening a bond after an equity must retire the equity-only tabs
    (Peers/Financials/…) and light up Bond details. If the currently-
    active tab is invalidated by the kind change it jumps to the new
    kind's default (bonds land on Bond details)."""
    from textual.widgets import TabbedContent

    from brvm.db import connect
    from brvm.models import Security
    from brvm.store import securities as sec_repo

    with connect(tui_db) as conn:
        sec_repo.upsert(conn, [
            Security(
                ticker="BIDCO4", name="BIDC.O4 SUPRA 6.10% 2027",
                kind="bond", country="CI", coupon_rate=6.10,
                maturity_year=2027, issuer_name="BIDC",
            ),
        ])

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        tv = app.query_one(TickerView)
        # First open an equity + park on the Peers tab.
        tv.set_ticker("SNTS")
        await pilot.pause()
        tabs = tv.query_one(TabbedContent)
        tabs.active = "tab-peers"
        await pilot.pause()

        # Now open a bond — Peers must disappear, and the active tab
        # can't stay on the now-hidden pane.
        tv.set_ticker("BIDCO4")
        await pilot.pause()
        assert tabs.get_tab("tab-peers").display is False
        assert tabs.active == "tab-bond"


async def test_clicking_empty_ticker_header_opens_palette(tui_db):
    """A pointer-only user landing on the empty Ticker view can click
    the "Select a ticker…" prompt to open the picker — no need to
    remember ctrl+k."""
    from brvm.apps.tui.palette import SearchPalette

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Show the Ticker view (empty).
        await pilot.press("t")
        await pilot.pause()
        # The `t` binding already pushes the palette; dismiss it first
        # so we're testing the click path in isolation.
        if isinstance(app.screen, SearchPalette):
            await pilot.press("escape")
            await pilot.pause()

        tv = app.query_one(TickerView)
        assert tv.has_ticker is False
        # A click anywhere on the empty view (the placeholder header
        # dominates the screen) posts HeaderClicked; the app pushes
        # the palette in response.
        await pilot.click(TickerView)
        await pilot.pause()
        assert isinstance(app.screen, SearchPalette)


async def test_sidebar_capital_w_cycles_watchlists(tui_db):
    """F-06: the sidebar's `shift+w` binding never fired from a real
    terminal (terminals send the character `"W"`; Textual doesn't map
    that onto a `shift+w` binding). Binding the raw uppercase key makes
    the advertised behaviour actually work."""
    from brvm.services import watchlist as wl_svc

    # Seed a second watchlist so the cycle has somewhere to go.
    wl_svc.create("Core")

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sb = app.query_one(Sidebar)
        sb.reload_and_refresh()
        await pilot.pause()

        start = sb.active_source.slug
        sb.focus()
        await pilot.press("W")
        await pilot.pause()
        assert sb.active_source.slug != start


async def test_market_closed_pauses_tick_refresh(tui_db, monkeypatch):
    """When `is_market_open` is False, `_tick_refresh` no-ops (no view refresh)."""
    from brvm.apps.tui import app as tui_app

    monkeypatch.setattr(tui_app, "is_market_open", lambda: False)
    calls: list[str] = []

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Wrap the sidebar's refresh_data to detect whether the auto tick
        # would have touched it. The manual `r` binding must still work.
        sb = app.query_one(Sidebar)
        orig = sb.refresh_data
        sb.refresh_data = lambda: (calls.append("sidebar"), orig())[-1]

        app._tick_refresh()
        await pilot.pause()
        assert calls == []  # off-hours ⇒ no automatic refresh

        # But the manual refresh still fires.
        app.action_refresh_now()
        await pilot.pause()
        assert "sidebar" in calls


async def test_chrome_shows_market_closed_off_hours(tui_db, monkeypatch):
    from brvm.apps.tui import app as tui_app

    monkeypatch.setattr(tui_app, "is_market_open", lambda: False)
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._paint_chrome()
        await pilot.pause()
        status = app.query_one("#market-status", Static)
        assert "CLOSED" in _plain(status.render())
        assert status.has_class("-closed")


async def test_home_shows_brief_collapsed_and_expands(tui_db):
    """Daily brief moved off the per-ticker view onto Home. It renders
    collapsed by default, expands to show the full markdown."""
    from textual.widgets import Collapsible, Markdown

    from brvm.db import connect
    from brvm.models import Brief
    from brvm.store import briefs as briefs_repo

    with connect(tui_db) as conn:
        briefs_repo.upsert(
            conn,
            Brief(
                day="2026-08-26",
                markdown="# Test brief\n\nMarket up broadly.",
                model="claude-haiku",
                context_json="{}",
            ),
        )

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        collapsible = app.query_one("#home-brief", Collapsible)
        # Collapsed by default so the fold-first area stays terse.
        assert collapsible.collapsed is True
        # Summary line always visible.
        summary = app.query_one("#home-brief-summary", Static)
        assert "2026-08-26" in _plain(summary.render())
        # Body is present in the tree and populated even while collapsed.
        body = app.query_one("#home-brief-md", Markdown)
        assert "Test brief" in _plain(body._markdown or "")


async def test_home_brief_summary_when_no_brief(tui_db):
    """No brief on file → summary explains it, body has a run-it hint."""
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        summary = app.query_one("#home-brief-summary", Static)
        assert "not yet generated" in _plain(summary.render())


async def test_ticker_view_has_no_brief_tab(tui_db):
    """The Brief tab was removed from the ticker view — the daily brief
    is a global summary so it lives on Home now."""
    from textual.widgets import TabbedContent

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sb = app.query_one(Sidebar)
        table = sb.query_one("#sidebar-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        tv = app.query_one(TickerView)
        tabs = tv.query_one("#ticker-tabs", TabbedContent)
        ids = {pane.id for pane in tabs.query("TabPane")}
        assert "tab-brief" not in ids
        # Non-Brief tabs still present.
        assert {"tab-overview", "tab-chart", "tab-financials"}.issubset(ids)


async def test_ticker_chart_tab_uses_plotext_widget(tui_db):
    """Regression test for the "chart shows mix of description + pixels"
    bug: the chart tab must render via textual-plotext's PlotextPlot
    widget, not Static.update(plt.build()) (which dumps raw ANSI)."""
    from textual_plotext import PlotextPlot

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sb = app.query_one(Sidebar)
        table = sb.query_one("#sidebar-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        tv = app.query_one(TickerView)
        # The chart pane holds a PlotextPlot instance, not a Static.
        assert tv.query_one("#chart-plot", PlotextPlot) is not None


async def test_ticker_tabs_are_scrollable(tui_db):
    """Regression test for the "can't scroll ticker tabs" bug: every
    non-DataTable TabPane wraps its body in `VerticalScroll` so the
    long Financials / Overview / Analyst bodies scroll with the wheel."""
    from textual.containers import VerticalScroll

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sb = app.query_one(Sidebar)
        table = sb.query_one("#sidebar-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        tv = app.query_one(TickerView)
        # Overview, Financials, and Analyst all live inside a scroll
        # container so long content is reachable.
        assert tv.query_one("#tab-overview VerticalScroll", VerticalScroll)
        assert tv.query_one("#tab-financials VerticalScroll", VerticalScroll)
        assert tv.query_one("#tab-analyst VerticalScroll", VerticalScroll)


async def test_news_tables_have_link_column(tui_db):
    """Home / ticker / standalone news tables all expose a `Link` column
    that renders the source URL as an OSC-8 hyperlink (clickable in
    modern terminals). Cell content is a Rich `Text` with the `link`
    style — not a plain string — so we assert the style rather than
    the visible label.

    Regression pins the column *shape* — the exact styled-Text rendering
    varies across terminal emulators, but the presence of the column
    and the `link` style is the invariant that matters."""
    from datetime import UTC, datetime, timedelta

    from rich.text import Text
    from textual.widgets import DataTable

    from brvm.db import connect
    from brvm.models import NewsItem
    from brvm.store import news as news_repo

    with connect(tui_db) as conn:
        now = datetime.now(UTC)
        news_repo.upsert_news_items(
            conn,
            [
                NewsItem(
                    source="sikafinance", kind="news",
                    url="https://sikafinance.com/marches/actualites/foo",
                    url_hash="hash-foo",
                    title="Big earnings beat", chapeau=None,
                    ticker_hint="SNTS",
                    published_at=(now - timedelta(hours=1)).isoformat(),
                ),
            ],
        )
        # Backfill the relevance so the row passes home-news's min-6 filter.
        conn.execute(
            "UPDATE news_items SET relevance = 8, category_llm = 'earnings' "
            "WHERE title = ?",
            ("Big earnings beat",),
        )
        conn.commit()

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()

        # Home news feed.
        home = app.query_one("#home-news", DataTable)
        assert len(home.columns) == 6
        assert list(home.columns.values())[-1].label.plain == "Link"
        link = home.get_row_at(0)[-1]
        assert isinstance(link, Text)
        assert "https://sikafinance.com" in link.style

        # Ticker view News tab.
        sb = app.query_one(Sidebar)
        table = sb.query_one("#sidebar-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        tv = app.query_one(TickerView)
        # Force a repaint against the SNTS feed so the row lands.
        tv.set_ticker("SNTS")
        await pilot.pause()
        ticker_news = tv.query_one("#ticker-news", DataTable)
        assert len(ticker_news.columns) == 5
        assert list(ticker_news.columns.values())[-1].label.plain == "Link"
        if ticker_news.row_count:
            cell = ticker_news.get_row_at(0)[-1]
            assert isinstance(cell, Text)
            assert "https://sikafinance.com" in cell.style

        # Standalone /news view.
        await pilot.press("f5")
        await pilot.pause()
        news_view = app.query_one(NewsView)
        news_table = news_view.query_one("#news-table", DataTable)
        assert len(news_table.columns) == 6
        assert list(news_table.columns.values())[-1].label.plain == "Link"


async def test_pressing_o_on_home_news_opens_the_story_url(tui_db, monkeypatch):
    """DataTable eats mouse clicks so an OSC-8 hyperlink in a cell is
    unreliable. The `o` binding is the fix: read the highlighted news
    row's URL and call `App.open_url(url)`."""
    from datetime import UTC, datetime, timedelta

    from textual.widgets import DataTable

    from brvm.db import connect
    from brvm.models import NewsItem
    from brvm.store import news as news_repo

    with connect(tui_db) as conn:
        now = datetime.now(UTC)
        news_repo.upsert_news_items(
            conn,
            [
                NewsItem(
                    source="sikafinance", kind="news",
                    url="https://sikafinance.com/marches/actualites/o-bind",
                    url_hash="hash-obind",
                    title="Story to open", chapeau=None,
                    ticker_hint="SNTS",
                    published_at=(now - timedelta(hours=1)).isoformat(),
                ),
            ],
        )
        conn.execute(
            "UPDATE news_items SET relevance = 8, category_llm = 'earnings' "
            "WHERE title = ?",
            ("Story to open",),
        )
        conn.commit()

    opened: list[str] = []
    app = BRVMTerminalApp()
    monkeypatch.setattr(app, "open_url", lambda url, **_kw: opened.append(url))

    async with app.run_test() as pilot:
        await pilot.pause()
        home_news = app.query_one("#home-news", DataTable)
        home_news.focus()
        await pilot.pause()
        # Cursor is on row 0 by default — press `o` to open its URL.
        await pilot.press("o")
        await pilot.pause()
        assert opened == ["https://sikafinance.com/marches/actualites/o-bind"]


async def test_pressing_o_on_ticker_news_tab_opens_the_story_url(tui_db, monkeypatch):
    """Same story as Home but from the per-ticker News tab."""
    from datetime import UTC, datetime, timedelta

    from textual.widgets import DataTable, TabbedContent

    from brvm.db import connect
    from brvm.models import NewsItem
    from brvm.store import news as news_repo

    with connect(tui_db) as conn:
        now = datetime.now(UTC)
        news_repo.upsert_news_items(
            conn,
            [
                NewsItem(
                    source="sikafinance", kind="news",
                    url="https://sikafinance.com/marches/actualites/ticker",
                    url_hash="hash-tickernews",
                    title="Ticker-scoped story", chapeau=None,
                    ticker_hint="SNTS",
                    published_at=(now - timedelta(hours=1)).isoformat(),
                ),
            ],
        )
        conn.commit()

    opened: list[str] = []
    app = BRVMTerminalApp()
    monkeypatch.setattr(app, "open_url", lambda url, **_kw: opened.append(url))

    async with app.run_test() as pilot:
        await pilot.pause()
        sb = app.query_one(Sidebar)
        sb_table = sb.query_one("#sidebar-table", DataTable)
        sb_table.focus()
        await pilot.press("enter")
        await pilot.pause()
        tv = app.query_one(TickerView)
        tv.set_ticker("SNTS")
        await pilot.pause()
        tabs = tv.query_one("#ticker-tabs", TabbedContent)
        tabs.active = "tab-news"
        await pilot.pause()
        news_table = tv.query_one("#ticker-news", DataTable)
        news_table.focus()
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert opened == ["https://sikafinance.com/marches/actualites/ticker"]


async def test_link_cell_falls_back_to_dash_when_url_missing():
    """Some scrapers (older communiqués) land without a URL — the Link
    cell must not raise; it renders as `—`."""
    from brvm.apps.tui.format import link_cell

    assert link_cell(None) == "—"
    assert link_cell("") == "—"


async def test_search_palette_opens_and_filters(tui_db):
    """Ctrl-K opens the palette, typing `SNT` shows SNTS."""
    from brvm.apps.tui.palette import SearchPalette

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()
        palette = app.screen
        assert isinstance(palette, SearchPalette)
        inp = palette.query_one("#palette-input")
        inp.focus()
        await pilot.press(*list("SNT"))
        await pilot.pause()
        assert "SNTS" in palette._tickers


# --- helpers ---------------------------------------------------------------


def _plain(renderable) -> str:
    """Coerce a Textual Content / Rich object / string to plain text."""
    # Textual >=8 uses `textual.content.Content`; older versions or Rich
    # renderables expose `.plain`.
    if hasattr(renderable, "plain"):
        return renderable.plain
    return str(renderable)


def _directory_cols():
    from brvm.apps.tui.views.directory import _COLS

    return _COLS


# --- Phase 8j: TUI polish batch ------------------------------------------


async def test_news_ticker_is_mounted_and_hidden_off_hours(tui_db, monkeypatch):
    """The NewsTicker sits between #body and Footer, and is paused
    (with a "market closed" message) whenever `is_market_open()` is
    False. On the wall-clock this test runs off-hours; if the wall
    clock happens to fall in market hours we monkeypatch."""
    from brvm.apps.tui.news_ticker import NewsTicker

    monkeypatch.setattr("brvm.apps.tui.app.is_market_open", lambda: False)
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # The ticker is on-screen (with a paused-state message).
        nt = app.query_one(NewsTicker)
        assert nt is not None
        rendered = _plain(nt.render())
        assert "paused" in rendered.lower() or "closed" in rendered.lower()


async def test_news_ticker_shows_headlines_when_pool_populated(tui_db, monkeypatch):
    """During market hours the ticker cycles through recent news items."""
    from brvm.apps.tui.news_ticker import NewsTicker
    from brvm.services._view import NewsFeed, NewsRow

    monkeypatch.setattr("brvm.apps.tui.app.is_market_open", lambda: True)

    def _fake_feed(**_kwargs):
        return NewsFeed(
            items=[
                NewsRow(
                    id=1, source="sikafinance", kind="news",
                    url="https://x/a", title="Sonatel posts strong H1",
                    tickers=["SNTS"], relevance=9,
                    published_at="2026-08-27T09:15:00Z",
                    fetched_utc="2026-08-27T09:20:00Z",
                ),
                NewsRow(
                    id=2, source="sikafinance", kind="news",
                    url="https://x/b", title="Orange CI expands network",
                    tickers=["ORAC"], relevance=7,
                    published_at="2026-08-27T10:15:00Z",
                    fetched_utc="2026-08-27T10:20:00Z",
                ),
            ],
            total=2, limit=10, offset=0,
            filters={
                "ticker": "", "category": "", "date_from": "",
                "date_to": "", "min_relevance": "6",
            },
        )

    monkeypatch.setattr("brvm.apps.tui.news_ticker.news_svc.list_feed", _fake_feed)
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        nt = app.query_one(NewsTicker)
        nt.refresh_pool()
        nt._render_current()
        text = _plain(nt.render())
        # The first-round headline is on screen.
        assert "Sonatel" in text or "Orange" in text


async def test_news_ticker_paint_chrome_does_not_advance_headline(tui_db, monkeypatch):
    """F-36: `_paint_chrome` runs every second and calls
    `set_paused(not market_open)`. With both the market open and the
    ticker already un-paused, that call must be a no-op — the old code
    ran `_render_current()` every second and, because `_tick` advanced
    `_idx` after rendering, showed the NEXT headline one second after
    the cadence-controlled tick had drawn the current one (each headline
    survived ≤1 s instead of the intended 5 s)."""
    from brvm.apps.tui.news_ticker import NewsTicker
    from brvm.services._view import NewsFeed, NewsRow

    monkeypatch.setattr("brvm.apps.tui.app.is_market_open", lambda: True)

    def _fake_feed(**_kwargs):
        return NewsFeed(
            items=[
                NewsRow(
                    id=1, source="sikafinance", kind="news",
                    url="https://x/a", title="Sonatel posts strong H1",
                    tickers=["SNTS"], relevance=9,
                    published_at="2026-08-27T09:15:00Z",
                    fetched_utc="2026-08-27T09:20:00Z",
                ),
                NewsRow(
                    id=2, source="sikafinance", kind="news",
                    url="https://x/b", title="Orange CI expands network",
                    tickers=["ORAC"], relevance=7,
                    published_at="2026-08-27T10:15:00Z",
                    fetched_utc="2026-08-27T10:20:00Z",
                ),
            ],
            total=2, limit=10, offset=0,
            filters={
                "ticker": "", "category": "", "date_from": "",
                "date_to": "", "min_relevance": "6",
            },
        )

    monkeypatch.setattr("brvm.apps.tui.news_ticker.news_svc.list_feed", _fake_feed)
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        nt = app.query_one(NewsTicker)
        # Force a fresh pool + first render so we know pool[0] is on
        # screen and `_idx == 0`.
        nt.refresh_pool()
        nt._render_current()
        first = _plain(nt.render())
        assert "Sonatel" in first

        # Advance one tick — `_tick` moves `_idx` to 1 and renders pool[1].
        nt._tick()
        after_tick = _plain(nt.render())
        assert "Orange" in after_tick

        # Now the app's 1-second chrome repaint fires with the market
        # still open. It must NOT redraw the "next" headline (that's the
        # F-36 regression); the screen stays on pool[1].
        for _ in range(3):
            nt.set_paused(False)
        assert _plain(nt.render()) == after_tick


async def test_news_ticker_query_filters_by_lookback_hours(tui_db, monkeypatch):
    """F-36: `LOOKBACK_HOURS = 24` used to only appear in the empty-state
    copy. The query itself had no time clause, so weeks-old headlines
    could rotate as if current. `refresh_pool` now passes `date_from`
    to `list_feed`."""
    from brvm.apps.tui.news_ticker import NewsTicker

    calls: list[dict] = []

    def _spy_feed(**kwargs):
        calls.append(kwargs)
        from brvm.services._view import NewsFeed
        return NewsFeed(
            items=[], total=0, limit=kwargs.get("limit", 10), offset=0,
            filters={},
        )

    monkeypatch.setattr("brvm.apps.tui.news_ticker.news_svc.list_feed", _spy_feed)
    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        nt = app.query_one(NewsTicker)
        calls.clear()
        nt.refresh_pool()

    assert calls, "refresh_pool should have called list_feed"
    kwargs = calls[-1]
    assert kwargs.get("date_from") is not None
    # The date_from should be roughly 24h in the past — sanity check it's
    # a parseable ISO string, not the literal placeholder from before.
    from datetime import UTC, datetime

    parsed = datetime.fromisoformat(kwargs["date_from"])
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta_hours = (datetime.now(UTC) - parsed).total_seconds() / 3600
    assert 23 <= delta_hours <= 25


async def test_slow_overview_fetch_does_not_freeze_event_loop(tui_db, monkeypatch):
    """F-34: description/peers/history used to run synchronously in
    handlers. A 2-second stub against a slow source blocked the whole
    TUI (clock, input, rendering) for ~2.7 s. Workers must let the
    event loop keep running — the placeholder text lands immediately,
    then the real render replaces it after the fetch returns."""
    import threading
    import time

    from brvm.apps.tui.views.ticker import TickerView
    from brvm.services import company as company_svc

    # Block the fetch until the test releases it. This way the placeholder
    # is definitely on screen while we assert on it.
    release = threading.Event()

    class _Profile:
        description = "Stub description that arrives after the fetch releases."
        sector = "Tech"
        industry = None
        address = None
        phone = None
        website = None
        leadership = None
        shares_outstanding = None
        market_cap = None
        shareholders = ()

    def _slow_get_description(ticker: str):
        release.wait(timeout=5.0)
        return _Profile()

    monkeypatch.setattr(company_svc, "get_description", _slow_get_description)
    # Peers can bail — this test only cares about the overview path.
    monkeypatch.setattr(
        company_svc, "get_peers_with_ratios",
        lambda t: (_ for _ in ()).throw(RuntimeError("peers not under test")),
    )

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        tv = app.query_one(TickerView)
        # Kick off a ticker open — the overview fetch would previously
        # block here for 2+ seconds before returning.
        t0 = time.monotonic()
        tv.set_ticker("SNTS")
        elapsed = time.monotonic() - t0
        # The synchronous portion must return quickly (well under our
        # 5-second worker wait). Old code blocked for the full fetch.
        assert elapsed < 1.0, f"set_ticker blocked for {elapsed:.2f}s"

        # Placeholder is on screen while the worker is still running.
        await pilot.pause()
        body = tv.query_one("#overview-body", Static)
        assert "loading" in _plain(body.render()).lower()

        # Release the worker; wait for the paint to land.
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        rendered = _plain(body.render())
        assert "Stub description" in rendered


async def test_chart_render_is_lazy_until_tab_activated(tui_db, monkeypatch):
    """Regression for #11: the Chart tab's `history.get_history` fetch
    should not fire on a scheduled refresh while the user is on a
    different tab. Opening Chart triggers exactly one render."""
    fetch_calls: list[str] = []
    real_get_history = None

    def _tracking_get_history(ticker, country=None):
        fetch_calls.append(ticker)
        # Call the real function so the chart still renders (may be empty).
        if real_get_history is not None:
            return real_get_history(ticker, country)
        return []

    from brvm.services import history as history_mod
    real_get_history = history_mod.get_history
    monkeypatch.setattr(
        "brvm.apps.tui.views.ticker.history.get_history",
        _tracking_get_history,
    )

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sb = app.query_one(Sidebar)
        table = sb.query_one("#sidebar-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        # Ticker view opens on the Overview tab. Chart hasn't rendered
        # yet — so no history fetch.
        initial_fetches = list(fetch_calls)
        assert initial_fetches == []
        # Trigger a scheduled refresh; still on Overview → still no fetch.
        tv = app.query_one(TickerView)
        tv.refresh_data()
        assert fetch_calls == initial_fetches
        # Switch to the Chart tab → fetches once.
        from textual.widgets import TabbedContent
        tabs = tv.query_one(TabbedContent)
        tabs.active = "tab-chart"
        await pilot.pause()
        assert len(fetch_calls) == 1


async def test_alerts_new_row_uses_selection_list_for_doctypes(tui_db):
    """Regression for #12: the new-rule row exposes doc_types as a
    multi-select SelectionList, not an Input."""
    from typing import get_args as _get_args

    from textual.widgets import SelectionList

    from brvm.models import FilingDocType

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        av = app.query_one(AlertsView)
        sel = av.query_one("#new-doctypes", SelectionList)
        assert sel is not None
        # SelectionList prompts + values cover every FilingDocType.
        offered = {opt.value for opt in sel._options}
        assert offered == set(_get_args(FilingDocType))


async def test_alerts_new_filing_rule_persists_selected_doctypes(
    tui_db, monkeypatch
):
    """The full flow: pick kind=new_filing, tick two doc_types, submit
    → the resulting AlertRule carries the CSV of the ticks."""
    from textual.widgets import Input, Select, SelectionList

    from brvm.services import alerts as alerts_svc

    created: list = []
    monkeypatch.setattr(
        alerts_svc, "create_rule",
        lambda r: created.append(r) or 1,
    )

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        av = app.query_one(AlertsView)
        av.query_one("#new-kind", Select).value = "new_filing"
        sel = av.query_one("#new-doctypes", SelectionList)
        sel.select("rapport_annuel")
        sel.select("resultats")
        arg = av.query_one("#new-arg", Input)
        arg.value = ""
        # Submit via the ticker Input (matches the on_input_submitted
        # gate condition — new-ticker or new-arg both trigger commit).
        ticker = av.query_one("#new-ticker", Input)
        ticker.focus()
        await pilot.press("enter")
        await pilot.pause()
    assert len(created) == 1
    rule = created[0]
    assert rule.kind == "new_filing"
    # CSV order matches the SelectionList's insertion order (Textual
    # preserves selection order, which for us follows toggle order).
    assert set(rule.doc_types.split(",")) == {"rapport_annuel", "resultats"}


async def test_alerts_new_filing_needs_at_least_one_doctype(tui_db, monkeypatch):
    """Submitting new_filing with an empty SelectionList should surface
    a warning notify and NOT create a rule."""
    from textual.widgets import Input, Select

    from brvm.services import alerts as alerts_svc

    created: list = []
    monkeypatch.setattr(
        alerts_svc, "create_rule",
        lambda r: created.append(r) or 1,
    )
    notifications: list = []
    from brvm.apps.tui.views.alerts import AlertsView as AV

    def _fake_notify(self, message, *, severity="information"):
        notifications.append((severity, message))

    monkeypatch.setattr(AV, "notify", _fake_notify)

    app = BRVMTerminalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        av = app.query_one(AlertsView)
        av.query_one("#new-kind", Select).value = "new_filing"
        av.query_one("#new-arg", Input).value = ""
        ticker = av.query_one("#new-ticker", Input)
        ticker.focus()
        await pilot.press("enter")
        await pilot.pause()
    assert created == []
    assert any("doc_type" in msg for _, msg in notifications)
