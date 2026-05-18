# Full target rename and split (artifact + channel)

## Context

Several existing target names describe the publish destination rather than what's built. This is confusing — especially when a target is used in non-default modes (e.g., `pypi` with `private: true` actually builds wheels and attaches them to GitHub Releases, not pypi.org).

Decisions made in earlier planning:
- Rename five targets to describe the artifact, not the channel
- Eventually split every target into "artifact" + "channel" so the architecture is consistent
- Add an alias system so old names keep working through a deprecation window
- Provide migration via `rlsbl migrate`

## Renames

| Current name | New name | Rationale |
|---|---|---|
| `pypi` | `wheel` | Describes the artifact (Python wheel/sdist per PEP 427) |
| `hex` | `beam` | Describes the BEAM VM (Erlang/Elixir/Gleam) |
| `cargo` | `crate` | Describes the artifact (Rust crate) |
| `deno` | `jsr` | Describes the registry (JavaScript Registry — used by Deno + Bun + Node) |
| `maven` | `jar` | Describes the artifact format |
| `docs` | `selfdoc` | Honest about being a selfdoc-specific target |

Targets keeping current names: `npm`, `go`, `swift`, `swift-apple`, `spec`, `zig`, `plain`, `docker`.

## Full split (artifact + channel)

Beyond renames, the larger architectural change: every target splits into:
- An artifact target (`wheel`, `crate`, `jar`, ...) — describes what's built
- A channel target (`pypi`, `cratesio`, `mavencentral`, ...) — describes where it goes

Example config:
```json
{
  "targets": ["wheel"],
  "channels": ["pypi"]
}
```

Private repos use just `["wheel"]` with no channel — wheels are built and attached to GitHub Releases.

## Implementation outline

1. Add a target alias system: `TARGETS` becomes `{"wheel": WheelTarget(), "pypi": "wheel", ...}` where strings are aliases. All lookups resolve through the alias map.
2. Rename target classes and files: `PypiTarget` → `WheelTarget`, `pypi.py` → `wheel.py`, `templates/pypi/` → `templates/wheel/`, etc.
3. Update template_dir() implementations and all hardcoded string references.
4. Add `channels` config key alongside `targets`. Update publish flow to iterate channels (currently each target.publish() does both).
5. Add `rlsbl migrate` command (or extend the existing one) to rewrite `config.json` from old names to new names.
6. Update scaffold to generate config.toml with new names (depends on the unified-toml-config todo).
7. Update consumer repos via the migrate command.

## Affected files

- `rlsbl/targets/__init__.py` — TARGETS dict + alias resolution
- `rlsbl/targets/*.py` — class renames
- `rlsbl/templates/*/` — directory renames
- ~15 production source locations with hardcoded target name strings (per earlier audit)
- Test files using target names as fixture data
- Consumer repos' config files

## Effort

Large. The rename alone touches ~30 files. The full split is a multi-week architectural change. Should be done in stages:
- Stage 1: alias system (allows new names without breaking old)
- Stage 2: rename classes/files/templates
- Stage 3: migration command + consumer migration
- Stage 4: full split (channels separated from artifacts)

## Related work

- `todo/.obsolete/rename-pypi-target.md` — earlier, narrower framing of this same problem

## Android Support (AAR)

### Context

After the target rename above, the current `maven` target becomes `jar`. The new name correctly describes the artifact format, but `jar` doesn't cover all JVM-ecosystem artifacts. Android libraries are packaged as AAR (Android Archive) files, not JARs.

### Problem

The current `maven` target only builds JARs via `mvn deploy` or `./gradlew publish`. Android projects need:
- A different Gradle plugin (`com.android.library` instead of `java-library`)
- AAR output instead of JAR
- Different POM metadata (android-specific attributes)
- Potentially Google Maven repository alongside Maven Central

### Proposed solution

Extend the renamed `jar` target to detect Android projects and produce AARs when appropriate. Or split into separate `jar` and `aar` targets — but this loses the artifact-name simplicity.

### Detection

An Android project has:
- `build.gradle` or `build.gradle.kts` with `com.android.library` plugin
- `AndroidManifest.xml` somewhere

If detected, the target should:
- Build the AAR instead of JAR
- Use `./gradlew bundleReleaseAar` (or similar)
- Publish to the configured repository (typically Maven Central or Google Maven)

### Affected files

- The renamed `rlsbl/targets/jar.py` (formerly maven.py)
- `rlsbl/templates/jar/` (formerly maven/) — separate `ci-android.yml.tpl` and `publish-android.yml.tpl` may be needed
- Detection logic to choose JAR vs AAR mode

### Effort

Medium. Android Gradle setup is non-trivial; testing requires Android SDK. May need to be a separate `android` target if the JAR/AAR detection complexity is too much.

### Dependency

This work depends on the rename above (Stage 2) being complete — it extends the `jar` target that the rename creates.
