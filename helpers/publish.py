"""Send a finished video where it needs to go, and upload it.

The last mile of the idea → video pipeline. Takes a rendered mp4 and delivers it
to one or more destinations, writing a receipt with the resulting URLs so the
agent can hand the user a link instead of a file path.

Destinations (`--to`, repeatable):

    youtube     YouTube Data API v3 resumable upload   YOUTUBE_CLIENT_ID / _SECRET
    tiktok      TikTok Content Posting API             TIKTOK_ACCESS_TOKEN
    instagram   Instagram Graph API (Reels)            IG_USER_ID / IG_ACCESS_TOKEN
    local       Copy into a folder                     --dest
    webhook     POST the file to a URL                 --url

Auth: YouTube uses the OAuth flow for limited-input devices, so it works over
SSH or in a container with no browser — you get a short code, approve it on your
phone, and the refresh token is saved to the repo-root .env. After that, uploads
are unattended.

Uploads are public actions and hard to take back. Two guards, both deliberate:
`--privacy` defaults to `private` on every platform that has the concept, and
`--dry-run` validates credentials, metadata and platform limits while uploading
nothing. Confirm the destination and the privacy setting with the user before a
real run.

Usage:
    python helpers/publish.py final.mp4 --to youtube --title "..." --dry-run
    python helpers/publish.py final.mp4 --to youtube --title "..." \
        --description "..." --tags launch,demo --privacy unlisted
    python helpers/publish.py final.mp4 --to tiktok --title "..." --to local --dest ~/Uploads
    python helpers/publish.py --auth youtube
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

from envkeys import get_key, set_key

# -------- Platform constants ------------------------------------------------

GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

TIKTOK_API = "https://open.tiktokapis.com/v2"
GRAPH_API = "https://graph.facebook.com/v21.0"

# Resumable chunk size. Google requires a multiple of 256 KB; 8 MB keeps the
# request count low without making a retry expensive on a slow uplink.
CHUNK = 8 * 1024 * 1024

# Advisory limits, checked before upload so a 20-minute upload does not fail at
# the end on a rule we could have caught up front.
LIMITS = {
    "youtube_shorts_seconds": 180,
    "tiktok_seconds": 600,
    "instagram_reels_seconds": 900,
}


class PublishError(RuntimeError):
    """A destination refused the video. Message is meant to be shown to the user."""


def need(name: str, hint: str = "") -> str:
    """Fetch a credential or raise.

    Deliberately not `sys.exit` — destinations stack, and a missing TikTok token
    must not prevent the YouTube upload in the same command from running.
    """
    v = get_key(name)
    if not v:
        msg = f"{name} is not set (looked in the environment and the repo-root .env)"
        if hint:
            msg += f"\n    {hint}"
        raise PublishError(msg)
    return v


def _check(resp: requests.Response, what: str) -> dict:
    if resp.status_code >= 400:
        raise PublishError(f"{what} returned {resp.status_code}: {resp.text[:600]}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        raise PublishError(f"{what} returned non-JSON: {resp.text[:300]}")


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise PublishError(f"ffprobe rejected {path.name}: {out.stderr.strip()[:200]}")
    payload = json.loads(out.stdout or "{}")
    streams = payload.get("streams") or [{}]
    return {
        "duration": float((payload.get("format") or {}).get("duration") or 0.0),
        "width": streams[0].get("width"),
        "height": streams[0].get("height"),
        "size_bytes": path.stat().st_size,
    }


# -------- YouTube -----------------------------------------------------------


def youtube_device_auth() -> None:
    """One-time interactive authorization. Persists a refresh token to .env."""
    client_id = need(
        "YOUTUBE_CLIENT_ID",
        "Create an OAuth client of type 'TVs and Limited Input devices' at "
        "https://console.cloud.google.com/apis/credentials (enable the YouTube Data API v3 first), "
        "then put YOUTUBE_CLIENT_ID=... and YOUTUBE_CLIENT_SECRET=... in the repo-root .env",
    )
    client_secret = need("YOUTUBE_CLIENT_SECRET")

    start = _check(
        requests.post(GOOGLE_DEVICE_CODE_URL,
                      data={"client_id": client_id, "scope": YOUTUBE_SCOPE}, timeout=60),
        "device code request",
    )

    print("\n  Open this page and enter the code:\n")
    print(f"      {start['verification_url']}")
    print(f"      code: {start['user_code']}\n")
    print("  Waiting for approval (Ctrl-C to cancel)...", flush=True)

    interval = float(start.get("interval", 5))
    deadline = time.time() + float(start.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": start["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, timeout=60)
        payload = resp.json() if resp.content else {}
        error = payload.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error:
            raise PublishError(f"authorization failed: {error} — {payload.get('error_description', '')}")
        refresh = payload.get("refresh_token")
        if not refresh:
            raise PublishError("Google returned no refresh token; revoke the app's access and retry")
        set_key("YOUTUBE_REFRESH_TOKEN", refresh)
        print("  authorized — YOUTUBE_REFRESH_TOKEN saved to the repo-root .env")
        return

    raise PublishError("authorization timed out before it was approved")


def youtube_access_token() -> str:
    refresh = get_key("YOUTUBE_REFRESH_TOKEN")
    if not refresh:
        raise PublishError("no YouTube refresh token — run: python helpers/publish.py --auth youtube")
    payload = _check(requests.post(GOOGLE_TOKEN_URL, data={
        "client_id": need("YOUTUBE_CLIENT_ID"),
        "client_secret": need("YOUTUBE_CLIENT_SECRET"),
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }, timeout=60), "token refresh")
    return payload["access_token"]


def publish_youtube(video: Path, meta: dict, info: dict) -> dict:
    token = youtube_access_token()
    size = info["size_bytes"]

    body = {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta.get("description", ""),
            "tags": meta.get("tags") or [],
            "categoryId": meta.get("category_id") or "22",
        },
        "status": {
            "privacyStatus": meta.get("privacy", "private"),
            "selfDeclaredMadeForKids": bool(meta.get("made_for_kids", False)),
        },
    }

    session = requests.post(
        YOUTUBE_UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/*",
        },
        json=body,
        timeout=120,
    )
    if session.status_code >= 400:
        raise PublishError(f"YouTube upload init returned {session.status_code}: {session.text[:600]}")
    upload_url = session.headers.get("Location")
    if not upload_url:
        raise PublishError("YouTube upload init returned no resumable session URL")

    print(f"  uploading {size / 1e6:.1f} MB in {max(1, -(-size // CHUNK))} chunk(s)")
    uploaded = 0
    with open(video, "rb") as f:
        while uploaded < size:
            chunk = f.read(CHUNK)
            last = uploaded + len(chunk)
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {uploaded}-{last - 1}/{size}",
            }
            resp = _put_with_retry(upload_url, chunk, headers)
            if resp.status_code in (200, 201):
                result = resp.json()
                video_id = result["id"]
                url = f"https://youtu.be/{video_id}"
                print(f"  uploaded: {url}")
                return {"video_id": video_id, "url": url, "privacy": body["status"]["privacyStatus"]}
            if resp.status_code != 308:
                raise PublishError(f"YouTube chunk upload returned {resp.status_code}: {resp.text[:400]}")
            # 308 Resume Incomplete: trust the server's byte count over our own.
            rng = resp.headers.get("Range")
            uploaded = int(rng.split("-")[1]) + 1 if rng else last
            f.seek(uploaded)
            print(f"    {uploaded / size * 100:5.1f}%", flush=True)

    raise PublishError("YouTube upload finished the file without returning a video id")


def _put_with_retry(url: str, data: bytes, headers: dict, attempts: int = 4) -> requests.Response:
    """PUT one chunk, retrying transient network and 5xx failures."""
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.put(url, data=data, headers=headers, timeout=900)
            if resp.status_code < 500 or attempt == attempts:
                return resp
        except requests.RequestException:
            if attempt == attempts:
                raise
        time.sleep(delay)
        delay *= 2
    raise PublishError("chunk upload exhausted retries")


# -------- TikTok ------------------------------------------------------------


def publish_tiktok(video: Path, meta: dict, info: dict) -> dict:
    """Upload to TikTok.

    Two modes. `inbox` (default) sends the video to the account's drafts, where
    the user taps publish in the app — it works with an unaudited app. `direct`
    posts straight to the profile and requires TikTok to have audited the app;
    until then TikTok forces private visibility, so --privacy is respected but
    may be overridden by them.
    """
    token = need(
        "TIKTOK_ACCESS_TOKEN",
        "Create an app at https://developers.tiktok.com, add the Content Posting API "
        "product, and put TIKTOK_ACCESS_TOKEN=... in the repo-root .env",
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    size = info["size_bytes"]

    # TikTok requires chunks of 5–64 MB, with the remainder carried by the final
    # chunk rather than sent as a short extra one.
    if size <= 64 * 1024 * 1024:
        chunk_size, chunk_count = size, 1
    else:
        chunk_size = 10 * 1024 * 1024
        chunk_count = size // chunk_size

    source_info = {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": chunk_size,
        "total_chunk_count": chunk_count,
    }

    mode = meta.get("tiktok_mode", "inbox")
    if mode == "direct":
        endpoint = f"{TIKTOK_API}/post/publish/video/init/"
        payload = {
            "post_info": {
                "title": meta["title"][:2200],
                "privacy_level": meta.get("tiktok_privacy", "SELF_ONLY"),
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": source_info,
        }
    else:
        endpoint = f"{TIKTOK_API}/post/publish/inbox/video/init/"
        payload = {"source_info": source_info}

    init = _check(requests.post(endpoint, headers=headers, json=payload, timeout=120), "TikTok init")
    data = init.get("data") or {}
    upload_url, publish_id = data.get("upload_url"), data.get("publish_id")
    if not upload_url or not publish_id:
        raise PublishError(f"TikTok init returned no upload url: {json.dumps(init)[:400]}")

    print(f"  uploading {size / 1e6:.1f} MB in {chunk_count} chunk(s) ({mode} mode)")
    with open(video, "rb") as f:
        for i in range(chunk_count):
            start = i * chunk_size
            # Final chunk absorbs the remainder.
            length = size - start if i == chunk_count - 1 else chunk_size
            f.seek(start)
            chunk = f.read(length)
            resp = _put_with_retry(upload_url, chunk, {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{size}",
            })
            if resp.status_code >= 400:
                raise PublishError(f"TikTok chunk {i} returned {resp.status_code}: {resp.text[:400]}")

    status = _check(
        requests.post(f"{TIKTOK_API}/post/publish/status/fetch/", headers=headers,
                      json={"publish_id": publish_id}, timeout=120),
        "TikTok status fetch",
    )
    state = ((status.get("data") or {}).get("status")) or "UNKNOWN"
    where = "your TikTok drafts (open the app to finish posting)" if mode == "inbox" else "your TikTok profile"
    print(f"  uploaded to {where} — publish_id {publish_id}, status {state}")
    return {"publish_id": publish_id, "status": state, "mode": mode, "url": None}


# -------- Instagram Reels ---------------------------------------------------


def publish_instagram(video: Path, meta: dict, info: dict) -> dict:
    """Publish a Reel.

    The Graph API pulls the file from a public URL — it never accepts bytes — so
    the video must already be hosted somewhere reachable. Pass --video-url.
    """
    ig_user = need(
        "IG_USER_ID",
        "Needs an Instagram Business/Creator account linked to a Facebook Page. "
        "Put IG_USER_ID=... and IG_ACCESS_TOKEN=... in the repo-root .env",
    )
    token = need("IG_ACCESS_TOKEN")
    video_url = meta.get("video_url")
    if not video_url:
        raise PublishError(
            "Instagram needs a publicly reachable URL for the file — pass --video-url. "
            "Upload the mp4 to storage first (S3, R2, GCS), or use --to local/webhook instead."
        )

    container = _check(requests.post(f"{GRAPH_API}/{ig_user}/media", data={
        "media_type": "REELS",
        "video_url": video_url,
        "caption": meta.get("caption") or meta.get("description") or meta["title"],
        "share_to_feed": "true",
        "access_token": token,
    }, timeout=120), "Instagram media container")

    creation_id = container.get("id")
    if not creation_id:
        raise PublishError(f"Instagram returned no creation id: {json.dumps(container)[:300]}")

    print("  waiting for Instagram to ingest the file", flush=True)
    deadline = time.time() + 900
    while True:
        status = _check(requests.get(f"{GRAPH_API}/{creation_id}", params={
            "fields": "status_code,status", "access_token": token,
        }, timeout=60), "Instagram container status")
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise PublishError(f"Instagram ingest failed: {status.get('status') or json.dumps(status)[:300]}")
        if time.time() > deadline:
            raise PublishError("Instagram ingest did not finish within 15 minutes")
        time.sleep(6)

    published = _check(requests.post(f"{GRAPH_API}/{ig_user}/media_publish", data={
        "creation_id": creation_id, "access_token": token,
    }, timeout=120), "Instagram media publish")

    media_id = published.get("id")
    print(f"  published to Instagram — media id {media_id}")
    return {"media_id": media_id, "creation_id": creation_id, "url": None}


# -------- Local + webhook ---------------------------------------------------


def publish_local(video: Path, meta: dict, info: dict) -> dict:
    dest_dir = Path(meta.get("dest") or ".").expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() or c in "-_ " else "" for c in meta["title"]).strip().replace(" ", "-")
    dest = dest_dir / f"{slug or video.stem}{video.suffix}"
    shutil.copy2(video, dest)
    dest.with_suffix(".json").write_text(json.dumps({
        "title": meta["title"],
        "description": meta.get("description", ""),
        "tags": meta.get("tags") or [],
        "probe": info,
    }, indent=2))
    print(f"  copied to {dest}")
    return {"path": str(dest), "url": dest.as_uri()}


def publish_webhook(video: Path, meta: dict, info: dict) -> dict:
    url = meta.get("url")
    if not url:
        raise PublishError("--to webhook needs --url")
    headers = {}
    if meta.get("webhook_token"):
        headers["Authorization"] = f"Bearer {meta['webhook_token']}"
    with open(video, "rb") as f:
        resp = requests.post(url, headers=headers,
                             files={"file": (video.name, f, "video/mp4")},
                             data={"title": meta["title"],
                                   "description": meta.get("description", "")},
                             timeout=1800)
    if resp.status_code >= 400:
        raise PublishError(f"webhook returned {resp.status_code}: {resp.text[:400]}")
    print(f"  posted to {url} ({resp.status_code})")
    return {"url": url, "status_code": resp.status_code, "response": resp.text[:400]}


DESTINATIONS = {
    "youtube": publish_youtube,
    "tiktok": publish_tiktok,
    "instagram": publish_instagram,
    "local": publish_local,
    "webhook": publish_webhook,
}


# -------- Preflight ---------------------------------------------------------


def preflight(dest: str, info: dict, meta: dict) -> list[str]:
    """Advisory checks. Returns warnings; does not block."""
    warn = []
    dur, w, h = info["duration"], info["width"] or 0, info["height"] or 0
    vertical = h > w

    if dest == "youtube":
        if dur <= LIMITS["youtube_shorts_seconds"] and vertical:
            warn.append(f"{dur:.0f}s vertical — will be treated as a Short")
        if len(meta["title"]) > 100:
            warn.append("title exceeds 100 chars and will be truncated")
    if dest == "tiktok":
        if not vertical:
            warn.append(f"{w}x{h} is not vertical — TikTok will letterbox it")
        if dur > LIMITS["tiktok_seconds"]:
            warn.append(f"{dur:.0f}s may exceed the account's TikTok length limit")
        if meta.get("tiktok_mode") == "direct":
            warn.append("direct post requires an audited TikTok app; unaudited apps are forced to private")
    if dest == "instagram":
        if not meta.get("video_url"):
            warn.append("no --video-url; the Graph API pulls from a public URL and cannot accept bytes")
        if not vertical:
            warn.append(f"{w}x{h} is not vertical — Reels expect 9:16")
        if dur > LIMITS["instagram_reels_seconds"]:
            warn.append(f"{dur:.0f}s exceeds the Reels limit")
    return warn


def credential_status(dest: str) -> str:
    needed = {
        "youtube": ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"],
        "tiktok": ["TIKTOK_ACCESS_TOKEN"],
        "instagram": ["IG_USER_ID", "IG_ACCESS_TOKEN"],
        "local": [],
        "webhook": [],
    }[dest]
    missing = [n for n in needed if not get_key(n)]
    return "ready" if not missing else f"MISSING {', '.join(missing)}"


# -------- CLI ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Upload a finished video to its destinations")
    ap.add_argument("video", nargs="?", type=Path, help="Path to the finished mp4")
    ap.add_argument("--auth", choices=["youtube"], help="Run the one-time authorization flow and exit")
    ap.add_argument("--to", action="append", choices=sorted(DESTINATIONS), default=[],
                    help="Destination (repeatable)")
    ap.add_argument("--title", help="Video title")
    ap.add_argument("--description", default="", help="Video description")
    ap.add_argument("--tags", default="", help="Comma-separated tags")
    ap.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"],
                    help="YouTube privacy (default: private)")
    ap.add_argument("--tiktok-mode", default="inbox", choices=["inbox", "direct"],
                    help="inbox = land in TikTok drafts (default), direct = post to profile")
    ap.add_argument("--tiktok-privacy", default="SELF_ONLY",
                    help="TikTok privacy_level for direct mode (default: SELF_ONLY)")
    ap.add_argument("--caption", help="Instagram caption (defaults to the description)")
    ap.add_argument("--video-url", help="Public URL of the file, required by Instagram")
    ap.add_argument("--dest", help="Target folder for --to local")
    ap.add_argument("--url", help="Endpoint for --to webhook")
    ap.add_argument("--webhook-token", help="Bearer token for --to webhook")
    ap.add_argument("--edit-dir", type=Path, help="Where to write publish.json (default: alongside the video)")
    ap.add_argument("--dry-run", action="store_true", help="Check credentials, metadata and limits; upload nothing")
    args = ap.parse_args()

    if args.auth:
        try:
            youtube_device_auth()
        except PublishError as e:
            sys.exit(str(e))
        return

    if not args.video:
        sys.exit("provide the video to publish, or use --auth")
    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")
    if not args.to:
        sys.exit("choose at least one destination with --to")
    if not args.title:
        sys.exit("--title is required")

    info = probe(video)
    meta = {
        "title": args.title,
        "description": args.description,
        "tags": [t.strip() for t in args.tags.split(",") if t.strip()],
        "privacy": args.privacy,
        "tiktok_mode": args.tiktok_mode,
        "tiktok_privacy": args.tiktok_privacy,
        "caption": args.caption,
        "video_url": args.video_url,
        "dest": args.dest,
        "url": args.url,
        "webhook_token": args.webhook_token,
    }

    print(f"{video.name}  {info['duration']:.1f}s  {info['width']}x{info['height']}  "
          f"{info['size_bytes'] / 1e6:.1f} MB")
    print(f"title: {meta['title']}")

    for dest in args.to:
        for w in preflight(dest, info, meta):
            print(f"  ! {dest}: {w}")

    if args.dry_run:
        print("\ndry run — credentials:")
        for dest in args.to:
            print(f"  {dest:<10} {credential_status(dest)}")
        print("nothing uploaded")
        return

    receipts, failures = [], []
    for dest in args.to:
        print(f"\n→ {dest}")
        try:
            result = DESTINATIONS[dest](video, meta, info)
            receipts.append({"destination": dest, "status": "ok",
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **result})
        except PublishError as e:
            print(f"  FAILED: {e}")
            failures.append(dest)
            receipts.append({"destination": dest, "status": "failed", "error": str(e),
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
            failures.append(dest)
            receipts.append({"destination": dest, "status": "failed",
                             "error": f"{type(e).__name__}: {e}",
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})

    edit_dir = (args.edit_dir or video.parent).resolve()
    edit_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = edit_dir / "publish.json"
    history = []
    if receipt_path.exists():
        try:
            history = json.loads(receipt_path.read_text())
        except ValueError:
            history = []
    history.append({"video": str(video), "title": meta["title"], "probe": info, "results": receipts})
    receipt_path.write_text(json.dumps(history, indent=2))

    print(f"\nreceipt: {receipt_path}")
    for r in receipts:
        if r.get("url"):
            print(f"  {r['destination']}: {r['url']}")

    if failures:
        sys.exit(f"failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
