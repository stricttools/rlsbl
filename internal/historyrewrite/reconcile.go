package historyrewrite

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"slices"
	"sort"
	"strings"
	"time"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/historyrewrite/reconcileplanspec"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/releasenotes"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// A reconcile judges one releasable's published release metadata (the refs
// on origin, the GitHub Releases) against what its records say it released,
// and writes the difference only when told to. A difference is repaired only
// when a record explains it: safegit's rewrite journal, the transition
// record's release-commit remaps, and the committed history-rewrite
// archives explain a moved commit; the archives are the authority for
// where each released ref belongs. One ref origin holds that nothing
// explains aborts the whole reconcile, and nothing anywhere is written.

// The verdicts, as the preview's states. A preview prints them with
// hyphens, the spelling the plan file records.
const (
	stateMaterialize      = "materialize"
	stateAlreadyCorrect   = "already_correct"
	stateRePoint          = "re_point_with_lease"
	stateRefuseForeign    = "refuse_foreign"
	stateRefuseIdentity   = "refuse_identity_mismatch"
	planFormatVersion     = 2
	subjectRef            = "ref"
	subjectRelease        = "release"
	releaseKeyPrefix      = "release:"
	reconcileStashActions = "The apply pushes refs, force-pushes the ones a recorded rewrite moved, and creates GitHub Releases."
)

func refusal(state string) bool { return state == stateRefuseForeign || state == stateRefuseIdentity }

// ReconcileMode is which half of the reconcile runs.
type ReconcileMode string

// The two halves: the plan observes and writes the plan file; the apply
// performs it.
const (
	ReconcilePlan  ReconcileMode = "plan"
	ReconcileApply ReconcileMode = "apply"
)

// ReconcileRequest is one `release reconcile`.
type ReconcileRequest struct {
	Mode       ReconcileMode
	Releasable declarations.Releasable
	// PushTimeout bounds each ref push of the apply.
	PushTimeout time.Duration
	// Version is the running rlsbl's version, recorded in the plan.
	Version string
}

// SelectReleasable is the releasable the working directory dir selects, or
// the one named when it selects none. Naming one where the directory
// already selects one is refused, as is naming none where it selects none:
// which releasable a command acts on is never guessed.
func SelectReleasable(ws *workspace.Workspace, dir, named string) (declarations.Releasable, error) {
	selected, found, err := ws.ReleasableForDirectory(dir)
	if err != nil {
		return declarations.Releasable{}, err
	}
	if found {
		if named != "" {
			return declarations.Releasable{}, fmt.Errorf("--releasable is refused here: the working directory already selects the releasable %q; run the command without --releasable, or from the directory of the releasable you mean", selected.Name)
		}
		return selected, nil
	}
	var names []string
	for _, r := range ws.Releasables() {
		names = append(names, r.Name)
	}
	if named == "" {
		return declarations.Releasable{}, fmt.Errorf("the working directory selects no releasable (its member is versioned under none); name the one to act on with --releasable <name>: %s", strings.Join(names, ", "))
	}
	r, ok := ws.Declarations.Releasable(named)
	if !ok {
		return declarations.Releasable{}, fmt.Errorf("no releasable is named %q; the releasables are: %s", named, strings.Join(names, ", "))
	}
	return r, nil
}

// explanations is everything the records say about how the world got this
// way: the merged commit map, which record contributed each entry, and the
// identities whose change forbids recreating an older version's refs.
type explanations struct {
	commitMap map[string]string
	origins   map[string]string
	// changedIdentities are the releasable's closed identities that move a
	// Go module's published identity.
	changedIdentities []lifecycle.Identity
	sources           []string
}

// resolve follows sha through every recorded rewrite to its final commit,
// naming the records that moved it. A cycle stops the walk.
func (x explanations) resolve(sha string) (string, []string) {
	seen := map[string]bool{sha: true}
	current := sha
	var chain []string
	for {
		next, ok := x.commitMap[current]
		if !ok || seen[next] {
			return current, chain
		}
		chain = append(chain, x.origins[current])
		seen[next] = true
		current = next
	}
}

// collectExplanations merges the records that explain a moved commit:
// the committed history-rewrite archives, the transition record's
// release-commit remaps, and safegit's journal, in that order, so a move the
// journal also explains is attributed to it, the most recent record. Where
// two records name one old commit they name the same new one.
func collectExplanations(repo git.Repo, root string, record *lifecycle.Record, releasable string) (explanations, error) {
	x := explanations{commitMap: map[string]string{}, origins: map[string]string{}}
	archives, names, err := ReadRewriteArchives(root)
	if err != nil {
		return x, err
	}
	var used []string
	for _, name := range names {
		a := archives[name]
		if len(a.Rewrites) == 0 {
			continue
		}
		used = append(used, name)
		for old, next := range a.Rewrites {
			x.commitMap[old] = next
			x.origins[old] = "history-rewrite archive " + name
		}
	}
	if len(used) > 0 {
		x.sources = append(x.sources, "committed history-rewrite archives ("+strings.Join(used, ", ")+")")
	}
	events, err := releaserecord.ReadEvents(root)
	if err != nil {
		return x, err
	}
	remaps := 0
	for _, e := range events {
		remap, ok := e.(*releaserecord.ReleaseCommitRemapEvent)
		if !ok {
			continue
		}
		remaps++
		for _, m := range remap.Mappings {
			x.commitMap[m.OldSHA] = m.NewSHA
			x.origins[m.OldSHA] = "transition record release-commit-remap (" + remap.Rewrite + ")"
		}
	}
	if remaps > 0 {
		x.sources = append(x.sources, "the transition record's release-commit remaps")
	}
	journal, found, err := ReadJournal(repo)
	if err != nil {
		return x, err
	}
	if found && len(journal.CommitMap) > 0 {
		for old, next := range journal.CommitMap {
			x.commitMap[old] = next
			x.origins[old] = journal.Label()
		}
		x.sources = append(x.sources, journal.Label())
	}
	for _, id := range record.Identities() {
		if id.Subject != releasable || id.Pending() || id.Open() {
			continue
		}
		if id.Facet == lifecycle.FacetGoModulePath || id.Facet == lifecycle.FacetRepositoryURL {
			x.changedIdentities = append(x.changedIdentities, id)
		}
	}
	return x, nil
}

// observation is the world, read once: origin's refs, the local tag refs,
// and the tags carrying a GitHub Release.
type observation struct {
	remote   map[string]string
	local    map[string]string
	releases map[string]bool
}

// digest covers everything on the far side the verdicts were judged
// against: origin's refs (the force-push leases) and the Release listing.
// The plan records it, and the apply refuses a plan whose digest no longer
// matches.
func (o observation) digest() string {
	h := sha256.New()
	for _, ref := range sortedKeys(o.remote) {
		fmt.Fprintf(h, "%s %s\n", ref, o.remote[ref])
	}
	h.Write([]byte("--releases--\n"))
	for _, tag := range sortedKeys(o.releases) {
		fmt.Fprintf(h, "%s\n", tag)
	}
	return hex.EncodeToString(h.Sum(nil))
}

// refAction is what an apply does for one subject, carried from the
// observation to the apply.
type refAction struct {
	subject string
	ref     string
	tag     string
	version semver.Version
	// hasVersion is false for a tag no archive of the releasable records.
	hasVersion bool
	target     string
	observed   string
	// createLocalTag creates the tag here at target before pushing it.
	createLocalTag bool
	// repointMarker rewrites the rlsbl-ci-sha marker of the version's
	// Release after its primary tag moved.
	repointMarker bool
}

// reconcileRun is one `release reconcile` in progress.
type reconcileRun struct {
	ctx        *strictcli.Context
	e          *strictcli.Effects
	root       string
	ws         *workspace.Workspace
	record     *lifecycle.Record
	req        ReconcileRequest
	refs       *releasableRefs
	repository github.Repository
	now        func() time.Time
}

func (r *reconcileRun) say(line string) { r.ctx.Out(line) }

func (r *reconcileRun) dir() string { return releaserecord.ArchiveDir(r.req.Releasable.Name) }

// Reconcile runs one half of `release reconcile` for one releasable of the
// repository rooted at root. now stamps the plan and the transition
// record's events.
func Reconcile(ctx *strictcli.Context, root string, req ReconcileRequest, now func() time.Time) error {
	if req.Mode != ReconcilePlan && req.Mode != ReconcileApply {
		return fmt.Errorf("the reconcile runs --mode plan or --mode apply, not %q", req.Mode)
	}
	e := ctx.Effects()
	ws, err := workspace.Load(root)
	if err != nil {
		return err
	}
	repo, err := git.Open(e, root)
	if err != nil {
		return err
	}
	if req.Mode == ReconcileApply {
		if err := repo.RefuseStash("reconcile", reconcileStashActions); err != nil {
			return err
		}
	}
	record, err := lifecycle.Load(root)
	if err != nil {
		return err
	}
	events, err := releaserecord.ReadEvents(root)
	if err != nil {
		return err
	}
	refs, err := newReleasableRefs(ws, events, req.Releasable)
	if err != nil {
		return err
	}
	url := ""
	if ws.Declarations.GitHubRepository == "" {
		if url, err = repo.RemoteURL(origin); err != nil {
			return err
		}
	}
	repository, err := github.ResolveRepository(ws.Declarations.GitHubRepository, url)
	if err != nil {
		return err
	}
	r := &reconcileRun{ctx: ctx, e: e, root: root, ws: ws, record: record, req: req, refs: refs, repository: repository, now: now}
	x, err := collectExplanations(repo, root, record, req.Releasable.Name)
	if err != nil {
		return err
	}
	// The verdicts are judged against the archives, so archives naming
	// commits a rewrite removed are moved through the records that explain
	// the move first. It writes, so it runs before the observation.
	healed, err := r.heal(repo, x)
	if err != nil {
		return err
	}

	var obs observation
	var plan *reconcilePlan
	var noops []string
	reconciler := previewapply.Reconciler{
		ShowKeys: true,
		Observe: func(o previewapply.Observer) (previewapply.Preview, error) {
			var p previewapply.Preview
			var err error
			obs, p, err = r.observe(o, x, healed)
			if err != nil {
				return p, err
			}
			if req.Mode == ReconcileApply {
				if plan, err = readPlan(root, req.Releasable.Name); err != nil {
					return p, err
				}
				if plan.WorldDigest != obs.digest() {
					return p, fmt.Errorf("the world changed since %s was written (it observed %s, now %s): its force-push leases were captured from values origin no longer holds. Run `rlsbl release reconcile --mode plan`, read the new plan, and apply that", runstate.ReconcilePlanPath(req.Releasable.Name), short(plan.WorldDigest), short(obs.digest()))
				}
				if blocked := refusals(p); len(blocked) > 0 {
					return p, tripwireError(blocked)
				}
				if noops, err = checkPlanCovers(plan, p, runstate.ReconcilePlanPath(req.Releasable.Name)); err != nil {
					return p, err
				}
			}
			return p, nil
		},
		Apply: func(e *strictcli.Effects, item previewapply.Item) error {
			if req.Mode == ReconcilePlan {
				return nil
			}
			return r.apply(e, item)
		},
	}
	p, err := previewapply.Reconcile(ctx, reconciler)
	if err != nil {
		return err
	}
	if req.Mode == ReconcilePlan && !ctx.DryRun() && p.Len() > 0 {
		r.say(previewapply.Render(p, true))
	}
	if len(x.sources) > 0 {
		r.say("Explained from: " + strings.Join(x.sources, "; "))
	} else {
		r.say("No record explains a moved commit here (no safegit rewrite journal, no release-commit remap in the transition record, no committed history-rewrite archive): only refs origin agrees with, and refs origin lacks entirely, can be reconciled.")
	}
	if p.Len() == 0 {
		r.say("Nothing to reconcile: origin matches this releasable's records.")
	}
	planPath := runstate.ReconcilePlanPath(req.Releasable.Name)
	if req.Mode == ReconcilePlan {
		if blocked := refusals(p); len(blocked) > 0 {
			return tripwireError(blocked)
		}
		if ctx.DryRun() {
			r.say(fmt.Sprintf("The plan above was not written to %s: run the plan without --dry-run to write it, then `rlsbl release reconcile --mode apply` to perform it.", planPath))
			return nil
		}
		// An empty plan is written too, so the apply half of a reconcile that
		// found nothing is a clean no-op rather than a request to plan first.
		if err := runstate.SaveReconcilePlan(e, root, req.Releasable.Name, renderPlan(p, obs.digest(), req.Version, now())); err != nil {
			return err
		}
		r.say(fmt.Sprintf("Wrote %s. Read it, then perform it with `rlsbl release reconcile --mode apply --approve-consequential`.", planPath))
		return nil
	}
	if ctx.DryRun() {
		r.say(fmt.Sprintf("%s matches the world and would be applied; nothing was pushed or created.", planPath))
		return nil
	}
	actionable := 0
	for _, item := range p.Items() {
		if _, ok := item.Data.(refAction); ok {
			actionable++
		}
	}
	r.say(fmt.Sprintf("Applied %d change(s).", actionable))
	for _, key := range noops {
		r.say(fmt.Sprintf("  %s: already correct by the time the plan was applied.", key))
	}
	return runstate.ClearReconcilePlan(e, root, req.Releasable.Name)
}

// heal moves the releasable's archives whose release commit names a commit
// this repository no longer has through the records that explain the move,
// and commits the rewritten archives with the transition record's event. A
// dangling release commit no record explains is an error naming the
// version. Under --dry-run nothing is written, and the healed commits it
// returns are what keeps the preview truthful about the world a real run
// would judge. A changed released tree refuses: this command did not
// perform the rewrite, so it cannot say a changed tree is intended.
func (r *reconcileRun) heal(repo git.Repo, x explanations) (map[string]string, error) {
	versions, err := releaserecord.ArchivedVersions(r.root, r.dir())
	if err != nil {
		return nil, err
	}
	dangling := map[string]string{}
	for _, v := range versions {
		a, err := releaserecord.ReadArchive(r.root, r.dir(), v)
		if err != nil {
			return nil, err
		}
		if a.Fate != releaserecord.FateRecorded {
			continue
		}
		_, found, err := repo.ResolveCommit(a.ReleaseCommit.Commit)
		if err != nil {
			return nil, err
		}
		if !found {
			dangling[v.String()] = a.ReleaseCommit.Commit
		}
	}
	if len(dangling) == 0 {
		return nil, nil
	}
	planned, err := releaserecord.PlanRemap(repo, r.dir(), x.commitMap, releaserecord.ContentChangeRefuse)
	if err != nil {
		return nil, fmt.Errorf("the release record cannot be moved through the recorded rewrite:\n%w", err)
	}
	healed := map[string]string{}
	for _, m := range planned {
		healed[m.Version.String()] = m.NewCommit
	}
	var unexplained []string
	for _, v := range sortedKeys(dangling) {
		if _, ok := healed[v]; !ok {
			unexplained = append(unexplained, fmt.Sprintf("  %s: released from %s", v, dangling[v]))
		}
	}
	if len(unexplained) > 0 {
		return nil, fmt.Errorf("the release record names commits this repository no longer has, and no record explains where they went:\n%s\nAn archive's release_commit is the commit that version shipped from, and the archives are the authority for where every released ref belongs, so nothing can be judged against them while they name a missing commit. safegit's rewrite journal, a release-commit remap in the transition record, or a committed history-rewrite archive would explain the move; none does. Restore the commits (fetch them, or recover them from the repository that holds them) and run the reconcile again", strings.Join(unexplained, "\n"))
	}
	r.say(fmt.Sprintf("The release record names %d commit(s) this repository no longer has, and the records explain the move:", len(dangling)))
	var origins []string
	event := &releaserecord.ReleaseCommitRemapEvent{Releasable: r.req.Releasable.Name}
	for _, m := range planned {
		why := x.origins[m.OldCommit]
		r.say(fmt.Sprintf("  %s: %s -> %s (%s)", m.Version, short(m.OldCommit), short(m.NewCommit), why))
		if !slices.Contains(origins, why) {
			origins = append(origins, why)
		}
		event.Mappings = append(event.Mappings, releaserecord.CommitMapping{OldSHA: m.OldCommit, NewSHA: m.NewCommit})
	}
	if r.ctx.DryRun() {
		r.say("  The archives were not rewritten (--dry-run); the verdicts below are those a real run judges after moving them.")
		return healed, nil
	}
	sort.Strings(origins)
	event.Rewrite = strings.Join(origins, "; ")
	lock, err := runstate.Acquire(r.e, r.root, runstate.AcquireOptions{Wait: runstate.WaitForHolder, OnWait: func(path string) {
		r.say(fmt.Sprintf("Another rlsbl process holds %s; waiting for it before rewriting the archives.", path))
	}})
	if err != nil {
		return nil, err
	}
	defer lock.Release()
	var touched []string
	for _, m := range planned {
		if err := releaserecord.WriteReleaseCommit(r.e, r.root, r.dir(), m.Version, releaserecord.ReleaseCommit{Commit: m.NewCommit, Trees: m.Trees}); err != nil {
			return nil, err
		}
		touched = append(touched, m.Path)
	}
	if err := releaserecord.AppendEvents(r.e, r.root, []releaserecord.Event{event}, r.now()); err != nil {
		return nil, err
	}
	touched = append(touched, declarations.TransitionsFile)
	if _, err := repo.Commit(git.CommitRequest{
		Message:       "reconcile: move the release record's release commits through the recorded rewrite",
		Paths:         touched,
		Autogenerated: true,
		RequireChange: true,
	}); err != nil {
		return nil, fmt.Errorf("the archives' release commits were moved, but the rewritten archives could not be committed (%w); commit them (they are read-only records every other command reads) before running the reconcile again", err)
	}
	r.say(fmt.Sprintf("  Committed %d rewritten record file(s).", len(touched)))
	return healed, nil
}

// observe reads the world and judges every subject: each archived version's
// refs and its GitHub Release, then every local tag origin also holds that
// no archive claims, which is what makes a divergence trip the reconcile in
// a repository with no archives at all.
func (r *reconcileRun) observe(o previewapply.Observer, x explanations, healed map[string]string) (observation, previewapply.Preview, error) {
	repo, err := git.Open(o, r.root)
	if err != nil {
		return observation{}, previewapply.Preview{}, err
	}
	gh, err := github.New(o)
	if err != nil {
		return observation{}, previewapply.Preview{}, err
	}
	obs := observation{releases: map[string]bool{}}
	if obs.remote, err = repo.RemoteRefsPeeled(origin); err != nil {
		return obs, previewapply.Preview{}, err
	}
	if obs.local, err = repo.LocalTagRefs(); err != nil {
		return obs, previewapply.Preview{}, err
	}
	if err := gh.CheckInstalled(); err != nil {
		return obs, previewapply.Preview{}, fmt.Errorf("the reconcile judges the GitHub Releases too, and %w", err)
	}
	if err := gh.CheckAuth(); err != nil {
		return obs, previewapply.Preview{}, fmt.Errorf("the reconcile judges the GitHub Releases too, and %w", err)
	}
	tags, err := gh.ReleaseTags(r.repository)
	if err != nil {
		return obs, previewapply.Preview{}, err
	}
	for _, t := range tags {
		obs.releases[t] = true
	}
	items, err := r.judge(repo, obs, x, healed)
	if err != nil {
		return obs, previewapply.Preview{}, err
	}
	p, err := previewapply.NewPreview(items...)
	return obs, p, err
}

// judge produces one verdict per subject.
func (r *reconcileRun) judge(repo git.Repo, obs observation, x explanations, healed map[string]string) ([]previewapply.Item, error) {
	var items []previewapply.Item
	claimed := map[string]bool{}
	var unversioned, neverReleased, unrecoverable []string
	for _, u := range r.record.UnversionedTags() {
		claimed["refs/tags/"+u.Tag] = true
		unversioned = append(unversioned, u.Tag)
	}
	versions, err := releaserecord.ArchivedVersions(r.root, r.dir())
	if err != nil {
		return nil, err
	}
	semver.Sort(versions)
	for _, v := range versions {
		a, err := releaserecord.ReadArchive(r.root, r.dir(), v)
		if err != nil {
			return nil, fmt.Errorf("the archive of %s cannot be read (%w), so the refs it owns are unknown and the reconcile cannot say whether origin is right about them", v, err)
		}
		switch a.Fate {
		case releaserecord.FateNeverReleased:
			// The refs it would have owned are claimed without a verdict: no
			// release was published under it, so origin cannot be wrong about
			// them, and a tag of its name left unclaimed would trip the
			// reconcile forever.
			neverReleased = append(neverReleased, v.String())
			if expected, err := r.refs.expected(v, &a, true); err == nil {
				for _, tag := range expected.Spellings() {
					claimed["refs/tags/"+tag] = true
				}
			}
			continue
		case releaserecord.FateUnrecoverable:
			unrecoverable = append(unrecoverable, v.String())
			continue
		case releaserecord.FateUnstated:
			return nil, fmt.Errorf("the archive %s states no fate (no release commit, no unrecoverable marker, no never_released marker), so there is no commit its refs should point at. Backfill it, previewing first: `rlsbl release backfill --dry-run`, then `rlsbl release backfill --approve-consequential`, and run the reconcile again", a.Path)
		}
		commit := a.ReleaseCommit.Commit
		if moved, ok := healed[v.String()]; ok {
			commit = moved
		}
		expected, err := r.refs.expected(v, &a, true)
		if err != nil {
			return nil, fmt.Errorf("the refs of %s cannot be derived, so the reconcile cannot say whether origin is right about them: %w", v, err)
		}
		for _, tag := range expected.Tags() {
			ref := "refs/tags/" + tag
			claimed[ref] = true
			item, err := r.refVerdict(repo, obs, x, ref, tag, &v, commit, true, tag == expected.Primary)
			if err != nil {
				return nil, err
			}
			items = append(items, item)
		}
		if expected.SchemeSpelling != "" {
			// The current scheme's spelling of a version shipped under a
			// historical one is owed nowhere; where origin holds it, it is
			// still this version's ref, judged against the release commit.
			ref := "refs/tags/" + expected.SchemeSpelling
			claimed[ref] = true
			if _, onOrigin := obs.remote[ref]; onOrigin {
				item, err := r.refVerdict(repo, obs, x, ref, expected.SchemeSpelling, &v, commit, true, false)
				if err != nil {
					return nil, err
				}
				items = append(items, item)
			}
		}
		items = append(items, releaseVerdict(obs, expected.Primary, expected.SchemeSpelling, v, commit))
	}
	created, err := repo.TagCreationTimes()
	if err != nil {
		return nil, err
	}
	dirs, err := releaserecord.ArchiveDirs(r.root)
	if err != nil {
		return nil, err
	}
	tagExplanations, err := releaserecord.BuildExplanations(r.root, nil, dirs, r.record)
	if err != nil {
		return nil, err
	}
	for _, ref := range sortedKeys(obs.local) {
		if strings.HasSuffix(ref, git.PeeledSuffix) || claimed[ref] {
			continue
		}
		if _, onOrigin := obs.remote[ref]; !onOrigin {
			continue
		}
		tag, _ := tagName(ref)
		if when, ok := created[tag]; ok {
			explained, found, err := tagExplanations.Explain(tag, when)
			if err != nil {
				return nil, err
			}
			if found && explained.Source == releaserecord.SourceRetiredIdentity {
				// A dead identity's tags are accounted for by its record:
				// never judged, never refused.
				continue
			}
		}
		item, err := r.refVerdict(repo, obs, x, ref, tag, nil, "", false, false)
		if err != nil {
			return nil, err
		}
		if item.State != stateAlreadyCorrect {
			items = append(items, item)
		}
	}
	if len(unversioned) > 0 {
		r.say(fmt.Sprintf("Skipping %d tag(s) the lifecycle-and-license record keeps outside the version model (no release, so no verdict is owed): %s", len(unversioned), strings.Join(unversioned, ", ")))
	}
	if len(neverReleased) > 0 {
		r.say(fmt.Sprintf("Skipping %d version(s) recorded never released (no ref or Release is owed): %s", len(neverReleased), strings.Join(neverReleased, ", ")))
	}
	if len(unrecoverable) > 0 {
		r.say(fmt.Sprintf("Skipping %d version(s) recorded unrecoverable (no commit to reconcile against): %s", len(unrecoverable), strings.Join(unrecoverable, ", ")))
	}
	return items, nil
}

// refVerdict judges one ref. version is nil for a tag no archive of the
// releasable records; primary marks the version's primary tag, whose
// Release carries the marker.
func (r *reconcileRun) refVerdict(repo git.Repo, obs observation, x explanations, ref, tag string, version *semver.Version, commit string, archived, primary bool) (previewapply.Item, error) {
	remote, onOrigin := obs.remote[ref]
	remotePeeled := remote
	if p, ok := obs.remote[ref+git.PeeledSuffix]; ok {
		remotePeeled = p
	}
	local := obs.local[ref]
	localPeeled := local
	if p, ok := obs.local[ref+git.PeeledSuffix]; ok {
		localPeeled = p
	}
	fact := "no archived version"
	if version != nil {
		fact = "version " + version.String()
	}
	item := previewapply.Item{Key: ref}
	if archived && localPeeled != "" && commit != "" && !sameCommit(localPeeled, commit) {
		item.State = stateRefuseForeign
		item.Summary = "the local ref does not match the release record"
		item.Facts = []string{fact, "local:          " + localPeeled, "release record: " + commit}
		item.Detail = "  Pushing this ref would publish a commit the release record does not record as released.\n  Point the tag at the release commit, or correct the archive, before reconciling."
		return item, nil
	}
	target := local
	if target == "" && archived {
		target = commit
	}
	action := refAction{subject: subjectRef, ref: ref, tag: tag, target: target}
	if version != nil {
		action.version = *version
		action.hasVersion = true
	}
	if !onOrigin {
		if target == "" {
			item.State = stateRefuseForeign
			item.Summary = "recorded as released but present nowhere"
			item.Facts = []string{fact, "absent here and on origin"}
			item.Detail = "  There is no commit to create this ref at. Fetch the ref, or record the version unrecoverable."
			return item, nil
		}
		if version != nil {
			changed, err := r.identityChange(repo, x, commit)
			if err != nil {
				return item, err
			}
			if changed != nil {
				item.State = stateRefuseIdentity
				item.Summary = "a recorded identity change forbids recreating it"
				item.Facts = []string{fact, fmt.Sprintf("%s %q of %s, until %s", changed.Facet, changed.Value, changed.Subject, changed.Until.Format(time.DateOnly))}
				item.Detail = "  A Go tag is the published artifact: pushing this one would publish a version released under the old\n  identity under the current one, for the first time and permanently. Create the ref yourself if that is what you want."
				return item, nil
			}
		}
		action.createLocalTag = local == ""
		item.State = stateMaterialize
		item.Summary = "recorded as released, absent on origin"
		item.Facts = []string{fact, "would push " + target}
		item.Actions = []string{fmt.Sprintf("push %s -> %s", ref, short(target))}
		item.Data = action
		return item, nil
	}
	here := localPeeled
	if here == "" {
		here = target
	}
	if sameCommit(remotePeeled, here) {
		item.State = stateAlreadyCorrect
		item.Summary = "origin already holds this ref"
		item.Facts = []string{fact, "origin: " + remotePeeled}
		return item, nil
	}
	resolved, chain := x.resolve(remotePeeled)
	if sameCommit(resolved, here) {
		action.observed = remote
		action.repointMarker = primary && version != nil
		why := strings.Join(chain, ", ")
		item.State = stateRePoint
		item.Summary = "origin holds a commit a recorded rewrite moved"
		item.Facts = []string{fact, "origin: " + remotePeeled, "here:   " + here, "explained by: " + why}
		item.Actions = []string{fmt.Sprintf("force-push %s -> %s (lease %s)", ref, short(target), short(remote))}
		item.Data = action
		return item, nil
	}
	item.State = stateRefuseForeign
	item.Summary = "origin holds a commit no record explains"
	item.Facts = []string{fact, "origin: " + remotePeeled, "here:   " + here}
	item.Detail = "  No rewrite journal entry, release-commit remap, or committed history-rewrite archive maps the origin value to this one.\n  Force-pushing over it could destroy work that is not part of any recorded rewrite."
	return item, nil
}

// identityChange is the closed identity whose change forbids recreating a
// ref of the version released from commit, and nil when none does: a Go
// tag is the published artifact, so a version released while an identity
// that has since closed was in effect shipped under that identity, and
// pushing its tag now would publish it under the current one.
func (r *reconcileRun) identityChange(repo git.Repo, x explanations, commit string) (*lifecycle.Identity, error) {
	if !r.refs.goTarget || len(x.changedIdentities) == 0 || commit == "" {
		return nil, nil
	}
	released, err := repo.CommitterDate(commit)
	if err != nil {
		return nil, err
	}
	for i := range x.changedIdentities {
		id := x.changedIdentities[i]
		if released.Before(id.Until) {
			return &id, nil
		}
	}
	return nil, nil
}

// releaseVerdict judges one version's GitHub Release from the listing:
// present under its primary tag, or under the current scheme's spelling of
// a version that shipped under a historical one.
func releaseVerdict(obs observation, primary, schemeSpelling string, v semver.Version, commit string) previewapply.Item {
	key := releaseKeyPrefix + primary
	for _, tag := range []string{primary, schemeSpelling} {
		if tag != "" && obs.releases[tag] {
			facts := []string{"version " + v.String()}
			if tag != primary {
				facts = append(facts, "published under "+tag)
			}
			return previewapply.Item{Key: key, State: stateAlreadyCorrect, Summary: "the GitHub Release exists", Facts: facts}
		}
	}
	return previewapply.Item{
		Key:     key,
		State:   stateMaterialize,
		Summary: "released, but no GitHub Release exists for its tag",
		Facts:   []string{"version " + v.String(), "released from " + commit},
		Actions: []string{fmt.Sprintf("create the GitHub Release %s with the version's notes and its rlsbl-ci-sha marker", primary)},
		Data:    refAction{subject: subjectRelease, tag: primary, version: v, hasVersion: true, target: commit},
	}
}

func refusals(p previewapply.Preview) []previewapply.Item {
	var out []previewapply.Item
	for _, item := range p.Items() {
		if refusal(item.State) {
			out = append(out, item)
		}
	}
	return out
}

// tripwireError is the refusal of a preview holding a subject no record
// explains: nothing is written anywhere, the repairable subjects included.
func tripwireError(blocked []previewapply.Item) error {
	lines := []string{"refusing to reconcile: published refs are in a state no record explains.", ""}
	for _, item := range blocked {
		lines = append(lines, fmt.Sprintf("  %s: %s: %s", item.Key, item.StateLabel(), item.Summary))
		for _, f := range item.Facts {
			lines = append(lines, "    "+f)
		}
	}
	lines = append(lines, "", "Nothing has been changed, not the refused subjects and not the repairable ones: a reconcile that repaired around an unexplained divergence would be choosing which half of an inconsistent world to trust. Investigate the divergence, resolve it, then run the reconcile again.")
	return fmt.Errorf("%s", strings.Join(lines, "\n"))
}

// apply performs one subject's verdict.
func (r *reconcileRun) apply(e *strictcli.Effects, item previewapply.Item) error {
	action, ok := item.Data.(refAction)
	if !ok {
		return nil
	}
	repo, err := git.Open(e, r.root)
	if err != nil {
		return err
	}
	gh, err := github.New(e)
	if err != nil {
		return err
	}
	if action.subject == subjectRef {
		if action.createLocalTag {
			if err := repo.CreateRef(action.ref, action.target); err != nil {
				return err
			}
			r.say(fmt.Sprintf("Created the local tag %s at %s.", action.tag, short(action.target)))
		}
		if err := pushWithLease(repo, action.ref, action.observed, action.target, r.req.PushTimeout); err != nil {
			return err
		}
		r.say(fmt.Sprintf("Pushed %s -> %s.", action.ref, short(action.target)))
		if !action.repointMarker {
			return nil
		}
		// The Release follows the tag's name onto the moved commit, but its
		// body still names the old one in the marker the publish workflow
		// reads.
		doc, err := releasenotes.Read(r.root, r.req.Releasable.Name, r.refs.scheme, action.version)
		if err != nil {
			return err
		}
		if doc.ReleaseCommit == "" {
			return nil
		}
		wrote, err := releasenotes.EnsureMarker(gh, r.repository, doc)
		if err != nil {
			return fmt.Errorf("the tag %s was moved, but the rlsbl-ci-sha marker of its GitHub Release could not be rewritten (%w); the publish workflow reads that marker to learn which commit CI verified. Fix the cause and run the reconcile again", action.tag, err)
		}
		if wrote {
			r.say(fmt.Sprintf("Moved the rlsbl-ci-sha marker of the Release %s.", action.tag))
		}
		return nil
	}
	doc, err := releasenotes.Read(r.root, r.req.Releasable.Name, r.refs.scheme, action.version)
	if err != nil {
		return err
	}
	moves, err := releasenotes.RepairTakesLatest(gh, r.repository, doc, func(latest string) (bool, error) {
		return releasenotes.TagNewerInHistory(repo, doc.Tag, latest)
	})
	if err != nil {
		return err
	}
	if err := releasenotes.Create(gh, r.repository, doc, moves); err != nil {
		return err
	}
	r.say(fmt.Sprintf("Created the GitHub Release %s.", doc.Tag))
	return nil
}

// reconcilePlan is the plan file: the preview's output and the apply's only
// input.
type reconcilePlan struct {
	FormatVersion int64      `toml:"format_version"`
	GeneratedAt   string     `toml:"generated_at"`
	GeneratedBy   string     `toml:"generated_by"`
	WorldDigest   string     `toml:"world_digest"`
	Items         []planItem `toml:"items"`
}

type planItem struct {
	Key         string `toml:"key"`
	SubjectType string `toml:"subject_type"`
	State       string `toml:"state"`
	Version     string `toml:"version"`
	Target      string `toml:"target"`
	Observed    string `toml:"observed"`
	Summary     string `toml:"summary"`
}

// renderPlan writes the preview as the plan document.
func renderPlan(p previewapply.Preview, digest, version string, at time.Time) []byte {
	q := tomledit.QuoteString
	var b strings.Builder
	b.WriteString("# Written by `rlsbl release reconcile --mode plan` and performed by\n")
	b.WriteString("# `rlsbl release reconcile --mode apply`. Do not edit: the apply observes the\n")
	b.WriteString("# world again and refuses when it no longer matches world_digest.\n")
	fmt.Fprintf(&b, "format_version = %d\n", planFormatVersion)
	fmt.Fprintf(&b, "generated_at = %s\n", q(at.UTC().Format(time.RFC3339)))
	fmt.Fprintf(&b, "generated_by = %s\n", q(version))
	fmt.Fprintf(&b, "world_digest = %s\n", q(digest))
	items := p.Items()
	if len(items) == 0 {
		b.WriteString("items = []\n")
	}
	for _, item := range items {
		b.WriteString("\n[[items]]\n")
		fmt.Fprintf(&b, "key = %s\n", q(item.Key))
		subject := subjectRef
		if strings.HasPrefix(item.Key, releaseKeyPrefix) {
			subject = subjectRelease
		}
		action, isAction := item.Data.(refAction)
		if isAction {
			subject = action.subject
		}
		fmt.Fprintf(&b, "subject_type = %s\n", q(subject))
		fmt.Fprintf(&b, "state = %s\n", q(item.StateLabel()))
		if isAction && action.hasVersion {
			fmt.Fprintf(&b, "version = %s\n", q(action.version.String()))
		}
		if isAction && action.target != "" {
			fmt.Fprintf(&b, "target = %s\n", q(action.target))
		}
		if isAction && action.observed != "" {
			fmt.Fprintf(&b, "observed = %s\n", q(action.observed))
		}
		if item.Summary != "" {
			fmt.Fprintf(&b, "summary = %s\n", q(item.Summary))
		}
	}
	return []byte(b.String())
}

// readPlan reads and validates the releasable's plan file.
func readPlan(root, releasable string) (*reconcilePlan, error) {
	rel := runstate.ReconcilePlanPath(releasable)
	data, found, err := runstate.LoadReconcilePlan(root, releasable)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, fmt.Errorf("there is no reconcile plan at %s: `rlsbl release reconcile --mode apply` performs the plan `rlsbl release reconcile --mode plan` writes. Run the plan first, read what it proposes, then apply it", rel)
	}
	return parsePlan(rel, data)
}

func parsePlan(rel string, data []byte) (*reconcilePlan, error) {
	if _, diags := reconcileplanspec.ValidateBytes(data, "toml"); len(diags) > 0 {
		var problems []string
		for _, d := range diags {
			problems = append(problems, d.Message)
		}
		return nil, fmt.Errorf("the reconcile plan %s is not a valid plan document: %s. Run `rlsbl release reconcile --mode plan` to write it again", rel, strings.Join(problems, "; "))
	}
	plan, err := tomledit.Unmarshal[reconcilePlan](data)
	if err != nil {
		return nil, fmt.Errorf("the reconcile plan %s cannot be read: %v. Run `rlsbl release reconcile --mode plan` to write it again", rel, err)
	}
	return plan, nil
}

// checkPlanCovers refuses an apply whose fresh observation names work the
// plan does not: the plan's items are the consent, and its digest covers
// origin only, so a change here (a tag fetched, created, or moved) can grow
// the work while the digest still matches. It refuses a subject the plan
// does not name, a planned subject whose verdict changed, and one whose
// lease or target moved. It returns the planned repairs the fresh
// observation no longer names: they became correct on their own.
func checkPlanCovers(plan *reconcilePlan, p previewapply.Preview, rel string) ([]string, error) {
	planned := map[string]planItem{}
	for _, item := range plan.Items {
		planned[item.Key] = item
	}
	again := "Run `rlsbl release reconcile --mode plan`, read the new plan, and apply that."
	fresh := map[string]bool{}
	for _, item := range p.Items() {
		action, ok := item.Data.(refAction)
		if !ok {
			continue
		}
		fresh[item.Key] = true
		entry, ok := planned[item.Key]
		if !ok {
			return nil, fmt.Errorf("the world grew a subject %s does not name: %s (now %s: %s). The plan's items are the consent, and its digest covers origin only, so a tag brought here since the plan was written enlarges the apply without changing the digest. %s", rel, item.Key, item.StateLabel(), item.Summary, again)
		}
		if entry.State != item.StateLabel() {
			return nil, fmt.Errorf("the verdict of %s changed since %s was written (planned %s, now %s: %s); applying it would perform what the plan does not describe. %s", item.Key, rel, entry.State, item.StateLabel(), item.Summary, again)
		}
		if entry.Observed != action.observed {
			return nil, fmt.Errorf("the force-push lease of %s changed since %s was written (planned %s, now %s). %s", item.Key, rel, orAbsent(entry.Observed), orAbsent(action.observed), again)
		}
		if entry.Target != action.target {
			return nil, fmt.Errorf("the commit %s would be pushed to changed since %s was written (planned %s, now %s): the verdict is unchanged, so it moved here, and applying it would publish a commit the plan never named. %s", item.Key, rel, orAbsent(entry.Target), orAbsent(action.target), again)
		}
	}
	var noops []string
	for _, entry := range plan.Items {
		if (entry.State == "materialize" || entry.State == "re-point-with-lease") && !fresh[entry.Key] {
			noops = append(noops, entry.Key)
		}
	}
	return noops, nil
}
