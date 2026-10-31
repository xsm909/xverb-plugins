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

"""A page of a Figma file as a flat list of shapes.

Figma stores the geometry already worked out: a rectangle with rounded corners
is not stored as a corner radius to be worked out by the reader but as a path
of lines and curves, and so is the outline of a boolean of three circles. Each
path sits in its node's own coordinates, and the chain of transforms down to it
is what puts it on the page.

So this walks the tree, composes the transforms, resolves the paints, and hands
the host paths in page coordinates with their colours on them. Nothing is
rasterised, because the host draws paths and does the magnification itself.

**What a Figma file does not carry, and so is not drawn:** the outlines of
text, which live in the fonts on the machine that typed it, and the pictures,
which are in the zip as PNG and JPEG and cannot be a path. Both are counted and
said out loud, and both are in the preview Figma itself rendered.
"""

from __future__ import annotations

import base64
import math
import struct

import figfile

#: The host's verbs, which the format's own happen to be one byte each too.
MOVE, LINE, CUBIC, CLOSE = 0, 1, 2, 3

#: How a path is written in a blob: the opcode, then that many floats.
_CLOSE, _MOVE, _LINE, _QUAD, _CUBIC = 0, 1, 2, 3, 4
_ARITY = {_CLOSE: 0, _MOVE: 2, _LINE: 2, _QUAD: 4, _CUBIC: 6}

#: Enough shapes for any real design; a file past this is drawn as far as it
#: goes and says so, rather than taking the window down with it.
MAX_SHAPES = 40000

IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def matrix_of(node: dict) -> tuple:
    m = node.get("transform")
    if not isinstance(m, dict):
        return IDENTITY
    return (m.get("m00", 1.0), m.get("m01", 0.0), m.get("m02", 0.0),
            m.get("m10", 0.0), m.get("m11", 1.0), m.get("m12", 0.0))


def multiply(a: tuple, b: tuple) -> tuple:
    """a applied after b, both as (m00, m01, m02, m10, m11, m12)."""
    a00, a01, a02, a10, a11, a12 = a
    b00, b01, b02, b10, b11, b12 = b
    return (
        a00 * b00 + a01 * b10,
        a00 * b01 + a01 * b11,
        a00 * b02 + a01 * b12 + a02,
        a10 * b00 + a11 * b10,
        a10 * b01 + a11 * b11,
        a10 * b02 + a11 * b12 + a12,
    )


def apply(m: tuple, x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[1] * y + m[2], m[3] * x + m[4] * y + m[5])


def invert(m: tuple) -> tuple | None:
    m00, m01, m02, m10, m11, m12 = m
    det = m00 * m11 - m01 * m10
    if abs(det) < 1e-12:
        return None
    i00, i01 = m11 / det, -m01 / det
    i10, i11 = -m10 / det, m00 / det
    return (i00, i01, -(i00 * m02 + i01 * m12),
            i10, i11, -(i10 * m02 + i11 * m12))


def path_of(blob: bytes) -> tuple[list[int], list[float]]:
    """One geometry blob as the host's verbs and points.

    Quadratics are raised to cubics on the way out: the host knows four verbs,
    and a format's own idea of a curve is the plugin's problem.
    """
    verbs: list[int] = []
    points: list[float] = []
    at = 0
    size = len(blob)
    last = (0.0, 0.0)
    while at < size:
        op = blob[at]
        at += 1
        arity = _ARITY.get(op)
        if arity is None:
            raise ValueError("a path command of an unknown kind: %d" % op)
        if at + 4 * arity > size:
            raise ValueError("a path command running past the end of its blob")
        values = struct.unpack_from("<%df" % arity, blob, at) if arity else ()
        at += 4 * arity

        if op == _CLOSE:
            verbs.append(CLOSE)
        elif op == _MOVE:
            verbs.append(MOVE)
            points.extend(values)
            last = (values[0], values[1])
        elif op == _LINE:
            verbs.append(LINE)
            points.extend(values)
            last = (values[0], values[1])
        elif op == _CUBIC:
            verbs.append(CUBIC)
            points.extend(values)
            last = (values[4], values[5])
        else:
            cx, cy, x, y = values
            verbs.append(CUBIC)
            points.extend([
                last[0] + 2.0 / 3.0 * (cx - last[0]),
                last[1] + 2.0 / 3.0 * (cy - last[1]),
                x + 2.0 / 3.0 * (cx - x),
                y + 2.0 / 3.0 * (cy - y),
                x, y,
            ])
            last = (x, y)
    return verbs, points


def colour_of(colour: dict | None, alpha: float) -> str | None:
    if not isinstance(colour, dict):
        return None
    a = colour.get("a", 1.0) * alpha
    if a <= 0.0:
        return None

    def channel(value):
        return max(0, min(255, int(round(value * 255.0))))

    return "#%02x%02x%02x%02x" % (channel(colour.get("r", 0.0)),
                                  channel(colour.get("g", 0.0)),
                                  channel(colour.get("b", 0.0)),
                                  channel(a))


def _stops(paint: dict, alpha: float) -> list[dict]:
    out = []
    for stop in paint.get("stops") or []:
        colour = colour_of(stop.get("color"), alpha)
        if colour is None:
            colour = "#00000000"
        out.append({"at": stop.get("position", 0.0), "colour": colour})
    return out


def _gradient(paint: dict, alpha: float, box: tuple, world: tuple) -> dict | None:
    """A Figma gradient paint as the host's gradient.

    Figma keeps a matrix taking the paint's own square to the shape's box in
    fractions of that box. The host wants two points in the drawing's
    coordinates, so the matrix is inverted to find where the paint's corners
    land, scaled by the box, and carried out through the node's transforms.
    """
    stops = _stops(paint, alpha)
    if len(stops) < 2:
        return None
    inverse = invert(matrix_of(paint))
    if inverse is None:
        return None

    x, y, width, height = box
    if width <= 0 or height <= 0:
        return None

    def place(u, v):
        fx, fy = apply(inverse, u, v)
        return apply(world, x + fx * width, y + fy * height)

    start = place(0.0, 0.0)
    end = place(1.0, 0.0)
    across = place(0.0, 1.0)

    if "RADIAL" not in str(paint.get("type")):
        return {
            "kind": "linear",
            "from": [start[0], start[1]],
            "to": [end[0], end[1]],
            "stops": stops,
            "spread": "pad",
        }

    # A round one is a centre and a focus rather than two ends, and the host
    # draws a circle: the two axes Figma allows become their average, which is
    # exact for the round ones and close for the rest.
    one = math.hypot(end[0] - start[0], end[1] - start[1])
    two = math.hypot(across[0] - start[0], across[1] - start[1])
    radius = (one + two) / 2.0
    if radius <= 0:
        return None
    return {
        "kind": "radial",
        "centre": [start[0], start[1]],
        "focus": [start[0], start[1]],
        "radius": radius,
        "stops": stops,
        "spread": "pad",
    }


def _paint(paints, alpha: float, box: tuple, world: tuple, tally: dict):
    """The one paint the host can draw, out of the stack a node carries."""
    for paint in paints or []:
        if not paint.get("visible", True):
            continue
        kind = str(paint.get("type") or "SOLID")
        opacity = paint.get("opacity", 1.0) * alpha
        if kind == "SOLID":
            colour = colour_of(paint.get("color"), opacity)
            if colour is not None:
                return colour, None
        elif kind.startswith("GRADIENT"):
            gradient = _gradient(paint, opacity, box, world)
            if gradient is not None:
                return None, gradient
        elif kind == "IMAGE":
            tally["pictures"] = tally.get("pictures", 0) + 1
        else:
            tally["other paints"] = tally.get("other paints", 0) + 1
    return None, None


def _scale_of(world: tuple) -> float:
    """What the transforms do to a length, for the stroke's width."""
    area = abs(world[0] * world[4] - world[1] * world[3])
    return math.sqrt(area) if area > 0 else 1.0


class Page:
    """One page, walked into shapes."""

    def __init__(self, document: figfile.Figma, progress=None) -> None:
        self.document = document
        self.progress = progress
        self.shapes: list[dict] = []
        self.tally: dict = {}
        self.bounds = [float("inf"), float("inf"), float("-inf"), float("-inf")]
        self._symbols: dict | None = None
        self._seen: set = set()

    # -- walking ----------------------------------------------------------

    def walk(self, node: dict, world: tuple, alpha: float, depth: int = 0) -> None:
        if len(self.shapes) >= MAX_SHAPES or depth > 64:
            return
        if not node.get("visible", True):
            self.tally["hidden"] = self.tally.get("hidden", 0) + 1
            return

        kind = node.get("type")
        here = multiply(world, matrix_of(node)) if kind != figfile.PAGE else world
        mine = alpha * node.get("opacity", 1.0)

        if kind == "TEXT":
            self.tally["text"] = self.tally.get("text", 0) + 1
            # A text node's own background and border are shapes like any
            # other; only the letters are missing.
        if kind == "INSTANCE":
            self._instance(node, here, mine, depth)
            return

        self._draw(node, here, mine)
        for child in self.document.children(node):
            self.walk(child, here, mine, depth + 1)

    def _instance(self, node: dict, world: tuple, alpha: float, depth: int) -> None:
        """A component instance: the component's own children, put here.

        What is not done: the overrides an instance carries — a changed colour
        or a hidden layer inside one instance — so an instance is drawn as the
        component stands. The count says how many there were.
        """
        self.tally["instances"] = self.tally.get("instances", 0) + 1
        if self._symbols is None:
            self._symbols = {}
            for other in self.document.nodes:
                if other.get("type") == "SYMBOL":
                    guid = figfile.guid_of(other.get("guid"))
                    if guid is not None:
                        self._symbols[guid] = other
        data = node.get("symbolData") or {}
        symbol = self._symbols.get(figfile.guid_of(data.get("symbolID")))
        self._draw(node, world, alpha)
        if symbol is None:
            self.tally["components not in the file"] = \
                self.tally.get("components not in the file", 0) + 1
            return
        guid = figfile.guid_of(symbol.get("guid"))
        if guid in self._seen:  # a component standing inside itself
            return
        self._seen.add(guid)
        for child in self.document.children(symbol):
            self.walk(child, world, alpha, depth + 1)
        self._seen.discard(guid)

    # -- one node's shapes ------------------------------------------------

    def _draw(self, node: dict, world: tuple, alpha: float) -> None:
        size = node.get("size") or {}
        box = (0.0, 0.0, size.get("x", 0.0), size.get("y", 0.0))

        fill, fill_gradient = _paint(node.get("fillPaints"), alpha, box, world,
                                     self.tally)
        stroke, stroke_gradient = _paint(node.get("strokePaints"), alpha, box,
                                         world, self.tally)
        width = node.get("strokeWeight", 1.0) * _scale_of(world)

        for geometry in node.get("fillGeometry") or []:
            self._shape(geometry, world, fill, fill_gradient, None, None, 0.0,
                        node)
        for geometry in node.get("strokeGeometry") or []:
            # Figma turns a stroke into its own outline, so what comes back is
            # a filled shape rather than a line to be stroked.
            self._shape(geometry, world, stroke, stroke_gradient, None, None,
                        0.0, node)
        if not (node.get("strokeGeometry")) and (stroke or stroke_gradient):
            for geometry in node.get("fillGeometry") or []:
                self._shape(geometry, world, None, None, stroke,
                            stroke_gradient, width, node)

    def _shape(self, geometry: dict, world: tuple, fill, fill_gradient,
               stroke, stroke_width_paint, width: float, node: dict) -> None:
        if fill is None and fill_gradient is None and stroke is None \
                and stroke_width_paint is None:
            return
        index = geometry.get("commandsBlob")
        if index is None or index >= len(self.document.blobs):
            return
        try:
            verbs, points = path_of(self.document.blobs[index])
        except ValueError:
            self.tally["unreadable paths"] = self.tally.get("unreadable paths", 0) + 1
            return
        if not verbs:
            return

        placed = []
        for i in range(0, len(points), 2):
            x, y = apply(world, points[i], points[i + 1])
            placed.append(x)
            placed.append(y)
            if x < self.bounds[0]:
                self.bounds[0] = x
            if y < self.bounds[1]:
                self.bounds[1] = y
            if x > self.bounds[2]:
                self.bounds[2] = x
            if y > self.bounds[3]:
                self.bounds[3] = y

        shape = {
            "verbs": base64.b64encode(bytes(verbs)).decode("ascii"),
            "points": base64.b64encode(
                struct.pack("<%df" % len(placed), *placed)).decode("ascii"),
            "fill": fill,
            "stroke": stroke,
            "strokeWidth": width,
            "evenOdd": geometry.get("windingRule") == "ODD",
            "cap": CAPS.get(node.get("strokeCap"), "butt"),
            "join": JOINS.get(node.get("strokeJoin"), "miter"),
        }
        if fill_gradient is not None:
            shape["fillGradient"] = fill_gradient
        if stroke_width_paint is not None:
            shape["strokeGradient"] = stroke_width_paint
        self.shapes.append(shape)


CAPS = {"NONE": "butt", "ROUND": "round", "SQUARE": "square"}
JOINS = {"MITER": "miter", "BEVEL": "bevel", "ROUND": "round"}


def of_page(document: figfile.Figma, page: dict, progress=None) -> dict:
    """One page as the host's drawing, in page coordinates."""
    walker = Page(document, progress=progress)
    walker.walk(page, IDENTITY, 1.0)
    shapes = walker.shapes

    left, top, right, bottom = walker.bounds
    if left > right or top > bottom:
        return {"width": 0.0, "height": 0.0, "shapes": [], "tally": walker.tally}

    # The drawing starts at its own top left corner: a page's content sits
    # wherever somebody dragged it, often a long way from the origin, and the
    # host would otherwise be asked to draw a mostly empty canvas.
    for shape in shapes:
        raw = base64.b64decode(shape["points"])
        values = list(struct.unpack("<%df" % (len(raw) // 4), raw))
        for i in range(0, len(values), 2):
            values[i] -= left
            values[i + 1] -= top
        shape["points"] = base64.b64encode(
            struct.pack("<%df" % len(values), *values)).decode("ascii")
        # A gradient's ends are points on the same page and move with it.
        for key in ("fillGradient", "strokeGradient"):
            gradient = shape.get(key)
            if not gradient:
                continue
            for where in ("from", "to", "centre", "focus"):
                point = gradient.get(where)
                if point:
                    gradient[where] = [point[0] - left, point[1] - top]

    return {
        "width": right - left,
        "height": bottom - top,
        "shapes": shapes,
        "tally": walker.tally,
        "truncated": len(shapes) >= MAX_SHAPES,
    }
