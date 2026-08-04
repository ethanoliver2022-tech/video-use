"""Credential lookup shared by generate.py and publish.py.

Resolution order for any key:

  1. process environment
  2. `.env` at the video-use repo root
  3. `.env` in the current working directory

Secrets belong in the repo-root `.env`, never in `<videos_dir>/`. The user's
footage directory is theirs; it may be synced, shared, or committed elsewhere.

Usage:
    from envkeys import get_key, require_key, set_key
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_ENV = REPO_ROOT / ".env"


def _parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get_key(name: str, default: str | None = None) -> str | None:
    """Return a credential, or `default` if it is not set anywhere."""
    v = os.environ.get(name)
    if v:
        return v
    for candidate in (REPO_ENV, Path(".env")):
        found = _parse_env(candidate).get(name)
        if found:
            return found
    return default


def require_key(name: str, hint: str = "") -> str:
    """Return a credential or exit with an actionable message."""
    v = get_key(name)
    if not v:
        msg = f"{name} not set (looked in the environment and {REPO_ENV})"
        if hint:
            msg += f"\n  {hint}"
        sys.exit(msg)
    return v


def set_key(name: str, value: str) -> None:
    """Write a credential into the repo-root .env, replacing any existing line.

    Used by the OAuth flows to persist a refresh token after the one-time
    browser step, so later sessions upload unattended.
    """
    lines = REPO_ENV.read_text().splitlines() if REPO_ENV.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.strip() and not line.strip().startswith("#") and "=" in line:
            if line.split("=", 1)[0].strip() == name:
                out.append(f"{name}={value}")
                replaced = True
                continue
        out.append(line)
    if not replaced:
        out.append(f"{name}={value}")
    REPO_ENV.write_text("\n".join(out).rstrip("\n") + "\n")
    try:
        REPO_ENV.chmod(0o600)
    except OSError:
        pass
