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

"""The objects inside an Unreal package: the export map, and each object's
tagged properties.

**An object is its tagged properties, then whatever its class writes by
hand.** The tagged part describes itself — a name, a type, a size — so it can
be read without knowing the class; the part after it cannot, and the readers
that need it (`ueanim.py`) know the one or two classes they read.

Three shapes of tag, by version: before UE 5.4 a type is a name with its
parameters after it (a struct's name, an array's inner type); from UE5 1011
an object starts with a byte of serialization flags and a tag may carry
extension flags; from 1012 a type is a small tree of names — `ArrayProperty`
over `StructProperty` over `BoneNode` over its package — and a byte of flags
says which optional parts follow.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

from uasset import Package, UassetError, _Reader

UE4_STRUCT_GUID_IN_PROPERTY_TAG = 441
UE4_ARRAY_PROPERTY_INNER_TAGS = 282
UE4_PROPERTY_GUID_IN_PROPERTY_TAG = 503
UE4_PROPERTY_TAG_SET_MAP_SUPPORT = 506
UE4_TEMPLATE_INDEX_IN_COOKED_EXPORTS = 508
UE4_64BIT_EXPORTMAP_SERIALSIZES = 511
UE4_LOAD_FOR_EDITOR_GAME = 365
UE4_COOKED_ASSETS_IN_EDITOR_SUPPORT = 485
UE4_PRELOAD_DEPENDENCIES = 507
UE5_LARGE_WORLD_COORDINATES = 1004
UE5_REMOVE_OBJECT_EXPORT_PACKAGE_GUID = 1005
UE5_TRACK_OBJECT_EXPORT_IS_INHERITED = 1006
UE5_OPTIONAL_RESOURCES = 1003
UE5_SCRIPT_SERIALIZATION_OFFSET = 1010
UE5_PROPERTY_TAG_EXTENSION = 1011
UE5_PROPERTY_TAG_COMPLETE_TYPE_NAME = 1012

#: EPropertyTagFlags, from 1012.
_HAS_ARRAY_INDEX = 0x01
_HAS_GUID = 0x02
_HAS_EXTENSIONS = 0x04
_BINARY_OR_NATIVE = 0x08
_BOOL_TRUE = 0x10


class Export:
    __slots__ = ("index", "klass", "name", "outer", "size", "offset")

    def __init__(self, index, klass, name, outer, size, offset):
        self.index = index
        self.klass = klass
        self.name = name
        self.outer = outer
        self.size = size
        self.offset = offset

    def __repr__(self):
        return "Export(%s %s)" % (self.klass, self.name)


def exports(package: Package) -> List[Export]:
    """The export map: each object's class, name, and where its bytes are."""
    p = package
    imports = p.imports()
    r = _Reader(p.data, p.export_offset)
    out: List[Export] = []

    def class_of(index: int) -> str:
        if index < 0 and -index - 1 < len(imports):
            return imports[-index - 1][1]
        if index > 0:
            return "export %d" % index
        return ""

    for i in range(p.export_count):
        klass = r.i32()
        r.i32()  # super
        if p.ue4 >= UE4_TEMPLATE_INDEX_IN_COOKED_EXPORTS:
            r.i32()
        outer = r.i32()
        name = p.name(r.i32(), r.i32())
        r.u32()  # object flags
        if p.ue4 >= UE4_64BIT_EXPORTMAP_SERIALSIZES:
            size, offset = r.i64(), r.i64()
        else:
            size, offset = r.i32(), r.i32()
        r.skip(12)  # forced export, not for client, not for server
        if p.ue5 < UE5_REMOVE_OBJECT_EXPORT_PACKAGE_GUID:
            r.skip(16)
        if p.ue5 >= UE5_TRACK_OBJECT_EXPORT_IS_INHERITED:
            r.skip(4)
        r.skip(4)  # package flags
        if p.ue4 >= UE4_LOAD_FOR_EDITOR_GAME:
            r.skip(4)
        if p.ue4 >= UE4_COOKED_ASSETS_IN_EDITOR_SUPPORT:
            r.skip(4)
        if p.ue5 >= UE5_OPTIONAL_RESOURCES:
            r.skip(4)
        if p.ue4 >= UE4_PRELOAD_DEPENDENCIES:
            r.skip(20)
        if p.ue5 >= UE5_SCRIPT_SERIALIZATION_OFFSET:
            r.skip(16)
        out.append(Export(i + 1, class_of(klass), name, outer, size, offset))
    return out


class Tag:
    """One property: its name, type (with parameters), and where its value is."""

    __slots__ = ("name", "type", "params", "size", "index", "at", "bool", "binary")

    def __init__(self, name, type_, params, size, index, at, boolean, binary):
        self.name = name
        self.type = type_
        #: A struct's name, an array's or set's inner type, a map's two.
        self.params = params
        self.size = size
        self.index = index
        self.at = at
        self.bool = boolean
        self.binary = binary

    def __repr__(self):
        return "Tag(%s: %s %s)" % (self.name, self.type, self.params)


def _fname(p: Package, r: _Reader) -> str:
    return p.name(r.i32(), r.i32())


def _type_tree(p: Package, r: _Reader) -> Tuple[str, list]:
    name = _fname(p, r)
    count = r.i32()
    if not 0 <= count < 64:
        raise UassetError("A property type is damaged.")
    return name, [_type_tree(p, r) for _ in range(count)]


def _params_of(tree: Tuple[str, list]) -> list:
    """What the older tags wrote after a type, out of the newer type tree."""
    name, inner = tree
    if name in ("StructProperty",) and inner:
        return [inner[0][0]]
    if name in ("ArrayProperty", "SetProperty", "OptionalProperty") and inner:
        child = inner[0]
        return [child[0], child]
    if name == "MapProperty" and len(inner) >= 2:
        return [inner[0][0], inner[1][0], inner[0], inner[1]]
    if name in ("ByteProperty", "EnumProperty") and inner:
        return [inner[0][0]]
    return []


def begin(p: Package, r: _Reader) -> None:
    """The byte of serialization flags an object starts with from 1011."""
    if p.ue5 >= UE5_PROPERTY_TAG_EXTENSION:
        flags = r.data[r.at]
        r.at += 1
        if flags & 0x02:
            # Overridable serialization information: an operation byte.
            r.at += 1


def tags(p: Package, r: _Reader, end: int) -> List[Tag]:
    """The tagged properties from ``r`` to `None`, each value skipped over."""
    out: List[Tag] = []
    while r.at < end:
        name = _fname(p, r)
        if name == "None":
            break
        boolean = None
        binary = False
        if p.ue5 >= UE5_PROPERTY_TAG_COMPLETE_TYPE_NAME:
            tree = _type_tree(p, r)
            type_ = tree[0]
            params = _params_of(tree)
            size = r.i32()
            flags = r.data[r.at]
            r.at += 1
            index = r.i32() if flags & _HAS_ARRAY_INDEX else 0
            if flags & _HAS_GUID:
                r.skip(16)
            if flags & _HAS_EXTENSIONS:
                extension = r.data[r.at]
                r.at += 1
                if extension & 0x02:
                    r.skip(2)
            boolean = bool(flags & _BOOL_TRUE)
            binary = bool(flags & _BINARY_OR_NATIVE)
        else:
            type_ = _fname(p, r)
            size = r.i32()
            index = r.i32()
            params = []
            if type_ == "StructProperty":
                params = [_fname(p, r)]
                if p.ue4 >= UE4_STRUCT_GUID_IN_PROPERTY_TAG:
                    r.skip(16)
            elif type_ == "BoolProperty":
                boolean = bool(r.data[r.at])
                r.at += 1
            elif type_ in ("ByteProperty", "EnumProperty"):
                params = [_fname(p, r)]
            elif type_ == "ArrayProperty":
                if p.ue4 >= UE4_ARRAY_PROPERTY_INNER_TAGS:
                    params = [_fname(p, r)]
            elif type_ in ("SetProperty", "OptionalProperty"):
                if p.ue4 >= UE4_PROPERTY_TAG_SET_MAP_SUPPORT:
                    params = [_fname(p, r)]
            elif type_ == "MapProperty":
                if p.ue4 >= UE4_PROPERTY_TAG_SET_MAP_SUPPORT:
                    params = [_fname(p, r), _fname(p, r)]
            if p.ue4 >= UE4_PROPERTY_GUID_IN_PROPERTY_TAG:
                if r.data[r.at]:
                    r.skip(1 + 16)
                else:
                    r.skip(1)
            if p.ue5 >= UE5_PROPERTY_TAG_EXTENSION:
                extension = r.data[r.at]
                r.at += 1
                if extension & 0x02:
                    r.skip(2)
        at = r.at
        if size < 0 or at + size > len(r.data):
            raise UassetError("A property's value runs past the package.")
        out.append(Tag(name, type_, params, size, index, at, boolean, binary))
        r.at = at + size
    return out


def by_name(found: List[Tag]) -> Dict[str, List[Tag]]:
    out: Dict[str, List[Tag]] = {}
    for tag in found:
        out.setdefault(tag.name, []).append(tag)
    return out


def properties(p: Package, export: Export) -> Tuple[List[Tag], int]:
    """An export's tags, and where its hand-written part begins."""
    r = _Reader(p.data, export.offset)
    begin(p, r)
    found = tags(p, r, export.offset + export.size)
    return found, r.at


def array_elements(p: Package, tag: Tag) -> Tuple[int, int, Optional[str]]:
    """``(count, where the first element is, the struct's name)`` of an array.

    Before 1012 an array of structs has one inner tag before its elements,
    naming the struct; from 1012 the type tree already did.
    """
    r = _Reader(p.data, tag.at)
    count = r.i32()
    inner = tag.params[0] if tag.params else ""
    struct_name = None
    if inner == "StructProperty":
        if p.ue5 >= UE5_PROPERTY_TAG_COMPLETE_TYPE_NAME:
            tree = tag.params[1] if len(tag.params) > 1 else None
            struct_name = tree[1][0][0] if tree and tree[1] else None
        else:
            _fname(p, r)  # the array's own name again
            _fname(p, r)  # StructProperty
            r.i32()
            r.i32()
            struct_name = _fname(p, r)
            if p.ue4 >= UE4_STRUCT_GUID_IN_PROPERTY_TAG:
                r.skip(16)
            if p.ue4 >= UE4_PROPERTY_GUID_IN_PROPERTY_TAG:
                r.skip(17 if r.data[r.at] else 1)
            if p.ue5 >= UE5_PROPERTY_TAG_EXTENSION:
                extension = r.data[r.at]
                r.at += 1
                if extension & 0x02:
                    r.skip(2)
    return count, r.at, struct_name


# -- plain values ---------------------------------------------------------------------

def int_value(p: Package, tag: Tag) -> int:
    return struct.unpack_from("<i", p.data, tag.at)[0]


def float_value(p: Package, tag: Tag) -> float:
    if tag.type == "DoubleProperty":
        return struct.unpack_from("<d", p.data, tag.at)[0]
    return struct.unpack_from("<f", p.data, tag.at)[0]


def name_value(p: Package, tag: Tag) -> str:
    return _fname(p, _Reader(p.data, tag.at))


def object_value(p: Package, tag: Tag) -> int:
    return struct.unpack_from("<i", p.data, tag.at)[0]


def names_array(p: Package, tag: Tag) -> List[str]:
    r = _Reader(p.data, tag.at)
    return [_fname(p, r) for _ in range(r.i32())]


def frame_rate(p: Package, tag: Tag) -> float:
    """A `FrameRate` struct, tagged or binary, as frames a second."""
    if tag.size == 8:
        num, den = struct.unpack_from("<ii", p.data, tag.at)
        return num / den if den else 30.0
    r = _Reader(p.data, tag.at)
    inner = by_name(tags(p, r, tag.at + tag.size))
    num = int_value(p, inner["Numerator"][0]) if "Numerator" in inner else 30
    den = int_value(p, inner["Denominator"][0]) if "Denominator" in inner else 1
    return num / den if den else 30.0
