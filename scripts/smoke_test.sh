#!/usr/bin/env bash
# End-to-end check: zip the sample project, submit it, poll until done, and
# download the artifact. Works in both fake (alpine) and real modes.
#
# Requires: curl, zip, unzip, python3.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
API="${API:-http://localhost:8000}"
TARGET="${TARGET:-web}"
SAMPLE_DIR="$ROOT_DIR/tests/sample"

# BUILD_TOKEN in the shell env wins; otherwise fall back to whatever `make up`
# actually deployed (.env, read by docker compose), so `make test` matches the
# running stack instead of silently using the "testtoken" default and getting
# a 401 from a real token set in .env.
if [ -z "${BUILD_TOKEN:-}" ] && [ -f "$ROOT_DIR/.env" ]; then
  BUILD_TOKEN="$(sed -n 's/^BUILD_TOKEN=//p' "$ROOT_DIR/.env" | tail -1)"
fi
TOKEN="${BUILD_TOKEN:-testtoken}"

jq_get() { python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
archive="$tmp/project.zip"
(cd "$SAMPLE_DIR" && zip -qr "$archive" .)

echo "==> Submitting build to $API ..."
resp="$(curl -sf -X POST "$API/builds" \
  -F "token=$TOKEN" -F "target=$TARGET" \
  -F "archive=@$archive;type=application/zip")"
job_id="$(printf '%s' "$resp" | jq_get job_id)"
[ -n "$job_id" ] || { echo "No job_id in response: $resp"; exit 1; }
echo "    job_id=$job_id"

echo "==> Polling status ..."
for _ in $(seq 1 150); do
  s="$(curl -sf "$API/builds/$job_id")"
  status="$(printf '%s' "$s" | jq_get status)"
  echo "    status=$status"
  case "$status" in
    success)
      curl -sf "$API/builds/$job_id/artifact" -o "$tmp/output.zip"
      echo "==> Artifact contents:"
      unzip -l "$tmp/output.zip"
      echo "==> SUCCESS"
      exit 0 ;;
    failed)
      echo "==> BUILD FAILED. Log:"
      printf '%s' "$s" | jq_get log
      exit 1 ;;
  esac
  sleep 2
done
echo "==> Timed out waiting for build"; exit 1
