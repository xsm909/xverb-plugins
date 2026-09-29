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

"""MTP, which is PTP with more operations, spoken over two bulk pipes.

**A transaction is three phases on one pipe pair.** The command goes out as a
container; then, for the operations that carry data, a data container goes
one way or the other; then the response comes back. Nothing else may use the
pipes in between, so every call here holds the session's lock from the
command to the response.

**The end of the data is where USB says it is.** A data container states its
length, but past 4 GB it says 0xFFFFFFFF and the reader has to go on until a
short packet. A transfer that is an exact multiple of the packet size ends
with an empty packet, and that one has to be read, or it is taken for the
start of the response.

Only what a file manager needs is here: storages, the objects in a folder,
reading all or part of one, sending one, making a folder, deleting, renaming
and moving.
"""

from __future__ import annotations

import datetime
import struct
import threading
from typing import BinaryIO, Callable, Dict, List, Optional, Tuple

import usb

# Container types.
COMMAND, DATA, RESPONSE, EVENT = 1, 2, 3, 4

# Operations.
GET_DEVICE_INFO = 0x1001
OPEN_SESSION = 0x1002
CLOSE_SESSION = 0x1003
GET_STORAGE_IDS = 0x1004
GET_STORAGE_INFO = 0x1005
GET_OBJECT_HANDLES = 0x1007
GET_OBJECT_INFO = 0x1008
GET_OBJECT = 0x1009
DELETE_OBJECT = 0x100B
SEND_OBJECT_INFO = 0x100C
SEND_OBJECT = 0x100D
MOVE_OBJECT = 0x1019
COPY_OBJECT = 0x101A
GET_PARTIAL_OBJECT = 0x101B
GET_OBJECT_PROP_VALUE = 0x9803
GET_OBJECT_PROP_LIST = 0x9805
SET_OBJECT_PROP_VALUE = 0x9804
GET_PARTIAL_OBJECT_64 = 0x95C1

# Responses.
OK = 0x2001
SESSION_ALREADY_OPEN = 0x201E
INVALID_OBJECT_HANDLE = 0x2009
STORE_FULL = 0x200C
ACCESS_DENIED = 0x200F
DEVICE_BUSY = 0x2019

# Object formats and properties.
FORMAT_UNDEFINED = 0x3000
FORMAT_ASSOCIATION = 0x3001
PROP_FORMAT = 0xDC02
PROP_SIZE = 0xDC04
PROP_FILE_NAME = 0xDC07
PROP_MODIFIED = 0xDC09

ROOT_PARENT = 0xFFFFFFFF

RESPONSE_NAMES = {
    0x2002: "general error", 0x2003: "session not open", 0x2005: "not supported",
    0x2008: "invalid storage", INVALID_OBJECT_HANDLE: "no such object",
    STORE_FULL: "the phone's storage is full", 0x200D: "the object is write-protected",
    0x200E: "the storage is read-only", ACCESS_DENIED: "access denied",
    DEVICE_BUSY: "the phone is busy", 0x201A: "invalid parent", 0x201D: "invalid parameter",
    0xA809: "the object is too large",
}


class MtpError(Exception):
    """[what] was being done and the phone said [code]. Both are English
    sentences the plugin translates: [what] says the step, [reason] the answer."""

    def __init__(self, what: str, code: int):
        self.what = what
        self.code = code
        self.reason = RESPONSE_NAMES.get(code, "")
        super().__init__("%s: %s" % (what, self.reason or "response %#06x" % code))


class Storage:
    def __init__(self, storage_id: int, description: str, volume: str,
                 capacity: int, free: int, access: int):
        self.id = storage_id
        self.description = description
        self.volume = volume
        self.capacity = capacity
        self.free = free
        self.read_only = access != 0


class Object:
    def __init__(self, handle: int, storage: int, parent: int, name: str,
                 is_folder: bool, size: int, modified: Optional[float]):
        self.handle = handle
        self.storage = storage
        self.parent = parent
        self.name = name
        self.is_folder = is_folder
        self.size = size
        self.modified = modified


# -- datasets ----------------------------------------------------------------

def _string(data: bytes, offset: int) -> Tuple[str, int]:
    count = data[offset]
    offset += 1
    if count == 0:
        return "", offset
    text = data[offset:offset + 2 * count].decode("utf-16-le", "replace")
    return text.rstrip("\0"), offset + 2 * count


def _pack_string(text: str) -> bytes:
    if not text:
        return b"\0"
    encoded = (text + "\0").encode("utf-16-le")
    return bytes([len(encoded) // 2]) + encoded


def _array16(data: bytes, offset: int) -> Tuple[List[int], int]:
    count = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    return list(struct.unpack_from("<%dH" % count, data, offset)), offset + 2 * count


def _date(text: str) -> Optional[float]:
    """PTP's YYYYMMDDThhmmss[.s][Z|±hhmm]. Android writes it in local time
    without a zone, so it is read as local time."""
    if len(text) < 15:
        return None
    try:
        moment = datetime.datetime.strptime(text[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if text.endswith("Z"):
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    try:
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _pack_date(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    return datetime.datetime.fromtimestamp(seconds).strftime("%Y%m%dT%H%M%S")


def _object_info(handle: int, data: bytes) -> Object:
    (storage, fmt, _protection, size, _thumb_format, _thumb_size, _thumb_w,
     _thumb_h, _w, _h, _depth, parent, association, _desc, _seq) = struct.unpack_from(
        "<IHHIHIIIIIIIHII", data, 0)
    offset = 52
    name, offset = _string(data, offset)
    _captured, offset = _string(data, offset)
    modified, offset = _string(data, offset)
    folder = fmt == FORMAT_ASSOCIATION
    return Object(handle, storage, 0 if parent == ROOT_PARENT else parent, name,
                  folder, 0 if folder else size, _date(modified))


def _pack_object_info(storage: int, parent: int, name: str, size: int,
                      folder: bool, modified: Optional[float]) -> bytes:
    fmt = FORMAT_ASSOCIATION if folder else FORMAT_UNDEFINED
    head = struct.pack(
        "<IHHIHIIIIIIIHII",
        storage, fmt, 0, 0 if folder else min(size, 0xFFFFFFFF),
        0, 0, 0, 0, 0, 0, 0, parent, 1 if folder else 0, 0, 0)
    date = _pack_date(modified)
    return head + _pack_string(name) + _pack_string(date) + _pack_string(date) + b"\0"


# -- the session -------------------------------------------------------------

class Device:
    """One phone with a session open on it."""

    #: Bytes asked of the pipe per read. A multiple of every packet size.
    READ_CHUNK = 1024 * 1024

    def __init__(self, candidate: usb.Candidate):
        self.candidate = candidate
        self.handle = usb.Handle(candidate)
        self.lock = threading.RLock()
        self.transaction = 0
        self.serial = ""
        self.manufacturer = ""
        self.model = ""
        self.operations: set = set()
        self.broken = False
        try:
            self.serial = self.handle.string(candidate.serial_index)
            self.handle.claim()
            self._drain()
            self._open_session()
        except Exception:
            self.handle.close()
            raise

    # -- plumbing --------------------------------------------------------

    def _command(self, code: int, params: Tuple[int, ...]) -> None:
        self.transaction += 1
        body = struct.pack("<IHHI", 12 + 4 * len(params), COMMAND, code,
                           self.transaction)
        body += b"".join(struct.pack("<I", p & 0xFFFFFFFF) for p in params)
        self.handle.write(body)

    def _response(self, first: Optional[bytes] = None) -> Tuple[int, Tuple[int, ...]]:
        packet = first if first is not None else self.handle.read(512, 30000)
        while True:
            length, kind, code, _tid = struct.unpack_from("<IHHI", packet)
            if kind == RESPONSE:
                count = (min(length, len(packet)) - 12) // 4
                return code, struct.unpack_from("<%dI" % count, packet, 12)
            # An event on the bulk pipe, or data nobody asked for: skip it.
            packet = self.handle.read(512, 30000)

    def _receive(self, sink: Callable[[bytes], None]) -> Tuple[int, Tuple[int, ...]]:
        """The data phase in, handed to [sink] piece by piece, then the response."""
        chunk = self.READ_CHUNK
        first = self.handle.read(chunk, 30000)
        length, kind, _code, _tid = struct.unpack_from("<IHHI", first)
        if kind == RESPONSE:
            return self._response(first)
        unknown = length == 0xFFFFFFFF
        payload = first[12:] if unknown else first[12:length]
        if payload:
            sink(payload)
        got = last = len(first)
        # A read stops at a short packet, the empty one included, so a read
        # that comes back short is the end. Past 4 GB that is the only way to
        # know; below it the length says so first.
        while last == chunk and (unknown or got < length):
            piece = self.handle.read(chunk, 30000)
            last = len(piece)
            got += last
            if piece:
                sink(piece if unknown else piece[:max(0, length - (got - last))])
        if not unknown and last == chunk and length % self.candidate.packet == 0:
            # It ended exactly where the read did, so the empty packet that
            # closes it is still waiting, and would be taken for the response.
            try:
                tail = self.handle.read(chunk, 2000)
            except usb.UsbError as failure:
                if failure.code != usb.LIBUSB_ERROR_TIMEOUT:
                    raise
                tail = b""
            if tail:
                return self._response(tail)
        return self._response()

    def _send(self, code: int, total: int, source: Callable[[int], bytes]) -> None:
        """The data phase out: [total] bytes, pulled from [source] as needed.

        Every write but the last is a whole number of packets. A short one
        would end the transfer where it stood, and the phone would take the
        rest for the next command.
        """
        chunk = self.READ_CHUNK

        def take(n: int) -> bytes:
            gathered = b""
            while len(gathered) < n:
                piece = source(n - len(gathered))
                if not piece:
                    raise MtpError("The file ended before its size said", 0x2002)
                gathered += piece
            return gathered

        header = struct.pack("<IHHI", min(12 + total, 0xFFFFFFFF), DATA, code,
                             self.transaction)
        self.handle.write(header + take(min(chunk - 12, total)), 60000)
        sent = min(chunk - 12, total)
        while sent < total:
            n = min(chunk, total - sent)
            self.handle.write(take(n), 60000)
            sent += n
        if (12 + total) % self.candidate.packet == 0:
            self.handle.write(b"", 5000)

    def call(self, code: int, *params: int, what: str = "") -> Tuple[int, ...]:
        with self.lock:
            self._guard()
            try:
                self._command(code, params)
                status, answer = self._response()
            except usb.UsbError:
                self.broken = True
                raise
            if status != OK:
                raise MtpError(what or "operation %#06x" % code, status)
            return answer

    def call_in(self, code: int, *params: int, what: str = "",
                sink: Optional[Callable[[bytes], None]] = None) -> Tuple[bytes, Tuple[int, ...]]:
        with self.lock:
            self._guard()
            gathered = bytearray()
            try:
                self._command(code, params)
                status, answer = self._receive(sink or gathered.extend)
            except usb.UsbError:
                self.broken = True
                raise
            if status != OK:
                raise MtpError(what or "operation %#06x" % code, status)
            return bytes(gathered), answer

    def call_out(self, code: int, *params: int, data: bytes = b"", what: str = "",
                 total: Optional[int] = None,
                 source: Optional[Callable[[int], bytes]] = None) -> Tuple[int, ...]:
        with self.lock:
            self._guard()
            if source is None:
                view = memoryview(data)
                position = [0]

                def source(n: int) -> bytes:
                    piece = bytes(view[position[0]:position[0] + n])
                    position[0] += len(piece)
                    return piece
                total = len(data)
            try:
                self._command(code, params)
                self._send(code, total or 0, source)
                status, answer = self._response()
            except usb.UsbError:
                self.broken = True
                raise
            if status != OK:
                raise MtpError(what or "operation %#06x" % code, status)
            return answer

    def _guard(self) -> None:
        if self.broken:
            raise MtpError("The connection to the phone was lost", 0x2002)

    def _drain(self) -> None:
        """Throw away whatever an earlier owner left in the pipe.

        A program that gave up on an answer leaves it waiting, and the phone
        will not take a new command until it is read. What arrives first would
        otherwise be taken for the answer to ours.
        """
        for _ in range(64):
            try:
                self.handle.read(self.READ_CHUNK, 300)
            except usb.UsbError:
                return

    def _open_session(self) -> None:
        data, _ = self.call_in(GET_DEVICE_INFO, what="Reading the phone's description")
        offset = 8
        _extensions, offset = _string(data, offset)
        offset += 2  # functional mode
        operations, offset = _array16(data, offset)
        self.operations = set(operations)
        _events, offset = _array16(data, offset)
        _props, offset = _array16(data, offset)
        _capture, offset = _array16(data, offset)
        _playback, offset = _array16(data, offset)
        self.manufacturer, offset = _string(data, offset)
        self.model, offset = _string(data, offset)
        _version, offset = _string(data, offset)
        serial, offset = _string(data, offset)
        # The USB serial is what the phone is called in a URL: it is short,
        # and it is the one the system shows. The MTP one is a hash on Samsung.
        if serial and not self.serial:
            self.serial = serial
        with self.lock:
            self._command(OPEN_SESSION, (1,))
            status, _ = self._response()
        if status == SESSION_ALREADY_OPEN:
            # Left open by whoever had the phone before: close it and start ours.
            with self.lock:
                self._command(CLOSE_SESSION, ())
                self._response()
                self._command(OPEN_SESSION, (1,))
                status, _ = self._response()
        if status != OK:
            raise MtpError("Opening a session with the phone", status)

    def close(self) -> None:
        with self.lock:
            if not self.broken:
                try:
                    self._command(CLOSE_SESSION, ())
                    self._response()
                except (usb.UsbError, struct.error):
                    pass
            self.handle.close()
            self.broken = True

    # -- what the plugin asks ------------------------------------------------

    def storages(self) -> List[Storage]:
        data, _ = self.call_in(GET_STORAGE_IDS, what="Listing the phone's storages")
        count = struct.unpack_from("<I", data)[0]
        found = []
        for storage_id in struct.unpack_from("<%dI" % count, data, 4):
            info, _ = self.call_in(GET_STORAGE_INFO, storage_id, what="Reading a storage")
            _type, _fs, access, capacity, free, _objects = struct.unpack_from("<HHHQQI", info)
            description, offset = _string(info, 26)
            volume, offset = _string(info, offset)
            found.append(Storage(storage_id, description, volume, capacity, free, access))
        return found

    #: A folder with more new entries than this is described by property
    #: lists rather than one question per entry.
    BY_PROPERTIES = 24

    def handles(self, storage: int, parent: int) -> List[int]:
        """What a folder holds, as handles only. [parent] 0 is the top level.

        Cheap on every phone measured: a folder of a thousand photographs
        answers in milliseconds. What costs is describing them.
        """
        data, _ = self.call_in(GET_OBJECT_HANDLES, storage, 0,
                               parent or ROOT_PARENT, what="Listing a folder")
        count = struct.unpack_from("<I", data)[0]
        return list(struct.unpack_from("<%dI" % count, data, 4))

    def describe(self, storage: int, parent: int, handles: List[int]) -> List[Object]:
        """[handles], all in [parent], described.

        **Four property lists instead of an ObjectInfo each**, past a few. A
        Galaxy A05s took 14 s over a thousand ObjectInfos and 8 s over the
        lists — names and formats in 0.15 s, sizes and dates about 4 s each,
        which is the phone looking at its files. Asking for every property at
        once is refused by that phone (0xA801), and late enough to jam the
        pipe, so each is asked for by name.
        """
        if (len(handles) > self.BY_PROPERTIES and parent
                and GET_OBJECT_PROP_LIST in self.operations):
            try:
                return self._by_properties(storage, parent, handles)
            except MtpError:
                pass
        return [self.info(h) for h in handles]

    def _by_properties(self, storage: int, parent: int,
                       handles: List[int]) -> List[Object]:
        names = self._property(parent, PROP_FILE_NAME)
        formats = self._property(parent, PROP_FORMAT)
        sizes = self._property(parent, PROP_SIZE)
        dates = self._property(parent, PROP_MODIFIED)
        found = []
        for handle in handles:
            name = names.get(handle)
            if not isinstance(name, str):
                found.append(self.info(handle))
                continue
            folder = formats.get(handle) == FORMAT_ASSOCIATION
            size = sizes.get(handle)
            date = dates.get(handle)
            found.append(Object(handle, storage, parent, name, folder,
                                0 if folder else int(size or 0),
                                _date(date) if isinstance(date, str) else None))
        return found

    def _property(self, parent: int, prop: int) -> Dict[int, object]:
        """One property of everything in [parent], by handle."""
        data, _ = self.call_in(GET_OBJECT_PROP_LIST, parent, 0, prop, 0, 1,
                               what="Listing a folder")
        count = struct.unpack_from("<I", data)[0]
        offset = 4
        values: Dict[int, object] = {}
        for _ in range(count):
            handle, _code, kind = struct.unpack_from("<IHH", data, offset)
            offset += 8
            value, offset = _value(data, offset, kind)
            values[handle] = value
        return values

    def info(self, handle: int) -> Object:
        data, _ = self.call_in(GET_OBJECT_INFO, handle, what="Reading an entry")
        found = _object_info(handle, data)
        if found.size == 0xFFFFFFFF and not found.is_folder:
            # Past 4 GB the dataset gives up; the property knows.
            found.size = self.size64(handle) or found.size
        return found

    def size64(self, handle: int) -> Optional[int]:
        try:
            data, _ = self.call_in(GET_OBJECT_PROP_VALUE, handle, PROP_SIZE, what="Reading a size")
        except MtpError:
            return None
        return struct.unpack_from("<Q", data)[0] if len(data) >= 8 else None

    def read_part(self, handle: int, offset: int, length: int) -> Optional[bytes]:
        """[length] bytes from [offset], or None when the phone cannot do part
        of an object and the whole of it has to come across."""
        if GET_PARTIAL_OBJECT_64 in self.operations:
            data, _ = self.call_in(GET_PARTIAL_OBJECT_64, handle, offset & 0xFFFFFFFF,
                                   offset >> 32, length, what="Reading a file")
            return data
        if GET_PARTIAL_OBJECT in self.operations and offset + length <= 0xFFFFFFFF:
            data, _ = self.call_in(GET_PARTIAL_OBJECT, handle, offset, length,
                                   what="Reading a file")
            return data
        return None

    def read_whole(self, handle: int, sink: Callable[[bytes], None]) -> None:
        self.call_in(GET_OBJECT, handle, what="Reading a file", sink=sink)

    def send(self, storage: int, parent: int, name: str, size: int,
             modified: Optional[float], stream: BinaryIO) -> int:
        """A new file from [stream], [size] bytes. Returns its handle."""
        info = _pack_object_info(storage, parent or ROOT_PARENT, name, size, False, modified)
        answer = self.call_out(SEND_OBJECT_INFO, storage, parent or ROOT_PARENT,
                               data=info, what="Creating a file")
        handle = answer[2] if len(answer) > 2 else 0
        self.call_out(SEND_OBJECT, data=b"", total=size, source=stream.read,
                      what="Sending a file")
        return handle

    def copy(self, handle: int, storage: int, parent: int) -> int:
        """The phone copies [handle] into [parent]. Returns the copy's handle."""
        answer = self.call(COPY_OBJECT, handle, storage, parent or ROOT_PARENT,
                           what="Copying")
        return answer[0] if answer else 0

    def make_folder(self, storage: int, parent: int, name: str) -> int:
        info = _pack_object_info(storage, parent or ROOT_PARENT, name, 0, True, None)
        answer = self.call_out(SEND_OBJECT_INFO, storage, parent or ROOT_PARENT,
                               data=info, what="Creating a folder")
        return answer[2] if len(answer) > 2 else 0

    def delete(self, handle: int) -> None:
        self.call(DELETE_OBJECT, handle, 0, what="Deleting")

    def rename(self, handle: int, name: str) -> None:
        self.call_out(SET_OBJECT_PROP_VALUE, handle, PROP_FILE_NAME,
                      data=_pack_string(name), what="Renaming")

    def move(self, handle: int, storage: int, parent: int) -> None:
        self.call(MOVE_OBJECT, handle, storage, parent or ROOT_PARENT, what="Moving")


_NUMBERS = {0x0001: "b", 0x0002: "B", 0x0003: "h", 0x0004: "H", 0x0005: "i",
            0x0006: "I", 0x0007: "q", 0x0008: "Q"}


def _value(data: bytes, offset: int, kind: int):
    """One value of an ObjectPropList, by its PTP data type."""
    if kind in _NUMBERS:
        code = "<" + _NUMBERS[kind]
        return struct.unpack_from(code, data, offset)[0], offset + struct.calcsize(code)
    if kind in (0x0009, 0x000A):  # 128-bit
        return None, offset + 16
    if kind == 0xFFFF:
        return _string(data, offset)
    raise MtpError("Listing a folder", 0x2002)


def devices() -> List[usb.Candidate]:
    """Every MTP interface on the machine, phones that have not said "MTP" of
    themselves left out."""
    found = []
    for candidate in usb.candidates():
        if candidate.iface_string_index:
            handle = usb.Handle(candidate)
            try:
                name = handle.string(candidate.iface_string_index)
            finally:
                handle.close()
            if "MTP" not in name.upper():
                continue
        found.append(candidate)
    return found
