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

"""An Office Open XML workbook — `.xlsx`, `.xlsm`, `.xltx` — read with the
standard library.

A workbook is a zip of XML: `xl/workbook.xml` names the sheets, the
relationships file says which part each one is, `xl/sharedStrings.xml` holds
every piece of text once, and `xl/styles.xml` says which number format each
cell's style number means. A sheet is read only when somebody turns to it,
and read as a stream, so a large one is never a large tree in memory.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from typing import Callable, Dict, List, Optional
from xml.etree import ElementTree as ET
from xml.parsers import expat

import numfmt


def _local(tag: str) -> str:
    """A tag without its namespace. Transitional and Strict OOXML name the
    same elements in two namespaces, and a reader that cared which would read
    half the files there are."""
    return tag.rsplit("}", 1)[-1]


def _attr(element, name: str) -> Optional[str]:
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return None


def column_index(ref: str) -> int:
    """`B12` -> 1: the letters of a cell reference as a column from zero."""
    n = 0
    for ch in ref:
        if "A" <= ch <= "Z":
            n = n * 26 + (ord(ch) - 64)
        elif "a" <= ch <= "z":
            n = n * 26 + (ord(ch) - 96)
        else:
            break
    return n - 1


def row_number(ref: str) -> int:
    digits = "".join(ch for ch in ref if ch.isdigit())
    return int(digits) - 1 if digits else -1


class Workbook:
    """The parts of a workbook that say how to read its sheets."""

    def __init__(self, raw: bytes, language: str = "en", max_rows: int = 0):
        self.zip = zipfile.ZipFile(io.BytesIO(raw))
        self.language = language
        self.max_rows = max_rows
        names = {n.lower(): n for n in self.zip.namelist()}
        self._names = names
        book = self._xml("xl/workbook.xml")
        if book is None:
            raise ValueError("This zip holds no workbook.")

        rels = self._rels("xl/workbook.xml")
        self.date1904 = False
        self.titles: List[str] = []
        self._parts: List[str] = []
        for element in book.iter():
            tag = _local(element.tag)
            if tag == "workbookPr":
                self.date1904 = (_attr(element, "date1904") or "0") in ("1", "true")
            elif tag == "sheet":
                target = rels.get(_attr(element, "id") or "")
                if target is None:
                    continue
                self.titles.append(_attr(element, "name") or "Sheet%d" % (len(self.titles) + 1))
                self._parts.append(target)

        self.strings = self._shared_strings()
        self.formats, self.bold = self._formats()

    def _open(self, name: str):
        real = self._names.get(name.lower())
        return None if real is None else self.zip.open(real)

    def _xml(self, name: str):
        part = self._open(name)
        if part is None:
            return None
        with part:
            return ET.parse(part).getroot()

    def _rels(self, owner: str) -> Dict[str, str]:
        folder, base = posixpath.split(owner)
        root = self._xml(posixpath.join(folder, "_rels", base + ".rels"))
        out: Dict[str, str] = {}
        if root is None:
            return out
        for element in root:
            target = element.get("Target") or ""
            if target.startswith("/"):
                path = target.lstrip("/")
            else:
                path = posixpath.normpath(posixpath.join(folder, target))
            out[element.get("Id") or ""] = path
        return out

    def _shared_strings(self) -> List[str]:
        part = self._open("xl/sharedStrings.xml")
        if part is None:
            return []
        out: List[str] = []
        with part:
            pieces: List[str] = []
            skip = 0
            for event, element in ET.iterparse(part, events=("start", "end")):
                tag = _local(element.tag)
                if event == "start":
                    # Phonetic guides ride along with Japanese text and are
                    # not part of what the cell says.
                    if tag in ("rPh", "phoneticPr"):
                        skip += 1
                    continue
                if tag in ("rPh", "phoneticPr"):
                    skip -= 1
                elif tag == "t" and not skip:
                    pieces.append(element.text or "")
                elif tag == "si":
                    out.append("".join(pieces))
                    pieces = []
                    element.clear()
        return out

    def _formats(self):
        """The number format of every cell style, by style number — and which
        styles set their text in bold, which is how a sheet says "heading" or
        "total" without a word for it."""
        root = self._xml("xl/styles.xml")
        if root is None:
            return [], []
        codes: Dict[int, str] = dict(numfmt.BUILTIN)
        styles: List[int] = []
        fonts_bold: List[bool] = []
        style_fonts: List[int] = []
        for element in root.iter():
            if _local(element.tag) == "fonts":
                for font in element:
                    bold = False
                    for part in font:
                        if _local(part.tag) == "b":
                            bold = (part.get("val") or "1") not in ("0", "false")
                    fonts_bold.append(bold)
                break
        for element in root.iter():
            tag = _local(element.tag)
            if tag == "numFmt":
                try:
                    codes[int(element.get("numFmtId") or -1)] = element.get("formatCode") or ""
                except ValueError:
                    pass
        for element in root.iter():
            if _local(element.tag) == "cellXfs":
                for xf in element:
                    try:
                        styles.append(int(xf.get("numFmtId") or 0))
                    except ValueError:
                        styles.append(0)
                    try:
                        style_fonts.append(int(xf.get("fontId") or 0))
                    except ValueError:
                        style_fonts.append(0)
                break
        made: Dict[int, numfmt.Format] = {}
        out = []
        for number in styles:
            if number not in made:
                made[number] = numfmt.Format(codes.get(number, "General"))
            out.append(made[number])
        bold = [0 <= f < len(fonts_bold) and fonts_bold[f] for f in style_fonts]
        return out, bold

    def rows(self, index: int) -> List[list]:
        """Every row of sheet [index], as values. Gaps the file leaves out —
        rows nobody typed in, cells skipped over — come back empty, so a cell
        stands in the column its reference names."""
        out: List[list] = []
        self.fill(index, out)
        return out

    def fill(self, index: int, out: List[list],
             stop: Optional[Callable[[], bool]] = None) -> None:
        """Reads sheet [index] into [out], a row at a time as the file goes.

        **Expat, not a tree.** ElementTree made an object of every `<c>` and
        `<v>` and handed each one over twice; on a sheet of two million cells
        that was seven seconds before anything could be shown. Expat calls
        three functions and keeps nothing, and the rows are appended to [out]
        as they are finished — so whoever holds the list can show the first
        of them while the rest are still being read. [stop] is asked between
        pieces of the file, for a reading nobody wants any more.
        """
        part = self._open(self._parts[index])
        if part is None:
            return
        general = numfmt.Format("General")
        comma = self.language.split("-")[0] in numfmt.COMMA_LANGUAGES
        plain_styles = self._plain_styles()
        max_rows = self.max_rows
        strings = self.strings
        cell_of = self._cell
        bolden = self._bolden

        # The parser's state, in locals the handlers close over.
        row: list = []
        state = {"row_at": -1, "ref": None, "kind": "n", "style": None,
                 "text": False, "inline": False}
        value_parts: List[str] = []
        inline: List[str] = []
        done = [False]

        def start(name, attrs):
            if ":" in name:
                name = name.rsplit(":", 1)[1]
            if name == "c":
                state["ref"] = attrs.get("r")
                state["kind"] = attrs.get("t") or "n"
                state["style"] = attrs.get("s")
                value_parts.clear()
                inline.clear()
            elif name == "v":
                value_parts.clear()
                state["text"] = True
            elif name == "t" and state["kind"] == "inlineStr":
                state["inline"] = True
            elif name == "row":
                wanted = row_number(attrs.get("r") or "")
                state["row_at"] = wanted if wanted >= 0 else state["row_at"] + 1
                row.clear()
            elif name in ("rPh", "phoneticPr"):
                state["inline"] = False

        def end(name):
            if ":" in name:
                name = name.rsplit(":", 1)[1]
            if name == "v":
                state["text"] = False
            elif name == "t":
                state["inline"] = False
            elif name == "c":
                kind = state["kind"]
                style = state["style"]
                value = "".join(value_parts) if value_parts else None
                # The common case first: a number with no format of its own.
                if kind == "n" and value is not None and (style is None or style in plain_styles):
                    try:
                        number = float(value)
                    except ValueError:
                        cell = value
                    else:
                        if number.is_integer() and abs(number) < 1e15:
                            cell = int(number)
                        elif comma:
                            cell = {"v": number, "t": numfmt.general(number, True)}
                        else:
                            cell = number
                elif kind == "s" and value is not None:
                    try:
                        cell = strings[int(value)]
                    except (ValueError, IndexError):
                        cell = ""
                    if style is not None:
                        cell = bolden(cell, style)
                else:
                    cell = cell_of(kind, value, inline, style, general, comma)
                    if cell is not None and cell != "" and style is not None:
                        cell = bolden(cell, style)
                if cell is None or cell == "":
                    return
                ref = state["ref"]
                column = column_index(ref) if ref else len(row)
                if len(row) < column:
                    row.extend([None] * (column - len(row)))
                if len(row) == column:
                    row.append(cell)
                else:
                    row[column] = cell
            elif name == "row":
                if row:
                    at = state["row_at"]
                    if len(out) < at:
                        out.extend([] for _ in range(at - len(out)))
                    out.append(list(row))
                    if max_rows and len(out) >= max_rows:
                        done[0] = True

        def text(data):
            if state["text"]:
                value_parts.append(data)
            elif state["inline"]:
                inline.append(data)

        parser = expat.ParserCreate()
        parser.buffer_text = True
        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.CharacterDataHandler = text
        with part:
            while not done[0]:
                if stop is not None and stop():
                    return
                piece = part.read(1 << 18)
                if not piece:
                    parser.Parse(b"", True)
                    break
                parser.Parse(piece, False)
        if max_rows and len(out) > max_rows:
            del out[max_rows:]

    def _plain_styles(self) -> set:
        """Style numbers whose number format is General and whose text is not
        bold — where a number is only a number."""
        return {
            str(i) for i, fmt in enumerate(self.formats)
            if fmt.code == "General" and not (i < len(self.bold) and self.bold[i])
        }

    def _bolden(self, cell, style):
        try:
            if not self.bold[int(style)]:
                return cell
        except (ValueError, IndexError):
            return cell
        if isinstance(cell, dict):
            if "r" not in cell:
                cell = dict(cell, r="strong")
            return cell
        return {"v": cell, "r": "strong"}

    def _cell(self, kind, value, inline, style, general, comma):
        if kind == "inlineStr":
            return "".join(inline)
        if value is None:
            return None
        if kind == "s":
            try:
                return self.strings[int(value)]
            except (ValueError, IndexError):
                return ""
        if kind in ("str", "e"):
            return {"v": value, "t": value, "r": "error"} if kind == "e" else value
        if kind == "b":
            return value.strip() in ("1", "true")
        if kind == "d":
            # An ISO date written out, which some writers use instead of a
            # day number.
            return {"v": value, "t": value.replace("T", " ").split(".")[0]}
        try:
            number = float(value)
        except ValueError:
            return value
        fmt = general
        if style is not None:
            try:
                fmt = self.formats[int(style)]
            except (ValueError, IndexError):
                pass
        shown = numfmt.show(number, fmt, self.language, self.date1904)
        plain = int(number) if number.is_integer() and abs(number) < 1e15 else number
        if fmt.is_date:
            # A date's value is its text, not its day number: copied out as
            # JSON, 46266 would mean nothing to anybody.
            return {"v": shown, "t": shown}
        if shown == str(plain) or (not comma and shown == repr(plain)):
            return plain
        return {"v": plain, "t": shown}


def is_workbook(zip_names: List[str]) -> bool:
    return any(n.lower() == "xl/workbook.xml" for n in zip_names)
