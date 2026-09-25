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

"""Who made the picture, and what they asked for.

Each generator's record is a different shape, and each reader here turns one
into the same answer: the prompt, the negative prompt, the settings a person
would type in again to get the picture back, the model and the LoRAs.

**The settings are ordered for a person, not by the file.** Seed, steps,
sampler, CFG and size come first because those are what is copied from one
picture into the next; the rest is under them, in the order the file gave.

**Nothing is guessed.** A key this module has no name for is still shown, as
the file spelled it. The text as it was written into the file goes at the end
as well, for the one thing a summary is never good for: pasting back into the
tool that wrote it.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

from meta import Found as Carried


class Generation:
    """What one reader made of a picture."""

    def __init__(self, tool: str):
        self.tool = tool
        self.prompt = ""
        self.negative = ""
        self.settings: List[Tuple[str, str]] = []
        self.model: List[Tuple[str, str]] = []
        self.loras: List[Tuple[str, str]] = []
        self.other: List[Tuple[str, str]] = []
        self.passes: List["Generation"] = []
        self.raw: Optional[str] = None
        self.raw_label = ""
        self.notes: List[str] = []

    def set(self, label: str, value) -> None:
        text = _scalar(value)
        if text != "":
            self.settings.append((label, text))

    def add_model(self, label: str, value) -> None:
        text = _scalar(value)
        if text != "":
            self.model.append((label, text))


def _scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return ("%.4f" % value).rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _json(text: str):
    text = (text or "").strip()
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


_LORA_TAG = re.compile(r"<(lora|lyco|hypernet):([^:>]+)(?::([^:>]+))?[^>]*>", re.I)


def _loras_in(prompt: str) -> List[Tuple[str, str]]:
    """`<lora:name:0.8>` in an AUTOMATIC1111 prompt is how a LoRA is asked for."""
    return [(name.strip(), (weight or "1").strip()) for _kind, name, weight in _LORA_TAG.findall(prompt)]


# -- AUTOMATIC1111, Forge, SD.Next and everything that copied them ------------

#: The pattern AUTOMATIC1111 parses its own line with: a key, a colon, and a
#: value that is either quoted — with commas inside — or runs to the next comma.
_PARAM = re.compile(r'\s*(\w[\w \-/+.()]*?):\s*("(?:\\.|[^\\"])*"|[^,]*)(?:,|$)')

#: Keys that go in Settings, in this order, and what they are called there.
A1111_SETTINGS = [
    ("Seed", "Seed"), ("Steps", "Steps"), ("Sampler", "Sampler"),
    ("Schedule type", "Scheduler"), ("CFG scale", "CFG scale"),
    ("Distilled CFG Scale", "Distilled CFG"), ("Size", "Size"),
    ("Denoising strength", "Denoising strength"), ("Clip skip", "Clip skip"),
    ("Hires upscale", "Hires upscale"), ("Hires resize", "Hires resize"),
    ("Hires steps", "Hires steps"), ("Hires upscaler", "Hires upscaler"),
    ("Variation seed", "Variation seed"),
    ("Variation seed strength", "Variation strength"),
]
A1111_MODEL = [
    ("Model", "Checkpoint"), ("Model hash", "Checkpoint hash"),
    ("VAE", "VAE"), ("VAE hash", "VAE hash"), ("Module 1", "Module"),
    ("Module 2", "Module"), ("Module 3", "Module"),
]


def looks_like_a1111(text: str) -> bool:
    last = text.strip().rsplit("\n", 1)[-1]
    return "Steps: " in last and ("Sampler: " in last or "Seed: " in last)


def a1111(text: str, where: str) -> Generation:
    lines = text.strip().split("\n")
    params_line = ""
    if lines and looks_like_a1111(lines[-1]):
        params_line = lines.pop()
    prompt, negative, in_negative = [], [], False
    for line in lines:
        if line.startswith("Negative prompt:"):
            in_negative = True
            line = line[len("Negative prompt:"):].lstrip()
        (negative if in_negative else prompt).append(line)

    params: Dict[str, str] = {}
    order: List[str] = []
    for key, value in _PARAM.findall(params_line):
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            try:
                value = json.loads(value)
            except ValueError:
                value = value[1:-1]
        if key not in params:
            order.append(key)
        params[key] = value

    version = params.get("Version", "")
    tool = "AUTOMATIC1111"
    if version.startswith("f") or "forge" in version.lower():
        tool = "Stable Diffusion WebUI Forge"
    elif "App" in params:
        tool = params["App"]
    found = Generation(tool)
    found.prompt = "\n".join(prompt).strip()
    found.negative = "\n".join(negative).strip()

    used = {"Version", "App"}
    for key, label in A1111_SETTINGS:
        if key in params:
            found.set(label, params[key])
            used.add(key)
    for key, label in A1111_MODEL:
        if key in params:
            found.add_model(label, params[key])
            used.add(key)

    hashes = params.get("Lora hashes")
    loras = _loras_in(found.prompt)
    if hashes:
        used.add("Lora hashes")
        known = {n for n, _w in loras}
        for pair in hashes.split(","):
            name, _, digest = pair.strip().partition(":")
            if name and name.strip() not in known:
                loras.append((name.strip(), "?"))
    found.loras = loras
    for key in order:
        if key not in used:
            found.other.append((key, params[key]))
    if version:
        found.other.append(("Version", version))
    found.raw, found.raw_label = text, where
    return found


# -- ComfyUI ---------------------------------------------------------------

#: Inputs that carry a prompt's text, in the nodes that encode one.
TEXT_INPUTS = ("text", "text_g", "text_l", "clip_l", "t5xxl", "prompt", "string", "value",
               "positive", "text_positive")
#: Inputs through which conditioning reaches a sampler.
CONDITIONING = ("conditioning", "conditioning_1", "conditioning_2", "conditioning_to",
                "conditioning_from", "base_positive", "positive")
#: Inputs a model travels through, loader to sampler.
MODEL_INPUTS = ("model", "unet", "base_model")


class _Graph:
    def __init__(self, prompt: dict):
        self.nodes = {str(k): v for k, v in prompt.items() if isinstance(v, dict)}

    def node(self, link) -> Optional[dict]:
        if isinstance(link, list) and link:
            return self.nodes.get(str(link[0]))
        return None

    @staticmethod
    def inputs(node: dict) -> dict:
        found = node.get("inputs")
        return found if isinstance(found, dict) else {}

    def value(self, raw, depth: int = 0):
        """An input's value, followed through a link to a primitive, a seed
        node or anything else that just holds one."""
        if not isinstance(raw, list) or depth > 6:
            return raw
        node = self.node(raw)
        if node is None:
            return None
        inputs = self.inputs(node)
        for key in ("value", "seed", "noise_seed", "int", "float", "number", "string", "text"):
            if key in inputs:
                return self.value(inputs[key], depth + 1)
        for candidate in inputs.values():
            if not isinstance(candidate, list):
                return candidate
        return None

    def texts(self, link, depth: int = 0) -> List[str]:
        """Every prompt text that flows into a conditioning input."""
        node = self.node(link)
        if node is None or depth > 16:
            return []
        inputs = self.inputs(node)
        found: List[str] = []
        kind = str(node.get("class_type", ""))
        takes_text = "TextEncode" in kind or "Prompt" in kind or "String" in kind or "Text" in kind
        for key in TEXT_INPUTS:
            if key in inputs and (takes_text or key in ("text", "prompt")):
                value = self.value(inputs[key])
                if isinstance(value, str) and value.strip() and value.strip() not in found:
                    found.append(value.strip())
        for key in CONDITIONING:
            if key in inputs and isinstance(inputs[key], list):
                for text in self.texts(inputs[key], depth + 1):
                    if text not in found:
                        found.append(text)
        return found

    def guidance(self, link, depth: int = 0):
        """FluxGuidance sits in the conditioning, not on the sampler."""
        node = self.node(link)
        if node is None or depth > 16:
            return None
        inputs = self.inputs(node)
        if "guidance" in inputs:
            return self.value(inputs["guidance"])
        for key in CONDITIONING:
            if key in inputs:
                found = self.guidance(inputs[key], depth + 1)
                if found is not None:
                    return found
        return None

    def models(self, link, depth: int = 0):
        """(checkpoint label, name), and the LoRAs met on the way to it."""
        node = self.node(link)
        loras: List[Tuple[str, str]] = []
        while node is not None and depth < 24:
            inputs = self.inputs(node)
            if "lora_name" in inputs:
                strength = inputs.get("strength_model", inputs.get("strength", 1))
                loras.append((_scalar(self.value(inputs["lora_name"])), _scalar(self.value(strength))))
            for key, label in (("ckpt_name", "Checkpoint"), ("unet_name", "Diffusion model"),
                               ("model_name", "Model"), ("gguf_name", "Model")):
                if key in inputs and not isinstance(inputs[key], list):
                    return (label, _scalar(inputs[key])), loras
            nxt = next((inputs[k] for k in MODEL_INPUTS if isinstance(inputs.get(k), list)), None)
            node = self.node(nxt)
            depth += 1
        return None, loras

    def size(self, link, depth: int = 0):
        node = self.node(link)
        while node is not None and depth < 12:
            inputs = self.inputs(node)
            if "width" in inputs and "height" in inputs:
                w, h = self.value(inputs["width"]), self.value(inputs["height"])
                return "%s×%s" % (_scalar(w), _scalar(h))
            if node.get("class_type") in ("LoadImage", "LoadImageMask"):
                return "from %s" % _scalar(inputs.get("image"))
            nxt = next((v for k, v in inputs.items()
                        if isinstance(v, list) and k in ("samples", "latent", "latent_image",
                                                         "pixels", "image", "images")), None)
            node = self.node(nxt)
            depth += 1
        return None


def _is_sampler(node: dict) -> bool:
    kind = str(node.get("class_type", ""))
    inputs = node.get("inputs") or {}
    return "Sampler" in kind and ("latent_image" in inputs or "positive" in inputs
                                  or "guider" in inputs)


def comfyui(prompt: dict, has_workflow: bool) -> Optional[Generation]:
    graph = _Graph(prompt)
    samplers = sorted(((k, v) for k, v in graph.nodes.items() if _is_sampler(v)),
                      key=lambda kv: (len(kv[0]), kv[0]))
    if not samplers:
        return None
    passes = []
    for node_id, node in samplers:
        inputs = graph.inputs(node)
        found = Generation("ComfyUI")
        positive = inputs.get("positive")
        negative = inputs.get("negative")
        guider = graph.node(inputs.get("guider"))
        if guider is not None:
            g = graph.inputs(guider)
            positive = g.get("positive", g.get("conditioning", positive))
            negative = g.get("negative", negative)
            if "cfg" in g:
                inputs = dict(inputs, cfg=g["cfg"])
            if "model" in g and "model" not in inputs:
                inputs = dict(inputs, model=g["model"])
        found.prompt = "\n\n".join(graph.texts(positive))
        found.negative = "\n\n".join(graph.texts(negative))

        seed = inputs.get("seed", inputs.get("noise_seed"))
        noise = graph.node(inputs.get("noise"))
        if seed is None and noise is not None:
            seed = graph.inputs(noise).get("noise_seed")
        found.set("Seed", graph.value(seed))
        steps, scheduler, denoise = inputs.get("steps"), inputs.get("scheduler"), inputs.get("denoise")
        sigmas = graph.node(inputs.get("sigmas"))
        if sigmas is not None:
            s = graph.inputs(sigmas)
            steps = s.get("steps", steps)
            scheduler = s.get("scheduler", scheduler)
            denoise = s.get("denoise", denoise)
        found.set("Steps", graph.value(steps))
        sampler_name = inputs.get("sampler_name")
        chosen = graph.node(inputs.get("sampler"))
        if sampler_name is None and chosen is not None:
            sampler_name = graph.inputs(chosen).get("sampler_name")
        found.set("Sampler", graph.value(sampler_name))
        found.set("Scheduler", graph.value(scheduler))
        found.set("CFG", graph.value(inputs.get("cfg")))
        found.set("Guidance", graph.value(graph.guidance(positive)))
        found.set("Size", graph.size(inputs.get("latent_image")))
        if denoise is not None and graph.value(denoise) not in (1, 1.0):
            found.set("Denoise", graph.value(denoise))
        found.set("Node", "%s #%s" % (node.get("class_type"), node_id))

        checkpoint, loras = graph.models(inputs.get("model"))
        if checkpoint:
            found.add_model(*checkpoint)
        found.loras = loras
        passes.append(found)

    first = passes[0]
    result = Generation("ComfyUI")
    result.prompt, result.negative = first.prompt, first.negative
    result.settings, result.model, result.loras = first.settings, first.model, first.loras
    result.passes = passes[1:]
    result.other.append(("Nodes", str(len(graph.nodes))))
    if has_workflow:
        result.notes.append("The whole workflow is in the picture too: Shift+F3 and "
                            "Node graph draws it.")
    return result


# -- NovelAI ---------------------------------------------------------------


def novelai(description: str, comment: dict) -> Generation:
    found = Generation("NovelAI")
    found.prompt = str(comment.get("prompt") or description or "").strip()
    found.negative = str(comment.get("uc") or "").strip()
    for key, label in (("seed", "Seed"), ("steps", "Steps"), ("sampler", "Sampler"),
                       ("noise_schedule", "Scheduler"), ("scale", "CFG scale"),
                       ("cfg_rescale", "CFG rescale"), ("strength", "Strength"),
                       ("noise", "Noise")):
        if key in comment:
            found.set(label, comment[key])
    if "width" in comment and "height" in comment:
        found.set("Size", "%s×%s" % (comment["width"], comment["height"]))
    shown = {"prompt", "uc", "seed", "steps", "sampler", "noise_schedule", "scale",
             "cfg_rescale", "strength", "noise", "width", "height"}
    for key, value in comment.items():
        if key not in shown and not isinstance(value, (dict, list)):
            found.other.append((key, _scalar(value)))
    found.raw, found.raw_label = json.dumps(comment, ensure_ascii=False, indent=2), "Comment"
    return found


# -- InvokeAI --------------------------------------------------------------


def invokeai(metadata: dict) -> Generation:
    found = Generation("InvokeAI")
    found.prompt = str(metadata.get("positive_prompt") or "").strip()
    found.negative = str(metadata.get("negative_prompt") or "").strip()
    for key, label in (("seed", "Seed"), ("steps", "Steps"), ("scheduler", "Scheduler"),
                       ("cfg_scale", "CFG scale"), ("cfg_rescale_multiplier", "CFG rescale"),
                       ("denoising_start", "Denoise start"), ("strength", "Strength")):
        if key in metadata:
            found.set(label, metadata[key])
    if "width" in metadata and "height" in metadata:
        found.set("Size", "%s×%s" % (metadata["width"], metadata["height"]))
    for key, label in (("model", "Model"), ("vae", "VAE")):
        model = metadata.get(key)
        if isinstance(model, dict):
            found.add_model(label, model.get("name") or model.get("model_name"))
        elif model:
            found.add_model(label, model)
    for entry in metadata.get("loras") or []:
        if isinstance(entry, dict):
            model = entry.get("model") or entry.get("lora") or {}
            name = model.get("name") or model.get("model_name") if isinstance(model, dict) else model
            found.loras.append((_scalar(name), _scalar(entry.get("weight"))))
    shown = {"positive_prompt", "negative_prompt", "seed", "steps", "scheduler", "cfg_scale",
             "cfg_rescale_multiplier", "denoising_start", "strength", "width", "height",
             "model", "vae", "loras"}
    for key, value in metadata.items():
        if key not in shown and not isinstance(value, (dict, list)):
            found.other.append((key, _scalar(value)))
    found.raw = json.dumps(metadata, ensure_ascii=False, indent=2)
    found.raw_label = "invokeai_metadata"
    return found


def invokeai_legacy(metadata: dict) -> Generation:
    """`sd-metadata`, which InvokeAI wrote before 3.0."""
    found = Generation("InvokeAI")
    image = metadata.get("image") or {}
    prompt = image.get("prompt")
    if isinstance(prompt, list):
        prompt = " ".join(p.get("prompt", "") for p in prompt if isinstance(p, dict))
    text = str(prompt or "")
    # The negative prompt rode inside the positive one, in square brackets.
    negatives = re.findall(r"\[([^\]]*)\]", text)
    found.prompt = re.sub(r"\[[^\]]*\]", "", text).strip()
    found.negative = ", ".join(n.strip() for n in negatives)
    for key, label in (("seed", "Seed"), ("steps", "Steps"), ("sampler", "Sampler"),
                       ("cfg_scale", "CFG scale"), ("strength", "Strength")):
        if key in image:
            found.set(label, image[key])
    if "width" in image and "height" in image:
        found.set("Size", "%s×%s" % (image["width"], image["height"]))
    found.add_model("Model", metadata.get("model_weights"))
    found.raw = json.dumps(metadata, ensure_ascii=False, indent=2)
    found.raw_label = "sd-metadata"
    return found


# -- anything else that is JSON: Fooocus, SwarmUI, Draw Things ------------

ALIASES = [
    ("prompt", ("prompt", "Prompt", "positive_prompt", "positive", "c", "full_prompt")),
    ("negative", ("negative_prompt", "negativeprompt", "Negative Prompt", "negative", "uc",
                  "full_negative_prompt")),
    ("Seed", ("seed", "Seed", "noise_seed")),
    ("Steps", ("steps", "Steps")),
    ("Sampler", ("sampler", "sampler_name", "Sampler")),
    ("Scheduler", ("scheduler", "Scheduler")),
    ("CFG scale", ("cfg_scale", "cfgscale", "CFG Scale", "Guidance Scale", "guidance_scale",
                   "scale", "cfg")),
    ("Size", ("size", "Resolution", "resolution", "aspect_ratio")),
    ("Checkpoint", ("model", "Model", "Base Model", "base_model", "model_name", "sd_model_name")),
]


def generic_json(data: dict, tool: str, label: str) -> Optional[Generation]:
    """A flat object whose keys name a prompt: the common ground of every tool
    that writes JSON and asked nobody about its keys."""
    inner = data.get("sui_image_params")
    if isinstance(inner, dict):
        tool = "SwarmUI"
        data = inner
    if not any(k in data for _n, keys in ALIASES[:2] for k in keys):
        return None
    found = Generation(tool)
    used = set()
    for name, keys in ALIASES:
        for key in keys:
            if key in data and not isinstance(data[key], (dict, list)):
                used.add(key)
                if name == "prompt":
                    found.prompt = str(data[key]).strip()
                elif name == "negative":
                    found.negative = str(data[key]).strip()
                elif name == "Checkpoint":
                    found.add_model(name, data[key])
                else:
                    found.set(name, data[key])
                break
    if "Size" not in dict(found.settings) and "width" in data and "height" in data:
        found.set("Size", "%s×%s" % (data["width"], data["height"]))
        used.update(("width", "height"))
    loras = data.get("loras") or data.get("LoRAs")
    if isinstance(loras, list):
        used.update(("loras", "LoRAs"))
        for entry in loras:
            if isinstance(entry, (list, tuple)) and entry:
                found.loras.append((_scalar(entry[0]), _scalar(entry[1]) if len(entry) > 1 else ""))
            elif isinstance(entry, dict):
                found.loras.append((_scalar(entry.get("name") or entry.get("model") or entry.get("file")),
                                    _scalar(entry.get("weight") or entry.get("strength"))))
            else:
                found.loras.append((_scalar(entry), ""))
    for key, value in data.items():
        if key not in used and not isinstance(value, (dict, list)):
            found.other.append((key, _scalar(value)))
    found.raw = json.dumps(data, ensure_ascii=False, indent=2)
    found.raw_label = label
    return found


# -- Midjourney ------------------------------------------------------------


def midjourney(text: str) -> Generation:
    """`a prompt --ar 16:9 --v 6.1 Job ID: …` — the parameters are the flags."""
    found = Generation("Midjourney")
    body, _, job = text.partition("Job ID:")
    flags = re.split(r"\s--", " " + body.strip())
    found.prompt = flags[0].strip()
    for flag in flags[1:]:
        name, _, value = flag.strip().partition(" ")
        label = {"ar": "Aspect ratio", "v": "Version", "s": "Stylize", "c": "Chaos",
                 "seed": "Seed", "q": "Quality", "no": "Negative", "niji": "Niji",
                 "style": "Style", "w": "Weird", "iw": "Image weight"}.get(name, "--" + name)
        if name == "no":
            found.negative = value.strip()
        else:
            found.set(label, value.strip() or "on")
    if job.strip():
        found.other.append(("Job ID", job.strip()))
    found.raw, found.raw_label = text, "Description"
    return found


# -- choosing --------------------------------------------------------------


def read(carried: Carried) -> Optional[Generation]:
    """The first reader that recognises what the picture carries."""
    text = carried.text

    invoke = _json(text.get("invokeai_metadata", ""))
    if isinstance(invoke, dict):
        return invokeai(invoke)
    legacy = _json(text.get("sd-metadata", ""))
    if isinstance(legacy, dict):
        return invokeai_legacy(legacy)

    parameters = text.get("parameters")
    if parameters:
        data = _json(parameters)
        if isinstance(data, dict):
            tool = "Fooocus" if "fooocus_scheme" in text or "Fooocus" in parameters else "Unknown tool"
            found = generic_json(data, tool, "parameters")
            if found:
                return found
        elif looks_like_a1111(parameters) or "Negative prompt:" in parameters:
            return a1111(parameters, "parameters")

    comment = _json(text.get("Comment", ""))
    if isinstance(comment, dict) and (text.get("Software", "").startswith("NovelAI")
                                      or "uc" in comment or "n_samples" in comment):
        return novelai(text.get("Description", ""), comment)

    for source in (text.get("prompt"), _prefixed(carried.exif.get("Model"), "prompt:"),
                   _prefixed(carried.exif.get("Make"), "prompt:")):
        graph = _json(source or "")
        if isinstance(graph, dict):
            has_workflow = "workflow" in text or "workflow:" in (carried.exif.get("Make") or "") \
                or "workflow:" in (carried.exif.get("Model") or "")
            found = comfyui(graph, has_workflow)
            if found:
                return found

    for label, value in (("EXIF UserComment", carried.exif.get("UserComment")),
                         ("XMP UserComment", carried.xmp.get("UserComment")),
                         ("JPEG comment", carried.comments[0] if carried.comments else None)):
        if not value:
            continue
        if looks_like_a1111(value) or "Negative prompt:" in value:
            return a1111(value, label)
        data = _json(value)
        if isinstance(data, dict):
            tool = "Draw Things" if "c" in data and ("uc" in data or "sampler" in data) else "Unknown tool"
            found = generic_json(data, tool, label)
            if found:
                return found

    for value in (text.get("Description"), carried.xmp.get("description"),
                  carried.exif.get("ImageDescription")):
        if value and ("Job ID:" in value or re.search(r"\s--(ar|v|niji|s|style)\b", value)):
            return midjourney(value)
    return None


def _prefixed(value: Optional[str], prefix: str) -> Optional[str]:
    if value and value.startswith(prefix):
        return value[len(prefix):]
    return None
