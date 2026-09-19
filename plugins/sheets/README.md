# Spreadsheets

Excel and OpenDocument workbooks, read as sheets. F3 on a workbook opens its
first sheet in the host's own grid — the headings, the row numbers, selecting
cells, rows, columns and ranges, the keys — and a pill in the corner turns to
the others (Ctrl+PgUp and Ctrl+PgDn do the same).

| Format | Extensions | Read by |
| --- | --- | --- |
| Office Open XML | `.xlsx`, `.xlsm`, `.xltx`, `.xltm` | `xlsx.py` |
| Excel 97–2003 (BIFF8) | `.xls`, `.xlt` | `xls.py` |
| OpenDocument | `.ods`, `.ots` | `ods.py` |

Everything is the standard library; nothing third-party is shipped.

## What is shown

- **Values as the file formats them.** A date is a date, not the day number
  it is stored as; `#,##0.00` groups the thousands; a percentage is one; text
  in a format's quotes stays round the number. The number itself is still the
  value, which is what a selection is summed from. Numbers are written the way
  the application's language writes them — a decimal comma in Russian.
- **The value a formula came to** when the file was saved. Formulas are not
  worked out again.
- **Errors** (`#DIV/0!`, `#N/A`) as errors, in the palette's own colour for one.
- Whether the first row is a header is the host's guess, overruled with one
  press.

## What is not

Charts, pictures, comments, merged cells, colours and fonts, hidden rows, and
anything a macro does. Excel 95 and older, password-protected workbooks, the
Excel 2003 XML format and web pages saved with an `.xls` name say what they are
rather than failing obscurely. Nothing is ever written.

## How it is sent

Only the rows on screen cross the pipe to the host: 256 when a sheet opens, and
more as it is scrolled. A sheet is read whole, into the plugin's memory, when
it is first turned to; a workbook of many sheets keeps only the one being
looked at. **Rows to read at most** in the plugin's settings caps a sheet for
a machine that would rather not hold a million rows.

## Checking it

    python3 selftest.py                 # workbooks made up here
    python3 selftest.py <folder>        # and every workbook under <folder>

The made-up ones include the one trap worth a test of its own: an `.xls`
string table cut by a CONTINUE record in the middle of a string, the second
half written two bytes a character where the first was one.
