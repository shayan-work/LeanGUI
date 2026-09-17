#!/usr/bin/env bash
#
# build.sh - Reproducible build of leandvb and ldpc_tool for LeanGUI.
#
# Background
# ----------
# LeanGUI depends on --fd-gse / --fd-bbf / --drift flags in leandvb that are
# NOT present in the upstream leansdr project's tagged releases (the latest
# tag, 1.2.0, is from 2017/2018). Those flags only exist in unreleased work
# committed to the `work` branch after the 1.2.0 tag. The binaries currently
# vendored at the project root were built from:
#
#     leansdr-1.2.0-110-g84c59e1   (i.e. `git describe` 110 commits past 1.2.0)
#
# which resolves to commit 84c59e1c7a1a79338d5722d63f28640cc9d350f3 - the tip
# of the `work` branch on https://github.com/pabr/leansdr as of this writing
# (confirmed via `git ls-remote`, and by cloning that commit and running
# `leandvb --version`, which reproduces the exact same version string).
#
# leandvb's LDPC decoding is offloaded to a separate helper, `ldpc_tool`,
# which is NOT part of the leansdr repo at all. It comes from a fork of
# xdsopl/LDPC maintained by the same author, on a dedicated branch:
#
#     git clone -b ldpc_tool https://github.com/pabr/xdsopl-LDPC-pabr
#
# That repo has no version tags/describe strings, and the vendored ldpc_tool
# binary carries no embedded version info, so we build from the tip of the
# `ldpc_tool` branch. Its default Makefile targets clang++ with libc++; this
# script overrides that to build with g++ (more commonly available) and
# bumps -std=c++11 to -std=c++17, which is required to fix a real build
# break (see comment near the ldpc_tool build step below) with modern g++.
#
# What this script does
# ----------------------
# 1. Clones/updates leansdr into ./leansdr-src, checked out at the exact
#    commit above, and builds `leandvb` from it.
# 2. Clones/updates the ldpc_tool fork into ./leansdr-src/ldpc-tool-src, and
#    builds `ldpc_tool` from it.
# 3. Copies both resulting binaries into ./build/ at the project root.
#
# This script never touches the `leandvb` / `ldpc_tool` binaries already
# committed/vendored at the project root - it only ever writes into
# ./leansdr-src/ (scratch clone, gitignored) and ./build/ (output,
# gitignored). Re-run any time to validate or refresh the parallel build;
# it is idempotent and safe to re-run.
#
set -euo pipefail

# --- Configuration -----------------------------------------------------

LEANSDR_REPO="https://github.com/pabr/leansdr"
LEANSDR_COMMIT="84c59e1c7a1a79338d5722d63f28640cc9d350f3"

LDPC_TOOL_REPO="https://github.com/pabr/xdsopl-LDPC-pabr"
LDPC_TOOL_BRANCH="ldpc_tool"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${SCRIPT_DIR}/leansdr-src"
LDPC_SRC_DIR="${SRC_DIR}/ldpc-tool-src"
OUT_DIR="${SCRIPT_DIR}/build"

log() { echo "[build.sh] $*"; }

# --- Sanity checks -------------------------------------------------------

for tool in git make g++; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "[build.sh] ERROR: required tool '$tool' not found on PATH." >&2
        echo "[build.sh] On Debian/Ubuntu: sudo apt install git make g++" >&2
        exit 1
    fi
done

# --- Step 1: leandvb (from pabr/leansdr) ---------------------------------

log "Fetching leansdr sources into ${SRC_DIR} ..."
if [ -d "${SRC_DIR}/.git" ]; then
    log "  repo already cloned; fetching updates."
    git -C "${SRC_DIR}" fetch --all --tags
else
    git clone "${LEANSDR_REPO}" "${SRC_DIR}"
fi

log "Checking out leansdr commit ${LEANSDR_COMMIT} ..."
git -C "${SRC_DIR}" checkout --detach "${LEANSDR_COMMIT}"

RESOLVED_DESCRIBE="$(git -C "${SRC_DIR}" describe --tags || echo "unknown")"
log "  leansdr checked out at: $(git -C "${SRC_DIR}" rev-parse HEAD) (describe: ${RESOLVED_DESCRIBE})"

log "Building leandvb (make generic in src/apps) ..."
make -C "${SRC_DIR}/src/apps" generic

# --- Step 2: ldpc_tool (from pabr's xdsopl-LDPC fork) --------------------

log "Fetching ldpc_tool sources into ${LDPC_SRC_DIR} ..."
if [ -d "${LDPC_SRC_DIR}/.git" ]; then
    log "  repo already cloned; fetching updates."
    git -C "${LDPC_SRC_DIR}" fetch origin "${LDPC_TOOL_BRANCH}"
    git -C "${LDPC_SRC_DIR}" checkout "${LDPC_TOOL_BRANCH}"
    git -C "${LDPC_SRC_DIR}" reset --hard "origin/${LDPC_TOOL_BRANCH}"
else
    git clone -b "${LDPC_TOOL_BRANCH}" "${LDPC_TOOL_REPO}" "${LDPC_SRC_DIR}"
fi

log "  ldpc_tool checked out at: $(git -C "${LDPC_SRC_DIR}" rev-parse HEAD)"

# The upstream Makefile defaults to clang++ with libc++ and -std=c++11.
# -std=c++11 fails to build on modern g++/libstdc++ because
# `PhaseShiftKeying::rot_cw`/`rot_acw` are `static constexpr` members that
# are ODR-used (their address is effectively taken via operator* on a
# reference) without an out-of-class definition, which is only an error
# pre-C++17 (in C++17 `static constexpr` members are implicitly `inline`,
# which resolves it). g++ is used instead of clang++ for wider availability.
log "Building ldpc_tool (g++, -std=c++17 fix for static constexpr ODR-use) ..."
make -C "${LDPC_SRC_DIR}" clean
make -C "${LDPC_SRC_DIR}" \
    CXX="g++ -march=native" \
    CXXFLAGS="-std=c++17 -W -Wall -Ofast -fno-exceptions -fno-rtti" \
    ldpc_tool

# --- Step 3: collect outputs ----------------------------------------------

log "Collecting binaries into ${OUT_DIR} ..."
mkdir -p "${OUT_DIR}"
cp -f "${SRC_DIR}/src/apps/leandvb" "${OUT_DIR}/leandvb"
cp -f "${LDPC_SRC_DIR}/ldpc_tool" "${OUT_DIR}/ldpc_tool"

log "Done. Verify with:"
log "  ${OUT_DIR}/leandvb --version"
log "  ${OUT_DIR}/leandvb --help | grep -E 'fd-gse|fd-bbf|drift'"
log "  ${OUT_DIR}/ldpc_tool --help"
log ""
log "Note: this build does NOT overwrite the binaries vendored at the"
log "project root (${SCRIPT_DIR}/leandvb, ${SCRIPT_DIR}/ldpc_tool)."
