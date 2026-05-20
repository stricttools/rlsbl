# Desktop target for platform packaging

## Context

wesktop apps need platform-specific packaging (.deb, .dmg, .msi, AppImage, Flatpak) for distribution. rlsbl should support a `desktop` target type that scaffolds packaging configs and produces platform artifacts at release time.

## Proposed design

### New target type

A new target type `desktop` in `.rlsbl/config.json`, alongside the existing `pypi`, `npm`, and `go` targets.

### Scaffold generation

`rlsbl scaffold --target desktop` generates platform packaging configs:

- **Flatpak**: manifest (`.flatpak.yml` or `.json`)
- **.deb**: `debian/` rules directory
- **AUR**: `PKGBUILD`
- **AppImage**: AppDir structure and build script
- **.dmg**: macOS disk image creation script
- **NSIS / WiX**: Windows installer configs
- **Homebrew**: formula template
- **winget**: manifest template

### Release integration

`rlsbl release` for projects with a `desktop` target produces platform artifacts alongside the PyPI wheel (or whatever the primary target is). The release flow:

1. Standard version bump, changelog validation, tests
2. Build the Python wheel (if `pypi` target is also present)
3. Build platform-specific packages from the packaging configs
4. Upload artifacts to the GitHub Release

### Desktop entry integration

Integration with wesktop's `create_entry()` for post-install desktop entry creation. The packaging configs should invoke this during the platform's post-install phase (e.g., `postinst` for .deb, `%post` for .rpm).

## Platforms

| Platform | Format | Distribution channel |
|----------|--------|---------------------|
| Linux | Flatpak | Flathub |
| Linux | AppImage | GitHub Releases |
| Linux | .deb | GitHub Releases, PPA |
| Linux | .rpm | GitHub Releases, COPR |
| Linux | PKGBUILD | AUR |
| macOS | .dmg | GitHub Releases |
| macOS | Homebrew formula | Homebrew tap |
| Windows | .msi | GitHub Releases |
| Windows | winget manifest | winget-pkgs repo |

## Effort

Large. Platform-specific tooling is inherently complex, and CI needs a build matrix for cross-platform artifact generation. The scaffold templates themselves are straightforward but testing across all platforms requires real packaging environments. A phased rollout (Linux first, then macOS, then Windows) is likely the practical path.
