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

"""An Excel binary workbook — `.xlsb` — read with the standard library.

The same zip and the same parts as an .xlsx, and the same relationships file
in XML; but the workbook, the strings, the styles and the sheets are *.bin*,
runs of BIFF12 records. A record starts with its type and its length, each
written seven bits a byte for as many bytes as it takes; text is a count of
UTF-16 characters and then the characters.
"""

from __future__ import annotations

import io
import posixpath
import struct
import zipfile
from typing import Callable, Dict, List, Optional
from xml.etree import ElementTree as ET

import numfmt

# The records this reads, by number.
ROW, BLANK, RK, ERROR, BOOL, REAL, ST, ISST = 0, 1, 2, 3, 4, 5, 6, 7
FMLA_STRING, FMLA_NUM, FMLA_BOOL, FMLA_ERROR = 8, 9, 10, 11
SST_ITEM, FONT, FMT, XF = 19, 43, 44, 47
WB_PROP, BUNDLE_SH = 153, 156
BEGIN_FONTS, BEGIN_CELL_XFS, END_CELL_XFS = 611, 617, 618

_ERRORS = {0x00: "#NULL!", 0x07: "#DIV/0!", 0x0F: "#VALUE!", 0x17: "#REF!",
           0x1D: "#NAME?", 0x24: "#NUM!", 0x2A: "#N/A", 0x2B: "#GETTING_DATA"}


def records(data: bytes):
    """(type, body) for every record in [data]."""
    at = 0
    n = len(data)
    while at < n:
        kind = 0
        for shift in (0, 7):
            if at >= n:
                return
            byte = data[at]
            at += 1
            kind |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
        size = 0
        for shift in (0, 7, 14, 21):
            if at >= n:
                return
            byte = data[at]
            at += 1
            size |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
        yield kind, data[at:at + size]
        at += size


def wide(data: bytes, at: int):
    """An XLWideString at [at]: the text, and where it ends."""
    count = struct.unpack_from("<I", data, at)[0]
    if count == 0xFFFFFFFF:
        return None, at + 4
    end = at + 4 + count * 2
    return data[at + 4:end].decode("utf-16-le", "replace"), end


def _rk(value: int) -> float:
    number = float(value >> 2) if value & 2 else \
        struct.unpack("<d", struct.pack("<Q", (value & 0xFFFFFFFC) << 32))[0]
    if value & 2 and value & 0x80000000:
        number = float((value >> 2) - (1 << 30))
    return number / 100 if value & 1 else number


def is_workbook(names: List[str]) -> bool:
    return any(n.lower() == "xl/workbook.bin" for n in names)


class Workbook:
    """The parts of a binary workbook that say how to read its sheets."""

    def __init__(self, raw: bytes, language: str = "en", max_rows: int = 0):
        self.zip = zipfile.ZipFile(io.BytesIO(raw))
        self._names = {n.lower(): n for n in self.zip.namelist()}
        self.language = language
        self.max_rows = max_rows
        self.date1904 = False
        rels = self._rels("xl/workbook.bin")
        self.titles: List[str] = []
        self._parts: List[str] = []
        for kind, body in records(self._read("xl/workbook.bin") or b""):
            if kind == WB_PROP and len(body) >= 4:
                self.date1904 = bool(struct.unpack_from("<I", body, 0)[0] & 1)
            elif kind == BUNDLE_SH and len(body) >= 12:
                rel, at = wide(body, 8)
                name, _ = wide(body, at)
                target = rels.get(rel or "")
                if target:
                    self.titles.append(name or "Sheet%d" % (len(self.titles) + 1))
                    self._parts.append(target)
        self.strings = [
            wide(body, 1)[0] or ""
            for kind, body in records(self._read("xl/sharedStrings.bin") or b"")
            if kind == SST_ITEM and len(body) >= 5
        ]
        self.formats, self.bold = self._styles()

    def _read(self, name: str) -> Optional[bytes]:
        real = self._names.get(name.lower())
        return None if real is None else self.zip.read(real)

    def _rels(self, owner: str) -> Dict[str, str]:
        folder, base = posixpath.split(owner)
        raw = self._read(posixpath.join(folder, "_rels", base + ".rels"))
        out: Dict[str, str] = {}
        if raw is None:
            return out
        for element in ET.fromstring(raw):
            target = element.get("Target") or ""
            path = target.lstrip("/") if target.startswith("/") else \
                posixpath.normpath(posixpath.join(folder, target))
            out[element.get("Id") or ""] = path
        return out

    def _styles(self):
        codes: Dict[int, str] = dict(numfmt.BUILTIN)
        fonts_bold: List[bool] = []
        styles: List[tuple] = []
        in_cell_xfs = False
        for kind, body in records(self._read("xl/styles.bin") or b""):
            if kind == FMT and len(body) >= 6:
                number = struct.unpack_from("<H", body, 0)[0]
                codes[number] = wide(body, 2)[0] or ""
            elif kind == FONT and len(body) >= 6:
                fonts_bold.append(struct.unpack_from("<H", body, 4)[0] >= 700)
            elif kind == BEGIN_CELL_XFS:
                in_cell_xfs = True
            elif kind == END_CELL_XFS:
                in_cell_xfs = False
            elif kind == XF and in_cell_xfs and len(body) >= 6:
                _, number, font = struct.unpack_from("<HHH", body, 0)
                styles.append((number, font))
        made: Dict[int, numfmt.Format] = {}
        formats = []
        bold = []
        for number, font in styles:
            if number not in made:
                made[number] = numfmt.Format(codes.get(number, "General"))
            formats.append(made[number])
            bold.append(0 <= font < len(fonts_bold) and fonts_bold[font])
        return formats, bold

    def rows(self, index: int) -> List[list]:
        out: List[list] = []
        self.fill(index, out)
        return out

    def fill(self, index: int, out: List[list],
             stop: Optional[Callable[[], bool]] = None) -> None:
        """Reads sheet [index] into [out], a row at a time."""
        data = self._read(self._parts[index]) or b""
        general = numfmt.Format("General")
        comma = self.language.split("-")[0] in numfmt.COMMA_LANGUAGES
        row: list = []
        row_at = -1
        seen = 0
        for kind, body in records(data):
            if kind == ROW and len(body) >= 4:
                if row:
                    self._put(out, row_at, row)
                    if self.max_rows and len(out) >= self.max_rows:
                        return
                row = []
                row_at = struct.unpack_from("<I", body, 0)[0]
                seen += 1
                if stop is not None and seen % 2048 == 0 and stop():
                    return
                continue
            if kind > FMLA_ERROR or kind == BLANK or len(body) < 8:
                continue
            column, style = struct.unpack_from("<II", body, 0)
            style &= 0xFFFFFF
            value = self._value(kind, body, style, general, comma)
            if value is None or value == "":
                continue
            if style < len(self.bold) and self.bold[style]:
                value = dict(value, r="strong") if isinstance(value, dict) and "r" not in value \
                    else value if isinstance(value, dict) else {"v": value, "r": "strong"}
            if len(row) < column:
                row.extend([None] * (column - len(row)))
            if len(row) == column:
                row.append(value)
            else:
                row[column] = value
        if row:
            self._put(out, row_at, row)

    @staticmethod
    def _put(out: List[list], at: int, row: list) -> None:
        if len(out) < at:
            out.extend([] for _ in range(at - len(out)))
        out.append(row)

    def _value(self, kind: int, body: bytes, style: int, general, comma: bool):
        if kind in (ST, FMLA_STRING):
            return wide(body, 8)[0]
        if kind == ISST:
            at = struct.unpack_from("<I", body, 8)[0]
            return self.strings[at] if at < len(self.strings) else ""
        if kind in (BOOL, FMLA_BOOL):
            return bool(body[8])
        if kind in (ERROR, FMLA_ERROR):
            text = _ERRORS.get(body[8], "#ERROR")
            return {"v": text, "t": text, "r": "error"}
        if kind == RK:
            number = _rk(struct.unpack_from("<I", body, 8)[0])
        elif kind in (REAL, FMLA_NUM) and len(body) >= 16:
            number = struct.unpack_from("<d", body, 8)[0]
        else:
            return None
        fmt = self.formats[style] if style < len(self.formats) else general
        plain = int(number) if number.is_integer() and abs(number) < 1e15 else number
        shown = numfmt.show(number, fmt, self.language, self.date1904)
        if fmt.is_date:
            return {"v": shown, "t": shown}
        if shown == str(plain) or (not comma and shown == repr(plain)):
            return plain
        return {"v": plain, "t": shown}
