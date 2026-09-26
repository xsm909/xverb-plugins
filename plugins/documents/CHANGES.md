# Documents as Markdown: changes

## 0.1.0 — 2026-09-26

- A Word file read as a document on F3 — .docx, .docm, .dotx, .dotm, and Word 97–2003's .doc and .dot: headings, lists with Word's own numbering, tables, links, notes and comments, with the fonts, page breaks, headers and footers thrown away.
- Tracked changes are read as accepted, and the reading says how many there are.
- Pictures are drawn where they stand, at their size in the document, and fetched only when scrolled to. A picture the application cannot draw — EMF, WMF — is its caption.
- A document with no heading styles at all has its short bold paragraphs read as headings, so the structure panel is not empty.
- A .doc is read from its own binary format: the piece table, paragraph and character properties, tables, notes and comments, and the pictures in Word's picture store, inline or floating. Which reader is used is decided by what the file is, not by its name.
- Books: EPUB 2 and 3, and FictionBook — .fb2, and .fb2.zip found by what is inside the zip. The cover first, then the title and the author, then the chapters in reading order, with every picture.
- A book whose pages do not mark their headings — most books converted from FB2 — has them from its own table of contents, at the depth the contents give, and the title is not written twice.
- EPUB 3 notes are written at the end of their chapter with their mark left in place; FictionBook's notes are the book's last chapter. A book under DRM says so.
- The cover is the book's picture on the strip and on its About card.
- About this document: title, author, dates, words, pictures, comments.
- On an application older than 1.1.0.501 it still reads every document, as plain Markdown with each picture as its caption.
- Speaks Russian.
