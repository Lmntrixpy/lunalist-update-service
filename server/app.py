import base64
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests
import yaml
from flask import Flask, jsonify, request

app = Flask(__name__)

GITHUB_OWNER = os.getenv("GITHUB_OWNER", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_PUBSPEC_PATH = os.getenv("GITHUB_PUBSPEC_PATH", "pubspec.yaml").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

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

def _repo_url() -> str:
    # GitHub Contents API
    # GET /repos/{owner}/{repo}/contents/{path}?ref={branch}
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_PUBSPEC_PATH}"

def _parse_pubspec(pubspec_text: str) -> Tuple[str, Optional[int], str]:
    """
    pubspec.yaml enthält typischerweise:
      version: 1.2.3+45
    Wir geben zurück:
      version_str="1.2.3", build=45, raw="1.2.3+45"
    build ist optional.
    """
    data = yaml.safe_load(pubspec_text) or {}
    raw = str(data.get("version", "")).strip()
    if not raw:
        raise ValueError("No 'version' field found in pubspec.yaml")

    # split at '+'
    if "+" in raw:
        v, b = raw.split("+", 1)
        v = v.strip()
        b = b.strip()
        build = int(b) if b.isdigit() else None
        return v, build, raw
    return raw, None, raw

def _fetch_pubspec_from_github() -> Dict[str, Any]:
    if not (GITHUB_OWNER and GITHUB_REPO and GITHUB_PUBSPEC_PATH):
        raise RuntimeError("Missing GitHub configuration (owner/repo/path).")

    params = {"ref": GITHUB_BRANCH} if GITHUB_BRANCH else {}
    url = _repo_url()

    etag = _cache.get("etag")
    r = requests.get(url, headers=_github_headers(etag), params=params, timeout=15)

    # 304: unverändert, ETag Match
    if r.status_code == 304 and _cache.get("version"):
        return {
            "unchanged": True,
            "etag": etag,
            "pubspec_text": None,
        }

    if r.status_code != 200:
        raise RuntimeError(f"GitHub API error: {r.status_code} - {r.text[:300]}")

    payload = r.json()
    content_b64 = payload.get("content")
    encoding = payload.get("encoding")

    if encoding != "base64" or not content_b64:
        raise RuntimeError("Unexpected GitHub contents response (missing base64 content).")

    pubspec_bytes = base64.b64decode(content_b64)
    pubspec_text = pubspec_bytes.decode("utf-8", errors="replace")

    new_etag = r.headers.get("ETag")

    return {
        "unchanged": False,
        "etag": new_etag,
        "pubspec_text": pubspec_text,
    }

def _refresh_cache(force: bool = False) -> None:
    now = time.time()
    if not force and now < float(_cache.get("expires_at", 0.0)) and _cache.get("version"):
        return

    try:
        fetched = _fetch_pubspec_from_github()
        _cache["last_checked_at"] = int(now)
        _cache["error"] = None

        if fetched["unchanged"] is True:
            _cache["source"] = "github(etag-cache)"
        else:
            pubspec_text = fetched["pubspec_text"]
            v, build, raw = _parse_pubspec(pubspec_text)
            _cache["version"] = v
            _cache["build"] = build
            _cache["raw_version"] = raw
            _cache["etag"] = fetched["etag"]
            _cache["source"] = "github"
        _cache["expires_at"] = now + CACHE_TTL_SECONDS

    except Exception as e:
        # Cache Fehler, aber falls noch ein alter Wert existiert, liefern wir den weiter aus
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

    if not _cache.get("version"):
        return jsonify({
            "ok": False,
            "error": _cache.get("error") or "unknown",
            "last_checked_at": _cache.get("last_checked_at"),
        }), 502

    return jsonify({
        "ok": True,
        "version": _cache.get("version"),        # z.B. "1.2.3"
        "build": _cache.get("build"),            # z.B. 45 oder null
        "raw": _cache.get("raw_version"),        # z.B. "1.2.3+45"
        "source": _cache.get("source"),
        "last_checked_at": _cache.get("last_checked_at"),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "error": _cache.get("error"),            # optional, falls GitHub aktuell Fehler liefert
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

    # sehr einfache Vergleichslogik:
    # Wenn build vorhanden ist, vergleichen wir build numerisch, sonst raw string fallback.
        # SemVer (+ optional build) korrekt vergleichen:
    # 1) major/minor/patch
    # 2) wenn gleich -> build (numerisch, fehlend = 0)
    def parse_version(v: str) -> Tuple[int, int, int, int]:
        v = v.strip()
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