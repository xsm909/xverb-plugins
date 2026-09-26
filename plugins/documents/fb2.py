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

"""FictionBook — .fb2, and .fb2.zip — as Markdown.

One XML file holds the whole book: `description` says what it is, the first
`body` is the text, a `body name="notes"` the notes, and the pictures are
`binary` elements in base64 at the end. The encoding is the prolog's, and it is
windows-1251 as often as UTF-8.

Structure is the file's own: a `section` inside a `section` is one heading
deeper, whatever its title says. Verses keep their lines. A note's mark stays
where it is and the notes are read as the book's last chapter, the way a
printed book has them.
"""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

import mdtext
from mdtext import Span

from xverb import Picture, picture_size

FB = "http://www.gribuser.ru/xml/fictionbook/2.0"
XLINK = "http://www.w3.org/1999/xlink"


def _f(name: str) -> str:
    return "{%s}%s" % (FB, name)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _href(element: ET.Element) -> str:
    for key, value in element.attrib.items():
        if _local(key) == "href":
            return value
    return ""


class Result:
    def __init__(self):
        self.markdown = ""
        self.pictures: Dict[str, Picture] = {}
        self.changes = 0
        self.comments = 0
        self.words = 0
        self.meta: Dict[str, str] = {}
        self.guessed = False
        self.cover: Optional[Picture] = None


def parse(data: bytes) -> ET.Element:
    """The book's tree, whatever it is encoded in. Expat reads UTF-8 and
    UTF-16 itself; anything else is decoded here first and the prolog's
    claim taken out, so it cannot be believed twice."""
    head = data[:200]
    found = re.search(rb'encoding=["\']([\w.-]+)["\']', head)
    name = found.group(1).decode("ascii", "ignore").lower() if found else "utf-8"
    if name.replace("_", "-") in ("utf-8", "utf8", "utf-16", "us-ascii", "ascii"):
        return ET.fromstring(data)
    try:
        text = data.decode(name)
    except (LookupError, UnicodeDecodeError):
        text = data.decode("cp1251", "replace")
    text = re.sub(r'^\s*<\?xml[^>]*\?>', '<?xml version="1.0"?>', text, count=1)
    return ET.fromstring(text.encode("utf-8"))


class Book:
    def __init__(self, root: ET.Element, tr):
        self.root = root
        self.tr = tr
        self.result = Result()
        self.binaries: Dict[str, str] = {}
        for binary in root.iter(_f("binary")):
            if binary.get("id"):
                self.binaries[binary.get("id")] = binary.text or ""
        self.keys: Dict[str, str] = {}

    # Pictures.

    def _picture(self, element: ET.Element) -> Optional[str]:
        href = _href(element).lstrip("#")
        caption = element.get("alt") or element.get("title") or ""
        caption = " ".join(caption.replace("[", "(").replace("]", ")").split())
        if href in self.keys:
            return "![%s](picture:%s)" % (caption, self.keys[href])
        encoded = self.binaries.get(href)
        if not encoded:
            return mdtext.escape("[%s]" % caption) if caption else None
        compact = re.sub(r"\s+", "", encoded[: (1 << 17)])
        head = base64.b64decode(compact[: len(compact) // 4 * 4] or b"")
        size = picture_size(head)
        if size is None:
            return mdtext.escape("[%s]" % self.tr("picture: {what}", {"what": caption or href}))

        def read(encoded=encoded) -> bytes:
            return base64.b64decode(re.sub(r"\s+", "", encoded))

        key = "p%d" % (len(self.result.pictures) + 1)
        self.result.pictures[key] = Picture(read, *size)
        self.keys[href] = key
        return "![%s](picture:%s)" % (caption, key)

    # Text.

    def _inline(self, element: ET.Element, spans: List[Span], bold=False, italic=False,
                strike=False, link=None) -> None:
        if element.text:
            spans.append(Span(element.text, bold, italic, strike, link))
        for child in element:
            name = _local(child.tag)
            if name == "a":
                href = _href(child)
                if (child.get("type") or "") == "note" or href.startswith("#"):
                    label = "".join(child.itertext()).strip().strip("[]{}")
                    if label:
                        spans.append(Span("\\[%s\\]" % mdtext.escape(label), raw=True))
                else:
                    self._inline(child, spans, bold, italic, strike,
                                 href if re.match(r"(?i)^(https?|mailto|ftp):", href) else None)
            elif name == "strong":
                self._inline(child, spans, True, italic, strike, link)
            elif name == "emphasis":
                self._inline(child, spans, bold, True, strike, link)
            elif name == "strikethrough":
                self._inline(child, spans, bold, italic, True, link)
            elif name == "image":
                pass
            else:
                self._inline(child, spans, bold, italic, strike, link)
            if child.tail:
                spans.append(Span(child.tail, bold, italic, strike, link))

    def _text(self, element: ET.Element, **style) -> str:
        spans: List[Span] = []
        self._inline(element, spans, **style)
        return mdtext.spans(spans)

    def _title(self, element: ET.Element) -> str:
        parts = [self._text(p) for p in element if _local(p.tag) == "p"]
        return " ".join(p for p in parts if p)

    def _plain_title(self, element: ET.Element) -> str:
        return " ".join(" ".join("".join(p.itertext()).split()) for p in element if _local(p.tag) == "p")

    def _blocks(self, parent: ET.Element, out: List[str], depth: int, quote: bool = False) -> None:
        prefix = "> " if quote else ""
        for child in parent:
            name = _local(child.tag)
            if name == "title":
                title = self._plain_title(child)
                if title:
                    out.append(mdtext.heading(max(1, depth), mdtext.escape(title)))
            elif name == "section":
                self._blocks(child, out, min(depth + 1, 6))
            elif name == "p":
                text = self._text(child)
                if text:
                    out.append(prefix + (mdtext.paragraph(text) if not quote else text))
                for image in child.iter(_f("image")):
                    line = self._picture(image)
                    if line:
                        out.append(line)
            elif name == "subtitle":
                text = self._text(child, bold=True)
                if text:
                    out.append(prefix + text)
            elif name in ("epigraph", "cite"):
                self._blocks(child, out, depth, quote=True)
            elif name == "text-author":
                text = self._text(child, italic=True)
                if text:
                    out.append(prefix + text)
            elif name == "poem":
                self._poem(child, out, prefix)
            elif name == "image":
                line = self._picture(child)
                if line:
                    out.append(line)
            elif name == "table":
                out.append(self._table(child))
            elif name == "annotation":
                self._blocks(child, out, depth, quote=True)

    def _poem(self, poem: ET.Element, out: List[str], prefix: str) -> None:
        for part in poem:
            name = _local(part.tag)
            if name == "title":
                title = self._plain_title(part)
                if title:
                    out.append(prefix + "**%s**" % mdtext.escape(title))
            elif name == "epigraph":
                self._blocks(part, out, 6, quote=True)
            elif name == "stanza":
                for line in part:
                    kind = _local(line.tag)
                    if kind in ("v", "subtitle"):
                        text = self._text(line, bold=kind == "subtitle")
                        if text:
                            out.append(prefix + mdtext.line_start(text))
                    elif kind == "title":
                        title = self._title(line)
                        if title:
                            out.append(prefix + title)
            elif name in ("text-author", "date"):
                text = self._text(part, italic=True)
                if text:
                    out.append(prefix + text)

    def _table(self, table: ET.Element) -> str:
        rows: List[List[str]] = []
        head = False
        for index, row in enumerate(table.findall(_f("tr"))):
            cells = []
            for cell in row:
                if _local(cell.tag) not in ("td", "th"):
                    continue
                if index == 0 and _local(cell.tag) == "th":
                    head = True
                cells.append(mdtext.cell(self._text(cell)))
                span = cell.get("colspan")
                if span and span.isdigit():
                    cells.extend([""] * (int(span) - 1))
            rows.append(cells)
        return mdtext.table(rows, head and len(rows) > 1) if rows else ""

    # The book.

    def _meta(self) -> None:
        meta = self.result.meta
        info = self.root.find("%s/%s" % (_f("description"), _f("title-info")))
        if info is None:
            return

        def said(path):
            found = info.find(path)
            return " ".join("".join(found.itertext()).split()) if found is not None else ""

        meta["title"] = said(_f("book-title"))
        authors = []
        for author in info.findall(_f("author")):
            name = " ".join(x for x in (said_in(author, "first-name"), said_in(author, "middle-name"),
                                        said_in(author, "last-name")) if x)
            name = name or said_in(author, "nickname")
            if name:
                authors.append(name)
        meta["creator"] = ", ".join(authors)
        meta["language"] = said(_f("lang"))
        meta["date"] = said(_f("date"))
        meta["subject"] = ", ".join(" ".join(g.itertext()).strip() for g in info.findall(_f("genre")))
        annotation = info.find(_f("annotation"))
        if annotation is not None:
            meta["description"] = " ".join(" ".join(annotation.itertext()).split())
        publish = self.root.find("%s/%s" % (_f("description"), _f("publish-info")))
        if publish is not None:
            for name, key in (("publisher", "publisher"), ("year", "year")):
                found = publish.find(_f(name))
                if found is not None and found.text:
                    meta[key] = found.text.strip()
        for key in [k for k, v in meta.items() if not v]:
            del meta[key]
        self.cover = info.find("%s/%s" % (_f("coverpage"), _f("image")))

    def convert(self) -> Result:
        self._meta()
        result = self.result
        meta = result.meta
        out: List[str] = []
        if self.cover is not None:
            line = self._picture(self.cover)
            if line and line.startswith("!"):
                out.append(line)
                result.cover = result.pictures[line.rsplit(":", 1)[1].rstrip(")")]
        if meta.get("title"):
            out.append("**%s**" % mdtext.escape(meta["title"]))
        if meta.get("creator"):
            out.append("*%s*" % mdtext.escape(meta["creator"]))
        if meta.get("description"):
            out.append("> " + mdtext.escape(meta["description"]))

        for body in self.root.findall(_f("body")):
            if body.get("name") in ("notes", "comments"):
                title = body.find(_f("title"))
                out.append(mdtext.heading(1, mdtext.escape(
                    self._plain_title(title) if title is not None else self.tr("Notes"))))
                for section in body.findall(_f("section")):
                    head = section.find(_f("title"))
                    label = self._plain_title(head) if head is not None else ""
                    parts = [self._text(p) for p in section.iter(_f("p"))
                             if head is None or p not in list(head)]
                    text = " ".join(p for p in parts if p)
                    if text:
                        out.append(("\\[%s\\] " % mdtext.escape(label.strip("[]{}")) if label else "") + text)
                continue
            # A body with a title of its own has its sections one below it;
            # without one, the sections are the top.
            self._blocks(body, out, 1 if body.find(_f("title")) is not None else 0)

        result.markdown = "\n\n".join(b for b in out if b.strip()) + "\n"
        result.words = len(re.findall(r"\w+", re.sub(r"\(picture:[^)]*\)", "", result.markdown)))
        return result


def said_in(element: ET.Element, name: str) -> str:
    found = element.find(_f(name))
    return " ".join("".join(found.itertext()).split()) if found is not None else ""


def in_zip(head: bytes) -> bool:
    """Whether a zip's first member is a .fb2 — the way FictionBook is
    handed out, `book.fb2.zip`. Read from the local header, which is at the
    start of the file and names its member."""
    if not head.startswith(b"PK\x03\x04") or len(head) < 30:
        return False
    length = int.from_bytes(head[26:28], "little")
    name = head[30:30 + length].decode("utf-8", "replace").lower()
    return name.endswith(".fb2")


def cover(data: bytes, tr) -> Optional[bytes]:
    """Only the cover's bytes."""
    book = Book(parse(data), tr)
    book._meta()
    if book.cover is None:
        return None
    encoded = book.binaries.get(_href(book.cover).lstrip("#"))
    return base64.b64decode(re.sub(r"\s+", "", encoded)) if encoded else None


def convert(data: bytes, tr) -> Result:
    return Book(parse(data), tr).convert()
