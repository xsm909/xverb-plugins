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

"""Checks the readers, on workbooks made up here and on real ones.

    python3 selftest.py                 # the checks that need no file
    python3 selftest.py <folder>        # and every workbook under there too

The made-up files are built byte by byte and record by record, so what should
come out of them is known before they go in. The hardest of them is the
string table of an .xls cut by a CONTINUE record in the middle of a string,
with the second half in the other width.

The corpus run asks the weaker question — nothing raises, every sheet reads,
and nothing takes longer than the host waits.
"""

from __future__ import annotations

import io
import os
import struct
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "xverb-dev", "assets", "python"))

import numfmt  # noqa: E402
import ods  # noqa: E402
import xls  # noqa: E402
import xlsx  # noqa: E402

failures = 0


def check(what: str, got, wanted) -> None:
    global failures
    if got != wanted:
        failures += 1
        print("FAIL %s\n  got    %r\n  wanted %r" % (what, got, wanted))


def formats() -> None:
    cases = [
        ("General", 0.1 + 0.2, "en", "0.3"),
        ("General", 0.1 + 0.2, "ru", "0,3"),
        ("0.00", 2.675, "en", "2.68"),
        ('"Total: "0.0', 3.25, "en", "Total: 3.3"),
        ("#,##0.00", 1234567.891, "ru", "1 234 567,89"),
        ('#,##0.00 "₽"', 1500, "ru", "1 500,00 ₽"),
        ("[$€-407] #,##0.00", 12.5, "de", "€ 12,50"),
        ("0%", 0.256, "en", "26%"),
        ("dd.mm.yyyy", 46266, "ru", "2026-09-01"),
        ("yyyy-mm-dd h:mm", 46266.5, "en", "2026-09-01 12:00"),
        ("h:mm", 0.75, "en", "18:00"),
        ("[h]:mm:ss", 1.5, "en", "36:00:00"),
        ("mmm-yy", 46266, "en", "2026-09-01"),
        ("yyyy-mm-dd", 1, "en", "1900-01-01"),
        # Day 60 is the 29th of February 1900, which never was; day 61 is
        # the first of March either way.
        ("yyyy-mm-dd", 61, "en", "1900-03-01"),
        # The parts of a format: below nought writes its own sign, nought can
        # be a word, and a part may be only a word.
        ("#,##0;(#,##0)", -1234, "en", "(1\u202f234)"),
        ('0.00;-0.00;"—"', 0, "en", "—"),
        ('0.00 "₽";-0.00 "₽"', -12, "ru", "-12,00 ₽"),
        ("# ?/?", 1.75, "en", "1 3/4"),
        ("# ??/??", 3.14159, "en", "3 14/99"),
        ("?/16", 0.3, "en", "5/16"),
        ("General", -3.5, "ru", "-3,5"),
    ]
    for code, value, language, wanted in cases:
        check("format %r of %r" % (code, value),
              numfmt.show(value, numfmt.Format(code), language), wanted)
    check("1904 dates", numfmt.show(0, numfmt.Format("yyyy-mm-dd"), "en", True),
          "1904-01-01")


def rk_numbers() -> None:
    check("rk integer", xls._rk((1234 << 2) | 2), 1234.0)
    check("rk negative", xls._rk(((-5 & 0x3FFFFFFF) << 2) | 2), -5.0)
    check("rk hundredths", xls._rk((1234 << 2) | 3), 12.34)
    top = struct.unpack("<Q", struct.pack("<d", 1.5))[0] >> 32
    check("rk double", xls._rk(top & 0xFFFFFFFC), 1.5)


def string_table() -> None:
    """Three strings, the second cut by a CONTINUE in the middle — one byte a
    character before the join and two after it, the way Excel writes a
    string that turns Cyrillic half-way."""
    def plain(text):  # compressed, one byte a character
        return struct.pack("<HB", len(text), 0) + text.encode("latin-1")

    second = "abcПривет"
    first_part = plain("one") + struct.pack("<HB", len(second), 0) + b"abc"
    continued = bytes([1]) + "Привет".encode("utf-16-le") + plain("three")
    pieces = xls._Pieces([first_part, continued])
    got = [pieces.string(), pieces.string(), pieces.string()]
    check("string table across a CONTINUE", got, ["one", second, "three"])


def made_xlsx() -> bytes:
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("xl/workbook.xml",
                   '<workbook xmlns="%s" xmlns:r="%s"><sheets>'
                   '<sheet name="Цены" sheetId="1" r:id="rId1"/>'
                   '<sheet name="Пусто" sheetId="2" r:id="rId2"/>'
                   '</sheets></workbook>' % (main, rel))
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/>'
                   '</Relationships>')
        z.writestr("xl/sharedStrings.xml",
                   '<sst xmlns="%s"><si><t>Товар</t></si><si><t>Цена</t></si>'
                   '<si><r><t>Ков</t></r><r><t>рик</t></r></si></sst>' % main)
        z.writestr("xl/styles.xml",
                   '<styleSheet xmlns="%s"><numFmts><numFmt numFmtId="164" formatCode="dd.mm.yyyy"/>'
                   '</numFmts><fonts><font/><font><b/></font></fonts>'
                   '<cellXfs><xf numFmtId="0"/><xf numFmtId="164"/><xf numFmtId="4" fontId="1"/>'
                   '</cellXfs></styleSheet>' % main)
        z.writestr("xl/worksheets/sheet1.xml",
                   '<worksheet xmlns="%s"><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                   '<row r="3"><c r="A3" t="s"><v>2</v></c><c r="C3" s="2"><v>1250.5</v></c>'
                   '<c r="D3" s="1"><v>46266</v></c><c r="E3" t="b"><v>1</v></c>'
                   '<c r="F3" t="inlineStr"><is><t>сам</t></is></c><c r="G3" t="e"><v>#DIV/0!</v></c></row>'
                   '</sheetData></worksheet>' % main)
        z.writestr("xl/worksheets/sheet2.xml",
                   '<worksheet xmlns="%s"><sheetData/></worksheet>' % main)
    return out.getvalue()


def workbooks() -> None:
    book = xlsx.Workbook(made_xlsx(), "ru")
    check("xlsx titles", book.titles, ["Цены", "Пусто"])
    rows = book.rows(0)
    check("xlsx header", rows[0], ["Товар", "Цена"])
    check("xlsx gap row", rows[1], [])
    check("xlsx cells", rows[2], [
        "Коврик", None,
        {"v": 1250.5, "t": "1 250,50", "r": "strong"},
        {"v": "2026-09-01", "t": "2026-09-01"},
        True,
        "сам",
        {"v": "#DIV/0!", "t": "#DIV/0!", "r": "error"},
    ])
    check("xlsx empty sheet", book.rows(1), [])

    content = (
        '<office:document-content xmlns:office="%s" xmlns:table="%s" xmlns:text="%s" '
        'xmlns:style="%s" xmlns:fo="%s">'
        '<office:automatic-styles><style:style style:name="ce1" style:family="table-cell">'
        '<style:text-properties fo:font-weight="bold"/></style:style></office:automatic-styles>'
        '<office:body><office:spreadsheet><table:table table:name="A">'
        '<table:table-row><table:table-cell table:style-name="ce1" office:value-type="float" office:value="2">'
        '<text:p>2</text:p></table:table-cell><table:table-cell table:number-columns-repeated="3"/>'
        '<table:table-cell office:value-type="string"><text:p>a<text:s text:c="2"/>b</text:p>'
        '</table:table-cell></table:table-row>'
        '<table:table-row table:number-rows-repeated="1048575"><table:table-cell '
        'table:number-columns-repeated="1024"/></table:table-row>'
        '</table:table></office:spreadsheet></office:body></office:document-content>'
    ) % (ods.OFFICE, ods.TABLE, ods.TEXT, ods.STYLE, ods.FO)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        z.writestr("content.xml", content)
    sheets = ods.read(out.getvalue(), "en")
    check("ods, the empty million rows not made, bold kept",
          sheets, [("A", [[{"v": 2, "r": "strong"}, None, None, None, "a  b"]])])


def corpus(folder: str) -> None:
    import main
    global failures
    for root, _, names in os.walk(folder):
        for name in sorted(names):
            if name.rsplit(".", 1)[-1].lower() not in main.EXTENSIONS:
                continue
            path = os.path.join(root, name)
            raw = open(path, "rb").read()
            started = time.time()
            try:
                titles, load = main._open(raw, "ru", 0)
                counts = [len(load(i).rows) for i in range(len(titles))]
            except Exception as failure:  # noqa: BLE001
                failures += 1
                print("FAIL %s: %s" % (name, failure))
                continue
            spent = time.time() - started
            print("  %-50s %d sheet(s), rows %s, %.2fs" % (name[:50], len(titles), counts, spent))
            if spent > 50:
                failures += 1
                print("FAIL %s: slower than the host waits" % name)


if __name__ == "__main__":
    formats()
    rk_numbers()
    string_table()
    workbooks()
    if len(sys.argv) > 1:
        corpus(sys.argv[1])
    print("%d failure(s)" % failures)
    sys.exit(1 if failures else 0)
