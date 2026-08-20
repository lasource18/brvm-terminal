"""Refetch the live pages that back our parser fixtures.

Dev-only. Hits the network. Run manually (`just refresh-fixtures`) when we
suspect a source layout has shifted. Never run from CI.

Each target is saved verbatim to tests/fixtures/<subdir>/<name>. The BOC
PDF filename embeds the resolved date so we can keep multiple captures.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from brvm.config import settings  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

UA = settings.http_user_agent
TIMEOUT = settings.http_timeout_s


@dataclass(frozen=True)
class Target:
    url: str
    dest: Path


def _targets() -> list[Target]:
    sika = "https://www.sikafinance.com"
    afx = "https://afx.kwayisi.org"
    brvm = "https://www.brvm.org"
    return [
        Target(f"{sika}/marches/aaz", FIXTURES / "sikafinance" / "aaz.html"),
        Target(
            f"{sika}/marches/cotation_SNTS.sn",
            FIXTURES / "sikafinance" / "cotation_SNTS.html",
        ),
        Target(
            f"{sika}/marches/cotation_ORAC.ci",
            FIXTURES / "sikafinance" / "cotation_ORAC.html",
        ),
        Target(
            f"{sika}/marches/cotation_BRVMC",
            FIXTURES / "sikafinance" / "cotation_BRVMC.html",
        ),
        Target(
            f"{sika}/marches/historiques/SNTS.sn",
            FIXTURES / "sikafinance" / "historique_SNTS.html",
        ),
        Target(f"{sika}/marches/palmares", FIXTURES / "sikafinance" / "palmares.html"),
        Target(
            f"{sika}/marches/societe/SNTS.sn",
            FIXTURES / "sikafinance" / "societe_SNTS.html",
        ),
        Target(
            f"{sika}/marches/secteur/SNTS.sn",
            FIXTURES / "sikafinance" / "secteur_SNTS.html",
        ),
        Target(f"{afx}/brvm/", FIXTURES / "afx" / "index.html"),
        Target(f"{afx}/brvm/snts.html", FIXTURES / "afx" / "snts.html"),
        Target(
            f"{brvm}/en/marche/bulletin-officiel-de-la-cote",
            FIXTURES / "brvm_org" / "boc_landing.html",
        ),
        Target(f"{brvm}/en/cours-actions/0", FIXTURES / "brvm_org" / "cours-actions-0.html"),
    ]


def _save(client: httpx.Client, t: Target) -> None:
    t.dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[fetch] {t.url}")
    r = client.get(t.url)
    r.raise_for_status()
    t.dest.write_bytes(r.content)
    print(f"        -> {t.dest.relative_to(ROOT)} ({len(r.content)} bytes)")


_BOC_HREF_RE = re.compile(r'href="([^"]+boc_(?:fr|eng)_\d{8}[^"]*\.pdf)"', re.IGNORECASE)


def _capture_boc_pdf(client: httpx.Client, landing_html: str) -> None:
    matches = _BOC_HREF_RE.findall(landing_html)
    if not matches:
        print("[warn] no BOC PDF href found in landing page; skipping PDF capture")
        return
    for href in matches:
        url = href if href.startswith("http") else f"https://www.brvm.org{href}"
        name = Path(url).name
        dest = FIXTURES / "brvm_org" / name
        print(f"[fetch] {url}")
        r = client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
        print(f"        -> {dest.relative_to(ROOT)} ({len(r.content)} bytes)")
        # One PDF is enough for fixture purposes; keep first.
        break


def main() -> None:
    headers = {"User-Agent": UA, "Accept-Language": "fr,en;q=0.7"}
    with httpx.Client(headers=headers, timeout=TIMEOUT, follow_redirects=True) as client:
        landing_html: str | None = None
        for t in _targets():
            try:
                _save(client, t)
                if t.dest.name == "boc_landing.html":
                    landing_html = t.dest.read_text(encoding="utf-8", errors="replace")
            except httpx.HTTPError as e:
                print(f"[error] {t.url}: {e}")
            time.sleep(0.8)  # polite pacing
        if landing_html:
            try:
                _capture_boc_pdf(client, landing_html)
            except httpx.HTTPError as e:
                print(f"[error] BOC PDF: {e}")


if __name__ == "__main__":
    main()
