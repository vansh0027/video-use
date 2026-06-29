# Reel styles — integration guide

How `build_short.py` / `fanout.py` should consume a **style** and merge it over
the existing **brandkit** + **profile** layers.

## The three config layers (and who wins)

```
profile   (engine/profiles/)   → EDITING defaults: target length, trim, min seg
brandkit  (engine/brandkits/)  → IDENTITY: colors, fonts, caption_style, CTA copy
style     (engine/styles/)     → AESTHETIC: grade, caption preset+overrides,
                                  punch-in, pacing, transition, sfx/music intensity
```

A **style is an aesthetic overlay** — it does NOT carry identity (colors stay in
the brandkit) and it does NOT carry trim thresholds (those stay in the profile).
Precedence when building the effective config: `profile` < `brandkit` < `style`
for fields they overlap (style is the most specific look choice), but style
**defers to the brandkit for all colors/keywords** by only ever setting
*non-color* caption fields in its `captions.overrides`.

## Loading

```python
# build_short already sys.path-inserts engine/, so:
from styles.registry import load_style, available_styles

style = load_style(args.style)          # e.g. "dark_sizzle"
# available_styles() → ['clean_premium','dark_sizzle','founder_raw','hormozi_punch']
```

Suggested CLI: add `--style <name>` to `build_short.py` (default `founder_raw`
or whatever the profile names). `fanout.py` can set a per-account `"style"` field
in `jobs.json` (next to `"brandkit"`/`"profile"`) and forward it as `--style`.

## Merging a style into the render

```python
kit  = brandkit.load(args.brandkit)     # existing
prof = load_profile(args.profile)       # existing
style = load_style(args.style)

# 1) GRADE — feed the named preset to grade.get_preset()
grade_filter = grade.get_preset(style["grade"]["preset"])   # subtle|neutral_punch|warm_cinematic|none

# 2) CAPTIONS — pick the preset, then layer style overrides on the brandkit's
#    caption_style (brandkit keeps colors/keywords; style sets the look).
cap = dict(kit.get("caption_style", {}))
cap["preset"] = style["captions"]["preset"]                 # karaoke_line|oneword_pop|clean_twoline
cap.update(style["captions"].get("overrides", {}))          # non-color fields only
brand_for_captions = {**kit, "caption_style": cap}
# → captions_animated.build_ass(words, out, brand_for_captions)

# 3) PUNCH-IN — drive transforms.punch_in / punch_in_animated
pi = style.get("punch_in", {})
if pi.get("on", True):
    zoom = pi["pct"] / 100.0
    vf_punch = transforms.punch_in(zoom)                    # static, reliable default
    # or transforms.punch_in_animated(zoom, frames/fps...) for the slow ramp look

# 4) PACING — cadence/cut-density hints for the segment selector
prof["visual_change_every_s"] = style["pacing"]["cut_cadence_s"]
prof["max_static_shot_s"]     = style["pacing"]["max_static_shot_s"]
#   style == "montage"/"hard" can also enable effects.speed_ramp.dead_air_speedup

# 5) TRANSITION — default join + how often a non-hard transition fires
#   style["transition"]["default"] in {hard_cut,dissolve,fade_through_black,whip_pan,zoom}
#   style["transition"]["frequency"] in [0,1]   (hard_cut ⇒ 0 ⇒ plain concat)
#   → effects/transitions.py (see effects/INTEGRATION.md, STAGE B)

# 6) SFX / MUSIC — intensity → gain, cue palette
#   sfx.INTENSITY_GAIN_DB[ style["sfx"]["intensity"] ]   (None == "off" ⇒ skip)
#   style["sfx"]["cues"]      → which cue names to place
#   style["music"]["duck_db"] → audio_polish.music_duck(bed, duck_db)
```

## Field reference

| field | type | consumed by |
|-------|------|-------------|
| `grade.preset` | `subtle\|neutral_punch\|warm_cinematic\|none` | `helpers/grade.get_preset()` |
| `captions.preset` | `karaoke_line\|oneword_pop\|clean_twoline` | `captions_animated.build_ass` |
| `captions.overrides` | dict (non-color caption_style keys) | merged onto `brandkit.caption_style` |
| `punch_in.pct` / `.frames` | int | `transforms.punch_in[_animated]` |
| `pacing.cut_cadence_s` / `.max_static_shot_s` / `.style` | num/num/str | segment selector + optional `speed_ramp` |
| `transition.default` / `.frequency` / `.duration_s` | str/0..1/num | `effects/transitions.py` |
| `sfx.intensity` / `.cues` | `off\|light\|medium\|heavy` / [str] | `effects/sfx.py` |
| `music.intensity` / `.duck_db` | str / int(dB) | `audio_polish.music_duck` |
| `notes` (top-level + per-section) | str | editorial guidance for the agent — read it |

## The four shipped styles (genuinely distinct)

| style | grade | captions | punch | pace | transition | sfx |
|-------|-------|----------|-------|------|-----------|-----|
| `clean_premium` | subtle | clean_twoline (centered, lowercase) | slow 6% ramp | slow (6s) | dissolve @15% | light (whoosh) |
| `dark_sizzle` | warm_cinematic | oneword_pop | snappy 14% | montage (1.8s) | zoom @40% | heavy (riser/whoosh/impact) |
| `founder_raw` | subtle | karaoke_line | gentle 8% | hard (4s) | hard_cut only | **off** |
| `hormozi_punch` | neutral_punch | oneword_pop (bold, keyword color) | aggressive 16% | hard (2.2s) | hard_cut only | medium (pop) |

`dark_sizzle` vs `hormozi_punch` share one-word captions but diverge hard:
sizzle = cinematic grade + zoom transitions + risers/whooshes; hormozi = clean
neutral grade + **zero** transitions + just pops. `founder_raw` and
`clean_premium` are the restrained pair (no/low SFX, subtle grade) but differ in
caption philosophy (karaoke line vs sparse centered statement) and pacing.

## Caveats

- A style sets caption **look** fields only. Colors, `keywords`, fonts, and CTA
  copy MUST keep coming from the brandkit, so `dark_sizzle` on Counza still pops
  Counza's orange. Never let a style hard-code a hex color.
- `transition.frequency > 0` does not mean "transition every cut" — it's the
  *fraction* of boundaries that should get one; the rest stay hard cuts. See
  `transitions.WHY_HARD_CUTS`.
- `sfx.intensity == "off"` (founder_raw) must short-circuit the whole SFX stage —
  `INTENSITY_GAIN_DB["off"]` is `None`, the sentinel for "skip".
