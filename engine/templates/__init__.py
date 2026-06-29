"""Branded motion-graphics templates + a product/launch-video assembler.

This package packages the *recurring* motion-graphic cards (so every edit does
not rebuild them from scratch) and adds an assembler that turns a list of beats
— screenshots, screen-recordings, or generated cards — into a vertical (or 16:9)
product / launch video.

Submodules
----------
* :mod:`engine.templates.mg_templates` — parametrized, brand-driven motion
  cards rendered as animated MP4s (or RGBA PNG / WebM for overlay use). Each
  card eases in / holds / out exactly like ``engine.motiongfx.hook_card`` and is
  driven by a brand-kit dict from ``engine.brandkit``. Cards:

      - ``stat_card``       — big number + label reveal.
      - ``quote_card``      — centered statement, the clean-premium caption look.
      - ``feature_bullets`` — sequential bullet reveal (never parallel).
      - ``cta_endcard``     — comment-trigger CTA on a brand field.
      - ``logo_reveal``     — wordmark + tagline reveal.

* :mod:`engine.templates.product_video` — the assembler. Takes a spec (a list of
  beats, each a screenshot / screen-recording path OR a generated card, plus a
  caption line and a duration) and produces a real mp4: Ken-Burns push on static
  screenshots (via ``engine.transforms``), hard cuts, animated captions (via
  ``engine.captions_animated``), interleaved branded cards, and an optional music
  bed.

Both submodules reuse helpers from :mod:`engine.motiongfx` (brand-font
discovery, the PIL-frame-sequence -> ffmpeg encode, easing) rather than
re-implementing them, and **import nothing they modify**.

Workstream dependencies (``engine.effects`` for sfx / transitions, and
``engine.video_gen`` for generated b-roll) are *late-imported* inside
``product_video`` under ``try/except`` so the assembler runs standalone and
degrades gracefully when they are absent.

See ``engine/templates/INTEGRATION.md`` for how ``build_short.py`` / ``fanout.py``
call these, and ``engine/templates/source_to_video.md`` for the
source-code / Figma -> launch-video workflow.
"""

from __future__ import annotations

__all__ = ["mg_templates", "product_video"]
