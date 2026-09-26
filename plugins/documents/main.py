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

"""Documents as Markdown — a Word file read as a document, not laid out as pages.

The same idea as PDF as Markdown, for files that are text to begin with: F3
gives the headings, the lists, the tables, the notes and the pictures, and
none of the fonts, the page breaks, the headers and the footers. The page as
it was set is one Enter away, in whatever this machine opens the file with.

The work is next door: `docx` for Word 2007 and later, `doc` and `compound`
for Word 97–2003, `mdtext` for the rules every
converter writes its Markdown by, `package` for a zip that is read again when
a picture is scrolled to.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xverb import Plugin, error, fact, fact_group, facts  # noqa: E402
from xverb.fs import local_path  # noqa: E402

import xml.etree.ElementTree as ET  # noqa: E402

import doc  # noqa: E402
import docx  # noqa: E402
import epub  # noqa: E402
import fb2  # noqa: E402
from compound import Compound  # noqa: E402
from package import Package, Unreadable  # noqa: E402

plugin = Plugin("org.xverb.documents", "Documents as Markdown")
tr = plugin.tr

#: A file read through another plugin's transport arrives whole; one on this
#: disk is opened where it lies and only the parts wanted are read.
MAX_BYTES = 512 << 20

WORD = ["docx", "docm", "dotx", "dotm", "doc", "dot"]
BOOKS = ["epub", "fb2"]

#: Word 97–2003 files, and a password-protected .docx too, are compound files.
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _head(url: str, count: int) -> bytes:
    path = local_path(url)
    if path:
        with open(path, "rb") as handle:
            return handle.read(count)
    return plugin.read_file(url, max_bytes=count)


def _whole(url: str) -> bytes:
    path = local_path(url)
    if path:
        with open(path, "rb") as handle:
            return handle.read(MAX_BYTES)
    return plugin.read_file(url, max_bytes=MAX_BYTES)


def _read(url: str):
    """The document as a Result, whichever Word wrote it. **Decided by what
    the file is, not by its name**: a .doc that is really a .docx is common,
    and the other way round happens."""
    head = _head(url, 8)
    if head.startswith(b"PK"):
        package = Package.open(plugin, url, MAX_BYTES)
        try:
            return docx.convert(package, tr)
        finally:
            package.close()
    if head == OLE:
        raw = _whole(url)
        compound = Compound(raw)
        if compound.has("EncryptedPackage") or compound.has("EncryptionInfo"):
            raise Unreadable(tr(
                "This document is protected with a password. Press Enter to "
                "open it in the application that can ask for one."))
        if compound.has("WordDocument"):
            try:
                return doc.convert(raw, tr)
            except doc.DocError as failure:
                raise Unreadable(str(failure))
    if head.startswith(b"{\\rtf"):
        raise Unreadable(tr(
            "This is a Rich Text file with a Word name. This viewer does not "
            "read RTF yet. Press Enter to open it in the system's own application."))
    raise Unreadable(tr("This is not a Word document, whatever its name says."))


def _book(url: str, only_cover: bool = False):
    """An EPUB or a FictionBook, decided by what the file is — or, asked
    for [only_cover], just the bytes of its cover."""
    head = _head(url, 4096)
    if head.startswith(b"PK"):
        package = Package.open(plugin, url, MAX_BYTES)
        try:
            names = [n for n in package.zip.namelist() if n.lower().endswith(".fb2")]
            if not package.has("META-INF/container.xml") and names:
                data = package.zip.read(names[0])
            elif only_cover:
                return epub.cover(package, tr)
            else:
                return epub.convert(package, tr)
        finally:
            package.close()
    else:
        data = _whole(url)
    try:
        return fb2.cover(data, tr) if only_cover else fb2.convert(data, tr)
    except ET.ParseError as failure:
        raise Unreadable(tr("This book could not be read: {error}", {"error": failure}))


def _answer(text: str, pictures: dict, cut: bool) -> dict:
    """The reading, its pictures fetched when they are scrolled to — which
    is plugin API level 2, and why the manifest asks for it."""
    return plugin.document(text, pictures, truncated=cut)


def _cut(text: str) -> tuple:
    limit = int(plugin.setting("maxCharacters", 0) or 0)
    if limit <= 0 or len(text) <= limit:
        return text, False
    end = text.rfind("\n", 0, limit)
    return text[: end if end > 0 else limit], True


def _preamble(result) -> str:
    """Said before the document only when there is something to say."""
    remarks = []
    if result.changes:
        remarks.append(tr(
            "The document has {n} tracked change(s) nobody has accepted yet. "
            "It is shown as it would read with all of them accepted.",
            {"n": result.changes}))
    if not remarks:
        return ""
    return "> " + "\n> ".join(remarks) + "\n\n"


@plugin.viewer(
    "documents.word",
    "Document",
    extensions=WORD,
    # Above the archive viewer, which claims a .docx as the zip it is; that
    # reading stays one Shift+F3 away.
    priority=40,
    produces="document",
)
def word(url: str) -> dict:
    started = time.time()
    try:
        result = _read(url)
    except Unreadable as failure:
        return error(str(failure))
    except OSError as failure:
        return error(tr("The file could not be read: {error}", {"error": failure}))
    except Exception as failure:  # noqa: BLE001
        return error(tr("This document could not be read: {error}", {"error": failure}))

    if not result.markdown.strip():
        return error(tr("There is no text in this document."))
    text, cut = _cut(_preamble(result) + result.markdown)
    plugin.log("%s: %d words, %d picture(s), %d change(s), %.2fs" % (
        url.rsplit("/", 1)[-1], result.words, len(result.pictures),
        result.changes, time.time() - started))
    return _answer(text, result.pictures, cut)


def _show(url: str, read) -> dict:
    started = time.time()
    try:
        result = read(url)
    except Unreadable as failure:
        return error(str(failure))
    except OSError as failure:
        return error(tr("The file could not be read: {error}", {"error": failure}))
    except Exception as failure:  # noqa: BLE001
        return error(tr("This book could not be read: {error}", {"error": failure}))
    if not result.markdown.strip():
        return error(tr("There is no text in this document."))
    text, cut = _cut(result.markdown)
    plugin.log("%s: %d words, %d picture(s), %.2fs" % (
        url.rsplit("/", 1)[-1], result.words, len(result.pictures), time.time() - started))
    return _answer(text, result.pictures, cut)


def _cover(url: str, pixels: int):
    """The cover, for the strip: the book's own picture of itself."""
    try:
        return _book(url, only_cover=True)
    except Exception:  # noqa: BLE001
        return None


@plugin.viewer(
    "documents.book",
    "Book",
    extensions=BOOKS,
    # Above the archive viewer, which claims an .epub as the zip it is.
    priority=40,
    produces="document",
    thumbnail=_cover,
)
def book(url: str) -> dict:
    return _show(url, _book)


@plugin.viewer(
    "documents.fb2zip",
    "Book",
    extensions=["zip"],
    # Below the archive viewer: a zip is an archive, unless the probe finds a
    # FictionBook inside — and then it goes ahead of everything.
    priority=1,
    probe=fb2.in_zip,
    produces="document",
    thumbnail=_cover,
)
def zipped_book(url: str) -> dict:
    return _show(url, _book)


@plugin.describer("documents.about", "About this document", extensions=WORD + BOOKS)
def about(url: str) -> dict:
    try:
        result = _book(url) if url.lower().rsplit(".", 1)[-1] in BOOKS else _read(url)
    except Unreadable as failure:
        return facts([], note=str(failure))
    except Exception as failure:  # noqa: BLE001
        return facts([], note=tr("This document could not be read: {error}", {"error": failure}))
    meta = result.meta
    cover = result.cover.read() if getattr(result, "cover", None) is not None else None

    def said(key, label):
        value = meta.get(key)
        return fact(label, value) if value else None

    def day(key, label):
        value = meta.get(key)
        return fact(label, value[:10]) if value else None

    return facts([
        fact_group(tr("Document"), [
            said("title", tr("Title")),
            said("subject", tr("Subject")),
            said("creator", tr("Author")),
            said("publisher", tr("Publisher")),
            said("date", tr("Date")) if not meta.get("created") else None,
            said("year", tr("Year")),
            said("language", tr("Language")),
            said("lastModifiedBy", tr("Last saved by")),
            day("created", tr("Created")),
            day("modified", tr("Modified")),
            said("Company", tr("Company")),
            said("Application", tr("Written with")),
        ]),
        fact_group(tr("Contents"), [
            fact(tr("Words"), result.words),
            said("Pages", tr("Pages when last laid out")),
            fact(tr("Pictures"), len(result.pictures)) if result.pictures else None,
            fact(tr("Comments"), result.comments) if result.comments else None,
            fact(tr("Tracked changes"), result.changes) if result.changes else None,
        ]),
        fact_group(tr("Description"), [
            fact(tr("Description"), meta["description"], wide=True) if meta.get("description") else None,
            fact(tr("Keywords"), meta["keywords"], wide=True) if meta.get("keywords") else None,
        ]),
    ], picture=cover, mime_type="image/png" if cover and cover.startswith(b"\x89PNG") else "image/jpeg")


plugin.run()
