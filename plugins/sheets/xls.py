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

"""The old binary `.xls` — Excel 97 to 2003, BIFF8 — read with the standard
library.

Two layers. The file is a **compound document**, a small FAT file system in
512-byte sectors, and the workbook is one stream inside it called `Workbook`.
That stream is a run of **records** — a two-byte type, a two-byte length, the
data — and a sheet is the records between its BOF and its EOF.

Only what a reader sees is read: the sheet names, the shared strings, the
number formats that make a number a date, and the cells — text, numbers, the
compressed "RK" numbers, booleans, errors, and the value a formula last came
to. Formulas themselves, charts, pictures and macros are left where they are.

**The trap in this format is the string table.** It is longer than a record may
be, so it goes on in CONTINUE records — and a string cut in two by that
boundary starts its second half with a fresh flag byte saying whether *that*
half is one byte a character or two. A reader that forgets it reads the rest
of the table shifted by one.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

import numfmt

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_FREE = 0xFFFFFFFF
_END = 0xFFFFFFFE


class XlsError(ValueError):
    """A sentence about why the file cannot be read."""


# -- the compound document ------------------------------------------------


class Compound:
    """The streams inside a compound document, by name."""

    def __init__(self, raw: bytes):
        if raw[:8] != MAGIC:
            raise XlsError("This is not an Excel 97–2003 file.")
        self.raw = raw
        shift = struct.unpack_from("<H", raw, 0x1E)[0]
        mini_shift = struct.unpack_from("<H", raw, 0x20)[0]
        self.sector = 1 << shift
        self.mini_sector = 1 << mini_shift
        fat_sectors = struct.unpack_from("<I", raw, 0x2C)[0]
        directory_start = struct.unpack_from("<I", raw, 0x30)[0]
        self.cutoff = struct.unpack_from("<I", raw, 0x38)[0]
        minifat_start = struct.unpack_from("<I", raw, 0x3C)[0]
        difat_start = struct.unpack_from("<I", raw, 0x44)[0]
        difat_count = struct.unpack_from("<I", raw, 0x48)[0]

        # Where the FAT itself is: 109 sectors named in the header, and more
        # in a chain of DIFAT sectors for a file too big for that.
        fat_at = [s for s in struct.unpack_from("<109I", raw, 0x4C) if s < _END]
        at = difat_start
        per = self.sector // 4 - 1
        for _ in range(difat_count):
            if at >= _END:
                break
            block = struct.unpack_from("<%dI" % (per + 1), raw, self._offset(at))
            fat_at.extend(s for s in block[:per] if s < _END)
            at = block[per]
        fat_at = fat_at[:fat_sectors] if fat_sectors else fat_at

        entries = self.sector // 4
        self.fat: List[int] = []
        for s in fat_at:
            self.fat.extend(struct.unpack_from("<%dI" % entries, raw, self._offset(s)))

        directory = self._chain(directory_start)
        self.entries: Dict[str, Tuple[int, int]] = {}
        root_start = None
        for i in range(0, len(directory) - 127, 128):
            length = struct.unpack_from("<H", directory, i + 0x40)[0]
            kind = directory[i + 0x42]
            if kind == 0 or length < 2:
                continue
            name = directory[i:i + length - 2].decode("utf-16-le", "replace")
            start = struct.unpack_from("<I", directory, i + 0x74)[0]
            size = struct.unpack_from("<I", directory, i + 0x78)[0]
            if kind == 5:
                root_start = (start, size)
            elif kind == 2:
                self.entries.setdefault(name, (start, size))

        self.minifat: List[int] = []
        if minifat_start < _END:
            block = self._chain(minifat_start)
            self.minifat = list(struct.unpack_from("<%dI" % (len(block) // 4), block))
        self.ministream = b""
        if root_start is not None and root_start[0] < _END:
            self.ministream = self._chain(root_start[0])[: root_start[1]]

    def _offset(self, sector: int) -> int:
        return (sector + 1) * self.sector

    def _chain(self, start: int, fat: Optional[List[int]] = None,
               mini: bool = False) -> bytes:
        table = self.minifat if mini else (fat or self.fat)
        size = self.mini_sector if mini else self.sector
        out = []
        at = start
        seen = set()
        while at < _END and at not in seen and at < len(table):
            seen.add(at)
            if mini:
                out.append(self.ministream[at * size:(at + 1) * size])
            else:
                offset = self._offset(at)
                out.append(self.raw[offset:offset + size])
            at = table[at]
        return b"".join(out)

    def stream(self, name: str) -> Optional[bytes]:
        found = self.entries.get(name)
        if found is None:
            return None
        start, size = found
        if size < self.cutoff:
            return self._chain(start, mini=True)[:size]
        return self._chain(start)[:size]


# -- the records ----------------------------------------------------------

BOF, EOF, CONTINUE = 0x0809, 0x000A, 0x003C
BOUNDSHEET, SST, FORMAT, XF, DATEMODE, FILEPASS, CODEPAGE = (
    0x0085, 0x00FC, 0x041E, 0x00E0, 0x0022, 0x002F, 0x0042)
LABELSST, LABEL, NUMBER, RK, MULRK, FORMULA, STRING, BOOLERR = (
    0x00FD, 0x0204, 0x0203, 0x027E, 0x00BD, 0x0006, 0x0207, 0x0205)

_ERRORS = {0x00: "#NULL!", 0x07: "#DIV/0!", 0x0F: "#VALUE!", 0x17: "#REF!",
           0x1D: "#NAME?", 0x24: "#NUM!", 0x2A: "#N/A"}


def _records(stream: bytes, at: int = 0):
    """(type, data, where) for every record from [at] on."""
    n = len(stream)
    while at + 4 <= n:
        kind, length = struct.unpack_from("<HH", stream, at)
        yield kind, stream[at + 4:at + 4 + length], at
        at += 4 + length


def _rk(value: int) -> float:
    """A number squeezed into 30 bits: an integer or the top of a double,
    perhaps a hundredth of what it says."""
    if value & 2:
        number = float(value >> 2) if not value & 0x80000000 else float((value >> 2) - (1 << 30))
    else:
        number = struct.unpack("<d", struct.pack("<Q", (value & 0xFFFFFFFC) << 32))[0]
    return number / 100 if value & 1 else number


class _Pieces:
    """The data of a record and its CONTINUE records, read across the joins.

    A character run that reaches a join goes on after a new flag byte, which
    says whether the rest is one byte a character or two. Everything else —
    lengths, rich-text runs, the extended data — simply continues.
    """

    def __init__(self, parts: List[bytes]):
        self.parts = parts
        self.part = 0
        self.at = 0

    def _need(self):
        while self.part < len(self.parts) and self.at >= len(self.parts[self.part]):
            self.part += 1
            self.at = 0

    def take(self, count: int) -> bytes:
        out = bytearray()
        while count > 0:
            self._need()
            if self.part >= len(self.parts):
                break
            chunk = self.parts[self.part][self.at:self.at + count]
            out += chunk
            self.at += len(chunk)
            count -= len(chunk)
        return bytes(out)

    def u8(self) -> int:
        got = self.take(1)
        return got[0] if got else 0

    def u16(self) -> int:
        got = self.take(2)
        return struct.unpack("<H", got)[0] if len(got) == 2 else 0

    def u32(self) -> int:
        got = self.take(4)
        return struct.unpack("<I", got)[0] if len(got) == 4 else 0

    def characters(self, count: int, wide: bool) -> str:
        out = []
        while count > 0:
            self._need()
            if self.part >= len(self.parts):
                break
            left = len(self.parts[self.part]) - self.at
            width = 2 if wide else 1
            if left <= 0:
                continue
            fits = min(count, left // width) if left >= width else 0
            if fits == 0:
                # A join in the middle of the run: a new flag byte opens the
                # next part and says how wide the rest is.
                self.part += 1
                self.at = 0
                if self.part >= len(self.parts):
                    break
                wide = bool(self.u8() & 1)
                continue
            raw = self.take(fits * width)
            out.append(raw.decode("utf-16-le", "replace") if wide else raw.decode("latin-1"))
            count -= fits
            if count > 0 and self.at >= len(self.parts[self.part]):
                self.part += 1
                self.at = 0
                if self.part >= len(self.parts):
                    break
                wide = bool(self.u8() & 1)
        return "".join(out)

    def string(self) -> str:
        """An XLUnicodeRichExtendedString, as the string table holds them."""
        count = self.u16()
        flags = self.u8()
        runs = self.u16() if flags & 0x08 else 0
        extended = self.u32() if flags & 0x04 else 0
        text = self.characters(count, bool(flags & 0x01))
        self.take(runs * 4 + extended)
        return text


def _short_string(data: bytes, at: int, count: int) -> str:
    """A string with its length already read: a flag byte, then the text."""
    flags = data[at]
    if flags & 1:
        return data[at + 1:at + 1 + count * 2].decode("utf-16-le", "replace")
    return data[at + 1:at + 1 + count].decode("latin-1")


class Book:
    """A BIFF8 workbook: its sheets by name, and how to read each one."""

    def __init__(self, raw: bytes, language: str = "en", max_rows: int = 0):
        compound = Compound(raw)
        stream = compound.stream("Workbook") or compound.stream("Book")
        if stream is None:
            raise XlsError("This compound document holds no workbook.")
        self.stream = stream
        self.language = language
        self.max_rows = max_rows
        self.date1904 = False
        self.titles: List[str] = []
        self._starts: List[int] = []
        self.strings: List[str] = []
        codes: Dict[int, str] = dict(numfmt.BUILTIN)
        styles: List[int] = []

        records = _records(stream)
        first = next(records, None)
        if first is None or first[0] != BOF:
            raise XlsError("The workbook stream does not start where a workbook does.")
        version = struct.unpack_from("<H", first[1], 0)[0] if len(first[1]) >= 2 else 0
        if version != 0x0600:
            raise XlsError(
                "This is an Excel 95 or older workbook, which this reader does "
                "not read. Excel 97 and later can save it again as .xls or .xlsx.")

        pending: Optional[List[bytes]] = None
        for kind, data, _ in records:
            if pending is not None:
                if kind == CONTINUE:
                    pending.append(data)
                    continue
                self._read_strings(pending)
                pending = None
            if kind == EOF:
                break
            if kind == FILEPASS:
                raise XlsError("This workbook is protected with a password.")
            if kind == BOUNDSHEET and len(data) >= 8:
                start, _visible, sheet_kind, count = struct.unpack_from("<IBBB", data, 0)
                if sheet_kind == 0:  # a worksheet, not a chart or a macro sheet
                    self._starts.append(start)
                    self.titles.append(_short_string(data, 7, count))
            elif kind == SST:
                pending = [data[8:]]
            elif kind == FORMAT and len(data) >= 5:
                number, count = struct.unpack_from("<HH", data, 0)
                codes[number] = _short_string(data, 4, count)
            elif kind == XF and len(data) >= 4:
                styles.append(struct.unpack_from("<H", data, 2)[0])
            elif kind == DATEMODE and len(data) >= 2:
                self.date1904 = struct.unpack_from("<H", data, 0)[0] == 1
        if pending is not None:
            self._read_strings(pending)

        made: Dict[int, numfmt.Format] = {}
        self.formats: List[numfmt.Format] = []
        for number in styles:
            if number not in made:
                made[number] = numfmt.Format(codes.get(number, "General"))
            self.formats.append(made[number])

    def _read_strings(self, parts: List[bytes]) -> None:
        pieces = _Pieces(parts)
        total = sum(len(p) for p in parts)
        read = 0
        while True:
            pieces._need()
            if pieces.part >= len(parts):
                break
            self.strings.append(pieces.string())
            read += 1
            if read > total:  # cannot be more strings than bytes
                break

    def rows(self, index: int) -> List[list]:
        """Every row of sheet [index], as values, gaps left empty."""
        general = numfmt.Format("General")
        comma = self.language.split("-")[0] in numfmt.COMMA_LANGUAGES
        grid: Dict[int, Dict[int, object]] = {}
        last_formula: Optional[Tuple[int, int]] = None
        records = _records(self.stream, self._starts[index])
        first = next(records, None)
        if first is None or first[0] != BOF:
            return []

        def put(row: int, column: int, value) -> None:
            if value is None or value == "":
                return
            if self.max_rows and row >= self.max_rows:
                return
            grid.setdefault(row, {})[column] = value

        def number(row: int, column: int, xf: int, value: float) -> None:
            put(row, column, self._number(value, xf, general, comma))

        for kind, data, _ in records:
            if kind == EOF:
                break
            if kind == LABELSST and len(data) >= 10:
                row, column, _xf, at = struct.unpack_from("<HHHI", data, 0)
                put(row, column, self.strings[at] if at < len(self.strings) else "")
            elif kind == NUMBER and len(data) >= 14:
                row, column, xf = struct.unpack_from("<HHH", data, 0)
                number(row, column, xf, struct.unpack_from("<d", data, 6)[0])
            elif kind == RK and len(data) >= 10:
                row, column, xf, value = struct.unpack_from("<HHHI", data, 0)
                number(row, column, xf, _rk(value))
            elif kind == MULRK and len(data) >= 6:
                row, column = struct.unpack_from("<HH", data, 0)
                at = 4
                while at + 6 <= len(data) - 2:
                    xf, value = struct.unpack_from("<HI", data, at)
                    number(row, column, xf, _rk(value))
                    column += 1
                    at += 6
            elif kind == LABEL and len(data) >= 9:
                row, column, _xf, count = struct.unpack_from("<HHHH", data, 0)
                put(row, column, _short_string(data, 8, count))
            elif kind == BOOLERR and len(data) >= 8:
                row, column, _xf, value, is_error = struct.unpack_from("<HHHBB", data, 0)
                if is_error:
                    text = _ERRORS.get(value, "#ERROR")
                    put(row, column, {"v": text, "t": text, "r": "error"})
                else:
                    put(row, column, bool(value))
            elif kind == FORMULA and len(data) >= 14:
                # The value the formula came to when the file was saved; the
                # formula itself is not worked out again.
                row, column, xf = struct.unpack_from("<HHH", data, 0)
                result = data[6:14]
                last_formula = None
                if result[6:8] == b"\xff\xff":
                    tag = result[0]
                    if tag == 0:
                        last_formula = (row, column)  # the text comes next
                    elif tag == 1:
                        put(row, column, bool(result[2]))
                    elif tag == 2:
                        text = _ERRORS.get(result[2], "#ERROR")
                        put(row, column, {"v": text, "t": text, "r": "error"})
                else:
                    number(row, column, xf, struct.unpack("<d", result)[0])
            elif kind == STRING and last_formula is not None and len(data) >= 3:
                count = struct.unpack_from("<H", data, 0)[0]
                put(last_formula[0], last_formula[1], _short_string(data, 2, count))
                last_formula = None

        if not grid:
            return []
        out: List[list] = []
        for row in range(max(grid) + 1):
            cells = grid.get(row)
            if not cells:
                out.append([])
                continue
            line: list = [None] * (max(cells) + 1)
            for column, value in cells.items():
                line[column] = value
            out.append(line)
        return out

    def _number(self, value: float, xf: int, general, comma: bool):
        fmt = self.formats[xf] if xf < len(self.formats) else general
        plain = int(value) if value.is_integer() and abs(value) < 1e15 else value
        shown = numfmt.show(value, fmt, self.language, self.date1904)
        if fmt.is_date:
            # A date's value is its text, not its day number: copied out as
            # JSON, 46266 would mean nothing to anybody.
            return {"v": shown, "t": shown}
        if shown == str(plain) or (not comma and shown == repr(plain)):
            return plain
        return {"v": plain, "t": shown}
