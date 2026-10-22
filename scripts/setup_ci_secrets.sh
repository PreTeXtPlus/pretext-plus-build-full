#!/usr/bin/env bash
# One-time setup for the "Update warm PreTeXt image" GitHub Actions workflow
# (.github/workflows/update-warm-image.yml). Run it from YOUR machine, not the
# droplet. It:
#
#   1. generates a dedicated, passphrase-less ed25519 key for the workflow,
#   2. adds its public half to that user's ~/.ssh/authorized_keys on the
#      droplet, replacing any key an earlier run of this script added. If you
#      can already SSH in from this machine it does this itself; otherwise
#      (e.g. you only use DigitalOcean's web console) it prints a one-line
#      command to paste into the droplet's console and waits for you,
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
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
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

# One shell line that drops any earlier workflow key (matched by its comment)
# and appends the new one; authorized_keys is only replaced once the new copy
# is fully written. Run over SSH if we can, else pasted into the web console.
pub="$(cat "$key.pub")"
install_cmd="(umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; { grep -v ' $KEY_COMMENT\$' ~/.ssh/authorized_keys; echo '$pub'; } > ~/.ssh/authorized_keys.new && mv ~/.ssh/authorized_keys.new ~/.ssh/authorized_keys && echo 'Workflow key installed.')"

SSH_OPTS=(-p "$PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)

echo "==> Installing the public key for $SSH_USER@$HOST ..."
if ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$SSH_USER@$HOST" "$install_cmd" 2>/dev/null; then
  echo "    Installed over your existing SSH access."
else
  cat <<EOF

    This machine can't SSH into $SSH_USER@$HOST yet, so install the key by hand:

      1. Open the droplet in the DigitalOcean control panel and launch its
         web console (Access > Launch Droplet Console), logged in as $SSH_USER.
      2. Paste this whole line into the console and press Enter:

$install_cmd

      3. Check that it printed "Workflow key installed.", then come back here.

EOF
  read -r -p "    Press Enter once that's done (Ctrl-C to abort) ... " _
fi

echo "==> Logging in with the new key to check the droplet is ready ..."
if ! ssh "${SSH_OPTS[@]}" -i "$key" -o IdentitiesOnly=yes -o BatchMode=yes "$SSH_USER@$HOST" \
  "sh -s -- '$REPO_PATH'" <<'REMOTE'
set -e
cd "$HOME/$1" 2>/dev/null || { echo "    FAIL: no directory ~/$1 on the droplet (set REPO_PATH)."; exit 1; }
[ -f scripts/update_warm_image.sh ] || { echo "    FAIL: ~/$1 isn't a checkout of this repo."; exit 1; }
git fetch --dry-run >/dev/null 2>&1 || { echo "    FAIL: 'git fetch' fails in ~/$1, so the workflow's 'git pull' would too."; exit 1; }
docker info >/dev/null 2>&1 || { echo "    FAIL: $(whoami) can't run docker without sudo (add it to the docker group, or use another user)."; exit 1; }
echo "    OK: ~/$1 can fetch from GitHub, and $(whoami) can run docker."
REMOTE
then
  echo "==> Stopped: a check above failed, or the droplet refused the new key."
  echo "    If it was refused, make sure the console command ran as $SSH_USER and"
  echo "    printed \"Workflow key installed.\", then re-run this script."
  echo "    No secrets were set."
  exit 1
fi

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
