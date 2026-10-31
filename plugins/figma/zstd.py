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

"""Zstandard, decompression only, in the standard library's absence.

A Figma file keeps its document in a zstd frame, and the interpreter the
application ships is 3.12, where zstd is not in the standard library — it
arrives in 3.14 as `compression.zstd`. A plugin may not install anything, so
the format is read here: frames, blocks, the Huffman literals and the FSE
sequences of RFC 8878.

Where the standard library does have it, that is used instead and none of this
runs. So the module gets faster for free on a newer interpreter, and the code
below is what makes the plugin work today.

What is not here: compression, dictionaries, and the checksum, which is read
past rather than verified — nothing in a file like this hangs on catching a bit
flip that the surrounding zip's own CRC already caught.
"""

from __future__ import annotations

import struct

try:  # Python 3.14 and later.
    from compression.zstd import decompress as _native
except ImportError:  # pragma: no cover - the usual case on 3.12.
    _native = None

MAGIC = 0xFD2FB528

#: Literals and sequences are read from the end of their stream towards its
#: start, most significant bit first. Everything else in the format is plain
#: little-endian bytes.
_LL_BASE = [
    0, 1, 2, 3, 4, 5, 6, 7,
    8, 9, 10, 11, 12, 13, 14, 15,
    16, 18, 20, 22, 24, 28, 32, 40,
    48, 64, 128, 256, 512, 1024, 2048, 4096,
    8192, 16384, 32768, 65536,
]
_LL_BITS = [
    0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0,
    1, 1, 1, 1, 2, 2, 3, 3,
    4, 6, 7, 8, 9, 10, 11, 12,
    13, 14, 15, 16,
]
_ML_BASE = [
    3, 4, 5, 6, 7, 8, 9, 10,
    11, 12, 13, 14, 15, 16, 17, 18,
    19, 20, 21, 22, 23, 24, 25, 26,
    27, 28, 29, 30, 31, 32, 33, 34,
    35, 37, 39, 41, 43, 47, 51, 59,
    67, 83, 99, 131, 259, 515, 1027, 2051,
    4099, 8195, 16387, 32771, 65539,
]
_ML_BITS = [
    0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0,
    1, 1, 1, 1, 2, 2, 3, 3,
    4, 4, 5, 7, 8, 9, 10, 11,
    12, 13, 14, 15, 16,
]
_LL_DEFAULT = [
    4, 3, 2, 2, 2, 2, 2, 2,
    2, 2, 2, 2, 2, 1, 1, 1,
    2, 2, 2, 2, 2, 2, 2, 2,
    2, 3, 2, 1, 1, 1, 1, 1,
    -1, -1, -1, -1,
]
_ML_DEFAULT = [
    1, 4, 3, 2, 2, 2, 2, 2,
    2, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, -1, -1,
    -1, -1, -1, -1, -1,
]
_OF_DEFAULT = [
    1, 1, 1, 1, 1, 1, 2, 2,
    2, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1,
    -1, -1, -1, -1, -1,
]


class ZstdError(Exception):
    """The frame is not one, or is damaged."""


class _Bits:
    """The backward bit reader every compressed part of the format uses.

    A stream's last byte carries a set bit marking where the data ends; reading
    starts just under it and walks towards the start of the buffer, taking the
    most significant bit of each byte first. Bytes are pulled into an integer
    accumulator as they are needed, so a long stream costs no more per read
    than a short one.
    """

    __slots__ = ("data", "index", "acc", "count", "spent")

    def __init__(self, data: bytes) -> None:
        if not data:
            raise ZstdError("an empty bitstream")
        last = data[-1]
        if last == 0:
            raise ZstdError("a bitstream with no end marker")
        self.data = data
        self.index = len(data) - 1
        bits = last.bit_length() - 1
        self.acc = last & ((1 << bits) - 1)
        self.count = bits
        self.spent = 0

    def _fill(self, want: int) -> None:
        while self.count < want and self.index > 0:
            self.index -= 1
            self.acc = (self.acc << 8) | self.data[self.index]
            self.count += 8

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        self._fill(n)
        if self.count >= n:
            left = self.count - n
            value = (self.acc >> left) & ((1 << n) - 1)
            self.acc &= (1 << left) - 1
            self.count = left
        else:
            # Past the end. The format allows a decoder to over-read the last
            # few bits of a stream; they are zeroes by definition.
            value = (self.acc << (n - self.count)) & ((1 << n) - 1)
            self.acc = 0
            self.count = 0
            self.spent += n
        return value

    @property
    def done(self) -> bool:
        return self.index == 0 and self.count == 0


class _Fse:
    """One FSE decoding table: a symbol, a bit count and a base per state."""

    __slots__ = ("log", "symbol", "bits", "base")

    def __init__(self, log: int, symbol: list[int], bits: list[int],
                 base: list[int]) -> None:
        self.log = log
        self.symbol = symbol
        self.bits = bits
        self.base = base

    @classmethod
    def rle(cls, symbol: int) -> "_Fse":
        return cls(0, [symbol], [0], [0])

    @classmethod
    def build(cls, counts: list[int], log: int) -> "_Fse":
        size = 1 << log
        table_symbol = [0] * size
        high = size - 1
        following = [0] * len(counts)

        # A symbol with less than one in a table's worth of probability is
        # parked at the top of the table, where it costs the maximum bits.
        for s, count in enumerate(counts):
            if count == -1:
                table_symbol[high] = s
                high -= 1
                following[s] = 1
            else:
                following[s] = count

        step = (size >> 1) + (size >> 3) + 3
        mask = size - 1
        position = 0
        for s, count in enumerate(counts):
            if count <= 0:
                continue
            for _ in range(count):
                table_symbol[position] = s
                position = (position + step) & mask
                while position > high:
                    position = (position + step) & mask

        symbol = [0] * size
        bits = [0] * size
        base = [0] * size
        for state in range(size):
            s = table_symbol[state]
            symbol[state] = s
            nxt = following[s]
            following[s] = nxt + 1
            nb = log - (nxt.bit_length() - 1)
            bits[state] = nb
            base[state] = ((nxt << nb) - size) & 0xFFFFFFFF
        return cls(log, symbol, bits, base)


def _read_counts(bits: _Bits, max_log: int, max_symbol: int) -> tuple[list[int], int]:
    """The normalized counts an FSE table is built from.

    Read forwards, most significant bit last, which is the one place in the
    format where that happens — so it has its own little reader rather than
    `_Bits`.
    """
    log = bits.read(4) + 5
    if log > max_log:
        raise ZstdError("an accuracy log of %d, above the %d allowed" % (log, max_log))
    remaining = (1 << log) + 1
    threshold = 1 << log
    nb = log + 1
    counts: list[int] = []
    previous_zero = False

    while remaining > 1 and len(counts) <= max_symbol:
        if previous_zero:
            # A run of symbols with no probability at all, in groups of three.
            zeroes = 0
            while True:
                pair = bits.read(2)
                zeroes += pair
                if pair != 3:
                    break
                if len(counts) + zeroes > max_symbol + 1:
                    break
            counts.extend([0] * zeroes)
            previous_zero = False
            continue

        maximum = (2 * threshold - 1) - remaining
        value = bits.read(nb - 1)
        if value < maximum:
            count = value
        else:
            # One more bit, and it is the *top* one: this reader takes the
            # least significant bit first.
            value |= bits.read(1) << (nb - 1)
            if value >= threshold:
                value -= maximum
            count = value
        count -= 1
        remaining -= abs(count)
        counts.append(count)
        previous_zero = count == 0
        while remaining < threshold:
            nb -= 1
            threshold >>= 1

    if remaining != 1:
        raise ZstdError("counts that do not add up")
    while len(counts) <= max_symbol:
        counts.append(0)
    return counts, log


class _Forward:
    """Little-endian bits, least significant first: counts are read this way."""

    __slots__ = ("data", "at")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.at = 0

    def read(self, n: int) -> int:
        value = 0
        for i in range(n):
            byte = self.at >> 3
            bit = (self.data[byte] >> (self.at & 7)) & 1 if byte < len(self.data) else 0
            value |= bit << i
            self.at += 1
        return value

    @property
    def bytes_used(self) -> int:
        return (self.at + 7) >> 3


def _fse_table(data: bytes, max_log: int, max_symbol: int) -> tuple[_Fse, int]:
    reader = _Forward(data)
    counts, log = _read_counts(reader, max_log, max_symbol)
    return _Fse.build(counts, log), reader.bytes_used


class _Huffman:
    """A Huffman table as one lookup of 2**max_bits entries."""

    __slots__ = ("max_bits", "symbol", "length")

    def __init__(self, weights: list[int]) -> None:
        total = sum(1 << (w - 1) for w in weights if w > 0)
        if total == 0:
            raise ZstdError("a Huffman table with no symbols")
        # The table is one bit wider than the weights sent fill, and the gap
        # left over is exactly the last symbol's own weight — which is why it
        # is never sent.
        max_bits = total.bit_length()
        rest = (1 << max_bits) - total
        if rest == 0 or (rest & (rest - 1)) != 0:
            raise ZstdError("Huffman weights that do not fill the table")
        weights = weights + [rest.bit_length()]

        self.max_bits = max_bits
        size = 1 << max_bits
        self.symbol = bytearray(size)
        self.length = bytearray(size)

        # The table is laid out by weight, lightest first — so the longest
        # codes are at the bottom of the table and the shortest at the top,
        # which is the other way round from how one writes canonical Huffman
        # codes out by hand.
        by_weight: dict[int, list[int]] = {}
        for s, w in enumerate(weights):
            if w > 0:
                by_weight.setdefault(w, []).append(s)
        slot = 0
        for w in range(1, max_bits + 1):
            run = 1 << (w - 1)
            nb = max_bits + 1 - w
            for s in by_weight.get(w, ()):
                for u in range(slot, slot + run):
                    self.symbol[u] = s
                    self.length[u] = nb
                slot += run

    def decode(self, bits: _Bits) -> int:
        peek = bits.read(self.max_bits)
        nb = self.length[peek]
        # Only the code's own bits were really consumed; give the rest back.
        bits.acc |= (peek & ((1 << (self.max_bits - nb)) - 1)) << bits.count
        bits.count += self.max_bits - nb
        return self.symbol[peek]


def _huffman_weights(data: bytes) -> tuple[_Huffman, int]:
    header = data[0]
    if header >= 128:
        count = header - 127
        weights = []
        for i in range(count):
            byte = data[1 + (i >> 1)]
            weights.append(byte >> 4 if i % 2 == 0 else byte & 15)
        return _Huffman(weights), 1 + ((count + 1) >> 1)

    # The weights are themselves FSE-coded, by two interleaved states.
    size = header
    body = data[1:1 + size]
    table, used = _fse_table(body, 6, 255)
    stream = _Bits(body[used:])
    state1 = stream.read(table.log)
    state2 = stream.read(table.log)
    weights = []
    # Two states take turns, and how many weights there are is not written
    # down anywhere: the stream simply runs out. The one asking for a bit that
    # is not there is the end, and the other state's symbol is the last weight.
    while True:
        weights.append(table.symbol[state1])
        state1 = (table.base[state1] + stream.read(table.bits[state1])) & 0xFFFFFFFF
        if stream.spent:
            weights.append(table.symbol[state2])
            break
        weights.append(table.symbol[state2])
        state2 = (table.base[state2] + stream.read(table.bits[state2])) & 0xFFFFFFFF
        if stream.spent:
            weights.append(table.symbol[state1])
            break
        if len(weights) > 255:
            raise ZstdError("more Huffman weights than there are symbols")
    return _Huffman(weights), 1 + size


def _literals(data: bytes, previous: _Huffman | None) -> tuple[bytes, int, _Huffman | None]:
    first = data[0]
    kind = first & 3
    size_format = (first >> 2) & 3

    if kind in (0, 1):  # raw, or one byte repeated
        if size_format in (0, 2):
            regenerated = first >> 3
            used = 1
        elif size_format == 1:
            regenerated = (first >> 4) | (data[1] << 4)
            used = 2
        else:
            regenerated = (first >> 4) | (data[1] << 4) | (data[2] << 12)
            used = 3
        if kind == 0:
            return data[used:used + regenerated], used + regenerated, previous
        return bytes([data[used]]) * regenerated, used + 1, previous

    if size_format == 0:
        regenerated = (first >> 4) | ((data[1] & 0x3F) << 4)
        compressed = (data[1] >> 6) | (data[2] << 2)
        streams, used = 1, 3
    elif size_format == 1:
        regenerated = (first >> 4) | ((data[1] & 0x3F) << 4)
        compressed = (data[1] >> 6) | (data[2] << 2)
        streams, used = 4, 3
    elif size_format == 2:
        regenerated = (first >> 4) | (data[1] << 4) | ((data[2] & 3) << 12)
        compressed = (data[2] >> 2) | (data[3] << 6)
        streams, used = 4, 4
    else:
        regenerated = (first >> 4) | (data[1] << 4) | ((data[2] & 0x3F) << 12)
        compressed = (data[2] >> 6) | (data[3] << 2) | (data[4] << 10)
        streams, used = 4, 5

    body = data[used:used + compressed]
    if kind == 2:
        table, spent = _huffman_weights(body)
        body = body[spent:]
    else:  # treeless: the table came with an earlier block
        if previous is None:
            raise ZstdError("literals pointing at a Huffman table that is not there")
        table = previous

    out = bytearray()
    if streams == 1:
        _huffman_stream(table, body, regenerated, out)
    else:
        sizes = struct.unpack_from("<3H", body, 0)
        offsets = [6, 6 + sizes[0], 6 + sizes[0] + sizes[1],
                   6 + sizes[0] + sizes[1] + sizes[2]]
        each = (regenerated + 3) >> 2
        wanted = [each, each, each, regenerated - 3 * each]
        for i in range(4):
            end = offsets[i + 1] if i < 3 else len(body)
            _huffman_stream(table, body[offsets[i]:end], wanted[i], out)
    if len(out) != regenerated:
        raise ZstdError("literals of %d bytes where %d were promised"
                        % (len(out), regenerated))
    return bytes(out), used + compressed, table


def _huffman_stream(table: _Huffman, data: bytes, count: int, out: bytearray) -> None:
    if count == 0:
        return
    bits = _Bits(data)
    for _ in range(count):
        out.append(table.decode(bits))


def _sequence_tables(data: bytes, previous: dict) -> tuple[dict, int]:
    modes = data[0]
    at = 1
    tables = {}
    for name, shift, max_log, max_symbol, default, default_log in (
        ("ll", 6, 9, 35, _LL_DEFAULT, 6),
        ("of", 4, 8, 28, _OF_DEFAULT, 5),
        ("ml", 2, 9, 52, _ML_DEFAULT, 6),
    ):
        mode = (modes >> shift) & 3
        if mode == 0:
            tables[name] = _Fse.build(list(default), default_log)
        elif mode == 1:
            tables[name] = _Fse.rle(data[at])
            at += 1
        elif mode == 2:
            table, used = _fse_table(data[at:], max_log, max_symbol)
            tables[name] = table
            at += used
        else:
            if name not in previous:
                raise ZstdError("a repeated %s table that was never sent" % name)
            tables[name] = previous[name]
    return tables, at


def _block(data: bytes, out: bytearray, state: dict) -> None:
    literals, used, state["huffman"] = _literals(data, state.get("huffman"))
    rest = data[used:]

    if not rest:
        out += literals
        return
    first = rest[0]
    if first == 0:
        out += literals
        return
    if first < 128:
        count = first
        at = 1
    elif first < 255:
        count = ((first - 128) << 8) + rest[1]
        at = 2
    else:
        count = rest[1] + (rest[2] << 8) + 0x7F00
        at = 3

    tables, used = _sequence_tables(rest[at:], state.setdefault("tables", {}))
    state["tables"] = tables
    bits = _Bits(rest[at + used:])

    ll, of, ml = tables["ll"], tables["of"], tables["ml"]
    ll_state = bits.read(ll.log)
    of_state = bits.read(of.log)
    ml_state = bits.read(ml.log)
    repeats = state["repeats"]
    at_literal = 0

    for i in range(count):
        ll_code = ll.symbol[ll_state]
        ml_code = ml.symbol[ml_state]
        of_code = of.symbol[of_state]

        # The three values are read in this order, whatever order their states
        # were initialized in.
        # Code zero is an offset value of one, not of nothing: the value is
        # always two to the code, and the code's own bits are added to it.
        offset_value = (1 << of_code) + bits.read(of_code)
        match = _ML_BASE[ml_code] + bits.read(_ML_BITS[ml_code])
        literal = _LL_BASE[ll_code] + bits.read(_LL_BITS[ll_code])

        if offset_value > 3:
            offset = offset_value - 3
            repeats[2] = repeats[1]
            repeats[1] = repeats[0]
            repeats[0] = offset
        else:
            which = offset_value + (1 if literal == 0 else 0)
            if which == 1:
                offset = repeats[0]
            elif which == 2:
                offset = repeats[1]
                repeats[1] = repeats[0]
                repeats[0] = offset
            elif which == 3:
                offset = repeats[2]
                repeats[2] = repeats[1]
                repeats[1] = repeats[0]
                repeats[0] = offset
            else:
                offset = repeats[0] - 1
                if offset < 1:
                    raise ZstdError("a repeated offset of nothing")
                repeats[2] = repeats[1]
                repeats[1] = repeats[0]
                repeats[0] = offset

        if literal:
            out += literals[at_literal:at_literal + literal]
            at_literal += literal
        if offset > len(out):
            raise ZstdError("a match reaching %d bytes back, before the start"
                            % offset)
        start = len(out) - offset
        if offset >= match:
            out += out[start:start + match]
        else:
            # Overlapping copy: the bytes being written are also being read.
            for j in range(match):
                out.append(out[start + j])

        if i + 1 < count:
            ll_state = (ll.base[ll_state] + bits.read(ll.bits[ll_state])) & 0xFFFFFFFF
            ml_state = (ml.base[ml_state] + bits.read(ml.bits[ml_state])) & 0xFFFFFFFF
            of_state = (of.base[of_state] + bits.read(of.bits[of_state])) & 0xFFFFFFFF

    out += literals[at_literal:]


def decompress(data: bytes, progress=None) -> bytes:
    """One zstd frame, or several in a row, as bytes.

    ``progress`` is called with a fraction between 0 and 1 after each block,
    where the frame says how much it will come to — which a Figma file's does.
    Reading one is seconds of work on a big board, and a bar that does not
    move for seconds is a bar that says nothing.
    """
    if _native is not None:
        return _native(data)

    out = bytearray()
    at = 0
    end = len(data)
    while at < end:
        (magic,) = struct.unpack_from("<I", data, at)
        if magic != MAGIC:
            if 0x184D2A50 <= magic <= 0x184D2A5F:  # a frame to be skipped
                (size,) = struct.unpack_from("<I", data, at + 4)
                at += 8 + size
                continue
            raise ZstdError("not a zstd frame: %08x" % magic)
        at = _frame(data, at + 4, out, progress)
    return bytes(out)


def _frame(data: bytes, at: int, out: bytearray, progress=None) -> int:
    descriptor = data[at]
    at += 1
    content_size_flag = descriptor >> 6
    single_segment = (descriptor >> 5) & 1
    checksum = (descriptor >> 2) & 1
    dictionary_flag = descriptor & 3

    if not single_segment:
        at += 1  # the window descriptor; the whole output is kept anyway
    at += (0, 1, 2, 4)[dictionary_flag]
    sizes = (1 if single_segment else 0, 2, 4, 8)
    width = sizes[content_size_flag]
    promised = 0
    if width == 1:
        promised = data[at]
    elif width == 2:
        # The two-byte form leaves out the first 256, which no frame can be.
        promised = struct.unpack_from("<H", data, at)[0] + 256
    elif width == 4:
        promised = struct.unpack_from("<I", data, at)[0]
    elif width == 8:
        promised = struct.unpack_from("<Q", data, at)[0]
    started = len(out)
    at += width

    state = {"repeats": [1, 4, 8], "huffman": None, "tables": {}}
    while True:
        header = data[at] | (data[at + 1] << 8) | (data[at + 2] << 16)
        at += 3
        last = header & 1
        kind = (header >> 1) & 3
        size = header >> 3
        if kind == 0:
            out += data[at:at + size]
            at += size
        elif kind == 1:
            out += bytes([data[at]]) * size
            at += 1
        elif kind == 2:
            _block(data[at:at + size], out, state)
            at += size
        else:
            raise ZstdError("a block of the reserved kind")
        if progress is not None and promised:
            progress(min(1.0, (len(out) - started) / float(promised)))
        if last:
            break
    if checksum:
        at += 4
    return at
