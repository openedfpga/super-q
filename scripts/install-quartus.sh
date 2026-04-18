#!/usr/bin/env bash
# Install Quartus Prime Lite locally (for the `local` backend).
#
# Usage:
#   bash scripts/install-quartus.sh --version=24.1 --target=$HOME/intelFPGA_lite
#
# This is a thin wrapper around Intel's installer. It does not try to
# accept the EULA for you — you must have accepted Intel's license
# separately. We just streamline the download and unpack, then append
# the correct $QUARTUS_ROOTDIR to your shell rc files.

set -euo pipefail

VERSION="24.1"
TARGET="${HOME}/intelFPGA_lite"
ACCEPT_EULA="0"
DRY_RUN="0"

for arg in "$@"; do
  case "$arg" in
    --version=*) VERSION="${arg#*=}" ;;
    --target=*)  TARGET="${arg#*=}" ;;
    --accept-eula) ACCEPT_EULA="1" ;;
    --dry-run)   DRY_RUN="1" ;;
    --help|-h)
      cat <<EOF
install-quartus.sh — install Quartus Prime Lite for super-q

  --version=<ver>     24.1 (default) | 23.1
  --target=<dir>      install prefix (default: ~/intelFPGA_lite)
  --accept-eula       set if you have read and accepted Intel's EULA
  --dry-run           print commands without executing

Supported on Linux (amd64). On macOS, prefer the Docker backend —
Quartus Lite does not run natively on arm64 macOS.
EOF
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 1 ;;
  esac
done

run() {
  echo "+ $*"
  [ "$DRY_RUN" = "1" ] || "$@"
}

OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  cat <<EOF >&2
Quartus Lite is not supported natively on macOS. Options:

  1. Use the Docker backend: \`superq sweep . --backend=docker\`
     (see docker/README.md to build the image with a pre-downloaded bundle)
  2. Use the AWS backend: \`superq sweep . --backend=aws\`
  3. Run inside a Linux VM (UTM, Multipass, Lima, …)

If you really want to proceed anyway, set SUPERQ_FORCE_MAC=1.
EOF
  [ "${SUPERQ_FORCE_MAC:-}" = "1" ] || exit 2
fi

case "$VERSION" in
  24.1) INSTALLER="Quartus-lite-24.1std.0.917-linux.tar" ;;
  23.1) INSTALLER="Quartus-lite-23.1std.0.991-linux.tar" ;;
  *) echo "unsupported version: $VERSION" >&2; exit 2 ;;
esac

mkdir -p "$TARGET"
cd "$TARGET"

if [ ! -f "$INSTALLER" ]; then
  echo "Downloading $INSTALLER (this is ~8 GB — grab a coffee)…"
  URL="https://downloads.intel.com/akdlm/software/acdsinst/${VERSION}std/ib_tar/${INSTALLER}"
  run curl -fL -o "$INSTALLER.tmp" "$URL"
  mv "$INSTALLER.tmp" "$INSTALLER"
fi

run tar -xf "$INSTALLER"
run chmod +x ./setup.sh

if [ "$ACCEPT_EULA" != "1" ]; then
  cat <<'EOF' >&2
Re-run with --accept-eula after reading:
  https://www.intel.com/content/www/us/en/legal/end-user-license-agreement.html
EOF
  exit 2
fi

run ./setup.sh \
  --mode unattended \
  --accept_eula 1 \
  --installdir "$TARGET/${VERSION}" \
  --disable-components quartus_help

echo
echo "Quartus installed to $TARGET/${VERSION}"
echo
cat <<EOF
# Add these to your shell rc (.bashrc / .zshrc):
export QUARTUS_ROOTDIR="$TARGET/${VERSION}/quartus"
export PATH="\$QUARTUS_ROOTDIR/bin:\$PATH"
EOF

mkdir -p "$HOME/.superq"
cat >"$HOME/.superq/env" <<EOF
export QUARTUS_ROOTDIR="$TARGET/${VERSION}/quartus"
export PATH="\$QUARTUS_ROOTDIR/bin:\$PATH"
EOF
echo "Wrote ~/.superq/env — source it from your shell rc to pick up \$QUARTUS_ROOTDIR."
