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

"""SFTP version 3, spoken over any pair of pipes.

**Why version 3 and nothing later.** It is the one OpenSSH speaks, and OpenSSH
is on the far end of nearly every SFTP connection there is. The later drafts
never became a standard; what OpenSSH added instead it added as extensions to
3 — `posix-rename`, `limits`, `copy-data` — and those are asked for by name.

**The pipes are not a socket.** This module does no cryptography and knows
nothing about SSH: the system's own `ssh` does all of that, and hands over a
channel that is nothing but the subsystem's stdin and stdout. Which is also
why it can be tested against `sftp-server` run directly, with no SSH at all.

**Requests are pipelined.** A packet carries an id, and the server answers each
one in its own time; reading a quarter of a megabyte one 32 KB request at a
time is eight round trips where it could be one. Everything that moves bulk
data sends all its requests before it reads the first answer.

The packet layout is draft-ietf-secsh-filexfer-02, which is what "version 3"
means.
"""

from __future__ import annotations

import struct
from typing import BinaryIO, Dict, List, Optional, Tuple

# -- packet types ----------------------------------------------------------

INIT, VERSION = 1, 2
OPEN, CLOSE, READ, WRITE = 3, 4, 5, 6
LSTAT, FSTAT, SETSTAT, FSETSTAT = 7, 8, 9, 10
OPENDIR, READDIR, REMOVE, MKDIR, RMDIR = 11, 12, 13, 14, 15
REALPATH, STAT, RENAME, READLINK, SYMLINK = 16, 17, 18, 19, 20
STATUS, HANDLE, DATA, NAME, ATTRS = 101, 102, 103, 104, 105
EXTENDED, EXTENDED_REPLY = 200, 201

# -- status codes ----------------------------------------------------------

OK, EOF, NO_SUCH_FILE, PERMISSION_DENIED, FAILURE = 0, 1, 2, 3, 4
BAD_MESSAGE, NO_CONNECTION, CONNECTION_LOST, OP_UNSUPPORTED = 5, 6, 7, 8

#: What the codes mean, for the one case a server sends no message with one.
STATUS_TEXT = {
    EOF: "end of file",
    NO_SUCH_FILE: "no such file",
    PERMISSION_DENIED: "permission denied",
    FAILURE: "the server refused",
    BAD_MESSAGE: "the server did not understand the request",
    NO_CONNECTION: "no connection",
    CONNECTION_LOST: "the connection was lost",
    OP_UNSUPPORTED: "the server does not do that",
}

# -- open flags and attribute flags ----------------------------------------

O_READ, O_WRITE, O_APPEND, O_CREAT, O_TRUNC, O_EXCL = 1, 2, 4, 8, 0x10, 0x20

A_SIZE, A_UIDGID, A_PERMISSIONS, A_TIMES = 1, 2, 4, 8
A_EXTENDED = 0x80000000

S_IFMT, S_IFDIR, S_IFLNK, S_IFREG = 0o170000, 0o040000, 0o120000, 0o100000

#: The size of a read or a write when the server does not say what it takes.
#: 32 KB is what every server since the draft was written accepts; OpenSSH
#: says through `limits@openssh.com` that it takes 256 KB, and is asked.
DEFAULT_CHUNK = 32 * 1024

#: The most requests in flight at once. OpenSSH queues far more, but past this
#: the pipe is full anyway and the answers only wait longer in it.
WINDOW = 64


class SftpError(Exception):
    """A request the server answered with a status other than OK."""

    def __init__(self, code: int, message: str = ""):
        self.code = code
        super().__init__(message or STATUS_TEXT.get(code, "status %d" % code))


class ChannelClosed(Exception):
    """The far end went away — ssh exited, or the server hung up."""


class Attributes:
    """What the server says about one file, read out of an ATTRS block."""

    __slots__ = ("size", "permissions", "mtime", "atime")

    def __init__(self):
        self.size: Optional[int] = None
        self.permissions: Optional[int] = None
        self.mtime: Optional[int] = None
        self.atime: Optional[int] = None

    @property
    def is_directory(self) -> bool:
        return (self.permissions or 0) & S_IFMT == S_IFDIR

    @property
    def is_link(self) -> bool:
        return (self.permissions or 0) & S_IFMT == S_IFLNK


# -- packing ---------------------------------------------------------------


def _string(value) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return struct.pack(">I", len(value)) + value


def _attrs(mtime: Optional[int] = None, permissions: Optional[int] = None) -> bytes:
    flags = 0
    body = b""
    if permissions is not None:
        flags |= A_PERMISSIONS
        body += struct.pack(">I", permissions)
    if mtime is not None:
        flags |= A_TIMES
        body += struct.pack(">II", mtime, mtime)
    return struct.pack(">I", flags) + body


class _Reader:
    """A cursor over one packet's body."""

    def __init__(self, data: bytes):
        self.data = data
        self.at = 0

    def u32(self) -> int:
        (value,) = struct.unpack_from(">I", self.data, self.at)
        self.at += 4
        return value

    def u64(self) -> int:
        (value,) = struct.unpack_from(">Q", self.data, self.at)
        self.at += 8
        return value

    def bytes(self) -> bytes:
        length = self.u32()
        value = self.data[self.at:self.at + length]
        self.at += length
        return value

    def text(self) -> str:
        # Names are bytes to the server; UTF-8 is what every system writes now,
        # and a name that is not stays readable rather than failing a listing.
        return self.bytes().decode("utf-8", "surrogateescape")

    def attrs(self) -> Attributes:
        found = Attributes()
        flags = self.u32()
        if flags & A_SIZE:
            found.size = self.u64()
        if flags & A_UIDGID:
            self.at += 8
        if flags & A_PERMISSIONS:
            found.permissions = self.u32()
        if flags & A_TIMES:
            found.atime = self.u32()
            found.mtime = self.u32()
        if flags & A_EXTENDED:
            for _ in range(self.u32()):
                self.bytes()
                self.bytes()
        return found

    @property
    def left(self) -> int:
        return len(self.data) - self.at


# -- the client ------------------------------------------------------------


class SftpClient:
    """One SFTP session over [incoming] and [outgoing].

    Not thread-safe by itself: the plugin serves the host one request at a time
    and the adapter holds a lock besides, so one conversation is in flight.
    """

    def __init__(self, incoming: BinaryIO, outgoing: BinaryIO):
        self._in = incoming
        self._out = outgoing
        self._next_id = 1
        self._early: Dict[int, Tuple[int, _Reader]] = {}
        self.extensions: Dict[str, bytes] = {}
        self.version = 0
        self.max_read = DEFAULT_CHUNK
        self.max_write = DEFAULT_CHUNK

    # -- framing -----------------------------------------------------------

    def _send(self, kind: int, body: bytes) -> None:
        packet = struct.pack(">IB", len(body) + 1, kind) + body
        try:
            self._out.write(packet)
            self._out.flush()
        except (OSError, ValueError) as failure:
            raise ChannelClosed(str(failure))

    def _exactly(self, count: int) -> bytes:
        chunks = []
        while count:
            chunk = self._in.read(count)
            if not chunk:
                raise ChannelClosed("the connection closed")
            chunks.append(chunk)
            count -= len(chunk)
        return b"".join(chunks)

    def _receive(self) -> Tuple[int, _Reader]:
        (length,) = struct.unpack(">I", self._exactly(4))
        if length < 1 or length > 64 << 20:
            raise ChannelClosed("the server sent a packet of %d bytes" % length)
        body = self._exactly(length)
        return body[0], _Reader(body[1:])

    def _request(self, kind: int, body: bytes) -> int:
        request_id = self._next_id
        self._next_id = (self._next_id + 1) & 0xFFFFFFFF or 1
        self._send(kind, struct.pack(">I", request_id) + body)
        return request_id

    def _answer(self, request_id: int) -> Tuple[int, _Reader]:
        """The reply to [request_id], keeping any that arrive before it."""
        early = self._early.pop(request_id, None)
        if early is not None:
            return early
        while True:
            kind, reader = self._receive()
            got = reader.u32()
            if got == request_id:
                return kind, reader
            self._early[got] = (kind, reader)

    @staticmethod
    def _status(kind: int, reader: _Reader, allow_eof: bool = False) -> int:
        if kind != STATUS:
            raise SftpError(BAD_MESSAGE, "the server answered with packet %d" % kind)
        code = reader.u32()
        message = reader.text() if reader.left >= 4 else ""
        if code == OK or (allow_eof and code == EOF):
            return code
        raise SftpError(code, message)

    def _expect(self, request_id: int, wanted: int) -> _Reader:
        kind, reader = self._answer(request_id)
        if kind == wanted:
            return reader
        self._status(kind, reader)
        raise SftpError(BAD_MESSAGE, "the server answered with packet %d" % kind)

    def _call_status(self, kind: int, body: bytes) -> None:
        answer_kind, reader = self._answer(self._request(kind, body))
        self._status(answer_kind, reader)

    # -- the session -------------------------------------------------------

    def start(self) -> None:
        """The version exchange, and asking what the server allows."""
        self._send(INIT, struct.pack(">I", 3))
        kind, reader = self._receive()
        if kind != VERSION:
            raise ChannelClosed(
                "the far end did not start SFTP (packet %d) — is the SFTP "
                "subsystem enabled on the server?" % kind
            )
        self.version = reader.u32()
        while reader.left >= 8:
            name = reader.bytes().decode("ascii", "replace")
            self.extensions[name] = reader.bytes()

        if "limits@openssh.com" in self.extensions:
            try:
                reader = self._extended("limits@openssh.com", b"")
                _packet, read_most, write_most = reader.u64(), reader.u64(), reader.u64()
                # Packet overhead is taken off the write, and neither is let
                # go past a megabyte however generous the server is.
                if read_most:
                    self.max_read = max(DEFAULT_CHUNK, min(read_most, 1 << 20))
                if write_most:
                    self.max_write = max(DEFAULT_CHUNK, min(write_most - 1024, 1 << 20))
            except SftpError:
                pass

    def _extended(self, name: str, body: bytes) -> _Reader:
        return self._expect(self._request(EXTENDED, _string(name) + body), EXTENDED_REPLY)

    # -- names -------------------------------------------------------------

    def realpath(self, path: str) -> str:
        reader = self._expect(self._request(REALPATH, _string(path)), NAME)
        if reader.u32() < 1:
            raise SftpError(FAILURE, "the server gave no name for %s" % path)
        return reader.text()

    def stat(self, path: str) -> Attributes:
        return self._expect(self._request(STAT, _string(path)), ATTRS).attrs()

    def lstat(self, path: str) -> Attributes:
        return self._expect(self._request(LSTAT, _string(path)), ATTRS).attrs()

    def readlink(self, path: str) -> str:
        reader = self._expect(self._request(READLINK, _string(path)), NAME)
        if reader.u32() < 1:
            raise SftpError(FAILURE, "the server gave no target for %s" % path)
        return reader.text()

    def stat_many(self, paths: List[str]) -> List[Optional[Attributes]]:
        """STAT for each of [paths], all asked before any is read; None for a
        path the server has nothing at — a dangling link, typically."""
        ids = [self._request(STAT, _string(p)) for p in paths]
        found: List[Optional[Attributes]] = []
        for request_id in ids:
            kind, reader = self._answer(request_id)
            if kind == ATTRS:
                found.append(reader.attrs())
            else:
                found.append(None)
        return found

    def readlink_many(self, paths: List[str]) -> List[Optional[str]]:
        ids = [self._request(READLINK, _string(p)) for p in paths]
        found: List[Optional[str]] = []
        for request_id in ids:
            kind, reader = self._answer(request_id)
            found.append(reader.text() if kind == NAME and reader.u32() >= 1 else None)
        return found

    def listdir(self, path: str) -> List[Tuple[str, Attributes]]:
        handle = self._expect(self._request(OPENDIR, _string(path)), HANDLE).bytes()
        rows: List[Tuple[str, Attributes]] = []
        try:
            while True:
                kind, reader = self._answer(self._request(READDIR, _string(handle)))
                if kind != NAME:
                    self._status(kind, reader, allow_eof=True)
                    break
                for _ in range(reader.u32()):
                    name = reader.text()
                    reader.bytes()  # the `ls -l` line, which is for people
                    rows.append((name, reader.attrs()))
        finally:
            self.close(handle)
        return [(n, a) for n, a in rows if n not in (".", "..")]

    # -- files -------------------------------------------------------------

    def open(self, path: str, flags: int) -> bytes:
        body = _string(path) + struct.pack(">I", flags) + _attrs()
        return self._expect(self._request(OPEN, body), HANDLE).bytes()

    def close(self, handle: bytes) -> None:
        self._call_status(CLOSE, _string(handle))

    def read(self, handle: bytes, offset: int, length: int) -> bytes:
        """Up to [length] bytes from [offset]: fewer only at the end of the file.

        Every request goes out before the first answer is read. A server may
        answer with less than was asked even in the middle of a file; the gap
        is asked for again rather than taken for the end, and only EOF — or an
        answer with nothing in it — is the end.
        """
        parts: List[bytes] = []
        at = offset
        end = offset + length
        while at < end:
            pending = []
            ask = at
            while ask < end and len(pending) < WINDOW:
                size = min(self.max_read, end - ask)
                body = _string(handle) + struct.pack(">QI", ask, size)
                pending.append((self._request(READ, body), ask, size))
                ask += size

            stopped: Optional[int] = None
            ended = False
            for request_id, start, size in pending:
                kind, reader = self._answer(request_id)
                if stopped is not None:
                    continue  # drained and dropped: the gap is asked again
                if kind == DATA:
                    data = reader.bytes()
                    parts.append(data)
                    if not data:
                        stopped, ended = start, True
                    elif len(data) < size:
                        stopped = start + len(data)
                    continue
                self._status(kind, reader, allow_eof=True)
                stopped, ended = start, True

            if stopped is None:
                at = ask
            elif ended:
                break
            else:
                at = stopped
        return b"".join(parts)

    def write(self, handle: bytes, offset: int, data: bytes) -> None:
        view = memoryview(data)
        at = 0
        while at < len(data):
            pending = []
            while at < len(data) and len(pending) < WINDOW:
                chunk = view[at:at + self.max_write]
                body = _string(handle) + struct.pack(">QI", offset + at, len(chunk)) + bytes(chunk)
                pending.append(self._request(WRITE, body))
                at += len(chunk)
            failure = None
            for request_id in pending:
                kind, reader = self._answer(request_id)
                try:
                    self._status(kind, reader)
                except SftpError as refused:
                    failure = failure or refused
            if failure is not None:
                raise failure

    def set_mtime(self, path: str, mtime: int) -> None:
        self._call_status(SETSTAT, _string(path) + _attrs(mtime=mtime))

    # -- changes -----------------------------------------------------------

    def mkdir(self, path: str) -> None:
        self._call_status(MKDIR, _string(path) + _attrs())

    def rmdir(self, path: str) -> None:
        self._call_status(RMDIR, _string(path))

    def remove(self, path: str) -> None:
        self._call_status(REMOVE, _string(path))

    def rename(self, source: str, target: str) -> None:
        """A rename that replaces what is at [target], where the server can.

        Plain version 3 RENAME refuses when the target exists; OpenSSH's
        `posix-rename` is rename(2), which replaces it. The host asks before it
        overwrites anything, so by the time this is called replacing is meant.
        """
        if "posix-rename@openssh.com" in self.extensions:
            body = (_string("posix-rename@openssh.com")
                    + _string(source) + _string(target))
            self._status(*self._answer(self._request(EXTENDED, body)))
            return
        self._call_status(RENAME, _string(source) + _string(target))

    @property
    def can_copy(self) -> bool:
        return "copy-data" in self.extensions

    def copy_data(self, source: bytes, target: bytes, length: int = 0) -> None:
        """The server copies from one open handle to another, and nothing
        crosses the network. `length` 0 means to the end of the source."""
        body = (_string("copy-data") + _string(source) + struct.pack(">QQ", 0, length)
                + _string(target) + struct.pack(">Q", 0))
        self._status(*self._answer(self._request(EXTENDED, body)))
