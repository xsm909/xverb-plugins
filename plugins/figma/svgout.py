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

"""A page, written out as SVG.

This is what a page *is* when the file is entered as a folder: each page is an
`.svg` the panel can copy out, open in anything, or hand to whichever viewer
claims a drawing. It is written from the same shapes the design view sends the
host, so the two never disagree — one of them turns paths into a drawing on
screen and the other into a file, and neither invents anything.

Everything is already absolute by the time it arrives: transforms composed,
curves cubic, gradients resolved to points on the page. So the writing is
mechanical, and the only decisions left are how many digits a coordinate keeps
and that a colour's alpha travels in its own attribute, since `#rrggbbaa` is
younger than most things that read SVG.
"""

from __future__ import annotations

import base64
import struct

MOVE, LINE, CUBIC, CLOSE = 0, 1, 2, 3

#: Enough for a design: a tenth of a pixel on a page of ten thousand.
DIGITS = 3


def _num(value: float) -> str:
    out = ("%.*f" % (DIGITS, value)).rstrip("0").rstrip(".")
    return "0" if out in ("", "-0") else out


def _split_colour(colour: str | None) -> tuple[str, float] | None:
    """`#rrggbbaa` as the pair SVG wants: a colour and its own opacity."""
    if not colour or len(colour) != 9 or not colour.startswith("#"):
        return None
    try:
        alpha = int(colour[7:9], 16) / 255.0
    except ValueError:
        return None
    return colour[:7], alpha


def _path(verbs: bytes, points: tuple) -> str:
    out = []
    at = 0
    for verb in verbs:
        if verb == MOVE:
            out.append("M%s %s" % (_num(points[at]), _num(points[at + 1])))
            at += 2
        elif verb == LINE:
            out.append("L%s %s" % (_num(points[at]), _num(points[at + 1])))
            at += 2
        elif verb == CUBIC:
            out.append("C%s %s %s %s %s %s"
                       % tuple(_num(v) for v in points[at:at + 6]))
            at += 6
        else:
            out.append("Z")
    return "".join(out)


def _gradient(gradient: dict, name: str) -> tuple[str, str]:
    """One gradient as a definition and the `url(…)` that points at it."""
    stops = []
    for stop in gradient.get("stops") or []:
        found = _split_colour(stop.get("colour"))
        if found is None:
            continue
        colour, alpha = found
        stops.append('<stop offset="%s" stop-color="%s" stop-opacity="%s"/>'
                     % (_num(stop.get("at", 0.0)), colour, _num(alpha)))
    body = "".join(stops)

    # Everything is in the drawing's own coordinates, which is what
    # userSpaceOnUse means and the reason nothing here needs a bounding box.
    if gradient.get("kind") == "radial":
        centre = gradient.get("centre") or [0.0, 0.0]
        focus = gradient.get("focus") or centre
        definition = (
            '<radialGradient id="%s" gradientUnits="userSpaceOnUse" '
            'cx="%s" cy="%s" r="%s" fx="%s" fy="%s">%s</radialGradient>'
            % (name, _num(centre[0]), _num(centre[1]),
               _num(gradient.get("radius", 0.0)),
               _num(focus[0]), _num(focus[1]), body))
    else:
        start = gradient.get("from") or [0.0, 0.0]
        end = gradient.get("to") or [0.0, 0.0]
        definition = (
            '<linearGradient id="%s" gradientUnits="userSpaceOnUse" '
            'x1="%s" y1="%s" x2="%s" y2="%s">%s</linearGradient>'
            % (name, _num(start[0]), _num(start[1]),
               _num(end[0]), _num(end[1]), body))
    return definition, "url(#%s)" % name


def of_drawing(drawing: dict, background: str | None = None,
               title: str | None = None) -> bytes:
    """A drawing as an SVG file, ready to be written or read by anybody."""
    width = drawing.get("width") or 0.0
    height = drawing.get("height") or 0.0
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" '
        'viewBox="0 0 %s %s">' % (_num(width), _num(height),
                                  _num(width), _num(height)),
    ]
    if title:
        out.append("<title>%s</title>"
                   % title.replace("&", "&amp;").replace("<", "&lt;"))

    definitions: list[str] = []
    body: list[str] = []
    if background:
        found = _split_colour(background)
        if found is not None:
            colour, alpha = found
            body.append('<rect x="0" y="0" width="%s" height="%s" fill="%s" '
                        'fill-opacity="%s"/>'
                        % (_num(width), _num(height), colour, _num(alpha)))

    for index, shape in enumerate(drawing.get("shapes") or []):
        verbs = base64.b64decode(shape.get("verbs") or "")
        raw = base64.b64decode(shape.get("points") or "")
        points = struct.unpack("<%df" % (len(raw) // 4), raw)
        attributes = []

        gradient = shape.get("fillGradient")
        if gradient:
            definition, reference = _gradient(gradient, "f%d" % index)
            definitions.append(definition)
            attributes.append('fill="%s"' % reference)
        else:
            found = _split_colour(shape.get("fill"))
            if found is None:
                attributes.append('fill="none"')
            else:
                attributes.append('fill="%s" fill-opacity="%s"'
                                  % (found[0], _num(found[1])))

        gradient = shape.get("strokeGradient")
        if gradient:
            definition, reference = _gradient(gradient, "s%d" % index)
            definitions.append(definition)
            attributes.append('stroke="%s"' % reference)
        else:
            found = _split_colour(shape.get("stroke"))
            if found is not None:
                attributes.append('stroke="%s" stroke-opacity="%s"'
                                  % (found[0], _num(found[1])))
        if shape.get("stroke") or shape.get("strokeGradient"):
            attributes.append('stroke-width="%s"'
                              % _num(shape.get("strokeWidth") or 0.0))
            if shape.get("cap", "butt") != "butt":
                attributes.append('stroke-linecap="%s"' % shape["cap"])
            if shape.get("join", "miter") != "miter":
                attributes.append('stroke-linejoin="%s"' % shape["join"])
        if shape.get("evenOdd"):
            attributes.append('fill-rule="evenodd"')

        body.append('<path d="%s" %s/>'
                    % (_path(verbs, points), " ".join(attributes)))

    if definitions:
        out.append("<defs>%s</defs>" % "".join(definitions))
    out.extend(body)
    out.append("</svg>")
    return "\n".join(out).encode("utf-8")
