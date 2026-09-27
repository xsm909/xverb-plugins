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

"""The package reader, checked: `python3 selftest.py [folder…]`.

A made-up header cannot test a reader of a format whose order is the whole
difficulty — it would only agree with itself. So the traps are checked on
bytes made here, and the order on **real packages**: every `.uasset` and
`.umap` under the folders given (by default an Unreal install, if one is
found) must read, and its imports must be names, not noise. That is the test
that found `MetaDataOffset` written after the imports rather than where it is
declared, and the saved hash that moved to the front at file version -9.
"""

from __future__ import annotations

import collections
import glob
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uasset  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        FAILED.append(name)


def refused(data: bytes, words: str) -> bool:
    try:
        uasset.Package(data)
    except uasset.UassetError as failure:
        return words in str(failure)
    return False


check("not a package", refused(b"PK\x03\x04" + bytes(60), "not an Unreal package"))
check("a console's big-endian package says so",
      refused(struct.pack("<I", uasset.TAG_SWAPPED) + bytes(60), "big-endian"))
check("Unreal Engine 3 says so",
      refused(struct.pack("<Ii", uasset.TAG, 864) + bytes(60), "Unreal Engine 3"))
check("a header cut short says so, not a crash",
      refused(struct.pack("<Iiiiii", uasset.TAG, -7, 864, 522, 0, 0), "ends where"))

text = "Материал"
utf16 = struct.pack("<i", -(len(text) + 1)) + text.encode("utf-16-le") + b"\0\0"
check("an FString in UTF-16", uasset._Reader(utf16).string() == text)
check("an FString in Latin-1", uasset._Reader(struct.pack("<i", 4) + b"abc\0").string() == "abc")

folders = sys.argv[1:]
if not folders:
    for guess in glob.glob(os.path.expanduser("~/Games/UE_*")) + glob.glob("C:/Program Files/Epic Games/UE_*"):
        folders.append(guess)
        break

for folder in folders:
    files = glob.glob(os.path.join(folder, "**", "*.uasset"), recursive=True) + \
        glob.glob(os.path.join(folder, "**", "*.umap"), recursive=True)
    started = time.time()
    tally = collections.Counter()
    first_bad = {}
    for path in files:
        with open(path, "rb") as handle:
            data = handle.read()
        try:
            package = uasset.Package(data)
        except uasset.UassetError as failure:
            tally["refused"] += 1
            first_bad.setdefault(str(failure), path)
            continue
        imports = package.imports()
        package.registry()
        if package.thumbnails():
            tally["thumbnail"] += 1
        if any(c == "?" or n == "?" for c, n, _ in imports):
            package.problems.append("imports are noise")
        if package.problems:
            tally["problem"] += 1
            first_bad.setdefault(", ".join(package.problems), path)
        tally["versions %d" % package.legacy] += 1
    print("%s: %d package(s) in %.1fs — %s" % (folder, len(files), time.time() - started,
                                              dict(sorted(tally.items()))))
    for why, path in list(first_bad.items())[:5]:
        print("    %s: %s" % (why, path))
    check("every package under %s reads, tables and all" % folder,
          tally["refused"] == 0 and tally["problem"] == 0, dict(tally))

print("\n%d failure(s)" % len(FAILED) if FAILED else "\nall good")
sys.exit(1 if FAILED else 0)
