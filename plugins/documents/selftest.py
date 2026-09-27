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

"""Documents as Markdown, checked on documents built here.

**What the fixtures are.** Each Word file is written by this script, part by
part, in the shape Word writes — the styles a Russian Word gives its headings,
numbering definitions, tracked changes, fields, notes, comments and pictures.
They are not files Word saved. Real ones can be checked by naming them:

    PYTHONPATH=<xverb>/assets/python python3 selftest.py [documents…]
"""

from __future__ import annotations

import base64
import io
import os
import struct
import sys
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import docx  # noqa: E402
from package import Package  # noqa: E402

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" ' \
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" ' \
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" ' \
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'

RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

#: A Russian Word's styles: the ids are not "Heading1", the names are.
STYLES = """<w:styles %s>
<w:style w:type="paragraph" w:default="1" w:styleId="a"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/><w:basedOn w:val="a"/></w:style>
<w:style w:type="paragraph" w:styleId="2"><w:name w:val="heading 2"/><w:basedOn w:val="a"/></w:style>
<w:style w:type="paragraph" w:styleId="myhead"><w:name w:val="Раздел"/><w:basedOn w:val="1"/></w:style>
<w:style w:type="paragraph" w:styleId="plan"><w:name w:val="Plan"/><w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="toc1"><w:name w:val="toc 1"/></w:style>
<w:style w:type="character" w:styleId="strong"><w:name w:val="Strong"/><w:rPr><w:b/></w:rPr></w:style>
</w:styles>""" % W

NUMBERING = """<w:numbering %s>
<w:abstractNum w:abstractNumId="0">
 <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/></w:lvl>
 <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="o"/></w:lvl>
</w:abstractNum>
<w:abstractNum w:abstractNumId="1">
 <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%%1."/></w:lvl>
 <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%%1.%%2."/></w:lvl>
</w:abstractNum>
<w:abstractNum w:abstractNumId="2">
 <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="russianLower"/><w:lvlText w:val="%%1)"/></w:lvl>
</w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
<w:num w:numId="3"><w:abstractNumId w:val="2"/></w:num>
<w:num w:numId="4"><w:abstractNumId w:val="1"/><w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>
</w:numbering>""" % W

FOOTNOTES = """<w:footnotes %s>
<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>
<w:footnote w:id="1"><w:p><w:r><w:footnoteRef/></w:r><w:r><w:t xml:space="preserve"> Article 8, as signed.</w:t></w:r></w:p></w:footnote>
</w:footnotes>""" % W

COMMENTS = """<w:comments %s>
<w:comment w:id="0" w:author="Anna" w:date="2026-08-26T10:00:00Z"><w:p><w:r><w:t>Too broad.</w:t></w:r></w:p></w:comment>
</w:comments>""" % W


def png(width, height):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def p(text, style=None, num=None, bold=False, extra=""):
    props = ""
    if style or num:
        props = "<w:pPr>%s%s</w:pPr>" % (
            '<w:pStyle w:val="%s"/>' % style if style else "",
            '<w:numPr><w:ilvl w:val="%d"/><w:numId w:val="%s"/></w:numPr>' % (num[1], num[0]) if num else "")
    run = '<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % ("<w:rPr><w:b/></w:rPr>" if bold else "", text) if text else ""
    return "<w:p>%s%s%s</w:p>" % (props, run, extra)


def drawing(rid, cx, cy, descr):
    return ('<w:r><w:drawing><wp:inline><wp:extent cx="%d" cy="%d"/><wp:docPr id="1" name="x" descr="%s"/>'
            '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="%s"/></pic:blipFill></pic:pic>'
            '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>' % (cx, cy, descr, rid))


BODY = "".join([
    # A table of contents, both ways Word writes one.
    '<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val="Table of Contents"/></w:docPartObj></w:sdtPr>'
    '<w:sdtContent>%s</w:sdtContent></w:sdt>' % p("Contents entry in a block"),
    p("Contents entry as a style", style="toc1"),
    p("Agreement", style="1"),
    p("Parties", style="2"),
    p("Section by a style of our own", style="myhead"),
    p("Plan by outline level", style="plan"),
    p("Stars *and* file_names [x] stay as written"),
    p("1. begins like a list and is not one"),
    p("First point", num=("1", 0)),
    p("Inner point", num=("1", 1)),
    p("Numbered one", num=("2", 0)),
    p("Numbered one-one", num=("2", 1)),
    p("Numbered one-two", num=("2", 1)),
    p("Numbered two", num=("2", 0)),
    p("Russian letter", num=("3", 0)),
    p("Russian letter again", num=("3", 0)),
    p("Numbered again from one", num=("4", 0)),
    # Formatting through a character style, and a direct one.
    '<w:p><w:r><w:rPr><w:rStyle w:val="strong"/></w:rPr><w:t>Strong</w:t></w:r>'
    '<w:r><w:t xml:space="preserve"> and </w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>slanted</w:t></w:r>'
    '<w:r><w:t xml:space="preserve"> and </w:t></w:r><w:r><w:rPr><w:strike/></w:rPr><w:t>struck</w:t></w:r></w:p>',
    # Tracked changes: the inserted word is read, the deleted one is not.
    '<w:p><w:r><w:t xml:space="preserve">The term is </w:t></w:r>'
    '<w:del w:id="10" w:author="A"><w:r><w:delText>two</w:delText></w:r></w:del>'
    '<w:ins w:id="11" w:author="A"><w:r><w:t>three</w:t></w:r></w:ins>'
    '<w:r><w:t xml:space="preserve"> years.</w:t></w:r></w:p>',
    # A field: its code is not read, its result is; a hyperlink field links.
    '<w:p><w:r><w:t xml:space="preserve">Page </w:t></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText> PAGE </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    '<w:r><w:t>7</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>',
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText xml:space="preserve"> HYPERLINK "https://example.com/terms" </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>the terms</w:t></w:r>'
    '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>',
    '<w:p><w:hyperlink r:id="rLink"><w:r><w:t>our site</w:t></w:r></w:hyperlink></w:p>',
    # A note and a comment.
    '<w:p><w:r><w:t>Work made before</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r></w:p>',
    '<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>All rights pass.</w:t></w:r><w:commentRangeEnd w:id="0"/>'
    '<w:r><w:commentReference w:id="0"/></w:r></w:p>',
    # Pictures: one the host draws, one it cannot.
    '<w:p>%s</w:p>' % drawing("rPng", 3 * 9525 * 100, 2 * 9525 * 100, "A red square"),
    '<w:p>%s</w:p>' % drawing("rEmf", 9525 * 50, 9525 * 50, "Chart"),
    # A table: head row, a span across two columns, a cell merged downwards.
    '<w:tbl><w:tblPr><w:tblLook w:firstRow="1"/></w:tblPr>'
    '<w:tr><w:tc>%s</w:tc><w:tc>%s</w:tc><w:tc>%s</w:tc></w:tr>'
    '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>%s</w:tc><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>%s</w:tc></w:tr>'
    '<w:tr><w:tc>%s%s</w:tc><w:tc>%s</w:tc><w:tc><w:tcPr><w:vMerge/></w:tcPr>%s</w:tc></w:tr>'
    '</w:tbl>' % (p("Name"), p("Role"), p("Note"), p("Both columns"), p("Down"),
                  p("Line one"), p("Line | two"), p("B"), p("")),
    '<w:sectPr/>',
])

RELS_XML = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rS" Type="%(r)s/styles" Target="styles.xml"/>
<Relationship Id="rN" Type="%(r)s/numbering" Target="numbering.xml"/>
<Relationship Id="rF" Type="%(r)s/footnotes" Target="footnotes.xml"/>
<Relationship Id="rC" Type="%(r)s/comments" Target="comments.xml"/>
<Relationship Id="rPng" Type="%(r)s/image" Target="media/image1.png"/>
<Relationship Id="rEmf" Type="%(r)s/image" Target="media/image2.emf"/>
<Relationship Id="rLink" Type="%(r)s/hyperlink" Target="https://example.com/" TargetMode="External"/>
</Relationships>""" % {"r": RELS}

ROOT_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="r1" Type="%s/officeDocument" Target="word/document.xml"/>
</Relationships>""" % RELS


def build(body, styles=STYLES, extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("word/document.xml", "<w:document %s><w:body>%s</w:body></w:document>" % (W, body))
        z.writestr("word/_rels/document.xml.rels", RELS_XML)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/numbering.xml", NUMBERING)
        z.writestr("word/footnotes.xml", FOOTNOTES)
        z.writestr("word/comments.xml", COMMENTS)
        z.writestr("word/media/image1.png", png(3, 2))
        z.writestr("word/media/image2.emf", b"\x01\x00\x00\x00")
        z.writestr("docProps/core.xml",
                   '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Agreement</dc:title>'
                   '<dc:creator>Anna</dc:creator></cp:coreProperties>')
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    raw = buffer.getvalue()
    return Package(lambda: zipfile.ZipFile(io.BytesIO(raw)))


def tr(text, values=None):
    return text.format(**(values or {}))


failed = []


def check(name, ok, detail=""):
    if ok:
        print("ok   %s" % name)
    else:
        failed.append(name)
        print("FAIL %s %s" % (name, detail))


def lines(markdown):
    return [line for line in markdown.split("\n") if line.strip()]


result = docx.convert(build(BODY), tr)
md = result.markdown
got = lines(md)

check("a table of contents is left out", "Contents entry" not in md)
check("heading by a Russian Word's style", "# Agreement" in got)
check("second level", "## Parties" in got)
check("a style based on a heading is one", "# Section by a style of our own" in got)
check("an outline level on a style", "### Plan by outline level" in got)
check("marks in the text are escaped",
      "Stars \\*and\\* file\\_names \\[x\\] stay as written" in got, repr([g for g in got if "Stars" in g]))
check("a paragraph beginning like a list stays one", "1\\. begins like a list and is not one" in got)
check("bullets, nested", "- First point" in got and "  - Inner point" in got)
check("numbers", "1. Numbered one" in got and "2. Numbered two" in got)
check("numbers of a level under a level", "1.1. Numbered one-one" in got and "1.2. Numbered one-two" in got,
      repr([g for g in got if "one-" in g]))
check("Russian letters", "а) Russian letter" in got and "б) Russian letter again" in got,
      repr([g for g in got if "Russian" in g]))
check("a restart the file asks for", "1. Numbered again from one" in got)
check("a character style, italic and struck",
      "**Strong** and *slanted* and ~~struck~~" in got, repr([g for g in got if "Strong" in g]))
check("changes accepted", "The term is three years." in got)
check("changes counted", result.changes == 2, result.changes)
check("a field is its result", "Page 7" in got)
check("a hyperlink field links", "[the terms](https://example.com/terms)" in got)
check("a hyperlink links", "[our site](https://example.com/)" in got)
check("a note is marked and written at the end",
      "Work made before\\[1\\]" in got and "\\[1\\] Article 8, as signed." in got, repr(got[-8:]))
check("a comment is marked", "All rights pass.\\[comment 1\\]" in got, repr([g for g in got if "rights" in g]))
check("a comment quotes what it is on", "> All rights pass." in got)
check("a comment says who", any(g.startswith("**Comment 1** — Anna, 2026-08-26: Too broad.") for g in got),
      repr([g for g in got if "Comment 1" in g]))
check("a picture the host draws, at its size",
      "![A red square](picture:p1)" in got and (result.pictures["p1"].width, result.pictures["p1"].height) == (300, 200))
check("the picture is read when asked", result.pictures["p1"].read() == png(3, 2))
check("a picture it cannot draw is its caption", "\\[picture: Chart\\]" in got)
check("table head", "| Name | Role | Note |" in got, repr([g for g in got if g.startswith("|")]))
check("a span and a merge", "| Both columns |  | Down |" in got, repr([g for g in got if g.startswith("|")]))
check("lines of a cell, and a bar inside one", "| Line one · Line \\| two | B |  |" in got,
      repr([g for g in got if g.startswith("|")]))
check("nothing is guessed where there are headings", not result.guessed)
check("what the file says about itself", result.meta.get("title") == "Agreement" and result.meta.get("creator") == "Anna")

# A document written by hand: bold paragraphs for headings, and no styles.
plain = "".join([
    p("1. Scope of the work", bold=True),
    p("The work is described below."),
    p("Note:", bold=True),
    p("2. Payment", bold=True),
])
guess = docx.convert(build(plain), tr)
got = lines(guess.markdown)
check("bold paragraphs stand in for headings where there are none",
      "# 1. Scope of the work" in got and "# 2. Payment" in got, repr(got))
check("but not one that ends like a sentence", "**Note:**" in got)
check("and says it guessed", guess.guessed)

# Strict Open XML names the same things differently.
strict_body = ("<w:document xmlns:w=\"http://purl.oclc.org/ooxml/wordprocessingml/main\"><w:body>"
               "<w:p><w:r><w:t>Strict text</w:t></w:r></w:p></w:body></w:document>")
buffer = io.BytesIO()
with zipfile.ZipFile(buffer, "w") as z:
    z.writestr("word/document.xml", strict_body)
raw = buffer.getvalue()
got = lines(docx.convert(Package(lambda: zipfile.ZipFile(io.BytesIO(raw))), tr).markdown)
check("strict Open XML, and no relationships at all", got == ["Strict text"], repr(got))

# Word 97–2003: a file macOS's own `textutil` wrote from HTML — styles it
# does not use, so its headings are the bold paragraphs; a typed bullet; a
# table with a bar in a cell.
import doc  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "textutil.doc")
with open(FIXTURE, "rb") as handle:
    old = doc.convert(handle.read(), tr)
got = lines(old.markdown)
check(".doc: headings", "# Договор" in got and "# Раздел" in got, repr(got))
check(".doc: bold, italic, struck, escaped",
      "Обычный **жирный** и *курсив*, file\\_name \\*star\\* и ~~зачёркнутый~~." in got, repr(got[1:2]))
check(".doc: typed bullets are a list", "- Первый" in got and "- Второй" in got)
check(".doc: table", "| Анна | Автор \\| редактор |" in got, repr([g for g in got if g.startswith("|")]))
check(".doc: an object nobody kept is nothing", "\ufffc" not in old.markdown)
try:
    doc.convert(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600, tr)
    check(".doc: a broken compound file is said so", False)
except doc.DocError as failure:
    check(".doc: a broken compound file is said so", "not a Word document" in str(failure), str(failure))

# EPUB. Two books built here: one the way converters from FB2 write them —
# titles as classed paragraphs, the headings only in toc.ncx — and one EPUB 3
# with real headings, a nav, a note aside, and a picture named `.jpg_1`.
import epub  # noqa: E402
import fb2  # noqa: E402


def zipped(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                   '<rootfiles><rootfile full-path="OPS/content.opf"/></rootfiles></container>')
        for name, data in files.items():
            z.writestr(name, data)
    raw_book = buffer.getvalue()
    return Package(lambda: zipfile.ZipFile(io.BytesIO(raw_book)))


XH = '<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title/></head><body>%s</body></html>'
OPF2 = ('<package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:title>Книга</dc:title><dc:creator>Автор</dc:creator><dc:language>ru</dc:language><meta name="cover" content="c"/></metadata>'
        '<manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/><item id="b" href="b.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="c" href="images/cover.png" media-type="image/png"/><item id="n" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>'
        '<spine toc="n"><itemref idref="a"/><itemref idref="b"/></spine></package>')
NCX = ('<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
       '<navPoint><navLabel><text>Часть первая Начало</text></navLabel><content src="a.xhtml#t1"/>'
       '<navPoint><navLabel><text>Глава 1</text></navLabel><content src="b.xhtml#t2"/></navPoint></navPoint></navMap></ncx>')
old_book = zipped({
    "OPS/content.opf": OPF2, "OPS/toc.ncx": NCX, "OPS/images/cover.png": png(3, 4),
    "OPS/a.xhtml": XH % '<span id="t1"><div class="title1"><p>Часть первая</p><p>Начало</p></div></span><p>Текст &nbsp;части <em>курсив</em>.</p>',
    "OPS/b.xhtml": XH % '<div id="t2" class="title2"><p>Глава 1</p></div><p>Абзац главы, *звёздочка*.</p><p><img src="images/cover.png"/></p>',
})
book = epub.convert(old_book, tr)
got = lines(book.markdown)
check("epub: headings from toc.ncx, at their depth", "# Часть первая Начало" in got and "## Глава 1" in got, repr(got))
check("epub: the title is not written twice", "Часть первая" not in got and "Начало" not in got and "Глава 1" not in got)
check("epub: text, entities, emphasis, escaping", "Текст части *курсив*." in got and "Абзац главы, \\*звёздочка\\*." in got,
      repr(got))
check("epub: the cover first, and not again inside", got[0] == "![](picture:p1)" and book.markdown.count("picture:p1") == 1,
      repr(got[:3]))
check("epub: title and author under it", "**Книга**" in got and "*Автор*" in got)
check("epub: the cover's bytes", book.cover is not None and book.cover.read() == png(3, 4))
check("epub: the cover alone", epub.cover(old_book, tr) == png(3, 4))

OPF3 = ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Three</dc:title></metadata>'
        '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        '<item id="c1" href="text/c1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="i" href="img/photo.jpg_1" media-type="image/png"/></manifest>'
        '<spine><itemref idref="nav"/><itemref idref="c1"/></spine></package>')
NAV = XH % ('<nav epub:type="toc"><ol><li><a href="text/c1.xhtml">Chapter One</a>'
            '<ol><li><a href="text/c1.xhtml#s">A section</a></li></ol></li></ol></nav>')
C1 = XH % ('<h1>Chapter One</h1><p>Before<a epub:type="noteref" href="#fn1">1</a> after.</p>'
           '<h2 id="s">A section</h2><ul><li>one</li><li>two<ol><li>inner</li></ol></li></ul>'
           '<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>x | y</td></tr></table>'
           '<blockquote><p>Quoted.</p></blockquote><p><img src="../img/photo.jpg_1" alt="A [photo]"/></p>'
           '<p><a href="https://example.com/">site</a></p>'
           '<aside epub:type="footnote" id="fn1"><p>The note.</p></aside>')
three = epub.convert(zipped({"OPS/content.opf": OPF3, "OPS/nav.xhtml": NAV, "OPS/text/c1.xhtml": C1,
                             "OPS/img/photo.jpg_1": png(5, 5)}), tr)
got = lines(three.markdown)
check("epub 3: the nav is the structure, not a page", "Chapter One" not in [g for g in got if not g.startswith("#")])
check("epub 3: headings, once each", got.count("# Chapter One") == 1 and got.count("## A section") == 1, repr(got))
check("epub 3: a note's mark stays and its text goes to the end", "Before\\[1\\] after." in got and got[-1] == "The note.",
      repr(got))
check("epub 3: lists", "- one" in got and "- two" in got and "  1. inner" in got, repr(got))
check("epub 3: table", "| A | B |" in got and "| 1 | x \\| y |" in got)
check("epub 3: quote", "> Quoted." in got)
check("epub 3: a picture by its bytes, not its name", "![A (photo)](picture:p1)" in got, repr([g for g in got if "picture" in g]))
check("epub 3: a link", "[site](https://example.com/)" in got)

drm = zipped({"OPS/content.opf": OPF3, "META-INF/encryption.xml":
              '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
              '<enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/></enc:EncryptedData></encryption>'})
try:
    epub.convert(drm, tr)
    check("epub: DRM is said", False)
except Exception as failure:  # noqa: BLE001
    check("epub: DRM is said", "DRM" in str(failure), str(failure))

# FictionBook, in UTF-8 and in windows-1251, bare and zipped.
FB2 = """<?xml version="1.0" encoding="%s"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info><author><first-name>Лев</first-name><last-name>Толстой</last-name></author>
<book-title>Война и мир</book-title><coverpage><image l:href="#c.png"/></coverpage><lang>ru</lang></title-info></description>
<body><section><title><p>Том первый</p></title><section><title><p>Глава I</p></title>
<p>Текст <emphasis>курсив</emphasis> file_name.<a l:href="#n1" type="note">[1]</a></p>
<poem><stanza><v>Строка один</v><v>Строка два</v></stanza></poem></section></section></body>
<body name="notes"><title><p>Примечания</p></title><section id="n1"><title><p>1</p></title><p>Сноска.</p></section></body>
<binary id="c.png" content-type="image/png">%s</binary></FictionBook>"""
for encoding in ("utf-8", "windows-1251"):
    data = (FB2 % (encoding, base64.b64encode(png(2, 3)).decode())).encode(encoding)
    book = fb2.convert(data, tr)
    got = lines(book.markdown)
    check("fb2 %s: sections are headings by depth" % encoding, "# Том первый" in got and "## Глава I" in got, repr(got))
    check("fb2 %s: text and a note's mark" % encoding, "Текст *курсив* file\\_name.\\[1\\]" in got, repr(got))
    check("fb2 %s: verses keep their lines" % encoding, "Строка один" in got and "Строка два" in got)
    check("fb2 %s: notes at the end" % encoding, "# Примечания" in got and "\\[1\\] Сноска." in got)
    check("fb2 %s: cover and who wrote it" % encoding, got[0] == "![](picture:p1)" and "*Лев Толстой*" in got
          and book.cover.read() == png(2, 3))
check("fb2: the cover alone", fb2.cover(data, tr) == png(2, 3))
buffer = io.BytesIO()
with zipfile.ZipFile(buffer, "w") as z:
    z.writestr("war.fb2", data)
check("fb2.zip: the probe says yes", fb2.in_zip(buffer.getvalue()[:4096]))
buffer = io.BytesIO()
with zipfile.ZipFile(buffer, "w") as z:
    z.writestr("photo.jpg", b"x")
check("fb2.zip: and no to any other zip", not fb2.in_zip(buffer.getvalue()[:4096]))

# OpenDocument, as LibreOffice writes it: text:h with outline levels, lists
# described by a list style, a table with a header row and a merged cell, a
# note, a comment with its author, a picture, a tracked change, and the meta.
import odt  # noqa: E402

ODF = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
       'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
       'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
       'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
       'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
       'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
       'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:dc="http://purl.org/dc/elements/1.1/" '
       'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
       'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"')
ODT_CONTENT = """<office:document-content %s><office:automatic-styles>
<style:style style:name="T1" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>
<style:style style:name="T2" style:family="text"><style:text-properties fo:font-style="italic"/></style:style>
<style:style style:name="T3" style:family="text"><style:text-properties style:text-line-through-style="solid"/></style:style>
<style:style style:name="P1" style:family="paragraph" style:parent-style-name="Heading_20_2"/>
<text:list-style style:name="L1"><text:list-level-style-bullet text:level="1" text:bullet-char="•"/>
 <text:list-level-style-bullet text:level="2" text:bullet-char="◦"/></text:list-style>
<text:list-style style:name="L2"><text:list-level-style-number text:level="1" style:num-format="1" style:num-suffix="."/>
 <text:list-level-style-number text:level="2" style:num-format="a" style:num-suffix=")"/></text:list-style>
</office:automatic-styles><office:body><office:text>
<text:tracked-changes><text:changed-region text:id="c1"><text:deletion><text:p>gone</text:p></text:deletion></text:changed-region></text:tracked-changes>
<text:sequence-decls/>
<text:table-of-content><text:index-body><text:p>Contents entry 1</text:p></text:index-body></text:table-of-content>
<text:h text:outline-level="1">Договор</text:h>
<text:p>Plain <text:span text:style-name="T1">bold</text:span>, <text:span text:style-name="T2">italic</text:span>,<text:s text:c="3"/><text:span text:style-name="T3">struck</text:span> and a <text:a xlink:href="https://example.org">link</text:a>.<text:note text:note-class="footnote"><text:note-citation>1</text:note-citation><text:note-body><text:p>The note.</text:p></text:note-body></text:note></text:p>
<text:p>A *star* and file_name<text:change text:change-id="c1"/>.<office:annotation><dc:creator>Анна</dc:creator><dc:date>2026-09-20T10:00:00</dc:date><text:p>Check this.</text:p></office:annotation></text:p>
<text:p text:style-name="P1">Heading by style</text:p>
<text:list text:style-name="L1"><text:list-item><text:p>first point</text:p><text:list><text:list-item><text:p>inner point</text:p></text:list-item></text:list></text:list-item><text:list-item><text:p>second point</text:p></text:list-item></text:list>
<text:list text:style-name="L2"><text:list-item><text:p>one</text:p><text:list><text:list-item><text:p>one-a</text:p></text:list-item><text:list-item><text:p>one-b</text:p></text:list-item></text:list></text:list-item><text:list-item><text:p>two</text:p></text:list-item></text:list>
<table:table><table:table-column table:number-columns-repeated="3"/>
<table:table-header-rows><table:table-row><table:table-cell><text:p>Name</text:p></table:table-cell><table:table-cell><text:p>Age</text:p></table:table-cell><table:table-cell><text:p>Note</text:p></table:table-cell></table:table-row></table:table-header-rows>
<table:table-row><table:table-cell table:number-columns-spanned="2"><text:p>Anna | A</text:p></table:table-cell><table:covered-table-cell/><table:table-cell><text:p>x</text:p></table:table-cell></table:table-row>
</table:table>
<text:p><draw:frame draw:name="Photo" svg:width="1in" svg:height="0.5in"><draw:image xlink:href="Pictures/a.png"/><svg:title>A photo</svg:title></draw:frame></text:p>
<text:p><text:soft-page-break/>Last.</text:p>
</office:text></office:body></office:document-content>""" % ODF
ODT_STYLES = """<office:document-styles %s><office:styles>
<style:style style:name="Heading_20_2" style:display-name="Heading 2" style:family="paragraph" style:default-outline-level="2"/>
</office:styles></office:document-styles>""" % ODF
ODT_META = """<office:document-meta %s><office:meta><dc:title>Договор аренды</dc:title>
<meta:initial-creator>Anna</meta:initial-creator><meta:creation-date>2026-09-01T09:00:00</meta:creation-date>
<meta:generator>LibreOffice/24.2$MacOSX_AARCH64 LibreOffice_project/x</meta:generator>
<meta:document-statistic meta:page-count="3"/></office:meta></office:document-meta>""" % ODF


def odt_package(content=ODT_CONTENT, encrypted=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", content)
        z.writestr("styles.xml", ODT_STYLES)
        z.writestr("meta.xml", ODT_META)
        z.writestr("Pictures/a.png", png(4, 2))
        z.writestr("META-INF/manifest.xml",
                   '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
                   '<manifest:file-entry manifest:full-path="content.xml">%s</manifest:file-entry></manifest:manifest>'
                   % ('<manifest:encryption-data/>' if encrypted else ''))
    raw_odt = buffer.getvalue()
    return Package(lambda: zipfile.ZipFile(io.BytesIO(raw_odt)))


found = odt.convert(odt_package(), tr)
got = lines(found.markdown)
check("odt: is a text document", odt.is_text(odt_package()))
check("odt: a heading with its level", "# Договор" in got, repr(got))
check("odt: a paragraph in a heading's style is a heading", "## Heading by style" in got, repr(got))
check("odt: the contents is left out", "Contents entry 1" not in found.markdown)
check("odt: the tracked deletion is not in the text", "gone" not in found.markdown and found.changes == 1)
check("odt: bold, italic, struck, spaces, link and a note's mark",
      "Plain **bold**, *italic*, ~~struck~~ and a [link](https://example.org).\\[1\\]" in got, repr(got))
check("odt: marks escaped", "A \\*star\\* and file\\_name.\\[comment 1\\]" in got, repr(got))
check("odt: bullets, nested", "- first point" in got and "  - inner point" in got and "- second point" in got, repr(got))
check("odt: numbers in their formats", "1. one" in got and "  a) one-a" in got and "  b) one-b" in got
      and "2. two" in got, repr(got))
check("odt: a header row, a merged cell, an escaped pipe",
      "| Name | Age | Note |" in got and "| Anna \\| A |  | x |" in got, repr(got))
check("odt: a picture with its size", "![A photo](picture:p1)" in got
      and found.pictures["p1"].read() == png(4, 2) and found.pictures["p1"].width == 96, repr(got))
check("odt: notes and comments at the end, with the author",
      "\\[1\\] The note." in got and "**Comment 1** — Анна, 2026-09-20: Check this." in got, repr(got[-4:]))
check("odt: meta", found.meta.get("title") == "Договор аренды" and found.meta.get("creator") == "Anna"
      and found.meta.get("Application") == "LibreOffice" and found.meta.get("Pages") == "3", repr(found.meta))
try:
    odt.convert(odt_package(encrypted=True), tr)
    check("odt: a password is said", False)
except Exception as failure:  # noqa: BLE001
    check("odt: a password is said", "password" in str(failure), str(failure))
flat = ('<office:document %s office:mimetype="application/vnd.oasis.opendocument.text">'
        '<office:body><office:text><text:h text:outline-level="2">Flat</text:h><text:p>body</text:p>'
        '</office:text></office:body></office:document>' % ODF).encode()
got = lines(odt.convert_flat(flat, tr).markdown)
check("fodt: the flat form too", got == ["## Flat", "body"], repr(got))

# Rich Text, as Word writes it in Russian: 1251 bytes in a Cyrillic font,
# heading styles in the style sheet, list labels in \listtext, a table with a
# header row, a link field, a note, a comment, a picture beside its metafile
# twin, a header and a group nobody knows — and TextEdit's, with none of that.
import rtf  # noqa: E402


def cyr(text):
    return "".join("\\'%02x" % b if b > 127 else chr(b) for b in text.encode("cp1251"))


RTF = ("{\\rtf1\\ansi\\ansicpg1251\\deff0"
       "{\\fonttbl{\\f0\\froman\\fcharset204 Times New Roman;}{\\f3\\fnil\\fcharset2 Symbol;}}"
       "{\\colortbl;\\red0\\green0\\blue0;}"
       "{\\stylesheet{\\s0 Normal;}{\\s1\\sbasedon0 heading 1;}{\\s2\\sbasedon0 heading 2;}"
       "{\\*\\cs10\\additive Default Paragraph Font;}}"
       "{\\info{\\title " + cyr("Договор") + "}{\\author Anna}{\\creatim\\yr2026\\mo9\\dy1}}"
       "{\\header \\pard Running header\\par}"
       "{\\*\\unknowndest secret}"
       "\\pard\\plain\\s1 " + cyr("Договор аренды") + "\\par"
       "\\pard\\plain " + cyr("Обычный") + " {\\b bold} {\\i italic} {\\strike struck} "
       "\\u1046\\'c6x*y_z{\\super\\chftn}{\\footnote\\pard\\plain{\\super\\chftn} The note.\\par}"
       " {\\field{\\*\\fldinst{HYPERLINK \"https://example.org\"}}{\\fldrslt{site}}} after."
       "{\\*\\atnid AN}{\\*\\atnauthor Anna}\\chatn{\\*\\annotation{\\*\\atnref 1}Check it.}\\par"
       "\\pard\\plain\\s2 Second\\par"
       "{\\listtext\\pard\\plain\\f3 \\'b7\\tab}\\pard\\plain\\ls1\\ilvl0 first point\\par"
       "{\\listtext\\pard\\plain\\f3 \\'b7\\tab}\\pard\\plain\\ls1\\ilvl1 inner point\\par"
       "{\\listtext\\pard\\plain 1.\\tab}\\pard\\plain\\ls2\\ilvl0 one\\par"
       "{\\listtext\\pard\\plain 2.\\tab}\\pard\\plain\\ls2\\ilvl0 two\\par"
       "\\trowd\\trhdr\\cellx1000\\cellx2000\\pard\\intbl Name\\cell Age\\cell\\row"
       "\\trowd\\cellx1000\\cellx2000\\pard\\intbl Anna | A\\cell 30\\par line two\\cell\\row"
       "\\pard\\plain {\\*\\shppict{\\pict\\pngblip\\picwgoal1500\\pichgoal750 " + png(4, 2).hex() + "}}"
       "{\\nonshppict{\\pict\\wmetafile8 0102}}After the picture.\\par"
       "}").encode("latin-1")
found = rtf.convert(RTF, tr)
got = lines(found.markdown)
check("rtf: a heading style, in 1251 bytes", "# Договор аренды" in got, repr(got))
check("rtf: a second level", "## Second" in got)
check("rtf: a header, and a group nobody knows, are not text",
      "Running header" not in found.markdown and "secret" not in found.markdown)
check("rtf: bold, italic, struck, \\u with its fallback skipped, escaping, a note's mark, a link, a comment",
      "Обычный **bold** *italic* ~~struck~~ Жx\\*y\\_z\\[1\\] [site](https://example.org) after.\\[comment 1\\]" in got,
      repr(got))
check("rtf: a bullet from the Symbol font, nested", "- first point" in got and "  - inner point" in got, repr(got))
check("rtf: numbers as written", "1. one" in got and "2. two" in got, repr(got))
check("rtf: a table with its header row and a cell of two lines",
      "| Name | Age |" in got and "| Anna \\| A | 30 · line two |" in got, repr(got))
check("rtf: the PNG, at its size, once, after its paragraph",
      "![](picture:p1)" in got and len(found.pictures) == 1 and found.pictures["p1"].read() == png(4, 2)
      and found.pictures["p1"].width == 100 and got.index("![](picture:p1)") > got.index("After the picture."),
      repr(got))
check("rtf: notes and comments at the end, with the author",
      "\\[1\\] The note." in got and "**Comment 1** — Anna: Check it." in got, repr(got[-4:]))
check("rtf: what the file says about itself", found.meta.get("title") == "Договор" and found.meta.get("creator") == "Anna",
      repr(found.meta))
COCOA = ("{\\rtf1\\ansi\\ansicpg1252\\cocoartf2870{\\fonttbl\\f0\\froman\\fcharset0 Times-Bold;\\f1\\froman\\fcharset0 Times-Roman;}"
         "\\pard\\f0\\b\\fs48 \\uc0\\u1043 \\u1083 \\u1072 \\u1074 \\u1072 \\\n"
         "\\pard\\f1\\b0\\fs24 Body text here.\\\n"
         "{\\listtext\t1\t}\\ls2\\ilvl0 first\\\n}").encode("latin-1")
got = lines(rtf.convert(COCOA, tr).markdown)
check("rtf: TextEdit's, headings guessed from bold, a bare number", got == ["# Глава", "Body text here.", "1. first"],
      repr(got))

# Word 97–2003 files saved by Word itself: numbered lists at several levels
# (Apache POI's Lists.doc) and a comment with its author (Apache Tika's).
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "poi-Lists.doc"), "rb") as handle:
    got = lines(doc.convert(handle.read(), tr).markdown)
check(".doc: numbers counted as Word counts them",
      "1. Ordered list 1" in got and "3. OL 3" in got and "  2.1. OL 2.1" in got
      and "      2.2.2.1. OL 2.2.2.1" in got, repr([g for g in got if "OL" in g]))
check(".doc: bullets still bullets, nested", "- UL 2" in got and "  - ML 2:1" in got and "    - ML 3:1" in got, repr(got))
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "tika-testComment.doc"), "rb") as handle:
    found = doc.convert(handle.read(), tr)
got = lines(found.markdown)
check(".doc: one comment, marked, with who wrote it",
      found.comments == 1 and "**Comment 1** — Michael McCandless: Here is a comment" in got, repr(got))

for path in sys.argv[1:]:
    with open(path, "rb") as handle:
        raw_file = handle.read()
    if path.lower().endswith(".epub"):
        found = epub.convert(Package(lambda: zipfile.ZipFile(io.BytesIO(raw_file))), tr)
    elif path.lower().endswith(".fb2"):
        found = fb2.convert(raw_file, tr)
    elif raw_file.startswith(b"{\\rtf"):
        found = rtf.convert(raw_file, tr)
    elif path.lower().endswith(".fodt"):
        found = odt.convert_flat(raw_file, tr)
    elif raw_file.startswith(b"PK") and path.lower().endswith((".odt", ".ott")):
        found = odt.convert(Package(lambda: zipfile.ZipFile(io.BytesIO(raw_file))), tr)
    elif raw_file.startswith(b"PK"):
        found = docx.convert(Package(lambda: zipfile.ZipFile(io.BytesIO(raw_file))), tr)
    else:
        found = doc.convert(raw_file, tr)
    heads = [g for g in lines(found.markdown) if g.startswith("#")]
    print("%s: %d words, %d heading(s)%s, %d picture(s), %d change(s), %d comment(s)" % (
        os.path.basename(path), found.words, len(heads), " (guessed)" if found.guessed else "",
        len(found.pictures), found.changes, found.comments))

sys.exit(1 if failed else 0)
