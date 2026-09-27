#!/usr/bin/env bash
# Pulls the latest pretextbook/pretext-full, rebuilds the warm image, and
# smoke-tests the result directly (using the same sandbox flags the worker
# applies in src/build.py) BEFORE promoting it to the "pretext-plus-build:warm"
# tag the live worker actually reads. This never touches the running
# api/worker/redis stack -- promotion is just a `docker tag`, so the next
# queued job picks it up with no restart needed.
#
# The image that was live before this run is kept as :warm-previous, so a bad
# PreTeXt release can be rolled back with one command:
#   docker tag pretext-plus-build:warm-previous pretext-plus-build:warm
#
# Safe to re-run: on a failed smoke test, :warm (and :warm-previous) are left
# untouched and the script exits non-zero.
#
# Set PRETEXT_VERSION (e.g. PRETEXT_VERSION=2.54.0) to pin the PreTeXt CLI in
# the warm image to that release; otherwise it uses whatever pretext-full ships.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

WARM_IMAGE="pretext-plus-build:warm"
CANDIDATE_IMAGE="pretext-plus-build:warm-candidate"
PREVIOUS_IMAGE="pretext-plus-build:warm-previous"

# Mirrors the .env sandbox defaults (see .env.example) so the smoke test
# proves the candidate under the same constraints real builds run with.
MEM_LIMIT="${BUILD_MEM_LIMIT:-2g}"
PIDS_LIMIT="${BUILD_PIDS_LIMIT:-512}"
TIMEOUT="${BUILD_TIMEOUT:-600}"

echo "==> Pulling latest pretextbook/pretext-full ..."
docker pull pretextbook/pretext-full

PRETEXT_VERSION="${PRETEXT_VERSION:-}"
echo "==> Building candidate warm image ($CANDIDATE_IMAGE)${PRETEXT_VERSION:+ with PreTeXt CLI $PRETEXT_VERSION} ..."
docker build --build-arg "PRETEXT_VERSION=$PRETEXT_VERSION" -t "$CANDIDATE_IMAGE" ./build-image

if [ -n "$PRETEXT_VERSION" ]; then
  actual_version="$(docker run --rm --network none "$CANDIDATE_IMAGE" sh -c "pretext --version" | tail -n 1)"
  if [ "$actual_version" != "$PRETEXT_VERSION" ]; then
    echo "==> Candidate has PreTeXt CLI '$actual_version', expected '$PRETEXT_VERSION'. Leaving '$WARM_IMAGE' untouched."
    exit 1
  fi
fi

tmp_root="$(mktemp -d)"
trap 'rm -rf "$tmp_root"' EXIT
cp -r "$ROOT_DIR/tests/sample" "$tmp_root/sample"
cp -r "$ROOT_DIR/tests/sample-journal" "$tmp_root/sample-journal"
chmod -R 777 "$tmp_root"

# Same prefix as the default BUILD_COMMAND (src/config.py): copy the image's
# pre-fetched journal latex packages to where PreTeXt looks for them.
# TODO: remove once PreTeXt checks ~/.ptx/latex-packages itself.
SEED_LATEX_PACKAGES='if [ -d ~/.ptx/latex-packages ]; then mkdir -p generated-assets/latex-packages && cp -r ~/.ptx/latex-packages/. generated-assets/latex-packages/; fi'

smoke_test_target() {
  local project="$1" target="$2"
  echo "==> Smoke-testing candidate image: project=$project target=$target ..."
  timeout "$TIMEOUT" docker run --rm \
    -v "$tmp_root/$project:/work" \
    -w /work \
    --network none \
    --memory "$MEM_LIMIT" \
    --pids-limit "$PIDS_LIMIT" \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    "$CANDIDATE_IMAGE" \
    sh -c "$SEED_LATEX_PACKAGES; pretext build $target"
}

# tests/sample/project.ptx defines "web" (html) and "print" (pdf) targets --
# the same two the warmup Dockerfile itself builds, so this exercises both
# the HTML and LaTeX/PDF toolchains, which is where PreTeXt releases are most
# likely to introduce a regression. tests/sample-journal builds a PDF in the
# electron-j-combin style, whose texstyle needs a downloaded e-jc.sty -- with
# --network none this only passes if the pre-fetched latex packages are found.
if smoke_test_target sample web \
  && smoke_test_target sample print \
  && smoke_test_target sample-journal print; then
  echo "==> Smoke test passed."
else
  echo "==> Smoke test FAILED. Leaving '$WARM_IMAGE' untouched."
  echo "    Inspect it with: docker run --rm -it $CANDIDATE_IMAGE sh"
  exit 1
fi

if docker image inspect "$WARM_IMAGE" >/dev/null 2>&1; then
  echo "==> Keeping the current image as a rollback target: $PREVIOUS_IMAGE"
  docker tag "$WARM_IMAGE" "$PREVIOUS_IMAGE"
fi

echo "==> Promoting candidate to $WARM_IMAGE ..."
docker tag "$CANDIDATE_IMAGE" "$WARM_IMAGE"
docker rmi "$CANDIDATE_IMAGE" >/dev/null 2>&1 || true

echo "==> Done. New jobs will use the updated $WARM_IMAGE."
echo "    Roll back if needed: docker tag $PREVIOUS_IMAGE $WARM_IMAGE"
