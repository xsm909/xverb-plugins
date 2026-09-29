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

"""The plugin against a real phone, without the app.

    python3 selftest.py [SDK folder]

The SDK folder is the app's `assets/python`, where `xverb` is. A phone has to
be plugged in, unlocked, with USB set to File transfer.

**It writes to the phone**, and only inside `Download/xverb-mtp-selftest`,
which it makes first and deletes at the end. The sizes are the ones that
break a USB transfer when it is wrong: nothing, one byte, a data container
that fills exactly one packet, one that fills exactly one read, and one of
several reads with a tail.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if len(sys.argv) > 1:
    sys.path.insert(0, sys.argv[1])

import main  # noqa: E402
import usb  # noqa: E402

CHUNK = 256 * 1024


def put(fs, url: str, data: bytes, modified=None) -> None:
    fs.begin_write(url, len(data), modified)
    for start in range(0, max(len(data), 1), CHUNK):
        fs.write(url, data[start:start + CHUNK], "create" if start == 0 else "append")
    fs.close_write(url, True)


def get(fs, url: str) -> bytes:
    out = bytearray()
    while True:
        piece = fs.read(url, len(out), CHUNK)
        out += piece
        if len(piece) < CHUNK:
            return bytes(out)


def check(what: str, ok: bool, failures: list) -> None:
    print("%-44s %s" % (what, "ok" if ok else "FAILED"))
    if not ok:
        failures.append(what)


def run() -> int:
    fs = main.mtp_fs
    failures: list = []
    started = time.time()
    roots = fs.roots()
    usb.lib()
    print("libusb:", usb._lib._name)
    if not roots:
        print("No phone. Plug one in and set USB to File transfer.")
        return 2
    for root in roots:
        print("root:", root.label, "-", root.subtitle)
    base = roots[0].url
    if base.count("/") < 4:
        print("The phone is locked. Unlock it and allow access to its data.")
        return 2

    top = [e.name for e in fs.list(base)]
    check("the top level lists", bool(top), failures)
    folder = base + "Download/xverb-mtp-selftest"
    if fs.stat(folder):
        fs.delete(folder)
    fs.mkdir(folder)
    check("a folder is made", getattr(fs.stat(folder), "kind", "") == "dir", failures)

    cases = {
        "empty.bin": b"",
        "one.txt": b"x",
        "one-packet.bin": os.urandom(512 - 12),
        "one-read.bin": os.urandom(1024 * 1024 - 12),
        "several.bin": os.urandom(7 * 1024 * 1024 + 333),
        "Привет, мир.txt": "кириллица".encode(),
    }
    for name, data in cases.items():
        url = folder + "/" + quote(name)
        put(fs, url, data, 1700000000)
        back = get(fs, url)
        found = fs.stat(url)
        check("%s: %d bytes there and back" % (name, len(data)),
              back == data and found is not None and found.size == len(data), failures)

    url = folder + "/one.txt"
    put(fs, url, b"second")
    check("a file is replaced", get(fs, url) == b"second", failures)

    # A new session: nothing remembered, everything asked of the phone.
    fs.close_all()
    listed = {e.name: e.size for e in fs.list(folder)}
    check("the listing survives a new session",
          listed.get("several.bin") == len(cases["several.bin"])
          and listed.get("one.txt") == 6, failures)

    part = fs.read(folder + "/several.bin", 3 * 1024 * 1024 + 7, 1000)
    start = 3 * 1024 * 1024 + 7
    check("part of a file, from the middle",
          part == cases["several.bin"][start:start + 1000], failures)

    fs.rename(folder + "/one.txt", folder + "/two.txt")
    names = {e.name for e in fs.list(folder)}
    check("a rename", "two.txt" in names and "one.txt" not in names, failures)

    fs.mkdir(folder + "/sub")
    fs.rename(folder + "/two.txt", folder + "/sub/moved.txt")
    check("a move into another folder",
          get(fs, folder + "/sub/moved.txt") == b"second"
          and fs.stat(folder + "/two.txt") is None, failures)

    copied = fs.copy_within(folder + "/several.bin", folder + "/sub/copy.bin")
    if copied:
        same = (hashlib.md5(get(fs, folder + "/sub/copy.bin")).digest()
                == hashlib.md5(cases["several.bin"]).digest())
        check("a copy made by the phone", same, failures)
    else:
        print("%-44s %s" % ("a copy made by the phone", "not offered"))

    fs.delete(folder)
    check("the folder is deleted with what is in it", fs.stat(folder) is None, failures)
    fs.close_all()

    print("%d failed, %.1f s" % (len(failures), time.time() - started))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
