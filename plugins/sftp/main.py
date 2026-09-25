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

"""SFTP as an xverb file system.

Three layers, one file each. `session.py` starts the machine's own `ssh` as the
SFTP subsystem and gives it a password when it asks; `sftp.py` speaks the
protocol over the two pipes; this file is the adapter between that and what
the host asks for.

A URL is `sftp://user@host:port/path`. **`/~` is the home folder** — the path a
server puts a user in, whatever it is called there — so a connection can open
where `sftp` on the command line would without anybody knowing that it is
`/home/alice` on one server and `/Users/alice` on the next.

What the connection dialog collects beyond the host, the user and the password
rides along as query parameters: `keyFile`, a private key to offer.
"""

from __future__ import annotations

import os
import posixpath
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xverb import DIRECTORY, Entry, FILE, FileSystem, Plugin, RpcError  # noqa: E402
from xverb.fs import query_of, split_url  # noqa: E402

import sftp  # noqa: E402
from sftp import ChannelClosed, SftpClient, SftpError  # noqa: E402
from session import SshFailed, SshSession, command, find_ssh  # noqa: E402

plugin = Plugin("org.xverb.sftp", "SFTP")

#: A session unused for longer than this is closed rather than trusted. The
#: manifest offers it as a setting; ssh's own keep-alive holds it open until.
DEFAULT_IDLE_TIMEOUT = 300


def idle_timeout() -> float:
    return float(plugin.setting("idleTimeout", DEFAULT_IDLE_TIMEOUT))


class _Session:
    def __init__(self, ssh: SshSession, home: str):
        self.ssh = ssh
        self.home = home
        self.touched = time.monotonic()

    @property
    def client(self) -> SftpClient:
        client = self.ssh.client
        if client is None:
            raise ChannelClosed("the session was closed")
        return client

    def is_stale(self) -> bool:
        return (not self.ssh.alive
                or time.monotonic() - self.touched > idle_timeout())


class _Open:
    """A file handle kept open between calls, for one path.

    The host reads a file as a run of `read(offset, length)` calls and writes
    one as a `create` and then `append`s. Opening and closing the file around
    each would be two more round trips per call, so the handle stays open
    until the file is done with — the end reached, the write closed, or some
    other file wanted.
    """

    def __init__(self, key: tuple, path: str, handle: bytes, writing: bool):
        self.key = key
        self.path = path
        self.handle = handle
        self.writing = writing
        self.offset = 0
        self.mtime: Optional[int] = None


def _kind(attrs: sftp.Attributes) -> str:
    return DIRECTORY if attrs.is_directory else FILE


def _entry(name: str, attrs: sftp.Attributes, target: Optional[str] = None,
           resolved: Optional[sftp.Attributes] = None) -> Entry:
    # A link is described by what it points at, with the link itself kept in
    # `target` — the host's rule, so a link to a folder is walked into.
    shown = resolved or attrs
    return Entry(
        name=name,
        kind=_kind(shown),
        size=0 if shown.is_directory else (shown.size or 0),
        modified=shown.mtime,
        hidden=name.startswith("."),
        target=target,
    )


class SftpFileSystem(FileSystem):
    scheme = "sftp"

    def __init__(self):
        self._sessions: Dict[tuple, _Session] = {}
        self._open: Optional[_Open] = None
        self._pending_mtime: Dict[str, int] = {}
        self._lock = threading.RLock()

    # -- connection handling ----------------------------------------------

    def _session(self, url: str) -> Tuple[tuple, _Session, str]:
        host, port, user, password, path = split_url(url)
        if not host:
            raise RpcError("No server in %s" % url)
        options = query_of(url)
        key_file = options.get("keyFile", "").strip()

        key = (host, port or 22, user or "", key_file)
        cached = self._sessions.get(key)
        if cached is not None and not cached.is_stale():
            cached.touched = time.monotonic()
            return key, cached, self._resolve(cached, path)
        if cached is not None:
            self._drop(key)

        ssh = find_ssh(str(plugin.setting("sshPath", "") or ""))
        if ssh is None:
            raise RpcError(
                "There is no ssh on this machine. SFTP here goes through the "
                "system's own OpenSSH client — on Windows it is the optional "
                "feature called OpenSSH Client."
            )
        session = SshSession(command(ssh, host, port, user, key_file), password or "")
        try:
            client = session.open()
            home = client.realpath(".")
        except SshFailed as failure:
            raise RpcError("Cannot connect to %s: %s" % (host, failure))
        except (SftpError, ChannelClosed) as failure:
            session.close()
            raise RpcError("Cannot connect to %s: %s" % (host, failure))

        opened = _Session(session, home)
        self._sessions[key] = opened
        plugin.log("Connected to %s as %s, SFTP %d, reads of %d KB, home %s%s"
                   % (host, user or "(ssh's default user)", client.version,
                      client.max_read // 1024, home,
                      ", copies on the server" if client.can_copy else ""))
        return key, opened, self._resolve(opened, path)

    @staticmethod
    def _resolve(session: _Session, path: str) -> str:
        """The URL's path as the server's: `/~/x` is `x` in the home folder."""
        path = path or "/"
        if path == "/~" or path.startswith("/~/"):
            rest = path[3:]
            return posixpath.join(session.home, rest) if rest else session.home
        return path

    def _drop(self, key: tuple) -> None:
        if self._open is not None and self._open.key == key:
            self._open = None
        session = self._sessions.pop(key, None)
        if session is not None:
            session.ssh.close()

    def close_all(self) -> None:
        with self._lock:
            for key in list(self._sessions):
                self._drop(key)

    def _run(self, url: str, work: Callable[[SftpClient, str], object],
             retry: bool = True):
        """[work] on the session for [url], once more on a new session if the
        old one turns out to have died — ssh exits quietly when a laptop
        sleeps, and the first anybody hears of it is the next request."""
        with self._lock:
            key, session, path = self._session(url)
            try:
                return work(session.client, path)
            except ChannelClosed as failure:
                errors = " ".join(session.ssh.errors[-2:])
                self._drop(key)
                if not retry:
                    raise RpcError("The connection was lost: %s" % (errors or failure))
            key, session, path = self._session(url)
            try:
                return work(session.client, path)
            except ChannelClosed as failure:
                self._drop(key)
                raise RpcError("The connection was lost: %s" % failure)

    def _let_go(self, client: SftpClient, path: Optional[str] = None) -> None:
        """Close the file kept open, if any — or only if it is [path]."""
        held = self._open
        if held is None or (path is not None and held.path != path):
            return
        self._open = None
        try:
            client.close(held.handle)
        except (SftpError, ChannelClosed):
            pass

    # -- navigation --------------------------------------------------------

    def default_location(self) -> str:
        return "sftp:///"

    def list(self, url: str) -> List[Entry]:
        def work(client: SftpClient, path: str) -> List[Entry]:
            try:
                rows = client.listdir(path)
            except SftpError as failure:
                raise RpcError("Cannot list %s: %s" % (path, failure))

            # Links are asked about all at once: where each points, and what
            # is there. A listing of /usr/lib with a hundred links in it is
            # then two round trips rather than two hundred.
            links = [(n, a) for n, a in rows if a.is_link]
            full = [posixpath.join(path, n) for n, _ in links]
            targets = client.readlink_many(full) if full else []
            resolved = client.stat_many(full) if full else []
            by_name = {
                n: (t, r) for (n, _), t, r in zip(links, targets, resolved)
            }

            entries = []
            for name, attrs in rows:
                if attrs.is_link:
                    target, found = by_name.get(name, (None, None))
                    entries.append(_entry(name, attrs, target or "?", found))
                else:
                    entries.append(_entry(name, attrs))
            return entries

        return self._run(url, work)

    def stat(self, url: str) -> Optional[Entry]:
        def work(client: SftpClient, path: str) -> Optional[Entry]:
            try:
                attrs = client.stat(path)
            except SftpError as failure:
                if failure.code == sftp.NO_SUCH_FILE:
                    return None
                raise RpcError("Cannot read %s: %s" % (path, failure))
            return _entry(posixpath.basename(path.rstrip("/")) or "/", attrs)

        return self._run(url, work)

    # -- transfer ----------------------------------------------------------

    def read(self, url: str, offset: int, length: int) -> bytes:
        def work(client: SftpClient, path: str) -> bytes:
            held = self._open
            key = self._key_of(url)
            if held is None or held.writing or held.path != path or held.key != key:
                self._let_go(client)
                try:
                    handle = client.open(path, sftp.O_READ)
                except SftpError as failure:
                    raise RpcError("Cannot open %s: %s" % (path, failure))
                held = self._open = _Open(key, path, handle, writing=False)
            try:
                data = client.read(held.handle, offset, length)
            except SftpError as failure:
                self._let_go(client)
                raise RpcError("Cannot read %s: %s" % (path, failure))
            if len(data) < length:
                # The end: nothing more will be asked of this handle.
                self._let_go(client)
            return data

        return self._run(url, work)

    def _key_of(self, url: str) -> tuple:
        host, port, user, _password, _path = split_url(url)
        return (host, port or 22, user or "",
                query_of(url).get("keyFile", "").strip())

    def begin_write(self, url: str, size, modified) -> None:
        if modified is not None:
            self._pending_mtime[url] = int(modified)

    def write(self, url: str, data: bytes, mode: str) -> None:
        def work(client: SftpClient, path: str) -> None:
            held = self._open
            key = self._key_of(url)
            if mode == "create" or held is None or not held.writing or held.path != path:
                self._let_go(client)
                flags = sftp.O_WRITE | sftp.O_CREAT
                offset = 0
                if mode == "create":
                    flags |= sftp.O_TRUNC
                else:
                    # An append with no handle open: the handle was lost with
                    # a dropped session, and the file on the server is the
                    # record of how far the copy got.
                    try:
                        offset = client.stat(path).size or 0
                    except SftpError:
                        offset = 0
                try:
                    handle = client.open(path, flags)
                except SftpError as failure:
                    raise RpcError("Cannot create %s: %s" % (path, failure))
                held = self._open = _Open(key, path, handle, writing=True)
                held.offset = offset
                held.mtime = self._pending_mtime.pop(url, None)
            try:
                client.write(held.handle, held.offset, data)
            except SftpError as failure:
                self._let_go(client)
                raise RpcError("Cannot write %s: %s" % (path, failure))
            held.offset += len(data)

        # Not retried on a fresh session: half a chunk may have landed, and
        # the offset the retry would use is only a guess.
        self._run(url, work, retry=False)

    def close_write(self, url: str, complete: bool) -> None:
        def work(client: SftpClient, path: str) -> None:
            held = self._open
            mtime = held.mtime if held is not None and held.path == path else None
            self._let_go(client, path)
            self._pending_mtime.pop(url, None)
            if not complete:
                # A cancelled copy leaves no truncated file behind to be taken
                # for the whole one.
                try:
                    client.remove(path)
                except SftpError:
                    pass
                return
            if mtime is not None:
                try:
                    client.set_mtime(path, mtime)
                except SftpError:
                    pass  # a server that will not date a file still has it

        self._run(url, work, retry=False)

    def copy_within(self, source: str, target: str) -> bool:
        """The copy done by the server, where it can and both ends are on it.

        OpenSSH 9.0's `copy-data` copies between two handles it holds; nothing
        comes down the wire and goes back up it again.
        """
        if self._key_of(source) != self._key_of(target):
            return False

        def work(client: SftpClient, path: str) -> bool:
            if not client.can_copy:
                return False
            _key, session, target_path = self._session(target)
            self._let_go(client)
            try:
                attrs = client.stat(path)
                if attrs.is_directory:
                    return False
                reading = client.open(path, sftp.O_READ)
            except SftpError:
                return False
            try:
                writing = client.open(target_path, sftp.O_WRITE | sftp.O_CREAT | sftp.O_TRUNC)
            except SftpError as failure:
                client.close(reading)
                raise RpcError("Cannot create %s: %s" % (target_path, failure))
            try:
                client.copy_data(reading, writing)
            except SftpError as failure:
                raise RpcError("The server could not copy %s: %s" % (path, failure))
            finally:
                client.close(reading)
                client.close(writing)
            if attrs.mtime is not None:
                try:
                    client.set_mtime(target_path, attrs.mtime)
                except SftpError:
                    pass
            return True

        return bool(self._run(source, work, retry=False))

    # -- changes -----------------------------------------------------------

    def mkdir(self, url: str) -> None:
        def work(client: SftpClient, path: str) -> None:
            try:
                client.mkdir(path)
            except SftpError as failure:
                raise RpcError("Cannot create %s: %s" % (path, failure))

        self._run(url, work)

    def delete(self, url: str) -> None:
        def work(client: SftpClient, path: str) -> None:
            self._let_go(client, path)
            try:
                attrs = client.lstat(path)
            except SftpError as failure:
                raise RpcError("Cannot delete %s: %s" % (path, failure))
            try:
                if attrs.is_directory:
                    self._delete_tree(client, path)
                else:
                    # A link is removed as a link, never followed.
                    client.remove(path)
            except SftpError as failure:
                raise RpcError("Cannot delete %s: %s" % (path, failure))

        self._run(url, work, retry=False)

    def _delete_tree(self, client: SftpClient, path: str) -> None:
        """Depth first: SFTP has no recursive delete, and RMDIR refuses a
        folder with anything in it."""
        for name, attrs in client.listdir(path):
            child = posixpath.join(path, name)
            if attrs.is_directory:
                self._delete_tree(client, child)
            else:
                client.remove(child)
        client.rmdir(path)

    def rename(self, source: str, target: str) -> None:
        if self._key_of(source) != self._key_of(target):
            raise RpcError("A rename cannot cross from one server to another; copy instead")

        def work(client: SftpClient, path: str) -> None:
            _key, session, target_path = self._session(target)
            self._let_go(client)
            try:
                client.rename(path, target_path)
            except SftpError as failure:
                raise RpcError("Cannot rename %s: %s" % (path, failure))

        self._run(source, work, retry=False)


sftp_fs = plugin.add_filesystem(SftpFileSystem())


@plugin.command("sftp.disconnect", "Disconnect all SFTP sessions")
def disconnect_all(_args):
    sftp_fs.close_all()
    return {"ok": True}


@plugin.on_shutdown
def disconnect():
    sftp_fs.close_all()


if __name__ == "__main__":
    plugin.run()
