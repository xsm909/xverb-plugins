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
        self.formats = self._formats()

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

    def _formats(self) -> List[numfmt.Format]:
        """The number format of every cell style, by style number."""
        root = self._xml("xl/styles.xml")
        if root is None:
            return []
        codes: Dict[int, str] = dict(numfmt.BUILTIN)
        styles: List[int] = []
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
                break
        made: Dict[int, numfmt.Format] = {}
        out = []
        for number in styles:
            if number not in made:
                made[number] = numfmt.Format(codes.get(number, "General"))
            out.append(made[number])
        return out

    def rows(self, index: int) -> List[list]:
        """Every row of sheet [index], as values. Gaps the file leaves out —
        rows nobody typed in, cells skipped over — come back empty, so a cell
        stands in the column its reference names."""
        part = self._open(self._parts[index])
        if part is None:
            return []
        general = numfmt.Format("General")
        comma = self.language.split("-")[0] in numfmt.COMMA_LANGUAGES
        out: List[list] = []
        row: list = []
        row_at = -1
        with part:
            ref = kind = style = None
            value: Optional[str] = None
            inline: List[str] = []
            for event, element in ET.iterparse(part, events=("start", "end")):
                tag = _local(element.tag)
                if event == "start":
                    if tag == "row":
                        wanted = row_number(element.get("r") or "")
                        row_at = wanted if wanted >= 0 else row_at + 1
                        row = []
                    elif tag == "c":
                        ref = element.get("r")
                        kind = element.get("t") or "n"
                        style = element.get("s")
                        value = None
                        inline = []
                    continue
                if tag == "v":
                    value = element.text
                elif tag == "t" and kind == "inlineStr":
                    inline.append(element.text or "")
                elif tag == "c":
                    column = column_index(ref) if ref else len(row)
                    cell = self._cell(kind, value, inline, style, general, comma)
                    if cell is not None and cell != "":
                        while len(row) < column:
                            row.append(None)
                        if len(row) == column:
                            row.append(cell)
                        else:
                            row[column] = cell
                    element.clear()
                elif tag == "row":
                    if row:
                        while len(out) < row_at:
                            out.append([])
                        out.append(row)
                        if self.max_rows and len(out) >= self.max_rows:
                            break
                    element.clear()
        return out

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
            return {"v": plain, "t": shown}
        if shown == str(plain) or (not comma and shown == repr(plain)):
            return plain
        return {"v": plain, "t": shown}


def is_workbook(zip_names: List[str]) -> bool:
    return any(n.lower() == "xl/workbook.xml" for n in zip_names)
