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

"""A compound document — the small FAT file system Word 97–2003 files live in.

The same reader the sheets plugin opens an .xls with, copied rather than shared
because a plugin is one folder that installs on its own. The file is 512-byte
sectors chained by a table; a stream is found by name in the directory and
read by following its chain, small streams from the mini stream.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_END = 0xFFFFFFFE


class CompoundError(ValueError):
    """A sentence about why the file cannot be read."""


class Compound:
    """The streams inside a compound document, by name."""

    def __init__(self, raw: bytes):
        if raw[:8] != MAGIC:
            raise CompoundError("not a compound document")
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
            if self._offset(s) + self.sector <= len(raw):
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

    def _chain(self, start: int, mini: bool = False) -> bytes:
        table = self.minifat if mini else self.fat
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

    def has(self, name: str) -> bool:
        return name in self.entries

    def stream(self, name: str) -> Optional[bytes]:
        found = self.entries.get(name)
        if found is None:
            return None
        start, size = found
        if size < self.cutoff:
            return self._chain(start, mini=True)[:size]
        return self._chain(start)[:size]
