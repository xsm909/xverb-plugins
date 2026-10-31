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

"""What a `.fig` is, and how to get the document out of it.

Three layers, and each one is somebody else's format:

1. a **zip**, holding `canvas.fig`, the `thumbnail.png` Figma rendered when the
   file was saved, a `meta.json`, and the pictures used in the design under
   `images/`, named by the hash of their contents. An older `.fig` is the
   middle layer on its own, with no zip around it.
2. `canvas.fig` is **`fig-kiwi`**: eight bytes naming the format, a version,
   then length-prefixed chunks. The first chunk is the Kiwi schema, the second
   the document. Both were deflate once; the document is zstd in anything
   recent, which is why this plugin carries a zstd reader.
3. the document is a **Kiwi message** — see `kiwi.py`.

A FigJam board is the same thing with `fig-jam.` in front instead, so it is
read by the same code and the plugin claims `.jam` as well.

What comes out is a flat list of node changes, which is how Figma stores a
document: every node carries its own `guid` and the `guid` of its parent, plus
a `position` string that orders it among its siblings. The tree is rebuilt from
those here, because everything else — the drawing, the report — needs it.
"""

from __future__ import annotations

import io
import json
import struct
import zipfile
import zlib

import kiwi
import zstd

MAGIC_FIG = b"fig-kiwi"
MAGIC_JAM = b"fig-jam."
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

#: Nodes that stand for a page rather than for something drawn on one.
PAGE = "CANVAS"
DOCUMENT = "DOCUMENT"

#: A node that is a page's frame of reference rather than a shape.
STRUCTURAL = (DOCUMENT, PAGE, "VARIABLE_SET", "SYMBOL")


class FigError(Exception):
    """The file is not a Figma file, or is damaged."""


def chunks(data: bytes, progress=None) -> tuple[str, int, list[bytes]]:
    """The `fig-kiwi` layer: what it is, its version, and its chunks.

    ``progress`` is handed on to the zstd reader for the document chunk, which
    is the one worth watching: the schema in front of it is a tenth of its
    size and unpacks in a blink.
    """
    head = data[:8]
    if head == MAGIC_FIG:
        kind = "fig"
    elif head == MAGIC_JAM:
        kind = "jam"
    else:
        raise FigError("this is not a Figma document: it begins %r"
                       % data[:8])
    if len(data) < 12:
        raise FigError("the document is eight bytes of name and nothing else")
    (version,) = struct.unpack_from("<I", data, 8)

    out = []
    at = 12
    while at + 4 <= len(data):
        (size,) = struct.unpack_from("<I", data, at)
        at += 4
        raw = data[at:at + size]
        if len(raw) != size:
            raise FigError("a chunk of %d bytes with only %d left"
                           % (size, len(raw)))
        at += size
        try:
            if raw[:4] == ZSTD_MAGIC:
                out.append(zstd.decompress(
                    raw, progress=progress if out else None))
            else:
                out.append(zlib.decompressobj(-15).decompress(raw))
        except (zlib.error, zstd.ZstdError) as failure:
            raise FigError("a chunk could not be unpacked: %s" % failure) from None
    if len(out) < 2:
        raise FigError("the document has no schema and body in it")
    return kind, version, out


def guid_of(value) -> tuple[int, int] | None:
    """A node's identity as something that can be a key.

    Kiwi leaves a field out when it is zero, so both halves have a default
    and the session alone is not enough to tell two nodes apart.
    """
    if not isinstance(value, dict):
        return None
    return (value.get("sessionID", 0), value.get("localID", 0))


class Figma:
    """One Figma document, with its pictures and its tree.

    Reading stops at the document: nothing here resolves a style, a variable or
    a component — those are each a pass over the nodes and belong to whatever
    asked for them.
    """

    def __init__(self, data: bytes, progress=None) -> None:
        self.kind = "fig"
        self.version = 0
        self.thumbnail: bytes | None = None
        self.meta: dict = {}
        self.images: dict[str, int] = {}
        self.archived = False

        body = data
        if data[:2] == b"PK":
            body = self._unzip(data)
        if progress:
            progress(0.05)

        # The document's own unpacking is most of the wait, so it owns most
        # of the bar: from a twentieth of the way along to three quarters.
        self.kind, self.version, parts = chunks(
            body, progress=(lambda f: progress(0.05 + 0.70 * f))
            if progress else None)
        if progress:
            progress(0.80)
        schema = kiwi.Schema(kiwi.parse_schema(parts[0]))
        message = schema.decode(parts[1])
        if progress:
            progress(0.98)

        self.nodes: list[dict] = message.get("nodeChanges") or []
        self.blobs: list[bytes] = [b.get("bytes", b"")
                                   for b in (message.get("blobs") or [])]

        self.by_guid: dict[tuple[int, int], dict] = {}
        for node in self.nodes:
            guid = guid_of(node.get("guid"))
            if guid is not None:
                self.by_guid[guid] = node
        self._children: dict[tuple[int, int], list[dict]] = {}
        for node in self.nodes:
            parent = node.get("parentIndex") or {}
            guid = guid_of(parent.get("guid"))
            if guid is None:
                continue
            self._children.setdefault(guid, []).append(node)
        for row in self._children.values():
            # Siblings are ordered by a string that sorts between its
            # neighbours, so inserting a layer never renumbers the rest.
            row.sort(key=lambda n: (n.get("parentIndex") or {}).get("position", ""))

    def _unzip(self, data: bytes) -> bytes:
        self.archived = True
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                canvas = "canvas.fig"
                if canvas not in names:
                    found = [n for n in names if n.endswith(".fig")]
                    if not found:
                        raise FigError("the archive holds no canvas.fig")
                    canvas = found[0]
                for name in names:
                    if name.startswith("images/") and not name.endswith("/"):
                        self.images[name[len("images/"):]] = \
                            archive.getinfo(name).file_size
                if "thumbnail.png" in names:
                    self.thumbnail = archive.read("thumbnail.png")
                if "meta.json" in names:
                    try:
                        self.meta = json.loads(archive.read("meta.json"))
                    except ValueError:
                        self.meta = {}
                return archive.read(canvas)
        except zipfile.BadZipFile as failure:
            raise FigError("the file looks like a zip but is not one: %s"
                           % failure) from None

    # -- the tree ---------------------------------------------------------

    def children(self, node: dict) -> list[dict]:
        guid = guid_of(node.get("guid"))
        if guid is None:
            return []
        return self._children.get(guid, [])

    @property
    def document(self) -> dict | None:
        for node in self.nodes:
            if node.get("type") == DOCUMENT:
                return node
        return None

    @property
    def pages(self) -> list[dict]:
        """The pages, in the order the tabs are in."""
        document = self.document
        if document is not None:
            pages = [n for n in self.children(document) if n.get("type") == PAGE]
            if pages:
                return pages
        return [n for n in self.nodes if n.get("type") == PAGE]

    def descendants(self, node: dict):
        """Everything under a node, parents before children."""
        stack = list(self.children(node))
        while stack:
            current = stack.pop()
            yield current
            stack.extend(self.children(current))

    # -- what the file says about itself ----------------------------------

    @property
    def name(self) -> str:
        return self.meta.get("file_name") or ""

    @property
    def exported_at(self) -> str:
        return self.meta.get("exported_at") or ""

    @property
    def thumbnail_size(self) -> tuple[int, int] | None:
        size = (self.meta.get("client_meta") or {}).get("thumbnail_size") or {}
        if "width" in size and "height" in size:
            return int(size["width"]), int(size["height"])
        return None

    @property
    def background(self) -> dict | None:
        return (self.meta.get("client_meta") or {}).get("background_color")


def read(data: bytes, progress=None) -> Figma:
    return Figma(data, progress=progress)
