# `rlsbl release run` broken: passes `--yes` to selfdoc, which strictcli 0.35.4 removed

## Symptom

`rlsbl release run` aborts at the "Running selfdoc gen…" step with:

```
error: unknown flag '--yes'
try 'selfdoc gen --help'
Error: selfdoc gen failed
```

The release dies after changelog preflight and CHANGELOG generation but **before** the version write, so nothing is mutated and there is no retry state to clean up. This blocks releases for every selfdoc-using project on a machine with the new strictcli, not just one project.

## Cause

A three-package version skew:

- **strictcli 0.35.4** re-keys the confirm protocol from `effect="mutating"` to `effect="consequential"`, renames the skip flag `--yes` → `--approve-consequential`, and adds `"yes"` to `_BANNED_FLAG_NAMES`.
- **selfdoc 0.34.0** declares `gen`, `check` and `deploy` as `mutating`. Under the new keying those commands no longer receive a confirm-skip flag at all, so `--yes` is simply unknown.
- **rlsbl 0.110.2** still injects `--yes` into those three invocations. Note this injection was *added* one release ago specifically to satisfy strictcli 0.35.3, so the flag has now flipped from required to invalid within two strictcli releases.

Call sites observed in rlsbl 0.110.2:

- `rlsbl/commands/release/validate.py` — the `selfdoc gen` invocation
- `rlsbl/commands/release/validate.py` — the `selfdoc check` invocation
- `rlsbl/pipelines/cloudflare_pages.py` — the `selfdoc deploy` invocation

Isolation check: `selfdoc gen --no-auto-commit --dry-run` exits 0 and plans its effects normally, confirming only the injected flag is at fault.

## Sequencing constraint (important)

The fix cannot land in rlsbl alone, and order matters:

1. strictcli 0.35.4 is released (its working tree is currently unreleased and dirty).
2. selfdoc decides whether `gen`/`check`/`deploy` are `consequential` (and therefore expose `--approve-consequential`) or need no confirm-skip flag at all, and releases.
3. rlsbl matches whatever selfdoc's CLI actually exposes.

Dropping or renaming the flag in rlsbl *today* would break every user still on released strictcli 0.35.3, so this needs the upstream releases first.

## Possible solutions

1. **Match the new flag after selfdoc lands** — pass `--approve-consequential` where selfdoc declares those commands consequential. Simple, but couples rlsbl to a strictcli flag name a third time; a future rename repeats this outage.
2. **Stop injecting a confirm-skip flag entirely** — if selfdoc's commands are non-interactive-safe under the new model, rlsbl passes nothing and the problem class disappears. Most robust; requires selfdoc to not demand confirmation in automated contexts.
3. **Probe the callee's CLI** (e.g. parse `--help` or a dumped schema) and pass whatever skip flag exists. Adaptive but adds a fragile runtime dependency on another tool's help text.
4. **Have selfdoc expose an explicit non-interactive mode** that rlsbl requests, decoupling rlsbl from the confirm protocol's spelling.

Option 2 or 4 removes the recurring breakage; option 1 only resolves the current instance.

## Affected files

- `rlsbl/commands/release/validate.py` (two invocations)
- `rlsbl/pipelines/cloudflare_pages.py` (one invocation)
- Any integration test that asserts the selfdoc invocation argv

## Effort

Small once upstream lands — a few lines plus a regression test that runs the real selfdoc invocation rather than asserting a hardcoded argv, so a future flag rename fails loudly in tests instead of during a release.
