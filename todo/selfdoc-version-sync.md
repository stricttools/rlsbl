# selfdoc.json version not bumped during release

## Problem

When a project has `selfdoc.json` but "docs" is NOT in the explicit `.rlsbl/config.json` targets list, the release code at `rlsbl/commands/release.py` lines 1158-1170 is supposed to bump `selfdoc.json`'s version field. In practice, it consistently fails to do so.

**Evidence:** wesktop required manual fixes 3 times:
- v0.4.2 release: selfdoc.json stuck at 0.4.1
- v0.4.3 release: selfdoc.json stuck at 0.4.2
- v0.4.4 release: selfdoc.json stuck at 0.4.3

## Code path (release.py ~lines 1158-1170)

```python
bumped_files = set(files_to_commit)
selfdoc_path = os.path.join(version_dir, "selfdoc.json")
if os.path.exists(selfdoc_path) and "docs" not in target_paths:
    docs_modified = DocsTarget().write_version(version_dir, new_version)
    for rel in docs_modified:
        fpath = vpath(rel)
        if fpath not in bumped_files:
            files_to_commit.append(fpath)
```

The logic appears correct: condition evaluates to True (selfdoc.json exists, "docs" not in targets), DocsTarget().write_version() should modify the file, and the result should be added to files_to_commit.

## Investigation findings

Static analysis couldn't pinpoint the exact failure. Two hypotheses:

1. **Path format mismatch**: `vpath(rel)` might produce a path that doesn't match what git expects, so the file is "added" to files_to_commit but not actually staged. Compare with how other targets use `target_vpath(version_dir, rel)`.

2. **Code block not reached**: Some earlier condition or silent exception might prevent the block from executing.

## Recommended debugging approach

Add logging to the release code path:
```python
log.info("selfdoc.json exists: %s, docs in targets: %s", os.path.exists(selfdoc_path), "docs" in target_paths)
if os.path.exists(selfdoc_path) and "docs" not in target_paths:
    docs_modified = DocsTarget().write_version(version_dir, new_version)
    log.info("DocsTarget.write_version returned: %s", docs_modified)
    ...
```

Then trigger a release and observe.

## Workaround

Adding `"docs"` to the project's `.rlsbl/config.json` targets makes selfdoc.json a regular target, bypassing the fallback code path entirely. This works but shouldn't be necessary.

## Affected projects

Any project with selfdoc.json that doesn't explicitly list "docs" in rlsbl targets (e.g., wesktop, PixelWeaver).

## Effort

Small once the root cause is identified — likely a 1-2 line fix in the path handling or condition logic.
