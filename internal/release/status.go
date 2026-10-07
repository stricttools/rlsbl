package release

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workflows"
)

// The drifts `status --registry` reports: the local version against the
// latest version the registry's package listing names.
const (
	DriftAhead  = "AHEAD"
	DriftBehind = "BEHIND"
	DriftSame   = "SAME"
	// DriftUnpublished: the registry has no package of the name.
	DriftUnpublished = "UNPUBLISHED"
	// DriftPublishesNothing: nothing releases the member to a registry
	// (its releasable's publish mode is none, or it is versioned under no
	// releasable), so the registry is not asked.
	DriftPublishesNothing = "PUBLISHES_NOTHING"
)

// StatusRequest is what `rlsbl status` is asked.
type StatusRequest struct {
	// Dir is the working directory, absolute.
	Dir string
	// Target names the target to report on; empty when not named.
	Target string
	// Registry asks the registry's package listing for the latest published
	// version.
	Registry bool
	Fork     Fork
}

// Status is `rlsbl status`'s report on the member the working directory
// selects and one of its targets.
type Status struct {
	Member string `json:"member"`
	// Releasable is null for a member versioned under no releasable, and
	// every release field is then null too.
	Releasable *string `json:"releasable"`
	// Workspace is whether the repository is a workspace.
	Workspace bool   `json:"workspace"`
	Target    string `json:"target"`
	// Name is the package name the target's manifest declares.
	Name    string `json:"name"`
	Version string `json:"version"`
	// VersionFiles are the files the target's version lives in.
	VersionFiles []string `json:"version_files"`
	// ReleasableVersion is a workspace releasable's version file's version,
	// and null in a standalone repository or without a releasable.
	ReleasableVersion *string `json:"releasable_version"`
	// Branch is null when HEAD is detached.
	Branch *string `json:"branch"`
	Clean  bool    `json:"clean"`
	LatestFields
	// ChangelogHasVersion is whether the releasable's changelog holds the
	// released file of its current version.
	ChangelogHasVersion *bool     `json:"changelog_has_version"`
	Coverage            *Coverage `json:"coverage"`
	// CommitsAhead counts the unreleased commits needing a changelog entry
	// since the nearest release, and is null when this checkout contains
	// no release.
	CommitsAhead *int `json:"commits_ahead"`
	// NearestReleaseTag is the tag of the nearest release this checkout
	// contains.
	NearestReleaseTag *string `json:"nearest_release_tag"`
	// CI are the member's own CI workflow files.
	CI []string `json:"ci"`
	// Publish is whether the member has a publish workflow.
	Publish bool `json:"publish"`
	// RegistryVersion and Drift are set only under --registry.
	RegistryVersion *string `json:"registry_version"`
	Drift           *string `json:"drift"`
}

// ReadStatus reports on the member the request's directory selects.
func ReadStatus(e *strictcli.Effects, req StatusRequest) (Status, error) {
	s, err := selectAt(e, req.Dir, req.Fork)
	if err != nil {
		return Status{}, err
	}
	target, err := statusTarget(s, req.Target)
	if err != nil {
		return Status{}, err
	}
	t, err := targets.Get(target.Name)
	if err != nil {
		return Status{}, err
	}
	dir := filepath.Join(s.ws.Root, filepath.FromSlash(s.member.TargetDir(target)))
	name, found, err := t.ReadName(dir)
	if err != nil {
		return Status{}, err
	}
	if !found {
		return Status{}, fmt.Errorf("the %s target of the member %q has no manifest in %s, so there is no package to report on: create it there, or correct the target's path in .strictmetadata/releasables/releasables.toml", target.Name, s.member.Name, s.member.TargetDir(target))
	}
	version, err := t.ReadVersion(dir)
	if err != nil {
		return Status{}, err
	}
	st := Status{
		Member:       s.member.Name,
		Workspace:    s.ws.IsWorkspace(),
		Target:       target.Name,
		Name:         name,
		Version:      version.String(),
		VersionFiles: append([]string{}, t.Facts().VersionFiles...),
		CI:           []string{},
	}
	branch, attached, err := s.repo.HeadBranch()
	if err != nil {
		return Status{}, err
	}
	if attached {
		st.Branch = &branch
	}
	if st.Clean, err = s.repo.IsClean(); err != nil {
		return Status{}, err
	}
	memberDir := s.ws.MemberDir(s.member)
	sources, err := workflows.CISources(memberDir)
	if err != nil {
		return Status{}, err
	}
	for _, p := range sources {
		st.CI = append(st.CI, filepath.Base(p))
	}
	if st.Publish, err = fileExists(filepath.Join(memberDir, filepath.FromSlash(workflows.Dir), workflows.PublishFile)); err != nil {
		return Status{}, err
	}
	if s.releasable != nil {
		if err := releaseStatus(s, version, &st); err != nil {
			return Status{}, err
		}
	} else {
		st.LatestFields = LatestFields{NeverReleasedVersions: []string{}, LatestReleaseLabel: "(versioned under no releasable)"}
	}
	if req.Registry {
		if err := registryStatus(e, s, t, dir, name, version, &st); err != nil {
			return Status{}, err
		}
	}
	return st, nil
}

// statusTarget is the target status reports on: the one named, which must
// be the member's, or the member's only target. A member with several
// targets and none named is refused, naming them, since which one is meant
// cannot be guessed.
func statusTarget(s selection, named string) (declarations.Target, error) {
	list, err := targets.MemberTargets(s.ws.Root, s.member)
	if err != nil {
		return declarations.Target{}, err
	}
	var names []string
	for _, t := range list {
		names = append(names, t.Name)
		if t.Name == named {
			return t, nil
		}
	}
	switch {
	case named != "" && len(list) == 0:
		return declarations.Target{}, fmt.Errorf("the member %q (path %q) has no %s target: it declares no target and none is detected in its directory", s.member.Name, s.member.Path, named)
	case named != "":
		return declarations.Target{}, fmt.Errorf("the member %q (path %q) has no %s target; its targets are %s", s.member.Name, s.member.Path, named, strings.Join(names, ", "))
	case len(list) == 1:
		return list[0], nil
	case len(list) > 1:
		return declarations.Target{}, fmt.Errorf("the member %q (path %q) has the targets %s; name the one to report on with --target", s.member.Name, s.member.Path, strings.Join(names, ", "))
	case s.ws.IsWorkspace() && s.member.IsRoot():
		return declarations.Target{}, errors.New("this is a workspace root and its root member declares no release target, so there is no single project for `rlsbl status` to report on. " +
			"Run `rlsbl monorepo status` for every member's version, release tag, and changelog coverage, or change into one member's directory and run `rlsbl status` there")
	}
	return declarations.Target{}, fmt.Errorf("the member %q (path %q) declares no target and none is detected in its directory, so there is no package for `rlsbl status` to report on", s.member.Name, s.member.Path)
}

// releaseStatus fills the release fields of a member versioned under a
// releasable.
func releaseStatus(s selection, version semver.Version, st *Status) error {
	name := s.releasable.Name
	st.Releasable = &name
	current := version
	if s.ws.IsWorkspace() {
		v, err := s.ws.ReadReleasableVersion(name)
		if err != nil {
			return err
		}
		text := v.String()
		st.ReleasableVersion = &text
		current = v
	}
	head, err := s.repo.Head()
	if err != nil {
		return err
	}
	latest, err := s.record().Latest(head)
	if err != nil {
		return err
	}
	st.LatestFields = latestOf(latest)
	released, err := changelog.Versions(s.ws.Root, s.subject.Dir())
	if err != nil {
		return err
	}
	has := false
	for _, v := range released {
		has = has || v == current
	}
	st.ChangelogHasVersion = &has
	u, err := s.readUnreleased()
	if err != nil {
		return err
	}
	c := u.coverage()
	st.Coverage = &c
	if u.nearest != nil {
		ahead, tag := c.Total, u.nearest.Tag(s.record().Scheme())
		st.CommitsAhead, st.NearestReleaseTag = &ahead, &tag
	}
	return nil
}

// registryStatus asks the registry's package listing for the latest
// published version of the package and compares the local version with it.
// Only package-level listings are read: npm's and PyPI's package documents
// and the Go module proxy's version list, never one version's metadata.
func registryStatus(e *strictcli.Effects, s selection, t targets.Target, dir, name string, version semver.Version, st *Status) error {
	if s.releasable == nil || s.releasable.PublishMode == declarations.PublishNone {
		drift := DriftPublishesNothing
		st.Drift = &drift
		return nil
	}
	eco, query := registry.Ecosystem(t.Name()), name
	if t.Name() == targets.Go {
		module, found, err := gomodule.ModulePath(dir)
		if err != nil {
			return err
		}
		if !found {
			return fmt.Errorf("%s holds no go.mod, so there is no module to ask the Go module proxy about", dir)
		}
		query = module
	}
	client, err := registry.New(registry.Reads(e))
	if err != nil {
		return err
	}
	latest, found, err := client.LatestVersion(eco, query)
	if err != nil {
		return fmt.Errorf("asking the %s registry for the latest version of %s: %w", eco, query, err)
	}
	if !found {
		drift := DriftUnpublished
		st.Drift = &drift
		return nil
	}
	published, err := semver.Parse(latest)
	if err != nil {
		return fmt.Errorf("the %s registry names %s as the latest version of %s, which cannot be compared with %s: %w", eco, latest, query, version, err)
	}
	drift := DriftSame
	switch c := semver.Compare(version, published); {
	case c > 0:
		drift = DriftAhead
	case c < 0:
		drift = DriftBehind
	}
	st.RegistryVersion, st.Drift = &latest, &drift
	return nil
}

func fileExists(path string) (bool, error) {
	info, err := os.Stat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return !info.IsDir(), nil
}
