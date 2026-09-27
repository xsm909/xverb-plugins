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

"""A Houdini scene — `.hip`, `.hipnc`, `.hiplc` — drawn as its networks.

**A scene file holds no geometry.** It holds the nodes that make it: a grid,
a group, a copy, an instancer, each with its parameters and its wires, and
Houdini cooks them when the scene is opened. So a scene is a node graph, and
that is how it is shown — every network in the file (`/obj/geo1`, `/stage`,
`/mat`…) a frame of its own, stacked, each node where the artist put it.

**The file is an archive of small texts**, one per thing, named by the node's
path: `obj/geo1/grid1.init` says what type it is, `.def` where it stands,
its flags and what is wired into it, `.parm` its parameters. A commercial
`.hip` joins them as MIME parts; the Apprentice and Indie ones, `.hipnc` and
`.hiplc`, each open a part with `HouNC` or `HouLC` and a header, then the
name and a zero, then the text. The same parts either way.

**What a box says.** Its name, and under it its type (`grid`, `copy`,
`brook::dev::treeAsset::1.0`); its first parameters as fields, the way the
parameter pane lists them; badges for what the flags say — display, render,
bypassed, locked — and for a node that is a network of its own. The node with
the display flag is an output; a node that reads from outside the scene
(`file`, `alembic`, `object_merge`, `reference`) is an input. The colours are
the palette's, never the file's.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

#: Houdini places nodes in network units, y growing upwards; a box on the
#: canvas is about this many points per unit, y growing down.
_SCALE_X = 170.0
_SCALE_Y = -120.0

#: The room between two networks, and around the nodes inside a frame.
_GAP = 120.0
_PAD = 60.0

_READS = {"file", "alembic", "object_merge", "reference", "sublayer", "lopimport",
          "usdimport", "filecache", "rop_geometry", "fbxarchive", "file::2.0",
          "filecache::2.0", "geometryvopglobal", "subinput", "parameter",
          "globalnode", "sopimport"}
_WRITES = {"output", "geometryvopoutput", "suboutput", "rop_geometry", "rop_alembic",
           "usd_rop", "karma", "opengl", "ifd", "null"}

_MIME_PART = re.compile(rb'filename="([^"]*)"[^\n]*\n(?:[^\n]*\n)*?\n', re.S)


def is_scene(head: bytes) -> bool:
    """Whether the first bytes are a Houdini scene of any licence."""
    return head.startswith((b"HouNC\x1a", b"HouLC\x1a")) or (
        head.startswith(b"MIME-Version") and b"HOUDINIMIMEBOUNDARY" in head[:400])


def parts(data: bytes) -> Dict[str, bytes]:
    """The archive's texts, by name, in file order."""
    out: Dict[str, bytes] = {}
    for marker in (b"HouNC\x1a", b"HouLC\x1a"):
        if data.startswith(marker):
            for chunk in data.split(marker)[1:]:
                # A header of hex digits, then the name up to a zero.
                at = 0
                while at < len(chunk) and chr(chunk[at]) in "0123456789abcdef":
                    at += 1
                # The header's last digits run into the name when it begins
                # with one; the header is 28 digits in every file seen.
                at = min(at, 28)
                name, _, body = chunk[at:].partition(b"\0")
                out[name.decode("utf-8", "replace")] = body
            return out
    boundary = re.search(rb'boundary="([^"]+)"', data[:600])
    if not boundary:
        return out
    for section in data.split(b"--" + boundary.group(1)):
        head, sep, body = section.partition(b"\n\n")
        if not sep:
            head, sep, body = section.partition(b"\r\n\r\n")
        found = re.search(rb'filename="([^"]*)"', head)
        if found:
            out[found.group(1).decode("utf-8", "replace")] = body.rstrip(b"\r\n")
    return out


# -- a node's texts ------------------------------------------------------------------------

def _block(text: str, name: str) -> List[str]:
    """The lines of `name { … }` in a `.def`."""
    found = re.search(r"(?m)^%s\s*\n\{\n(.*?)\n\}" % re.escape(name), text, re.S)
    return found.group(1).splitlines() if found else []


def _definition(text: str) -> dict:
    position = re.search(r"(?m)^position\s+(-?[\d.e+-]+)\s+(-?[\d.e+-]+)", text)
    flags_line = re.search(r"(?m)^flags\s*=\s*(.*)$", text)
    flags = {}
    if flags_line:
        words = flags_line.group(1).split()
        for k in range(0, len(words) - 1, 2):
            flags[words[k]] = words[k + 1] == "on"
    wires = []
    for line in _block(text, "inputs"):
        words = line.split()
        if len(words) >= 3 and words[0].isdigit():
            wires.append((int(words[0]), words[1], int(words[2]) if words[2].isdigit() else 0))
    named_in = {}
    for line in _block(text, "inputsNamed3"):
        found = re.match(r'\s*(\d+)\s+(\S+)?.*?"([^"]*)"\s*$', line)
        if found:
            named_in[int(found.group(1))] = found.group(3)
    named_out = {}
    for line in _block(text, "outputsNamed3"):
        found = re.match(r'\s*(\d+)\s+"([^"]*)"', line)
        if found:
            named_out[int(found.group(1))] = found.group(2)
    comment = re.search(r'(?m)^comment\s+"((?:[^"\\]|\\.)*)"', text)
    return {
        "x": float(position.group(1)) if position else 0.0,
        "y": float(position.group(2)) if position else 0.0,
        "flags": flags,
        "wires": wires,
        "inputs": named_in,
        "outputs": named_out,
        "comment": comment.group(1) if comment else "",
    }


_PARM = re.compile(r"(?m)^(\w+)\s*\[[^\]]*\]\s*\(\s*(.*?)\s*\)\s*$")


def _parameters(text: str, most: int) -> List[dict]:
    fields = []
    for name, value in _PARM.findall(text):
        value = re.sub(r"\s+", " ", value.replace('"', "")).strip()
        if len(value) > 60:
            value = value[:57] + "…"
        fields.append({"label": name, "value": value})
        if len(fields) >= most:
            break
    return fields


# -- the graph ----------------------------------------------------------------------------

def read(data: bytes, max_nodes: int) -> Tuple[dict, int]:
    texts = parts(data)
    nodes_by_net: Dict[str, List[str]] = {}
    kinds: Dict[str, str] = {}
    for name, body in texts.items():
        if name.endswith(".init"):
            path = name[:-5]
            found = re.search(rb"(?m)^type\s*=\s*(\S+)", body)
            kinds[path] = found.group(1).decode("utf-8", "replace") if found else "?"
            network = path.rpartition("/")[0]
            nodes_by_net.setdefault(network, []).append(path)

    has_children = {net for net in nodes_by_net}
    # A dot on a wire is a point it bends at, and what it carries on is its
    # own input: `"input":"color1 0 1"`. Followed back to a node.
    dots: Dict[str, Tuple[str, int]] = {}
    for name, body in texts.items():
        if name.endswith(".networkdotinit"):
            found = re.search(rb'"input"\s*:\s*"(\S+)\s+(\d+)', body)
            if found:
                dots[name[:-len(".networkdotinit")]] = (
                    found.group(1).decode("utf-8", "replace"), int(found.group(2)))
    out_nodes: List[dict] = []
    links: List[dict] = []
    groups: List[dict] = []
    dropped = 0
    top = 0.0

    # Networks in the order a person walks a scene: objects, then the stage,
    # then everything else, each network before the ones inside it.
    order = sorted(nodes_by_net, key=lambda n: (_context_rank(n), n.count("/"), n))
    for network in order:
        members = nodes_by_net[network]
        defs = {p: _definition(texts.get(p + ".def", b"").decode("utf-8", "replace"))
                for p in members}
        if not defs:
            continue
        xs = [d["x"] for d in defs.values()]
        ys = [d["y"] for d in defs.values()]
        left, low_y, high_y = min(xs), min(ys), max(ys)
        height = (high_y - low_y) * -_SCALE_Y + 80
        width = (max(xs) - left) * _SCALE_X + 220
        frame_top = top
        for path in members:
            if len(out_nodes) >= max_nodes:
                dropped += 1
                continue
            d = defs[path]
            kind = kinds.get(path, "?")
            base = kind.split("::")[-2] if kind.count("::") >= 2 else kind.split("::")[0]
            flags = d["flags"]
            badges = []
            if flags.get("display"):
                badges.append("display")
            if flags.get("render") and not flags.get("display"):
                badges.append("render")
            if flags.get("bypass"):
                badges.append("bypassed")
            if flags.get("lock"):
                badges.append("locked")
            if path in has_children:
                badges.append("network")
            role = "normal"
            if flags.get("display") or base in _WRITES and base != "null":
                role = "output"
            elif base in _READS:
                role = "input"
            count_in = max([k + 1 for k in d["inputs"]] + [w[0] + 1 for w in d["wires"]] + [0])
            count_out = max([k + 1 for k in d["outputs"]] + [1])
            node = {
                "id": path,
                "title": path.rsplit("/", 1)[-1],
                "subtitle": kind,
                "role": role,
                "x": (d["x"] - left) * _SCALE_X + _PAD,
                "y": frame_top + (high_y - d["y"]) * -_SCALE_Y + _PAD,
                "group": network,
                "inputs": [{"id": "in%d" % k, "label": d["inputs"].get(k, "")}
                           for k in range(count_in)],
                "outputs": [{"id": "out%d" % k, "label": d["outputs"].get(k, "")}
                            for k in range(count_out)],
            }
            if badges:
                node["badges"] = badges
            fields = _parameters(texts.get(path + ".parm", b"").decode("utf-8", "replace"), 6)
            if d["comment"]:
                fields.insert(0, {"label": "comment", "value": d["comment"]})
            if fields:
                node["fields"] = fields
            out_nodes.append(node)
            for index, source, output in d["wires"]:
                # An input left empty is written with a name of "".
                if source in ('""', ""):
                    continue
                hops = 0
                while source.startswith("(") and hops < 64:
                    back = dots.get((network + "/" if network else "") + source.strip("()"))
                    if back is None:
                        break
                    source, output = back
                    hops += 1
                if source.startswith("("):
                    continue
                links.append({
                    "from": network + "/" + source if network else source,
                    "to": path,
                    "fromPin": "out%d" % output,
                    "toPin": "in%d" % index,
                    "role": "data",
                })
        groups.append({
            "id": network,
            "title": "/" + network,
            "x": 0.0,
            "y": frame_top,
            "width": width + 2 * _PAD,
            "height": height + 2 * _PAD,
        })
        top = frame_top + height + 2 * _PAD + _GAP

    note = []
    if not out_nodes:
        note.append({"text": "This scene holds no networks with nodes in them.",
                     "x": 0, "y": 0, "width": 360, "height": 60})
    return {"nodes": out_nodes, "links": links, "groups": groups, "notes": note,
            "layout": "given", "direction": "tb"}, dropped


def _context_rank(network: str) -> int:
    root = network.split("/", 1)[0]
    return {"obj": 0, "stage": 1, "mat": 2, "out": 3, "ch": 4}.get(root, 5)


def summary(data: bytes) -> dict:
    """What the scene says about itself, for the notes and a report."""
    texts = parts(data)
    start = texts.get(".start", b"").decode("utf-8", "replace")
    return {
        "nodes": sum(1 for n in texts if n.endswith(".init")),
        "fps": (re.search(r"(?m)^fps\s+(\S+)", start) or [None, ""])[1],
        "range": (re.search(r"(?m)^frange\s+(.+)$", start) or [None, ""])[1],
    }
