# Add Android AAR support to the jar target

## Context

After the target rename (see `todo/target-rename-split.md`), the current `maven` target becomes `jar`. The new name correctly describes the artifact format, but `jar` doesn't cover all JVM-ecosystem artifacts. Android libraries are packaged as AAR (Android Archive) files, not JARs.

## Problem

The current `maven` target only builds JARs via `mvn deploy` or `./gradlew publish`. Android projects need:
- A different Gradle plugin (`com.android.library` instead of `java-library`)
- AAR output instead of JAR
- Different POM metadata (android-specific attributes)
- Potentially Google Maven repository alongside Maven Central

## Proposed solution

Extend the renamed `jar` target to detect Android projects and produce AARs when appropriate. Or split into separate `jar` and `aar` targets — but this loses the artifact-name simplicity.

## Detection

An Android project has:
- `build.gradle` or `build.gradle.kts` with `com.android.library` plugin
- `AndroidManifest.xml` somewhere

If detected, the target should:
- Build the AAR instead of JAR
- Use `./gradlew bundleReleaseAar` (or similar)
- Publish to the configured repository (typically Maven Central or Google Maven)

## Affected files

- The renamed `rlsbl/targets/jar.py` (formerly maven.py)
- `rlsbl/templates/jar/` (formerly maven/) — separate `ci-android.yml.tpl` and `publish-android.yml.tpl` may be needed
- Detection logic to choose JAR vs AAR mode

## Effort

Medium. Android Gradle setup is non-trivial; testing requires Android SDK. May need to be a separate `android` target if the JAR/AAR detection complexity is too much.

## Related work

- `todo/target-rename-split.md` — must be done first (the rename creates the `jar` target this todo extends)
