# Config/hook desync on private flag change

## Context

The `post-release-private.sh.tpl` hook is installed at scaffold time based on the `"private"` flag in `config.json`. A private project's v0.1.0-v0.1.7 incident (956MB release assets uploaded) was partly caused by changing `"private": false` in config without running `scaffold --update`, leaving the old private hook in place.

## Problem

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

Recommendation: **(a)** as the immediate fix -- it is defensive, low-risk, and catches the exact failure mode that caused that incident. **(c)** as a long-term structural fix once the config management story matures.

## Affected files

- `rlsbl/commands/release.py` -- add pre-hook config/hook consistency check (around line 886 where `post_release_script` is checked)
- `rlsbl/templates/shared/hooks/post-release-private.sh.tpl` -- add marker comment for detection
- Tests: assert that a mismatch produces a warning/error

## Effort

Small. Reading config + grep for a marker in the hook file is ~20 lines in the release command.

## Related work

- `todo/unified-toml-config.md` -- config management overhaul; option (c) depends on this
- `todo/scaffold-hook-regeneration.md` -- may overlap with the hook resync concern
- `todo/.done/release-asset-size-guard.md` -- the companion asset size guard problem (split from the same original todo)

**Blocked on:** Option (c) (config set triggers hook regeneration) depends on `todo/unified-toml-config.md` — specifically the `rlsbl config set` command that doesn't exist yet. Options (a) and (b) can proceed independently.
