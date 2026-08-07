# npm lockfile sync runs lifecycle scripts, so wrapper packages always warn

## Problem

`_LOCKFILE_SPECS` (`rlsbl/commands/release/execute.py:1168`) syncs
`package-lock.json` with:

```python
("package-lock.json", "npm", ["npm", "install", "--package-lock-only"], None),
```

`--package-lock-only` skips writing `node_modules`, but it does **not** skip
lifecycle scripts. npm still runs the package's own `postinstall`.

For a binary-wrapper npm package -- one whose `postinstall` downloads a
platform binary from the GitHub Release for the version in `package.json` --
this fails **every single release, by construction**. The sync runs after the
version has been bumped to X.Y.Z and long before the GitHub Release for X.Y.Z
exists, so `postinstall` requests a URL that is guaranteed to 404, exits 1, and
the release logs:

```
Warning: package-lock.json sync failed: Command '['npm', 'install', '--package-lock-only']' returned non-zero exit status 1.
```

Observed on a real release: the wrapper's `install.js` reported
`HTTP 404: .../releases/download/vX.Y.Z/<tool>_X.Y.Z_linux_amd64.tar.gz` and
npm exited 1.

## Why it matters

1. **The warning is permanent noise.** It cannot be fixed in the consumer repo
   -- the 404 is a correct consequence of the release ordering. An agent
   reading release output learns to ignore lockfile warnings, which is exactly
   the class of habit the fleet's "hard errors, not warnings" rule exists to
   prevent.
2. **The sync is not doing its job.** Whether the lockfile ends up correct is
   incidental: npm may have written the version bump before the postinstall
   ran, or the file may be left stale. Nothing verifies it, because the
   command's nonzero exit is swallowed into a warning.
3. **It is a pointless network round trip** inside the release's critical path,
   on a 30-second timeout, that can only ever fail for this package shape.

## Fix

Add `--ignore-scripts`:

```python
("package-lock.json", "npm", ["npm", "install", "--package-lock-only", "--ignore-scripts"], None),
```

The sync's only job is to reconcile the lockfile with `package.json`. Running
the package's own install hooks is not part of that job for any package, and
for wrapper packages it is actively wrong. `--ignore-scripts` is also the safer
default in a release pipeline generally: a lockfile sync should never execute
third-party or first-party install code.

## Follow-on question (decide, don't guess)

Once the command can no longer fail for this reason, should a failed lockfile
sync stay a warning or become a hard error? A stale lockfile committed into a
release is the kind of thing the fleet's philosophy says should block. Worth an
explicit ruling rather than leaving the `except` in place by inertia.

## Affected files

- `rlsbl/commands/release/execute.py:1168` (`_LOCKFILE_SPECS`)
- `tests/test_release_lockfile_sync.py:97,116` -- the pinned command tuple
  asserts the exact argv and must be updated in the same edit
- Red-green: a fixture npm package whose `postinstall` exits nonzero; assert
  the sync succeeds and the lockfile version is reconciled

## Effort

Under an hour including the regression test.
