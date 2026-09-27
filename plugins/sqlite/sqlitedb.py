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

"""A SQLite database, read and never written — without the host, so the
self-test can ask it everything the viewer does.

**Opened read-only, and for as long as one question takes.** A connection
held open is a file held open, and on Windows a file held open cannot be
deleted: a database looked at with F3 would then refuse F8 until the plugin
was stopped. So every question — a screen of rows, a count — opens the file,
asks, and closes it again, which SQLite does in well under a millisecond.

**A database somebody else is using may refuse to be read.** A browser keeps
its history locked for as long as it runs, and a database in WAL mode cannot
be opened read-only where its `-shm` file cannot be made. Either way the
answer is the same: the file and its `-wal` are copied aside and the copy is
opened — a snapshot of the moment, which is what a viewer shows anyway.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from typing import Callable, List, Optional, Sequence, Tuple
from urllib.parse import quote

MAGIC = b"SQLite format 3\x00"

#: Rows fetched in one query, and how many such blocks are kept. A screen asks
#: for a few hundred rows; a block answers several screens of scrolling.
BLOCK = 1024
KEEP_BLOCKS = 16

#: The longest a count may take before it is given up — a view over a join of
#: two large tables can take minutes, and a sheet is not made to wait for it.
COUNT_SECONDS = 8.0


def is_database(head: bytes) -> bool:
    return head.startswith(MAGIC)


def quoted(name: str) -> str:
    """An identifier as SQL writes one, whatever is in it."""
    return '"' + name.replace('"', '""') + '"'


class Blob:
    """A BLOB cell: its length, and its first bytes to say what it is."""

    __slots__ = ("size", "head")

    def __init__(self, data: bytes):
        self.size = len(data)
        self.head = bytes(data[:16])

    def kind(self) -> str:
        head = self.head
        if head.startswith(b"\x89PNG"):
            return "PNG"
        if head.startswith(b"\xff\xd8\xff"):
            return "JPEG"
        if head.startswith((b"GIF87a", b"GIF89a")):
            return "GIF"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "WebP"
        if head.startswith(b"bplist"):
            return "plist"
        if head.startswith(b"PK\x03\x04"):
            return "zip"
        if head.startswith(b"\x1f\x8b"):
            return "gzip"
        if head.startswith(MAGIC):
            return "SQLite"
        if head[:1] in (b"{", b"[") and self.size < 1 << 20:
            return "JSON"
        return ""


class _Copies:
    """The copies made of databases that would not be read in place.

    Kept for as long as the plugin runs, since the sheet goes on asking for
    rows, and removed when it stops; the oldest go first past a handful.
    """

    KEEP = 4

    def __init__(self):
        self._made: "OrderedDict[str, str]" = OrderedDict()
        self._lock = threading.Lock()
        atexit.register(self.clear)

    def of(self, source: str) -> str:
        with self._lock:
            found = self._made.get(source)
            if found and os.path.exists(found):
                self._made.move_to_end(source)
                return found
            folder = tempfile.mkdtemp(prefix="xverb-sqlite-")
            target = os.path.join(folder, "copy.db")
            shutil.copyfile(source, target)
            # The recent writes of a WAL database are in its -wal file, and
            # the copy replays them when it is first opened.
            if os.path.exists(source + "-wal"):
                shutil.copyfile(source + "-wal", target + "-wal")
            self._made[source] = target
            while len(self._made) > self.KEEP:
                _, old = self._made.popitem(last=False)
                shutil.rmtree(os.path.dirname(old), ignore_errors=True)
            return target

    def clear(self) -> None:
        with self._lock:
            for path in self._made.values():
                shutil.rmtree(os.path.dirname(path), ignore_errors=True)
            self._made.clear()


COPIES = _Copies()


class Database:
    """One database file, opened afresh for every question.

    ``path`` is a file on this machine. ``copied`` says the file could not be
    read where it lies and a copy is being read instead.
    """

    def __init__(self, path: str):
        self.path = path
        self.copied = False
        with open(path, "rb") as f:
            #: The file's first hundred bytes — its header, for the report.
            self.head = f.read(100)
        self._target = path
        try:
            with self.connect() as db:
                db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError as failure:
            if isinstance(failure, sqlite3.OperationalError) and _would_a_copy_help(failure):
                self._target = COPIES.of(path)
                self.copied = True
                with self.connect() as db:
                    db.execute("SELECT count(*) FROM sqlite_master").fetchone()
            else:
                raise

    def connect(self) -> "_Connection":
        return _Connection(self._target, read_only=not self.copied)

    def objects(self) -> List[Tuple[str, str, str]]:
        """``(name, type, sql)`` of every table and view a person made.

        SQLite's own tables are left out; tables before views, in the order
        they were made — which is usually the order that explains the rest.
        """
        with self.connect() as db:
            found = db.execute(
                "SELECT name, type, coalesce(sql, '') FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
                "ORDER BY type = 'view', rowid").fetchall()
        return [(str(n), str(t), str(s)) for n, t, s in found]

    def columns(self, name: str) -> List[dict]:
        """What PRAGMA table_info says, one dict a column."""
        with self.connect() as db:
            found = db.execute("PRAGMA table_info(%s)" % quoted(name)).fetchall()
        return [{"name": str(r[1]), "type": str(r[2] or ""), "notnull": bool(r[3]),
                 "default": r[4], "pk": int(r[5] or 0)} for r in found]

    def count(self, name: str, seconds: float = COUNT_SECONDS) -> Optional[int]:
        """The rows in a table or view, or None when counting took too long."""
        deadline = _Deadline(seconds)
        with self.connect() as db:
            deadline.bind(db)
            try:
                return int(db.execute("SELECT count(*) FROM %s" % quoted(name)).fetchone()[0])
            except sqlite3.OperationalError:
                if deadline.passed:
                    return None
                raise

    def rows(self, name: str, start: int, count: int) -> List[tuple]:
        with self.connect() as db:
            return db.execute("SELECT * FROM %s LIMIT ? OFFSET ?" % quoted(name),
                              (count, start)).fetchall()

    def query(self, sql: str, *args) -> list:
        with self.connect() as db:
            return db.execute(sql, args).fetchall()


def _would_a_copy_help(failure: sqlite3.OperationalError) -> bool:
    text = str(failure).lower()
    return any(word in text for word in ("locked", "busy", "unable to open", "readonly",
                                         "read-only", "disk i/o"))


class _Connection:
    """A connection closed on the way out — `sqlite3`'s own `with` only commits."""

    def __init__(self, path: str, read_only: bool):
        self._path = path
        self._read_only = read_only
        self._db: Optional[sqlite3.Connection] = None

    def __enter__(self) -> sqlite3.Connection:
        uri = "file:%s?mode=%s" % (quote(os.path.abspath(self._path).replace(os.sep, "/")),
                                   "ro" if self._read_only else "rw")
        db = sqlite3.connect(uri, uri=True, timeout=0.5, check_same_thread=False)
        # Text that is not UTF-8 is shown, not refused: an old program writing
        # Latin-1 into a TEXT column is common, and one cell must not stop a sheet.
        db.text_factory = lambda raw: raw.decode("utf-8", "replace")
        self._db = db
        return db

    def __exit__(self, *_):
        if self._db is not None:
            self._db.close()
        return False


class _Deadline:
    """Interrupts a query that has run longer than it was given."""

    def __init__(self, seconds: float):
        self._until = time.monotonic() + seconds
        self.passed = False

    def bind(self, db: sqlite3.Connection) -> sqlite3.Connection:
        def check() -> int:
            if time.monotonic() > self._until:
                self.passed = True
                return 1
            return 0

        db.set_progress_handler(check, 20000)
        return db


class TableRows(Sequence):
    """A table's rows as the sheet asks for them: its header first, then the
    rows, fetched a block at a time and only when they are asked for.
    """

    def __init__(self, database: Database, name: str, header: List[str], count: int,
                 convert: Callable[[object], object]):
        self._database = database
        self._name = name
        self._header = list(header)
        self._count = count
        self._convert = convert
        self._blocks: "OrderedDict[int, list]" = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return self._count + 1

    def __getitem__(self, at):
        if isinstance(at, slice):
            start, stop, step = at.indices(len(self))
            if step != 1:
                return [self[i] for i in range(start, stop, step)]
            return self._range(start, stop)
        if at < 0:
            at += len(self)
        if not 0 <= at < len(self):
            raise IndexError(at)
        return self._range(at, at + 1)[0]

    def _range(self, start: int, stop: int) -> list:
        out = []
        if start == 0 and stop > 0:
            out.append(self._header)
            start = 1
        at = start - 1
        end = stop - 1
        while at < end:
            block = self._block(at // BLOCK)
            offset = at % BLOCK
            taken = block[offset:offset + (end - at)]
            if not taken:
                break
            out.extend(taken)
            at += len(taken)
        return out

    def _block(self, number: int) -> list:
        with self._lock:
            found = self._blocks.get(number)
            if found is not None:
                self._blocks.move_to_end(number)
                return found
        rows = self._database.rows(self._name, number * BLOCK, BLOCK)
        convert = self._convert
        block = [[convert(v) for v in row] for row in rows]
        with self._lock:
            self._blocks[number] = block
            while len(self._blocks) > KEEP_BLOCKS:
                self._blocks.popitem(last=False)
        return block


def header_facts(head: bytes) -> dict:
    """What the first hundred bytes of the file say about it."""
    if len(head) < 100 or not is_database(head):
        return {}
    page = int.from_bytes(head[16:18], "big")
    version = int.from_bytes(head[96:100], "big")
    return {
        "pageSize": 65536 if page == 1 else page,
        "wal": head[18] == 2 or head[19] == 2,
        "pages": int.from_bytes(head[28:32], "big"),
        "encoding": {1: "UTF-8", 2: "UTF-16le", 3: "UTF-16be"}.get(
            int.from_bytes(head[56:60], "big"), "UTF-8"),
        "userVersion": int.from_bytes(head[60:64], "big"),
        "applicationId": int.from_bytes(head[68:72], "big"),
        "writtenBy": "%d.%d.%d" % (version // 1000000, version // 1000 % 1000, version % 1000)
        if version else "",
    }
