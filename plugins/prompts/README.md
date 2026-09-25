# AI prompts

A picture made by Stable Diffusion carries its own recipe: every popular tool
writes the prompt and the settings into the file it saves. This reads it and
lays it out — the prompt and the negative prompt as they were written, the
settings a person types in again to get the picture back, the model and the
LoRAs, and the text as it stands in the file, for pasting into the tool that
wrote it.

**Shift+F3 on the picture.** F3 is still the picture — that is what the file
is. This viewer claims PNG, JPEG and WebP at the lowest priority and never
probes, so it is in the way of nothing.

| tool | where it writes | what is read |
| --- | --- | --- |
| AUTOMATIC1111, Forge, SD.Next | PNG `parameters`; EXIF `UserComment` in a JPEG or a WebP | the parameter line, `<lora:…>` tags, `Lora hashes` |
| ComfyUI | PNG `prompt` and `workflow`; EXIF `Model`/`Make` in a WebP | each sampler followed back through the graph: its prompts, seed, steps, sampler, CFG, Flux guidance, the latent's size, the model and every LoRA on the way |
| NovelAI | PNG `Description`, `Comment` | the prompt, undesired content, sampler, schedule |
| InvokeAI | PNG `invokeai_metadata`, and `sd-metadata` before 3.0 | prompts, model, LoRAs with weights |
| Fooocus, SwarmUI | PNG `parameters` as JSON | prompts and the settings by their keys |
| Draw Things | XMP `UserComment` | its JSON |
| Midjourney | PNG `Description`, XMP | the prompt and its `--` flags |

**ComfyUI's graph is Node graph's.** A ComfyUI picture has two records: the
graph the editor draws, which the Node graph plugin draws too, and the prompt
the server was sent. This reads the second — it has no coordinates and says
exactly what ran — and follows each sampler back: the prompt through any
guidance or combining node to the text encoder, the model through each LoRA
loader to the checkpoint. A workflow with two samplers, a base and a hires
pass, shows both.

**A picture with no recipe** says so in one sentence; one that carries other
text — a comment, a software name — shows that text instead of claiming there
is nothing.

## Checking it

```
PYTHONPATH=<xverb>/assets/python python3 selftest.py [pictures…]
```

The ComfyUI prompt in `fixtures/` is a real export. The other records are
written by the test in each tool's documented shape and packed into PNG, JPEG
and WebP the way those tools pack them — they are not files those tools
saved. Real pictures named on the command line are read and summarised.
