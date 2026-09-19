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

"""Spreadsheets as sheets — `.xlsx`, `.xls`, `.ods` and their kin.

The host draws the sheet: the grid, the headings, the selection, the scrolling.
This plugin only reads the file and answers for rows, a screen at a time, so a
workbook of five thousand rows sends two hundred and fifty-six of them when it
opens and the rest only if somebody scrolls there.

Everything is the standard library. `.xlsx` and `.ods` are zips of XML; `.xls`
is the old binary format, read here from its compound document up — see
`xls.py`. Nothing is written back: a spreadsheet is edited in a spreadsheet
program, and this is for looking.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import zipfile
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xverb import Plugin, Sheet, error  # noqa: E402

import ods  # noqa: E402
import textbooks  # noqa: E402
import xls  # noqa: E402
import xlsb  # noqa: E402
import xlsx  # noqa: E402

plugin = Plugin("org.xverb.sheets", "Spreadsheets")

#: A workbook is read whole — the zip keeps its table of contents at the end,
#: and a compound document its allocation table anywhere — so a part of one is
#: not a smaller workbook but an unreadable one.
MAX_BYTES = 256 << 20

EXTENSIONS = ["xlsx", "xlsm", "xltx", "xltm", "xlsb", "xls", "xlt", "ods", "ots"]


@plugin.viewer(
    "sheets.workbook",
    "Workbook",
    extensions=EXTENSIONS,
    # Above the archive viewer, which claims .xlsx as the zip it is at 30: a
    # workbook is opened to be read, and its parts are one Shift+F3 away.
    priority=40,
    produces="spreadsheet",
)
def workbook(url: str) -> dict:
    started = time.time()
    try:
        raw = plugin.read_file(url, max_bytes=MAX_BYTES)
    except Exception as failure:  # noqa: BLE001
        return error(plugin.tr("The file could not be read: {error}",
                               {"error": failure}))
    if not raw:
        return error(plugin.tr("The file is empty."))
    if len(raw) >= MAX_BYTES:
        return error(plugin.tr(
            "This workbook is larger than {size} MB, and a workbook cannot be "
            "read in part. Press Enter to open it in the spreadsheet program.",
            {"size": MAX_BYTES >> 20}))

    language = plugin.language
    max_rows = int(plugin.setting("maxRows", 0) or 0)
    try:
        titles, load = _open(raw, language, max_rows)
    except (xls.XlsError, ValueError) as failure:
        # The readers write their reasons in English; the catalogue has them.
        return error(plugin.tr(str(failure)))
    except zipfile.BadZipFile:
        return error(plugin.tr("This workbook is damaged: its zip cannot be read."))
    except Exception as failure:  # noqa: BLE001
        return error(plugin.tr("This workbook could not be read: {error}",
                               {"error": failure}))

    plugin.log("%s: %d sheet(s), opened in %.2fs"
               % (url, len(titles), time.time() - started))
    return plugin.workbook(titles, load=load)


def _open(raw: bytes, language: str, max_rows: int):
    """The sheet names, and how to read any one of them."""
    head = raw[:8192]
    if raw[:8] == xls.MAGIC:
        book = xls.Book(raw, language, max_rows)

        def page(i):
            extras = _extras()
            return _sheet(book.titles[i], book.rows(i, extras), extras)

        return book.titles, page

    if raw[:2] == b"PK":
        archive = zipfile.ZipFile(io.BytesIO(raw))
        names = archive.namelist()
        if ods.is_spreadsheet(archive):
            sheets = ods.read(raw, language, max_rows)
            if not sheets:
                raise ValueError(plugin.tr("This spreadsheet holds no sheets."))
            return [name for name, _, _ in sheets], \
                lambda i: _sheet(sheets[i][0], sheets[i][1], sheets[i][2])
        if xlsx.is_workbook(names):
            book = xlsx.Workbook(raw, language, max_rows)
            if not book.titles:
                raise ValueError(plugin.tr("This workbook holds no sheets."))
            return book.titles, lambda i: _reading(
                book.titles[i],
                lambda rows, stop, extras: book.fill(i, rows, stop, extras),
                notes=book.notes(i))
        if xlsb.is_workbook(names):
            binary = xlsb.Workbook(raw, language, max_rows)
            if not binary.titles:
                raise ValueError(plugin.tr("This workbook holds no sheets."))
            return binary.titles, lambda i: _reading(
                binary.titles[i], lambda rows, stop, extras: binary.fill(i, rows, stop))
        raise ValueError(plugin.tr(
            "This zip is not a spreadsheet. Shift+F3 shows what is inside it."))

    # Two things called .xls that are text: Excel 2003's XML, and a web page —
    # which is how a great many programs export "Excel". Asked in that order,
    # because the XML's sheets are <Table> elements.
    if textbooks.looks_like_spreadsheetml(head):
        return _given(textbooks.read_spreadsheetml(raw, language), max_rows)
    if textbooks.looks_like_html(head):
        return _given(textbooks.read_html(raw), max_rows)
    raise ValueError(plugin.tr("This is not a spreadsheet this viewer can read."))


def _given(sheets, max_rows: int):
    if not sheets:
        raise ValueError(plugin.tr("This file holds no table."))
    sheets = [
        (name or plugin.tr("Table {number}", {"number": at + 1}),
         rows[:max_rows] if max_rows else rows)
        for at, (name, rows) in enumerate(sheets)
    ]
    return [name for name, _ in sheets], lambda i: _sheet(sheets[i][0], sheets[i][1])


#: Rows read before a sheet is shown; the rest follow while it is looked at.
FIRST_ROWS = 300


def _extras() -> dict:
    return {"merges": [], "notes": {}, "hidden_rows": [], "hidden_columns": []}


def _reading(title: str, fill, notes: Optional[dict] = None) -> Sheet:
    """A sheet shown as soon as its first screen is read.

    A large .xlsx takes seconds to read to the end, and there is no reason to
    look at a blank window for them: the reading goes on in a thread, the
    host is handed the rows so far and told the count is still growing, and
    asks again until it is not.
    """
    rows: list = []
    extras = _extras()
    if notes:
        extras["notes"] = notes
    stop = threading.Event()
    finished = threading.Event()
    failure: list = []

    def work() -> None:
        try:
            fill(rows, stop.is_set, extras)
        except Exception as problem:  # noqa: BLE001 - said, not raised
            failure.append(problem)
        finally:
            finished.set()

    threading.Thread(target=work, name="read " + title, daemon=True).start()
    waited = time.time() + 20
    while not finished.is_set() and len(rows) < FIRST_ROWS and time.time() < waited:
        finished.wait(0.02)
    if failure and not rows:
        raise ValueError(plugin.tr("This sheet could not be read: {error}",
                                   {"error": failure[0]}))
    if finished.is_set() and not rows:
        return Sheet(title, [], message=plugin.tr("This sheet is empty."))
    return Sheet(title, rows, done=finished.is_set, cancel=stop.set,
                 merges=extras["merges"], notes=extras["notes"],
                 hidden_rows=extras["hidden_rows"],
                 hidden_columns=extras["hidden_columns"])


def _sheet(title: str, rows: list, extras: Optional[dict] = None) -> Sheet:
    # Trailing empty rows say nothing and would only lengthen the scroll.
    while rows and not rows[-1]:
        rows.pop()
    if not rows:
        return Sheet(title, [], message=plugin.tr("This sheet is empty."))
    extras = extras or _extras()
    return Sheet(title, rows, merges=extras["merges"], notes=extras["notes"],
                 hidden_rows=extras["hidden_rows"],
                 hidden_columns=extras["hidden_columns"])


if __name__ == "__main__":
    plugin.run()
