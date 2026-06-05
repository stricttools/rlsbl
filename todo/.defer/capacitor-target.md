# Capacitor release target

## Context

Capacitor is a cross-platform mobile framework (by the Ionic team) that wraps web apps in native iOS and Android containers. Unlike Flutter, Capacitor apps have hardcoded version values in platform-specific files that are NOT derived from `package.json`. `cap sync` copies web assets but doesn't touch version numbers.

## Why deferred

Capacitor has a complex version model with independent platform files that can diverge. Three locations hold version information (`package.json`, `build.gradle`, `pbxproj`/`Project.swift`) and they must be bumped atomically. This is fundamentally different from single-source frameworks like Flutter where `pubspec.yaml` drives everything.

Implementing this properly should depend on mobileinfra-version (now public on PyPI), which already has the full cross-platform read/write API for Capacitor version files (build.gradle, package.json, pbxproj, Project.swift parsing).

## Problems from the original mobile-targets todo

### Independent platform version files (original P3)

Unlike Flutter, Capacitor apps have hardcoded version values in `build.gradle` and `pbxproj`/`Project.swift` that are NOT derived from `package.json`. These three locations can drift. A Capacitor release needs to atomically bump all three.

The current target interface (`read_version` returns one string, `write_version` modifies files) can handle this (write_version can touch multiple files), but the mental model of "one target = one version file" breaks down.

### Platform-only patches (original P4)

Android has platform-specific bugs. iOS has platform-specific bugs. For Capacitor/native apps where platform version files are independent, you can bump `versionCode` in `build.gradle` without touching `pbxproj`. This enables platform-only patch releases without forcing a no-op update on the other platform's app store.

How to model this in rlsbl's target system (single target that bumps all files vs. separate per-platform targets that can be released independently) is an open design question.
