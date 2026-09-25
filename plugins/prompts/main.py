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

"""How a generated picture was made: the prompt, the seed, the model.

**A picture made by Stable Diffusion carries its own recipe** — every popular
tool writes the prompt and the settings into the file it saves, and people
keep, share and study those files for exactly that. What it lacks is a way to
read it: the text is in a PNG chunk, an EXIF comment or an XMP packet, in one
of half a dozen shapes, and ComfyUI's is a graph of forty nodes.

**Shift+F3, not F3.** F3 on a picture is the picture; that is what the file
is. This viewer claims the same extensions at a low priority and never
probes, so it is one Shift+F3 away on every PNG, JPEG and WebP and in the way
of none. The graph of a ComfyUI picture is the Node graph plugin's; this is
the page that says what the graph *did*.

`meta.py` finds the text, `readers.py` recognises whose it is, and this file
lays it out as a page.
"""

from __future__ import annotations

import os
import sys
import time
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xverb import Plugin, error, markdown  # noqa: E402

import meta  # noqa: E402
import readers  # noqa: E402

plugin = Plugin("org.xverb.prompts", "AI prompts")

#: The whole file is read: a WebP keeps its EXIF after the pixels, and a PNG
#: may put a text chunk after them too. Generated pictures are a few
#: megabytes; the cap is for the one that is not.
MAX_BYTES = 96 << 20

EXTENSIONS = ["png", "jpg", "jpeg", "jpe", "jfif", "webp"]


def _fence(text: str) -> str:
    """Text in a code block, fenced by more backticks than it holds, so a
    prompt is shown exactly as written — brackets, underscores, weights —
    and copies out the same."""
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return "%s\n%s\n%s" % (fence, text, fence)


def _cell(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _table(rows: List[Tuple[str, str]], head: Tuple[str, str]) -> str:
    lines = ["| %s | %s |" % head, "| --- | --- |"]
    lines += ["| %s | %s |" % (_cell(k), _cell(v)) for k, v in rows]
    return "\n".join(lines)


def page(found: readers.Generation, kind: str) -> str:
    tr = plugin.tr
    parts = ["# %s" % tr("Made with {tool}", {"tool": found.tool})]
    if found.prompt:
        parts += ["## " + tr("Prompt"), _fence(found.prompt)]
    else:
        parts.append(tr("The picture names no prompt."))
    if found.negative:
        parts += ["## " + tr("Negative prompt"), _fence(found.negative)]
    if found.settings:
        parts += ["## " + tr("Settings"), _table(found.settings, (tr("Setting"), tr("Value")))]
    model_rows = list(found.model) + [(tr("LoRA"), "%s — %s" % (n, w) if w else n)
                                      for n, w in found.loras]
    if model_rows:
        parts += ["## " + tr("Model"), _table(model_rows, (tr("What"), tr("Name")))]

    for number, extra in enumerate(found.passes, 2):
        parts.append("## " + tr("Pass {number}", {"number": number}))
        if extra.prompt and extra.prompt != found.prompt:
            parts += ["**%s**" % tr("Prompt"), _fence(extra.prompt)]
        if extra.negative and extra.negative != found.negative:
            parts += ["**%s**" % tr("Negative prompt"), _fence(extra.negative)]
        rows = list(extra.settings) + list(extra.model) + [
            (tr("LoRA"), "%s — %s" % (n, w) if w else n) for n, w in extra.loras]
        if rows:
            parts.append(_table(rows, (tr("Setting"), tr("Value"))))

    if found.other:
        parts += ["## " + tr("Everything else"), _table(found.other, (tr("Key"), tr("Value")))]
    for note in found.notes:
        parts.append("> " + tr(note))
    if found.raw:
        parts += ["## " + tr("As written in the file"),
                  tr("{where}, in the {kind}.", {"where": found.raw_label, "kind": kind}),
                  _fence(found.raw)]
    return "\n\n".join(parts) + "\n"


def nothing(carried: meta.Found) -> dict:
    """No recipe — but whatever text the picture does carry is still shown,
    because "nothing here" about a file with a paragraph in it is a lie."""
    rows = [("PNG " + k, v) for k, v in carried.text.items()]
    rows += [("EXIF " + k, v) for k, v in carried.exif.items()]
    rows += [("XMP " + k, v) for k, v in carried.xmp.items()]
    rows += [("Comment", c) for c in carried.comments]
    if not rows:
        return error(plugin.tr(
            "This picture says nothing about how it was made. Stable Diffusion "
            "tools write their prompt into the file they save; a picture from "
            "anywhere else, or one that was re-saved, has none."))
    parts = ["# " + plugin.tr("No prompt this reader recognises"),
             plugin.tr("The picture carries this text, in no shape a generator this "
                       "plugin knows writes:")]
    for label, value in rows:
        parts += ["## " + label, _fence(value)]
    return markdown("\n\n".join(parts) + "\n")


def answer(raw: bytes) -> dict:
    """The page for one file, given its bytes — separate from the viewer so
    the self-test can call it without a host."""
    if not raw:
        return error(plugin.tr("The file is empty."))
    carried = meta.read(raw)
    if not carried.kind:
        return error(plugin.tr("This is not a PNG, a JPEG or a WebP."))
    found = readers.read(carried)
    if found is None:
        return nothing(carried)
    return markdown(page(found, carried.kind))


@plugin.viewer(
    "prompts.read",
    "Prompt",
    extensions=EXTENSIONS,
    priority=1,
    produces="prompt",
)
def prompt(url: str) -> dict:
    started = time.time()
    try:
        raw = plugin.read_file(url, max_bytes=MAX_BYTES)
    except Exception as failure:  # noqa: BLE001
        return error(plugin.tr("The file could not be read: {error}", {"error": failure}))
    try:
        content = answer(raw)
    except Exception as failure:  # noqa: BLE001 - a damaged record is a sentence
        content = error(plugin.tr("The picture's record could not be read: {error}",
                                  {"error": failure}))
    plugin.log("%s: %s, %.2fs" % (url.rsplit("/", 1)[-1], content.get("kind"),
                                  time.time() - started))
    return content


if __name__ == "__main__":
    plugin.run()
