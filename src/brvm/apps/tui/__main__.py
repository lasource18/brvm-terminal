"""Entry point: `python -m brvm.apps.tui` or `brvm-tui`."""

from __future__ import annotations


def main() -> None:
    from brvm.apps.tui.app import BRVMTerminalApp

    BRVMTerminalApp().run()


if __name__ == "__main__":
    main()
