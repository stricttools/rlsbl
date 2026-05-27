# Auto-bump selfdoc.json version during release

## Problem

`selfdoc.json` has a `"version"` field that must match the project version. Currently, `rlsbl release` does not bump this field during the version bump step (step 12). As a result, every release of a project that uses selfdoc fails on the first attempt because `selfdoc check` (step 8) finds a VER002 version mismatch between the just-bumped project version and the stale `selfdoc.json` version.

The workaround is to manually bump `selfdoc.json` before releasing, but this defeats the purpose of automated version management.

## Solution

In the version bump step (step 12), detect `selfdoc.json` in the project root and bump its `"version"` field to the new version, alongside `pyproject.toml`, `package.json`, `VERSION`, and other target files.

## Affected

Any project with a `selfdoc.json` file (i.e., any project using selfdoc).

## Effort

Small. The version bump step already handles multiple file formats; adding `selfdoc.json` is a straightforward extension (read JSON, update `"version"` key, write back).
