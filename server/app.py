import os
import re
import shutil
import time
from typing import Any, Dict, Optional, Tuple

import requests
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

GITHUB_OWNER = os.getenv("GITHUB_OWNER", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

# "latest" nutzt /releases/latest (ignoriert prereleases per GitHub-Definition)
# "list" nutzt /releases und nimmt das erste passende (kann prereleases optional filtern)
GITHUB_RELEASES_MODE = os.getenv("GITHUB_RELEASES_MODE", "latest").strip().lower()
GITHUB_INCLUDE_PRERELEASES = os.getenv("GITHUB_INCLUDE_PRERELEASES", "0").strip() == "1"

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "60"))
DOWNLOAD_CACHE_DIR = os.getenv("DOWNLOAD_CACHE_DIR", "/app/cache").strip() or "/app/cache"

SUPPORTED_PLATFORM_EXTENSIONS = {
    "android": ".apk",
    "windows": ".exe",
}

PUBLISHED_FILENAMES = {
    "android": "lunalist_update.apk",
    "windows": "lunalist_update.exe",
}

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
    "downloads": {},
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


def _detect_platform_from_asset_name(name: str) -> Optional[str]:
    lower_name = (name or "").strip().lower()
    for platform, extension in SUPPORTED_PLATFORM_EXTENSIONS.items():
        if lower_name.endswith(extension):
            return platform
    return None


def _safe_filename(name: str) -> str:
    base = os.path.basename((name or "").strip())
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)


def _cache_key(raw_version: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", (raw_version or "").strip()) or "latest"


def _supported_assets(release: Dict[str, Any]) -> list[Dict[str, Any]]:
    assets = release.get("assets") or []
    supported = []

    for asset in assets:
        name = (asset.get("name") or "").strip()
        download_url = (asset.get("browser_download_url") or "").strip()
        platform = _detect_platform_from_asset_name(name)
        if not (name and download_url and platform):
            continue

        supported.append({
            "id": asset.get("id"),
            "name": name,
            "platform": platform,
            "content_type": asset.get("content_type"),
            "size": asset.get("size"),
            "api_url": (asset.get("url") or "").strip(),
            "download_url": download_url,
        })

    return supported


def _download_asset(asset: Dict[str, Any], destination: str) -> None:
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temp_path = f"{destination}.part"

    api_url = (asset.get("api_url") or "").strip()
    if api_url and GITHUB_TOKEN:
        download_target = api_url
        headers = _github_headers()
        headers["Accept"] = "application/octet-stream"
    else:
        download_target = asset["download_url"]
        headers = {"User-Agent": "version-check-server"}

    with requests.get(download_target, headers=headers, timeout=60, stream=True) as response:
        if response.status_code != 200:
            raise RuntimeError(
                f"Asset download failed for {asset['name']}: "
                f"{response.status_code} - {response.text[:300]}"
            )

        with open(temp_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)

    expected_size = asset.get("size")
    actual_size = os.path.getsize(temp_path)
    if isinstance(expected_size, int) and expected_size > 0 and actual_size != expected_size:
        os.remove(temp_path)
        raise RuntimeError(
            f"Downloaded asset size mismatch for {asset['name']}: expected {expected_size}, got {actual_size}"
        )

    os.replace(temp_path, destination)


def _cache_release_assets(release: Dict[str, Any], raw_version: str) -> Dict[str, Dict[str, Any]]:
    supported_assets = _supported_assets(release)
    if not supported_assets:
        return {}

    release_cache_dir = os.path.join(DOWNLOAD_CACHE_DIR, _cache_key(raw_version))
    os.makedirs(release_cache_dir, exist_ok=True)

    cached_assets: Dict[str, Dict[str, Any]] = {}
    for asset in supported_assets:
        filename = _safe_filename(asset["name"])
        local_path = os.path.join(release_cache_dir, filename)

        if not os.path.exists(local_path):
            _download_asset(asset, local_path)
        else:
            expected_size = asset.get("size")
            if isinstance(expected_size, int) and expected_size > 0 and os.path.getsize(local_path) != expected_size:
                _download_asset(asset, local_path)

        cached_assets[asset["platform"]] = {
            "platform": asset["platform"],
            "name": asset["name"],
            "content_type": asset.get("content_type") or "application/octet-stream",
            "download_url": asset["download_url"],
            "size": asset.get("size"),
            "path": local_path,
        }

    return cached_assets


def _cleanup_old_cache(raw_version: str) -> None:
    os.makedirs(DOWNLOAD_CACHE_DIR, exist_ok=True)
    current_key = _cache_key(raw_version)
    for entry in os.listdir(DOWNLOAD_CACHE_DIR):
        entry_path = os.path.join(DOWNLOAD_CACHE_DIR, entry)
        if entry != current_key and os.path.isdir(entry_path):
            shutil.rmtree(entry_path, ignore_errors=True)


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
            cached_downloads = _cache_release_assets(rel, raw)
            _cleanup_old_cache(raw)

            _cache["version"] = v
            _cache["build"] = build
            _cache["raw_version"] = raw
            _cache["etag"] = fetched["etag"]
            _cache["source"] = "github-releases"
            _cache["downloads"] = cached_downloads
            _cache["release"] = {
                "id": rel.get("id"),
                "tag_name": rel.get("tag_name"),
                "name": rel.get("name"),
                "html_url": rel.get("html_url"),
                "download_url": _extract_download_url(rel),
                "assets": [
                    {
                        "platform": asset["platform"],
                        "name": asset["name"],
                        "content_type": asset["content_type"],
                        "api_url": asset["api_url"],
                        "download_url": asset["download_url"],
                        "size": asset["size"],
                    }
                    for asset in _supported_assets(rel)
                ],
                "published_at": rel.get("published_at"),
                "prerelease": rel.get("prerelease"),
                "draft": rel.get("draft"),
            }

        _cache["expires_at"] = now + CACHE_TTL_SECONDS

    except Exception as e:
        _cache["error"] = str(e)
        _cache["last_checked_at"] = int(now)
        _cache["expires_at"] = now + min(CACHE_TTL_SECONDS, 30)


def _extract_download_url(release: Dict[str, Any]) -> Optional[str]:
    supported_assets = _supported_assets(release)
    if supported_assets:
        return supported_assets[0]["download_url"]

    html_url = (release.get("html_url") or "").strip()
    return html_url or None


def _requested_platform() -> Optional[str]:
    platform = (request.args.get("platform") or "").strip().lower()
    if platform in {"apk", "android"}:
        return "android"
    if platform in {"exe", "windows", "win"}:
        return "windows"

    user_agent = (request.headers.get("User-Agent") or "").lower()
    if "android" in user_agent:
        return "android"
    if "windows" in user_agent:
        return "windows"

    return None


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


@app.get("/download")
def download():
    force = request.args.get("force", "0") == "1"
    _refresh_cache(force=force)

    downloads = _cache.get("downloads") or {}
    if not downloads:
        return jsonify({
            "ok": False,
            "error": _cache.get("error") or "No cached APK/EXE available for the latest release.",
            "last_checked_at": _cache.get("last_checked_at"),
        }), 502

    platform = _requested_platform()
    selected = downloads.get(platform) if platform else None

    if selected is None and len(downloads) == 1:
        selected = next(iter(downloads.values()))

    if selected is None:
        return jsonify({
            "ok": False,
            "error": "Multiple cached assets available. Please specify ?platform=android or ?platform=windows.",
            "available_platforms": sorted(downloads.keys()),
            "version": _cache.get("raw_version"),
            "last_checked_at": _cache.get("last_checked_at"),
        }), 400

    return send_file(
        selected["path"],
        as_attachment=True,
        download_name=PUBLISHED_FILENAMES.get(selected["platform"], selected["name"]),
        mimetype=selected["content_type"],
    )
