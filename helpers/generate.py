"""Generate video shots from text prompts with a hosted AI video model.

This is the front door of the idea → video pipeline. It takes a *brief* — a
small JSON file describing the shots — and produces one mp4 per shot in
`<edit_dir>/generated/`, then (optionally) an `edl.json` so `render.py` can
assemble, grade and subtitle them exactly like filmed footage.

Shots generate in parallel. Every result is probed with ffprobe before it is
accepted, and every accepted shot is cached by a hash of its spec, so re-running
after a partial failure only regenerates what actually failed or changed.

Providers (`--provider`, or `"provider"` in the brief):

    veo      Google Veo via the Gemini API   GEMINI_API_KEY      (default)
    luma     Luma Dream Machine              LUMA_API_KEY
    runway   Runway Gen-4                    RUNWAY_API_KEY
    sora     OpenAI Sora                     OPENAI_API_KEY

Hosted video APIs change often. Every base URL and model default below can be
overridden with an environment variable (see PROVIDER_ENDPOINT_ENV) so a moved
endpoint is a config change, not a code change.

Brief format:

    {
      "title": "Launch teaser",
      "provider": "veo",
      "model": "veo-3.1-generate-preview",
      "aspect": "9:16",
      "resolution": "1080p",
      "shots": [
        {
          "id": "01",
          "beat": "HOOK",
          "prompt": "Slow push in on a cracked hourglass on a desk, dust in a
                     shaft of window light, shallow depth of field, 35mm",
          "duration": 8,
          "negative_prompt": "text, watermark, distorted hands",
          "image": "refs/frame1.png"
        }
      ]
    }

Usage:
    python helpers/generate.py brief.json --edit-dir <dir> --emit-edl
    python helpers/generate.py brief.json --edit-dir <dir> --shots 01,04 --force
    python helpers/generate.py --prompt "a neon jellyfish drifting over Tokyo" \
        --edit-dir <dir> --aspect 9:16
    python helpers/generate.py brief.json --edit-dir <dir> --dry-run
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from envkeys import get_key, require_key

# -------- Provider configuration --------------------------------------------
#
# Overridable so a provider moving a URL or shipping a new model does not
# require editing this file.

PROVIDER_ENDPOINT_ENV = {
    "veo": ("VEO_API_BASE", "https://generativelanguage.googleapis.com/v1beta"),
    "luma": ("LUMA_API_BASE", "https://api.lumalabs.ai/dream-machine/v1"),
    "runway": ("RUNWAY_API_BASE", "https://api.dev.runwayml.com/v1"),
    "sora": ("SORA_API_BASE", "https://api.openai.com/v1"),
}

PROVIDER_MODEL_ENV = {
    "veo": ("VEO_MODEL", "veo-3.1-generate-preview"),
    "luma": ("LUMA_MODEL", "ray-2"),
    "runway": ("RUNWAY_MODEL", "gen4_turbo"),
    "sora": ("SORA_MODEL", "sora-2"),
}

PROVIDER_KEY_ENV = {
    "veo": "GEMINI_API_KEY",
    "luma": "LUMA_API_KEY",
    "runway": "RUNWAY_API_KEY",
    "sora": "OPENAI_API_KEY",
}

KEY_HINT = {
    "veo": "Get one at https://aistudio.google.com/apikey, then put GEMINI_API_KEY=... in the repo-root .env",
    "luma": "Get one at https://lumalabs.ai/api, then put LUMA_API_KEY=... in the repo-root .env",
    "runway": "Get one at https://dev.runwayml.com, then put RUNWAY_API_KEY=... in the repo-root .env",
    "sora": "Get one at https://platform.openai.com/api-keys, then put OPENAI_API_KEY=... in the repo-root .env",
}

# Generation is slow and bursty: a single 8s shot can take several minutes and
# providers queue under load. Poll gently, give up eventually.
POLL_INTERVAL = 6.0
POLL_TIMEOUT = 1500.0


def base_url(provider: str) -> str:
    env_name, default = PROVIDER_ENDPOINT_ENV[provider]
    return (get_key(env_name) or default).rstrip("/")


def default_model(provider: str) -> str:
    env_name, default = PROVIDER_MODEL_ENV[provider]
    return get_key(env_name) or default


# -------- Shared HTTP -------------------------------------------------------


class GenerationError(RuntimeError):
    """A shot failed. Carries a message the agent can act on."""


def _check(resp: requests.Response, what: str) -> dict:
    if resp.status_code >= 400:
        raise GenerationError(f"{what} returned {resp.status_code}: {resp.text[:600]}")
    try:
        return resp.json()
    except ValueError:
        raise GenerationError(f"{what} returned non-JSON: {resp.text[:300]}")


def poll_until(fetch, is_done, describe, timeout: float = POLL_TIMEOUT) -> dict:
    """Poll `fetch()` until `is_done(payload)`. Returns the finished payload."""
    deadline = time.time() + timeout
    while True:
        payload = fetch()
        if is_done(payload):
            return payload
        if time.time() > deadline:
            raise GenerationError(f"{describe}: still not done after {timeout:.0f}s")
        time.sleep(POLL_INTERVAL)


def download_to(url: str, dest: Path, headers: dict | None = None, params: dict | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, headers=headers or {}, params=params or {}, stream=True, timeout=900) as r:
        if r.status_code >= 400:
            raise GenerationError(f"download returned {r.status_code}: {r.text[:300]}")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
    tmp.replace(dest)


def encode_image(path: Path) -> tuple[str, str]:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return base64.b64encode(path.read_bytes()).decode("ascii"), mime


# -------- Providers ---------------------------------------------------------
#
# Each provider exposes generate(shot, spec, dest) and blocks until the shot is
# on disk. The batch runner supplies concurrency.


def generate_veo(shot: dict, spec: dict, dest: Path) -> dict:
    key = require_key(PROVIDER_KEY_ENV["veo"], KEY_HINT["veo"])
    api, model = base_url("veo"), spec["model"]
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}

    instance: dict = {"prompt": shot["prompt"]}
    if shot.get("image"):
        data, mime = encode_image(Path(shot["image"]))
        instance["image"] = {"bytesBase64Encoded": data, "mimeType": mime}

    params: dict = {}
    if spec.get("aspect"):
        params["aspectRatio"] = spec["aspect"]
    if spec.get("resolution"):
        params["resolution"] = spec["resolution"]
    if shot.get("duration"):
        params["durationSeconds"] = int(shot["duration"])
    if shot.get("negative_prompt"):
        params["negativePrompt"] = shot["negative_prompt"]
    if spec.get("person_generation"):
        params["personGeneration"] = spec["person_generation"]
    params.update(spec.get("extra_params") or {})

    def submit(p: dict) -> requests.Response:
        return requests.post(
            f"{api}/models/{model}:predictLongRunning",
            headers=headers,
            json={"instances": [instance], "parameters": p},
            timeout=180,
        )

    resp = submit(params)
    # Veo model variants accept different parameter sets (Lite has no 4K, some
    # versions fix duration). Rather than hard-code a per-model matrix that goes
    # stale, retry once with only the universally accepted parameters.
    if resp.status_code == 400 and params:
        minimal = {k: v for k, v in params.items() if k in ("aspectRatio", "negativePrompt")}
        if minimal != params:
            print(f"    [{shot['id']}] 400 with full parameters, retrying with {sorted(minimal)}", flush=True)
            resp = submit(minimal)

    op = _check(resp, "Veo predictLongRunning")
    op_name = op.get("name")
    if not op_name:
        raise GenerationError(f"Veo did not return an operation name: {json.dumps(op)[:300]}")

    def fetch() -> dict:
        return _check(requests.get(f"{api}/{op_name}", headers=headers, timeout=120), "Veo operation poll")

    done = poll_until(fetch, lambda p: bool(p.get("done")), f"Veo shot {shot['id']}")
    if done.get("error"):
        raise GenerationError(f"Veo generation failed: {json.dumps(done['error'])[:400]}")

    response = done.get("response") or {}
    inner = response.get("generateVideoResponse") or response
    samples = inner.get("generatedSamples") or inner.get("generatedVideos") or []
    if not samples:
        # A content filter rejection lands here rather than in `error`.
        reason = inner.get("raiMediaFilteredReasons") or inner.get("raiMediaFilteredCount")
        detail = f" (filtered: {reason})" if reason else ""
        raise GenerationError(f"Veo returned no video for shot {shot['id']}{detail}: {json.dumps(done)[:400]}")

    uri = (samples[0].get("video") or {}).get("uri")
    if not uri:
        raise GenerationError(f"Veo sample has no video uri: {json.dumps(samples[0])[:300]}")

    download_to(uri, dest, headers={"x-goog-api-key": key}, params={"alt": "media"})
    return {"provider": "veo", "model": model, "operation": op_name}


def generate_luma(shot: dict, spec: dict, dest: Path) -> dict:
    key = require_key(PROVIDER_KEY_ENV["luma"], KEY_HINT["luma"])
    api, model = base_url("luma"), spec["model"]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    body: dict = {"prompt": shot["prompt"], "model": model}
    if spec.get("aspect"):
        body["aspect_ratio"] = spec["aspect"]
    if spec.get("resolution"):
        body["resolution"] = spec["resolution"]
    if shot.get("duration"):
        body["duration"] = f"{int(shot['duration'])}s"
    if shot.get("image"):
        # Luma takes a reachable URL, not bytes.
        body["keyframes"] = {"frame0": {"type": "image", "url": shot["image"]}}

    created = _check(
        requests.post(f"{api}/generations", headers=headers, json=body, timeout=180),
        "Luma generations",
    )
    gen_id = created.get("id")
    if not gen_id:
        raise GenerationError(f"Luma did not return a generation id: {json.dumps(created)[:300]}")

    def fetch() -> dict:
        return _check(
            requests.get(f"{api}/generations/{gen_id}", headers=headers, timeout=120),
            "Luma generation poll",
        )

    done = poll_until(
        fetch,
        lambda p: p.get("state") in ("completed", "failed"),
        f"Luma shot {shot['id']}",
    )
    if done.get("state") == "failed":
        raise GenerationError(f"Luma generation failed: {done.get('failure_reason') or json.dumps(done)[:300]}")

    url = (done.get("assets") or {}).get("video")
    if not url:
        raise GenerationError(f"Luma returned no video asset: {json.dumps(done)[:300]}")

    download_to(url, dest)
    return {"provider": "luma", "model": model, "generation_id": gen_id}


def generate_runway(shot: dict, spec: dict, dest: Path) -> dict:
    key = require_key(PROVIDER_KEY_ENV["runway"], KEY_HINT["runway"])
    api, model = base_url("runway"), spec["model"]
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Runway-Version": get_key("RUNWAY_API_VERSION") or "2024-11-06",
    }

    body: dict = {"model": model, "promptText": shot["prompt"]}
    if shot.get("duration"):
        body["duration"] = int(shot["duration"])
    if spec.get("aspect"):
        body["ratio"] = spec.get("runway_ratio") or spec["aspect"]

    if shot.get("image"):
        endpoint = "image_to_video"
        ref = shot["image"]
        if not str(ref).startswith("http"):
            data, mime = encode_image(Path(ref))
            ref = f"data:{mime};base64,{data}"
        body["promptImage"] = ref
    else:
        endpoint = "text_to_video"

    created = _check(
        requests.post(f"{api}/{endpoint}", headers=headers, json=body, timeout=180),
        f"Runway {endpoint}",
    )
    task_id = created.get("id")
    if not task_id:
        raise GenerationError(f"Runway did not return a task id: {json.dumps(created)[:300]}")

    def fetch() -> dict:
        return _check(requests.get(f"{api}/tasks/{task_id}", headers=headers, timeout=120), "Runway task poll")

    done = poll_until(
        fetch,
        lambda p: p.get("status") in ("SUCCEEDED", "FAILED", "CANCELLED"),
        f"Runway shot {shot['id']}",
    )
    if done.get("status") != "SUCCEEDED":
        raise GenerationError(f"Runway task {done.get('status')}: {json.dumps(done)[:400]}")

    output = done.get("output") or []
    if not output:
        raise GenerationError(f"Runway returned no output: {json.dumps(done)[:300]}")

    download_to(output[0], dest)
    return {"provider": "runway", "model": model, "task_id": task_id}


def generate_sora(shot: dict, spec: dict, dest: Path) -> dict:
    key = require_key(PROVIDER_KEY_ENV["sora"], KEY_HINT["sora"])
    api, model = base_url("sora"), spec["model"]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    body: dict = {"model": model, "prompt": shot["prompt"]}
    if shot.get("duration"):
        body["seconds"] = str(int(shot["duration"]))
    if spec.get("size"):
        body["size"] = spec["size"]
    elif spec.get("aspect"):
        body["size"] = "1280x720" if spec["aspect"] == "16:9" else "720x1280"

    created = _check(requests.post(f"{api}/videos", headers=headers, json=body, timeout=180), "Sora videos")
    video_id = created.get("id")
    if not video_id:
        raise GenerationError(f"Sora did not return a video id: {json.dumps(created)[:300]}")

    def fetch() -> dict:
        return _check(requests.get(f"{api}/videos/{video_id}", headers=headers, timeout=120), "Sora video poll")

    done = poll_until(
        fetch,
        lambda p: p.get("status") in ("completed", "failed"),
        f"Sora shot {shot['id']}",
    )
    if done.get("status") == "failed":
        raise GenerationError(f"Sora generation failed: {json.dumps(done.get('error') or done)[:400]}")

    download_to(f"{api}/videos/{video_id}/content", dest, headers={"Authorization": f"Bearer {key}"})
    return {"provider": "sora", "model": model, "video_id": video_id}


PROVIDERS = {
    "veo": generate_veo,
    "luma": generate_luma,
    "runway": generate_runway,
    "sora": generate_sora,
}


# -------- Verification + caching --------------------------------------------


def probe(path: Path) -> dict:
    """Return {duration, width, height} for a rendered file, or raise."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise GenerationError(f"ffprobe rejected {path.name}: {out.stderr.strip()[:200]}")
    payload = json.loads(out.stdout or "{}")
    streams = payload.get("streams") or []
    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    if not streams or duration <= 0:
        raise GenerationError(f"{path.name} downloaded but has no usable video stream")
    return {"duration": duration, "width": streams[0].get("width"), "height": streams[0].get("height")}


def shot_hash(shot: dict, spec: dict) -> str:
    """Identity of a shot: prompt + its own params + the brief-level params.

    Two shots with the same hash produce interchangeable output, so a cached
    file is reusable. Changing any of it invalidates the cache.
    """
    material = {
        "prompt": shot.get("prompt"),
        "duration": shot.get("duration"),
        "negative_prompt": shot.get("negative_prompt"),
        "image": shot.get("image"),
        "provider": spec.get("provider"),
        "model": spec.get("model"),
        "aspect": spec.get("aspect"),
        "resolution": spec.get("resolution"),
        "extra_params": spec.get("extra_params"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


# -------- Brief handling ----------------------------------------------------


def load_brief(args) -> dict:
    if args.brief:
        spec = json.loads(Path(args.brief).read_text())
    elif args.prompt:
        spec = {"title": "single shot", "shots": [{"id": "01", "prompt": args.prompt}]}
    else:
        sys.exit("provide a brief JSON file, or --prompt for a single shot")

    if args.provider:
        spec["provider"] = args.provider
    if args.aspect:
        spec["aspect"] = args.aspect
    if args.resolution:
        spec["resolution"] = args.resolution
    if args.duration:
        for shot in spec.get("shots", []):
            shot.setdefault("duration", args.duration)

    spec.setdefault("provider", "veo")
    if spec["provider"] not in PROVIDERS:
        sys.exit(f"unknown provider '{spec['provider']}' (choose from: {', '.join(PROVIDERS)})")
    spec.setdefault("model", default_model(spec["provider"]))
    spec.setdefault("aspect", "16:9")

    shots = spec.get("shots") or []
    if not shots:
        sys.exit("brief has no shots")
    for i, shot in enumerate(shots):
        shot.setdefault("id", f"{i + 1:02d}")
        if not shot.get("prompt"):
            sys.exit(f"shot {shot['id']} has no prompt")
        # Reference images are written relative to the brief.
        if shot.get("image") and args.brief:
            img = Path(shot["image"])
            if not img.is_absolute():
                shot["image"] = str((Path(args.brief).resolve().parent / img).resolve())

    if args.shots:
        wanted = {s.strip() for s in args.shots.split(",")}
        spec["shots"] = [s for s in shots if s["id"] in wanted]
        missing = wanted - {s["id"] for s in spec["shots"]}
        if missing:
            sys.exit(f"no such shot id(s) in brief: {', '.join(sorted(missing))}")

    return spec


def write_edl(spec: dict, results: list[dict], edit_dir: Path) -> Path:
    """Emit an EDL that render.py can assemble directly.

    Every generated clip is used whole — a generated shot has no filler to trim.
    The agent is expected to edit these ranges afterwards to tighten the cut.
    """
    sources, ranges = {}, []
    for r in results:
        if r["status"] != "ok":
            continue
        name = f"shot_{r['id']}"
        sources[name] = str(Path(r["path"]).relative_to(edit_dir))
        ranges.append({
            "source": name,
            "start": 0.0,
            "end": round(r["probe"]["duration"], 3),
            "beat": r.get("beat") or "",
        })

    edl = {
        "title": spec.get("title", "ai video"),
        "sources": sources,
        "ranges": ranges,
        "grade": spec.get("grade"),
        "overlays": [],
    }
    path = edit_dir / "edl.json"
    path.write_text(json.dumps(edl, indent=2))
    return path


# -------- Batch runner ------------------------------------------------------


def run_shot(shot: dict, spec: dict, out_dir: Path, force: bool) -> dict:
    sid = shot["id"]
    dest = out_dir / f"shot_{sid}.mp4"
    digest = shot_hash(shot, spec)
    sidecar = out_dir / f"shot_{sid}.json"

    if dest.exists() and not force:
        cached = json.loads(sidecar.read_text()) if sidecar.exists() else {}
        if cached.get("hash") == digest:
            print(f"  [{sid}] cached", flush=True)
            return {**cached, "status": "ok", "cached": True}

    print(f"  [{sid}] generating via {spec['provider']}/{spec['model']}", flush=True)
    t0 = time.time()
    try:
        meta = PROVIDERS[spec["provider"]](shot, spec, dest)
        info = probe(dest)
    except GenerationError as e:
        print(f"  [{sid}] FAILED: {e}", flush=True)
        return {"id": sid, "status": "failed", "error": str(e), "beat": shot.get("beat", "")}
    except Exception as e:  # network stack, filesystem, malformed provider payload
        print(f"  [{sid}] FAILED: {type(e).__name__}: {e}", flush=True)
        return {"id": sid, "status": "failed", "error": f"{type(e).__name__}: {e}", "beat": shot.get("beat", "")}

    record = {
        "id": sid,
        "status": "ok",
        "path": str(dest),
        "hash": digest,
        "prompt": shot["prompt"],
        "beat": shot.get("beat", ""),
        "probe": info,
        "meta": meta,
        "seconds_taken": round(time.time() - t0, 1),
    }
    sidecar.write_text(json.dumps(record, indent=2))
    print(
        f"  [{sid}] done  {info['duration']:.1f}s  {info['width']}x{info['height']}  "
        f"in {record['seconds_taken']:.0f}s",
        flush=True,
    )
    return record


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate AI video shots from a brief")
    ap.add_argument("brief", nargs="?", type=Path, help="Brief JSON describing the shots")
    ap.add_argument("--prompt", help="Generate a single shot from this prompt instead of a brief")
    ap.add_argument("--edit-dir", type=Path, required=True, help="Session output directory")
    ap.add_argument("--provider", choices=sorted(PROVIDERS), help="Override the brief's provider")
    ap.add_argument("--aspect", help="Override aspect ratio, e.g. 16:9 or 9:16")
    ap.add_argument("--resolution", help="Override resolution, e.g. 720p or 1080p")
    ap.add_argument("--duration", type=int, help="Default per-shot duration in seconds")
    ap.add_argument("--shots", help="Comma-separated shot ids to (re)generate, e.g. 01,04")
    ap.add_argument("--workers", type=int, default=3, help="Parallel generations (default: 3)")
    ap.add_argument("--force", action="store_true", help="Regenerate even if a cached shot matches")
    ap.add_argument("--emit-edl", action="store_true", help="Write edl.json for render.py")
    ap.add_argument("--dry-run", action="store_true", help="Validate the brief and credentials, generate nothing")
    args = ap.parse_args()

    spec = load_brief(args)
    edit_dir = args.edit_dir.resolve()
    out_dir = edit_dir / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)

    shots = spec["shots"]
    planned = sum(float(s.get("duration") or 0) for s in shots)
    print(f"brief: {spec.get('title', '(untitled)')}")
    print(f"  provider : {spec['provider']} / {spec['model']}")
    print(f"  format   : {spec['aspect']} {spec.get('resolution') or ''}".rstrip())
    print(f"  shots    : {len(shots)} ({planned:.0f}s planned)")

    if args.dry_run:
        key_name = PROVIDER_KEY_ENV[spec["provider"]]
        print(f"  key      : {key_name} {'present' if get_key(key_name) else 'MISSING'}")
        for s in shots:
            print(f"  [{s['id']}] {s.get('beat', ''):<10} {s['prompt'][:88]}")
        print("dry run: nothing generated")
        return

    require_key(PROVIDER_KEY_ENV[spec["provider"]], KEY_HINT[spec["provider"]])

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda s: run_shot(s, spec, out_dir, args.force), shots))

    results.sort(key=lambda r: r["id"])
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]

    manifest = out_dir / "manifest.json"
    manifest.write_text(json.dumps({"spec": {k: v for k, v in spec.items() if k != "shots"},
                                    "shots": results}, indent=2))

    total = sum(r["probe"]["duration"] for r in ok)
    print(f"\n{len(ok)}/{len(results)} shot(s) generated, {total:.1f}s of footage, "
          f"wall time {time.time() - t0:.0f}s")
    print(f"manifest: {manifest}")

    if ok and args.emit_edl:
        print(f"edl: {write_edl(spec, results, edit_dir)}")

    if failed:
        print("\nfailed shots (rerun with --shots " + ",".join(r["id"] for r in failed) + "):")
        for r in failed:
            print(f"  [{r['id']}] {r['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
