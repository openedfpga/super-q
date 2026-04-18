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

# Intel's CDN drops old build numbers when a new sub-release ships, so
# the URL below drifts over time. If you see a 404, either:
#   * bump the build number here after checking the current one at
#     https://www.altera.com/downloads/fpga-development-tools/…
#   * or set QUARTUS_URL to your own mirror (Tigris/R2/S3 signed URL).
case "${VERSION}" in
    24.1) DEFAULT_URL="https://downloads.intel.com/akdlm/software/acdsinst/24.1std/1077/ib_tar/Quartus-lite-24.1std.0.1077-linux.tar" ;;
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

# --------------------------------------------------------------------------
# Conservative trim.
#
# Lesson from the Popeye CI runs: anything inside `${PREFIX}/quartus/` is
# dangerous to touch. Quartus is internally cross-linked — `quartus_asm`
# dlopens pgmio which scans `pgmparts/`, binaries share `common/pkgdb`,
# fitter needs `common/devinfo/*`, etc. An "obviously unused" directory
# may be the one quartus_asm crashes without.
#
# So we only drop Quartus SIBLINGS (NiosII, ModelSim, HLS — whole separate
# products with their own installers), plus obviously non-runtime files
# (docs, PDFs, simulation-only `eda/`). Everything inside quartus/common,
# quartus/linux64, quartus/pgmparts, quartus/devinfo stays intact.
#
# Result: ~9 GB install instead of ~21 GB. Fits in GHA's 10 GB cache
# ceiling and still builds a bitstream.
# --------------------------------------------------------------------------
QUARTUS_DIR="${PREFIX}/quartus"
echo "pre-trim size: $(du -sh "${PREFIX}" 2>/dev/null | cut -f1)"

# Siblings of quartus/ — entirely separate products we never touch.
for sib in nios2eds niosv modelsim_ase modelsim_ae questa_fse questa_fe \
           hls hld embedded ip_compiler uninstall logs; do
    rm -rf "${PREFIX}/${sib}" 2>/dev/null || true
done

# Quartus subtrees that are purely simulation-glue or GUI assets. `eda/`
# drives third-party simulators; `docs/`, `help/`, `examples/` are
# documentation and sample projects.
for sub in eda docs help examples; do
    rm -rf "${QUARTUS_DIR}/${sub}" 2>/dev/null || true
done

# Uninstall scaffolding + PDFs/HTML docs scattered around.
find "${QUARTUS_DIR}" -type d -name 'uninstall'    -prune -exec rm -rf {} + 2>/dev/null || true
find "${PREFIX}"      -type f -name '*.log'        -delete 2>/dev/null || true
find "${PREFIX}"      -type f \( -name '*.pdf' -o -name '*.html' -o -name '*.htm' \) \
    -delete 2>/dev/null || true
find "${PREFIX}"      -type d -name '.pfinst-*'    -prune -exec rm -rf {} + 2>/dev/null || true

echo "post-trim size: $(du -sh "${PREFIX}" 2>/dev/null | cut -f1)"
echo "pgmparts present: $([ -d "${QUARTUS_DIR}/pgmparts" ] && echo yes || echo MISSING)"
echo "quartus ${VERSION} installed at ${PREFIX}"
"${SH}" --version 2>/dev/null | head -1 || true
