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

"""Rich Text Format as Markdown.

RTF is text with control words in it — `\\b` turns bold on, `\\par` ends a
paragraph — and groups in braces that keep a change to themselves: the
formatting of a character *and of a paragraph* belongs to the group it was
set in, which is what lets Word write a list item's label inside a group that
resets everything and still have the item keep its own settings. Word,
WordPad, TextEdit and every mail program write it, each a little differently.

- **The characters.** A byte written as `\\'e0` means whatever the code page
  says: the document's `\\ansicpg`, or — how Word writes Russian — the charset
  of the font in force (`\\fcharset204` is 1251). `\\uN` is a Unicode
  character, followed by `\\ucN` fallback characters that are skipped.
- **What is not text.** A font table, a colour table, headers, footers, a
  list table and the rest are groups to step over; a group opened with `\\*`
  and not known here is stepped over too, which is what the format asks of a
  reader that does not know it.
- **Headings** are paragraphs in a style called `heading N` in the style sheet
  (English even in a Russian Word), or with `\\outlinelevelN`. A file with
  none — TextEdit's, WordPad's — gets its short bold paragraphs as headings,
  the same guess as for Word.
- **Lists.** Word writes each item's label as text in a `\\listtext` group
  (older writers in `\\pntext`), so the label is taken as written.
- **Tables** are paragraphs marked `\\intbl`, closed by `\\cell` and `\\row`.
- **Fields** are their result; a `HYPERLINK` field's result is a link.
- **Notes** (`\\footnote`) and **comments** (`\\annotation`, the author from
  `\\atnauthor`) are written at the end. **Pictures** in PNG or JPEG are
  drawn; a metafile is named, not drawn.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional

import mdtext
from docx import Result, _MAYBE_LONGEST, _Maybe
from mdtext import Span

from xverb import Picture

#: A font's charset as the code page its bytes are in.
_CHARSETS = {
    0: None, 1: None, 2: "symbol", 77: "mac_roman", 128: "cp932", 129: "cp949",
    130: "johab", 134: "gbk", 136: "cp950", 161: "cp1253", 162: "cp1254",
    163: "cp1258", 177: "cp1255", 178: "cp1256", 186: "cp1257", 204: "cp1251",
    222: "cp874", 238: "cp1250", 255: "cp437",
}

#: Groups that are never text, stepped over whole.
_SKIPPED = {
    "colortbl", "header", "headerl", "headerr", "headerf", "footer", "footerl",
    "footerr", "footerf", "listtable", "listoverridetable", "revtbl", "rsidtbl",
    "generator", "xmlnstbl", "themedata", "colorschememapping", "latentstyles",
    "datastore", "mmathPr", "pgdsctbl", "expandedcolortbl", "ftnsep", "ftnsepc",
    "aftnsep", "aftnsepc", "nonshppict", "objdata", "objclass", "userprops",
    "docvar", "bkmkstart", "bkmkend", "atnref", "atndate", "atntime", "atnicn",
    "atnparent", "pn", "shpinst", "sp", "sn", "sv", "xe", "tc", "template",
    "filetbl", "protusertbl", "wgrffmtfilter", "passwordhash", "blipuid",
    "comment", "hlinkbase", "falt", "panose", "fname", "picprop", "defchp",
    "defpap", "listpicture", "fchars", "lchars", "writereservation",
    # A list table's parts, met outside a list table in a file somebody
    # broke: its names would otherwise be read as text.
    "list", "listlevel", "listname", "leveltext", "levelnumbers",
    "listoverride", "lfolevel",
}

#: Control words that are characters.
_CHARACTERS = {
    "emdash": "—", "endash": "–", "bullet": "•", "lquote": "‘", "rquote": "’",
    "ldblquote": "“", "rdblquote": "”", "emspace": " ", "enspace": " ",
    "qmspace": " ", "tab": " ", "line": " ", "zwj": "", "zwnj": "",
    "ltrmark": "", "rtlmark": "",
}

_INFO = {"title": "title", "subject": "subject", "author": "creator",
         "keywords": "keywords", "doccomm": "description", "company": "Company",
         "operator": "lastModifiedBy"}

_TOKEN = re.compile(
    r"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?"   # a control word
    r"|\\'([0-9a-fA-F]{2})"                # a byte in the code page
    r"|\\([^a-zA-Z'])"                     # a control symbol
    r"|([{}])"
    r"|[\r\n]+"
    r"|([^\\{}\r\n]+)"
)

#: A heading style's name. English whatever the language of Word that wrote
#: it — but not every other writer knows that.
_HEADING_NAME = re.compile(r"^\s*(?:heading|заголовок)\s+(\d)\s*$", re.I)


class _Group:
    """What a brace keeps to itself: where its text goes, and how it looks."""

    __slots__ = ("dest", "bold", "italic", "strike", "hidden", "font", "uc",
                 "style", "outline", "listed", "level", "table", "number")

    def __init__(self, parent: Optional["_Group"] = None):
        if parent is None:
            self.dest = ""
            self.bold = self.italic = self.strike = self.hidden = False
            self.font = -1
            self.uc = 1
            self.style = -1
            self.outline = 0
            self.listed = False
            self.level = 0
            self.table = False
        else:
            for name in self.__slots__:
                if name != "number":
                    setattr(self, name, getattr(parent, name))
        #: A style's number while its definition is being read.
        self.number = -1

    def plain(self) -> None:
        self.bold = self.italic = self.strike = self.hidden = False

    def pard(self) -> None:
        self.style = -1
        self.outline = 0
        self.listed = False
        self.level = 0
        self.table = False


class _Flow:
    """Where text goes: the body, or a note or a comment being read."""

    def __init__(self):
        self.blocks: List[str] = []
        self.pieces: List[Span] = []
        self.cell: List[str] = []
        self.row: List[str] = []
        self.rows: List[List[str]] = []
        self.header = False
        self.row_header = False
        self.label = ""


class Reader:
    def __init__(self, raw: bytes, tr: Callable[..., str]):
        self.raw = raw
        self.tr = tr
        self.result = Result()
        self.codepage = "cp1252"
        self.fonts: Dict[int, Optional[str]] = {}
        self.default_font = -1
        self.styles: Dict[int, int] = {}
        self.meta: Dict[str, str] = {}
        self.notes: List[str] = []
        self.comments: List[str] = []
        self.author = ""
        self.headings = 0
        self.body = _Flow()
        self.flow = self.body
        self.flows: List[_Flow] = []
        self.link: Optional[str] = None
        self.after: List[str] = []
        # Text a destination gathers until its group closes.
        self.gathered: Dict[int, List[str]] = {}
        self.picture: Optional[dict] = None
        #: The groups that opened a note, a comment or a field. Their
        #: children inherit the destination, and only the opener's closing
        #: brace ends it.
        self.owners: set = set()

    # -- characters -------------------------------------------------------------

    def _decode(self, data: bytes, group: _Group) -> str:
        charset = self.fonts.get(group.font if group.font >= 0 else self.default_font)
        if charset == "symbol":
            return "".join("•" if b == 0xB7 else bytes([b]).decode("cp1252", "replace")
                           for b in data)
        try:
            return data.decode(charset or self.codepage, "replace")
        except LookupError:
            return data.decode("cp1252", "replace")

    # -- the walk -----------------------------------------------------------------

    def read(self) -> Result:
        text = self.raw.decode("latin-1")
        stack: List[_Group] = []
        group = _Group()
        pending = bytearray()
        skipping = 0
        first = False
        at = 0
        length = len(text)

        while at < length:
            found = _TOKEN.match(text, at)
            if found is None:
                at += 1
                continue
            at = found.end()
            word, param, byte, symbol, brace, plain = found.groups()

            if byte is not None:
                if skipping:
                    skipping -= 1
                else:
                    pending.append(int(byte, 16))
                continue
            if pending:
                self._text(self._decode(bytes(pending), group), group)
                pending = bytearray()

            if brace == "{":
                stack.append(group)
                group = _Group(group)
                first = True
                skipping = 0
                continue
            if brace == "}":
                self._close(group)
                group = stack.pop() if stack else _Group()
                first = False
                continue

            if plain is not None:
                if skipping:
                    drop = min(skipping, len(plain))
                    plain, skipping = plain[drop:], skipping - drop
                if plain:
                    self._text(plain, group)
                first = False
                continue

            starts, first = first, False
            if symbol is not None:
                if symbol == "*":
                    if starts:
                        group.dest = "*"
                elif symbol in "\\{}":
                    self._text(symbol, group)
                elif symbol == "~":
                    self._text(" ", group)
                elif symbol == "_":
                    self._text("-", group)
                elif symbol in "\n\r" and group.dest == "":
                    self._paragraph(group)
                continue

            value = int(param) if param is not None else None
            if word == "bin":
                at += max(0, value or 0)
                continue
            if word == "u" and value is not None:
                self._text(chr(value + 65536 if value < 0 else value), group)
                skipping = group.uc
                continue
            if word == "uc" and value is not None:
                group.uc = value
                continue
            if (starts or group.dest == "*") and self._destination(word, group):
                continue
            self._control(word, value, group)

        if pending:
            self._text(self._decode(bytes(pending), group), group)
        self._paragraph(group)
        self._table_end()
        return self._finish()

    # -- destinations -----------------------------------------------------------

    def _destination(self, word: str, group: _Group) -> bool:
        """A group that is about something other than the body's text."""
        starred = group.dest == "*"
        inside = group.dest
        if inside == "skip":
            # Nothing inside a group being stepped over comes back to life —
            # not even the {\pict} inside {\nonshppict}, which is the same
            # picture again as a metafile.
            return True
        if word in ("fonttbl", "stylesheet", "info", "fldinst", "listtext",
                    "atnauthor", "atnid"):
            group.dest = word
        elif word == "pntext":
            group.dest = "listtext"
        elif inside == "info" and word in _INFO:
            group.dest = "info:" + word
        elif inside == "info":
            group.dest = "skip"
        elif word in ("footnote", "annotation"):
            group.dest = word
            self.owners.add(id(group))
            # Its own flow; the paragraph it is in goes on after it.
            self.flows.append(self.flow)
            self.flow = _Flow()
        elif word == "fldrslt":
            group.dest = ""
        elif word == "field":
            group.dest = "field"
            self.owners.add(id(group))
        elif word == "pict":
            group.dest = "pict"
            self.picture = {"kind": "", "goalw": 0, "goalh": 0, "scalex": 100,
                            "scaley": 100, "hex": [], "group": id(group)}
        elif word in ("shppict", "shp", "shptxt", "result", "object"):
            group.dest = inside if inside not in ("*",) else ""
        elif word in _SKIPPED or starred:
            group.dest = "skip"
        else:
            return False
        if group.dest in ("listtext", "fldinst", "atnauthor", "atnid") or group.dest.startswith("info:"):
            self.gathered[id(group)] = []
        return True

    def _close(self, group: _Group) -> None:
        dest = group.dest
        gathered = self.gathered.pop(id(group), None)
        said = "".join(gathered) if gathered is not None else ""
        if dest.startswith("info:") and gathered is not None:
            self.meta[_INFO[dest[5:]]] = said.strip()
        elif dest in ("atnauthor", "atnid") and gathered is not None:
            if dest == "atnauthor" or not self.author:
                self.author = said.strip()
        elif dest == "fldinst" and gathered is not None:
            found = re.match(r'\s*HYPERLINK\s+"([^"]+)"(.*)', said, re.I | re.S)
            # A link to a place inside the document is its text: the reading
            # has no bookmarks to go to.
            self.link = found.group(1) if (found and "\\l" not in found.group(2)
                                           and not found.group(1).startswith("#")) else None
        elif dest == "field" and id(group) in self.owners:
            self.owners.discard(id(group))
            self.link = None
        elif dest == "listtext" and gathered is not None:
            self.flow.label = said
        elif dest == "pict" and self.picture is not None and self.picture["group"] == id(group):
            self._picture(self.picture)
            self.picture = None
        elif dest in ("footnote", "annotation") and id(group) in self.owners:
            self.owners.discard(id(group))
            self._paragraph(group)
            self._table_end()
            inner = self.flow
            self.flow = self.flows.pop() if self.flows else self.body
            flat = mdtext.cell(" · ".join(b for b in inner.blocks if b.strip()))
            if dest == "footnote":
                self.notes.append(flat)
                self.flow.pieces.append(Span("\\[%d\\]" % len(self.notes), raw=True))
            else:
                head = self.author
                self.comments.append((mdtext.escape(head) + ": " if head else "") + flat)
                self.flow.pieces.append(Span("\\[%s\\]" % mdtext.escape(
                    self.tr("comment {n}", {"n": len(self.comments)})), raw=True))
                self.author = ""

    # -- text and control words -----------------------------------------------------

    def _text(self, text: str, group: _Group) -> None:
        dest = group.dest
        if dest in ("", "field"):
            if not group.hidden:
                self.flow.pieces.append(
                    Span(text, group.bold, group.italic, group.strike, self.link))
        elif dest in ("footnote", "annotation"):
            if not group.hidden:
                self.flow.pieces.append(Span(text, group.bold, group.italic, group.strike))
        elif dest == "stylesheet":
            found = _HEADING_NAME.match(text.split(";")[0])
            if found and group.number >= 0 and group.number not in self.styles:
                self.styles[group.number] = int(found.group(1))
        elif dest == "pict":
            if self.picture is not None:
                self.picture["hex"].append(text)
        elif dest in ("listtext", "fldinst", "atnauthor", "atnid") or dest.startswith("info:"):
            # Gathered by the nearest group that asked to gather it: the text
            # may be in a group of its own inside that one.
            for key in reversed(list(self.gathered)):
                self.gathered[key].append(text)
                break

    def _control(self, word: str, value, group: _Group) -> None:
        dest = group.dest
        if dest == "fonttbl":
            if word == "f" and value is not None:
                group.font = value
            elif word == "fcharset" and value is not None:
                self.fonts[group.font] = _CHARSETS.get(value)
            return
        if dest == "stylesheet":
            if word == "s" and value is not None:
                group.number = value
            elif word == "outlinelevel" and value is not None and group.number >= 0:
                self.styles[group.number] = value + 1
            return
        if dest == "pict":
            self._picture_word(word, value)
            return

        on = value != 0
        if word == "ansicpg" and value:
            self.codepage = "cp%d" % value
        elif word == "mac":
            self.codepage = "mac_roman"
        elif word == "deff" and value is not None:
            self.default_font = value
        elif word == "f" and value is not None:
            group.font = value
        elif word == "b":
            group.bold = on
        elif word == "i":
            group.italic = on
        elif word in ("strike", "striked"):
            group.strike = on
        elif word == "v":
            group.hidden = on
        elif word == "plain":
            group.plain()
        elif word == "pard":
            group.pard()
        elif dest in ("", "footnote", "annotation", "field"):
            if word in ("par", "sect", "page"):
                self._paragraph(group)
            elif word == "s" and value is not None:
                group.style = value
            elif word == "outlinelevel" and value is not None:
                group.outline = value + 1
            elif word == "ls":
                group.listed = True
            elif word == "ilvl" and value is not None:
                group.level = value
            elif word == "intbl":
                group.table = True
            elif word in ("cell", "nestcell"):
                self._cell_end()
            elif word in ("row", "nestrow"):
                self._row_end()
            elif word == "trowd":
                self.flow.row_header = False
            elif word == "trhdr":
                self.flow.row_header = True
            elif word in _CHARACTERS:
                self._text(_CHARACTERS[word], group)
        elif word in _CHARACTERS:
            self._text(_CHARACTERS[word], group)

    # -- blocks --------------------------------------------------------------------

    def _line(self) -> tuple:
        pieces = self.flow.pieces
        self.flow.pieces = []
        return mdtext.spans(pieces), pieces

    def _paragraph(self, group: _Group) -> None:
        flow = self.flow
        if group.table:
            # A paragraph in a cell is a line of the cell.
            text, _ = self._line()
            if text:
                flow.cell.append(text)
            return
        self._table_end()
        text, pieces = self._line()
        label = flow.label.strip()
        flow.label = ""
        if text:
            flow.blocks.append(self._block(text, pieces, label, group))
        if flow is self.body and self.after:
            flow.blocks.extend(self.after)
            self.after = []

    def _block(self, text: str, pieces: List[Span], label: str, group: _Group) -> str:
        level = group.outline or self.styles.get(group.style, 0)
        if 0 < level < 10:
            self.headings += 1
            # A numbered heading keeps its number, as Word shows it; a bullet
            # in front of a heading says nothing.
            if label and any(c.isalnum() for c in label):
                text = mdtext.escape(label) + " " + text
            return mdtext.heading(min(level, 6), text)
        indent = "  " * group.level
        if label:
            if len(label) <= 2 and not any(c.isalnum() for c in label):
                return indent + "- " + text
            number = re.fullmatch(r"(\d+)[.)]?", label)
            if number:
                return indent + number.group(1) + ". " + text
            return indent + mdtext.line_start(mdtext.escape(label) + " " + text)
        if self.flow is self.body:
            plain = "".join(p.text for p in pieces if not p.raw)
            bold = all(p.bold or not p.text.strip() for p in pieces if not p.raw)
            if (bold and plain.strip() and len(plain.strip()) <= _MAYBE_LONGEST
                    and not plain.rstrip().endswith((".", ":", ";", ",", "!", "?"))):
                maybe = _Maybe(mdtext.paragraph(text))
                maybe.heading = mdtext.escape(mdtext.squeeze(plain).strip())
                return maybe
        return mdtext.paragraph(text)

    def _cell_end(self) -> None:
        flow = self.flow
        text, _ = self._line()
        if text:
            flow.cell.append(text)
        flow.row.append(mdtext.cell(" · ".join(flow.cell)))
        flow.cell = []

    def _row_end(self) -> None:
        flow = self.flow
        if flow.pieces or flow.cell:
            self._cell_end()
        if flow.row:
            if not flow.rows:
                flow.header = flow.row_header
            flow.rows.append(flow.row)
        flow.row = []

    def _table_end(self) -> None:
        flow = self.flow
        if flow.row or flow.cell:
            self._row_end()
        if flow.rows:
            flow.blocks.append(mdtext.table(flow.rows, flow.header and len(flow.rows) > 1))
        flow.rows = []
        flow.header = False

    # -- pictures --------------------------------------------------------------------

    def _picture_word(self, word: str, value) -> None:
        picture = self.picture
        if picture is None:
            return
        if word in ("pngblip", "jpegblip", "emfblip", "wmetafile", "macpict",
                    "dibitmap", "wbitmap"):
            picture["kind"] = word
        elif word in ("picwgoal", "pichgoal") and value:
            picture["goal" + word[3]] = value
        elif word in ("picscalex", "picscaley") and value:
            picture["scale" + word[-1]] = value

    def _picture(self, picture: dict) -> None:
        kind = picture["kind"]
        if kind not in ("pngblip", "jpegblip"):
            what = {"emfblip": "EMF", "wmetafile": "WMF", "macpict": "PICT"}.get(kind) \
                or self.tr("picture")
            self.after.append(mdtext.escape("[%s]" % self.tr("picture: {what}", {"what": what})))
            return
        try:
            data = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", "".join(picture["hex"])))
        except ValueError:
            return
        if not data:
            return
        # Twentieths of a point, at 96 pixels an inch: a pixel is fifteen.
        width = height = None
        if picture["goalw"] and picture["goalh"]:
            width = round(picture["goalw"] / 15 * picture["scalex"] / 100)
            height = round(picture["goalh"] / 15 * picture["scaley"] / 100)
        key = "p%d" % (len(self.result.pictures) + 1)
        self.result.pictures[key] = Picture(lambda data=data: data, width, height)
        self.after.append("![](picture:%s)" % key)

    # -- the end -----------------------------------------------------------------------

    def _finish(self) -> Result:
        out: List[str] = list(self.body.blocks) + self.after
        if self.notes:
            out.append(mdtext.heading(2, mdtext.escape(self.tr("Notes"))))
            out += ["\\[%d\\] %s" % (n, t) for n, t in enumerate(self.notes, 1)]
        if self.comments:
            out.append(mdtext.heading(2, mdtext.escape(self.tr("Comments"))))
            for number, text in enumerate(self.comments, 1):
                label = "**%s**" % mdtext.escape(self.tr("Comment {n}", {"n": number}))
                out.append("%s — %s" % (label, text))
        guessed = self.headings == 0
        out = [
            (mdtext.heading(1, b.heading) if guessed else str(b)) if isinstance(b, _Maybe) else b
            for b in out
        ]
        result = self.result
        result.guessed = guessed and any(b.startswith("# ") for b in out)
        result.markdown = _whole("\n\n".join(b for b in out if b.strip()) + "\n")
        result.comments = len(self.comments)
        result.words = len(re.findall(r"\w+", re.sub(r"\(picture:[^)]*\)", "", result.markdown)))
        result.meta = {k: _whole(v) for k, v in self.meta.items() if v}
        return result


def _whole(text: str) -> str:
    """[text] with its surrogate halves joined: RTF writes a character past
    U+FFFF as two `\\uN`, one half each, and a half on its own is replaced."""
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def convert(raw: bytes, tr: Callable[..., str]) -> Result:
    return Reader(raw, tr).read()
