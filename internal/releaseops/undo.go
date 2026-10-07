package releaseops

import (
	"errors"
	"fmt"
	"os"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
)

// UndoRequest is a `release undo`.
type UndoRequest struct {
	// Dir is the working directory, absolute.
	Dir string
	// Version names an earlier release to undo, and is empty for the
	// latest release.
	Version string
	// Target is the --target flag, empty when not passed. Undo reads the
	// version from the release archives and the evidence from every target
	// of the releasable, so a target selects nothing and is refused.
	Target string
	// Now is the clock the audit line is stamped with.
	Now func() time.Time
}

// undoPlan is everything an undo does, decided before anything is written.
type undoPlan struct {
	s       Selection
	version semver.Version
	latest  bool
	// tag is the version's tag as the releasable's tag format spells it,
	// the Release's tag.
	tag string
	// tags are every ref of the version the undo deletes, tag first, then
	// the companion and alias tags; the spelling an archive records the
	// version shipped under is never among them.
	tags []string
	// localTags and remoteTags map each of tags present here and on origin
	// to the object it holds.
	localTags  map[string]string
	remoteTags map[string]string
	remote     bool
	release    bool
	gate       GateResult
	revert     revertPlan
	// restoreChangelog and restoreReleaseFile repair what the release's
	// finalization recorded, when no reverted commit already undid it.
	restoreChangelog   bool
	restoreReleaseFile bool
	branch             string
	// remoteBranch is the object origin's branch holds, empty when origin
	// has no such branch.
	remoteBranch string
	audits       string
	notes        []string
}

// Undo reverts a release. Without a version it reverts the releasable's
// latest release (the highest version its archives record as released):
// it deletes the GitHub Release and the version's tags here and on origin,
// reverts the release's version-bump commit (and, in histories that made
// them below the release commit, its finalization commits), restores the
// version's changelog entries to the unreleased file and its archive as the
// release file, regenerates the changelog, commits that as one commit, and
// pushes the branch. With a version it undoes an earlier release, deleting
// its Release and tags and restoring its changelog and release file
// without reverting commits, since history moved on.
//
// Either way the version must be provably unpublished: the evidence (the
// registries' package listings, origin's Go module tags, and the runs of
// the publish workflows its tag started) must contain a source that
// observed the version's absence and none that saw it published, or the
// undo is refused, naming `release yank` and `release deprecate`. Every
// refusal comes before anything is written, a --dry-run prints the plan and
// writes nothing, and an audit line recording the plan and its evidence is
// written and committed before the first deletion.
func Undo(ctx *strictcli.Context, req UndoRequest) (err error) {
	if req.Target != "" {
		return fmt.Errorf("--target selects nothing in `release undo`: the version comes from the release archives, and the evidence is read from every target of the releasable, so one target cannot narrow it. Run the undo without --target")
	}
	if req.Now == nil {
		return errors.New("an undo needs a clock to stamp its audit line with")
	}
	e := ctx.Effects()
	s, err := Select(e, req.Dir)
	if err != nil {
		return err
	}
	gh, slug, err := openGitHub(e, s)
	if err != nil {
		return err
	}
	l, err := lock(ctx, s.Root())
	if err != nil {
		return err
	}
	defer unlock(l, &err)
	p, err := planUndo(s, gh, slug, req)
	if err != nil {
		return err
	}
	if p.gate.Verdict != Cleared {
		return gateRefusal(p)
	}
	for _, line := range p.describe() {
		ctx.Info(line)
	}
	if ctx.DryRun() {
		ctx.Out(fmt.Sprintf("Dry run: the undo of %s is cleared and planned above; nothing was changed.", p.tag))
		return nil
	}
	return p.execute(ctx, gh, slug, req.Now())
}

// gateRefusal is the refusal of an undo the evidence did not clear.
func gateRefusal(p undoPlan) error {
	lines := []string{fmt.Sprintf("cannot undo %s: %s.", p.tag, p.gate.Reason)}
	for _, e := range p.gate.Evidence {
		lines = append(lines, "  "+e.String())
	}
	v := p.version
	if slices.ContainsFunc(p.gate.Evidence, func(e Evidence) bool { return e.Finding == Published }) {
		lines = append(lines, "  The release is published, and deleting it would strand everyone who installed it. Nothing was changed. Remove it the way the registries allow instead:",
			fmt.Sprintf("    rlsbl release yank %s --approve-consequential       # npm deprecate, a Go retraction, PyPI's yank", v),
			fmt.Sprintf("    rlsbl release deprecate %s --approve-consequential  # a notice on the GitHub Release", v))
	} else {
		lines = append(lines, fmt.Sprintf("  Nothing was changed. An undo needs a source that observed the version's absence; once the sources answer (or a running publish run ends), run the undo again. If %s was published, `rlsbl release yank %s` and `rlsbl release deprecate %s` remove it the way the registries allow", v, v, v))
	}
	return errors.New(strings.Join(lines, "\n"))
}

// planUndo reads and decides everything; it writes nothing (the merges of
// the revert store scratch objects only).
func planUndo(s Selection, gh github.Client, slug github.Repository, req UndoRequest) (undoPlan, error) {
	p := undoPlan{s: s}
	branch, attached, err := s.Repo.HeadBranch()
	if err != nil {
		return undoPlan{}, err
	}
	if !attached || !slices.Contains(s.Workspace.Declarations.ReleaseBranches, branch) {
		return undoPlan{}, fmt.Errorf("HEAD is not on a release branch (%s), and an undo commits and pushes the release branch. Nothing was changed. Check out the release branch and run the undo again", strings.Join(s.Workspace.Declarations.ReleaseBranches, ", "))
	}
	p.branch = branch
	if err := refuseUnrecordedInProgress(s); err != nil {
		return undoPlan{}, err
	}
	var a releaserecord.Archive
	if req.Version == "" {
		if a, err = latestArchive(s); err != nil {
			return undoPlan{}, err
		}
		p.latest = true
	} else {
		v, err := ParseVersion(req.Version, "--version")
		if err != nil {
			return undoPlan{}, err
		}
		if a, err = s.releasedArchive(v); err != nil {
			return undoPlan{}, err
		}
		latest, _, err := s.latestReleased()
		if err != nil {
			return undoPlan{}, err
		}
		if semver.Compare(latest, v) == 0 {
			return undoPlan{}, fmt.Errorf("--version names %s, the latest release of %s, and --version undoes an earlier release, without reverting its commits. Nothing was changed. Run the undo without --version: it reverts the latest release's commits too", v, s.Releasable.Name)
		}
	}
	p.version = a.Version
	p.tag = s.Scheme.Render(a.Version)
	releaseCommit := ""
	if a.Fate == releaserecord.FateRecorded {
		sha, found, err := s.Repo.ResolveCommit(a.ReleaseCommit.Commit + "^{commit}")
		if err != nil {
			return undoPlan{}, err
		}
		if found {
			releaseCommit = sha
		} else if p.latest {
			return undoPlan{}, fmt.Errorf("the commit the archive %s records %s at, %s, is not in this repository. Undo refused: nothing was destroyed. Fetch the missing history (`git fetch --unshallow`) and run the undo again", a.Path, a.Version, a.ReleaseCommit.Commit)
		}
	}
	if p.latest {
		if a.Fate != releaserecord.FateRecorded {
			return undoPlan{}, fmt.Errorf("%s records %s unrecoverable: the commit it shipped from is unknown, so the commits it made cannot be found, and deleting its tag and Release while leaving the version files bumped is the half-undone state undo exists to prevent. Nothing was changed", a.Path, a.Version)
		}
		if p.revert, err = planReleaseRevert(s, a, releaseCommit); err != nil {
			return undoPlan{}, err
		}
		if at, found, err := s.Repo.TagCommit(p.tag); err != nil {
			return undoPlan{}, err
		} else if found && at != releaseCommit {
			p.notes = append(p.notes, fmt.Sprintf("the tag %s points at %s while the archive records the release at %s; the archive decides what is reverted, and the tag is deleted either way", p.tag, at, releaseCommit))
		}
	}
	if p.tags, err = undoneTags(s, a); err != nil {
		return undoPlan{}, err
	}
	if err := p.readRefs(); err != nil {
		return undoPlan{}, err
	}
	if p.release, err = gh.ReleaseExists(slug, p.tag); err != nil {
		return undoPlan{}, err
	}
	if p.gate, err = undoEvidence(s, gh, slug, p.version, p.tag, releaseCommit, p.remote); err != nil {
		return undoPlan{}, err
	}
	if err := p.planRestore(); err != nil {
		return undoPlan{}, err
	}
	if p.audits, err = readAudits(s); err != nil {
		return undoPlan{}, err
	}
	return p, nil
}

// refuseUnrecordedInProgress refuses an undo while a release the record
// does not contain is in progress. Undo chooses its version from the
// archives alone, so a release that stopped before its archive step is
// invisible to it, and the version it would choose is the release before
// that one. A state file naming a version the archives record as released
// is a leftover of a release that got that far, and changes nothing.
func refuseUnrecordedInProgress(s Selection) error {
	statePath := runstate.InProgressPath(s.Releasable.Name)
	state, found, err := runstate.LoadInProgress(s.Root(), s.Releasable.Name)
	if err != nil {
		return fmt.Errorf("the in-progress release state could not be read: %w\n  Undo refused: nothing was changed. While a release may be in progress and unreadable, undo cannot tell whether the version it would choose is the one you mean", err)
	}
	if !found {
		return nil
	}
	v, err := semver.Parse(state.Version)
	if err != nil {
		return fmt.Errorf("the in-progress release state %s names the version %q: %w\n  Undo refused: nothing was changed. Undo cannot tell whether the version it would choose is the release in progress", statePath, state.Version, err)
	}
	fate, err := s.Record.Fate(v)
	if err != nil {
		return err
	}
	if fate.Released() {
		return nil
	}
	instead := ""
	if latest, ok, err := s.latestReleased(); err == nil && ok {
		instead = fmt.Sprintf("\n  Undoing now would have reverted %s instead, the release before this one, deleting its tag and its GitHub Release.", latest)
	}
	archive := releaserecord.ArchivePath(s.Record.Dir(), v)
	if fate == releaserecord.FateNeverReleased {
		return fmt.Errorf("a release of %s is in progress, and the record of %s holds %s as never released.%s\n  Nothing was changed.\n  %s", v, s.Releasable.Name, v, instead, LeftoverStateRemedy(v, archive, statePath))
	}
	return fmt.Errorf("a release of %s is in progress, and the record of %s does not contain it: undo reverts a recorded release, and there is no %s among the archives in %s, because the release stopped before the step that writes it (it completed %d steps).%s\n  Nothing was changed.\n  To finish %s, run `rlsbl release resume`; once %s is recorded, undo reverts it.\n  %s\n    %s", v, s.Releasable.Name, releaserecord.ArchiveName(v), s.Record.Dir(), len(state.CompletedSteps), instead, v, v, AbandonRemedy(v), statePath)
}

// latestArchive is the archive of the releasable's latest release: the
// highest archived version that records a release, passing over the
// versions recorded never released. An archive stating no fate refuses,
// since which release is the latest cannot be told past it.
func latestArchive(s Selection) (releaserecord.Archive, error) {
	versions, err := s.Record.Versions()
	if err != nil {
		return releaserecord.Archive{}, err
	}
	for _, v := range versions {
		a, err := releaserecord.ReadArchive(s.Root(), s.Record.Dir(), v)
		if err != nil {
			return releaserecord.Archive{}, fmt.Errorf("%w\n  Undo refused: nothing was destroyed. Which version is the latest release cannot be decided while an archive above it is unreadable", err)
		}
		switch a.Fate {
		case releaserecord.FateNeverReleased:
			continue
		case releaserecord.FateUnstated:
			return releaserecord.Archive{}, fmt.Errorf("%s states no fate, so whether %s is the latest release cannot be told. Undo refused: nothing was destroyed. Backfill it, previewing first: `rlsbl release backfill --dry-run`, then `rlsbl release backfill --approve-consequential`", a.Path, v)
		}
		return a, nil
	}
	return releaserecord.Archive{}, fmt.Errorf("the release archives of %s in %s record no release, so there is nothing to undo", s.Releasable.Name, s.Record.Dir())
}

// predecessorCommit is the release commit of the highest release below v
// that records one, and empty when there is none: the boundary below which
// no commit is v's.
func predecessorCommit(s Selection, v semver.Version) (string, error) {
	versions, err := s.Record.Versions()
	if err != nil {
		return "", err
	}
	for _, below := range versions {
		if semver.Compare(below, v) >= 0 {
			continue
		}
		a, err := releaserecord.ReadArchive(s.Root(), s.Record.Dir(), below)
		if err != nil {
			return "", err
		}
		if a.Fate != releaserecord.FateRecorded {
			continue
		}
		sha, found, err := s.Repo.ResolveCommit(a.ReleaseCommit.Commit + "^{commit}")
		if err != nil {
			return "", err
		}
		if !found {
			return "", fmt.Errorf("the commit %s records %s at, %s, is not in this repository, so where %s's commits begin cannot be told. Undo refused: nothing was destroyed. Fetch the missing history (`git fetch --unshallow`) and run the undo again", a.Path, below, a.ReleaseCommit.Commit, v)
		}
		return sha, nil
	}
	return "", nil
}

// planReleaseRevert finds the commits the latest release made at or below
// its release commit (its version-bump commit, and the finalization commits
// histories made there) and computes their revert. The version-bump commit
// is the one commit between the previous release's commit and this one
// carrying a version-bump subject; none, or more than one, refuses. Commits
// somebody else made in between (a fix that made CI pass) are kept.
func planReleaseRevert(s Selection, a releaserecord.Archive, releaseCommit string) (revertPlan, error) {
	v := a.Version
	tag := s.Scheme.Render(v)
	predecessor, err := predecessorCommit(s, v)
	if err != nil {
		return revertPlan{}, err
	}
	var exclude []string
	rangeText := releaseCommit
	if predecessor != "" {
		exclude = []string{predecessor}
		rangeText = predecessor + ".." + releaseCommit
	}
	commits, err := s.Repo.CommitSubjects([]string{releaseCommit}, exclude)
	if err != nil {
		return revertPlan{}, err
	}
	bumpSubjects := releaserecord.VersionBumpSubjects(s.Releasable.Name, tag, v)
	var bumps []int
	for i, c := range commits {
		if slices.Contains(bumpSubjects, c.Subject) {
			bumps = append(bumps, i)
		}
	}
	switch {
	case len(bumps) == 0:
		return revertPlan{}, fmt.Errorf("the version-bump commit of %s could not be found: none of the %d commits in %s carries the subject %q or %q. Undo refused: nothing was destroyed; deleting the tag and the GitHub Release while leaving the version files bumped is the half-undone state undo exists to prevent. Inspect the range (`git log --oneline %s`); if the release's commits were rewritten, repair the release record and the tags first (`rlsbl release reconcile --mode plan`, then `--mode apply`)", tag, len(commits), rangeText, bumpSubjects[0], bumpSubjects[1], rangeText)
	case len(bumps) > 1:
		var shas []string
		for _, i := range bumps {
			shas = append(shas, commits[i].SHA[:12])
		}
		return revertPlan{}, fmt.Errorf("%d commits in %s carry a version-bump subject of %s: %s. Undo refused: nothing was destroyed; rlsbl will not guess which one shipped the release", len(bumps), rangeText, tag, strings.Join(shas, ", "))
	}
	var reverted []git.CommitSubject
	for _, c := range commits[:bumps[0]+1] {
		if slices.Contains(bumpSubjects, c.Subject) || releaserecord.IsFinalizationSubject(c.Subject, v) {
			reverted = append(reverted, c)
		}
	}
	return planRevert(s.Repo, reverted)
}

// undoneTags are the refs of version a's release the undo deletes: the
// tag its tag format spells, the companion tags its members' modules owe,
// and the alias tags the transition record created for it. The spelling
// the archive records the version shipped under is kept: the release being
// undone did not create it, and consumers resolve it.
func undoneTags(s Selection, a releaserecord.Archive) ([]string, error) {
	var members []targets.RefMember
	for _, m := range s.Workspace.MembersOf(s.Releasable.Name) {
		ts, err := targets.MemberTargets(s.Root(), m)
		if err != nil {
			return nil, err
		}
		members = append(members, targets.RefMember{Path: m.Path, Targets: ts, Publishes: s.Releasable.PublishMode != declarations.PublishNone})
	}
	var recorded []string
	for p := range a.ReleaseCommit.Trees {
		recorded = append(recorded, p)
	}
	sort.Strings(recorded)
	events, err := releaserecord.ReadEvents(s.Root())
	if err != nil {
		return nil, err
	}
	primary := s.tagOf(a)
	var aliases []string
	for _, ev := range events {
		alias, ok := ev.(*releaserecord.BoundaryAliasEvent)
		if !ok || (alias.Releasable != "" && alias.Releasable != s.Releasable.Name) {
			continue
		}
		for _, b := range alias.Aliases {
			if b.AliasedTag == primary {
				aliases = append(aliases, b.AliasTag)
			}
		}
	}
	refs, err := targets.ExpectedRefsOf(a.Version, targets.RefInputs{
		SchemeTag:     s.Scheme.Render(a.Version),
		Members:       members,
		RecordedPaths: recorded,
		ShippedAs:     a.ShippedAs,
		Aliases:       aliases,
	})
	if err != nil {
		return nil, err
	}
	out := []string{s.Scheme.Render(a.Version)}
	for _, tag := range refs.Spellings() {
		if tag != a.ShippedAs && !slices.Contains(out, tag) {
			out = append(out, tag)
		}
	}
	return out, nil
}

// readRefs reads which of the plan's tags exist here and on origin, and
// origin's release branch, which must not hold commits this checkout lacks:
// the push of the undo carries the branch as it stands here.
func (p *undoPlan) readRefs() error {
	s := p.s
	local, err := s.Repo.LocalRefs("refs/tags/")
	if err != nil {
		return err
	}
	p.localTags, p.remoteTags = map[string]string{}, map[string]string{}
	for _, tag := range p.tags {
		if object, ok := local["refs/tags/"+tag]; ok {
			p.localTags[tag] = object
		}
	}
	if p.remote, err = s.Repo.RemoteConfigured(origin); err != nil || !p.remote {
		return err
	}
	var patterns []string
	for _, tag := range p.tags {
		patterns = append(patterns, "refs/tags/"+tag)
	}
	remote, err := s.Repo.RemoteRefs(origin, patterns...)
	if err != nil {
		return err
	}
	for _, tag := range p.tags {
		if object, ok := remote["refs/tags/"+tag]; ok {
			p.remoteTags[tag] = object
		}
	}
	object, found, err := s.Repo.RemoteRef(origin, "refs/heads/"+p.branch)
	if err != nil || !found {
		return err
	}
	head, err := s.Repo.Head()
	if err != nil {
		return err
	}
	verdict, err := s.Repo.Ancestry(object, head)
	if err != nil {
		return err
	}
	if verdict != git.IsAncestor {
		return fmt.Errorf("origin's %s (%s) holds commits this checkout does not, and the undo pushes %s as it stands here. Nothing was changed. Bring the branch up to date (`git pull --ff-only`) and run the undo again", p.branch, object, p.branch)
	}
	p.remoteBranch = object
	return nil
}

// undoEvidence gathers the evidence on version v and judges it.
func undoEvidence(s Selection, gh github.Client, slug github.Repository, v semver.Version, tag, releaseCommit string, remote bool) (GateResult, error) {
	pkgs, err := s.packages()
	if err != nil {
		return GateResult{}, err
	}
	reg, err := registry.New(registry.Reads(s.effects()))
	if err != nil {
		return GateResult{}, err
	}
	p := prober{reg: reg, repo: s.Repo, remote: remote}
	var evidence []Evidence
	for _, pkg := range pkgs {
		evidence = append(evidence, p.packageEvidence(pkg, v)...)
	}
	if releaseCommit != "" {
		evidence = append(evidence, publishRunEvidence(gh, slug, s.Repo, tag, releaseCommit)...)
	}
	return judge(evidence), nil
}

// planRestore decides the repairs of the finalization, and refuses when a
// path the undo writes has uncommitted changes or the release file holds
// someone's fields.
func (p *undoPlan) planRestore() error {
	s := p.s
	name := s.Releasable.Name
	reverted := p.revert.files
	released := changelog.Dir(name) + "/" + changelog.VersionName(p.version)
	if _, err := os.Lstat(s.abs(released)); err == nil {
		_, touched := reverted[released]
		p.restoreChangelog = !touched
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	archive := releaserecord.ArchivePath(s.Record.Dir(), p.version)
	_, touched := reverted[archive]
	p.restoreReleaseFile = !touched
	written := append(p.revert.paths(), changelog.Dir(name), declarations.ReleasesDir(name), changelog.Home(s.Workspace.Declarations, name))
	if s.Workspace.IsWorkspace() {
		written = append(written, changelog.RollUpPath)
	}
	dirty, err := s.Repo.ChangedPaths(written, git.UntrackedNormal)
	if err != nil {
		return err
	}
	if len(dirty) > 0 {
		return fmt.Errorf("paths the undo writes have uncommitted changes:\n  %s\n  Undo refused: nothing was changed. Commit or discard them, then run the undo again", strings.Join(dirty, "\n  "))
	}
	if !p.restoreReleaseFile {
		return nil
	}
	release := releaserecord.ReleaseFilePath(name)
	data, err := os.ReadFile(s.abs(release))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if !releaserecord.IsPristineReleaseFile(data) {
		return fmt.Errorf("%s holds the next release's fields, and the undo restores %s's archive there. Undo refused: nothing was changed. Empty the file's bump and description (keep its content elsewhere), commit, and run the undo again", release, p.version)
	}
	return nil
}

// describe is the plan, one line each.
func (p undoPlan) describe() []string {
	which := "an earlier release: its commits are not reverted, since history moved on"
	if p.latest {
		which = "the latest release"
	}
	lines := []string{fmt.Sprintf("Undo of %s (%s):", p.tag, which)}
	for _, n := range p.notes {
		lines = append(lines, "  note: "+n)
	}
	lines = append(lines, fmt.Sprintf("  - append the plan and its evidence to %s and commit it", AuditPath(p.s.Releasable.Name)))
	if p.release {
		lines = append(lines, "  - delete the GitHub Release of "+p.tag)
	} else {
		lines = append(lines, "  - the GitHub Release of "+p.tag+": none exists")
	}
	for _, tag := range p.tags {
		var where []string
		if _, ok := p.remoteTags[tag]; ok {
			where = append(where, "origin")
		}
		if _, ok := p.localTags[tag]; ok {
			where = append(where, "here")
		}
		if len(where) == 0 {
			lines = append(lines, "  - the tag "+tag+": exists nowhere")
			continue
		}
		lines = append(lines, "  - delete the tag "+tag+" ("+strings.Join(where, " and ")+")")
	}
	for _, c := range p.revert.commits {
		lines = append(lines, fmt.Sprintf("  - revert %s %s", c.SHA[:12], c.Subject))
	}
	if p.restoreChangelog {
		lines = append(lines, fmt.Sprintf("  - restore %s's changelog entries to the unreleased file", p.version))
	}
	if p.restoreReleaseFile {
		lines = append(lines, fmt.Sprintf("  - restore %s's archive as the release file", p.version))
	}
	lines = append(lines, "  - regenerate the changelog and commit the undo as one commit")
	if p.remote {
		lines = append(lines, "  - push "+p.branch+" to origin")
	}
	lines = append(lines, "  - clear the in-progress release state, if any", "  Evidence ("+string(p.gate.Verdict)+": "+p.gate.Reason+"):")
	for _, e := range p.gate.Evidence {
		lines = append(lines, "    "+e.String())
	}
	return lines
}

// execute performs the plan. A failure stops it, naming what was done.
func (p undoPlan) execute(ctx *strictcli.Context, gh github.Client, slug github.Repository, now time.Time) error {
	e := ctx.Effects()
	s := p.s
	var done []string
	fail := func(err error) error {
		return fmt.Errorf("%w\n  Done before it: %s. The audit line in %s records the whole plan", err, joinOrNone(done), AuditPath(s.Releasable.Name))
	}
	record := auditRecord{
		FormatVersion:   auditFormatVersion,
		RecordedAt:      now.UTC().Format(time.RFC3339),
		Version:         p.version.String(),
		Tag:             p.tag,
		Latest:          p.latest,
		Verdict:         p.gate.Verdict,
		Reason:          p.gate.Reason,
		Evidence:        append([]Evidence{}, p.gate.Evidence...),
		RevertedCommits: []string{},
		DeletedTags:     []string{},
		ReleaseDeleted:  p.release,
	}
	for _, c := range p.revert.commits {
		record.RevertedCommits = append(record.RevertedCommits, c.SHA)
	}
	for _, tag := range p.tags {
		_, here := p.localTags[tag]
		_, there := p.remoteTags[tag]
		if here || there {
			record.DeletedTags = append(record.DeletedTags, tag)
		}
	}
	audit := AuditPath(s.Releasable.Name)
	if err := appendAudit(e, s, p.audits, record); err != nil {
		return fmt.Errorf("%w\n  Undo refused: nothing was destroyed", err)
	}
	if err := commit(ctx, s, "Record the undo of "+p.tag, []string{audit}, true); err != nil {
		return fmt.Errorf("the audit line was written into %s, and committing it failed: %w\n  Undo refused: nothing was destroyed. Commit that file and run the undo again", audit, err)
	}
	done = append(done, "the audit line committed")
	if p.release {
		if err := gh.DeleteRelease(slug, p.tag); err != nil {
			return fail(err)
		}
		done = append(done, "the GitHub Release deleted")
	}
	for _, tag := range p.tags {
		if object, ok := p.remoteTags[tag]; ok {
			if err := s.Repo.Push(origin, git.RefUpdate{Ref: "refs/tags/" + tag, Expected: object}, s.pushTimeout()); err != nil {
				return fail(err)
			}
			done = append(done, tag+" deleted from origin")
		}
		if object, ok := p.localTags[tag]; ok {
			if err := s.Repo.DeleteRef("refs/tags/"+tag, object); err != nil {
				return fail(err)
			}
			done = append(done, tag+" deleted here")
		}
	}
	if err := p.revert.write(e, s); err != nil {
		return fail(err)
	}
	paths := p.revert.paths()
	name := s.Releasable.Name
	if p.restoreChangelog {
		changed, err := changelog.Unfinalize(e, s.Root(), changelog.Dir(name), p.version)
		if err != nil {
			return fail(err)
		}
		paths = append(paths, changed...)
	}
	if p.restoreReleaseFile {
		changed, err := releaserecord.Unfinalize(e, s.Root(), name, p.version)
		if err != nil {
			return fail(err)
		}
		paths = append(paths, changed...)
	}
	regenerated, err := changelog.Regenerate(e, s.Root(), s.Workspace.Declarations, name, nil)
	if err != nil {
		return fail(err)
	}
	paths = append(paths, regenerated...)
	if len(paths) > 0 {
		if err := commit(ctx, s, "Undo the release "+p.tag, dedupe(paths), true); err != nil {
			return fail(err)
		}
		done = append(done, "the undo committed")
	}
	if p.remote {
		head, err := s.Repo.Head()
		if err != nil {
			return fail(err)
		}
		if err := s.Repo.Push(origin, git.RefUpdate{Ref: "refs/heads/" + p.branch, New: head, Expected: p.remoteBranch}, s.pushTimeout()); err != nil {
			return fmt.Errorf("%w\n  Done before it: %s. The undo is committed here and not on origin; the next release's push carries it. Do not run the undo again: it would undo the release before this one", err, joinOrNone(done))
		}
		done = append(done, p.branch+" pushed to origin")
	}
	if err := runstate.ClearInProgress(e, s.Root(), name); err != nil {
		return fail(err)
	}
	ctx.Out(fmt.Sprintf("Undid %s: %s.", p.tag, strings.Join(done, "; ")))
	return nil
}

// dedupe is paths with each path once, in first-seen order.
func dedupe(paths []string) []string {
	var out []string
	for _, p := range paths {
		if !slices.Contains(out, p) {
			out = append(out, p)
		}
	}
	return out
}
