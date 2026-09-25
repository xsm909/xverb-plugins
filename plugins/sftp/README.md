# SFTP

Files on any machine you can `ssh` into, in a panel like the local disk.

**It goes through the machine's own `ssh`.** Python's standard library has no
SSH, and one written in Python would do its ciphers in Python — a megabyte or
two a second. OpenSSH is on every machine Xverb runs on: macOS and Linux
always, Windows 10 1809 and later as the optional feature *OpenSSH Client*.
Using it means everything already set up for it works here too:

| | |
| --- | --- |
| keys | whatever ssh offers, and one more named in the connection |
| the agent | as in a terminal, including a key on a hardware token |
| `~/.ssh/config` | host aliases, users, ports, `ProxyJump` |
| `known_hosts` | read and added to |

## Connecting

**Drives and connections → SFTP.** Host, user, and either a password or
nothing, for a key. The remote directory may be `~` for the home folder, which
is where the server puts you: in a URL it is `/~`, so
`sftp://alice@server/~/projects` is `projects` in Alice's home, whatever it is
called on that server.

A password is handed to ssh through `SSH_ASKPASS` when it asks, and reaches
it through the environment of that one process, never its command line.
**That needs OpenSSH 8.4 or later** — an older ssh gets no chance to ask and
the connection gives up after 40 seconds saying so. A key works with any.

**A new server's key is accepted and remembered; a changed one is refused**
(`StrictHostKeyChecking=accept-new`). A changed key is what a reinstalled
server looks like and also what somebody in the middle looks like, and the
error says which command removes the old one if it is the first.

## What it does

Lists, reads, writes, makes folders, renames and deletes, recursively. Reads
and writes are pipelined — every request of a 256 KB chunk goes out before the
first answer is read. A file arrives with its own modification date. A copy
that is cancelled removes the half it wrote. A copy within one server is done
by the server when it has OpenSSH 9's `copy-data`, and nothing crosses the
network. A session that died while the laptop slept is reopened by the next
request.

What ssh fixes regardless of the config: no terminal, no forwarding, no remote
or local command, so a config written for interactive use cannot turn a file
transfer into a shell.

## Checking it

```
PYTHONPATH=<xverb>/assets/python python3 selftest.py
```

It needs no server: `sftp-server`, the program sshd runs for the subsystem,
speaks SFTP on its own stdin and stdout, which is exactly what `ssh -s sftp`
hands over. The whole file system is driven against a temporary folder
through it.
