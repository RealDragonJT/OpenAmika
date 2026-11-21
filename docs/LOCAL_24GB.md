# Fully local on one 24 GB card

A setup for an RTX 3090, 3090 Ti or 4090: her brain, her voice, her ears, her memory and her eyes on one PC, with
nothing sent to OpenRouter.

> Worked out and measured by **Denn96** on an RTX 3090 (pull request #12). Edited by MekaHime, who has no 24 GB card
> and has not run it: the memory figures below are the contributor's.

## Why it needs a setup of its own

The README's rule of thumb is that a 24 GB card does not hold a local model next to her: a 27B model takes about
19 GB, and her ears, memory and voice take about 5.5 GB more (about 10 GB with her eyes). This setup gets under
24 GB in four moves:

| Move | What it saves |
|---|---|
| The 16 GB QAT build of Gemma 4 26B as her brain | about 3 GB against the 19 GB build. Gemma 4 26B is the one model under 27B that holds her character |
| Her ears and her memory on the CPU | the video memory both would take; her voice stays on the card |
| The smallest eyes model | about 1.2 GB where the default one takes about 4.3 GB |
| A smaller cache and one model at a time in Ollama | part of the model's working memory |

What the contributor measured on the RTX 3090 with all of it running: **20.5 to 23.5 GB of 24 GB** of dedicated video
memory, `100% GPU` in `ollama ps`, and no shared memory in use.

| Part | Runs on | Video memory |
|---|---|---|
| Her brain: `gemma4:26b-a4b-it-qat`, 16k context | the card | the rest of the total |
| Her voice | the card | about 2.2 GB |
| Her eyes: `smolvlm2-500m` | the card | about 1.2 GB |
| Her ears (speech recognition) and her memory (the embedder) | the CPU | none |

## 1. Stop the driver from spilling into system memory

When a card is almost full, NVIDIA's driver moves the overflow into "shared GPU memory", which is the PC's normal
memory. A language model that spills even a little becomes many times slower, and OpenAmika gives up waiting.

1. Right-click the desktop and open **NVIDIA Control Panel**.
2. **Manage 3D settings**, the **Global Settings** tab.
3. Set **CUDA - Sysmem Fallback Policy** to **Prefer No Sysmem Fallback**, then **Apply**.

## 2. Tell Ollama to use less

Ollama reads these as Windows environment variables. Quit Ollama from its icon in the taskbar tray, open **Edit
environment variables for your account**, add them under **User variables**, and start Ollama again.

| Variable | Value | Why |
|---|---|---|
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` or `q4_0` | The cache of the conversation, which Ollama keeps at 16 bits by default. In Ollama's own words, `q8_0` takes about half the memory "with a very small loss in precision", and `q4_0` about a quarter "with a small-medium loss" that shows more in long contexts. The contributor ran `q4_0`. Start with `q8_0` if it fits |
| `OLLAMA_FLASH_ATTENTION` | `1` | The smaller caches work with flash attention. Ollama turns it on by itself where the card supports it; `1` makes sure |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | Ollama may otherwise keep several models loaded. With `1`, trying another model unloads the one before |

Two variables you may see recommended change nothing here. `OLLAMA_NUM_PARALLEL` is already 1 by default. And
`OLLAMA_KEEP_ALIVE` does not apply to her calls: OpenAmika asks Ollama with every call to keep the model loaded for
30 minutes.

## 3. Get the model

```powershell
ollama pull gemma4:26b-a4b-it-qat
```

It is a 16 GB download. The plain `gemma4:26b` tag may bring a build of up to 19 GB, which does not leave room for
the rest. If older models are still installed, `ollama list` shows them and `ollama rm <name>` removes one.

## 4. Move her ears and memory to the CPU, and give her small eyes

Stop OpenAmika (`Stop_OpenAmika.bat`). Open `settings.json` in the OpenAmika folder with Notepad and change only
these six values. Every other line stays as it is:

| In `settings.json` | Set it to |
|---|---|
| `"speech_device"` | `"cpu"` |
| `"device"` inside `"embedder"` | `"cpu"` |
| `"enabled"` inside `"vision"` | `true` |
| `"model"` inside `"vision"` | `"smolvlm2-500m"` |
| `"max_tokens"` inside `"vision"` | `45` |
| `"max_dps"` inside `"vision"` | `0.3` |

- `speech_device` and the embedder's `device`: her speech recognition and her memory search run on the CPU. Both
  are small models. This is also how OpenAmika sets itself up on a PC without an NVIDIA card.
- `smolvlm2-500m` describes what the camera sees in English only. She still speaks your language. It is downloaded
  at the next start.
- `max_dps` 0.3: at most one description about every 3 seconds, so her eyes do not compete with her brain for the
  card while she answers. `max_tokens` 45 is enough for a short description.
- Without a camera, set `"enabled"` inside `"vision"` to `false`, and the 1.2 GB stay free.

## 5. Start her and choose the model

1. Run `Start_OpenAmika.bat` and wait until every row is ready.
2. On the **Setup** page, under **Brain**, choose **Ollama**, press **Detect models**, pick
   `gemma4:26b-a4b-it-qat`, and press **Use this model**.
3. If an earlier, smaller model left her repeating her own lines, start her memory over: **Setup → Danger zone →
   Wipe memory**. She forgets every conversation and fact.

## 6. Check that it fits

While you talk with her:

- `ollama ps` in a terminal shows `100% GPU` for the model. A split such as `12%/88% CPU/GPU` means it did not fit.
- Task Manager → **Performance** → **GPU**: **Dedicated GPU memory** stays under 24 GB, and **Shared GPU memory**
  stays close to 0.

If it does not fit: use `q4_0`, switch her eyes off, and close other programs that use the card (a game, a browser
with many tabs, another AI tool).
