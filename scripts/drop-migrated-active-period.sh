#!/usr/bin/env bash
# One-time conversion for the derived unreleased status: remove the active
# lifecycle period `rlsbl migrate records` wrote for a releasable whose release
# archives record no release, so its status is derived (unreleased) instead.
#
# Usage: drop-migrated-active-period.sh --mode plan|apply <repo> <releasable>
#
# Refuses unless the record holds exactly one lifecycle period of the
# releasable, open, active, with the migration's reason, and the releasable's
# archives record no release. plan prints the block it would remove; apply
# removes it. Committing is left to the caller (`rlsbl commit`).
set -euo pipefail

if [[ $# -ne 4 || $1 != --mode || ( $2 != plan && $2 != apply ) ]]; then
	echo "usage: $0 --mode plan|apply <repo> <releasable>" >&2
	exit 2
fi
mode=$2 repo=$3 subject=$4
record="$repo/.strictmetadata/lifecycle-and-license/lifecycle-and-license.toml"
archives="$repo/.strictmetadata/releases/$subject"
reason="active when rlsbl's records moved to the .strictmetadata layout"

[[ -f $record ]] || { echo "$record does not exist" >&2; exit 1; }

released=0
if [[ -d $archives ]]; then
	for a in "$archives"/v*.toml; do
		[[ -e $a ]] || continue
		grep -qx 'never_released = true' "$a" || released=$((released + 1))
	done
fi
if [[ $released -ne 0 ]]; then
	echo "$subject: $released archive(s) under $archives record a release (or state no fate); its status is not unreleased, nothing removed" >&2
	exit 1
fi

periods=$(awk -v s="subject = \"$subject\"" '/^\[\[/{inlc = ($0 == "[[lifecycle]]")} inlc && $0 == s {n++} END {print n + 0}' "$record")
if [[ $periods -ne 1 ]]; then
	echo "$subject: the record holds $periods lifecycle periods of it, not one; remove by hand or not at all" >&2
	exit 1
fi

block=$(printf '[[lifecycle]]\nsubject = "%s"\nstatus = "active"\nfrom = 2026-10-08\nreason = "%s"' "$subject" "$reason")
matches=$(awk -v b="$block" 'BEGIN {RS = "\0"} {n = 0; s = $0; while ((i = index(s, b)) > 0) {n++; s = substr(s, i + length(b))}; print n}' "$record")
if [[ $matches -ne 1 ]]; then
	echo "$subject: its lifecycle period is not the open active period the migration wrote (found $matches matches of the expected block); nothing removed" >&2
	exit 1
fi

echo "$record: removing"
printf '%s\n' "$block" | sed 's/^/  - /'
if [[ $mode == apply ]]; then
	tmp="$record.drop-migrated-active-period"
	awk -v b="$block" 'BEGIN {RS = "\0"; ORS = ""} {i = index($0, b); rest = substr($0, i + length(b)); sub(/^\n/, "", rest); print substr($0, 1, i - 1) rest}' "$record" >"$tmp"
	mv "$tmp" "$record"
	echo "removed"
fi
