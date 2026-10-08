# Release asset size guard

## Context

The `post-release-private.sh.tpl` hook unconditionally uploads everything in `dist/` to GitHub Releases via `gh release upload "v$version" ./dist/* --clobber` with no size checks. This caused a private project's v0.1.0 through v0.1.7 to have 956MB release assets containing personal Parquet snapshot data that should never have been uploaded.

## Problem

The private post-release hook runs `gh release upload` without checking file sizes. A stray large file in `dist/` (build artifacts, data files, cached models) gets silently uploaded to the GitHub Release, potentially leaking private data and bloating the release.

## Chosen solution

**(a) Shell-side check in the hook template.** Add a configurable `max_asset_size_mb` setting in `.rlsbl/config.json` (default: 2MB). Before `gh release upload`, the hook iterates files in `dist/` and checks each against the threshold. If any file exceeds it, the hook aborts with a clear error message.

Projects with legitimately large artifacts (Go cross-compiled binaries, bundled frontends) can increase the limit deliberately.

## Affected files

- `rlsbl/templates/shared/hooks/post-release-private.sh.tpl` -- add size check
- `rlsbl/config.py` -- add `max_asset_size_mb` config key (with default)
- Tests: assert that files exceeding the limit cause the upload to abort

## Effort

Small-medium. The size check logic is straightforward -- a few lines of bash in the hook template plus config plumbing.
