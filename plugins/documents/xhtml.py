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

"""A book's XHTML page as Markdown.

Read with the standard library's forgiving HTML parser rather than an XML one:
a chapter is meant to be XHTML and is often only HTML, with an `&nbsp;` no
XML parser knows or a tag left open, and a book that stops at its fourth
chapter because of that is worse than one read a little loosely.

**Tags, not classes.** What a heading, a list, a quotation or a table is comes
from the tag. Style sheets are not read. Where a book marks its headings only
with classes — which is what most converters from FB2 do — the book's own
table of contents says where the headings are: every place it points at gets
its heading, at the depth the contents give it, and the paragraphs after it
that only repeat that title are not written twice.

**Notes.** An EPUB 3 note is an element marked `epub:type="footnote"` (or
endnote, rearnote); it is taken out of the flow and written after a rule at
the end of the page, and its mark stays where it was. A note in EPUB 2 is just
a link to another page, and stays a mark; the page of notes reads as it is.
"""

from __future__ import annotations

import posixpath
import re
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote

import mdtext
from mdtext import Span

_SKIP = {"head", "script", "style", "template", "title", "noscript", "math"}
_BLOCK = {
    "p", "div", "section", "article", "header", "footer", "main", "figure", "figcaption",
    "address", "center", "dt", "dd", "caption", "body", "html", "hgroup", "details", "summary",
}
_HEADING = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BOLD = {"b", "strong"}
_ITALIC = {"i", "em", "cite", "var", "dfn"}
_STRIKE = {"s", "strike", "del"}
_NOTE_TYPES = {"footnote", "endnote", "rearnote", "note"}
_VOID = {"br", "hr", "img", "image", "meta", "link", "input", "col", "area", "base", "wbr", "source"}


def norm(text: str) -> str:
    """Letters and digits only, lower case: how a title is compared with the
    words that repeat it."""
    return re.sub(r"[\W_]+", "", text.lower())


class Page(HTMLParser):
    """One page. [picture] turns a picture's address into the Markdown line
    for it, or None to leave it out; [anchors] is the page's places the table
    of contents points at, id → (depth, title)."""

    def __init__(self, path: str, picture: Callable[[str, str, Optional[int], Optional[int]], Optional[str]],
                 anchors: Dict[str, Tuple[int, str]], start: Optional[Tuple[int, str]] = None):
        super().__init__(convert_charrefs=True)
        self.path = path
        self.picture = picture
        self.anchors = anchors
        self.out: List[str] = []
        self.spans: List[Span] = []
        self.skip = 0
        self.bold = self.italic = self.strike = 0
        self.links: List[Optional[str]] = []
        self.pre = 0
        self.quote = 0
        self.heading = 0
        self.lists: List[List] = []  # [kind, counter]
        self.item: Optional[str] = None
        self.table: Optional[List[List[str]]] = None
        self.table_head = False
        self.row: Optional[List[str]] = None
        self.cell: Optional[List[str]] = None
        self.note: Optional[Tuple[str, List[str], List[Span], int]] = None
        self.notes: List[Tuple[str, str]] = []
        self.stack: List[str] = []
        self.swallow: Optional[str] = None
        self.headings = 0
        if start:
            self._toc_heading(*start)

    # Writing out.

    def _toc_heading(self, depth: int, title: str) -> None:
        self.flush()
        self.out.append(mdtext.heading(depth, mdtext.escape(title)))
        self.headings += 1
        self.swallow = norm(title)

    def flush(self) -> None:
        spans, self.spans = self.spans, []
        if self.pre:
            return
        text = mdtext.spans(spans)
        if not text:
            return
        plain = norm("".join(s.text for s in spans if not s.raw))
        if self.swallow is not None:
            if plain and self.swallow.startswith(plain):
                self.swallow = self.swallow[len(plain):]
                if not self.swallow:
                    self.swallow = None
                return
            self.swallow = None
        self._emit(text)

    def _emit(self, text: str) -> None:
        if self.note is not None:
            self.note[1].append(text)
            return
        if self.cell is not None:
            self.cell.append(text)
            return
        if self.heading:
            self.out.append(mdtext.heading(self.heading, text))
            self.headings += 1
            return
        if self.item is not None:
            self.out.append(self.item + text)
            self.item = "  " * len(self.lists) + "  "
            return
        if self.quote:
            self.out.append("> " + text)
            return
        self.out.append(mdtext.paragraph(text))

    def _block(self, line: str) -> None:
        """A line that is a block of its own: a picture, a rule."""
        self.flush()
        if self.note is not None or self.cell is not None:
            return
        self.out.append(line)

    # Parsing.

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in _VOID:
            self.stack.append(tag)
        if self.skip:
            if tag not in _VOID:
                self.skip += 1
            return
        if tag in _SKIP:
            self.skip = 1
            return
        kind = (attrs.get("epub:type") or "").split()
        anchor = attrs.get("id") or attrs.get("name")
        if anchor and anchor in self.anchors and self.note is None:
            self._toc_heading(*self.anchors[anchor])
        if tag == "nav" and "toc" in kind:
            self.skip = 1
            return
        if (tag == "aside" or tag in _BLOCK) and _NOTE_TYPES.intersection(kind):
            self.flush()
            self.note = (anchor or "", [], self.spans, len(self.stack))
            self.spans = []
            return

        if tag in _HEADING:
            self.flush()
            self.heading = _HEADING[tag]
        elif tag in _BLOCK:
            self.flush()
        elif tag == "blockquote":
            self.flush()
            self.quote += 1
        elif tag == "pre":
            self.flush()
            self.pre += 1
            self.spans = []
        elif tag in ("ul", "ol"):
            self.flush()
            start = attrs.get("start")
            self.lists.append([tag, int(start) - 1 if start and start.isdigit() else 0])
        elif tag == "li":
            self.flush()
            if self.lists:
                kind_, count = self.lists[-1]
                self.lists[-1][1] = count + 1
                mark = "%d. " % (count + 1) if kind_ == "ol" else "- "
                self.item = "  " * (len(self.lists) - 1) + mark
            else:
                self.item = "- "
        elif tag == "table":
            self.flush()
            self.table, self.table_head = [], False
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.flush()
            self.cell = []
            if tag == "th" and not self.table:
                self.table_head = True
        elif tag == "br":
            if self.pre:
                self.spans.append(Span("\n"))
            else:
                self.flush()
        elif tag == "hr":
            self._block("---")
        elif tag in ("img", "image"):
            src = attrs.get("src") or attrs.get("xlink:href") or attrs.get("href") or ""
            line = self.picture(self._resolve(src), attrs.get("alt") or attrs.get("title") or "",
                                _int(attrs.get("width")), _int(attrs.get("height")))
            if line:
                if self.cell is not None or self.note is not None:
                    self.spans.append(Span(line if line.startswith("\\[") else "", raw=True))
                else:
                    self._block(line)
        elif tag in _BOLD:
            self.bold += 1
        elif tag in _ITALIC:
            self.italic += 1
        elif tag in _STRIKE:
            self.strike += 1
        elif tag == "a":
            href = attrs.get("href") or ""
            if "noteref" in kind:
                self.links.append("\0note")
            elif re.match(r"(?i)^(https?|mailto|ftp):", href):
                self.links.append(href)
            else:
                self.links.append(None)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        # Close what the page forgot to close, up to this tag.
        if tag not in self.stack:
            return
        while self.stack:
            open_tag = self.stack.pop()
            self._close(open_tag)
            if open_tag == tag:
                break

    def _close(self, tag):
        if self.skip:
            self.skip -= 1
            return
        if self.note is not None and len(self.stack) < self.note[3]:
            self.flush()
            anchor, parts, saved, _ = self.note
            self.notes.append((anchor, " ".join(parts)))
            self.note = None
            self.spans = saved
            return
        if tag in _HEADING:
            self.flush()
            self.heading = 0
        elif tag in _BLOCK:
            self.flush()
        elif tag == "blockquote":
            self.flush()
            self.quote = max(0, self.quote - 1)
        elif tag == "pre":
            code = "".join(s.text for s in self.spans)
            self.spans = []
            self.pre = max(0, self.pre - 1)
            if code.strip():
                self._block("```\n%s\n```" % code.strip("\n"))
        elif tag in ("ul", "ol"):
            self.flush()
            if self.lists:
                self.lists.pop()
            self.item = None
        elif tag == "li":
            self.flush()
            self.item = None
        elif tag in ("td", "th") and self.cell is not None:
            self.flush()
            if self.row is not None:
                self.row.append(mdtext.cell(" · ".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.table is not None:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            table = self.table
            self.table = None
            if table:
                self.out.append(mdtext.table(table, self.table_head and len(table) > 1))
        elif tag in _BOLD:
            self.bold = max(0, self.bold - 1)
        elif tag in _ITALIC:
            self.italic = max(0, self.italic - 1)
        elif tag in _STRIKE:
            self.strike = max(0, self.strike - 1)
        elif tag == "a" and self.links:
            self.links.pop()

    def handle_data(self, data):
        if self.skip or not data:
            return
        if self.pre:
            self.spans.append(Span(data))
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip() and not self.spans:
            return
        link = self.links[-1] if self.links else None
        if link == "\0note":
            label = text.strip().strip("[]")
            if label:
                self.spans.append(Span("\\[%s\\]" % mdtext.escape(label), raw=True))
            return
        self.spans.append(Span(text, self.bold > 0, self.italic > 0, self.strike > 0, link))

    def _resolve(self, src: str) -> str:
        if not src or src.startswith("data:") or re.match(r"(?i)^[a-z]+://", src):
            return src
        src = unquote(src.split("#", 1)[0])
        return posixpath.normpath(posixpath.join(posixpath.dirname(self.path), src))

    def finish(self) -> List[str]:
        self.close()
        while self.stack:
            self._close(self.stack.pop())
        self.flush()
        if self.notes:
            self.out.append("---")
            for anchor, text in self.notes:
                if text:
                    self.out.append(text)
        return self.out


def _int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    found = re.match(r"\s*(\d+)\s*(px)?\s*$", value)
    return int(found.group(1)) if found else None
