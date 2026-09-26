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

"""Markdown written for the host's own reader, which is small on purpose.

Everything a converter hands over passes through here, so the rules are in one
place: what has to be escaped so that a Word file's asterisks stay asterisks,
how a run of formatting becomes marks the host reads, and how a table with
merged cells becomes the pipe table the host pins the head of.

**The host's reader is flat.** It reads `**…**`, `*…*`, `~~…~~` and `***…***`
and does not look inside them for more marks — so bold and struck through at
once cannot both be said, and this picks one rather than writing marks that
would show as characters.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence

#: What a backslash makes literal in the host's reader: every mark it knows.
_INLINE = re.compile(r"([\\`*_~\[\]|!])")

#: Openings that would make a paragraph into something else.
_BLOCK = re.compile(r"^(\s*)(#{1,6}\s|>|[-+*]\s|\d+[.)]\s|```|~~~|[-*_](\s*[-*_]){2,}\s*$)")

_SPACES = re.compile(r"[ \t ]+")


def escape(text: str) -> str:
    """[text] as characters, whatever marks are in it."""
    return _INLINE.sub(r"\\\1", text)


def line_start(text: str) -> str:
    """[text] made safe to begin a line with: a paragraph that happens to begin
    "1. " or "# " stays a paragraph."""
    found = _BLOCK.match(text)
    if not found:
        return text
    at = len(found.group(1))
    if text[at].isdigit():
        stop = at
        while stop < len(text) and text[stop].isdigit():
            stop += 1
        return text[:stop] + "\\" + text[stop:]
    return text[:at] + "\\" + text[at:]


def squeeze(text: str) -> str:
    """Runs of spaces and tabs as one space — a document lays text out with
    them, and a reading does not."""
    return _SPACES.sub(" ", text)


class Span:
    """A piece of a paragraph with one formatting."""

    __slots__ = ("text", "bold", "italic", "strike", "link", "raw")

    def __init__(self, text: str, bold: bool = False, italic: bool = False,
                 strike: bool = False, link: Optional[str] = None, raw: bool = False):
        self.text = text
        self.bold = bold
        self.italic = italic
        self.strike = strike
        self.link = link
        #: Already Markdown — a note's mark — and not to be escaped.
        self.raw = raw

    def key(self):
        return (self.bold, self.italic, self.strike, self.link, self.raw)


def _marks(span: Span) -> str:
    if span.strike and not span.bold and not span.italic:
        return "~~"
    if span.bold and span.italic:
        return "***"
    if span.bold:
        return "**"
    if span.italic:
        return "*"
    if span.strike:
        return "~~"
    return ""


def spans(pieces: Sequence[Span]) -> str:
    """A paragraph's pieces as one line of Markdown.

    Neighbours with the same formatting are joined first, so a word Word split
    into three runs is one bold word and not three. Spaces are kept outside
    the marks, where the host's reader expects them.
    """
    joined: List[Span] = []
    for piece in pieces:
        if not piece.text:
            continue
        if joined and joined[-1].key() == piece.key():
            joined[-1] = Span(joined[-1].text + piece.text, *piece.key()[:4], raw=piece.raw)
        else:
            joined.append(piece)

    out = []
    for piece in joined:
        text = squeeze(piece.text)
        if piece.raw:
            out.append(text)
            continue
        body = text.strip()
        if not body:
            out.append(" " if text else "")
            continue
        lead = " " if text[:1].isspace() else ""
        tail = " " if text[-1:].isspace() else ""
        written = escape(body)
        marks = _marks(piece)
        if marks:
            written = marks + written + marks
        if piece.link:
            written = "[%s](%s)" % (written if not marks else escape(body), _url(piece.link))
        out.append(lead + written + tail)
    return squeeze("".join(out)).strip()


def _url(url: str) -> str:
    """A link's address as the host's reader can find the end of it."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def heading(level: int, text: str) -> str:
    return "%s %s" % ("#" * max(1, min(6, level)), text)


#: A bullet typed by hand rather than made by the list: a list item all the same.
_TYPED_BULLET = re.compile(r"^[•·▪◦‣–]\s+")


def paragraph(text: str) -> str:
    """A paragraph that is only a paragraph — unless it begins with a bullet
    somebody typed, which reads as the list item it is meant to be."""
    typed = _TYPED_BULLET.match(text)
    if typed:
        return "- " + text[typed.end():]
    return line_start(text)


def table(rows: List[List[str]], header: bool) -> str:
    """A pipe table. ``rows`` are cells already written as Markdown.

    The host needs a head row; a table whose first row is not one gets an
    empty head rather than having its first row promoted, because a first row
    of data drawn as the head of the columns says something the file did not.
    """
    width = max((len(r) for r in rows), default=0)
    if width == 0:
        return ""
    rows = [r + [""] * (width - len(r)) for r in rows]
    if header:
        head, body = rows[0], rows[1:]
    else:
        head, body = [""] * width, rows
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def cell(text: str) -> str:
    """A cell's Markdown on one line: the host's table has no room for more."""
    return squeeze(text.replace("\n", " · ")).strip()
