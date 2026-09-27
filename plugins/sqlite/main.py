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

"""SQLite databases as sheets — a page for every table and view.

The host draws the sheet; this plugin answers for rows a screen at a time,
straight out of the database with `LIMIT` and `OFFSET`, so a table of ten
million rows costs what its first screen costs. Shift+F3 gives the report:
every table with its columns, keys, indexes and the statement that made it.

Everything is Python's own `sqlite3`. Nothing is ever written: the file is
opened read-only, and a database that will not be read where it lies — a
browser's history while the browser runs — is read from a copy.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xverb import Plugin, Sheet, error, local_path, markdown, sheet_cell  # noqa: E402

import sqlitedb  # noqa: E402

plugin = Plugin("org.xverb.sqlite", "SQLite databases")

EXTENSIONS = ["db", "sqlite", "sqlite3", "db3", "s3db", "sl3", "sqlitedb", "gpkg", "mbtiles"]

#: A database on another file system is copied here to be read, and SQLite
#: needs the whole file for that; past this it says so instead.
MAX_REMOTE_BYTES = 512 << 20

#: Text longer than this is cut in the cell: the sheet shows a line of it, and
#: a column of whole documents would cross the pipe a screen at a time.
MAX_TEXT = 32 << 10


def looks_like_a_database(head: bytes) -> bool:
    # `.db` is claimed by far more than SQLite — Windows' Thumbs.db is a
    # compound document — so the extension alone is not enough.
    return sqlitedb.is_database(head)


class TableSheet(Sheet):
    """A sheet whose width is the table's, not a walk over every row."""

    def __init__(self, title: str, rows, columns: int, **kwargs):
        super().__init__(title, rows, **kwargs)
        self._columns = columns

    @property
    def width(self) -> int:
        return self._columns


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0
    return ""


def cell(value: object) -> object:
    if isinstance(value, bytes):
        blob = sqlitedb.Blob(value)
        kind = blob.kind()
        text = "%s · %s" % (kind or "BLOB", _size(blob.size))
        return sheet_cell(text, text=text, role="dim")
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + "…"
    return value


def _database(url: str) -> Tuple[Optional[sqlitedb.Database], Optional[dict]]:
    path = local_path(url)
    try:
        if path is None:
            path = _fetched(url)
        return sqlitedb.Database(path), None
    except _TooLarge:
        return None, error(plugin.tr(
            "This database is larger than {size} MB and is not on this machine. "
            "Copy it here to look inside it.", {"size": MAX_REMOTE_BYTES >> 20}))
    except sqlitedb.sqlite3.DatabaseError as failure:
        text = str(failure)
        if "encrypted" in text or "not a database" in text:
            return None, error(plugin.tr(
                "This file is not a database SQLite can read — it may be encrypted."))
        return None, error(plugin.tr("The database could not be opened: {error}",
                                     {"error": failure}))
    except OSError as failure:
        return None, error(plugin.tr("The file could not be read: {error}",
                                     {"error": failure}))


class _TooLarge(Exception):
    pass


_FETCHED: dict = {}


def _fetched(url: str) -> str:
    """A database on another file system, brought here — with its -wal."""
    found = _FETCHED.get(url)
    if found and os.path.exists(found):
        return found
    raw = plugin.read_file(url, max_bytes=MAX_REMOTE_BYTES + 1)
    if len(raw) > MAX_REMOTE_BYTES:
        raise _TooLarge()
    folder = tempfile.mkdtemp(prefix="xverb-sqlite-")
    target = os.path.join(folder, "remote.db")
    with open(target, "wb") as out:
        out.write(raw)
    try:
        wal = plugin.read_file(url + "-wal", max_bytes=MAX_REMOTE_BYTES)
    except Exception:  # noqa: BLE001 - most databases have none
        wal = b""
    if wal:
        with open(target + "-wal", "wb") as out:
            out.write(wal)
    # Remembered for the plugin's life and cleared with the copies.
    old = _FETCHED.pop(url, None)
    if old:
        shutil.rmtree(os.path.dirname(old), ignore_errors=True)
    _FETCHED[url] = target
    atexit.register(shutil.rmtree, folder, True)
    return target


@plugin.viewer(
    "sqlite.tables",
    "Tables",
    extensions=EXTENSIONS,
    priority=40,
    probe=looks_like_a_database,
    produces="database",
)
def tables(url: str) -> dict:
    started = time.time()
    database, refusal = _database(url)
    if refusal is not None:
        return refusal
    try:
        objects = database.objects()
    except sqlitedb.sqlite3.DatabaseError as failure:
        return error(plugin.tr("The database could not be read: {error}", {"error": failure}))
    if not objects:
        return error(plugin.tr("This database holds no tables."))

    titles = [name if kind == "table" else plugin.tr("{name} (view)", {"name": name})
              for name, kind, _ in objects]

    def load(index: int) -> Sheet:
        name, kind, _ = objects[index]
        return _sheet(database, titles[index], name, kind)

    def on_menu(index: int, item: str, selection: dict) -> Optional[dict]:
        name, kind, sql = objects[index]
        if item == "create" and sql:
            return {"copy": sql.strip() + ";", "notice": plugin.tr("Copied.")}
        if item == "select":
            return {"copy": "SELECT * FROM %s;" % sqlitedb.quoted(name),
                    "notice": plugin.tr("Copied.")}
        return None

    plugin.log("%s: %d table(s) and view(s)%s, opened in %.2fs" % (
        url, len(objects), " from a copy" if database.copied else "", time.time() - started))
    return plugin.workbook(
        titles, load=load,
        menu=[("create", plugin.tr("Copy the statement that made it")),
              ("select", plugin.tr("Copy a SELECT of it"))],
        on_menu=on_menu)


def _sheet(database: sqlitedb.Database, title: str, name: str, kind: str) -> Sheet:
    try:
        columns = database.columns(name)
        count = database.count(name)
    except sqlitedb.sqlite3.DatabaseError as failure:
        # A view over a table that is gone, a virtual table whose module this
        # SQLite lacks: the page says so, and the others still open.
        said = (plugin.tr("This view could not be read: {error}", {"error": failure})
                if kind == "view" else
                plugin.tr("This table could not be read: {error}", {"error": failure}))
        return Sheet(title, [], message=said)
    header = [c["name"] for c in columns]
    if count is None:
        # Counting gave up: show what a fixed number of rows holds and say so.
        limit = 100000
        count = len(database.query("SELECT 1 FROM %s LIMIT %d" % (sqlitedb.quoted(name), limit)))
    if not header:
        return Sheet(title, [], message=plugin.tr("This table has no columns."))
    rows = sqlitedb.TableRows(database, name, header, count, cell)
    message = "" if count else plugin.tr("This table is empty.")
    return TableSheet(title, rows, len(header), header=True, message=message)


@plugin.viewer("sqlite.contents", "What is in the database", extensions=EXTENSIONS,
               priority=10, probe=looks_like_a_database)
def contents(url: str) -> dict:
    database, refusal = _database(url)
    if refusal is not None:
        return refusal
    try:
        return markdown(_report(database, sqlitedb.header_facts(database.head)))
    except sqlitedb.sqlite3.DatabaseError as failure:
        return error(plugin.tr("The database could not be read: {error}", {"error": failure}))


def _report(database: sqlitedb.Database, facts: dict) -> str:
    tr = plugin.tr
    out: List[str] = ["# " + tr("What is in the database"), ""]
    if facts:
        size = facts["pages"] * facts["pageSize"]
        out.append("- " + tr("Size: {size} in {pages} pages of {page} bytes", {
            "size": _size(size), "pages": facts["pages"], "page": facts["pageSize"]}))
        out.append("- " + tr("Text is stored as {encoding}", {"encoding": facts["encoding"]}))
        out.append("- " + (tr("Journal: WAL") if facts["wal"] else tr("Journal: rollback")))
        if facts["writtenBy"]:
            out.append("- " + tr("Last written by SQLite {version}",
                                 {"version": facts["writtenBy"]}))
        if facts["userVersion"]:
            out.append("- " + tr("The application's own version number: {n}",
                                 {"n": facts["userVersion"]}))
        if facts["applicationId"]:
            out.append("- " + tr("Application id: {id}", {"id": "0x%08X" % facts["applicationId"]}))
    if database.copied:
        out.append("- " + tr("Another program holds this database, so a copy of it was read."))
    out.append("")

    objects = database.objects()
    tables = [(n, s) for n, k, s in objects if k == "table"]
    views = [(n, s) for n, k, s in objects if k == "view"]
    budget = time.monotonic() + 10

    if tables:
        out += ["## " + tr("Tables"), "",
                "| %s | %s | %s |" % (tr("Table"), tr("Rows"), tr("Columns")),
                "| --- | ---: | ---: |"]
        counts = {}
        for name, _ in tables:
            left = budget - time.monotonic()
            counts[name] = database.count(name, max(0.2, min(2.0, left))) if left > 0 else None
            columns = len(database.columns(name))
            shown = "%d" % counts[name] if counts[name] is not None else tr("not counted")
            out.append("| %s | %s | %d |" % (_cell(name), shown, columns))
        out.append("")
        for name, sql in tables:
            out += _table(database, name, sql)
    if views:
        out += ["## " + tr("Views"), ""]
        for name, sql in views:
            out += ["### " + name, "", "```sql", sql.strip(), "```", ""]

    triggers = database.query(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name")
    if triggers:
        out += ["## " + tr("Triggers"), ""]
        for name, table, sql in triggers:
            out += ["### %s — %s" % (name, table), "", "```sql", str(sql).strip(), "```", ""]
    return "\n".join(out)


def _table(database: sqlitedb.Database, name: str, sql: str) -> List[str]:
    tr = plugin.tr
    out = ["### " + name, "",
           "| %s | %s | %s |" % (tr("Column"), tr("Type"), tr("Notes")),
           "| --- | --- | --- |"]
    for c in database.columns(name):
        notes = []
        if c["pk"]:
            notes.append(tr("primary key"))
        if c["notnull"]:
            notes.append(tr("not null"))
        if c["default"] is not None:
            notes.append(tr("default {value}", {"value": c["default"]}))
        out.append("| %s | %s | %s |" % (_cell(c["name"]), _cell(c["type"]), ", ".join(notes)))
    out.append("")
    keys = database.query("PRAGMA foreign_key_list(%s)" % sqlitedb.quoted(name))
    for key in keys:
        out.append("- " + tr("{column} refers to {table}.{target}", {
            "column": key[3], "table": key[2], "target": key[4] or "rowid"}))
    indexes = database.query("PRAGMA index_list(%s)" % sqlitedb.quoted(name))
    for index in indexes:
        columns = database.query("PRAGMA index_info(%s)" % sqlitedb.quoted(index[1]))
        said = ", ".join(str(c[2]) for c in columns if c[2] is not None)
        said = {"name": index[1], "columns": said}
        out.append("- " + (tr("Unique index {name} on {columns}", said) if index[2]
                           else tr("Index {name} on {columns}", said)))
    if keys or indexes:
        out.append("")
    if sql:
        out += ["```sql", sql.strip(), "```", ""]
    return out


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|") or " "


if __name__ == "__main__":
    plugin.run()
