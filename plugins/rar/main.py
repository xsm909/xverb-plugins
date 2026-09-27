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

"""RAR archives as folders, read-only.

Enter one and walk it, F5 out of it to unpack, F3 for a table of what is in
it — the same as a ZIP in the archives plugin, less everything that writes.
**Nobody can write a RAR** but RARLAB's own program, so the scheme says it is
not writable and the host never offers it in the pack dialog; F8, F7 and F6
inside one are refused before they are pressed.

**A plugin of its own, not a third format in `archives`,** because it is a
different kind of thing: it needs a native library (see :mod:`libarc` for why
that one and no other), and a machine without one still has ZIP and tar.

**A RAR is read front to back.** libarchive reads that way, and a *solid*
archive can be read no other way by anybody: every file is compressed as the
continuation of the one before it. So a member is read by a cursor walking the
archive, kept between calls — a copy asks for 256 KB at a time, and each call
carries on where the last one stopped. A copy of a whole folder reads the
members in the archive's own order where it can; going back means starting
again from the first volume.

**Volumes.** `name.part1.rar`, `name.part2.rar`… and the older `name.rar`,
`name.r00`, `name.r01`… are one archive. Entering any volume of a set on this
machine opens the whole set. Over another file system — FTP, somebody else's
plugin — only the volume itself is read, and a member that runs on into the
next one says so.
"""

from __future__ import annotations

import os
import time
import unicodedata
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import hostfile
import libarc
from volumes import volumes
from xverb import (
    DIRECTORY,
    Entry,
    FILE,
    LINK,
    FileSystem,
    Plugin,
    RpcError,
    error,
    local_path,
    table,
)

plugin = Plugin("org.xverb.rar", "RAR archives")

#: How long an archive's listing is trusted without asking whether the file
#: changed. A copy reads a piece every few milliseconds.
TRUST = 1.0

#: How many archives' listings are kept.
KEEP = 4


def _split(url: str) -> Tuple[str, str]:
    """``rar:///inner/path?from=file:///C:/box.rar`` into its two halves."""
    parsed = urlparse(url)
    archive = (parse_qs(parsed.query).get("from") or [""])[0]
    if not archive:
        raise RpcError("No archive in %s" % url)
    return archive, _normalised(unquote(parsed.path or ""))


def _normalised(path: str) -> str:
    """One spelling for a path, whichever way it was written.

    NFC as well as the slashes: an archive made on a Mac holds its names
    decomposed — だ as た and a mark — and a name typed or pasted anywhere
    else is composed, so the two would otherwise never match.
    """
    path = unicodedata.normalize("NFC", path)
    return "/".join(part for part in path.replace("\\", "/").split("/")
                    if part and part != ".")


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


# -- one archive, listed ----------------------------------------------------


class _Listing:
    """An archive's headers, arranged as the folders they imply."""

    def __init__(self, archive_url: str, stamp, members, problem: str):
        self.archive_url = archive_url
        self.stamp = stamp
        self.checked = time.monotonic()
        self.problem = problem
        self.files: Dict[str, libarc.Member] = {}
        self.folders: Dict[str, Dict[str, Entry]] = {"": {}}
        self.refused: List[str] = []
        for member in members:
            self._add(member)

    def _folder(self, path: str, modified=None) -> None:
        if path in self.folders:
            return
        parent, _, name = path.rpartition("/")
        self._folder(parent)
        self.folders[path] = {}
        self.folders[parent][name] = Entry(name=name, kind=DIRECTORY, modified=modified)

    def _add(self, member: libarc.Member) -> None:
        parts = member.path.replace("\\", "/").split("/")
        # A name that climbs out of the archive is somebody else's path, and
        # unpacking it would write there.
        if ".." in parts or member.path.startswith("/"):
            self.refused.append(member.path)
            return
        path = _normalised(member.path)
        if not path:
            return
        if member.kind == libarc.IFDIR:
            self._folder(path, member.modified)
            entry = self.folders[path.rpartition("/")[0]].get(_basename(path))
            if entry is not None and member.modified is not None:
                entry.modified = member.modified
            return
        parent = path.rpartition("/")[0]
        self._folder(parent)
        self.files[path] = member
        self.folders[parent][_basename(path)] = Entry(
            name=_basename(path),
            kind=LINK if member.kind == libarc.IFLNK else FILE,
            size=member.size,
            modified=member.modified,
            target=member.target,
        )


class _Cursor:
    """A reader part way through an archive, and which listing it belongs to."""

    def __init__(self, archive_url: str, stamp, reader: libarc.Reader):
        self.archive_url = archive_url
        self.stamp = stamp
        self.reader = reader

    def close(self) -> None:
        self.reader.close()


def _plainly(failure: Exception) -> str:
    """libarchive's words, where they are words a person can use."""
    said = str(failure)
    if said == "no libarchive":
        return plugin.tr(
            "RAR archives are read with libarchive, and none was found on "
            "this machine. Looked in: {places}",
            {"places": ", ".join(libarc.searched())},
        )
    if "ncrypt" in said:
        return plugin.tr(
            "This archive is encrypted. Xverb reads RAR archives with "
            "libarchive, which cannot decrypt them."
        )
    if "multivolume" in said.lower() or "volume" in said.lower():
        return plugin.tr(
            "This archive goes on into another volume that is not here. "
            "Put all the parts in one folder on this machine."
        )
    return said


class RarFileSystem(FileSystem):
    """A RAR read as a directory. Nothing here writes."""

    scheme = "rar"
    writable = False
    icon = "archive"

    def __init__(self):
        self._listings: Dict[str, _Listing] = {}
        self._cursor: Optional[_Cursor] = None

    # -- opening ----------------------------------------------------------

    def _stamp(self, archive_url: str):
        """What the archive is right now: size and date of every volume."""
        path = local_path(archive_url)
        if path and os.path.isfile(path):
            parts = volumes(path)
            return tuple((p, os.stat(p).st_size, os.stat(p).st_mtime_ns) for p in parts)
        stat = plugin.stat(archive_url)
        if not stat:
            raise RpcError(plugin.tr("{url} is not there", {"url": archive_url}))
        return ((archive_url, int(stat.get("size") or 0), stat.get("modified")),)

    def _reader(self, archive_url: str, stamp) -> libarc.Reader:
        path = local_path(archive_url)
        try:
            if path and os.path.isfile(path):
                return libarc.Reader(paths=[part for part, _, _ in stamp])
            # Anywhere else the host is the only way to the bytes, and only
            # this one volume can be had.
            return libarc.Reader(stream=hostfile.opened(plugin, archive_url, stamp[0][1]))
        except libarc.ArchiveError as failure:
            raise RpcError(_plainly(failure))

    def _listing(self, archive_url: str) -> _Listing:
        cached = self._listings.get(archive_url)
        now = time.monotonic()
        if cached is not None and now - cached.checked < TRUST:
            return cached
        stamp = self._stamp(archive_url)
        if cached is not None and cached.stamp == stamp:
            cached.checked = now
            return cached

        members = []
        problem = ""
        with self._reader(archive_url, stamp) as reader:
            while True:
                try:
                    member = reader.next()
                except libarc.ArchiveError as failure:
                    # What was read before the damage is still worth showing:
                    # a truncated download lists up to where it stops.
                    problem = _plainly(failure)
                    if not members:
                        raise RpcError(problem)
                    break
                if member is None:
                    break
                members.append(member)
        listing = _Listing(archive_url, stamp, members, problem)
        if listing.refused:
            plugin.log(
                "%s: %d entry(ies) point outside the archive and were left out: %s"
                % (archive_url, len(listing.refused), ", ".join(listing.refused[:5])),
                level="warning",
            )
        if problem:
            plugin.log("%s: listed %d entries, then: %s"
                       % (archive_url, len(members), problem), level="warning")
        while len(self._listings) >= KEEP and archive_url not in self._listings:
            oldest = min(self._listings, key=lambda k: self._listings[k].checked)
            self._forget(oldest)
        self._listings[archive_url] = listing
        return listing

    def _forget(self, archive_url: str) -> None:
        self._listings.pop(archive_url, None)
        if self._cursor is not None and self._cursor.archive_url == archive_url:
            self._cursor.close()
            self._cursor = None

    def forget_all(self) -> None:
        for archive_url in list(self._listings):
            self._forget(archive_url)
        if self._cursor is not None:
            self._cursor.close()
            self._cursor = None

    def _absent(self, archive_url: str) -> bool:
        path = local_path(archive_url)
        if path:
            return not os.path.isfile(path)
        return not plugin.stat(archive_url)

    # -- reading ----------------------------------------------------------

    def list(self, url: str) -> List[Entry]:
        archive_url, inner = _split(url)
        if self._absent(archive_url):
            return []
        listing = self._listing(archive_url)
        folder = listing.folders.get(inner)
        if folder is None:
            raise RpcError(plugin.tr("{path} is not in the archive", {"path": inner}))
        return list(folder.values())

    def stat(self, url: str) -> Optional[Entry]:
        archive_url, inner = _split(url)
        if not inner:
            return Entry(name=_basename(unquote(urlparse(archive_url).path)), kind=DIRECTORY)
        if self._absent(archive_url):
            return None
        listing = self._listing(archive_url)
        parent = inner.rpartition("/")[0]
        return listing.folders.get(parent, {}).get(_basename(inner))

    def read(self, url: str, offset: int, length: int) -> bytes:
        archive_url, inner = _split(url)
        listing = self._listing(archive_url)
        member = listing.files.get(inner)
        if member is None:
            raise RpcError(plugin.tr("{path} is not in the archive", {"path": inner}))
        if member.encrypted:
            raise RpcError(plugin.tr(
                "{path} is encrypted. Xverb reads RAR archives with libarchive, "
                "which cannot decrypt them.", {"path": inner}))

        cursor = self._cursor
        if (
            cursor is None
            or cursor.archive_url != archive_url
            or cursor.stamp != listing.stamp
            or cursor.reader.index > member.index
            or (cursor.reader.index == member.index and cursor.reader.read_so_far > offset)
        ):
            if cursor is not None:
                cursor.close()
            self._cursor = None
            cursor = _Cursor(archive_url, listing.stamp, self._reader(archive_url, listing.stamp))
            self._cursor = cursor

        try:
            reader = cursor.reader
            while reader.index < member.index:
                if reader.next() is None:
                    raise RpcError(plugin.tr("{path} is not in the archive", {"path": inner}))
            while reader.read_so_far < offset:
                if not reader.read(min(offset - reader.read_so_far, 1 << 20)):
                    return b""
            return reader.read(length)
        except libarc.ArchiveError as failure:
            cursor.close()
            self._cursor = None
            raise RpcError(_plainly(failure))

    # -- nothing writes -----------------------------------------------------

    def _refuse(self, *_args, **_kwargs):
        raise RpcError(plugin.tr(
            "A RAR archive can only be read. Nobody but RARLAB's own program "
            "can write one."))

    begin_write = write = close_write = mkdir = delete = rename = _refuse


rars = RarFileSystem()
plugin.add_filesystem(rars)


# -- F3 -----------------------------------------------------------------------


def _date(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))


@plugin.viewer("rar.contents", "RAR contents", extensions=["rar", "cbr"],
               priority=30)
def contents(url):
    """What is in there, as a table, without walking into it."""
    try:
        listing = rars._listing(url)
    except RpcError as failure:
        return error(str(failure))

    rows = []
    for path in sorted(listing.files, key=str.lower):
        member = listing.files[path]
        note = []
        if member.encrypted:
            note.append(plugin.tr("encrypted"))
        if member.target:
            note.append("→ " + member.target)
        rows.append([path, member.size, _date(member.modified), ", ".join(note)])
    if not rows:
        return error(listing.problem or plugin.tr("The archive is empty."))
    total = sum(m.size for m in listing.files.values())
    plugin.log("%s: %d file(s), %d bytes, %s" % (url, len(rows), total, libarc.version()))
    return table([plugin.tr("Name"), plugin.tr("Size"), plugin.tr("Modified"),
                  plugin.tr("Note")], rows)


@plugin.on_shutdown
def _cleanup():
    rars.forget_all()


plugin.run()
