# Dart and Flutter target support

## Context

rlsbl needs to support Dart packages (libraries) and Flutter apps (iOS/Android) as target types. Dart packages use `pubspec.yaml` as their manifest. Flutter apps are Dart packages that additionally produce platform-specific binaries.

## Decisions

### Target types

- **`dart` target**: for Dart/Flutter library packages. Reads/writes `pubspec.yaml` version field. Publish is a no-op by default (same as spec target). pub.dev publishing gated by `publish.dart.local` config.
- **`flutter-ios` target**: for Flutter app iOS releases. Reads version from `pubspec.yaml`. Separate release lifecycle from Android.
- **`flutter-android` target**: for Flutter app Android releases. Reads version from `pubspec.yaml`. Separate release lifecycle from iOS.

Separate per-platform targets because: platform-specific bugs need independent hotfixes, app store reviews are independent, native code changes (Kotlin/Swift) may affect only one platform.

### Version management

- All three targets read version from `pubspec.yaml` `version:` field.
- **Shared semver** (`X.Y.Z`): an iOS-only hotfix bumps the shared version in pubspec.yaml. Android doesn't release that version, but the source version advances.
- **Build number** (`+N`): configurable, off by default. CI manages build numbers at build time via `flutter build --build-number`. Opt-in config enables rlsbl auto-increment. When off, rlsbl does not read or write the `+N` suffix.
- Tag format: `<name>-ios@vX.Y.Z`, `<name>-android@vX.Y.Z`, `<name>@vX.Y.Z` (for dart libraries).

### Shorebird OTA support

Shorebird is in scope. Release types:

- **`build` release**: bumps version, creates app store submission artifacts. Required when native code (Kotlin/Swift/Gradle/Xcode) changes.
- **`ota` release**: Shorebird patch targeting an existing build release. Does not bump the semver version. Only valid when changes are Dart-only.

Release type is specified explicitly: `rlsbl release build` or `rlsbl release ota`. rlsbl auto-detects which type is valid based on changed files since last build release:
- Native file changes detected + user says `ota` -> error ("native changes require a build release")
- Dart-only changes + user says `build` -> allowed (user may want a full release for non-code reasons)
- No explicit flag -> error ("specify --build or --ota")

### Publishing

- `dart` target: publish is a no-op by default. pub.dev publishing available via config (`publish.dart.local = true`). Uses `dart pub publish --force`. First publish to pub.dev must be interactive (OIDC for subsequent CI publishes).
- `flutter-ios` / `flutter-android`: publish means building the artifact. Actual app store upload is a post-release hook concern (Fastlane, Codemagic, etc.), not rlsbl's.

### Workspace graph integration

- Dart scanner (from cross-language-workspace todo) parses pubspec.yaml for intra-workspace deps.
- Supports `resolution: workspace` pattern (Dart 3.6+). No legacy `path:` dep support.
- The package name in `import 'package:foo/...'` always maps 1:1 to the `name:` field in pubspec.yaml (confirmed by research).

## Implementation

### dart target (`rlsbl/targets/dart.py`)

- `detect()`: returns True if `pubspec.yaml` exists
- `read_version()`: parses `pubspec.yaml`, extracts `version:` field, strips `+N` suffix (returns only `X.Y.Z`)
- `write_version()`: updates `version:` field in pubspec.yaml. If build number management is enabled, increments `+N`. Otherwise preserves existing `+N` or omits it.
- `publish()`: no-op unless `publish.dart.local = true`, then runs `dart pub publish --force`
- `version_file()`: returns `"pubspec.yaml"`
- `tag_format()`: returns `"v{version}"`
- `monorepo_tag_format()`: returns `"{name}@v{version}"`

### flutter-ios / flutter-android targets

- Extend or wrap the dart target with platform-specific behavior.
- `detect()`: returns True if `pubspec.yaml` exists AND contains Flutter-specific fields (e.g., `flutter:` section, `uses-material-design`)
- `tag_format()`: returns `"{name}-ios@v{version}"` / `"{name}-android@v{version}"`
- `publish()`: builds the platform artifact (`flutter build ios` / `flutter build apk`), but actual store upload is out of scope (hooks).
- Release type validation: checks changed files to validate `build` vs `ota` flag.

### Build number config

In `.rlsbl/config.json`:
```json
{
  "build_number": {
    "enabled": false,
    "strategy": "increment"
  }
}
```

When `enabled: true`, `write_version()` reads the current `+N`, increments it, and writes `X.Y.Z+N+1`. When `enabled: false`, the `+N` portion is not touched.

## Affected files

- New: `rlsbl/targets/dart.py`, `rlsbl/targets/flutter_ios.py`, `rlsbl/targets/flutter_android.py`
- `rlsbl/targets/__init__.py` -- register new targets in TARGETS dict
- `rlsbl/targets/protocol.py` -- may need a `release_type` concept (build vs ota)
- `rlsbl/commands/release.py` -- support release type flags, changed-file detection for ota validation

## Prerequisites

- Cross-language workspace support (Dart scanner for pubspec.yaml)

## Effort

Large. Three target implementations, Shorebird integration, release type detection, build number management.
