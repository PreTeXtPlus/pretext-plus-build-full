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
  # This is the host-level firewall, kept permissive on 80/443 (open to
  # everyone) so it doesn't fight with Caddy/ACME. The actual access
  # restriction -- since this server is only meant to be called by
  # pretext.plus, not browsers -- belongs one layer up, in a DigitalOcean
  # Cloud Firewall that allowlists port 443 to known IPs. See "Restricting
  # access" in the README.
  #
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
       - set SITE_ADDRESS to a DEDICATED subdomain for this server, e.g.
         build-full.pretext.plus (don't reuse the lite server's hostname)
  4. In Cloudflare, point that subdomain's A record at this droplet's IP with
     the proxy OFF ("DNS only" / grey cloud) -- see README "Restricting access"
  5. In the DigitalOcean console, add a Cloud Firewall on this droplet that
     allows port 443 ONLY from known IPs (your testing IP + whoever calls this
     server), and leaves port 80 open to everyone (Let's Encrypt needs it, and
     it never serves anything sensitive)
  6. make up            # build + start caddy, api, worker, redis
  7. make test          # confirm a real build runs end-to-end

Caddy (started by `make up`) handles TLS termination on 80/443; the API itself
is bound to 127.0.0.1 only and is not reachable except through Caddy. This
server is meant to be called machine-to-machine (from pretext.plus), not
browsed to directly -- restrict it with the Cloud Firewall above, not just
BUILD_TOKEN.
EOF
