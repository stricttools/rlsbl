# Scaffold --update UX friction

## 1. Auto-commits files with merge conflict markers

`scaffold --update` commits all created/modified files including those with status "CONFLICTS". This means conflict markers (`<<<<<<<`) get committed to git. The user then has to resolve conflicts and make a second commit.

Expected: scaffold should not auto-commit files that have conflict markers. Either skip conflicted files from the commit, or don't auto-commit at all when conflicts exist.

## 2. No --dry-run for scaffold --update

There's no way to preview what `scaffold --update` will change before it changes it. A `--dry-run` that shows which files would be created, updated, conflicted, or unchanged would let users assess the impact before running.

## 3. CI workflows still use three-way merge

`.github/workflows/ci.yml` and `publish.yml` use three-way merge on `scaffold --update`, producing conflicts when both the user and scaffold have modified them. Unlike `.gitignore` (which was fixed with set-union merge in v0.28.1), CI workflows have structural dependencies that make set-union impractical. But the conflicts are predictable and tedious.

Options:
- User-owned sections within CI workflows (fenced markers that scaffold preserves)
- A separate user-owned workflow file that scaffold never touches
- Better base tracking so the merge has less noise

## 4. Multiple auto-commits per migration

A single JSONL changelog migration produces 3-4 auto-commits (scaffold, generate, backfill, changelog add). Each is a separate commit. For a one-time migration, a single "migrate to JSONL changelog" commit would be cleaner.

Is there a way to batch scaffold operations into a single commit? Or should migrations be documented as a multi-commit process?

## Affected files

- `rlsbl/commands/init_cmd.py` — scaffold commit logic, conflict handling
- CI workflow templates — merge strategy

## Effort

Items 1 and 2 are small. Items 3 and 4 are design questions that may not have simple answers.
