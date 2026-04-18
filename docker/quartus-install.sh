#!/usr/bin/env bash
# Installs Quartus Prime Lite with Cyclone V support inside the Docker build.
#
# Meant to be called from docker/Dockerfile with the EULA mounted as a
# build secret at /run/secrets/altera_eula. Handles the tarball URL
# for each supported release (currently 24.1).

set -euo pipefail

VERSION="${1:?version}"
PREFIX="${2:?install prefix}"
EULA=/run/secrets/altera_eula

if [ ! -f "${EULA}" ]; then
  cat <<EOF >&2
quartus-install: EULA secret missing.

Re-run the docker build with:
  docker build --secret id=altera_eula,src=./EULA.txt ...

Obtain the EULA text from Intel/Altera's site and save it to EULA.txt
after you accept the license. super-q does not redistribute Intel's
installer.
EOF
  exit 3
fi

case "${VERSION}" in
  24.1)
    BASE_URL="https://downloads.intel.com/akdlm/software/acdsinst/24.1std/917/ib_tar"
    TAR_FILE="Quartus-lite-24.1std.0.917-linux.tar"
    CYCLONEV_FILE="cyclonev-24.1std.0.917.qdz"
    ;;
  23.1)
    BASE_URL="https://downloads.intel.com/akdlm/software/acdsinst/23.1std/991/ib_tar"
    TAR_FILE="Quartus-lite-23.1std.0.991-linux.tar"
    CYCLONEV_FILE="cyclonev-23.1std.0.991.qdz"
    ;;
  *)
    echo "quartus-install: unsupported version ${VERSION}" >&2
    exit 2
    ;;
esac

mkdir -p "${PREFIX}" /tmp/quartus
cd /tmp/quartus

if [ ! -f "${TAR_FILE}" ]; then
  curl -fSL -o "${TAR_FILE}" "${BASE_URL}/${TAR_FILE}"
fi
tar -xf "${TAR_FILE}"
chmod +x ./setup.sh

# Non-interactive install: uses the local EULA copy. See Quartus docs
# for exact flag set — they've been stable since 19.x.
./setup.sh \
  --mode unattended \
  --accept_eula 1 \
  --installdir "${PREFIX}" \
  --disable-components quartus_help

# Clean installer junk to slim the image.
rm -rf /tmp/quartus
find "${PREFIX}" -name 'uninstall' -prune -exec rm -rf {} + || true
find "${PREFIX}" -name '*.log' -delete || true
