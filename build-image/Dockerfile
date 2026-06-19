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

WORKDIR /opt/warmup
COPY warmup/ ./

# Build both an HTML and a PDF target so every code path runs its first-time
# setup: ~/.ptx population, the asset/npm pipeline (triggered by the
# latex-image), and runestone static fetching.
RUN pretext build web && pretext build print

# Leave a clean workdir; the worker mounts each job at /work.
WORKDIR /work
