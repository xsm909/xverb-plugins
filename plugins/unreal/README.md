# Unreal assets

Unreal Engine assets and maps, `.uasset` and `.umap`, shown the way the
Content Browser shows them — without the engine. F3 gives one page:

- **the thumbnail** the editor saved for the asset, as it saved it;
- **the class** — Texture2D, StaticMesh, Blueprint, World — and **the engine
  that saved it**, with the version it is marked as working in;
- **the file it was imported from**, where the asset says;
- **what the Content Browser shows on hover** — a texture's size and format,
  a mesh's triangles, vertices and LODs, an animation's frames and rate, a
  blueprint's parent class, a sound's length and channels;
- **the other assets it uses**.

The film strip in a full-screen view shows the thumbnails too, so a folder of
assets is walked like a folder of pictures.

## How it reads

Only the header, never an object, and only as much of the file as the
header's tables reach: the first megabyte, then on to the furthest table if
it starts past that. Every table a package keeps near its start — the names,
the imports, the thumbnails, the asset registry — was within the first
quarter of the file and the first 3 MB in every package measured.

The header is a chain of version tests, each field read under the version the
engine writes it from; the numbers are `ObjectVersion.h`'s. Two things the
declarations do not say and the files do: from file version -9 the saved hash
and the header's size come first, and `MetaDataOffset` is written after the
imports, not where it is declared.

From UE 4.0 to 5.7. A cooked package, whose versions are left out, is read as
the newest. Unreal Engine 3, big-endian console packages and packages
compressed whole say what they are.

## Checking it

    python3 selftest.py                 # the traps, then an Unreal install if one is found
    python3 selftest.py <folder…>       # every package under these folders

Measured on UE 5.7's own content: **15 111 packages, file versions -2 to -9,
every one read with its tables**, in six seconds.
