---
description: "Native iOS and Android release targets — detection rules, version management, build number handling, and platform constraints."
---

# Native targets

## Overview

The `native-ios` and `native-android` targets handle platform-specific versioning for native mobile applications without cross-platform frameworks. They manage marketing versions and build numbers in platform-native project files (Xcode pbxproj, Gradle build files).

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

The native-ios target activates when:

1. No `Package.swift` is present in the project root (SPM projects use the `swift` target instead)
2. At least one `*.xcodeproj` directory exists containing a `project.pbxproj` file
3. The pbxproj file contains `MARKETING_VERSION` or `CURRENT_PROJECT_VERSION` build settings

Alternatively, Tuist-managed projects are detected via `Project.swift` containing `CFBundleShortVersionString`.

### Version reading

Version is extracted from pbxproj using a regex pattern:

```
MARKETING_VERSION\s*=\s*([^;]+);
```

The matched value is stripped of whitespace and quotes.

### Build number

`CURRENT_PROJECT_VERSION` is auto-incremented (integer bump) on each release. This corresponds to `CFBundleVersion` in the app's Info.plist and is required by App Store Connect for each submission.

### Multi-target projects

When multiple `*.xcodeproj` directories exist, the target uses the **first** xcodeproj (alphabetically) that contains version keys in its pbxproj. All version-containing xcodeproj files are updated during a version bump.

### Tuist support

For Tuist-managed projects (detected via `Project.swift`):

- Looks for `CFBundleShortVersionString` in the Swift manifest
- Version reading and writing use the Tuist project configuration rather than raw pbxproj
- Build number handling follows the same auto-increment pattern

### version_file()

Returns `None` because the actual file depends on which xcodeproj is found at runtime. This means version consistency checks rely on the target's `read_version()` method rather than direct file reads.

## native-android

### Detection

The native-android target activates when:

1. A `build.gradle` or `build.gradle.kts` file exists in the project root
2. The file contains the `com.android.application` plugin declaration
3. The file does **not** contain `com.android.library` (library projects use the `maven` target)

The content-inspection step prevents false positives on Android library modules that should be published to Maven rather than versioned as standalone apps.

### Mutual exclusion with Maven

Android libraries (`com.android.library` plugin) are excluded from native-android detection. These projects publish to Maven Central or a private Maven repository and should use the `maven` target, which handles artifact publishing, POM generation, and Maven-specific versioning conventions.

### Version reading

Version is extracted from build.gradle using regex-based inline parsing (~15 lines). The pattern matches:

```
versionName\s+["']([^"']+)["']
```

Both Groovy DSL (build.gradle) and Kotlin DSL (build.gradle.kts) use the same pattern.

### Build number

`versionCode` is auto-incremented (integer bump) on each release. This integer is required by Google Play for each APK/AAB upload and must be strictly increasing.

### Version writing

Both `versionName` and `versionCode` are updated in-place using regex substitution on the same build.gradle file where they were found.

## No CI templates

Neither native target generates CI workflow templates during `rlsbl scaffold`. This is intentional:

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
