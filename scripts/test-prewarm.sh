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
if ls "${GO_DOWNLOAD_CACHE}"/github.com/smm-h/safegit/@v/"${SAFEGIT_PIN}".info \
      >/dev/null 2>&1; then
  echo "[prewarm] safegit ${SAFEGIT_PIN} already in the module cache" >&2
  exit 0
fi

if [ -f "${STAGED_BIN}" ]; then
  echo "[prewarm] safegit ${SAFEGIT_PIN} already staged at ${STAGED_BIN}" >&2
  exit 0
fi

echo "[prewarm] fetching safegit ${SAFEGIT_PIN} into the module cache (network)..." >&2
if GOBIN="$(mktemp -d)" go install "github.com/smm-h/safegit@${SAFEGIT_PIN}"; then
  exit 0
fi

# The pin is declared but not published (rlsbl raises SAFEGIT_MIN_VERSION
# before safegit ships it). Build it from the local checkout with the pinned
# version stamped in, so the sandbox exercises the real binary the floor
# describes instead of skipping the whole real-binary suite.
if [ -f "${SAFEGIT_SRC}/go.mod" ] && \
   grep -q '^module github.com/smm-h/safegit$' "${SAFEGIT_SRC}/go.mod"; then
  echo "[prewarm] safegit ${SAFEGIT_PIN} is not published; building it from" >&2
  echo "          ${SAFEGIT_SRC} and staging it at ${STAGED_BIN}" >&2
  mkdir -p "${STAGE_DIR}"
  ( cd "${SAFEGIT_SRC}" && go build -o "${STAGED_BIN}" \
      -ldflags "-X main.version=${SAFEGIT_PIN}" . )
  exit 0
fi

# Neither route worked. Do not abort the whole suite over it: the safegit_bin
# fixture skips the real-binary tests with a reason naming the unpublished
# floor, which is louder and more precise than a pre-warm failure here.
{
  echo "[prewarm] WARNING: safegit ${SAFEGIT_PIN} could not be obtained."
  echo "  The module proxy does not have it (the floor is not published yet)"
  echo "  and no local safegit checkout was found at ${SAFEGIT_SRC}."
  echo "  Real-binary safegit tests will SKIP. Set RLSBL_SAFEGIT_SRC to a"
  echo "  safegit checkout to build the pin locally."
} >&2
