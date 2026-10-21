#!/usr/bin/env bash
# One-time setup for the "Update warm PreTeXt image" GitHub Actions workflow
# (.github/workflows/update-warm-image.yml). Run it from YOUR machine, not the
# droplet, using the host and user you normally SSH into the droplet as. It:
#
#   1. generates a dedicated, passphrase-less ed25519 key for the workflow,
#   2. adds its public half to that user's ~/.ssh/authorized_keys on the
#      droplet (over your existing SSH access), replacing any key an earlier
#      run of this script added,
#   3. logs in with the NEW key to check that ~/pretext-plus-build-full is a
#      checkout that can fetch from GitHub and that the user can run docker
#      without sudo -- the two things the workflow relies on,
#   4. stores DROPLET_SSH_HOST, DROPLET_SSH_USER, DROPLET_SSH_KEY (and
#      DROPLET_SSH_PORT, if not 22) as this repo's Actions secrets via `gh`,
#   5. deletes the local copy of the private key (GitHub keeps the only one).
#
# Usage:
#   bash scripts/setup_ci_secrets.sh <droplet-ip-or-hostname> <ssh-user> [port]
#
# Set REPO_PATH if the repo isn't at ~/pretext-plus-build-full on the droplet.
# Requires ssh, ssh-keygen, and gh (logged in, with admin access to this repo).
# Safe to re-run: it rotates the workflow's key and overwrites the secrets.
set -euo pipefail

if [ $# -lt 2 ]; then
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi
HOST="$1"
SSH_USER="$2"
PORT="${3:-22}"
REPO_PATH="${REPO_PATH:-pretext-plus-build-full}"
KEY_COMMENT="github-actions-update-warm-image"

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"   # so `gh` targets this repo (from the git remote)

for cmd in ssh ssh-keygen gh; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "Missing required command: $cmd"; exit 1; }
done
gh auth status >/dev/null 2>&1 || { echo "Not logged in to GitHub: run 'gh auth login' first."; exit 1; }
echo "==> Secrets will be stored in: $(gh repo view --json nameWithOwner -q .nameWithOwner)"

key_dir="$(mktemp -d)"
trap 'rm -rf "$key_dir"' EXIT
key="$key_dir/id_ed25519"

echo "==> Generating a dedicated key for the workflow ..."
ssh-keygen -q -t ed25519 -N "" -C "$KEY_COMMENT" -f "$key"

echo "==> Installing the public key for $SSH_USER@$HOST (using your existing SSH access) ..."
# Drops any earlier workflow key (matched by its comment) and appends the new
# one; the file is only replaced once the new copy is fully written.
ssh -p "$PORT" "$SSH_USER@$HOST" "
  set -e
  umask 077
  mkdir -p ~/.ssh
  touch ~/.ssh/authorized_keys
  { grep -v ' $KEY_COMMENT\$' ~/.ssh/authorized_keys || true; cat; } > ~/.ssh/authorized_keys.new
  mv ~/.ssh/authorized_keys.new ~/.ssh/authorized_keys
" < "$key.pub"

echo "==> Logging in with the new key to check the droplet is ready ..."
ssh -p "$PORT" -i "$key" -o IdentitiesOnly=yes -o BatchMode=yes "$SSH_USER@$HOST" \
  "sh -s -- '$REPO_PATH'" <<'REMOTE'
set -e
cd "$HOME/$1" 2>/dev/null || { echo "    FAIL: no directory ~/$1 on the droplet (set REPO_PATH)."; exit 1; }
[ -f scripts/update_warm_image.sh ] || { echo "    FAIL: ~/$1 isn't a checkout of this repo."; exit 1; }
git fetch --dry-run >/dev/null 2>&1 || { echo "    FAIL: 'git fetch' fails in ~/$1, so the workflow's 'git pull' would too."; exit 1; }
docker info >/dev/null 2>&1 || { echo "    FAIL: $(whoami) can't run docker without sudo (add it to the docker group, or use another user)."; exit 1; }
echo "    OK: ~/$1 can fetch from GitHub, and $(whoami) can run docker."
REMOTE

echo "==> Storing the repo secrets ..."
gh secret set DROPLET_SSH_HOST --body "$HOST"
gh secret set DROPLET_SSH_USER --body "$SSH_USER"
gh secret set DROPLET_SSH_KEY < "$key"
if [ "$PORT" != "22" ]; then
  gh secret set DROPLET_SSH_PORT --body "$PORT"
else
  gh secret delete DROPLET_SSH_PORT >/dev/null 2>&1 || true
fi

echo "==> Done. The local copy of the private key has been deleted."
echo "    To catch up on a release the workflow missed, run e.g.:"
echo "      gh workflow run update-warm-image.yml -f pretext_version=2.56.0 && gh run watch"
