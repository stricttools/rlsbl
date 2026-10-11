package releaseops

import (
	"fmt"
	"os"
	"slices"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/confidential"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
)

// RetractInvocation is the command a retraction that stopped is finished
// with: the same command, run again.
const RetractInvocation = "rlsbl release retract --versions <the same versions> --reason <the same reason> --approve-consequential"

// RetractRequest is `release retract`: the released versions to retract,
// bare, and why.
type RetractRequest struct {
	Dir      string
	Versions []string
	Reason   string
	// TermList is where the confidential-term list the pushed tag's commits
	// are scanned against is.
	TermList confidential.Location
	Now      func() time.Time
}

// retraction is a retraction's plan, read before anything is written.
type retraction struct {
	s        Selection
	versions []semver.Version
	// next is the retraction version: the next number no archive and no tag
	// (here or on origin) holds.
	next semver.Version
	// goMods are the go.mod files of the releasable's Go modules,
	// repository-relative.
	goMods []string
	// commit is the retraction commit, known once it exists (a run that
	// finishes a stopped one reads it from the retraction archive).
	commit string
	// resuming is true when the record already holds the retraction.
	resuming bool
	// releases are the tags of the retracted versions that carry a GitHub
	// Release, which the retraction deletes.
	releases []string
	gh       github.Client
	slug     github.Repository
}

// Retract retracts released versions of a Go module through a new version:
// the next free version number is committed with a go.mod retract directive
// covering the named versions and itself, tagged, and only its tags are
// pushed (no branch, no GitHub Release, since it has nothing to download or
// read). The record then holds every one of them as retracted, so none
// counts as a release: the retracted versions' archives name the retraction
// version, which is archived with its release commit and the versions it
// retracts. The retracted versions' tags stay, as the Go module proxy keeps
// them for good, and their GitHub Releases are deleted. The releasable's
// lifecycle is not asked: a retraction releases nothing.
//
// Everything is read and refused before anything is written. The go.mod
// change and the records are committed before the first tag is pushed, so a
// run that stops is finished by running the same command again, which finds
// the retraction recorded and does only what is left.
func Retract(ctx *strictcli.Context, req RetractRequest) (err error) {
	e := ctx.Effects()
	if strings.TrimSpace(req.Reason) == "" || strings.Contains(req.Reason, "\n") {
		return fmt.Errorf("--reason is one non-empty line: it becomes the go.mod retract directive's comment, which the go command shows every consumer")
	}
	if len(req.Versions) == 0 {
		return fmt.Errorf("--versions names at least one released version to retract")
	}
	s, err := Select(e, req.Dir)
	if err != nil {
		return err
	}
	record, err := lifecycle.Load(s.Root())
	if err != nil {
		return err
	}
	now := time.Now()
	if req.Now != nil {
		now = req.Now()
	}
	if err := publishrules.RegistryWriteAllowed(record, s.Releasable.Name, now); err != nil {
		return fmt.Errorf("%w\n  Nothing was changed: a retraction publishes a Go module version, and no registry write is made for a proprietary releasable", err)
	}
	var versions []semver.Version
	for _, text := range req.Versions {
		v, err := ParseVersion(text, "--versions")
		if err != nil {
			return err
		}
		if slices.ContainsFunc(versions, func(w semver.Version) bool { return semver.Compare(v, w) == 0 }) {
			return fmt.Errorf("--versions names %s twice", v)
		}
		versions = append(versions, v)
	}
	semver.Sort(versions)
	l, err := lock(ctx, s.Root())
	if err != nil {
		return err
	}
	defer unlock(l, &err)
	r, err := planRetraction(ctx, s, versions, req.Reason)
	if err != nil {
		return err
	}
	if err := r.scan(req.TermList, now); err != nil {
		return err
	}
	if ctx.DryRun() {
		ctx.Out(r.describe("Would retract", req.Reason))
		return nil
	}
	if !r.resuming {
		if err := r.write(ctx, req.Reason); err != nil {
			return err
		}
	}
	if err := r.publish(ctx); err != nil {
		return err
	}
	ctx.Out(r.describe("Retracted", req.Reason))
	return nil
}

// planRetraction reads everything the retraction decides from and refuses
// what it cannot do.
func planRetraction(ctx *strictcli.Context, s Selection, versions []semver.Version, reason string) (*retraction, error) {
	r := &retraction{s: s, versions: versions}
	branch, err := s.Repo.CurrentBranch()
	if err != nil {
		return nil, err
	}
	if !slices.Contains(s.Workspace.Declarations.ReleaseBranches, branch) {
		return nil, fmt.Errorf("a retraction commits on the release branch, and %q is not one (release_branches in %s: %s). Nothing was changed: check out a release branch and run the retraction again", branch, declarations.ReleasablesFile, strings.Join(s.Workspace.Declarations.ReleaseBranches, ", "))
	}
	if _, inProgress, err := runstate.LoadInProgress(s.Root(), s.Releasable.Name); err != nil {
		return nil, err
	} else if inProgress {
		return nil, fmt.Errorf("a release of %s is in progress (%s), and a retraction takes the next version number, which that release may claim. Nothing was changed: finish it with `rlsbl release resume --watch` or record it abandoned with `rlsbl release abandon`, then retract", s.Releasable.Name, runstate.InProgressPath(s.Releasable.Name))
	}
	pkgs, err := s.packages()
	if err != nil {
		return nil, err
	}
	for _, p := range pkgs {
		if p.Target == declarations.TargetGo {
			r.goMods = append(r.goMods, declarations.Join(p.Dir, gomodule.FileName))
		}
	}
	if len(r.goMods) == 0 {
		return nil, fmt.Errorf("%s has no Go module: a retraction is a go.mod retract directive, which only a Go module carries. Nothing was changed; a version published elsewhere is withdrawn with `rlsbl release yank`", s.Releasable.Name)
	}
	var by []string
	for _, v := range versions {
		a, found, err := s.archive(v)
		if err != nil {
			return nil, err
		}
		if !found {
			return nil, fmt.Errorf("%s has no release archive %s, so the record holds no release of it to retract. Nothing was changed", v, releaserecord.ArchivePath(s.Record.Dir(), v))
		}
		if !a.Fate.Released() {
			return nil, fmt.Errorf("%s records %s as %s, and only a version that was released is retracted. Nothing was changed", a.Path, v, map[releaserecord.Fate]string{releaserecord.FateNeverReleased: "never released", releaserecord.FateUnstated: "stating no fate"}[a.Fate])
		}
		if len(a.Retracts) > 0 {
			return nil, fmt.Errorf("%s is a retraction version (%s), which retracts itself already. Nothing was changed", v, a.Path)
		}
		by = append(by, a.RetractedBy)
	}
	switch {
	case !slices.ContainsFunc(by, func(b string) bool { return b != "" }):
		if err := r.planFresh(); err != nil {
			return nil, err
		}
	case !slices.Contains(by, ""):
		if err := r.planResume(by); err != nil {
			return nil, err
		}
	default:
		var done []string
		for i, b := range by {
			if b != "" {
				done = append(done, fmt.Sprintf("%s (by %s)", versions[i], b))
			}
		}
		return nil, fmt.Errorf("some of the named versions are retracted already: %s. Nothing was changed: a version is retracted once, so name only the others", strings.Join(done, ", "))
	}
	gh, slug, err := openGitHub(ctx.Effects(), s)
	if err != nil {
		return nil, err
	}
	r.gh, r.slug = gh, slug
	for _, v := range versions {
		a, _, err := s.archive(v)
		if err != nil {
			return nil, err
		}
		tag := s.tagOf(a)
		exists, err := gh.ReleaseExists(slug, tag)
		if err != nil {
			return nil, err
		}
		if exists {
			r.releases = append(r.releases, tag)
		}
	}
	return r, nil
}

// planFresh decides the retraction version and refuses a retraction whose
// range would retract a version not named.
func (r *retraction) planFresh() error {
	s := r.s
	known, err := r.knownVersions()
	if err != nil {
		return err
	}
	highest, _ := semver.Highest(known)
	if r.next, err = highest.Bump(semver.Patch); err != nil {
		return err
	}
	low := r.versions[0]
	var covered []string
	for _, v := range known {
		if semver.Compare(v, low) < 0 || slices.ContainsFunc(r.versions, func(w semver.Version) bool { return semver.Compare(v, w) == 0 }) {
			continue
		}
		if fate, err := s.Record.Fate(v); err != nil {
			return err
		} else if fate == releaserecord.FateNeverReleased {
			continue
		}
		covered = append(covered, v.String())
	}
	if len(covered) > 0 {
		return fmt.Errorf("the retraction is one go.mod line, retract [v%s, v%s], and it would also retract %s, which --versions does not name. Nothing was changed: name every version from %s up, or retract the lower ones first, on their own", low, r.next, strings.Join(covered, ", "), low)
	}
	for _, goMod := range r.goMods {
		changed, err := s.Repo.ChangedPaths([]string{goMod}, git.UntrackedNormal)
		if err != nil {
			return err
		}
		if len(changed) > 0 {
			return fmt.Errorf("%s has uncommitted changes, and the retraction commits it on its own. Nothing was changed: commit or discard them, then run the retraction again", goMod)
		}
		data, err := os.ReadFile(s.abs(goMod))
		if err != nil {
			return fmt.Errorf("reading %s: %w", goMod, err)
		}
		if _, changed, err := gomodule.AddRetractionRange(goMod, data, "v"+low.String(), "v"+r.next.String(), ""); err != nil {
			return err
		} else if !changed {
			return fmt.Errorf("%s already retracts every version from v%s to v%s, yet the record holds none of them retracted, so the retraction version would carry no retraction of its own. Nothing was changed: remove that retract directive by hand and commit it, then run the retraction again", goMod, low, r.next)
		}
	}
	return nil
}

// knownVersions are every version the record or a tag of the releasable's
// scheme holds, here and on origin: the numbers the retraction version must
// not take. The Go module proxy is never asked about a version.
func (r *retraction) knownVersions() ([]semver.Version, error) {
	s := r.s
	known, err := s.Record.Versions()
	if err != nil {
		return nil, err
	}
	local, err := s.Repo.TagCommits()
	if err != nil {
		return nil, err
	}
	remote, err := s.Repo.RemoteTagCommits(origin)
	if err != nil {
		return nil, fmt.Errorf("origin's tags could not be read (%w), and the retraction version must be a number no tag there holds. Nothing was changed", err)
	}
	for _, tags := range []map[string]string{local, remote} {
		for tag := range tags {
			if v, ok := s.Scheme.VersionOf(tag); ok && !slices.ContainsFunc(known, func(w semver.Version) bool { return semver.Compare(v, w) == 0 }) {
				known = append(known, v)
			}
		}
	}
	return known, nil
}

// planResume reads the retraction a stopped run recorded: every named
// version retracted by one retraction version, which retracts exactly them.
func (r *retraction) planResume(by []string) error {
	s := r.s
	for _, b := range by {
		if b != by[0] {
			return fmt.Errorf("the named versions are retracted by different retraction versions (%s). Nothing was changed: a retraction that stopped is finished by naming the versions it retracted", strings.Join(by, ", "))
		}
	}
	next, err := semver.Parse(by[0])
	if err != nil {
		return err
	}
	a, found, err := s.archive(next)
	if err != nil {
		return err
	}
	if !found || a.Fate != releaserecord.FateRecorded {
		return fmt.Errorf("the named versions are retracted by %s, whose archive %s does not record its release commit. Nothing was changed", next, releaserecord.ArchivePath(s.Record.Dir(), next))
	}
	if len(a.Retracts) != len(r.versions) || !slices.EqualFunc(a.Retracts, r.versions, func(x, y semver.Version) bool { return semver.Compare(x, y) == 0 }) {
		return fmt.Errorf("%s retracts %s, not the versions named. Nothing was changed: name the versions it retracted", next, versionPhrase(a.Retracts))
	}
	r.next, r.commit, r.resuming = next, a.ReleaseCommit.Commit, true
	return nil
}

// refs are the tags of the retraction version.
func (r *retraction) refs() ([]string, error) {
	s := r.s
	var members []targets.RefMember
	for _, m := range s.Workspace.MembersOf(s.Releasable.Name) {
		ts, err := targets.MemberTargets(s.Root(), m)
		if err != nil {
			return nil, err
		}
		members = append(members, targets.RefMember{Path: m.Path, Targets: ts, Publishes: s.Releasable.PublishMode != declarations.PublishNone})
	}
	refs, err := targets.ExpectedRefsOf(r.next, targets.RefInputs{SchemeTag: s.Scheme.Render(r.next), Members: members})
	if err != nil {
		return nil, err
	}
	return refs.Tags(), nil
}

// scan refuses a retraction whose tag push would publish an unresolved
// confidential-term hit: the commits the tags carry that origin's release
// branch does not, the retraction's go.mod line and commit message, and the
// tag names.
func (r *retraction) scan(loc confidential.Location, now time.Time) error {
	s := r.s
	scanner, err := publishrules.LoadScanner(s.Repo, loc, now)
	if err != nil {
		return err
	}
	tip := r.commit
	if tip == "" {
		if tip, err = s.Repo.Head(); err != nil {
			return err
		}
	}
	branch, err := s.Repo.CurrentBranch()
	if err != nil {
		return err
	}
	var exclude []string
	remote, found, err := s.Repo.RemoteRef(origin, "refs/heads/"+branch)
	if err != nil {
		return err
	}
	if found {
		if _, have, err := s.Repo.ResolveCommit(remote); err != nil {
			return err
		} else if !have {
			return fmt.Errorf("origin's %s is at %s, which this repository does not have, so which commits the tag push carries cannot be scanned for confidential terms. Nothing was changed: fetch origin and run the retraction again", branch, remote)
		}
		exclude = append(exclude, remote)
	}
	hits, err := scanner.HitsInRange(s.Repo, []string{tip}, exclude)
	if err != nil {
		return err
	}
	tags, err := r.refs()
	if err != nil {
		return err
	}
	texts := []publishrules.Text{{Name: "the retraction commit's message", Content: r.message("")}}
	for _, tag := range tags {
		texts = append(texts, publishrules.Text{Name: "a tag name of " + r.next.String(), Content: tag})
	}
	hits = append(hits, scanner.HitsInTexts(texts)...)
	return scanner.Refuse(fmt.Sprintf("the retraction %s of %s", r.next, s.Releasable.Name), hits)
}

// retracted is every version the go.mod line retracts: the named ones and
// the retraction version.
func (r *retraction) retracted() []semver.Version {
	return append(append([]semver.Version(nil), r.versions...), r.next)
}

// message is the retraction commit's message, with reason as its body.
func (r *retraction) message(reason string) string {
	m := fmt.Sprintf("%s: retract %s", r.s.Scheme.Render(r.next), versionPhrase(r.retracted()))
	if reason != "" {
		m += "\n\n" + reason
	}
	return m
}

// write commits the go.mod retraction, then records it: each named version's
// archive names the retraction version, which is archived with its release
// commit, and both are committed.
func (r *retraction) write(ctx *strictcli.Context, reason string) error {
	s := r.s
	e := ctx.Effects()
	low, high := "v"+r.versions[0].String(), "v"+r.next.String()
	for _, goMod := range r.goMods {
		data, err := os.ReadFile(s.abs(goMod))
		if err != nil {
			return fmt.Errorf("reading %s: %w", goMod, err)
		}
		out, _, err := gomodule.AddRetractionRange(goMod, data, low, high, reason)
		if err != nil {
			return err
		}
		if err := writeGoMod(e, s.abs(goMod), goMod, out); err != nil {
			return err
		}
	}
	if err := commit(ctx, s, r.message(reason), r.goMods, true); err != nil {
		return fmt.Errorf("the retraction was written into %s, and committing it failed: %w. Commit those files, then remove the retraction commit's change by hand if the retraction is to be given up", strings.Join(r.goMods, ", "), err)
	}
	sha, err := s.Repo.Head()
	if err != nil {
		return err
	}
	r.commit = sha
	trees := map[string]string{}
	for _, p := range r.releasedPaths() {
		tree, found, err := s.Repo.TreeAt(sha, p)
		if err != nil {
			return err
		}
		if !found {
			return fmt.Errorf("the retraction commit %s holds no %s, so its released tree cannot be recorded", sha, p)
		}
		trees[p] = tree
	}
	dir := s.Record.Dir()
	paths := []string{releaserecord.ArchivePath(dir, r.next)}
	for _, v := range r.versions {
		if _, err := releaserecord.MarkRetracted(e, s.Root(), dir, v, r.next); err != nil {
			return fmt.Errorf("%w\n  The retraction commit %s is made and nothing is pushed: run `%s` again once this is fixed", err, sha, RetractInvocation)
		}
		paths = append(paths, releaserecord.ArchivePath(dir, v))
	}
	include, exclude := r.targets()
	spec := releaserecord.ArchiveSpec{
		ReleaseFile: releaserecord.ReleaseFile{
			Bump:        semver.Patch,
			Include:     include,
			Exclude:     exclude,
			Description: fmt.Sprintf("Retracts %s: %s", versionPhrase(r.retracted()), reason),
		},
		Fate:          releaserecord.FateRecorded,
		ReleaseCommit: releaserecord.ReleaseCommit{Commit: sha, Trees: trees},
		Header:        retractionHeader,
		RetractedBy:   r.next,
		Retracts:      r.versions,
	}
	if _, err := releaserecord.WriteArchive(e, s.Root(), dir, r.next, spec); err != nil {
		return err
	}
	if err := commit(ctx, s, fmt.Sprintf("Record the retraction of %s", versionPhrase(r.retracted())), paths, true); err != nil {
		return fmt.Errorf("the retraction's records were written and committing them failed: %w. Commit %s, then run `%s` again", err, strings.Join(paths, ", "), RetractInvocation)
	}
	return nil
}

var retractionHeader = []string{
	"Archived by `rlsbl release retract`. This version publishes only a go.mod",
	"retraction: it is tagged and has no GitHub Release, and it retracts itself.",
}

// releasedPaths are the paths whose trees the retraction version records, as
// a release records them.
func (r *retraction) releasedPaths() []string {
	if !r.s.Workspace.IsWorkspace() {
		return []string{declarations.RootPath}
	}
	var out []string
	for _, m := range r.s.Workspace.MembersOf(r.s.Releasable.Name) {
		out = append(out, m.Path)
	}
	slices.Sort(out)
	return out
}

// targets are the retraction version's include (go, the one target a
// retraction publishes) and exclude (every other target of the releasable's
// members).
func (r *retraction) targets() ([]string, []string) {
	include := []string{declarations.TargetGo}
	exclude := []string{}
	pkgs, err := r.s.packages()
	if err != nil {
		return include, exclude
	}
	for _, p := range pkgs {
		if p.Target != declarations.TargetGo && !slices.Contains(exclude, p.Target) {
			exclude = append(exclude, p.Target)
		}
	}
	return include, exclude
}

// publish creates the retraction version's tags at the retraction commit,
// pushes each one on its own (and nothing else), and deletes the retracted
// versions' GitHub Releases. Each step finds what a stopped run already did.
func (r *retraction) publish(ctx *strictcli.Context) error {
	s := r.s
	tags, err := r.refs()
	if err != nil {
		return err
	}
	for _, tag := range tags {
		at, found, err := s.Repo.TagCommit(tag)
		if err != nil {
			return err
		}
		if found && at != r.commit {
			return fmt.Errorf("the tag %s exists already, at %s, and the retraction tags %s; a tag is never moved, so find out where it came from before running the retraction again", tag, at, r.commit)
		}
		if !found {
			if err := s.Repo.CreateRef("refs/tags/"+tag, r.commit); err != nil {
				return err
			}
		}
		remote, onOrigin, err := s.Repo.RemoteTagCommit(origin, tag)
		if err != nil {
			return fmt.Errorf("refusing to push %s blind: %w", tag, err)
		}
		if onOrigin {
			if remote != r.commit {
				return fmt.Errorf("origin holds the tag %s at %s, and the retraction tags %s; a tag is never moved", tag, remote, r.commit)
			}
			continue
		}
		if err := s.Repo.Push(origin, git.RefUpdate{Ref: "refs/tags/" + tag, New: r.commit}, s.pushTimeout()); err != nil {
			return fmt.Errorf("pushing %s failed: %w\n  The retraction is committed and recorded; run `%s` again to push what is left", tag, err, RetractInvocation)
		}
		ctx.Info(fmt.Sprintf("Pushed %s -> %s (the retraction commit)", tag, r.commit))
	}
	for _, tag := range r.releases {
		if err := r.gh.DeleteRelease(r.slug, tag); err != nil {
			return fmt.Errorf("deleting the GitHub Release of %s failed: %w\n  The retraction is pushed; run `%s` again to delete the Releases left", tag, err, RetractInvocation)
		}
		ctx.Info(fmt.Sprintf("Deleted the GitHub Release of %s (its tag stays)", tag))
	}
	return nil
}

// describe is the closing report.
func (r *retraction) describe(verb, reason string) string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s %s of %s through %s (%q).\n", verb, versionPhrase(r.versions), r.s.Releasable.Name, r.next, reason)
	fmt.Fprintf(&b, "  go.mod: retract [v%s, v%s] in %s\n", r.versions[0], r.next, strings.Join(r.goMods, ", "))
	if tags, err := r.refs(); err == nil {
		fmt.Fprintf(&b, "  tags pushed, and nothing else: %s\n", strings.Join(tags, ", "))
	}
	if len(r.releases) > 0 {
		fmt.Fprintf(&b, "  GitHub Releases deleted: %s (their tags stay)\n", strings.Join(r.releases, ", "))
	} else {
		b.WriteString("  GitHub Releases deleted: none (none of the retracted versions has one)\n")
	}
	fmt.Fprintf(&b, "  The record holds %s as retracted: none counts as a release.", versionPhrase(r.retracted()))
	return b.String()
}

// versionPhrase lists versions as prose: "v0.5.0", "v0.5.0 and v0.5.1",
// "v0.4.0, v0.5.0, and v0.5.1".
func versionPhrase(vs []semver.Version) string {
	names := make([]string, len(vs))
	for i, v := range vs {
		names[i] = "v" + v.String()
	}
	switch len(names) {
	case 0:
		return "nothing"
	case 1:
		return names[0]
	case 2:
		return names[0] + " and " + names[1]
	}
	return strings.Join(names[:len(names)-1], ", ") + ", and " + names[len(names)-1]
}

// writeGoMod replaces a go.mod with out through a file beside it, so a
// reader never sees half of it.
func writeGoMod(e *strictcli.Effects, path, rel string, out []byte) error {
	tmp := path + ".rlsbl-writing"
	if _, err := e.Write(tmp, string(out)); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	if _, err := e.Rename(tmp, path); err != nil {
		return fmt.Errorf("writing %s: %w", rel, err)
	}
	return nil
}
