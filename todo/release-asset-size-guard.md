# Release asset size guard and config/hook sync

## Context

The `post-release-private.sh.tpl` hook unconditionally uploads everything in `dist/` to GitHub Releases via `gh release upload "v$version" ./dist/* --clobber`. There are zero size checks. This caused a private project v0.1.0 through v0.1.7 to have 956MB release assets containing personal Parquet snapshot data that should never have been uploaded.

Root cause chain:
1. Initial `rlsbl scaffold` detected the repo as private and installed the private post-release hook
2. Config was later changed to `"private": false` but `scaffold --update` was not run
3. The old private hook persisted and uploaded local `dist/` contents (which included 1.1GB of Parquet files from the frontend build)

Two independent problems contributed. Both need fixes.

## Problem 1: No asset size guard

The private post-release hook (`rlsbl/templates/shared/hooks/post-release-private.sh.tpl`) runs `gh release upload "v$version" ./dist/* --clobber` without checking file sizes. A stray large file in `dist/` (build artifacts, data files, cached models) gets silently uploaded to the GitHub Release, potentially leaking private data and bloating the release.

### Proposed solution

Add a configurable `max_asset_size_mb` setting in `.rlsbl/config.json` (default: 2MB). Before `gh release upload`, the hook iterates files in `dist/` and checks each against the threshold. If any file exceeds it, the hook aborts with a clear error message:

```
Error: dist/snapshot.parquet is 912MB, exceeds max_asset_size_mb (2MB).
Set max_asset_size_mb in .rlsbl/config.json to increase the limit.
```

This prevents accidents while allowing projects with legitimately large artifacts (Go cross-compiled binaries, bundled frontends) to increase the limit deliberately.

Implementation options:

- **(a) Shell-side check in the hook template**: The `.tpl` file itself checks file sizes using `stat` before uploading. Pros: self-contained, works even if rlsbl is not installed at hook runtime. Cons: the hook must read `config.json` (parsing JSON in bash is fragile -- requires `jq` or a Python one-liner), and updating the check requires `scaffold --update`.
- **(b) Python helper called from the hook**: The hook calls `rlsbl upload-assets` (new subcommand) which reads config, validates sizes, and runs `gh release upload`. Pros: clean config access, can add more validations later (checksum, file type allowlist). Cons: adds a dependency on rlsbl being installed in the hook environment (already true in practice since the hook runs in the release flow).
- **(c) Validate in `rlsbl release` before running the hook**: The release command itself inspects `dist/` after the build step and before `gh release upload`. Pros: no hook changes needed, centralized logic. Cons: the release command does not currently know about `dist/` contents -- the hook owns the build+upload lifecycle, and mixing concerns is messy.

Recommendation: **(b)** gives the cleanest separation and the most room to grow. The hook template becomes a thin wrapper around the subcommand. Fallback: **(a)** is acceptable if adding a subcommand is too heavy -- use `python3 -c 'import json; ...'` instead of `jq` to avoid a new dependency.

## Problem 2: Config changes do not resync hooks

Changing `"private"` in `config.json` has no effect on the installed hook. The hook file is written once at scaffold time and only updated by `scaffold --update`. Users who change the config expect the behavior to change, but it does not. There is no warning that the hook is out of sync.

This is a specific instance of a general problem: any config key that influences which hook template is installed creates a silent drift risk when the config changes outside of `scaffold --update`.

### Proposed solution

Two complementary approaches, not mutually exclusive:

- **(a) Runtime mismatch detection in `rlsbl release`**: Before running the post-release hook, `rlsbl release` reads the config and inspects the hook file. If config says `private: false` but the hook contains the upload code (e.g., a marker comment like `# rlsbl:private-hook`), emit a warning or abort:

  ```
  Warning: config.json has "private": false but .rlsbl/hooks/post-release.sh
  contains the private-repo upload hook. Run `rlsbl scaffold --update` to
  reconcile, or set "private": true if this repo is actually private.
  ```

  Pros: catches the problem at the moment it matters, no false positives if the user intentionally customized the hook. Cons: fragile if the marker comment is removed or the user heavily customizes the hook.

- **(b) `rlsbl release` re-evaluates the private flag at runtime**: Instead of relying solely on the hook file, the release command checks `config["private"]` and skips the asset upload step if `false`. The hook still runs for other post-release tasks but the upload is gated. Pros: authoritative, config is the source of truth. Cons: changes the contract between the release command and the hook (currently the hook owns the upload decision entirely).

- **(c) `rlsbl config set` triggers hook regeneration**: When `private` is changed via a config command, automatically re-run the hook template selection and update the hook file (with three-way merge as `scaffold --update` does). Pros: keeps hooks in sync proactively. Cons: requires a `config set` command (does not exist yet; see `todo/unified-toml-config.md`), and does not help users who edit `config.json` by hand.

Recommendation: **(a)** as the immediate fix -- it is defensive, low-risk, and catches the exact failure mode that caused the a private project incident. **(c)** as a long-term structural fix once the config management story matures.

## Affected files

### Problem 1 (size guard)
- `rlsbl/templates/shared/hooks/post-release-private.sh.tpl` -- add size check or call to upload subcommand
- `rlsbl/config.py` -- add `max_asset_size_mb` config key (with default)
- `rlsbl/commands/upload_cmd.py` (new, if option b) -- upload-assets subcommand
- `rlsbl/__init__.py` (if option b) -- register new subcommand
- Tests: assert that files exceeding the limit cause the upload to abort

### Problem 2 (config/hook mismatch)
- `rlsbl/commands/release.py` -- add pre-hook config/hook consistency check (around line 886 where `post_release_script` is checked)
- `rlsbl/templates/shared/hooks/post-release-private.sh.tpl` -- add marker comment for detection
- Tests: assert that a mismatch produces a warning/error

## Effort

- Problem 1: Small-medium. The size check logic is straightforward. Option (a) is a few lines of bash; option (b) is a small new subcommand.
- Problem 2: Small. Reading config + grep for a marker in the hook file is ~20 lines in the release command.
- Total: medium. Both fixes are independent and can be done separately.

## Related work

- `todo/unified-toml-config.md` -- config management overhaul; option (c) of Problem 2 depends on this
- `todo/scaffold-hook-regeneration.md` -- may overlap with the hook resync concern
- The a private project cleanup (deleting 956MB of release assets from v0.1.0-v0.1.7) is a separate remediation task in that project
