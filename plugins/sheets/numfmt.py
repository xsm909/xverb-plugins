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

"""What a spreadsheet's number format makes of a number.

A cell in a workbook holds a bare number and, beside it, the name of a format:
`0.00`, `#,##0`, `dd.mm.yyyy`, `0%`. The number is the value — what a sum adds
up — and the format is how the file wants it read. A date is the sharpest case:
the file holds 46266 and means the first of September 2026.

Only what changes the reading is done here: whether it is a date, a time or
both; how many decimals; thousands grouped; a percentage; text around the
number in quotes. Colours, conditions, fractions and padding to a width are
left out — the palette answers for colour, and the rest is decoration.

Numbers are written the way the application's language writes them: a decimal
comma in Russian, German or French, a point in English. A spreadsheet program
does the same with the same file, which is why the file does not say.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

#: Built-in formats, by number, as every Excel writes them without saying.
BUILTIN = {
    0: "General",
    1: "0",
    2: "0.00",
    3: "#,##0",
    4: "#,##0.00",
    9: "0%",
    10: "0.00%",
    11: "0.00E+00",
    12: "# ?/?",
    13: "# ??/??",
    14: "yyyy-mm-dd",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "yyyy-mm-dd h:mm",
    37: "#,##0 ;(#,##0)",
    38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)",
    40: "#,##0.00;[Red](#,##0.00)",
    45: "mm:ss",
    46: "[h]:mm:ss",
    47: "mmss.0",
    48: "##0.0E+0",
    49: "@",
}

#: Languages whose decimal mark is a comma — the host's language codes.
COMMA_LANGUAGES = {"ru", "uk", "de", "fr", "es", "it", "fi", "nb"}

_QUOTED = re.compile(r'"[^"]*"')
_BRACKET = re.compile(r"\[[^\]]*\]")
_ESCAPED = re.compile(r"\\.")


class Format:
    """A number format, read once and applied to every cell that names it."""

    __slots__ = ("code", "date", "time", "percent", "decimals", "grouped",
                 "prefix", "suffix", "text", "scientific", "elapsed")

    def __init__(self, code: str):
        self.code = code or "General"
        first = self.code.split(";")[0]
        # What is left once literal text and bracketed switches are taken out
        # is what the number itself is drawn with.
        bare = _ESCAPED.sub("", _QUOTED.sub("", first))
        self.elapsed = bool(re.search(r"\[[hms]+\]", bare, re.I))
        bare = _BRACKET.sub("", bare)
        lower = bare.lower()
        self.text = bare.strip() == "@"
        self.date = any(c in lower for c in "dy") or (
            "m" in lower and not any(c in lower for c in "hs"))
        self.time = any(c in lower for c in "hs") or self.elapsed
        if lower.strip() in ("general", ""):
            self.date = self.time = False
        # "m" between hours and seconds is minutes, and a format of minutes
        # alone is a time — but "mmm-yy" is a month.
        if self.time and not any(c in lower for c in "dy"):
            self.date = False
        self.percent = "%" in bare
        self.scientific = "e+" in lower or "e-" in lower
        digits = bare.split(".", 1)
        self.decimals = (
            len(re.findall(r"[0#?]", digits[1].split("e")[0].split("E")[0]))
            if len(digits) == 2 else 0
        )
        self.grouped = bool(re.search(r"[0#?],[0#?]", bare))
        self.prefix, self.suffix = _around(first)

    @property
    def is_date(self) -> bool:
        return self.date or self.time


def _around(section: str) -> tuple:
    """The text a format writes before the number and after it: `"Итого: "`,
    a currency sign from `[$€-407]`, a unit after a space."""
    places = []
    quoted = False
    i = 0
    while i < len(section):
        c = section[i]
        if c == '"':
            quoted = not quoted
        elif c == "\\" and not quoted:
            i += 1
        elif c == "[" and not quoted:
            end = section.find("]", i)
            i = len(section) if end < 0 else end
        elif not quoted and c in "0#?":
            places.append(i)
        i += 1
    if not places:
        return "", ""
    return (_literal(section[: places[0]]).lstrip(),
            _literal(section[places[-1] + 1:]).rstrip())


def _literal(part: str) -> str:
    out = []
    i = 0
    while i < len(part):
        c = part[i]
        if c == '"':
            end = part.find('"', i + 1)
            end = len(part) if end < 0 else end
            out.append(part[i + 1:end])
            i = end + 1
            continue
        if c == "\\":
            out.append(part[i + 1:i + 2])
            i += 2
            continue
        if c == "[":
            end = part.find("]", i)
            end = len(part) if end < 0 else end
            inner = part[i + 1:end]
            if inner.startswith("$"):
                out.append(inner[1:].split("-")[0])
            i = end + 1
            continue
        if c in "_*":
            # Padding: as wide as the character after it. A space reads the
            # same and keeps the number where the format meant it to stand.
            out.append(" " if c == "_" else "")
            i += 2
            continue
        if c in ",.%":
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def from_serial(serial: float, date1904: bool = False) -> Optional[dt.datetime]:
    """A spreadsheet's day number as a moment.

    Day 1 is the first of January 1900 — and day 60 is the 29th of February
    1900, a day that never was: Lotus 1-2-3 thought 1900 a leap year and Excel
    kept the mistake so its files would agree. Days before it are one off, which
    is why they are counted from the 31st of December rather than the 30th.
    """
    if serial < 0 or serial > 2958466:
        return None
    if date1904:
        base = dt.datetime(1904, 1, 1)
    elif serial < 60:
        base = dt.datetime(1899, 12, 31)
    else:
        base = dt.datetime(1899, 12, 30)
    # To the nearest millisecond, or 0.1 + 0.2 of a day is a second too few.
    return base + dt.timedelta(milliseconds=round(serial * 86400000))


def show(value: float, fmt: Format, language: str = "en",
         date1904: bool = False) -> str:
    """How [value] reads under [fmt], in [language]'s way of writing numbers."""
    comma = language.split("-")[0] in COMMA_LANGUAGES
    if fmt.is_date:
        moment = from_serial(value, date1904)
        if moment is not None:
            if fmt.elapsed:
                seconds = round(value * 86400)
                return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)
            if fmt.date and fmt.time:
                return moment.strftime("%Y-%m-%d %H:%M:%S").removesuffix(":00") \
                    if moment.second == 0 else moment.strftime("%Y-%m-%d %H:%M:%S")
            if fmt.date:
                return moment.strftime("%Y-%m-%d")
            return moment.strftime("%H:%M:%S") if moment.second else moment.strftime("%H:%M")
    if fmt.percent:
        return _number(value * 100, fmt.decimals, fmt.grouped, comma) + "%"
    if fmt.scientific:
        text = "%.*E" % (fmt.decimals, value)
        return text.replace(".", ",") if comma else text
    if fmt.code == "General" or fmt.text:
        return general(value, comma)
    return fmt.prefix + _number(value, fmt.decimals, fmt.grouped, comma) + fmt.suffix


def general(value: float, comma: bool = False) -> str:
    """A number with no format: up to fifteen significant digits, and no
    exponent until the number is too large or too small to write out — which
    is what a spreadsheet shows, and not what Python's repr does with 0.1+0.2.
    """
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    text = "%.15g" % value
    if "e" in text and 1e-9 < abs(value) < 1e15:
        text = ("%.15f" % value).rstrip("0").rstrip(".")
    return text.replace(".", ",") if comma else text


def _number(value: float, decimals: int, grouped: bool, comma: bool) -> str:
    # Half away from zero, the way a spreadsheet rounds: 3.25 to one place is
    # 3.3. Python's own formatting rounds the binary number, which is a hair
    # under 3.25, and says 3.2.
    step = Decimal(1).scaleb(-decimals)
    text = str(Decimal(repr(value)).quantize(step, rounding=ROUND_HALF_UP))
    if text in ("-0", "-0." + "0" * decimals):
        text = text[1:]
    whole, _, fraction = text.partition(".")
    if grouped:
        sign = "-" if whole.startswith("-") else ""
        digits = whole.lstrip("-")
        groups = []
        while len(digits) > 3:
            groups.insert(0, digits[-3:])
            digits = digits[:-3]
        groups.insert(0, digits)
        # A narrow no-break space: the same in every language, and never a
        # line break in the middle of a number.
        whole = sign + " ".join(groups)
    if not fraction:
        return whole
    return whole + ("," if comma else ".") + fraction
