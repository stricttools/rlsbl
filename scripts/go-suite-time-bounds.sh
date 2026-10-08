#!/usr/bin/env bash
# Reads `go test` output on stdin, prints it unchanged, and fails when a
# package's tests took longer than that package's bound below. A package
# whose result came from the test cache carries no time and is not judged.
#
# The bounds sit at about twice what each package took under
# `heavy --mem 3G -- scripts/go-suite.sh` on the development machine (-p 2,
# -race, beside other work), the margin being for slower CI runners. A
# package over its bound is a defect to cut at its source (measure where the
# time goes), never a bound to raise.
set -euo pipefail

# bound_seconds prints the bound of the package it is given.
bound_seconds() {
	case "$1" in
	github.com/stricttools/rlsbl/internal/release) echo 40 ;;
	github.com/stricttools/rlsbl/internal/monorepo) echo 30 ;;
	github.com/stricttools/rlsbl/internal/migration) echo 30 ;;
	github.com/stricttools/rlsbl/internal/releaseops) echo 25 ;;
	github.com/stricttools/rlsbl/internal/cli) echo 25 ;;
	github.com/stricttools/rlsbl/internal/batchrelease) echo 20 ;;
	github.com/stricttools/rlsbl/internal/checks) echo 20 ;;
	github.com/stricttools/rlsbl/internal/historyrewrite) echo 15 ;;
	# Every package not named above.
	*) echo 10 ;;
	esac
}

over=()
while IFS= read -r line; do
	printf '%s\n' "${line}"
	# A timed result: "ok  <package>  <seconds>s", optionally followed by
	# coverage; "FAIL" results fail go test itself.
	if [[ "${line}" =~ ^ok[[:space:]]+([^[:space:]]+)[[:space:]]+([0-9]+)(\.[0-9]+)?s([[:space:]]|$) ]]; then
		package="${BASH_REMATCH[1]}"
		seconds="${BASH_REMATCH[2]}"
		limit="$(bound_seconds "${package}")"
		if ((seconds >= limit)); then
			over+=("${package} took ${seconds}${BASH_REMATCH[3]}s; its bound is ${limit}s")
		fi
	fi
done

if ((${#over[@]} > 0)); then
	echo "error: these packages' tests ran over their time bound (scripts/go-suite-time-bounds.sh); find where the time goes and cut it at its source:" >&2
	printf '  %s\n' "${over[@]}" >&2
	exit 1
fi
