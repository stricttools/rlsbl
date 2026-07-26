#!/usr/bin/env bash
#
# Layer 2 of the three-layer test sandbox (see tests/conftest.py header).
#
# Canonical way to run the rlsbl test suite. Runs pytest inside a bubblewrap
# (bwrap) sandbox where:
#   - the REAL repo is bound read-only (a stray write to the dev tree -> EROFS),
#   - the suite executes in a WRITABLE throwaway copy of the tree (cwd),
#   - TMPDIR is a private tmpfs, HOME is a throwaway tmpfs,
#   - there is NO network (--unshare-net) -- a real push/clone/API call is
#     physically impossible (ENETUNREACH), on top of the always-on Layer-1
#     env floor and the push/chdir guards,
#   - the Go module cache is bound READ-ONLY and safegit is (re)built offline
#     from it via a file:// proxy; ~/.ssh and other credential paths are simply
#     never bound, so they are unreachable.
#
# The sandbox exports RLSBL_TEST_SANDBOX=1, which lifts conftest's bare-run
# refusal (a bare full-suite `pytest` outside the sandbox is a hard error).
#
# Usage:
#   scripts/test.sh                 # full suite, default args: -q -n auto
#   scripts/test.sh -k foo -x       # forward any pytest args
#   scripts/test.sh --selftest      # prove the sandbox invariants (RO repo, no net)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- resolve the real toolchain paths (bound read-only into the sandbox) ---
UV_BIN="$(command -v uv)"
UV_CACHE="$(uv cache dir)"
UV_PY_DIR="$(uv python dir)"
GOMODCACHE="$(go env GOMODCACHE)"
GO_DOWNLOAD_CACHE="${GOMODCACHE}/cache/download"
# Host GOPATH/bin holds go-installed toolchain binaries the suite shells out to
# (notably gitleaks, used by the release-flow secret-scan tests).
GO_BIN_DIR="$(go env GOPATH)/bin"
# git-filter-repo installs its importable module under the user-site base
# ($HOME/.local/lib/pythonX/site-packages); bind that so extract/absorb tests
# keep working under the throwaway HOME (mirrors Layer-1 PYTHONUSERBASE).
USERBASE="${HOME}/.local"

# overlay-src requires the lower dir to exist; ensure the caches are present
# (a fresh CI runner may not have populated them yet).
mkdir -p "${UV_CACHE}" "${GOMODCACHE}"

# Pinned safegit version -- MUST match rlsbl's declared SAFEGIT_MIN_VERSION so
# the safegit_bin fixture's `go install ...@vX` is a cache hit offline.
PIN_RAW="$(grep -oP 'SAFEGIT_MIN_VERSION\s*=\s*\(\K[0-9,\s]+' \
  "${REPO_ROOT}/rlsbl/commands/release_scrub.py")"
SAFEGIT_PIN="v$(echo "${PIN_RAW}" | tr -d ' ' | tr ',' '.')"

# --- pre-warm (OUTSIDE the sandbox, network allowed): ensure the pinned
# safegit module is in the Go module cache so the in-sandbox offline build
# succeeds. Only fetches when missing. ---
if ! ls "${GO_DOWNLOAD_CACHE}"/github.com/smm-h/safegit/@v/"${SAFEGIT_PIN}".info \
      >/dev/null 2>&1; then
  echo "[sandbox] pre-warming safegit ${SAFEGIT_PIN} module cache (network)..." >&2
  GOBIN="$(mktemp -d)" go install "github.com/smm-h/safegit@${SAFEGIT_PIN}"
fi

# --- writable throwaway working copy of the tree (INCLUDING .git; the suite
# needs real git history). Exclude the venv (absolute paths break on move --
# rebuilt offline inside via `uv sync`) and disposable caches. ---
WORK="$(mktemp -d "${TMPDIR:-/tmp}/rlsbl-sandbox-work.XXXXXX")"
cleanup() { chmod -R u+w "${WORK}" 2>/dev/null || true; rm -rf "${WORK}"; }
trap cleanup EXIT
rsync -a \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.mypy_cache' \
  --exclude '.ruff_cache' \
  --exclude '.wrangler' \
  --exclude 'node_modules' \
  --exclude '.coverage' \
  "${REPO_ROOT}/" "${WORK}/"

# --- inner command: default to a full parallel run, else forward pytest args ---
if [ "${1:-}" = "--selftest" ]; then
  INNER='
    set -u
    fail=0
    echo "[selftest] real repo path should be read-only:"
    if touch "'"${REPO_ROOT}"'/.sandbox-probe" 2>probe.err; then
      echo "  FAIL: wrote to the real repo (expected EROFS)"; fail=1
    else
      echo "  OK: $(cat probe.err)"
    fi
    echo "[selftest] external network should be dead:"
    if python3 - <<PY 2>net.err; then
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
s.connect(("1.1.1.1", 443))
PY
      echo "  FAIL: opened an external TCP connection (expected no network)"; fail=1
    else
      echo "  OK: $(tr "\n" " " <net.err | tail -c 200)"
    fi
    exit $fail
  '
else
  if [ "$#" -eq 0 ]; then
    PYTEST_ARGS="-q -n auto"
  else
    PYTEST_ARGS="$*"
  fi
  INNER="uv sync --offline && uv run --offline pytest ${PYTEST_ARGS}"
fi

echo "[sandbox] bwrap: real repo RO, writable copy=${WORK}, no network, TMPDIR=tmpfs, safegit=${SAFEGIT_PIN}" >&2

# Core binds (always present). Optional toolchain binds are appended only when
# the path exists, so the same script runs on a dev box (git-filter-repo in
# ~/.local, gitleaks in ~/go/bin) and on a bare CI runner (gitleaks in
# /usr/local/bin, no ~/.local/lib) without a hard bwrap failure on a missing dir.
BIND_ARGS=(
  --unshare-net --unshare-pid --unshare-ipc --unshare-uts
  --die-with-parent
  --clearenv
  --ro-bind /usr /usr
  --symlink usr/bin /bin
  --symlink usr/sbin /sbin
  --symlink usr/lib /lib
  --symlink usr/lib64 /lib64
  --ro-bind /etc /etc
  --proc /proc
  --dev /dev
  --tmpfs /tmp
  --ro-bind "${REPO_ROOT}" "${REPO_ROOT}"
  --ro-bind "${UV_BIN}" "${UV_BIN}"
  --ro-bind "${UV_PY_DIR}" "${UV_PY_DIR}"
  --overlay-src "${UV_CACHE}" --tmp-overlay "${UV_CACHE}"
  --overlay-src "${GOMODCACHE}" --tmp-overlay "${GOMODCACHE}"
  --bind "${WORK}" "${WORK}"
  --tmpfs /sandbox-home
  --tmpfs /sandbox-tmp
  --tmpfs /sandbox-gocache
)
for opt in "${USERBASE}/bin" "${USERBASE}/lib" "${GO_BIN_DIR}"; do
  [ -d "${opt}" ] && BIND_ARGS+=(--ro-bind "${opt}" "${opt}")
done

# Forward UV_PYTHON when set (the CI matrix uses it to pin the interpreter the
# in-sandbox `uv sync` selects); left unset, uv picks the default interpreter.
ENV_EXTRA=()
[ -n "${UV_PYTHON:-}" ] && ENV_EXTRA+=(--setenv UV_PYTHON "${UV_PYTHON}")

exec bwrap \
  "${BIND_ARGS[@]}" \
  "${ENV_EXTRA[@]}" \
  --chdir "${WORK}" \
  --setenv HOME /sandbox-home \
  --setenv TMPDIR /sandbox-tmp \
  --setenv PATH "${USERBASE}/bin:${GO_BIN_DIR}:$(dirname "${UV_BIN}"):/usr/local/go/bin:/usr/local/bin:/usr/bin:/usr/sbin" \
  --setenv LANG C.UTF-8 \
  --setenv RLSBL_TEST_SANDBOX 1 \
  --setenv UV_CACHE_DIR "${UV_CACHE}" \
  --setenv UV_PYTHON_INSTALL_DIR "${UV_PY_DIR}" \
  --setenv UV_OFFLINE 1 \
  --setenv UV_LINK_MODE copy \
  --setenv PYTHONUSERBASE "${USERBASE}" \
  --setenv GOMODCACHE "${GOMODCACHE}" \
  --setenv GOCACHE /sandbox-gocache \
  --setenv GOPATH /sandbox-home/go \
  --setenv GOPROXY "file://${GO_DOWNLOAD_CACHE}" \
  --setenv GOSUMDB off \
  bash -c "${INNER}"
