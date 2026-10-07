package publishrules

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/pipelines"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// Use is one publishing output a release would produce, and where it is
// declared.
type Use struct {
	// Subject is the releasable producing it; empty for an output of the
	// repository as a whole (a committed publish workflow).
	Subject string
	Output  lifecycle.Output
	// Where names the declaration or file that produces it, for a refusal.
	Where string
}

// ReleasableUses are the outputs the releasable's declarations publish: for
// each pipeline of its members, a registry write unless it is a go binary
// published from CI (whose archives go to the repository's own GitHub
// Releases), the Go module proxy notification and the Go library a pipeline
// asking the proxy makes, a Homebrew tap, and a published manifest naming
// the repository (package.json repository, homepage, or bugs; pyproject.toml
// [project.urls]) for npm and pypi pipelines. A releasable whose publish
// mode is none publishes nothing. Manifests are read from the working tree
// under w.Root.
func ReleasableUses(w *workspace.Workspace, releasable string) ([]Use, error) {
	r, ok := w.Declarations.Releasable(releasable)
	if !ok {
		return nil, fmt.Errorf("no releasable %q is declared in .strictmetadata/releasables/releasables.toml", releasable)
	}
	if r.PublishMode == declarations.PublishNone {
		return nil, nil
	}
	var uses []Use
	for _, m := range w.MembersOf(releasable) {
		for _, p := range m.Pipelines {
			where := fmt.Sprintf("the %s pipeline %q of the member %q", p.Type, p.Name, m.Name)
			add := func(o lifecycle.Output, at string) {
				uses = append(uses, Use{Subject: releasable, Output: o, Where: at})
			}
			if pipelines.AsksGoProxy(p) {
				add(lifecycle.RegistryPackage, where)
				add(lifecycle.GoProxyNotification, where)
			} else if p.Type != declarations.TargetGo {
				add(lifecycle.RegistryPackage, where)
			}
			if p.Type == declarations.TargetGo && p.Artifact == declarations.ArtifactLibrary {
				add(lifecycle.GoLibrary, where)
			}
			if p.HomebrewTap != "" {
				add(lifecycle.HomebrewTap, where+" (homebrew_tap)")
			}
			if p.Type == declarations.TargetNPM || p.Type == declarations.TargetPyPI {
				dir, err := targetDir(m, p)
				if err != nil {
					return nil, err
				}
				fields, err := repositoryFields(p.Type, filepath.Join(w.Root, filepath.FromSlash(dir)))
				if err != nil {
					return nil, err
				}
				for _, f := range fields {
					add(lifecycle.RepositoryURLInManifest, fmt.Sprintf("%s in %s, published by %s", f, dir, where))
				}
			}
		}
	}
	return uses, nil
}

// targetDir is the repository-relative directory of the target the pipeline
// publishes. The declarations refuse a pipeline whose target its member does
// not declare.
func targetDir(m declarations.Member, p declarations.Pipeline) (string, error) {
	for _, t := range m.Targets {
		if t.Name == p.Target {
			return m.TargetDir(t), nil
		}
	}
	return "", fmt.Errorf("the pipeline %q of the member %q publishes the %s target, which the member does not declare", p.Name, m.Name, p.Target)
}

// repositoryFields are the published manifest fields in dir that name the
// repository: package.json's repository, homepage, and bugs, and
// pyproject.toml's [project.urls].
func repositoryFields(target, dir string) ([]string, error) {
	switch target {
	case declarations.TargetNPM:
		path := filepath.Join(dir, "package.json")
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("reading %s: %w", path, err)
		}
		var top map[string]json.RawMessage
		if err := json.Unmarshal(data, &top); err != nil {
			return nil, fmt.Errorf("%s is not a JSON object: %w", path, err)
		}
		var fields []string
		for _, key := range []string{"repository", "homepage", "bugs"} {
			if _, ok := top[key]; ok {
				fields = append(fields, "package.json's "+key+" field")
			}
		}
		return fields, nil
	case declarations.TargetPyPI:
		path := filepath.Join(dir, "pyproject.toml")
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("reading %s: %w", path, err)
		}
		doc, err := tomledit.Parse(data)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		values, err := tomledit.Decode[map[string]any](doc)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		project, _ := (*values)["project"].(map[string]any)
		var fields []string
		if _, ok := project["urls"]; ok {
			fields = append(fields, "pyproject.toml's [project.urls] table")
		}
		if dynamic, ok := project["dynamic"].([]any); ok {
			for _, d := range dynamic {
				if d == "urls" {
					fields = append(fields, "pyproject.toml's dynamic urls")
				}
			}
		}
		return fields, nil
	}
	return nil, nil
}

// Refusals is every refusal one evaluation found, each naming where the
// refused output is declared.
type Refusals struct {
	Items []string
}

func (r *Refusals) Error() string {
	if len(r.Items) == 1 {
		return r.Items[0]
	}
	return fmt.Sprintf("%d publishing outputs are refused:\n  - %s", len(r.Items), strings.Join(r.Items, "\n  - "))
}

// dependsOnVisibility reports whether the private-repository-publishing
// rule's answer for o, on that date, turns on GitHub's visibility: refused
// for a private repository and allowed for a public one.
func dependsOnVisibility(record *lifecycle.Record, o lifecycle.Output, on time.Time) bool {
	return record.PrivateRepositoryOutputAllowed(o, lifecycle.VisibilityPrivate, on) != nil &&
		record.PrivateRepositoryOutputAllowed(o, lifecycle.VisibilityPublic, on) == nil
}

// CheckUses evaluates both publishing rules for every use on the date of on
// and returns every refusal (*Refusals), or nil. The source is asked only
// when an answer turns on the visibility; when it cannot answer, the
// refusals that rest on it carry the reason. Release validation calls it
// with ReleasableUses and the committed workflows' uses, and the
// private-repo-publishing check with the working tree's.
func CheckUses(record *lifecycle.Record, uses []Use, source VisibilitySource, on time.Time) error {
	visibility := lifecycle.VisibilityUnknown
	var askErr error
	for _, u := range uses {
		if dependsOnVisibility(record, u.Output, on) {
			visibility, askErr = source.Visibility()
			break
		}
	}
	var items []string
	for _, u := range uses {
		if err := record.PublishAllowed(u.Subject, u.Output, visibility, on); err != nil {
			var refusal *lifecycle.Refusal
			if errors.As(err, &refusal) && refusal.Rule == lifecycle.RulePrivateRepositoryPublishing && visibility == lifecycle.VisibilityUnknown {
				err = withReason(err, askErr)
			}
			items = append(items, fmt.Sprintf("%s: %v", u.Where, err))
		}
	}
	if len(items) > 0 {
		return &Refusals{Items: items}
	}
	return nil
}

// RegistryWriteAllowed evaluates the proprietary-refuses-public-output rule
// for a registry write that is not a publish (an npm deprecate, a Go
// retract): `release yank` asks it before its registry steps.
func RegistryWriteAllowed(record *lifecycle.Record, releasable string, on time.Time) error {
	return record.PublicOutputAllowed(releasable, lifecycle.RegistryPackage, on)
}

// DeployAllowed refuses a deploy_command on a releasable that is not a
// server: a server's license is proprietary on the date of on, and it
// publishes to no registry (publish_mode = "none"), because registries only
// ever receive the client. A releasable without a deploy_command is
// allowed. The declarations-valid check and release validation call it.
func DeployAllowed(record *lifecycle.Record, r declarations.Releasable, on time.Time) error {
	if r.DeployCommand == nil {
		return nil
	}
	var problems []string
	if l, ok := record.LicenseOn(r.Name, on); !ok || !l.Proprietary() {
		current := "has no license period in effect"
		if ok {
			current = "is licensed " + l.License
		}
		problems = append(problems, fmt.Sprintf("it %s on %s, and a server's license is proprietary: classify it (`rlsbl transition classify --subject %s`), or delete deploy_command", current, on.Format(time.DateOnly), r.Name))
	}
	if r.PublishMode != declarations.PublishNone {
		problems = append(problems, fmt.Sprintf("its publish_mode is %q, and a server publishes to no registry: set publish_mode = \"none\" and publish the client from a releasable of its own", r.PublishMode))
	}
	if len(problems) == 0 {
		return nil
	}
	return fmt.Errorf("the releasable %q declares deploy_command, which only a server releasable declares, and %s", r.Name, strings.Join(problems, "; and "))
}

// WorkflowFeatures are the publish workflow features scaffold may render:
// each is false where the private-repository-publishing rule refuses it.
type WorkflowFeatures struct {
	// BuildAttestations are npm --provenance and PyPI attestations.
	BuildAttestations bool
	// GoProxyNotification is the publish job asking the Go module proxy for
	// the released module.
	GoProxyNotification bool
	// RepositoryURLs are the repository fields of generated manifests (the
	// npm platform packages and wheels).
	RepositoryURLs bool
}

// ScaffoldFeatures decides the workflow features on the date of on. A
// confidential repository renders none and asks nothing; otherwise the
// source is asked, and a visibility that cannot be had is an error carrying
// the reason, so scaffold never renders a feature it could not judge.
func ScaffoldFeatures(record *lifecycle.Record, source VisibilitySource, on time.Time) (WorkflowFeatures, error) {
	if record.Confidential(on) {
		return WorkflowFeatures{}, nil
	}
	v, askErr := source.Visibility()
	if v == lifecycle.VisibilityUnknown {
		return WorkflowFeatures{}, withReason(record.PrivateRepositoryOutputAllowed(lifecycle.BuildAttestation, v, on), askErr)
	}
	allowed := func(o lifecycle.Output) bool {
		return record.PrivateRepositoryOutputAllowed(o, v, on) == nil
	}
	return WorkflowFeatures{
		BuildAttestations:   allowed(lifecycle.BuildAttestation),
		GoProxyNotification: allowed(lifecycle.GoProxyNotification),
		RepositoryURLs:      allowed(lifecycle.RepositoryURLInManifest),
	}, nil
}
