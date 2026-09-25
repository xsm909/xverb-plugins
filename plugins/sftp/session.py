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

"""The machine's own `ssh`, started as the SFTP subsystem.

**Why the system's ssh and not one written here.** The standard library has no
SSH, and one written in Python would do its ciphers in Python — a megabyte or
two a second, on a transport whose whole job is moving files. OpenSSH is on
every machine this runs on (macOS and Linux always, Windows since 10 1809), it
is fast, and it already knows everything the user has set up: keys, the agent,
`~/.ssh/config` with its aliases and jump hosts, `known_hosts`.

**What is fixed on the command line, whatever the config says.** No terminal,
no forwarding of X11, the agent or ports, no remote command and no local one:
a config written for interactive use must not turn a file transfer into a
shell. Unknown host keys are accepted and remembered, a *changed* one is
refused — `accept-new`, which is what a first `ssh` to a new machine does
after the user types yes, minus the typing.

**The password goes through `SSH_ASKPASS`**, a small program ssh runs to be
told the secret, and the secret reaches it through the environment of that one
process. `SSH_ASKPASS_REQUIRE=force` is OpenSSH 8.4 and later; an older ssh
with no terminal falls back to asking the askpass program anyway when
`DISPLAY` is set, which is why it is.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from typing import List, Optional

from sftp import ChannelClosed, SftpClient

HERE = os.path.dirname(os.path.abspath(__file__))

#: How long the whole handshake may take: resolving, connecting, the key
#: exchange, authentication and the SFTP version, together. A server that
#: has not answered by then is not going to, and a hung process is the one
#: thing worse than an error.
HANDSHAKE_SECONDS = 40


class SshFailed(Exception):
    """ssh could not be started or did not get as far as SFTP."""


def find_ssh(configured: str = "") -> Optional[str]:
    if configured:
        return configured if os.path.isfile(configured) else None
    found = shutil.which("ssh")
    if found:
        return found
    if os.name == "nt":
        # The optional feature installs here and does not always reach PATH
        # of a process started from a shortcut.
        root = os.environ.get("SystemRoot", r"C:\Windows")
        for folder in ("System32", "Sysnative"):
            candidate = os.path.join(root, folder, "OpenSSH", "ssh.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


_launcher: Optional[str] = None


def _askpass_launcher() -> str:
    """An executable ssh can run that runs `askpass.py`.

    Written at run time into a private folder, because a plugin installed
    from a downloaded archive has lost its executable bits, and ssh will not
    run a script through an interpreter it has not been told about.
    """
    global _launcher
    if _launcher and os.path.exists(_launcher):
        return _launcher
    folder = tempfile.mkdtemp(prefix="xverb-sftp-")
    script = os.path.join(HERE, "askpass.py")
    if os.name == "nt":
        path = os.path.join(folder, "askpass.cmd")
        with open(path, "w", encoding="utf-8") as out:
            out.write('@"%s" "%s" %%*\r\n' % (sys.executable, script))
    else:
        path = os.path.join(folder, "askpass")
        with open(path, "w", encoding="utf-8") as out:
            out.write("#!/bin/sh\nexec '%s' '%s' \"$@\"\n"
                      % (sys.executable.replace("'", "'\\''"),
                         script.replace("'", "'\\''")))
        os.chmod(path, stat.S_IRWXU)
    _launcher = path
    return path


def command(ssh: str, host: str, port: Optional[int], user: Optional[str],
            key_file: str) -> List[str]:
    if host.startswith("-"):
        raise SshFailed("%r is not a host name" % host)
    args = [
        ssh, "-T", "-x", "-a",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "RemoteCommand=none",
        "-o", "RequestTTY=no",
        "-o", "PermitLocalCommand=no",
        "-o", "ClearAllForwardings=yes",
        # Warnings such as "permanently added to the list of known hosts"
        # would otherwise be the last line of every error that follows.
        "-o", "LogLevel=ERROR",
    ]
    if port:
        args += ["-p", str(port)]
    if user:
        args += ["-l", user]
    if key_file:
        args += ["-i", os.path.expanduser(key_file)]
    return args + ["-s", "--", host, "sftp"]


class SshSession:
    """One ssh process and the SFTP conversation over its pipes."""

    def __init__(self, args: List[str], password: str = ""):
        self.args = args
        self.password = password
        self.process: Optional[subprocess.Popen] = None
        self.client: Optional[SftpClient] = None
        self._errors: List[str] = []

    def open(self) -> SftpClient:
        env = dict(os.environ)
        env.update({
            "SSH_ASKPASS": _askpass_launcher(),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": env.get("DISPLAY") or ":0",
            "XVERB_SFTP_SECRET": self.password,
        })
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=flags,
                # No terminal of its own to ask on, on any system: a new
                # session has no controlling tty, so ssh has to use askpass.
                start_new_session=os.name != "nt",
            )
        except OSError as failure:
            raise SshFailed("ssh could not be started: %s" % failure)

        threading.Thread(target=self._collect_errors, daemon=True).start()
        client = SftpClient(self.process.stdout, self.process.stdin)

        # The handshake in a thread of its own, so that a server that never
        # answers — or an ssh waiting on a prompt nobody can see — is killed
        # rather than holding the panel for ever.
        outcome: List[object] = []

        def handshake():
            try:
                client.start()
                outcome.append(None)
            except Exception as failure:  # noqa: BLE001
                outcome.append(failure)

        worker = threading.Thread(target=handshake, daemon=True)
        worker.start()
        worker.join(HANDSHAKE_SECONDS)
        if worker.is_alive() or outcome[0] is not None:
            timed_out = worker.is_alive()
            self.close()
            worker.join(2)
            raise SshFailed(self._explain(timed_out))
        self.client = client
        return client

    def _collect_errors(self) -> None:
        stream = self.process.stderr if self.process else None
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._errors.append(line)
                del self._errors[:-20]

    @property
    def errors(self) -> List[str]:
        return list(self._errors)

    def _explain(self, timed_out: bool) -> str:
        """ssh's own words, with a sentence in front where they need one."""
        if self.process is not None:
            try:
                self.process.wait(2)
            except subprocess.TimeoutExpired:
                pass
        said = " ".join(self._errors[-3:])
        lowered = said.lower()
        if "identification has changed" in lowered or "host key verification failed" in lowered:
            return ("The server's key is not the one this machine remembers for it. "
                    "That is what a server that was reinstalled looks like, and also "
                    "what somebody in the middle looks like; if it is expected, "
                    "remove the old key with ssh-keygen -R <host>. ssh said: " + said)
        if "permission denied" in lowered:
            return "The server did not accept the login. ssh said: " + said
        if "subsystem request failed" in lowered:
            return "The server has no SFTP subsystem enabled. ssh said: " + said
        if timed_out:
            return ("No answer within %d seconds%s. An ssh older than OpenSSH 8.4 "
                    "cannot be given a password here — a key works with any."
                    % (HANDSHAKE_SECONDS, (" — ssh said: " + said) if said else ""))
        return said or "ssh exited before SFTP started"

    def close(self) -> None:
        process, self.process = self.process, None
        self.client = None
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        try:
            process.wait(2)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(2)
            except subprocess.TimeoutExpired:
                pass

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


__all__ = ["SshSession", "SshFailed", "ChannelClosed", "command", "find_ssh"]
