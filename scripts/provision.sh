#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu droplet. Installs Docker, pre-pulls the
# heavy PreTeXt image so the first real build doesn't time out, and reminds you
# what to configure. Re-running is safe.
set -euo pipefail

BUILD_IMAGE="${BUILD_IMAGE:-pretextbook/pretext-full}"

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker Engine + compose plugin ..."
  curl -fsSL https://get.docker.com | sh
else
  echo "==> Docker already installed."
fi

echo "==> Pre-pulling build image: $BUILD_IMAGE (this is large) ..."
docker pull "$BUILD_IMAGE"

cat <<'EOF'

==> Provisioning done.

Next steps:
  1. cp .env.example .env
  2. Edit .env:
       - set a strong BUILD_TOKEN
       - switch to REAL MODE (uncomment the pretextbook/pretext-full lines)
  3. make up        # build + start api, worker, redis
  4. make test      # confirm a real build runs end-to-end

Put a TLS-terminating reverse proxy (Caddy/nginx) or a DigitalOcean load
balancer in front of port 8000 before exposing it publicly.
EOF
