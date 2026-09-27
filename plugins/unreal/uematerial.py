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

"""Which picture a mesh's material paints it with.

A material is a graph that only the engine can evaluate, so this does not
try: it looks for the one texture a preview needs, the base colour, by name.

- A mesh's material slots are its imports of material classes; each is
  matched to a slot of the mesh description by name (`MI_Quinn_01` wears
  `Quinn_01`), else by order.
- A material instance lists its textures as parameters; the one whose
  parameter or texture is named like a base colour — *base*, *diffuse*,
  *albedo*, *color*, `_D`, `_BC` — and not like a normal, mask, roughness or
  packed map, is taken. An instance without one asks its parent.
- A material names its textures only as imports, and is asked the same way
  by the textures' own names.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

import ueobject
from uasset import Package, _Reader

MATERIAL_CLASSES = ("Material", "MaterialInstanceConstant", "MaterialInstanceDynamic",
                    "MaterialInstance")

_NOT_COLOUR = re.compile(r"(normal|_n$|_n_|nrm|mra|orm|arm|rough|metal|spec|mask|height|"
                         r"displace|_ao|occlusion|opacity|emissive|_e$|_m$|_r$|noise|detail|"
                         r"cavity|bump|_h$|flow|lut|ramp)", re.I)
_COLOUR = re.compile(r"(base\s*colou?r|basecolou?r|base\s*texture|diffuse|albedo|_d$|_bc$|"
                     r"_basecolor|_col$|_color$|colou?r)", re.I)


def import_path(p: Package, index: int) -> Tuple[str, str]:
    """``(class, package path)`` of an import, following its outers to the package."""
    imports = p.imports()
    if index >= 0 or -index - 1 >= len(imports):
        return "", ""
    klass, name, outer = imports[-index - 1]
    seen = 0
    while outer < 0 and -outer - 1 < len(imports) and seen < 16:
        k, n, outer = imports[-outer - 1]
        seen += 1
        if k == "Package":
            return klass, n
    return klass, ""


def slot_materials(p: Package, slots: List[str]) -> List[str]:
    """For each slot name, the package path of its material, or ""."""
    found = []
    imports = p.imports()
    for i, (klass, name, _) in enumerate(imports):
        if klass in MATERIAL_CLASSES:
            found.append((name, import_path(p, -i - 1)[1]))
    out = []
    for at, slot in enumerate(slots):
        key = _plain(slot)
        match = [path for name, path in found if key and (key in _plain(name) or _plain(name) in key)]
        if len(match) == 1:
            out.append(match[0])
        elif at < len(found) and len(found) == len(slots):
            out.append(found[at][1])
        elif len(found) == 1:
            out.append(found[0][1])
        else:
            out.append(match[0] if match else "")
    return out


def _plain(name: str) -> str:
    return re.sub(r"^(mi|m|mat|mtl)_", "", name.lower()).replace("_", "")


def _score(parameter: str, texture: str) -> int:
    text = parameter + " " + texture
    if _NOT_COLOUR.search(texture) or _NOT_COLOUR.search(parameter):
        return 0
    if _COLOUR.search(parameter):
        return 3
    if _COLOUR.search(texture):
        return 2
    return 1 if text.strip() else 0


def _texture_parameters(p: Package) -> Tuple[List[Tuple[str, str]], str]:
    """``[(parameter, texture package)]`` and the parent's package, of an instance."""
    textures: List[Tuple[str, str]] = []
    parent = ""
    for export in ueobject.exports(p):
        if export.klass not in MATERIAL_CLASSES:
            continue
        tags, _ = ueobject.properties(p, export)
        for tag in tags:
            if tag.name == "Parent":
                parent = import_path(p, ueobject.object_value(p, tag))[1]
            elif tag.name == "TextureParameterValues":
                count, at, _ = ueobject.array_elements(p, tag)
                r = _Reader(p.data, at)
                for _ in range(count):
                    element = {t.name: t for t in ueobject.tags(p, r, tag.at + tag.size)}
                    name = ""
                    info = element.get("ParameterInfo")
                    if info is not None:
                        inner = {t.name: t for t in ueobject.tags(
                            p, _Reader(p.data, info.at), info.at + info.size)}
                        if "Name" in inner:
                            name = ueobject.name_value(p, inner["Name"])
                    if "ParameterValue" in element:
                        path = import_path(p, ueobject.object_value(p, element["ParameterValue"]))[1]
                        if path:
                            textures.append((name, path))
        break
    if not textures:
        # A material, or an instance that sets none: the textures it imports.
        for i, (klass, name, _) in enumerate(p.imports()):
            if klass.startswith("Texture"):
                path = import_path(p, -i - 1)[1]
                if path:
                    textures.append(("", path))
    return textures, parent


def base_colour(path: str, load: Callable[[str], Optional[Package]], depth: int = 0) -> str:
    """The package path of the base colour texture a material paints with, or ""."""
    if not path or depth > 6:
        return ""
    p = load(path)
    if p is None:
        return ""
    textures, parent = _texture_parameters(p)
    scored = sorted(((_score(name, texture.rsplit("/", 1)[-1]), i, texture)
                     for i, (name, texture) in enumerate(textures)), key=lambda s: (-s[0], s[1]))
    if scored and scored[0][0] >= 2:
        return scored[0][2]
    inherited = base_colour(parent, load, depth + 1) if parent else ""
    if inherited:
        return inherited
    return scored[0][2] if scored and scored[0][0] >= 1 else ""
