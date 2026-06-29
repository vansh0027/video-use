#!/usr/bin/env python3
"""
engine/image_gen.py
===================

Generate a still image from a text prompt, with a **pluggable backend**.

The content engine needs images on demand — generated B-roll, illustrative
backgrounds, concept art for overlays. This module hides *where* the pixels
come from behind a single function:

    from image_gen import gen_image
    png = gen_image("a serene ivy league campus in autumn, cinematic",
                    out_dir="/tmp", width=1080, height=1080, seed=7)
    # -> "/tmp/imggen_<hash>_1080x1080_s7.png"  (or None on failure)

Backends
--------
* ``pollinations`` (DEFAULT, working) — free, no API key, returns a PNG over
  plain HTTP.  https://image.pollinations.ai/prompt/<prompt>?width=&height=&...
* ``muapi``  (cloud, STUBBED) — submit+poll REST flow, needs ``MUAPI_API_KEY``.
  Wired and documented but intentionally not exercised in CI (costs money).
* ``sdcpp``  (LOCAL, STUBBED) — stable-diffusion.cpp (Metal on Apple Silicon).
  Shells out to a prebuilt ``sd`` binary if ``CZ_SDCPP_BIN`` is set, otherwise
  raises ``NotImplementedError`` with setup instructions.

Backend selection precedence:
    explicit ``backend=`` arg  >  ``$CZ_IMAGE_GEN_BACKEND``  >  "pollinations".

Design notes
------------
* **Robust by construction**: every backend is wrapped so the public
  ``gen_image`` returns ``None`` on *any* failure (network, HTTP, decode,
  missing key, write error) instead of raising. Callers branch on ``None``.
* **Output is validated**: bytes are decoded + verified with PIL before the
  file is accepted, so a 200-with-HTML-error-page never masquerades as a PNG.
* stdlib + ``requests`` (already in the venv); ``Pillow`` for validation.
  No ``diffusers`` dependency — do not add one.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
import urllib.parse
from typing import Callable, Dict, Optional

try:
    import requests
except Exception:  # pragma: no cover - requests is expected in the venv
    requests = None  # type: ignore[assignment]

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow is expected in the venv
    Image = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_BACKEND = "pollinations"
_USER_AGENT = "CounzaEngine/1.0 (image_gen)"

# Pollinations model default. ``flux`` is markedly higher quality than the
# legacy default; ``enhance=true`` asks the service to expand the prompt for
# richer detail. Overridable per-call via gen_image(..., model=...) or globally
# via the CZ_POLLINATIONS_MODEL env var.
_DEFAULT_POLLINATIONS_MODEL = "flux"

# Network budgets (seconds). Pollinations renders server-side, so the read
# timeout is generous; the connect timeout stays short to fail fast offline.
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 120

# muapi poll budget.
_MUAPI_POLL_INTERVAL = 3
_MUAPI_POLL_TIMEOUT = 180


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _slug(prompt: str, width: int, height: int, seed: int) -> str:
    """Deterministic, filesystem-safe stem for a (prompt, size, seed) tuple."""
    digest = hashlib.sha1(
        f"{prompt}|{width}x{height}|{seed}".encode("utf-8")
    ).hexdigest()[:12]
    return f"imggen_{digest}_{width}x{height}_s{seed}"


def _validate_and_write(
    data: bytes,
    out_path: str,
    target_size: Optional[tuple[int, int]] = None,
) -> Optional[str]:
    """Decode ``data`` as an image, verify it, write it to ``out_path``.

    Returns the absolute path on success, or ``None`` if the bytes are not a
    valid/parseable image (e.g. an HTML error page returned with HTTP 200).
    Re-encodes to a clean PNG so the on-disk file is always a real PNG
    regardless of what content type the backend actually sent back.

    If ``target_size`` is given and the decoded image does not already match it,
    the image is resized (high-quality Lanczos) to exactly ``(W, H)``. Some
    free backends silently cap the long edge (e.g. Pollinations clamps to
    1024px), so this guarantees the on-disk PNG matches the caller's requested
    dimensions — which downstream compositing relies on.
    """
    if not data:
        print("[image_gen] empty response body")
        return None

    if Image is None:
        print("[image_gen] Pillow unavailable; cannot validate image bytes")
        return None

    try:
        # First pass: verify() detects truncated/corrupt data.
        Image.open(io.BytesIO(data)).verify()
        # verify() leaves the image unusable, so reopen to actually save.
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:
        snippet = data[:80].decode("utf-8", "replace").replace("\n", " ")
        print(f"[image_gen] response is not a valid image: {exc} | head={snippet!r}")
        return None

    if target_size is not None and img.size != target_size:
        print(
            f"[image_gen] backend returned {img.size[0]}x{img.size[1]}; "
            f"resizing to requested {target_size[0]}x{target_size[1]}"
        )
        try:
            img = img.resize(target_size, Image.LANCZOS)
        except Exception as exc:
            print(f"[image_gen] resize to {target_size} failed: {exc}")
            return None

    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        img.save(out_path, format="PNG")
    except Exception as exc:
        print(f"[image_gen] failed to write {out_path}: {exc}")
        return None

    return os.path.abspath(out_path)


# --------------------------------------------------------------------------- #
# Backend: pollinations (DEFAULT, working)
# --------------------------------------------------------------------------- #

def _backend_pollinations(
    prompt: str, out_path: str, width: int, height: int, seed: int,
    model: Optional[str] = None,
) -> Optional[str]:
    """Free, keyless image generation via image.pollinations.ai.

    GET https://image.pollinations.ai/prompt/<urlencoded prompt>
        ?width=W&height=H&nologo=true&seed=N&model=flux&enhance=true -> PNG bytes

    ``model`` selects the rendering model; if ``None`` it falls back to
    ``$CZ_POLLINATIONS_MODEL`` then ``_DEFAULT_POLLINATIONS_MODEL`` ("flux").
    ``enhance=true`` is always requested so the service expands the prompt for
    richer detail.
    """
    if requests is None:
        print("[image_gen] requests unavailable; cannot reach pollinations")
        return None

    chosen_model = (
        model or os.environ.get("CZ_POLLINATIONS_MODEL") or _DEFAULT_POLLINATIONS_MODEL
    )

    # Encode the prompt as a path segment (quote, not quote_plus: spaces -> %20).
    encoded = urllib.parse.quote(prompt, safe="")
    url = f"https://image.pollinations.ai/prompt/{encoded}"
    params = {
        "width": width,
        "height": height,
        "nologo": "true",
        "seed": seed,
        "model": chosen_model,
        "enhance": "true",
    }

    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": _USER_AGENT},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"[image_gen] pollinations request failed: {exc}")
        return None

    return _validate_and_write(resp.content, out_path, target_size=(width, height))


# --------------------------------------------------------------------------- #
# Backend: muapi (CLOUD, STUBBED — submit + poll)
# --------------------------------------------------------------------------- #

def _backend_muapi(
    prompt: str, out_path: str, width: int, height: int, seed: int
) -> Optional[str]:
    """Cloud generation via MUAPI (muapi.ai / "open generative ai").

    STUB STATUS
    -----------
    The submit+poll *shape* below follows the muapi.ai open-generative-ai
    pattern (authenticate with an ``x-api-key`` header, POST a job, then poll a
    status endpoint until the image URL is ready). Exact endpoint paths and the
    response field names vary by model/account, so confirm them against your
    muapi dashboard before relying on this in production. It is shipped wired
    but unexercised because each call costs credits.

    To use:
      1. ``export MUAPI_API_KEY=sk-...``
      2. Optionally override the base URL via ``MUAPI_BASE_URL`` and the model
         via ``MUAPI_MODEL``.
    """
    if requests is None:
        print("[image_gen] requests unavailable; cannot reach muapi")
        return None

    api_key = os.environ.get("MUAPI_API_KEY")
    if not api_key:
        print(
            "[image_gen] muapi backend selected but MUAPI_API_KEY is not set. "
            "Export MUAPI_API_KEY=... (see muapi.ai/open-generative-ai)."
        )
        return None

    base_url = os.environ.get("MUAPI_BASE_URL", "https://api.muapi.ai").rstrip("/")
    model = os.environ.get("MUAPI_MODEL", "flux-schnell")
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    # --- 1) submit the job -------------------------------------------------- #
    try:
        submit = requests.post(
            f"{base_url}/api/v1/images/generations",
            json={
                "model": model,
                "prompt": prompt,
                "width": width,
                "height": height,
                "seed": seed,
            },
            headers=headers,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        submit.raise_for_status()
        job = submit.json()
    except Exception as exc:
        print(f"[image_gen] muapi submit failed: {exc}")
        return None

    # Field names below are best-effort; adjust to your account's schema.
    job_id = job.get("id") or job.get("request_id") or job.get("task_id")
    if not job_id:
        print(f"[image_gen] muapi submit returned no job id: {job!r}")
        return None

    # --- 2) poll until ready ------------------------------------------------ #
    image_url: Optional[str] = None
    deadline = time.monotonic() + _MUAPI_POLL_TIMEOUT
    while time.monotonic() < deadline:
        try:
            poll = requests.get(
                f"{base_url}/api/v1/images/generations/{job_id}",
                headers=headers,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            poll.raise_for_status()
            status = poll.json()
        except Exception as exc:
            print(f"[image_gen] muapi poll failed: {exc}")
            return None

        state = (status.get("status") or status.get("state") or "").lower()
        if state in {"succeeded", "success", "completed", "done"}:
            outputs = status.get("outputs") or status.get("images") or []
            if outputs:
                first = outputs[0]
                image_url = first if isinstance(first, str) else first.get("url")
            image_url = image_url or status.get("image_url") or status.get("url")
            break
        if state in {"failed", "error", "canceled", "cancelled"}:
            print(f"[image_gen] muapi job {job_id} failed: {status!r}")
            return None

        time.sleep(_MUAPI_POLL_INTERVAL)

    if not image_url:
        print(f"[image_gen] muapi job {job_id} did not produce an image in time")
        return None

    # --- 3) download the result -------------------------------------------- #
    try:
        img_resp = requests.get(
            image_url,
            headers={"User-Agent": _USER_AGENT},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        img_resp.raise_for_status()
    except Exception as exc:
        print(f"[image_gen] muapi result download failed: {exc}")
        return None

    return _validate_and_write(img_resp.content, out_path, target_size=(width, height))


# --------------------------------------------------------------------------- #
# Backend: sdcpp (LOCAL, STUBBED — stable-diffusion.cpp)
# --------------------------------------------------------------------------- #

def _backend_sdcpp(
    prompt: str, out_path: str, width: int, height: int, seed: int
) -> Optional[str]:
    """Local, open-source generation via stable-diffusion.cpp.

    STUB STATUS
    -----------
    stable-diffusion.cpp (github.com/anil-matcha/open-generative-ai) runs fully
    offline and uses **Metal** on Apple Silicon. It needs (a) a compiled ``sd``
    binary and (b) a model checkpoint downloaded separately (e.g. Z-Image
    Turbo, DreamShaper) — neither ships with this repo.

    Behaviour:
      * If ``CZ_SDCPP_BIN`` points at an ``sd`` binary, shell out to it.
        Optionally set ``CZ_SDCPP_MODEL`` to the checkpoint path (else we trust
        the binary's own default/config).
      * Otherwise raise ``NotImplementedError`` with setup instructions. (The
        public ``gen_image`` wrapper converts that into a clean ``None``.)
    """
    sd_bin = os.environ.get("CZ_SDCPP_BIN")
    if not sd_bin:
        raise NotImplementedError(
            "sdcpp backend requires a stable-diffusion.cpp binary. Build it from "
            "github.com/anil-matcha/open-generative-ai (Metal-enabled on Apple "
            "Silicon), download a model checkpoint (e.g. Z-Image Turbo or "
            "DreamShaper), then set CZ_SDCPP_BIN=/path/to/sd "
            "(and optionally CZ_SDCPP_MODEL=/path/to/model.safetensors)."
        )

    import shutil
    import subprocess

    if not (os.path.isfile(sd_bin) and os.access(sd_bin, os.X_OK)) and not shutil.which(sd_bin):
        print(f"[image_gen] CZ_SDCPP_BIN is not an executable: {sd_bin!r}")
        return None

    cmd = [
        sd_bin,
        "-p", prompt,
        "-W", str(width),
        "-H", str(height),
        "-s", str(seed),
        "-o", out_path,
    ]
    model = os.environ.get("CZ_SDCPP_MODEL")
    if model:
        cmd += ["-m", model]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except Exception as exc:
        print(f"[image_gen] sdcpp invocation failed: {exc}")
        return None

    if proc.returncode != 0:
        print(
            f"[image_gen] sdcpp exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
        )
        return None

    if not os.path.isfile(out_path):
        print(f"[image_gen] sdcpp reported success but {out_path} is missing")
        return None

    # Re-validate/normalize whatever the binary wrote.
    try:
        with open(out_path, "rb") as fh:
            data = fh.read()
    except Exception as exc:
        print(f"[image_gen] could not read sdcpp output {out_path}: {exc}")
        return None
    return _validate_and_write(data, out_path, target_size=(width, height))


# --------------------------------------------------------------------------- #
# Registry + public API
# --------------------------------------------------------------------------- #

_BACKENDS: Dict[str, Callable[[str, str, int, int, int], Optional[str]]] = {
    "pollinations": _backend_pollinations,
    "muapi": _backend_muapi,
    "sdcpp": _backend_sdcpp,
}


def gen_image(
    prompt: str,
    out_dir: str,
    width: int = 1080,
    height: int = 1080,
    seed: int = 0,
    backend: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[str]:
    """Generate an image from ``prompt`` and return the absolute PNG path.

    Args:
        prompt: Text description of the image.
        out_dir: Directory to write the PNG into (created if missing).
        width:  Output width in pixels (default 1080).
        height: Output height in pixels (default 1080).
        seed:   Generation seed for reproducibility (default 0).
        backend: One of ``"pollinations"``, ``"muapi"``, ``"sdcpp"``. If
            ``None``, falls back to ``$CZ_IMAGE_GEN_BACKEND`` then to
            ``"pollinations"``.
        model:  Optional model override for the pollinations backend (e.g.
            ``"flux"``). If ``None``, that backend falls back to
            ``$CZ_POLLINATIONS_MODEL`` then to ``"flux"``. Ignored by the muapi
            and sdcpp backends (they read their own model env vars).

    Returns:
        Absolute path to a written, validated PNG, or ``None`` on any failure.
        This function never raises for an expected runtime problem — network
        errors, missing API keys, decode failures, and unconfigured local
        backends all surface as ``None`` (with a diagnostic printed).
    """
    if not prompt or not isinstance(prompt, str):
        print(f"[image_gen] prompt must be a non-empty string, got {prompt!r}")
        return None

    try:
        width = int(width)
        height = int(height)
        seed = int(seed)
    except (TypeError, ValueError):
        print(f"[image_gen] width/height/seed must be ints: {width!r} {height!r} {seed!r}")
        return None

    if width <= 0 or height <= 0:
        print(f"[image_gen] width/height must be positive, got {width}x{height}")
        return None

    name = (backend or os.environ.get("CZ_IMAGE_GEN_BACKEND") or DEFAULT_BACKEND).lower()
    fn = _BACKENDS.get(name)
    if fn is None:
        print(
            f"[image_gen] unknown backend {name!r}; "
            f"choose one of: {', '.join(sorted(_BACKENDS))}"
        )
        return None

    out_path = os.path.join(out_dir, _slug(prompt, width, height, seed) + ".png")

    try:
        if name == "pollinations":
            # Only this backend accepts a model override; the others read their
            # own model env vars, so keep their uniform 5-arg signature.
            return fn(prompt, out_path, width, height, seed, model)
        return fn(prompt, out_path, width, height, seed)
    except NotImplementedError as exc:
        # Stub backends raise this when unconfigured; treat as a clean failure.
        print(f"[image_gen] backend {name!r} not available: {exc}")
        return None
    except Exception as exc:  # last-resort guard: never propagate
        print(f"[image_gen] backend {name!r} crashed: {exc}")
        return None


# --------------------------------------------------------------------------- #
# CLI / self-test
# --------------------------------------------------------------------------- #

def _selftest(out_dir: str) -> int:
    """Generate one image via the default (pollinations) backend and report."""
    prompt = "a serene ivy league university campus in autumn, cinematic"
    print(f"[selftest] backend=pollinations prompt={prompt!r}")
    path = gen_image(prompt, out_dir, width=1024, height=1024, seed=42,
                     backend="pollinations")
    if not path:
        print("[selftest] FAILED: gen_image returned None")
        return 1

    size_bytes = os.path.getsize(path)
    if Image is not None:
        with Image.open(path) as im:
            dims = im.size
            fmt = im.format
        print(f"[selftest] OK  path={path}")
        print(f"[selftest]     dims={dims[0]}x{dims[1]} format={fmt} bytes={size_bytes}")
    else:
        print(f"[selftest] OK  path={path} bytes={size_bytes} (PIL unavailable for dims)")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate an image from a text prompt (pluggable backend)."
    )
    parser.add_argument("prompt", nargs="?", help="Text prompt. Omit to run the self-test.")
    parser.add_argument("-o", "--out-dir", default=".", help="Output directory (default: .)")
    parser.add_argument("-W", "--width", type=int, default=1080)
    parser.add_argument("-H", "--height", type=int, default=1080)
    parser.add_argument("-s", "--seed", type=int, default=0)
    parser.add_argument(
        "-b", "--backend",
        choices=sorted(_BACKENDS),
        default=None,
        help="Backend (default: $CZ_IMAGE_GEN_BACKEND or pollinations).",
    )
    parser.add_argument("--selftest", action="store_true", help="Run the built-in self-test.")
    args = parser.parse_args()

    if args.selftest or not args.prompt:
        raise SystemExit(_selftest(args.out_dir))

    result = gen_image(
        args.prompt, args.out_dir,
        width=args.width, height=args.height, seed=args.seed, backend=args.backend,
    )
    if result:
        print(result)
        raise SystemExit(0)
    raise SystemExit(1)
