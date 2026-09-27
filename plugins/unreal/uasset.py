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

"""An Unreal package — `.uasset`, `.umap` — read for what it says about itself.

**Only the header is read, never an object.** A package opens with a
summary (`FPackageFileSummary`): versions, then where each table is. From
those tables come the four things worth showing, and none of them needs the
engine:

- the **thumbnail** the editor saved for the Content Browser — a PNG or a
  JPEG, stored as it is;
- the **asset registry tags** — what the Content Browser shows on hover: a
  texture's size and format, a mesh's triangles, a blueprint's parent class;
- the **imports** — the other packages this one uses;
- the **engine** that saved it.

**The summary is a chain of version tests.** Every field after the first few
exists only from some version on, so each is read under the same condition
the engine writes it under; the numbers come from `ObjectVersion.h`. A
cooked package carries no versions at all ("unversioned") and is read as the
newest; if that does not add up, the tables that did are still shown.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

TAG = 0x9E2A83C1
TAG_SWAPPED = 0xC1832A9E

PKG_FILTER_EDITOR_ONLY = 0x80000000
PKG_COOKED = 0x00000200  # PKG_Cooked in older engines; still a fair hint

# EUnrealEngineObjectUE4Version
UE4_WORLD_LEVEL_INFO = 224
UE4_ADDED_CHUNKID = 278
UE4_CHUNKID_ARRAY = 326
UE4_ENGINE_VERSION_OBJECT = 336
UE4_STRING_ASSET_REFERENCES_MAP = 384
UE4_COMPATIBLE_ENGINE_VERSION = 444
UE4_SERIALIZE_TEXT_IN_PACKAGES = 459
UE4_NAME_HASHES_SERIALIZED = 504
UE4_PRELOAD_DEPENDENCIES = 507
UE4_SEARCHABLE_NAMES = 510
UE4_LOCALIZATION_ID = 516
UE4_PACKAGE_OWNER = 518
UE4_NON_OUTER_PACKAGE_IMPORT = 520
UE4_ASSETREGISTRY_DEPENDENCYFLAGS = 521
UE4_NEWEST = 522

# EUnrealEngineObjectUE5Version
UE5_NAMES_FROM_EXPORT_DATA = 1001
UE5_PAYLOAD_TOC = 1002
UE5_OPTIONAL_RESOURCES = 1003
UE5_ADD_SOFTOBJECTPATH_LIST = 1008
UE5_DATA_RESOURCES = 1009
UE5_METADATA_SERIALIZATION_OFFSET = 1014
UE5_VERSE_CELLS = 1015
UE5_PACKAGE_SAVED_HASH = 1016
UE5_IMPORT_TYPE_HIERARCHIES = 1018
UE5_NEWEST = 1018


class UassetError(Exception):
    pass


def is_package(head: bytes) -> bool:
    return len(head) >= 4 and struct.unpack_from("<I", head, 0)[0] == TAG


class _Reader:
    def __init__(self, data: bytes, at: int = 0):
        self.data = data
        self.at = at

    def need(self, n: int) -> None:
        if self.at + n > len(self.data) or self.at < 0:
            raise UassetError("The package ends where its header says there is more.")

    def i32(self) -> int:
        self.need(4)
        (v,) = struct.unpack_from("<i", self.data, self.at)
        self.at += 4
        return v

    def u32(self) -> int:
        self.need(4)
        (v,) = struct.unpack_from("<I", self.data, self.at)
        self.at += 4
        return v

    def i64(self) -> int:
        self.need(8)
        (v,) = struct.unpack_from("<q", self.data, self.at)
        self.at += 8
        return v

    def u16(self) -> int:
        self.need(2)
        (v,) = struct.unpack_from("<H", self.data, self.at)
        self.at += 2
        return v

    def skip(self, n: int) -> None:
        self.need(n)
        self.at += n

    def string(self) -> str:
        """`FString`: a length with its terminator, negative for UTF-16.

        No cap but the file: a Control Rig's search data is one tag several
        megabytes long, and a length past the end is caught by `need`."""
        n = self.i32()
        if n == 0:
            return ""
        if n > 0:
            self.need(n)
            raw = self.data[self.at:self.at + n]
            self.at += n
            return raw[:-1].decode("latin-1")
        n = -n
        self.need(n * 2)
        raw = self.data[self.at:self.at + n * 2]
        self.at += n * 2
        return raw[:-2].decode("utf-16-le", "replace")


class Package:
    """What a package's header says. Fields not reached stay at their defaults."""

    def __init__(self, data: bytes):
        self.data = data
        self.size = len(data)
        self.legacy = 0
        self.ue4 = 0
        self.ue5 = 0
        self.licensee = 0
        self.unversioned = False
        self.flags = 0
        self.package_name = ""
        self.names: List[str] = []
        self.name_count = self.name_offset = 0
        self.export_count = self.export_offset = 0
        self.import_count = self.import_offset = 0
        self.thumbnail_offset = 0
        self.soft_count = self.soft_offset = 0
        self.bulk_start = 0
        self.registry_offset = 0
        self.saved_by = ""
        self.compatible = ""
        self.custom_versions = 0
        self.problems: List[str] = []
        #: Each table, once read.
        self._read: Dict[str, list] = {}
        self._summary()
        self._names()

    # -- the summary --

    @property
    def editor_only_filtered(self) -> bool:
        return bool(self.flags & PKG_FILTER_EDITOR_ONLY)

    def _summary(self) -> None:
        r = _Reader(self.data)
        tag = r.u32()
        if tag == TAG_SWAPPED:
            raise UassetError("This package was saved big-endian, for a console.")
        if tag != TAG:
            raise UassetError("This is not an Unreal package.")
        self.legacy = r.i32()
        if self.legacy >= 0:
            raise UassetError("This package is from Unreal Engine 3, which is not read.")
        if self.legacy < -9:
            raise UassetError("This package is from an engine newer than this reader.")
        if self.legacy != -4:
            r.i32()  # the UE3 version
        self.ue4 = r.i32()
        if self.legacy <= -8:
            self.ue5 = r.i32()
        self.licensee = r.i32()
        if self.legacy <= -9:
            # From -9 the saved hash and the header's size come before the
            # custom versions, and are not repeated further down.
            r.skip(20)
            r.i32()
        if self.legacy <= -2:
            count = r.i32()
            if not 0 <= count < 10000:
                raise UassetError("The package's list of custom versions is damaged.")
            self.custom_versions = count
            for _ in range(count):
                if self.legacy == -2:
                    r.skip(8)
                elif self.legacy >= -5:
                    r.skip(20)
                    r.string()
                else:
                    r.skip(20)
        if self.ue4 == 0 and self.ue5 == 0 and self.licensee == 0:
            # Cooked for a game: versions left out. Read as the newest.
            self.unversioned = True
            self.ue4 = UE4_NEWEST
            self.ue5 = UE5_NEWEST if self.legacy <= -8 else 0
        ue4, ue5 = self.ue4, self.ue5

        if self.legacy > -9:
            r.i32()  # TotalHeaderSize
        self.package_name = r.string()
        self.flags = r.u32()
        self.name_count = r.i32()
        self.name_offset = r.i32()
        if ue5 >= UE5_ADD_SOFTOBJECTPATH_LIST:
            self.soft_count = r.i32()
            self.soft_offset = r.i32()
        if not self.editor_only_filtered and ue4 >= UE4_LOCALIZATION_ID:
            r.string()
        if ue4 >= UE4_SERIALIZE_TEXT_IN_PACKAGES:
            r.i32()
            r.i32()
        self.export_count = r.i32()
        self.export_offset = r.i32()
        self.import_count = r.i32()
        self.import_offset = r.i32()
        # Declared before the exports in the header, written after the
        # imports in the file — measured on the engine's own packages.
        if ue5 >= UE5_METADATA_SERIALIZATION_OFFSET:
            r.i32()
        if ue5 >= UE5_VERSE_CELLS:
            r.skip(16)
        r.i32()  # DependsOffset
        if ue4 >= UE4_STRING_ASSET_REFERENCES_MAP:
            r.i32()
            r.i32()
        if ue4 >= UE4_SEARCHABLE_NAMES:
            r.i32()
        self.thumbnail_offset = r.i32()
        if ue5 >= UE5_IMPORT_TYPE_HIERARCHIES:
            r.skip(8)
        if self.legacy <= -9:
            pass
        elif ue5 >= UE5_PACKAGE_SAVED_HASH:
            r.skip(20)
        else:
            r.skip(16)
        if not self.editor_only_filtered:
            if ue4 >= UE4_PACKAGE_OWNER:
                r.skip(16)
            if UE4_PACKAGE_OWNER <= ue4 < UE4_NON_OUTER_PACKAGE_IMPORT:
                r.skip(16)
        generations = r.i32()
        if not 0 <= generations < 100000:
            raise UassetError("The package's generations are damaged.")
        r.skip(generations * 8)
        if ue4 >= UE4_ENGINE_VERSION_OBJECT:
            self.saved_by = self._engine_version(r)
        else:
            self.saved_by = "changelist %d" % r.i32()
        if ue4 >= UE4_COMPATIBLE_ENGINE_VERSION:
            self.compatible = self._engine_version(r)
        r.u32()  # CompressionFlags
        chunks = r.i32()
        if chunks:
            raise UassetError("This package is compressed as a whole, the way only "
                              "very old engines saved them.")
        r.u32()  # PackageSource
        extra = r.i32()
        if not 0 <= extra < 10000:
            raise UassetError("The package's summary is damaged.")
        for _ in range(extra):
            r.string()
        if self.legacy > -7:
            r.i32()  # NumTextureAllocations
        self.registry_offset = r.i32()
        self.bulk_start = r.i64()

    def reach(self) -> int:
        """How far into the file the header's tables start, at the furthest."""
        return max(self.name_offset, self.import_offset, self.export_offset,
                   self.thumbnail_offset, self.registry_offset, 0)

    @staticmethod
    def _engine_version(r: _Reader) -> str:
        major, minor, patch = r.u16(), r.u16(), r.u16()
        changelist = r.u32() & 0x7FFFFFFF
        branch = r.string()
        text = "%d.%d.%d" % (major, minor, patch)
        if changelist:
            text += " (%d)" % changelist
        if branch:
            text += " " + branch
        return text if (major or minor or changelist) else ""

    def _names(self) -> None:
        if not (0 < self.name_count < 2_000_000 and 0 < self.name_offset < self.size):
            return
        r = _Reader(self.data, self.name_offset)
        hashes = self.ue4 >= UE4_NAME_HASHES_SERIALIZED
        try:
            for _ in range(self.name_count):
                self.names.append(r.string())
                if hashes:
                    r.skip(4)
        except UassetError:
            self.problems.append("names")

    # -- the tables --

    def name(self, index: int, number: int = 0) -> str:
        if not 0 <= index < len(self.names):
            return "?"
        text = self.names[index]
        return "%s_%d" % (text, number - 1) if number > 0 else text

    def imports(self) -> List[Tuple[str, str, int]]:
        """``(class, name, outer)`` for every import, in the file's order."""
        if "imports" in self._read:
            return self._read["imports"]
        out = self._read["imports"] = []
        if not (0 < self.import_count < 1_000_000 and 0 < self.import_offset < self.size):
            return out
        r = _Reader(self.data, self.import_offset)
        package_name = self.ue4 >= UE4_NON_OUTER_PACKAGE_IMPORT and not self.editor_only_filtered
        optional = self.ue5 >= UE5_OPTIONAL_RESOURCES
        try:
            for _ in range(self.import_count):
                r.skip(8)  # class package
                class_name = self.name(r.i32(), r.i32())
                outer = r.i32()
                object_name = self.name(r.i32(), r.i32())
                if package_name:
                    r.skip(8)
                if optional:
                    r.skip(4)
                out.append((class_name, object_name, outer))
        except UassetError:
            self.problems.append("imports")
        return out

    def soft_path(self, index: int) -> str:
        """Entry ``index`` of the soft object paths a UE 5.2+ package lists
        once and points into: the package, then the asset in it."""
        if not (0 <= index < self.soft_count and 0 < self.soft_offset < self.size):
            return ""
        r = _Reader(self.data, self.soft_offset)
        for i in range(index + 1):
            package = self.name(r.i32(), r.i32())
            r.skip(8)  # the asset's name
            r.string()  # a path below it
        return package

    def packages_used(self) -> List[str]:
        """The other packages this one imports from, engine scripts left out."""
        found = []
        for class_name, name, outer in self.imports():
            if class_name == "Package" and outer == 0 and not name.startswith("/Script/"):
                if name not in found:
                    found.append(name)
        return found

    def classes_used(self) -> List[str]:
        return sorted({name for class_name, name, _ in self.imports() if class_name == "Class"})

    def thumbnails(self) -> List[dict]:
        """Each thumbnail: its class and object, size, and the picture's bytes."""
        if "thumbnails" in self._read:
            return self._read["thumbnails"]
        out: List[dict] = self._read.setdefault("thumbnails", [])
        if not 0 < self.thumbnail_offset < self.size:
            return out
        r = _Reader(self.data, self.thumbnail_offset)
        try:
            count = r.i32()
            if not 0 <= count < 10000:
                return out
            entries = [(r.string(), r.string(), r.i32()) for _ in range(count)]
            for class_name, object_path, offset in entries:
                if not 0 < offset < self.size:
                    continue
                t = _Reader(self.data, offset)
                width = t.i32()
                height = t.i32()
                jpeg = height < 0
                height = abs(height)
                size = t.i32()
                if size <= 0 or width <= 0 or height <= 0:
                    continue
                t.need(size)
                picture = self.data[t.at:t.at + size]
                if not (picture.startswith(b"\x89PNG") or picture.startswith(b"\xff\xd8")):
                    continue
                out.append({"class": class_name, "object": object_path,
                            "width": width, "height": height, "jpeg": jpeg,
                            "bytes": picture})
        except UassetError:
            self.problems.append("thumbnails")
        return out

    def registry(self) -> List[dict]:
        """The asset registry's entries: object, class, and its tags."""
        if "registry" in self._read:
            return self._read["registry"]
        out: List[dict] = self._read.setdefault("registry", [])
        if not 0 < self.registry_offset < self.size:
            return out
        r = _Reader(self.data, self.registry_offset)
        try:
            if self.ue4 >= UE4_ASSETREGISTRY_DEPENDENCYFLAGS and not self.editor_only_filtered:
                r.i64()  # where the dependency data is
            count = r.i32()
            if not 0 <= count < 10000:
                self.problems.append("registry")
                return out
            for _ in range(count):
                path = r.string()
                class_name = r.string()
                tags = r.i32()
                if not 0 <= tags < 100000:
                    raise UassetError("damaged tags")
                pairs: Dict[str, str] = {}
                for _ in range(tags):
                    key = r.string()
                    pairs[key] = r.string()
                out.append({"object": path, "class": class_name, "tags": pairs})
        except UassetError:
            self.problems.append("registry")
        return out

    def main_class(self) -> str:
        """The class of the package's asset, from wherever it is said first."""
        for entry in self.registry():
            if entry["class"]:
                return entry["class"].rsplit(".", 1)[-1]
        for thumb in self.thumbnails():
            if thumb["class"]:
                return thumb["class"].rsplit(".", 1)[-1]
        return ""

    def engine(self) -> str:
        if self.saved_by:
            return self.saved_by
        if self.ue5:
            return "Unreal Engine 5"
        return "Unreal Engine 4"
