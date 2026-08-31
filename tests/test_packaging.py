"""Guards for the kodji-terminal package identity (PR-V rename).

The rename from `brvm` to `kodji` touched 785 import sites while
deliberately leaving 1 122 references to BRVM-the-exchange alone. These
tests pin both halves so neither drifts back: a stray `from brvm.` would
not fail at import time (the old distribution is simply gone from the
env, so it fails at *run* time on some rarely-exercised path), and an
over-eager future rename could quietly break the scrapers by rewriting
brvm.org URLs or the BRVMC index code.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "kodji"

# Anchored to line start so it matches real import statements only. Prose
# in docstrings routinely says "from brvm.org", and `brvm_org` is the source
# module named after that domain — neither is a stale package reference.
_STALE_IMPORT = re.compile(r"^\s*(?:from|import)\s+brvm(?![\w])")


def _python_files() -> list[Path]:
    return [
        p
        for p in (*SRC.rglob("*.py"), *(ROOT / "tests").rglob("*.py"), *(ROOT / "scripts").rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


def test_no_stale_brvm_package_imports() -> None:
    offenders = [
        f"{p.relative_to(ROOT)}:{i}"
        for p in _python_files()
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _STALE_IMPORT.search(line)
    ]
    assert offenders == [], f"stale `brvm` package imports: {offenders}"


def test_package_is_importable_as_kodji() -> None:
    import kodji.config
    import kodji.services.market
    import kodji.store.securities

    assert kodji.config.settings.db_path.endswith(".sqlite")


def test_exchange_references_are_preserved() -> None:
    """BRVM is the market this product covers, not the product's name.

    A blanket s/brvm/kodji/ would silently repoint the scrapers at a
    domain that doesn't exist, so assert the real ones survived.
    """
    afx = (SRC / "sources" / "afx_kwayisi.py").read_text(encoding="utf-8")
    assert "/brvm/" in afx, "afx.kwayisi.org/brvm URL path was rewritten"

    assert (SRC / "sources" / "brvm_org.py").exists(), "brvm.org source module was renamed"

    org_urls = [
        line
        for p in (SRC / "sources").rglob("*.py")
        for line in p.read_text(encoding="utf-8").splitlines()
        if "brvm.org" in line
    ]
    assert org_urls, "no brvm.org references left in sources/ — URLs were rewritten"


def test_product_identity_is_kodji() -> None:
    from kodji.apps.tui.app import KodjiTerminalApp
    from kodji.apps.web.main import app

    assert app.title == "kodji-terminal"
    assert KodjiTerminalApp.TITLE == "kodji-terminal"
