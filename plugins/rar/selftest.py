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

"""The RAR plugin, checked without the application.

Run it with `python3 selftest.py`. Two halves: the library binding on its own,
and then `main.py` driven over the pipe the way the application drives it —
listing, stat, reading in the 256 KB pieces a copy asks for, F3, and the
refusals. The SDK is taken from `XVERB_SDK`, or from an `xverb-dev` checkout
beside this repository.

The archives in `fixtures/` are libarchive's own test archives (BSD licence,
see `native/COPYING.libarchive`): nobody can make a RAR without RARLAB's
program, so the only honest fixtures are ones somebody made with it. The
checksums were taken from `bsdtar -x` of the same files.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.parse import quote

import libarc
import volumes as main_volumes

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
FAILURES = []

SUMS = {
    "rar_compress_normal.rar": {
        "LibarchiveAddingTest.html": "78943e839b05c89a973845de122285a2",
        "testdir/LibarchiveAddingTest.html": "78943e839b05c89a973845de122285a2",
        "testdir/test.txt": "f03f566a28784ddb31d216e6a5056e83",
    },
    "rar_unicode.rar": {
        "表だよ/漢字長いファイル名long-filename-in-漢字.txt": "7a48b3323b0b04bd61a7c10fdcb10120",
        "abcdefghijklmnopqrsテスト.txt": "45226aa24b72ce0ccc4ff73eefe2e26f",
    },
    "rar5_compressed.rar": {"test.bin": "2e01b70f6711145cfd2e52ed8b844060"},
    "rar_multi_lzss_blocks.rar": {
        "multi_lzss_blocks_test.txt": "10f7c5b39564a7cdf87fcade99578b17",
    },
    "rar5_multiarchive_solid.part01.rar": {
        "cebula.txt": "6c752dc783e3baf373af4079a08a5b9b",
        "test.bin": "2e01b70f6711145cfd2e52ed8b844060",
        "test6.bin": "c7333a188fb1d2bf893bd5d7d7764879",
        # Its CRC is 0x886F91EB, as libarchive's own test says; `bsdtar -x`
        # of the first volume alone gives other bytes, which is the point of
        # reading a set as a set.
        "elf-Linux-ARMv7-ls": "de9f91f9cd038989fec8abf25031b42b",
    },
}


def check(name, got, expected):
    if got != expected:
        FAILURES.append("%s\n  expected %r\n  got      %r" % (name, expected, got))


def ok(name, condition):
    if not condition:
        FAILURES.append("%s\n  expected it to hold, and it did not" % name)


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


# -- the binding ------------------------------------------------------------


def binding():
    ok("a libarchive is found (%s)" % ", ".join(libarc.searched()),
       libarc.library() is not None)
    if libarc.library() is None:
        return
    print("libarchive:", libarc.version())

    members = libarc.walk(lambda: libarc.Reader([fixture("rar_unicode.rar")]))
    names = [m.path for m in members]
    ok("names come back in UTF-8, kanji and all", "abcdefghijklmnopqrsテスト.txt" in names)
    link = [m for m in members if m.kind == libarc.IFLNK]
    check("a link is a link, with its target",
          [(unicodedata.normalize("NFC", m.path), unicodedata.normalize("NFC", m.target))
           for m in link],
          [("表だよ/ファイル", "漢字長いファイル名long-filename-in-漢字.txt")])

    members = libarc.walk(lambda: libarc.Reader([fixture("rar4_encrypted.rar")]))
    check("an encrypted member says so, and the others do not",
          [(m.path, m.encrypted) for m in members],
          [("a.txt", False), ("b.txt", True), ("c.txt", False), ("d.txt", True)])

    try:
        libarc.walk(lambda: libarc.Reader([fixture("rar5_encrypted_filenames.rar")]))
        FAILURES.append("encrypted names were listed, which cannot be")
    except libarc.ArchiveError as failure:
        ok("encrypted names are refused in words: %s" % failure, "ncrypt" in str(failure))

    with open(fixture("rar5_compressed.rar"), "rb") as handle:
        members = libarc.walk(lambda: libarc.Reader(stream=handle))
    check("read from a stream, the same as from a path",
          [(m.path, m.size) for m in members], [("test.bin", 1200)])


def volume_sets():
    parts = main_volumes.volumes(fixture("rar5_multiarchive_solid.part03.rar"))
    check("any part of a set opens the whole set, first to last",
          [os.path.basename(p) for p in parts],
          ["rar5_multiarchive_solid.part0%d.rar" % n for n in (1, 2, 3, 4)])
    check("an archive on its own is itself",
          main_volumes.volumes(fixture("rar5_compressed.rar")),
          [fixture("rar5_compressed.rar")])


# -- over the pipe ------------------------------------------------------------


def sdk() -> str:
    given = os.environ.get("XVERB_SDK")
    if given:
        return given
    return os.path.normpath(os.path.join(
        HERE, "..", "..", "..", "xverb-dev", "assets", "python"))


class Host:
    """The application, as far as the plugin can tell: the calls it makes, and
    the file system calls it answers."""

    def __init__(self):
        env = dict(os.environ, PYTHONPATH=sdk())
        self.process = subprocess.Popen(
            [sys.executable, "main.py"], cwd=HERE, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            encoding="utf-8", bufsize=1)
        self.lock = threading.Lock()
        self.replies = {}
        self.next_id = 1
        threading.Thread(target=self._listen, daemon=True).start()
        answer = self.call("initialize", {
            "apiVersion": 3, "pluginId": "org.xverb.rar", "pluginDirectory": HERE,
            "platform": sys.platform, "settings": {}, "language": "en"})
        check("the plugin starts and offers its scheme, read-only",
              [(s["scheme"], s["writable"]) for s in answer["schemes"]], [("rar", False)])

    def _send(self, message):
        with self.lock:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def _listen(self):
        for line in self.process.stdout:
            message = json.loads(line)
            if "method" in message:
                if "id" in message:
                    self._send({"jsonrpc": "2.0", "id": message["id"],
                                "result": self._serve(message)})
            else:
                self.replies[message["id"]] = message

    def _serve(self, message):
        params = message.get("params") or {}
        if message["method"] == "host.stat":
            path = params["url"][len("file://"):]
            if not os.path.exists(path):
                return None
            return {"name": os.path.basename(path), "kind": "file",
                    "size": os.path.getsize(path), "modified": 0}
        if message["method"] == "host.read":
            with open(params["url"][len("file://"):], "rb") as handle:
                handle.seek(params["offset"])
                data = handle.read(params["length"])
            return {"data": base64.b64encode(data).decode(), "eof": len(data) < params["length"]}
        return None

    def call(self, method, params, fails=False):
        request = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request, "method": method, "params": params})
        began = time.time()
        while request not in self.replies:
            if time.time() - began > 60:
                raise RuntimeError("%s did not answer" % method)
            time.sleep(0.002)
        reply = self.replies.pop(request)
        if fails:
            return reply.get("error")
        if "error" in reply:
            raise RuntimeError("%s: %s" % (method, reply["error"]))
        return reply.get("result")

    def close(self):
        try:
            self.call("shutdown", {})
        except Exception:  # noqa: BLE001
            pass
        self.process.wait(5)


def inside(archive: str, path: str = "", remote: bool = False) -> str:
    where = "file://" + (archive if not remote else archive)
    return "rar:///%s?from=%s" % (quote(path), quote(where, safe=""))


def read_all(host: Host, url: str, size: int) -> bytes:
    """As the application's copy reads: 256 KB at a time, front to back."""
    pieces = []
    offset = 0
    while True:
        answer = host.call("fs.read", {"url": url, "offset": offset, "length": 1 << 18})
        data = base64.b64decode(answer["data"])
        pieces.append(data)
        offset += len(data)
        if len(data) < 1 << 18 or offset >= size:
            break
    return b"".join(pieces)


def over_the_pipe():
    host = Host()
    try:
        archive = fixture("rar_compress_normal.rar")
        top = host.call("fs.list", {"url": inside(archive)})["entries"]
        check("the top of the archive, folders implied and empty ones kept",
              sorted((e["name"], e["kind"]) for e in top),
              [("LibarchiveAddingTest.html", "file"), ("testdir", "dir"),
               ("testemptydir", "dir"), ("testlink", "link")])
        entry = host.call("fs.stat", {"url": inside(archive, "testdir/test.txt")})
        check("stat of a member inside a folder", (entry["name"], entry["size"]), ("test.txt", 20))
        check("stat of nothing is nothing",
              host.call("fs.stat", {"url": inside(archive, "nope.txt")}), None)

        for name, sums in SUMS.items():
            archive = fixture(name)
            # Reversed as well as in order: a copy of a folder does not ask in
            # the archive's order, and going back must still give the right bytes.
            for order in (sorted(sums), sorted(sums, reverse=True)):
                for path in order:
                    size = host.call("fs.stat", {"url": inside(archive, path)})["size"]
                    data = read_all(host, inside(archive, path), size)
                    check("%s: %s reads back whole" % (name, path),
                          hashlib.md5(data).hexdigest(), sums[path])

        # Through the host, as an archive on FTP would be read: the same bytes.
        archive = fixture("rar5_compressed.rar")
        data = read_all(host, inside(archive, "test.bin"), 1200)
        check("the host's own reads give the same bytes",
              hashlib.md5(data).hexdigest(), SUMS["rar5_compressed.rar"]["test.bin"])

        refused = host.call("fs.read", {"url": inside(fixture("rar4_encrypted.rar"), "b.txt"),
                                        "offset": 0, "length": 100}, fails=True)
        ok("an encrypted member is refused in words", refused and "encrypted" in refused["message"])
        refused = host.call("fs.mkdir", {"url": inside(fixture("rar5_compressed.rar"), "new")},
                            fails=True)
        ok("nothing writes a RAR", refused and "only be read" in refused["message"])

        table = host.call("viewer.open", {"viewerId": "rar.contents",
                                          "url": "file://" + fixture("rar_unicode.rar")})
        check("F3 is a table of the files", table["kind"], "table")
        check("with a row per file", len(table["rows"]), 4)
        broken = host.call("viewer.open", {"viewerId": "rar.contents",
                                           "url": "file://" + fixture("rar_invalid1.rar")})
        check("a broken archive lists what came before the damage",
              broken["kind"], "table")
        locked = host.call("viewer.open", {"viewerId": "rar.contents",
                                           "url": "file://" + fixture("rar5_encrypted_filenames.rar")})
        ok("encrypted names are said to be encrypted",
           locked["kind"] == "error" and "encrypted" in locked["message"])
    finally:
        host.close()


if __name__ == "__main__":
    binding()
    volume_sets()
    if libarc.library() is not None:
        over_the_pipe()
    if FAILURES:
        print("\n\n".join(FAILURES))
        print("\n%d failure(s)" % len(FAILURES))
        sys.exit(1)
    print("all good")
