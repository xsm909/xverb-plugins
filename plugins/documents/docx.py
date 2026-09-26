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

"""Word's .docx as Markdown: the document as it would read with every change
accepted, and none of the page.

What is read and why, in the order the file is read:

- **Relationships** say where the body is and what each picture and link
  points at. The body is looked for through them rather than assumed to be
  `word/document.xml`, which is only the usual name.
- **Styles** say which paragraphs are headings. Not by the style's id: in a
  Russian Word the id of "Heading 1" is `1` or `a3`. A built-in style's
  *name* is always English (`heading 1`), and any style can carry an outline
  level; both are followed up the `basedOn` chain.
- **Numbering** turns a list paragraph into its label, counted the way Word
  counts: per list definition, deeper levels starting again under a new
  higher one, a restart where the file asks for one.
- **The body**, paragraph by paragraph. Deleted text is left out and inserted
  text kept; field codes are left out and their results kept; a table of
  contents is left out because the structure panel is one.
- **Notes and comments** are gathered as they are met and written at the end.

Colour, fonts, sizes, page breaks, headers and footers are thrown away. That is
the point of reading a document as text.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional, Tuple

import mdtext
from mdtext import Span
from package import Package, join

from xverb import Picture

MAIN = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
VML = "urn:schemas-microsoft-com:vml"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"

#: Strict Open XML says the same things under other names; they are read as
#: the transitional ones.
STRICT = {
    "http://purl.oclc.org/ooxml/wordprocessingml/main": MAIN,
    "http://purl.oclc.org/ooxml/officeDocument/relationships": REL,
    "http://purl.oclc.org/ooxml/drawingml/wordprocessingDrawing": WP,
    "http://purl.oclc.org/ooxml/drawingml/main": A,
}

OFFICE_DOCUMENT = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument",
)

#: What the host draws. Anything else in a document — EMF and WMF above all,
#: which is what Word keeps a pasted chart or a drawing as — is its caption.
DRAWN = {"png", "jpg", "jpeg", "jpe", "gif", "bmp", "webp"}

#: A drawing's size is given in English Metric Units, 914 400 to the inch; a
#: pixel of the page is a ninety-sixth of an inch.
EMU_PER_PIXEL = 9525


def w(name: str) -> str:
    return "{%s}%s" % (MAIN, name)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _ns(tag) -> str:
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _parse(data: Optional[bytes]) -> Optional[ET.Element]:
    if not data:
        return None
    root = ET.fromstring(data)
    if any(uri in data[:4096].decode("utf-8", "replace") for uri in STRICT):
        for element in root.iter():
            uri = _ns(element.tag)
            if uri in STRICT:
                element.tag = "{%s}%s" % (STRICT[uri], _local(element.tag))
            for key in list(element.attrib):
                if _ns(key) in STRICT:
                    element.attrib["{%s}%s" % (STRICT[_ns(key)], _local(key))] = element.attrib.pop(key)
    return root


def _val(element: Optional[ET.Element], name: str = "val") -> Optional[str]:
    if element is None:
        return None
    return element.get(w(name))


def _on(element: Optional[ET.Element]) -> bool:
    """A toggle property: present means on, unless it says otherwise."""
    if element is None:
        return False
    return (_val(element) or "true").lower() not in ("0", "false", "off", "none")


class _Maybe(str):
    """A paragraph that is a heading if the document has no real ones: short,
    bold from end to end, and not ending the way a sentence does. Many Word
    files are written like that, by hand, and without this their structure
    panel would be empty."""

    heading = ""


#: The most a paragraph standing in for a heading may be.
_MAYBE_LONGEST = 150


class Result:
    def __init__(self):
        self.markdown = ""
        self.pictures: Dict[str, Picture] = {}
        self.changes = 0
        self.comments = 0
        self.words = 0
        self.meta: Dict[str, str] = {}
        #: Whether the headings were worked out from bold paragraphs.
        self.guessed = False


# --- Numbering -----------------------------------------------------------

_RUSSIAN = "абвгдежзиклмнопрстуфхцчшщэюя"


def _letters(n: int, alphabet: str) -> str:
    out = ""
    while n > 0:
        n -= 1
        out = alphabet[n % len(alphabet)] + out
        n //= len(alphabet)
    return out


def _roman(n: int) -> str:
    if n <= 0 or n >= 4000:
        return str(n)
    out = ""
    for value, mark in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
                        (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"),
                        (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out += mark
            n -= value
    return out


def _format(n: int, fmt: str) -> str:
    if fmt == "decimalZero":
        return "%02d" % n
    if fmt == "lowerLetter":
        return _letters(n, "abcdefghijklmnopqrstuvwxyz")
    if fmt == "upperLetter":
        return _letters(n, "abcdefghijklmnopqrstuvwxyz").upper()
    if fmt == "lowerRoman":
        return _roman(n)
    if fmt == "upperRoman":
        return _roman(n).upper()
    if fmt == "russianLower":
        return _letters(n, _RUSSIAN)
    if fmt == "russianUpper":
        return _letters(n, _RUSSIAN).upper()
    return str(n)


class Numbering:
    """Word's list numbering, counted as the document is read."""

    def __init__(self, root: Optional[ET.Element]):
        #: abstract id → level → (format, text, start, legal)
        self.abstract: Dict[str, Dict[int, Tuple[str, str, int, bool]]] = {}
        #: num id → (abstract id, level → start override)
        self.nums: Dict[str, Tuple[str, Dict[int, int]]] = {}
        self.counts: Dict[str, Dict[int, int]] = {}
        self.started: set = set()
        if root is None:
            return
        for abstract in root.findall(w("abstractNum")):
            levels = {}
            for lvl in abstract.findall(w("lvl")):
                ilvl = int(_val(lvl, "ilvl") or 0)
                start = int(_val(lvl.find(w("start"))) or 1)
                fmt = _val(lvl.find(w("numFmt"))) or "decimal"
                text = _val(lvl.find(w("lvlText")))
                levels[ilvl] = (fmt, "" if text is None else text, start,
                                _on(lvl.find(w("isLgl"))))
            self.abstract[_val(abstract, "abstractNumId") or ""] = levels
        for num in root.findall(w("num")):
            overrides = {}
            for over in num.findall(w("lvlOverride")):
                start = over.find(w("startOverride"))
                if start is not None:
                    overrides[int(_val(over, "ilvl") or 0)] = int(_val(start) or 1)
            self.nums[_val(num, "numId") or ""] = (_val(num.find(w("abstractNumId"))) or "", overrides)

    def next(self, num_id: str, ilvl: int) -> Optional[Tuple[str, str]]:
        """(kind, label) for the next paragraph of list [num_id] at [ilvl]:
        kind is ``bullet``, ``number`` or ``none``."""
        found = self.nums.get(num_id)
        if found is None:
            return None
        abstract_id, overrides = found
        levels = self.abstract.get(abstract_id)
        if not levels or ilvl not in levels:
            return None
        counts = self.counts.setdefault(abstract_id, {})
        if num_id not in self.started:
            self.started.add(num_id)
            for level, start in overrides.items():
                counts[level] = start - 1
        fmt, text, start, legal = levels[ilvl]
        counts[ilvl] = counts.get(ilvl, start - 1) + 1
        for deeper in [k for k in counts if k > ilvl]:
            del counts[deeper]
        if fmt == "bullet":
            return "bullet", ""
        if fmt == "none" or not text:
            return "none", ""

        def number(match):
            level = int(match.group(1)) - 1
            lf, _, ls, _ = levels.get(level, ("decimal", "", 1, False))
            value = counts.get(level, ls)
            return _format(value, "decimal" if legal else lf)

        return "number", re.sub(r"%(\d)", number, text)


# --- Styles --------------------------------------------------------------

class Styles:
    def __init__(self, root: Optional[ET.Element]):
        self.styles: Dict[str, ET.Element] = {}
        self.default_paragraph: Optional[str] = None
        self._levels: Dict[str, int] = {}
        if root is None:
            return
        for style in root.findall(w("style")):
            sid = style.get(w("styleId"))
            if sid:
                self.styles[sid] = style
                if style.get(w("type")) == "paragraph" and style.get(w("default")) in ("1", "true"):
                    self.default_paragraph = sid

    def chain(self, sid: Optional[str]):
        seen = set()
        while sid and sid not in seen and sid in self.styles:
            seen.add(sid)
            style = self.styles[sid]
            yield style
            sid = _val(style.find(w("basedOn")))

    def name(self, sid: Optional[str]) -> str:
        style = self.styles.get(sid or "")
        return (_val(style.find(w("name"))) or "").lower() if style is not None else ""

    def heading(self, sid: Optional[str]) -> int:
        """0 for body text, else the heading level 1–9 the style gives."""
        key = sid or ""
        if key in self._levels:
            return self._levels[key]
        level = 0
        for style in self.chain(sid):
            name = (_val(style.find(w("name"))) or "").lower()
            found = re.fullmatch(r"heading ([1-9])", name)
            if found:
                level = int(found.group(1))
                break
            if name == "title":
                level = 1
                break
            outline = style.find("%s/%s" % (w("pPr"), w("outlineLvl")))
            if outline is not None:
                value = int(_val(outline) or 9)
                level = value + 1 if value < 9 else 0
                break
        self._levels[key] = level
        return level

    def numbering(self, sid: Optional[str]) -> Optional[Tuple[str, int]]:
        for style in self.chain(sid):
            num = style.find("%s/%s" % (w("pPr"), w("numPr")))
            if num is not None:
                num_id = _val(num.find(w("numId")))
                if num_id:
                    return num_id, int(_val(num.find(w("ilvl"))) or 0)
        return None

    def run(self, sid: Optional[str]) -> Tuple[bool, bool, bool]:
        bold = italic = strike = None
        for style in self.chain(sid):
            props = style.find(w("rPr"))
            if props is None:
                continue
            if bold is None and props.find(w("b")) is not None:
                bold = _on(props.find(w("b")))
            if italic is None and props.find(w("i")) is not None:
                italic = _on(props.find(w("i")))
            if strike is None and props.find(w("strike")) is not None:
                strike = _on(props.find(w("strike")))
        return bool(bold), bool(italic), bool(strike)

    def skipped(self, sid: Optional[str]) -> bool:
        """Word's own table of contents, written out as paragraphs."""
        name = self.name(sid)
        return bool(re.fullmatch(r"toc [1-9]|toc heading", name))


# --- The document ----------------------------------------------------------

class Field:
    __slots__ = ("instr", "result", "link")

    def __init__(self):
        self.instr = ""
        self.result = False
        self.link: Optional[str] = None

    @property
    def hides(self) -> bool:
        head = self.instr.strip().split(" ", 1)[0].upper()
        return head in ("TOC", "INDEX", "XE", "TC")


class Converter:
    def __init__(self, package: Package, tr: Callable[..., str]):
        self.package = package
        self.tr = tr
        self.result = Result()
        self.main = self._main_part()
        self.rels = self._rels(self.main)
        self.styles = Styles(_parse(self._related("styles")))
        self.numbering = Numbering(_parse(self._related("numbering")))
        self.footnotes = self._notes("footnotes", "footnote")
        self.endnotes = self._notes("endnotes", "endnote")
        self.comment_parts = self._comments()
        self.fields: List[Field] = []
        self.notes: List[str] = []
        self.comments: List[Tuple[str, str]] = []
        self.comment_numbers: Dict[str, int] = {}
        self.quotes: Dict[str, List[str]] = {}
        self.change_ids: set = set()
        self.in_cell = 0
        self.after: List[str] = []
        #: Headings the file itself marks as headings.
        self.styled_headings = 0

    # Parts and relationships.

    def _main_part(self) -> str:
        root = _parse(self.package.read("_rels/.rels"))
        if root is not None:
            for rel in root:
                if rel.get("Type") in OFFICE_DOCUMENT:
                    return join("", rel.get("Target") or "")
        return "word/document.xml"

    def _rels(self, part: str) -> Dict[str, Tuple[str, str, bool]]:
        folder, name = part.rsplit("/", 1) if "/" in part else ("", part)
        root = _parse(self.package.read("%s/_rels/%s.rels" % (folder, name) if folder else "_rels/%s.rels" % name))
        found = {}
        if root is not None:
            for rel in root:
                external = (rel.get("TargetMode") or "") == "External"
                target = rel.get("Target") or ""
                found[rel.get("Id") or ""] = (
                    (rel.get("Type") or "").rsplit("/", 1)[-1],
                    target if external else join(part, target),
                    external,
                )
        return found

    def _related(self, kind: str) -> Optional[bytes]:
        for rel_type, target, external in self.rels.values():
            if rel_type == kind and not external:
                return self.package.read(target)
        return None

    def _notes(self, kind: str, tag: str) -> Dict[str, ET.Element]:
        root = _parse(self._related(kind))
        if root is None:
            return {}
        return {
            note.get(w("id")) or "": note
            for note in root.findall(w(tag))
            if (note.get(w("type")) or "normal") == "normal"
        }

    def _comments(self) -> Dict[str, ET.Element]:
        root = _parse(self._related("comments"))
        if root is None:
            return {}
        return {c.get(w("id")) or "": c for c in root.findall(w("comment"))}

    # Reading.

    def convert(self) -> Result:
        root = _parse(self.package.read(self.main))
        if root is None:
            raise ValueError("The document has no body.")
        body = root.find(w("body"))
        out: List[str] = []
        if body is not None:
            self._blocks(body, out)
        self._endnotes(out)
        guessed = self.styled_headings == 0
        out = [
            (mdtext.heading(1, block.heading) if guessed else str(block))
            if isinstance(block, _Maybe) else block
            for block in out
        ]
        result = self.result
        result.guessed = guessed and any(b.startswith("# ") for b in out)
        result.markdown = "\n\n".join(block for block in out if block.strip()) + "\n"
        result.changes = len(self.change_ids)
        result.comments = len(self.comments)
        result.words = len(re.findall(r"\w+", re.sub(r"\(picture:[^)]*\)", "", result.markdown)))
        result.meta = self._meta()
        return result

    def _blocks(self, parent: ET.Element, out: List[str]) -> None:
        for child in parent:
            tag = child.tag
            if tag == w("p"):
                self._paragraph(child, out)
            elif tag == w("tbl"):
                out.append(self._table(child))
            elif tag == w("sdt"):
                gallery = child.find(".//%s/%s" % (w("docPartObj"), w("docPartGallery")))
                if gallery is not None and "content" in (_val(gallery) or "").lower():
                    continue
                content = child.find(w("sdtContent"))
                if content is not None:
                    self._blocks(content, out)
            elif tag in (w("customXml"), w("ins"), w("moveTo")):
                self._change(child)
                self._blocks(child, out)
            elif tag in (w("del"), w("moveFrom")):
                self._change(child)
            elif _local(tag) == "AlternateContent":
                choice = child.find("{%s}Choice" % MC)
                if choice is not None:
                    self._blocks(choice, out)

    def _change(self, element: ET.Element) -> None:
        if element.tag in (w("ins"), w("del"), w("moveTo"), w("moveFrom")):
            self.change_ids.add(element.get(w("id")) or id(element))

    def _paragraph(self, p: ET.Element, out: List[str]) -> None:
        props = p.find(w("pPr"))
        style = _val(props.find(w("pStyle"))) if props is not None else None
        style = style or self.styles.default_paragraph
        self.after = []
        pieces: List[Span] = []
        self._inline(p, pieces, (False, False, False), None)
        after = self.after
        self.after = []
        if self.styles.skipped(style):
            out.extend(after)
            return

        text = mdtext.spans(pieces)
        level = self.styles.heading(style)
        outline = props.find(w("outlineLvl")) if props is not None else None
        if outline is not None:
            value = int(_val(outline) or 9)
            level = value + 1 if value < 9 else 0

        numbered = None
        num = props.find(w("numPr")) if props is not None else None
        if num is not None and num.find(w("numId")) is not None:
            numbered = (_val(num.find(w("numId"))) or "", int(_val(num.find(w("ilvl"))) or 0))
        elif num is None:
            numbered = self.styles.numbering(style)
        label = None
        if numbered and numbered[0] != "0" and text:
            label = self.numbering.next(*numbered)

        if text:
            if self.in_cell:
                prefix = mdtext.escape(label[1]) + " " if label and label[0] == "number" else ""
                out.append(prefix + text)
            elif level:
                self.styled_headings += 1
                prefix = mdtext.escape(label[1]) + " " if label and label[0] == "number" else ""
                out.append(mdtext.heading(level, prefix + text))
            elif label and label[0] == "bullet":
                out.append("  " * numbered[1] + "- " + text)
            elif label and label[0] == "number" and re.fullmatch(r"\d+[.)]", label[1]):
                out.append("  " * numbered[1] + label[1][:-1] + ". " + text)
            elif label and label[0] == "number":
                out.append(mdtext.line_start(mdtext.escape(label[1]) + " " + text))
            else:
                plain = "".join(p.text for p in pieces if not p.raw)
                bold = all(p.bold or not p.text.strip() for p in pieces if not p.raw)
                if (bold and plain.strip() and len(plain.strip()) <= _MAYBE_LONGEST
                        and not plain.rstrip().endswith((".", ":", ";", ",", "!", "?"))):
                    maybe = _Maybe(mdtext.paragraph(text))
                    maybe.heading = mdtext.escape(mdtext.squeeze(plain).strip())
                    out.append(maybe)
                else:
                    out.append(mdtext.paragraph(text))
        out.extend(after)

    def _hidden(self) -> bool:
        return any(f.hides for f in self.fields)

    def _link(self) -> Optional[str]:
        for field in reversed(self.fields):
            if field.result and field.link:
                return field.link
        return None

    def _inline(self, parent: ET.Element, pieces: List[Span], style, link: Optional[str]) -> None:
        for child in parent:
            tag = child.tag
            if tag == w("r"):
                self._run(child, pieces, link)
            elif tag == w("hyperlink"):
                target = link
                rid = child.get("{%s}id" % REL)
                if rid and rid in self.rels and self.rels[rid][2]:
                    target = self.rels[rid][1]
                self._inline(child, pieces, style, target)
            elif tag in (w("ins"), w("moveTo")):
                self._change(child)
                self._inline(child, pieces, style, link)
            elif tag in (w("del"), w("moveFrom")):
                self._change(child)
            elif tag in (w("smartTag"), w("customXml"), w("fldSimple"), w("bdo"), w("dir")):
                if tag == w("fldSimple"):
                    field = Field()
                    field.instr = child.get(w("instr")) or ""
                    if field.hides:
                        continue
                self._inline(child, pieces, style, link)
            elif tag == w("sdt"):
                content = child.find(w("sdtContent"))
                if content is not None:
                    self._inline(content, pieces, style, link)
            elif tag == w("commentRangeStart"):
                self.quotes.setdefault(child.get(w("id")) or "", [])
            elif tag == w("commentRangeEnd"):
                cid = child.get(w("id")) or ""
                if cid in self.quotes:
                    self.quotes[cid].append("\0")
            elif _local(tag) == "AlternateContent":
                choice = child.find("{%s}Choice" % MC)
                if choice is not None:
                    self._inline(choice, pieces, style, link)

    def _run_style(self, run: ET.Element) -> Tuple[bool, bool, bool, bool]:
        props = run.find(w("rPr"))
        bold, italic, strike = self.styles.run(_val(props.find(w("rStyle"))) if props is not None else None)
        hidden = False
        if props is not None:
            if props.find(w("b")) is not None:
                bold = _on(props.find(w("b")))
            if props.find(w("i")) is not None:
                italic = _on(props.find(w("i")))
            for name in ("strike", "dstrike"):
                if props.find(w(name)) is not None:
                    strike = _on(props.find(w(name)))
            hidden = _on(props.find(w("vanish"))) or _on(props.find(w("webHidden")))
        return bold, italic, strike, hidden

    def _run(self, run: ET.Element, pieces: List[Span], link: Optional[str]) -> None:
        bold, italic, strike, hidden = self._run_style(run)

        def say(text: str, raw: bool = False) -> None:
            if not text or self._hidden():
                return
            if self.fields and not self.fields[-1].result:
                return
            for cid, quote in self.quotes.items():
                if not quote or quote[-1] != "\0":
                    quote.append(text if not raw else "")
            pieces.append(Span(text, bold, italic, strike, self._link() or link, raw))

        for child in run:
            tag = child.tag
            if tag == w("t"):
                if not hidden:
                    say(child.text or "")
            elif tag == w("instrText"):
                if self.fields and not self.fields[-1].result:
                    self.fields[-1].instr += child.text or ""
            elif tag == w("fldChar"):
                kind = child.get(w("fldCharType"))
                if kind == "begin":
                    self.fields.append(Field())
                elif kind == "separate" and self.fields:
                    field = self.fields[-1]
                    field.result = True
                    found = re.match(r'\s*HYPERLINK\s+"([^"]+)"', field.instr, re.I)
                    if found and "\\l" not in field.instr.split('"', 2)[-1][:4]:
                        field.link = found.group(1)
                elif kind == "end" and self.fields:
                    self.fields.pop()
            elif tag == w("tab") or tag == w("ptab"):
                say(" ")
            elif tag in (w("br"), w("cr")):
                if (child.get(w("type")) or "textWrapping") == "textWrapping":
                    say(" ")
            elif tag == w("noBreakHyphen"):
                say("-")
            elif tag == w("sym"):
                code = child.get(w("char")) or ""
                try:
                    value = int(code, 16)
                except ValueError:
                    continue
                # Symbol-font characters sit in the private area and mean
                # nothing without the font; the rest are what they say.
                if not 0xF000 <= value <= 0xF0FF:
                    say(chr(value))
            elif tag in (w("footnoteReference"), w("endnoteReference")):
                notes = self.footnotes if tag == w("footnoteReference") else self.endnotes
                note = notes.get(child.get(w("id")) or "")
                if note is not None and not self._hidden():
                    self.notes.append(self._note_text(note))
                    say("\\[%d\\]" % len(self.notes), raw=True)
            elif tag == w("commentReference"):
                self._comment(child.get(w("id")) or "", say)
            elif tag == w("drawing"):
                self._drawing(child)
            elif tag in (w("pict"), w("object")):
                self._vml(child)
            elif _local(tag) == "AlternateContent":
                choice = child.find("{%s}Choice" % MC)
                if choice is not None:
                    for inner in choice:
                        if inner.tag == w("drawing"):
                            self._drawing(inner)
                        elif inner.tag in (w("pict"), w("object")):
                            self._vml(inner)

    # Notes and comments.

    def _flat(self, parent: ET.Element) -> str:
        """Paragraphs inside something small — a note, a comment, a cell — as
        one line."""
        out: List[str] = []
        saved = (self.after, self.fields)
        self.after, self.fields = [], []
        self.in_cell += 1
        try:
            self._blocks(parent, out)
        finally:
            self.in_cell -= 1
            extra = self.after
            self.after, self.fields = saved
        return mdtext.cell(" · ".join(b for b in out + extra if b.strip()))

    def _note_text(self, note: ET.Element) -> str:
        return self._flat(note)

    def _comment(self, cid: str, say) -> None:
        comment = self.comment_parts.get(cid)
        if comment is None or cid in self.comment_numbers:
            return
        number = len(self.comments) + 1
        self.comment_numbers[cid] = number
        author = comment.get(w("author")) or ""
        when = (comment.get(w("date")) or "")[:10]
        head = ", ".join(x for x in (author, when) if x)
        self.comments.append((cid, (mdtext.escape(head) + ": " if head else "") + self._flat(comment)))
        say("\\[%s\\]" % mdtext.escape(self.tr("comment {n}", {"n": number})), raw=True)

    def _endnotes(self, out: List[str]) -> None:
        if self.notes:
            out.append(mdtext.heading(2, mdtext.escape(self.tr("Notes"))))
            for number, text in enumerate(self.notes, 1):
                out.append("\\[%d\\] %s" % (number, text))
        if self.comments:
            out.append(mdtext.heading(2, mdtext.escape(self.tr("Comments"))))
            for number, (cid, text) in enumerate(self.comments, 1):
                quote = "".join(q for q in self.quotes.get(cid, []) if q != "\0").strip()
                quote = mdtext.squeeze(quote)
                if len(quote) > 300:
                    quote = quote[:300].rsplit(" ", 1)[0] + "…"
                label = "**%s**" % mdtext.escape(self.tr("Comment {n}", {"n": number}))
                if quote:
                    out.append("> " + mdtext.escape(quote))
                out.append("%s — %s" % (label, text))

    # Pictures.

    def _drawing(self, drawing: ET.Element) -> None:
        for boxed in drawing.iter(w("txbxContent")):
            self._textbox(boxed)
        frame = None
        for tag in ("inline", "anchor"):
            frame = drawing.find("{%s}%s" % (WP, tag))
            if frame is not None:
                break
        if frame is None:
            return
        extent = frame.find("{%s}extent" % WP)
        size = None
        if extent is not None:
            try:
                cx, cy = int(extent.get("cx") or 0), int(extent.get("cy") or 0)
                if cx > 0 and cy > 0:
                    size = (max(1, round(cx / EMU_PER_PIXEL)), max(1, round(cy / EMU_PER_PIXEL)))
            except ValueError:
                size = None
        doc = frame.find("{%s}docPr" % WP)
        caption = ""
        if doc is not None:
            caption = doc.get("descr") or doc.get("title") or ""
        blip = frame.find(".//{%s}blip" % A)
        if blip is None:
            return
        rid = blip.get("{%s}embed" % REL)
        self._picture(rid, caption, size)

    def _vml(self, element: ET.Element) -> None:
        for boxed in element.iter(w("txbxContent")):
            self._textbox(boxed)
        image = element.find(".//{%s}imagedata" % VML)
        if image is None:
            return
        rid = image.get("{%s}id" % REL)
        caption = image.get("{urn:schemas-microsoft-com:office:office}title") or ""
        size = None
        shape = element.find(".//{%s}shape" % VML)
        if shape is not None:
            style = dict(
                part.split(":", 1) for part in (shape.get("style") or "").split(";") if ":" in part
            )
            try:
                width = _points(style.get("width", ""))
                height = _points(style.get("height", ""))
                if width and height:
                    size = (round(width * 96 / 72), round(height * 96 / 72))
            except ValueError:
                size = None
        self._picture(rid, caption, size)

    def _picture(self, rid: Optional[str], caption: str, size) -> None:
        caption = " ".join(caption.replace("[", "(").replace("]", ")").split())
        found = self.rels.get(rid or "")
        if found is None or found[2]:
            if caption:
                self.after.append(mdtext.escape("[%s]" % caption))
            return
        part = found[1]
        extension = part.rsplit(".", 1)[-1].lower() if "." in part else ""
        if self.in_cell or extension not in DRAWN or not self.package.has(part):
            what = caption or extension.upper() or self.tr("picture")
            self.after.append(mdtext.escape("[%s]" % self.tr("picture: {what}", {"what": what})))
            return
        key = "p%d" % (len(self.result.pictures) + 1)
        width, height = size if size else (None, None)
        self.result.pictures[key] = Picture(self.package.later(part), width, height)
        self.after.append("![%s](picture:%s)" % (caption, key))

    def _textbox(self, content: ET.Element) -> None:
        out: List[str] = []
        saved = self.after
        self.after = []
        self._blocks(content, out)
        self.after = saved + out + self.after

    # Tables.

    def _table(self, table: ET.Element) -> str:
        rows: List[List[str]] = []
        header = False
        look = table.find("%s/%s" % (w("tblPr"), w("tblLook")))
        if look is not None:
            first = look.get(w("firstRow"))
            if first is not None:
                header = first.lower() in ("1", "true", "on")
            else:
                try:
                    header = bool(int(_val(look) or "0", 16) & 0x0020)
                except ValueError:
                    header = False
        marked = False
        for index, row in enumerate(table.findall(w("tr"))):
            props = row.find(w("trPr"))
            if props is not None and props.find(w("del")) is not None:
                continue
            if index == 0 and props is not None and props.find(w("tblHeader")) is not None:
                marked = True
            cells: List[str] = []
            for cell in row:
                if cell.tag == w("sdt"):
                    cell = cell.find(w("sdtContent"))
                    cell = cell.find(w("tc")) if cell is not None else None
                if cell is None or cell.tag != w("tc"):
                    continue
                cprops = cell.find(w("tcPr"))
                span = 1
                merged = False
                if cprops is not None:
                    span = int(_val(cprops.find(w("gridSpan"))) or 1)
                    vmerge = cprops.find(w("vMerge"))
                    merged = vmerge is not None and (_val(vmerge) or "continue") != "restart"
                text = "" if merged else self._cell(cell)
                cells.append(text)
                cells.extend([""] * (span - 1))
            rows.append(cells)
        if not rows:
            return ""
        return mdtext.table(rows, (marked or header) and len(rows) > 1)

    def _cell(self, cell: ET.Element) -> str:
        parts: List[str] = []
        for child in cell:
            if child.tag == w("tbl"):
                nested = []
                for row in child.iter(w("tc")):
                    text = self._cell(row)
                    if text:
                        nested.append(text)
                parts.append(" · ".join(nested))
            elif child.tag != w("tcPr"):
                holder = ET.Element("x")
                holder.append(child)
                text = self._flat(holder)
                if text:
                    parts.append(text)
        return mdtext.cell(" · ".join(parts))

    # What the file says about itself.

    def _meta(self) -> Dict[str, str]:
        meta: Dict[str, str] = {}
        core = _parse(self.package.read("docProps/core.xml"))
        if core is not None:
            for element in core:
                name = _local(element.tag)
                if element.text and element.text.strip():
                    meta[name] = element.text.strip()
        app = _parse(self.package.read("docProps/app.xml"))
        if app is not None:
            for element in app:
                name = _local(element.tag)
                if name in ("Pages", "Words", "Application", "Company") and element.text:
                    meta[name] = element.text.strip()
        return meta


def _points(value: str) -> Optional[float]:
    found = re.fullmatch(r"\s*([\d.]+)\s*(pt|in|cm|mm|px)?\s*", value)
    if not found:
        return None
    number = float(found.group(1))
    unit = found.group(2) or "px"
    return number * {"pt": 1, "in": 72, "cm": 72 / 2.54, "mm": 72 / 25.4, "px": 0.75}[unit]


def convert(package: Package, tr: Callable[..., str]) -> Result:
    return Converter(package, tr).convert()
