# Add Go project support to strictcli detection

## Problem

`detect_strictcli()` only handles Python projects — it checks `pyproject.toml` for a strictcli dependency. Go projects that use the Go strictcli library (`github.com/smm-h/strictcli/go`) are not detected. This means `rlsbl release run` silently skips the strictcli schema dump step for Go projects, causing the `.strictcli/schema.json` to become stale whenever new CLI commands are added.

This was discovered when a Go project added a new command group (`testdb`) but the schema was never updated automatically. The schema was only regenerated manually.

## Proposed fix

Extend `detect_strictcli()` to also check for Go projects:

1. Check if `go.mod` exists and contains a strictcli dependency (e.g., `github.com/smm-h/strictcli`)
2. Find the binary entry point from the `cmd/` directory structure
3. Run `go run ./cmd/<name>/ --dump-schema` to generate the schema
4. Write to `.strictcli/schema.json` as with Python projects

## Affected files

- `rlsbl/strictcli_detect.py` (or wherever `detect_strictcli` lives)
- The release flow step that calls the detection function
