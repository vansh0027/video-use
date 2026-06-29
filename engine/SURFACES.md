# Surfaces: face reel vs product video (added-text policy)

The engine produces two surfaces with **different on-screen-text rules**. This is
a hard product rule for Counza, not a style preference.

## Face reel (founder talking-head)
Profiles: `founder_edtech`, `founder_edtech_band` (`"added_text": "spoken_only"`).

- **On-screen text = spoken-word captions ONLY.** The active-word/karaoke caption
  of what the person is actually saying. Nothing else.
- **NOT allowed:** hook cards, stat/quote/feature/CTA cards, the Counza wordmark,
  lower-third name banners, titles, labels, designed banners, kinetic CTA endcards.
- The CTA is **spoken** or lives in the **post caption** — never an on-screen card.
- Allowed: grade, punch-in, spoken captions, music/sfx, and (sparingly, ~10–25%)
  generated/stock B-roll cutaways. B-roll is *visual*, not *text* — it's fine, but
  use it lightly on high-trust founder moments.

Rationale: added labels read as patronizing/cluttered; the founder's face and
words carry the trust. See memory `feedback-no-added-text`.

## Product / launch video
Built via `engine/templates/product_video.py`. **Added text IS allowed** —
`mg_templates` cards (hook/stat/quote/feature/CTA/logo), wordmarks, branded
banners are the whole point. Use the brand kit freely.

## Enforcement
When wiring `engine/styles` + `engine/templates` cards into `build_short.py`:
read the profile's `added_text` field. If `"spoken_only"`, the reel renderer must
**refuse to attach** any `hook_card`, `cta_endcard`, `lower_third`, `cards[]`, or
wordmark overlay — only the spoken-word caption track. Cards are only assembled on
the product-video path.
