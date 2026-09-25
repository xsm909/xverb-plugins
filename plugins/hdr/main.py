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

"""Pictures that hold light rather than colour: OpenEXR, Radiance and PFM.

**Why they need a reader of their own.** A render, an HDRI to light a scene
with, a texture baked for a game engine — each is saved as light, in floats,
and a sky can be forty times brighter than white paper. No machine's picture
decoder reads EXR on Windows or Linux, and none of them decides what forty
times white should look like. This plugin does both: it reads the file (see
`exr.py`, `rgbe.py`, `pfm.py`), and turns light into a picture the way a
compositor's viewer does (see `tone.py`), at the exposure and with the view
the settings name.

**The shape is the Pictures plugin's.** The picture goes to the host as an
ordinary PNG, so F3, the Ctrl+Q panel and the film strip all show it with fit,
1:1 and zoom, and the strip walks `.exr` beside `.jpg` because both viewers say
they produce a `picture`.

**What the file is, apart from the picture,** is the describer's: the layers
of a multilayer render, the compression, what the renderer wrote about itself.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xverb import Plugin, error, fact, fact_group, facts, image  # noqa: E402

import exr  # noqa: E402
import pfm  # noqa: E402
import png  # noqa: E402
import pool  # noqa: E402
import rgbe  # noqa: E402
import tone  # noqa: E402

plugin = Plugin("org.xverb.hdr", "HDR and EXR")

#: A multilayer render keeps every pass at full size in floats; this is
#: generous and still finite.
MAX_BYTES = 768 << 20

#: How much is worth sending on the chance the machine's decoder reads it.
MAX_HANDOVER = 64 << 20

#: The head of a file, for the describer: an EXR header with a hundred
#: attributes is a few kilobytes, a Radiance header a line or ten.
HEAD_BYTES = 1 << 20

EXTENSIONS = ["exr", "hdr", "pfm"]

#: Seconds of expected work past which an EXR is decompressed on every core.
PARALLEL_FROM = 1.0


def _exposure() -> float:
    try:
        return float(plugin.setting("exposure", "0"))
    except (TypeError, ValueError):
        return 0.0


def _view() -> str:
    return "filmic" if plugin.setting("view", "standard") == "filmic" else "standard"


# -- which channels are the picture ----------------------------------------


def layers(parts: List[exr.Part]) -> List[Tuple[int, str, Dict[str, exr.Channel]]]:
    """Every layer in the file: (part, layer name, its channels by component).

    A layer is what comes before a channel's last dot — `ViewLayer.Combined`
    for `ViewLayer.Combined.R` — and the channels with no dot are the layer
    with no name, which is where an ordinary picture keeps R, G and B.
    """
    found = []
    for index, part in enumerate(parts):
        groups: Dict[str, Dict[str, exr.Channel]] = {}
        order: List[str] = []
        for channel in part.channels:
            layer, _, component = channel.name.rpartition(".")
            if layer not in groups:
                groups[layer] = {}
                order.append(layer)
            groups[layer][component] = channel
        found.extend((index, layer, groups[layer]) for layer in order)
    return found


def _rank(layer: str) -> int:
    lowered = layer.lower()
    if not layer:
        return 0
    for rank, word in enumerate(("combined", "beauty", "rgba", "image", "composite"), 1):
        if lowered.endswith(word) or ("." + word) in lowered:
            return rank
    return 10


def choose(parts: List[exr.Part]):
    """(part index, layer, channel names, is data): the picture a person
    opening the file expects — the unnamed layer, then a beauty pass, then
    any colour; luminance alone as grey; failing that, the first channel as
    data, stretched."""
    every = layers(parts)
    colour = [(i, n, c) for i, n, c in every if all(k in c for k in "RGB")]
    if colour:
        colour.sort(key=lambda row: (_rank(row[1]), row[0]))
        i, name, comps = colour[0]
        return i, name, [comps[k].name for k in "RGB"], False
    for i, name, comps in every:
        if "Y" in comps and len(comps) <= 4:
            return i, name, [comps["Y"].name], False
    for i, name, comps in every:
        if comps:
            first = next(iter(comps.values()))
            return i, name, [first.name], True
    raise exr.ExrError("The file has no channels")


# -- a picture from a file -------------------------------------------------


class Drawn:
    def __init__(self, width: int, height: int, channels: int, pixels: bytes, notes: List[str]):
        self.width, self.height, self.channels = width, height, channels
        self.pixels, self.notes = pixels, notes


def draw(extension: str, raw: bytes, step: int = 1) -> Drawn:
    exposure, view = _exposure(), _view()
    light = "%+g EV, %s" % (exposure, view)
    if extension == "exr":
        _version, parts, _at = exr.read_header(raw)
        index, layer, names, data = choose(parts)
        part = parts[index]
        # The pool only when it pays: starting it is a fraction of a second,
        # and a small ZIP file is done before the workers would be.
        spread = pool.decode if exr.cost(part, step) > PARALLEL_FROM else None
        picture = exr.read(raw, part, names, step, spread)
        channels, pixels, stretched = tone.render(
            picture.planes, picture.types, names, exposure, view, data)
        notes = []
        count = len(layers(parts))
        if count > 1:
            notes.append(plugin.tr("Layer {layer}, one of {count}; About lists them all.",
                                   {"layer": layer or "(unnamed)", "count": count}))
        if data:
            low, high = stretched or (0.0, 0.0)
            notes.append("%s is data, not light: drawn from %.4g as black to %.4g as white."
                         % (names[0], low, high))
        else:
            notes.append(plugin.tr("Shown at {light}.", {"light": light}))
        if picture.missing_chunks:
            notes.append("%d block(s) are missing or damaged and are black — an "
                         "unfinished render looks like this." % picture.missing_chunks)
        if part.display != (part.xmin, part.ymin, part.xmax, part.ymax):
            notes.append("The pixels cover %d × %d of a %d × %d frame; only they are drawn."
                         % (part.width, part.height,
                            part.display[2] - part.display[0] + 1,
                            part.display[3] - part.display[1] + 1))
        return Drawn(picture.width, picture.height, channels, pixels, notes)

    if extension == "hdr":
        width, height, planes, _header = rgbe.read(raw, step)
        types = {n: tone.RGBE for n in planes}
        channels, pixels, _ = tone.render(planes, types, ["R", "G", "B"], exposure, view)
        return Drawn(width, height, channels, pixels,
                     [plugin.tr("Shown at {light}.", {"light": light})])

    if extension == "pfm":
        width, height, planes = pfm.read(raw, step)
        names = ["R", "G", "B"] if "R" in planes else ["Y"]
        types = {n: tone.FLOAT for n in planes}
        channels, pixels, _ = tone.render(planes, types, names, exposure, view)
        return Drawn(width, height, channels, pixels,
                     [plugin.tr("Shown at {light}.", {"light": light})])

    raise exr.ExrError("This viewer does not read a .%s" % extension)


FAILURES = (exr.ExrError, rgbe.RgbeError, pfm.PfmError)


def answer(extension: str, raw: bytes) -> dict:
    """The content for one file, given its bytes — separate from the viewer
    so the self-test can call it with no host at the other end."""
    if not raw:
        return error(plugin.tr("The file is empty."))
    try:
        drawn = draw(extension, raw)
    except FAILURES as failure:
        return _instead(extension, raw, str(failure))
    except Exception as failure:  # noqa: BLE001
        return error("This file could not be read: %s" % failure)

    pixels = drawn.width * drawn.height
    body = png.write(drawn.width, drawn.height, drawn.channels, drawn.pixels,
                     level=1 if pixels > 4_000_000 else 6)
    content = image(body)
    content["detail"] = " ".join(drawn.notes) or None
    return content


def _instead(extension: str, raw: bytes, reason: str) -> dict:
    """Something other than nothing, where there is something.

    **The machine first, on the machine that can.** macOS reads EXR through
    ImageIO — DWAA and DWAB included, which this reader does not — so the file
    is handed over there rather than refused. **Then the file's own preview**,
    a small eight-bit copy some writers put in the header. And only then the
    sentence saying why.
    """
    if extension == "exr" and sys.platform == "darwin" and len(raw) <= MAX_HANDOVER:
        content = image(raw, mime_type="image/x-exr")
        content["detail"] = "%s — shown by this machine's own decoder instead." % reason
        return content
    if extension == "exr":
        try:
            _v, parts, _a = exr.read_header(raw, offsets=False)
            preview = parts[0].get("preview")
        except exr.ExrError:
            preview = None
        if preview and preview[0] and preview[1]:
            w, h, rgba = preview
            content = image(png.write(w, h, 4, rgba))
            content["detail"] = ("%s — this is the %d × %d preview the file carries."
                                 % (reason, w, h))
            return content
    return error(reason)


#: The host waits eight seconds for a thumbnail. A file that would take longer
#: is answered at once with nothing — the strip then shows its name — rather
#: than after eight seconds with nothing.
THUMBNAIL_BUDGET = 5.0


def small_copy(url: str, pixels: int) -> Optional[bytes]:
    """A thumbnail, read thinned: every n-th row and column, and for an EXR
    only the chunks holding a row that is kept.

    **Not for every file.** A 4K float panorama in PIZ is 50 million values
    through a Huffman decoder written in Python, 25 seconds whatever size the
    thumbnail is, because each chunk is 32 whole rows. Its cost is estimated
    from the header first and it is declined when it cannot make the budget.
    """
    extension = url.rsplit(".", 1)[-1].lower()
    if extension not in EXTENSIONS:
        return None
    try:
        raw = plugin.read_file(url, max_bytes=MAX_BYTES)
        size = _size_of(extension, raw)
        if size is None:
            return None
        step = max(1, (max(size) + pixels - 1) // pixels)
        if extension == "exr":
            _v, parts, _a = exr.read_header(raw)
            index, _layer, _names, _data = choose(parts)
            expected = exr.cost(parts[index], step)
            if expected > PARALLEL_FROM:
                # Half the workers, not all: on a machine with efficiency
                # cores seven workers were measured at 3.9 times one.
                expected /= max(1.0, pool.workers() / 2)
            if expected > THUMBNAIL_BUDGET:
                plugin.log("No thumbnail for %s: about %.0fs of %s"
                           % (url.rsplit("/", 1)[-1], expected,
                              exr.COMPRESSION_NAMES.get(parts[index].compression)))
                return None
        drawn = draw(extension, raw, step)
    except Exception:  # noqa: BLE001 - no thumbnail is not an error
        return None
    return png.write(drawn.width, drawn.height, drawn.channels, drawn.pixels, level=1)


def _size_of(extension: str, raw: bytes) -> Optional[Tuple[int, int]]:
    if extension == "exr":
        _v, parts, _a = exr.read_header(raw, offsets=False)
        index, _layer, _names, _data = choose(parts)
        return parts[index].width, parts[index].height
    if extension == "hdr":
        _vars, _other, width, height, _up, _at = rgbe.read_header(raw)
        return width, height
    if extension == "pfm":
        _c, width, height, _little, _at = pfm.read_header(raw)
        return width, height
    return None


@plugin.viewer(
    "hdr.picture",
    "HDR picture",
    extensions=EXTENSIONS,
    priority=20,
    produces="picture",
    thumbnail=small_copy,
)
def picture(url: str) -> dict:
    started = time.time()
    extension = url.rsplit(".", 1)[-1].lower()
    try:
        raw = plugin.read_file(url, max_bytes=MAX_BYTES)
    except Exception as failure:  # noqa: BLE001
        return error("The file could not be read: %s" % failure)
    content = answer(extension, raw)
    plugin.log("%s %s, %.2fs" % (extension, content.get("kind"), time.time() - started))
    return content


# -- what the file says about itself ---------------------------------------

#: Attributes every EXR has, which the groups below say in words.
STANDARD = {
    "channels", "compression", "dataWindow", "displayWindow", "lineOrder",
    "pixelAspectRatio", "screenWindowCenter", "screenWindowWidth", "tiles",
    "type", "name", "chunkCount", "version", "preview",
}


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "bytes" else ("%.1f %s" % (n, unit))
        n /= 1024
    return str(n)


def _shown(kind: str, value) -> str:
    if value is None:
        return ""
    if kind == "preview":
        return "%d × %d" % (value[0], value[1])
    if kind == "chromaticities":
        return ("red %.4g, %.4g · green %.4g, %.4g · blue %.4g, %.4g · white %.4g, %.4g"
                % tuple(value))
    if kind in ("m33f", "m44f", "m33d", "m44d"):
        size = 3 if kind.startswith("m33") else 4
        return " / ".join(" ".join("%.4g" % v for v in value[r * size:(r + 1) * size])
                          for r in range(size))
    if kind == "rational":
        return "%d/%d" % value
    if isinstance(value, float):
        return "%.6g" % value
    if isinstance(value, (tuple, list)):
        return ", ".join(_shown("", v) if not isinstance(v, str) else v for v in value)
    return str(value)


def describe_exr(name: str, whole, head: bytes) -> dict:
    version, parts, _at = exr.read_header(head, offsets=False)
    first = parts[0]
    kind = "OpenEXR %d" % (version & 0xFF)
    if version & 0x1000:
        kind += ", %d parts" % len(parts)
    rows = [fact(plugin.tr("Name"), name)]
    if isinstance(whole, int):
        rows.append(fact(plugin.tr("Size"), _size(whole)))
    rows.append(fact(plugin.tr("Format"), kind))
    rows.append(fact(plugin.tr("Dimensions"), "%d × %d" % (first.width, first.height)))
    dx0, dy0, dx1, dy1 = first.display
    if first.display != (first.xmin, first.ymin, first.xmax, first.ymax):
        rows.append(fact(plugin.tr("Frame"), "%d × %d, the pixels from %d, %d to %d, %d"
                         % (dx1 - dx0 + 1, dy1 - dy0 + 1,
                            first.xmin, first.ymin, first.xmax, first.ymax)))
    aspect = first.get("pixelAspectRatio")
    if isinstance(aspect, float) and abs(aspect - 1.0) > 1e-6:
        rows.append(fact(plugin.tr("Pixel aspect"), "%.4g" % aspect))

    storage = [fact(plugin.tr("Compression"), exr.COMPRESSION_NAMES.get(first.compression, "unknown")
                    + ("" if first.compression in exr.READABLE else " — not read here"))]
    order = first.get("lineOrder")
    if order is not None and order != 0:
        storage.append(fact(plugin.tr("Line order"), exr.LINE_ORDERS.get(order, str(order))))
    if first.tiled and first.tiles:
        tx, ty, mode = first.tiles
        storage.append(fact(plugin.tr("Tiles"), "%d × %d, %s" % (tx, ty, exr.LEVEL_MODES.get(mode & 0xF, "?"))))
    if first.deep:
        storage.append(fact(plugin.tr("Deep"), "many samples per pixel"))

    layer_rows = []
    picked = None
    try:
        picked = choose(parts)
    except exr.ExrError:
        pass
    for index, layer, comps in layers(parts):
        types = sorted({exr.TYPE_NAMES.get(c.type, "?") for c in comps.values()})
        label = layer or "(unnamed)"
        if len(parts) > 1 and parts[index].name and parts[index].name != layer:
            label = "%s — part %s" % (label, parts[index].name)
        value = "%s · %s" % (" ".join(sorted(comps, key=_component_order)), "/".join(types))
        if picked and picked[0] == index and picked[1] == layer:
            value += " · drawn"
        layer_rows.append(fact(label, value))

    made, rest, colour = [], [], []
    for key, (kind_name, value) in sorted(first.attributes.items()):
        if key in STANDARD:
            continue
        shown = _shown(kind_name, value)
        if not shown:
            continue
        row = fact(key, shown, wide=len(shown) > 40)
        if key in ("chromaticities", "whiteLuminance", "adoptedNeutral", "colorInteropID",
                   "renderingTransform", "lookModTransform"):
            colour.append(row)
        elif kind_name == "string" or key in ("capDate", "utcOffset", "owner", "comments"):
            made.append(row)
        else:
            rest.append(row)

    groups = [
        fact_group(plugin.tr("Picture"), rows),
        fact_group(plugin.tr("Storage"), storage),
        fact_group(plugin.tr("Layers"), layer_rows),
        fact_group(plugin.tr("Made with"), made),
        fact_group(plugin.tr("Colour"), colour),
        fact_group(plugin.tr("Everything else"), rest),
    ]
    note = ""
    if len(parts) > 1:
        note = "Made with and everything else are the first part's; the other parts carry their own."
    return facts([g for g in groups if g["facts"]], note=note)


def _component_order(name: str):
    order = "RGBAYXZ"
    return (order.index(name) if name in order else len(order), name)


def describe_hdr(name: str, whole, head: bytes) -> dict:
    variables, other, width, height, bottom_up, _at = rgbe.read_header(head)
    rows = [fact(plugin.tr("Name"), name)]
    if isinstance(whole, int):
        rows.append(fact(plugin.tr("Size"), _size(whole)))
    fmt = variables.get("FORMAT", "32-bit_rle_rgbe")
    rows.append(fact(plugin.tr("Format"), "Radiance, " + ("XYZE" if "xyze" in fmt.lower() else "RGBE")))
    rows.append(fact(plugin.tr("Dimensions"), "%d × %d" % (width, height)))
    if bottom_up:
        rows.append(fact(plugin.tr("Rows"), "bottom to top"))
    said = [fact(k.title(), v, wide=len(v) > 40) for k, v in variables.items() if k != "FORMAT"]
    said += [fact(plugin.tr("Comment"), line, wide=True) for line in other[:12]]
    groups = [fact_group(plugin.tr("Picture"), rows), fact_group(plugin.tr("Header"), said)]
    return facts([g for g in groups if g["facts"]])


def describe_pfm(name: str, whole, head: bytes) -> dict:
    channels, width, height, little, _at = pfm.read_header(head)
    rows = [fact(plugin.tr("Name"), name)]
    if isinstance(whole, int):
        rows.append(fact(plugin.tr("Size"), _size(whole)))
    rows += [
        fact(plugin.tr("Format"), "Portable float map, " + ("colour" if channels == 3 else "grey")),
        fact(plugin.tr("Dimensions"), "%d × %d" % (width, height)),
        fact(plugin.tr("Byte order"), "little-endian" if little else "big-endian"),
    ]
    return facts([fact_group(plugin.tr("Picture"), rows)])


@plugin.describer("hdr.about", "About this picture", extensions=EXTENSIONS)
def about(url: str) -> dict:
    name = url.rsplit("/", 1)[-1]
    extension = name.rsplit(".", 1)[-1].lower()
    try:
        head = plugin.read_file(url, max_bytes=HEAD_BYTES)
    except Exception as failure:  # noqa: BLE001
        return facts([], note="The file could not be read: %s" % failure)
    if not head:
        return facts([], note="The file is empty.")
    whole = (plugin.stat(url) or {}).get("size")
    try:
        if extension == "exr":
            return describe_exr(name, whole, head)
        if extension == "hdr":
            return describe_hdr(name, whole, head)
        return describe_pfm(name, whole, head)
    except FAILURES as failure:
        return facts([], note="This file's header could not be read: %s" % failure)


@plugin.on_shutdown
def stop_workers():
    pool.shutdown()


if __name__ == "__main__":
    plugin.run()
