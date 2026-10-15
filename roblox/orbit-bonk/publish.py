#!/usr/bin/env python3
"""Publish Orbit Bonk straight to Roblox with the Open Cloud Place Publishing API.

Usage:
    export ROBLOX_API_KEY=...      # Open Cloud key with universe-places:write
    python3 publish.py --universe 1234567890 --place 9876543210

The place must already exist (create any experience once in Roblox Studio or
on create.roblox.com, then copy its Universe ID and Place ID).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_FILE = HERE / "build" / "OrbitBonk.rbxlx"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--universe", required=True, help="Universe (experience) ID")
    parser.add_argument("--place", required=True, help="Place ID to overwrite")
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="Place file (.rbxlx or .rbxl)")
    parser.add_argument("--saved", action="store_true", help="Save a version without publishing it live")
    args = parser.parse_args()

    key = os.environ.get("ROBLOX_API_KEY")
    if not key:
        print("Set ROBLOX_API_KEY to an Open Cloud API key first.", file=sys.stderr)
        return 1

    path = Path(args.file)
    if not path.exists():
        print(f"Place file not found: {path}", file=sys.stderr)
        return 1
    content_type = "application/xml" if path.suffix == ".rbxlx" else "application/octet-stream"
    version_type = "Saved" if args.saved else "Published"
    url = (
        f"https://apis.roblox.com/universes/v1/{args.universe}"
        f"/places/{args.place}/versions?versionType={version_type}"
    )
    req = urllib.request.Request(
        url,
        data=path.read_bytes(),
        method="POST",
        headers={"x-api-key": key, "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        print(f"Roblox rejected the upload ({e.code}): {e.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    print(f"{version_type} version {body.get('versionNumber')} of place {args.place}.")
    print(f"https://www.roblox.com/games/{args.place}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
