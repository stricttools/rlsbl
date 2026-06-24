# check-name --target leaks GitHub repo results

## Problem

`rlsbl check-name <name> --target npm` shows GitHub repository counts even though only npm was requested:

```
$ rlsbl check-name orxtra --target npm
Checking npm for "orxtra"...
"orxtra" is available on npm.

  (i) 2 GitHub repo(s) named "orxtra" (informational, not a registry)

Checked: npm, variants, moniker similarity, GitHub repos
```

The `--target npm` flag should scope the check to npm only. GitHub repo counts are a cross-registry informational check that should only appear when no specific target is requested (i.e., checking all registries) or when `--target github` is explicitly passed.

The "Checked:" summary line also lists "GitHub repos" as a checked target, reinforcing the impression that the target filter isn't being applied to the GitHub lookup.

## Expected behavior

When `--target npm` is specified, the output should only show npm results. GitHub repo counts should be suppressed. The "Checked:" line should only list npm-related checks.

## Effort

Small. The GitHub repo lookup is likely unconditional and just needs to be gated on the target filter.
