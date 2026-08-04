"""Idea in, published video out. One command, no stops.

    python helpers/autopilot.py "a 25s vertical teaser about an hourglass running backwards"

Runs the whole chain unattended: Claude writes the treatment and the shot
brief, the shots generate in parallel, they assemble through render.py, and the
result uploads to whichever destinations the policy names.

The confirmation gates in skills/ai-video/SKILL.md exist because generation
costs money and publishing is public. Autopilot does not remove those
decisions — it moves them from *per video* to *once*, into a policy file. Set
the budget, the destination and the privacy one time; every later run inherits
them. Three things enforce the policy at runtime:

  * Credentials are checked BEFORE anything is generated. A missing upload
    token stops the run at second zero rather than after you've paid for a
    video that has nowhere to go.
  * The brief is capped at `max_seconds` and `max_shots`. A typo in an idea
    cannot bill a feature film.
  * `privacy` defaults to unlisted. The upload happens without asking; the
    video is not public until the policy says so.

Every stage is resumable: re-running skips work that already succeeded, so a
failure at publish does not regenerate the footage.

Usage:
    python helpers/autopilot.py "<idea>" --project-dir ~/videos/teaser
    python helpers/autopilot.py "<idea>" --to youtube --privacy public
    python helpers/autopilot.py "<idea>" --dry-run     # plan + preflight only
    python helpers/autopilot.py "<idea>" --policy my-policy.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import anthropic

from envkeys import REPO_ROOT, get_key

HELPERS = Path(__file__).resolve().parent

# -------- Policy -------------------------------------------------------------
#
# The decisions you would otherwise be asked for on every video. Override with
# autopilot.json at the repo root, --policy, or the CLI flags below.

DEFAULT_POLICY = {
    "model": "claude-opus-5",
    "provider": "veo",
    "aspect": "9:16",
    "resolution": "1080p",
    "shot_seconds": 8,
    "target_seconds": 24,
    # Budget guard. The brief is truncated to fit; generation never exceeds it.
    "max_seconds": 48,
    "max_shots": 8,
    # Generated shots settle in the first frames and drift in the last.
    "trim_head": 0.3,
    "trim_tail": 0.3,
    "grade": None,
    "destinations": ["youtube"],
    "privacy": "unlisted",
    "dest_dir": None,
    "workers": 3,
}

POLICY_PATH = REPO_ROOT / "autopilot.json"


def load_policy(path: Path | None) -> dict:
    policy = dict(DEFAULT_POLICY)
    source = path or (POLICY_PATH if POLICY_PATH.exists() else None)
    if source:
        if not source.exists():
            sys.exit(f"policy file not found: {source}")
        policy.update(json.loads(source.read_text()))
        print(f"policy: {source}")
    else:
        print("policy: built-in defaults (write autopilot.json to change them)")
    return policy


# -------- Stage 1: idea → brief ---------------------------------------------

BRIEF_SYSTEM = """You are the director of a short AI-generated video. You turn a \
one-line idea into a treatment and a shot-by-shot brief that a text-to-video model \
can execute. You get one pass — nobody reviews your brief before it is generated \
and published, so it has to be right the first time.

Rules that decide whether the video works:

1. CONTINUITY IS COPY-PASTE. The model has no memory between shots; each prompt is \
generated independently. Every shot after the first must repeat the subject, the \
surface/setting, the light direction, the lens and the grade from shot 01 VERBATIM. \
Vary only the action and the framing. Without those repeated anchors the shots will \
not look like the same world.

2. CONCRETE NOUNS BEAT ADJECTIVES. "cracked brass hourglass on dark walnut" survives \
generation; "a beautiful timepiece" does not. Name materials, colours, light sources.

3. PROMPT GRAMMAR: subject -> action -> camera move -> lens -> lighting -> grade -> style.

4. NO ON-SCREEN TEXT. Generated lettering is always malformed. Never ask for text, \
logos, titles or UI in a shot; real text is added in post. Put "text, watermark, \
logos" in negative_prompt for every shot.

5. NO RECOGNIZABLE REAL PEOPLE, no named brands, no violence — these trip provider \
content filters and the shot fails.

6. The beats must form an arc that ends. Give each shot a beat label (HOOK, TURN, \
REVEAL, CTA or whatever the idea calls for).

Also write the publishing metadata: a title that works as a headline, a description, \
and tags. Write them for the platform, not for me."""


def brief_schema() -> dict:
    shot = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Zero-padded shot number, e.g. '01'"},
            "beat": {"type": "string", "description": "Structural role, e.g. HOOK"},
            "prompt": {"type": "string", "description": "The full generation prompt"},
            "negative_prompt": {"type": "string"},
            "duration": {"type": "integer", "description": "Shot length in seconds"},
        },
        "required": ["id", "beat", "prompt", "negative_prompt", "duration"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "treatment": {"type": "string", "description": "3-6 sentences: arc, look, pacing, ending"},
            "shots": {"type": "array", "items": shot},
        },
        "required": ["title", "description", "tags", "treatment", "shots"],
        "additionalProperties": False,
    }


def requested_shots(policy: dict) -> int:
    """How many shots the target length asks for, within the policy's ceiling."""
    return max(1, min(policy["max_shots"],
                      round(policy["target_seconds"] / max(1, policy["shot_seconds"]))))


def write_brief(idea: str, policy: dict) -> dict:
    """Ask Claude for a treatment + shot brief. Returns the parsed object."""
    if not (get_key("ANTHROPIC_API_KEY") or get_key("ANTHROPIC_AUTH_TOKEN")):
        # The SDK also resolves `ant auth login` profiles, so absence of a key is
        # not proof of no credentials — let the client try before complaining.
        print("  (no ANTHROPIC_API_KEY in env or .env; relying on a stored profile)")

    client = anthropic.Anthropic()
    shots = requested_shots(policy)

    user = f"""IDEA: {idea}

FORMAT: {policy['aspect']}, {policy['resolution']}, generated by {policy['provider']}.
LENGTH: about {policy['target_seconds']} seconds total.
SHOTS: exactly {shots} shots of {policy['shot_seconds']} seconds each.

Write the treatment and the brief."""

    request = {
        "model": policy["model"],
        "max_tokens": 16000,
        "system": BRIEF_SYSTEM,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": brief_schema()}},
    }

    # Claude Opus 5's safety classifiers can decline a request outright. Server-side
    # fallbacks re-run the declined request on another model inside the same call,
    # so an ordinary idea that trips a classifier still produces a brief.
    try:
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request
        )
    except anthropic.BadRequestError:
        response = client.messages.create(**request)
    except anthropic.NotFoundError:
        sys.exit(f"model '{policy['model']}' is not available to this API key")
    except anthropic.AuthenticationError:
        sys.exit("Anthropic API key rejected. Set ANTHROPIC_API_KEY in the repo-root .env")
    except anthropic.APIConnectionError as e:
        sys.exit(f"could not reach the Anthropic API: {e}")

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", "") or ""
        sys.exit(f"the idea was declined by safety classifiers. {detail}".strip())

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        sys.exit("the model returned no brief text")
    return json.loads(text)


def build_brief_file(idea: str, brief: dict, policy: dict, edit_dir: Path) -> Path:
    """Clamp the brief to the budget and write it in generate.py's format."""
    shots = brief.get("shots") or []
    if not shots:
        sys.exit("the brief contains no shots")

    # Two ceilings, both real. `max_seconds` is the wallet's hard limit. The
    # requested shot count is what this video actually asked for — a model that
    # returns more shots than it was told to must not silently double the spend.
    ceiling = min(policy["max_shots"], requested_shots(policy))

    kept, total = [], 0.0
    for i, shot in enumerate(shots):
        duration = int(shot.get("duration") or policy["shot_seconds"])
        if len(kept) >= ceiling or total + duration > policy["max_seconds"]:
            print(f"  budget guard: dropping {len(shots) - len(kept)} shot(s) beyond "
                  f"{ceiling} shots / {policy['max_seconds']}s")
            break
        shot["id"] = shot.get("id") or f"{i + 1:02d}"
        shot["duration"] = duration
        kept.append(shot)
        total += duration

    spec = {
        "title": brief["title"],
        "idea": idea,
        "treatment": brief["treatment"],
        "provider": policy["provider"],
        "aspect": policy["aspect"],
        "resolution": policy["resolution"],
        "grade": policy["grade"],
        "publish": {
            "title": brief["title"],
            "description": brief["description"],
            "tags": brief.get("tags") or [],
        },
        "shots": kept,
    }
    path = edit_dir / "brief.json"
    path.write_text(json.dumps(spec, indent=2))
    return path


# -------- Stage plumbing -----------------------------------------------------


def run(cmd: list[str], stage: str) -> None:
    print(f"\n=== {stage} ===")
    result = subprocess.run([sys.executable, *cmd])
    if result.returncode != 0:
        sys.exit(f"{stage} failed (exit {result.returncode}) — nothing after this ran")


def trim_edl(edl_path: Path, policy: dict) -> None:
    """Trim the settling frames off every generated clip.

    Models spend the first fraction of a second resolving the scene and the last
    drifting. The emitted EDL uses each clip whole; this is the tightening pass
    a human would do by hand before rendering.
    """
    head, tail = float(policy["trim_head"]), float(policy["trim_tail"])
    if head <= 0 and tail <= 0:
        return
    edl = json.loads(edl_path.read_text())
    for r in edl.get("ranges", []):
        start, end = float(r["start"]), float(r["end"])
        if end - start > head + tail + 1.0:      # never trim into a short clip
            r["start"], r["end"] = round(start + head, 3), round(end - tail, 3)
    edl_path.write_text(json.dumps(edl, indent=2))
    print(f"  trimmed {head}s/{tail}s off each clip")


def preflight(policy: dict, edit_dir: Path, sample: Path) -> None:
    """Check every credential before a cent is spent.

    Generation is billed per second of output. Discovering a missing upload
    token after the footage exists is the expensive ordering; this is the cheap
    one.
    """
    print("\n=== preflight ===")
    gen_key = {"veo": "GEMINI_API_KEY", "luma": "LUMA_API_KEY",
               "runway": "RUNWAY_API_KEY", "sora": "OPENAI_API_KEY"}[policy["provider"]]
    if not get_key(gen_key):
        sys.exit(f"{gen_key} is not set — {policy['provider']} cannot generate anything.\n"
                 f"  Put it in {REPO_ROOT / '.env'} and re-run.")
    print(f"  {gen_key:<20} present")

    dests = [d for d in policy["destinations"] if d not in ("local", "webhook")]
    if dests:
        # publish.py --dry-run validates credentials and platform limits without
        # uploading. Give it a real file so its probe has something to read.
        cmd = [str(HELPERS / "publish.py"), str(sample), "--title", "preflight", "--dry-run"]
        for d in policy["destinations"]:
            cmd += ["--to", d]
        result = subprocess.run([sys.executable, *cmd], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if "MISSING" in line:
                sys.exit(f"upload credentials incomplete —{line.split(maxsplit=1)[-1]}\n"
                         f"  Fix this before generating; see .env.example.")
        print(f"  destinations         {', '.join(policy['destinations'])} ready")


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Idea in, published video out")
    ap.add_argument("idea", help="One sentence describing the video you want")
    ap.add_argument("--project-dir", type=Path, default=Path.cwd(),
                    help="Where the video lives (default: cwd). Outputs go to <dir>/edit/")
    ap.add_argument("--policy", type=Path, help="Policy JSON (default: autopilot.json at the repo root)")
    ap.add_argument("--to", action="append", help="Override destinations (repeatable)")
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"], help="Override privacy")
    ap.add_argument("--aspect", help="Override aspect ratio")
    ap.add_argument("--seconds", type=int, help="Override target length")
    ap.add_argument("--dry-run", action="store_true",
                    help="Write the brief and run preflight; generate and upload nothing")
    ap.add_argument("--force", action="store_true", help="Redo stages that already succeeded")
    args = ap.parse_args()

    policy = load_policy(args.policy)
    if args.to:
        policy["destinations"] = args.to
    if args.privacy:
        policy["privacy"] = args.privacy
    if args.aspect:
        policy["aspect"] = args.aspect
    if args.seconds:
        policy["target_seconds"] = args.seconds

    project = args.project_dir.expanduser().resolve()
    edit_dir = project / "edit"
    edit_dir.mkdir(parents=True, exist_ok=True)

    print(f"idea: {args.idea}")
    print(f"output: {edit_dir}")
    print(f"plan: {policy['target_seconds']}s {policy['aspect']} via {policy['provider']} "
          f"-> {', '.join(policy['destinations'])} ({policy['privacy']})")

    t0 = time.time()

    # 1. Brief
    brief_path = edit_dir / "brief.json"
    if brief_path.exists() and not args.force:
        print("\n=== brief (cached) ===")
        spec = json.loads(brief_path.read_text())
    else:
        print("\n=== brief ===")
        spec = None
        brief = write_brief(args.idea, policy)
        brief_path = build_brief_file(args.idea, brief, policy, edit_dir)
        spec = json.loads(brief_path.read_text())
        print(f"  {spec['title']}")
        print(f"  {spec['treatment']}")
    for shot in spec["shots"]:
        print(f"  [{shot['id']}] {shot['beat']:<8} {shot['duration']}s  {shot['prompt'][:76]}")

    planned = sum(s["duration"] for s in spec["shots"])
    print(f"  {len(spec['shots'])} shots, {planned}s to generate")

    # 2. Preflight — before any spend.
    preflight(policy, edit_dir, sample=brief_path)

    if args.dry_run:
        print(f"\ndry run: brief written to {brief_path}, nothing generated or uploaded")
        return

    # 3. Generate
    final = edit_dir / "final.mp4"
    if not final.exists() or args.force:
        gen = [str(HELPERS / "generate.py"), str(brief_path), "--edit-dir", str(edit_dir),
               "--emit-edl", "--workers", str(policy["workers"])]
        if args.force:
            gen.append("--force")
        run(gen, "generate")

        # 4. Assemble
        trim_edl(edit_dir / "edl.json", policy)
        run([str(HELPERS / "render.py"), str(edit_dir / "edl.json"), "-o", str(final)], "render")
    else:
        print(f"\n=== generate + render (cached: {final.name}) ===")

    # 5. Publish
    meta = spec["publish"]
    pub = [str(HELPERS / "publish.py"), str(final),
           "--title", meta["title"],
           "--description", meta["description"],
           "--tags", ",".join(meta["tags"]),
           "--privacy", policy["privacy"],
           "--edit-dir", str(edit_dir)]
    for d in policy["destinations"]:
        pub += ["--to", d]
    if policy.get("dest_dir"):
        pub += ["--dest", str(Path(policy["dest_dir"]).expanduser())]
    run(pub, "publish")

    # 6. Persist
    receipts = json.loads((edit_dir / "publish.json").read_text())
    urls = [r["url"] for r in receipts[-1]["results"] if r.get("url")]
    note = (f"\n## {time.strftime('%Y-%m-%d %H:%M')} — autopilot\n"
            f"- idea: {args.idea}\n- title: {meta['title']}\n"
            f"- {len(spec['shots'])} shots, {policy['provider']}, {policy['aspect']}\n"
            f"- published: {', '.join(urls) if urls else policy['destinations']}\n")
    with open(edit_dir / "project.md", "a") as f:
        f.write(note)

    print(f"\ndone in {time.time() - t0:.0f}s")
    for url in urls:
        print(f"  {url}")


if __name__ == "__main__":
    main()
