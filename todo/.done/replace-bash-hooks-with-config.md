# Replace bash hook scripts with config-driven commands

## Problem

rlsbl uses three bash scripts as release hooks (.rlsbl/hooks/pre-checks.sh, pre-release.sh, post-release.sh). These cause friction:

1. **Merge conflicts**: When scaffold updates template comments, three-way merge with user customizations produces conflict markers. 4 repos currently have unresolved conflicts in pre-release.sh.
2. **Hash-based customization detection is fragile**: rlsbl tracks known template hashes to detect "customized" hooks. Every template change requires adding new hashes.
3. **Bash is the wrong tool**: Most customizations are simple command lists (run tests, build wheels, upload artifacts). These don't need Turing-complete scripts.

## What users actually put in hooks

From investigating 25 projects:
- **pre-release.sh**: Multi-language test runners (go vet + pytest + npm test), specialized build validation (uv build --wheel), auto-generated asset updates (schema dumps). Most projects don't customize at all.
- **post-release.sh**: Local install, deploy, notify, artifact uploads (gh release upload for private repos).
- **pre-checks.sh**: Almost never customized. Setup tasks (start services, set env vars).

## Proposed solution

### Replace pre-release.sh with config-driven test commands

Add to .rlsbl/config.json:
```json
{
  "custom_tests": [
    ["go", "vet", "./..."],
    ["go", "test", "./...", "-race"],
    ["uv", "run", "pytest", "-x"]
  ]
}
```

rlsbl executes each command in order. Non-zero exit aborts release. No bash, no merge conflicts, no hash tracking.

When `custom_tests` is configured, built-in tests are skipped (same override behavior as customized pre-release.sh).

### Keep pre-checks.sh as USER_OWNED

Rarely customized, low conflict risk. No template changes needed. Alternatively, move to `custom_setup` command list in config.

### Consolidate post-release.sh into pipelines

Post-release hooks mostly do deployment (which pipelines already handle) or artifact uploads (which custom_assets handles). The remaining use cases (local install, notifications) could become config-driven too:
```json
{
  "post_release": [
    ["rlsbl", "dev", "install"],
    ["notify-send", "Released v{version}"]
  ]
}
```

### Migration path

1. Add `custom_tests` and `custom_post_release` to config.json schema
2. During scaffold, if pre-release.sh is customized, offer to extract its commands into config
3. Deprecate pre-release.sh and post-release.sh with warnings
4. Remove after one major version

## Effort

Medium-large. Config schema changes, release flow changes, migration tooling, documentation. But eliminates an entire class of merge conflict bugs.

## Affected files

- rlsbl/commands/release.py (execute custom_tests instead of bash hook)
- rlsbl/config.py (validate new config keys)
- rlsbl/commands/init_cmd.py (scaffold changes)
- rlsbl/hook_hashes.py (can be simplified/removed for pre-release)
