# MatteLoop icon masters

The original masters are design references. Corrected siblings ending in
`-alpha-green` are the publication assets: they change only the ribbon colour
to the DESIGN.md accent `#B7F34A`. The graphite tile, neutral checker segment,
transparency, and alpha edges are otherwise preserved.

## License

These visual assets are available under the repository's Zero-Clause BSD
license, to the extent copyright or related rights exist. MatteLoop does not
reserve separate project-controlled trademark restrictions for the name or
logo; they may be reused for any purpose without fee or attribution.

## Publication status

The product rename is complete. The committed derived assets in `derived/` are
used for native packaging:

- `derived/matteloop.icns` — macOS application icon.
- `derived/matteloop.ico` — Windows application icon.

`scripts/build.py` verifies their file signatures and selects the matching icon
for the target platform in its temporary packaging spec. They are committed
derived assets rather than generated during every build. The UI mark is not
wired into the application window.

The private sample name that appeared in one test fixture and in one commit
message has been removed: the fixture now uses the neutral name `clip.webp`,
and the commit messages were rewritten before the repository had any remote.
Broad substring searches over the working tree and over every reachable commit
find no remaining occurrence. No matching committed filenames, author names, or
author email addresses exist.

## Files

- `matteloop-app-icon-1024.png` — 1024 x 1024 RGBA program-icon master. The
  generated mark is composited on a deterministic graphite squircle.
- `matteloop-app-icon-1024-alpha-green.png` — corrected publication master
  used to derive the native application icons.
- `matteloop-ui-mark-1024.png` — 1024 x 1024 RGBA transparent brand mark for
  use inside the UI.
- `matteloop-ui-mark-1024-alpha-green.png` — corrected publication master,
  retained for a future agreed in-app placement.
- `source/grok-refined-raw.jpg` — unchanged 1408 x 1408 Grok refinement.
- `source/muapi-bg-removed-raw.png` — unchanged 1408 x 1408 MuAPI
  background-removal output.

## Art direction

The alpha-green continuous ribbon combines an infinity loop with successive
video frames. Its embedded gray checker segment represents transparency. The
mark uses one dominant silhouette, no typography, and enough negative space to
remain identifiable at 32 px.

## MuAPI provenance

Generated on 2026-08-31 through MuAPI with Grok only.

1. Text-to-image master candidates
   - Model: `grok-quality` (`grok-imagine-text-to-image-quality`)
   - Request: `ded0b461-bf89-470f-965e-2939399e5898`
   - Cost: USD 0.05
   - Prompt: `Production-ready desktop application icon for a technical media
     tool named MatteLoop. One dominant bold symbol: a continuous alpha-green
     ribbon forming an elegant infinite loop that also suggests two successive
     video frames. The center contains a crisp abstract foreground cutout made
     from negative space, while one small controlled segment transitions into
     a sparse transparency checker pattern, communicating video matting and
     transparent animated WebP export. Precision workshop visual language,
     restrained geometric construction, strong simple silhouette, flat
     vector-like shapes with subtle dimensional lighting, deep graphite
     rounded-square app tile, alpha green as the only accent color, high
     contrast, centered orthographic front view, balanced symmetry with slight
     forward motion, generous 15 percent safe padding, readable at 32 pixels,
     suitable for macOS and Windows program icon and small in-app brand mark.
     Isolate the rounded-square tile on a plain pure white background for later
     background removal. No letters, no words, no typography, no play button,
     no wand, no sparkle, no robot, no brain, no camera, no scenery, no extra
     props, no border, no watermark, no signature, no mockup, no duplicate
     icon.`
2. Controlled image-to-image refinement
   - Model: `grok` (`grok-imagine-image-to-image`)
   - Request: `e3d9decb-78cf-49e9-87e4-51020b04d6c2`
   - Cost: USD 0.05
   - Prompt: `Refine this exact app icon while preserving its identity, deep
     graphite rounded-square tile, alpha-green palette, centered infinity-loop
     composition, camera, proportions, and clean technical style. Simplify the
     green infinity ribbon into one coherent continuous band with smooth
     intentional crossings. Remove the two awkward sharp internal arrow-like
     protrusions and any broken geometry. Make the transparency transition on
     the right a slightly larger, clean 4-by-3 checker section embedded flush
     into the ribbon so it remains legible at 32 pixels, using neutral light
     gray and dark gray checks, not a hole. Keep the mark bold and uncluttered
     with generous safe padding. Preserve the pure white outer background for
     later removal. No play triangle, no arrows, no letters, no words, no
     typography, no sparkle, no extra symbols, no border, no watermark, no
     mockup.`
3. Background removal
   - Model: MuAPI background removal
   - Request: `994af341-e5c2-443e-91a8-3cba65fb445f`
   - Cost: USD 0.01

Total billed generation cost: USD 0.11.

## Deterministic finishing

- The transparent mark was trimmed, scaled to fit within an 800 px safe area,
  and centered on a transparent 1024 px square.
- The app tile is a vertical `#2d2e35` to `#191a1f` graphite gradient clipped
  to a rounded rectangle spanning `(48, 48)` through `(976, 976)` with a 210 px
  corner radius. The transparent mark is centered over it unchanged.
- Both final PNGs were verified as 1024 x 1024 8-bit sRGBA with alpha extrema
  0–1 and fully transparent corner pixels.

## SHA-256

```text
2cf1639adb443991170830f5d627739e2294d89cf27b7b2140812936b0339ddd  matteloop-app-icon-1024-alpha-green.png
9eb41b58b3e74f9adcead2a573400705e5e8012b455e57e0c6efca8fd2618995  matteloop-app-icon-1024.png
6e05eda8959dea4b95dbdf564b2943b2133cacc94288d6b556ddc96e2f7642db  matteloop-ui-mark-1024-alpha-green.png
bb80a36af039434d0c468d0b3dcd789c232e50c5da2a7809e2d5672138754bfa  matteloop-ui-mark-1024.png
8b352ce904770b06e4fd15586e855452d007e99b21fc9b46c84de73363366b27  derived/matteloop.icns
a91404d20228966f6ce3d972ec6d7c7e27431efc1ac6e49467b7aae58f3de7f2  derived/matteloop.ico
75337ca45f2ffcc3c5f6efa78f71924198470a6b09e6a1bfaeea283d4fe3d159  source/grok-refined-raw.jpg
9e0c2f5f3f61ea436c6553f61775770f9180d04eb7a360bdb4d4edb68bd55961  source/muapi-bg-removed-raw.png
```
