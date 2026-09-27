# SQLite databases

SQLite databases as sheets. F3 on a database opens its first table in the
host's own grid — headings, row numbers, selecting, sorting, searching,
copying — and the pill in the corner turns to every other table and view
(Ctrl+PgUp and Ctrl+PgDn do the same). Shift+F3 gives the report instead.

Claims `.db`, `.sqlite`, `.sqlite3`, `.db3`, `.s3db`, `.sl3`, `.sqlitedb`, and
GeoPackage `.gpkg` and `.mbtiles`, which are SQLite underneath — **by the
file's first bytes**, not only its name: a `.db` that is not SQLite, Windows'
`Thumbs.db` among them, is left to whatever else reads it.

Everything is Python's own `sqlite3`; nothing third-party is shipped.

## What is shown

- **Every table and view**, tables first, in the order they were made.
  SQLite's own tables are left out.
- **The column names as the header**, always — a table knows its columns, so
  nothing is guessed.
- **A BLOB as what it is and how big**: `PNG · 12.4 KB`, `plist · 830 B`, or
  `BLOB · 2.0 MB` when its first bytes say nothing.
- **Text that is not UTF-8** — an old program writing Latin-1 into a TEXT
  column — shown with the odd character replaced, not refused.
- **A view that cannot be read**, because a table it reads is gone, says so
  on its page; the other pages still open.
- The sheet's menu adds **Copy the statement that made it** and **Copy a
  SELECT of it**.

## The report (Shift+F3)

The file's size, page size, text encoding and journal; the version of SQLite
that last wrote it; every table with its row count, its columns and their
types, keys, defaults, foreign keys and indexes, and the `CREATE` statement;
every view and trigger with its SQL. Counting stops after ten seconds in all,
and a table not reached says *not counted*.

## How it reads

- **Only the rows on screen**, with `LIMIT` and `OFFSET`, a block of 1024
  rows at a time and sixteen blocks kept. A table of a million rows opens in
  milliseconds, and a screen from its end comes in about twenty.
- **Read-only, and never held open.** Every question opens the file, asks and
  closes it, so a database looked at can still be moved or deleted on Windows.
- **A database another program holds** — a browser's history while it runs,
  a WAL database whose `-shm` cannot be made — is copied aside with its `-wal`
  and the copy read: a snapshot of the moment. The copies go when the plugin
  stops.
- **On another file system** (FTP, SFTP, an archive) the database is brought
  here first, with its `-wal`, up to 512 MB.
- A view that takes more than eight seconds to count shows its first hundred
  thousand rows.

## What is not

Nothing is written, ever, and no SQL is typed in: this is for looking.
Encrypted databases (SQLCipher) say they cannot be read.

## Checking it

    python3 selftest.py                 # databases made up here
    python3 selftest.py <files…>        # and these, summed up
