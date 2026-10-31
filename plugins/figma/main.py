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

"""Figma files, opened without Figma.

A `.fig` is a design somebody exported from Figma, and until now it was a file
you could only look at by uploading it again. It is not a picture: it is the
document, with every shape's geometry in it, so the right thing to do with it
is to draw the shapes — and then it is as sharp as the design is, at any
magnification, which no exported PNG ever is.

Three ways to look at one, in the order F3 offers them:

* **Design** — a page drawn as shapes. What the file really holds.
* **Preview** — the picture Figma itself rendered when the file was saved. It
  is small, and it is the only thing here that shows the text and the
  photographs, because the file carries neither the fonts nor a way to turn a
  photograph into a path.
* **What is in the file** — the pages, the frames and their sizes, the fonts
  it will ask the machine for, what the text says, the components.

The format is three formats deep and the last of them is zstd, which is not in
the Python the application ships; `zstd.py` is here for that reason.
"""

from __future__ import annotations

import io
import os
import sys
import time
import urllib.parse
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xverb import (  # noqa: E402
    Cancelled,
    DIRECTORY,
    Entry,
    FileSystem,
    Plugin,
    RpcError,
    error,
    fact,
    fact_group,
    facts,
    image,
    markdown,
    query_of,
)

import drawing  # noqa: E402
import figfile  # noqa: E402
import report  # noqa: E402
import svgout  # noqa: E402

plugin = Plugin("org.xverb.figma", "Figma")

#: Figma's own limit on a file is far below this; the cap is here so that a
#: file somebody has renamed cannot be read into memory whole.
MAX_BYTES = 256 << 20

EXTENSIONS = ["fig", "jam"]


def _name_of(url: str) -> str:
    """The file's name as it is written in the panel, not as a URL spells it."""
    return urllib.parse.unquote(os.path.basename(url))


def _stamp(url: str):
    """What tells one state of a file from another, as cheaply as asking."""
    try:
        entry = plugin.stat(url) or {}
    except Exception:  # noqa: BLE001
        return None
    return (url, entry.get("size"), entry.get("modified"))


#: The last file read, kept because the three views are three calls and the
#: reading is the slow half: a board of a thousand layers is three seconds of
#: unpacking, and pressing Shift+F3 should not pay it again.
_LAST: dict = {}


def _load(url: str, fraction: float = 0.9):
    """The document, or the content explaining why there is none."""
    stamp = _stamp(url)
    if stamp is not None and _LAST.get("stamp") == stamp:
        return _LAST["document"], None
    try:
        raw = plugin.read_file(url, max_bytes=MAX_BYTES)
    except Exception as failure:  # noqa: BLE001
        return None, error(plugin.tr("The file could not be read: {error}",
                                     {"error": failure}))
    if not raw:
        return None, error(plugin.tr("The file is empty."))

    def progress(done):
        plugin.progress(done * fraction)

    try:
        document = figfile.read(raw, progress=progress)
        if stamp is not None:
            _LAST["stamp"] = stamp
            _LAST["document"] = document
        return document, None
    except Cancelled:
        # Walking away from the page is not a failure to read the file: the
        # SDK answers the host for us, so this has to travel.
        raise
    except figfile.FigError as failure:
        return None, error(str(failure))
    except Exception as failure:  # noqa: BLE001
        return None, error(plugin.tr("This Figma file could not be read: "
                                     "{error}", {"error": failure}))


def _saved_preview(url: str) -> bytes | None:
    """The thumbnail Figma put in the zip, without reading the document.

    Worth its own path: it is one entry out of the archive, where the design
    is three layers of unpacking and a walk of the tree.
    """
    try:
        raw = plugin.read_file(url, max_bytes=MAX_BYTES)
        if raw[:2] != b"PK":
            return None
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if "thumbnail.png" not in archive.namelist():
                return None
            return archive.read("thumbnail.png")
    except Exception:  # noqa: BLE001
        return None


def _for_the_strip(url: str, pixels: int):
    """A small copy for the strip along the bottom of a viewer.

    The picture in the file is 400 pixels across at most, which is smaller
    than the strip ever asks for, so it is sent as it is rather than grown.
    """
    return _saved_preview(url)


def _unsaid(tally: dict) -> list[str]:
    """The parts of a page that are counted rather than drawn."""
    said = []
    if tally.get("text"):
        said.append(plugin.tr("{count} text layer(s)", {"count": tally["text"]}))
    if tally.get("pictures"):
        said.append(plugin.tr("{count} picture(s)", {"count": tally["pictures"]}))
    return said


@plugin.viewer(
    "figma.drawing",
    "Design",
    extensions=EXTENSIONS,
    # Above the text viewer, which would otherwise take a .fig and show the
    # first bytes of a zip.
    priority=20,
    produces="drawing",
    thumbnail=_for_the_strip,
)
def design(url: str) -> dict:
    """A page of the design, drawn as shapes: F3."""
    started = time.time()
    document, refusal = _load(url)
    if refusal is not None:
        return refusal

    pages = document.pages
    if not pages:
        return _contents_of(document, url)

    # The first page with anything on it. A design often opens with an empty
    # page, or one somebody left hidden, and an empty canvas is not an answer.
    chosen = None
    result = None
    for index, page in enumerate(pages):
        found = drawing.of_page(document, page)
        if found["shapes"]:
            chosen, result = index, found
            break
    if result is None:
        preview = document.thumbnail
        if preview:
            return image(preview, "image/png")
        return _contents_of(document, url)

    notes = []
    if len(pages) > 1:
        notes.append(plugin.tr("page {number} of {total}: {name}",
                               {"number": chosen + 1, "total": len(pages),
                                "name": pages[chosen].get("name") or "?"}))
    unsaid = _unsaid(result["tally"])
    if unsaid:
        notes.append(plugin.tr("{things} not drawn — the preview Figma saved "
                               "is one Shift+F3 away",
                               {"things": ", ".join(unsaid)}))
    if result.get("truncated"):
        notes.append(plugin.tr("the first {count} shapes of a longer page",
                               {"count": len(result["shapes"])}))

    plugin.log("%s: %d shape(s), %.0fx%.0f, %.2fs"
               % (_name_of(url), len(result["shapes"]),
                  result["width"], result["height"], time.time() - started))
    return {
        "kind": "vector",
        "width": result["width"],
        "height": result["height"],
        "shapes": result["shapes"],
        "detail": " · ".join(notes) if notes else None,
    }


@plugin.viewer(
    "figma.preview",
    "Preview",
    extensions=EXTENSIONS,
    priority=15,
    produces="picture",
)
def preview(url: str) -> dict:
    """What Figma drew of the file when it was saved: Shift+F3.

    The one view with the text and the photographs in it, and the only one a
    file saved before the zip was introduced does not have.
    """
    found = _saved_preview(url)
    if found is None:
        return error(plugin.tr("This file carries no picture of itself. "
                               "Shift+F3 draws the design from its shapes."))
    return image(found, "image/png")


def _contents_of(document: figfile.Figma, url: str) -> dict:
    try:
        size = plugin.stat(url).get("size", 0)
    except Exception:  # noqa: BLE001
        size = 0
    return markdown(report.of(document, size, _name_of(url), tr=plugin.tr))


@plugin.viewer(
    "figma.contents",
    "What is in the file",
    extensions=EXTENSIONS,
    priority=10,
)
def contents(url: str) -> dict:
    """The pages, the frames, the fonts and the text: Shift+F3.

    Also what the design view falls back to when there is nothing to draw, so
    a file whose pages are all empty says what it holds rather than showing an
    empty canvas.
    """
    document, refusal = _load(url)
    if refusal is not None:
        return refusal
    return _contents_of(document, url)


@plugin.describer("figma.about", "About this design", extensions=EXTENSIONS)
def about(url: str) -> dict:
    """What the file says about itself, for the information panel."""
    document, refusal = _load(url)
    if refusal is not None:
        return facts([], note=plugin.tr("This Figma file could not be read."))

    pages = document.pages
    drawn = sum(1 for n in document.nodes if n.get("type") in report.DRAWN)
    texts = sum(1 for n in document.nodes if n.get("type") == "TEXT")
    group = [
        fact(plugin.tr("Name"), document.name or _name_of(url)),
        fact(plugin.tr("Kind"), plugin.tr("FigJam board")
             if document.kind == "jam" else plugin.tr("Figma design")),
        fact(plugin.tr("Pages"), len(pages)),
        fact(plugin.tr("Layers"), drawn),
    ]
    if texts:
        group.append(fact(plugin.tr("Text layers"), texts))
    if document.images:
        group.append(fact(plugin.tr("Pictures"), len(document.images)))
    if document.exported_at:
        group.append(fact(plugin.tr("Saved"), document.exported_at[:10]))
    group.append(fact(plugin.tr("Format"), "%s %d"
                      % ("fig-jam" if document.kind == "jam" else "fig-kiwi",
                         document.version)))

    groups = [fact_group(plugin.tr("Figma"), group)]
    if pages:
        groups.append(fact_group(
            plugin.tr("Pages"),
            [fact("%d" % (i + 1), page.get("name") or "?")
             for i, page in enumerate(pages[:12])]))
    return facts(groups, picture=document.thumbnail, mime_type="image/png")



# -- the file as a folder of pages ---------------------------------------

#: What a page is called in the panel. The number keeps the pages in the
#: order the tabs are in whatever the panel is sorted by, and the extension
#: is the truth about the bytes: a page really is an SVG here.
def _page_name(index: int, width: int, title: str, taken: set) -> str:
    plain = " ".join((title or "").split()) or plugin.tr("Page")
    for bad in '/\\:*?"<>|':
        plain = plain.replace(bad, "-")
    plain = plain.strip(". ") or plugin.tr("Page")
    name = "%0*d · %s.svg" % (width, index, plain[:80])
    while name.lower() in taken:
        name = "%0*d · %s (%d).svg" % (width, index, plain[:72], len(taken))
    taken.add(name.lower())
    return name


def _background_of(page: dict) -> str | None:
    if not page.get("backgroundEnabled", True):
        return None
    return drawing.colour_of(page.get("backgroundColor"),
                             page.get("backgroundOpacity", 1.0))


#: The pages of the last file entered, written out once. Entering a folder
#: asks for its listing again on every refresh, and drawing a board of two
#: thousand layers four times a second is not a listing anybody wants.
_PAGES: dict = {}


def _pages_of(file_url: str) -> list[tuple[str, bytes]]:
    stamp = _stamp(file_url)
    if stamp is not None and _PAGES.get("stamp") == stamp:
        return _PAGES["pages"]

    document, refusal = _load(file_url)
    if refusal is not None:
        raise RpcError(refusal.get("message")
                       or plugin.tr("This Figma file could not be read."))

    pages = []
    taken: set = set()
    width = len(str(max(1, len(document.pages))))
    for index, page in enumerate(document.pages, 1):
        drawn = drawing.of_page(document, page)
        pages.append((
            _page_name(index, width, page.get("name"), taken),
            svgout.of_drawing(drawn, background=_background_of(page),
                              title=page.get("name")),
        ))
    if stamp is not None:
        _PAGES["stamp"] = stamp
        _PAGES["pages"] = pages
    return pages


def _container_of(url: str) -> tuple[str, str]:
    """`figma:///<page>?from=file:///…fig` as the file and the page in it."""
    from_url = query_of(url).get("from") or ""
    if not from_url:
        raise RpcError(plugin.tr("There is no Figma file in {url}",
                                 {"url": url}))
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path or "")
    return from_url, path.strip("/")


def _modified_of(file_url: str):
    """When the file was last written, in the seconds an Entry wants.

    The host answers a stat in milliseconds and the SDK multiplies an entry's
    own time by a thousand on the way back out, so handing one straight to the
    other dates every page to the year 58000.
    """
    found = (plugin.stat(file_url) or {}).get("modified")
    return None if found is None else found / 1000.0


class PagesFileSystem(FileSystem):
    """A `.fig` entered as a folder: one SVG per page.

    **Why the pages are files rather than a chooser inside the viewer.** A
    viewer answers with content and hears nothing back — a button or a tab
    drawn in one is dead — so the place in this application where you choose
    among several of something is the panel, and the panel is a file system.
    Entering the design puts its pages under the cursor, F3 draws the one you
    are on, and in full screen the strip walks them with Left and Right. The
    bytes are a real SVG, so a page copies out of the panel into anything.
    """

    scheme = "figma"
    writable = False
    icon = "view"

    def list(self, url: str) -> list:
        file_url, inner = _container_of(url)
        if inner:
            # A page has nothing inside it; the panel asked anyway.
            return []
        modified = _modified_of(file_url)
        return [Entry(name, size=len(data), modified=modified)
                for name, data in _pages_of(file_url)]

    def stat(self, url: str):
        file_url, inner = _container_of(url)
        if not inner:
            return Entry("", kind=DIRECTORY)
        modified = _modified_of(file_url)
        for name, data in _pages_of(file_url):
            if name == inner:
                return Entry(name, size=len(data), modified=modified)
        return None

    def read(self, url: str, offset: int, length: int) -> bytes:
        file_url, inner = _container_of(url)
        for name, data in _pages_of(file_url):
            if name == inner:
                return data[offset:offset + length]
        raise RpcError(plugin.tr("{name} is not a page of this file",
                                 {"name": inner}))


plugin.add_filesystem(PagesFileSystem())


plugin.run()
