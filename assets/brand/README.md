# Onoats brand assets

The approved **grain wave** direction uses three oat grains as a compact speech
waveform. The colour mark is the canonical brand expression; the menu-bar
glyph keeps only the grain-wave silhouette so it stays clear in an 18-point
monochrome slot. Do not shrink the colour mark into the menu bar.

## Palette

| Role | Value |
| --- | --- |
| Midnight | `#17233A` |
| Oat gold | `#E9B44C` |
| Oat cream | `#FFF8E8` |

Use the full palette on light backgrounds. On dark backgrounds, the wordmark
lettering may change from Midnight to Oat cream; keep the mark colours intact.
The menu-bar assets are black template images with transparency, so macOS owns
their final colour and contrast.

## Geometry and spacing

- `onoats-mark.svg` is the canonical standalone mark on a 256-unit square.
- `onoats-wordmark.svg` is the canonical horizontal lock-up. Its lowercase
  word is set in Avenir Next Rounded Demi Bold, with Avenir Next and Helvetica
  Neue as source-preview fallbacks.
- Keep clear space of at least one grain width (one eighth of the mark) around
  the mark and wordmark.
- Do not redraw, rotate, outline, recolour individual grains, or add copy inside
  the clear-space area.
- Below 32 points, use the monochrome grain-wave silhouette. At 16, 18, and
  22 points it must be inspected at 100% scale, not judged from an enlarged
  preview.

## Native exports

Exports are committed because the native app is built directly with `swiftc`
and copies an explicit resource allowlist into the signed bundle.

| Export | Pixel size | Source and treatment |
| --- | ---: | --- |
| `OnoatsHeader.png` | 190 x 64 | `onoats-wordmark.svg`, transparent |
| `OnoatsHeader@2x.png` | 380 x 128 | `onoats-wordmark.svg`, transparent |
| `OnoatsMenuBarTemplate<State>.png` | 18 x 18 | grain-wave silhouette plus state cue, black alpha template |
| `OnoatsMenuBarTemplate<State>@2x.png` | 36 x 36 | same state artwork at 2x |
| `Onoats.icns` | 16 through 1024 | `onoats-mark.svg`, square app-icon export |

`State` is `Idle`, `Recording`, `Warning`, `Busy`, or `Failed`. Idle is the
plain grain wave. Recording adds a solid status dot, Warning a triangular cue,
Busy a small open ring, and Failed a cross. State cues occupy the same lower
right badge area, leaving the shared grain-wave silhouette recognisable.

The full mark was reviewed at header scale and the template silhouette at 16,
18, and 22 points. Website work should consume the SVG sources directly and
make its own responsive raster exports rather than upscaling the native PNGs.
