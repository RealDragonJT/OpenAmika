# ollama-bridge

Use vLLM where OpenAmika expects Ollama.

OpenAmika's fully local route is Ollama. Some models do not run there. NVIDIA's NVFP4 checkpoints for RTX 50 series
and other Blackwell GPUs are the case this was written for: vLLM serves them, Ollama does not load them.

`ollama_bridge.py` answers the part of Ollama's API that OpenAmika uses and passes each call on to a server that
speaks the OpenAI API. You point the Setup page's **Ollama server** field at the bridge. Nothing in OpenAmika
changes: to her it is Ollama, with a model per role and calls booked at zero cost.

It is one Python file with no dependencies beyond the standard library. OpenAmika does not load it or know that
it is there: if you never start it, nothing about OpenAmika is different.

> A community contribution by realtime3392 (pull request #13). MekaHime does not run vLLM: the recipes below are the
> contributor's, and each says what was run. Problems with the bridge itself and fixes to it are welcome as GitHub
> issues and pull requests; for installing vLLM, the projects the recipes link to are the place to ask.

## Local servers only

The bridge cannot be pointed at a hosted API, and it has nothing to do with API keys:

- The model server's address must be plain `http` on this machine or on a private network. A public address is
  refused when the bridge starts and again on every call.
- No API key is read, stored or sent. There is no option for one.
- It listens on this machine only unless you tell it otherwise, and refuses requests that come from a web page.

## Use

1. Start your model server. The recipes below give the whole command for three kinds of machine. For vLLM in
   general, give it a context window of at least 16k (`--max-model-len 16384` or more: her prompts are about 8k
   tokens) and its reasoning parser if the model thinks, so that thinking arrives apart from the answer.
2. Start the bridge with the server's OpenAI address:

   ```powershell
   envs\brain\Scripts\python.exe contrib\ollama-bridge\ollama_bridge.py --backend http://127.0.0.1:8000/v1
   ```

   It prints the models it found and its own address, `http://127.0.0.1:11435` unless you pass `--listen`.
3. On OpenAmika's Setup page, under **Brain**, choose **Ollama**, put the bridge's address in **Ollama server**,
   press **Detect models**, pick the model and press **Use this model**. The status line then reads
   "Ollama 0.1.0 (ollama-bridge) is running".

Leave the bridge running while she does. If Ollama itself is installed it is not disturbed: it stays on port 11434.

**When something is not running.** The bridge may be started before the model server; it waits for it. If the
model server or the bridge is down, OpenAmika says what it says for a stopped Ollama: the Setup page shows "not
answering" and the pre-flight shows `!! Ollama  not answering at …`. She cannot answer until both are back. To go
on without them, switch back to the OpenRouter preset on the Setup page, as from Ollama. The bridge's own window
prints the reason whenever the model server does not answer.

| Option | Meaning |
|---|---|
| `--backend URL` | The model server's OpenAI base, for example `http://127.0.0.1:8000/v1`. Required. |
| `--listen HOST:PORT` | Where the bridge answers. Default `127.0.0.1:11435`. Another host exposes it to your network, with no key, the way `OLLAMA_HOST` does. |
| `--no-think-switch` | Do not offer OpenAmika the thinking switch (below). |
| `--verbose` | Print one line per request. Without it only failures are printed. Message text is never printed. |

## Recipes

Three ways to get a vLLM server for the bridge to stand in front of. The model in each is NVIDIA's NVFP4 build of
Gemma 4 26B, [`nvidia/Gemma-4-26B-A4B-NVFP4`](https://huggingface.co/nvidia/Gemma-4-26B-A4B-NVFP4): public, Apache
2.0, 18.8 GB, downloaded on the first start. NVFP4 is for Blackwell GPUs, such as the RTX 50 series and GB10.

The flags that matter in every recipe:

| Flag | Why |
|---|---|
| `--reasoning-parser gemma4` | Thinking arrives apart from the answer, so the bridge can keep it out of her words. |
| `--moe-backend cutlass` | The kernel that is built into vLLM. Left to choose, vLLM takes one that it compiles at the first start: that needs the CUDA toolkit and a compiler, takes minutes, and fails on Windows on ARM64. |
| `--max-model-len` | 16384 or more: her prompts are about 8k tokens. |
| `--served-model-name` | The name the Setup page will show. |

### Windows 11 on x86_64

vLLM has no official Windows build. [SystemPanic/vllm-windows](https://github.com/SystemPanic/vllm-windows)
publishes wheels; this uses its release [v0.32.0](https://github.com/SystemPanic/vllm-windows/releases/tag/v0.32.0),
which wants Python 3.12 and an NVIDIA driver with CUDA 13.

1. Install Python 3.12 and download `vllm-0.32.0+cu132-cp312-cp312-win_amd64.whl` from that release.
2. Install it and start the model server:

   ```powershell
   py -3.12 -m venv C:\vllm-venv
   C:\vllm-venv\Scripts\pip install .\vllm-0.32.0+cu132-cp312-cp312-win_amd64.whl --extra-index-url https://download.pytorch.org/whl/cu130
   $env:VLLM_USE_FLASHINFER_SAMPLER = '0'
   C:\vllm-venv\Scripts\vllm serve nvidia/Gemma-4-26B-A4B-NVFP4 --served-model-name Gemma-4-26B --max-model-len 24576 --reasoning-parser gemma4 --moe-backend cutlass --host 127.0.0.1 --port 8000
   ```

   `VLLM_USE_FLASHINFER_SAMPLER=0` is needed with this wheel. Without it the sampler loads a FlashInfer module
   that does not parse in v0.32.0 (`SyntaxError` at the model load; fixed in source for v0.33.0, see
   [vllm-windows#96](https://github.com/SystemPanic/vllm-windows/issues/96)), and past that it would compile itself with
   tools a normal PC does not have. With more than one NVIDIA GPU, pick the card with
   `$env:CUDA_DEVICE_ORDER = 'PCI_BUS_ID'` and `$env:CUDA_VISIBLE_DEVICES = '<index from nvidia-smi>'`.

   On a PC with one NVIDIA card, mind the card's memory: vLLM takes 90% of it unless it is told otherwise
   (`--gpu-memory-utilization`, 0.9 by default), and her ears, her memory and her voice need about 5.5 GB of the same
   card (about 10 GB with her eyes). Give vLLM what is left, with 1 GB to spare: (card − 5.5 − 1) / card, which is
   about 0.8 on a 32 GB card, or (card − 10 − 1) / card with her eyes, about 0.65. The model's 18.8 GB must fit into
   that share, so a 24 GB card is too small for it next to her. This is arithmetic from what the two programs
   document, added in review; it has not been tried.

3. In a second window, from the OpenAmika folder, start the bridge:

   ```powershell
   envs\brain\Scripts\python.exe contrib\ollama-bridge\ollama_bridge.py --backend http://127.0.0.1:8000/v1
   ```

4. On the Setup page, **Ollama server** is `http://127.0.0.1:11435`.

What was run, on an x86_64 Windows 11 laptop with an RTX PRO 5000 Blackwell eGPU (48 GB, driver 621.14): steps 1 to 3
as written, from a fresh environment (the install took 5 minutes, 5.6 GB), the model from a local copy. The server
was ready 143 s after its start with no kernels compiled, and OpenAmika's own Ollama client completed all four roles
through the bridge, with thinking on and off, and the model stopped generating within a few tokens of a hang-up.
A whole session was run too, on the same laptop: OpenAmika installed fresh and unmodified by its own
`scripts\setup_windows.ps1` and `scripts\setup_tts.py`, pointed at the bridge from the Setup page (Detect models, Use
this model), and talked with by voice, with her avatar and the camera on. Every role completed through the bridge:
the surface and deep replies (first words in about 0.3 s), the memory writer and the reflection.

### GB10: DGX Spark and other machines on DGX OS (Ubuntu 24.04, aarch64)

The model server and the bridge run on the GB10 machine. OpenAmika stays on your Windows PC and reaches them over
your network, the way it reaches Ollama on a second PC. Only `ollama_bridge.py` has to be copied to the GB10
machine; its own Python runs it.

```bash
python3 -m venv ~/vllm-venv
~/vllm-venv/bin/pip install "vllm==0.33.0" --extra-index-url https://download.pytorch.org/whl/cu130
~/vllm-venv/bin/vllm serve nvidia/Gemma-4-26B-A4B-NVFP4 --served-model-name Gemma-4-26B \
    --max-model-len 16384 --reasoning-parser gemma4 --moe-backend cutlass \
    --gpu-memory-utilization 0.22 --host 127.0.0.1 --port 8000
```

```bash
python3 ollama_bridge.py --backend http://127.0.0.1:8000/v1 --listen 0.0.0.0:11435
```

On the Setup page, **Ollama server** is `http://<address of the GB10 machine>:11435`.

- Graphics and system memory are one pool on this machine, and `--gpu-memory-utilization` is a share of all of it:
  0.22 is about 27 GB. Without the flag vLLM takes nearly all of the machine. With room to spare, raise it together
  with `--max-model-len`.
- `--listen 0.0.0.0:11435` opens the bridge, which has no key, to your network. That is the point here; keep it to a
  network you trust.

What was run, on a DGX Spark with DGX OS 7.2.3: the install (3 minutes, 9 GB), the server (ready 3 minutes after its
start, the model already on disk), the bridge under the system's Python 3.12, and OpenAmika's own Ollama client on a
Windows PC, which completed all four roles across the network. A whole OpenAmika session was not run against it.

### ADVANCED: RTX Spark N1X laptops (Windows 11 on ARM64)

Two things make this one hard, and neither is the bridge.

- **vLLM has to be put together by hand.** Follow SystemPanic's
  [RTX Spark guide](https://github.com/SystemPanic/vllm-windows/blob/vllm-for-windows/README_SPARK.md): Python 3.14,
  CUDA 13.4, PyTorch from NVIDIA's index, several dependencies compiled from source, then its `win_arm64` wheel or
  a build of its branch.
- **OpenAmika's own setup does not cover Windows on ARM64 yet.** `scripts\setup_windows.ps1` installs PyTorch builds
  that do not exist for it. Run OpenAmika on another PC and point it at this one, as in the GB10 recipe, or build
  her environments by hand.

What this machine needed beyond the guide:

- `--moe-backend cutlass` is not optional here: the kernel vLLM would otherwise compile at the first start does
  not compile on this platform.
- `VLLM_USE_FLASHINFER_SAMPLER=0`, or the sampler too compiles itself at first use.
- `--gpu-memory-utilization 0.28`: memory is one pool here as on GB10, and vLLM sees all of it.
- A CUDA toolkit no newer than the CUDA level of the driver (`nvidia-smi` shows it) when building: kernels built
  with a newer toolkit fail to load.

```powershell
$env:VLLM_USE_FLASHINFER_SAMPLER = '0'
C:\vllm-venv\Scripts\vllm serve nvidia/Gemma-4-26B-A4B-NVFP4 --served-model-name Gemma-4-26B --max-model-len 24576 --reasoning-parser gemma4 --moe-backend cutlass --gpu-memory-utilization 0.28 --host 127.0.0.1 --port 8000
```

The bridge is started as in the first recipe, with the Python of whichever environment you have.

What was run, on an RTX Spark N1X laptop with a build of that branch at 0.33.0: the serve line above (ready in under
two minutes) and OpenAmika's own Ollama client through the bridge, all four roles. Unmodified OpenAmika, with
environments built by hand, was also set up through its Setup page and answered typed turns, on another NVFP4
checkpoint of Gemma 4 26B. SystemPanic's 0.32.0 `win_arm64` wheel was not tried.

### Linux on x86_64

Not written yet: it will be added once it has been run. PyPI has the vLLM wheel for it.

## What is translated

| OpenAmika asks Ollama | The model server gets |
|---|---|
| `GET /api/tags`, `/api/ps` | `GET /models`; each served model is listed under its own name |
| `POST /api/show` | `GET /models`; the context window comes from vLLM's `max_model_len` |
| `POST /api/chat` | `POST /chat/completions`, streamed or whole, as asked |
| `options.num_predict` | `max_tokens` |
| `think: true` or `false` | `chat_template_kwargs: {"enable_thinking": true}` or `false` |
| `format: "json"` | `response_format: {"type": "json_object"}` |
| `options.num_ctx`, `keep_alive` | nothing: the server fixed its window and keeps its model loaded |

Coming back, the answer's text becomes `message.content`, reasoning the server reports apart from the answer becomes
`message.thinking`, and the server's token counts become `prompt_eval_count` and `eval_count`. An error from the
server reaches OpenAmika with its reason, also when it arrives in the middle of an answer. An answer that breaks off
before the server finished it, or a whole answer with no reply in it, is reported as an error, not as a short reply.
When OpenAmika hangs up, on Unlock for example, the bridge hangs up on the server, which stops generating.

**The thinking switch.** OpenAmika lets only the reflection think and switches thinking off for the roles she
speaks with, when Ollama says the model can think. The bridge says so for every model, so the switch is always sent
and reaches the model's chat template as `enable_thinking`. A model without such a switch ignores it. Without the
switch a thinking model can spend its whole answer budget on thought, and she says nothing. `--no-think-switch` is for
a server that rejects `chat_template_kwargs`.

## Limits

- Text only: no images, no embeddings, no tool calls. OpenAmika's Ollama transport uses none of them.
- No model management. `ollama pull` and the like have no meaning here; the model server decides what is loaded.
- Model size and quantization are shown as unknown on the Setup page, because the OpenAI API does not report them.
- Thinking written into the answer itself as `<think>…</think>` is not taken out. Run the server with its reasoning
  parser.
- Tried with vLLM only, as the recipes say. Other OpenAI-compatible local servers should work where they accept
  the same request fields, and have not been tried.

## Tests

`tests/test_ollama_bridge.py` runs with the offline suite. Most of it drives OpenAmika's own Ollama client
(`brain/f_ollama.py`) through the bridge against a stand-in model server, so a change in what that client asks of
Ollama fails there.
