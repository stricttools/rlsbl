# Dart and Flutter target support

## Context

The F monorepo (~/Work/F) is a Flutter project with 41 Dart packages, 1 Python package, and 3 spec (data-only) packages. rlsbl currently supports pypi, npm, go, hex, cargo, and docker targets. Dart/Flutter is not supported.

Dart packages use `pubspec.yaml` as their manifest. Flutter apps are a special case of Dart packages that additionally produce iOS and Android binaries. The Flutter ecosystem uses `pub.dev` as its package registry (though these packages are internal and won't be published).

## What we need

- A `dart` target type that rlsbl understands for version bumping, changelog validation, and release tagging.
- A `flutter-ios` and `flutter-android` target type (or a single `flutter` target with sub-targets) for the app package, which has a separate release cadence per platform.
- Version lives in `pubspec.yaml` under the `version:` field (format: `major.minor.patch+build`). The `+build` suffix is Flutter-specific (App Store / Play Store build number).
- rlsbl should read and bump `pubspec.yaml` version during release, same as it reads `pyproject.toml` for Python or `package.json` for npm.
- Changelog validation should work identically to other targets.
- Tag format: `package_name@vX.Y.Z` (consistent with existing monorepo tag format).

## Flutter-specific concerns

- iOS and Android releases may have different versions or build numbers. The App Store and Play Store are separate distribution channels with separate review processes.
- The `+build` number in pubspec.yaml often increments independently of the semver version (e.g., `1.2.3+45` where 45 is the build number).
- rlsbl may need to understand that a single `pubspec.yaml` produces two release artifacts (iOS build, Android build) with potentially different release cadences.
- Shorebird (OTA code push) is another release mechanism that bypasses app stores. It's unclear if rlsbl should track Shorebird patches as releases.

## Monorepo implications

- The F monorepo has 35 Dart packages that are internal (never published to pub.dev). They still need version tracking, changelogs, and tags for internal consistency.
- `pubspec.yaml` declares dependencies on sibling packages via `path:` references (e.g., `models: path: ../models`). rlsbl's workspace graph should parse these to build the dependency graph, similar to how it parses `pyproject.toml` path deps today.

## Where to look

- `rlsbl/targets/` -- where target-specific logic lives (pypi.py, npm.py, go.py, etc.)
- `rlsbl/workspace_graph.py` -- where manifest parsing for dependency detection happens
- `rlsbl/dep_rewrite.py` -- where path deps are rewritten to versioned deps for publishing
