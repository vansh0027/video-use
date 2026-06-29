# Effects library — integration guide

How `build_short.py` / `fanout.py` should call `engine/effects/*`. Every builder
here is pure (returns ffmpeg strings / small plan objects) — the renderer owns
all process spawning. These four effects slot into the **existing** build_short
pipeline without changing its shape: per-segment extract → concat → composite
overlays → **burn subtitles LAST** → loudnorm once at the end.

Import (build_short already does `sys.path` to `engine/`, so):

```python
from effects import sfx, transitions, speed_ramp, broll
```

---

## Pipeline map (where each hooks)

```
per source clip
  └─ encode_segment()          ← speed_ramp (per-segment vf/af)   [STAGE A]
concat_segments()              ← transitions (replace a hard cut)  [STAGE B]
final_pass()
  ├─ composite graphic overlays ← broll overlays (BEFORE captions) [STAGE C]
  ├─ burn subtitles  (LAST)                                        ← unchanged
  └─ audio: program + SFX mix → loudnorm (ONCE, at the very end)   [STAGE D]
```

Hard-rule reminders that constrain these hooks:
- **Subtitles are burned LAST.** B-roll overlays and SFX mixing happen *before*
  the caption burn so a cutaway/cue can never hide or desync a caption.
- **Per-segment extract + lossless concat** is the spine. Speed ramps and b-roll
  splices must preserve that: each piece is its own faded segment.
- **30 ms audio fades at every boundary** (`audio_polish.boundary_afades`) still
  apply to every segment, including sped ones.
- **Overlays use `setpts`.** `broll_overlay_filter` already emits
  `setpts=PTS-STARTPTS` on the b-roll leg, matching the image-overlay convention.
- **loudnorm runs once, at the end** — AFTER the SFX mix, never per-cue.

---

## 1. SFX — `effects/sfx.py`  [STAGE D, audio]

```python
# once per render: materialize the synthesized cue library (cached on disk)
lib = sfx.build_cue_library(os.path.join(edit_dir, "sfx_cache"))   # {name: wav}

# derive events from cut boundaries and/or keyword caption beats:
events = sfx.events_from_cuts(cut_times_on_output_timeline, cue="whoosh")
# ...or hand-built: [{"t": 3.4, "cue": "pop"}, {"t": 7.0, "cue": "impact"}]

gain = sfx.INTENSITY_GAIN_DB[style["sfx"]["intensity"]]   # None == "off" → skip
if gain is not None and events:
    plan = sfx.build_sfx_mix(events, lib, gain_db=gain,
                             user_sfx_dir=user_sfx_dir_or_None)
    # splice into the FINAL audio graph, BEFORE loudnorm:
    #   ffmpeg -i <program_video> <*plan.inputs>
    #          -filter_complex "<plan.filtergraph>;
    #                           <plan.out_label>loudnorm=I=-14:TP=-1:LRA=11[a]"
    #          -map 0:v -map "[a]" ...
```

Key points:
- Program audio is **input 0** and is never replaced — cues are `adelay`-ed to
  their event time and `amix`-ed *under* it (`normalize=0` keeps the voice at
  full level; each cue is pre-attenuated by `gain_db`).
- `plan.inputs` are the extra `-i` args; place them on the command line in the
  same order, starting right after the program input (default `first_cue_input=1`).
- `t` values are on the **output** timeline (post-concat), not source clips.
- `user_sfx_dir` (if the user dropped real wavs in the project) overrides the
  synthesized cue of the same name.
- Cues are synthesized with lavfi — **no asset files ship**. Library build is a
  one-time ~4-file ffmpeg pass, cached in `edit/sfx_cache/`.

## 2. Transitions — `effects/transitions.py`  [STAGE B, concat]

Default stays **hard cut** (`concat_segments` unchanged). Only when the style
asks for a transition at a given boundary do you replace that one join:

```python
t = transitions.dissolve(len_a=seg_a_dur, len_b=seg_b_dur,
                          duration=style["transition"]["duration_s"])
# join segment A and segment B with xfade instead of demuxer concat:
#   ffmpeg -i segA.mp4 -i segB.mp4
#          -filter_complex "<t.graph>"        # already refs [0:v]/[1:v]
#          -map "<t.out_label>" ...
# audio across the join: crossfade or hard-join with the usual 30ms fades.
```

- `t.graph` is the complete `-filter_complex` body (it normalizes both legs to
  `yuv420p,fps=30,setsar=1` internally — xfade requires matching formats).
- `t.offset = len_a - duration`; `t.total_out = len_a + len_b - duration`. Use
  `t.total_out` to keep downstream caption/SFX timestamps in sync (a transition
  *shortens* the combined timeline by `duration`).
- `t.suggested_sfx` names a cue that pairs with the transition (whip→whoosh,
  zoom→riser, fadeblack→impact) — feed it into the SFX events list.
- **Apply sparingly.** `transitions.WHY_HARD_CUTS` is the rule of thumb; respect
  `style["transition"]["frequency"]` (0..1) when deciding which boundaries get one.
- Because xfade fuses two segments into one stream, a transitioned pair is best
  rendered as its own sub-render whose output then re-enters the concat list.

## 3. Speed ramp — `effects/speed_ramp.py`  [STAGE A, per-segment]

Constant speed on a segment (slow-mo or speed-up, pitch preserved):

```python
vf, af = speed_ramp.speed_segment(rate)         # e.g. 1.5
# inside encode_segment(), append to the existing chains:
#   -vf "<normalize>,<grade>,<punch>,<vf>"
#   -af "<polish_af>,<af>,<boundary_afades(new_dur)>"
new_dur = speed_ramp.scaled_duration(old_dur, rate)   # for fades + caption map
```

Dead-air compression ("jump-cut without the cut"):

```python
plan = speed_ramp.dead_air_speedup(low_energy_spans, rate=2.5, clip_dur=clip_len)
# render each plan["keep"] span at 1x and each plan["speed"] span with plan["vf"]/
# plan["af"], then concat in order (lossless concat path). new length:
plan["new_total_s"]   # use to remap caption/SFX timestamps; saved == plan["saved_s"]
```

- Audio uses an `atempo` **chain** so factors outside `[0.5, 2.0]` stay
  pitch-correct (`atempo_chain` builds the minimal valid chain).
- After any speed change, recompute the segment duration with
  `scaled_duration` so `boundary_afades` and the word→output caption mapping
  (`map_words_to_output`) stay aligned.
- `dead_air_speedup`'s `keep`/`speed` spans tile `[0, clip_dur]` with no gaps —
  feed them straight into the per-segment extract + concat loop.

## 4. B-roll — `effects/broll.py`  [STAGE C, pre-caption composite]

```python
plan = broll.plan_broll(aroll_dur=output_len, inserts=[
    {"t": 6.0, "dur": 2.5, "asset": "edit/broll/dash.mp4"},
    {"t": 14.0, "dur": 3.0, "asset": "edit/broll/letter.jpg"},  # still auto-detected
])
# in final_pass(), BEFORE the subtitle burn:
#   ffmpeg -i aroll.mp4 <*plan.input_args()>
#          -filter_complex "<plan.overlay_chain()>"
#          -map "<plan.final_label>" -map 0:a    # A-roll AUDIO kept verbatim
#          ... then burn subtitles on the result, then loudnorm.
```

- **Say-it/show-it:** the b-roll covers only the *picture* for its window
  (`overlay ... enable='between(t,t0,t1)'`); the A-roll audio is mapped straight
  through (`-map 0:a`) and never touched. The b-roll's own audio is dropped.
- Stills get `-loop 1 -t <dur>` automatically (`is_image` auto-detected by
  extension); `plan.input_args()` emits the right flags in order.
- Overlapping or too-short inserts are dropped into `plan.dropped` with a reason
  — surface them in the build log.
- This is the **same compositing slot** build_short already uses for image
  overlays, so it inherits the "overlays before captions" ordering for free.

---

## Putting a style together

`fanout.py` / `build_short.py` resolve a **style** (see
`engine/styles/INTEGRATION.md`) into concrete effect calls:

| style field            | effect call |
|------------------------|-------------|
| `sfx.intensity`        | `sfx.INTENSITY_GAIN_DB[...]` → `build_sfx_mix(...)` |
| `sfx.cues`             | which cue names to place on cuts/beats |
| `transition.default`   | `transitions.<name>(...)` |
| `transition.frequency` | fraction of boundaries that get the transition |
| `pacing.style`         | `montage`/`hard` → tighter cadence, maybe `dead_air_speedup` |
| (b-roll is content-driven, not style-driven — inserts come from the EDL) |
