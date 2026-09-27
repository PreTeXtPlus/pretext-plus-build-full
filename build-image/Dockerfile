# A "warm" PreTeXt build image. It layers on the official full image and runs a
# throwaway build at IMAGE-BUILD time, which forces PreTeXt's expensive one-time
# setup to happen here (with network available) and bake into the image:
#
#   * the ~/.ptx assets (XSL, core Python, static JS)
#   * node_modules for the asset-generation pipeline (npm install)
#   * runestone static imports
#
# Runtime build containers then start "warm" and need no network. To refresh
# the pinned runestone/npm assets later, just rebuild this image.
FROM pretextbook/pretext-full

# Run warmup (and later, builds) as a single known identity so the baked
# ~/.ptx is found at runtime. If you change this, also set BUILD_USER /
# BUILD_HOME in .env to match.
USER root
ENV HOME=/root

# Optionally pin the PreTeXt CLI to a specific release (e.g. 2.54.0) on top of
# whatever the upstream image ships. The upstream image is rebuilt after each
# CLI release, but on a delay, so this lets a rebuild triggered by that release
# use it straight away. Empty = keep the image's own CLI.
ARG PRETEXT_VERSION=
RUN if [ -n "$PRETEXT_VERSION" ]; then \
      pip install --no-cache-dir --prefer-binary "pretext[all]==$PRETEXT_VERSION"; \
    fi \
 && pretext --version

WORKDIR /opt/warmup
COPY warmup/ ./

# Build both an HTML and a PDF target so every code path runs its first-time
# setup: ~/.ptx population, the asset/npm pipeline (triggered by the
# latex-image), runestone static fetching, and local mathjax build.
RUN pretext build web -v debug && pretext build print -v debug && pretext build epub -v debug

# Leave a clean workdir; the worker mounts each job at /work.
WORKDIR /work
