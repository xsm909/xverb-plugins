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

"""The SFTP plugin checked with no server and no network.

**The protocol half needs no SSH at all.** `sftp-server` is the program sshd
runs for the subsystem, and started directly it speaks SFTP on its own stdin
and stdout — exactly the pipes `ssh -s sftp` hands over. So the whole file
system is driven here against a temporary folder, through the same adapter
the host calls, with `command()` pointed at `sftp-server` instead of `ssh`.

**The ssh half is checked where it can be without a server:** that askpass
answers a password prompt and refuses a yes/no one, and that a connection
nobody answers comes back as a sentence rather than a hang.

    python3 selftest.py [path to sftp-server]

The xverb SDK has to be importable — `PYTHONPATH` pointing at the host's
`assets/python`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import main  # noqa: E402
import sftp  # noqa: E402
import session  # noqa: E402

CANDIDATES = [
    "/usr/libexec/sftp-server",
    "/usr/lib/openssh/sftp-server",
    "/usr/lib/ssh/sftp-server",
    "/usr/libexec/openssh/sftp-server",
    r"C:\Windows\System32\OpenSSH\sftp-server.exe",
]

failures = []


def check(what: str, ok: bool, detail: object = "") -> None:
    print("%s  %s%s" % ("ok  " if ok else "FAIL", what, ("  — %s" % (detail,)) if detail else ""))
    if not ok:
        failures.append(what)


def run_filesystem(server: str) -> None:
    root = tempfile.mkdtemp(prefix="xverb-sftp-test-")
    # macOS hands out /var/... which is a link to /private/var/...; the server
    # reports real paths, so the test speaks them too.
    root = os.path.realpath(root)
    try:
        main.command = lambda *_args, **_kw: [server, "-e"]
        main.find_ssh = lambda _configured="": server
        fs = main.SftpFileSystem()
        base = "sftp://tester@localhost" + root

        # A folder, a file in it, and one big enough to need many requests.
        fs.mkdir(base + "/sub")
        small = b"hello over sftp\n"
        fs.begin_write(base + "/sub/a.txt", len(small), 1_600_000_000)
        fs.write(base + "/sub/a.txt", small, "create")
        fs.close_write(base + "/sub/a.txt", True)
        check("a small file arrives with its date",
              os.path.getmtime(os.path.join(root, "sub", "a.txt")) == 1_600_000_000)

        big = os.urandom(3 * 1024 * 1024 + 12345)
        started = time.time()
        step = 256 * 1024
        for at in range(0, len(big), step):
            fs.write(base + "/big.bin", big[at:at + step], "create" if at == 0 else "append")
        fs.close_write(base + "/big.bin", True)
        wrote = time.time() - started
        with open(os.path.join(root, "big.bin"), "rb") as f:
            check("a 3 MB file written in 256 KB appends is byte for byte", f.read() == big)

        started = time.time()
        got = b""
        while True:
            chunk = fs.read(base + "/big.bin", len(got), step)
            got += chunk
            if len(chunk) < step:
                break
        read = time.time() - started
        check("and reads back byte for byte", got == big, "%d bytes" % len(got))
        print("      write %.3fs, read %.3fs for %.1f MB"
              % (wrote, read, len(big) / 1048576))

        check("a read past the end is empty", fs.read(base + "/big.bin", len(big) + 10, 100) == b"")

        os.symlink("sub", os.path.join(root, "to-sub"))
        os.symlink("missing", os.path.join(root, "dangling"))
        rows = {e.name: e for e in fs.list(base)}
        check("the listing has the folder, the file and both links",
              set(rows) == {"sub", "big.bin", "to-sub", "dangling"}, sorted(rows))
        check("a folder is a folder", rows["sub"].kind == main.DIRECTORY)
        check("a file has its size", rows["big.bin"].size == len(big))
        check("a link to a folder is walked like one, and says where it points",
              rows["to-sub"].kind == main.DIRECTORY and rows["to-sub"].target == "sub",
              (rows["to-sub"].kind, rows["to-sub"].target))
        check("a dangling link is still listed", rows["dangling"].target == "missing")

        check("stat of a file", fs.stat(base + "/sub/a.txt").size == len(small))
        check("stat of nothing is None", fs.stat(base + "/nothing") is None)

        fs.rename(base + "/sub/a.txt", base + "/sub/b.txt")
        check("rename", os.path.exists(os.path.join(root, "sub", "b.txt")))
        with open(os.path.join(root, "sub", "c.txt"), "wb") as f:
            f.write(b"old")
        fs.rename(base + "/sub/b.txt", base + "/sub/c.txt")
        with open(os.path.join(root, "sub", "c.txt"), "rb") as f:
            check("rename onto an existing file replaces it", f.read() == small)

        client = fs._sessions[next(iter(fs._sessions))].client
        if client.can_copy:
            copied = fs.copy_within(base + "/big.bin", base + "/sub/copy.bin")
            with open(os.path.join(root, "sub", "copy.bin"), "rb") as f:
                check("a copy done by the server", copied and f.read() == big)
        else:
            print("      (this sftp-server has no copy-data; copy_within says so)")
            check("copy_within declines", fs.copy_within(base + "/big.bin", base + "/x") is False)

        fs.write(base + "/half.bin", b"x" * 1000, "create")
        fs.close_write(base + "/half.bin", False)
        check("a cancelled copy leaves nothing", not os.path.exists(os.path.join(root, "half.bin")))

        fs.delete(base + "/to-sub")
        check("deleting a link leaves what it points at",
              os.path.isdir(os.path.join(root, "sub")) and not os.path.lexists(os.path.join(root, "to-sub")))
        fs.delete(base + "/sub")
        check("a folder is deleted with what is in it", not os.path.exists(os.path.join(root, "sub")))

        home = fs.list("sftp://tester@localhost/~")
        check("/~ lists the home folder", isinstance(home, list), "%d entries" % len(home))

        # A session that died between two calls is replaced, not reported.
        key = next(iter(fs._sessions))
        fs._sessions[key].ssh.process.kill()
        fs._sessions[key].ssh.process.wait()
        check("a dead session is reopened on the next call",
              {e.name for e in fs.list(base)} >= {"big.bin"})
        fs.close_all()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_askpass() -> None:
    script = os.path.join(HERE, "askpass.py")
    env = dict(os.environ, XVERB_SFTP_SECRET="s3cret word")
    said = subprocess.run([sys.executable, script, "alice@host's password: "],
                          env=env, capture_output=True, text=True)
    check("askpass gives the password", said.stdout == "s3cret word\n" and said.returncode == 0)
    said = subprocess.run([sys.executable, script, "Are you sure you want to continue connecting (yes/no/[fingerprint])? "],
                          env=env, capture_output=True, text=True)
    check("askpass says no to a yes/no question", said.stdout == "no\n")
    said = subprocess.run([sys.executable, script, "Password: "],
                          env=dict(os.environ, XVERB_SFTP_SECRET=""), capture_output=True, text=True)
    check("askpass with no secret fails at once", said.returncode == 1 and not said.stdout)

    launcher = session._askpass_launcher()
    said = subprocess.run([launcher, "Password: "], env=env, capture_output=True, text=True)
    check("the launcher ssh runs reaches askpass", said.stdout == "s3cret word\n", said.stderr)


def run_refused() -> None:
    ssh = session.find_ssh()
    if ssh is None:
        print("      (no ssh here; the refusal is not checked)")
        return
    started = time.time()
    attempt = session.SshSession(session.command(ssh, "127.0.0.1", 1, "nobody", ""), "x")
    try:
        attempt.open()
        check("a closed port is refused", False)
    except session.SshFailed as failure:
        check("a closed port comes back as ssh's own sentence, quickly",
              time.time() - started < 10 and "refused" in str(failure).lower(), failure)


if __name__ == "__main__":
    server = sys.argv[1] if len(sys.argv) > 1 else next(
        (c for c in CANDIDATES if os.path.isfile(c)), None)
    if server is None:
        print("No sftp-server found; pass its path.")
        sys.exit(2)
    run_filesystem(server)
    run_askpass()
    run_refused()
    print("\n%s" % ("all passed" if not failures else "%d failed: %s" % (len(failures), failures)))
    sys.exit(1 if failures else 0)
