import os
import re
import time
from typing import Any, Dict, Optional, Tuple

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

GITHUB_OWNER = os.getenv("GITHUB_OWNER", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

# "latest" nutzt /releases/latest (ignoriert prereleases per GitHub-Definition)
# "list" nutzt /releases und nimmt das erste passende (kann prereleases optional filtern)
GITHUB_RELEASES_MODE = os.getenv("GITHUB_RELEASES_MODE", "latest").strip().lower()
GITHUB_INCLUDE_PRERELEASES = os.getenv("GITHUB_INCLUDE_PRERELEASES", "0").strip() == "1"

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "60"))

_cache: Dict[str, Any] = {
    "expires_at": 0.0,
    "version": None,
    "build": None,
    "raw_version": None,
    "source": None,
    "etag": None,
    "last_checked_at": None,
    "error": None,
    "release": None,  # optional: release metadata (id/name/html_url/etc.)
}


def _github_headers(etag: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "version-check-server",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    if etag:
        headers["If-None-Match"] = etag
    return headers


def _releases_latest_url() -> str:
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"


def _releases_list_url() -> str:
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"


def _parse_tag_to_version(tag: str) -> Tuple[str, Optional[int], str]:
    """
    Erwartet tag_name wie:
      v1.2.3+45
      1.2.3+45
      1.2.3
    Gibt zurück:
      version_str="1.2.3", build=45, raw="1.2.3+45"
    """
    raw = (tag or "").strip()
    if not raw:
        raise ValueError("Empty release tag_name")

    # optional führendes "v"
    raw = re.sub(r"^v", "", raw, flags=re.IGNORECASE).strip()

    # split at '+'
    if "+" in raw:
        v, b = raw.split("+", 1)
        v = v.strip()
        b = b.strip()
        build = int(b) if b.isdigit() else None
        if not _is_semver3(v):
            raise ValueError(f"Invalid version in tag_name: {tag}")
        return v, build, raw

    if not _is_semver3(raw):
        raise ValueError(f"Invalid tag_name format: {tag}. Expected e.g. v1.16.1+3")
    return raw, None, raw


def _is_semver3(v: str) -> bool:
    parts = [p.strip() for p in v.split(".")]
    return len(parts) == 3 and all(p.isdigit() for p in parts)


def _fetch_latest_release_from_github() -> Dict[str, Any]:
    if not (GITHUB_OWNER and GITHUB_REPO):
        raise RuntimeError("Missing GitHub configuration (owner/repo).")

    etag = _cache.get("etag")

    if GITHUB_RELEASES_MODE == "list":
        url = _releases_list_url()
        r = requests.get(url, headers=_github_headers(etag), timeout=15)
        if r.status_code == 304 and _cache.get("raw_version"):
            return {"unchanged": True, "etag": etag, "release": None}

        if r.status_code != 200:
            raise RuntimeError(f"GitHub API error: {r.status_code} - {r.text[:300]}")

        releases = r.json() or []
        # Filter prereleases optional
        candidates = releases if GITHUB_INCLUDE_PRERELEASES else [x for x in releases if not x.get("prerelease")]
        if not candidates:
            raise RuntimeError("No suitable releases found (maybe only prereleases?).")

        rel = candidates[0]
        new_etag = r.headers.get("ETag")
        return {"unchanged": False, "etag": new_etag, "release": rel}

    # default: "latest"
    url = _releases_latest_url()
    r = requests.get(url, headers=_github_headers(etag), timeout=15)

    if r.status_code == 304 and _cache.get("raw_version"):
        return {"unchanged": True, "etag": etag, "release": None}

    if r.status_code != 200:
        raise RuntimeError(f"GitHub API error: {r.status_code} - {r.text[:300]}")

    rel = r.json()
    new_etag = r.headers.get("ETag")
    return {"unchanged": False, "etag": new_etag, "release": rel}


def _refresh_cache(force: bool = False) -> None:
    now = time.time()
    if not force and now < float(_cache.get("expires_at", 0.0)) and _cache.get("raw_version"):
        return

    try:
        fetched = _fetch_latest_release_from_github()
        _cache["last_checked_at"] = int(now)
        _cache["error"] = None

        if fetched["unchanged"] is True:
            _cache["source"] = "github-releases(etag-cache)"
        else:
            rel = fetched["release"] or {}
            tag_name = (rel.get("tag_name") or "").strip()
            v, build, raw = _parse_tag_to_version(tag_name)

            _cache["version"] = v
            _cache["build"] = build
            _cache["raw_version"] = raw
            _cache["etag"] = fetched["etag"]
            _cache["source"] = "github-releases"
            _cache["release"] = {
                "id": rel.get("id"),
                "tag_name": rel.get("tag_name"),
                "name": rel.get("name"),
                "html_url": rel.get("html_url"),
                "published_at": rel.get("published_at"),
                "prerelease": rel.get("prerelease"),
                "draft": rel.get("draft"),
            }

        _cache["expires_at"] = now + CACHE_TTL_SECONDS

    except Exception as e:
        _cache["error"] = str(e)
        _cache["last_checked_at"] = int(now)
        _cache["expires_at"] = now + min(CACHE_TTL_SECONDS, 30)


@app.get("/health")
def health():
    return jsonify({"ok": True}), 200


@app.get("/version")
def version():
    force = request.args.get("force", "0") == "1"
    _refresh_cache(force=force)

    if not _cache.get("raw_version"):
        return jsonify({
            "ok": False,
            "error": _cache.get("error") or "unknown",
            "last_checked_at": _cache.get("last_checked_at"),
        }), 502

    return jsonify({
        "ok": True,
        "version": _cache.get("version"),
        "build": _cache.get("build"),
        "raw": _cache.get("raw_version"),
        "source": _cache.get("source"),
        "release": _cache.get("release"),
        "last_checked_at": _cache.get("last_checked_at"),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "error": _cache.get("error"),
    }), 200


@app.get("/check")
def check():
    """
    Beispiel: /check?current=1.2.0+40
    Antwort: { update_available: true/false, latest: ..., current: ... }
    """
    current = (request.args.get("current") or "").strip()
    if not current:
        return jsonify({"ok": False, "error": "Missing 'current' query param"}), 400

    _refresh_cache(force=False)
    latest_raw = _cache.get("raw_version")
    if not latest_raw:
        return jsonify({"ok": False, "error": _cache.get("error") or "unknown"}), 502

    def parse_version(v: str) -> Tuple[int, int, int, int]:
        v = v.strip()
        v = re.sub(r"^v", "", v, flags=re.IGNORECASE).strip()
        build = 0

        if "+" in v:
            version_part, build_part = v.split("+", 1)
            build_part = build_part.strip()
            if build_part.isdigit():
                build = int(build_part)
        else:
            version_part = v

        parts = [p.strip() for p in version_part.split(".")]
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(f"Invalid version format: {v}. Expected e.g. 1.16.1+3")

        major, minor, patch = (int(parts[0]), int(parts[1]), int(parts[2]))
        return major, minor, patch, build

    try:
        cur = parse_version(current)
        lat = parse_version(latest_raw)

        if lat[:3] > cur[:3]:
            update_available = True
        elif lat[:3] < cur[:3]:
            update_available = False
        else:
            update_available = lat[3] > cur[3]

    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    return jsonify({
        "ok": True,
        "update_available": update_available,
        "current": current,
        "latest": latest_raw,
    }), 200