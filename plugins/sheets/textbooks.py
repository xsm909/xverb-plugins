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

"""The two things called ".xls" that are not Excel's binary format at all.

**A web page.** A great many programs — accounting systems, banks, 1C — make
an "Excel file" by writing an HTML table and giving it an .xls name, and Excel
opens it without a word. Each `<table>` is a sheet here.

**Excel 2003 XML** (SpreadsheetML): one XML document, `<Workbook>` holding
`<Worksheet>`s of `<Row>`s of `<Cell>`s, with `ss:Index` to skip ahead and
`ss:MergeAcross` for a cell that spans.

Both are text, and both are read with the standard library.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple
from xml.parsers import expat

import numfmt

SS = "urn:schemas-microsoft-com:office:spreadsheet"


def looks_like_html(head: bytes) -> bool:
    lower = head[:4096].lower()
    return b"<table" in lower or b"<html" in lower


def looks_like_spreadsheetml(head: bytes) -> bool:
    """Excel 2003 XML. Asked before [looks_like_html], because its sheets are
    `<Table>` elements and a test for "<table" alone takes it for a page."""
    head = head[:8192]
    return b"urn:schemas-microsoft-com:office:spreadsheet" in head and \
        b"<workbook" in head.lower() and b"<html" not in head.lower()


def decode(raw: bytes) -> str:
    """The text of a web page or an XML file, in whatever it says it is in —
    and Windows-1251 when it says nothing and is not UTF-8, which is what the
    programs that write these most often use here."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    head = raw[:4096].decode("latin-1").lower()
    said = re.search(r'charset\s*=\s*["\']?([\w-]+)|encoding\s*=\s*["\']([\w-]+)', head)
    if said:
        name = said.group(1) or said.group(2)
        try:
            return raw.decode(name, "replace")
        except LookupError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", "replace")


# -- a web page ------------------------------------------------------------


class _Tables(HTMLParser):
    """Every `<table>` as rows of cell text. A cell spanning columns is given
    its place and the ones it covers left empty, so the columns stay columns."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: List[Tuple[str, List[list]]] = []
        self._depth = 0
        self._rows: Optional[List[list]] = None
        self._row: Optional[list] = None
        self._cell: Optional[List[str]] = None
        self._span = 1
        self._bold = False
        self._strong = 0
        self._caption: Optional[List[str]] = None
        self._title = ""

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._rows = []
                self._title = ""
        elif self._depth != 1:
            return
        elif tag == "caption":
            self._caption = []
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            self._bold = tag == "th"
            try:
                self._span = max(1, min(int(dict(attrs).get("colspan") or 1), 256))
            except ValueError:
                self._span = 1
        elif tag in ("b", "strong") and self._cell is not None:
            self._strong += 1
            self._bold = True
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag):
        if tag == "table":
            if self._depth == 1 and self._rows is not None:
                # No caption: the name is the reader's to give, in their
                # own language.
                self.tables.append((self._title or None, self._rows))
                self._rows = None
            self._depth = max(0, self._depth - 1)
        elif self._depth != 1:
            return
        elif tag == "caption" and self._caption is not None:
            self._title = " ".join("".join(self._caption).split())
            self._caption = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = "\n".join(" ".join(line.split()) for line in "".join(self._cell).split("\n")).strip()
            value = _typed(text)
            if value != "" and (self._bold or self._strong):
                value = {"v": value, "r": "strong"}
            self._row.append(value if value != "" else None)
            self._row.extend([None] * (self._span - 1))
            self._cell = None
            self._strong = 0
        elif tag in ("b", "strong") and self._strong:
            self._strong -= 1
        elif tag == "tr" and self._row is not None and self._rows is not None:
            while self._row and self._row[-1] is None:
                self._row.pop()
            self._rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        elif self._caption is not None:
            self._caption.append(data)


def _typed(text: str):
    """A number written as a number is a number — with a comma or a point,
    with spaces between the thousands. Anything else stays as it was written."""
    if not text or len(text) > 40:
        return text
    bare = text.replace(" ", "").replace(" ", "").replace(" ", "")
    if re.fullmatch(r"[+-]?\d+", bare):
        return int(bare) if len(bare) < 16 else text
    if re.fullmatch(r"[+-]?\d+[.,]\d+", bare):
        try:
            number = float(bare.replace(",", "."))
        except ValueError:
            return text
        return {"v": number, "t": text}
    return text


def read_html(raw: bytes) -> List[Tuple[str, List[list]]]:
    parser = _Tables()
    parser.feed(decode(raw))
    parser.close()
    return [(name, rows) for name, rows in parser.tables if rows]


# -- Excel 2003 XML --------------------------------------------------------


def read_spreadsheetml(raw: bytes, language: str = "en") -> List[Tuple[str, List[list]]]:
    """Every `<Worksheet>` as (name, rows)."""
    comma = language.split("-")[0] in numfmt.COMMA_LANGUAGES
    sheets: List[Tuple[str, List[list]]] = []
    styles_bold = set()
    styles_format = {}
    state = {"sheet": None, "rows": None, "row": None, "column": 0,
             "type": None, "style": None, "text": None, "styleid": None,
             "merge": 0}

    def local(name: str) -> str:
        return name.rsplit(" ", 1)[-1].rsplit(":", 1)[-1]

    def attr(attrs, name):
        for key, value in attrs.items():
            if local(key) == name:
                return value
        return None

    def start(name, attrs):
        tag = local(name)
        if tag == "Style":
            state["styleid"] = attr(attrs, "ID")
        elif tag == "Font" and state["styleid"] and attr(attrs, "Bold") == "1":
            styles_bold.add(state["styleid"])
        elif tag == "NumberFormat" and state["styleid"]:
            styles_format[state["styleid"]] = attr(attrs, "Format") or ""
        elif tag == "Worksheet":
            state["sheet"] = attr(attrs, "Name") or "Sheet%d" % (len(sheets) + 1)
            state["rows"] = []
        elif tag == "Row" and state["rows"] is not None:
            at = attr(attrs, "Index")
            if at and at.isdigit():
                while len(state["rows"]) < int(at) - 1:
                    state["rows"].append([])
            state["row"] = []
            state["column"] = 0
        elif tag == "Cell" and state["row"] is not None:
            at = attr(attrs, "Index")
            if at and at.isdigit():
                state["column"] = int(at) - 1
            state["style"] = attr(attrs, "StyleID")
            merge = attr(attrs, "MergeAcross")
            state["merge"] = int(merge) if merge and merge.isdigit() else 0
            state["type"] = None
        elif tag == "Data":
            state["type"] = attr(attrs, "Type") or "String"
            state["text"] = []

    def end(name):
        tag = local(name)
        if tag == "Style":
            state["styleid"] = None
        elif tag == "Data" and state["text"] is not None:
            state["value"] = "".join(state["text"])
            state["text"] = None
        elif tag == "Cell" and state["row"] is not None:
            value = _xml_value(state.get("value"), state["type"], state["style"],
                               styles_format, comma, language)
            state["value"] = None
            if value not in (None, ""):
                if state["style"] in styles_bold:
                    value = dict(value, r="strong") if isinstance(value, dict) \
                        else {"v": value, "r": "strong"}
                row = state["row"]
                while len(row) < state["column"]:
                    row.append(None)
                row.append(value)
            state["column"] = max(state["column"] + 1 + state["merge"], len(state["row"]))
        elif tag == "Row" and state["rows"] is not None and state["row"] is not None:
            state["rows"].append(state["row"])
            state["row"] = None
        elif tag == "Worksheet" and state["rows"] is not None:
            rows = state["rows"]
            while rows and not rows[-1]:
                rows.pop()
            sheets.append((state["sheet"], rows))
            state["rows"] = None

    def text(data):
        if state["text"] is not None:
            state["text"].append(data)

    parser = expat.ParserCreate()
    parser.buffer_text = True
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.Parse(raw, True)
    return sheets


def _xml_value(raw: Optional[str], kind: Optional[str], style, formats,
               comma: bool, language: str):
    if raw is None:
        return None
    if kind == "Number":
        try:
            number = float(raw)
        except ValueError:
            return raw
        plain = int(number) if number.is_integer() and abs(number) < 1e15 else number
        code = formats.get(style or "")
        if code:
            shown = numfmt.show(number, numfmt.Format(_named(code)), language)
            if shown != str(plain):
                return {"v": plain, "t": shown}
        if comma and not isinstance(plain, int):
            return {"v": plain, "t": numfmt.general(plain, True)}
        return plain
    if kind == "DateTime":
        text = raw.replace("T", " ").split(".")[0]
        if text.endswith(" 00:00:00"):
            text = text[:-9]
        return {"v": text, "t": text}
    if kind == "Boolean":
        return raw.strip() in ("1", "true")
    if kind == "Error":
        return {"v": raw, "t": raw, "r": "error"}
    return html.unescape(raw)


def _named(code: str) -> str:
    """SpreadsheetML names some formats rather than writing them out."""
    return {
        "General Number": "General",
        "Fixed": "0.00",
        "Standard": "#,##0.00",
        "Percent": "0.00%",
        "Scientific": "0.00E+00",
        "Short Date": "yyyy-mm-dd",
        "Medium Date": "yyyy-mm-dd",
        "Long Date": "yyyy-mm-dd",
        "Short Time": "h:mm",
        "Medium Time": "h:mm:ss",
        "Long Time": "h:mm:ss",
    }.get(code, code)
