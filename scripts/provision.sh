#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu droplet. Installs Docker, configures the
# firewall, pre-pulls the heavy PreTeXt image so the first real build doesn't
# time out, and reminds you what to configure. Re-running is safe.
set -euo pipefail

BUILD_IMAGE="${BUILD_IMAGE:-pretextbook/pretext-full}"

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker Engine + compose plugin ..."
  curl -fsSL https://get.docker.com | sh
else
  echo "==> Docker already installed."
fi

if command -v ufw >/dev/null 2>&1; then
  echo "==> Configuring firewall (ufw): allow SSH, 80, 443; deny the rest ..."
  # Allow OpenSSH BEFORE enabling ufw, or a fresh SSH session can get locked
  # out the moment the default-deny policy takes effect.
  ufw allow OpenSSH
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
  ufw status verbose
else
  echo "==> ufw not found; skipping firewall setup (configure manually or use a DO Cloud Firewall)."
fi

echo "==> Pre-pulling build image: $BUILD_IMAGE (this is large) ..."
docker pull "$BUILD_IMAGE"

cat <<'EOF'

==> Provisioning done.

Next steps:
  1. make warm-image   # bake in PreTeXt's first-run setup (npm, runestone, ~/.ptx)
  2. cp .env.example .env
  3. Edit .env:
       - set a strong BUILD_TOKEN
       - switch to REAL MODE (use the pretext-plus-build:warm image)
       - set SITE_ADDRESS to your domain for automatic HTTPS (e.g. build.pretext.plus)
  4. Point DNS at this droplet's IP if using a domain
  5. make up            # build + start caddy, api, worker, redis
  6. make test          # confirm a real build runs end-to-end

Caddy (started by `make up`) handles TLS termination on 80/443; the API itself
is bound to 127.0.0.1 only and is not reachable except through Caddy.
EOF
