# check-name: --target npm is silently ignored

## Problem

`rlsbl check-name <name> --target npm --target pypi` only checks PyPI. The npm target is silently skipped -- no error, no warning, no indication that it was ignored.

Observed output:

```
$ rlsbl check-name orchide --target npm --target pypi
Checking PyPI for "orchide"...
"orchide" is available on PyPI.
Checked: PyPI, stdlib, variants, GitHub repos
```

npm was never checked despite being explicitly requested.

## Expected behavior

Either:
1. Check npm (query the npm registry, report taken/available), or
2. Hard error if npm is not a supported target (don't silently skip it)

Option 2 is the minimum -- silent skipping violates the "hard errors, not warnings" principle.

## Possible causes

- `--target` may only accept a single value, and the second `--target npm` is silently dropped
- npm checking may not be implemented yet, but the flag is accepted without validation
- strictcli may not support repeated flags, so only one `--target` takes effect

## Effort

Small -- likely a validation check or an npm registry HTTP call (`https://registry.npmjs.org/<name>`).
