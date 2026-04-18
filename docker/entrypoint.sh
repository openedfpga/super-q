#!/usr/bin/env bash
# Docker entrypoint for super-q builds.
#
# Usage:
#   entrypoint.sh <mode> <qdir-relative-to-pwd> <project-name>
#
#   mode          "full" (synth+fit+sta) or "split-fit" (fit+sta from qdb)
#   qdir          path to the Quartus dir inside /work/core
#   project-name  .qpf stem
#
# Env:
#   SUPER_Q_SEED     fitter seed
#   SUPER_Q_QDB      absolute path to a shared .qdb (split-fit only)
#
# Output artifacts are written to /work/out, which the parent process
# mounts from the host or a volume.

set -euo pipefail

MODE="${1:-full}"
QDIR="${2:-src/fpga}"
PROJECT="${3:-${SUPER_Q_PROJECT:?SUPER_Q_PROJECT not set}}"

if [ -z "${QUARTUS_ROOTDIR:-}" ]; then
  echo "entrypoint: QUARTUS_ROOTDIR not set in image" >&2
  exit 2
fi

SANDBOX=/work/out/core
mkdir -p "${SANDBOX}"
# Copy sources to a writable sandbox so parallel seeds don't collide
# when the host mounts /work/core read-only.
rsync -a --delete \
  --exclude='.git' --exclude='.superq' --exclude='output_files' \
  --exclude='db' --exclude='incremental_db' --exclude='qdb' \
  /work/core/ "${SANDBOX}/"

cd "${SANDBOX}/${QDIR}"

export SUPER_Q_PROJECT="${PROJECT}"
export SUPER_Q_SEED="${SUPER_Q_SEED:-1}"

TCL_DIR="/opt/super-q/tcl"
case "${MODE}" in
  full)
    exec quartus_sh -t "${TCL_DIR}/build_seed.tcl" "${PROJECT}"
    ;;
  split-fit)
    if [ -z "${SUPER_Q_QDB:-}" ]; then
      echo "entrypoint: SUPER_Q_QDB required for split-fit mode" >&2
      exit 2
    fi
    exec quartus_sh -t "${TCL_DIR}/fit_from_qdb.tcl" "${PROJECT}"
    ;;
  synth-only)
    exec quartus_sh -t "${TCL_DIR}/synth_only.tcl" "${PROJECT}"
    ;;
  *)
    echo "entrypoint: unknown mode: ${MODE}" >&2
    exit 2
    ;;
esac
