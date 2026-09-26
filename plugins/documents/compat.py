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

"""What this plugin needs from the SDK, on an application that may be older.

`Picture`, `picture_size` and `Plugin.document` came with 1.1.0.501. The SDK
ships inside the application, so on 1.1.0.500 and before they are not there —
and a plugin that imported them outright would not start at all. Here they are
taken from the SDK when it has them and written out when it does not; `main`
asks `modern()` and, on an older application, answers plain Markdown instead.
"""

from __future__ import annotations

import re
import struct
from typing import Callable, Optional, Tuple, Union

try:
    from xverb import Picture, picture_size  # noqa: F401
except ImportError:  # an application older than 1.1.0.501

    def picture_size(data: bytes) -> Optional[Tuple[int, int]]:
        try:
            if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
                return struct.unpack(">II", data[16:24])
            if data[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", data[6:10])
            if data[:2] == b"BM" and len(data) >= 26:
                width, height = struct.unpack("<ii", data[18:26])
                return width, abs(height)
            if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                chunk = data[12:16]
                if chunk == b"VP8 ":
                    width, height = struct.unpack("<HH", data[26:30])
                    return width & 0x3FFF, height & 0x3FFF
                if chunk == b"VP8L":
                    bits = int.from_bytes(data[21:25], "little")
                    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
                if chunk == b"VP8X":
                    return (int.from_bytes(data[24:27], "little") + 1,
                            int.from_bytes(data[27:30], "little") + 1)
                return None
            if data[:2] == b"\xff\xd8":
                at = 2
                while at + 9 < len(data):
                    if data[at] != 0xFF:
                        return None
                    marker = data[at + 1]
                    if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                        at += 2
                        continue
                    length = struct.unpack(">H", data[at + 2:at + 4])[0]
                    if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                        height, width = struct.unpack(">HH", data[at + 5:at + 9])
                        return width, height
                    at += 2 + length
        except (struct.error, IndexError):
            return None
        return None

    class Picture:  # type: ignore[no-redef]
        def __init__(self, data: Union[bytes, Callable[[], Optional[bytes]]],
                     width: Optional[int] = None, height: Optional[int] = None):
            self.data = data
            if (width is None or height is None) and isinstance(data, (bytes, bytearray)):
                found = picture_size(bytes(data))
                if found is not None:
                    width, height = found
            self.width = width
            self.height = height

        def read(self) -> Optional[bytes]:
            data = self.data
            return bytes(data) if isinstance(data, (bytes, bytearray)) else data()


def modern(plugin) -> bool:
    """Whether the application draws a document's pictures and reads its
    escapes — 1.1.0.501 and later."""
    return hasattr(plugin, "document")


_ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")


def for_older(text: str, picture: str = "picture") -> str:
    """[text] for an application that reads no escapes: each escaped mark as
    the character it stands for — a bar inside a table cell as a broken bar,
    so that it does not split the cell — and each picture as its caption."""
    text = text.replace("\\|", "¦")
    text = re.sub(r"!\[([^\]]*)\]\(picture:[^)]*\)", lambda m: "*[%s]*" % (m.group(1) or picture), text)
    return _ESCAPED.sub(r"\1", text)
