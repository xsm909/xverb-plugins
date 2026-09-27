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

"""USD's text layer — `#usda 1.0` — read into the shape the binary one is.

The result is ``{path: {"kind": ..., "fields": {...}}}`` with the field names
USD itself uses (`specifier`, `typeName`, `primChildren`, `default`,
`timeSamples`, `references`, `variantSelection`…), so that composing a stage
and building meshes never asks which kind of file a layer came from.

**Big arrays are the whole cost.** A mesh's points are a hundred thousand
tuples; walking them token by token is what makes a text reader slow. So an
array that is only numbers, commas and brackets is cut out of the text with one
expression and its numbers read with another — everything else goes through
an ordinary recursive descent.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from usdcrate import AssetPath, ListOp, Reference, TimeSamples


class UsdaError(Exception):
    pass


_TOKEN = re.compile(r'''
    (?P<skip>(?:\s+|\#[^\n]*)+)
  | (?P<str3>"""(?:[^"\\]|\\.|"(?!""))*"""|\'\'\'(?:[^'\\]|\\.|'(?!''))*\'\'\')
  | (?P<str>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<asset3>@@@.*?@@@)
  | (?P<asset>@[^@\n]*@)
  | (?P<path><[^>\n]*>)
  | (?P<num>[-+]?(?:\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)(?![\w]))
  | (?P<word>[-+]?[A-Za-z_][\w:.\-]*(?:\[\])?)
  | (?P<punct>[\[\](){}=,;:&])
''', re.X | re.S)

#: An array of numbers alone — no strings, no paths — cut out whole.
_NUMERIC_ARRAY = re.compile(r'\[[\s\d.eE+\-,()]*\]')
_NUMBER = re.compile(r'[-+]?(?:\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)')
_TUPLE = re.compile(r'\(([^()]*)\)')

_LIST_OPS = ("prepend", "append", "delete", "add", "reorder")
_VARIABILITY = ("uniform", "varying", "config")


def child_path(parent: str, name: str) -> str:
    if parent == "/":
        return "/" + name
    if parent.endswith("}"):
        return parent + name
    return parent + "/" + name


def _unquote(text: str) -> str:
    if text.startswith(('"""', "'''")):
        body = text[3:-3]
    else:
        body = text[1:-1]
    if "\\" not in body:
        return body
    return re.sub(r'\\(.)', lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)),
                  body)


class _Lexer:
    def __init__(self, text: str):
        self.text = text
        self.at = 0
        self._peeked: Optional[Tuple[str, str, int]] = None

    def _scan(self) -> Tuple[str, str, int]:
        text = self.text
        while True:
            if self.at >= len(text):
                return ("end", "", self.at)
            found = _TOKEN.match(text, self.at)
            if found is None:
                raise UsdaError("Unexpected text at character %d: %r"
                                % (self.at, text[self.at:self.at + 30]))
            self.at = found.end()
            kind = found.lastgroup
            if kind == "skip":
                continue
            return (kind, found.group(kind), found.start())

    def peek(self) -> Tuple[str, str, int]:
        if self._peeked is None:
            self._peeked = self._scan()
        return self._peeked

    def next(self) -> Tuple[str, str, int]:
        token = self.peek()
        self._peeked = None
        return token

    def expect(self, value: str) -> None:
        kind, text, where = self.next()
        if text != value:
            raise UsdaError("Expected %r at character %d, found %r" % (value, where, text))

    def accept(self, value: str) -> bool:
        if self.peek()[1] == value:
            self.next()
            return True
        return False

    def numeric_array(self) -> Optional[list]:
        """The array starting at the next token, if it is numbers alone."""
        kind, text, where = self.peek()
        if text != "[":
            return None
        found = _NUMERIC_ARRAY.match(self.text, where)
        if found is None:
            return None
        body = found.group(0)[1:-1]
        if "((" in body.replace(" ", "").replace("\n", ""):
            return None  # matrices: tuples of tuples, read the slow way
        self._peeked = None
        self.at = found.end()
        if "(" in body:
            return [tuple(float(n) for n in _NUMBER.findall(t)) for t in _TUPLE.findall(body)]
        numbers = _NUMBER.findall(body)
        return [float(n) if ("." in n or "e" in n or "E" in n) else int(n) for n in numbers]


class _Parser:
    def __init__(self, text: str):
        self.lex = _Lexer(text)
        self.specs: Dict[str, dict] = {}

    # -- values --

    def value(self):
        numbers = self.lex.numeric_array()
        if numbers is not None:
            return numbers
        kind, text, where = self.lex.next()
        if kind == "num":
            return float(text) if ("." in text or "e" in text or "E" in text) else int(text)
        if kind in ("str", "str3"):
            return _unquote(text)
        if kind == "asset":
            return AssetPath(text[1:-1])
        if kind == "asset3":
            return AssetPath(text[3:-3])
        if kind == "path":
            return ("path", text[1:-1])
        if kind == "word":
            if text in ("true", "True"):
                return True
            if text in ("false", "False"):
                return False
            if text == "None":
                return None
            if text in ("inf", "+inf"):
                return float("inf")
            if text == "-inf":
                return float("-inf")
            if text == "nan":
                return float("nan")
            return text
        if text == "(":
            items = []
            while not self.lex.accept(")"):
                items.append(self.value())
                self._layer_offset_or_path(items)
                self.lex.accept(",")
            return tuple(items)
        if text == "[":
            items = []
            while not self.lex.accept("]"):
                items.append(self.value())
                self._layer_offset_or_path(items)
                self.lex.accept(",")
            return items
        if text == "{":
            return self._dictionary()
        raise UsdaError("Unexpected %r at character %d" % (text, where))

    def _layer_offset_or_path(self, items: list) -> None:
        """What may follow an asset in a list: its prim, and its layer offset."""
        if not items or not isinstance(items[-1], AssetPath):
            return
        if self.lex.peek()[0] == "path":
            items[-1] = Reference(str(items[-1]), self.lex.next()[1][1:-1])
        if self.lex.peek()[1] == "(":
            # `(offset = 2; scale = 1)` — a layer offset, which a still
            # picture has no use for.
            depth = 0
            while True:
                text = self.lex.next()[1]
                depth += text == "("
                depth -= text == ")"
                if depth == 0 or text == "":
                    break

    def _dictionary(self) -> dict:
        out = {}
        while not self.lex.accept("}"):
            kind, text, _ = self.lex.next()
            if kind in ("num", "str"):
                # A time-sample block: `time: value,`.
                key = float(text) if kind == "num" else _unquote(text)
                self.lex.expect(":")
                out[key] = self.value()
                self.lex.accept(",")
                continue
            # `type key = value`, the key perhaps quoted.
            key_kind, key, _ = self.lex.next()
            if key_kind in ("str", "str3"):
                key = _unquote(key)
            self.lex.expect("=")
            out[key] = self.value()
            self.lex.accept(";")
            self.lex.accept(",")
        return out

    # -- metadata --

    def metadata(self, into: dict) -> None:
        """`( key = value … )`, into a spec's fields."""
        self.lex.expect("(")
        while not self.lex.accept(")"):
            kind, text, where = self.lex.next()
            if kind in ("str", "str3"):
                into["documentation"] = _unquote(text)
                continue
            op = None
            if text in _LIST_OPS:
                op = text
                kind, text, where = self.lex.next()
            key = text
            self.lex.expect("=")
            value = self.value()
            if isinstance(value, AssetPath):
                held = [value]
                self._layer_offset_or_path(held)
                value = held[0]
            self._field(into, key, value, op)
            self.lex.accept(";")

    def _field(self, into: dict, key: str, value, op: Optional[str]) -> None:
        if key == "variants":
            into["variantSelection"] = dict(value or {})
            return
        if key == "variantSets":
            names = value if isinstance(value, list) else [value]
            self._list_op(into, "variantSetNames", names, op)
            return
        if key in ("references", "payload", "inherits", "specializes", "apiSchemas"):
            items = value if isinstance(value, list) else ([] if value is None else [value])
            converted = []
            for item in items:
                if isinstance(item, tuple) and item and item[0] == "path":
                    converted.append(Reference("", item[1]) if key in ("references", "payload")
                                     else item[1])
                elif isinstance(item, AssetPath):
                    converted.append(Reference(str(item), ""))
                elif isinstance(item, Reference) and key in ("inherits", "specializes"):
                    converted.append(item.prim)
                else:
                    converted.append(item)
            self._list_op(into, key, converted, op)
            return
        into[key] = _plain(value)

    @staticmethod
    def _list_op(into: dict, key: str, items: list, op: Optional[str]) -> None:
        found = into.get(key)
        if not isinstance(found, ListOp):
            found = ListOp()
            into[key] = found
        if op is None:
            found.explicit = True
            found.items = list(items)
        elif op == "prepend":
            found.prepended.extend(items)
        elif op in ("append", "add"):
            found.appended.extend(items)
        elif op == "delete":
            found.deleted.extend(items)

    # -- the layer --

    def layer(self) -> Dict[str, dict]:
        text = self.lex.text
        if not text.startswith("#usda"):
            raise UsdaError("This is not a USD text file.")
        self.lex.at = text.index("\n") if "\n" in text else len(text)
        root = {"kind": "PseudoRoot", "fields": {"primChildren": []}}
        self.specs["/"] = root
        if self.lex.peek()[1] == "(":
            self.metadata(root["fields"])
        while self.lex.peek()[0] != "end":
            self.prim("/", root)
        return self.specs

    def prim(self, parent: str, owner: dict) -> None:
        kind, specifier, where = self.lex.next()
        if specifier not in ("def", "over", "class"):
            raise UsdaError("Expected a prim at character %d, found %r" % (where, specifier))
        type_name = ""
        kind, text, _ = self.lex.next()
        if kind == "word":
            type_name = text
            kind, text, _ = self.lex.next()
        name = _unquote(text)
        path = child_path(parent, name)
        fields = {"specifier": specifier, "primChildren": [], "properties": []}
        if type_name:
            fields["typeName"] = type_name
        spec = {"kind": "Prim", "fields": fields}
        self.specs[path] = spec
        owner["fields"].setdefault("primChildren", []).append(name)
        if self.lex.peek()[1] == "(":
            self.metadata(fields)
        self.lex.expect("{")
        self.body(path, spec)

    def body(self, path: str, spec: dict) -> None:
        while not self.lex.accept("}"):
            kind, text, where = self.lex.peek()
            if text in ("def", "over", "class"):
                self.prim(path, spec)
            elif text == "variantSet":
                self.variant_set(path, spec)
            elif text == "reorder":
                self.lex.next()
                self.lex.next()  # nameChildren / properties
                self.lex.expect("=")
                self.value()
            elif kind == "end":
                raise UsdaError("The file ends inside %s." % path)
            else:
                self.property(path, spec)
            self.lex.accept(";")

    def variant_set(self, path: str, spec: dict) -> None:
        self.lex.expect("variantSet")
        name = _unquote(self.lex.next()[1])
        self.lex.expect("=")
        self.lex.expect("{")
        set_path = "%s{%s=}" % (path, name)
        variants = []
        self.specs[set_path] = {"kind": "VariantSet", "fields": {"variantChildren": variants}}
        names = spec["fields"].get("variantSetNames")
        if not isinstance(names, ListOp) or name not in names.applied():
            self._list_op(spec["fields"], "variantSetNames", [name], "append")
        while not self.lex.accept("}"):
            variant = _unquote(self.lex.next()[1])
            variants.append(variant)
            variant_path = "%s{%s=%s}" % (path, name, variant)
            fields = {"primChildren": [], "properties": []}
            variant_spec = {"kind": "Variant", "fields": fields}
            self.specs[variant_path] = variant_spec
            if self.lex.peek()[1] == "(":
                self.metadata(fields)
            self.lex.expect("{")
            self.body(variant_path, variant_spec)

    def property(self, path: str, spec: dict) -> None:
        op = None
        custom = False
        variability = None
        kind, text, where = self.lex.next()
        if text in _LIST_OPS:
            op = text
            kind, text, where = self.lex.next()
        if text == "custom":
            custom = True
            kind, text, where = self.lex.next()
        if text in _VARIABILITY:
            variability = text
            kind, text, where = self.lex.next()
        if text == "rel":
            self._relationship(path, spec, op)
            return
        type_name = text
        kind, name, where = self.lex.next()
        if kind != "word":
            raise UsdaError("Expected a property name at character %d, found %r" % (where, name))
        suffix = None
        for ending in (".connect", ".timeSamples", ".spline", ".default"):
            if name.endswith(ending):
                name, suffix = name[:-len(ending)], ending[1:]
        prop_path = path + "." + name
        found = self.specs.get(prop_path)
        if found is None:
            found = {"kind": "Attribute", "fields": {"typeName": type_name, "custom": custom}}
            if variability == "uniform":
                found["fields"]["variability"] = 1
            self.specs[prop_path] = found
            spec["fields"].setdefault("properties", []).append(name)
        fields = found["fields"]
        if self.lex.accept("="):
            value = self.value()
            if suffix == "connect":
                targets = value if isinstance(value, list) else [value]
                self._list_op(fields, "connectionPaths",
                              [_resolve(path, t[1]) for t in targets
                               if isinstance(t, tuple) and t and t[0] == "path"], op)
            elif suffix == "timeSamples":
                samples = TimeSamples()
                for t, v in (value or {}).items():
                    samples[float(t)] = _typed(type_name, v)
                fields["timeSamples"] = samples
            elif suffix == "spline":
                pass
            else:
                fields["default"] = _typed(type_name, value)
        if self.lex.peek()[1] == "(":
            self.metadata(fields)

    def _relationship(self, path: str, spec: dict, op: Optional[str]) -> None:
        kind, name, where = self.lex.next()
        if name.endswith(".default"):
            name = name[:-8]
        prop_path = path + "." + name
        found = self.specs.get(prop_path)
        if found is None:
            found = {"kind": "Relationship", "fields": {}}
            self.specs[prop_path] = found
            spec["fields"].setdefault("properties", []).append(name)
        if self.lex.accept("="):
            value = self.value()
            targets = value if isinstance(value, list) else ([] if value is None else [value])
            self._list_op(found["fields"], "targetPaths",
                          [_resolve(path, t[1]) for t in targets
                           if isinstance(t, tuple) and t and t[0] == "path"], op)
        if self.lex.peek()[1] == "(":
            self.metadata(found["fields"])


def _plain(value):
    """A path value as its text; everything else as it is."""
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "path":
        return value[1]
    return value


def _typed(type_name: str, value):
    if type_name.startswith("bool") and isinstance(value, int) and not isinstance(value, bool):
        return bool(value)
    if type_name.startswith("asset") and isinstance(value, list):
        return [AssetPath(v) for v in value]
    return _plain(value)


def _resolve(anchor: str, target: str) -> str:
    """A path as written, made absolute against the prim it was written in."""
    if target.startswith("/"):
        return target
    base = anchor.split(".", 1)[0]
    parts = [p for p in base.split("/") if p]
    rest = target
    while rest.startswith("../") or rest == "..":
        if parts:
            parts.pop()
        rest = rest[3:]
    if rest.startswith("./"):
        rest = rest[2:]
    head = "/" + "/".join(parts) if parts else ""
    if rest.startswith("."):
        return (head or "/") + rest
    return head + "/" + rest if rest else (head or "/")


def parse(data: bytes) -> Dict[str, dict]:
    text = data.decode("utf-8", "replace")
    return _Parser(text).layer()
