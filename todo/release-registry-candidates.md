# Release registries worth considering as future targets

## Context

rlsbl releases a project by bumping its version, tagging it, and publishing
to one or more registries through its release targets. This file records
other registries that hold semver-versioned releases, so a future session can
decide whether any of them should become a target. It is a catalog for later
consideration, not a plan: nothing here has been decided.

The properties that matter when judging a registry as a release destination:

- **Version immutability.** npm, PyPI, and Go's module proxy never let a
  released version be re-published with different content. A registry that
  lets a version's content change (a movable git tag, a mutable container
  tag, a replaceable release asset) breaks the guarantee that one version
  names one content, unless rlsbl enforces it itself.
- **Semver enforcement.** Some registries require semver syntax, one
  (Elm's) enforces semver meaning by comparing public APIs, and many accept
  any version string.
- **Fit for the family's projects.** The family's tools are Go
  command-line programs plus Python and npm packages, so registries for
  installable programs matter more than registries for other languages'
  libraries.

The immutability and semver notes below are general knowledge as of filing
and were not individually verified; entries marked "unverified" are the
least certain.

## Candidate registries

### Registries for installable programs

| Registry | Platform | Notes |
|---|---|---|
| Homebrew (a family tap, or homebrew-core) | macOS, Linux | `brew install <tool>`; see the dedicated section below |
| winget, Scoop, Chocolatey | Windows | only relevant if a family tool ever supports Windows |
| AUR | Arch Linux | a build recipe per version, maintained by the author |
| Snap Store, Flathub | Linux | mainly graphical applications; channels on top of versions |
| Nix packages | Nix | versions live in the package set rather than in a registry the author publishes to |

### Language package registries

| Registry | Ecosystem | Re-publish a version? | Semver |
|---|---|---|---|
| crates.io | Rust | no (yank only) | semver syntax required |
| RubyGems | Ruby | no (yank only) | not enforced |
| Maven Central | Java, Kotlin | no | not enforced |
| NuGet | .NET | no (unlist only) | semver syntax required |
| Packagist | PHP | versions are git tags, so a moved tag moves the version | constraints assume semver |
| Hex.pm | Elixir, Erlang | no after a short grace window (unverified) | required |
| pub.dev | Dart, Flutter | no | required |
| JSR | TypeScript, Deno | no | required |
| Elm packages | Elm | no | enforced by public-API comparison |
| CocoaPods trunk | iOS, macOS | no (unverified) | semver syntax required |
| Swift Package Index | Swift | indexes git tags, so a moved tag moves the version | semver tags required |
| Julia General registry | Julia | no | required |
| Hackage | Haskell | no | its own versioning policy, not semver |
| CPAN, CRAN, LuaRocks, opam, Nimble | Perl, R, Lua, OCaml, Nim | mostly no | not enforced |

The user is not interested in Rust, so crates.io is recorded only for
completeness.

### Artifact and extension registries

| Registry | Holds | Notes |
|---|---|---|
| Docker Hub, GitHub's container registry, Quay | container images | tags are mutable and deletable by default; see the container section below |
| Helm chart repositories, Artifact Hub | Kubernetes charts | chart versions must be semver; often stored in OCI registries, with the same mutable-tag caveat |
| Terraform Registry | modules and providers | versions are git tags; semver required |
| VS Code Marketplace, Open VSX | editor extensions | see the dedicated section below |
| JetBrains Marketplace | IDE plugins | a new version per upload |
| Chrome Web Store, Firefox add-ons | browser extensions | versions must increase; submissions are reviewed |
| Buf Schema Registry | Protobuf schemas | versioned by commits and labels, not semver |
| Ansible Galaxy, Puppet Forge | configuration-management content | semver versions |
| GitHub Releases | anything | tags can move and assets can be replaced unless GitHub's immutable-releases setting is on (unverified) |

## Homebrew

Homebrew gives a family Go tool the shortest human install
(`brew install <tool>`). Two routes exist.

**A family tap.** A repository such as `stricttools/homebrew-tap`, installed
from with `brew install stricttools/tap/<tool>`, or `brew tap stricttools/tap`
once and then `brew install <tool>`. It is under the family's full control
and available immediately.

- The Go targets release through GoReleaser. GoReleaser's documentation (read
  at filing time) marks the `brews` formula section as deprecated and
  `homebrew_casks` as its replacement since GoReleaser v2.10. A minimal entry
  names the tap repository's owner and name.
- Publishing from one repository into the tap needs a token with content
  write access to the tap repository; GitHub Actions' default token cannot
  write to another repository.
- rlsbl's GoReleaser template carries a `brewsSection` for the deprecated
  `brews` form, so adopting a tap also means moving that template to
  `homebrew_casks`.
- Unverified and decisive: casks may install only on macOS. If so, a cask
  gives Linux Homebrew users nothing, and those users keep installing through
  the npm or PyPI packages or `go install`. Verify before building.

**homebrew-core.** The default repository, giving `brew install <tool>` with
no tap, but acceptance is decided by Homebrew's maintainers. Their acceptance
rules (read at filing time) require: a stable release with an immutable tag
or release; a formula that builds from source (binary-only software belongs
in casks; a Go program builds from source, which satisfies this); a license
compatible with the Debian Free Software Guidelines; self-updating disabled;
no native macOS `.app` bundle as the main output; and a notability judgment
(the page read gave no numeric thresholds).

**Suggested direction.** A family tap for every Go command-line tool first,
and homebrew-core submissions for tools that gain users later.

## VS Code Marketplace and Open VSX

No family project is a VS Code extension at filing time, so these matter only
once an extension exists; what that extension would be is a product decision
outside rlsbl.

- VS Code Marketplace: a registered publisher, packaging with Microsoft's
  `vsce`, publishing with a token; every upload needs a new semver version.
- Open VSX: the open registry used by VSCodium, Cursor, and other VS Code
  derivatives; a claimed namespace and publishing with `ovsx`.
- Publishing to both reaches every VS Code-based editor. rlsbl would need a
  target covering both (the mechanics above are general knowledge, not
  re-verified).

## Container registries as release destinations

Container registries are a legitimate semver release channel for runnable
software, but by default they lack version immutability: a version tag can be
re-pushed with different content or deleted, and only the content digest is
permanent. Docker Hub offers opt-in immutable tags per repository (all tags,
or tags matching a pattern), after which a push to an existing tag is
refused and the tag cannot be deleted (Docker's documentation, read at filing
time; plan availability not stated there). Whether GitHub's container
registry has an equivalent is unverified.

If rlsbl keeps or adds a container target, three conditions make it as
trustworthy as the immutable registries:

1. When the version tag already exists, compare digests: identical content
   means the release already happened; different content is a hard error.
   Silently skipping an existing tag is not acceptable.
2. Record the pushed digest in the release record, tying the version to its
   content even if the tag later moves.
3. Push only the exact version tag, never `latest` or moving major and minor
   tags unless a project declares them.

## Solutions

1. **Add a Homebrew tap target (or a tap option on the Go target).** Pros:
   the shortest install for every Go tool, built on GoReleaser support that
   already exists. Cons: a tap repository and a cross-repository token to
   manage; the macOS-only question above must be settled first.
2. **Add a VS Code target publishing to both the Marketplace and Open VSX.**
   Pros: reaches every VS Code-based editor. Cons: nothing to publish until an
   extension exists.
3. **Harden any container target per the three conditions above.** Pros:
   container releases become as trustworthy as npm and PyPI releases. Cons:
   digest comparison and recording add work to the publish job.
4. **Leave the other registries catalogued here until a real project needs
   one.** Pros: no speculative targets. Cons: none; this is the default for
   every entry without a user.

## Affected files

- `rlsbl/targets/` for any new target, plus its templates under
  `rlsbl/templates/`.
- The Go target's GoReleaser template, for moving `brewsSection` to
  `homebrew_casks`.
- Target detection, `check-name` target choices, docs, and the config schema
  wherever targets are enumerated.

## Effort

- Homebrew tap: small to medium (GoReleaser does the formula or cask; rlsbl
  adds config, the template change, and the token wiring).
- VS Code target: medium, and only once an extension exists.
- Container hardening: small to medium.
- The catalogued registries: none until a project needs one.
