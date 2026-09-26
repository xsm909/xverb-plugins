# Documents as Markdown

A Word file — `.docx` or the older `.doc` — or a book — EPUB or FictionBook — read as a **document**, not laid out as pages — the same idea as
PDF as Markdown, for files that were text to begin with.

F3 on a `.docx` gives the headings, the lists, the tables, the links, the notes,
the comments and the pictures. The fonts, the colours, the page breaks, the
headers and the footers are thrown away: that is the point of reading a document
as text. The page as it was set is one **Enter** away, in whatever this machine
opens Word files with. Shift+F3 still shows the file as the zip it is.

Nothing third-party is shipped: the standard library reads the zip and the XML.

## What becomes what

| In the file | In the reading |
| --- | --- |
| A heading style, or any style with an outline level | `#` … `######` — found by the style's English name and outline level, followed up `basedOn`, because in a Russian Word the id of "Heading 1" is `1` or `a3` |
| A list | its label as Word counts it: bullets and plain numbers as Markdown lists, `1.1.`, `а)`, `iv.` as the paragraph's own first words |
| Bold, italic, struck through | `**`, `*`, `~~` — the host's reader is flat, so one of them where the file says two |
| A table | a pipe table; a span leaves the cells it covers empty, a cell merged downwards is empty below its first row, lines of a cell are joined with ` · ` |
| A link, or a `HYPERLINK` field | a link |
| Any other field | its result — the page number, the date — never its code |
| Word's table of contents | left out: the structure panel is one |
| A tracked change | accepted: inserted text read, deleted text not, and a line at the top says how many |
| A footnote or an endnote | `[1]` where it stands, and the text under **Notes** at the end |
| A comment | `[comment 1]` where it stands, and under **Comments** at the end the words it is on, who wrote it and when |
| A picture | drawn where it stands at its size in the document, fetched only when scrolled to; EMF and WMF, which the application cannot draw, are their captions |
| A text box | its paragraphs after the paragraph it is anchored to |
| Headers, footers, fonts, colours, page and section breaks | nothing |

**A document with no heading styles at all** — written by hand, with bold
paragraphs for its sections — has its short, wholly bold paragraphs that do not
end like a sentence read as headings. Only then: one real heading in the file
and nothing is guessed.

A password-protected document is not a zip but a compound file, and says so.

## Word 97–2003: `.doc`

The binary format is read with the standard library too. The text is in pieces
listed by the piece table, some one byte a character and some two; whether a
paragraph is a heading or in a table is its properties, found through the PAPX
pages by the position of its end — and the style's built-in identifier says
"heading 1" whatever language Word was in. Bold, italic and struck through come
from the character properties. Pictures are found in Word's picture store,
inline ones through their block in the `Data` stream and floating ones through
their anchor and shape; a picture kept as EMF or WMF is its caption, naming the
format.

Not yet: list numbers in a .doc (its list items are bullets), comments' authors,
and anything Word 95 or older wrote.

**Which reader is decided by the file, not by its name**: a `.doc` that is
really a `.docx` is read as one, and a Rich Text file named `.doc` says what it
is.

## Books: EPUB and FictionBook

**EPUB 2 and 3.** The package file names the chapters and their order; each is
read with a forgiving HTML parser, because a chapter meant to be XHTML is
often only HTML. Headings, lists, quotations, tables, links and pictures come
from the tags; style sheets are not read. A book whose pages mark their
headings only with classes — what most converters from FB2 write — has its
headings from its own table of contents (`nav.xhtml`, or `toc.ncx`): every
place the contents point at is a heading at the depth the contents give it,
and the paragraphs after it that only repeat the title are not written again.
EPUB 3's notes (`epub:type="footnote"`) go to the end of their chapter, their
mark left where it was. A picture is recognised by its bytes, not its name.
Fonts obfuscated the way EPUB allows are no obstacle; anything else encrypted
is DRM, and the book says so.

**FictionBook** (`.fb2`, and `.fb2.zip` — found by a probe that reads the
zip's first member's name, so an ordinary zip is still an archive). One XML
file, in UTF-8 or windows-1251: sections are headings by how deep they sit,
verses keep their lines, epigraphs and citations are quotations, the notes
body is the last chapter, and the pictures are the file's own base64.

The cover comes first, then the title and the author; it is also the book's
picture on the strip and on its About card.

## Which Xverb it needs

Pictures in a reading, and the escapes that keep a document's asterisks from
turning into emphasis, are plugin API level 2 — Xverb 1.1.0.502 and later. An
older Xverb lists this plugin greyed, with Install turned off.

## Files

| File | What it does |
| --- | --- |
| `main.py` | The viewer and the About card |
| `docx.py` | Word 2007 and later: relationships, styles, numbering, the body, notes, comments, pictures |
| `doc.py` | Word 97–2003: the FIB, the piece table, paragraph and character properties, tables, notes, the picture store |
| `epub.py` | EPUB: container, package, contents, cover, DRM |
| `xhtml.py` | A chapter's XHTML as Markdown, with headings from the contents |
| `fb2.py` | FictionBook, and the probe for .fb2.zip |
| `compound.py` | The compound file a .doc lives in, the same reader the sheets plugin has |
| `mdtext.py` | The Markdown every converter writes: escaping, marks, tables |
| `package.py` | A zip opened where it lies, and opened again when a picture is wanted |

## Checking it

```
PYTHONPATH=<xverb>/assets/python python3 selftest.py [documents…]
```

The Word files are built by the test in the shape Word writes them; they are
not files Word saved. Real documents named on the command line are read and
summarised.
