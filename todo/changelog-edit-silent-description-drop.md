# changelog edit silently drops --description when flipping to non-user-facing

## Problem

`rlsbl changelog edit --id <id> --no-user-facing --unset-type
--description "..."` exits 0, prints "Edited entry in unreleased.jsonl",
and leaves the description UNCHANGED. The suppression is deliberate
(changelog_cmd.py, in the edit handler: a value write is suppressed when
the same invocation flips the entry to non-user-facing, "where the value
would have no reader") — but it is SILENT. The caller passed a flag; the
tool ignored it and reported success.

Two problems with the current shape:

1. The premise is wrong for descriptions. Non-user-facing entries have no
   REQUIRED description, but a present one is legal and useful — it is
   how a cluster entry explains what the cluster is ("tests and probes
   pinning new behavior"). A caller flipping an entry non-user-facing and
   rewriting its description in one invocation is expressing exactly that.
2. Even where suppression is right, silence is not. A passed flag that
   will not take effect should be a hard error (the fleet's hard-errors-
   over-warnings rule) — "cannot set --description in the same invocation
   that sets --no-user-facing; run the edits separately" — never an
   ignored input behind exit 0.

## Observed

Live: the combined invocation reported success with the old description
intact; re-running `--description` alone took effect. The workaround is
two invocations, discoverable only by diffing the JSONL afterward.

## Solution sketch

Either honor the description on the flip (preferred — the value has a
reader: humans auditing the JSONL and the coverage tooling), or refuse
the combination loudly. Never accept-and-ignore.

## Affected

`changelog edit`'s flag handling; any script that batches entry edits.

## Effort

Small — one branch in the edit handler plus a test for the combined
invocation.
