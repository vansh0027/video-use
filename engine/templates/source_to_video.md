# Source code / Figma → launch video

A concrete workflow for turning a **product's source code or Figma file** into a
launch / product-demo video, using the engine's existing animation slots
(HyperFrames, Remotion, Manim — described in `SKILL.md`), screen-recording, the
branded `mg_templates` cards, and the `product_video.py` assembler.

The output of every path below is a **beat** — a screenshot, a clip, or a card —
that `engine/templates/product_video.py` stitches into the final vertical (or
16:9) video with captions and an optional music bed.

---

## 0. Decide the engine per shot (do NOT default to Remotion)

`SKILL.md` is explicit: *pick the engine per animation slot.* For a
source-code/Figma launch video the decision tree is:

| You have…                                              | Use…                         | Why |
|--------------------------------------------------------|------------------------------|-----|
| A **running web app / live UI** you can drive          | **Screen-recording** + Ken-Burns | Real product motion, zero authoring. The fastest, most authentic path. |
| A **deployed page or a static mock** (HTML/CSS)        | **HyperFrames** (capture web UI) | Browser-native HTML/CSS/GSAP composition; deterministic frame capture, lint/validate/render. Best for website→video, kinetic type, transparent WebM overlays. |
| **Real React components** / an existing Remotion brand system | **Remotion** (import the components) | Compose with component state and reusable React primitives; render the *actual* components, not a recreation. |
| **Architecture / data-flow / state machines / equations** | **Manim** (`skills/manim-video/`) | Formal diagrams, graph morphs, derivations. |
| A **static screen** (Figma export, screenshot, App Store shot) | **Screenshot + Ken-Burns** | A flat PNG reads as motion via `product_video`'s push-in. No engine needed. |
| **Stat / quote / feature-list / CTA / logo** moments    | **`mg_templates` cards**     | Pre-built, brand-driven; no per-video rebuild. |

You will usually **mix** these in one video: e.g. a `logo_reveal` open → two
HyperFrames UI captures → a `stat_card` → a screen-recording → a `cta_endcard`.

> **Node 22 required for HyperFrames.** Node is **v18** here by default. Before
> any HyperFrames slot: `nvm install 22 && nvm use 22`. Manim and the PIL-based
> cards work as-is on the default Node/Python.

---

## 1. From a Figma file

1. **Export the frames you want as PNGs** (Figma → export at 2–3x). These become
   `screenshot` beats directly — `product_video` Ken-Burns-pushes them.
2. For **motion** (a button press, a screen-to-screen transition, a number
   counting up), recreate the frame as an **HyperFrames** HTML/CSS composition
   and animate it with GSAP, OR rebuild it as **Remotion** React if you already
   have the components. Capture/render to an mp4 (or transparent WebM) — that mp4
   is a `screen_recording` beat.
3. **Figma MCP** (`get_design_context` / `get_screenshot` / `get_variable_defs`)
   can pull the exact layout, tokens, and a screenshot to seed the HyperFrames
   HTML or the Remotion components so the motion matches the design system.

## 2. From source code

1. **Run the app locally** and **screen-record** the real flows you want to
   feature (a login, a dashboard loading, a search). QuickTime / `ffmpeg
   avfoundation` / any recorder → an mp4 per flow. These are `screen_recording`
   beats. This is the highest-fidelity, lowest-effort path — prefer it.
2. For UI that's **awkward to drive live** (an error state, an empty state, a
   paywall), capture a **HyperFrames** composition of just that view, or render
   the **real React component** in **Remotion** (import it from the codebase, feed
   it props for the exact state). Render → mp4 beat.
3. For **how it works** (architecture, request flow, a state machine), build a
   **Manim** slot. Render → mp4 beat. Read `skills/manim-video/SKILL.md` first.

---

## 3. Author the animation slots (per `SKILL.md`)

Scaffold every slot inside `edit/animations/slot_<id>/` — never at the repo root.

**HyperFrames** (Node 22+):
```bash
nvm use 22
cd <videos_dir>/edit/animations/slot_dashboard
npx --yes hyperframes init . --example blank --non-interactive --skip-skills
# build index.html (the captured UI / kinetic type) with GSAP
npx --yes hyperframes lint .
npx --yes hyperframes validate .
npx --yes hyperframes render . -o render.mp4            # opaque full-frame beat
npx --yes hyperframes render . --format webm -o render.webm   # alpha overlay
```

**Remotion** (project-local, isolated in the slot):
```bash
cd <videos_dir>/edit/animations/slot_counter
npx create-video@latest .          # or install remotion locally
# import the real React component, drive it with props for the target state
npx remotion render <CompId> render.mp4
ffprobe -v error -show_entries stream=width,height:format=duration render.mp4
```

**Manim**: build the scene, render to `render.mp4`, verify dims/duration with
`ffprobe`. (`skills/manim-video/SKILL.md`.)

Verify **every** rendered slot with `ffprobe` (duration + dimensions) before
wiring it into a beat — a wrong-length or wrong-aspect render silently desyncs
the cut.

---

## 4. Assemble with `product_video.py`

Collect every beat (screenshots, slot renders, cards) into a spec and build:

```jsonc
// launch_spec.json
{
  "beats": [
    { "kind": "card", "card": "logo_reveal", "wordmark": "Counza",
      "args": { "tagline": "college, decoded" }, "duration": 2.2, "bg": "bone" },

    { "kind": "screen_recording", "path": "edit/animations/slot_dashboard/render.mp4",
      "caption": "Your whole admissions plan in one place", "duration": 4.0 },

    { "kind": "screenshot", "path": "figma/profile_screen@3x.png",
      "caption": "See exactly where your profile stands", "duration": 3.0, "zoom": 1.14 },

    { "kind": "card", "card": "stat_card", "number": "92%",
      "label": "of students improved their profile", "caption": "Real results", "bg": "navy" },

    { "kind": "card", "card": "feature_bullets", "title": "What you get",
      "bullets": ["Profile analysis", "Essay feedback", "No $300/hr fees"], "bg": "navy" },

    { "kind": "card", "card": "cta_endcard", "duration": 2.8, "bg": "navy" }
  ],
  "music": "edit/audio/bed.m4a"
}
```

```bash
PY=~/Developer/video-use/.venv/bin/python
$PY ~/Developer/video-use/engine/templates/product_video.py \
    launch_spec.json -o <videos_dir>/edit/final.mp4 \
    --brandkit counza --aspect 9:16 --music edit/audio/bed.m4a
```

`product_video` Ken-Burns-pushes the stills, hard-cuts between beats, builds
word-timed captions from each beat's `caption` line, interleaves the branded
cards, ducks the music under any beat audio, **burns subtitles LAST**, and
loudnorm-normalizes once. The `cta_endcard` text/keyword default from
`brand["cta"]` when omitted.

For a 16:9 YouTube cut, change `--aspect 16:9` (and re-export your slots at 16:9
so they don't get center-cropped).

---

## 5. Cheat sheet — which path for which shot

- **"Show the product working."** → screen-recording (real app) → `screen_recording` beat.
- **"A polished hero screen with motion."** → HyperFrames capture of the page (or Remotion of the real component) → `screen_recording` beat.
- **"A single static screen."** → Figma/screenshot PNG → `screenshot` beat (Ken-Burns).
- **"How the system works."** → Manim diagram → `screen_recording` beat.
- **"A headline number / a quote / a feature list / the CTA / the logo."** → the matching `mg_templates` card → `card` beat.
