# Extended EDL Schema

The **EDL** (Edit Decision List) is the single JSON document that drives a
render. The content engine consumes an *extended* EDL: it is a strict superset
of the EDL that `helpers/render.py` already understands, so any extended EDL
remains renderable by the base pipeline (the engine just resolves the new
fields into the base ones before calling `render.py`).

- **Base fields** — already consumed by `helpers/render.py` today. Stable.
- **Extended fields** — additive, all **optional**. An EDL with none of them is
  a plain video-use EDL. Each extended field below is tagged:
  - **[MVP]** — implemented by the engine *now*.
  - **[LATER]** — reserved; documented so authors can write forward-compatible
    EDLs, but the current engine ignores it (and warns).

Anything not recognised is ignored, never fatal. Times are seconds (float).
Colors are brand-kit roles or hex; see `engine/brandkits/*.json`.

---

## 1. Base EDL (existing video-use contract)

```jsonc
{
  // name -> path. Paths may be absolute or relative to the EDL / edit dir.
  "sources": {
    "clip1": "raw/clip1.mp4",
    "clip2": "raw/clip2.mp4"
  },

  // Ordered cut list. Each range is one segment of the output timeline.
  "ranges": [
    {
      "source": "clip1",        // required — key into `sources`
      "start": 12.40,           // required — in-point in the SOURCE (s)
      "end":   16.80,           // required — out-point in the SOURCE (s)
      "beat":  "hook",          // optional — narrative beat / label (alias: "note")
      "quote": "Most students…",// optional — the line spoken in this range
      "reason":"strongest take" // optional — why this take was chosen (for review)
    }
  ],

  // Color grade. Either a named look the engine/grade.py knows, or null/"auto"
  // to run the per-clip auto-grade. May also be a raw ffmpeg filter string.
  "grade": "auto",

  // Motion-graphic / text overlays composited on the output timeline.
  // Each overlay targets an OUTPUT time window (not source time).
  "overlays": [
    {
      "at": 0.0,                // output-time start (s)
      "duration": 2.5,          // visible length (s)
      "type": "lower_third",    // overlay kind understood by the overlay engine
      "text": "Vansh • Counza"  // payload (varies by type)
    }
  ],

  // Burned-in subtitle control. When building from transcripts, render.py is
  // invoked with --build-subtitles and emits a master SRT on the output
  // timeline. `subtitles` carries styling / on-off intent.
  "subtitles": {
    "on": true,
    "style": "default"
  },

  // Optional advisory total; the true duration is the sum of the ranges.
  "total_duration_s": 27.3
}
```

### Notes on base fields
- `ranges[].beat` and `ranges[].note` are interchangeable; `render.py` reads
  whichever is present for its progress log.
- `render.py` already honours a flat per-range `"zoom"` (a scale factor applied
  to the segment). The engine writes that field as the *resolved* output of the
  richer `transform.zoom` block described below — authors should prefer
  `transform`.
- Subtitles require `libass` (we ship `ffmpeg-full`).

---

## 2. Extended fields

### 2.1 `account` / `template` — brand-kit selection · **[MVP]**

Names a brand kit in `engine/brandkits/<name>.json` (palette, fonts, caption
style, CTA copy). `account` and `template` are aliases; if both are present,
`account` wins.

```jsonc
{
  "account": "founder",        // -> engine/brandkits/founder.json
  // "template": "counza"      // alias; same resolution
}
```

The MVP resolves the kit and uses it for **caption colors and CTA copy**. The
multi-account *fan-out* (rendering the same EDL across several kits in one run)
is **[LATER]** — see §2.8.

---

### 2.2 `ranges[].transform` — per-range geometry & speed

Per-segment camera/geometry block. Lives inside each range object.

```jsonc
{
  "source": "clip1",
  "start": 12.40,
  "end": 16.80,
  "transform": {
    "zoom": 1.12,              // [MVP] scale factor (1.0 = none). 1.12 = "punch-in".
    "reframe": {              // [LATER] crop/track to recompose framing
      "mode": "track_face",   //   "track_face" | "static_crop"
      "rect": [0.1, 0.0, 0.8, 1.0] // normalized x,y,w,h for static_crop
    },
    "speed": 1.0              // [LATER] time-stretch (1.0 = realtime, 2.0 = 2x)
  }
}
```

- **`transform.zoom`** · **[MVP]** — the engine emits this as the flat `zoom`
  that `render.py` already applies (a quality-free centered scale). The
  `founder_edtech` profile's `punch_in.pct: 112` corresponds to `zoom: 1.12`.
- **`transform.reframe`** · **[LATER]** — face-tracking / crop recomposition.
  Ignored for now (no crop applied).
- **`transform.speed`** · **[LATER]** — per-segment speed change (video + audio
  retime). Ignored for now (treated as `1.0`).

---

### 2.3 `images[]` — logo / generated-scene / B-roll inserts · **[MVP]**

Still images dropped onto the output timeline as branded overlays: a real logo
or crest (Wikimedia), a generated scene (Pollinations), or a local file. Each
insert is resolved to a *pre-positioned* 1080x1920 RGBA overlay PNG
(`engine/image_overlay.py`) and composited in the final pass **before**
subtitles (Hard Rule 1).

```jsonc
{
  "images": [
    {
      "source": "wikimedia",            // "wikimedia" | "gen" | "file"
      "query":  "Harvard University",   // wikimedia: page/search term (logo/crest)
      // "prompt": "ivy campus, autumn, cinematic",  // gen: text-to-image prompt
      // "file":   "assets/letter.png",              // file: local image path
      "at": 5.0,                        // output-time start (s)
      "duration": 2.0,                  // visible length (s)
      "placement": "under_caption",     // "under_caption"|"top"|"corner_tr"|"full"
      "label": "Class of 2026",         // optional small caption under the insert
      "seed": 7                         // optional (gen only) reproducible seed
    }
  ]
}
```

- **`source`** picks the resolver: `wikimedia` → `image_fetch.fetch_image`
  (real logos/crests/seals), `gen` → `image_gen.gen_image` (Pollinations by
  default), `file` → a local path (absolute, cwd-relative, or source-relative).
  If omitted, it is inferred from whichever of `query`/`prompt`/`file` is present.
- **payload** — exactly one of `query` (wikimedia), `prompt` (gen), or `file`
  (file). `wikimedia` also accepts `want: "logo"|"photo"` (default `logo`); `gen`
  also accepts `width`/`height`/`backend`.
- **`placement`** — `under_caption` (logo on a bone chip just above the caption
  band; the default for logos), `top`, `corner_tr`, or `full` (a scene image with
  rounded corners + navy border; the default for `gen`/`file`). The overlay PNG is
  drawn at 0:0 (it is already positioned on a full 1080×1920 canvas).
- **`at` / `duration`** are OUTPUT-timeline seconds; the overlay is gated with
  `enable='between(t,at,at+duration)'`. `label` and `seed` are optional.
- Author inserts inside the EDL's `images` array **and/or** via the `--images`
  CLI arg (a path to a JSON file *or* inline JSON — a bare array or an object
  with an `images` array). The two sources are concatenated (EDL entries first).

Resolution is best-effort: a failed fetch/gen or a missing file is warned about
and **skipped, never fatal** — overlays are enhancements. The inserts actually
composited (with their resolved + overlay paths) are written back into the
output `.edl.json` artifact.

#### `images[].queries` — multi-logo row · **[MVP]**

Instead of a single payload, an `images` entry may carry a `queries` **list**
(2-4 marks). Each is fetched via `image_fetch.fetch_image` and laid out as ONE
centered row of bone chips (`image_overlay.make_logo_row`) — a "got into" /
"trusted by" callout — composited with a short alpha fade-in.

```jsonc
{
  "images": [
    {
      "queries": ["Harvard University", "Stanford University", "MIT"], // 2-4 logos
      "labels":  ["Harvard", "Stanford", "MIT"],   // optional, aligned by index
      "at": 6.0,
      "duration": 3.0,
      "placement": "under_caption"   // "under_caption" (default) | "top" | "center"
                                     //   ("logo_row" is accepted as under_caption)
    }
  ]
}
```

- **`queries`** takes precedence over a single `query`/`prompt`/`file` on the
  same entry. `want` (default `"logo"`) is forwarded to each fetch.
- **`labels`** are optional small navy captions aligned to `queries` by index; a
  query that can't be fetched drops its label with it so the rest stay matched.
- The row PNG is composited at 0:0 with `enable='between(t,at,at+duration)'` and
  a 0.3s alpha fade-in, BEFORE subtitles. Best-effort: unfetchable logos are
  dropped; if none resolve the row is skipped (never fatal).

---

### 2.4 `music` — background bed + ducking · **[LATER]**

```jsonc
{
  "music": {
    "file": "assets/bed.mp3",   // local audio path
    "duck_db": -20              // attenuation under speech (sidechain duck)
  }
}
```

Status: **[LATER]**. The profile already declares the intended
`audio.music_duck_db: -20`; sidechain ducking wiring is deferred. The MVP does
**not** mix a music bed (speech-only output). Loudness normalization of the
speech track itself is part of the base audio chain.

---

### 2.5 `loop` — seamless loop / replay tail · **[MVP]**

```jsonc
{
  "loop": {
    "on": true,
    "mode": "crossfade",        // "crossfade" | "freeze_match"
    "duration": 0.5             // blend length (s); clamped to <= 0.5*clip length
  }
}
```

- **[MVP]** — after the final render, the finished file is post-processed by
  `engine/loop.py` so the end flows back into the start. `crossfade` dissolves the
  tail into the head (output length is preserved); `freeze_match` briefly holds
  the last frame, then blends it toward the head (for near-static endings).
- **Driven by the CLI**: enable with `--loop [crossfade|freeze_match]` (+ optional
  `--loop-dur`); a bare `--loop` uses `crossfade` at 0.5 s. Default: off. The
  `loop` block shown above is written into the output `.edl.json` as a record of
  what was applied (`{"on": false}` when no loop ran).
- `loudnorm` is **not** re-applied (the input already passed the final loudness
  stage; the crossfade is equal-power). A loop-blend failure is non-fatal — the
  un-looped render is kept and a warning is printed.

---

### 2.6 `cta` — closing call-to-action · **[MVP]**

Comment-bait / CTA shown near the end of the video. Copy defaults come from the
resolved brand kit's `cta` block; the EDL may override per-render.

```jsonc
{
  "cta": {
    "keyword": "PROFILE",                                   // the word to comment
    "text": "Comment 'PROFILE' for your free profile review" // the on-screen line
  }
}
```

- **[MVP]** — the engine renders a CTA card/overlay at the tail using brand-kit
  colors. When `cta` is omitted but the active profile has `cta.on: true`, the
  engine falls back to the brand kit's `cta` copy.
- Set the profile's `cta.on` to `false` (or omit this block with a non-CTA
  profile) to suppress it.

---

### 2.7 `captions` — caption styling override · **[MVP]**

EDL-level override of the profile's caption behaviour. The headline feature is
animated, keyword-highlighted captions.

```jsonc
{
  "captions": {
    "style": "animated"         // [MVP] "animated" | "static"
  }
}
```

- **`captions.style: "animated"`** · **[MVP]** — word-pop captions, max ~4 words
  per line, brand keyword highlighting (colors from the brand kit's
  `caption_style`). This corresponds to the `founder_edtech` profile's
  `captions.animated: true`.
- `"static"` falls back to the base burned-SRT path in `render.py`.
- This block layers on top of the base `subtitles` field; if both are present,
  `captions.style` decides the rendering path and `subtitles.on` decides
  visibility.

---

### 2.8 Multi-account fan-out · **[LATER]**

Reserved: a future top-level `accounts: ["founder", "counza", ...]` that renders
the same cut once per brand kit in a single invocation. **[LATER]** — the MVP
renders a single account (see §2.1). Documented here so the field name is
reserved and won't collide.

---

### 2.9 `hook_card` — animated opening title card · **[MVP]**

An animated full-frame "hook" title that opens the short
(`engine/motiongfx.hook_card`): a big serif headline (with an optional orange
highlight word) under the Counza wordmark, revealing with an ease-out rise +
fade. It is rendered to an **opaque** 1080×1920 mp4 and composited as a VIDEO
overlay covering the output's first `duration` seconds (`overlay=0:0`,
`enable='between(t,0,duration)'`), **before** subtitles (Hard Rule 1).

```jsonc
{
  "hook_card": {
    "text": "Most students get this WRONG",  // headline (required)
    "highlight": "WRONG",                      // optional word/phrase -> orange
    "bg": "navy",                              // "bone" (default) | "navy"
    "duration": 2.5                            // seconds (default 2.5)
  }
}
```

- Also settable on the CLI: `--hook-card "TEXT"` `[--hook-card-highlight WORD]`
  `[--hook-card-bg bone|navy]`. CLI fields override the EDL block **per field**.
- Best-effort: a render failure is warned about and skipped (never fatal). The
  card actually rendered (its copy + resolved clip path) is written into the
  output `.edl.json`.

---

### 2.10 `lower_thirds` — name / credential bars · **[MVP]**

Branded lower-third bars (`engine/motiongfx.lower_third_png`): a rounded
navy_deep panel with a left orange accent tick, a white name line, and a lighter
credential line. Each is rendered to a tight RGBA PNG and composited so it
**slides in from the left over 0.4s** and holds at y≈1450
(`enable='between(t,at,at+duration)'`), **before** subtitles (Hard Rule 1).

```jsonc
{
  "lower_thirds": [
    {
      "name": "Vansh Gupta",            // required
      "credential": "Founder, Counza",  // optional second line
      "at": 3.0,                        // output-time start (s)
      "duration": 4.0                   // visible length (s; default 3.0)
    }
  ]
}
```

- Also settable on the CLI (repeatable): `--lower-third "Name|Credential|AT|DUR"`
  (only `Name` is required; `AT`/`DUR` default to 0 and 3.0). CLI bars are
  appended after any EDL `lower_thirds`.
- Best-effort: a bar without a name, or a render failure, is warned about and
  skipped. The bars actually rendered are written into the output `.edl.json`.

---

## 3. MVP implementation status — quick reference

| Field                         | Status   | MVP behaviour                                              |
|-------------------------------|----------|-----------------------------------------------------------|
| `sources`, `ranges`, `grade`, `overlays`, `subtitles`, `total_duration_s` | base | as in `helpers/render.py` |
| `account` / `template`        | **MVP**  | resolve brand kit → caption colors + CTA copy             |
| `ranges[].transform.zoom`     | **MVP**  | emitted as flat per-range `zoom` (punch-in)               |
| `cta`                         | **MVP**  | tail CTA card; falls back to brand-kit copy               |
| `captions.style: "animated"`  | **MVP**  | animated keyword-highlight captions                       |
| `images[]`                    | **MVP**  | logo/gen/file inserts → positioned overlays, composited BEFORE subtitles |
| `images[].queries` (logo row) | **MVP**  | 2-4 logos fetched → one centered chip row overlay (alpha fade-in) |
| `hook_card`                   | **MVP**  | animated full-frame opening title; opaque over the first DUR s, BEFORE subtitles |
| `lower_thirds`                | **MVP**  | name/credential bars that slide in from the left at y≈1450, BEFORE subtitles |
| `loop`                        | **MVP**  | seamless end→start loop via `--loop`; recorded in the output EDL |
| `ranges[].transform.reframe`  | LATER    | ignored (no crop / tracking)                              |
| `ranges[].transform.speed`    | LATER    | ignored (treated as 1.0)                                  |
| `music`                       | LATER    | ignored (no music bed / ducking)                          |
| multi-account fan-out         | LATER    | single account only                                       |

---

## 4. Minimal MVP example

```jsonc
{
  "account": "founder",
  "sources": { "a": "raw/take1.mp4" },
  "ranges": [
    { "source": "a", "start": 3.10, "end": 9.40, "beat": "hook",
      "transform": { "zoom": 1.12 } },
    { "source": "a", "start": 14.0, "end": 31.5, "beat": "payoff" }
  ],
  "grade": "auto",
  "captions": { "style": "animated" },
  "hook_card": { "text": "Most students get this WRONG", "highlight": "WRONG",
                 "bg": "navy", "duration": 2.5 },
  "lower_thirds": [
    { "name": "Vansh Gupta", "credential": "Founder, Counza", "at": 3.0, "duration": 4.0 }
  ],
  "images": [
    { "source": "wikimedia", "query": "Harvard University",
      "at": 4.0, "duration": 2.5, "placement": "under_caption", "label": "Harvard" },
    { "queries": ["Harvard University", "Stanford University", "MIT"],
      "labels": ["Harvard", "Stanford", "MIT"],
      "at": 12.0, "duration": 3.0, "placement": "under_caption" }
  ],
  "cta": { "keyword": "PROFILE", "text": "Comment 'PROFILE' for a free profile review" },
  "loop": { "on": true, "mode": "crossfade", "duration": 0.5 },
  "total_duration_s": 23.8
}
```

> The `images` array here is consumed directly. The `loop` block is currently a
> *record* — trigger looping with the `--loop` CLI flag (the engine writes the
> applied settings back into the output EDL).
