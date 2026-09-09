"""Regenerate the product screenshots in `screenshots/`.

Run with `just screenshots` (or `uv run python scripts/screenshots.py`).
Pass `--locale fr` for the French set — the audience is majority
francophone, so FR is what ships on the README and landing page.

Three things this handles that a manual Chrome invocation does not:

**It never writes to the live database.** The app is served against a
`sqlite3` backup copy in a temp dir and `build_scheduler` is replaced
with a no-op, so no job fires and nothing a capture does can reach the
real file. Note this is not the same as offline: routes that fetch on
demand (the chart tab pulls history) still go out over the network,
they just land in the throwaway copy.

**`--headless=old`.** Chrome's new headless mode wants a display and
hangs on macOS.

**`--timeout` is what actually ends a capture.** These pages poll (HTMX
auto-refresh, chart data), so `--virtual-time-budget` never expires and
Chrome waits forever.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "screenshots"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# CSS pixel window per shot; every PNG lands at 2x these numbers. Heights
# are sized to the content — French copy runs longer than English, so a
# shot that gains a row needs its height bumped here, not cropped after.
SHOTS = [
    ("01-market-overview.png", "/", (1600, 800)),
    ("02-security-chart-snts.png", "/s/SNTS/chart", (1600, 660)),
    ("03-financials-snts.png", "/s/SNTS/financials", (1600, 2290)),
    ("04-news-feed.png", "/news", (1600, 1312)),
    ("06-daily-brief.png", "/brief", (1600, 860)),
    # Narrower than the rest: at 1600 the prose column leaves half the
    # frame empty. 1400 is the floor that still fits the FR topbar on one row.
    ("07-analyst-note-snts.png", "/s/SNTS/analyst", (1400, 1610)),
]
TUI_SHOT = "05-tui.png"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _backup_db(dest: Path) -> Path:
    """A real `sqlite3` backup, not a file copy — the live DB is in WAL
    mode, so copying the .sqlite alone would miss uncommitted pages."""
    from kodji.config import settings

    src = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    return dest


def _serve(port: int) -> threading.Thread:
    import uvicorn

    from kodji.jobs import scheduler as sched_mod

    class _NoopScheduler:
        def start(self): ...
        def shutdown(self, wait=False): ...
        def get_jobs(self): return []

    sched_mod.build_scheduler = lambda *a, **k: _NoopScheduler()

    from kodji.apps.web.main import app

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            urlopen(f"http://127.0.0.1:{port}/", timeout=1).read()
            return t
        except Exception:
            time.sleep(0.2)
    raise SystemExit("server never came up")


def _capture(url: str, out: Path, size: tuple[int, int]) -> None:
    subprocess.run(
        [
            CHROME, "--headless=old", "--no-sandbox", "--disable-gpu",
            "--hide-scrollbars", "--force-device-scale-factor=2",
            f"--window-size={size[0]},{size[1]}", "--timeout=12000",
            f"--screenshot={out}", url,
        ],
        check=True,
        capture_output=True,
    )


def _capture_tui(out: Path, locale: str) -> None:
    """Textual's own harness, not a terminal grab: it renders the real
    widget tree against the real database and writes an SVG we rasterise."""
    import asyncio

    from kodji.apps.tui.app import KodjiApp

    svg = out.with_suffix(".svg")

    async def run() -> None:
        app = KodjiApp()
        async with app.run_test(size=(190, 46)):
            await asyncio.sleep(3)
            app.save_screenshot(str(svg))

    asyncio.run(run())
    subprocess.run(
        [CHROME, "--headless=old", "--no-sandbox", "--disable-gpu",
         "--force-device-scale-factor=2", "--window-size=2336,1173",
         "--timeout=8000", f"--screenshot={out}", svg.as_uri()],
        check=True, capture_output=True,
    )
    svg.unlink()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--locale", default="fr", choices=("fr", "en"))
    ap.add_argument("--skip-tui", action="store_true")
    args = ap.parse_args()

    if not Path(CHROME).exists():
        print(f"Chrome not found at {CHROME}", file=sys.stderr)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="kodji-shots-"))
    try:
        db = _backup_db(tmp / "kodji.sqlite")

        # `settings` is a proxy, so re-pointing it after import is enough —
        # no module reloads, same trick the test suite uses.
        import os

        from kodji.config import reset_settings_cache

        os.environ["DB_PATH"] = str(db)
        reset_settings_cache()

        port = _free_port()
        _serve(port)
        base = f"http://127.0.0.1:{port}"

        for name, path, size in SHOTS:
            sep = "&" if "?" in path else "?"
            url = f"{base}{path}{sep}lang={args.locale}"
            _capture(url, OUT / name, size)
            print(f"  {name}  {size[0]}x{size[1]}  {path}")

        if not args.skip_tui:
            _capture_tui(OUT / TUI_SHOT, args.locale)
            print(f"  {TUI_SHOT}")

        print(f"\n{args.locale} set written to {OUT}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
