---
description: "Native iOS and Android release targets — detection rules, version management, build number handling, and platform constraints."
---

# Native targets

## Overview

The `native-ios` and `native-android` targets handle platform-specific versioning for native mobile applications without cross-platform frameworks. They manage 2 version fields each (marketing version + build number) in platform-native project files (Xcode pbxproj, Gradle build files), with auto-incremented integer build numbers on every release.

Both targets are **conditionally auto-detectable** — they only activate when specific manifest patterns are found and competing targets are absent.

## Comparison

| Aspect | native-ios | native-android |
| --- | --- | --- |
| Manifest | `*.xcodeproj/project.pbxproj` or `Project.swift` | `build.gradle` / `build.gradle.kts` |
| Version field | `MARKETING_VERSION` | `versionName` |
| Build number field | `CURRENT_PROJECT_VERSION` | `versionCode` |
| Build number behavior | Auto-incremented on each release | Auto-incremented on each release |
| Rejection rule | `Package.swift` present (use swift target) | `com.android.library` plugin (use maven target) |
| CI templates | None (requires macOS runner) | None (requires Android SDK) |
| version_file() | Returns None (dynamic) | Returns the detected build.gradle path |

## native-ios

### Detection

The native-ios target uses a 3-step activation check that ensures it only applies to genuine Xcode app projects, not Swift packages or other iOS-adjacent project types. The target activates when all 3 conditions are met:

1. No `Package.swift` is present in the project root (SPM projects use the `swift` target instead)
2. At least one `*.xcodeproj` directory exists containing a `project.pbxproj` file
3. The pbxproj file contains `MARKETING_VERSION` or `CURRENT_PROJECT_VERSION` build settings

Alternatively, Tuist-managed projects are detected via `Project.swift` containing `CFBundleShortVersionString`.

### Version reading

Version is extracted from the Xcode project's pbxproj file using regex-based inline parsing rather than a full plist parser, since pbxproj files use a non-standard ASCII plist variant. The pattern matches the `MARKETING_VERSION` build setting, which corresponds to `CFBundleShortVersionString` in the app bundle and is the user-visible version string displayed on the App Store:

```
MARKETING_VERSION\s*=\s*([^;]+);
```

The matched value is stripped of whitespace and quotes.

### Build number

`CURRENT_PROJECT_VERSION` is auto-incremented as an integer (e.g., 1, 2, 3, ...) on each release, independent of the marketing version string. This corresponds to `CFBundleVersion` in the app's Info.plist and is required by App Store Connect for each binary submission. Apple requires the build number to be strictly increasing within each marketing version, so rlsbl reads the current integer value and writes value + 1 during every release bump.

### Multi-target projects

When multiple `*.xcodeproj` directories exist in the project root, the target uses the **first** xcodeproj (sorted alphabetically) that contains version keys in its pbxproj for reading the current version. During a version bump, all xcodeproj files containing version keys are updated simultaneously to maintain consistency across app targets, extensions, and frameworks within the same project.

### Tuist support

For Tuist-managed projects, rlsbl detects the presence of a `Project.swift` manifest and reads version information from the Tuist project configuration rather than raw pbxproj files. This supports teams that generate their Xcode project from a Swift-based DSL:

- Looks for `CFBundleShortVersionString` in the Swift manifest
- Version reading and writing use the Tuist project configuration rather than raw pbxproj
- Build number handling follows the same auto-increment pattern

### version_file()

Returns `None` because the actual file depends on which xcodeproj is found at runtime. This means version consistency checks rely on the target's `read_version()` method rather than direct file reads.

## native-android

### Detection

The native-android target uses content inspection with 3 conditions to distinguish Android application modules from library modules, preventing false positives on projects that should use the Maven target instead. The target activates when:

1. A `build.gradle` or `build.gradle.kts` file exists in the project root
2. The file contains the `com.android.application` plugin declaration
3. The file does **not** contain `com.android.library` (library projects use the `maven` target)

The content-inspection step prevents false positives on Android library modules that should be published to Maven rather than versioned as standalone apps.

### Mutual exclusion with Maven

Android libraries (`com.android.library` plugin) are excluded from native-android detection. These projects publish to Maven Central or a private Maven repository and should use the `maven` target, which handles artifact publishing, POM generation, and Maven-specific versioning conventions.

### Version reading

Version is extracted from the detected build.gradle file using regex-based inline parsing that handles both Groovy DSL and Kotlin DSL syntax. The same pattern works for both DSL variants because they share the same `versionName` property syntax. The pattern matches:

```
versionName\s+["']([^"']+)["']
```

Both Groovy DSL (build.gradle) and Kotlin DSL (build.gradle.kts) use the same pattern.

### Build number

`versionCode` is auto-incremented as an integer (e.g., 1, 2, 3, ...) on each release, independent of the semver version string. This integer is required by Google Play for each APK/AAB upload and must be strictly increasing -- rlsbl reads the current value from build.gradle and writes value + 1 during every version bump.

### Version writing

Both `versionName` and `versionCode` are updated in-place using regex substitution on the same build.gradle file where they were originally found during detection. The version name is set to the new semver string and the version code is auto-incremented as an integer, ensuring both values stay synchronized in a single atomic write.

## No CI templates

Neither native target generates CI workflow templates during `rlsbl scaffold`, unlike npm, PyPI, Go, and other targets that include ready-to-use GitHub Actions workflows. This is intentional because native mobile builds have platform-specific requirements that vary too widely for a generic template:

| Platform | Requirement | Why no template |
| --- | --- | --- |
| iOS | macOS runner with Xcode | GitHub Actions macOS runners are expensive; build configuration varies widely (certificates, provisioning profiles, Xcode version) |
| Android | Android SDK, build tools, NDK | Configuration depends heavily on project specifics (SDK version, NDK usage, signing keystores) |

Users must configure CI manually for native targets. The release flow still handles version bumping, tagging, and GitHub Release creation — only the publish/build step is absent.

## Decision table: which target to use

| Project type | Indicators | Correct target |
| --- | --- | --- |
| iOS app (Xcode) | `*.xcodeproj` with MARKETING_VERSION, no Package.swift | `native-ios` |
| iOS app (Tuist) | `Project.swift` with CFBundleShortVersionString | `native-ios` |
| Swift package | `Package.swift` present | `swift` |
| Android app | `build.gradle` with `com.android.application` | `native-android` |
| Android library | `build.gradle` with `com.android.library` | `maven` |
| Cross-platform (Flutter) | `pubspec.yaml` with flutter dependency | `flutter` |
| Cross-platform (React Native) | `package.json` + native dirs | `npm` (with manual native versioning) |
