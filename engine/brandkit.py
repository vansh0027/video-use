"""Brand-kit loader for the Counza content engine.

Loads structured brand kits from engine/brandkits/<name>.json and provides
colour conversion from web hex (#RRGGBB) to the ASS subtitle format used by
libass (&HAABBGGRR).

This module is intentionally dependency-free (stdlib only) so it can be
imported by any helper in the pipeline.

Usage:
    import brandkit
    kit = brandkit.load_brandkit("counza")
    kit["colors"]["orange"]            # -> "#e46e24"
    brandkit.hex_to_ass("#e46e24")     # -> "&H00246EE4"
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

# Directory that holds the <name>.json brand-kit files, resolved relative to
# this file so the loader works regardless of the caller's cwd.
BRANDKITS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brandkits")


def _normalize_hex(value: str) -> str:
    """Return a 6-digit uppercase hex string (no leading '#').

    Accepts '#rgb', 'rgb', '#rrggbb', 'rrggbb' in any case. Raises
    ValueError on anything that is not a valid hex colour.
    """
    if not isinstance(value, str):
        raise ValueError(f"hex colour must be a string, got {type(value).__name__}")

    h = value.strip().lstrip("#").strip()

    # Expand shorthand #rgb -> #rrggbb.
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)

    if len(h) != 6:
        raise ValueError(f"invalid hex colour {value!r}: expected 3 or 6 hex digits")

    try:
        int(h, 16)
    except ValueError as exc:
        raise ValueError(f"invalid hex colour {value!r}: not hexadecimal") from exc

    return h.upper()


def hex_to_ass(hex_color: str, alpha: int = 0) -> str:
    """Convert a web hex colour (#RRGGBB) to an ASS colour string.

    ASS / libass encodes colours as ``&HAABBGGRR`` where the byte order is
    reversed relative to web hex and the leading pair is *alpha* (00 = fully
    opaque, FF = fully transparent).

    Examples:
        hex_to_ass("#FFFFFF")  -> "&H00FFFFFF"   (white, opaque)
        hex_to_ass("#e46e24")  -> "&H00246EE4"   (Counza orange)
        hex_to_ass("#003f7d")  -> "&H007D3F00"   (Counza navy)

    Args:
        hex_color: A web hex colour, with or without a leading '#'.
        alpha: ASS alpha byte, 0 (opaque) .. 255 (transparent).
    """
    if not isinstance(alpha, int) or not (0 <= alpha <= 255):
        raise ValueError(f"alpha must be an int in 0..255, got {alpha!r}")

    h = _normalize_hex(hex_color)
    rr, gg, bb = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{bb}{gg}{rr}"


def load_brandkit(name: str) -> Dict[str, Any]:
    """Load a brand kit by name from engine/brandkits/<name>.json.

    The returned dict is the parsed JSON with one convenience addition: an
    ``ass`` block mapping every colour in ``colors`` to its ASS-encoded
    equivalent (opaque), so downstream subtitle code does not have to convert
    by hand.

    Args:
        name: Brand-kit name (e.g. "counza", "founder"). A trailing
            ".json" is tolerated.

    Raises:
        FileNotFoundError: if no matching brand-kit file exists.
        ValueError: if the file is not valid JSON or is missing required keys.
    """
    if not name or not isinstance(name, str):
        raise ValueError(f"brand-kit name must be a non-empty string, got {name!r}")

    stem = name[:-5] if name.endswith(".json") else name
    path = os.path.join(BRANDKITS_DIR, f"{stem}.json")

    if not os.path.isfile(path):
        available = _available_brandkits()
        hint = f" Available: {', '.join(available)}." if available else ""
        raise FileNotFoundError(f"brand kit {stem!r} not found at {path}.{hint}")

    with open(path, "r", encoding="utf-8") as fh:
        try:
            kit: Dict[str, Any] = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"brand kit {stem!r} at {path} is not valid JSON: {exc}") from exc

    if "colors" not in kit or not isinstance(kit["colors"], dict):
        raise ValueError(f"brand kit {stem!r} is missing a 'colors' object")

    # Precompute ASS-encoded colours for convenience (non-destructive: keep
    # the original web-hex 'colors' intact).
    kit["ass"] = {key: hex_to_ass(val) for key, val in kit["colors"].items()}

    return kit


def _available_brandkits() -> list[str]:
    """Return the sorted stems of available brand-kit JSON files."""
    if not os.path.isdir(BRANDKITS_DIR):
        return []
    return sorted(
        f[:-5] for f in os.listdir(BRANDKITS_DIR) if f.endswith(".json")
    )


if __name__ == "__main__":
    # Tiny smoke test / CLI for eyeballing a kit.
    import sys

    kit_name = sys.argv[1] if len(sys.argv) > 1 else "counza"
    loaded = load_brandkit(kit_name)
    print(f"Loaded brand kit: {loaded.get('name', kit_name)}")
    for key, hexval in loaded["colors"].items():
        print(f"  {key:12} {hexval}  ->  {loaded['ass'][key]}")
