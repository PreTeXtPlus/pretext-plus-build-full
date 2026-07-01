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
- `callback_url` *(optional)* — when set, the worker POSTs the final job status
  here once the build finishes, so the caller can react immediately instead of
  polling `GET /builds/{id}`. The body is HMAC-signed with `CALLBACK_SECRET`
  (header `X-PreTeXt-Signature: sha256=<hex>`) so the receiver can verify it;
  the secret itself is never sent. URLs that resolve to internal/private
  addresses are rejected (SSRF guard). See `CALLBACK_*` in [Configuration](#configuration).

Example:

```bash
curl -X POST http://localhost:8000/builds \
  -F token=testtoken -F target=web \
  -F callback_url=https://pretext.plus/api/build-complete \
  -F 'archive=@project.zip;type=application/zip'
```

## Hosting: why a Droplet, not App Platform

**Use a plain Droplet (a VM). Do _not_ use DigitalOcean App Platform for this
server.**

The reason is structural, not a matter of tuning: the worker builds by spawning
sibling containers through the host's Docker socket (Docker-out-of-Docker). App
Platform — like any PaaS — runs your container in a managed sandbox with **no
access to the host Docker daemon**: you can't mount `/var/run/docker.sock`, run
privileged containers, or spawn sibling containers. That makes our architecture
impossible there, full stop. Secondary problems pile on too: App Platform has
short request timeouts (builds run for minutes), tight image-size limits (the
warm image is multiple GB), small ephemeral disk (builds need real scratch
space), and an always-on per-component cost model that fits spiky CPU-heavy
bursts poorly.

A dedicated Droplet *is* the build host, so handing the worker the Docker socket
(root-equivalent on that box) is acceptable — there are no other tenants to
protect. A PaaS forbids socket access precisely because it would let one tenant
escape into the platform.

**The lightweight server is the opposite case.** [pretext-plus-build](../pretext-plus-build)
is stateless, in-process, and sub-second, with no Docker — an *ideal* App
Platform workload. So the intended topology is:

- **App Platform** → `pretext-plus-build` (snippet/SVG previews): cheap, managed
  TLS, auto-scaling, zero ops.
- **Droplet** → `pretext-plus-build-full` (whole-project builds): full control of
  the Docker runtime.

pretext.plus routes preview requests to the App Platform URL and full-build
requests to the Droplet URL.

If a single Droplet is ever outgrown, the next step is DigitalOcean Kubernetes
(DOKS) running each build as a Kubernetes `Job` — more operational complexity, so
only once a single Droplet measurably falls behind.

## Deploy to a Droplet

### Creating the Droplet (DigitalOcean console)

| Setting | Choice | Why |
|---|---|---|
| Image | Ubuntu 24.04 LTS (plain, not the "Docker on Ubuntu" marketplace image) | `provision.sh` installs Docker itself via `get.docker.com`; the marketplace image's snap-based Docker can conflict with it |
| Droplet type | **Basic** (shared CPU) | The workload is I/O/burst-bound (LaTeX/Sage/npm), not sustained-CPU-bound — no need for CPU-Optimized |
| Size | **4 vCPU / 8 GB / 160 GB SSD** (~$48/mo) to start | Matches the concurrency assumptions in this README; resize up if builds start queuing |
| Backups | Optional | Manual snapshots (see below) cover the "rebuild is slow" problem more cheaply |
| Monitoring | Enable (free) | Watch CPU/mem/disk headroom as concurrent builds run |
| SSH keys | Add yours at creation | Avoid password auth |

Also create a **DigitalOcean Cloud Firewall** (or rely on `provision.sh`'s `ufw`
setup below) allowing only 22 (SSH), 80, and 443 — nothing else needs to be
reachable from the internet.

### Provisioning

```bash
git clone <this repo> && cd pretext-plus-build-full
./scripts/provision.sh        # installs Docker, configures ufw (22/80/443 only),
                               # pre-pulls pretext-full
make warm-image                # bake in PreTeXt's first-run setup (see above)
cp .env.example .env           # set a strong BUILD_TOKEN, switch to REAL MODE,
                               # set SITE_ADDRESS to your domain for HTTPS
make up
```

Take a Droplet **snapshot** once it works — rebuilding from scratch (5GB pull +
warm-image build) is slow; restoring a snapshot is minutes. Concurrency ≈ droplet
RAM ÷ `BUILD_MEM_LIMIT`; scale by running more `worker` replicas
(`docker compose up -d --scale worker=N`).

### TLS / reverse proxy (Caddy)

The stack includes a [Caddy](Caddyfile) service as the public entrypoint. Caddy
does two things: terminates TLS (HTTPS) and reverse-proxies to the `api`
container. The API itself is bound to `127.0.0.1:8000` — reachable on the host
(so `make test` still works) but **not** exposed on the public interface, so all
public traffic must go through Caddy.

`SITE_ADDRESS` (in `.env`) controls how Caddy serves:

- **`:80`** (default) — plain HTTP, no certificates. Fine for local testing.
- **a real domain**, e.g. `build.pretext.plus` — Caddy **automatically obtains
  and renews a Let's Encrypt certificate**, serves HTTPS on 443, and redirects
  80→443. No manual cert wrangling.

To go live with HTTPS:

1. Point a DNS A record (e.g. `build.pretext.plus`) at the Droplet's IP.
2. Set `SITE_ADDRESS=build.pretext.plus` in `.env`.
3. Open ports 80 and 443 in the Droplet's firewall.
4. `make up`. Caddy fetches the cert on first request; issued certs persist in
   the `caddy-data` volume across restarts.

(A DO Load Balancer can do TLS instead, but Caddy keeps it self-contained in the
compose stack with zero cert management.)

## Configuration

All settings are environment variables — see [.env.example](.env.example) for
the full list (auth, build image/command, sandbox limits, storage TTL).

### Completion-callback security

`callback_url` is supplied by the build submitter, so the worker treats it as
untrusted. **In production, set `CALLBACK_ALLOWED_HOSTS` to the host(s) you
actually call back** (e.g. `pretext.plus`) — this is the primary SSRF control:
with it set, the worker will only POST to that host and nowhere else.

As a backstop for the no-allowlist case, callback URLs that resolve to
loopback/private/link-local/reserved IPs are rejected, redirects are disabled,
and the URL is re-validated immediately before the POST. Note this backstop
cannot fully prevent DNS-rebinding (the host can re-resolve between validation
and connection); the allowlist closes that gap, which is why it's the
recommended production control. Set `CALLBACK_ALLOW_PRIVATE_IPS=true` only for
local dev with a localhost/private receiver.
