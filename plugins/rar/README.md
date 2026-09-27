# RAR archives

A RAR opens like a folder: Enter walks into it, F5 copies out of it, F3 lists
what is in it. RAR 4 and RAR 5, solid archives, links, names in any script.
`.cbr` comic books are RARs and open the same way.

**Read-only, and it has to be.** Nobody can write a RAR but RARLAB's own
program: the compressor is closed. The panel knows this before you press
anything — the pack dialog never offers RAR, and F7, F8 and F6 inside one are
off.

**Sets of volumes.** `name.part1.rar`, `name.part2.rar`… or the older
`name.rar`, `name.r00`, `name.r01`… are one archive. Enter any of them and the
whole set opens, as long as every part is in the same folder on this machine.
An archive on another file system — FTP, another plugin — is read one volume
at a time.

**What cannot be read.** Encrypted files, and archives whose names are
encrypted: libarchive does not decrypt RAR. They are listed where the names
can be read, and said to be encrypted.

## Where the reading is done

[libarchive](https://libarchive.org), BSD-licensed. Not RARLAB's UnRAR: its
licence forbids using it to re-create the compressor, which the GPL this
plugin is under cannot take in — and 7-Zip's RAR code is derived from it.
libarchive's readers were written independently.

- **macOS** has libarchive in the system (`/usr/lib/libarchive.2.dylib`), so
  nothing is shipped.
- **Windows x64, Linux x64 and arm64** use the library in `native/`, built by
  `native/build.sh` with zig from libarchive 3.8.1, checked by its sha256.
  No zlib, iconv or OpenSSL: one file, the C runtime and nothing else.
- A Linux machine that has `libarchive.so.13` of its own uses it when the
  shipped one is missing. `XVERB_RAR_LIBARCHIVE` names a library to use
  instead.

libarchive's licence is in `native/COPYING.libarchive`. The archives in
`fixtures/` are from libarchive's own tests, under the same licence.

## Checking it

`python3 selftest.py` reads every fixture through the library, then drives
`main.py` over the pipe the way the application does — listing, reading in
256 KB pieces in and out of order, F3, and the refusals.
