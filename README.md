# pretext-plus-build-full

The **full** build server for PreTeXt Plus: builds entire PreTeXt projects
(HTML, PDF via LaTeX, Sage, etc.) by running the official
`pretextbook/pretext-full` image in sandboxed, ephemeral containers.

It is the heavyweight sibling of [pretext-plus-build](../pretext-plus-build),
which renders small HTML/SVG snippets in-process and synchronously. This server
is **asynchronous**: submit a job, poll for status, download the artifact.

## Architecture

```
pretext.plus ──HTTP──> API (FastAPI) ──> Redis (queue + status)
                          │                  │
                          │             Worker (RQ)
                          │                  │ docker run --rm (sandboxed)
                          │             pretext-full container (ephemeral)
                          └── GET status / artifact
```

- **API** — accepts a zipped/tarred project, enqueues a job, serves status and
  artifacts. Has **no** Docker access.
- **Worker** — the only component with the Docker socket. For each job it spawns
  a fresh, locked-down `pretext-full` container (no network, dropped caps,
  CPU/memory/pid/time limits, non-root) to run the build, then zips the output.
- **Redis** — work queue (RQ) and per-job status.

Why a fresh container per build: isolation between users, clean reproducible
state, and the ability to use the official image unmodified.

## Test locally before paying for a droplet

The build backend is swappable via env vars. The default `.env.example` uses a
tiny `alpine` "fake build" so you can validate the whole pipeline in seconds
with no multi-GB download.

```bash
cp .env.example .env      # fake-build mode is the default
make up                   # start api + worker + redis
make test                 # zip the sample project, submit, poll, fetch artifact
make logs                 # watch what's happening
make down
```

You only need Docker + `make` (and `curl`, `zip`, `unzip`, `python3` for the
test script). `make` sets `HOST_DATA_DIR` for you; if you run
`docker compose` directly, first `export HOST_DATA_DIR=$PWD/data`.

**Docker Desktop users:** the worker mounts the host Docker socket. On a Linux
droplet that's `/var/run/docker.sock` (the default). If your local daemon's
socket is elsewhere (e.g. Docker Desktop at `~/.docker/desktop/docker.sock`),
point the mount at it:

```bash
export DOCKER_SOCK=$HOME/.docker/desktop/docker.sock   # check: docker context ls
make up
```

### Switch to real builds (the "warm" image)

`pretextbook/pretext-full` is built to be a long-lived environment: on first use
PreTeXt copies assets to `~/.ptx`, runs `npm install` for its asset pipeline,
and downloads runestone static imports. In an ephemeral-container-per-build
model that work would repeat on *every* build — and the npm/runestone steps need
network, which our sandbox blocks. So instead we bake that setup into a **warm
image** once, at image-build time:

```bash
make pull-real            # docker pull pretextbook/pretext-full (~5GB)
make warm-image           # build pretext-plus-build:warm (runs a throwaway
                          # build so ~/.ptx, node_modules, runestone are baked in)
```

Then edit `.env`: comment the `alpine` lines and uncomment the
`pretext-plus-build:warm` lines, and:

```bash
make up                    # always use `make up`, not `docker compose up`,
                           # so HOST_DATA_DIR gets exported correctly
make test
```

Builds now start warm and need no network (`BUILD_NETWORK=none`). To refresh the
pinned runestone/npm assets later, re-run `make warm-image`. See
[build-image/](build-image/) for the warmup project and Dockerfile.

## API

| Method | Path                     | Purpose                                   |
|--------|--------------------------|-------------------------------------------|
| GET    | `/health`                | liveness                                  |
| POST   | `/builds`                | submit a build → `{job_id, status_url}`   |
| GET    | `/builds/{id}`           | status: `queued`/`running`/`success`/`failed` (+ logs) |
| GET    | `/builds/{id}/artifact`  | download `output.zip` (on success)        |

`POST /builds` is `multipart/form-data`:
- `archive` — `.zip` or `.tar.gz` of the project root (the dir with `project.ptx`)
- `target` — the PreTeXt target name to build (e.g. `web`)
- `token` — the shared `BUILD_TOKEN` (or send `Authorization: Bearer <token>`)

Example:

```bash
curl -X POST http://localhost:8000/builds \
  -F token=testtoken -F target=web \
  -F 'archive=@project.zip;type=application/zip'
```

## Deploy to a DigitalOcean droplet

Start with **4 vCPU / 8 GB** and ample disk (the image alone is several GB).

```bash
git clone <this repo> && cd pretext-plus-build-full
./scripts/provision.sh        # installs Docker, pre-pulls pretext-full
cp .env.example .env          # set a strong BUILD_TOKEN, switch to REAL MODE
make up
```

Front it with a TLS reverse proxy (Caddy/nginx) or a DO load balancer before
exposing publicly. Concurrency ≈ droplet RAM ÷ `BUILD_MEM_LIMIT`; scale by
running more `worker` replicas (`docker compose up -d --scale worker=N`).

## Configuration

All settings are environment variables — see [.env.example](.env.example) for
the full list (auth, build image/command, sandbox limits, storage TTL).
