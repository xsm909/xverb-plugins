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

"""Which files are one archive: the volumes of a RAR set, first to last.

`name.part1.rar`, `name.part2.rar`… since RAR 3, and before that `name.rar`,
`name.r00`, `name.r01`… Any volume of a set opens the whole set, because the
file someone presses Enter on is whichever one their cursor was on.
"""

from __future__ import annotations

import os
import re
from typing import List

_PART = re.compile(r"^(?P<stem>.*)\.part(?P<number>\d+)\.rar$", re.IGNORECASE)
_OLD = re.compile(r"^(?P<stem>.*)\.(rar|r\d\d)$", re.IGNORECASE)


def volumes(path: str) -> List[str]:
    """Every volume of the set ``path`` belongs to, first to last, or just
    ``path`` when it is an archive on its own."""
    folder, name = os.path.split(path)
    try:
        names = os.listdir(folder or ".")
    except OSError:
        return [path]

    match = _PART.match(name)
    if match:
        stem = match.group("stem").lower()
        found = {}
        for other in names:
            m = _PART.match(other)
            if m and m.group("stem").lower() == stem:
                found[int(m.group("number"))] = os.path.join(folder, other)
        numbers = sorted(found)
        if numbers and numbers[0] <= 1 and numbers == list(range(numbers[0], numbers[-1] + 1)):
            return [found[n] for n in numbers]
        return [path]

    match = _OLD.match(name)
    if match:
        stem = match.group("stem")
        first = None
        rest = {}
        for other in names:
            m = _OLD.match(other)
            if not m or m.group("stem").lower() != stem.lower():
                continue
            extension = other.rsplit(".", 1)[-1].lower()
            if extension == "rar":
                first = os.path.join(folder, other)
            else:
                rest[int(extension[1:])] = os.path.join(folder, other)
        if first is not None and rest:
            numbers = sorted(rest)
            if numbers == list(range(len(numbers))):
                return [first] + [rest[n] for n in numbers]
    return [path]
