"""Entry point: `python -m kodji.apps.tui` or `kodji-tui`."""

from __future__ import annotations


def main() -> None:
    from kodji.apps.tui.app import KodjiTerminalApp

    KodjiTerminalApp().run()


if __name__ == "__main__":
    main()
