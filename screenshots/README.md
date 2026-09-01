# Screenshots

Captured 1 Sep 2026 against the real database — live BRVM data, not mock-ups.
All PNGs are 2× (retina); the TUI frame is Textual's own SVG export, rasterised.

| File | What it shows |
| --- | --- |
| `01-market-overview.png` | `/` — index strip, turnover leaders, gainers/losers, 30-day dividend calendar. The hook image. |
| `02-security-chart-snts.png` | `/s/SNTS/chart` — Sonatel OHLC candles + volume, three months. |
| `03-financials-snts.png` | `/s/SNTS/financials` — annual + interim statements, 18 ratios, and the filing references each number came from. |
| `04-news-feed.png` | `/news` — the AI layer: category badge, relevance score, and FR/EN summaries per item. |
| `05-tui.png` | The Textual TUI — indices, movers, top news and the brief in one terminal frame. |
| `06-daily-brief.png` | `/brief` — a generated session recap with movers, dividends and what to watch. |
| `07-analyst-note-snts.png` | `/s/SNTS/analyst` — the weekly per-ticker note. |

## Regenerating

Serve the app, then drive Chrome headlessly:

```bash
just dev    # or point DB_PATH at a copy so nothing writes to your live DB
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=old --no-sandbox --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 --window-size=1600,1500 --timeout=12000 \
  --screenshot=screenshots/01-market-overview.png http://127.0.0.1:8765/
```

Two gotchas worth keeping:

- **`--headless=old`.** The new headless mode wants a display and hangs on macOS.
- **`--timeout` is what actually stops it.** The pages poll (HTMX auto-refresh,
  chart data), so `--virtual-time-budget` alone never expires and Chrome waits
  forever.

The TUI frame comes from Textual's test harness rather than a terminal capture:
`app.run_test(size=(190, 46))` then `app.save_screenshot(...)`, which renders the
real widget tree against the real database and writes an SVG.
