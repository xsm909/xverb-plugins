# Node graphs: changes

## 0.9.0 — 2026-09-29

- Commercial .hip scenes open: they are cpio archives, which this did not read, so every one of nineteen tried had failed. Scenes from before Houdini 9, whose inputs name no output, get their wires.
- A Houdini node has the connectors it has: one input was drawn as three pins, because a connector's id was read as its place.
- A network is drawn turned a quarter: Houdini flows down, the view's boxes take their inputs on the left and give outputs on the right, so a chain now runs left to right with every wire straight from one node into the next, instead of looping round both boxes.
- No box lands on another: each network is spaced by how far apart its nodes usually are, one piled up in a single place is laid out by its wires in rows of eight, and what still touches is moved.
- Links made by naming a node in a parameter — a DOP object's SOP path, an Object Merge, a DOP Import — are drawn too, as the other kind of link, named by the parameter.
- n8n: positions are pulled apart to fit the view's boxes, which are wider than n8n's own, so the nodes of a chain no longer touch and hide the wire between them; the sticky notes behind them grow with them. Connections keyed by a node's id are followed too, and a workflow that has no connections left says so on the canvas.
- The face shows three parameters that say something, not the tabs of the parameter pane, and the connectors carry no words to run into them.

## 0.8.0 — 2026-09-27

- Houdini scenes — .hip, .hipnc, .hiplc — drawn as their networks: a frame per network, each node where it was put, with its type, its first parameters and its flags; wires followed through dots.

## 0.7.1 — 2026-09-06

- Renamed with the application: the id is org.xverb, the SDK is xverb.
- Every viewer says what kind of thing it gives back.

## 0.7.0 — 2026-08-16

- A page in the store for it.
- A Godot scene, which is two graphs at once.

## 0.6.0 — 2026-08-16

- The workflow inside the picture.

## 0.5.0 — 2026-08-16

- It knows its own files when it sees them.

## 0.4.2 — 2026-08-16

- The claim that took files it had no business taking.

## 0.4.1 — 2026-08-16

- A Node-RED flow drawn at the size these boxes actually are.

## 0.4.0 — 2026-08-15

- Node-RED, where a tab is a canvas of its own.

## 0.3.0 — 2026-08-15

- The API export, which has no coordinates.

## 0.2.0 — 2026-08-15

- n8n, and what real files taught.

## 0.1.0 — 2026-08-15

- The tool, and its first reader.
