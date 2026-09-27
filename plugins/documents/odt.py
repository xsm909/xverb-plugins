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

"""OpenDocument text — .odt, .ott and the flat .fodt — as Markdown.

What LibreOffice, OpenOffice, Google Docs' export and Pages' export write.
The format says what things are, which is what makes it the easy one:

- **A heading is `text:h`** with its `text:outline-level`, or a paragraph in a
  style that carries `style:default-outline-level` — which is how a heading
  pasted from somewhere else often arrives.
- **Formatting is in styles**, most of them automatic ones written into the
  document itself, each able to inherit from another: bold, italic and struck
  through are followed up the chain.
- **A list is `text:list`**, its labels described by the list style it names —
  a number in a format (`1`, `a`, `A`, `i`, `I`) between a prefix and a
  suffix, or a bullet. Counted here, per list, deeper levels starting again.
- **Notes** (`text:note`) and **comments** (`office:annotation`, with the
  author and the date) are gathered and written at the end, as for Word.
- **Tracked changes** are kept in a region of their own; skipping it leaves the
  text as it reads with every change accepted, which is what Word's reading
  here shows too.

A document with no headings at all — what `textutil` and many exports write —
gets its short, wholly bold paragraphs as headings, the same guess the Word
reader makes, so the structure panel has something to show.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional, Tuple

import mdtext
from docx import Result, _MAYBE_LONGEST, _Maybe, _format
from mdtext import Span
from package import Package, Unreadable

from xverb import Picture

OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
STYLE = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
FO = "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
XLINK = "http://www.w3.org/1999/xlink"
DC = "http://purl.org/dc/elements/1.1/"
META = "urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
SVG = "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
MANIFEST = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"

DRAWN = {"png", "jpg", "jpeg", "jpe", "gif", "bmp", "webp"}

#: Pixels in one of each unit ODF measures in, at the 96 a pixel is.
_UNITS = {"in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4, "pt": 96 / 72, "pc": 16.0, "px": 1.0}

#: What a list number is written as, in ODF's words and Word's.
_NUM_FORMATS = {"1": "decimal", "a": "lowerLetter", "A": "upperLetter",
                "i": "lowerRoman", "I": "upperRoman",
                "а": "russianLower", "А": "russianUpper"}


def _q(namespace: str, name: str) -> str:
    return "{%s}%s" % (namespace, name)


def _length(value: Optional[str]) -> Optional[float]:
    """An ODF length as pixels, or None for one this cannot read."""
    found = re.fullmatch(r"\s*([0-9.]+)\s*([a-z]*)\s*", value or "")
    if not found:
        return None
    try:
        return float(found.group(1)) * _UNITS.get(found.group(2) or "px", 1.0)
    except ValueError:
        return None


class _Styles:
    """Text and paragraph styles, automatic and named, followed up their chain."""

    def __init__(self, roots: List[Optional[ET.Element]]):
        self.text: Dict[str, Tuple[Optional[str], Dict[str, str]]] = {}
        self.paragraph: Dict[str, Tuple[Optional[str], Dict[str, str], Optional[int]]] = {}
        self.lists: Dict[str, Dict[int, Tuple[str, str, str, int]]] = {}
        self.display: Dict[str, str] = {}
        for root in roots:
            if root is None:
                continue
            for style in root.iter(_q(STYLE, "style")):
                name = style.get(_q(STYLE, "name")) or ""
                parent = style.get(_q(STYLE, "parent-style-name"))
                props = style.find(_q(STYLE, "text-properties"))
                said = dict(props.attrib) if props is not None else {}
                family = style.get(_q(STYLE, "family"))
                shown = style.get(_q(STYLE, "display-name"))
                if shown:
                    self.display[name] = shown
                if family == "text":
                    self.text[name] = (parent, said)
                elif family == "paragraph":
                    level = style.get(_q(STYLE, "default-outline-level"))
                    self.paragraph[name] = (
                        parent, said, int(level) if level and level.isdigit() else None)
            for listed in root.iter(_q(TEXT, "list-style")):
                levels: Dict[int, Tuple[str, str, str, int]] = {}
                for child in listed:
                    level = int(child.get(_q(TEXT, "level")) or 1)
                    if child.tag == _q(TEXT, "list-level-style-number"):
                        levels[level] = (
                            _NUM_FORMATS.get(child.get(_q(STYLE, "num-format")) or "1", "decimal"),
                            child.get(_q(STYLE, "num-prefix")) or "",
                            child.get(_q(STYLE, "num-suffix")) or ".",
                            int(child.get(_q(TEXT, "start-value")) or 1),
                        )
                    else:
                        levels[level] = ("bullet", "", "", 1)
                self.lists[listed.get(_q(STYLE, "name")) or ""] = levels

    def _said(self, table, name: Optional[str], key: str) -> Optional[str]:
        seen = set()
        while name and name not in seen:
            seen.add(name)
            entry = table.get(name)
            if entry is None:
                return None
            if key in entry[1]:
                return entry[1][key]
            name = entry[0]
        return None

    def run(self, name: Optional[str], inherited: Tuple[bool, bool, bool]) -> Tuple[bool, bool, bool]:
        bold, italic, strike = inherited
        for table in (self.text, self.paragraph):
            weight = self._said(table, name, _q(FO, "font-weight"))
            if weight is not None:
                bold = weight in ("bold", "600", "700", "800", "900")
            posture = self._said(table, name, _q(FO, "font-style"))
            if posture is not None:
                italic = posture in ("italic", "oblique")
            through = self._said(table, name, _q(STYLE, "text-line-through-style"))
            if through is not None:
                strike = through not in ("none", "")
        return bold, italic, strike

    def outline(self, name: Optional[str]) -> Optional[int]:
        seen = set()
        while name and name not in seen:
            seen.add(name)
            entry = self.paragraph.get(name)
            if entry is None:
                break
            if entry[2]:
                return entry[2]
            name = entry[0]
        # A named style's display name says it where the level is left out:
        # "Heading 2", and "Title" which is the document's first heading.
        shown = (self.display.get(name or "") or name or "").lower().replace("_20_", " ")
        found = re.fullmatch(r"heading (\d)", shown)
        if found:
            return int(found.group(1))
        return 1 if shown == "title" else None


class Converter:
    def __init__(self, content: ET.Element, styles: Optional[ET.Element],
                 meta: Optional[ET.Element], package: Optional[Package],
                 tr: Callable[..., str]):
        self.content = content
        self.package = package
        self.tr = tr
        self.meta_root = meta
        self.styles = _Styles([styles, content])
        self.result = Result()
        self.notes: List[str] = []
        self.comments: List[str] = []
        self.after: List[str] = []
        self.in_cell = 0
        self.headings = 0
        self.changes = 0
        #: Per list, the count at each level, reset below a level that moves.
        self.counters: Dict[int, List[int]] = {}

    def convert(self) -> Result:
        body = self.content.find(_q(OFFICE, "body"))
        text = body.find(_q(OFFICE, "text")) if body is not None else None
        if text is None:
            raise Unreadable(self.tr("This is an OpenDocument file, but not a text document."))
        changes = text.find(_q(TEXT, "tracked-changes"))
        if changes is not None:
            self.changes = len(changes.findall(_q(TEXT, "changed-region")))
        out: List[str] = []
        self._blocks(text, out, None, 0)
        if self.notes:
            out.append(mdtext.heading(2, mdtext.escape(self.tr("Notes"))))
            out += ["\\[%d\\] %s" % (n, t) for n, t in enumerate(self.notes, 1)]
        if self.comments:
            out.append(mdtext.heading(2, mdtext.escape(self.tr("Comments"))))
            for number, text_ in enumerate(self.comments, 1):
                label = "**%s**" % mdtext.escape(self.tr("Comment {n}", {"n": number}))
                out.append("%s — %s" % (label, text_))
        guessed = self.headings == 0
        out = [
            (mdtext.heading(1, block.heading) if guessed else str(block))
            if isinstance(block, _Maybe) else block
            for block in out
        ]
        result = self.result
        result.guessed = guessed and any(b.startswith("# ") for b in out)
        result.markdown = "\n\n".join(b for b in out if b.strip()) + "\n"
        result.changes = self.changes
        result.comments = len(self.comments)
        result.words = len(re.findall(r"\w+", re.sub(r"\(picture:[^)]*\)", "", result.markdown)))
        result.meta = self._meta()
        return result

    # Blocks.

    def _blocks(self, parent: ET.Element, out: List[str], listed, depth: int) -> None:
        for child in parent:
            tag = child.tag
            if tag == _q(TEXT, "h"):
                level = int(child.get(_q(TEXT, "outline-level")) or 1)
                self._paragraph(child, out, level, None)
            elif tag == _q(TEXT, "p"):
                self._paragraph(child, out, None, None)
            elif tag == _q(TEXT, "list"):
                self._list(child, out, listed, depth)
            elif tag == _q(TABLE, "table"):
                out.append(self._table(child))
                out.extend(self._take_after())
            elif tag in (_q(TEXT, "section"), _q(TEXT, "index-body")):
                self._blocks(child, out, listed, depth)
            elif tag in (_q(TEXT, "table-of-content"), _q(TEXT, "tracked-changes"),
                         _q(TEXT, "illustration-index"), _q(TEXT, "alphabetical-index"),
                         _q(TEXT, "bibliography"), _q(TEXT, "sequence-decls"),
                         _q(TEXT, "variable-decls"), _q(TEXT, "user-field-decls"),
                         _q(OFFICE, "forms")):
                # The contents is the structure panel's; the rest is bookkeeping.
                continue

    def _take_after(self) -> List[str]:
        after, self.after = self.after, []
        return after

    def _list(self, element: ET.Element, out: List[str], listed, depth: int) -> None:
        style = element.get(_q(TEXT, "style-name")) or (listed[0] if listed else None)
        key = listed[1] if listed and element.get(_q(TEXT, "continue-numbering")) != "false" \
            else id(element)
        if depth == 0 and element.get(_q(TEXT, "continue-numbering")) != "true":
            self.counters.pop(key, None)
        for item in element:
            if item.tag not in (_q(TEXT, "list-item"), _q(TEXT, "list-header")):
                continue
            first = True
            for child in item:
                if child.tag == _q(TEXT, "list"):
                    self._list(child, out, (style, key), depth + 1)
                elif child.tag in (_q(TEXT, "p"), _q(TEXT, "h")):
                    label = None
                    if first and item.tag == _q(TEXT, "list-item"):
                        label = self._label(style, key, depth,
                                            item.get(_q(TEXT, "start-value")))
                    level = int(child.get(_q(TEXT, "outline-level")) or 1) \
                        if child.tag == _q(TEXT, "h") else None
                    self._paragraph(child, out, level, (label, depth) if first else ("", depth))
                    first = False
                else:
                    self._blocks(ET.Element("x", {}), out, None, depth)

    def _label(self, style: Optional[str], key, depth: int, start: Optional[str]) -> Tuple[str, str]:
        levels = self.styles.lists.get(style or "", {})
        kind, prefix, suffix, first = levels.get(depth + 1, ("bullet", "", "", 1))
        if kind == "bullet":
            return ("bullet", "")
        counts = self.counters.setdefault(key, [])
        while len(counts) <= depth:
            counts.append(0)
        del counts[depth + 1:]
        counts[depth] = int(start) if start and start.isdigit() else (counts[depth] or first - 1) + 1
        return ("number", prefix + _format(counts[depth], kind) + suffix)

    def _paragraph(self, p: ET.Element, out: List[str], level: Optional[int], listed) -> None:
        style = p.get(_q(TEXT, "style-name"))
        if level is None:
            level = self.styles.outline(style)
        pieces: List[Span] = []
        self._inline(p, pieces, self.styles.run(style, (False, False, False)), None)
        after = self._take_after()
        text = mdtext.spans(pieces)
        if text:
            label = listed[0] if listed else None
            depth = listed[1] if listed else 0
            if self.in_cell:
                out.append(text)
            elif level:
                self.headings += 1
                prefix = mdtext.escape(label[1]) + " " if label and label[0] == "number" else ""
                out.append(mdtext.heading(level, prefix + text))
            elif label and label[0] == "bullet":
                out.append("  " * depth + "- " + text)
            elif label and label[0] == "number" and re.fullmatch(r"\d+[.)]", label[1]):
                out.append("  " * depth + label[1][:-1] + ". " + text)
            elif label and label[0] == "number":
                out.append("  " * depth + mdtext.line_start(mdtext.escape(label[1]) + " " + text))
            elif listed:
                # A second paragraph of one item: kept under it.
                out.append("  " * (depth + 1) + mdtext.line_start(text))
            else:
                plain = "".join(piece.text for piece in pieces if not piece.raw)
                bold = all(piece.bold or not piece.text.strip() for piece in pieces if not piece.raw)
                if (bold and plain.strip() and len(plain.strip()) <= _MAYBE_LONGEST
                        and not plain.rstrip().endswith((".", ":", ";", ",", "!", "?"))):
                    maybe = _Maybe(mdtext.paragraph(text))
                    maybe.heading = mdtext.escape(mdtext.squeeze(plain).strip())
                    out.append(maybe)
                else:
                    out.append(mdtext.paragraph(text))
        out.extend(after)

    # Inline.

    def _inline(self, parent: ET.Element, pieces: List[Span], style, link: Optional[str]) -> None:
        bold, italic, strike = style

        def say(text: Optional[str]) -> None:
            if text:
                pieces.append(Span(text, bold, italic, strike, link))

        say(parent.text)
        for child in parent:
            tag = child.tag
            if tag == _q(TEXT, "span"):
                self._inline(child, pieces,
                             self.styles.run(child.get(_q(TEXT, "style-name")), style), link)
            elif tag == _q(TEXT, "a"):
                href = child.get(_q(XLINK, "href")) or ""
                self._inline(child, pieces, style,
                             href if re.match(r"(?i)(https?|mailto|ftp):", href) else link)
            elif tag == _q(TEXT, "s"):
                say(" " * int(child.get(_q(TEXT, "c")) or 1))
            elif tag in (_q(TEXT, "tab"), _q(TEXT, "line-break")):
                say(" ")
            elif tag == _q(TEXT, "note"):
                body = child.find(_q(TEXT, "note-body"))
                if body is not None:
                    self.notes.append(self._flat(body))
                    pieces.append(Span("\\[%d\\]" % len(self.notes), raw=True))
            elif tag == _q(OFFICE, "annotation"):
                self._annotation(child, pieces)
            elif tag == _q(DRAW, "frame"):
                self._frame(child)
            elif tag in (_q(TEXT, "bookmark"), _q(TEXT, "bookmark-start"),
                         _q(TEXT, "bookmark-end"), _q(OFFICE, "annotation-end"),
                         _q(TEXT, "soft-page-break"), _q(TEXT, "change"),
                         _q(TEXT, "change-start"), _q(TEXT, "change-end"),
                         _q(TEXT, "reference-mark-start"), _q(TEXT, "reference-mark-end")):
                pass
            else:
                # A field — a date, a page count, a cross reference — is its
                # shown value, which is its text.
                self._inline(child, pieces, style, link)
            say(child.tail)

    def _flat(self, parent: ET.Element) -> str:
        out: List[str] = []
        saved = self.after
        self.after = []
        self.in_cell += 1
        try:
            self._blocks(parent, out, None, 0)
        finally:
            self.in_cell -= 1
            extra = self.after
            self.after = saved
        return mdtext.cell(" · ".join(b for b in out + extra if b.strip()))

    def _annotation(self, element: ET.Element, pieces: List[Span]) -> None:
        author = element.findtext(_q(DC, "creator")) or ""
        when = (element.findtext(_q(DC, "date")) or "")[:10]
        holder = ET.Element("x")
        for child in element:
            if child.tag in (_q(TEXT, "p"), _q(TEXT, "list")):
                holder.append(child)
        head = ", ".join(x for x in (author, when) if x)
        self.comments.append((mdtext.escape(head) + ": " if head else "") + self._flat(holder))
        pieces.append(Span("\\[%s\\]" % mdtext.escape(
            self.tr("comment {n}", {"n": len(self.comments)})), raw=True))

    def _frame(self, frame: ET.Element) -> None:
        caption = " ".join(
            (frame.findtext(_q(SVG, "title")) or frame.findtext(_q(SVG, "desc"))
             or frame.get(_q(DRAW, "name")) or "").split())
        box = frame.find(_q(DRAW, "text-box"))
        if box is not None:
            out: List[str] = []
            self._blocks(box, out, None, 0)
            self.after.extend(out)
            return
        image = frame.find(_q(DRAW, "image"))
        if image is None:
            return
        part = image.get(_q(XLINK, "href")) or ""
        extension = part.rsplit(".", 1)[-1].lower() if "." in part else ""
        caption = caption.replace("[", "(").replace("]", ")")
        if (self.in_cell or self.package is None or extension not in DRAWN
                or not self.package.has(part)):
            what = caption or extension.upper() or self.tr("picture")
            self.after.append(mdtext.escape("[%s]" % self.tr("picture: {what}", {"what": what})))
            return
        width = _length(frame.get(_q(SVG, "width")))
        height = _length(frame.get(_q(SVG, "height")))
        key = "p%d" % (len(self.result.pictures) + 1)
        self.result.pictures[key] = Picture(
            self.package.later(part),
            round(width) if width else None, round(height) if height else None)
        self.after.append("![%s](picture:%s)" % (caption, key))

    # Tables.

    def _table(self, table: ET.Element) -> str:
        rows: List[List[str]] = []
        header = False

        def walk(parent: ET.Element, heading: bool) -> None:
            nonlocal header
            for child in parent:
                if child.tag == _q(TABLE, "table-header-rows"):
                    header = header or not rows
                    walk(child, True)
                elif child.tag in (_q(TABLE, "table-rows"), _q(TABLE, "table-row-group")):
                    walk(child, heading)
                elif child.tag == _q(TABLE, "table-row"):
                    cells: List[str] = []
                    for cell in child:
                        if cell.tag == _q(TABLE, "covered-table-cell"):
                            cells.append("")
                        elif cell.tag == _q(TABLE, "table-cell"):
                            repeat = int(cell.get(_q(TABLE, "number-columns-repeated")) or 1)
                            text = self._cell(cell)
                            cells.extend([text] * min(repeat, 64))
                    rows.append(cells)

        walk(table, False)
        # Trailing empty columns that a spreadsheet-like repeat left behind.
        while rows and all(len(r) and not r[-1] for r in rows) and max(len(r) for r in rows) > 1:
            rows = [r[:-1] for r in rows]
        if not rows:
            return ""
        return mdtext.table(rows, header and len(rows) > 1)

    def _cell(self, cell: ET.Element) -> str:
        parts: List[str] = []
        for child in cell:
            if child.tag == _q(TABLE, "table"):
                nested = [self._cell(c) for c in child.iter(_q(TABLE, "table-cell"))]
                parts.append(" · ".join(n for n in nested if n))
            else:
                holder = ET.Element("x")
                holder.append(child)
                text = self._flat(holder)
                if text:
                    parts.append(text)
        return mdtext.cell(" · ".join(parts))

    # What the file says about itself.

    def _meta(self) -> Dict[str, str]:
        meta: Dict[str, str] = {}
        root = self.meta_root
        if root is None:
            return meta
        found = root.find(_q(OFFICE, "meta"))
        if found is None:
            found = root
        pairs = (
            ("title", _q(DC, "title")), ("subject", _q(DC, "subject")),
            ("description", _q(DC, "description")), ("language", _q(DC, "language")),
            ("creator", _q(META, "initial-creator")), ("lastModifiedBy", _q(DC, "creator")),
            ("created", _q(META, "creation-date")), ("modified", _q(DC, "date")),
            ("Application", _q(META, "generator")),
        )
        for key, tag in pairs:
            value = (found.findtext(tag) or "").strip()
            if value:
                meta[key] = value
        if "creator" not in meta and "lastModifiedBy" in meta:
            meta["creator"] = meta.pop("lastModifiedBy")
        words = [k.text.strip() for k in found.findall(_q(META, "keyword")) if k.text and k.text.strip()]
        if words:
            meta["keywords"] = ", ".join(words)
        statistic = found.find(_q(META, "document-statistic"))
        if statistic is not None and statistic.get(_q(META, "page-count")):
            meta["Pages"] = statistic.get(_q(META, "page-count"))
        if "Application" in meta:
            meta["Application"] = meta["Application"].split("$")[0].split("/")[0].strip()
        return meta


def _parse(data: Optional[bytes]) -> Optional[ET.Element]:
    if not data:
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def convert(package: Package, tr: Callable[..., str]) -> Result:
    """A packaged .odt or .ott."""
    manifest = _parse(package.read("META-INF/manifest.xml"))
    if manifest is not None and manifest.find(".//" + _q(MANIFEST, "encryption-data")) is not None:
        raise Unreadable(tr(
            "This document is protected with a password. Press Enter to open it "
            "in the application that can ask for one."))
    content = _parse(package.read("content.xml"))
    if content is None:
        raise Unreadable(tr("This OpenDocument file has no readable text in it."))
    return Converter(content, _parse(package.read("styles.xml")),
                     _parse(package.read("meta.xml")), package, tr).convert()


def convert_flat(data: bytes, tr: Callable[..., str]) -> Result:
    """A flat .fodt: the whole package as one XML file, pictures inline and
    so not drawn."""
    root = _parse(data)
    if root is None:
        raise Unreadable(tr("This OpenDocument file has no readable text in it."))
    return Converter(root, root, root, None, tr).convert()


def is_text(package: Package) -> bool:
    """Whether a zip is an OpenDocument *text* rather than a sheet or slides."""
    kind = (package.read("mimetype") or b"").decode("ascii", "replace").strip()
    return kind.startswith("application/vnd.oasis.opendocument.text")
