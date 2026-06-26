# Per-package lint exceptions for library packages

## Problem

The `library-lint` check forbids certain imports (e.g., `net/http` in Go, `fastapi` in Python) for packages marked `library = true`. This is correct for foundational libraries that should minimize dependencies, but too broad for SDK packages that are libraries by nature but necessarily import HTTP/framework packages.

Example: `auth-sdk-go` is a Go HTTP client SDK marked `library = true`. It imports `net/http` in 4 files (client.go, extract.go, middleware.go, stub.go). This is fundamental to its purpose — an HTTP client SDK cannot avoid `net/http`. The library lint blocks its release with 4 `forbidden-import` errors.

## Impact

Any library package that legitimately needs a forbidden import cannot be released. The only workaround is removing `library = true`, which also disables all other library lint checks (print detection, logging checks, etc.) that ARE valuable for that package.

## Proposed solution

Add per-package lint exception lists in workspace.toml or per-package config.json:

```toml
[[projects]]
path = "auth-sdk-go"
name = "auth-sdk-go"
library = true
lint_allow = ["net/http"]
```

Or in `.rlsbl/config.json`:
```json
{
  "lint": {
    "allow_imports": ["net/http"]
  }
}
```

The forbidden-import check would skip imports listed in the exception list for that package. All other library lint rules continue to apply.

## Affected files

- `rlsbl/lint/go_lint.py` or equivalent — forbidden-import check
- `rlsbl/config.py` — read lint exceptions from config
- `rlsbl/workspace.py` — read lint_allow from workspace.toml (if workspace-level)
