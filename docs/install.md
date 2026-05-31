# Install Job Hunt Hacker

Four supported install paths, ordered from "most flexible" to "most disposable":

1. [From source](#1-from-source) — recommended for developers and Claude Code users.
2. [Docker](#2-docker-single-container) — one command, no Python on the host.
3. [docker-compose](#3-docker-compose) — Docker with friendlier defaults + volumes.
4. [Single binary](#4-single-binary-pyinstaller) — `./jhh`, no Python, no Docker.

All four expose the same UI at <http://127.0.0.1:8731> and the same JSON API under `/api/...`.

---

## 1. From source

This is what `install.sh` automates and what most contributors use.

### Requirements

- Python 3.10+ (3.12 recommended).
- `git`.
- macOS / Linux. Windows works via WSL.

### Steps

```bash
git clone https://github.com/joseantoniopau/Job-Hunt-Hacker.git
cd Job-Hunt-Hacker
./install.sh        # installs deps, seeds the DB, symlinks the skill
./run.sh            # serves http://127.0.0.1:8731
```

`install.sh` accepts `--skip-deps` (don't touch pip) and `--uninstall` (drop the Claude
Code skill symlink). API keys are optional — add them later via the Settings tab or:

```bash
./setup-keys.sh
```

### Manual variant

If `install.sh` doesn't fit (locked-down Python, custom virtualenv, etc.):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8731
```

---

## 2. Docker (single container)

The published image lives at `ghcr.io/joseantoniopau/job-hunt-hacker`. The container
runs as **uid 1000**, exposes port **8731**, and has a `HEALTHCHECK` against
`/api/health` baked in.

```bash
docker run -d \
    --name jhh \
    -p 8731:8731 \
    -v ~/.jhh/data:/app/data \
    -v ~/.jhh/uploads:/app/uploads \
    -v ~/.jhh/resumes:/app/resumes \
    -v ~/.jhh/packets:/app/packets \
    -v ~/.jhh/cache:/app/cache \
    --restart unless-stopped \
    ghcr.io/joseantoniopau/job-hunt-hacker:latest
```

Then open <http://127.0.0.1:8731>.

To pass API keys, either mount an `.env` file:

```bash
docker run ... --env-file ~/.jhh/.env ghcr.io/joseantoniopau/job-hunt-hacker:latest
```

…or set them individually with repeated `-e KEY=value` flags.

### Build the image locally

```bash
git clone https://github.com/joseantoniopau/Job-Hunt-Hacker.git
cd Job-Hunt-Hacker
docker build -t job-hunt-hacker:local .
```

The multi-stage build keeps the runtime image under ~400 MB by:

- Compiling deps in a `builder` stage that's discarded.
- Using `python:3.12-slim` for runtime.
- Installing apt packages with `--no-install-recommends` and only runtime libs
  (no `-dev` headers, no compilers) in the final image.

---

## 3. docker-compose

For volume management, env files, and auto-restart, prefer compose.

```bash
git clone https://github.com/joseantoniopau/Job-Hunt-Hacker.git
cd Job-Hunt-Hacker
cp .env.example .env        # optional — fill in API keys
docker compose up -d        # or: docker-compose up -d
```

The bundled `docker-compose.yml` already wires up:

- Volume mounts for `data/`, `uploads/`, `resumes/`, `packets/`, `cache/`
  so your vault/resumes/packets persist across rebuilds.
- `restart: unless-stopped` so the container survives reboots.
- A `healthcheck` against `/api/health` every 30s.

Tail logs:

```bash
docker compose logs -f jhh
```

Stop & remove:

```bash
docker compose down
```

---

## 4. Single binary (PyInstaller)

For air-gapped machines or "give my non-technical friend an executable", build a
standalone `jhh` binary.

```bash
git clone https://github.com/joseantoniopau/Job-Hunt-Hacker.git
cd Job-Hunt-Hacker
pip install pyinstaller       # only needed for the build itself
scripts/build_binary.sh
./dist/jhh                    # boots on http://127.0.0.1:8731
```

The binary bundles `ui/`, `docs/`, and `data/seed/` so it's self-contained. Override
host/port via env vars:

```bash
JHH_HOST=0.0.0.0 JHH_PORT=9000 ./dist/jhh
```

Build artifacts live under `dist/` (final binary) and `build/` (scratch). Both
are safe to delete between releases.

---

## Updating

Job Hunt Hacker can check GitHub for new releases:

```bash
# CLI — exit code 0 (up-to-date) or 1 (update available)
python scripts/check_for_updates.py

# HTTP — also returns the data the dashboard renders
curl -s http://127.0.0.1:8731/api/updates/check | python -m json.tool
```

Results are cached for 24h so the check doesn't hammer the GitHub API.

---

## Troubleshooting

### Stale SQLite WAL files

If the server crashed mid-write you may end up with `data/jhh.db-wal` /
`data/jhh.db-shm` files that block a clean reopen. Stop the server, then:

```bash
rm -f data/jhh.db-wal data/jhh.db-shm
```

Your data is safe — these are SQLite's write-ahead log sidecars, not the
primary DB. They'll be re-created on the next write.

### Missing optional deps (`lxml`, `python-docx`, `feedparser`, …)

The router system is built to **degrade gracefully**: each router imports
inside a `try/except`, so a missing optional dep disables only its feature.
Check which routers loaded:

```bash
curl -s http://127.0.0.1:8731/api/health | python -m json.tool
```

Look at `routers_loaded` vs `routers_failed`. To fix a failure, install the
specific package the error message mentions, e.g.:

```bash
pip install lxml python-docx feedparser
```

On Apple Silicon, `lxml` sometimes needs `brew install libxml2 libxslt` first.

### "address already in use" on port 8731

Another instance is still bound. Kill it and retry:

```bash
lsof -ti:8731 | xargs kill        # macOS / Linux
./run.sh
```

To run on a different port, just set `JHH_PORT`:

```bash
JHH_PORT=9000 ./run.sh
```

### Docker container restarts in a loop

Almost always a healthcheck failure. Inspect:

```bash
docker logs jhh --tail 100
docker inspect --format '{{json .State.Health}}' jhh | python -m json.tool
```

The most common cause is the bind-mounted `data/` directory being owned by a
UID that doesn't match the container's `uid 1000`. Fix:

```bash
sudo chown -R 1000:1000 ~/.jhh
```

### CORS / browser issues

The server allows all origins by default (`*`). If you're proxying it behind
nginx/Caddy and seeing CORS errors, double-check that **your proxy** isn't
stripping the headers — Job Hunt Hacker itself doesn't enforce any.

### Slow startup on first run

`init_db()` migrates the schema and seeds reference data on first boot. A 5-10s
delay is normal. Subsequent starts should be <1s.
