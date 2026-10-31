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

"""Kiwi: a binary format that carries its own schema.

Kiwi is Evan Wallace's schema format, and a Figma file is one Kiwi message
with the schema it was written against stored in front of it. That is the
whole reason a file saved by a version of Figma nobody has seen can still be
read: the field names and their numbers come out of the file rather than out of
this code, so a field added next year arrives named and in the right place.

Three kinds of definition, and the difference is where the field names go:

* a **struct** writes its fields in order, with nothing to say which is which;
* a **message** writes a number before each field and a zero to finish, so a
  field it does not care about is simply absent;
* an **enum** writes one number, and the name it stands for is in the schema.

Numbers are small by habit rather than by width: an integer is written seven
bits at a time, and a float is written as one byte when it is zero. Strings are
UTF-8 and end at a zero byte.
"""

from __future__ import annotations

import struct

BUILTIN = ("bool", "byte", "int", "uint", "float", "string", "int64", "uint64")


class KiwiError(Exception):
    """The schema or the message does not hold together."""


class Reader:
    """A byte reader with the format's own number encodings on it."""

    __slots__ = ("b", "i")

    def __init__(self, data: bytes) -> None:
        self.b = data
        self.i = 0

    def byte(self) -> int:
        try:
            value = self.b[self.i]
        except IndexError:
            raise KiwiError("the message ends in the middle of a value") from None
        self.i += 1
        return value

    def varuint(self) -> int:
        shift = 0
        value = 0
        while True:
            c = self.byte()
            value |= (c & 127) << shift
            shift += 7
            if not (c & 128) or shift >= 35:
                break
        return value & 0xFFFFFFFF

    def varint(self) -> int:
        v = self.varuint()
        return (v >> 1) ^ -(v & 1)

    def varuint64(self) -> int:
        shift = 0
        value = 0
        while True:
            c = self.byte()
            value |= (c & 127) << shift
            shift += 7
            if not (c & 128) or shift >= 70:
                break
        return value & 0xFFFFFFFFFFFFFFFF

    def varint64(self) -> int:
        v = self.varuint64()
        return (v >> 1) ^ -(v & 1)

    def varfloat(self) -> float:
        first = self.byte()
        if first == 0:
            return 0.0
        bits = first | (self.byte() << 8) | (self.byte() << 16) | (self.byte() << 24)
        # The exponent is stored at the bottom so that zero is one byte.
        bits = ((bits << 23) | (bits >> 9)) & 0xFFFFFFFF
        return struct.unpack("<f", struct.pack("<I", bits))[0]

    def string(self) -> str:
        start = self.i
        b = self.b
        try:
            while b[self.i] != 0:
                self.i += 1
        except IndexError:
            raise KiwiError("a string with no end") from None
        out = b[start:self.i].decode("utf-8", "replace")
        self.i += 1
        return out


def parse_schema(data: bytes) -> list[dict]:
    """The definitions a file carries, in the order they were written."""
    r = Reader(data)
    definitions = []
    for _ in range(r.varuint()):
        name = r.string()
        kind = r.byte()
        if kind > 2:
            raise KiwiError("a definition of an unknown kind: %d" % kind)
        fields = []
        for _ in range(r.varuint()):
            field = r.string()
            kind_of_field = r.varint()
            is_array = bool(r.byte() & 1)
            number = r.varuint()
            fields.append((field, kind_of_field, is_array, number))
        definitions.append({
            "name": name,
            "kind": ("ENUM", "STRUCT", "MESSAGE")[kind],
            "fields": fields,
        })
    return definitions


class Schema:
    """A parsed schema, able to decode a message written against it."""

    def __init__(self, definitions: list[dict]) -> None:
        self.definitions = definitions
        self.index = {d["name"]: i for i, d in enumerate(definitions)}
        # Field lookup by number, built once: a Figma file holds tens of
        # thousands of fields and searching a definition's list for each one
        # is the whole cost of reading it.
        self._by_number = []
        for d in definitions:
            self._by_number.append({n: (f, t, a) for f, t, a, n in d["fields"]})

    def decode(self, data: bytes, root: str = "Message") -> dict:
        if root not in self.index:
            raise KiwiError("the schema has no %s in it" % root)
        return self._value(Reader(data), self.index[root])

    def _one(self, r: Reader, kind: int):
        if kind < 0:
            name = BUILTIN[~kind]
            if name == "bool":
                return r.byte() != 0
            if name == "byte":
                return r.byte()
            if name == "int":
                return r.varint()
            if name == "uint":
                return r.varuint()
            if name == "float":
                return r.varfloat()
            if name == "string":
                return r.string()
            if name == "int64":
                return r.varint64()
            return r.varuint64()
        return self._value(r, kind)

    def _array(self, r: Reader, kind: int):
        count = r.varuint()
        if kind == ~BUILTIN.index("byte"):
            out = r.b[r.i:r.i + count]
            if len(out) != count:
                raise KiwiError("an array of bytes running past the end")
            r.i += count
            return out
        return [self._one(r, kind) for _ in range(count)]

    def _value(self, r: Reader, index: int):
        definition = self.definitions[index]
        kind = definition["kind"]

        if kind == "ENUM":
            number = r.varuint()
            for field, _t, _a, value in definition["fields"]:
                if value == number:
                    return field
            # An enum member this schema does not name: the number itself is
            # still worth keeping, and nothing here switches on unknown ones.
            return number

        if kind == "STRUCT":
            out = {}
            for field, t, is_array, _n in definition["fields"]:
                out[field] = self._array(r, t) if is_array else self._one(r, t)
            return out

        out = {}
        numbers = self._by_number[index]
        while True:
            number = r.varuint()
            if number == 0:
                return out
            found = numbers.get(number)
            if found is None:
                raise KiwiError("field %d, which %s does not have"
                                % (number, definition["name"]))
            field, t, is_array = found
            out[field] = self._array(r, t) if is_array else self._one(r, t)
