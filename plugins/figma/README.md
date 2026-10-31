# Figma

Figma designs (`.fig`) and FigJam boards (`.jam`), opened without Figma and
without an account. A `.fig` is the document rather than a picture of it, so
the page is **drawn from its own shapes** — and stays sharp however far into
it you go, which no exported PNG does.

F3 offers three, in this order:

- **Design** — a page of the file, drawn as paths. The host does the
  magnification; nothing here is rasterised.
- **Preview** — the picture Figma rendered when the file was saved. Small,
  and the only view with the text and the photographs in it.
- **What is in the file** — the pages, the frames and their sizes, **the
  fonts the design will ask the machine for**, what its text says, the
  components it is built from, and the pictures it carries.

The preview is the film strip's picture too, so a folder of designs is walked
like a folder of photographs, and the information panel says what the file is
without opening it.

## What is not drawn, and why

**Text.** A `.fig` names the fonts and does not carry them, and it stores no
outlines for the letters — Figma asks the machine, or its own servers, for the
font at the moment it draws. So a text layer's box, its background and its
border are drawn and its letters are not. The design view says how many there
were, what they say is under *What is in the file*, and the preview shows them
as Figma drew them.

**Photographs.** They are in the file as PNG and JPEG, under `images/`, and a
photograph is not a path. Counted, and in the preview.

**Component overrides.** An instance is drawn as its component stands: a
colour changed or a layer hidden inside one instance is not followed yet.

Effects — shadows, blurs — and a frame's clipping are not drawn either, and
Figma's two exotic gradients, the angular and the diamond, are drawn as
straight ones.

## How a .fig is put together

Three formats, one inside the next:

1. a **zip**, holding `canvas.fig`, `thumbnail.png`, `meta.json` and the
   pictures under `images/`, each named by the hash of its own contents. A
   file saved before Figma introduced the zip is the next layer on its own.
2. **`fig-kiwi`** — eight bytes naming the format, a version, then
   length-prefixed chunks: the schema, then the document. A FigJam board says
   `fig-jam.` instead and is otherwise the same.
3. a **Kiwi** message — Evan Wallace's schema format, with the schema the file
   was written against stored in front of it, which is why a file saved by a
   version of Figma nobody has seen still reads: the field names come out of
   the file, not out of this plugin.

The chunks were deflate; the document is **zstd** in anything recent. Python
3.12, which the application ships, has no zstd — it arrives in 3.14 — and a
plugin may not install anything, so `zstd.py` here is a decompressor written
for the purpose. Where the standard library does have one, it is used instead.

The document is a flat list of nodes, each with its own id and its parent's,
ordered among its siblings by a string that sorts between its neighbours. The
tree is rebuilt from that. Geometry arrives already worked out — a rounded
rectangle is a path, and so is the outline of a boolean of three circles — so
what this plugin does is compose the transforms, resolve the paints, and hand
the host paths in page coordinates.

Read so far: `fig-kiwi` 101 and 106, and `fig-jam.` 101.

## Checking it

    python3 selftest.py                 # the format, on fixtures and on paper
    python3 selftest.py <folder>        # and every .fig and .jam under there

The zstd fixtures under `fixtures/` were compressed by the reference `zstd`
and are checked against the digest of what went into it. On Python 3.14, where
the standard library has zstd, every fixture is decoded both ways and the two
are compared.
