# Blender

Looks inside a `.blend`.

**F3** draws the model: shaded, turnable, framed on itself. **Shift+F3** says
what is in the file instead — objects by kind, every mesh and how heavy it is,
materials, images, collections, rigs, actions and scenes, and, where the
geometry lives in another file, which file that is.

The two are not a strict either/or, which is what makes putting the model first
safe. About two files in ten hold nothing drawable, and on those F3 gives the
table of contents as well — never an error, never an empty canvas.

## Files

| | |
| --- | --- |
| `blendfile.py` | The container: the header, the chain of blocks, the DNA the file carries, and the map from an address to a block. No meaning attached. |
| `catalog.py` | What the blocks mean: the counting behind the report, and finding a named run of mesh data in whichever of three stores holds it. |
| `geometry.py` | Placing, triangulating and shading meshes into world-space triangles. |
| `main.py` | The contributions. |
| `selftest.py` | Runs the reader over a folder of real files. |

## Reading a `.blend`

A `.blend` is a dump of Blender's own memory: a header, a chain of blocks,
`ENDB`. Four facts make it readable.

**The file describes itself.** One block, `DNA1`, holds the C struct definitions
of the exact build that wrote it — type names, field names, sizes. Every field
offset here is computed from that at run time. **Nothing branches on the version
number**, which is why one reader covers 2.91 through 5.1 although `Mesh` differs
in field count and order across them and `MVert` is 20 bytes in one and 16 in
another.

**Pointers are the writing process's old addresses**, and every block records the
address it had. A pointer that does not resolve is normal and is never an error.

**Three stores, one set of names.** Where a mesh keeps its data has changed
twice, and all three shapes are live in one folder of real work:

| | up to 3.6 | 4.0–4.5 | 5.0 and later |
| --- | --- | --- | --- |
| positions | `MVert.co` | vertex layer `position` | attribute `position` |
| face corners | `MLoop.v` | corner layer `.corner_vert` | attribute `.corner_vert` |
| face extents | `MPoly.loopstart` | `*_offset_indices` | `*_offset_indices` |

The names never changed, only where they are kept, so everything is asked for by
name and the store that has it answers.

**Where an object stands** changed too. Up to 3.6 the composed world matrix is in
the file as `obmat`. From 4.0 it is worked out when the file opens and is not
saved, so it is built here from position, rotation in whichever of seven modes
the object uses, scale, the delta transforms, and the parent chain.

## Files that hold no geometry

**A `.blend` with no drawable mesh is an ordinary file, not a failure**, and about
two in ten of a real collection are one — a rig, or a file whose meshes are
linked out of another `.blend`. Such a file never produces an error, an empty
canvas or the word "empty". The table of contents is the answer and it says
*why*: how many objects hold no data of their own, and the library files they
name, written exactly as the `.blend` writes them. Asking for the model on such
a file gives the same report rather than an error.

## Traps paid for, in this reader

- **A resolving pointer is not data.** In a linked-library file every mesh
  object's `data` resolves perfectly — to an `ID` placeholder. Check the code of
  the block a pointer lands on, always.
- **An address does not name one block.** Blender frees a buffer and allocates
  another in the same place between two passes of writing, and both go into the
  file under that address. One file's mesh kept its faces at an address shared
  with a UV layer of exactly the same length. Worse, Blender 5 writes every
  mesh's attribute list out of one reused buffer, so *every* mesh records the
  same address. So a pointer is resolved by naming the struct wanted and by
  starting the search from the pointing block's own place in the file — the
  thing pointed at was written after it.
- **A triangle count is a count, not a formula.** `corners - faces` overstates a
  quad mesh by a full triangle a face; the count is `corners - 2 * faces`.
- **Stored is not placed.** One mesh stood in forty places is stored once and
  drawn forty times. The report gives both numbers.
- **Modifiers are not in the file.** Subdivision, mirroring, geometry nodes and
  shape keys are worked out when Blender opens the file. What is drawn is the
  base cage, and both views say so.

## Checking it

```
python3 selftest.py ~/Work/_Models
```

It does not merely look for crashes — a reader that returns nothing does not
crash either. It checks that the triangles the contents count and the triangles
the geometry builds are the same number, which is two paths through different
storage shapes that have no reason to agree unless both are right; that every
coordinate is finite and every normal is a unit vector; and that every index
points at a vertex that exists.

Measured over the user's own collection: **170 files, 170 read, 0 problems**,
Blender 2.91 / 3.1 / 3.3–3.6 / 4.0–4.5 / 5.1.

## What is not here

Modifiers, textures, node-based shading, lighting, editing, and following a link
into another `.blend` to fetch the geometry it holds — the library is named, not
opened.

Material colour is deliberately absent rather than wrong: `Material.r/g/b` is
right there and is the default 0.80 grey on very nearly every material in real
files, because the real colour lives in a node tree. Useful colour means walking
`bNodeTree` to the Principled BSDF, and until that is written the host's own
default is the honest answer.

**zstd-compressed files cannot be read.** Blender offers zstd and it is the
default where compression is asked for at all; 13 of the 170 files measured use
it. gzip is free in the standard library and works. zstd arrived in the standard
library in Python 3.14 and the runtime staged for plugins is 3.12, so such a file
is named as what it is rather than failing obscurely.
