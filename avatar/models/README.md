# Live2D models

Amika's own Live2D model is a MekaHime asset under a personal-use license: `scripts/setup_tts.py` downloads
it into `avatar/models/Amika/` from the Hugging Face repository `MekaHime/MekaHime_Amika` (with her voice,
under the same license; it may not be redistributed). The same script puts MekaHime's paper doll, the stand-in
for her helpers, into `avatar/models/Host/` (`live2d/Host/` in that repository, same license; see *The agent
avatar* below). Without any model the Use page shows an empty stage and everything else (voice, subtitles,
memory) works.

**Getting a model.** Any Cubism 3/4/5 model works (a `.model3.json` + `.moc3` + textures):

- your own, exported from Live2D Cubism Editor (export the **runtime** files);
- one of Live2D's free sample models (https://www.live2d.com/en/learn/sample/): download it, and copy the
  folder that holds the `.model3.json` (usually named `runtime`) to `avatar/models/<Name>/`. The samples are
  under Live2D's Free Material License; read it before you publish anything made with them;
- a model you bought or commissioned, within its license.

Then reload the Use page; the Setup page lists it under Avatar, where you can tune scale, position and the
mouth parameters. The emotion banks (which motion and expression plays for each of her 69 emotions) are
optional: see the naming convention below.

Drop each Live2D Cubism model in its own folder here:

```
avatar/models/
└── MyCharacter/
    ├── MyCharacter.model3.json   ← exactly one *.model3.json per folder
    ├── MyCharacter.moc3
    ├── MyCharacter.physics3.json
    ├── textures/ or *.4096/ ...
    ├── *.motion3.json             ← at least one motion
    ├── *.exp3.json                ← expressions (optional)
    └── banks.json                 ← emotion banks (optional; created by the Setup page)
```

Notes:

- Models are **not** committed to git (they are licensed assets). Each user supplies their own —
  e.g. a model exported from Live2D Cubism Editor, or the free Live2D sample models
  (check each model's license before redistribution).
- Lipsync drives `ParamMouthOpenY` (+ `ParamMouthForm` when present). If your model names its
  mouth parameters differently, set them per model on the Setup page (stored in `banks.json`).
- Motions named after emotion keys (e.g. `casual_happy.motion3.json`, `casual_happy.2.motion3.json`)
  are picked automatically when no bank entry exists for that emotion — handy convention if you
  don't want to author banks at all.
- If the `.model3.json` has no `Motions`/`Expressions` blocks, the server injects sibling
  `*.motion3.json` / `*.exp3.json` files automatically when serving it.

## Browser caching

The browser keeps downloaded avatar files in its normal cache, including between visits.
On opening the page, OpenAmika checks the small model description and motion list. Local asset
URLs include a SHA-256 content version, so changing one file downloads that file again while
unchanged textures, model binaries, motions, and expressions can be reused. A normal page reload
picks up edits; changing only a file's timestamp does not invalidate its content version.

Versioned files use a private, long-lived browser cache. Unversioned requests and the generated
model description revalidate with ETags and return an empty `304` response when unchanged.
Clearing browser data, cache eviction, or using another browser/address can require another
download. The browser still needs to prepare the model for rendering on each new page.

## The agent avatar (`role: agent`)

One model folder may stand in for her helpers (Hermes, nanoMuse) instead of being a model she wears.
OpenAmika's own is MekaHime's paper doll, `avatar/models/Host/`, which `scripts/setup_tts.py` installs with her
model (summary row `paper doll Host`; `--skip-paper-doll` leaves it out). Any other model can take its place;
mark it in its `banks.json`:

```json
{ "config": { "display_name": "Host (agent avatar)", "role": "agent",
              "scale": 0.42, "nx": 0.84, "ny": 0.70, "head_y": 0.26 } }
```

- `role: "agent"` keeps it out of her model picker; the Use page seats it on her stage (bottom-right by
  default) whenever an agent server is enabled on the Tools page, slides it away when none is. The first such
  folder in name order is used (so keep one, or skip the paper doll when you bring your own).
- `scale` is its height as a fraction of the stage, `nx`/`ny` its position (fractions of the stage),
  `head_y` how far above its centre the head sits (speech bubbles and glances aim there). Dragging or
  zooming the doll on the Use page saves a per-browser override; "Re-center" clears it.
- It plays motions by file-name prefix, no banks needed: `idle*`, `talk*` (when it has the word),
  `listen*` (while she talks to it), `think*` (a job running), `happy*` (a result), `worried*`
  (an error), `surprise*` (a question for you), chosen by its state alone (the Use page switches off
  the library's random idle motions on it). Recordings made for her can be reused for it when the
  two share the standard Cubism parameter ids (the paper doll's 64 motions were made this way from her
  recordings; `<YourDoll>` holds your model's `.model3.json`, `.moc3`, `.cdi3.json` and physics):

  ```
  python avatar/retarget_motions.py --source avatar/models/Amika --target avatar/models/<YourDoll> --preset agent
  ```

  keeps only the curves the target has (physics-driven parameters are left to physics) and writes
  `idle1…`, `talk1…` etc. into the target folder; `--map PREFIX=stem,stem` picks your own families.
- Her `banks.json` may name the expression used as the "loading" emote (`"busy_expression":
  "Loading"`, the default) and a looping motion that animates it (`"busy_motion": "Scene1"`; none by
  default). The Use page layers both over whatever face and motion she is in while the deep layer has
  not answered yet: the expression's parameters are added, the motion's curves are evaluated on top (it
  never interrupts her idle or emotion motions).
