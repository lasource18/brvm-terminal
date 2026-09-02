# Screenshots

Captured against the real database — live BRVM data, not mock-ups.
All PNGs are 2× (retina); the TUI frame is Textual's own SVG export, rasterised.

**The web set is French.** The audience is majority francophone, so `fr` is the
default the capture script ships. `--locale en` regenerates the same shots in
English if a page is ever needed for an anglophone reader.

| File | What it shows |
| --- | --- |
| `01-market-overview.png` | `/` — index strip, turnover leaders, gainers/losers, 30-day dividend calendar. The hook image. |
| `02-security-chart-snts.png` | `/s/SNTS/chart` — Sonatel OHLC candles + volume, three months. |
| `03-financials-snts.png` | `/s/SNTS/financials` — annual + interim statements, 18 ratios, and the filing references each number came from. |
| `04-news-feed.png` | `/news` — the AI layer: category badge, relevance score, and the summary in the reader's language. |
| `05-tui.png` | The Textual TUI — indices, movers, top news and the brief in one terminal frame. **English:** the TUI has no i18n yet. |
| `06-daily-brief.png` | `/brief` — a generated session recap with movers, dividends and what to watch. |
| `07-analyst-note-snts.png` | `/s/SNTS/analyst` — the weekly per-ticker note. |

## Regenerating

```bash
just screenshots            # French set (default)
just screenshots --locale en
```

`scripts/screenshots.py` serves the app against a `sqlite3` backup copy in a
temp dir with `build_scheduler` stubbed to a no-op, so a capture run can't write
to the live DB or fire a job. It is not offline, though: routes that fetch on
demand (the chart tab pulls history) still reach the network — they just land in
the throwaway copy.

Per-shot window sizes live in the `SHOTS` table at the top of the script. French
copy runs longer than English, so a shot that gains a row needs its height bumped
there rather than cropped afterwards.

Two Chrome gotchas worth keeping:

- **`--headless=old`.** The new headless mode wants a display and hangs on macOS.
- **`--timeout` is what actually stops it.** The pages poll (HTMX auto-refresh,
  chart data), so `--virtual-time-budget` alone never expires and Chrome waits
  forever.

The TUI frame comes from Textual's test harness rather than a terminal capture:
`app.run_test(size=(190, 46))` then `app.save_screenshot(...)`, which renders the
real widget tree against the real database and writes an SVG.

## Known gaps

- **The TUI is English-only.** `src/kodji/apps/tui/` has no `i18n` wiring at all,
  so `--locale fr` does nothing for `05-tui.png`. Localising it is queued with
  the TUI sync work (PR-AC).
- **Filing doc-type codes** (`rapport_activites`, `etats_financiers`) and period
  labels (`annual 2025`) render as stored. They are data keys, not chrome.
