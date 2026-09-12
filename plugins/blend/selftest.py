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

"""Runs the reader over a folder of real `.blend` files.

    python3 selftest.py ~/Work/_Models

**What it is looking for is disagreement, not absence of a crash.** A reader
that returns nothing at all does not crash either, and neither does one that
reports half a mesh. So the checks are against things that have to be true:

* the file parses, and its block chain reaches `ENDB`;
* the triangles the table of contents counts and the triangles the geometry
  step actually builds are the same number — two paths through two different
  storage shapes, which have no reason to agree unless both are right;
* every coordinate is finite, and every normal is a unit vector;
* every index points at a vertex that exists;
* a file with no geometry is reported as such and not as an error.
"""

from __future__ import annotations

import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import blendfile  # noqa: E402
import catalog  # noqa: E402
import geometry  # noqa: E402


def find(root: str):
    if os.path.isfile(root):
        return [root]
    out = []
    for here, _folders, files in os.walk(root):
        for name in sorted(files):
            if name.lower().endswith(".blend"):
                out.append(os.path.join(here, name))
    return sorted(out)


def check(path: str) -> dict:
    """One file, read both ways, and everything that has to agree checked."""
    answer = {"path": path, "problems": [], "skipped": ""}
    data = open(path, "rb").read()
    answer["size"] = len(data)

    start = time.perf_counter()
    try:
        opened = blendfile.BlendFile(data)
    except blendfile.BlendError as refusal:
        # A refusal with a sentence in it is a result, not a failure: a 5.x
        # header and a zstd-compressed file are both known and both said out
        # loud. What must never happen is a traceback.
        answer["skipped"] = str(refusal)
        return answer
    answer["parse"] = (time.perf_counter() - start) * 1000
    answer["version"] = blendfile.version_text(opened.version)
    answer["blocks"] = len(opened.blocks)
    if opened.truncated:
        answer["problems"].append("the block chain does not reach ENDB")

    facts = catalog.summarise(opened)
    # As placed, not as stored: the geometry step walks objects, so a mesh
    # stood in forty places is forty meshes' worth of triangles to it. Compared
    # against the stored total this check would fire on every file that
    # instances anything, which is most of them.
    answer["counted"] = facts["placed"]["triangles"]
    answer["meshes"] = len([m for m in facts["meshes"] if m["triangles"] > 0])
    answer["linked"] = facts["linked"]["objects"]

    start = time.perf_counter()
    parts, note = geometry.meshes(opened)
    answer["build"] = (time.perf_counter() - start) * 1000
    answer["built"] = sum(len(p["indices"]) // 3 for p in parts)
    answer["vertices"] = sum(len(p["positions"]) // 3 for p in parts)
    answer["painted"] = sum(1 for p in parts if p.get("picture"))
    answer["packed"] = sum(1 for p in parts
                           if (p.get("picture") or {}).get("bytes"))

    # The check worth having. A mesh is counted from `totloop` and `totpoly`
    # and built from the corner and offset arrays, and on a 4.x file those are
    # not even the same storage. Agreement is evidence; a difference is a bug
    # in one of them and says which file to look at.
    if not note["droppedMeshes"] and answer["built"] != answer["counted"]:
        answer["problems"].append(
            "the contents count %s triangles and the geometry builds %s"
            % (answer["counted"], answer["built"]))

    if facts["placed"]["triangles"] and not parts and not note["droppedMeshes"]:
        # A mesh datablock no object stands on is not in the scene and is
        # rightly not built, and one past the budget is refused on purpose.
        # One that is stood on, fits, and still was not built is a reader that
        # failed quietly, which is the failure worth catching.
        answer["problems"].append(
            "%d object(s) stand on %s triangles and none was built"
            % (facts["placed"]["objects"], facts["placed"]["triangles"]))

    for part in parts:
        _check_part(part, answer["problems"])
    return answer


def _check_part(part: dict, problems: list) -> None:
    name = part["name"]
    positions = part["positions"]
    normals = part["normals"]
    indices = part["indices"]

    if len(positions) != len(normals):
        problems.append("%s: %d position floats against %d normal floats"
                        % (name, len(positions), len(normals)))
        return
    count = len(positions) // 3
    if len(indices) % 3:
        problems.append("%s: %d indices is not whole triangles"
                        % (name, len(indices)))

    # A UV that is short by one vertex paints the whole model a hand's width
    # off, which looks like a bad unwrapping rather than a bad reader.
    uvs = part.get("uvs") or []
    if uvs:
        if len(uvs) != count * 2:
            problems.append("%s: %d uv floats against %d vertices"
                            % (name, len(uvs), count))
        elif not all(math.isfinite(value) for value in uvs):
            problems.append("%s: a uv coordinate is not a finite number" % name)

    for value in positions:
        if not math.isfinite(value):
            problems.append("%s: a coordinate is not a finite number" % name)
            break

    # A normal that is not a unit vector is a lighting bug that looks like a
    # material bug, and is invisible until somebody wonders why one mesh is
    # darker than the rest.
    worst = 0.0
    for at in range(0, len(normals), 3):
        length = math.sqrt(normals[at] ** 2 + normals[at + 1] ** 2
                           + normals[at + 2] ** 2)
        worst = max(worst, abs(length - 1.0))
        if worst > 1e-3:
            break
    if worst > 1e-3:
        problems.append("%s: a normal is %.4f long, not 1" % (name, 1 + worst))

    for value in indices:
        if not 0 <= value < count:
            problems.append("%s: an index points past the %d vertices there are"
                            % (name, count))
            break


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__.strip())
        return 2

    files = []
    for root in argv[1:]:
        files.extend(find(root))
    if not files:
        print("No .blend files under %s." % ", ".join(argv[1:]))
        return 2

    print("%-38s %-5s %7s %8s %8s %7s %7s" % (
        "file", "ver", "meshes", "tris", "verts", "parse", "build"))
    bad = 0
    skipped = 0
    painted = 0
    packed = 0
    parse_total = build_total = 0.0
    versions = {}

    for path in files:
        try:
            answer = check(path)
        except Exception as failure:  # noqa: BLE001 - that is what is being looked for
            bad += 1
            print("%-38s CRASHED  %s: %s"
                  % (os.path.basename(path)[:38], type(failure).__name__,
                     failure))
            continue

        if answer["skipped"]:
            skipped += 1
            print("%-38s not read: %s"
                  % (os.path.basename(path)[:38], answer["skipped"]))
            continue

        painted += answer["painted"]
        packed += answer["packed"]
        parse_total += answer["parse"]
        build_total += answer["build"]
        versions[answer["version"]] = versions.get(answer["version"], 0) + 1
        print("%-38s %-5s %7d %8d %8d %6.0f %6.0f%s%s" % (
            os.path.basename(path)[:38], answer["version"], answer["meshes"],
            answer["built"], answer["vertices"], answer["parse"],
            answer["build"],
            ("  %d linked" % answer["linked"]) if answer["linked"] else "",
            ("  %d painted" % answer["painted"]) if answer["painted"] else ""))
        for problem in answer["problems"]:
            bad += 1
            print("    PROBLEM: %s" % problem)

    read = len(files) - skipped
    print("\n%d file(s), %d read, %d not read, %d problem(s)."
          % (len(files), read, skipped, bad))
    if painted:
        print("%d mesh part(s) carry a picture, %d of them packed into the file."
              % (painted, packed))
    if versions:
        print("Blender %s." % ", ".join(
            "%s x%d" % (v, n) for v, n in sorted(versions.items())))
    if read:
        print("%.0f ms parsing, %.0f ms building, %.0f ms a file on average."
              % (parse_total, build_total,
                 (parse_total + build_total) / read))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
