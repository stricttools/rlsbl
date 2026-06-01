# Exclude test/example files from library-specific lint rules

## Problem

Library-lint currently scans test files and example directories for stdout usage and forbidden imports. These are false positives:

- Test files legitimately use `fmt.Println` (Go) and `print()` (Python) for test output and debugging.
- Example directories legitimately print to stdout to demonstrate library usage -- that is their entire purpose.

This produces noise that must be manually suppressed or ignored, undermining the usefulness of the lint.

## Proposed fix

Exclude the following patterns from **library-specific** lint rules (stdout usage, forbidden-import):

- `*_test.go`
- `test_*.py`
- `tests/` directories
- `examples/` directories

These exclusions should only apply to library-specific rules. Non-library-specific rules (e.g., general code quality checks) should continue to scan all files including tests and examples.

## Rationale

Library lint rules exist to enforce that library code does not have side effects like printing to stdout or importing packages that pull in heavy transitive dependencies. Test and example code is not library code -- it is consumer code that runs in a controlled context where stdout is expected and dependency weight is irrelevant.
