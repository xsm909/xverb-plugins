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

"""What is in the file, counted and written out.

The drawing answers "what does it look like"; this answers the questions a
drawing cannot — how many pages, what the frames are called and how big they
are, which fonts it asks the machine for, what the text actually says, which
components it is built from, and how much of its weight is pictures.

The fonts are the ones worth saying out loud: a `.fig` names them and does not
carry them, so a design opened on a machine without them is a different
design, and that is a thing to learn from a listing rather than from a
surprise.
"""

from __future__ import annotations

import figfile

#: Node types that stand for a shape somebody drew.
DRAWN = (
    "VECTOR", "ELLIPSE", "ROUNDED_RECTANGLE", "RECTANGLE", "REGULAR_POLYGON",
    "STAR", "LINE", "BOOLEAN_OPERATION", "TEXT", "FRAME", "SECTION",
    "INSTANCE", "SYMBOL", "STICKY", "SHAPE_WITH_TEXT", "CONNECTOR", "WIDGET",
    "MEDIA", "STAMP", "TABLE",
)

#: How many of a list are written out before it is cut short.
SOME = 40


def _size(count: int) -> str:
    if count >= 1 << 20:
        return "%.1f MB" % (count / float(1 << 20))
    if count >= 1 << 10:
        return "%.0f KB" % (count / float(1 << 10))
    return "%d bytes" % count


def _number(value: float) -> str:
    if abs(value - round(value)) < 0.01:
        return "%d" % round(value)
    return "%.1f" % value


def _text_of(node: dict) -> str:
    data = node.get("textData") or {}
    return (data.get("characters") or "").strip()


def _font_of(node: dict) -> str | None:
    font = node.get("fontName") or {}
    family = font.get("family")
    if not family:
        return None
    style = font.get("style")
    return "%s %s" % (family, style) if style else family


def of(document: figfile.Figma, bytes_on_disk: int, filename: str,
       tr=lambda s, v=None: s if v is None else s.format(**v)) -> str:
    """The whole report, as Markdown."""
    pages = document.pages
    lines: list[str] = []

    title = document.name or filename
    lines.append("# %s" % title)
    lines.append("")

    what = tr("A FigJam board") if document.kind == "jam" else tr("A Figma design")
    saved = document.exported_at[:10] if document.exported_at else ""
    if saved:
        lines.append(tr("{what}, saved out of Figma on {date}. {size} on disk.",
                        {"what": what, "date": saved,
                         "size": _size(bytes_on_disk)}))
    else:
        lines.append(tr("{what}. {size} on disk.",
                        {"what": what, "size": _size(bytes_on_disk)}))
    lines.append("")

    # -- the count of everything -----------------------------------------
    drawn = [n for n in document.nodes if n.get("type") in DRAWN]
    texts = [n for n in document.nodes if n.get("type") == "TEXT"]
    symbols = [n for n in document.nodes if n.get("type") == "SYMBOL"]
    instances = [n for n in document.nodes if n.get("type") == "INSTANCE"]
    pictures = sum(document.images.values())

    lines.append("| | |")
    lines.append("|---|---|")
    lines.append("| %s | %d |" % (tr("Pages"), len(pages)))
    lines.append("| %s | %d |" % (tr("Layers"), len(drawn)))
    if symbols:
        lines.append("| %s | %d |" % (tr("Components"), len(symbols)))
    if instances:
        lines.append("| %s | %d |" % (tr("Component instances"), len(instances)))
    if document.images:
        lines.append("| %s | %d, %s |"
                     % (tr("Pictures"), len(document.images), _size(pictures)))
    lines.append("| %s | %s %d |"
                 % (tr("Format"),
                    "fig-jam" if document.kind == "jam" else "fig-kiwi",
                    document.version))
    lines.append("")

    # -- the pages --------------------------------------------------------
    if pages:
        lines.append("## %s" % tr("Pages"))
        lines.append("")
        lines.append("| %s | %s | %s | %s |"
                     % (tr("Page"), tr("Layers"), tr("Frames"), tr("Text")))
        lines.append("|---|---:|---:|---:|")
        for page in pages:
            under = list(document.descendants(page))
            frames = sum(1 for n in under if n.get("type") in ("FRAME", "SECTION"))
            words = sum(1 for n in under if n.get("type") == "TEXT")
            lines.append("| %s | %d | %d | %d |"
                         % (page.get("name") or tr("Page"),
                            len(under), frames, words))
        lines.append("")

    # -- the frames somebody would export ---------------------------------
    tops: list[tuple[str, str, str]] = []
    for page in pages:
        for node in document.children(page):
            if node.get("type") not in ("FRAME", "SECTION", "SYMBOL"):
                continue
            size = node.get("size") or {}
            tops.append((page.get("name") or "", node.get("name") or "",
                         "%s × %s" % (_number(size.get("x", 0.0)),
                                      _number(size.get("y", 0.0)))))
    if tops:
        lines.append("## %s" % tr("Frames"))
        lines.append("")
        lines.append("| %s | %s | %s |" % (tr("Page"), tr("Frame"), tr("Size")))
        lines.append("|---|---|---|")
        for page_name, name, size in tops[:SOME * 2]:
            lines.append("| %s | %s | %s |" % (page_name, name, size))
        if len(tops) > SOME * 2:
            lines.append("| | %s | |"
                         % tr("{count} more", {"count": len(tops) - SOME * 2}))
        lines.append("")

    # -- the fonts it will ask the machine for ----------------------------
    fonts: dict[str, int] = {}
    for node in texts:
        name = _font_of(node)
        if name:
            fonts[name] = fonts.get(name, 0) + 1
    if fonts:
        lines.append("## %s" % tr("Fonts"))
        lines.append("")
        lines.append(tr("Named by the file and not carried in it — a machine "
                        "without one of these shows the design in something "
                        "else."))
        lines.append("")
        for name in sorted(fonts, key=lambda k: (-fonts[k], k)):
            lines.append("- %s — %s"
                         % (name, tr("{count} layer(s)", {"count": fonts[name]})))
        lines.append("")

    # -- what it says -----------------------------------------------------
    said = [t for t in (_text_of(n) for n in texts) if t]
    if said:
        lines.append("## %s" % tr("Text"))
        lines.append("")
        for line in said[:SOME]:
            one = " ".join(line.split())
            if len(one) > 160:
                one = one[:160] + "…"
            lines.append("- %s" % one)
        if len(said) > SOME:
            lines.append("- %s" % tr("{count} more",
                                     {"count": len(said) - SOME}))
        lines.append("")

    # -- the components ---------------------------------------------------
    if symbols:
        lines.append("## %s" % tr("Components"))
        lines.append("")
        for node in symbols[:SOME]:
            lines.append("- %s" % (node.get("name") or "?"))
        if len(symbols) > SOME:
            lines.append("- %s" % tr("{count} more",
                                     {"count": len(symbols) - SOME}))
        lines.append("")

    # -- the pictures -----------------------------------------------------
    if document.images:
        lines.append("## %s" % tr("Pictures"))
        lines.append("")
        lines.append(tr("Inside the file, named by the hash of their own "
                        "contents, which is how the same picture used twice "
                        "is stored once."))
        lines.append("")
        for name in sorted(document.images)[:SOME]:
            lines.append("- `%s` — %s" % (name[:16], _size(document.images[name])))
        if len(document.images) > SOME:
            lines.append("- %s" % tr("{count} more",
                                     {"count": len(document.images) - SOME}))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
