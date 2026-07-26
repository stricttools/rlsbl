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
#   - the Go module DOWNLOAD cache is bound READ-ONLY and served to the offline
#     safegit (re)build via a file:// GOPROXY, with GOMODCACHE pointed at a
#     writable tmpfs so `go` extracts into the sandbox while the real cache stays
#     immutable; the UV cache is a throwaway copy-on-write clone (uv insists on
#     writing locks/markers and caching the local build), so the real dev cache
#     is likewise never mutated; ~/.ssh and other credential paths are simply
#     never bound, so they are unreachable.
#
# Portability: this sandbox uses only universally-supported bwrap options
# (--ro-bind/--bind/--tmpfs/--proc/--dev/--symlink/--unshare-*/--die-with-parent/
# --clearenv/--setenv/--chdir). It deliberately avoids --overlay-src/--tmp-overlay
# (overlayfs), which the stock apt bubblewrap on CI runners is built without.
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

# The read-only cache binds require their source dirs to exist; ensure the
# caches are present (a fresh CI runner may not have populated them yet).
mkdir -p "${UV_CACHE}" "${GO_DOWNLOAD_CACHE}"

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
# --- writable throwaway clone of the uv cache. uv refuses to run fully
# read-only even in offline + UV_LINK_MODE=copy mode: it touches a root `.lock`
# and per-bucket `.git` markers, and it caches the freshly-built local `rlsbl`
# wheel into `archive-v0` (verified: EROFS / cross-device rename against a
# read-only cache). Overlayfs (which gave copy-on-write for free) is
# unavailable on stock apt bubblewrap, so we clone the whole cache into a
# throwaway dir and bind THAT writable at the real cache path. The clone uses
# `cp --reflink=auto`, which is a near-instant copy-on-write clone on btrfs/xfs
# and degrades to a plain copy on ext4 CI runners (where the cache holds only
# this project's warmed deps and is small). It MUST be a sibling of the real
# cache so it lands on the same filesystem -- a tmpfs target would defeat
# reflink and force a full data copy into RAM. The real dev cache is never
# mutated. ---
UV_CACHE_COPY="$(mktemp -d "${UV_CACHE}.sandbox.XXXXXX")"
cleanup() {
  chmod -R u+w "${WORK}" "${UV_CACHE_COPY}" 2>/dev/null || true
  rm -rf "${WORK}" "${UV_CACHE_COPY}"
}
trap cleanup EXIT
cp -a --reflink=auto "${UV_CACHE}/." "${UV_CACHE_COPY}/"
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
  --bind "${UV_CACHE_COPY}" "${UV_CACHE}"
  --ro-bind "${GO_DOWNLOAD_CACHE}" "${GO_DOWNLOAD_CACHE}"
  --bind "${WORK}" "${WORK}"
  --tmpfs /sandbox-home
  --tmpfs /sandbox-tmp
  --tmpfs /sandbox-gocache
  --tmpfs /sandbox-gomodcache
)
for opt in "${USERBASE}/bin" "${USERBASE}/lib" "${GO_BIN_DIR}"; do
  [ -d "${opt}" ] && BIND_ARGS+=(--ro-bind "${opt}" "${opt}")
done

# Forward UV_PYTHON when set (the CI matrix uses it to pin the interpreter the
# in-sandbox `uv sync` selects); left unset, uv picks the default interpreter.
ENV_EXTRA=()
[ -n "${UV_PYTHON:-}" ] && ENV_EXTRA+=(--setenv UV_PYTHON "${UV_PYTHON}")

# --- preflight: the two known sandbox-SETUP failures (distinct from test
# failures) surface as a non-zero bwrap exit at startup, before the inner
# command ever runs. Probe them with a trivial `--unshare-net` sandbox so we
# can emit a targeted diagnostic instead of a bare bwrap error line. ---
PROBE_ERR="$(mktemp "${TMPDIR:-/tmp}/rlsbl-bwrap-probe.XXXXXX")"
if ! bwrap \
      --unshare-net --unshare-pid --die-with-parent \
      --ro-bind /usr /usr \
      --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
      --proc /proc --dev /dev \
      true 2>"${PROBE_ERR}"; then
  {
    echo "[sandbox] FATAL: bwrap could not create the --unshare-net sandbox."
    echo "  Two known causes on CI runners (esp. Ubuntu 24.04):"
    echo "    1. bubblewrap < 0.10 cannot configure loopback under --unshare-net"
    echo "       ('loopback: Failed RTM_NEWADDR: Operation not permitted')."
    echo "       Fix: use bubblewrap >= 0.11.0."
    echo "    2. Ubuntu 24.04 restricts unprivileged user namespaces to"
    echo "       AppArmor-profiled binaries; a source-built bwrap has no profile."
    echo "       Fix: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
    echo "  bwrap version: $(bwrap --version 2>/dev/null || echo unknown)"
    echo "  bwrap stderr : $(tr '\n' ' ' <"${PROBE_ERR}")"
  } >&2
  rm -f "${PROBE_ERR}"
  exit 1
fi
rm -f "${PROBE_ERR}"

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
  --setenv GOMODCACHE /sandbox-gomodcache \
  --setenv GOCACHE /sandbox-gocache \
  --setenv GOPATH /sandbox-home/go \
  --setenv GOPROXY "file://${GO_DOWNLOAD_CACHE}" \
  --setenv GOSUMDB off \
  bash -c "${INNER}"
