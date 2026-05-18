# Go monorepo tag missing path separator

## Bug

`GoTarget.monorepo_tag_format()` produces `auth-gatewayv0.1.0` instead of `auth-gateway/v0.1.0` when `workspace.toml` has `path = "auth-gateway"` (no trailing slash).

## Root cause

In `rlsbl/targets/go.py`:
```python
def monorepo_tag_format(self, name, version, path=None):
    if path is not None:
        return f"{path}v{version}"
```

The format string doesn't add a `/` separator between path and version. It relies on the path having a trailing slash, but `workspace.toml` paths don't have trailing slashes (and shouldn't — they're directory paths).

## Expected behavior

`monorepo_tag_format("auth-gateway", "0.1.0", path="auth-gateway")` should return `auth-gateway/v0.1.0`.

The Go module proxy requires the slash — `go get github.com/smm-h/www/auth-sdk-go@v0.1.0` looks for tag `auth-sdk-go/v0.1.0`.

## Fix

```python
def monorepo_tag_format(self, name, version, path=None):
    if path is not None:
        sep = "" if path.endswith("/") else "/"
        return f"{path}{sep}v{version}"
```

## Test that passes with wrong behavior

The existing test uses `path="go/"` (with trailing slash), which masks the bug:
```python
result = GoTarget().monorepo_tag_format("go-strictcli", "0.1.1", path="go/")
assert result == "go/v0.1.1"
```

Should also test without trailing slash:
```python
result = GoTarget().monorepo_tag_format("auth-gateway", "0.1.0", path="auth-gateway")
assert result == "auth-gateway/v0.1.0"
```

## Workaround

Manual tag fix after release:
```bash
git tag auth-gateway/v0.1.0 <commit> && git tag -d auth-gatewayv0.1.0
git push origin auth-gateway/v0.1.0 :refs/tags/auth-gatewayv0.1.0
```
