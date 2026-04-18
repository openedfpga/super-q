#!/usr/bin/env bash
# Idempotent Quartus Lite installer, tuned for CI caching.
#
# Usage:
#   superq-install-quartus [VERSION]      (default: 24.1)
#
# Env:
#   QUARTUS_URL           full URL to the Quartus Lite tar (overrides default)
#   SUPERQ_ACCEPT_EULA    must be "1" — attests the caller has read Intel's
#                         license at
#                         https://www.intel.com/content/www/us/en/legal/end-user-license-agreement.html
#
# Behavior:
#   * If ${QUARTUS_ROOTDIR}/bin/quartus_sh already exists → no-op (exit 0).
#     This is the cache-hit path.
#   * Otherwise download + unattended-install into the standard prefix.
#
# The script never fails on a cache-hit and never re-downloads unnecessarily,
# so dropping it into a GHA step after actions/cache@v4 is safe.

set -euo pipefail

VERSION="${1:-${QUARTUS_VERSION:-24.1}}"
PREFIX="/opt/intelFPGA_lite/${VERSION}"
SH="${PREFIX}/quartus/bin/quartus_sh"

if [ -x "${SH}" ]; then
    echo "quartus already installed at ${PREFIX} (cache hit)"
    "${SH}" --version 2>/dev/null | head -1 || true
    exit 0
fi

if [ "${SUPERQ_ACCEPT_EULA:-}" != "1" ]; then
    cat >&2 <<'EOF'
superq-install-quartus: refusing to install without SUPERQ_ACCEPT_EULA=1.

The caller (CI workflow, Dockerfile, developer) must attest they've read
and accepted Intel's license at:

  https://www.intel.com/content/www/us/en/legal/end-user-license-agreement.html

Set SUPERQ_ACCEPT_EULA=1 in the environment to proceed.
EOF
    exit 2
fi

case "${VERSION}" in
    24.1) DEFAULT_URL="https://downloads.intel.com/akdlm/software/acdsinst/24.1std/917/ib_tar/Quartus-lite-24.1std.0.917-linux.tar" ;;
    23.1) DEFAULT_URL="https://downloads.intel.com/akdlm/software/acdsinst/23.1std/991/ib_tar/Quartus-lite-23.1std.0.991-linux.tar" ;;
    *) echo "unsupported Quartus version: ${VERSION}" >&2; exit 2 ;;
esac
URL="${QUARTUS_URL:-${DEFAULT_URL}}"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
cd "${WORK}"

echo "downloading Quartus ${VERSION} from ${URL}"
curl -fSL --retry 3 -o q.tar "${URL}"
tar -xf q.tar
chmod +x setup.sh

mkdir -p "${PREFIX}"
./setup.sh \
    --mode unattended \
    --accept_eula 1 \
    --installdir "${PREFIX}" \
    --disable-components quartus_help

# Trim obvious fat to keep the cache under GHA's 10 GB ceiling.
# We keep Cyclone V (Pocket target) and drop everything else.
QUARTUS_DIR="${PREFIX}/quartus"
if [ -d "${QUARTUS_DIR}/eda" ]; then
    rm -rf "${QUARTUS_DIR}/eda"
fi
find "${QUARTUS_DIR}" -type d -name 'uninstall'    -prune -exec rm -rf {} + || true
find "${PREFIX}"      -type f -name '*.log'        -delete || true
find "${PREFIX}"      -type d -name '.pfinst-*'    -prune -exec rm -rf {} + || true

# Keep only Cyclone V device libraries (Pocket is 5CEBA4F23C8).
if [ -d "${QUARTUS_DIR}/common/devinfo" ]; then
    pushd "${QUARTUS_DIR}/common/devinfo" >/dev/null
    for d in */; do
        case "${d%/}" in
            cyclonev|cyclone|common|shared) ;;
            *) rm -rf "${d}" ;;
        esac
    done
    popd >/dev/null
fi

echo "quartus ${VERSION} installed at ${PREFIX}"
"${SH}" --version 2>/dev/null | head -1 || true
