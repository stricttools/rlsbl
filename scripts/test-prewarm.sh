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

mkdir -p "${GO_DOWNLOAD_CACHE}"
if ! ls "${GO_DOWNLOAD_CACHE}"/github.com/smm-h/safegit/@v/"${SAFEGIT_PIN}".info \
      >/dev/null 2>&1; then
  echo "[prewarm] fetching safegit ${SAFEGIT_PIN} into the module cache (network)..." >&2
  GOBIN="$(mktemp -d)" go install "github.com/smm-h/safegit@${SAFEGIT_PIN}"
else
  echo "[prewarm] safegit ${SAFEGIT_PIN} already in the module cache" >&2
fi
