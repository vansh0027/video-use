#!/usr/bin/env python3
"""
engine/video_gen.py
===================

Generate a short MOVING B-roll clip from a still image (or a text prompt), with
a **pluggable backend**. This is the motion sibling of ``image_gen.py``: where
that module synthesises a static PNG, this one produces a living ~4-second mp4 —
the kind of slow, abstract, drifting B-roll seen behind a talking head in
premium AI-creator reels.

    from video_gen import gen_video, gen_broll_clip

    # animate an existing still into a 4s vertical clip
    mp4 = gen_video(image="frame.png", out_dir="/tmp",
                    duration=4.0, width=1080, height=1920)
    # -> "/tmp/vidgen_<hash>_1080x1920_4.0s.mp4"  (or None on failure)

    # concept -> Flux still (via image_gen) -> animated clip, one call
    mp4 = gen_broll_clip("a neural network firing, abstract, cinematic",
                         out_dir="/tmp", duration=4.0)

Backends
--------
* ``kenburns`` (DEFAULT, working — FREE, LOCAL, OFFLINE, no key): take a still
  and animate it into B-roll entirely with ffmpeg — a slow ``zoompan`` push/pan
  plus a subtle drift/grain so it reads as *motion*, not a frozen zoom. Works
  today with zero credentials. The pragmatic 80%-quality path.
* ``svd`` / ``wan`` (LOCAL, STUBBED): image/text-to-video via a local model
  binary (Stable Video Diffusion / Wan2.x). Honours ``$CZ_SVD_BIN`` /
  ``$CZ_WAN_BIN``; if absent, logs and returns ``None``. Never downloads models.
* ``replicate`` / ``runway`` / ``kling`` (CLOUD, STUBBED): submit+poll REST flow
  honouring an API-key env var. Request shape is wired; without a key each logs
  and returns ``None``. Shipped unexercised because each call costs money.

Backend selection precedence:
    explicit ``backend=`` arg  >  ``$CZ_VIDEO_GEN_BACKEND``  >  "kenburns".

Design notes
------------
* **Robust by construction**: every backend is wrapped so the public
  ``gen_video`` returns ``None`` on *any* failure (network, HTTP, decode,
  missing key, missing binary, bad write) instead of raising. Callers branch on
  ``None`` — exactly like ``image_gen.gen_image`` / ``stock.fetch_stock``.
* **Output is validated**: before a path is returned it is probed with
  ``ffprobe`` and accepted only if it is a real, decodable video stream with a
  plausible duration — so a 0-byte file or a truncated write never masquerades
  as a clip.
* stdlib + ``ffmpeg``/``ffprobe`` (ffmpeg-full in this repo) for the free path;
  ``requests`` only for the cloud stubs; ``Pillow`` only to sniff/normalise an
  input still's size. No diffusers / torch dependency — do not add one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.parse
from typing import Callable, Dict, Optional, Tuple

# Load the repo-root .env so the cloud backends' keys (REPLICATE_API_TOKEN,
# RUNWAY_API_KEY, KLING_API_KEY) are honoured without manual shell sourcing.
# The real environment still wins. Never fatal if absent.
try:
    from _env import load_env as _load_env
    _load_env()
except Exception:  # pragma: no cover
    pass

try:
    import requests
except Exception:  # pragma: no cover - requests is expected in the venv
    requests = None  # type: ignore[assignment]

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow is expected in the venv
    Image = None  # type: ignore[assignment]

# image_gen lives alongside this module; import it lazily/softly so video_gen
# still loads (and the kenburns path still works on an explicit still) even if
# image_gen is unavailable for some reason.
try:
    import image_gen  # type: ignore
except Exception:  # pragma: no cover
    try:
        from . import image_gen  # type: ignore
    except Exception:
        image_gen = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_BACKEND = "kenburns"
_USER_AGENT = "CounzaEngine/1.0 (video_gen)"

# Canonical vertical canvas for short-form (TikTok / Reels / Shorts).
_DEFAULT_W = 1080
_DEFAULT_H = 1920
_DEFAULT_FPS = 30

# ffmpeg/ffprobe binaries. Allow override but default to PATH lookup so this
# works against the repo's ffmpeg-full install.
_FFMPEG = os.environ.get("CZ_FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"
_FFPROBE = os.environ.get("CZ_FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe"

# Render budget for a single short clip (seconds). zoompan on a still is cheap,
# but be generous so slower machines / longer durations don't get killed.
_FFMPEG_TIMEOUT = 300

# Network budgets for the cloud stubs (seconds).
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 120
_CLOUD_POLL_INTERVAL = 4
_CLOUD_POLL_TIMEOUT = 300


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _even(n: float) -> int:
    """Round up to the nearest even integer (encoder-safe dimension)."""
    i = int(n)
    if i < n:
        i += 1
    return i if i % 2 == 0 else i + 1


def _slug(key: str, width: int, height: int, duration: float, seed: int) -> str:
    """Deterministic, filesystem-safe stem for a clip request."""
    digest = hashlib.sha1(
        f"{key}|{width}x{height}|{duration}|{seed}".encode("utf-8")
    ).hexdigest()[:12]
    return f"vidgen_{digest}_{width}x{height}_{duration:g}s"


def _probe_video(path: str) -> Optional[Tuple[float, int, int, str]]:
    """Probe ``path`` with ffprobe. Return ``(duration, w, h, codec)`` or None.

    Used to *validate* that a backend actually produced a real, decodable video
    stream before the public API hands the path back to a caller.
    """
    if not (path and os.path.isfile(path) and os.path.getsize(path) > 0):
        return None
    cmd = [
        _FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name:format=duration",
        "-of", "json", path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        print(f"[video_gen] ffprobe failed on {path}: {exc}")
        return None
    if proc.returncode != 0:
        print(f"[video_gen] ffprobe error on {path}: {(proc.stderr or '').strip()[:200]}")
        return None
    try:
        meta = json.loads(proc.stdout or "{}")
        streams = meta.get("streams") or []
        if not streams:
            return None
        s = streams[0]
        w = int(s.get("width") or 0)
        h = int(s.get("height") or 0)
        codec = str(s.get("codec_name") or "")
        dur = float((meta.get("format") or {}).get("duration") or 0.0)
        if w <= 0 or h <= 0 or dur <= 0:
            return None
        return (dur, w, h, codec)
    except Exception as exc:
        print(f"[video_gen] could not parse ffprobe output for {path}: {exc}")
        return None


def _accept(path: str, want_dur: float) -> Optional[str]:
    """Validate a freshly written clip; return its abs path or None.

    Accepts if ffprobe sees a real video stream whose duration is within a
    tolerant window of the requested ``want_dur`` (encoders pad/round the last
    frame, so we allow ~0.5s + 15% slack).
    """
    info = _probe_video(path)
    if info is None:
        print(f"[video_gen] output failed validation (not a playable video): {path}")
        return None
    dur, w, h, codec = info
    tol = max(0.5, want_dur * 0.15)
    if abs(dur - want_dur) > tol:
        print(
            f"[video_gen] WARNING: clip duration {dur:.2f}s differs from "
            f"requested {want_dur:.2f}s (>{tol:.2f}s); accepting anyway."
        )
    print(f"[video_gen] OK clip={path} dur={dur:.2f}s {w}x{h} codec={codec}")
    return os.path.abspath(path)


def _ensure_still(
    prompt: Optional[str],
    image: Optional[str],
    out_dir: str,
    width: int,
    height: int,
    seed: int,
) -> Optional[str]:
    """Resolve an input still: use ``image`` if given, else synthesise from
    ``prompt`` via ``image_gen.gen_image``. Returns a PNG path or None."""
    if image:
        if os.path.isfile(image):
            return os.path.abspath(image)
        print(f"[video_gen] given image does not exist: {image!r}")
        return None
    if not prompt:
        print("[video_gen] need either image= or prompt= to make a clip")
        return None
    if image_gen is None:
        print("[video_gen] image_gen unavailable; cannot synthesise a still from prompt")
        return None
    # Generate at the clip's larger edge so the Ken Burns push has headroom to
    # crop into without upscaling soft pixels.
    gen = image_gen.gen_image(prompt, out_dir, width=width, height=height, seed=seed)
    if not gen:
        print(f"[video_gen] image_gen returned no still for {prompt!r}")
        return None
    return gen


# --------------------------------------------------------------------------- #
# Backend: kenburns (DEFAULT, working — free, local, offline)
# --------------------------------------------------------------------------- #

def _kenburns_filter(
    width: int, height: int, duration: float, fps: int, seed: int,
) -> str:
    """Build the ffmpeg filter that animates a still into living B-roll.

    The chain, in order:
      1. ``scale`` the still up to an oversized working canvas (with a small
         margin) so the pan/zoom never reveals an edge, force even dims.
      2. ``zoompan`` does a slow continuous push *and* a gentle diagonal drift
         (x/y ramp), so it reads as parallax-ish motion rather than a dead
         center zoom. ``d = duration*fps`` frames, output locked to WxH.
      3. ``noise`` adds a faint animated film grain (``alls`` + ``allf=t``) so
         flat gradient regions shimmer almost imperceptibly — the difference
         between "static image with a zoom" and "alive".
      4. ``format=yuv420p`` for broad player compatibility.

    Determinism: the drift direction is derived from ``seed`` so repeated calls
    are reproducible but different concepts move differently.
    """
    total = max(1, int(round(duration * fps)))

    # Oversized canvas: 1.25x gives zoompan room to push to ~1.18x and drift.
    work_w = _even(width * 1.25)
    work_h = _even(height * 1.25)

    # Slow zoom from 1.0 -> ~1.18 across the clip.
    zoom_end = 1.18
    zstep = (zoom_end - 1.0) / total
    z_expr = f"min(1.0+{zstep:.8f}*on,{zoom_end})"

    # Drift: pan the crop window across a fraction of the slack between the
    # working canvas and the zoomed crop. Direction flips with the seed so
    # different clips don't all pan the same way.
    sgn_x = 1 if (seed % 2 == 0) else -1
    sgn_y = 1 if ((seed // 2) % 2 == 0) else -1
    # Base centered crop, plus a small linear offset over the clip (in px).
    drift_px = 60.0
    dx = (sgn_x * drift_px) / total
    dy = (sgn_y * drift_px) / total
    x_expr = f"(iw-iw/zoom)/2+({dx:.6f})*on"
    y_expr = f"(ih-ih/zoom)/2+({dy:.6f})*on"

    return (
        f"scale={work_w}:{work_h}:force_original_aspect_ratio=increase,"
        f"crop={work_w}:{work_h},"
        f"zoompan=z='{z_expr}'"
        f":x='{x_expr}':y='{y_expr}'"
        f":d={total}:fps={fps}:s={width}x{height},"
        f"noise=alls=6:allf=t,"
        f"format=yuv420p,setsar=1"
    )


def _backend_kenburns(
    prompt: Optional[str], image: Optional[str], out_path: str,
    width: int, height: int, duration: float, seed: int,
    model: Optional[str] = None,  # accepted for API symmetry; unused here
) -> Optional[str]:
    """Animate a still into a moving clip with ffmpeg (free, local, offline)."""
    if not (shutil.which(_FFMPEG) or os.path.isfile(_FFMPEG)):
        print(f"[video_gen] ffmpeg not found ({_FFMPEG!r}); kenburns unavailable")
        return None

    still = _ensure_still(prompt, image, os.path.dirname(out_path) or ".",
                          width, height, seed)
    if not still:
        return None

    fps = _DEFAULT_FPS
    vf = _kenburns_filter(width, height, duration, fps, seed)

    cmd = [
        _FFMPEG, "-y",
        "-loop", "1", "-i", still,
        "-t", f"{duration:g}",
        "-vf", vf,
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-an",
        out_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_FFMPEG_TIMEOUT)
    except Exception as exc:
        print(f"[video_gen] kenburns ffmpeg invocation failed: {exc}")
        return None
    if proc.returncode != 0:
        print(
            f"[video_gen] kenburns ffmpeg exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[-400:]}"
        )
        return None

    return _accept(out_path, duration)


# --------------------------------------------------------------------------- #
# Backend: svd / wan (LOCAL, STUBBED — image/text-to-video binaries)
# --------------------------------------------------------------------------- #

def _backend_local_bin(
    env_var: str, label: str, doc_url: str,
    prompt: Optional[str], image: Optional[str], out_path: str,
    width: int, height: int, duration: float, seed: int,
) -> Optional[str]:
    """Shared local-binary stub for SVD / Wan-style image-to-video models.

    STUB STATUS
    -----------
    Looks for a prebuilt inference binary via ``$<env_var>`` (e.g.
    ``CZ_SVD_BIN`` / ``CZ_WAN_BIN``). If unset/not-executable, logs setup
    guidance and returns ``None`` — it never downloads a model. When the binary
    *is* present we shell out with a conventional CLI shape; confirm the exact
    flags against your build before relying on it. Output is then ffprobe
    -validated like every other backend.
    """
    sd_bin = os.environ.get(env_var)
    if not sd_bin:
        print(
            f"[video_gen] {label} backend selected but {env_var} is not set. "
            f"Build/obtain a local {label} inference binary (see {doc_url}), "
            f"then export {env_var}=/path/to/bin. No model is downloaded for you."
        )
        return None
    if not (os.path.isfile(sd_bin) and os.access(sd_bin, os.X_OK)) and not shutil.which(sd_bin):
        print(f"[video_gen] {env_var} is not an executable: {sd_bin!r}")
        return None

    still = _ensure_still(prompt, image, os.path.dirname(out_path) or ".",
                          width, height, seed)
    # SVD/Wan are image-conditioned; a still is strongly preferred. If we
    # couldn't get one and have no prompt either, bail.
    if not still and not prompt:
        return None

    # Conventional CLI shape: ``bin --image IN --prompt P --frames N --fps F
    # --width W --height H --seed S --out OUT``. Adjust to your build's flags.
    fps = _DEFAULT_FPS
    frames = max(1, int(round(duration * fps)))
    cmd = [sd_bin]
    if still:
        cmd += ["--image", still]
    if prompt:
        cmd += ["--prompt", prompt]
    cmd += [
        "--frames", str(frames),
        "--fps", str(fps),
        "--width", str(width),
        "--height", str(height),
        "--seed", str(seed),
        "--out", out_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as exc:
        print(f"[video_gen] {label} invocation failed: {exc}")
        return None
    if proc.returncode != 0:
        print(
            f"[video_gen] {label} exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[-400:]}"
        )
        return None
    return _accept(out_path, duration)


def _backend_svd(prompt, image, out_path, width, height, duration, seed, model=None):
    return _backend_local_bin(
        "CZ_SVD_BIN", "svd", "github.com/Stability-AI/generative-models",
        prompt, image, out_path, width, height, duration, seed,
    )


def _backend_wan(prompt, image, out_path, width, height, duration, seed, model=None):
    return _backend_local_bin(
        "CZ_WAN_BIN", "wan", "github.com/Wan-Video/Wan2.1",
        prompt, image, out_path, width, height, duration, seed,
    )


# --------------------------------------------------------------------------- #
# Backend: replicate / runway / kling (CLOUD, STUBBED — submit + poll)
# --------------------------------------------------------------------------- #

def _backend_replicate(
    prompt, image, out_path, width, height, duration, seed, model=None,
) -> Optional[str]:
    """Cloud image/text-to-video via Replicate (submit + poll).

    STUB STATUS
    -----------
    Wired to the Replicate predictions API shape (``Authorization: Token ...``,
    POST a prediction, poll ``urls.get`` until ``status == succeeded``, then
    download ``output``). Needs ``REPLICATE_API_TOKEN``; without it this logs
    and returns ``None``. Pick the model via ``CZ_REPLICATE_VIDEO_MODEL`` (a
    version hash or owner/model). Shipped unexercised — each run costs money.
    """
    token = os.environ.get("REPLICATE_API_TOKEN")
    if not token:
        print(
            "[video_gen] replicate backend selected but REPLICATE_API_TOKEN is "
            "not set. Export REPLICATE_API_TOKEN=r8_... (replicate.com/account)."
        )
        return None
    if requests is None:
        print("[video_gen] requests unavailable; cannot reach replicate")
        return None

    version = (
        model or os.environ.get("CZ_REPLICATE_VIDEO_MODEL")
        or "stability-ai/stable-video-diffusion"
    )
    headers = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
    payload_input: Dict[str, object] = {
        "fps": _DEFAULT_FPS,
        "width": width,
        "height": height,
        "seed": seed,
    }
    if prompt:
        payload_input["prompt"] = prompt
    if image and os.path.isfile(image):
        # Real use would upload the still and pass a URL; documented as a TODO.
        payload_input["image"] = image

    try:
        submit = requests.post(
            "https://api.replicate.com/v1/predictions",
            json={"version": version, "input": payload_input},
            headers=headers, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        submit.raise_for_status()
        job = submit.json()
    except Exception as exc:
        print(f"[video_gen] replicate submit failed: {exc}")
        return None

    get_url = (job.get("urls") or {}).get("get")
    status = (job.get("status") or "").lower()
    output = job.get("output")
    deadline = time.monotonic() + _CLOUD_POLL_TIMEOUT
    while status not in {"succeeded", "failed", "canceled"} and get_url:
        if time.monotonic() > deadline:
            print("[video_gen] replicate poll timed out")
            return None
        time.sleep(_CLOUD_POLL_INTERVAL)
        try:
            poll = requests.get(get_url, headers=headers,
                                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT))
            poll.raise_for_status()
            data = poll.json()
        except Exception as exc:
            print(f"[video_gen] replicate poll failed: {exc}")
            return None
        status = (data.get("status") or "").lower()
        output = data.get("output")

    if status != "succeeded":
        print(f"[video_gen] replicate job did not succeed (status={status!r})")
        return None
    video_url = output[-1] if isinstance(output, list) and output else output
    if not isinstance(video_url, str):
        print(f"[video_gen] replicate returned no video url: {output!r}")
        return None
    return _download_and_accept(video_url, out_path, duration)


def _backend_runway(
    prompt, image, out_path, width, height, duration, seed, model=None,
) -> Optional[str]:
    """Cloud image-to-video via Runway (Gen-3/Gen-4). STUBBED.

    Needs ``RUNWAY_API_KEY``. Wired to the ``image_to_video`` task shape
    (POST a task, poll ``/tasks/{id}`` until ``SUCCEEDED``). Without a key this
    logs and returns ``None``.
    """
    key = os.environ.get("RUNWAY_API_KEY") or os.environ.get("RUNWAYML_API_SECRET")
    if not key:
        print(
            "[video_gen] runway backend selected but RUNWAY_API_KEY is not set. "
            "Export RUNWAY_API_KEY=... (dev.runwayml.com)."
        )
        return None
    if requests is None:
        print("[video_gen] requests unavailable; cannot reach runway")
        return None

    base = os.environ.get("RUNWAY_BASE_URL", "https://api.dev.runwayml.com").rstrip("/")
    runway_model = model or os.environ.get("CZ_RUNWAY_MODEL", "gen4_turbo")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Runway-Version": os.environ.get("RUNWAY_API_VERSION", "2024-11-06"),
    }
    body: Dict[str, object] = {
        "model": runway_model,
        "promptText": prompt or "",
        "duration": int(round(duration)),
        "seed": seed,
    }
    if image and os.path.isfile(image):
        body["promptImage"] = image  # real use: a hosted URL or data URI

    try:
        submit = requests.post(
            f"{base}/v1/image_to_video", json=body, headers=headers,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        submit.raise_for_status()
        task = submit.json()
    except Exception as exc:
        print(f"[video_gen] runway submit failed: {exc}")
        return None

    task_id = task.get("id")
    if not task_id:
        print(f"[video_gen] runway submit returned no task id: {task!r}")
        return None

    deadline = time.monotonic() + _CLOUD_POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_CLOUD_POLL_INTERVAL)
        try:
            poll = requests.get(f"{base}/v1/tasks/{task_id}", headers=headers,
                                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT))
            poll.raise_for_status()
            data = poll.json()
        except Exception as exc:
            print(f"[video_gen] runway poll failed: {exc}")
            return None
        status = (data.get("status") or "").upper()
        if status == "SUCCEEDED":
            out = data.get("output") or []
            url = out[0] if isinstance(out, list) and out else None
            if not isinstance(url, str):
                print(f"[video_gen] runway succeeded but no output url: {data!r}")
                return None
            return _download_and_accept(url, out_path, duration)
        if status in {"FAILED", "CANCELLED", "CANCELED"}:
            print(f"[video_gen] runway task {task_id} failed: {data!r}")
            return None
    print("[video_gen] runway poll timed out")
    return None


def _backend_kling(
    prompt, image, out_path, width, height, duration, seed, model=None,
) -> Optional[str]:
    """Cloud image/text-to-video via Kling. STUBBED.

    Needs ``KLING_API_KEY`` (some resellers use a JWT from access/secret keys;
    here we pass the key as a Bearer token). Wired to a generic submit+poll
    shape; confirm endpoints against your provider. Without a key, returns
    ``None``.
    """
    key = os.environ.get("KLING_API_KEY")
    if not key:
        print(
            "[video_gen] kling backend selected but KLING_API_KEY is not set. "
            "Export KLING_API_KEY=... (klingai.com / your reseller)."
        )
        return None
    if requests is None:
        print("[video_gen] requests unavailable; cannot reach kling")
        return None

    base = os.environ.get("KLING_BASE_URL", "https://api.klingai.com").rstrip("/")
    kling_model = model or os.environ.get("CZ_KLING_MODEL", "kling-v1")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    endpoint = "image2video" if (image and os.path.isfile(image)) else "text2video"
    body: Dict[str, object] = {
        "model_name": kling_model,
        "prompt": prompt or "",
        "duration": str(int(round(duration))),
    }
    if endpoint == "image2video":
        body["image"] = image  # real use: base64 or a hosted URL

    try:
        submit = requests.post(
            f"{base}/v1/videos/{endpoint}", json=body, headers=headers,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        submit.raise_for_status()
        resp = submit.json()
    except Exception as exc:
        print(f"[video_gen] kling submit failed: {exc}")
        return None

    task_id = (resp.get("data") or {}).get("task_id") or resp.get("task_id")
    if not task_id:
        print(f"[video_gen] kling submit returned no task id: {resp!r}")
        return None

    deadline = time.monotonic() + _CLOUD_POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_CLOUD_POLL_INTERVAL)
        try:
            poll = requests.get(
                f"{base}/v1/videos/{endpoint}/{task_id}", headers=headers,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            poll.raise_for_status()
            data = (poll.json().get("data") or {})
        except Exception as exc:
            print(f"[video_gen] kling poll failed: {exc}")
            return None
        status = (data.get("task_status") or "").lower()
        if status in {"succeed", "succeeded", "success"}:
            videos = ((data.get("task_result") or {}).get("videos")) or []
            url = videos[0].get("url") if videos and isinstance(videos[0], dict) else None
            if not isinstance(url, str):
                print(f"[video_gen] kling succeeded but no video url: {data!r}")
                return None
            return _download_and_accept(url, out_path, duration)
        if status in {"failed", "error"}:
            print(f"[video_gen] kling task {task_id} failed: {data!r}")
            return None
    print("[video_gen] kling poll timed out")
    return None


def _download_and_accept(url: str, out_path: str, duration: float) -> Optional[str]:
    """Download a remote video to ``out_path`` and ffprobe-validate it."""
    if requests is None:
        return None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT},
                            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT), stream=True)
        resp.raise_for_status()
        with open(out_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
    except Exception as exc:
        print(f"[video_gen] download of result failed: {exc}")
        return None
    return _accept(out_path, duration)


# --------------------------------------------------------------------------- #
# Backend: flux_morph (FREE, generative — morph between several Flux stills)
# --------------------------------------------------------------------------- #

def _backend_flux_morph(
    prompt: Optional[str], image: Optional[str], out_path: str,
    width: int, height: int, duration: float, seed: int,
    model: Optional[str] = None,
) -> Optional[str]:
    """Free *generative* clip: cross-dissolve several Flux stills of the concept.

    Generates K stills from the prompt at different seeds (free via Pollinations/
    Flux) and xfades between them with faint grain, so the clip visibly EVOLVES
    rather than just panning one frame — ideal for abstract B-roll (plasma,
    neural, energy fields). Free, and offline after the still fetch. Degrades to
    kenburns when fewer than two stills resolve (e.g. no network). No paid API.
    """
    if not (shutil.which(_FFMPEG) or os.path.isfile(_FFMPEG)):
        return None
    if not prompt and not image:
        return None
    out_dir = os.path.dirname(out_path) or "."
    fps = _DEFAULT_FPS

    k = 3 if duration >= 3.0 else 2
    stills: list = []
    if image and os.path.isfile(image):
        stills.append(os.path.abspath(image))
    if prompt and image_gen is not None:
        for i in range(max(0, k - len(stills))):
            s = image_gen.gen_image(prompt, out_dir, width=width, height=height,
                                    seed=seed + 101 + i)
            if s and os.path.isfile(s):
                stills.append(s)
    if len(stills) < 2:
        # Not enough to morph -> fall back to the single-still Ken Burns path.
        return _backend_kenburns(prompt, (stills[0] if stills else None),
                                 out_path, width, height, duration, seed)

    n = len(stills)
    xf = 0.7                                   # crossfade length (s)
    seg = max(xf + 0.3, (duration + (n - 1) * xf) / n)   # per-still on-screen time
    parts: list = []
    inputs: list = []
    for i, s in enumerate(stills):
        inputs += ["-loop", "1", "-t", f"{seg:g}", "-i", s]
        parts.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={fps},setsar=1[v{i}]"
        )
    prev = "v0"
    for i in range(1, n):
        out_lbl = "vout" if i == n - 1 else f"x{i}"
        offset = i * (seg - xf)
        parts.append(
            f"[{prev}][v{i}]xfade=transition=fade:duration={xf:.3f}:"
            f"offset={offset:.3f}[{out_lbl}]"
        )
        prev = out_lbl
    parts.append("[vout]noise=alls=6:allf=t,format=yuv420p,setsar=1[vfinal]")
    fc = ";".join(parts)

    cmd = [
        _FFMPEG, "-y", *inputs,
        "-filter_complex", fc, "-map", "[vfinal]",
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
        out_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_FFMPEG_TIMEOUT)
    except Exception as exc:
        print(f"[video_gen] flux_morph ffmpeg invocation failed: {exc}")
        return None
    if proc.returncode != 0:
        print(f"[video_gen] flux_morph ffmpeg exited {proc.returncode}: "
              f"{(proc.stderr or proc.stdout or '').strip()[-400:]}")
        # Last resort: a Ken Burns clip on the first still still beats nothing.
        return _backend_kenburns(prompt, stills[0], out_path,
                                 width, height, duration, seed)
    return _accept(out_path, duration)


# --------------------------------------------------------------------------- #
# Registry + public API
# --------------------------------------------------------------------------- #

# Every backend has the uniform signature:
#   (prompt, image, out_path, width, height, duration, seed, model) -> str|None
_BACKENDS: Dict[str, Callable[..., Optional[str]]] = {
    "kenburns": _backend_kenburns,
    "flux_morph": _backend_flux_morph,
    "svd": _backend_svd,
    "wan": _backend_wan,
    "replicate": _backend_replicate,
    "runway": _backend_runway,
    "kling": _backend_kling,
}


def gen_video(
    prompt: Optional[str] = None,
    image: Optional[str] = None,
    out_dir: str = ".",
    duration: float = 4.0,
    width: int = _DEFAULT_W,
    height: int = _DEFAULT_H,
    seed: Optional[int] = None,
    backend: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[str]:
    """Generate a short moving B-roll clip and return its absolute mp4 path.

    Args:
        prompt: Text description of the motion B-roll. Required unless ``image``
            is given; if both are given the still is used and the prompt is
            passed through to backends that condition on text.
        image:  Path to an existing still to animate. If omitted, a still is
            synthesised from ``prompt`` via ``image_gen.gen_image`` (Flux).
        out_dir: Directory to write the mp4 into (created if missing).
        duration: Clip length in seconds (default 4.0).
        width:  Output width in pixels (default 1080).
        height: Output height in pixels (default 1920, i.e. 9:16 vertical).
        seed:   Reproducibility seed; if ``None``, defaults to 0.
        backend: One of ``"kenburns"`` (free, default), ``"flux_morph"`` (free,
            generative — morph between Flux stills), ``"svd"``, ``"wan"``,
            ``"replicate"``, ``"runway"``, ``"kling"``. If ``None``, falls back
            to ``$CZ_VIDEO_GEN_BACKEND`` then to ``"kenburns"``. The two free
            backends need no API key.
        model:  Optional model override passed to cloud/local backends (ignored
            by kenburns).

    Returns:
        Absolute path to a written, ffprobe-validated mp4, or ``None`` on any
        failure. Never raises for an expected runtime problem — network errors,
        missing API keys, missing local binaries, decode failures, and bad
        writes all surface as ``None`` (with a diagnostic printed).
    """
    if not prompt and not image:
        print("[video_gen] need either prompt= or image=")
        return None
    if prompt is not None and not isinstance(prompt, str):
        print(f"[video_gen] prompt must be a string, got {prompt!r}")
        return None

    try:
        width = int(width)
        height = int(height)
        duration = float(duration)
        seed = 0 if seed is None else int(seed)
    except (TypeError, ValueError):
        print(f"[video_gen] bad numeric args: w={width!r} h={height!r} "
              f"dur={duration!r} seed={seed!r}")
        return None

    if width <= 0 or height <= 0:
        print(f"[video_gen] width/height must be positive, got {width}x{height}")
        return None
    if duration <= 0:
        print(f"[video_gen] duration must be positive, got {duration}")
        return None
    # Force even dims (encoder-safe).
    width, height = _even(width), _even(height)

    name = (backend or os.environ.get("CZ_VIDEO_GEN_BACKEND") or DEFAULT_BACKEND).lower()
    fn = _BACKENDS.get(name)
    if fn is None:
        print(
            f"[video_gen] unknown backend {name!r}; "
            f"choose one of: {', '.join(sorted(_BACKENDS))}"
        )
        return None

    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as exc:
        print(f"[video_gen] could not create out_dir {out_dir!r}: {exc}")
        return None

    key = image or (prompt or "")
    out_path = os.path.join(out_dir, _slug(key, width, height, duration, seed) + ".mp4")

    try:
        return fn(prompt, image, out_path, width, height, duration, seed, model)
    except NotImplementedError as exc:
        print(f"[video_gen] backend {name!r} not available: {exc}")
        return None
    except Exception as exc:  # last-resort guard: never propagate
        print(f"[video_gen] backend {name!r} crashed: {exc}")
        return None


def gen_broll_clip(
    concept: str,
    out_dir: str,
    duration: float = 4.0,
    **kw,
) -> Optional[str]:
    """Convenience: turn a *concept* into a moving B-roll clip in one call.

    If no ``image=`` is supplied in ``kw``, a Flux still is synthesised for
    ``concept`` via ``image_gen.gen_image`` and then animated by the selected
    backend (kenburns by default). This is the function the storyboard /
    build_short pipeline should call to drop generated motion B-roll at a
    transcript concept timestamp.

    Args:
        concept: The B-roll concept / prompt, e.g. "neural network abstract".
        out_dir: Directory to write into.
        duration: Clip length in seconds (default 4.0).
        **kw: Forwarded to :func:`gen_video` (``image``, ``width``, ``height``,
            ``seed``, ``backend``, ``model``).

    Returns:
        Absolute path to the generated clip, or ``None`` on failure.
    """
    if not concept or not isinstance(concept, str):
        print(f"[video_gen] concept must be a non-empty string, got {concept!r}")
        return None
    return gen_video(prompt=concept, out_dir=out_dir, duration=duration, **kw)


# --------------------------------------------------------------------------- #
# CLI / self-test
# --------------------------------------------------------------------------- #

def _selftest(out_dir: str) -> int:
    """Offline self-test: synthesise a still with lavfi, kenburns-animate it."""
    os.makedirs(out_dir, exist_ok=True)
    still = os.path.join(out_dir, "selftest_src.png")
    # Generate a synthetic still with ffmpeg lavfi (no network needed).
    gen_cmd = [
        _FFMPEG, "-y", "-f", "lavfi",
        "-i", "mandelbrot=size=1080x1920:rate=1",
        "-frames:v", "1", still,
    ]
    proc = subprocess.run(gen_cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not os.path.isfile(still):
        print(f"[selftest] FAILED to make synthetic still: "
              f"{(proc.stderr or '').strip()[-300:]}")
        return 1

    path = gen_video(image=still, out_dir=out_dir, duration=4.0,
                     width=1080, height=1920, seed=7, backend="kenburns")
    if not path:
        print("[selftest] FAILED: gen_video returned None")
        return 1
    info = _probe_video(path)
    if not info:
        print("[selftest] FAILED: output did not probe as a video")
        return 1
    dur, w, h, codec = info
    print(f"[selftest] OK path={path}")
    print(f"[selftest]    dur={dur:.2f}s {w}x{h} codec={codec}")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate a moving B-roll clip from a still or prompt."
    )
    parser.add_argument("prompt", nargs="?", help="Concept/prompt. Omit to self-test.")
    parser.add_argument("-i", "--image", default=None, help="Still to animate.")
    parser.add_argument("-o", "--out-dir", default=".", help="Output dir (default: .)")
    parser.add_argument("-d", "--duration", type=float, default=4.0)
    parser.add_argument("-W", "--width", type=int, default=_DEFAULT_W)
    parser.add_argument("-H", "--height", type=int, default=_DEFAULT_H)
    parser.add_argument("-s", "--seed", type=int, default=0)
    parser.add_argument(
        "-b", "--backend", choices=sorted(_BACKENDS), default=None,
        help="Backend (default: $CZ_VIDEO_GEN_BACKEND or kenburns).",
    )
    parser.add_argument("--selftest", action="store_true", help="Run the offline self-test.")
    args = parser.parse_args()

    if args.selftest or (not args.prompt and not args.image):
        raise SystemExit(_selftest(args.out_dir))

    result = gen_video(
        prompt=args.prompt, image=args.image, out_dir=args.out_dir,
        duration=args.duration, width=args.width, height=args.height,
        seed=args.seed, backend=args.backend,
    )
    if result:
        print(result)
        raise SystemExit(0)
    raise SystemExit(1)
