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

"""n8n, as the editor exports it.

    {"nodes": [{"id": "…", "name": "Loop Over Items", "type": "n8n-nodes-base…",
                "position": [x, y], "parameters": {…}}],
     "connections": {"Limit": {"main": [[{"node": "Loop Over Items",
                                          "type": "main", "index": 0}]]}}}

Three things about it decide most of the code:

- **Connections are keyed by the node's *name*, not by its id.** So the reader
  works in names and hands the host names as ids. Rename two nodes to the same
  thing in n8n and the file is already ambiguous; nothing here can mend that.
- **The output index is the position in the outer list**, and the *type* of the
  connection is its key: `main` is the flow, and everything beginning `ai_` is a
  model, a tool or a parser hanging off an agent. They are drawn as different
  pins, because in the editor they leave the node from different places and mean
  different things.
- **A sticky note is a node.** It has no wires and it carries what somebody wrote
  about the workflow, which is often the only documentation there is — so it
  becomes a note on the canvas rather than a box in the graph.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

#: What a node's type says about its part in the flow. Matched on the last
#: segment of `n8n-nodes-base.something`, longest first.
ROLES: List[Tuple[str, str]] = [
    ("Trigger", "event"),
    ("webhook", "event"),
    ("stickyNote", "note"),
    ("agent", "flow"),
    ("lmChat", "variable"),
    ("outputParser", "pure"),
    ("tool", "input"),
    ("set", "pure"),
    ("code", "pure"),
    ("if", "flow"),
    ("switch", "flow"),
    ("merge", "flow"),
    ("splitInBatches", "flow"),
    ("wait", "flow"),
    ("notion", "output"),
    ("gmail", "output"),
    ("airtable", "output"),
    ("httpRequest", "input"),
]

#: Parameters worth writing on the face of a node. Everything else in n8n's
#: parameter tree is nesting, credentials and editor state — and a box covered
#: in `{"__rl": true}` says less than a box with nothing on it.
INTERESTING = (
    "url",
    "method",
    "operation",
    "resource",
    "mode",
    "text",
    "prompt",
    "query",
    "amount",
    "unit",
    "maxItems",
    "fieldToSplitOut",
    "jsCode",
    "toolDescription",
    "description",
)


def looks_like(document: Any) -> bool:
    """Whether this is an n8n workflow export."""
    if not isinstance(document, dict):
        return False
    nodes = document.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    first = nodes[0]
    return (
        isinstance(first, dict)
        and "name" in first
        and "type" in first
        and isinstance(document.get("connections"), dict)
    )


def signature(head: str) -> bool:
    """Whether the first pages of a file look like an n8n export.

    **Not `connections`**, which is the thing `looks_like` asks a whole document
    for: n8n writes its nodes first and its wires last, so in a real export the
    word does not appear until tens of kilobytes in — the self-test caught that
    on his own competitor-research workflow. What *is* at the front is the node
    shape, and `typeVersion` beside a `position` is n8n's alone: LiteGraph
    writes `pos`, Node-RED writes bare `x` and `y`, and neither versions its
    node types.
    """
    if '"typeVersion"' not in head:
        return False
    return '"position"' in head or '"connections"' in head

def role_of(kind: str) -> str:
    for needle, role in ROLES:
        if needle.lower() in kind.lower():
            return role
    return "normal"


def _short(kind: str) -> str:
    """`n8n-nodes-base.splitInBatches` → `splitInBatches`."""
    return kind.rsplit(".", 1)[-1]


def _fields(parameters: Any) -> List[dict]:
    if not isinstance(parameters, dict):
        return []
    fields = []
    for key in INTERESTING:
        if key not in parameters:
            continue
        value = parameters[key]
        if isinstance(value, (dict, list)):
            continue
        text = str(value).replace("\n", " ").strip()
        if not text:
            continue
        if len(text) > 120:
            text = text[:119] + "…"
        fields.append({"label": key, "value": text})
    return fields


def read(document: dict, cap: int) -> Tuple[dict, int]:
    """The workflow as the host's own document, and how many nodes were dropped."""
    raw = [node for node in document.get("nodes", []) if isinstance(node, dict)]

    notes = []
    boxes = []
    for entry in raw:
        if _short(str(entry.get("type") or "")) == "stickyNote":
            notes.append(entry)
        else:
            boxes.append(entry)

    dropped = max(0, len(boxes) - cap)
    boxes = boxes[:cap]

    known = {str(entry.get("name")) for entry in boxes}
    # n8n keys its connections by name; a file that keys them by a node's id
    # is read through the id as well.
    by_id = {str(entry.get("id")): str(entry.get("name")) for entry in boxes if entry.get("id")}
    connections = document.get("connections") or {}
    lost = 0

    # Which pins each node needs. Worked out from the wires rather than declared
    # anywhere in the file: n8n says what is joined, never what the sockets are.
    outputs: Dict[str, List[str]] = {}
    inputs: Dict[str, List[str]] = {}
    links: List[dict] = []

    for source, kinds in connections.items():
        source = by_id.get(source, source) if source not in known else source
        if source not in known or not isinstance(kinds, dict):
            continue
        for kind, slots in kinds.items():
            if not isinstance(slots, list):
                continue
            for index, wires in enumerate(slots):
                if not isinstance(wires, list):
                    continue
                pin = kind if len(slots) == 1 else "%s %d" % (kind, index)
                for wire in wires:
                    if not isinstance(wire, dict):
                        continue
                    target = str(wire.get("node"))
                    target = by_id.get(target, target) if target not in known else target
                    if target not in known:
                        lost += 1
                        continue
                    into = str(wire.get("type") or "main")
                    outputs.setdefault(source, [])
                    inputs.setdefault(target, [])
                    if pin not in outputs[source]:
                        outputs[source].append(pin)
                    if into not in inputs[target]:
                        inputs[target].append(into)
                    links.append(
                        {
                            "from": source,
                            "to": target,
                            "fromPin": pin,
                            "toPin": into,
                            # `main` is the order things run in; the `ai_` ones
                            # are what an agent is *made of*, and they read
                            # differently because they are different.
                            "role": "flow" if kind == "main" else "data",
                            "type": kind,
                        }
                    )

    nodes = []
    for entry in boxes:
        name = str(entry.get("name"))
        kind = str(entry.get("type") or "")
        position = entry.get("position") or [0, 0]
        node: dict = {
            "id": name,
            "title": name,
            "subtitle": _short(kind),
            "role": role_of(kind),
            "x": float(position[0] if len(position) > 0 else 0),
            "y": float(position[1] if len(position) > 1 else 0),
        }
        if entry.get("disabled"):
            node["badges"] = ["muted"]
        if inputs.get(name):
            node["inputs"] = [{"id": pin, "label": _pin_label(pin)} for pin in inputs[name]]
        if outputs.get(name):
            node["outputs"] = [{"id": pin, "label": _pin_label(pin)} for pin in outputs[name]]
        fields = _fields(entry.get("parameters"))
        if fields:
            node["fields"] = fields
        nodes.append(node)

    drawn_notes = [_note(entry) for entry in notes]
    _spread(nodes, drawn_notes)
    if len(nodes) > 1 and not links:
        # A workflow of boxes and no wires is not what n8n saves: somebody
        # took them out, or pointed them at nodes that are not in the file.
        # Said on the canvas, above the boxes, rather than left to look like
        # a reader that failed.
        top = min((n["y"] for n in nodes), default=0.0)
        left = min((n["x"] for n in nodes), default=0.0)
        said = ("This workflow's connections point at %d node(s) that are not in the file, "
                "so none can be drawn." % lost) if lost else \
            "This workflow keeps no connections: the file was saved without them."
        drawn_notes.append({"text": said, "x": left, "y": top - 140, "width": 520, "height": 90})
    return (
        {
            "nodes": nodes,
            "links": links,
            "groups": [],
            "notes": drawn_notes,
        },
        dropped,
    )


#: A box as the host draws it at the default size — wider than n8n's own
#: square icon of a node, which its positions are spaced for.
_BOX_WIDTH = 180.0
_ROW = 19.0
_TITLE = 25.0
_MOST = 2.5


def _pin_label(pin: str) -> str:
    """`main` is nearly every pin n8n has and says nothing; printed, it ran
    into the fields beside it. The AI pins — `ai_languageModel`, `ai_tool` —
    say what plugs in there, and keep their words. A space, because an empty
    label is drawn as the pin's id."""
    return " " if pin.split(" ")[0] == "main" else pin


def _spread(nodes: List[dict], notes: List[dict]) -> None:
    """n8n's positions, pulled apart to fit the host's boxes.

    n8n draws a node as a square about a hundred points wide and spaces its
    chains for that; the host's box is 180 wide with its fields under the
    title, so side by side they touched and the wire between them vanished.
    Everything is scaled by what the closer neighbours need — nodes and the
    sticky notes behind them alike, so a note still frames the nodes it was
    drawn round.
    """
    if len(nodes) < 2:
        return
    beside, below, tall = [], [], []
    for a in nodes:
        rows = max(len(a.get("inputs", [])) + len(a.get("outputs", [])),
                   len(a.get("fields", [])), 1)
        tall.append(_TITLE + rows * _ROW + 12)
        side = [abs(b["x"] - a["x"]) for b in nodes
                if b is not a and abs(b["y"] - a["y"]) < 60 and b["x"] != a["x"]]
        down = [abs(b["y"] - a["y"]) for b in nodes
                if b is not a and abs(b["x"] - a["x"]) < 100 and b["y"] != a["y"]]
        if side:
            beside.append(min(side))
        if down:
            below.append(min(down))
    kx = ky = 1.0
    if beside:
        beside.sort()
        # The tighter quarter, not the middle: a chain drawn close together
        # is exactly where the wires vanish.
        kx = (_BOX_WIDTH + 60) / max(1.0, beside[len(beside) // 4])
    if below:
        below.sort()
        tall.sort()
        ky = (tall[len(tall) // 2] + 40) / max(1.0, below[len(below) // 4])
    kx = min(max(kx, 1.0), _MOST)
    ky = min(max(ky, 1.0), _MOST)
    # What each note is drawn round, asked in n8n's own coordinates, where a
    # node is a square of about a hundred points.
    covers = []
    for note in notes:
        covers.append([n for n in nodes
                       if note["x"] <= n["x"] + 50 <= note["x"] + note["width"]
                       and note["y"] <= n["y"] + 50 <= note["y"] + note["height"]])
    for node in nodes:
        # About the centre of n8n's square, not its corner: the notes were
        # drawn round the square, and a box of another size placed by its
        # corner slid out from under the note that points at it.
        rows = max(len(node.get("inputs", [])) + len(node.get("outputs", [])),
                   len(node.get("fields", [])), 1)
        width = node.get("width") or _BOX_WIDTH
        height = _TITLE + rows * _ROW + 12
        node["x"] = (node["x"] + 50) * kx - width / 2
        node["y"] = (node["y"] + 50) * ky - height / 2
    for note, inside in zip(notes, covers):
        note["x"] *= kx
        note["y"] *= ky
        note["width"] *= kx
        note["height"] *= ky
        # Scaled, a note no longer fits the boxes, which are not n8n's size:
        # it is grown to hold every node it held, with room kept round them.
        for n in inside:
            rows = max(len(n.get("inputs", [])) + len(n.get("outputs", [])),
                       len(n.get("fields", [])), 1)
            right = n["x"] + (n.get("width") or _BOX_WIDTH) + 20
            bottom = n["y"] + _TITLE + rows * _ROW + 12 + 20
            if n["x"] - 20 < note["x"]:
                note["width"] += note["x"] - (n["x"] - 20)
                note["x"] = n["x"] - 20
            if n["y"] - 20 < note["y"]:
                note["height"] += note["y"] - (n["y"] - 20)
                note["y"] = n["y"] - 20
            note["width"] = max(note["width"], right - note["x"])
            note["height"] = max(note["height"], bottom - note["y"])


def _note(entry: dict) -> dict:
    """A sticky note, which in n8n is what documentation looks like."""
    parameters = entry.get("parameters") or {}
    position = entry.get("position") or [0, 0]
    return {
        "text": str(parameters.get("content") or ""),
        "x": float(position[0] if len(position) > 0 else 0),
        "y": float(position[1] if len(position) > 1 else 0),
        "width": float(parameters.get("width") or 240),
        "height": float(parameters.get("height") or 160),
    }
