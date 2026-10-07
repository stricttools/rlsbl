package declarations

// Layout is whether a repository is one standalone project or a workspace.
// It is what releasables.toml declares, whatever the member count: a
// workspace whose only member is the root member is still a workspace.
type Layout string

// The two layouts.
const (
	LayoutStandalone Layout = "standalone"
	LayoutWorkspace  Layout = "workspace"
)

// PublishMode is whether a releasable publishes from CI or not at all.
type PublishMode string

// The two publish modes.
const (
	PublishCI   PublishMode = "ci"
	PublishNone PublishMode = "none"
)

// The release targets rlsbl supports, which are also the pipeline types.
const (
	TargetGo   = "go"
	TargetNPM  = "npm"
	TargetPyPI = "pypi"
)

// The artifacts a pipeline publishes.
const (
	// ArtifactBinary is a go pipeline's built binaries.
	ArtifactBinary = "binary"
	// ArtifactLibrary is a go pipeline's importable module.
	ArtifactLibrary = "library"
	// ArtifactPackage is an npm or pypi pipeline's plain package.
	ArtifactPackage = "package"
	// ArtifactGoBinary is an npm or pypi pipeline's per-platform packages
	// carrying a go binary pipeline's binaries.
	ArtifactGoBinary = "go-binary"
)

// Releasables is a repository's release declarations,
// .strictmetadata/releasables/releasables.toml.
type Releasables struct {
	Layout Layout
	// ReleaseBranches is never empty.
	ReleaseBranches []string
	// GitHubRepository is owner/name, or empty when the origin remote names
	// the repository.
	GitHubRepository string
	// EnvironmentFile is a KEY=VALUE file loaded into the release's
	// environment, or empty.
	EnvironmentFile string
	Timeouts        Timeouts
	Releasables     []Releasable
	Members         []Member
}

// Timeouts are the release's timeouts in seconds. Zero is a timeout the
// file does not declare (a declared one is at least 1).
type Timeouts struct {
	PushSeconds  int64
	CISeconds    int64
	CheckSeconds int64
	// HookSeconds zero means hooks run with no timeout.
	HookSeconds int64
}

// Releasable is a unit of versioning: one version, one changelog, one tag
// scheme, and the members versioned under it.
type Releasable struct {
	Name string
	// TagFormat holds one {version} placeholder and optionally {name}.
	TagFormat   string
	PublishMode PublishMode
	// PublishCICheckPattern is the pattern of the check-run names the root's
	// own CI reports. Only the releasable owning the root member of a
	// workspace that publishes from CI declares one, and it must.
	PublishCICheckPattern string
	// DeployCommand is the argv a release runs to deploy the releasable,
	// {version} replaced by the released version; nil when none.
	DeployCommand []string
	Hooks         Hooks
}

// Hooks are the commands run at the release's hook points, in order. A
// point with no commands is an empty list.
type Hooks struct {
	PreChecks   []Hook
	PreRelease  []Hook
	PostRelease []Hook
}

// IsEmpty reports whether no hook point has a command.
func (h Hooks) IsEmpty() bool {
	return len(h.PreChecks) == 0 && len(h.PreRelease) == 0 && len(h.PostRelease) == 0
}

// Hook is one command a hook point runs.
type Hook struct {
	// Command is the shell command line.
	Command string
	// Dir is where it runs, relative to the declaring member; empty means
	// the member's own directory.
	Dir string
	// Env holds environment variables added for it; nil when none.
	Env map[string]string
}

// Member is one directory of the repository rlsbl knows. It owns every file
// under its path that no deeper member claims; the root member owns what no
// other member claims.
type Member struct {
	// Path is canonical and repository-relative; the root member's is ".".
	Path string
	// Name is "root" for the root member and never "root" for another.
	Name string
	// Releasable names the releasable the member is versioned under; empty
	// when the member declares releasable = false.
	Releasable        string
	DevOnly           bool
	Library           bool
	TestOnly          bool
	DependsOn         []string
	ImportName        string
	RegistryName      string
	Description       string
	LintAllow         []string
	InternalDepFloors []string
	// Targets is nil when the member declares none and its targets are
	// detected from its manifests.
	Targets        []Target
	Hooks          Hooks
	ExternalChecks []ExternalCheck
	Test           TestSettings
	Pipelines      []Pipeline
}

// IsRoot reports whether the member owns the repository root.
func (m Member) IsRoot() bool { return m.Path == RootPath }

// Versioned reports whether the member is versioned under a releasable.
func (m Member) Versioned() bool { return m.Releasable != "" }

// DevNode reports whether the member is a dev node: dev-only, and versioned
// under no releasable. Both halves are declared; "dev node" is derived from
// them and declared nowhere.
func (m Member) DevNode() bool { return m.DevOnly && m.Releasable == "" }

// Pipeline is the member's pipeline of that name, and false when it declares
// none.
func (m Member) Pipeline(name string) (Pipeline, bool) {
	for _, p := range m.Pipelines {
		if p.Name == name {
			return p, true
		}
	}
	return Pipeline{}, false
}

// Target is one release target a member declares.
type Target struct {
	// Name is go, npm, or pypi.
	Name string
	// Path is the target's directory relative to the member, canonical;
	// empty means the member's own directory.
	Path string
}

// ExternalCheck is a check a member declares: a shell command.
type ExternalCheck struct {
	Name      string
	Tag       string
	Command   string
	DependsOn []string
	// Cwd is relative to the member; empty means the member's directory.
	Cwd string
}

// TestSettings say how the built-in tests run; an empty field is a setting
// the member does not declare.
type TestSettings struct {
	PyPIMarkers string
	GoCommand   string
}

// Pipeline is one publish pipeline of a member.
type Pipeline struct {
	Name string
	// Type is go, npm, or pypi.
	Type string
	// Target is the target the pipeline publishes, one its member declares.
	Target string
	// Local publishes from this machine instead of CI.
	Local bool
	// Artifact is binary or library for go, package or go-binary for npm
	// and pypi.
	Artifact string
	// InstallPaths are a go pipeline's main packages; nil when none.
	InstallPaths []string
	// HomebrewTap is a go binary pipeline's Homebrew tap, or empty.
	HomebrewTap string
	// BinaryPipeline is the go binary pipeline an npm or pypi go-binary
	// pipeline wraps, or empty.
	BinaryPipeline string
}

// Releasable is the releasable of that name, and false when none is
// declared.
func (d *Releasables) Releasable(name string) (Releasable, bool) {
	for _, r := range d.Releasables {
		if r.Name == name {
			return r, true
		}
	}
	return Releasable{}, false
}

// Member is the member of that name, and false when none is declared.
func (d *Releasables) Member(name string) (Member, bool) {
	for _, m := range d.Members {
		if m.Name == name {
			return m, true
		}
	}
	return Member{}, false
}

// MemberAt is the member declared at the canonical path p, and false when
// none is.
func (d *Releasables) MemberAt(p string) (Member, bool) {
	for _, m := range d.Members {
		if m.Path == p {
			return m, true
		}
	}
	return Member{}, false
}

// RootMember is the member owning the repository root. Parse refuses
// declarations without one, so every parsed Releasables has it.
func (d *Releasables) RootMember() Member {
	m, _ := d.MemberAt(RootPath)
	return m
}

// MembersOf are the members versioned under the named releasable, in
// declaration order. The empty name names no releasable and has none.
func (d *Releasables) MembersOf(releasable string) []Member {
	if releasable == "" {
		return nil
	}
	var out []Member
	for _, m := range d.Members {
		if m.Releasable == releasable {
			out = append(out, m)
		}
	}
	return out
}

// IsWorkspace reports whether the declarations declare a workspace.
func (d *Releasables) IsWorkspace() bool { return d.Layout == LayoutWorkspace }
