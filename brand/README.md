# kodji brand assets

Marketing exports. Nothing here is served by the app — the icons the app
actually ships are in `src/kodji/apps/web/static/icons/`.

Everything in both places is generated from one source, `scripts/logo.py`:

```bash
just logo
```

Do not hand-edit these files. Change the geometry or the colours in the
script and regenerate, so the favicon, the Home Screen icon and the press
kit can never drift apart.

## The mark

The letter **K**, drawn as vector paths — no font is involved, so the SVGs
render the same on a machine that has never installed JetBrains Mono.

| | |
| --- | --- |
| Accent (the K) | `#ffb454` |
| Background | `#0b0f14` |

Both are the site's own tokens (`--accent` and `--bg` in `static/style.css`).
If either changes there, change it in `scripts/logo.py` and rerun `just logo`.

## Files

| File | Use |
| --- | --- |
| `kodji-icon.svg` | The app icon — K on the rounded dark square. Master; scales to anything. |
| `kodji-icon-1024.png` | Same, raster. App-store listings, press kits, anywhere a big square icon is wanted. |
| `kodji-icon-256.png` | Same, for slide decks and directory listings. |
| `kodji-glyph.svg` / `-1024.png` | The bare K in accent yellow on transparency. For dark layouts. |
| `kodji-glyph-dark.svg` / `-1024.png` | The bare K in `#0b0f14`. **Use this on white or light backgrounds** — yellow on white does not have enough contrast to read. |
| `kodji-glyph-white.svg` / `-1024.png` | The bare K in white. For photos and coloured panels. |
| `kodji-social-1200x630.png` | Open Graph / Twitter card. The size link unfurlers expect. |

The bare-glyph files are cropped tight to the letter and are 264×288, not
square — place them on your own background rather than assuming padding.

The social card is deliberately text-free. A wordmark would tie the export
to whichever font happened to be installed on the machine that generated
it; if you want the name alongside the mark, set it in your own layout.

## Clear space and minimum size

Leave clear space of at least the width of the K's stem on every side.
The mark reads down to 16px as a favicon, which is what the `.ico` carries
— below that, use a solid block of `#ffb454` rather than a smudged letter.
