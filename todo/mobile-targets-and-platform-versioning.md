# Mobile targets and platform versioning

## Context

rlsbl has `flutter-ios` and `flutter-android` targets. A separate project (mobilepublisher) extracted mobile tooling that includes cross-platform version management for Capacitor apps (reading/writing versions across `build.gradle`, `package.json`, `pbxproj`, `Project.swift`). This raised questions about how rlsbl should model mobile app versioning generally.

## Problem 1: Framework × platform combinatorial explosion

If every mobile framework gets separate iOS and Android targets, the target count grows multiplicatively:

- flutter-ios, flutter-android (exist today)
- capacitor-ios, capacitor-android
- react-native-ios, react-native-android
- native-ios, native-android
- compose-multiplatform-android, compose-multiplatform-ios
- ionic-ios, ionic-android

Every new framework doubles. This doesn't scale and creates maintenance burden in target code, CI templates, and documentation.

## Problem 2: Flutter's version model doesn't match the split

Flutter has a single version source of truth (`pubspec.yaml`). Both platforms derive their versions from it automatically via Flutter's build toolchain (`flutter.versionCode`/`flutter.versionName` in Gradle, `$(FLUTTER_BUILD_NAME)`/`$(FLUTTER_BUILD_NUMBER)` in Xcode). There are no independent platform version files to manage.

Investigation of the only Flutter consumer (`~/Work/F`, 45-package monorepo) confirmed: `build.gradle.kts` reads from the Flutter Gradle plugin, `Info.plist` reads from `Generated.xcconfig`, and both ultimately derive from `pubspec.yaml`. Zero independent version settings exist.

Having `flutter-ios` and `flutter-android` as separate targets implies they can be versioned independently, but they can't — they always read from the same pubspec.yaml.

## Problem 3: Capacitor/React Native version model DOES have independent files

Unlike Flutter, Capacitor apps have hardcoded version values in `build.gradle` and `pbxproj`/`Project.swift` that are NOT derived from `package.json`. `cap sync` copies web assets but doesn't touch version numbers. These three locations can drift. A Capacitor release needs to atomically bump all three.

## Problem 4: Platform-only patches

Android has platform-specific bugs. iOS has platform-specific bugs. When fixing an Android-only crash, releasing a new version bumps iOS too, causing iOS users to see a no-op update in the App Store.

How this plays out differs by framework:
- **Flutter**: You bump the `+N` build number in pubspec.yaml, then build and submit only to Play Store. iOS users see no update because nothing was submitted to the App Store. The version still incremented in pubspec.yaml.
- **Capacitor/native**: Platform version files are independent. You can bump `versionCode` in `build.gradle` without touching `pbxproj`.

## Problem 5: versionCode is not semver

Android's `versionCode` and iOS's `CFBundleVersion`/`CURRENT_PROJECT_VERSION` are monotonically increasing integers (or integer-like strings), independent of the semver marketing version. rlsbl's existing `build_number.strategy = "increment"` handles this for Flutter's `+N` suffix, but non-Flutter projects need the same concept applied to their native version files.

## Problem 6: Multi-target array handling bugs

The monorepo snapshot and graph generators don't handle multi-target arrays correctly. When a project declares `["flutter-ios", "flutter-android"]`:
- `snapshot.json` flattens to just the first element (`"flutter-ios"`)
- `graph.json` loses the Flutter target entirely, showing `"dart"`

This means monorepo tooling (impact analysis, release ordering, status) has incorrect target information for multi-target mobile projects.

## Problem 7: What is a "version source of truth"?

Different frameworks have different answers:

| Framework | Marketing version source | Build number source | Platform files independent? |
|-----------|------------------------|--------------------|-----------------------------|
| Flutter | pubspec.yaml | pubspec.yaml (`+N`) | No — derived automatically |
| Capacitor | package.json | build.gradle / pbxproj | Yes — must sync manually |
| React Native | package.json | build.gradle / pbxproj | Yes — must sync manually |
| Native Android | build.gradle | build.gradle | N/A (single platform) |
| Native iOS | pbxproj / Project.swift | pbxproj / Project.swift | N/A (single platform) |

The target system currently treats each target as owning one version file. But Capacitor-style projects have one logical version across three physical files that need atomic updating. The current target interface (`read_version` returns one string, `write_version` modifies files) can handle this (write_version can touch multiple files), but the mental model of "one target = one version file" breaks down.

## Problem 8: Publishing is conflated with versioning

The existing `flutter-ios` / `flutter-android` split appears to exist because iOS and Android have different build/publish pipelines (IPA vs AAB, App Store vs Play Store). But that's a deployment concern, not a versioning concern. The target interface combines both (`write_version` + `publish`), which forces a target split wherever the publish step differs, even when the version source is shared.

## Affected files

- `rlsbl/targets/__init__.py` — target registry (TARGETS dict)
- `rlsbl/targets/protocol.py` — ReleaseTarget protocol
- `rlsbl/targets/base.py` — BaseTarget
- `rlsbl/targets/dart.py` — DartTarget (parent of flutter targets)
- `rlsbl/targets/flutter_ios.py` and `flutter_android.py` — current Flutter targets
- `rlsbl/monorepo/snapshot.py` — multi-target array flattening bug
- `rlsbl/monorepo/graph.py` — multi-target loss bug
- `rlsbl/templates/` — CI templates per target

## Related

- `~/Projects/mobilepublisher/app-version/` — standalone library with Capacitor version read/write logic (build.gradle, package.json, pbxproj, Project.swift parsing). Could become the implementation for new targets.
- `~/Work/F/` — the only consumer of flutter-ios/flutter-android targets (45-package Flutter monorepo, zero releases so far)
