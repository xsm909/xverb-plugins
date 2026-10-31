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

"""Checks the readers, on made-up data and on real files.

    python3 selftest.py                 # the checks that need no file
    python3 selftest.py <folder>        # and every .fig and .jam under there

Each answer is worked out somewhere other than in the reader being checked.
The zstd fixtures were compressed by the reference `zstd` and are checked
against the digest of what went in, so agreeing with this decoder is not the
test — agreeing with the format is. The Kiwi check writes its message with an
encoder written here, from the specification rather than from `kiwi.py`. The
path and the matrices are arithmetic anybody can do on paper.

On Python 3.14 and later the standard library has zstd, and then every fixture
is also decoded both ways and compared, which is the strongest check available
and costs nothing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import sys
import time
import xml.etree.ElementTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import drawing  # noqa: E402
import figfile  # noqa: E402
import kiwi  # noqa: E402
import report  # noqa: E402
import svgout  # noqa: E402
import zstd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FAILURES: list[str] = []


def check(what: str, got, want) -> None:
    if got == want:
        print("  ok   %s" % what)
    else:
        print("  FAIL %s: got %r, wanted %r" % (what, got, want))
        FAILURES.append(what)


def close(what: str, got: float, want: float, slack: float = 1e-4) -> None:
    if abs(got - want) <= slack:
        print("  ok   %s" % what)
    else:
        print("  FAIL %s: got %r, wanted %r" % (what, got, want))
        FAILURES.append(what)


# -- zstd ----------------------------------------------------------------

def test_zstd() -> None:
    print("zstd, against what the reference compressor was given:")
    folder = os.path.join(HERE, "fixtures")
    with open(os.path.join(folder, "expected.json")) as handle:
        expected = json.load(handle)

    native = zstd._native
    for name in sorted(expected):
        with open(os.path.join(folder, name + ".zst"), "rb") as handle:
            data = handle.read()
        zstd._native = None
        try:
            out = zstd.decompress(data)
        except zstd.ZstdError as failure:
            print("  FAIL %s: %s" % (name, failure))
            FAILURES.append(name)
            continue
        finally:
            zstd._native = native
        check("%s, %d bytes" % (name, expected[name]["size"]),
              hashlib.sha256(out).hexdigest(), expected[name]["sha256"])

    if native is None:
        print("  --   the standard library has no zstd here, so the two "
              "readers cannot be compared")
        return
    print("zstd, this reader against the standard library's:")
    for name in sorted(expected):
        with open(os.path.join(folder, name + ".zst"), "rb") as handle:
            data = handle.read()
        zstd._native = None
        mine = zstd.decompress(data)
        zstd._native = native
        check("%s both ways" % name, mine, native(data))


def test_zstd_pieces() -> None:
    print("zstd, the parts:")
    # A stream of one byte, with the end marker above it, read backwards and
    # most significant bit first: 0b0010_1100 is a marker then 0b01100.
    bits = zstd._Bits(b"\x2c")
    check("the end marker is skipped", bits.read(5), 0b01100)
    check("past the end reads as zero", bits.read(3), 0)
    check("and says that it over-read", bits.spent, 3)

    # The default distributions are what the format fixes for a block that
    # sends no table of its own; they each have to add up to their table.
    for name, table, log in (("literal lengths", zstd._LL_DEFAULT, 6),
                             ("match lengths", zstd._ML_DEFAULT, 6),
                             ("offsets", zstd._OF_DEFAULT, 5)):
        check("the default %s fill a table of %d" % (name, 1 << log),
              sum(abs(n) for n in table), 1 << log)


# -- kiwi ----------------------------------------------------------------

def _string(value: str) -> bytes:
    return value.encode("utf-8") + b"\0"


def _varuint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 127
        value >>= 7
        if value:
            out.append(byte | 128)
        else:
            out.append(byte)
            return bytes(out)


def _varint(value: int) -> bytes:
    return _varuint((value << 1) ^ (value >> 31) if value >= 0
                    else ((-value) << 1) - 1)


def _varfloat(value: float) -> bytes:
    if value == 0.0:
        return b"\0"
    (bits,) = struct.unpack("<I", struct.pack("<f", value))
    # The exponent goes to the bottom, which is the rotation the reader undoes.
    rotated = ((bits >> 23) | (bits << 9)) & 0xFFFFFFFF
    return struct.pack("<I", rotated)


def test_kiwi() -> None:
    print("kiwi, on a message written here from the specification:")
    # One enum, one struct and one message, written by hand.
    schema = bytearray()
    schema += _varuint(3)

    schema += _string("Shade") + bytes([0]) + _varuint(2)
    schema += _string("DARK") + _varint(0) + bytes([0]) + _varuint(1)
    schema += _string("LIGHT") + _varint(0) + bytes([0]) + _varuint(2)

    schema += _string("Point") + bytes([1]) + _varuint(2)
    schema += _string("x") + _varint(-5) + bytes([0]) + _varuint(0)
    schema += _string("y") + _varint(-5) + bytes([0]) + _varuint(0)

    schema += _string("Message") + bytes([2]) + _varuint(4)
    schema += _string("name") + _varint(-6) + bytes([0]) + _varuint(1)
    schema += _string("count") + _varint(-4) + bytes([0]) + _varuint(2)
    schema += _string("corners") + _varint(1) + bytes([1]) + _varuint(3)
    schema += _string("shade") + _varint(0) + bytes([0]) + _varuint(4)

    message = bytearray()
    message += _varuint(1) + _string("a name")
    message += _varuint(2) + _varuint(300)
    message += _varuint(3) + _varuint(2)
    message += _varfloat(1.5) + _varfloat(0.0)
    message += _varfloat(-2.25) + _varfloat(10.0)
    message += _varuint(4) + _varuint(2)
    message += _varuint(0)

    read = kiwi.Schema(kiwi.parse_schema(bytes(schema))).decode(bytes(message))
    check("a string field", read["name"], "a name")
    check("a number over one byte", read["count"], 300)
    check("an array of structs", read["corners"],
          [{"x": 1.5, "y": 0.0}, {"x": -2.25, "y": 10.0}])
    check("an enum comes back named", read["shade"], "LIGHT")
    check("a field that was not sent is absent", "missing" in read, False)


# -- the geometry --------------------------------------------------------

def test_path() -> None:
    print("a path blob, against the square it draws:")
    # Move, three lines, close: a 10 by 20 rectangle at the origin.
    blob = (bytes([1]) + struct.pack("<2f", 0.0, 0.0)
            + bytes([2]) + struct.pack("<2f", 10.0, 0.0)
            + bytes([2]) + struct.pack("<2f", 10.0, 20.0)
            + bytes([2]) + struct.pack("<2f", 0.0, 20.0)
            + bytes([0]))
    verbs, points = drawing.path_of(blob)
    check("the verbs", verbs, [drawing.MOVE, drawing.LINE, drawing.LINE,
                               drawing.LINE, drawing.CLOSE])
    check("the points", points, [0.0, 0.0, 10.0, 0.0, 10.0, 20.0, 0.0, 20.0])

    # A quadratic from (0,0) with its control at (3,0) ending at (6,0) is the
    # straight line its control sits on; raised to a cubic its two controls
    # are two thirds of the way to that control, which is (2,0) and (4,0).
    blob = (bytes([1]) + struct.pack("<2f", 0.0, 0.0)
            + bytes([3]) + struct.pack("<4f", 3.0, 0.0, 6.0, 0.0))
    verbs, points = drawing.path_of(blob)
    check("a quadratic becomes a cubic", verbs, [drawing.MOVE, drawing.CUBIC])
    close("its first control", points[2], 2.0)
    close("its second control", points[4], 4.0)


def test_matrices() -> None:
    print("the transforms, on paper:")
    # Two along, three down, then doubled: a point at (1,1) lands at (4,8).
    move = (1.0, 0.0, 2.0, 0.0, 1.0, 3.0)
    double = (2.0, 0.0, 0.0, 0.0, 2.0, 0.0)
    both = drawing.multiply(double, move)
    check("scale after a move", drawing.apply(both, 1.0, 1.0), (6.0, 8.0))
    check("the other way round", drawing.apply(drawing.multiply(move, double),
                                               1.0, 1.0), (4.0, 5.0))
    back = drawing.invert(both)
    x, y = drawing.apply(back, 6.0, 8.0)
    close("undoing it", x, 1.0)
    close("undoing it, the other half", y, 1.0)
    check("a matrix that flattens everything has no inverse",
          drawing.invert((0.0, 0.0, 0.0, 0.0, 0.0, 0.0)), None)


def test_colours() -> None:
    print("colours:")
    white = {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0}
    check("white", drawing.colour_of(white, 1.0), "#ffffffff")
    check("white at a tenth", drawing.colour_of(white, 0.1), "#ffffff1a")
    check("nothing at all is nothing to draw",
          drawing.colour_of(white, 0.0), None)
    check("a colour that is not one", drawing.colour_of(None, 1.0), None)


def test_svg() -> None:
    print("the SVG a page is written out as:")
    # One triangle with a gradient across it, worked out here rather than read
    # out of a file: verbs 0, 1, 1, 3 and six coordinates.
    verbs = base64.b64encode(bytes([drawing.MOVE, drawing.LINE, drawing.LINE,
                                    drawing.CLOSE])).decode("ascii")
    points = base64.b64encode(
        struct.pack("<6f", 0.0, 0.0, 10.0, 0.0, 10.0, 20.0)).decode("ascii")
    page = {
        "width": 10.0,
        "height": 20.0,
        "shapes": [{
            "verbs": verbs,
            "points": points,
            "fill": None,
            "stroke": "#ff000080",
            "strokeWidth": 2.0,
            "evenOdd": True,
            "cap": "round",
            "join": "miter",
            "fillGradient": {
                "kind": "linear",
                "from": [0.0, 0.0],
                "to": [10.0, 0.0],
                "stops": [{"at": 0.0, "colour": "#000000ff"},
                          {"at": 1.0, "colour": "#ffffff00"}],
                "spread": "pad",
            },
        }],
    }
    written = svgout.of_drawing(page, background="#101010ff",
                                title="A page & a half").decode("utf-8")
    root = xml.etree.ElementTree.fromstring(written)
    check("it is XML, and it is an svg",
          root.tag, "{http://www.w3.org/2000/svg}svg")
    check("the title is escaped",
          root.find("{http://www.w3.org/2000/svg}title").text,
          "A page & a half")
    paths = root.findall("{http://www.w3.org/2000/svg}path")
    check("one path", len(paths), 1)
    check("the path is the triangle", paths[0].get("d"), "M0 0L10 0L10 20Z")
    check("an odd winding rule travels", paths[0].get("fill-rule"), "evenodd")
    check("a stroke keeps its colour", paths[0].get("stroke"), "#ff0000")
    check("and its alpha, in its own attribute",
          paths[0].get("stroke-opacity"), "0.502")
    check("the fill points at the gradient", paths[0].get("fill"), "url(#f0)")
    gradient = root.find(".//{http://www.w3.org/2000/svg}linearGradient")
    check("which is in the drawing's own coordinates",
          gradient.get("gradientUnits"), "userSpaceOnUse")
    check("with both its stops",
          len(gradient.findall("{http://www.w3.org/2000/svg}stop")), 2)
    check("the background is a rectangle of the whole page",
          root.find("{http://www.w3.org/2000/svg}rect").get("width"), "10")


# -- real files ----------------------------------------------------------

def test_files(folder: str) -> None:
    found = []
    for root, _dirs, names in os.walk(folder):
        for name in names:
            if name.lower().endswith((".fig", ".jam")):
                found.append(os.path.join(root, name))
    if not found:
        print("no .fig or .jam under %s" % folder)
        return
    print("real files under %s:" % folder)
    for path in sorted(found):
        started = time.time()
        try:
            with open(path, "rb") as handle:
                document = figfile.read(handle.read())
        except Exception as failure:  # noqa: BLE001
            print("  FAIL %s: %s" % (os.path.basename(path), failure))
            FAILURES.append(os.path.basename(path))
            continue
        read = time.time()

        pages = document.pages
        drawn = None
        for page in pages:
            drawn = drawing.of_page(document, page)
            if drawn["shapes"]:
                break
        written = report.of(document, os.path.getsize(path),
                            os.path.basename(path))
        print("  ok   %-34s %s %d, %d page(s), %d node(s), %d shape(s) "
              "%.0fx%.0f, read %.2fs, drawn %.2fs, %d line(s) of report"
              % (os.path.basename(path), document.kind, document.version,
                 len(pages), len(document.nodes),
                 len(drawn["shapes"]) if drawn else 0,
                 drawn["width"] if drawn else 0,
                 drawn["height"] if drawn else 0,
                 read - started, time.time() - read,
                 written.count("\n")))
        for page in pages:
            one = drawing.of_page(document, page)
            try:
                xml.etree.ElementTree.fromstring(
                    svgout.of_drawing(one, title=page.get("name")))
            except Exception as failure:  # noqa: BLE001
                print("  FAIL %s, page %s, as SVG: %s"
                      % (os.path.basename(path), page.get("name"), failure))
                FAILURES.append("%s as SVG" % os.path.basename(path))
        if drawn and drawn["shapes"]:
            for shape in drawn["shapes"]:
                if shape["fill"] is None and shape["stroke"] is None \
                        and "fillGradient" not in shape \
                        and "strokeGradient" not in shape:
                    FAILURES.append("%s: a shape with no paint on it"
                                    % os.path.basename(path))
                    break


def main() -> int:
    test_zstd()
    test_zstd_pieces()
    test_kiwi()
    test_path()
    test_matrices()
    test_colours()
    test_svg()
    if len(sys.argv) > 1:
        test_files(sys.argv[1])
    print()
    if FAILURES:
        print("%d check(s) failed: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
