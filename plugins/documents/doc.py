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

"""Word 97–2003's binary .doc as Markdown.

**Where the text is.** Not in one place. The `WordDocument` stream opens with
the FIB, which says how long each part of the text is — the body, the
footnotes, the headers, the comments — and where the tables describing it sit
in the *table stream* (`1Table` or `0Table`, the FIB says which). One of those
is the CLX: the text as a list of pieces, each a run of character positions
kept somewhere in `WordDocument` either one byte a character (cp1252) or two
(UTF-16). A file edited many times is dozens of pieces in no order.

**What a paragraph is** is not in the text either. The text says where
paragraphs end (`\\r`) and where table cells end (`\\x07`); whether a paragraph
is a heading, and whether it is in a table and the mark that closes a row, are
its *properties*, found through the PAPX pages by the file position of its
end mark. The style's built-in identifier says "heading 1" whatever language
Word was in, which is the one thing easier here than in a .docx.

**Special characters in the text:** a field is `\\x13` code `\\x14` result
`\\x15`, and only the result is read; `\\x02` is a footnote's mark, `\\x05` a
comment's, `\\x01` and `\\x08` a picture's; `\\x0b` a line break, `\\x0c` a page
or section break, `\\x1e` a hyphen that does not break and `\\x1f` one that is
only a suggestion.

**Character properties** — bold, italic, struck through, and where an inline
picture's bytes are — come the same way through the CHPX pages. A picture is
a block in the `Data` stream: a PICF header with its size on the page, then
the Office Art records the PNG or JPEG itself is inside.

**The picture's bytes are rarely in that block.** Word keeps each picture once,
in a store in the table stream (`fcDggInfo`), and a picture's entry there
says where its bytes are — usually far down the `WordDocument` stream itself.
An inline picture's block names its entry; a picture floating over the text is
a shape anchored at a `\x08`, found through the anchors table (`PlcSpaMom`)
by its position, its shape by id, and its entry by the shape's `pib`.

**List numbers** are in two tables of their own: the lists (`PlfLst`), each
with a level's format, its start and the text around its number — `%1.`
written with the level's index where the number goes — and the overrides
(`PlfLfo`) a paragraph's `ilfo` points at, which name a list by its id and
may start a level somewhere else. Counted as Word counts: per list, a deeper
level starting again under a new higher one.

**A comment's author** is an index into a list of names (`GrpXstAtnOwners`),
kept in the comment's reference record beside the initials.
"""

from __future__ import annotations

import re
import struct
from typing import Callable, Dict, List, Optional, Tuple

import mdtext
from compound import Compound
from docx import _format
from mdtext import Span

from xverb import Picture, picture_size

#: The FIB's flags word and the two bits read in it.
_ENCRYPTED = 0x0100
_TABLE_ONE = 0x0200

#: Where fc/lcb pairs start in the FIB, and the pairs this reads by index.
_FCLCB = 0x9A
_STSHF, _PLCFFNDREF, _PLCFFNDTXT, _PLCFANDREF, _PLCFANDTXT = 1, 2, 3, 4, 5
_PLCFBTECHPX, _PLCFBTEPAPX, _CLX, _PLCSPAMOM, _PLCFENDREF, _PLCFENDTXT, _DGGINFO = 12, 13, 33, 40, 46, 47, 50
_GRPXSTATNOWNERS, _PLFLST, _PLFLFO = 36, 73, 74

#: A list level's number format (`nfc`) as the Word 2007 name `docx` counts in.
_NFC = {0: "decimal", 1: "upperRoman", 2: "lowerRoman", 3: "upperLetter",
        4: "lowerLetter", 22: "decimalZero", 23: "bullet", 255: "none"}

# Paragraph properties this reads, by sprm.
_IN_TABLE, _TTP, _INNER_TTP, _INNER_CELL = 0x2416, 0x2417, 0x244C, 0x244B
_OUTLINE, _ILFO, _ILVL, _TABLE_HEADER = 0x2640, 0x460B, 0x260A, 0x3404

# Character properties this reads.
_BOLD, _ITALIC, _STRIKE, _DSTRIKE, _PIC_LOCATION, _VANISH = 0x0835, 0x0836, 0x0837, 0x2A53, 0x6A03, 0x083C

#: A twip is a twentieth of a point; a pixel of the page is fifteen of them.
_TWIPS_PER_PIXEL = 15

#: Built-in style identifiers: heading 1–9, the table of contents, the title.
_TITLE = 62
_TOC = range(19, 28)


class DocError(ValueError):
    """A sentence about why the file cannot be read."""


class Result:
    def __init__(self):
        self.markdown = ""
        self.pictures: dict = {}
        self.changes = 0
        self.comments = 0
        self.words = 0
        self.meta: Dict[str, str] = {}
        self.guessed = False


def _sprms(data: bytes, at: int, end: int):
    """(sprm, operand) pairs of a grpprl."""
    while at + 2 <= end:
        sprm = struct.unpack_from("<H", data, at)[0]
        at += 2
        spra = sprm >> 13
        if spra in (0, 1):
            size = 1
        elif spra in (2, 4, 5):
            size = 2
        elif spra == 3:
            size = 4
        elif spra == 7:
            size = 3
        else:
            if at >= end:
                return
            if sprm in (0xD608, 0xD606):
                size = struct.unpack_from("<H", data, at)[0] - 1
                at += 2
            else:
                size = data[at]
                at += 1
        yield sprm, data[at:at + size]
        at += size


class Paragraph:
    __slots__ = ("istd", "in_table", "ttp", "inner_ttp", "outline", "ilfo", "ilvl", "header")

    def __init__(self):
        self.istd = 0
        self.in_table = False
        self.ttp = False
        self.inner_ttp = False
        self.outline: Optional[int] = None
        self.ilfo = 0
        self.ilvl = 0
        self.header = False


class Document:
    def __init__(self, raw: bytes, tr: Callable[..., str]):
        self.tr = tr
        try:
            compound = Compound(raw)
        except Exception:  # noqa: BLE001
            raise DocError(tr("This is not a Word document, whatever its name says."))
        word = compound.stream("WordDocument")
        if not word or len(word) < 0x200 or struct.unpack_from("<H", word, 0)[0] != 0xA5EC:
            raise DocError(tr("This is not a Word document, whatever its name says."))
        self.word = word
        nfib = struct.unpack_from("<H", word, 2)[0]
        flags = struct.unpack_from("<H", word, 0x0A)[0]
        if flags & _ENCRYPTED:
            raise DocError(tr(
                "This document is protected with a password. Press Enter to "
                "open it in the application that can ask for one."))
        if nfib < 0x00C1:
            raise DocError(tr(
                "This document was saved by Word 95 or older, in a format this "
                "viewer does not read. Press Enter to open it in the system's "
                "own application."))
        table = compound.stream("1Table" if flags & _TABLE_ONE else "0Table")
        if table is None:
            raise DocError(tr("This is not a Word document, whatever its name says."))
        self.table = table
        self.lengths = struct.unpack_from("<8i", word, 0x4C)
        self.pairs = struct.unpack_from("<H", word, 0x98)[0]
        self.data = compound.stream("Data") or b""
        self._pieces()
        self._styles()
        self._papx()
        self._chpx()
        self._lists()

    def _fc(self, index: int) -> Tuple[int, int]:
        if index >= self.pairs:
            return 0, 0
        return struct.unpack_from("<II", self.word, _FCLCB + index * 8)

    # The text.

    def _pieces(self) -> None:
        fc, lcb = self._fc(_CLX)
        clx = self.table[fc:fc + lcb]
        at = 0
        while at < len(clx) and clx[at] == 0x01:
            at += 3 + struct.unpack_from("<h", clx, at + 1)[0]
        if at >= len(clx) or clx[at] != 0x02:
            raise DocError(self.tr("This document could not be read: {error}",
                                   {"error": "no piece table"}))
        size = struct.unpack_from("<I", clx, at + 1)[0]
        plc = clx[at + 5:at + 5 + size]
        count = (size - 4) // 12
        cps = struct.unpack_from("<%di" % (count + 1), plc, 0)
        self.pieces: List[Tuple[int, int, int, bool]] = []
        for i in range(count):
            raw_fc = struct.unpack_from("<I", plc, (count + 1) * 4 + i * 8 + 2)[0]
            compressed = bool(raw_fc & 0x40000000)
            start = (raw_fc & 0x3FFFFFFF) // 2 if compressed else raw_fc & 0x3FFFFFFF
            self.pieces.append((cps[i], cps[i + 1], start, compressed))

    def text(self, start: int, end: int) -> str:
        out = []
        for cp0, cp1, fc, compressed in self.pieces:
            lo, hi = max(start, cp0), min(end, cp1)
            if lo >= hi:
                continue
            if compressed:
                chunk = self.word[fc + lo - cp0:fc + hi - cp0]
                out.append(chunk.decode("cp1252", "replace"))
            else:
                chunk = self.word[fc + 2 * (lo - cp0):fc + 2 * (hi - cp0)]
                out.append(chunk.decode("utf-16-le", "replace"))
        return "".join(out)

    def fc_of(self, cp: int) -> Optional[int]:
        for cp0, cp1, fc, compressed in self.pieces:
            if cp0 <= cp < cp1:
                return fc + (cp - cp0) * (1 if compressed else 2)
        return None

    # Styles: the built-in identifier and what each style is based on.

    def _styles(self) -> None:
        self.sti: List[int] = []
        self.base: List[int] = []
        fc, lcb = self._fc(_STSHF)
        data = self.table[fc:fc + lcb]
        if len(data) < 6:
            return
        cb_stshi = struct.unpack_from("<H", data, 0)[0]
        count = struct.unpack_from("<H", data, 2)[0]
        at = 2 + cb_stshi
        for _ in range(count):
            if at + 2 > len(data):
                break
            cb = struct.unpack_from("<H", data, at)[0]
            if cb >= 4:
                first, second = struct.unpack_from("<HH", data, at + 2)
                self.sti.append(first & 0x0FFF)
                self.base.append(second >> 4)
            else:
                self.sti.append(0x0FFE)
                self.base.append(0x0FFF)
            at += 2 + cb

    def heading(self, istd: int) -> int:
        seen = 0
        while 0 <= istd < len(self.sti) and seen < 12:
            sti = self.sti[istd]
            if 1 <= sti <= 9:
                return sti
            if sti == _TITLE:
                return 1
            if sti == 0:
                return 0
            istd = self.base[istd]
            seen += 1
        return 0

    def toc(self, istd: int) -> bool:
        return 0 <= istd < len(self.sti) and self.sti[istd] in _TOC

    # Paragraph properties, by the file position of a paragraph's end.

    def _papx(self) -> None:
        self.runs: List[Tuple[int, int, Paragraph]] = []
        fc, lcb = self._fc(_PLCFBTEPAPX)
        plc = self.table[fc:fc + lcb]
        count = (len(plc) - 4) // 8
        if count <= 0:
            return
        pages = struct.unpack_from("<%dI" % count, plc, (count + 1) * 4)
        for page in pages:
            base = (page & 0x3FFFFF) * 512
            fkp = self.word[base:base + 512]
            if len(fkp) < 512:
                continue
            crun = fkp[511]
            fcs = struct.unpack_from("<%dI" % (crun + 1), fkp, 0)
            for i in range(crun):
                offset = fkp[(crun + 1) * 4 + i * 13] * 2
                props = Paragraph()
                if offset:
                    cb = fkp[offset]
                    if cb == 0:
                        size = fkp[offset + 1] * 2
                        at = offset + 2
                    else:
                        size = cb * 2 - 1
                        at = offset + 1
                    if size >= 2:
                        props.istd = struct.unpack_from("<H", fkp, at)[0]
                        for sprm, operand in _sprms(fkp, at + 2, at + size):
                            self._apply(props, sprm, operand)
                self.runs.append((fcs[i], fcs[i + 1], props))
        self.runs.sort(key=lambda r: r[0])

    @staticmethod
    def _apply(props: Paragraph, sprm: int, operand: bytes) -> None:
        if not operand:
            return
        if sprm == _IN_TABLE:
            props.in_table = operand[0] != 0
        elif sprm == _TTP:
            props.ttp = operand[0] != 0
        elif sprm == _INNER_TTP:
            props.inner_ttp = operand[0] != 0
        elif sprm == _OUTLINE:
            props.outline = operand[0]
        elif sprm == _ILFO and len(operand) >= 2:
            props.ilfo = struct.unpack_from("<h", operand, 0)[0]
        elif sprm == _ILVL:
            props.ilvl = operand[0]
        elif sprm == _TABLE_HEADER:
            props.header = operand[0] != 0

    def chars(self, start: int, end: int):
        """(character, file position) for each position in [start, end)."""
        for cp0, cp1, fc, compressed in self.pieces:
            lo, hi = max(start, cp0), min(end, cp1)
            if lo >= hi:
                continue
            width = 1 if compressed else 2
            text = self.text(lo, hi)
            base = fc + (lo - cp0) * width
            for i, ch in enumerate(text):
                yield ch, base + i * width

    def _chpx(self) -> None:
        self.char_runs: List[Tuple[int, int, bytes]] = []
        fc, lcb = self._fc(_PLCFBTECHPX)
        plc = self.table[fc:fc + lcb]
        count = (len(plc) - 4) // 8
        if count <= 0:
            return
        pages = struct.unpack_from("<%dI" % count, plc, (count + 1) * 4)
        for page in pages:
            base = (page & 0x3FFFFF) * 512
            fkp = self.word[base:base + 512]
            if len(fkp) < 512:
                continue
            crun = fkp[511]
            fcs = struct.unpack_from("<%dI" % (crun + 1), fkp, 0)
            for i in range(crun):
                offset = fkp[(crun + 1) * 4 + i] * 2
                grpprl = b""
                if offset:
                    size = fkp[offset]
                    grpprl = fkp[offset + 1:offset + 1 + size]
                self.char_runs.append((fcs[i], fcs[i + 1], grpprl))
        self.char_runs.sort(key=lambda r: r[0])
        self._char_cache: Dict[int, dict] = {}

    def char(self, fc: int) -> dict:
        """The character properties in force at file position [fc]."""
        runs = self.char_runs
        lo, hi = 0, len(runs)
        while lo < hi:
            mid = (lo + hi) // 2
            if runs[mid][1] <= fc:
                lo = mid + 1
            else:
                hi = mid
        if lo >= len(runs) or not runs[lo][0] <= fc < runs[lo][1]:
            return {}
        found = self._char_cache.get(lo)
        if found is None:
            found = {}
            grpprl = runs[lo][2]
            for sprm, operand in _sprms(grpprl, 0, len(grpprl)):
                if not operand:
                    continue
                if sprm in (_BOLD, _ITALIC, _STRIKE, _DSTRIKE, _VANISH):
                    # 0 and 1 are off and on; 0x80 and 0x81 say "as the style,
                    # or the opposite of it" — read as off and on.
                    found[sprm] = operand[0] in (1, 0x81)
                elif sprm == _PIC_LOCATION and len(operand) >= 4:
                    found[sprm] = struct.unpack_from("<I", operand, 0)[0]
            self._char_cache[lo] = found
        return found

    # Pictures.

    def _art(self, data: bytes, start: int, end: int):
        """Every Office Art record in [start, end), containers opened:
        (type, instance, payload start, payload end)."""
        at = start
        while at + 8 <= end:
            ver_inst, kind, length = struct.unpack_from("<HHI", data, at)
            body, stop = at + 8, min(end, at + 8 + length)
            yield kind, ver_inst >> 4, body, stop
            if ver_inst & 0x0F == 0x0F:
                yield from self._art(data, body, stop)
            at = stop

    @staticmethod
    def _image(data: bytes, start: int, end: int) -> Optional[bytes]:
        """The PNG or JPEG inside [start, end) of [data], if there is one."""
        block = data[start:end]
        at = block.find(b"\x89PNG\r\n\x1a\n")
        if at >= 0:
            stop = block.find(b"IEND", at)
            return block[at:stop + 8] if stop > 0 else block[at:]
        at = block.find(b"\xff\xd8\xff")
        if at >= 0:
            stop = block.rfind(b"\xff\xd9")
            return block[at:stop + 2] if stop > at else block[at:]
        return None

    def _entry(self, data: bytes, body: int, stop: int) -> Optional[bytes]:
        """A picture store entry's bytes: after it, or where it points."""
        found = self._image(data, body + 36, stop)
        if found is not None:
            return found
        if stop - body >= 36:
            size, _, delay = struct.unpack_from("<III", data, body + 20)
            if 0 < delay < len(self.word):
                length = struct.unpack_from("<I", self.word, delay + 4)[0] if delay + 8 <= len(self.word) else size
                return self._image(self.word, delay, delay + 8 + max(length, 0))
        return None

    def _store(self) -> None:
        """The picture store, the shapes' pictures, and the anchors."""
        self.store: List[Tuple[bytes, int, int]] = []
        self.shape_picture: Dict[int, int] = {}
        fc, lcb = self._fc(_DGGINFO)
        art = self.table[fc:fc + lcb]
        spid = None
        # The store's container, then each drawing — the main text's and the
        # headers' — as one byte saying which, and its container.
        records = []
        at = 0
        while at + 8 <= len(art):
            length = struct.unpack_from("<I", art, at + 4)[0]
            records.extend(self._art(art, at, min(len(art), at + 8 + length)))
            at += 8 + length + 1
        for kind, _, body, stop in records:
            if kind == 0xF007:
                self.store.append((art, body, stop))
            elif kind == 0xF00A and stop - body >= 4:
                spid = struct.unpack_from("<I", art, body)[0]
            elif kind == 0xF00B and spid is not None:
                count = (struct.unpack_from("<H", art, body - 8)[0] >> 4)
                for i in range(count):
                    if body + i * 6 + 6 > stop:
                        break
                    opid, value = struct.unpack_from("<HI", art, body + i * 6)
                    if opid & 0x3FFF == 0x0104:
                        self.shape_picture[spid] = value
        self.anchors: Dict[int, Tuple[int, Tuple[int, int]]] = {}
        fc, lcb = self._fc(_PLCSPAMOM)
        if lcb >= 4 + 26:
            plc = self.table[fc:fc + lcb]
            count = (lcb - 4) // 30
            cps = struct.unpack_from("<%di" % (count + 1), plc, 0)
            for i in range(count):
                spid, left, top, right, bottom = struct.unpack_from("<Iiiii", plc, (count + 1) * 4 + i * 26)
                width = round(abs(right - left) / _TWIPS_PER_PIXEL)
                height = round(abs(bottom - top) / _TWIPS_PER_PIXEL)
                self.anchors[cps[i]] = (spid, (width, height))

    def shape_has_picture(self, cp: int) -> bool:
        if not hasattr(self, "store"):
            self._store()
        anchor = self.anchors.get(cp)
        return anchor is not None and bool(self.shape_picture.get(anchor[0]))

    #: What a store entry says its picture is, for the caption of one the
    #: application cannot draw.
    _KINDS = {2: "EMF", 3: "WMF", 4: "PICT", 5: "JPEG", 6: "PNG", 7: "DIB", 0x11: "TIFF"}

    def floating_kind(self, cp: int) -> str:
        anchor = self.anchors.get(cp)
        pib = self.shape_picture.get(anchor[0]) if anchor else None
        if not pib or pib > len(self.store):
            return ""
        art, body, _ = self.store[pib - 1]
        return self._KINDS.get(art[body], "")

    def floating(self, cp: int) -> Optional[Tuple[bytes, Optional[Tuple[int, int]]]]:
        """The picture of the shape anchored at [cp], and its size."""
        if not hasattr(self, "store"):
            self._store()
        anchor = self.anchors.get(cp)
        if anchor is None:
            return None
        spid, size = anchor
        pib = self.shape_picture.get(spid)
        if not pib or pib > len(self.store):
            return None
        found = self._entry(*self.store[pib - 1])
        if found is None:
            return None
        return found, size if size[0] and size[1] else None

    def picture(self, offset: int) -> Optional[Tuple[bytes, Optional[Tuple[int, int]]]]:
        """The inline picture whose block is at [offset] in the Data stream,
        and its size on the page in pixels."""
        data = self.data
        if offset + 0x44 > len(data):
            return None
        total = struct.unpack_from("<I", data, offset)[0]
        header = struct.unpack_from("<H", data, offset + 4)[0]
        end = min(len(data), offset + total)
        size = None
        dxa, dya, mx, my = struct.unpack_from("<hhHH", data, offset + 0x1C)
        if dxa > 0 and dya > 0:
            size = (max(1, round(dxa * (mx or 1000) / 1000 / _TWIPS_PER_PIXEL)),
                    max(1, round(dya * (my or 1000) / 1000 / _TWIPS_PER_PIXEL)))
        start = offset + header
        if struct.unpack_from("<h", data, offset + 6)[0] == 0x66:
            start += 1 + data[start]
        found = self._image(data, start, end)
        if found is None:
            for kind, _, body, stop in self._art(data, start, end):
                if kind == 0xF007:
                    found = self._entry(data, body, stop)
                    if found is not None:
                        break
        return (found, size) if found is not None else None

    def props(self, cp: int) -> Paragraph:
        fc = self.fc_of(cp)
        if fc is not None:
            lo, hi = 0, len(self.runs)
            while lo < hi:
                mid = (lo + hi) // 2
                if self.runs[mid][1] <= fc:
                    lo = mid + 1
                else:
                    hi = mid
            if lo < len(self.runs) and self.runs[lo][0] <= fc < self.runs[lo][1]:
                return self.runs[lo][2]
        return Paragraph()

    # Lists.

    def _lists(self) -> None:
        """The list definitions and their overrides, as far as a number needs."""
        #: lsid → level → (format, number text, start)
        self.lists: Dict[int, Dict[int, Tuple[str, str, int]]] = {}
        #: ilfo - 1 → (lsid, level → start override)
        self.overrides: List[Tuple[int, Dict[int, int]]] = []
        try:
            self._read_lists()
            self._read_overrides()
        except (struct.error, IndexError):
            # A list table that does not add up leaves every item a bullet,
            # which is what this reader did before it read them at all.
            self.lists, self.overrides = {}, []

    def _read_lists(self) -> None:
        fc, lcb = self._fc(_PLFLST)
        data = self.table[fc:fc + lcb]
        if len(data) < 2:
            return
        count = struct.unpack_from("<h", data, 0)[0]
        at = 2
        heads = []
        for _ in range(max(0, count)):
            lsid = struct.unpack_from("<i", data, at)[0]
            simple = data[at + 26] & 0x01
            heads.append((lsid, 1 if simple else 9))
            at += 28
        # The levels follow the whole array, in its order, in the table
        # stream at the end of what the FIB says is the list table's size.
        at = fc + at
        table = self.table
        for lsid, levels in heads:
            found: Dict[int, Tuple[str, str, int]] = {}
            for level in range(levels):
                start = struct.unpack_from("<i", table, at)[0]
                nfc = table[at + 4]
                papx, chpx = table[at + 25], table[at + 24]
                at += 28 + papx + chpx
                cch = struct.unpack_from("<H", table, at)[0]
                text = table[at + 2:at + 2 + cch * 2].decode("utf-16-le", "replace")
                at += 2 + cch * 2
                found[level] = (_NFC.get(nfc, "decimal"), text, start)
            self.lists[lsid] = found

    def _read_overrides(self) -> None:
        fc, lcb = self._fc(_PLFLFO)
        data = self.table[fc:]
        if lcb < 4:
            return
        count = struct.unpack_from("<i", data, 0)[0]
        heads = []
        for i in range(max(0, count)):
            lsid = struct.unpack_from("<i", data, 4 + i * 16)[0]
            levels = data[4 + i * 16 + 12]
            heads.append((lsid, levels))
        at = 4 + count * 16
        for lsid, levels in heads:
            at += 4  # the cp nobody uses
            starts: Dict[int, int] = {}
            for _ in range(levels):
                start = struct.unpack_from("<i", data, at)[0]
                flags = struct.unpack_from("<I", data, at + 4)[0]
                at += 8
                level, starts_again, formatted = flags & 0x0F, flags & 0x10, flags & 0x20
                if starts_again:
                    starts[level] = start
                if formatted:
                    # A whole level of its own follows; its number is not
                    # read, only stepped over.
                    papx, chpx = data[at + 25], data[at + 24]
                    at += 28 + papx + chpx
                    at += 2 + struct.unpack_from("<H", data, at)[0] * 2
            self.overrides.append((lsid, starts))

    # Who wrote the comments.

    def comment_authors(self) -> List[str]:
        """Each comment's author, in the order of the comments; an empty name
        where the file does not say."""
        fc, lcb = self._fc(_GRPXSTATNOWNERS)
        names: List[str] = []
        data = self.table[fc:fc + lcb]
        at = 0
        while at + 2 <= len(data):
            cch = struct.unpack_from("<H", data, at)[0]
            names.append(data[at + 2:at + 2 + cch * 2].decode("utf-16-le", "replace"))
            at += 2 + cch * 2
        fc, lcb = self._fc(_PLCFANDREF)
        refs = self.table[fc:fc + lcb]
        # n + 1 positions, then n records of 30 bytes; the author's index is
        # the sixth word of each, after the initials.
        count = (len(refs) - 4) // 34
        authors = []
        for i in range(max(0, count)):
            at = (count + 1) * 4 + i * 30 + 20
            if at + 2 > len(refs):
                break
            index = struct.unpack_from("<h", refs, at)[0]
            authors.append(names[index] if 0 <= index < len(names) else "")
        return authors

    def plc(self, index: int) -> List[int]:
        fc, lcb = self._fc(index)
        if lcb < 4:
            return []
        data = self.table[fc:fc + lcb]
        return list(struct.unpack_from("<%di" % (lcb // 4), data, 0))


#: The most a paragraph standing in for a heading may be.
_MAYBE_LONGEST = 150

_HIDING_FIELDS = ("TOC", "INDEX", "XE", "TC")


class _Numbers:
    """List items counted as the document is read, the way Word counts them."""

    def __init__(self, doc: "Document"):
        self.doc = doc
        self.counts: Dict[int, Dict[int, int]] = {}
        self.started: set = set()

    def next(self, ilfo: int, ilvl: int) -> Optional[Tuple[str, str]]:
        """(kind, label): ``bullet``, ``number`` or ``none``; None where the
        list tables do not say."""
        doc = self.doc
        if not 0 < ilfo <= len(doc.overrides):
            return None
        lsid, starts = doc.overrides[ilfo - 1]
        levels = doc.lists.get(lsid)
        if not levels:
            return None
        level = ilvl if ilvl in levels else max(levels)
        fmt, text, start = levels[level]
        counts = self.counts.setdefault(lsid, {})
        if ilfo not in self.started:
            self.started.add(ilfo)
            for at, value in starts.items():
                counts[at] = value - 1
        counts[level] = counts.get(level, start - 1) + 1
        for deeper in [k for k in counts if k > level]:
            del counts[deeper]
        if fmt == "bullet":
            return "bullet", ""
        if fmt == "none" or not text:
            return "none", ""
        label = []
        for ch in text:
            code = ord(ch)
            if code < 9:
                shown, _, first = levels.get(code, ("decimal", "", 1))
                label.append(_format(counts.get(code, first), shown))
            else:
                label.append(ch)
        return "number", "".join(label).strip()


class _Reader:
    """Walks the body a paragraph at a time and writes it as Markdown."""

    def __init__(self, doc: Document, tr: Callable[..., str], result: Result):
        self.doc = doc
        self.tr = tr
        self.result = result
        self.fields: List[list] = []
        self.notes: List[str] = []
        self.note_queue: List[str] = []
        self.comments: List[str] = []
        self.comments_said = 0
        self.after: List[str] = []

    def hidden(self) -> bool:
        return any(f[0].strip().split(" ", 1)[0].upper() in _HIDING_FIELDS for f in self.fields)

    def spans(self, start: int, end: int, plain: bool = False) -> List[Span]:
        """The characters of [start, end) as formatted pieces: field codes
        out and their results in, the special marks made into what they mean.
        [plain] reads a note or a comment, which has no pictures or marks."""
        doc = self.doc
        out: List[Span] = []
        for offset, (ch, fc) in enumerate(doc.chars(start, end)):
            code = ord(ch)
            if code == 0x13:
                self.fields.append(["", False])
                continue
            if code == 0x14:
                if self.fields:
                    self.fields[-1][1] = True
                continue
            if code == 0x15:
                if self.fields:
                    self.fields.pop()
                continue
            if self.fields and not self.fields[-1][1]:
                self.fields[-1][0] += ch
                continue
            if self.hidden():
                continue
            props = doc.char(fc)
            if props.get(_VANISH):
                continue
            if code == 0x02 and not plain:
                if len(self.notes) < len(self.note_queue):
                    self.notes.append(self.note_queue[len(self.notes)])
                    out.append(Span("\\[%d\\]" % len(self.notes), raw=True))
                continue
            if code == 0x05 and not plain:
                self.comments_said += 1
                if self.comments_said <= len(self.comments):
                    label = self.tr("comment {n}", {"n": self.comments_said})
                    out.append(Span("\\[%s\\]" % mdtext.escape(label), raw=True))
                continue
            if code == 0x01 and not plain:
                self.picture(props.get(_PIC_LOCATION))
                continue
            if code == 0x08 and not plain:
                # A shape with no picture — a text box, a line — shows nothing.
                if doc.shape_has_picture(start + offset):
                    self.place(doc.floating(start + offset), doc.floating_kind(start + offset))
                continue
            if code in (0x09, 0x0B, 0xA0):
                ch = " "
            elif code == 0x1E:
                ch = "-"
            elif code < 0x20 or code == 0xFFFC:
                # U+FFFC stands for an object the writer did not keep.
                continue
            out.append(Span(ch, props.get(_BOLD, False), props.get(_ITALIC, False),
                            props.get(_STRIKE, False) or props.get(_DSTRIKE, False)))
        return out

    def picture(self, location: Optional[int]) -> None:
        # A `\x01` is also an embedded object's or a hyperlink's data; one
        # that holds no picture is nothing to show.
        found = self.doc.picture(location) if location is not None else None
        if found is not None:
            self.place(found)

    def place(self, found, kind: str = "") -> None:
        if found is None:
            what = self.tr("picture: {what}", {"what": kind}) if kind else self.tr("picture")
            self.after.append(mdtext.escape("[%s]" % what))
            return
        data, size = found
        if picture_size(data) is None:
            self.after.append(mdtext.escape("[%s]" % self.tr("picture")))
            return
        key = "p%d" % (len(self.result.pictures) + 1)
        width, height = size if size else (None, None)
        picture = Picture(data, width, height)
        self.result.pictures[key] = picture
        self.after.append("![](picture:%s)" % key)

    def small(self, start: int, end: int) -> str:
        saved = self.fields
        self.fields = []
        try:
            return mdtext.cell(mdtext.spans(self.spans(start, end, plain=True)))
        finally:
            self.fields = saved

    def texts(self, ref_index: int, txt_index: int, base: int, record: int = 2) -> List[str]:
        """The notes or comments, each read from its stretch of the text.

        How many there are is the reference table's size over its entry: a
        position and a [record] — two bytes for a note, thirty for a
        comment. Counting a comment's thirty as two read one comment as two,
        the second of them empty."""
        doc = self.doc
        bounds = doc.plc(txt_index)
        _, lcb = doc._fc(ref_index)
        count = max(0, (lcb - 4) // (4 + record))
        return [self.small(base + bounds[i], base + bounds[i + 1])
                for i in range(count) if i + 1 < len(bounds)]


def convert(raw: bytes, tr: Callable[..., str]) -> Result:
    doc = Document(raw, tr)
    ccp_text, ccp_ftn, ccp_hdd, _, ccp_atn, ccp_edn = doc.lengths[:6]
    result = Result()
    reader = _Reader(doc, tr, result)

    atn_base = ccp_text + ccp_ftn + ccp_hdd
    footnotes = reader.texts(_PLCFFNDREF, _PLCFFNDTXT, ccp_text) if ccp_ftn else []
    endnotes = reader.texts(_PLCFENDREF, _PLCFENDTXT, atn_base + ccp_atn) if ccp_edn else []
    reader.comments = reader.texts(_PLCFANDREF, _PLCFANDTXT, atn_base, record=30) if ccp_atn else []
    if reader.comments:
        # "Anna: the text", as a comment is written from every other format.
        try:
            authors = doc.comment_authors()
        except struct.error:
            authors = []
        reader.comments = [
            (mdtext.escape(authors[i]) + ": " if i < len(authors) and authors[i] else "") + text
            for i, text in enumerate(reader.comments)
        ]
    reader.note_queue = footnotes + endnotes

    numbers = _Numbers(doc)
    out: List[object] = []
    table: List[List[str]] = []
    row: List[str] = []
    cell: List[str] = []
    header = False
    styled = 0

    def close_table() -> None:
        nonlocal table, row, cell, header
        if cell:
            row.append(" · ".join(cell))
            cell = []
        if row:
            table.append(row)
            row = []
        if table:
            out.append(mdtext.table([[mdtext.cell(c) for c in r] for r in table], header and len(table) > 1))
        table, header = [], False

    body = doc.text(0, ccp_text)
    start = 0
    for index, ch in enumerate(body):
        if ch not in "\r\x07\x0c":
            continue
        props = doc.props(index)
        reader.after = []
        pieces = reader.spans(start, index)
        after = reader.after
        text = mdtext.spans(pieces)
        start = index + 1

        if props.in_table and not props.inner_ttp:
            if props.ttp:
                if not table:
                    header = props.header
                if cell:
                    row.append(" · ".join(cell))
                    cell = []
                table.append(row)
                row = []
                continue
            if text:
                cell.append(text)
            cell.extend(a for a in after if not a.startswith("!["))
            if ch == "\x07":
                row.append(" · ".join(cell))
                cell = []
            continue
        if props.inner_ttp:
            continue
        if table or row or cell:
            close_table()
        if doc.toc(props.istd):
            continue

        level = doc.heading(props.istd)
        if props.outline is not None and props.outline < 9:
            level = props.outline + 1
        label = numbers.next(props.ilfo, props.ilvl) if props.ilfo > 0 and text else None
        indent = "  " * min(props.ilvl, 8)
        if text:
            if level:
                styled += 1
                if label and label[0] == "number":
                    text = mdtext.escape(label[1]) + " " + text
                out.append(mdtext.heading(level, text))
            elif label and label[0] == "number" and re.fullmatch(r"\d+[.)]", label[1]):
                out.append(indent + label[1][:-1] + ". " + text)
            elif label and label[0] == "number":
                out.append(indent + mdtext.line_start(mdtext.escape(label[1]) + " " + text))
            elif props.ilfo > 0 and not (label and label[0] == "none"):
                out.append(indent + "- " + text)
            else:
                plain = "".join(p.text for p in pieces if not p.raw)
                bold = all(p.bold or not p.text.strip() for p in pieces if not p.raw)
                if (bold and plain.strip() and len(plain.strip()) <= _MAYBE_LONGEST
                        and not plain.rstrip().endswith((".", ":", ";", ",", "!", "?"))):
                    out.append(("maybe", mdtext.line_start(text),
                                mdtext.escape(mdtext.squeeze(plain).strip())))
                else:
                    out.append(mdtext.paragraph(text))
        out.extend(after)
    close_table()

    guessed = styled == 0
    blocks = [
        (mdtext.heading(1, b[2]) if guessed else b[1]) if isinstance(b, tuple) else b
        for b in out
    ]
    result.guessed = guessed and any(isinstance(b, tuple) for b in out)
    if reader.notes:
        blocks.append(mdtext.heading(2, mdtext.escape(tr("Notes"))))
        blocks.extend("\\[%d\\] %s" % (n, text) for n, text in enumerate(reader.notes, 1))
    if reader.comments:
        blocks.append(mdtext.heading(2, mdtext.escape(tr("Comments"))))
        blocks.extend("**%s** — %s" % (mdtext.escape(tr("Comment {n}", {"n": n})), text)
                      for n, text in enumerate(reader.comments, 1))

    result.markdown = "\n\n".join(b for b in blocks if b.strip()) + "\n"
    result.comments = len(reader.comments)
    result.words = len(re.findall(r"\w+", re.sub(r"\(picture:[^)]*\)", "", result.markdown)))
    return result
