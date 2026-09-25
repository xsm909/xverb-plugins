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

"""Chunks decompressed on every core but one.

**Why processes.** An EXR is chunks that know nothing of each other, and
decompressing one is Python arithmetic that holds the interpreter's lock; a
thread would wait its turn. A process does not. A 4K HDRI in PIZ was 26
seconds on one core.

**Spawned, not forked.** The plugin runs threads — the SDK reads the pipe on
one — and a fork of a process with threads may copy a lock somebody was
holding. Spawn starts clean, costs a fraction of a second once, and the pool
is kept for the next file, so walking a folder of renders pays it once.

**Nothing may reach stdout.** The children inherit the plugin's stdout, which
is the protocol pipe to the host; each worker points it at stderr before it
does anything. On Windows they run `pythonw.exe`, so no console window opens
for a process started from one that has none.

If the pool cannot be started, or breaks, the caller is told `None` and does
the work itself, as it did before this existed.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from typing import List, Optional

import exr

_executor: Optional[ProcessPoolExecutor] = None
_workers = 0


def workers() -> int:
    """Every core but one — the one the host is drawing with — and at most
    eight, past which the pipe to each worker is the cost, not the arithmetic."""
    return max(1, min(8, (os.cpu_count() or 1) - 1))


def _quiet() -> None:
    sys.stdout = sys.stderr
    try:
        os.dup2(2, 1)
    except OSError:
        pass


def _batch(spec, jobs):
    return [exr.decode_job(spec, job) for job in jobs]


def _start() -> Optional[ProcessPoolExecutor]:
    global _executor, _workers
    if _executor is not None:
        return _executor
    count = workers()
    if count < 2:
        return None
    context = multiprocessing.get_context("spawn")
    if os.name == "nt":
        quiet = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.isfile(quiet):
            context.set_executable(quiet)
    try:
        _executor = ProcessPoolExecutor(max_workers=count, mp_context=context,
                                        initializer=_quiet)
        _workers = count
    except (OSError, ValueError):
        _executor = None
    return _executor


def decode(spec, jobs: List[tuple]) -> Optional[List[Optional[bytes]]]:
    """Every job decompressed, in order, or None when there is no pool.

    The jobs are cut into about four batches a worker, so a worker that drew
    the busy part of a picture — a sky of noise over a flat ground — is not
    what everything waits for.
    """
    global _executor
    executor = _start()
    if executor is None:
        return None
    size = max(1, len(jobs) // (_workers * 4))
    batches = [jobs[i:i + size] for i in range(0, len(jobs), size)]
    try:
        results = executor.map(_batch, [spec] * len(batches), batches)
        return [block for batch in results for block in batch]
    except Exception:  # noqa: BLE001 - a broken pool means doing it here
        shutdown()
        return None


def shutdown() -> None:
    global _executor
    executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
