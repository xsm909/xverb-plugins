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

"""Unreal assets — `.uasset`, `.umap` — as the Content Browser shows them.

F3 gives one page: the thumbnail the editor saved, the class, the engine that
saved it, the file it was imported from, what the Content Browser shows on
hover, and the packages it uses. The film strip gets the thumbnails too, so a
folder of assets walks like a folder of pictures.

Only the header is read — see `uasset.py` — and only as much of the file as
the header's tables reach: the start, then more if a table runs past it.
"""

from __future__ import annotations

import base64
import html as _html
import json
import os
import struct
import sys
from typing import List, Optional
from urllib.parse import quote, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xverb import Plugin, error, html, image  # noqa: E402

import uasset  # noqa: E402
import ueanim  # noqa: E402
import uemesh  # noqa: E402
import uetexture  # noqa: E402

plugin = Plugin("org.xverb.unreal", "Unreal assets")

#: Read first; the header's tables are within the first quarter of every
#: package measured, and within 3 MB of the largest.
FIRST_BYTES = 1 << 20
#: Never more than this, whatever a damaged header says.
MOST_BYTES = 64 << 20

#: Tags that are machinery, not description: the blueprint search index, a
#: level's actor list, and the like — long, and unreadable to a person.
_NOISE = {"FiBData", "ActorsMetaData", "AssetImportData", "ImportedNamespaces",
          "ClassFlags", "BlueprintPath", "PackageLocalizationNamespace",
          "ChunkDependencies", "ExternalActors", "ExternalObjects"}
_LONGEST = 240


def _package(url: str) -> uasset.Package:
    """The header, read as far as it needs."""
    size = int((plugin.stat(url) or {}).get("size") or 0)
    want = FIRST_BYTES if size <= 0 else min(size, FIRST_BYTES)
    while True:
        data = plugin.read_file(url, max_bytes=want)
        truncated = len(data) >= want and (size <= 0 or want < size)
        try:
            package = uasset.Package(data)
            # Asking the tables is what finds out whether they fit.
            package.thumbnails()
            package.registry()
            package.imports()
            beyond = package.reach() >= len(data)
            if not truncated or not (package.problems or beyond):
                return package
            # A table starts past what was read: read to it and a little on.
            need = package.reach() + FIRST_BYTES if beyond else 0
        except uasset.UassetError:
            if not truncated:
                raise
            need = 0
        if want >= MOST_BYTES:
            return uasset.Package(data)
        want = min(max(want * 4, need), MOST_BYTES, size or MOST_BYTES)


def _thumbnail(url: str, pixels: int) -> Optional[bytes]:
    try:
        thumbs = _package(url).thumbnails()
    except Exception:  # noqa: BLE001 - a strip cell with a name is fine
        return None
    return thumbs[0]["bytes"] if thumbs else None


def _whole(url: str) -> uasset.Package:
    """The whole package — an animation's keys are past the header."""
    return uasset.Package(plugin.read_file(url, max_bytes=MOST_BYTES))


def _open(url: str):
    try:
        return _package(url), None
    except uasset.UassetError as failure:
        return None, error(plugin.tr(str(failure)))
    except Exception as failure:  # noqa: BLE001
        return None, error(plugin.tr("The package could not be read: {error}", {"error": failure}))


@plugin.viewer("unreal.asset", "Unreal asset", extensions=["uasset", "umap"],
               priority=30, produces="unreal", thumbnail=_thumbnail)
def asset(url: str) -> dict:
    """An animation plays on its skeleton, a skeleton stands in its rest pose,
    and anything else is the page of what it is."""
    package, refusal = _open(url)
    if refusal is not None:
        return refusal
    klass = package.main_class()
    notes: List[str] = []
    if klass in ("Texture2D", "TextureCube", "Texture2DArray", "VolumeTexture"):
        try:
            data, mime, _ = uetexture.picture(_whole(url))
            return image(data, mime)
        except uemesh.NeedsOodle:
            notes.append(plugin.tr(
                "The picture is compressed with Oodle, whose decompressor comes with Unreal "
                "Engine; with Unreal installed on this machine it is shown."))
        except (uetexture.TextureError, uemesh.MeshError, uasset.UassetError,
                struct.error, IndexError, ValueError) as failure:
            notes.append(plugin.tr("The picture could not be read: {error}", {"error": failure}))
    if klass in ("StaticMesh", "SkeletalMesh"):
        try:
            shown = _mesh_in_3d(url, klass, notes)
            if shown is not None:
                return shown
        except uemesh.NeedsOodle:
            notes.append(plugin.tr(
                "The mesh is compressed with Oodle, whose decompressor comes with Unreal "
                "Engine; with Unreal installed on this machine it is shown in 3D."))
        except (uemesh.MeshError, uasset.UassetError, struct.error, IndexError, KeyError) as failure:
            notes.append(plugin.tr("The mesh could not be read: {error}", {"error": failure}))
    if klass in ("AnimSequence", "Skeleton"):
        try:
            shown = _in_3d(url, klass, notes)
            if shown is not None:
                return shown
        except (uasset.UassetError, struct.error, IndexError, ValueError) as failure:
            notes.append(plugin.tr("The animation could not be read: {error}", {"error": failure}))
    return html(_page(url, package, notes))


@plugin.viewer("unreal.info", "What is in the asset", extensions=["uasset", "umap"],
               priority=10)
def info(url: str) -> dict:
    package, refusal = _open(url)
    if refusal is not None:
        return refusal
    return html(_page(url, package))


def _in_3d(url: str, klass: str, notes: List[str]) -> Optional[dict]:
    whole = _whole(url)
    title = unquote(url.rstrip("/").rsplit("/", 1)[-1]).rsplit(".", 1)[0]
    if klass == "Skeleton":
        skeleton = ueanim.skeleton(whole)
        return _mesh3d(skeleton, None, title) if skeleton else None
    anim = ueanim.animation(whole)
    if anim is None or not anim.tracks:
        notes.append(plugin.tr("This animation moves no bones: it holds curves only."))
        return None
    path = ueanim.skeleton_path(whole)
    where = _find_package(url, path) if path else None
    if where is None:
        notes.append(plugin.tr(
            "Its skeleton, {path}, is not where this project keeps it, so it cannot be played.",
            {"path": path or "?"}))
        return None
    skeleton_package = _whole(where)
    skeleton = ueanim.skeleton(skeleton_package)
    if skeleton is None:
        return None
    # On its body, where the skeleton names the mesh it is previewed on and
    # that mesh can be unpacked; on its bones otherwise.
    body_path = ueanim.preview_mesh_path(skeleton_package)
    body_url = _find_package(url, body_path) if body_path else None
    if body_url is not None:
        try:
            md = _largest(uemesh.descriptions(_whole(body_url).data))
            bones = uemesh.bones(md) if md else None
            if bones is not None:
                return _body(md, ueanim.Skeleton(*bones), anim, title)
        except uemesh.NeedsOodle:
            notes.append(plugin.tr(
                "Its body is compressed with Oodle, which comes with Unreal Engine; "
                "without Unreal on this machine it plays on its bones."))
        except (uemesh.MeshError, uasset.UassetError, struct.error, IndexError, KeyError):
            pass
    return _mesh3d(skeleton, anim, title)


def _largest(found: list):
    """LOD 0 — of a mesh's descriptions, the one with the most triangles."""
    return max(found, key=lambda md: len(md["Triangles"][0]), default=None)


def _mesh_in_3d(url: str, klass: str, notes: List[str]) -> Optional[dict]:
    whole = _whole(url)
    md = _largest(uemesh.descriptions(whole.data))
    if md is None:
        notes.append(plugin.tr("No mesh description was found in this package."))
        return None
    title = unquote(url.rstrip("/").rsplit("/", 1)[-1]).rsplit(".", 1)[0]
    bones = uemesh.bones(md) if klass == "SkeletalMesh" else None
    if bones is not None:
        return _body(md, ueanim.Skeleton(*bones), None, title)
    return _body(md, None, None, title)


#: Shades for the material slots, since the materials are other assets.
_SHADES = ["#B8B2A7", "#8FA3B5", "#B59A8F", "#9DB58F", "#A99FC0", "#C2B48A"]

MAX_TRIANGLES = 400000


def _body(md, skeleton, anim, title: str) -> dict:
    """Triangles, and — for a skeletal mesh — the skin, the bones and a clip."""
    parts, total = uemesh.parts(md)
    meshes = []
    kept = 0
    for at, part in enumerate(parts):
        count = len(part["indices"]) // 3
        if kept + count > MAX_TRIANGLES:
            continue
        kept += count
        entry = {
            "name": part["name"],
            "color": _SHADES[at % len(_SHADES)],
            "image": -1,
            "uvs": "",
            "bones": "",
            "boneParents": "",
            "joints": 0,
            "jointIndices": "",
            "jointWeights": "",
            "positions": _floats(part["positions"]),
            "normals": _floats(part["normals"]),
            "indices": base64.b64encode(struct.pack(
                "<%dI" % len(part["indices"]), *part["indices"])).decode("ascii"),
        }
        if skeleton is not None:
            indices, weights = uemesh.skin(md, part)
            entry.update({
                "bones": _floats(ueanim.rest_bones(skeleton)),
                "boneParents": base64.b64encode(struct.pack(
                    "<%dh" % len(skeleton.parents), *skeleton.parents)).decode("ascii"),
                "joints": len(skeleton.names),
                "jointIndices": base64.b64encode(struct.pack(
                    "<%dH" % len(indices), *indices)).decode("ascii"),
                "jointWeights": _floats(weights),
            })
        meshes.append(entry)
    clips = ueanim.clip(skeleton, anim, title) if skeleton is not None and anim is not None else []
    for c in clips:
        # One track a mesh: every part of the body moves with the same bones.
        c["tracks"] = [_floats(c["tracks"][0])] * len(meshes)
    return {
        "kind": "mesh3d",
        "upAxis": "Y",
        "unitScale": 1.0,
        "triangles": kept,
        "truncated": kept < total,
        "detail": "",
        "clips": clips,
        "images": [],
        "meshes": meshes,
    }


def _find_package(url: str, path: str) -> Optional[str]:
    """Where a package path like `/Game/Characters/SK_Body` is, beside this file.

    A path starts with its mount — `/Game` for the project, a plugin's name
    for a plugin — and the rest is under a `Content` folder; the one this
    file is under is the first place looked. Then the same name in this
    folder and the ones above it, for assets copied out of their project.
    """
    rel = [quote(part) for part in path.strip("/").split("/")[1:]]
    if not rel:
        return None
    folder, _, _ = url.rpartition("/")
    parts = folder.split("/")
    tried = []
    for i in range(len(parts) - 1, 2, -1):
        if unquote(parts[i]) == "Content":
            tried.append("/".join(parts[:i + 1] + rel) + ".uasset")
    name = rel[-1] + ".uasset"
    for up in range(0, 4):
        base = "/".join(parts[:len(parts) - up]) if up else folder
        if base.count("/") > 2:
            tried.append(base + "/" + name)
    for candidate in tried:
        if plugin.stat(candidate):
            return candidate
    return None


def _floats(values) -> str:
    return base64.b64encode(struct.pack("<%df" % len(values), *values)).decode("ascii")


def _mesh3d(skeleton: ueanim.Skeleton, anim: Optional[ueanim.Animation], title: str) -> dict:
    """The host's 3D view, in the form the 3D plugin sends a rig without a body."""
    mesh, clips = ueanim.model(skeleton, anim, title)
    return {
        "kind": "mesh3d",
        "upAxis": "Y",
        "unitScale": 1.0,
        "triangles": 0,
        "truncated": False,
        "detail": "",
        "clips": [{"name": c["name"], "frames": c["frames"], "fps": c["fps"],
                   "seconds": c["seconds"], "tracks": [_floats(t) for t in c["tracks"]]}
                  for c in clips],
        "images": [],
        "meshes": [{
            "name": mesh["name"],
            "color": "",
            "image": -1,
            "uvs": "",
            "bones": _floats(mesh["bones"]),
            "boneParents": base64.b64encode(struct.pack(
                "<%dh" % len(mesh["boneParents"]), *mesh["boneParents"])).decode("ascii"),
            "joints": mesh["joints"],
            "jointIndices": "",
            "jointWeights": "",
            "positions": "",
            "normals": "",
            "indices": "",
        }],
    }


def _source_file(value: str) -> str:
    """The file an asset was imported from, out of AssetImportData's JSON."""
    try:
        found = json.loads(value)
    except ValueError:
        return ""
    for entry in found if isinstance(found, list) else []:
        if isinstance(entry, dict) and entry.get("RelativeFilename"):
            return str(entry["RelativeFilename"])
    return ""


def _clean(value: str) -> str:
    """`Class'/Script/Engine.Character'` as `Character`, and the like."""
    if value.endswith("'") and "'" in value[:-1]:
        inner = value[value.index("'") + 1:-1]
        return inner.rsplit(".", 1)[-1] if "/Script/" in inner else inner
    return value


def _page(url: str, package: uasset.Package, notes: Optional[List[str]] = None) -> str:
    tr = plugin.tr
    e = _html.escape
    name = url.rstrip("/").rsplit("/", 1)[-1]
    name = name.rsplit(".", 1)[0]
    registry = package.registry()
    main = registry[0] if registry else {"class": "", "tags": {}}
    klass = (main["class"] or package.main_class()).rsplit(".", 1)[-1]
    tags = main["tags"]
    thumbs = package.thumbnails()

    out: List[str] = ["<html><body>"]
    out.append("<h1>%s</h1>" % e(name))
    out.append("<p>%s</p>" % e(" · ".join(x for x in (klass, package.engine()) if x)))
    if thumbs:
        picture = thumbs[0]
        kind = "jpeg" if picture["bytes"][:2] == b"\xff\xd8" else "png"
        # Twice the saved size, which is 256 for nearly everything; a
        # thumbnail saved a few pixels wide is still shown at a size to see.
        scale = max(256 / max(picture["width"], picture["height"]), 1) * 2
        out.append('<p><img src="data:image/%s;base64,%s" width="%d" height="%d" alt="%s"></p>' % (
            kind, base64.b64encode(picture["bytes"]).decode("ascii"),
            min(512, round(picture["width"] * scale)), min(512, round(picture["height"] * scale)),
            e(name)))
    elif package.ue4 and not thumbs:
        out.append("<p><i>%s</i></p>" % e(tr("The editor saved no thumbnail for this asset.")))

    where = package.package_name if package.package_name not in ("", "None") else \
        (main.get("object") or "").split(".", 1)[0]
    rows = [(tr("Class"), klass),
            (tr("Package"), where),
            (tr("Saved by"), package.engine()),
            (tr("Works in"), package.compatible if package.compatible != package.saved_by else "")]
    source = _source_file(tags.get("AssetImportData", ""))
    if source:
        rows.append((tr("Imported from"), source))
    if package.unversioned:
        rows.append((tr("Cooked"), tr("for a game; its versions were left out")))
    rows.append((tr("In the package"), tr("{exports} object(s), {imports} import(s), {names} name(s)", {
        "exports": package.export_count, "imports": package.import_count,
        "names": package.name_count})))
    out.append("<table>")
    for label, value in rows:
        if value:
            out.append("<tr><th align=\"left\">%s</th><td>%s</td></tr>" % (e(label), e(str(value))))
    out.append("</table>")

    shown = [(k, _clean(v)) for k, v in tags.items()
             if k not in _NOISE and v not in ("", "None", "()") and len(v) <= _LONGEST]
    if shown:
        out.append("<h2>%s</h2>" % e(tr("What the Content Browser shows")))
        out.append("<table>")
        for key, value in shown:
            out.append("<tr><th align=\"left\">%s</th><td>%s</td></tr>" % (e(key), e(value)))
        out.append("</table>")

    if len(registry) > 1:
        out.append("<h2>%s</h2>" % e(tr("Also in the package")))
        out.append("<ul>")
        for entry in registry[1:30]:
            out.append("<li>%s — %s</li>" % (e(entry["object"].rsplit(".", 1)[-1]),
                                            e(entry["class"].rsplit(".", 1)[-1])))
        out.append("</ul>")

    used = package.packages_used()
    if used:
        out.append("<h2>%s</h2>" % e(tr("Uses {count} other asset(s)", {"count": len(used)})))
        out.append("<ul>")
        for path in used[:60]:
            out.append("<li>%s</li>" % e(path))
        if len(used) > 60:
            out.append("<li>%s</li>" % e(tr("and {count} more", {"count": len(used) - 60})))
        out.append("</ul>")

    for note in notes or []:
        out.append("<p><i>%s</i></p>" % e(note))
    if package.problems:
        out.append("<p><i>%s</i></p>" % e(tr(
            "Part of the header could not be read: {parts}.",
            {"parts": ", ".join(package.problems)})))
    out.append("</body></html>")
    return "\n".join(out)


if __name__ == "__main__":
    plugin.run()
