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

"""libarchive, through ctypes, for the one thing it is here for: reading RAR.

**Why libarchive and nothing else.** RARLAB's own UnRAR sources come with a
licence that forbids using them to re-create the compressor, which makes them
incompatible with the GPL this plugin is under — and 7-Zip's RAR codec is
derived from the same code. libarchive's RAR and RAR5 readers were written
independently and are BSD-licensed, which the GPL takes in without a word.
Nobody can *create* a RAR without RARLAB's program, so this is a reader and
nothing more.

**Where it comes from.** macOS has had libarchive in ``/usr/lib`` for as long
as it has had ``tar``, with RAR5 in it, so nothing is shipped there. Windows
and Linux get one built by ``native/build.sh`` with zig — no zlib, no iconv,
nothing beside it to install; the formats RAR needs are all libarchive's own
code. A Linux machine that has ``libarchive.so.13`` of its own is used when
the shipped one is missing. ``XVERB_RAR_LIBARCHIVE`` names a library to use
instead, for trying a build somewhere it is not shipped.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import sys
from typing import Callable, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))

OK = 0
EOF = 1
RETRY = -10
WARN = -20
FAILED = -25
FATAL = -30

#: `archive_entry_filetype`, as the stat bits it is.
IFMT = 0o170000
IFREG = 0o100000
IFDIR = 0o040000
IFLNK = 0o120000

_lib = None
_tried = False
_where: List[str] = []


def target() -> Optional[str]:
    """This machine's folder under `native/`, as build.sh names them."""
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if arch is None:
        return None
    if os.name == "nt":
        return "windows-" + arch
    if sys.platform.startswith("linux"):
        return "linux-" + arch
    if sys.platform == "darwin":
        return "macos-" + arch
    return None


def _candidates() -> List[str]:
    given = os.environ.get("XVERB_RAR_LIBARCHIVE")
    if given:
        return [given]
    found = []
    folder = target()
    if folder is not None:
        name = {"windows": "archive.dll", "linux": "libarchive.so",
                "macos": "libarchive.dylib"}[folder.split("-")[0]]
        found.append(os.path.join(HERE, "native", folder, name))
    if sys.platform == "darwin":
        # In the dyld cache rather than on the disk since Big Sur, which is
        # why the file is not there to see and still loads.
        found.append("/usr/lib/libarchive.2.dylib")
    elif sys.platform.startswith("linux"):
        found.append("libarchive.so.13")
        other = ctypes.util.find_library("archive")
        if other:
            found.append(other)
    return found


def _bind(lib) -> None:
    p, i, s, i64 = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int64
    table = {
        "archive_version_string": (s, []),
        "archive_read_new": (p, []),
        "archive_read_support_format_rar": (i, [p]),
        "archive_read_support_format_rar5": (i, [p]),
        "archive_read_open_filename": (i, [p, s, ctypes.c_size_t]),
        "archive_read_open_filenames": (i, [p, ctypes.POINTER(s), ctypes.c_size_t]),
        "archive_read_set_read_callback": (i, [p, p]),
        "archive_read_set_seek_callback": (i, [p, p]),
        "archive_read_set_skip_callback": (i, [p, p]),
        "archive_read_set_callback_data": (i, [p, p]),
        "archive_read_open1": (i, [p]),
        "archive_read_next_header": (i, [p, ctypes.POINTER(p)]),
        "archive_read_data": (ctypes.c_ssize_t, [p, p, ctypes.c_size_t]),
        "archive_read_data_skip": (i, [p]),
        "archive_read_free": (i, [p]),
        "archive_error_string": (s, [p]),
        "archive_entry_pathname_utf8": (s, [p]),
        "archive_entry_pathname": (s, [p]),
        "archive_entry_symlink_utf8": (s, [p]),
        "archive_entry_hardlink_utf8": (s, [p]),
        "archive_entry_size": (i64, [p]),
        "archive_entry_size_is_set": (i, [p]),
        "archive_entry_mtime": (i64, [p]),
        "archive_entry_mtime_is_set": (i, [p]),
        "archive_entry_filetype": (ctypes.c_uint, [p]),
        "archive_entry_is_encrypted": (i, [p]),
    }
    for name, (restype, argtypes) in table.items():
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = argtypes
    if os.name == "nt":
        wide = ctypes.c_wchar_p
        lib.archive_read_open_filename_w.restype = i
        lib.archive_read_open_filename_w.argtypes = [p, wide, ctypes.c_size_t]
        lib.archive_entry_pathname_w.restype = wide
        lib.archive_entry_pathname_w.argtypes = [p]


def library():
    """The loaded library, or None — :func:`searched` then says where from."""
    global _lib, _tried
    if _tried:
        return _lib
    _tried = True
    for path in _candidates():
        _where.append(path)
        try:
            lib = ctypes.CDLL(path)
            _bind(lib)
        except (OSError, AttributeError):
            continue
        _lib = lib
        return lib
    return None


def searched() -> List[str]:
    library()
    return list(_where)


def version() -> str:
    lib = library()
    if lib is None:
        return ""
    return (lib.archive_version_string() or b"").decode("ascii", "replace")


class ArchiveError(Exception):
    """What libarchive said, in its own words."""


READ = ctypes.CFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p))
SKIP = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_int64)
SEEK = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_int64, ctypes.c_int)


class Member:
    """One header, as far as a listing needs it."""

    __slots__ = ("index", "path", "size", "modified", "kind", "encrypted", "target")

    def __init__(self, index, path, size, modified, kind, encrypted, target):
        self.index = index
        self.path = path
        self.size = size
        self.modified = modified
        self.kind = kind
        self.encrypted = encrypted
        self.target = target


class Reader:
    """One pass through an archive, front to back — the only way libarchive
    reads, and the only way a solid RAR can be read by anybody.

    Opened from files on this machine (every volume of a multi-part set, in
    order), or from a file-like object whose bytes come from anywhere.
    """

    def __init__(self, paths: Optional[List[str]] = None, stream=None):
        lib = library()
        if lib is None:
            raise ArchiveError("no libarchive")
        self._lib = lib
        self._a = lib.archive_read_new()
        if not self._a:
            raise ArchiveError("libarchive would not start a reader")
        self._keep = []
        self._buffer = None
        self.index = -1
        self.entry = None
        self.read_so_far = 0
        lib.archive_read_support_format_rar(self._a)
        lib.archive_read_support_format_rar5(self._a)
        try:
            if stream is not None:
                self._open_stream(stream)
            elif paths and len(paths) == 1:
                self._open_path(paths[0])
            elif paths:
                self._open_paths(paths)
            else:
                raise ArchiveError("nothing to open")
        except Exception:
            self.close()
            raise

    def _check(self, result: int) -> int:
        if result < WARN:
            raise ArchiveError(self.error() or "libarchive failed (%d)" % result)
        return result

    def error(self) -> str:
        said = self._lib.archive_error_string(self._a) if self._a else None
        return (said or b"").decode("utf-8", "replace")

    def _open_path(self, path: str) -> None:
        if os.name == "nt":
            self._check(self._lib.archive_read_open_filename_w(self._a, path, 1 << 16))
        else:
            self._check(self._lib.archive_read_open_filename(
                self._a, os.fsencode(path), 1 << 16))

    def _open_paths(self, paths: List[str]) -> None:
        # Windows has no wide form of the list in every libarchive, and the
        # names of the parts of one set differ only by a number: the ANSI
        # form is enough when the folder's own name is ANSI, and a set in a
        # folder that is not is read one volume at a time instead.
        names = (ctypes.c_char_p * (len(paths) + 1))()
        for at, path in enumerate(paths):
            names[at] = os.fsencode(path) if os.name != "nt" else path.encode("mbcs")
        names[len(paths)] = None
        self._keep.append(names)
        self._check(self._lib.archive_read_open_filenames(self._a, names, 1 << 16))

    def _open_stream(self, stream) -> None:
        chunk = ctypes.create_string_buffer(1 << 18)

        def read(_a, _data, where):
            try:
                piece = stream.read(len(chunk))
            except Exception:  # noqa: BLE001 - libarchive only wants -1
                return -1
            if not piece:
                return 0
            ctypes.memmove(chunk, piece, len(piece))
            where[0] = ctypes.cast(chunk, ctypes.c_void_p)
            return len(piece)

        def skip(_a, _data, count):
            try:
                before = stream.tell()
                after = stream.seek(count, os.SEEK_CUR)
                return after - before
            except Exception:  # noqa: BLE001
                return 0

        def seek(_a, _data, offset, whence):
            try:
                return stream.seek(offset, whence)
            except Exception:  # noqa: BLE001
                return -30

        callbacks = (READ(read), SKIP(skip), SEEK(seek))
        self._keep.extend([chunk, callbacks])
        lib, a = self._lib, self._a
        lib.archive_read_set_read_callback(a, ctypes.cast(callbacks[0], ctypes.c_void_p))
        lib.archive_read_set_skip_callback(a, ctypes.cast(callbacks[1], ctypes.c_void_p))
        lib.archive_read_set_seek_callback(a, ctypes.cast(callbacks[2], ctypes.c_void_p))
        lib.archive_read_set_callback_data(a, None)
        self._check(lib.archive_read_open1(a))

    def next(self) -> Optional[Member]:
        """The next header, its data skipped over; None at the end."""
        entry = ctypes.c_void_p()
        result = self._lib.archive_read_next_header(self._a, ctypes.byref(entry))
        if result == EOF:
            self.entry = None
            return None
        self._check(result)
        self.index += 1
        self.entry = entry.value
        self.read_so_far = 0
        return self._member(entry.value)

    def _member(self, entry) -> Member:
        lib = self._lib
        raw = lib.archive_entry_pathname_utf8(entry)
        if raw is not None:
            path = raw.decode("utf-8", "replace")
        elif os.name == "nt":
            path = lib.archive_entry_pathname_w(entry) or ""
        else:
            path = os.fsdecode(lib.archive_entry_pathname(entry) or b"")
        kind = lib.archive_entry_filetype(entry) & IFMT
        target = lib.archive_entry_symlink_utf8(entry)
        if kind != IFLNK:
            target = None
        return Member(
            index=self.index,
            path=path,
            size=lib.archive_entry_size(entry) if lib.archive_entry_size_is_set(entry) else 0,
            modified=float(lib.archive_entry_mtime(entry))
            if lib.archive_entry_mtime_is_set(entry) else None,
            kind=kind,
            encrypted=bool(lib.archive_entry_is_encrypted(entry)),
            target=target.decode("utf-8", "replace") if target else None,
        )

    def read(self, length: int) -> bytes:
        """Up to ``length`` bytes more of the member the last :meth:`next`
        stopped at; fewer only at its end."""
        if self._buffer is None or len(self._buffer) < min(length, 1 << 20):
            self._buffer = ctypes.create_string_buffer(max(1 << 16, min(length, 1 << 20)))
        parts = []
        wanted = length
        while wanted > 0:
            got = self._lib.archive_read_data(
                self._a, self._buffer, min(wanted, len(self._buffer)))
            if got < 0:
                raise ArchiveError(self.error() or "the member could not be read")
            if got == 0:
                break
            parts.append(self._buffer.raw[:got])
            wanted -= got
        data = b"".join(parts)
        self.read_so_far += len(data)
        return data

    def close(self) -> None:
        if self._a:
            self._lib.archive_read_free(self._a)
            self._a = None
        self._keep.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def walk(open_reader: Callable[[], Reader]) -> List[Member]:
    """Every header in the archive, in the order it holds them."""
    with open_reader() as reader:
        members = []
        while True:
            member = reader.next()
            if member is None:
                return members
            members.append(member)
