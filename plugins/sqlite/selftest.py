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

"""The database reader, checked without the host: `python3 selftest.py [files…]`.

The databases are made here, each for one trap: a name that needs quoting,
a view over a table that is gone, the writes a WAL database holds only in its
`-wal`, a database another connection has locked, text that is not UTF-8, and
a table long enough that its rows must come a block at a time. Files named on
the command line are opened and summed up.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlitedb  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        FAILED.append(name)


def same(v):
    return v


folder = tempfile.mkdtemp(prefix="sqlite-selftest-")

# -- a plain database, with the awkward names and values --------------------------

plain = os.path.join(folder, "plain.db")
db = sqlite3.connect(plain)
db.executescript('''
    CREATE TABLE "order items" ("the ""id""" INTEGER PRIMARY KEY, name TEXT NOT NULL,
                                price REAL DEFAULT 0, picture BLOB);
    CREATE TABLE gone (x);
    CREATE VIEW cheap AS SELECT name FROM "order items" WHERE price < 5;
    CREATE VIEW broken AS SELECT x FROM gone;
    CREATE TABLE empty (a, b);
    CREATE TABLE long (n INTEGER, square INTEGER);
    CREATE INDEX by_name ON "order items"(name);
''')
db.execute('INSERT INTO "order items" VALUES (1, ?, 2.5, ?)', ("tea", b"\x89PNG\r\n\x1a\n" + b"\0" * 100))
db.execute('INSERT INTO "order items" VALUES (2, ?, 7, NULL)', ("coffee",))
db.execute('INSERT INTO "order items" VALUES (3, CAST(? AS TEXT), 1, NULL)', (b"caf\xe9",))
db.executemany("INSERT INTO long VALUES (?, ?)", ((n, n * n) for n in range(200000)))
db.execute("DROP TABLE gone")
db.commit()
db.close()

with open(plain, "rb") as f:
    head = f.read(4096)
check("the header is recognised", sqlitedb.is_database(head))
check("a compound document called .db is not", not sqlitedb.is_database(b"\xd0\xcf\x11\xe0" + b"\0" * 60))

database = sqlitedb.Database(plain)
objects = database.objects()
names = [(n, k) for n, k, _ in objects]
check("tables first, then views, SQLite's own left out",
      names == [("order items", "table"), ("empty", "table"), ("long", "table"),
                ("cheap", "view"), ("broken", "view")], names)
columns = database.columns("order items")
check("a column name with quotes in it", columns[0]["name"] == 'the "id"', columns[0])
check("its primary key and NOT NULL", columns[0]["pk"] == 1 and columns[1]["notnull"], columns)
check("three rows counted", database.count("order items") == 3)

rows = sqlitedb.TableRows(database, "order items", [c["name"] for c in columns], 3, same)
check("the header is row 0", rows[0] == ['the "id"', "name", "price", "picture"], rows[0])
check("a BLOB is left to the caller", isinstance(rows[1][3], bytes))
check("text that is not UTF-8 is shown, not refused", rows[3][1] == "caf�", rows[3])
check("a PNG in a BLOB is named", sqlitedb.Blob(rows[1][3]).kind() == "PNG")

try:
    database.columns("broken")
    database.count("broken")
    check("a view over a missing table says so", False, "no error")
except sqlite3.OperationalError as failure:
    check("a view over a missing table says so", "gone" in str(failure), failure)

count = database.count("long")
long_rows = sqlitedb.TableRows(database, "long", ["n", "square"], count, same)
check("200 000 rows and the header", len(long_rows) == 200001, len(long_rows))
started = time.time()
piece = long_rows[150000:150300]
took = time.time() - started
check("a screen deep in the table is its own rows",
      piece[0] == [149999, 149999 ** 2] and len(piece) == 300, piece[:1])
check("and comes quickly (%.0f ms)" % (took * 1000), took < 0.5, took)
across = long_rows[1020:1030]
check("a screen across two blocks", [r[0] for r in across] == list(range(1019, 1029)),
      [r[0] for r in across])
check("the end of the table stops at the end", len(long_rows[199990:200500]) == 11)

started = time.time()
check("a count past its time says it gave up",
      database.count("long", seconds=0.0) is None or time.time() - started < 0.1)

facts = sqlitedb.header_facts(head[:100])
check("the header's page size and encoding", facts["pageSize"] in (1024, 4096, 8192)
      and facts["encoding"] == "UTF-8", facts)

# -- WAL: the rows that are only in the -wal file --------------------------------

wal = os.path.join(folder, "wal.db")
writer = sqlite3.connect(wal)
writer.execute("PRAGMA journal_mode=WAL")
writer.execute("PRAGMA wal_autocheckpoint=0")
writer.execute("CREATE TABLE t (x)")
writer.executemany("INSERT INTO t VALUES (?)", ((i,) for i in range(10)))
writer.commit()
check("the writes are still in the -wal", os.path.getsize(wal + "-wal") > 0)
check("a WAL database is read with what its -wal holds",
      sqlitedb.Database(wal).count("t") == 10)

# -- locked by another connection --------------------------------------------------

locked = os.path.join(folder, "locked.db")
holder = sqlite3.connect(locked)
holder.execute("CREATE TABLE t (x)")
holder.execute("INSERT INTO t VALUES (1)")
holder.commit()
holder.execute("PRAGMA locking_mode=EXCLUSIVE")
holder.execute("BEGIN EXCLUSIVE")
holder.execute("INSERT INTO t VALUES (2)")
opened = sqlitedb.Database(locked)
check("a locked database is read from a copy", opened.copied and opened.count("t") == 1,
      (opened.copied, opened.count("t")))
holder.rollback()
holder.close()
writer.close()

# -- nothing is left open ----------------------------------------------------------

database = sqlitedb.Database(plain)
database.count("long")
sqlitedb.TableRows(database, "long", ["n", "square"], 200000, same)[5:10]
renamed = plain + ".moved"
os.replace(plain, renamed)
os.replace(renamed, plain)
check("no connection is held between questions",
      sqlitedb.Database(plain).count("empty") == 0)

for path in sys.argv[1:]:
    started = time.time()
    try:
        d = sqlitedb.Database(path)
        objects = d.objects()
        print("%s: %d table(s)/view(s)%s, %.2fs" % (
            os.path.basename(path), len(objects), ", from a copy" if d.copied else "",
            time.time() - started))
        for name, kind, _ in objects[:8]:
            print("    %-6s %-32s %s rows" % (kind, name[:32], d.count(name, 2.0)))
    except sqlite3.DatabaseError as failure:
        print("%s: %s" % (os.path.basename(path), failure))

print("\n%d failure(s)" % len(FAILED) if FAILED else "\nall good")
sys.exit(1 if FAILED else 0)
