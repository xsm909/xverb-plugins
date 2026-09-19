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
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xverb import Plugin, Sheet, error  # noqa: E402

import ods  # noqa: E402
import xls  # noqa: E402
import xlsx  # noqa: E402

plugin = Plugin("org.xverb.sheets", "Spreadsheets")

#: A workbook is read whole — the zip keeps its table of contents at the end,
#: and a compound document its allocation table anywhere — so a part of one is
#: not a smaller workbook but an unreadable one.
MAX_BYTES = 256 << 20

EXTENSIONS = ["xlsx", "xlsm", "xltx", "xltm", "xls", "xlt", "ods", "ots"]


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
    head = raw[:4096]
    if raw[:8] == xls.MAGIC:
        book = xls.Book(raw, language, max_rows)
        return book.titles, lambda i: _sheet(book.titles[i], book.rows(i))

    if raw[:2] == b"PK":
        archive = zipfile.ZipFile(io.BytesIO(raw))
        if ods.is_spreadsheet(archive):
            sheets = ods.read(raw, language, max_rows)
            if not sheets:
                raise ValueError(plugin.tr("This spreadsheet holds no sheets."))
            return [name for name, _ in sheets], \
                lambda i: _sheet(sheets[i][0], sheets[i][1])
        if xlsx.is_workbook(archive.namelist()):
            book = xlsx.Workbook(raw, language, max_rows)
            if not book.titles:
                raise ValueError(plugin.tr("This workbook holds no sheets."))
            return book.titles, lambda i: _sheet(book.titles[i], book.rows(i))
        raise ValueError(plugin.tr(
            "This zip is not a spreadsheet. Shift+F3 shows what is inside it."))

    lower = head.lower()
    if b"urn:schemas-microsoft-com:office:spreadsheet" in lower:
        raise ValueError(plugin.tr(
            "This is an Excel 2003 XML spreadsheet, which this viewer does not "
            "read yet. Shift+F3 shows it as text."))
    if b"<html" in lower or b"<table" in lower:
        raise ValueError(plugin.tr(
            "This file is a web page saved with a spreadsheet's name, which "
            "is how many programs export “Excel”. Shift+F3 shows it as text."))
    raise ValueError(plugin.tr("This is not a spreadsheet this viewer can read."))


def _sheet(title: str, rows: list) -> Sheet:
    # Trailing empty rows say nothing and would only lengthen the scroll.
    while rows and not rows[-1]:
        rows.pop()
    if not rows:
        return Sheet(title, [], message=plugin.tr("This sheet is empty."))
    return Sheet(title, rows)


if __name__ == "__main__":
    plugin.run()
