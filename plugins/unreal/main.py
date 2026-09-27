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
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xverb import Plugin, error, html  # noqa: E402

import uasset  # noqa: E402

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


@plugin.viewer("unreal.asset", "Unreal asset", extensions=["uasset", "umap"],
               priority=30, produces="unreal", thumbnail=_thumbnail)
def asset(url: str) -> dict:
    try:
        package = _package(url)
    except uasset.UassetError as failure:
        return error(plugin.tr(str(failure)))
    except Exception as failure:  # noqa: BLE001
        return error(plugin.tr("The package could not be read: {error}", {"error": failure}))
    return html(_page(url, package))


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


def _page(url: str, package: uasset.Package) -> str:
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

    if package.problems:
        out.append("<p><i>%s</i></p>" % e(tr(
            "Part of the header could not be read: {parts}.",
            {"parts": ", ".join(package.problems)})))
    out.append("</body></html>")
    return "\n".join(out)


if __name__ == "__main__":
    plugin.run()
