# Version source architecture refactor

## Context

The version source detection for Gradle/JVM projects (`_find_version_file` in `targets/maven.py`) uses a fixed priority list of 5 file formats. The only ambiguous case today is `libs.versions.toml` existing without `version_catalog_key` configured — resolved by conditional injection (catalog only participates when the config key is present).

If more version sources are added (Kotlin Multiplatform conventions, Android version codes, custom TOML-based version files) or the priority logic becomes more complex, the current if/elif chain in `_find_version_file` and the format-string dispatch in `read_version`/`write_version` will not scale.

## Solutions explored (from a 10-solution analysis)

Solutions 1-5 address the immediate bug. Solutions 6-10 are architectural improvements deferred here:

**Solution 6: VersionSourceResolver class.** Separate detection from selection. `detect_candidates(dir_path)` returns ranked candidates with metadata, `select(candidates)` picks the best. Makes the two concerns independently testable.

**Solution 7: Multi-candidate return.** `_find_version_files` returns all detected sources with usability flags. Callers pick the first usable one. Enables rich diagnostic error messages ("Found X but Y not configured; found Z but no version= line").

**Solution 8: Check-time validation + opt-out.** A `project`-tag check detects ambiguous version source configurations. Projects with a catalog must explicitly opt in (`version_catalog_key`) or opt out (`version_catalog: false`). Catches problems at check time, not release time.

**Solution 9: Strategy pattern.** Discrete strategy objects per format (`VersionCatalogSource`, `GradlePropertiesSource`, `GradleKtsSource`, `GradleGroovySource`, `PomSource`). Each implements `can_use`, `read`, `write`. Eliminates the format-string dispatch chain. ~200 lines.

**Solution 10: Full protocol + schema + registry.** Formal version source protocol with config schema validation, registration conflict detection, and scaffold integration. Maximum formalism, ~400+ lines.

## When to revisit

When a 6th version source is added, or when the format-string dispatch in `read_version`/`write_version` accumulates more branches. The current 5-format list is manageable as-is.
