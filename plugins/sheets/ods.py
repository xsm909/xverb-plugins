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

"""An OpenDocument spreadsheet — `.ods`, `.ots` — read with the standard
library.

All the sheets live in one part, `content.xml`, so they are read together in
one pass. The format says how many times a row or a cell repeats instead of
writing it out, which is how an empty sheet claims a million rows: repeats of
nothing are counted and only become rows if something follows them.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import List, Tuple
from xml.etree import ElementTree as ET

import numfmt

TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
STYLE = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
FO = "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"

_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?")

#: A row or cell repeated more than this many times with nothing in it is the
#: rest of an empty sheet, not data.
_ENOUGH = 100000


def is_spreadsheet(archive: zipfile.ZipFile) -> bool:
    try:
        kind = archive.read("mimetype").decode("ascii", "replace")
    except KeyError:
        return False
    return kind.startswith("application/vnd.oasis.opendocument.spreadsheet")


def read(raw: bytes, language: str = "en", max_rows: int = 0) -> List[Tuple[str, List[list], dict]]:
    """Every sheet, as (name, rows, extras) — extras being the joined cells,
    the notes, and the rows and columns the sheet hides."""
    archive = zipfile.ZipFile(io.BytesIO(raw))
    comma = language.split("-")[0] in numfmt.COMMA_LANGUAGES
    sheets: List[Tuple[str, List[list], dict]] = []
    t_table = "{%s}table" % TABLE
    t_row = "{%s}table-row" % TABLE
    t_cell = "{%s}table-cell" % TABLE
    t_covered = "{%s}covered-table-cell" % TABLE
    t_p = "{%s}p" % TEXT
    a_name = "{%s}name" % TABLE
    a_rows = "{%s}number-rows-repeated" % TABLE
    a_cols = "{%s}number-columns-repeated" % TABLE
    t_style = "{%s}style" % STYLE
    t_text_props = "{%s}text-properties" % STYLE
    a_style_name = "{%s}style-name" % TABLE
    a_visibility = "{%s}visibility" % TABLE
    a_span_cols = "{%s}number-columns-spanned" % TABLE
    a_span_rows = "{%s}number-rows-spanned" % TABLE
    t_column = "{%s}table-column" % TABLE
    t_note = "{%s}annotation" % OFFICE
    bold_styles = set()
    extras: dict = {}
    column_at = 0

    with archive.open("content.xml") as part:
        rows: List[list] = []
        row: list = []
        blank_rows = 0
        blank_cells = 0
        depth = 0
        for event, element in ET.iterparse(part, events=("start", "end")):
            tag = element.tag
            if event == "start":
                if tag == t_table:
                    depth += 1
                    if depth == 1:
                        rows = []
                        blank_rows = 0
                        column_at = 0
                        extras = {"merges": [], "notes": {}, "hidden_rows": [],
                                  "hidden_columns": []}
                elif tag == t_row:
                    row = []
                    blank_cells = 0
                continue

            if tag == t_style:
                # Automatic styles come before the tables: a cell style whose
                # text is bold is how a sheet marks a heading or a total.
                for props in element.iter(t_text_props):
                    if props.get("{%s}font-weight" % FO) in ("bold", "700", "800", "900"):
                        bold_styles.add(element.get("{%s}name" % STYLE))
                continue
            if tag == t_column and depth == 1:
                repeat = min(int(element.get(a_cols) or 1), 1024)
                if element.get(a_visibility) in ("collapse", "filter"):
                    extras["hidden_columns"].extend(range(column_at, column_at + repeat))
                column_at += repeat
                element.clear()
                continue
            if tag in (t_cell, t_covered):
                repeat = int(element.get(a_cols) or 1)
                row_at = len(rows) + blank_rows
                col_at = len(row) + blank_cells
                if tag == t_cell and depth == 1:
                    spans = (int(element.get(a_span_rows) or 1),
                             int(element.get(a_span_cols) or 1))
                    if spans != (1, 1):
                        extras["merges"].append(
                            (row_at, col_at, row_at + spans[0] - 1, col_at + spans[1] - 1))
                    note = element.find(t_note)
                    if note is not None:
                        said = "\n".join(_flatten(p) for p in note.iter(t_p)).strip()
                        if said:
                            extras["notes"][(row_at, col_at)] = said
                value = _value(element, comma, t_p) if tag == t_cell else None
                if value not in (None, "") and element.get(a_style_name) in bold_styles:
                    if not isinstance(value, dict):
                        value = {"v": value, "r": "strong"}
                    elif "r" not in value:
                        value = dict(value, r="strong")
                if value is None or value == "":
                    blank_cells += repeat
                else:
                    if blank_cells:
                        row.extend([None] * blank_cells)
                        blank_cells = 0
                    row.extend([value] * min(repeat, 1024))
                element.clear()
            elif tag == t_row:
                repeat = int(element.get(a_rows) or 1)
                if depth == 1 and element.get(a_visibility) in ("collapse", "filter"):
                    first = len(rows) + blank_rows
                    extras["hidden_rows"].extend(range(first, first + min(repeat, 1024)))
                if not row:
                    blank_rows += repeat
                else:
                    if blank_rows:
                        rows.extend([] for _ in range(min(blank_rows, _ENOUGH)))
                        blank_rows = 0
                    for _ in range(min(repeat, _ENOUGH)):
                        rows.append(list(row))
                if max_rows and len(rows) >= max_rows:
                    del rows[max_rows:]
                element.clear()
            elif tag == t_table:
                depth -= 1
                if depth == 0:
                    last = len(rows)
                    extras["hidden_rows"] = [r for r in extras["hidden_rows"] if r < last]
                    sheets.append((element.get(a_name) or "Sheet%d" % (len(sheets) + 1),
                                   rows, extras))
                    element.clear()
    return sheets


def _value(cell, comma: bool, t_p: str):
    kind = cell.get("{%s}value-type" % OFFICE)
    if kind in ("float", "percentage", "currency"):
        raw = cell.get("{%s}value" % OFFICE)
        shown = _text(cell, t_p)
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return shown
        plain = int(number) if number.is_integer() and abs(number) < 1e15 else number
        # The file carries the text as it was shown when it was saved — the
        # format already applied. That is the reading to keep.
        if shown and shown != str(plain):
            return {"v": plain, "t": shown}
        return plain
    if kind == "date":
        raw = cell.get("{%s}date-value" % OFFICE) or ""
        text = raw.replace("T", " ").split(".")[0]
        if text.endswith(" 00:00:00"):
            text = text[:-9]
        return {"v": raw, "t": text}
    if kind == "time":
        raw = cell.get("{%s}time-value" % OFFICE) or ""
        found = _DURATION.fullmatch(raw)
        if found:
            days, hours, minutes, seconds = found.groups()
            total = (int(days or 0) * 24 + int(hours or 0)) * 3600 + \
                int(minutes or 0) * 60 + float(seconds or 0)
            text = "%d:%02d:%02d" % (total // 3600, total // 60 % 60, total % 60)
            return {"v": raw, "t": text}
        return raw
    if kind == "boolean":
        return (cell.get("{%s}boolean-value" % OFFICE) or "") == "true"
    return _text(cell, t_p)


def _text(cell, t_p: str) -> str:
    """The paragraphs of a cell, a line each; `text:s` is a run of spaces
    and `text:tab` a tab, which is how the format keeps them."""
    # The cell's own paragraphs only: a note hangs inside the cell too, with
    # paragraphs of its own, and it is not what the cell says.
    lines = []
    for paragraph in cell.findall(t_p):
        lines.append(_flatten(paragraph))
    return "\n".join(lines)


def _flatten(element) -> str:
    out = [element.text or ""]
    for child in element:
        name = child.tag.rsplit("}", 1)[-1]
        if name == "s":
            out.append(" " * int(child.get("{%s}c" % TEXT) or 1))
        elif name == "tab":
            out.append("\t")
        elif name == "line-break":
            out.append("\n")
        else:
            out.append(_flatten(child))
        out.append(child.tail or "")
    return "".join(out)
