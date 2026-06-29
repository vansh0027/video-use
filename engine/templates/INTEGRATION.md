# `engine/templates/` — integration

How the branded motion-graphics cards (`mg_templates.py`) and the launch-video
assembler (`product_video.py`) plug into the rest of the engine —
`build_short.py`, `fanout.py`, and the storyboard/EDL flow — and how a **launch
video** job differs from a **reel** job.

Both modules are **additive**: they import sibling engine modules (`motiongfx`,
`brandkit`, `transforms`, `captions_animated`, `audio_polish`) and modify none of
them. They honour the same Hard Rules the rest of the pipeline does.

---

## What's here

```
engine/templates/
├── __init__.py
├── mg_templates.py     # 5 brand-driven cards (mp4 / alpha-webm)
├── product_video.py    # beats → launch/product video assembler
├── source_to_video.md  # source-code/Figma → video workflow
└── INTEGRATION.md      # this file
```

### `mg_templates.py` — public API

Every card is driven by a brand-kit dict and returns the absolute path written.
Full-frame default = 1080x1920 opaque h264/yuv420p/30fps, no audio (same contract
as `motiongfx.hook_card`). `overlay=True` + a `.webm` out-path = VP9/yuva420p with
real alpha for compositing.

```python
stat_card(number, label, out_path, brand, *, duration=2.6, sublabel=None,
          bg="bone", accent_number=True, fps=30, overlay=False) -> str
quote_card(text, out_path, brand, *, duration=3.0, attribution=None,
           bg="bone", highlight=None, fps=30, overlay=False) -> str
feature_bullets(bullets, out_path, brand, *, duration=None, title=None,
                bg="navy", per_bullet=0.9, fps=30, overlay=False) -> str
cta_endcard(text, out_path, brand, *, keyword=None, duration=2.8,
            bg="navy", fps=30, overlay=False) -> str
logo_reveal(wordmark, out_path, brand, *, tagline=None, duration=2.4,
            bg="bone", fps=30, overlay=False) -> str
```

`CARD_BUILDERS = {"stat_card":…, "quote_card":…, "feature_bullets":…,
"cta_endcard":…, "logo_reveal":…}` lets a caller/EDL dispatch by name.

### `product_video.py` — public API

```python
build_product_video(spec, out_path, brand, *, aspect="9:16",
                    music=None, fps=30, work_dir=None) -> dict
```

`spec` is a list of beat dicts (or `{"beats":[…], "music":…}`). Returns a report
dict `{output,width,height,duration,beats,captions,music,degraded}`. Also runs as
a CLI: `product_video.py SPEC.json -o OUT.mp4 --brandkit counza --aspect 9:16`.

---

## Hard-rule alignment (both jobs obey these)

1. **Subtitles LAST.** `product_video._final_pass` burns the `.ass` after the
   beats are concatenated and the music is mixed — never before. Cards never burn
   captions themselves.
2. **Per-segment extract → lossless concat.** Each beat is rendered to an mp4
   with *identical* encode params (`libx264/yuv420p/30fps/aac 48k stereo`), then
   `-c copy` concatenated (filter-concat fallback only on param drift). No
   single-pass filtergraph over the whole timeline.
3. **30 ms boundary audio fades** on every beat via
   `audio_polish.boundary_afades` — silent beats get an `anullsrc` track so the
   fade still applies and the concat stays uniform.
4. **Overlays use `setpts`.** Cards used as *windowed overlays* (the
   `build_short` path below) always start their animation at frame 0, which is
   exactly what `setpts=PTS-STARTPTS+T/TB` resets to. As full *beats* in
   `product_video` they need no setpts (they are the whole segment).
5. **Output-timeline caption offsets.** `product_video._build_caption_words`
   spreads each beat's caption line across that beat's span using the cumulative
   beat offset — captions stay aligned after concat.
6. **loudnorm once, at the end.** In the final pass only. (It is *skipped* when
   the whole program is silence — loudnorm on digital silence emits NaN and
   breaks the AAC encode; see "Gotchas".)

---

## How `build_short.py` calls these (reel job)

`build_short.py` already composites `motiongfx.hook_card` (opaque full-frame mp4)
and `lower_third_png` (sliding RGBA bar) in `final_pass()` **before** subtitles,
via its uniform overlay spec:

```python
{ "kind": "hook_card"|"image"|"logo_row"|"lower_third",
  "asset": <png|mp4>, "at": <out-time s>, "duration": <s>,
  "loop_input": bool, "fade_in": float|None }
```

A `mg_templates` card drops straight into that same mechanism — it produces the
same kind of opaque full-frame mp4 as `hook_card`:

```python
import sys, os
sys.path.insert(0, os.path.join(ENGINE_DIR, "templates"))
import mg_templates

card_mp4 = mg_templates.stat_card("92%", "got in early",
                                  os.path.join(tmpdir, "stat.mp4"), kit, duration=2.6)
overlays.append({"kind": "hook_card",   # reuse the opaque-full-frame compositor
                 "asset": card_mp4, "at": 0.0, "duration": 2.6})
# final_pass() composites it at overlay=0:0, gated to its window, BEFORE subtitles.
```

For a **transparent** card laid OVER live footage (not covering it), render with
`overlay=True` to a `.webm` and composite like a `logo_row` (it carries its own
alpha; add `setpts` at the splice). Suggested EDL surface, mirroring the existing
`hook_card` / `lower_thirds` blocks:

```jsonc
"cards": [
  { "card": "stat_card", "at": 3.0, "duration": 2.6,
    "args": { "number": "92%", "label": "got in early", "bg": "navy" } }
]
```
A thin `resolve_cards()` (modelled on `resolve_hook_card`) would call
`CARD_BUILDERS[name]` and append a `hook_card`-kind overlay — best-effort, warn
and skip on failure, never fatal.

---

## How `fanout.py` calls these (multi-account)

`fanout.py` is a thin orchestrator that shells out to `build_short.py` per
`(clip, account)`. It re-implements no rendering. Two integration points:

- **Cards in reels:** once `build_short` grows the `cards` EDL block above,
  fanout passes it through per-account exactly like it already passes
  `hook_card`/`lower_thirds` — the cards pick up each account's brand kit
  automatically (the kit is the `brand` arg to every builder), so a `stat_card`
  renders in Counza orange for the counza account and in the founder palette for
  the founder account with zero extra wiring.
- **Launch-video jobs:** a launch video is **not** a `build_short` job (no single
  source transcript). fanout would gain a parallel path that calls
  `product_video.build_product_video(spec, out, kit, aspect=…)` per account,
  writing `<out-dir>/<account>/<name>.mp4` and recording it in the same
  `manifest.json`. The per-account brand kit is the only thing that varies; the
  beat list is shared.

---

## Reel job vs launch-video job

| | **Reel** (`build_short.py`) | **Launch video** (`product_video.py`) |
|---|---|---|
| Source | ONE talking-head video | MANY assets: screenshots, screen-recordings, cards |
| Captions | WhisperX **forced alignment** (per-word, ASR) | **Script-driven** — each beat's `caption` line, spread across the beat |
| Cuts | word-boundary ranges of the source | one hard cut per beat boundary |
| Motion | punch-in on kept ranges | Ken-Burns on stills + card animations |
| Cards | *overlays* (windowed, over footage) | full *beats* (interleaved in the cut) |
| Transcript | required (cached per source) | none |
| Entry point | `build_short.py --source …` | `product_video.py SPEC.json -o …` |
| Shared primitives | `transforms`, `captions_animated`, `audio_polish`, `motiongfx`, `mg_templates` | same |

Both end with: graphic layers composited → **subtitles burned LAST** → **loudnorm
once** → `+faststart` h264/yuv420p.

---

## Optional Workstream dependencies (graceful degradation)

`product_video` **late-imports** the cross-workstream modules inside `try/except`
and runs standalone without them:

- **`engine.effects`** (Workstream 1 — sfx, transitions): absent ⇒ hard cuts
  only, no sfx. Recorded in the report's `degraded` list.
- **`engine.video_gen`** (Workstream 2 — generated b-roll): used only when a
  `card` beat carries a `prompt` and its card builder is unavailable; absent ⇒
  the beat degrades to a solid brand-colour placeholder so the build still
  completes. Called via `gen_broll_clip` / `gen_video` if present.

The `degraded` array in the returned report names exactly which enhancements were
skipped, so a caller (or fanout's manifest) can surface it.

---

## Gotchas

- **ffmpeg-full / libass.** Caption burning needs libass (`ffmpeg-full`). Pass
  the binaries via `FFMPEG` / `FFPROBE` env vars (both modules honour them),
  matching `build_short.py`.
- **loudnorm on silence ⇒ NaN.** A launch video assembled entirely from silent
  screenshots/cards has no program audio; loudnorm on pure silence emits NaN and
  the AAC encoder rejects the frame. `product_video` probes the body with
  `volumedetect` and **skips loudnorm when the program is silent** (≤ −70 dB),
  resampling instead. A music bed or any beat audio re-enables it.
- **Boundary fades need > 60 ms beats.** Any beat shorter than `2*FADE_DUR`
  (0.06 s) is clamped up to the minimum rather than dropped, so the 30 ms fades
  stay legal.
- **Card `overlay=True` alpha requires `.webm`.** h264 here is opaque; an
  `overlay=True` render to a non-`.webm` path still flattens onto the brand bg.
- **Fonts are file-pathed, not fontconfig.** Cards reuse `motiongfx`'s concrete
  font discovery (Georgia/Helvetica/Arial), so they never depend on fontconfig.
