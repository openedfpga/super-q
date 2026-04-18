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
# Aggressive trim.
#
# Pocket cores only compile for Cyclone V 5CEBA4F23C8 via headless
# quartus_sh. Everything else (simulators, HLS, NiosII, non-Cyclone-V
# devices, docs, PDFs) is shipping weight we pay at cache-restore time
# but never use. Strip it out now so the cache is ~5 GB instead of
# ~15 GB, and so the 10 GB GHA cache ceiling isn't in play.
# --------------------------------------------------------------------------
QUARTUS_DIR="${PREFIX}/quartus"
echo "pre-trim size: $(du -sh "${PREFIX}" 2>/dev/null | cut -f1)"

# Siblings of quartus/ we don't need.
for sib in nios2eds niosv modelsim_ase modelsim_ae questa_fse questa_fe \
           hls hld embedded ip_compiler uninstall logs; do
    rm -rf "${PREFIX}/${sib}" 2>/dev/null || true
done

# Quartus subtrees we don't need. `eda/` is simulation-glue; `sopc_builder/`
# and `dni/` are Qsys; GUI help/docs/pdf are wasted bytes on a headless
# runner; `megafunctions/examples` and `libraries/vhdl` templates aren't
# used by our compile flow.
for sub in eda sopc_builder dni docs help examples pgmparts \
           common/help common/devinfo_html common/pkgdb; do
    rm -rf "${QUARTUS_DIR}/${sub}" 2>/dev/null || true
done

# Only Cyclone V device libraries are needed (Pocket is 5CEBA4F23C8).
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

# linux/ has per-tool binary folders; we only ever call quartus_sh /
# quartus_fit / quartus_syn / quartus_sta / quartus_asm / quartus_cpf
# / quartus_map from our flow. Keep them, toss the rest.
if [ -d "${QUARTUS_DIR}/linux64" ]; then
    pushd "${QUARTUS_DIR}/linux64" >/dev/null
    for f in quartus_*; do
        case "$f" in
            quartus_sh|quartus_fit|quartus_syn|quartus_sta|quartus_asm| \
            quartus_cpf|quartus_map|quartus_cdb|quartus_drc|quartus_eda| \
            quartus_pow|quartus_si|quartus_tan|quartus_jli|quartus_jbcc| \
            quartus_npp|quartus_stp) ;;
            quartus_pgm*|quartus_pgmw*|quartus_gui*|quartus_help*|quartus_ipgenerate*) \
                rm -f "$f" || true ;;
        esac
    done
    popd >/dev/null
fi

find "${QUARTUS_DIR}" -type d -name 'uninstall'    -prune -exec rm -rf {} + 2>/dev/null || true
find "${PREFIX}"      -type f -name '*.log'        -delete 2>/dev/null || true
find "${PREFIX}"      -type f \( -name '*.pdf' -o -name '*.html' -o -name '*.htm' \) \
    -delete 2>/dev/null || true
find "${PREFIX}"      -type d -name '.pfinst-*'    -prune -exec rm -rf {} + 2>/dev/null || true

echo "post-trim size: $(du -sh "${PREFIX}" 2>/dev/null | cut -f1)"
echo "quartus ${VERSION} installed at ${PREFIX}"
"${SH}" --version 2>/dev/null | head -1 || true
