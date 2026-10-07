#!/usr/bin/env bash
# List every git repository that holds the old rlsbl layout, the repositories
# `rlsbl migrate records` converts: each repository whose working tree holds a
# .rlsbl/config.json, a .rlsbl/releasable.toml, or a
# .rlsbl-monorepo/workspace.toml that the repository does not ignore, tracked
# or not (the migration reads the working tree). Each repository's root is
# printed once.
#
# The walk skips .archive/ at the top, every node_modules/, experiments/, .git/
# (and so every release checkout under .git/rlsbl/), and every *.local-only
# path, and leaves out a marker its repository ignores (a scratch copy of
# another repository). It only reads. The list it prints is never written
# into a committed file.
set -euo pipefail

usage() {
	cat <<'EOF'
Usage: list-rlsbl-repositories.sh [--dry-run] [--root DIR]

Print the root of every git repository under DIR (~/Projects when --root is
not given) that holds the old rlsbl layout, one per line.

  --dry-run   Print every marker file found and why its repository is
              included or left out, then the repositories, instead of the
              bare list.
  --root DIR  Walk DIR instead of ~/Projects.
EOF
}

dry_run=0
root="$HOME/Projects"
while [ $# -gt 0 ]; do
	case "$1" in
	--dry-run)
		dry_run=1
		shift
		;;
	--root)
		if [ $# -lt 2 ]; then
			echo "list-rlsbl-repositories.sh: --root needs a directory" >&2
			exit 2
		fi
		root="$2"
		shift 2
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		echo "list-rlsbl-repositories.sh: unknown argument $1" >&2
		usage >&2
		exit 2
		;;
	esac
done

if [ ! -d "$root" ]; then
	echo "list-rlsbl-repositories.sh: $root is not a directory" >&2
	exit 2
fi
root="$(cd "$root" && pwd -P)"

declare -A why=()
declare -a repositories=()

while IFS= read -r -d '' marker; do
	# The directory holding .rlsbl/ or .rlsbl-monorepo/.
	holder="${marker%/*}"
	holder="${holder%/*}"
	if ! top="$(git -C "$holder" rev-parse --show-toplevel 2>/dev/null)"; then
		if [ "$dry_run" -eq 1 ]; then
			echo "left out $marker: it is in no git repository"
		fi
		continue
	fi
	if git -C "$top" check-ignore -q -- "$marker"; then
		if [ "$dry_run" -eq 1 ]; then
			echo "left out $marker: $top ignores it, so it is a copy, not a record"
		fi
		continue
	fi
	if [ -z "${why[$top]+set}" ]; then
		repositories+=("$top")
		why[$top]="$marker"
	else
		why[$top]="${why[$top]}, $marker"
	fi
	if [ "$dry_run" -eq 1 ]; then
		echo "found $marker: includes $top"
	fi
done < <(
	find "$root" \
		\( -path "$root/.archive" -o -name node_modules -o -name experiments -o -name .git -o -name '*.local-only' \) -prune \
		-o \( -path '*/.rlsbl/config.json' -o -path '*/.rlsbl/releasable.toml' -o -path '*/.rlsbl-monorepo/workspace.toml' \) -type f -print0 |
		sort -z
)

if [ "$dry_run" -eq 1 ]; then
	echo "${#repositories[@]} repositories hold the old rlsbl layout:"
	for top in "${repositories[@]}"; do
		echo "  $top (because of ${why[$top]})"
	done
	exit 0
fi
for top in "${repositories[@]}"; do
	echo "$top"
done
