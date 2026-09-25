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

"""The prompts plugin, checked on pictures built here in each tool's shape.

**What the fixtures are and are not.** The ComfyUI API prompt in `fixtures/`
is a real export — the same file the Node graph plugin is tested on. Every
other record is written here in the shape its tool documents and writes —
AUTOMATIC1111's parameter line, NovelAI's `Comment`, InvokeAI's
`invokeai_metadata`, Midjourney's description, Draw Things' XMP — and put
into a PNG, a JPEG or a WebP by the same byte layouts those tools use. They
are not pictures those tools saved. Real ones can be checked by naming them:

    PYTHONPATH=<xverb>/assets/python python3 selftest.py [pictures…]
"""

from __future__ import annotations

import json
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import main  # noqa: E402
import meta  # noqa: E402
import readers  # noqa: E402

failures = []


def check(what: str, ok: bool, detail: object = "") -> None:
    print("%s  %s%s" % ("ok  " if ok else "FAIL", what, ("  — %s" % (detail,)) if detail else ""))
    if not ok:
        failures.append(what)


# -- containers, byte for byte ----------------------------------------------


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(text=(), itxt=(), exif: bytes = b"") -> bytes:
    body = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    for key, value in text:
        body += _chunk(b"tEXt", key.encode("latin-1") + b"\0" + value.encode("utf-8"))
    for key, value in itxt:
        body += _chunk(b"iTXt", key.encode() + b"\0\x01\0\0\0" + zlib.compress(value.encode("utf-8")))
    if exif:
        body += _chunk(b"eXIf", exif)
    body += _chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
    return body + _chunk(b"IEND", b"")


def tiff(entries0, exif_entries, order="<") -> bytes:
    """A TIFF header, IFD0 and an Exif IFD: [(tag, type, bytes)]."""
    def ifd(entries, start):
        blob = struct.pack(order + "H", len(entries))
        extra = b""
        data_at = start + 2 + 12 * len(entries) + 4
        for tag, kind, value in entries:
            if len(value) <= 4:
                blob += struct.pack(order + "HHI", tag, kind, len(value)) + value.ljust(4, b"\0")
            else:
                blob += struct.pack(order + "HHII", tag, kind, len(value), data_at + len(extra))
                extra += value + (b"\0" if len(value) & 1 else b"")
        return blob + b"\0\0\0\0" + extra

    head = (b"II*\0" if order == "<" else b"MM\0*") + struct.pack(order + "I", 8)
    first_len = len(ifd(entries0 + [(0x8769, 4, b"\0\0\0\0")], 8))
    exif_at = 8 + first_len
    pointer = struct.pack(order + "I", exif_at)
    first = ifd(entries0 + [(0x8769, 4, pointer)], 8)
    return head + first + ifd(exif_entries, exif_at)


def user_comment(text: str, codec: str) -> bytes:
    return b"UNICODE\0" + text.encode(codec)


def jpeg(exif: bytes = b"", comment: str = "") -> bytes:
    body = b"\xff\xd8"
    if exif:
        payload = b"Exif\0\0" + exif
        body += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    if comment:
        payload = comment.encode()
        body += b"\xff\xfe" + struct.pack(">H", len(payload) + 2) + payload
    return body + b"\xff\xda\0\x02" + b"\x00" * 16 + b"\xff\xd9"


def webp(exif: bytes = b"") -> bytes:
    chunks = b"VP8L" + struct.pack("<I", 5) + b"\x2f\0\0\0\0" + b"\0"
    if exif:
        chunks += b"EXIF" + struct.pack("<I", len(exif)) + exif + (b"\0" if len(exif) & 1 else b"")
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WEBP" + chunks


# -- the records -----------------------------------------------------------

A1111 = (
    "masterpiece, best quality, a cat in a spacesuit, <lora:spacecat_v2:0.8>, (glowing eyes:1.2)\n"
    "Negative prompt: lowres, bad anatomy, watermark\n"
    "Steps: 28, Sampler: DPM++ 2M, Schedule type: Karras, CFG scale: 6.5, Seed: 1234567890, "
    "Size: 832x1216, Model hash: 31e35c80fc, Model: sd_xl_base_1.0, VAE hash: 235745af8d, "
    "VAE: sdxl_vae.safetensors, Denoising strength: 0.4, Clip skip: 2, Hires upscale: 1.5, "
    "Hires steps: 12, Hires upscaler: 4x-UltraSharp, "
    'Lora hashes: "spacecat_v2: 7ab3c2d1e0f4, other_lora: 99aa", '
    'TI hashes: "easynegative: c74b4e810b03", ADetailer model: face_yolov8n.pt, Version: v1.10.1'
)

FLUX = {
    "10": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux1-dev.safetensors", "weight_dtype": "default"}},
    "11": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": "t5xxl_fp16.safetensors", "clip_name2": "clip_l.safetensors", "type": "flux"}},
    "12": {"class_type": "LoraLoaderModelOnly", "inputs": {"lora_name": "ink_style.safetensors", "strength_model": 0.75, "model": ["10", 0]}},
    "20": {"class_type": "CLIPTextEncode", "inputs": {"text": "an ink drawing of a heron at dawn", "clip": ["11", 0]}},
    "21": {"class_type": "FluxGuidance", "inputs": {"guidance": 3.5, "conditioning": ["20", 0]}},
    "25": {"class_type": "RandomNoise", "inputs": {"noise_seed": 424242}},
    "26": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
    "27": {"class_type": "BasicScheduler", "inputs": {"scheduler": "simple", "steps": 24, "denoise": 1.0, "model": ["12", 0]}},
    "28": {"class_type": "BasicGuider", "inputs": {"model": ["12", 0], "conditioning": ["21", 0]}},
    "30": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 768, "batch_size": 1}},
    "31": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["25", 0], "guider": ["28", 0], "sampler": ["26", 0], "sigmas": ["27", 0], "latent_image": ["30", 0]}},
    "40": {"class_type": "LatentUpscaleBy", "inputs": {"upscale_method": "nearest-exact", "scale_by": 1.5, "samples": ["31", 0]}},
    "41": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["11", 0]}},
    "42": {"class_type": "KSampler", "inputs": {"seed": 7, "steps": 10, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 0.45, "model": ["12", 0], "positive": ["21", 0], "negative": ["41", 0], "latent_image": ["40", 0]}},
}

NOVELAI_COMMENT = {
    "prompt": "1girl, silver hair, library, soft light", "steps": 28, "height": 1216, "width": 832,
    "scale": 5.0, "uncond_scale": 1.0, "cfg_rescale": 0.0, "seed": 3141592653,
    "n_samples": 1, "noise_schedule": "karras", "sampler": "k_euler_ancestral",
    "uc": "lowres, jpeg artifacts", "strength": 0.7, "noise": 0.0,
}

INVOKE = {
    "generation_mode": "txt2img", "positive_prompt": "a watercolor fox in snow",
    "negative_prompt": "photo, 3d", "width": 1024, "height": 1024, "seed": 99,
    "steps": 30, "cfg_scale": 7.5, "scheduler": "dpmpp_2m_k",
    "model": {"key": "abc", "hash": "blake3:00", "name": "Juggernaut XL v9", "base": "sdxl", "type": "main"},
    "loras": [{"model": {"key": "l1", "name": "watercolor_xl"}, "weight": 0.6}],
    "app_version": "5.4.2",
}

XMP_DRAW_THINGS = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/"><exif:UserComment><rdf:Alt>'
    '<rdf:li xml:lang="x-default">{&quot;c&quot;:&quot;a lighthouse, gouache&quot;,'
    '&quot;uc&quot;:&quot;blurry&quot;,&quot;seed&quot;:5,&quot;steps&quot;:20,'
    '&quot;scale&quot;:4.5,&quot;sampler&quot;:&quot;DPM++ 2M Karras&quot;,'
    '&quot;model&quot;:&quot;sd_xl_base_1.0_f16.ckpt&quot;,&quot;size&quot;:&quot;1024x1024&quot;}'
    '</rdf:li></rdf:Alt></exif:UserComment></rdf:Description></rdf:RDF></x:xmpmeta>'
)


def settings_of(found) -> dict:
    return dict(found.settings)


def run() -> None:
    # AUTOMATIC1111 in a PNG.
    found = readers.read(meta.read(png(text=[("parameters", A1111)])))
    s = settings_of(found)
    check("A1111: the prompt and the negative are told apart",
          found.prompt.startswith("masterpiece") and found.negative == "lowres, bad anatomy, watermark")
    check("A1111: seed, steps, sampler, scheduler, CFG, size",
          (s.get("Seed"), s.get("Steps"), s.get("Sampler"), s.get("Scheduler"), s.get("CFG scale"), s.get("Size"))
          == ("1234567890", "28", "DPM++ 2M", "Karras", "6.5", "832x1216"), s)
    check("A1111: the checkpoint and the VAE", dict(found.model).get("Checkpoint") == "sd_xl_base_1.0"
          and dict(found.model).get("VAE") == "sdxl_vae.safetensors", found.model)
    check("A1111: a LoRA from the prompt with its weight, and one only the hashes name",
          found.loras == [("spacecat_v2", "0.8"), ("other_lora", "?")], found.loras)
    check("A1111: a quoted value with commas inside is one value",
          dict(found.other).get("TI hashes") == "easynegative: c74b4e810b03", found.other)
    check("A1111: the version says which tool", found.tool == "AUTOMATIC1111")

    forge = A1111.replace("Version: v1.10.1", "Version: f2.0.1v1.10.1-previous-313-g4b4b4c8a")
    # In a JPEG, as piexif writes it: UTF-16 big-endian after UNICODE.
    exif = tiff([], [(0x9286, 7, user_comment(forge, "utf-16-be"))])
    found = readers.read(meta.read(jpeg(exif)))
    check("A JPEG's UserComment, big-endian", found is not None and found.tool == "Stable Diffusion WebUI Forge"
          and settings_of(found).get("Seed") == "1234567890")
    exif = tiff([], [(0x9286, 7, user_comment(A1111, "utf-16-le"))])
    found = readers.read(meta.read(webp(b"Exif\0\0" + exif)))
    check("A WebP's UserComment, little-endian", found is not None and found.prompt.startswith("masterpiece"))
    found = readers.read(meta.read(jpeg(tiff([], [(0x9286, 7, user_comment(A1111, "utf-16-be"))], order=">"))))
    check("A big-endian TIFF around it", found is not None and found.negative.startswith("lowres"))

    # ComfyUI, the real export.
    with open(os.path.join(HERE, "fixtures", "comfyui-api-prompt.json")) as f:
        api = f.read()
    with open(os.path.join(HERE, "fixtures", "comfyui-workflow.json")) as f:
        workflow = f.read()
    found = readers.read(meta.read(png(text=[("prompt", api), ("workflow", workflow)])))
    s = settings_of(found)
    check("ComfyUI: the prompt and the negative, followed from the sampler",
          found.prompt == "a lighthouse in a storm, oil painting" and found.negative == "blurry, watermark",
          (found.prompt, found.negative))
    check("ComfyUI: seed, steps, CFG, sampler and the latent's size",
          (s.get("Seed"), s.get("Steps"), s.get("CFG"), s.get("Sampler"), s.get("Size"))
          == ("812734", "20", "8", "euler", "1024×1024"), s)
    check("ComfyUI: the checkpoint", found.model == [("Checkpoint", "sd_xl_base.safetensors")], found.model)
    check("ComfyUI: says the graph is there too", any("Node graph" in n for n in found.notes))

    # ComfyUI, Flux with the custom sampler and a second pass.
    found = readers.read(meta.read(png(text=[("prompt", json.dumps(FLUX))])))
    s = settings_of(found)
    check("Flux: the prompt through FluxGuidance and BasicGuider",
          found.prompt == "an ink drawing of a heron at dawn", found.prompt)
    check("Flux: noise seed, sampler, scheduler, steps, guidance, size",
          (s.get("Seed"), s.get("Sampler"), s.get("Scheduler"), s.get("Steps"), s.get("Guidance"), s.get("Size"))
          == ("424242", "euler", "simple", "24", "3.5", "1024×768"), s)
    check("Flux: the diffusion model, and the LoRA on the way to it",
          found.model == [("Diffusion model", "flux1-dev.safetensors")]
          and found.loras == [("ink_style.safetensors", "0.75")], (found.model, found.loras))
    check("Flux: the second pass is its own, with its denoise",
          len(found.passes) == 1 and settings_of(found.passes[0]).get("Denoise") == "0.45",
          [p.settings for p in found.passes])

    # ComfyUI in a WebP: prompt:{…} in EXIF Model, workflow:{…} in Make.
    exif = tiff([(0x010F, 2, b"workflow:" + workflow.encode() + b"\0"),
                 (0x0110, 2, b"prompt:" + api.encode() + b"\0")], [])
    found = readers.read(meta.read(webp(b"Exif\0\0" + exif)))
    check("ComfyUI in a WebP's EXIF", found is not None and found.tool == "ComfyUI"
          and found.prompt.startswith("a lighthouse"))

    # NovelAI.
    found = readers.read(meta.read(png(text=[
        ("Title", "NovelAI generated image"), ("Description", NOVELAI_COMMENT["prompt"]),
        ("Software", "NovelAI"), ("Source", "NovelAI Diffusion V4.5"),
        ("Comment", json.dumps(NOVELAI_COMMENT))])))
    s = settings_of(found)
    check("NovelAI: prompt, undesired content, sampler and size",
          found.tool == "NovelAI" and found.negative == "lowres, jpeg artifacts"
          and s.get("Sampler") == "k_euler_ancestral" and s.get("Size") == "832×1216", s)

    # InvokeAI.
    found = readers.read(meta.read(png(text=[("invokeai_metadata", json.dumps(INVOKE))])))
    check("InvokeAI: prompt, model and LoRA",
          found.tool == "InvokeAI" and found.prompt == "a watercolor fox in snow"
          and found.model == [("Model", "Juggernaut XL v9")] and found.loras == [("watercolor_xl", "0.6")],
          (found.model, found.loras))

    # Draw Things: XMP in a PNG's iTXt.
    found = readers.read(meta.read(png(itxt=[("XML:com.adobe.xmp", XMP_DRAW_THINGS)])))
    check("Draw Things: JSON in XMP's UserComment",
          found is not None and found.tool == "Draw Things" and found.prompt == "a lighthouse, gouache"
          and settings_of(found).get("Seed") == "5", found and found.settings)

    # Midjourney.
    found = readers.read(meta.read(png(text=[(
        "Description", "a koi pond in autumn, ukiyo-e --ar 3:2 --v 6.1 --s 250 --no people "
        "Job ID: 1b2c3d4e-0000-4000-8000-123456789abc")])))
    s = settings_of(found)
    check("Midjourney: the prompt and its flags",
          found.prompt == "a koi pond in autumn, ukiyo-e" and s.get("Aspect ratio") == "3:2"
          and s.get("Version") == "6.1" and found.negative == "people", s)

    # Fooocus / SwarmUI as JSON in `parameters`.
    swarm = {"sui_image_params": {"prompt": "a red bicycle", "negativeprompt": "text", "model": "juggernaut",
                                  "seed": 11, "steps": 20, "cfgscale": 7, "width": 896, "height": 1152}}
    found = readers.read(meta.read(png(text=[("parameters", json.dumps(swarm))])))
    check("SwarmUI: its parameters object",
          found.tool == "SwarmUI" and found.prompt == "a red bicycle"
          and settings_of(found).get("Size") == "896×1152", found.settings)

    # Words that are not Latin, written as UTF-8 into tEXt as every tool does.
    found = readers.read(meta.read(png(text=[("parameters", A1111.replace("a cat", "кот и 猫"))])))
    check("A prompt in Cyrillic and Japanese reads back", "кот и 猫" in found.prompt)

    # The page.
    content = main.answer(png(text=[("parameters", A1111)]))
    body = content.get("text", "")
    check("The page is Markdown with the prompt fenced",
          content.get("kind") == "markdown" and "```\nmasterpiece" in body and "## Settings" in body)
    check("A table cell with a | in it stays one cell", main._cell("a|b") == "a\\|b")
    check("A prompt with backticks gets a longer fence", main._fence("x ``` y").startswith("````"))

    # Nothing to find.
    content = main.answer(png())
    check("A plain PNG says it carries nothing", content.get("kind") == "error")
    content = main.answer(png(text=[("Software", "GIMP 2.10")]))
    check("A PNG with other text shows that text rather than nothing",
          content.get("kind") == "markdown" and "GIMP 2.10" in content.get("text", ""))


def run_files(paths) -> None:
    for path in paths:
        with open(path, "rb") as f:
            raw = f.read()
        carried = meta.read(raw)
        found = readers.read(carried)
        if found is None:
            print("      %s: nothing recognised (%s)" % (os.path.basename(path),
                  ", ".join(list(carried.text) + list(carried.exif) + list(carried.xmp)) or "no text"))
            continue
        print("      %s: %s — %s" % (os.path.basename(path), found.tool,
                                      (found.prompt[:70] + "…") if len(found.prompt) > 70 else found.prompt))
        print("        %s" % dict(found.settings))


if __name__ == "__main__":
    run()
    run_files(sys.argv[1:])
    print("\n%s" % ("all passed" if not failures else "%d failed: %s" % (len(failures), failures)))
    sys.exit(1 if failures else 0)
