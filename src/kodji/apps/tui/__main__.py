"""Entry point: `python -m kodji.apps.tui` or `kodji-tui`."""

from __future__ import annotations

import sys


def main() -> None:
    from kodji.apps.tui.app import KodjiTerminalApp
    from kodji.config import settings
    from kodji.db import PendingMigrations, assert_schema_current

    # Same guard as the web app. A Textual screen is a bad place to
    # discover a missing column: the traceback lands under the alternate
    # screen buffer, so fail here where the message is readable.
    try:
        assert_schema_current(settings.db_path)
    except PendingMigrations as e:
        print(f"kodji: {e}", file=sys.stderr)
        raise SystemExit(2) from None

    KodjiTerminalApp().run()


if __name__ == "__main__":
    main()
