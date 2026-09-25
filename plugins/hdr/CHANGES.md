# HDR and EXR: changes

## 0.2.0 — 2026-09-25

- A large EXR is decompressed on every core but one: a 4K HDRI in PIZ opens in 8 seconds instead of 26.

## 0.1.0 — 2026-09-25

- OpenEXR, scan-line and tiled, one part or many, in none, RLE, ZIPS, ZIP, PIZ, PXR24, B44 and B44A.
- Radiance .hdr and PFM.
- Exposure and a standard or filmic view, in the settings.
- A multilayer render opens on its beauty pass; About lists every layer and what the renderer wrote.
- Thumbnails in the strip, read thinned; a file too slow to thumbnail is declined at once.
