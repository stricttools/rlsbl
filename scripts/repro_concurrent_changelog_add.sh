#!/usr/bin/env bash
# Reproduce the live-observed loss from concurrent `rlsbl changelog add`
# invocations. Builds a throwaway git project OUTSIDE any repo (this project's
# own guards walk the tree and would trip on a nested repo), runs N adds
# concurrently against it, and reports what survived in the working tree file
# and in HEAD.
#
# Usage: scripts/repro_concurrent_changelog_add.sh [N]
set -uo pipefail
N="${1:-2}"
RLSBL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d /tmp/rlsbl-concurrent-add.XXXXXX)"
cd "$WORK" || exit 1
git init -q .
git config user.email t@t.local
git config user.name Test
git config commit.gpgsign false
mkdir -p .rlsbl/changes
printf '{"publish_mode": "none"}\n' > .rlsbl/config.json
echo "# repo" > README.md
git update-index --add README.md .rlsbl/config.json
git commit -qm initial
git tag v0.0.0
SHAS=()
for i in $(seq 1 "$N"); do
  echo "line $i" >> README.md
  git commit -qam "change $i"
  SHAS+=("$(git rev-parse HEAD)")
done

pids=()
for i in $(seq 1 "$N"); do
  (
    cd "$WORK" || exit 1
    uv run --project "$RLSBL_ROOT" rlsbl changelog add \
      --commits "${SHAS[$((i - 1))]}" --no-user-facing \
      >"$WORK/add-$i.out" 2>"$WORK/add-$i.err"
  ) &
  pids+=($!)
done
for p in "${pids[@]}"; do wait "$p"; done

echo "=== worktree file lines: $(wc -l <.rlsbl/changes/unreleased.jsonl 2>/dev/null || echo MISSING)"
echo "=== HEAD file lines: $(git show HEAD:.rlsbl/changes/unreleased.jsonl 2>/dev/null | wc -l)"
echo "=== git log:"
git log --oneline | head -20
echo "=== git status:"
git status --porcelain
echo "=== per-run output:"
for i in $(seq 1 "$N"); do
  echo "--- run $i stdout"
  cat "$WORK/add-$i.out"
  echo "--- run $i stderr"
  cat "$WORK/add-$i.err"
done
echo "=== workdir: $WORK"
