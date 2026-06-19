# Releasable model gaps (fixed in v0.79.0)

Three gaps discovered during first-release preparation of a monorepo with 16 sub-projects grouped under one releasable. All fixed in v0.79.0.

## 1. Publish workflow ignores releasable tags (FIXED)

`rlsbl monorepo sync` now uses releasable tag prefixes in the publish router when in explicit mode.

## 2. UV_NO_SOURCES hardcoded in CI template (FIXED)

Template conditionally includes UV_NO_SOURCES based on source analysis. Sync strips it as safety net.

## 3. Namespace package false positives in deps-unused (FIXED)

Auto-discovers namespace-to-project mapping from src/ layout. Composite matching with full import path extraction.
