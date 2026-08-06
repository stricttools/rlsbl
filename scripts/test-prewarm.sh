#!/usr/bin/env bash
#
# rlsbl-specific pre-warm for the sandboxed test runner (scripts/test.sh).
#
# Declared in .rlsbl/config.json under `test_sandbox.prewarm`, this runs
# OUTSIDE the sandbox (network allowed) from the repo root, before the sandbox
# is entered. It exists because the suite builds safegit offline inside the
# sandbox: the pinned module must already sit in the real Go module download
# cache, which the sandbox binds read-only and serves via a file:// GOPROXY.
#
# The runner exports REPO_ROOT, GO_DOWNLOAD_CACHE and GOTOOLCHAIN=local before
# calling this; the fallbacks below keep the script runnable on its own.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export GOTOOLCHAIN="${GOTOOLCHAIN:-local}"
GO_DOWNLOAD_CACHE="${GO_DOWNLOAD_CACHE:-$(go env GOMODCACHE)/cache/download}"

# Minimum Go language version required to build the pinned safegit. MUST match
# (or exceed) safegit's go.mod `go` directive; bump this in lockstep when
# safegit raises its floor. Kept explicit here because the floor must be
# enforced BEFORE the pre-warm, when safegit's .mod may not yet be cached.
GO_MIN_VERSION="1.25.7"

# --- Go version floor: fail loudly and early if the installed toolchain is too
# old to build safegit under GOTOOLCHAIN=local (no download will rescue us). ---
GO_VERSION="$(go env GOVERSION 2>/dev/null | sed 's/^go//')"
if [ -z "${GO_VERSION}" ] || \
   [ "$(printf '%s\n%s\n' "${GO_MIN_VERSION}" "${GO_VERSION}" | sort -V | head -1)" \
     != "${GO_MIN_VERSION}" ]; then
  {
    echo "[prewarm] FATAL: Go ${GO_VERSION:-<unknown>} is too old."
    echo "  Building the pinned safegit needs Go >= ${GO_MIN_VERSION}, and the"
    echo "  sandbox pins GOTOOLCHAIN=local (no toolchain auto-download), so an"
    echo "  older toolchain cannot be silently upgraded."
    echo "  Fix: install Go >= ${GO_MIN_VERSION} (CI: actions/setup-go with a"
    echo "  pinned go-version) before running the suite."
    echo "  go: $(command -v go) ($(go version 2>/dev/null || echo unavailable))"
  } >&2
  exit 1
fi

# Pinned safegit version -- MUST match rlsbl's declared SAFEGIT_MIN_VERSION so
# the safegit_bin fixture's `go install ...@vX` is a cache hit offline.
PIN_RAW="$(grep -oP 'SAFEGIT_MIN_VERSION\s*=\s*\(\K[0-9,\s]+' \
  "${REPO_ROOT}/rlsbl/commands/release_scrub.py")"
SAFEGIT_PIN="v$(echo "${PIN_RAW}" | tr -d ' ' | tr ',' '.')"

# Where a locally-built safegit is staged for the sandbox. The sandbox has no
# network and no view of a sibling safegit checkout, so the only way in is the
# repo tree itself: this dir is gitignored and rsync copies it into the
# throwaway working copy, where tests/conftest.py picks it up.
STAGE_DIR="${REPO_ROOT}/.rlsbl-test-tools"
STAGED_BIN="${STAGE_DIR}/safegit-${SAFEGIT_PIN}"

# Local safegit source checkout, used only when the pin is not published.
SAFEGIT_SRC="${RLSBL_SAFEGIT_SRC:-$(dirname "${REPO_ROOT}")/safegit}"

mkdir -p "${GO_DOWNLOAD_CACHE}"

# A module-cache entry is usable only when ALL THREE files an offline
# `go install` needs are present and non-empty. An interrupted fetch leaves
# the `.info` behind without the `.mod`/`.zip`, so gating on the `.info` alone
# declared a cache hit for a module the sandbox's file:// GOPROXY cannot
# serve -- and the fallback binary was deleted on the strength of that lie,
# leaving the sandbox with neither route and silently skipping every
# real-binary safegit test.
MODULE_CACHE_V="${GO_DOWNLOAD_CACHE}/github.com/smm-h/safegit/@v"
cache_entry_complete() {
  local ext
  for ext in info mod zip; do
    [ -s "${MODULE_CACHE_V}/${SAFEGIT_PIN}.${ext}" ] || return 1
  done
  return 0
}

if cache_entry_complete; then
  echo "[prewarm] safegit ${SAFEGIT_PIN} already in the module cache" >&2
else
  echo "[prewarm] fetching safegit ${SAFEGIT_PIN} into the module cache (network)..." >&2
fi

# Prove the pin is installable BEFORE touching the staged fallback. The probe
# installs into a throwaway GOBIN -- a cache hit when the entry above is
# complete, a real fetch otherwise -- and only a materialized binary counts:
# a corrupt zip, a checksum mismatch or a failed build all exit non-zero or
# produce nothing, and any of those must leave the fallback in place.
PROBE_GOBIN="$(mktemp -d)"
trap 'rm -rf "${PROBE_GOBIN}" "${STAGED_BIN}.new"' EXIT
if GOBIN="${PROBE_GOBIN}" go install "github.com/smm-h/safegit@${SAFEGIT_PIN}" \
   && [ -x "${PROBE_GOBIN}/safegit" ]; then
  # The pin is published and installable, so a locally-built stand-in for it
  # is now a lie. Drop it; the fixture prefers the module anyway, but leaving
  # a stale binary named after a released version invites confusion.
  rm -f "${STAGED_BIN}"
  exit 0
fi

# The module route did not produce a binary -- usually because the pin is
# declared but not published (rlsbl raises SAFEGIT_MIN_VERSION before safegit
# ships it), otherwise because the cached module is unusable. Build it from
# the local checkout with the pinned version stamped in, so the sandbox
# exercises the real binary the floor describes instead of skipping the whole
# real-binary suite.
if [ -f "${SAFEGIT_SRC}/go.mod" ] && \
   grep -q '^module github.com/smm-h/safegit$' "${SAFEGIT_SRC}/go.mod"; then
  # ALWAYS rebuilt, never reused. An unpublished pin names a MOVING local
  # checkout, so a binary staged yesterday is a stale snapshot of a version
  # number that has not shipped yet, and reusing it silently exercises the
  # wrong tool. That is not hypothetical: a safegit CLI change (the confirm
  # protocol's flag rename) went undetected here for a day because the staged
  # build from the day before kept satisfying the same pin.
  echo "[prewarm] safegit ${SAFEGIT_PIN} is not installable from the module" >&2
  echo "          cache or proxy; rebuilding it from" >&2
  echo "          ${SAFEGIT_SRC} and staging it at ${STAGED_BIN}" >&2
  mkdir -p "${STAGE_DIR}"
  # Build beside the staged binary, then rename over it. Building straight
  # onto ${STAGED_BIN} destroys the previous fallback the moment the compiler
  # opens the output file, so a failed build left the sandbox with a
  # truncated binary and no stand-in at all.
  BUILD_TMP="${STAGED_BIN}.new"
  ( cd "${SAFEGIT_SRC}" && go build -o "${BUILD_TMP}" \
      -ldflags "-X main.version=${SAFEGIT_PIN}" . )
  mv -f "${BUILD_TMP}" "${STAGED_BIN}"
  exit 0
fi

# Neither route worked. Do not abort the whole suite over it: the safegit_bin
# fixture skips the real-binary tests with a reason naming the unpublished
# floor, which is louder and more precise than a pre-warm failure here.
{
  echo "[prewarm] WARNING: safegit ${SAFEGIT_PIN} could not be obtained."
  echo "  The module route produced no binary (the floor is not published"
  echo "  yet, or the cached module is unusable) and no local safegit"
  echo "  checkout was found at ${SAFEGIT_SRC}."
  echo "  Real-binary safegit tests will SKIP. Set RLSBL_SAFEGIT_SRC to a"
  echo "  safegit checkout to build the pin locally."
} >&2
