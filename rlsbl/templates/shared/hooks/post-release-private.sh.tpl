#!/usr/bin/env bash
# Post-release hook for private repositories.
# Builds artifacts and uploads them to the GitHub Release.
# Environment: RLSBL_VERSION is set to the released version.

set -euo pipefail

version="$RLSBL_VERSION"
echo "Post-release (private): v$version"

# Detect project type and build artifacts
if [ -f "pyproject.toml" ]; then
    echo "Building Python package..."
    rm -rf dist/
    uv build
elif [ -f "package.json" ]; then
    echo "Packing npm package..."
    rm -rf dist/
    mkdir -p dist
    npm pack --pack-destination ./dist/
elif [ -f "go.mod" ]; then
    echo "Building Go binary..."
    mkdir -p dist/
    go build -o ./dist/
else
    echo "Warning: no recognized project file found, skipping build."
    exit 0
fi

# Read max asset size from config (default 2MB)
max_size_mb=$(python3 -c "
import json, pathlib
c = pathlib.Path('.rlsbl/config.json')
d = json.loads(c.read_text()) if c.exists() else {}
print(d.get('max_asset_size_mb', 2))
" 2>/dev/null || echo 2)

# Check each file in dist/ against the size limit
max_size_bytes=$((max_size_mb * 1024 * 1024))
for f in dist/*; do
    [ -f "$f" ] || continue
    size=$(stat --format=%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null)
    if [ "$size" -gt "$max_size_bytes" ]; then
        size_mb=$((size / 1024 / 1024))
        echo "Error: $f is ${size_mb}MB, exceeds max_asset_size_mb (${max_size_mb}MB)." >&2
        echo "Set max_asset_size_mb in .rlsbl/config.json to increase the limit." >&2
        exit 1
    fi
done

# Upload artifacts to GitHub Release
if ls dist/* 1>/dev/null 2>&1; then
    echo "Uploading artifacts to GitHub Release v$version..."
    gh release upload "v$version" ./dist/* --clobber
    echo "Artifacts uploaded successfully."
else
    echo "Warning: no artifacts found in dist/ to upload."
fi
