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

"""A packaged document opened for reading: a zip on this disk or elsewhere.

A .docx, an .odt and an .epub are all zips, and a picture inside one is read
long after the document was opened — when the reader scrolls to it. So what is
kept is the way back to the file, not the file: a path to open again on this
machine, or, for a file that lives behind another plugin's transport, the bytes
it was read as.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from typing import Callable, Optional

from xverb.fs import local_path


class Unreadable(Exception):
    """The file is not the kind of package it is named as, said in a sentence."""


class Package:
    def __init__(self, opener: Callable[[], zipfile.ZipFile]):
        self._open = opener
        self.zip = opener()
        self._names = {name.lower(): name for name in self.zip.namelist()}

    @classmethod
    def open(cls, plugin, url: str, max_bytes: int) -> "Package":
        path = local_path(url)
        try:
            if path:
                return cls(lambda: zipfile.ZipFile(path))
            raw = plugin.read_file(url, max_bytes=max_bytes)
            return cls(lambda: zipfile.ZipFile(io.BytesIO(raw)))
        except zipfile.BadZipFile:
            raise Unreadable("not a zip")

    def name(self, part: str) -> Optional[str]:
        """The member called [part], matched without regard to case — a file
        written on one system and read on another does not always agree."""
        return self._names.get(part.lstrip("/").lower())

    def has(self, part: str) -> bool:
        return self.name(part) is not None

    def read(self, part: str) -> Optional[bytes]:
        found = self.name(part)
        if found is None:
            return None
        return self.zip.read(found)

    def later(self, part: str) -> Callable[[], Optional[bytes]]:
        """A function that reads [part] when it is called, from a fresh handle
        on the file: the one this was opened with is long closed by then."""
        found = self.name(part)

        def read() -> Optional[bytes]:
            if found is None:
                return None
            with self._open() as package:
                return package.read(found)

        return read

    def size(self, part: str) -> int:
        found = self.name(part)
        return self.zip.getinfo(found).file_size if found else 0

    def close(self) -> None:
        self.zip.close()


def join(base: str, target: str) -> str:
    """A relationship's target read against the part it belongs to."""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), target))
