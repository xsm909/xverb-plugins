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

"""Blender — F3 draws what is in a `.blend`, Shift+F3 says what is in it.

This is a utility for looking, not a Blender viewer. The model comes first
because that is what a model file is opened for; the table of contents is a
keystroke away and is the answer whenever there is no picture to give.

Two of every ten working files hold no geometry at all — they are rigs, or they
link their meshes out of another `.blend` — and that case is what decides
whether the tool is useful or a liar. Such a file gets a full answer saying
what it does hold and which file its geometry lives in, never an error and
never the word "empty".
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import base64  # noqa: E402
import hashlib  # noqa: E402
from urllib.parse import quote  # noqa: E402

from xverb import Plugin, error, markdown  # noqa: E402

import blendfile  # noqa: E402
import catalog  # noqa: E402
import geometry  # noqa: E402

plugin = Plugin("org.xverb.blend", "Blender")

#: Files are read whole. The heaviest the reader has been pointed at is 63 MB;
#: the cap is for the one that is not.
MAX_BYTES = 512 << 20

#: How many meshes a table lists before it says how many more there are. A
#: scene file with hundreds of them is a listing, not a table of contents.
MOST_ROWS = 40


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%d %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f GB" % size


def thousands(value: int) -> str:
    return "{:,}".format(value).replace(",", " ")


def _load(url: str):
    """The file, opened, or the content explaining why not."""
    data = plugin.read_file(url, max_bytes=MAX_BYTES)
    if not data:
        return None, None, error("The file is empty, or could not be read.")
    try:
        return blendfile.BlendFile(data), len(data), None
    except blendfile.BlendError as failure:
        # These messages are written to be read by whoever pressed F3: which
        # Blender wrote the file, or which compression it uses. They are the
        # answer, not a stack trace.
        return None, None, error(str(failure))
    except Exception as failure:  # noqa: BLE001 - a malformed file is not a crash
        return None, None, error(
            "This file could not be read as a .blend: %s" % failure)


@plugin.viewer("blend.contents", "What is in the file", extensions=["blend"],
               priority=10)
def contents(url: str) -> dict:
    """What the file holds, in full: Shift+F3.

    It is also what the model view falls back to, so this is never the only way
    to reach it — a `.blend` with nothing to draw gives this answer to F3 as
    well, rather than an error or an empty canvas.
    """
    opened, size, refusal = _load(url)
    if refusal is not None:
        return refusal
    try:
        facts = catalog.summarise(opened)
    except Exception as failure:  # noqa: BLE001 - one odd file is not a crash
        return error("This file was read, but what is in it could not be "
                     "counted: %s" % failure)
    return markdown(report(facts, size, os.path.basename(url)))


#: What the model view says under the picture, always. It is not a caveat
#: tucked away in a README: a subdivided character really does arrive faceted
#: and a mirrored half really is missing, and somebody who has not been told
#: that reads it as a broken reader rather than as the file's own contents.
BASE_CAGE = ("the file as saved — modifiers are worked out when Blender opens "
             "it and are not in it, so this is the base cage")


@plugin.viewer("blend.model", "Model", extensions=["blend"], priority=20,
               produces="model")
def model(url: str) -> dict:
    """The picture, where there is one.

    **Where there is not, this answers with the table of contents rather than
    an error.** A rig, or a file whose meshes are linked out of another
    `.blend`, is an ordinary file and two in ten of a real collection are one.
    Since this is what F3 opens, an error here would be the first thing seen of
    a file that was read perfectly well.
    """
    opened, size, refusal = _load(url)
    if refusal is not None:
        return refusal

    try:
        parts, note = geometry.meshes(opened)
    except Exception as failure:  # noqa: BLE001 - one odd file is not a crash
        return error("The geometry in this file could not be read: %s" % failure)

    if not parts:
        try:
            facts = catalog.summarise(opened)
        except Exception as failure:  # noqa: BLE001
            return error("This file was read, but what is in it could not be "
                         "counted: %s" % failure)
        if note["droppedMeshes"]:
            # Read perfectly, and too heavy to draw — which is a different
            # answer from "nothing to draw" and has to read as one. A sculpt of
            # two million triangles is not a file this failed on.
            said = ("This file holds %s triangles, past the %s a preview "
                    "draws. Nothing is shown rather than a part of it shown "
                    "as if it were the whole. This is what it holds."
                    % (thousands(note["held"]),
                       thousands(geometry.MAX_TRIANGLES)))
        else:
            said = "There is nothing in this file to draw. This is what it holds."
        return markdown(report(facts, size, os.path.basename(url),
                               preface=said))

    images, missing, unreadable = _pictures(url, parts)
    return mesh3d(parts, note, images, missing, unreadable)


#: Pictures are sent whole, and a character with fifteen 2K maps packed into it
#: would otherwise put tens of megabytes through a pipe meant for a preview.
MAX_PICTURE_BYTES = 24 << 20

#: What the host can actually decode, by the first bytes of the file. A picture
#: it cannot read costs the pipe its whole size and the host an error, and the
#: mesh falls back to its material colour either way — so it is not sent.
_DECODABLE = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"RIFF")


def _readable(data: bytes) -> bool:
    if data[:4] == b"RIFF":
        return data[8:12] == b"WEBP"
    return any(data.startswith(magic) for magic in _DECODABLE)


def _rooted(name: str) -> bool:
    """Whether a name is a place on somebody else's machine.

    Real files put them there: a character here names its textures at
    `F:\\BMS\\19_SWAT\\MASTERS\\RIG\\2K\\Textures\\Arm_color.jpg`. Such a name
    says nothing about where the picture is now, so only the name at the end of
    it is ever used.
    """
    return name.startswith("/") or (len(name) > 1 and name[1] == ":")


def _places(url: str, picture: dict) -> list:
    """Where to look for a picture the file does not carry, in order.

    Blender writes `//` for "beside this file" and then the separators of the
    machine that saved it, so a Windows path has to be turned round before it
    means anything anywhere else. Relative and downward first, as written; then
    the bare name beside the model and in a `textures` folder, which is how a
    model with its maps unpacked is actually laid out.
    """
    folder = url.rsplit("/", 1)[0] if "/" in url else url
    above = folder.rsplit("/", 1)[0] if "/" in folder else folder
    wanted = (picture.get("beside") or "").replace("\\", "/")
    name = picture.get("name") or wanted.rsplit("/", 1)[-1]

    out = []
    if wanted and not _rooted(wanted) and ".." not in wanted:
        out.append(folder + "/" + quote(wanted.lstrip("./")))
    if name:
        for place in (folder, folder + "/textures", folder + "/Textures",
                      above + "/textures"):
            candidate = place + "/" + quote(name)
            if candidate not in out:
                out.append(candidate)
    return out


def _pictures(url: str, parts: list) -> tuple:
    """Every bitmap the meshes want, read once, and each mesh told which.

    Two meshes painted with one material share one picture; a file that packs
    its own needs nothing read at all.

    Also counts the pictures a file *names* and does not have. A model whose
    maps live five folders up on a drive that is not here — which is what a
    real one says — then draws in flat colour, and the view can say why instead
    of leaving it to look like a fault in the reader.
    """
    images: list = []
    known: dict = {}
    missing: set = set()
    unreadable: set = set()
    spent = 0

    for mesh in parts:
        mesh["image"] = -1
        picture = mesh.get("picture")
        if not picture:
            continue

        data = picture.get("bytes")
        name = picture.get("name") or picture.get("beside") or "?"
        if data is None:
            for attempt in _places(url, picture):
                try:
                    data = plugin.read_file(attempt,
                                            max_bytes=MAX_PICTURE_BYTES - spent)
                except Exception:  # noqa: BLE001 - a missing texture is not a crash
                    data = None
                if data:
                    break
            if not data:
                missing.add(name)
                continue

        key = hashlib.sha1(data).hexdigest()
        if key in known:
            mesh["image"] = known[key]
            continue
        if spent + len(data) > MAX_PICTURE_BYTES or not _readable(data):
            # Found, and no use: past the cap, or in a form the host has no
            # decoder for. Which of the two it is matters to whoever is
            # looking, so it is not lumped in with a picture that is simply
            # not there.
            unreadable.add(name)
            continue
        spent += len(data)
        known[key] = len(images)
        mesh["image"] = len(images)
        images.append({"name": name.rsplit("/", 1)[-1],
                       "data": base64.b64encode(data).decode("ascii")})

    return images, len(missing), len(unreadable)


def mesh3d(parts: list, note: dict, images: list, missing: int = 0,
           unreadable: int = 0) -> dict:
    """The content the host draws.

    Numbers travel packed and base64'd rather than as JSON arrays: a mesh of
    ninety thousand triangles is over a million numbers, and written out as
    decimal digits that is megabytes of text to parse before anything appears.
    """
    short = note["held"] > note["triangles"]
    said = [BASE_CAGE]
    if short:
        said.append("%s of the file's %s triangles"
                    % (thousands(note["triangles"]), thousands(note["held"])))
    if note.get("linked"):
        said.append("%d object(s) keep their mesh in another file, which is "
                    "named in full under Shift+F3 and not opened" % note["linked"])
    if missing:
        said.append("%d picture(s) this file names are neither packed into it "
                    "nor beside it" % missing)
    if unreadable:
        said.append("%d picture(s) in a form this cannot read" % unreadable)
    return {
        "kind": "mesh3d",
        "triangles": note["triangles"],
        # A preview that quietly shows a third of a model is a preview that
        # lies. There is a cap, a scene file can reach it, and when it is
        # reached the view has to be able to say so.
        "truncated": short,
        "detail": " · ".join(said),
        "images": images,
        "meshes": [
            {
                "name": part["name"],
                "color": part.get("color") or "",
                #: Which of `images` this mesh is painted with, or −1 for none.
                "image": part.get("image", -1),
                # Only where there is a picture to read them against: on a
                # model with none they are eight bytes a vertex saying nothing.
                "uvs": base64.b64encode(geometry.pack_floats(part["uvs"]))
                       .decode("ascii")
                       if part.get("image", -1) >= 0 and part.get("uvs") else "",
                "positions": base64.b64encode(
                    geometry.pack_floats(part["positions"])).decode("ascii"),
                "normals": base64.b64encode(
                    geometry.pack_floats(part["normals"])).decode("ascii"),
                "indices": base64.b64encode(
                    geometry.pack_indices(part["indices"])).decode("ascii"),
            }
            for part in parts
        ],
    }


def report(facts: dict, size: int, name: str = "", preface: str = "") -> str:
    lines: list = []
    out = lines.append

    out("# Blender %s" % blendfile.version_text(facts["version"]))
    out("")
    if preface:
        out(preface)
        out("")
    out("| | |")
    out("| --- | --- |")
    out("| Size | %s |" % human(size))
    out("| Pointers | %d-bit%s |" % (
        facts["pointerSize"] * 8, ", big-endian" if facts["bigEndian"] else ""))
    out("| Blocks | %s, over %s struct definitions |"
        % (thousands(facts["blocks"]), thousands(facts["structs"])))
    if facts["compressed"]:
        out("| Saved | compressed |")
    out("")

    if facts["truncated"]:
        out("> This file ends before its last block does. Everything above and "
            "below was read from the part that is there; something is missing "
            "from the end.")
        out("")

    _objects(out, facts)
    _geometry(out, facts)
    _elsewhere(out, facts)
    _holds(out, facts)

    return "\n".join(lines)


def _objects(out, facts: dict) -> None:
    kinds = facts["objects"]
    if not kinds:
        return
    out("## Objects")
    out("")
    out("%s in all — %s" % (
        thousands(sum(kinds.values())),
        " · ".join("%s %s" % (name, thousands(count))
                   for name, count in sorted(kinds.items(),
                                             key=lambda kv: (-kv[1], kv[0])))))
    out("")


def _geometry(out, facts: dict) -> None:
    """Always a section, even — especially — when there is nothing to draw.

    A file with no drawable mesh is an ordinary file, not a failure, and the
    one thing it must get is a straight answer about *why*. Leaving the heading
    out and letting the reader work it out from an absence is how a tool starts
    looking broken on the two files in ten that are like this.
    """
    meshes = [m for m in facts["meshes"] if m["triangles"] > 0]
    empty = [m for m in facts["meshes"] if m["triangles"] <= 0]
    linked = facts["linked"]

    if not meshes:
        out("## Geometry")
        out("")
        said = []
        if linked["objects"]:
            said.append("%s of its objects keep their data in another file"
                        % thousands(linked["objects"]))
        if empty:
            said.append("%d mesh datablock(s) here hold no faces" % len(empty))
        rigs = facts["objects"].get("Armature", 0)
        if rigs and not linked["objects"]:
            said.append("what it does hold is %d rig(s) and the objects posing "
                        "them" % rigs)
        if said:
            out("Nothing in this file is drawable: %s." % "; ".join(said))
        else:
            out("Nothing in this file is drawable — it carries no mesh.")
        out("")
        return

    standing = facts["placed"]
    out("## Geometry — %s triangles in %d mesh(es)"
        % (thousands(facts["triangles"]), len(meshes)))
    out("")
    if standing["triangles"] and standing["triangles"] != facts["triangles"]:
        # One mesh stood in forty places is stored once and drawn forty times,
        # and a report that gave only the stored figure would make the model
        # view look like it had invented geometry.
        out("Stood in the scene %s time(s), which is %s triangles as drawn."
            % (thousands(standing["objects"]),
               thousands(standing["triangles"])))
        out("")
    out("| Mesh | Vertices | Faces | Triangles |")
    out("| --- | ---: | ---: | ---: |")
    for mesh in meshes[:MOST_ROWS]:
        out("| %s | %s | %s | %s |" % (
            mesh["name"], thousands(mesh["vertices"]),
            thousands(mesh["faces"]), thousands(mesh["triangles"])))
    if len(meshes) > MOST_ROWS:
        out("| … and %d more | | | |" % (len(meshes) - MOST_ROWS))
    out("")
    # The single most important sentence in this report, and the reason it is
    # here rather than worked around: what is in the file is the base cage.
    # Subdivision, mirroring, geometry nodes and shape keys are worked out when
    # Blender opens the file and are not written into it, so a subdivided
    # character reads as faceted here and a mirrored half is missing. A reader
    # told this once is not surprised by it.
    out("Counted from the file as saved. Modifiers — subdivision, mirroring, "
        "geometry nodes, shape keys — are worked out by Blender when it opens "
        "the file and are not in the file, so what is counted here, and drawn "
        "by the model view, is the base cage.")
    out("")
    if empty:
        out("%d further mesh datablock(s) hold no faces." % len(empty))
        out("")


def _elsewhere(out, facts: dict) -> None:
    """Why a file with no geometry has none — the case that decides this tool.

    A `.blend` with nothing to draw is an ordinary file. What it must never do
    is look like a failure, so this section says plainly where the geometry is
    and names the file it is in, exactly as the `.blend` writes it.
    """
    linked = facts["linked"]
    if not linked["objects"] and not linked["libraries"]:
        return

    out("## Linked from elsewhere")
    out("")
    if linked["objects"]:
        kinds = ", ".join(
            "%s %d" % (name, count)
            for name, count in sorted(linked["byKind"].items(),
                                      key=lambda kv: (-kv[1], kv[0])))
        out("%s object(s) here hold no data of their own — %s. What they are "
            "made of lives in the file(s) below, which this names and does not "
            "open." % (thousands(linked["objects"]), kinds))
        out("")
    if linked["libraries"]:
        out("| File it comes from |")
        out("| --- |")
        for path in linked["libraries"][:MOST_ROWS]:
            # As the file writes it: `//` is Blender's "beside this one", and
            # the separators are the authoring machine's. Resolving it here
            # would be a guess about a machine this is not running on.
            out("| `%s` |" % path.replace("|", "\\|"))
        if len(linked["libraries"]) > MOST_ROWS:
            out("| … and %d more |" % (len(linked["libraries"]) - MOST_ROWS))
        out("")


def _holds(out, facts: dict) -> None:
    rows = facts["datablocks"]
    if not rows:
        return
    out("## What the file holds")
    out("")
    out(" · ".join("%s %s" % (label, thousands(count)) for label, count in rows))
    out("")

    for title, names in (("Scenes", facts["scenes"]),
                         ("Armatures", facts["armatures"]),
                         ("Actions", facts["actions"])):
        if not names:
            continue
        shown = ", ".join(names[:12])
        if len(names) > 12:
            shown += " … and %d more" % (len(names) - 12)
        out("**%s.** %s" % (title, shown))
        out("")


plugin.run()
