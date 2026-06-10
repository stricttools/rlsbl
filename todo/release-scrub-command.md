# release scrub command

## Problem

When sensitive content is discovered in git history (credentials, confidential project names, etc.), the scrub process requires manual steps: run safegit scrub, update JSONL changelog hashes, re-tag, recreate GitHub Release, force-push. This was done manually for v0.63.0 and took significant effort.

## Proposed solution

New subcommand: `rlsbl release scrub <version>`

Flags forwarded to safegit:
- `--pattern` / `--file`: what to scrub (forwarded to `safegit scrub match` or `safegit scrub file`)
- `--replace` / `--mangle`: replacement strategy
- `--dry-run`: preview what would be scrubbed + what release metadata would change

Post-scrub steps (automated):
1. Read SHA mapping from safegit scrub --json output
2. Update JSONL changelog entries: scan all .rlsbl/changes/*.jsonl, replace old SHAs with new mappings (temporarily unlock read-only files, update, re-lock)
3. Delete old tag locally and remotely, create new tag on rewritten commit
4. Delete old GitHub Release, recreate with same body from CHANGELOG entry
5. Force-push with --force-with-lease
6. Post-push verification (confirm remote HEAD matches local)

Safety: requires --yes (same as other destructive operations). No extra confirmation flag.

## Blocked on

safegit shipping `--json` SHA mapping output for scrub commands. Todo filed: safegit/todo/scrub-json-sha-mapping.md

## Effort

Medium. The individual steps are straightforward (JSONL update, gh release delete/create, git tag). The complexity is in the orchestration and error handling (what if force-push fails after tag recreation?).

## Affected files

- rlsbl/__init__.py (register scrub subcommand)
- rlsbl/commands/scrub.py (new file, main implementation)
- rlsbl/changelog/files.py (JSONL hash update utility)
- tests/test_release_scrub.py (new file)
- docs/release-workflow.md (document the command)
