package releaseops

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/semver"
)

// npmDeprecateTimeout bounds `npm deprecate`.
const npmDeprecateTimeout = 2 * time.Minute

// yankStep is one registry's removal of a published package.
type yankStep struct {
	pkg Package
	// done is true when the registry already shows the removal (a Go
	// retraction go.mod already carries, a PyPI release whose files are all
	// yanked), so nothing is written for it.
	done bool
	// goMod is a Go module's go.mod, repository-relative.
	goMod string
}

// Yank removes a published version, the latest one included, from the registries the way each
// allows, and marks its GitHub Release: npm deprecates the version, a Go
// module gains a retract directive in its go.mod (committed; the next
// release publishes it), and PyPI, which offers no yank to a tool, is
// confirmed from its project document once the release's files are yanked
// by hand. Nothing is ever unpublished: a registry name, once held, is kept.
// The yank notice is recorded in the version's archive and committed, and
// the Release rewritten from the record. Every package's publication is read
// first, from the registries' listings, and a package whose publication
// cannot be read refuses the yank before anything is written. A releasable
// whose current license is proprietary is refused before anything is read:
// a yank writes to registries, which proprietary software never does.
func Yank(ctx *strictcli.Context, req NoticeRequest) (err error) {
	e := ctx.Effects()
	s, err := Select(e, req.Dir)
	if err != nil {
		return err
	}
	record, err := lifecycle.Load(s.Root())
	if err != nil {
		return err
	}
	if err := publishrules.RegistryWriteAllowed(record, s.Releasable.Name, time.Now()); err != nil {
		return fmt.Errorf("%w\n  Nothing was changed: a yank writes to registries (npm deprecate, a Go retraction), and no registry write is made for a proprietary releasable. `rlsbl release deprecate %s` marks the GitHub Release without writing to any registry", err, req.Version)
	}
	l, err := lock(ctx, s.Root())
	if err != nil {
		return err
	}
	defer unlock(l, &err)
	n, err := prepareNotice(ctx, s, req, yankedLabel)
	if err != nil {
		return err
	}
	steps, err := planYank(s, n.version)
	if err != nil {
		return err
	}
	if len(steps) == 0 {
		ctx.Info(fmt.Sprintf("%s is published on no registry; only its GitHub Release is marked", n.tag))
	}
	for _, st := range steps {
		state := "will yank"
		if st.done {
			state = "already yanked"
		}
		ctx.Info(fmt.Sprintf("%s %s %s: %s", st.pkg.Target, st.pkg.Name, n.version, state))
	}
	var owed []string
	var done []string
	for _, st := range steps {
		if st.done {
			continue
		}
		switch st.pkg.Target {
		case declarations.TargetNPM:
			if err := npmDeprecate(e, s, st.pkg, n.version, req.Reason, req.Use); err != nil {
				return fmt.Errorf("%w\n  Done before it: %s. Nothing else was changed; run the yank again once npm accepts the deprecation", err, joinOrNone(done))
			}
			done = append(done, "npm deprecated "+st.pkg.Name+"@"+n.version.String())
		case declarations.TargetGo:
			if err := retract(ctx, s, st, n.version, req.Reason); err != nil {
				return fmt.Errorf("%w\n  Done before it: %s", err, joinOrNone(done))
			}
			done = append(done, "retracted v"+n.version.String()+" in "+st.goMod)
		case declarations.TargetPyPI:
			owed = append(owed, pypiManualSteps(st.pkg.Name, n.version))
		}
	}
	if err := n.publish(ctx, "yank"); err != nil {
		return err
	}
	if len(owed) > 0 {
		return fmt.Errorf("%s is yanked everywhere rlsbl can yank it and its GitHub Release is marked, and PyPI is not done: PyPI offers no yank to a tool, so it is done by hand.\n%s\n  Then run this yank again: it reads PyPI's project document, finds the files yanked, and finishes", n.tag, strings.Join(owed, "\n"))
	}
	verb := "Yanked"
	if ctx.DryRun() {
		verb = "Would yank"
	}
	ctx.Out(fmt.Sprintf("%s %s (GitHub Release marked pre-release):\n%s", verb, n.tag, n.notice))
	return nil
}

func joinOrNone(items []string) string {
	if len(items) == 0 {
		return "nothing"
	}
	return strings.Join(items, "; ")
}

// planYank reads each package's publication and decides each published
// package's removal. A package whose publication cannot be read refuses
// the whole yank, naming every such package.
func planYank(s Selection, v semver.Version) ([]yankStep, error) {
	pkgs, err := s.packages()
	if err != nil {
		return nil, err
	}
	reg, err := registry.New(registry.Reads(s.effects()))
	if err != nil {
		return nil, err
	}
	remote, err := s.Repo.RemoteConfigured(origin)
	if err != nil {
		return nil, err
	}
	p := prober{reg: reg, repo: s.Repo, remote: remote}
	var steps []yankStep
	var unknown []string
	for _, pkg := range pkgs {
		evidence := p.packageEvidence(pkg, v)
		switch packageFinding(evidence) {
		case Inconclusive:
			for _, e := range evidence {
				unknown = append(unknown, "  "+e.String())
			}
			continue
		case Unpublished:
			continue
		}
		st := yankStep{pkg: pkg}
		switch pkg.Target {
		case declarations.TargetNPM:
			if _, err := exec.LookPath("npm"); err != nil {
				return nil, fmt.Errorf("npm is not on PATH, and %s@%s is published on npm: npm deprecate is how a version is yanked there. Install npm and run the yank again", pkg.Name, v)
			}
		case declarations.TargetGo:
			if st.done, st.goMod, err = retracted(s, pkg, v); err != nil {
				return nil, err
			}
		case declarations.TargetPyPI:
			r, _, err := reg.PypiRelease(pkg.Name, v.String())
			if err != nil {
				return nil, err
			}
			st.done = r.IsYanked()
		}
		steps = append(steps, st)
	}
	if len(unknown) > 0 {
		return nil, fmt.Errorf("whether %s is published could not be read for every package, and a yank acts on certainty. Nothing was changed:\n%s", v, strings.Join(unknown, "\n"))
	}
	return steps, nil
}

// retracted reads whether the module's go.mod already retracts v, and
// refuses a go.mod with uncommitted changes, which the retraction's commit
// would carry along.
func retracted(s Selection, pkg Package, v semver.Version) (bool, string, error) {
	goMod := declarations.Join(pkg.Dir, gomodule.FileName)
	f, found, err := gomodule.Read(s.abs(pkg.Dir))
	if err != nil {
		return false, goMod, err
	}
	if !found {
		return false, goMod, fmt.Errorf("%s does not exist, so the retraction of v%s has nowhere to go", goMod, v)
	}
	if gomodule.Retracted(f, "v"+v.String()) {
		return true, goMod, nil
	}
	changed, err := s.Repo.ChangedPaths([]string{goMod}, git.UntrackedNormal)
	if err != nil {
		return false, goMod, err
	}
	if len(changed) > 0 {
		return false, goMod, fmt.Errorf("%s has uncommitted changes, and the retraction of v%s is committed on its own. Nothing was changed: commit or discard them, then run the yank again", goMod, v)
	}
	return false, goMod, nil
}

// npmDeprecate runs `npm deprecate <name>@<version> <message>` in the
// package's directory, so npm reads that package's own configuration.
func npmDeprecate(e *strictcli.Effects, s Selection, pkg Package, v semver.Version, reason, use string) error {
	message := strings.TrimSpace(reason)
	if message == "" {
		message = "This version has been yanked."
	}
	if use != "" {
		message = strings.TrimRight(message, ". ") + ". Use v" + use + " instead."
	}
	spec := pkg.Name + "@" + v.String()
	if _, err := e.Run([]interface{}{"npm", "deprecate", spec, message}, strictcli.Cwd(s.abs(pkg.Dir)), strictcli.Timeout(npmDeprecateTimeout), strictcli.Stream(true)); err != nil {
		return fmt.Errorf("npm deprecate %s failed: %w", spec, err)
	}
	return nil
}

// retract adds `retract v<version>` to the module's go.mod, with the reason
// as its rationale comment, and commits it. The commit carries no
// Autogenerated trailer: the retraction reaches consumers with the next
// release, whose changelog says so.
func retract(ctx *strictcli.Context, s Selection, st yankStep, v semver.Version, reason string) error {
	path := s.abs(st.goMod)
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("reading %s: %w", st.goMod, err)
	}
	out, changed, err := gomodule.AddRetraction(st.goMod, data, "v"+v.String(), reason)
	if err != nil {
		return err
	}
	if !changed {
		// planYank asks retracted first, so a covered version never gets here.
		return fmt.Errorf("%s already retracts v%s, and the yank planned a retraction of it", st.goMod, v)
	}
	text := string(out)
	e := ctx.Effects()
	tmp := path + ".rlsbl-writing"
	if _, err := e.Write(tmp, text); err != nil {
		return fmt.Errorf("writing %s: %w", st.goMod, err)
	}
	if _, err := e.Rename(tmp, path); err != nil {
		return fmt.Errorf("writing %s: %w", st.goMod, err)
	}
	message := fmt.Sprintf("Retract v%s of %s: release a new version to publish the retraction", v, st.pkg.Name)
	if err := commit(ctx, s, message, []string{st.goMod}, false); err != nil {
		return fmt.Errorf("the retraction of v%s was written into %s, and committing it failed: %w. Commit that file", v, st.goMod, err)
	}
	ctx.Info(fmt.Sprintf("go: retracted v%s in %s; the retraction reaches consumers with the next release", v, st.goMod))
	return nil
}

// pypiManualSteps are the steps of PyPI's yank, done by hand.
func pypiManualSteps(name string, v semver.Version) string {
	return fmt.Sprintf("  PyPI %s %s:\n    1. open https://pypi.org/manage/project/%s/release/%s/\n    2. choose Options, then Yank, give the reason, and confirm\n    The release stays installable by its exact version and is hidden from every other resolution.", name, v, name, v)
}
