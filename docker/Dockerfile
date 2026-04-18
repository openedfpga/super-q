# super-q Docker image.
#
# This is the only Dockerfile in the repo. It produces a lightweight
# image (~250 MB uncompressed) containing:
#
#   * Python 3.11 + super-q
#   * Quartus runtime dependencies (libc6-i386, libncurses, …)
#   * /usr/local/bin/superq-install-quartus helper
#
# What's **not** in the image: Quartus itself. Intel's redistribution
# terms forbid bundling it in a public image, and at ~15 GB installed it
# would blow past GHA's 10 GB cache limit anyway. Instead, the image
# expects Quartus to be mounted at /opt/intelFPGA_lite/24.1 — either:
#
#   * from an `actions/cache` key in GitHub Actions
#   * from a Docker volume you populated yourself
#   * from a bind mount on a developer workstation
#
# Build with:
#
#   docker buildx build -t ghcr.io/<you>/super-q:24.1 \
#     --build-arg QUARTUS_VERSION=24.1 \
#     -f docker/Dockerfile .
#
# Run a build against a mounted Quartus install:
#
#   docker run --rm \
#     -v "$PWD:/work/core:rw" \
#     -v ~/intelFPGA_lite/24.1:/opt/intelFPGA_lite/24.1:ro \
#     ghcr.io/<you>/super-q:24.1 \
#     superq ci build /work/core --min=1 --max=4

ARG QUARTUS_VERSION=24.1
FROM python:3.11-slim-bookworm

ARG QUARTUS_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    QUARTUS_VERSION=${QUARTUS_VERSION} \
    QUARTUS_ROOTDIR=/opt/intelFPGA_lite/${QUARTUS_VERSION}/quartus \
    PATH=/opt/intelFPGA_lite/${QUARTUS_VERSION}/quartus/bin:/usr/local/bin:/usr/bin:/bin

# ---- apt layer (stable, heavy, rarely changes) -----------------------
# Quartus 24.1 is distributed as 32-bit-capable Linux binaries; we need
# the 32-bit runtime deps on a 64-bit host.
RUN dpkg --add-architecture i386 && \
    apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget git rsync zip unzip xz-utils \
        libc6-i386 libncurses5 libncurses6 libtinfo5 libstdc++6 \
        libxft2 libxrender1 libxtst6 libxi6 libfreetype6 \
        libxcursor1 libxrandr2 libxinerama1 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxext6 libx11-6 \
        libpng16-16 libjpeg62-turbo make bash && \
    rm -rf /var/lib/apt/lists/*

# ---- super-q install layer (this is the one that changes often) -----
WORKDIR /opt/super-q
COPY pyproject.toml README.md ./
COPY src ./src
COPY tcl ./tcl
COPY scripts ./scripts

RUN pip install --no-cache-dir .

# Tiny helper that downloads + installs Quartus into the standard
# location. Designed to be run _inside_ a CI job with a cache attached
# to /opt/intelFPGA_lite/${QUARTUS_VERSION}; idempotent (skips when
# quartus_sh is already present).
COPY docker/install-quartus.sh /usr/local/bin/superq-install-quartus
RUN chmod +x /usr/local/bin/superq-install-quartus

# A non-root user keeps Quartus's silent installer happy and makes the
# image safe to use under stricter CI runners. The entrypoint is left as
# `bash -lc` so the composite GHA Action can string commands together.
RUN useradd -m -u 1000 builder && \
    mkdir -p /opt/intelFPGA_lite/${QUARTUS_VERSION} && \
    chown -R builder:builder /opt/intelFPGA_lite /opt/super-q
USER builder
WORKDIR /work

LABEL org.opencontainers.image.source="https://github.com/openedfpga/super-q"
LABEL org.opencontainers.image.description="super-q: Analogue Pocket core builds with Quartus Lite ${QUARTUS_VERSION}"
LABEL org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["/bin/bash", "-lc"]
CMD ["superq info"]
