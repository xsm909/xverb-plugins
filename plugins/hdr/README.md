# HDR and EXR

Pictures that hold light rather than colour: the render out of Blender, the
HDRI a scene is lit with, the texture baked for an engine.

| format | what is read |
| --- | --- |
| OpenEXR `.exr` | scan-line and tiled, one part or many; half, float and uint; none, RLE, ZIPS, ZIP, PIZ, PXR24, B44, B44A |
| Radiance `.hdr` | RGBE and XYZE, run-length or flat, upright or bottom-up |
| PFM `.pfm` | colour and grey, either byte order |

**Which picture.** A multilayer render opens on the layer with no name, or
else its beauty pass — `Combined`, `beauty`, `rgba` — or else the first layer
with red, green and blue. Luminance alone is drawn grey. A file of nothing but
data, a depth pass, is stretched from its smallest value to its largest and the
page says the range. **About** (Ctrl+Shift+O) lists every layer, the
compression, tiles, and what the renderer wrote about itself — Blender's
camera, frame and render time among them.

**How light becomes a picture** is in the settings: an exposure, in stops, and
a view. *Standard* clips at white, the way Blender, Nuke and Photoshop show a
render by default. *Filmic* rolls the highlights off — the ACES fit Krzysztof
Narkowicz published — so a sky forty times brighter than paper keeps its
gradient. Alpha is not applied: the picture is the light, as a compositor's
viewer shows it.

## What is not read

**DWAA and DWAB** — a JPEG-like transform with Huffman and zip stages of its
own. macOS's ImageIO refuses them too, measured on the fixtures. The small
preview some EXR writers put in the header is shown if there is one, and
otherwise a sentence says which compression it was.
Deep images, which have many samples per pixel and no single picture, are
refused the same way. A subsampled channel (the luminance-chroma layout) is
refused rather than drawn wrongly.

## How fast

Everything is the standard library, so everything per sample is where the time
goes, and the reader is written so that nearly all of it runs in C — slices,
`zlib`, `accumulate`, and one 65 536-entry table per curve. An EXR's chunks
know nothing of each other, so a file worth more than a second of work is
decompressed on every core but one (at most eight), in worker processes kept
for the next file. Measured on a Mac mini, 4 performance and 4 efficiency
cores:

| file | one core | all of them |
| --- | --- | --- |
| Radiance `.hdr`, 1920 × 1080 | 0.3 s | — |
| ZIP, half RGBA, 1920 × 1080 | 0.8 s | — |
| PIZ, float RGBA, 1920 × 1080, every pixel noise | 10 s | 3.1 s |
| a Poly Haven HDRI, 4096 × 2048 float RGB in PIZ, 75 MB | 26 s | 7.8 s, 8.6 s through the host |

**On a Mac the system decodes it.** ImageIO is the OpenEXR library in C++,
and `ctypes` reaches it with nothing shipped: that HDRI opens in 1.6 seconds
through the host, 0.3 of them decoding, and its thumbnail takes half a second.
A file on the disk is read from it directly, not in base64 through the host. The samples are
taken as the file stores them, before any drawing, and the picture is byte
for byte the one this reader makes — the self-test holds that. Exposure, the
view and the choice of layer stay the plugin's. A file of several parts or a
multilayer render, where ImageIO would pick the layer, and anything it
refuses are read by the plugin as everywhere else.

PIZ is the slow one because its Huffman codes and its wavelet have to be
worked one value at a time; seven workers came to 3.9 times one here, which
is what four fast cores and four slow ones give.

**Thumbnails have eight seconds**, and a PIZ chunk is 32 whole rows, so a
thumbnail of that HDRI costs as much as the picture. The cost is estimated
from the header first — within a few percent on the files above, and high
rather than low — and a file over five seconds, counting half the workers,
gets no thumbnail at once rather than none after eight. On macOS the system
reads EXR thumbnails itself and this is never asked.

## The compiled decoder, on Windows and Linux

Those machines have no EXR decoder of their own, so the plugin carries one:
[tinyexr](https://github.com/syoyo/tinyexr) (BSD 3-clause, with miniz, MIT),
compiled for `windows-x64`, `linux-x64` and `linux-arm64` under `native/`,
about 0.7 MB each, loaded with `ctypes`. Its sources are in `native/src`
beside the one-function wrapper `xvexr.cc`, and `native/build.sh` rebuilds all
three from a Mac with zig (`brew install zig`). Linux needs glibc 2.17 or
later; the Windows library needs nothing but the system's C runtime.

The plugin still reads the header and chooses the part and the channels; the
library decodes that part on its threads and hands back float planes, and
exposure and the view come after, as on every path. It reads every
compression this reader does, multi-part and tiled files included; DWAA and
DWAB it refuses too. No library for the machine, one that will not load, or a
file it refuses: the plugin reads the file itself, on every core.

Measured with the same library built for this Mac, the 4K HDRI opens in 1.5
seconds and a 1080p frame of float noise decodes in 0.1. The self-test holds
it to this reader sample for sample on every fixture and on a mipmapped tiled
file; `XVERB_HDR_NATIVE` names a library to check instead of the shipped one.

## Checking it

```
PYTHONPATH=<xverb>/assets/python python3 selftest.py [more files to time]
```

`fixtures/` is one picture saved by Blender — by OpenEXR itself — in every
compression it offers, at half and at float, beside `source.json`, the values
it was made from; `fixtures/make.py` writes them again. Every file is compared
with those values sample by sample. A tiled file with mipmaps is built by the
test itself, because Blender writes none.
