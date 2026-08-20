"""Small logging bootstrap (stdlib logging, one-line format)."""

from __future__ import annotations

import logging

from brvm.config import settings

_CONFIGURED = False


def setup() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)
