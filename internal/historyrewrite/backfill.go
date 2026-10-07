package historyrewrite

import (
	"fmt"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The backfill brings every releasable's archives into the fate model from
// the repository's own history, judging each subject once:
//
//   - an unexplained tag: a tag nothing accounts for, listed first, and one
//     of them refuses the whole apply;
//   - adopt: a tag one of whose releasable's versions would own it, for a
//     version no archive and no changelog file records; it gets an archive
//     recording the release it is evidence of;
//   - materialize: a version the changelog records as released, with no
//     archive;
//   - repair: an archive missing a required field, its fate, or both;
//   - settled: nothing to do.
//
// It derives two of the three fates, recorded and unrecoverable, never the
// third: a version never released has no tag and no version-bump commit, as
// does a released one whose commit is gone, and only an operator knows which
// it is. never_released is declared by writing the archive first.

// The backfill's verdicts.
const (
	stateUnexplained = "unexplained_tag"
	stateAdopt       = "adopt"
	stateRepair      = "repair"
	stateSettled     = "settled"
)

// subjectLimit is how many commit subjects a reconstructed description
// quotes.
const subjectLimit = 5

// placeholderDescription is a materialized archive's description when no
// source yields one: it names the obligation instead of pretending the
// version had no summary.
const placeholderDescription = "RECOVERY OBLIGATION: no description was recoverable for this version (neither the GitHub Release notes, nor the CHANGELOG.md section, nor the commit subjects in its tag range carried one). Author a description from this version's changelog entries."

// materializedHeader is the comment block of an archive the backfill
// materialized: written after the fact, which a reader of a read-only
// archive has no other way to know.
var materializedHeader = []string{
	"Materialized by `rlsbl release backfill`: this version shipped before rlsbl",
	"archived a release file per version, so no archive existed. A description",
	"is recovered from the first source that yields one: an operator-reviewed",
	"--overrides file, the version's GitHub Release body, its CHANGELOG.md",
	"section, the commit subjects in its tag range, and otherwise a placeholder",
	"naming the recovery obligation. bump is derived by version arithmetic",
	"against the predecessor, and include is the targets detected at backfill",
	"time (the historical target set is not recoverable).",
}

// BackfillRequest is one `release backfill`.
type BackfillRequest struct {
	// Overrides is the path of an operator-reviewed descriptions file, or
	// empty for none.
	Overrides string
	// AutoCommit commits the written archives.
	AutoCommit bool
}

// override is one reviewed description.
type override struct {
	description string
	context     string
}

// readOverrides reads an overrides file: one [versions."X.Y.Z"] table per
// version with a description and an optional context, and nothing else.
// Every other shape is refused, naming the key: the file is reviewed text,
// and a typo that applied to nothing would be worse than one that stops the
// backfill.
func readOverrides(file string) (map[string]override, error) {
	data, err := os.ReadFile(file)
	if err != nil {
		return nil, fmt.Errorf("the overrides file %s cannot be read: %w", file, err)
	}
	parsed, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		return nil, fmt.Errorf("%s: %v", file, err)
	}
	doc := *parsed
	var unknown []string
	for key := range doc {
		if key != "versions" {
			unknown = append(unknown, key)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return nil, fmt.Errorf("%s: unknown top-level key(s) %s; an overrides file holds one [versions.\"X.Y.Z\"] table per version and nothing else", file, strings.Join(unknown, ", "))
	}
	raw, ok := doc["versions"]
	if !ok {
		return nil, fmt.Errorf("%s has no [versions] table; write one [versions.\"X.Y.Z\"] table per version whose description you reviewed", file)
	}
	versions, ok := raw.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("%s: [versions] must be a table of version tables", file)
	}
	out := map[string]override{}
	for _, version := range sortedKeys(versions) {
		where := fmt.Sprintf("%s: [versions.%q]", file, version)
		if _, err := semver.Parse(version); err != nil {
			return nil, fmt.Errorf("%s: %v", where, err)
		}
		entry, ok := versions[version].(map[string]any)
		if !ok {
			return nil, fmt.Errorf("%s must be a table", where)
		}
		var extra []string
		for key := range entry {
			if key != "description" && key != "context" {
				extra = append(extra, key)
			}
		}
		if len(extra) > 0 {
			sort.Strings(extra)
			return nil, fmt.Errorf("%s: unknown key(s) %s; only description and context are accepted", where, strings.Join(extra, ", "))
		}
		description, _ := entry["description"].(string)
		if strings.TrimSpace(description) == "" {
			return nil, fmt.Errorf("%s: description must be a non-empty string", where)
		}
		context := ""
		if raw, present := entry["context"]; present {
			s, isString := raw.(string)
			if !isString {
				return nil, fmt.Errorf("%s: context must be a string", where)
			}
			context = strings.TrimSpace(s)
		}
		out[version] = override{description: strings.TrimSpace(description), context: context}
	}
	return out, nil
}

// backfillScope is one releasable's release state.
type backfillScope struct {
	refs          *releasableRefs
	dir           string
	changelogDir  string
	changelogMD   string
	releasedPaths []string
	firstMember   *declarations.Member
	// undecidable is why the companion tags of the members could not be
	// derived, empty when they could.
	undecidable string
	spellings   map[string][]string
}

func (s *backfillScope) name() string { return s.refs.releasable.Name }

// candidates are every tag spelling v is addressable under in this scope,
// from targets.ExpectedRefsOf. Members whose targets cannot be read leave
// the companions out, and the scope says why.
func (s *backfillScope) candidates(v semver.Version) []string {
	key := v.String()
	if cached, ok := s.spellings[key]; ok {
		return cached
	}
	refs, err := s.refs.expected(v, nil, s.refs.membersErr == nil)
	var spellings []string
	if err == nil {
		spellings = refs.Spellings()
	} else {
		spellings = []string{s.refs.scheme.Render(v)}
	}
	s.spellings[key] = spellings
	return spellings
}

// versionPlan is what the backfill does to one version's archive.
type versionPlan struct {
	scope         *backfillScope
	version       semver.Version
	archivePath   string
	archiveExists bool
	state         string
	actions       []string
	notes         []string
	tag           string
	releaseCommit string
	trees         map[string]string
	unrecoverable bool
	fills         []releaserecord.ArchiveFill
	bump          semver.Bump
	include       []string
	description   string
	source        string
	context       string
	shippedAs     string
}

func (vp *versionPlan) key() string { return vp.scope.name() + " " + vp.version.String() }

func (vp *versionPlan) changed() bool { return len(vp.actions) > 0 }

// unexplainedTag is a tag nothing in the repository accounts for.
type unexplainedTag struct {
	tag     string
	version string
	probed  []string
}

// backfillPlan is everything the backfill decided, before anything is
// written.
type backfillPlan struct {
	scopes      []*backfillScope
	versions    []*versionPlan
	unexplained []unexplainedTag
	unversioned []string
	stash       []string
}

func (p *backfillPlan) changedVersions() []*versionPlan {
	var out []*versionPlan
	for _, vp := range p.versions {
		if vp.changed() {
			out = append(out, vp)
		}
	}
	return out
}

// backfillRun is one `release backfill` in progress.
type backfillRun struct {
	ctx  *strictcli.Context
	root string
	ws   *workspace.Workspace
	// repo reads through the observation's screened runner.
	repo       git.Repo
	gh         github.Client
	repository github.Repository
	ghChecked  bool
	record     *lifecycle.Record
	overrides  map[string]override
}

// Backfill runs `release backfill` over every releasable of the repository
// rooted at root: the releasables share one tag namespace, and a backfill
// that saw one of them would report every other's tags unexplained.
func Backfill(ctx *strictcli.Context, root string, req BackfillRequest) error {
	ws, err := workspace.Load(root)
	if err != nil {
		return err
	}
	record, err := lifecycle.Load(root)
	if err != nil {
		return err
	}
	var overrides map[string]override
	if req.Overrides != "" {
		if overrides, err = readOverrides(req.Overrides); err != nil {
			return err
		}
	}
	var plan *backfillPlan
	reconciler := previewapply.Reconciler{
		ShowKeys: true,
		Observe: func(o previewapply.Observer) (previewapply.Preview, error) {
			r, err := newBackfillRun(ctx, o, root, ws, record, overrides)
			if err != nil {
				return previewapply.Preview{}, err
			}
			if plan, err = r.plan(); err != nil {
				return previewapply.Preview{}, err
			}
			r.sayScopes(plan)
			if !ctx.DryRun() {
				if len(plan.unexplained) > 0 {
					return previewapply.Preview{}, unexplainedError(plan)
				}
				if len(plan.stash) > 0 {
					return previewapply.Preview{}, fmt.Errorf("%s", git.StashRefusal(plan.stash, "backfill", "The backfill rewrites archived release files and commits them."))
				}
			}
			return backfillPreview(plan)
		},
		Apply: func(e *strictcli.Effects, item previewapply.Item) error {
			vp, ok := item.Data.(*versionPlan)
			if !ok || !vp.changed() {
				return nil
			}
			if err := applyVersion(e, root, vp); err != nil {
				return err
			}
			ctx.Out(fmt.Sprintf("%s: wrote %s", vp.key(), vp.archivePath))
			return nil
		},
	}
	if _, err := previewapply.Reconcile(ctx, reconciler); err != nil {
		return err
	}
	if len(plan.unversioned) > 0 {
		ctx.Out("Tags the lifecycle-and-license record keeps outside the version model (accounted for, nothing owed): " + strings.Join(plan.unversioned, ", "))
	}
	changed := plan.changedVersions()
	if ctx.DryRun() {
		ctx.Out(fmt.Sprintf("Dry run: %d archive(s) would be written, %d unexplained tag(s) would refuse the apply. Nothing was written.", len(changed), len(plan.unexplained)))
		if len(plan.stash) > 0 {
			ctx.Out(fmt.Sprintf("%d stash entr(ies) are present and will refuse the apply; see `git stash list`.", len(plan.stash)))
		}
		if len(plan.unexplained) > 0 {
			return unexplainedError(plan)
		}
		return nil
	}
	if len(changed) == 0 {
		ctx.Out("Nothing to do: every archive records a fate and answers every required field.")
		return nil
	}
	var written []string
	for _, vp := range changed {
		written = append(written, vp.archivePath)
	}
	ctx.Out(fmt.Sprintf("Wrote %d archive(s).", len(written)))
	if !req.AutoCommit {
		return nil
	}
	repo, err := git.Open(ctx.Effects(), root)
	if err != nil {
		return err
	}
	_, err = repo.Commit(git.CommitRequest{Message: commitMessage(written), Paths: written, Autogenerated: true, RequireChange: true})
	return err
}

// commitMessage names the directories the backfill wrote in.
func commitMessage(written []string) string {
	var dirs []string
	for _, p := range written {
		if d := path.Dir(p); !slices.Contains(dirs, d) {
			dirs = append(dirs, d)
		}
	}
	sort.Strings(dirs)
	return fmt.Sprintf("Backfill release archives in %s", strings.Join(dirs, ", "))
}

func newBackfillRun(ctx *strictcli.Context, o previewapply.Observer, root string, ws *workspace.Workspace, record *lifecycle.Record, overrides map[string]override) (*backfillRun, error) {
	repo, err := git.Open(o, root)
	if err != nil {
		return nil, err
	}
	gh, err := github.New(o)
	if err != nil {
		return nil, err
	}
	return &backfillRun{ctx: ctx, root: root, ws: ws, repo: repo, gh: gh, record: record, overrides: overrides}, nil
}

// scopes are one per releasable.
func (r *backfillRun) scopes() ([]*backfillScope, error) {
	events, err := releaserecord.ReadEvents(r.root)
	if err != nil {
		return nil, err
	}
	var scopes []*backfillScope
	for _, rel := range r.ws.Releasables() {
		refs, err := newReleasableRefs(r.ws, events, rel)
		if err != nil {
			return nil, err
		}
		s := &backfillScope{
			refs:         refs,
			dir:          releaserecord.ArchiveDir(rel.Name),
			changelogDir: changelog.Dir(rel.Name),
			changelogMD:  changelog.Home(r.ws.Declarations, rel.Name),
			spellings:    map[string][]string{},
		}
		for _, m := range r.ws.MembersOf(rel.Name) {
			if s.firstMember == nil {
				member := m
				s.firstMember = &member
			}
			s.releasedPaths = append(s.releasedPaths, m.Path)
		}
		if len(s.releasedPaths) == 0 {
			s.releasedPaths = []string{declarations.RootPath}
		}
		if refs.membersErr != nil {
			s.undecidable = fmt.Sprintf("%s: the companion tags its members' ecosystems need cannot be derived (%v)", rel.Name, refs.membersErr)
		}
		scopes = append(scopes, s)
	}
	return scopes, nil
}

// sayScopes prints what each scope reads, before the verdicts.
func (r *backfillRun) sayScopes(plan *backfillPlan) {
	for _, s := range plan.scopes {
		r.ctx.Out(fmt.Sprintf("releasable %s: archives %s, changelog %s, released paths %s, tag format %s", s.name(), s.dir, s.changelogDir, strings.Join(s.releasedPaths, ", "), s.refs.scheme.Pattern()))
		if s.undecidable != "" {
			r.ctx.Out("  NOTE " + s.undecidable)
		}
	}
}

// tagCommit is the commit the local tag points at, and empty when there is
// no such tag.
func (r *backfillRun) tagCommit(tag string) (string, error) {
	sha, found, err := r.repo.TagCommit(tag)
	if err != nil || !found {
		return "", err
	}
	return sha, nil
}

// scopeData is what one scope already records.
type scopeData struct {
	archives   map[string]releaserecord.LooseArchive
	changelogs map[string]bool
	versions   []semver.Version
}

// plan inspects the repository and decides every action, writing nothing.
func (r *backfillRun) plan() (*backfillPlan, error) {
	scopes, err := r.scopes()
	if err != nil {
		return nil, err
	}
	plan := &backfillPlan{scopes: scopes}
	known := map[string]semver.Version{}
	inScope := map[string]bool{}
	data := map[*backfillScope]*scopeData{}
	for _, s := range scopes {
		d := &scopeData{archives: map[string]releaserecord.LooseArchive{}, changelogs: map[string]bool{}}
		archived, err := releaserecord.ArchivedVersions(r.root, s.dir)
		if err != nil {
			return nil, err
		}
		seen := map[string]bool{}
		for _, v := range archived {
			a, err := releaserecord.ReadArchiveLoosely(r.root, s.dir, v)
			if err != nil {
				return nil, err
			}
			d.archives[v.String()] = a
			seen[v.String()] = true
			d.versions = append(d.versions, v)
		}
		released, err := changelog.Versions(r.root, s.changelogDir)
		if err != nil {
			return nil, err
		}
		for _, v := range released {
			d.changelogs[v.String()] = true
			if !seen[v.String()] {
				seen[v.String()] = true
				d.versions = append(d.versions, v)
			}
		}
		semver.Sort(d.versions)
		data[s] = d
		for _, v := range d.versions {
			inScope[v.String()] = true
			// Every spelling that resolves, not the first: a version stands
			// under several at once (a renamed releasable's alias beside the
			// tag its archive names in shipped_as, a Go member's companion
			// beside the primary), and a spelling left out here is one
			// nothing explains.
			for _, candidate := range probeOrder(s, v, d.archives[v.String()]) {
				sha, err := r.tagCommit(candidate)
				if err != nil {
					return nil, err
				}
				if sha != "" {
					known[candidate] = v
				}
			}
		}
	}
	tags, err := r.repo.TagCommits()
	if err != nil {
		return nil, err
	}
	allTags := sortedKeys(tags)
	type adoption struct {
		scope   *backfillScope
		version semver.Version
		tag     string
	}
	adopted := map[string]adoption{}
	for _, tag := range allTags {
		if _, ok := known[tag]; ok {
			continue
		}
		s, v, ok := scopeForTag(scopes, tag)
		if !ok {
			continue
		}
		if slices.ContainsFunc(data[s].versions, func(x semver.Version) bool { return semver.Compare(x, v) == 0 }) {
			continue
		}
		// A version adopted under one spelling is adopted once; every further
		// spelling of it is explained by the same release.
		key := s.name() + " " + v.String()
		if _, ok := adopted[key]; !ok {
			adopted[key] = adoption{scope: s, version: v, tag: tag}
		}
		known[tag] = v
		inScope[v.String()] = true
	}
	var unknown []string
	for _, v := range sortedKeys(r.overrides) {
		if !inScope[v] {
			unknown = append(unknown, v)
		}
	}
	if len(unknown) > 0 {
		return nil, fmt.Errorf("the overrides file names version(s) this repository does not have: %s. Every override must name a version some releasable records (an archive, a released changelog file, or a version tag under its own tag format); nothing is silently ignored", strings.Join(unknown, ", "))
	}
	for _, s := range scopes {
		d := data[s]
		versions := append([]semver.Version(nil), d.versions...)
		adoptedTags := map[string]string{}
		for _, a := range adopted {
			if a.scope == s {
				versions = append(versions, a.version)
				adoptedTags[a.version.String()] = a.tag
			}
		}
		semver.Sort(versions)
		var include []string
		for i, v := range versions {
			var predecessor *semver.Version
			if i > 0 {
				predecessor = &versions[i-1]
			}
			vp, err := r.planVersion(s, d, v, predecessor, adoptedTags[v.String()], include)
			if err != nil {
				return nil, err
			}
			if vp.include != nil {
				include = vp.include
			}
			plan.versions = append(plan.versions, vp)
		}
	}
	if err := r.findUnexplained(plan, known, allTags); err != nil {
		return nil, err
	}
	if plan.stash, err = r.repo.StashEntries(); err != nil {
		return nil, err
	}
	return plan, nil
}

// findUnexplained lists every tag nothing accounts for: no archive or
// changelog file of a releasable claims it, and the lifecycle-and-license
// record neither keeps it outside the version model nor names a closed
// identity that owned it when it was created.
func (r *backfillRun) findUnexplained(plan *backfillPlan, known map[string]semver.Version, allTags []string) error {
	// The archives' shipped_as tags are claimed through probeOrder above:
	// the strict archive reader is not asked here, because the archives this
	// backfill repairs are the ones it refuses.
	explanations, err := releaserecord.BuildExplanations(r.root, known, nil, r.record)
	if err != nil {
		return err
	}
	created, err := r.repo.TagCreationTimes()
	if err != nil {
		return err
	}
	for _, tag := range allTags {
		x, found, err := explanations.Explain(tag, created[tag])
		if err != nil {
			return err
		}
		if found {
			if x.Source == releaserecord.SourceUnversionedTag {
				plan.unversioned = append(plan.unversioned, tag)
			}
			continue
		}
		entry := unexplainedTag{tag: tag}
		if v, _, ok := workspace.ParseVersionTag(tag); ok {
			entry.version = v.String()
			for _, s := range plan.scopes {
				for _, spelling := range s.candidates(v) {
					entry.probed = append(entry.probed, s.name()+": "+spelling)
				}
			}
		}
		plan.unexplained = append(plan.unexplained, entry)
	}
	return nil
}

// scopeForTag is the scope one of whose refs is tag, and the version: by
// construction (each scope's whole ref set derived at the version the tag
// parses as, then compared), so a scope never claims a spelling it would not
// write itself.
func scopeForTag(scopes []*backfillScope, tag string) (*backfillScope, semver.Version, bool) {
	v, _, ok := workspace.ParseVersionTag(tag)
	if !ok {
		return nil, semver.Version{}, false
	}
	for _, s := range scopes {
		if slices.Contains(s.candidates(v), tag) {
			return s, v, true
		}
	}
	return nil, semver.Version{}, false
}

// probeOrder are the spellings to try for v, the archive's shipped_as
// first: it names the spelling the version did ship under, where the
// scheme's spelling is what today's tag format gives it.
func probeOrder(s *backfillScope, v semver.Version, a releaserecord.LooseArchive) []string {
	var order []string
	if a.ShippedAs != "" {
		order = append(order, a.ShippedAs)
	}
	for _, c := range s.candidates(v) {
		if !slices.Contains(order, c) {
			order = append(order, c)
		}
	}
	return order
}

// bumpMessages are the whole commit messages a release of v of the scope
// commits its version bump under: the release's tag spellings, the bare
// version earlier flows wrote, and a releasable's own "<name>: release
// v<version>". Each is matched whole, so one releasable's message never
// matches another's.
func bumpMessages(s *backfillScope, v semver.Version) []string {
	messages := []string{"v" + v.String(), v.String()}
	messages = append(messages, s.candidates(v)...)
	messages = append(messages, s.name()+": release v"+v.String())
	var out []string
	for _, m := range messages {
		if !slices.Contains(out, m) {
			out = append(out, m)
		}
	}
	return out
}

// deriveBump is the bump the highest-order component that moved names; how
// far it moved is never read (a gap in the archived history says nothing
// about how many releases crossed it). A first version is measured against
// 0.0.0. An infra release moved the version as a patch did, so patch is
// the answer for both.
func deriveBump(v semver.Version, predecessor *semver.Version) semver.Bump {
	prev := semver.Version{}
	if predecessor != nil {
		prev = *predecessor
	}
	switch {
	case v.Major != prev.Major:
		return semver.Major
	case v.Minor != prev.Minor:
		return semver.Minor
	}
	return semver.Patch
}

func describePredecessor(p *semver.Version) string {
	if p == nil {
		return "0.0.0"
	}
	return p.String()
}

// planVersion decides what the backfill does to one version's archive.
func (r *backfillRun) planVersion(s *backfillScope, d *scopeData, v semver.Version, predecessor *semver.Version, adoptedTag string, includeHint []string) (*versionPlan, error) {
	state, exists := d.archives[v.String()]
	vp := &versionPlan{scope: s, version: v, archivePath: releaserecord.ArchivePath(s.dir, v), archiveExists: exists, state: stateSettled}
	if !d.changelogs[v.String()] {
		vp.notes = append(vp.notes, "no released changelog file for this version")
	}
	ov, hasOverride := r.overrides[v.String()]
	if exists && state.Fate == releaserecord.FateNeverReleased {
		if hasOverride {
			return nil, fmt.Errorf("the overrides file names %s, whose archive %s records never_released = true: the version number exists, but no release does, so it has no description to review. Remove it from the overrides file, or, if the version did ship, delete the never_released line of the archive and run the backfill again", v, vp.archivePath)
		}
		vp.notes = append(vp.notes, "the archive records never_released = true (the version number exists, no release does); the fate is settled and nothing is proposed")
		return vp, nil
	}
	probed := probeOrder(s, v, state)
	sha := ""
	if adoptedTag != "" {
		vp.tag = adoptedTag
		var err error
		if sha, err = r.tagCommit(adoptedTag); err != nil {
			return nil, err
		}
	} else {
		for _, candidate := range probed {
			found, err := r.tagCommit(candidate)
			if err != nil {
				return nil, err
			}
			if found != "" {
				vp.tag, sha = candidate, found
				break
			}
		}
	}
	if vp.tag != "" && exists && state.ShippedAs == vp.tag {
		vp.notes = append(vp.notes, fmt.Sprintf("recorded from the tag %s, the spelling the archive names in shipped_as", vp.tag))
	}
	alreadyRecorded := exists && (state.Fate == releaserecord.FateRecorded || state.Fate == releaserecord.FateUnrecoverable)
	if vp.tag == "" && !alreadyRecorded {
		commits, err := r.repo.CommitsWithMessage(bumpMessages(s, v))
		if err != nil {
			return nil, err
		}
		if len(commits) > 0 {
			sha = commits[0]
			vp.notes = append(vp.notes, fmt.Sprintf("no tag (probed %s); recorded from the version-bump commit %s", strings.Join(probed, ", "), short(sha)))
			if len(commits) > 1 {
				var shorts []string
				for _, c := range commits {
					shorts = append(shorts, short(c))
				}
				vp.notes = append(vp.notes, fmt.Sprintf("%d commits carry this version-bump message; the newest was taken (%s)", len(commits), strings.Join(shorts, ", ")))
			}
			vp.notes = append(vp.notes, fmt.Sprintf("if %s was never released, no commit may be recorded for it: declare its fate first by writing %s with never_released = true, then run the backfill again. The archive is the declaration, because only you know whether a release happened", v, vp.archivePath))
		} else {
			vp.unrecoverable = true
			vp.notes = append(vp.notes, fmt.Sprintf("no tag (probed %s) and no version-bump commit in history: unrecoverable", strings.Join(probed, ", ")))
		}
	}
	if sha != "" && !vp.unrecoverable && !alreadyRecorded {
		vp.releaseCommit = sha
		trees, notes, err := r.treesAt(sha, s.releasedPaths)
		if err != nil {
			return nil, err
		}
		vp.trees = trees
		vp.notes = append(vp.notes, notes...)
		if len(trees) == 0 {
			vp.unrecoverable = true
			vp.releaseCommit = ""
			vp.notes = append(vp.notes, fmt.Sprintf("the commit %s holds no tree for any released path", short(sha)))
		}
	}
	describe := func() (string, string, error) {
		if hasOverride {
			return ov.description, "overrides-file", nil
		}
		subject := vp.releaseCommit
		if subject == "" && exists {
			subject = state.ReleaseCommit
		}
		if subject == "" {
			subject = sha
		}
		return r.recoverDescription(s, v, vp.tag, subject, predecessor)
	}
	if !exists {
		vp.state = stateMaterialize
		if adoptedTag != "" {
			vp.state = stateAdopt
			vp.notes = append(vp.notes, fmt.Sprintf("the tag %s records a release no archive and no changelog file names; adopting it as released", adoptedTag))
		}
		include, note := r.detectInclude(s, includeHint)
		vp.include = include
		if note != "" {
			vp.notes = append(vp.notes, note)
		}
		vp.bump = deriveBump(v, predecessor)
		var err error
		if vp.description, vp.source, err = describe(); err != nil {
			return nil, err
		}
		if hasOverride {
			vp.context = ov.context
		}
		if vp.tag != "" && !slices.Contains(s.candidates(v), vp.tag) {
			vp.shippedAs = vp.tag
		}
		vp.actions = append(vp.actions, fmt.Sprintf("write the archive (bump %s from the predecessor %s, description from %s, include %s)", vp.bump, describePredecessor(predecessor), vp.source, formatList(vp.include)))
		if vp.unrecoverable {
			vp.actions = append(vp.actions, "record unrecoverable = true")
		} else {
			vp.actions = append(vp.actions, fmt.Sprintf("record release_commit %s and released_trees %s", short(vp.releaseCommit), formatTrees(vp.trees)))
		}
		return vp, nil
	}
	vp.state = stateRepair
	missing := state.Missing()
	if slices.Contains(missing, "bump") {
		vp.bump = deriveBump(v, predecessor)
		vp.fills = append(vp.fills, releaserecord.ArchiveFill{Field: "bump", Value: string(vp.bump), Source: "version arithmetic against " + describePredecessor(predecessor)})
	}
	if slices.Contains(missing, "include") {
		include, note := r.detectInclude(s, includeHint)
		vp.include = include
		if note != "" {
			vp.notes = append(vp.notes, note)
		}
		vp.fills = append(vp.fills, releaserecord.ArchiveFill{Field: "include", Value: include, Source: "the targets detected at backfill time"})
	}
	if slices.Contains(missing, "exclude") {
		vp.fills = append(vp.fills, releaserecord.ArchiveFill{Field: "exclude", Value: []string{}, Source: "the default: nothing excluded"})
	}
	if slices.Contains(missing, "description") {
		var err error
		if vp.description, vp.source, err = describe(); err != nil {
			return nil, err
		}
		vp.fills = append(vp.fills, releaserecord.ArchiveFill{Field: "description", Value: vp.description, Source: vp.source})
	} else if hasOverride && strings.TrimSpace(state.Description) != ov.description {
		vp.description, vp.source = ov.description, "overrides-file"
		vp.fills = append(vp.fills, releaserecord.ArchiveFill{Field: "description", Value: vp.description, Source: vp.source})
	}
	if hasOverride && ov.context != "" && strings.TrimSpace(state.Context) != ov.context {
		vp.context = ov.context
		vp.fills = append(vp.fills, releaserecord.ArchiveFill{Field: "context", Value: ov.context, Source: "overrides-file"})
	}
	for _, f := range vp.fills {
		vp.actions = append(vp.actions, fmt.Sprintf("write %s (from %s)", f.Field, f.Source))
	}
	switch {
	case alreadyRecorded:
		// Said only beside other work, where it explains why the fate is
		// left as it is.
		if len(vp.actions) > 0 {
			if state.Fate == releaserecord.FateUnrecoverable {
				vp.notes = append(vp.notes, "already recorded unrecoverable; the fate is left as it is")
			} else {
				vp.notes = append(vp.notes, "already recorded; the fate is left as it is")
			}
		}
	case vp.unrecoverable:
		vp.actions = append(vp.actions, "record unrecoverable = true")
	default:
		vp.actions = append(vp.actions, fmt.Sprintf("record release_commit %s and released_trees %s", short(vp.releaseCommit), formatTrees(vp.trees)))
	}
	if !vp.changed() {
		vp.state = stateSettled
	}
	return vp, nil
}

func formatList(values []string) string { return "[" + strings.Join(values, ", ") + "]" }

func formatTrees(trees map[string]string) string {
	var parts []string
	for _, p := range sortedKeys(trees) {
		parts = append(parts, p+"="+short(trees[p]))
	}
	return "{" + strings.Join(parts, ", ") + "}"
}

// treesAt is the tree of each released path at sha. A path that did not
// exist at that commit is left out with a note (a workspace's member
// directories did not exist in its standalone history); when none existed,
// the root tree, under ".", is what that commit released.
func (r *backfillRun) treesAt(sha string, paths []string) (map[string]string, []string, error) {
	trees := map[string]string{}
	var notes []string
	for _, p := range paths {
		tree, found, err := r.repo.TreeAt(sha, p)
		if err != nil {
			return nil, nil, err
		}
		if found {
			trees[p] = tree
		} else if p != declarations.RootPath {
			notes = append(notes, fmt.Sprintf("the path %s did not exist at %s", p, short(sha)))
		}
	}
	if len(trees) == 0 {
		tree, found, err := r.repo.TreeAt(sha, declarations.RootPath)
		if err != nil {
			return nil, nil, err
		}
		if found {
			trees[declarations.RootPath] = tree
			notes = append(notes, "no released path existed at this commit; recorded the root tree as \".\"")
		}
	}
	return trees, notes, nil
}

// detectInclude is the targets detected now on the scope's first member:
// the historical target set is not recoverable. A member whose targets
// cannot be read gets an empty include and a note saying why.
func (r *backfillRun) detectInclude(s *backfillScope, hint []string) ([]string, string) {
	if hint != nil {
		return append([]string{}, hint...), ""
	}
	include := []string{}
	if s.firstMember == nil {
		return include, ""
	}
	found, err := targets.MemberTargets(r.root, *s.firstMember)
	if err != nil {
		return include, fmt.Sprintf("include is empty: the targets of the member %q cannot be read (%v)", s.firstMember.Name, err)
	}
	for _, t := range found {
		if !slices.Contains(include, t.Name) {
			include = append(include, t.Name)
		}
	}
	return include, ""
}

// recoverDescription recovers a version's description from the first
// source that yields one: its GitHub Release body (unless it carries only
// the boilerplate GitHub generates), its CHANGELOG.md section's lead
// paragraph, the commit subjects of its tag range, and otherwise the
// placeholder naming the obligation. It returns the description and its
// source.
func (r *backfillRun) recoverDescription(s *backfillScope, v semver.Version, tag, sha string, predecessor *semver.Version) (string, string, error) {
	if tag != "" {
		body, found, err := r.releaseBody(tag)
		if err != nil {
			return "", "", err
		}
		if found {
			if text := descriptionFromBody(body); text != "" {
				return text, "github-release:" + tag, nil
			}
		}
	}
	if content, err := os.ReadFile(filepath.Join(r.root, filepath.FromSlash(s.changelogMD))); err == nil {
		if section, ok := changelog.ExtractSection(string(content), v.String()); ok {
			if text := leadParagraph(section); text != "" {
				return text, "changelog-md", nil
			}
		}
	} else if !os.IsNotExist(err) {
		return "", "", fmt.Errorf("reading %s: %w", s.changelogMD, err)
	}
	if sha != "" {
		previous := ""
		if predecessor != nil {
			for _, candidate := range s.candidates(*predecessor) {
				found, err := r.tagCommit(candidate)
				if err != nil {
					return "", "", err
				}
				if found != "" {
					previous = found
					break
				}
			}
		}
		subjects, err := r.repo.Subjects(sha, previous, subjectLimit+1)
		if err != nil {
			return "", "", err
		}
		if text := subjectDescription(subjects); text != "" {
			span := short(sha)
			if previous != "" {
				span = short(previous) + ".." + short(sha)
			}
			return text, "commit-subjects:" + span, nil
		}
	}
	return placeholderDescription, "placeholder", nil
}

// releaseBody reads the GitHub Release body of tag; found is false when the
// tag carries no Release, which is an answer. gh missing or unauthenticated
// is an error: the Release body is the recovery's first source, and a
// backfill that skipped it would record a description from a lesser source
// as if the Release had none.
func (r *backfillRun) releaseBody(tag string) (string, bool, error) {
	if !r.ghChecked {
		if err := r.gh.CheckInstalled(); err != nil {
			return "", false, fmt.Errorf("the backfill recovers descriptions from the GitHub Releases first, and %w", err)
		}
		if err := r.gh.CheckAuth(); err != nil {
			return "", false, fmt.Errorf("the backfill recovers descriptions from the GitHub Releases first, and %w", err)
		}
		url := ""
		if r.ws.Declarations.GitHubRepository == "" {
			var err error
			if url, err = r.repo.RemoteURL(origin); err != nil {
				return "", false, err
			}
		}
		repository, err := github.ResolveRepository(r.ws.Declarations.GitHubRepository, url)
		if err != nil {
			return "", false, err
		}
		r.repository = repository
		r.ghChecked = true
	}
	exists, err := r.gh.ReleaseExists(r.repository, tag)
	if err != nil || !exists {
		return "", false, err
	}
	body, err := r.gh.ReleaseBody(r.repository, tag)
	if err != nil {
		return "", false, err
	}
	return body, true, nil
}

// compareLink is the compare link GitHub writes into a body nobody
// authored.
var compareLink = regexp.MustCompile(`(?i)^\**\s*full changelog\s*\**\s*:`)

var bullet = regexp.MustCompile(`^[-*+]\s+`)

func boilerplate(line string) bool {
	return line == "" || strings.HasPrefix(line, "<!--") || strings.HasPrefix(line, "#") || compareLink.MatchString(line)
}

// bodyIsSubstantive reports whether a Release body carries content rather
// than generated boilerplate (headings, comments, the compare link).
func bodyIsSubstantive(body string) bool {
	for _, raw := range strings.Split(body, "\n") {
		if !boilerplate(strings.TrimSpace(raw)) {
			return true
		}
	}
	return false
}

// descriptionFromBody is a one-line description from a Release body, or
// empty: the prose paragraph it opens with, else its first bullet, else its
// first blockquote. A body whose only content is a table yields none: a
// table cell is data, not a sentence about the release.
func descriptionFromBody(body string) string {
	if !bodyIsSubstantive(body) {
		return ""
	}
	var lines []string
	for _, raw := range strings.Split(body, "\n") {
		lines = append(lines, strings.TrimSpace(raw))
	}
	var prose []string
	for _, line := range lines {
		if boilerplate(line) {
			if len(prose) > 0 {
				break
			}
			continue
		}
		if bullet.MatchString(line) || strings.HasPrefix(line, "<details") || strings.HasPrefix(line, "|") || strings.HasPrefix(line, ">") {
			break
		}
		prose = append(prose, line)
	}
	if text := strings.TrimSpace(strings.Join(prose, " ")); text != "" {
		return text
	}
	for _, line := range lines {
		if bullet.MatchString(line) {
			return strings.TrimSpace(bullet.ReplaceAllString(line, ""))
		}
	}
	for _, line := range lines {
		if strings.HasPrefix(line, ">") {
			return strings.TrimSpace(strings.TrimLeft(line, "> "))
		}
	}
	return ""
}

// leadParagraph is the prose paragraph a CHANGELOG.md version section opens
// with: everything from its first heading, bullet, details block, comment,
// table, or blockquote onward is the generated part.
func leadParagraph(section string) string {
	var lines []string
	for _, raw := range strings.Split(strings.TrimSpace(section), "\n") {
		line := strings.TrimSpace(raw)
		if strings.HasPrefix(line, "#") || strings.HasPrefix(line, "-") || strings.HasPrefix(line, "*") || strings.HasPrefix(line, "<details") || strings.HasPrefix(line, "<!--") || strings.HasPrefix(line, "|") || strings.HasPrefix(line, ">") {
			break
		}
		if line == "" && len(lines) > 0 {
			break
		}
		if line != "" {
			lines = append(lines, line)
		}
	}
	return strings.TrimSpace(strings.Join(lines, " "))
}

// subjectDescription is a description built from a version's commit
// subjects, or empty.
func subjectDescription(subjects []string) string {
	var quoted []string
	for i, s := range subjects {
		if i == subjectLimit {
			break
		}
		if s != "" {
			quoted = append(quoted, s)
		}
	}
	if len(quoted) == 0 {
		return ""
	}
	more := ""
	if len(subjects) > subjectLimit {
		more = " (and later commits)"
	}
	return "Reconstructed from this version's commit subjects: " + strings.Join(quoted, "; ") + "." + more
}

// backfillPreview is the plan as a preview, unexplained tags first: they
// refuse the apply, so a reader sees the blocker before the work. A settled
// version with nothing to say is not shown.
func backfillPreview(plan *backfillPlan) (previewapply.Preview, error) {
	var items []previewapply.Item
	for _, u := range plan.unexplained {
		facts := []string{"parses under no version tag shape"}
		if u.version != "" {
			facts = []string{"parses as version " + u.version}
			if len(u.probed) > 0 {
				facts = append(facts, "probed: "+strings.Join(u.probed, "; "))
			}
		}
		items = append(items, previewapply.Item{
			Key:     "tag " + u.tag,
			State:   stateUnexplained,
			Summary: "nothing in this repository accounts for this tag",
			Facts:   facts,
			Detail:  resolutions(u.tag),
		})
	}
	for _, vp := range plan.versions {
		if !vp.changed() && len(vp.notes) == 0 {
			continue
		}
		summary := "the archive is incomplete"
		switch vp.state {
		case stateSettled:
			summary = "nothing to do"
		case stateAdopt:
			summary = "a released version only the tag " + vp.tag + " records"
		case stateMaterialize:
			summary = "released, with no archive at all"
		}
		facts := []string{"archive: " + vp.archivePath}
		for _, n := range vp.notes {
			facts = append(facts, "note: "+n)
		}
		var actions []string
		for _, a := range vp.actions {
			actions = append(actions, "-> "+a)
		}
		items = append(items, previewapply.Item{Key: vp.key(), State: vp.state, Summary: summary, Facts: facts, Actions: actions, Data: vp})
	}
	return previewapply.NewPreview(items...)
}

// resolutions are the three ways out of an unexplained tag, each spelled
// out so it can be performed.
func resolutions(tag string) string {
	return "  Resolve it in one of three ways, then run the backfill again:\n" +
		"    1. Adopt it as released. A tag that is one of the refs some version here would own is adopted\n" +
		"       on its own, and this one is not, so an older tag format spelled it. Record that version's\n" +
		"       archive with shipped_as = \"" + tag + "\", and the backfill records its commit from this tag.\n" +
		"    2. Record it outside the version model, if it is no release at all (a moving marker, a vendor\n" +
		"       tag imported with the history):\n" +
		"         rlsbl transition unversioned-tag --tag " + tag + " --reason \"<why>\"\n" +
		"    3. Delete it, on your own decision:\n" +
		"         git tag -d " + tag + "\n" +
		"       (and on origin too, if it was ever pushed).\n" +
		"  rlsbl does not guess which of the three this is."
}

// unexplainedError is the refusal an unexplained tag stops the apply with.
func unexplainedError(plan *backfillPlan) error {
	lines := []string{fmt.Sprintf("refusing to backfill: %d tag(s) in this repository are not accounted for.", len(plan.unexplained)), ""}
	for _, u := range plan.unexplained {
		lines = append(lines, "  "+u.tag, resolutions(u.tag), "")
	}
	for _, s := range plan.scopes {
		if s.undecidable != "" {
			lines = append(lines, "  NOTE "+s.undecidable+"\n       A tag above may be one of the spellings those companions would have accounted for.", "")
		}
	}
	lines = append(lines, "Nothing has been written, not for the unexplained tags and not the archives the plan would repair: a backfill that wrote around an unexplained tag would record a release history it knows to be incomplete.")
	return fmt.Errorf("%s", strings.Join(lines, "\n"))
}

// applyVersion writes one version's archive.
func applyVersion(e *strictcli.Effects, root string, vp *versionPlan) error {
	fate := releaserecord.FateRecorded
	commit := releaserecord.ReleaseCommit{Commit: vp.releaseCommit, Trees: vp.trees}
	if vp.unrecoverable {
		fate = releaserecord.FateUnrecoverable
		commit = releaserecord.ReleaseCommit{}
	}
	if !vp.archiveExists {
		header := append(append([]string(nil), materializedHeader...), "This archive's description came from: "+vp.source+".")
		_, err := releaserecord.WriteArchive(e, root, vp.scope.dir, vp.version, releaserecord.ArchiveSpec{
			ReleaseFile: releaserecord.ReleaseFile{
				Bump:        vp.bump,
				Include:     vp.include,
				Exclude:     []string{},
				Description: vp.description,
				Context:     vp.context,
			},
			Fate:          fate,
			ReleaseCommit: commit,
			ShippedAs:     vp.shippedAs,
			Header:        header,
		})
		return err
	}
	completion := releaserecord.ArchiveCompletion{Fills: vp.fills}
	if vp.unrecoverable || vp.releaseCommit != "" {
		completion.Fate = fate
		completion.ReleaseCommit = commit
	}
	return releaserecord.CompleteArchive(e, root, vp.scope.dir, vp.version, completion)
}
