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

"""An EPUB, 2 or 3, as one Markdown document.

`META-INF/container.xml` names the package file; the package lists every file
of the book (the manifest), the order they are read in (the spine) and what
the book says about itself. The chapters are read in spine order by `xhtml`.
The table of contents — `nav.xhtml` in EPUB 3, `toc.ncx` in EPUB 2 — is read
first, because it is also where a book converted from FB2 keeps its headings.

**DRM.** `META-INF/encryption.xml` lists what is encrypted. Fonts obfuscated
by the two schemes EPUB defines for them are no obstacle — nothing here reads
fonts. Anything else encrypted is a book sold to one application, and says so.
"""

from __future__ import annotations

import base64
import posixpath
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

import mdtext
from package import Package, Unreadable
from xhtml import Page

from compat import Picture, picture_size

OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
XENC = "http://www.w3.org/2001/04/xmlenc#"

#: The two ways EPUB obfuscates a font — which is not DRM.
FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}

DRAWN = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}


class Result:
    def __init__(self):
        self.markdown = ""
        self.pictures: Dict[str, Picture] = {}
        self.changes = 0
        self.comments = 0
        self.words = 0
        self.meta: Dict[str, str] = {}
        self.guessed = False
        self.cover: Optional[Picture] = None


def _xml(data: Optional[bytes]) -> Optional[ET.Element]:
    if not data:
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _decode(data: bytes) -> str:
    head = data[:1024]
    found = re.search(rb'encoding=["\']([\w.-]+)', head) or re.search(rb'charset=["\']?([\w.-]+)', head)
    for name in ([found.group(1).decode("ascii", "ignore")] if found else []) + ["utf-8", "cp1252"]:
        try:
            return data.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


class Book:
    def __init__(self, package: Package, tr):
        self.package = package
        self.tr = tr
        self.result = Result()
        self._drm()
        root = _xml(package.read("META-INF/container.xml"))
        opf_path = None
        if root is not None:
            for rootfile in root.iter("{%s}rootfile" % CONTAINER):
                opf_path = rootfile.get("full-path")
                if opf_path:
                    break
        if not opf_path:
            raise Unreadable(tr("This is not an EPUB book: it names no package file."))
        self.opf_path = opf_path
        opf = _xml(package.read(opf_path))
        if opf is None:
            raise Unreadable(tr("This is not an EPUB book: it names no package file."))
        self.opf = opf
        self.items: Dict[str, Tuple[str, str, str]] = {}
        for item in opf.iter("{%s}item" % OPF):
            href = self._path(opf_path, item.get("href") or "")
            self.items[item.get("id") or ""] = (href, item.get("media-type") or "", item.get("properties") or "")
        self.pictures_by_path: Dict[str, str] = {}

    @staticmethod
    def _path(base: str, href: str) -> str:
        href = unquote(href.split("#", 1)[0])
        return posixpath.normpath(posixpath.join(posixpath.dirname(base), href))

    def _drm(self) -> None:
        root = _xml(self.package.read("META-INF/encryption.xml"))
        if root is None:
            return
        for data in root.iter("{%s}EncryptedData" % XENC):
            method = data.find("{%s}EncryptionMethod" % XENC)
            algorithm = method.get("Algorithm") if method is not None else ""
            if algorithm not in FONT_OBFUSCATION:
                raise Unreadable(self.tr(
                    "This book is protected by DRM: only the application it was "
                    "sold to can open it."))

    # What the book says about itself.

    def _meta(self) -> None:
        meta = self.result.meta
        metadata = self.opf.find("{%s}metadata" % OPF)
        if metadata is None:
            return
        for name in ("title", "creator", "language", "date", "publisher", "description", "subject"):
            values = [e.text.strip() for e in metadata.findall("{%s}%s" % (DC, name)) if e.text and e.text.strip()]
            if values:
                meta[name] = ", ".join(dict.fromkeys(values)) if name in ("creator", "subject") else values[0]
        cover_id = None
        for element in metadata.iter("{%s}meta" % OPF):
            if element.get("name") == "cover":
                cover_id = element.get("content")
        cover = None
        for item_id, (href, media, properties) in self.items.items():
            if "cover-image" in properties.split():
                cover = href
        if cover is None and cover_id in self.items:
            cover = self.items[cover_id][0]
        if cover is None:
            guide = self.opf.find("{%s}guide" % OPF)
            if guide is not None:
                for reference in guide:
                    href = reference.get("href") or ""
                    if (reference.get("type") or "").lower() == "cover" and "html" not in href.rsplit(".", 1)[-1].lower():
                        cover = self._path(self.opf_path, href)
        self.cover = cover if cover and self.package.has(cover) else None

    # The table of contents.

    def _toc(self) -> Dict[str, Dict[str, Tuple[int, str]]]:
        """page → anchor ('' for the page itself) → (depth, title)."""
        found: Dict[str, Dict[str, Tuple[int, str]]] = {}

        def add(base: str, href: str, depth: int, title: str) -> None:
            title = " ".join(title.split())
            if not title or not href:
                return
            page = self._path(base, href)
            anchor = unquote(href.split("#", 1)[1]) if "#" in href else ""
            found.setdefault(page, {}).setdefault(anchor, (min(depth, 6), title))

        nav = next((href for href, media, props in self.items.values() if "nav" in props.split()), None)
        self.nav = nav
        if nav:
            page = Page(nav, lambda *a: None, {})
            text = _decode(self.package.read(nav) or b"")
            self._nav(text, nav, add)
            if found:
                return found
        spine = self.opf.find("{%s}spine" % OPF)
        ncx_id = spine.get("toc") if spine is not None else None
        ncx = self.items.get(ncx_id or "", (None,))[0]
        if not ncx:
            ncx = next((href for href, media, _ in self.items.values() if media == "application/x-dtbncx+xml"), None)
        root = _xml(self.package.read(ncx)) if ncx else None
        if root is not None:
            def walk(parent, depth):
                for point in parent.findall("{%s}navPoint" % NCX):
                    label = point.find("{%s}navLabel/{%s}text" % (NCX, NCX))
                    content = point.find("{%s}content" % NCX)
                    if content is not None:
                        add(ncx, content.get("src") or "", depth, (label.text or "") if label is not None else "")
                    walk(point, depth + 1)
            nav_map = root.find("{%s}navMap" % NCX)
            if nav_map is not None:
                walk(nav_map, 1)
        return found

    def _nav(self, text: str, base: str, add) -> None:
        """EPUB 3's contents: the links of `<nav epub:type="toc">`, their depth
        by how many lists deep they sit."""
        inside = re.search(r'<nav\b[^>]*epub:type="[^"]*\btoc\b[^"]*"[^>]*>(.*?)</nav>', text, re.S)
        if not inside:
            return
        depth = 0
        for match in re.finditer(r"<(/?)(ol|ul)\b[^>]*>|<a\b[^>]*href=\"([^\"]*)\"[^>]*>(.*?)</a>", inside.group(1), re.S):
            if match.group(2):
                depth += -1 if match.group(1) else 1
            elif match.group(3) is not None:
                title = re.sub(r"<[^>]+>", "", match.group(4))
                from html import unescape
                add(base, match.group(3), max(1, depth), unescape(title))

    # Pictures.

    def _picture(self, path: str, alt: str, width: Optional[int], height: Optional[int]) -> Optional[str]:
        caption = " ".join(alt.replace("[", "(").replace("]", ")").split())
        if path == self.cover and self.cover_shown:
            return None
        if path.startswith("data:"):
            found = re.match(r"data:image/[\w.+-]+;base64,(.*)", path, re.S)
            if not found:
                return None
            data = base64.b64decode(found.group(1))
            key = "p%d" % (len(self.result.pictures) + 1)
            self.result.pictures[key] = Picture(data)
            return "![%s](picture:%s)" % (caption, key)
        if not path or re.match(r"(?i)^[a-z]+://", path):
            return mdtext.escape("[%s]" % caption) if caption else None
        key = self.pictures_by_path.get(path)
        if key is None:
            # What the picture is, by its bytes rather than its name: books
            # carry `photo.jpg_1` and the like.
            size = self._size(path) if self.package.has(path) else None
            if size is None:
                extension = path.rsplit(".", 1)[-1].upper() if "." in path else ""
                what = caption or extension or self.tr("picture")
                return mdtext.escape("[%s]" % self.tr("picture: {what}", {"what": what}))
            key = "p%d" % (len(self.result.pictures) + 1)
            self.result.pictures[key] = Picture(self.package.later(path), *size)
            self.pictures_by_path[path] = key
        return "![%s](picture:%s)" % (caption, key)

    def _size(self, path: str) -> Optional[Tuple[int, int]]:
        """A picture's size from its first bytes — enough for every header
        but a JPEG whose EXIF comes first, and that one is read further."""
        name = self.package.name(path)
        if name is None:
            return None
        with self.package.zip.open(name) as handle:
            head = handle.read(1 << 16)
            found = picture_size(head)
            if found is None and head[:2] == b"\xff\xd8":
                found = picture_size(head + handle.read(1 << 20))
        return found

    # Reading.

    def convert(self) -> Result:
        self._meta()
        toc = self._toc()
        result = self.result
        out: List[str] = []
        self.cover_shown = False
        if self.cover:
            line = self._picture(self.cover, "", None, None)
            if line and line.startswith("!"):
                out.append(line)
                self.cover_shown = True
                result.cover = result.pictures[line.rsplit(":", 1)[1].rstrip(")")]
        meta = result.meta
        if meta.get("title"):
            out.append("**%s**" % mdtext.escape(meta["title"]))
        if meta.get("creator"):
            out.append("*%s*" % mdtext.escape(meta["creator"]))
        if meta.get("description"):
            description = re.sub(r"<[^>]+>", " ", meta["description"])
            out.append("> " + mdtext.escape(mdtext.squeeze(" ".join(description.split()))))

        spine = self.opf.find("{%s}spine" % OPF)
        headings = 0
        for itemref in (spine if spine is not None else []):
            item = self.items.get(itemref.get("idref") or "")
            if item is None:
                continue
            href, media, _ = item
            if "html" not in media or href == self.nav:
                continue
            data = self.package.read(href)
            if not data:
                continue
            anchors = dict(toc.get(href, {}))
            start = anchors.pop("", None)
            page = Page(href, self._picture, anchors, start)
            page.feed(_decode(data))
            out.extend(page.finish())
            headings += page.headings

        result.markdown = "\n\n".join(b for b in out if b.strip()) + "\n"
        result.words = len(re.findall(r"\w+", re.sub(r"\(picture:[^)]*\)", "", result.markdown)))
        return result


def cover(package: Package, tr) -> Optional[bytes]:
    """Only the cover, for the strip and the About card: the package file
    is read, the chapters are not."""
    book = Book(package, tr)
    book._meta()
    return package.read(book.cover) if book.cover else None


def convert(package: Package, tr) -> Result:
    return Book(package, tr).convert()
