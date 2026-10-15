#!/usr/bin/env bash
# End-to-end check of the asset-generation endpoint: zip tests/sample-generate
# (one latex-image, one asymptote, one sageplot figure), POST it to /generate,
# poll until done, print the cache files it produced, and fetch one of them.
# Works in both fake (alpine) and real modes.
#
# ASSETS limits which asset types are generated, e.g. ASSETS=latex-image
# (default: all). Requires: curl, zip, unzip, python3.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
API="${API:-http://localhost:8000}"
TARGET="${TARGET:-web}"
ASSETS="${ASSETS:-}"
SAMPLE_DIR="$ROOT_DIR/tests/sample-generate"

# Same token resolution as smoke_test.sh: shell env wins, else .env.
if [ -z "${BUILD_TOKEN:-}" ] && [ -f "$ROOT_DIR/.env" ]; then
  BUILD_TOKEN="$(sed -n 's/^BUILD_TOKEN=//p' "$ROOT_DIR/.env" | tail -1)"
fi
TOKEN="${BUILD_TOKEN:-testtoken}"

jq_get() { python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
archive="$tmp/project.zip"
(cd "$SAMPLE_DIR" && zip -qr "$archive" .)

echo "==> Submitting generate job to $API (assets: ${ASSETS:-all}) ..."
resp="$(curl -sf -X POST "$API/generate" \
  -F "token=$TOKEN" -F "target=$TARGET" -F "assets=$ASSETS" \
  -F "archive=@$archive;type=application/zip")"
job_id="$(printf '%s' "$resp" | jq_get job_id)"
[ -n "$job_id" ] || { echo "No job_id in response: $resp"; exit 1; }
echo "    job_id=$job_id"

echo "==> Polling status ..."
for _ in $(seq 1 150); do
  s="$(curl -sf "$API/generate/$job_id")"
  status="$(printf '%s' "$s" | jq_get status)"
  echo "    status=$status"
  case "$status" in
    success)
      echo "==> Files (cache-key named):"
      printf '%s' "$s" | python3 -c 'import sys,json; [print("    " + f) for f in json.load(sys.stdin)["files"]]'
      first="$(printf '%s' "$s" | python3 -c 'import sys,json; f=json.load(sys.stdin)["files"]; print(f[0] if f else "")')"
      if [ -n "$first" ]; then
        curl -sf "$API/generate/$job_id/files/$first" -o "$tmp/first"
        echo "==> Fetched $first ($(wc -c < "$tmp/first") bytes) via /files/"
      fi
      curl -sf "$API/generate/$job_id/artifact" -o "$tmp/output.zip"
      echo "==> Artifact contents:"
      unzip -l "$tmp/output.zip"
      echo "==> SUCCESS"
      exit 0 ;;
    failed)
      echo "==> GENERATE FAILED. Log:"
      printf '%s' "$s" | jq_get log
      exit 1 ;;
  esac
  sleep 2
done
echo "==> Timed out waiting for generate job"; exit 1
