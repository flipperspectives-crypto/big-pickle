"""Build identity + static asset fingerprint (safe, read-only).

Exposes a small, evidence-backed build identity for the running gateway:

- ``current_commit``: the 40-char git SHA of the running build, resolved safely
  (env override -> git checkout -> ``"unknown"``). Never the repo path, branch,
  remote, username, hostname, dirty-file names, env vars, or git error text.
- ``checkpoint_tag`` / ``checkpoint_commit``: the frozen, human-validated
  recovery baseline (NOT a claim that the current build equals the checkpoint).
- ``asset_version``: a short content fingerprint of the two static assets
  (``static/app.js`` + ``static/app.css``), so browsers cache-bust automatically
  when either file changes.

Everything here is safe to expose publicly: no secrets, hosts, IPs, paths, or
upstream error text. The git SHA is resolved ONCE and cached; the asset
fingerprint is cheap and recomputed from file contents on demand.
"""
import hashlib
import os
import re
import subprocess

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static"
)

CHECKPOINT_TAG = "clarity-local-v1.0.0"
CHECKPOINT_COMMIT = "f7dd11c4b5b31f44dc0d4f938be8528b8aef8fa0"

_HEX40 = re.compile(r"^[0-9a-f]{40}$")

# Cached git SHA (resolved at most once per process). ``None`` => not yet resolved.
_GIT_SHA = None


def _env_build_sha() -> str | None:
    raw = os.environ.get("CLARITY_BUILD_SHA", "").strip()
    return raw if _HEX40.match(raw) else None


def _git_head_sha() -> str:
    """Resolve the running build's git SHA, cached, sanitized to 40 hex chars.

    Returns ``"unknown"`` if it cannot be determined. Never raises and never
    exposes repo path, branch, remote, username, host, or git error output.
    """
    global _GIT_SHA
    if _GIT_SHA is not None:
        return _GIT_SHA
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
            shell=False,
        )
        sha = proc.stdout.strip() if proc.returncode == 0 else ""
        if _HEX40.match(sha):
            _GIT_SHA = sha
            return _GIT_SHA
    except Exception:
        # Timeout, OSError, or any other failure: fail safe -> unresolved.
        pass
    _GIT_SHA = "unknown"
    return _GIT_SHA


def _reset_build_cache() -> None:
    """Test-only: clear the cached git SHA so resolution re-runs."""
    global _GIT_SHA
    _GIT_SHA = None


def asset_fingerprint(paths: list[str] | None = None) -> str:
    """Short (12-hex) SHA-256 fingerprint of the static assets.

    Default: the contents of ``static/app.js`` + ``static/app.css``. The result
    changes automatically whenever either file's bytes change, providing natural
    cache-busting. An unreadable file still contributes a stable sentinel so the
    fingerprint never throws.
    """
    if paths is None:
        paths = [os.path.join(_STATIC_DIR, n) for n in ("app.js", "app.css")]
    h = hashlib.sha256()
    for p in paths:
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(p.encode("utf-8"))
    return h.hexdigest()[:12]


def get_build_info() -> dict:
    """Public build identity. ``current_commit`` prefers ``CLARITY_BUILD_SHA``
    (when a valid 40-hex) and otherwise the cached git SHA (or ``"unknown"``)."""
    env_sha = _env_build_sha()
    current_commit = env_sha if env_sha else _git_head_sha()
    return {
        "name": "Clarity",
        "current_commit": current_commit,
        "checkpoint_tag": CHECKPOINT_TAG,
        "checkpoint_commit": CHECKPOINT_COMMIT,
        "asset_version": asset_fingerprint(),
    }
