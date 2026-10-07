package releaseops

import (
	"fmt"
	"path"
	"strings"

	"github.com/stricttools/rlsbl/internal/ci"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
)

// Whether a version is out in the world is read from evidence: the package
// listings of the registries (npm's package document, PyPI's project
// document, the Go module proxy's version list), whether origin carries a Go
// module's version tag, and the runs of the publish workflows a release's
// tag started. No request ever names one version: a registry is asked for
// the listing of every version it holds, and the version is looked up in
// it, because a lookup naming a version that was never published can
// record it and burn the number.

// Finding is what one piece of evidence says about a version.
type Finding string

// The findings.
const (
	// Published: the version is out; it alone blocks an undo.
	Published Finding = "published"
	// Unpublished: a source that would see the version observed its
	// absence.
	Unpublished Finding = "unpublished"
	// Publishing: a publish run for the version's tag is still running, so
	// it may publish at any moment; it blocks like published.
	Publishing Finding = "publishing"
	// Inconclusive: the source could not say.
	Inconclusive Finding = "inconclusive"
)

// Evidence is one source's finding about one package, module, or workflow.
type Evidence struct {
	// Source names where the finding was read: "npm", "pypi", "go proxy",
	// "go tag", or "publish runs".
	Source string `json:"source"`
	// Subject is the package, the module, or the workflow file.
	Subject string  `json:"subject"`
	Finding Finding `json:"finding"`
	Message string  `json:"message"`
	// observed is true for an inconclusive finding the source did answer
	// (the proxy not listing a version), and false for a question that went
	// unanswered.
	observed bool
}

func (e Evidence) String() string {
	return fmt.Sprintf("%s %s: %s (%s)", e.Source, e.Subject, e.Finding, e.Message)
}

// Package is one package a member of the releasable publishes under one of
// its targets.
type Package struct {
	Member string
	// Target is go, npm, or pypi.
	Target string
	// Dir is the target's directory, repository-relative.
	Dir string
	// Name is the npm or PyPI package name, or the Go module path; empty
	// when the manifest names none.
	Name string
}

// packages are the packages of every member of the releasable, in
// declaration order and, within a member, target order.
func (s Selection) packages() ([]Package, error) {
	var out []Package
	for _, m := range s.Workspace.MembersOf(s.Releasable.Name) {
		ts, err := targets.MemberTargets(s.Root(), m)
		if err != nil {
			return nil, err
		}
		for _, t := range ts {
			dir := m.TargetDir(t)
			pkg := Package{Member: m.Name, Target: t.Name, Dir: dir}
			switch t.Name {
			case declarations.TargetGo:
				name, _, err := gomodule.ModulePath(s.abs(dir))
				if err != nil {
					return nil, err
				}
				pkg.Name = name
			default:
				target, err := targets.Get(t.Name)
				if err != nil {
					return nil, err
				}
				name, _, err := target.ReadName(s.abs(dir))
				if err != nil {
					return nil, err
				}
				pkg.Name = name
			}
			out = append(out, pkg)
		}
	}
	return out, nil
}

// goModuleTag is the tag the Go module proxy resolves version v of the
// module rooted at the repository-relative dir by: v<version> at the root,
// <dir>/v<version> below it.
func goModuleTag(dir string, v semver.Version) string {
	if dir == declarations.RootPath {
		return "v" + v.String()
	}
	return path.Join(dir, "v"+v.String())
}

// prober gathers evidence.
type prober struct {
	reg  registry.Client
	repo git.Repo
	// remote is false for a repository with no origin: a Go module's tag
	// then has nowhere to be published.
	remote bool
}

// packageEvidence is what the sources say about version v of pkg. npm and
// PyPI answer from their listings, published or unpublished. A Go module
// answers twice: origin's tag (present: published; absent: unpublished),
// and the proxy's version list, which can only say published, because the
// proxy lists a version lazily, the first time anyone asks for it, so its
// silence clears nothing.
func (p prober) packageEvidence(pkg Package, v semver.Version) []Evidence {
	subject := pkg.Name
	if subject == "" {
		subject = pkg.Dir
	}
	if pkg.Name == "" {
		return []Evidence{{Source: pkg.Target, Subject: subject, Finding: Inconclusive, Message: fmt.Sprintf("the %s manifest in %s names no package", pkg.Target, pkg.Dir)}}
	}
	switch pkg.Target {
	case declarations.TargetNPM:
		doc, found, err := p.reg.NpmPackage(pkg.Name)
		switch {
		case err != nil:
			return []Evidence{{Source: "npm", Subject: subject, Finding: Inconclusive, Message: err.Error()}}
		case !found:
			return []Evidence{{Source: "npm", Subject: subject, Finding: Unpublished, Message: "npm has no package " + pkg.Name}}
		case doc.Has(v.String()):
			return []Evidence{{Source: "npm", Subject: subject, Finding: Published, Message: fmt.Sprintf("npm lists %s@%s", pkg.Name, v)}}
		}
		return []Evidence{{Source: "npm", Subject: subject, Finding: Unpublished, Message: fmt.Sprintf("npm's listing of %s does not hold %s", pkg.Name, v)}}
	case declarations.TargetPyPI:
		r, found, err := p.reg.PypiRelease(pkg.Name, v.String())
		switch {
		case err != nil:
			return []Evidence{{Source: "pypi", Subject: subject, Finding: Inconclusive, Message: err.Error()}}
		case !found:
			return []Evidence{{Source: "pypi", Subject: subject, Finding: Unpublished, Message: "PyPI has no project " + pkg.Name}}
		case r.Listed:
			return []Evidence{{Source: "pypi", Subject: subject, Finding: Published, Message: fmt.Sprintf("PyPI lists %s %s", pkg.Name, v)}}
		}
		return []Evidence{{Source: "pypi", Subject: subject, Finding: Unpublished, Message: fmt.Sprintf("PyPI's listing of %s does not hold %s", pkg.Name, v)}}
	}
	return []Evidence{p.goTagEvidence(pkg, v), p.goProxyEvidence(pkg, v)}
}

func (p prober) goTagEvidence(pkg Package, v semver.Version) Evidence {
	tag := goModuleTag(pkg.Dir, v)
	if !p.remote {
		return Evidence{Source: "go tag", Subject: pkg.Name, Finding: Unpublished, Message: "the repository has no origin remote, so " + tag + " was never pushed"}
	}
	_, found, err := p.repo.RemoteRef(origin, "refs/tags/"+tag)
	switch {
	case err != nil:
		return Evidence{Source: "go tag", Subject: pkg.Name, Finding: Inconclusive, Message: err.Error()}
	case found:
		return Evidence{Source: "go tag", Subject: pkg.Name, Finding: Published, Message: "origin carries " + tag + ", which the module proxy serves to anyone who asks"}
	}
	return Evidence{Source: "go tag", Subject: pkg.Name, Finding: Unpublished, Message: "origin does not carry " + tag}
}

func (p prober) goProxyEvidence(pkg Package, v semver.Version) Evidence {
	listing, found, err := p.reg.GoVersions(pkg.Name)
	switch {
	case err != nil:
		return Evidence{Source: "go proxy", Subject: pkg.Name, Finding: Inconclusive, Message: err.Error()}
	case found && listing.Has("v"+v.String()):
		return Evidence{Source: "go proxy", Subject: pkg.Name, Finding: Published, Message: fmt.Sprintf("the module proxy lists %s@v%s, and it serves a listed version forever", pkg.Name, v)}
	}
	return Evidence{Source: "go proxy", Subject: pkg.Name, Finding: Inconclusive, Message: "the module proxy does not list v" + v.String() + ", which it lists only once somebody has asked for it", observed: true}
}

// publishRunEvidence is what the runs of the release-triggered workflows in
// the tagged tree say: a run for the tag that succeeded published, and one
// still running may publish at any moment. A workflow with no run for the
// tag, or only failed ones, says nothing either way.
func publishRunEvidence(gh github.Client, slug github.Repository, repo git.Repo, tag, commit string) []Evidence {
	workflows, err := ci.ReleaseWorkflows(repo, commit)
	if err != nil {
		return []Evidence{{Source: "publish runs", Subject: tag, Finding: Inconclusive, Message: err.Error()}}
	}
	var out []Evidence
	for _, w := range workflows {
		if !w.Starts() {
			continue
		}
		runs, err := gh.WorkflowRunsAt(slug, w.File(), commit)
		if err != nil {
			out = append(out, Evidence{Source: "publish runs", Subject: w.Path, Finding: Inconclusive, Message: err.Error()})
			continue
		}
		finding, message := Inconclusive, "no run of "+w.Path+" for "+tag+" succeeded"
		for _, r := range runs {
			if r.HeadBranch != tag || (r.Event != "release" && r.Event != "workflow_dispatch") {
				continue
			}
			if r.Status != "completed" {
				finding, message = Publishing, fmt.Sprintf("the run %d of %s for %s is %s", r.ID, w.Path, tag, r.Status)
				break
			}
			if r.Conclusion == "success" {
				finding, message = Published, fmt.Sprintf("the run %d of %s for %s succeeded", r.ID, w.Path, tag)
			}
		}
		out = append(out, Evidence{Source: "publish runs", Subject: w.Path, Finding: finding, Message: message})
	}
	return out
}

// Verdict is the evidence's answer to whether a version may be treated as
// never published.
type Verdict string

// The verdicts.
const (
	Cleared Verdict = "cleared"
	Blocked Verdict = "blocked"
)

// GateResult is the verdict with its reason and every piece of evidence.
type GateResult struct {
	Verdict  Verdict    `json:"verdict"`
	Reason   string     `json:"reason"`
	Evidence []Evidence `json:"evidence"`
}

// judge decides: any published or publishing finding blocks; otherwise at
// least one source must have observed the version's absence, and when none
// did there is no authoritative evidence, which blocks too.
func judge(evidence []Evidence) GateResult {
	var published, publishing []string
	unpublished := false
	for _, e := range evidence {
		switch e.Finding {
		case Published:
			published = append(published, e.Source+" "+e.Subject)
		case Publishing:
			publishing = append(publishing, e.Subject)
		case Unpublished:
			unpublished = true
		}
	}
	result := GateResult{Verdict: Blocked, Evidence: append([]Evidence{}, evidence...)}
	switch {
	case len(published) > 0:
		result.Reason = "published: " + strings.Join(published, ", ")
	case len(publishing) > 0:
		result.Reason = "a publish run is still running: " + strings.Join(publishing, ", ")
	case !unpublished:
		result.Reason = "no source observed the version's absence, so whether it was published cannot be told"
	default:
		result.Verdict = Cleared
		result.Reason = "a source that would see the version observed its absence, and none saw it published"
	}
	return result
}

// packageFinding is one package's publication for a yank: published when a
// source saw it, unpublished when a source observed its absence and none
// could not answer, and inconclusive otherwise.
func packageFinding(evidence []Evidence) Finding {
	unpublished, unanswered := false, false
	for _, e := range evidence {
		switch e.Finding {
		case Published:
			return Published
		case Unpublished:
			unpublished = true
		case Inconclusive:
			if !e.observed {
				unanswered = true
			}
		}
	}
	if unpublished && !unanswered {
		return Unpublished
	}
	return Inconclusive
}
