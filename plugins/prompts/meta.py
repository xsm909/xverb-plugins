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

"""Every place a picture keeps text, read out of PNG, JPEG and WebP.

Each generator writes its record somewhere different, and none of them asked
the others:

| where | who writes there |
| --- | --- |
| PNG text chunks | AUTOMATIC1111 and Forge (`parameters`), ComfyUI (`prompt`, `workflow`), NovelAI (`Description`, `Comment`), InvokeAI (`invokeai_metadata`), Fooocus, SwarmUI |
| EXIF `UserComment` | AUTOMATIC1111 and Forge in a JPEG or a WebP |
| EXIF `Make` and `Model` | ComfyUI in a WebP — `workflow:{…}` and `prompt:{…}` |
| XMP, in a PNG as the text chunk `XML:com.adobe.xmp` | Draw Things, Midjourney |
| JPEG comment | some older scripts |

So this reads all of them and decides nothing: which of them is a prompt is
`readers.py`'s question. Pixels are walked past, never decoded.
"""

from __future__ import annotations

import html
import re
import struct
import zlib
from typing import Dict, List, Optional


class Found:
    """What a picture carries, as text, by where it was found."""

    def __init__(self, kind: str):
        self.kind = kind
        self.text: Dict[str, str] = {}
        self.exif: Dict[str, str] = {}
        self.xmp: Dict[str, str] = {}
        self.comments: List[str] = []

    @property
    def empty(self) -> bool:
        return not (self.text or self.exif or self.xmp or self.comments)


def read(data: bytes) -> Found:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png(data)
    if data[:2] == b"\xff\xd8":
        return _jpeg(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp(data)
    return Found("")


# -- PNG -------------------------------------------------------------------


def _png(data: bytes) -> Found:
    found = Found("PNG")
    at = 8
    while at + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, at)
        kind = data[at + 4:at + 8]
        body = data[at + 8:at + 8 + length]
        if len(body) < length or kind == b"IEND":
            break
        if kind in (b"tEXt", b"zTXt", b"iTXt"):
            pair = _png_text(kind, body)
            if pair and pair[0] == "XML:com.adobe.xmp":
                # XMP in a PNG is a text chunk under this one key.
                _xmp(pair[1].encode("utf-8"), found)
            elif pair and pair[0] not in found.text:
                found.text[pair[0]] = pair[1]
        elif kind == b"eXIf":
            _exif(body, found)
        at += 12 + length
    return found


def _png_text(kind: bytes, body: bytes):
    try:
        key, _, rest = body.partition(b"\0")
        name = key.decode("latin-1")
        if kind == b"tEXt":
            return name, _text(rest)
        if kind == b"zTXt":
            return name, _text(zlib.decompress(rest[1:]))
        compressed = rest[:1] == b"\x01"
        rest = rest[2:]
        _language, _, rest = rest.partition(b"\0")
        _translated, _, value = rest.partition(b"\0")
        if compressed:
            value = zlib.decompress(value)
        return name, value.decode("utf-8", "replace")
    except (zlib.error, ValueError):
        return None


def _text(raw: bytes) -> str:
    """tEXt is Latin-1 by the standard and UTF-8 by habit: a prompt with a
    Cyrillic or Japanese word in it is written as UTF-8 by nearly every tool,
    so UTF-8 is tried first and Latin-1 is the fallback that cannot fail."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


# -- JPEG ------------------------------------------------------------------


def _jpeg(data: bytes) -> Found:
    found = Found("JPEG")
    at = 2
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            break
        marker = data[at + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            at += 2
            continue
        if marker in (0xDA, 0xD9):  # the pixels begin, or the file ends
            break
        (length,) = struct.unpack_from(">H", data, at + 2)
        body = data[at + 4:at + 2 + length]
        if marker == 0xE1 and body.startswith(b"Exif\0\0"):
            _exif(body[6:], found)
        elif marker == 0xE1 and body.startswith(b"http://ns.adobe.com/xap/1.0/\0"):
            _xmp(body[29:], found)
        elif marker == 0xFE:
            comment = _text(body).strip("\0 \n")
            if comment:
                found.comments.append(comment)
        at += 2 + length
    return found


# -- WebP ------------------------------------------------------------------


def _webp(data: bytes) -> Found:
    found = Found("WebP")
    at = 12
    while at + 8 <= len(data):
        kind = data[at:at + 4]
        (size,) = struct.unpack_from("<I", data, at + 4)
        body = data[at + 8:at + 8 + size]
        if kind == b"EXIF":
            _exif(body[6:] if body.startswith(b"Exif\0\0") else body, found)
        elif kind == b"XMP ":
            _xmp(body, found)
        at += 8 + size + (size & 1)
    return found


# -- EXIF ------------------------------------------------------------------

#: The tags a generator writes into, and the name each goes by here.
TAGS = {
    0x010E: "ImageDescription",
    0x010F: "Make",
    0x0110: "Model",
    0x0131: "Software",
    0x013B: "Artist",
    0x9286: "UserComment",
    0x9C9C: "XPComment",
    0x9C9F: "XPSubject",
}


def _exif(tiff: bytes, found: Found) -> None:
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return
    order = "<" if tiff[:2] == b"II" else ">"
    try:
        (first,) = struct.unpack_from(order + "I", tiff, 4)
        seen = set()
        pending = [first]
        while pending:
            offset = pending.pop()
            if offset in seen or offset + 2 > len(tiff):
                continue
            seen.add(offset)
            (count,) = struct.unpack_from(order + "H", tiff, offset)
            for i in range(min(count, 512)):
                entry = offset + 2 + i * 12
                if entry + 12 > len(tiff):
                    break
                tag, kind, n = struct.unpack_from(order + "HHI", tiff, entry)
                if tag == 0x8769:  # the Exif IFD, where UserComment lives
                    pending.append(struct.unpack_from(order + "I", tiff, entry + 8)[0])
                    continue
                name = TAGS.get(tag)
                if name is None:
                    continue
                size = n * {1: 1, 2: 1, 7: 1}.get(kind, 0)
                if not size:
                    continue
                if size <= 4:
                    raw = tiff[entry + 8:entry + 8 + size]
                else:
                    (where,) = struct.unpack_from(order + "I", tiff, entry + 8)
                    raw = tiff[where:where + size]
                value = _exif_value(name, raw, order)
                if value:
                    found.exif[name] = value
    except struct.error:
        return


def _exif_value(name: str, raw: bytes, order: str) -> str:
    if name == "UserComment":
        return user_comment(raw)
    if name.startswith("XP"):
        return raw.decode("utf-16-le", "replace").strip("\0 ")
    return _text(raw).strip("\0 ")


def user_comment(raw: bytes) -> str:
    """UserComment names its encoding in its first eight bytes.

    `UNICODE` is UTF-16, and in which byte order is left to the writer —
    AUTOMATIC1111 writes big-endian through piexif, other tools little-endian,
    whatever the TIFF around it says. For a text that is mostly ASCII the
    zero bytes give the order away: they sit in front of each letter in
    big-endian and behind it in little-endian.
    """
    head, body = raw[:8], raw[8:]
    if head.startswith(b"UNICODE"):
        body = body[:len(body) & ~1]
        evens = body[0::2].count(0)
        odds = body[1::2].count(0)
        codec = "utf-16-be" if evens >= odds else "utf-16-le"
        return body.decode(codec, "replace").strip("\0 ")
    if head.startswith(b"ASCII") or head == b"\0" * 8:
        return _text(body).strip("\0 ")
    return _text(raw).strip("\0 ")


# -- XMP -------------------------------------------------------------------

# `rdf:Description` is the wrapper every XMP field sits in, not a field.
_ATTRIBUTE = re.compile(r'\b((?!rdf:)\w+:(?:description|UserComment|Description|Prompt))="([^"]*)"')
_ELEMENT = re.compile(
    r"<((?!rdf:)\w+:(?:description|UserComment|Description|Prompt))\b[^>]*>(.*?)</\1>", re.S)
_LI = re.compile(r"<rdf:li\b[^>]*>(.*?)</rdf:li>", re.S)


def _xmp(body: bytes, found: Found) -> None:
    """The few XMP fields a generator writes a prompt into, as text.

    Read with patterns rather than a parser: an XMP packet is XML inside a
    picture, often with a byte of padding or a namespace nobody declared, and
    the fields wanted here are three, always in one of two shapes.
    """
    text = body.decode("utf-8", "replace")
    for name, value in _ATTRIBUTE.findall(text):
        found.xmp.setdefault(name.split(":", 1)[1], html.unescape(value).strip())
    for name, inner in _ELEMENT.findall(text):
        items = _LI.findall(inner)
        value = items[0] if items else inner
        value = html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
        if value:
            found.xmp.setdefault(name.split(":", 1)[1], value)
