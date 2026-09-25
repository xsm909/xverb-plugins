# Copyright (C) 2026 xsm909
#
# This file is part of xverb-plugins.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""The HDR plugin checked against files it did not write.

`fixtures/` holds one picture saved by Blender — so by OpenEXR itself — in
every compression it offers, at half and at float, and `source.json` holds the
values it was made from. Every file this reader claims to read is compared
with those values, sample by sample:

- lossless compressions exactly (a half to within a half's own rounding),
- PXR24 to within its 24 bits,
- B44 is lossy by design, so it is held to the source loosely; that it matches
  OpenEXR's own decoding exactly was measured once, through Blender, and the
  tolerance here only catches a reader that has gone wrong.

A tiled file with mipmaps is built here by hand, because Blender writes none;
the Radiance file and the PFM are checked the same way.

    PYTHONPATH=<xverb>/assets/python python3 selftest.py [more .exr/.hdr files]

Extra files given on the command line are drawn and timed, with nothing to
compare them to.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import exr  # noqa: E402
import main  # noqa: E402
import pfm  # noqa: E402
import rgbe  # noqa: E402
import tone  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
failures = []


def check(what: str, ok: bool, detail: object = "") -> None:
    print("%s  %s%s" % ("ok  " if ok else "FAIL", what, ("  — %s" % (detail,)) if detail else ""))
    if not ok:
        failures.append(what)


def floats(plane: bytes, kind: int):
    code = {exr.HALF: "e", exr.FLOAT: "f", exr.UINT: "I"}[kind]
    return struct.unpack("<%d%s" % (len(plane) // struct.calcsize(code), code), plane)


def worst(values, source, width, channel) -> float:
    most = 0.0
    for y, row in enumerate(source):
        for x, pixel in enumerate(row):
            want = pixel[channel]
            most = max(most, abs(values[y * width + x] - want) / max(1.0, abs(want)))
    return most


def run_fixtures() -> None:
    with open(os.path.join(FIXTURES, "source.json")) as f:
        source = json.load(f)
    pixels = source["pixels"]

    tolerance = {"none": 5e-4, "rle": 5e-4, "zips": 5e-4, "zip": 5e-4, "piz": 5e-4,
                 "pxr24": 5e-4, "b44": 0.08, "b44a": 0.08}
    for name in sorted(os.listdir(FIXTURES)):
        if not name.startswith("rgba_") or not name.endswith(".exr"):
            continue
        depth, codec = name[5:-4].split("_")
        with open(os.path.join(FIXTURES, name), "rb") as f:
            raw = f.read()
        _v, parts, _a = exr.read_header(raw)
        if codec in ("dwaa", "dwab"):
            try:
                exr.read(raw, parts[0], ["R", "G", "B"])
                check("%s is refused, not misread" % name, False)
            except exr.ExrError:
                check("%s is refused, not misread" % name, True)
            continue
        picture = exr.read(raw, parts[0], ["R", "G", "B", "A"])
        off = max(worst(floats(picture.planes[c], picture.types[c]), pixels, picture.width, i)
                  for i, c in enumerate("RGBA"))
        # A float holds the source to its own rounding: one part in 2^24.
        limit = 2e-7 if depth == "32" else tolerance[codec]
        if depth == "32" and codec == "pxr24":
            limit = 1e-4
        check("%s matches what it was made from" % name, off <= limit, "worst %.2g" % off)

    # Radiance: eight bits of mantissa under an exponent the three share, so a
    # channel is exact to 1/256 of the brightest of them — here up to 4.
    with open(os.path.join(FIXTURES, "rgb.hdr"), "rb") as f:
        raw = f.read()
    width, height, planes, _header = rgbe.read(raw)
    table = tone.values(tone.RGBE)
    off = 0.0
    for i, c in enumerate("RGB"):
        keys = tone.keys(planes[c], tone.RGBE)
        off = max(off, worst([table[k] for k in keys], pixels, width, i))
    check("rgb.hdr matches what it was made from", off < 4.0 / 256 + 1e-6, "worst %.2g" % off)

    for name in ("multipart.exr", "multilayer.exr"):
        with open(os.path.join(FIXTURES, name), "rb") as f:
            raw = f.read()
        _v, parts, _a = exr.read_header(raw)
        index, layer, names, data = main.choose(parts)
        check("%s draws the combined pass" % name,
              layer == "ViewLayer.Combined" and not data, (layer, names))
        content = main.answer("exr", raw)
        check("%s opens as a picture" % name, content.get("kind") == "image", content.get("detail"))
        facts = main.describe_exr(name, len(raw), raw)
        titles = [g["title"] for g in facts["groups"]]
        layers = [f["label"] for g in facts["groups"] if g["title"] == "Layers" for f in g["facts"]]
        check("%s lists both layers" % name,
              any("Combined" in l for l in layers) and any("Depth" in l for l in layers), layers)
        check("%s says what made it" % name, "Made with" in titles, titles)


def _tiled_exr(width: int, height: int, tile: int) -> bytes:
    """A tiled, mipmapped, uncompressed EXR with R, G, B in half — value
    x + 100 y in R, so a misplaced tile is a wrong number."""

    def attr(name, kind, body):
        return name.encode() + b"\0" + kind.encode() + b"\0" + struct.pack("<i", len(body)) + body

    channels = b"".join(c.encode() + b"\0" + struct.pack("<iB3xii", exr.HALF, 0, 1, 1) for c in "BGR") + b"\0"
    header = (
        attr("channels", "chlist", channels)
        + attr("compression", "compression", bytes([exr.NONE]))
        + attr("dataWindow", "box2i", struct.pack("<4i", 0, 0, width - 1, height - 1))
        + attr("displayWindow", "box2i", struct.pack("<4i", 0, 0, width - 1, height - 1))
        + attr("lineOrder", "lineOrder", b"\0")
        + attr("pixelAspectRatio", "float", struct.pack("<f", 1.0))
        + attr("screenWindowCenter", "v2f", struct.pack("<2f", 0, 0))
        + attr("screenWindowWidth", "float", struct.pack("<f", 1.0))
        + attr("tiles", "tiledesc", struct.pack("<IIB", tile, tile, 1))
        + b"\0"
    )
    count = exr._tile_count(width, height, tile, tile, 1)
    head = exr.MAGIC + struct.pack("<I", 2 | 0x200) + header
    table_at = len(head)
    body = bytearray()
    offsets = []

    def value(x, y, c):
        return {"R": x + 100 * y, "G": 0.5, "B": -1.0}[c]

    level, w, h = 0, width, height
    while len(offsets) < count:
        for ty in range((h + tile - 1) // tile):
            for tx in range((w + tile - 1) // tile):
                nx, ny = min(tile, w - tx * tile), min(tile, h - ty * tile)
                data = bytearray()
                for y in range(ny):
                    for c in "BGR":
                        row = [value(tx * tile + x, ty * tile + y, c) if level == 0 else 7.0
                               for x in range(nx)]
                        data += struct.pack("<%de" % nx, *row)
                offsets.append(table_at + 8 * count + len(body))
                body += struct.pack("<5i", tx, ty, level, level, len(data)) + data
        level += 1
        w, h = max(1, w // 2), max(1, h // 2)
    return bytes(head + struct.pack("<%dQ" % count, *offsets) + body)


def run_tiled() -> None:
    raw = _tiled_exr(21, 13, 8)
    _v, parts, _a = exr.read_header(raw)
    part = parts[0]
    check("a tiled file is known for one", part.tiled and part.tiles[:2] == (8, 8))
    picture = exr.read(raw, part, ["R", "G", "B"])
    red = floats(picture.planes["R"], exr.HALF)
    ok = all(red[y * 21 + x] == x + 100 * y for y in range(13) for x in range(21))
    check("every tile of level 0 lands where it belongs, and no mipmap is read", ok)

    thin = exr.read(raw, part, ["R"], step=4)
    red = floats(thin.planes["R"], exr.HALF)
    ok = thin.width == 6 and thin.height == 4 and all(
        red[y * 6 + x] == 4 * x + 400 * y for y in range(4) for x in range(6))
    check("a thumbnail takes every fourth row and column, across tiles", ok, (thin.width, thin.height))


def run_pfm() -> None:
    w, h = 5, 3
    rows = [[(x + 10 * y, 0.25, 2.0) for x in range(w)] for y in range(h)]
    body = b"".join(struct.pack("<%df" % (w * 3), *[v for px in rows[y] for v in px])
                    for y in reversed(range(h)))
    raw = b"PF\n5 3\n-1.0\n" + body
    width, height, planes = pfm.read(raw)
    red = struct.unpack("<%df" % (w * h), planes["R"])
    check("a PFM is read top row first", red[:5] == (0, 1, 2, 3, 4) and red[10] == 20, red[:6])
    content = main.answer("pfm", raw)
    check("and opens as a picture", content.get("kind") == "image")


def run_system() -> None:
    """On a Mac, the system's decoder must give the samples this reader gives."""
    import macos
    if sys.platform != "darwin":
        print("      (not a Mac: the system decoder is not checked)")
        return
    for name in ("rgba_16_zip.exr", "rgba_32_piz.exr", "rgba_16_pxr24.exr", "rgba_16_b44.exr"):
        with open(os.path.join(FIXTURES, name), "rb") as f:
            raw = f.read()
        found = macos.decode(raw)
        _v, parts, _a = exr.read_header(raw)
        mine = exr.read(raw, parts[0], ["R", "G", "B"])
        same = found is not None and all(found[3][c] == mine.planes[c] for c in "RGB")
        check("the system decodes %s to the same samples" % name, same)
    with open(os.path.join(FIXTURES, "rgba_16_dwaa.exr"), "rb") as f:
        check("the system refuses DWAA, as measured", macos.decode(f.read()) is None)


def run_library() -> None:
    """The compiled decoder, where there is one for this machine (or one named
    by XVERB_HDR_NATIVE), must give the samples this reader gives."""
    import native
    if not native.available():
        print("      (no compiled decoder for %s: not checked)" % native.target())
        return
    names = sorted(n for n in os.listdir(FIXTURES) if n.endswith(".exr"))
    for name in names:
        with open(os.path.join(FIXTURES, name), "rb") as f:
            raw = f.read()
        _v, parts, _a = exr.read_header(raw)
        index, _layer, wanted, _data = main.choose(parts)
        part = parts[index]
        got = native.decode(raw, index, wanted, part.width, part.height)
        if part.compression not in exr.READABLE:
            check("the library refuses %s" % name, got is None, native.last_error)
            continue
        mine = exr.read(raw, part, wanted)
        same = got is not None and all(
            floats(got[c], exr.FLOAT) == tuple(float(v) for v in floats(mine.planes[c], mine.types[c]))
            for c in wanted)
        check("the library decodes %s to the same samples" % name, same, native.last_error)
    raw = _tiled_exr(21, 13, 8)
    _v, parts, _a = exr.read_header(raw)
    got = native.decode(raw, 0, ["R"], 21, 13)
    ok = got is not None and floats(got["R"], exr.FLOAT) == tuple(
        float(x + 100 * y) for y in range(13) for x in range(21))
    check("the library puts a mipmapped tiled file together from level 0", ok, native.last_error)


def run_tone() -> None:
    lut = tone.table(tone.HALF, 0.0, "standard")
    key = lambda v: struct.unpack("<H", struct.pack("<e", v))[0]  # noqa: E731
    check("standard: 0 is black, 1 is white, 18% grey is sRGB 118",
          lut[key(0.0)] == 0 and lut[key(1.0)] == 255 and lut[key(0.18)] == 118,
          (lut[key(0.0)], lut[key(1.0)], lut[key(0.18)]))
    check("standard: forty times white is white", lut[key(40.0)] == 255)
    check("a NaN is black, negative light is black, infinity is white",
          lut[key(float("nan"))] == 0 and lut[key(-2.0)] == 0 and lut[key(float("inf"))] == 255)
    film = tone.table(tone.HALF, 0.0, "filmic")
    check("filmic keeps a gradient above white",
          film[key(1.0)] < film[key(4.0)] < film[key(16.0)] <= 255,
          (film[key(1.0)], film[key(4.0)], film[key(16.0)]))
    brighter = tone.table(tone.HALF, 1.0, "standard")
    check("+1 EV doubles the light", brighter[key(0.25)] == lut[key(0.5)])


def run_extra(paths) -> None:
    for path in paths:
        with open(path, "rb") as f:
            raw = f.read()
        extension = path.rsplit(".", 1)[-1].lower()
        started = time.time()
        content = main.answer(extension, raw)
        print("      %s: %s in %.2fs — %s" % (os.path.basename(path), content.get("kind"),
                                              time.time() - started,
                                              content.get("detail") or content.get("message")))


if __name__ == "__main__":
    run_fixtures()
    run_tiled()
    run_pfm()
    run_tone()
    run_system()
    run_library()
    run_extra(sys.argv[1:])
    print("\n%s" % ("all passed" if not failures else "%d failed: %s" % (len(failures), failures)))
    sys.exit(1 if failures else 0)
