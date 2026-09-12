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

"""The container a `.blend` is, with no meaning attached to it.

A `.blend` is a dump of Blender's own memory: a header, a chain of blocks, and
`ENDB`. What makes it readable without a version table is that **the file
describes itself** — one block, `DNA1`, carries the C struct definitions of the
exact build that wrote it, so field offsets are worked out here at run time.

Nothing in this module knows what a mesh is. It answers three questions:

* what blocks are in the file, and under what four-letter code;
* what the struct behind a block looks like, by name, for this file;
* what block a pointer lands on.

That last one is the whole reason a `.blend` can be walked at all: the
addresses in it are the writing process's own, and every block records the
address it had.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

#: `BLENDER` then pointer size, endianness and three version digits.
MAGIC = b"BLENDER"

#: Blender can save compressed, and says which by the first bytes.
GZIP = b"\x1f\x8b"
ZSTD = b"\x28\xb5\x2f\xfd"


class BlendError(Exception):
    """The file is not a `.blend`, or is one this cannot read."""


#: What a DNA type name is read as. The sizes come from the file's own `TLEN`,
#: so this says the shape and the file says the width — `long` is four bytes in
#: every file measured and need not stay so.
_NUMBERS = {
    "char": "b",
    "uchar": "B",
    "short": "h",
    "ushort": "H",
    "int": "i",
    "uint": "I",
    "long": "i",
    "ulong": "I",
    "int8_t": "b",
    "uint8_t": "B",
    "int16_t": "h",
    "uint16_t": "H",
    "int32_t": "i",
    "uint32_t": "I",
    "int64_t": "q",
    "uint64_t": "Q",
    "float": "f",
    "double": "d",
}

#: Sizes that have to agree before a type name is read as that number. A file
#: whose `int` is not four bytes is not one to guess at.
_WIDTHS = {"b": 1, "B": 1, "h": 2, "H": 2, "i": 4, "I": 4,
           "q": 8, "Q": 8, "f": 4, "d": 8}


def bare_name(name: str) -> str:
    """`*mvert` -> `mvert`, `co[3]` -> `co`, `(*doit)()` -> `doit`."""
    name = name.strip()
    while name.startswith("*"):
        name = name[1:]
    if name.startswith("(") and name.endswith(")()"):
        name = name[1:-3].lstrip("*")
    cut = name.find("[")
    if cut >= 0:
        name = name[:cut]
    return name


def _elements(name: str) -> int:
    """How many of it there are: `obmat[4][4]` is sixteen."""
    total = 1
    rest = name
    while "[" in rest:
        open_at = rest.index("[")
        close_at = rest.find("]", open_at)
        if close_at < 0:
            break
        try:
            total *= int(rest[open_at + 1:close_at])
        except ValueError:
            return total
        rest = rest[close_at + 1:]
    return total


class Field:
    """One member of a struct, as this file lays it out.

    `offset` and `size` are computed from the file's own DNA, never from a
    table of what some Blender version is supposed to look like.
    """

    __slots__ = ("name", "bare", "type", "type_index", "offset", "size",
                 "pointer", "count")

    def __init__(self, name: str, type_name: str, type_index: int, offset: int,
                 size: int, pointer: bool, count: int):
        self.name = name
        #: The name with the `*`, `[n]` and function-pointer clutter taken off,
        #: which is what anything asking for a field says.
        self.bare = bare_name(name)
        self.type = type_name
        self.type_index = type_index
        self.offset = offset
        self.size = size
        self.pointer = pointer
        #: How many elements, for `co[3]` or `obmat[4][4]`. 1 for a plain one.
        self.count = count

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "<Field %s %s at %d>" % (self.type, self.name, self.offset)


class Struct:
    """A C struct as this file writes it: its size, and its fields by name."""

    __slots__ = ("name", "size", "fields", "order")

    def __init__(self, name: str, size: int, fields: List[Field]):
        self.name = name
        self.size = size
        self.order = fields
        self.fields: Dict[str, Field] = {}
        for field in fields:
            # First wins: a name cannot legally repeat, and if a malformed file
            # repeats one the earlier offset is what the struct was built
            # around.
            self.fields.setdefault(field.bare, field)

    def field(self, name: str) -> Optional[Field]:
        return self.fields.get(name)

    def has(self, *names: str) -> bool:
        return all(name in self.fields for name in names)

    def first(self, *names: str) -> Optional[str]:
        """The first of these names the struct actually has.

        This is how the two shapes of `Mesh` are told apart without asking the
        version: `mesh.first("verts_num", "totvert")`.
        """
        for name in names:
            if name in self.fields:
                return name
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "<Struct %s %d bytes, %d fields>" % (
            self.name, self.size, len(self.fields))


class SDNA:
    """Every struct the writing build had, keyed by type name."""

    __slots__ = ("names", "types", "lengths", "structs", "by_name", "by_type")

    def __init__(self, names, types, lengths, structs, by_name, by_type):
        self.names = names
        self.types = types
        self.lengths = lengths
        #: By SDNA index, which is what a block header carries.
        self.structs: List[Struct] = structs
        self.by_name: Dict[str, Struct] = by_name
        #: Type index -> SDNA index, for stepping into an embedded struct.
        self.by_type: Dict[int, int] = by_type

    def struct(self, name: str) -> Optional[Struct]:
        return self.by_name.get(name)

    def of_type(self, type_index: int) -> Optional[Struct]:
        at = self.by_type.get(type_index)
        return self.structs[at] if at is not None else None


def _aligned(at: int) -> int:
    return at + ((4 - (at % 4)) % 4)


def _tag(data: bytes, at: int, expected: bytes) -> int:
    if data[at:at + 4] != expected:
        raise BlendError(
            "The DNA block is malformed: expected %s where the file has %r."
            % (expected.decode("ascii"), data[at:at + 4]))
    return at + 4


def parse_sdna(data: bytes, order: str, pointer_size: int) -> SDNA:
    """The `DNA1` block: names, type names, type sizes, and the structs.

    Laid out as four tagged runs — `NAME`, `TYPE`, `TLEN`, `STRC` — each padded
    up to a four-byte boundary before the next begins.
    """
    at = _tag(data, 0, b"SDNA")

    at = _tag(data, at, b"NAME")
    count = struct.unpack_from(order + "i", data, at)[0]
    at += 4
    names: List[str] = []
    for _ in range(count):
        end = data.index(b"\0", at)
        names.append(data[at:end].decode("ascii", "replace"))
        at = end + 1
    at = _aligned(at)

    at = _tag(data, at, b"TYPE")
    count = struct.unpack_from(order + "i", data, at)[0]
    at += 4
    types: List[str] = []
    for _ in range(count):
        end = data.index(b"\0", at)
        types.append(data[at:end].decode("ascii", "replace"))
        at = end + 1
    at = _aligned(at)

    at = _tag(data, at, b"TLEN")
    lengths = list(struct.unpack_from(order + "%dh" % len(types), data, at))
    at += 2 * len(types)
    at = _aligned(at)

    at = _tag(data, at, b"STRC")
    total = struct.unpack_from(order + "i", data, at)[0]
    at += 4

    structs: List[Struct] = []
    by_name: Dict[str, Struct] = {}
    by_type: Dict[int, int] = {}
    for index in range(total):
        type_index, field_count = struct.unpack_from(order + "2h", data, at)
        at += 4
        offset = 0
        fields: List[Field] = []
        for _ in range(field_count):
            field_type, field_name = struct.unpack_from(order + "2h", data, at)
            at += 4
            name = names[field_name] if 0 <= field_name < len(names) else "?"
            type_name = types[field_type] if 0 <= field_type < len(types) else "?"
            # A pointer is a pointer whatever it points at, and a function
            # pointer is one too — both are the file's word size, never the
            # size of the thing named.
            pointer = name.startswith("*") or name.startswith("(*")
            elements = _elements(name)
            one = pointer_size if pointer else (
                lengths[field_type] if 0 <= field_type < len(lengths) else 0)
            fields.append(Field(name, type_name, field_type, offset,
                                one * elements, pointer, elements))
            offset += one * elements
        name = types[type_index] if 0 <= type_index < len(types) else "?"
        size = lengths[type_index] if 0 <= type_index < len(lengths) else offset
        made = Struct(name, size, fields)
        structs.append(made)
        by_name.setdefault(name, made)
        by_type.setdefault(type_index, index)

    return SDNA(names, types, lengths, structs, by_name, by_type)


class Block:
    """One block: its four-letter code, where its bytes are, and what shape.

    `count` is how many of the struct the block holds — a `DATA` block behind
    `Mesh.mvert` is one block of eight thousand `MVert`.
    """

    __slots__ = ("code", "length", "address", "sdna_index", "count", "at")

    def __init__(self, code: bytes, length: int, address: int,
                 sdna_index: int, count: int, at: int):
        self.code = code
        self.length = length
        self.address = address
        self.sdna_index = sdna_index
        self.count = count
        #: Where the block's own bytes start in the file.
        self.at = at

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "<Block %r %d bytes at %d>" % (self.code, self.length, self.at)


class BlendFile:
    """A `.blend`, opened: its blocks, its DNA, and its addresses.

    Walking the chain is eager and reading the bytes behind it is lazy: 106 222
    block headers cost a tenth of a second on the heaviest file measured, and
    nothing behind them is touched unless something asks.
    """

    def __init__(self, data: bytes):
        self.data = decompress(data)
        self.compressed = len(self.data) != len(data)
        (self.pointer_size, self.order, self.version,
         self._starts_at, self._layout) = read_header(self.data)
        self.blocks: List[Block] = []
        self.by_address: Dict[int, Block] = {}
        self.truncated = False
        self._all_at: Dict[int, List[Block]] = {}
        self._by_code: Dict[bytes, List[Block]] = {}
        self._walk()
        self.sdna = self._read_dna()

    # -- the chain ---------------------------------------------------------

    @property
    def header_size(self) -> int:
        """How many bytes a block header takes in this file.

        Up to 4.x it is the code, the length, the address, the SDNA index and
        the count, packed with no slack: 16 bytes plus one pointer. Blender 5
        made it a flat 32 with the SDNA index moved up behind the code and two
        four-byte holes left spare.
        """
        return 32 if self._layout == 5 else 16 + self.pointer_size

    def _walk(self) -> None:
        data = self.data
        order = self.order
        pointer = order + ("Q" if self.pointer_size == 8 else "I")
        header = self.header_size
        five = self._layout == 5
        total = len(data)
        at = self._starts_at
        while at + header <= total:
            code = data[at:at + 4]
            if code == b"ENDB":
                return
            if five:
                sdna_index = struct.unpack_from(order + "i", data, at + 4)[0]
                address = struct.unpack_from(order + "Q", data, at + 8)[0]
                length, count = (
                    struct.unpack_from(order + "i", data, at + 16)[0],
                    struct.unpack_from(order + "i", data, at + 24)[0])
            else:
                length = struct.unpack_from(order + "i", data, at + 4)[0]
                address = struct.unpack_from(pointer, data, at + 8)[0]
                sdna_index, count = struct.unpack_from(
                    order + "2i", data, at + 8 + self.pointer_size)
            if length < 0 or at + header + length > total:
                # A file cut short is read as far as it goes rather than
                # refused: what was walked is a real answer about what is in
                # it, and the caller is told the chain ended early.
                break
            block = Block(code, length, address, sdna_index, count, at + header)
            self.blocks.append(block)
            # **An address does not name one block.** Blender frees a buffer
            # and allocates another at the same place between two passes of
            # writing, and both go into the file carrying that address. It is
            # rare — one address in a file that has it at all — and it is not
            # harmless: the mesh in the file that taught this had its faces at
            # an address shared with a UV layer of exactly the same length, so
            # keeping one block an address silently lost them.
            #
            # So every block is kept, and a caller that knows what it is
            # looking for can say so. The plain lookup keeps the old meaning:
            # the last one written.
            self.by_address[address] = block
            self._all_at.setdefault(address, []).append(block)
            self._by_code.setdefault(code, []).append(block)
            at += header + length
        self.truncated = True

    def _read_dna(self) -> SDNA:
        found = self._by_code.get(b"DNA1")
        if not found:
            raise BlendError(
                "This file carries no DNA1 block, so nothing in it can be "
                "read: a .blend says what its own structs look like, and this "
                "one does not.")
        return parse_sdna(self.bytes_of(found[0]), self.order,
                          self.pointer_size)

    # -- looking things up -------------------------------------------------

    def bytes_of(self, block: Block) -> bytes:
        return self.data[block.at:block.at + block.length]

    def of_code(self, *codes: bytes) -> List[Block]:
        """Every block under these codes, which are asked for as written.

        A code is four bytes and a two-letter one is padded with zeros, so
        `of_code(b"ME")` has to mean `ME\\0\\0` or it silently finds nothing —
        which looks exactly like a file with no meshes in it.
        """
        out: List[Block] = []
        for code in codes:
            out.extend(self._by_code.get(code.ljust(4, b"\0"), ()))
        return out

    def codes(self) -> Dict[bytes, int]:
        """Every code in the file and how many blocks carry it."""
        return {code: len(blocks) for code, blocks in self._by_code.items()}

    def struct_of(self, block: Block) -> Optional[Struct]:
        if 0 <= block.sdna_index < len(self.sdna.structs):
            return self.sdna.structs[block.sdna_index]
        return None

    def follow(self, block: Block, path: str, expect: Optional[str] = None,
               index: int = 0) -> Optional[Block]:
        """Resolve a pointer field of `block` to the block it means.

        **Which block, where an address names several.** Blender 5 writes every
        mesh's attribute list out of one reused buffer, so all of them record
        the same old address and the file holds one block an attribute list,
        all at that address. The one a given mesh means is the one written
        after it — a writer puts down the struct and then what it points at —
        so the search starts from the pointing block's own place in the file.
        """
        address = self.pointer(block, path, index, default=0) or 0
        return self.at_address(address, expect, after=block.at)

    def at_address(self, address: int, expect: Optional[str] = None,
                   after: Optional[int] = None) -> Optional[Block]:
        """The block a pointer lands on, or None.

        **A pointer that does not resolve is normal** — it pointed at something
        the writer chose not to save — and is never an error. Nor is a pointer
        that resolves proof of anything: check what came back. In the
        linked-library files every mesh object's `data` resolves perfectly, to
        an `ID` placeholder; and in another file the pointer to a mesh's faces
        resolves onto a UV layer of the same length, because a freed buffer was
        written twice under one address.

        `expect` is the answer to both: name the struct wanted and only a block
        written as that struct comes back. A pointer is typed in the C the file
        was dumped from, so the caller always knows.
        """
        if not address:
            return None
        if expect is None and after is None:
            return self.by_address.get(address)

        candidates = self._all_at.get(address, ())
        if expect is not None:
            candidates = [block for block in candidates
                          if (self.struct_of(block) is not None
                              and self.struct_of(block).name == expect)]
        if not candidates:
            return None
        if after is not None:
            for block in candidates:
                if block.at > after:
                    return block
        # Nothing after it: the pointer names something written earlier, which
        # is ordinary for a shared buffer two datablocks both point at. The
        # last one written is then the live one, as it always was.
        return candidates[-1]

    # -- reading a field ---------------------------------------------------

    def _walk_path(self, block: Block, path: str):
        """Follow `id.name` down to the struct and field that finally hold it.

        Returns the field and the offset it sits at within the block, or None
        where any step of the path is not in this file's DNA — which is the
        answer for a field a different Blender version had and this one does
        not, and is never an error.
        """
        shape = self.struct_of(block)
        if shape is None:
            return None
        at = 0
        field = None
        for step in path.split("."):
            if shape is None:
                return None
            field = shape.field(step)
            if field is None:
                return None
            at += field.offset
            shape = None if field.pointer else \
                self.sdna.of_type(field.type_index)
        return (field, at) if field is not None else None

    def value(self, block: Block, path: str, index: int = 0,
              default=None):
        """One number out of a block, by the name its struct gives it.

        `index` picks an element of an array field, so `obmat` is read one
        number at a time and `co` three.
        """
        found = self._walk_path(block, path)
        if found is None:
            return default
        field, at = found
        if field.pointer:
            return self.pointer(block, path, index, default)
        shape = _NUMBERS.get(field.type)
        if shape is None:
            return default
        width = _WIDTHS[shape]
        if field.count and field.size // field.count != width:
            return default
        at += index * width
        if at + width > block.length:
            return default
        return struct.unpack_from(self.order + shape, self.data,
                                  block.at + at)[0]

    def numbers(self, block: Block, path: str, count: int = 0) -> List:
        """A whole array field — `co[3]`, `obmat[4][4]` — in one read."""
        found = self._walk_path(block, path)
        if found is None:
            return []
        field, at = found
        if field.pointer:
            return []
        shape = _NUMBERS.get(field.type)
        if shape is None:
            return []
        width = _WIDTHS[shape]
        wanted = count or field.count
        if at + wanted * width > block.length:
            return []
        return list(struct.unpack_from(self.order + "%d%s" % (wanted, shape),
                                       self.data, block.at + at))

    def pointer(self, block: Block, path: str, index: int = 0, default=None):
        """The address a pointer field holds. Resolve it with `at_address`."""
        found = self._walk_path(block, path)
        if found is None:
            return default
        field, at = found
        if not field.pointer:
            return default
        width = self.pointer_size
        at += index * width
        if at + width > block.length:
            return default
        shape = "Q" if width == 8 else "I"
        return struct.unpack_from(self.order + shape, self.data,
                                  block.at + at)[0]

    def string(self, block: Block, path: str, limit: int = 0) -> str:
        """A `char[]` field, cut at its first zero.

        Blender writes its paths as bytes with no encoding said anywhere, and a
        name typed on a Russian or German keyboard is in whatever that machine
        used. UTF-8 first, because that is what Blender itself writes, and the
        machine's own reading is not guessed at — a name that will not decode
        keeps its readable parts rather than becoming an error.
        """
        found = self._walk_path(block, path)
        if found is None:
            return ""
        field, at = found
        if field.pointer:
            return ""
        wide = limit or field.count
        if at + wide > block.length:
            wide = max(0, block.length - at)
        raw = self.data[block.at + at:block.at + at + wide]
        end = raw.find(b"\0")
        if end >= 0:
            raw = raw[:end]
        return raw.decode("utf-8", "replace")

    def name_of(self, block: Block) -> str:
        """What a datablock is called, without the two-letter code in front.

        Every `ID` begins `char name[66]`, and its first two characters are the
        code — `OBCube`, `MECube`. Callers want `Cube`.
        """
        text = self.string(block, "id.name") or self.string(block, "name")
        return text[2:] if len(text) > 2 and text[:2].isupper() else text


def decompress(data: bytes) -> bytes:
    """The file's bytes, whatever it was saved as.

    gzip costs nothing in either runtime. zstd, which Blender 3.0 and later
    offer and which is the default where compression is asked for at all, has
    no decoder in the standard library before Python 3.14 — so it is named
    rather than guessed at.
    """
    if data[:2] == GZIP:
        import gzip
        try:
            return gzip.decompress(data)
        except Exception as failure:  # noqa: BLE001 - a bad archive is not a crash
            raise BlendError("This file is gzip-compressed and the "
                             "compression is damaged: %s" % failure)
    if data[:4] == ZSTD:
        try:
            from compression import zstd  # Python 3.14 and later
            return zstd.decompress(data)
        except ImportError:
            pass
        try:
            import zstandard
        except ImportError:
            raise BlendError(
                "This file is saved with zstd compression, which the Python "
                "running this plugin cannot decompress. Saving it "
                "uncompressed, or with zlib compression, makes it readable.")
        import io
        return zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(data)).read()
    return data


def read_header(data: bytes) -> Tuple[int, str, int, int, int]:
    """Pointer size, byte order, version, where the blocks start, which layout.

    Two headers exist and the second announces itself by its own length.

    Up to 4.x: `BLENDER`, then `_` for a 32-bit pointer or `-` for 64, then `v`
    or `V` for the byte order, then three version digits — twelve bytes.

    Blender 5 writes seventeen: `BLENDER17-01v0501`. The `17` is the length of
    the header itself, which is how a reader can tell without knowing anything
    else; `-` and `v` keep their old meanings in their new places; `01` is a
    revision of the format; and the version is four digits, so `0501` is 5.1
    and reads with the same arithmetic the three-digit form does.
    """
    if data[:7] != MAGIC:
        raise BlendError("This is not a .blend file: it does not begin with "
                         "BLENDER.")

    mark = data[7:8]
    if mark not in (b"_", b"-"):
        return _read_long_header(data)

    pointer_size = 8 if mark == b"-" else 4
    endian = data[8:9]
    if endian not in (b"v", b"V"):
        raise BlendError("This .blend does not say which way round its "
                         "numbers are: byte nine is %r." % endian)
    try:
        version = int(data[9:12])
    except ValueError:
        raise BlendError("This .blend does not carry a version number where "
                         "one belongs: %r." % data[9:12])
    return pointer_size, ("<" if endian == b"v" else ">"), version, 12, 4


def _read_long_header(data: bytes) -> Tuple[int, str, int, int, int]:
    """The Blender 5 header, which begins by saying how long it is."""
    try:
        size = int(data[7:9])
    except ValueError:
        raise BlendError(
            "This file begins with BLENDER but nothing this can read follows "
            "it (%r). It may be a newer .blend than this knows about."
            % data[:16])
    if size < 13 or size > 64 or len(data) < size:
        raise BlendError(
            "This .blend says its header is %d bytes, which is not a length "
            "this can read." % size)

    mark = data[9:10]
    if mark not in (b"_", b"-"):
        raise BlendError("This .blend does not say how wide its pointers are: "
                         "byte ten is %r." % mark)
    endian = data[12:13]
    if endian not in (b"v", b"V"):
        raise BlendError("This .blend does not say which way round its "
                         "numbers are: byte thirteen is %r." % endian)
    try:
        version = int(data[13:size])
    except ValueError:
        raise BlendError("This .blend does not carry a version number where "
                         "one belongs: %r." % data[13:size])
    return (8 if mark == b"-" else 4, "<" if endian == b"v" else ">",
            version, size, 5)


def version_text(version: int) -> str:
    """`306` -> `3.6`, `404` -> `4.4`. Blender's own three digits."""
    return "%d.%d" % (version // 100, version % 100)
