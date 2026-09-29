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
`.hip` is a cpio archive of them — the old portable kind, fields written in
octal — or, from some versions and exports, MIME parts; the Apprentice and
Indie ones, `.hipnc` and `.hiplc`, each open a part with `HouNC` or `HouLC`
and a header, then the name and a zero, then the text. The same parts
whichever way.

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

#: Houdini places nodes in network units, y growing upwards. A unit is at
#: least this many points on the canvas, y growing down — and more where the
#: boxes, which are taller than Houdini's, would otherwise land on each other.
_SCALE_X = 170.0
_SCALE_Y = 120.0
_MOST_SCALE = 2.0

#: How many boxes a row of a network laid out here holds before it wraps.
_WRAP = 8

#: A box, as the host draws one: its width, its title bar, and a row a pin or
#: a field.
_BOX_WIDTH = 250.0
_BOX_TITLE = 40.0
_BOX_ROW = 26.0
_GAP_X = 50.0
_GAP_Y = 36.0


def _box_height(definition: dict, fields: list) -> float:
    # The host gives each input a row and then each output one below them,
    # with the fields beside them from the top.
    ins = max([k + 1 for k in definition["inputs"]] + [w[0] + 1 for w in definition["wires"]] + [0])
    outs = max([k + 1 for k in definition["outputs"]] + [1])
    return _BOX_TITLE + max(ins + outs, len(fields), 1) * _BOX_ROW + 10


def _piled(defs: Dict[str, dict]) -> bool:
    """Whether many of a network's nodes share one place — a network nobody
    arranged, or one written by a script — so that no scaling can part them."""
    places = {}
    for d in defs.values():
        key = (round(d["x"], 3), round(d["y"], 3))
        places[key] = places.get(key, 0) + 1
    shared = sum(n for n in places.values() if n > 1)
    return len(defs) > 2 and shared > len(defs) // 4


def _layer(defs: Dict[str, dict], network: str) -> None:
    """Rows by the wires, top to bottom, the way Houdini flows: a node sits a
    row below the lowest of the nodes feeding it."""
    names = {path.rsplit("/", 1)[-1]: path for path in defs}
    feeds: Dict[str, List[str]] = {path: [] for path in defs}
    for path, d in defs.items():
        for _, source, _ in d["wires"]:
            if source in names:
                feeds[path].append(names[source])
    row: Dict[str, int] = {}

    def depth(path: str, seen: set) -> int:
        if path in row:
            return row[path]
        if path in seen:
            return 0
        seen.add(path)
        row[path] = 1 + max((depth(f, seen) for f in feeds[path]), default=-1)
        return row[path]

    for path in defs:
        depth(path, set())
    # A row of many — sixty render nodes with no wires between them — is
    # wrapped at eight, so it stays a block and not a line off the screen.
    by_row: Dict[int, List[str]] = {}
    for path in sorted(defs, key=lambda p: (row[p], p)):
        by_row.setdefault(row[path], []).append(path)
    line = 0
    for r in sorted(by_row):
        members = by_row[r]
        for i, path in enumerate(members):
            # Units, as the file's own positions are: the spacing turns them
            # into points like any other network's.
            defs[path]["x"] = (i % _WRAP) * 2.0
            defs[path]["y"] = -(line + i // _WRAP) * 2.0
        line += (len(members) + _WRAP - 1) // _WRAP


def _settle(nodes: List[dict], heights: Dict[str, float]) -> None:
    """What still overlaps after the spacing, pushed down until it does not."""
    placed: List[dict] = []
    for node in sorted(nodes, key=lambda n: (n["y"], n["x"])):
        moved = True
        while moved:
            moved = False
            for other in placed:
                if (node["x"] < other["x"] + _BOX_WIDTH + 8 and other["x"] < node["x"] + _BOX_WIDTH + 8
                        and node["y"] < other["y"] + heights[other["id"]] + 8
                        and other["y"] < node["y"] + heights[node["id"]] + 8):
                    node["y"] = other["y"] + heights[other["id"]] + _GAP_Y
                    moved = True
        placed.append(node)


def _spacing(defs: Dict[str, dict], tall: Dict[str, float]) -> Tuple[float, float]:
    """How many points a unit is, from how far apart the nodes usually are.

    Taken from the typical neighbour, not the closest pair: one pair of nodes
    left touching once set the scale of the whole network and flung the rest
    six times further apart than Houdini has them. What the typical spacing
    does not part, `_settle` moves on its own.
    """
    items = list(defs.items())[:1500]
    below = []
    beside = []
    for a, da in items:
        down = [da["y"] - db["y"] for b, db in items
                if b != a and abs(da["x"] - db["x"]) < 1.0 and db["y"] < da["y"]]
        side = [abs(da["x"] - db["x"]) for b, db in items
                if b != a and abs(da["y"] - db["y"]) < 0.5 and db["x"] != da["x"]]
        if down:
            below.append((min(down), tall[a]))
        if side:
            beside.append(min(side))
    scale_x, scale_y = _SCALE_X, _SCALE_Y
    if below:
        below.sort()
        dy, height = below[len(below) // 2]
        if dy > 1e-6:
            scale_y = (height + _GAP_Y) / dy
    if beside:
        beside.sort()
        dx = beside[len(beside) // 2]
        if dx > 1e-6:
            scale_x = (_BOX_WIDTH + _GAP_X) / dx
    return (max(_SCALE_X, min(scale_x, _SCALE_X * _MOST_SCALE)),
            max(_SCALE_Y, min(scale_y, _SCALE_Y * _MOST_SCALE)))


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
    if head.startswith((b"070707", b"070701", b"070702")):
        # A cpio archive, and a scene's first member is its `.start`.
        return b".start\0" in head[:200]
    return head.startswith((b"HouNC\x1a", b"HouLC\x1a")) or (
        head.startswith(b"MIME-Version") and b"HOUDINIMIMEBOUNDARY" in head[:400])


def _cpio(data: bytes) -> Dict[str, bytes]:
    """A cpio archive's members: the "portable" form with octal fields
    (`070707`), which is how a commercial `.hip` has been written since the
    beginning, and the newer one with hex fields (`070701`), in case."""
    out: Dict[str, bytes] = {}
    at = 0
    end = len(data)
    while at + 6 <= end:
        magic = data[at:at + 6]
        try:
            if magic == b"070707":
                namesize = int(data[at + 59:at + 65], 8)
                filesize = int(data[at + 65:at + 76], 8)
                at += 76
                name = data[at:at + namesize - 1].decode("utf-8", "replace")
                at += namesize
                body_at = at
                at += filesize
            elif magic in (b"070701", b"070702"):
                filesize = int(data[at + 54:at + 62], 16)
                namesize = int(data[at + 94:at + 102], 16)
                at += 110
                name = data[at:at + namesize - 1].decode("utf-8", "replace")
                at = (at + namesize + 3) & ~3
                body_at = at
                at = (at + filesize + 3) & ~3
            else:
                break
        except ValueError:
            break
        if name == "TRAILER!!!":
            break
        out[name] = data[body_at:body_at + filesize]
    return out


def parts(data: bytes) -> Dict[str, bytes]:
    """The archive's texts, by name, in file order."""
    if data.startswith((b"070707", b"070701", b"070702")):
        return _cpio(data)
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
        # `0 grid1 0` — the input, the node, its output; scenes from before
        # about Houdini 9 leave the output out.
        if len(words) >= 2 and words[0].isdigit():
            output = int(words[2]) if len(words) >= 3 and words[2].isdigit() else 0
            wires.append((int(words[0]), words[1], output))
    # The first number on a named line is the connector's own id
    # (`connectornextid` counts them), not which input it is: the input is
    # the line's place in the block. Read as ids, one input became three pins.
    named_in = {}
    for line in _block(text, "inputsNamed3"):
        found = re.match(r'\s*\d+\s+.*"([^"]*)"\s*$', line)
        if found:
            named_in[len(named_in)] = found.group(1)
    named_out = {}
    for line in _block(text, "outputsNamed3"):
        found = re.match(r'\s*\d+\s+"([^"]*)"', line)
        if found:
            named_out[len(named_out)] = found.group(1)
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


#: Parameters that are the parameter pane's furniture, not the node's
#: settings: the tabs and folders it is laid out in.
_FURNITURE = re.compile(r"(switcher|^folder|_folder|^fd_|^sepparm|^label\d*$|^stdswitcher|^parmop_)", re.I)


def _parameters(text: str, most: int) -> List[dict]:
    fields = []
    for name, value in _PARM.findall(text):
        if _FURNITURE.search(name):
            continue
        value = re.sub(r"\s+", " ", value.replace('"', "")).strip()
        if not value:
            continue
        if len(value) > 24:
            value = value[:23] + "…"
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
        fields_of = {p: _parameters(texts.get(p + ".parm", b"").decode("utf-8", "replace"), 3)
                     for p in members}
        tall = {p: _box_height(defs[p], fields_of[p]) for p in members}
        if _piled(defs):
            _layer(defs, network)
        # Houdini flows down; the host puts an input on a box's left and its
        # output on the right. Drawn as Houdini has it, every wire left a box
        # on one side and came back round to the next one's other side,
        # across both. Turned a quarter, a chain runs left to right, straight
        # from one node's output into the next one's input: what was below
        # is to the right, what was to the left is above.
        for d in defs.values():
            d["x"], d["y"] = -d["y"], -d["x"]
        xs = [d["x"] for d in defs.values()]
        ys = [d["y"] for d in defs.values()]
        left, low_y, high_y = min(xs), min(ys), max(ys)
        scale_x, scale_y = _spacing(defs, tall)
        height = (high_y - low_y) * scale_y + max(tall.values(), default=80)
        width = (max(xs) - left) * scale_x + _BOX_WIDTH
        frame_top = top
        first = len(out_nodes)
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
                "x": (d["x"] - left) * scale_x + _PAD,
                "y": frame_top + (high_y - d["y"]) * scale_y + _PAD,
                "group": network,
                # No words on a connector, as Houdini draws none: its names
                # are `input1`, `output1`, and printed they ran into the
                # fields beside them. A space, because an empty label would
                # be drawn as the pin's id instead.
                "inputs": [{"id": "in%d" % k, "label": " "} for k in range(count_in)],
                "outputs": [{"id": "out%d" % k, "label": " "} for k in range(count_out)],
            }
            if badges:
                node["badges"] = badges
            fields = list(fields_of[path])
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
        placed = out_nodes[first:]
        _settle(placed, {n["id"]: tall.get(n["id"], _BOX_TITLE + _BOX_ROW) for n in placed})
        if placed:
            # The frame holds what was settled, however far a box was pushed.
            height = max(height, max(n["y"] + tall.get(n["id"], 80) for n in placed) - frame_top - _PAD)
            width = max(width, max(n["x"] for n in placed) + _BOX_WIDTH - _PAD)
        groups.append({
            "id": network,
            "title": "/" + network,
            "x": 0.0,
            "y": frame_top,
            "width": width + 2 * _PAD,
            "height": height + 2 * _PAD,
        })
        top = frame_top + height + 2 * _PAD + _GAP

    links.extend(_references(texts, {n["id"] for n in out_nodes}, links))

    note = []
    if not out_nodes:
        note.append({"text": "This scene holds no networks with nodes in them.",
                     "x": 0, "y": 0, "width": 360, "height": 60})
    return {"nodes": out_nodes, "links": links, "groups": groups, "notes": note,
            "layout": "given", "direction": "lr"}, dropped


#: A path to a node written into a parameter: `/obj/geo1/null1`, inside an
#: expression or not.
_PATH = re.compile(r'(?<![\w/])/(?:obj|stage|mat|out|ch|img|shop|tasks|vex)(?:/[A-Za-z0-9_.\-]+)+')
_MOST_REFERENCES = 6


def _references(texts: Dict[str, bytes], ids: set, wires: List[dict]) -> List[dict]:
    """The links a scene makes by naming nodes in parameters.

    Houdini joins networks this way rather than by wires: a DOP object reads
    its geometry through a path, an Object Merge fetches another network's
    output, a DOP Import points back at the simulation. Drawn as the other
    kind of link, from what is named to what names it, labelled with the
    parameter — so the scene's second set of connections is on the page too.
    """
    out: List[dict] = []
    seen = {(w["from"], w["to"]) for w in wires}
    for name, body in texts.items():
        if not name.endswith(".parm"):
            continue
        reader = name[:-5]
        if reader not in ids:
            continue
        made = 0
        for parameter, value in _PARM.findall(body.decode("utf-8", "replace")):
            for found in _PATH.findall(value):
                target = found.strip("/").rstrip(".")
                # The node, not something inside it: walk up until a node answers.
                while target and target not in ids:
                    target = target.rpartition("/")[0]
                if not target or target == reader or reader.startswith(target + "/"):
                    continue
                key = (target, reader)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"from": target, "to": reader, "role": "flow", "label": parameter})
                made += 1
                if made >= _MOST_REFERENCES:
                    break
            if made >= _MOST_REFERENCES:
                break
    return out


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
