"""Generate the kodji logo, app icons, favicons and marketing exports.

Run with `just logo`. Everything below comes from one geometric `K`
defined here as vector paths — no font is involved, so the SVGs render
identically on a machine that has never heard of JetBrains Mono.

The two colours are the site's own tokens (`static/style.css`):
accent `#ffb454` on background `#0b0f14`.

**Why the glyph is stroked and then clipped.** The `K` is three strokes:
a stem and two diagonal arms. Stroke ends are cut perpendicular to their
own direction, so the arms' terminals would overshoot the stem's flat top
by ~22px and the letter would look broken. Clipping the whole glyph to the
cap-height band cuts every terminal flat on the same two lines, which is
what a real typeface does. Drawing the outline as one filled polygon would
achieve the same thing with about forty hand-computed vertices.

Two outputs, two audiences:

* `src/kodji/apps/web/static/icons/` is what the app serves — the PWA
  icon set the manifest names, the favicons, and the monochrome
  notification badge.
* `brand/` is for marketing and is not served. SVG masters plus large
  PNGs, including light- and dark-surface variants of the bare glyph.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "src" / "kodji" / "apps" / "web" / "static" / "icons"
BRAND = ROOT / "brand"

# --- brand ----------------------------------------------------------------
BG = "#0b0f14"       # style.css --bg
FG = "#ffb454"       # style.css --accent

# --- glyph geometry, on a 512 canvas --------------------------------------
# Cap height band: every terminal is cut flat on these two lines.
CAP_TOP, CAP_BOT = 112, 400
WEIGHT = 58
STEM_X = 153                      # stem centreline
VERTEX_X = 179                    # where the arms meet, just inside the stem
ARM_X = 369                       # arm centreline endpoints
# The strokes run past the cap band so the clip, not the cap, shapes the ends.
OVER_TOP, OVER_BOT = 92, 420
# Glyph bounding box, measured from the geometry above — the tight viewBox
# the bare-glyph exports use.
BOX = (124, CAP_TOP, 264, CAP_BOT - CAP_TOP)


def _glyph(color: str, scale: float = 1.0) -> str:
    """The K itself: two paths, clipped to the cap-height band."""
    at = ""
    if scale != 1.0:
        at = f' transform="translate(256,256) scale({scale}) translate(-256,-256)"'
    return (
        f'<defs><clipPath id="cap">'
        f'<rect x="0" y="{CAP_TOP}" width="512" height="{CAP_BOT - CAP_TOP}"/>'
        f"</clipPath></defs>"
        f'<g{at}><g clip-path="url(#cap)" stroke="{color}" stroke-width="{WEIGHT}"'
        f' fill="none" stroke-linecap="butt" stroke-linejoin="miter">'
        f'<path d="M{STEM_X} {OVER_TOP} V{OVER_BOT}"/>'
        f'<path d="M{ARM_X} {OVER_TOP} L{VERTEX_X} 256 L{ARM_X} {OVER_BOT}"/>'
        f"</g></g>"
    )


def _svg(body: str, view: tuple[int, int, int, int] = (0, 0, 512, 512)) -> str:
    x, y, w, h = view
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {y} {w} {h}" '
        f'width="{w}" height="{h}">\n  {body}\n</svg>\n'
    )


def app_icon() -> str:
    """Rounded square, as it appears on a home screen or a browser tab."""
    return _svg(f'<rect width="512" height="512" rx="96" fill="{BG}"/>\n  {_glyph(FG)}')


def maskable_icon() -> str:
    """Full-bleed square with the glyph inside Android's 80% safe circle.

    A maskable icon is cropped to whatever shape the launcher likes, so the
    background must reach every edge and the artwork must stay well clear
    of them. At 0.85 the glyph's furthest corner sits ~165px from centre,
    against a safe radius of 205.
    """
    return _svg(f'<rect width="512" height="512" fill="{BG}"/>\n  {_glyph(FG, scale=0.85)}')


def bare_glyph(color: str) -> str:
    """Just the K on transparency, cropped tight — for marketing layouts."""
    return _svg(_glyph(color), view=BOX)


def badge() -> str:
    """Android draws the notification badge as a solid silhouette — white
    on transparency, tinted and masked by the system.

    It is rendered at 96px inside a status bar, so the glyph is cropped to
    a square around itself rather than sitting in the icon's full canvas,
    where it would occupy barely half the frame and shrink to nothing.

    That square is sized from the glyph's own diagonal, not from its
    height: launchers mask the badge to a circle, and a frame merely tall
    enough lets the corners of the K fall outside the inscribed circle and
    get sliced off.
    """
    radius = math.hypot(BOX[2] / 2, BOX[3] / 2) * 1.02   # 2% breathing room
    side = round(radius * 2)
    return _svg(
        _glyph("#ffffff"),
        view=(round(256 - radius), round(256 - radius), side, side),
    )


def social_card() -> str:
    """1200x630 OG card. Deliberately text-free: a wordmark would bind the
    export to whichever font is installed on the machine regenerating it.

    The glyph's own centre is (256, 256) in its 512 canvas, so it is moved
    to the card's centre and scaled to about 300px tall.
    """
    scale = 300 / BOX[3]
    inner = (
        f'<rect width="1200" height="630" fill="{BG}"/>'
        f'<g transform="translate(600,315) scale({scale:.4f}) translate(-256,-256)">'
        f"{_glyph(FG)}</g>"
    )
    return _svg(inner, view=(0, 0, 1200, 630))


# ---------------------------------------------------------------------------
# Rasterising
# ---------------------------------------------------------------------------


def _rsvg() -> str:
    exe = shutil.which("rsvg-convert")
    if not exe:
        sys.exit("rsvg-convert not found — `brew install librsvg`")
    return exe


def png(svg_path: Path, out: Path, width: int, height: int | None = None) -> None:
    subprocess.run(
        [_rsvg(), "-w", str(width), "-h", str(height or width), "-o", str(out), str(svg_path)],
        check=True,
    )
    print(f"  {out.relative_to(ROOT)}  {width}x{height or width}")


def ico(out: Path, pngs: list[Path]) -> None:
    """Pack PNGs into a .ico.

    Written here rather than pulled from Pillow: the format is a 6-byte
    header, a 16-byte directory entry per image, then the PNG bytes
    verbatim (PNG-in-ICO is understood by every browser since IE11).
    """
    entries, blobs = [], []
    offset = 6 + 16 * len(pngs)
    for p in pngs:
        data = p.read_bytes()
        # Width/height of 0 means 256 in the ICO directory.
        side = int(p.stem.rsplit("-", 1)[1])
        entries.append(
            struct.pack(
                "<BBBBHHII", side % 256, side % 256, 0, 0, 1, 32, len(data), offset
            )
        )
        blobs.append(data)
        offset += len(data)
    out.write_bytes(
        struct.pack("<HHH", 0, 1, len(pngs)) + b"".join(entries) + b"".join(blobs)
    )
    print(f"  {out.relative_to(ROOT)}  {', '.join(p.stem.rsplit('-', 1)[1] for p in pngs)}")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  {path.relative_to(ROOT)}")
    return path


def main() -> int:
    import tempfile

    ICONS.mkdir(parents=True, exist_ok=True)
    BRAND.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="kodji-logo-"))

    print("\nApp icons + favicons (served):")
    icon_svg = write(ICONS / "icon.svg", app_icon())
    # Sources for rasters that ship only as PNG — no need to keep the SVG.
    maskable_svg = tmp / "maskable.svg"
    maskable_svg.write_text(maskable_icon(), encoding="utf-8")
    badge_svg = tmp / "badge.svg"
    badge_svg.write_text(badge(), encoding="utf-8")

    png(icon_svg, ICONS / "icon-192.png", 192)
    png(icon_svg, ICONS / "icon-512.png", 512)
    png(maskable_svg, ICONS / "icon-maskable-512.png", 512)
    png(icon_svg, ICONS / "apple-touch-icon.png", 180)
    png(badge_svg, ICONS / "badge-96.png", 96)

    favicons = []
    for side in (16, 32, 48):
        out = ICONS / f"favicon-{side}.png"
        png(icon_svg, out, side)
        favicons.append(out)
    ico(ICONS / "favicon.ico", favicons)

    print("\nMarketing (brand/, not served):")
    write(BRAND / "kodji-icon.svg", app_icon())
    glyph = write(BRAND / "kodji-glyph.svg", bare_glyph(FG))
    glyph_dark = write(BRAND / "kodji-glyph-dark.svg", bare_glyph(BG))
    glyph_white = write(BRAND / "kodji-glyph-white.svg", bare_glyph("#ffffff"))
    social = tmp / "social.svg"
    social.write_text(social_card(), encoding="utf-8")

    png(icon_svg, BRAND / "kodji-icon-1024.png", 1024)
    png(icon_svg, BRAND / "kodji-icon-256.png", 256)
    # Bare glyph keeps its 264x288 aspect ratio rather than being squared.
    for src, name in ((glyph, "kodji-glyph"), (glyph_dark, "kodji-glyph-dark"),
                      (glyph_white, "kodji-glyph-white")):
        png(src, BRAND / f"{name}-1024.png", 1024, round(1024 * BOX[3] / BOX[2]))
    png(social, BRAND / "kodji-social-1200x630.png", 1200, 630)

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nicons → {ICONS.relative_to(ROOT)}\nbrand → {BRAND.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
