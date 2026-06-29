# `video_gen.py` — AI-generation VIDEO (motion B-roll)

The motion sibling of `image_gen.py`. Where `image_gen` synthesises a static
PNG, `video_gen` produces a short **moving** B-roll mp4 — slow, abstract,
drifting visuals of the kind seen behind a talking head in premium AI-creator
reels.

It is **pluggable** and **robust by construction**: every backend returns
`None` on any failure (network, missing key, missing binary, decode, bad
write), and every output is `ffprobe`-validated before it is handed back. It
mirrors `image_gen.py`/`stock.py` exactly, so callers branch on `None` instead
of wrapping in `try/except`.

## Public API

```python
from video_gen import gen_video, gen_broll_clip

# 1) animate an existing still
gen_video(prompt=None, image="frame.png", out_dir=".", duration=4.0,
          width=1080, height=1920, seed=None, backend=None, model=None) -> str | None

# 2) concept -> Flux still (image_gen) -> animated clip, one call
gen_broll_clip(concept, out_dir, duration=4.0, **kw) -> str | None
#   **kw forwards image / width / height / seed / backend / model to gen_video
```

Returns an **absolute mp4 path**, or `None`. Defaults to **1080×1920 (9:16
vertical)**, 30 fps, 4.0 s, h264/yuv420p.

`gen_broll_clip` is the entry point the pipeline should call: if no `image=` is
passed, it synthesises a Flux still for `concept` via `image_gen.gen_image`,
then animates it.

## Backends

Selection precedence: `backend=` arg **>** `$CZ_VIDEO_GEN_BACKEND` **>**
`kenburns`.

| backend     | free? | key / bin needed                         | status                          |
|-------------|-------|------------------------------------------|---------------------------------|
| `kenburns`  | ✅ yes | none (local ffmpeg, offline)             | **working — DEFAULT**           |
| `svd`       | ✅ yes | `CZ_SVD_BIN` (local model binary)        | stub — returns None if unset    |
| `wan`       | ✅ yes | `CZ_WAN_BIN` (local model binary)        | stub — returns None if unset    |
| `replicate` | ❌ paid | `REPLICATE_API_TOKEN`                     | stub — wired submit+poll        |
| `runway`    | ❌ paid | `RUNWAY_API_KEY`                          | stub — wired submit+poll        |
| `kling`     | ❌ paid | `KLING_API_KEY`                          | stub — wired submit+poll        |

**Cost / quality tradeoff.** `kenburns` is free, instant, fully offline, and
gets you ~80% of the way — a still pushed/panned with `zoompan` plus a faint
animated grain so it reads as *motion*, not a frozen zoom. The cloud backends
(`replicate`/`runway`/`kling`) produce genuinely generated motion (true
image-to-video diffusion) but cost credits per call and need network + a key.
The local `svd`/`wan` backends are free once you supply your own compiled
inference binary and model checkpoint (never downloaded for you). Start on
`kenburns`; reach for a paid backend only when a shot truly needs synthesised
motion.

### Backend env vars

- `CZ_VIDEO_GEN_BACKEND` — global default backend.
- `CZ_SVD_BIN` / `CZ_WAN_BIN` — path to a local image-to-video inference binary.
- `REPLICATE_API_TOKEN` (+ optional `CZ_REPLICATE_VIDEO_MODEL`).
- `RUNWAY_API_KEY` (+ optional `CZ_RUNWAY_MODEL`, `RUNWAY_API_VERSION`).
- `KLING_API_KEY` (+ optional `CZ_KLING_MODEL`).
- `CZ_FFMPEG_BIN` / `CZ_FFPROBE_BIN` — override binary paths (default: PATH).

## Integration: dropping motion B-roll at a transcript concept timestamp

`build_short.py` / `storyboard.py` / `compose_best.py` already call
`image_gen.gen_image(...)` to produce a *still* asset for a `"scene"`/`"broll"`
beat. To upgrade a beat to **motion**, swap that call for
`gen_broll_clip(...)` and overlay the returned clip exactly as a video asset:

```python
import video_gen

clip = video_gen.gen_broll_clip(
    concept="neural network firing, abstract, cinematic",
    out_dir=assets_dir,
    duration=beat_end - beat_start,   # match the transcript window
    width=1080, height=1920,
    seed=beat_index,                  # reproducible, distinct per beat
)
if clip:  # always branch on None — gen returns None on any failure
    overlay_clip_at(clip, start=beat_start)
```

### Hard Rules when compositing the clip (from `SKILL.md`)

1. **Overlay timing.** A generated clip dropped at timestamp `T` MUST have its
   PTS reset and offset so it plays at the right moment:
   ```
   [broll]setpts=PTS-STARTPTS+T/TB[bv];
   [base][bv]overlay=enable='between(t,T,T+dur)':x=...:y=...
   ```
   Reset with `setpts=PTS-STARTPTS` first, then add `+T/TB` to place it. This is
   the same rule `loop.py` follows for every overlaid segment.
2. **Subtitles burned LAST.** Generate/composite all motion B-roll first; the
   `subtitles` (libass) filter is the final stage of the graph. Never burn
   captions before overlaying a generated clip — the clip would cover them.
3. **Even dimensions / `setsar=1`.** `video_gen` already emits even dims and
   `setsar=1`, so the clip concats/overlays cleanly with `normalize_vertical`
   output. Keep it that way if you re-scale downstream.
4. **No added text.** `video_gen` produces *visuals only* — never burn titles,
   labels, or wordmarks into the generated clip (project rule: only
   spoken-word captions appear on screen, added at the subtitle stage).

### Audio note

`video_gen` clips are silent (`-an`). They are overlay B-roll, not the audio
bed — the talking-head/original audio stays the master track. Do not pull audio
from a generated clip.

## CLI / self-test

```bash
PY=~/Developer/video-use/.venv/bin/python

# offline self-test: lavfi still -> kenburns -> ffprobe-checked 4s 1080x1920 mp4
$PY engine/video_gen.py --selftest -o /tmp/out

# animate your own still
$PY engine/video_gen.py "data streams, abstract" -i still.png -o /tmp/out -d 4

# concept -> Flux still -> motion (needs network for the still only)
$PY engine/video_gen.py "neural network abstract" -o /tmp/out -b kenburns
```
