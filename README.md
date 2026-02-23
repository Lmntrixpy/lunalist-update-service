# Lunalist Update Service

A Docker-based update service for Flutter applications.

The service reads the `version` field from a `pubspec.yaml` file via the GitHub API
and exposes it through a REST API so the app can check for updates on startup.

---

## Architecture

- Flask (REST API)
- Gunicorn (Production WSGI Server)
- Nginx (Reverse Proxy)
- Cloudflare Tunnel (Public Access)
- Docker Compose

Network setup:
- `api` → only in `internal_net`
- `nginx` → in both `internal_net` and `external_net`
- Cloudflare Tunnel points to the `nginx` container

---

## API Endpoints

### GET /health

Health check endpoint.

Response:
```json
{
  "ok": true
}
```

---

### GET /version

Returns the current version from the `pubspec.yaml`.

Example:
```json
{
  "ok": true,
  "version": "1.16.1",
  "build": 1,
  "raw": "1.16.1+1",
  "source": "github"
}
```

---

### GET /check?current=1.16.0+5

Compares the current app version with the latest available version.

Example:
```json
{
  "ok": true,
  "update_available": true,
  "current": "1.16.0+5",
  "latest": "1.16.1+1"
}
```

---

## Configuration

Create a `.env` file in the project root directory:

```
GITHUB_OWNER=your_github_username
GITHUB_REPO=your_repository_name
GITHUB_BRANCH=main
GITHUB_PUBSPEC_PATH=pubspec.yaml
GITHUB_TOKEN=github_pat_xxxxxxxxx
CACHE_TTL_SECONDS=60
```

### GitHub Token

Recommended: Fine-grained Personal Access Token with:

Repository permissions → Contents → Read-only

---

## Start

Build and start the containers:

```
docker compose up -d --build
```

Test locally:

```
curl http://localhost:33080/version
```

---

## Cloudflare Tunnel

The tunnel must point to the Nginx container:

```
http://lunalist_update_service_nginx:80
```

Public hostnames must not contain underscores.
Recommended format:

```
lunalist-version.yourdomain.tld
```

---

## Security

- Do not commit `.env`
- Never expose your GitHub token
- Only expose Nginx to the external network
- Do not directly expose the API container

---

## .gitignore Recommendation

```
.env
.DS_Store
._*
__pycache__/
*.pyc
```

---

## Purpose

The app can call the following endpoint on startup:

```
GET https://yourdomain/version
```

Or with version comparison:

```
GET https://yourdomain/check?current=1.16.0+5
```

If `update_available = true`, the app can trigger the update flow.
