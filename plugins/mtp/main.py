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

"""An Android phone on a cable, as an xverb file system.

Three layers, one file each. `usb.py` is libusb through ctypes; `ptp.py`
speaks MTP over the two pipes it finds; this file is the adapter between that
and what the host asks for.

A URL is `mtp://SERIAL/STORAGE/path`: the phone's USB serial number, then the
storage by the name the phone gives it ("Internal storage", an SD card), then
the path. `mtp://SERIAL/` lists the storages.

**MTP has no paths.** Everything is a numbered object with a parent, so a path
is found by walking from the top, folder by folder. What each folder held is
remembered, and a name not found is asked about again before it is called
missing — the phone may have taken a photograph since.

**A description is remembered by its handle.** Handles are the phone's for
the length of a session, and describing a thousand photographs costs seconds
while listing their handles costs nothing. So a folder is listed afresh every
time and only what is new in it is described.

**A file sent to the phone is gathered first.** MTP takes a file as one
transfer of a length stated up front, and the pipe is the phone's only one: a
file trickling in over many host calls would hold it against everything else.
The chunks go to a temporary file and the transfer happens when the last one
has arrived.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xverb import DIRECTORY, Entry, FILE, FileSystem, Plugin, Root, RpcError  # noqa: E402
from xverb.fs import split_url  # noqa: E402

import ptp  # noqa: E402
import usb  # noqa: E402

plugin = Plugin("org.xverb.mtp", "Android phones")

#: A phone not asked anything for this long is let go, so that another
#: program — Image Capture, a backup tool — can have it.
IDLE_SECONDS = 60

LOCKED = ("The phone shows no storage. Unlock it; if it asks whether to allow "
          "access to its data, allow it; and check that USB is set to File "
          "transfer.")
NOT_CONNECTED = "No phone is connected. Plug it in and set USB to File transfer."


class _Phone:
    """One phone with a session, and what has been learnt about it."""

    def __init__(self, device: ptp.Device):
        self.device = device
        self.touched = time.monotonic()
        self.storages: List[ptp.Storage] = []
        #: handle -> description, for as long as the session lasts.
        self.known: Dict[int, ptp.Object] = {}
        #: (storage, parent) -> name -> handle, from the last listing.
        self.folders: Dict[Tuple[int, int], Dict[str, int]] = {}

    @property
    def name(self) -> str:
        return self.device.model or self.device.manufacturer or "Android phone"

    def storage_named(self, name: str) -> Optional[ptp.Storage]:
        for storage in self.storages:
            if _storage_label(storage, self.storages) == name:
                return storage
        return None

    def list(self, storage: int, parent: int) -> List[ptp.Object]:
        handles = self.device.handles(storage, parent)
        new = [h for h in handles if h not in self.known]
        if new:
            for found in self.device.describe(storage, parent, new):
                self.known[found.handle] = found
        rows = [self.known[h] for h in handles if h in self.known]
        self.folders[(storage, parent)] = {row.name: row.handle for row in rows}
        return rows

    def child(self, storage: int, parent: int, name: str) -> Optional[ptp.Object]:
        names = self.folders.get((storage, parent))
        handle = names.get(name) if names is not None else None
        if handle is not None and handle in self.known:
            return self.known[handle]
        for row in self.list(storage, parent):
            if row.name == name:
                return row
        return None

    def forget(self, handle: int) -> None:
        gone = self.known.pop(handle, None)
        if gone is not None:
            names = self.folders.get((gone.storage, gone.parent))
            if names is not None and names.get(gone.name) == handle:
                del names[gone.name]

    def learn(self, found: ptp.Object) -> None:
        self.known[found.handle] = found
        self.folders.setdefault((found.storage, found.parent), {})[found.name] = found.handle


def _storage_label(storage: ptp.Storage, all_storages: List[ptp.Storage]) -> str:
    label = (storage.description or storage.volume or "Storage").replace("/", "-")
    if sum(1 for s in all_storages
           if (s.description or s.volume or "Storage").replace("/", "-") == label) > 1:
        label = "%s %08X" % (label, storage.id)
    return label


def _gigabytes(count: int) -> str:
    return "%.1f GB" % (count / 1e9)


class _Upload:
    """A file on its way to the phone, gathered in a temporary file."""

    def __init__(self, url: str, size: Optional[int], modified: Optional[float]):
        self.url = url
        self.size = size
        self.modified = modified
        self.spool = tempfile.TemporaryFile(prefix="xverb-mtp-")


class MtpFileSystem(FileSystem):
    scheme = "mtp"

    def __init__(self):
        self._phones: Dict[str, _Phone] = {}
        self._lock = threading.RLock()
        self._uploads: Dict[str, _Upload] = {}
        self._pending: Dict[str, Tuple[Optional[int], Optional[float]]] = {}
        self._watcher = threading.Thread(target=self._let_go_idle, daemon=True)
        self._watcher.start()

    # -- phones ------------------------------------------------------------

    def _open_all(self) -> List[_Phone]:
        """Every phone on the machine, opened if it was not."""
        with self._lock:
            try:
                candidates = ptp.devices()
            except (OSError, usb.UsbError) as failure:
                raise RpcError(plugin.tr("USB cannot be read: {reason}", {"reason": failure}))
            present = set()
            for candidate in candidates:
                key = "%d.%d" % (candidate.bus, candidate.address)
                present.add(key)
                if any(p.device.candidate.bus == candidate.bus
                       and p.device.candidate.address == candidate.address
                       and not p.device.broken for p in self._phones.values()):
                    continue
                try:
                    device = ptp.Device(candidate)
                except (usb.UsbError, ptp.MtpError) as failure:
                    plugin.log("A phone at %s would not open: %s" % (key, failure), "warning")
                    continue
                phone = _Phone(device)
                self._drop(device.serial.lower())
                self._phones[device.serial.lower()] = phone
                plugin.log("Opened %s %s (%s)" % (device.manufacturer, device.model, device.serial))
            for serial, phone in list(self._phones.items()):
                c = phone.device.candidate
                if phone.device.broken or "%d.%d" % (c.bus, c.address) not in present:
                    self._drop(serial)
            return list(self._phones.values())

    def _phone(self, serial: str) -> _Phone:
        serial = (serial or "").lower()
        phone = self._phones.get(serial)
        if phone is None or phone.device.broken:
            self._open_all()
            phone = self._phones.get(serial)
        if phone is None:
            raise RpcError(plugin.tr(NOT_CONNECTED))
        phone.touched = time.monotonic()
        return phone

    def _drop(self, serial: str) -> None:
        phone = self._phones.pop(serial, None)
        if phone is not None:
            phone.device.close()

    def _let_go_idle(self) -> None:
        while True:
            time.sleep(10)
            with self._lock:
                if self._uploads:
                    continue
                now = time.monotonic()
                for serial, phone in list(self._phones.items()):
                    if now - phone.touched > IDLE_SECONDS:
                        self._drop(serial)

    def close_all(self) -> None:
        with self._lock:
            for serial in list(self._phones):
                self._drop(serial)

    def _storages(self, phone: _Phone) -> List[ptp.Storage]:
        # Asked afresh each time: it is one round trip, and it is how an
        # unlocked phone shows up — locked, it has no storage at all.
        phone.storages = phone.device.storages()
        return phone.storages

    # -- addressing --------------------------------------------------------

    def _where(self, url: str):
        """(phone, storage or None, parts of the path inside it)."""
        host, _port, _user, _password, path = split_url(url)
        phone = self._phone(host or "")
        parts = [p for p in path.split("/") if p]
        if not parts:
            return phone, None, []
        storage = phone.storage_named(parts[0])
        if storage is None:
            self._storages(phone)
            storage = phone.storage_named(parts[0])
        if storage is None:
            raise RpcError(plugin.tr("{phone} has no storage called {name}", {"phone": phone.name, "name": parts[0]}))
        return phone, storage, parts[1:]

    def _resolve(self, phone: _Phone, storage: ptp.Storage,
                 parts: List[str]) -> Optional[ptp.Object]:
        """The object at [parts], or None. The storage's top has no object."""
        parent = 0
        found: Optional[ptp.Object] = None
        for name in parts:
            found = phone.child(storage.id, parent, name)
            if found is None:
                return None
            parent = found.handle
        return found

    def _run(self, url: str, work):
        """[work] under the lock, turning the lower layers' failures into
        sentences, and a phone that went away into one that is asked for
        again next time."""
        with self._lock:
            try:
                return work(*self._where(url))
            except ptp.MtpError as failure:
                raise RpcError(_refusal(failure))
            except usb.UsbError as failure:
                host = split_url(url)[0] or ""
                self._drop(host.lower())
                raise RpcError(plugin.tr(
                    "The phone stopped answering ({reason}). Check the cable and "
                    "that the phone is unlocked.", {"reason": failure}))

    # -- navigation --------------------------------------------------------

    def roots(self) -> List[Root]:
        roots = []
        try:
            phones = self._open_all()
        except RpcError:
            return []
        with self._lock:
            for phone in phones:
                serial = phone.device.serial
                try:
                    storages = self._storages(phone)
                except (ptp.MtpError, usb.UsbError):
                    continue
                if not storages:
                    roots.append(Root("mtp://%s/" % quote(serial), phone.name,
                                      plugin.tr("Locked: unlock the phone to see its files"),
                                      icon="removable"))
                for storage in storages:
                    label = _storage_label(storage, storages)
                    roots.append(Root(
                        "mtp://%s/%s/" % (quote(serial), quote(label)),
                        phone.name if len(storages) == 1 else "%s: %s" % (phone.name, label),
                        plugin.tr("{storage}, {free} free of {size}", {
                            "storage": label, "free": _gigabytes(storage.free),
                            "size": _gigabytes(storage.capacity)}),
                        icon="removable"))
        return roots

    def default_location(self) -> str:
        phones = self._open_all()
        if not phones:
            raise RpcError(plugin.tr(NOT_CONNECTED))
        return "mtp://%s/" % quote(phones[0].device.serial)

    def list(self, url: str) -> List[Entry]:
        def work(phone: _Phone, storage: Optional[ptp.Storage], parts: List[str]):
            if storage is None:
                storages = self._storages(phone)
                if not storages:
                    raise RpcError(plugin.tr(LOCKED))
                return [Entry(_storage_label(s, storages), DIRECTORY) for s in storages]
            parent = 0
            if parts:
                folder = self._resolve(phone, storage, parts)
                if folder is None or not folder.is_folder:
                    raise RpcError(plugin.tr("There is no folder {path} on {phone}", {"path": "/".join(parts), "phone": phone.name}))
                parent = folder.handle
            return [_entry(row) for row in phone.list(storage.id, parent)]

        return self._run(url, work)

    def stat(self, url: str) -> Optional[Entry]:
        def work(phone: _Phone, storage: Optional[ptp.Storage], parts: List[str]):
            if storage is None:
                return Entry(phone.name, DIRECTORY)
            if not parts:
                return Entry(_storage_label(storage, phone.storages), DIRECTORY)
            found = self._resolve(phone, storage, parts)
            return None if found is None else _entry(found)

        return self._run(url, work)

    # -- transfer ----------------------------------------------------------

    def read(self, url: str, offset: int, length: int) -> bytes:
        def work(phone: _Phone, storage: Optional[ptp.Storage], parts: List[str]):
            found = self._resolve(phone, storage, parts) if storage and parts else None
            if found is None or found.is_folder:
                raise RpcError(plugin.tr("There is no file {path} on {phone}", {"path": "/".join(parts), "phone": phone.name}))
            if offset >= found.size:
                return b""
            want = min(length, found.size - offset)
            data = phone.device.read_part(found.handle, offset, want)
            if data is None:
                data = self._whole(phone, found)[offset:offset + want]
            return data

        return self._run(url, work)

    def _whole(self, phone: _Phone, found: ptp.Object) -> bytes:
        """For a phone that cannot hand over part of a file: the whole of it,
        kept for the reads that follow."""
        held = getattr(self, "_whole_held", None)
        if held is not None and held[0] == (id(phone), found.handle):
            return held[1]
        gathered = bytearray()
        phone.device.read_whole(found.handle, gathered.extend)
        self._whole_held = ((id(phone), found.handle), bytes(gathered))
        return self._whole_held[1]

    def begin_write(self, url: str, size, modified) -> None:
        self._pending[url] = (size, modified)

    def write(self, url: str, data: bytes, mode: str) -> None:
        with self._lock:
            upload = self._uploads.get(url)
            if mode == "create" or upload is None:
                if upload is not None:
                    upload.spool.close()
                size, modified = self._pending.pop(url, (None, None))
                upload = self._uploads[url] = _Upload(url, size, modified)
            upload.spool.write(data)

    def close_write(self, url: str, complete: bool) -> None:
        with self._lock:
            upload = self._uploads.pop(url, None)
            self._pending.pop(url, None)
        if upload is None:
            return
        try:
            if complete:
                self._send(upload)
        finally:
            upload.spool.close()

    def _send(self, upload: _Upload) -> None:
        def work(phone: _Phone, storage: Optional[ptp.Storage], parts: List[str]):
            if storage is None or not parts:
                raise RpcError(plugin.tr("A file goes inside one of the phone's storages"))
            parent = self._parent(phone, storage, parts)
            name = parts[-1]
            size = upload.spool.seek(0, os.SEEK_END)
            upload.spool.seek(0)
            if size > storage.free > 0:
                raise RpcError(plugin.tr("{name} does not fit: {free} free on {phone}", {
                    "name": name, "free": _gigabytes(storage.free), "phone": phone.name}))
            # MTP has no overwrite: the old one goes first.
            old = phone.child(storage.id, parent, name)
            if old is not None:
                if old.is_folder:
                    raise RpcError(plugin.tr("{name} is a folder on the phone", {"name": name}))
                phone.device.delete(old.handle)
                phone.forget(old.handle)
            handle = phone.device.send(storage.id, parent, name, size,
                                       upload.modified, upload.spool)
            phone.learn(ptp.Object(handle, storage.id, parent, name, False,
                                   size, upload.modified or time.time()))

        self._run(upload.url, work)

    def _parent(self, phone: _Phone, storage: ptp.Storage, parts: List[str]) -> int:
        if len(parts) == 1:
            return 0
        folder = self._resolve(phone, storage, parts[:-1])
        if folder is None or not folder.is_folder:
            raise RpcError(plugin.tr("There is no folder {path} on {phone}", {"path": "/".join(parts[:-1]), "phone": phone.name}))
        return folder.handle

    # -- changes -----------------------------------------------------------

    def mkdir(self, url: str) -> None:
        def work(phone: _Phone, storage: Optional[ptp.Storage], parts: List[str]):
            if storage is None or not parts:
                raise RpcError(plugin.tr("A phone's storages are the phone's to make"))
            parent = self._parent(phone, storage, parts)
            if phone.child(storage.id, parent, parts[-1]) is not None:
                raise RpcError(plugin.tr("{name} already exists", {"name": parts[-1]}))
            handle = phone.device.make_folder(storage.id, parent, parts[-1])
            phone.learn(ptp.Object(handle, storage.id, parent, parts[-1], True, 0, time.time()))

        self._run(url, work)

    def delete(self, url: str) -> None:
        def work(phone: _Phone, storage: Optional[ptp.Storage], parts: List[str]):
            found = self._resolve(phone, storage, parts) if storage and parts else None
            if found is None:
                raise RpcError(plugin.tr("There is nothing at {path} to delete", {"path": "/".join(parts)}))
            # A folder goes with everything in it: MTP deletes the subtree.
            phone.device.delete(found.handle)
            phone.forget(found.handle)
            if found.is_folder:
                self._forget_below(phone, found)

        self._run(url, work)

    @staticmethod
    def _forget_below(phone: _Phone, folder: ptp.Object) -> None:
        phone.folders.pop((folder.storage, folder.handle), None)
        for handle in [h for h, o in phone.known.items() if o.parent == folder.handle]:
            below = phone.known.pop(handle)
            if below.is_folder:
                MtpFileSystem._forget_below(phone, below)

    def rename(self, source: str, target: str) -> None:
        if (split_url(source)[0] or "").lower() != (split_url(target)[0] or "").lower():
            raise RpcError(plugin.tr("A move cannot cross from one phone to another; copy instead"))

        def work(phone: _Phone, storage: Optional[ptp.Storage], parts: List[str]):
            found = self._resolve(phone, storage, parts) if storage and parts else None
            if found is None:
                raise RpcError(plugin.tr("There is nothing at {path}", {"path": "/".join(parts)}))
            _phone, to_storage, to_parts = self._where(target)
            if to_storage is None or not to_parts:
                raise RpcError(plugin.tr("Nothing can take the place of a storage"))
            to_parent = self._parent(phone, to_storage, to_parts)
            name = to_parts[-1]
            if phone.child(to_storage.id, to_parent, name) is not None:
                raise RpcError(plugin.tr("{name} already exists", {"name": name}))
            if (to_storage.id, to_parent) != (found.storage, found.parent):
                phone.device.move(found.handle, to_storage.id, to_parent)
            if name != found.name:
                phone.device.rename(found.handle, name)
            phone.forget(found.handle)
            if found.is_folder:
                self._forget_below(phone, found)
            phone.learn(ptp.Object(found.handle, to_storage.id, to_parent, name,
                                   found.is_folder, found.size, found.modified))

        self._run(source, work)

    def copy_within(self, source: str, target: str) -> bool:
        """The copy done by the phone, file to file, when it can: nothing
        crosses the cable."""
        if (split_url(source)[0] or "").lower() != (split_url(target)[0] or "").lower():
            return False

        def work(phone: _Phone, storage: Optional[ptp.Storage], parts: List[str]):
            if ptp.COPY_OBJECT not in phone.device.operations:
                return False
            found = self._resolve(phone, storage, parts) if storage and parts else None
            if found is None or found.is_folder:
                return False
            _phone, to_storage, to_parts = self._where(target)
            if to_storage is None or not to_parts:
                return False
            to_parent = self._parent(phone, to_storage, to_parts)
            name = to_parts[-1]
            old = phone.child(to_storage.id, to_parent, name)
            if old is not None:
                if old.is_folder:
                    return False
                phone.device.delete(old.handle)
                phone.forget(old.handle)
            handle = phone.device.copy(found.handle, to_storage.id, to_parent)
            if name != found.name:
                phone.device.rename(handle, name)
            phone.learn(ptp.Object(handle, to_storage.id, to_parent, name, False,
                                   found.size, found.modified))
            return True

        return bool(self._run(source, work))


def _refusal(failure: ptp.MtpError) -> str:
    """What the phone said, as a sentence in the user's language."""
    reason = (plugin.tr(failure.reason) if failure.reason
              else plugin.tr("the phone answered {code}", {"code": "%#06x" % failure.code}))
    return "%s: %s" % (plugin.tr(failure.what), reason)


def _entry(row: ptp.Object) -> Entry:
    return Entry(
        name=row.name,
        kind=DIRECTORY if row.is_folder else FILE,
        size=row.size,
        modified=row.modified,
        hidden=row.name.startswith("."),
    )


mtp_fs = plugin.add_filesystem(MtpFileSystem())


@plugin.command("mtp.release", "Let go of connected phones")
def release(_args):
    mtp_fs.close_all()
    return {"ok": True}


@plugin.on_shutdown
def shutdown():
    mtp_fs.close_all()


if __name__ == "__main__":
    plugin.run()
