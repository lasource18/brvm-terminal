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
