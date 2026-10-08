#!/usr/bin/env bash
# Runs rlsbl's Go suite: a gofmt check, then go vet, then every package with
# bounded parallelism and the race detector, each package held to its time
# bound (scripts/go-suite-time-bounds.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

# The scratch directories are modules of their own and are left out.
unformatted="$(find . \( -path ./experiments -o -path ./screenshots -o -path ./.git -o -path ./node_modules \) -prune -o -type f -name '*.go' -print0 | xargs -0 -r gofmt -l)"
if [[ -n "${unformatted}" ]]; then
	echo "error: gofmt would reformat these files; run gofmt -w on them:" >&2
	echo "${unformatted}" >&2
	exit 1
fi

go vet ./...
go test -p 2 -parallel 2 -race -timeout 40m ./... 2>&1 | scripts/go-suite-time-bounds.sh
