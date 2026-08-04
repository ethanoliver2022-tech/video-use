---
name: ai-video
description: Turn a spoken idea into a finished AI-generated video and publish it. Interview the idea into a treatment, write a shot brief, generate the shots with Veo/Luma/Runway/Sora, assemble them with the video-use render pipeline, self-evaluate, then upload to YouTube, TikTok, Instagram, a folder, or a webhook. Confirm before spending money and before anything goes public.
---

# AI Video

The other half of video-use. `video-use` edits footage that exists; this makes
footage that doesn't, then sends it where it needs to go.

The user says an idea in one sentence. You return a link.

```
Idea ──> Treatment ──> Brief ──> [confirm] ──> Generate ──> Assemble ──> Self-Eval ──> [confirm] ──> Publish
              │            │                        │                                                     │
              └── ask ─────┘                        └── parallel, cached                       receipt + link
```

## Principle

1. **The idea is not the brief.** A sentence from the user is an intent. The
   brief is a shot-by-shot spec with camera, lighting and continuity language.
   Getting from one to the other is an interview, not a guess.
2. **Two gates, both real.** Generation costs money per second of output.
   Publishing is public and hard to take back. Never cross either without an
   explicit yes to a concrete plan.
3. **Generated shots are footage.** Once on disk they are inputs to the existing
   pipeline — same EDL, same grade, same subtitles, same self-eval. Do not build
   a second renderer.
4. **The model has no memory between shots.** Every call is independent.
   Continuity is something you write into each prompt, not something you request.
5. **Verify before you show, show before you ship.** Self-eval the render. Then
   put the file in front of the user. Then upload.

## Hard Rules (non-negotiable)

1. **Confirm the brief before generating.** Show shot count, total seconds,
   provider and model. Generation is billed per second of output — a silent
   retry of a 6-shot brief is real money.
2. **Confirm destination and privacy before uploading.** Say the platform and
   the privacy setting in plain words and wait. Default to `private` /
   `unlisted` unless the user asks for public in that turn.
3. **Never upload something you have not self-evaluated.** The pipeline in
   `../../SKILL.md` step 7 applies unchanged.
4. **Never re-run generation without `--shots`.** Regenerating a whole brief to
   fix one shot bills the whole brief. Fix one shot: `--shots 03 --force`.
5. **Aspect ratio is a generation-time decision.** Ask where it is going
   *before* the first shot. Cropping 16:9 to 9:16 afterwards throws away half
   the frame and every generated composition is centered for its native ratio.
6. **Credentials go in the repo-root `.env`, never in `<project_dir>/`.** Same
   rule as the ElevenLabs key.
7. **All outputs in `<project_dir>/edit/`.** Never write into the video-use
   repo directory.
8. **Trim the head and tail of every generated shot.** Models routinely spend
   the first ~0.3s settling and the last ~0.3s drifting. The emitted EDL uses
   each clip whole — tighten those ranges before the final render.

## Setup

Beyond the base video-use setup (ffmpeg, Python deps):

- **A generation key.** `GEMINI_API_KEY` for Veo (default). Or `LUMA_API_KEY`,
  `RUNWAY_API_KEY`, `OPENAI_API_KEY`. Ask the user to paste one and write it to
  the repo-root `.env`.
- **Upload credentials, only for the destination they actually want.** Do not
  ask for TikTok credentials if they said YouTube.
- **YouTube first-time authorization** is one interactive step:
  `python helpers/publish.py --auth youtube`. It prints a URL and a short code;
  the user approves on their phone. The refresh token is saved to `.env` and
  every later upload is unattended. It needs an OAuth client of type *TVs and
  Limited Input devices* with the YouTube Data API v3 enabled.

## The process

### 1. Intake

The user gives you an idea. Before anything else, establish four things — from
context if you can, by asking if you can't:

- **Where it goes.** YouTube / Shorts / TikTok / Reels / just a file. This
  fixes aspect ratio and length.
- **How long.** Most hosted models generate 4–8s per call. A 30s video is
  4–6 generated shots, not one call.
- **Who is in it.** Recognizable people are a content-policy problem on every
  provider. Products, places, and abstractions are safe.
- **Voice.** Veo generates native audio and dialogue. Luma, Runway and Sora
  return silent video — if the idea needs narration, plan music or a voiceover
  track and say so now.

### 2. Treatment

Write 3–6 sentences in plain English: the arc, the look, the pacing, how it
ends. No shot numbers yet. This is the cheapest place to be wrong — a bad
treatment caught here costs nothing, caught after generation costs the brief.

### 3. Brief

Turn the treatment into `<project_dir>/edit/brief.json`. One object per shot:

```json
{
  "title": "Hourglass teaser",
  "provider": "veo",
  "aspect": "9:16",
  "resolution": "1080p",
  "shots": [
    {
      "id": "01",
      "beat": "HOOK",
      "duration": 8,
      "prompt": "Slow push in on a cracked brass hourglass on a dark walnut desk. Sand frozen mid-fall. Hard shaft of late afternoon window light from camera left, dust motes drifting. 35mm, shallow depth of field, warm amber grade, cinematic.",
      "negative_prompt": "text, watermark, logos, distorted hands, extra fingers"
    },
    {
      "id": "02",
      "beat": "TURN",
      "duration": 8,
      "prompt": "Same cracked brass hourglass on the same dark walnut desk, same hard window light from camera left. Macro on the neck of the glass as sand begins to fall upward. 35mm, shallow depth of field, warm amber grade, cinematic.",
      "negative_prompt": "text, watermark, logos"
    }
  ]
}
```

**Prompt grammar that works.** Subject → action → camera move → lens →
lighting → grade → style. Concrete nouns beat adjectives: "cracked brass
hourglass on dark walnut" survives generation; "a beautiful timepiece" does not.

**Continuity is copy-paste.** Shot 02 repeats shot 01's subject, surface, light
direction, lens and grade *verbatim*. That repeated clause is the only thing
holding the two shots in the same world. Vary the action and the framing; never
vary the anchors.

**Negative prompts earn their place** on anything with hands, faces, or text.
Generated on-screen text is almost always wrong — put real text on in post with
subtitles or an overlay instead.

### 4. Confirm

Show the user: shot count, total seconds, aspect, provider/model, and the
one-line intent of each shot. Estimate the spend if you know the provider's
rate. **Wait.** `--dry-run` validates the brief and the key without generating:

```bash
python helpers/generate.py edit/brief.json --edit-dir edit --dry-run
```

### 5. Generate

```bash
python helpers/generate.py edit/brief.json --edit-dir edit --emit-edl
```

Shots generate in parallel (3 at a time by default). Each accepted shot is
probed with ffprobe and cached by a hash of its spec, so:

- Re-running costs nothing for unchanged shots.
- Editing one prompt regenerates only that shot.
- A partial failure is resumable: the command prints the exact
  `--shots 02,05` line to retry just the failures, and exits non-zero.

Content filters, not outages, are the usual failure. When a shot is filtered,
rewrite the prompt — remove named people, brands, violence, or the word that
tripped it — and retry that one shot.

### 6. Assemble

`--emit-edl` writes `edit/edl.json` using each clip whole. **Edit it before
rendering** — trim the settling frames (Hard Rule 8), reorder if the cut wants
it, drop a shot that didn't land. Then it is the ordinary pipeline:

```bash
python helpers/render.py edit/edl.json -o edit/preview.mp4 --preview
```

Everything in the parent skill applies: grades, overlays, 30ms audio fades,
subtitles last. If there is narration, build the SRT and burn it — generated
video has no transcript, so write the subtitle text from the script you wrote,
not from Scribe.

### 7. Self-eval

`timeline_view` on the **rendered output** at every cut boundary, exactly as in
the parent skill. For generated footage, also check specifically for:

- **Continuity breaks** at cuts — the subject changing color, size, or material
  between shots is the characteristic AI-video failure.
- **Morphing** inside a shot, usually in the last second.
- **Garbled text or logos** the model invented.
- **Audio mismatch** — a shot with native audio cutting to a silent shot.

Fix by regenerating the offending shot with a tightened prompt (one shot, not
the brief), or by trimming past the artifact in the EDL. Cap at 3 passes, then
tell the user what you could not fix.

### 8. Show, then publish

Put the file in front of the user first (`SendUserFile`), with the destination
and privacy setting written out:

> `final.mp4`, 24s, 1080×1920. Ready to upload to YouTube as **unlisted**,
> titled "Hourglass teaser". Say go and I'll push it.

On yes:

```bash
python helpers/publish.py edit/final.mp4 --to youtube \
  --title "Hourglass teaser" \
  --description "..." --tags launch,ai --privacy unlisted --edit-dir edit
```

Report the URL from the receipt. Every attempt — success or failure — appends to
`edit/publish.json`, so the history of what went where survives the session.

### 9. Persist

Append to `edit/project.md`: the treatment, the final brief, which shots were
regenerated and why, the destination and the resulting URL. Next session starts
from what worked instead of from the idea again.

## Destinations

| `--to` | What happens | Needs |
|---|---|---|
| `youtube` | Resumable upload, returns a `youtu.be` link. Vertical and ≤180s is a Short automatically. | Device-flow OAuth, one time |
| `tiktok` | `inbox` mode (default) lands it in the app's drafts to publish by hand — works with an unaudited app. `direct` posts to the profile and requires TikTok to have audited the app. | `TIKTOK_ACCESS_TOKEN` |
| `instagram` | Publishes a Reel. The Graph API pulls from a **public URL** — it never accepts bytes, so the file must be hosted first (`--video-url`). | Business/Creator account, `IG_USER_ID`, `IG_ACCESS_TOKEN` |
| `local` | Copies to a folder with a metadata sidecar. | `--dest` |
| `webhook` | POSTs the file to any endpoint. | `--url` |

Destinations stack — `--to youtube --to local` does both and reports each
separately. A failure in one does not abort the others.

Always `--dry-run` first on a destination the user has never used. It checks
credentials, title length, aspect and duration against the platform's limits
without uploading, so the first real attempt is not the one that discovers a
missing token twenty minutes into an upload.

## Providers

| `--provider` | Key | Notes |
|---|---|---|
| `veo` (default) | `GEMINI_API_KEY` | Native audio and dialogue. 4/6/8s, 16:9 or 9:16, up to 1080p. The default for anything that needs sound. |
| `luma` | `LUMA_API_KEY` | Fast and cheap. Silent. Good for abstract and stylized b-roll. |
| `runway` | `RUNWAY_API_KEY` | Strongest image-to-video — give it a still and a motion prompt. Silent. |
| `sora` | `OPENAI_API_KEY` | Silent. Needs an OpenAI key with video access. |

Hosted video APIs move fast. Every base URL and model default is overridable
from `.env` — `VEO_API_BASE`, `VEO_MODEL`, `LUMA_MODEL`, and so on — so a
changed endpoint or a newly released model is a one-line config change. If a
provider returns a shape these helpers don't recognize, the error prints the
raw response; read it and adjust rather than guessing.
