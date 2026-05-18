# scaffold --update: regenerate pre-push hook

## Problem

`rlsbl scaffold --update` does not regenerate the `.git/hooks/pre-push` hook. It uses install-once behavior: the hook is written on initial `scaffold` but never touched again on `--update`. This means projects that scaffolded with an older version of rlsbl keep a stale pre-push hook indefinitely, even as the hook logic evolves (e.g., the fix for passing `$@` to rlsbl, new checks, etc.).

## Proposed solution

Detect old hook versions by content hash and regenerate when the installed hook matches a known old version.

### Approach

1. Maintain a set of known content hashes for all previously shipped versions of the pre-push hook template.
2. On `scaffold --update`, read the installed `.git/hooks/pre-push` and compute its hash.
3. If the hash matches a known old version, overwrite it with the current template.
4. If the hash does NOT match any known version (user has customized it), leave it untouched and print a warning suggesting manual review.

This preserves user customizations: only hooks whose content exactly matches a known rlsbl-shipped version are overwritten. Users who have edited their hook will see a warning but no data loss.

### Details

- The hash list lives in the rlsbl source (e.g., `rlsbl/hook_hashes.py` or embedded in the scaffold module).
- Each rlsbl release that changes the hook template must add the old template's hash to the list.
- Use SHA-256 of the file content (stripped of trailing whitespace to avoid false negatives from editors).
- The warning message should include a diff or at least show what changed, so users can merge manually.

## Affected files

- `rlsbl/commands/scaffold_cmd.py` -- hook installation logic
- New or existing module for known hook hashes
- `rlsbl/templates/` -- hook templates (if not already separated)

## Effort estimate

Small-medium. The hash-matching logic is straightforward. The main work is auditing all historical hook versions to build the initial hash set and deciding on the warning/diff UX.
