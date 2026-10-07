package checks

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/pipelines"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/registry"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The release family: what lies outside the working tree that a release
// leaves behind or depends on: the refs and Releases of every archived
// release, the branch against its remote, the CI credentials, what a
// private repository must not publish, and the obligations closed
// identities leave. Every probe that cannot answer is an error, never a
// pass.
func releaseChecks() []check {
	return []check{
		errorCheck("unpublished-refs", checkUnpublishedRefs),
		errorCheck("branch-sync", checkBranchSync),
		errorCheck("ci-publish-secrets", checkCIPublishSecrets),
		errorCheck("npm-token-synced", checkNpmTokenSynced),
		errorCheck("private-repo-publishing", checkPrivateRepoPublishing),
		errorCheck("old-repo-archived", checkOldRepoArchived),
		errorCheck("go-deprecation-published", checkGoDeprecationPublished),
	}
}

// reconcileFix repairs every finding of unpublished-refs.
const reconcileFix = "Repair the release metadata with `rlsbl release reconcile --mode plan`, then `--mode apply`."

// sameCommit reports whether two commit ids name one commit, allowing one to
// be abbreviated.
func sameCommit(a, b string) bool {
	n := min(len(a), len(b))
	return n > 0 && a[:n] == b[:n]
}

// refMembers are the releasable's members as the companion tags see them.
func refMembers(c *Context, rel declarations.Releasable) []targets.RefMember {
	var out []targets.RefMember
	for _, m := range c.Workspace().MembersOf(rel.Name) {
		declared, err := targets.MemberTargets(c.Root(), m)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		out = append(out, targets.RefMember{Path: m.Path, Targets: declared, Publishes: rel.PublishMode != declarations.PublishNone})
	}
	return out
}

// boundaryAliases maps a tag to the alias tags the transition record's
// boundary-alias events of the releasable created for it.
func boundaryAliases(c *Context, rel declarations.Releasable) map[string][]string {
	events, err := releaserecord.ReadEvents(c.Root())
	if err != nil {
		panic(unanswered(err.Error()))
	}
	out := map[string][]string{}
	for _, e := range events {
		alias, ok := e.(*releaserecord.BoundaryAliasEvent)
		if !ok || (alias.Releasable != "" && alias.Releasable != rel.Name) {
			continue
		}
		for _, a := range alias.Aliases {
			out[a.AliasedTag] = append(out[a.AliasedTag], a.AliasTag)
		}
	}
	return out
}

// refTally counts what unpublished-refs found besides its errors.
type refTally struct {
	neverReleased      []string
	unrecoverableRefs  int
	unrecoverableNotes int
	releasesListed     bool
}

func checkUnpublishedRefs(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	scheme, err := workspace.SchemeOf(rel)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	dir := releaserecord.ArchiveDir(rel.Name)
	versions, err := releaserecord.ArchivedVersions(c.Root(), dir)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if len(versions) == 0 {
		return r.Skipped(fmt.Sprintf("the release record of %q records no release", rel.Name))
	}
	local, err := c.Repo().TagCommits()
	if err != nil {
		return reportErrors(r, []string{fmt.Sprintf("the local tags could not be read (%v), so no released version's refs could be verified. %s", err, reconcileFix)}, "the local tags could not be read", "")
	}
	hasRemote, err := c.Repo().RemoteConfigured("origin")
	if err != nil {
		panic(unanswered(err.Error()))
	}
	var remote map[string]string
	if hasRemote {
		if remote, err = c.Repo().RemoteTagCommits("origin"); err != nil {
			return reportErrors(r, []string{fmt.Sprintf("the tags of origin could not be read (%v), so no released version's refs could be verified against it; an unanswered probe is not an answer", err)}, "origin's tags could not be read", "")
		}
	}
	var releases map[string]bool
	if hasRemote {
		repo, found, err := c.GitHubRepository()
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if found {
			tags, err := c.GitHub().ReleaseTags(repo)
			if err != nil {
				return reportErrors(r, []string{fmt.Sprintf("%v, so no released version's GitHub Release could be verified; an unanswered probe is not an answer", err)}, "the GitHub Releases could not be listed", "")
			}
			releases = map[string]bool{}
			for _, t := range tags {
				releases[t] = true
			}
		}
	}
	members := refMembers(c, rel)
	aliases := boundaryAliases(c, rel)
	var problems []string
	tally := refTally{releasesListed: releases != nil}
	for _, v := range versions {
		problems = append(problems, versionRefProblems(c, rel, scheme, dir, v, members, aliases, local, remote, releases, &tally)...)
	}
	if len(problems) > 0 {
		return reportErrors(r, problems, fmt.Sprintf("%d ref or Release problem(s) of archived releases of %q", len(problems), rel.Name), "")
	}
	released := len(versions) - len(tally.neverReleased)
	subjects := "every ref"
	if tally.releasesListed {
		subjects = "every ref and GitHub Release"
	}
	scope := fmt.Sprintf("%d released version(s)", released)
	if !hasRemote {
		scope += ", locally (no origin remote)"
	}
	if len(tally.neverReleased) > 0 {
		scope += fmt.Sprintf("; %d archived version(s) recorded never released, which own no refs: %s", len(tally.neverReleased), strings.Join(tally.neverReleased, ", "))
	}
	if tally.unrecoverableRefs > 0 {
		scope += fmt.Sprintf("; %d ref(s) absent for versions recorded unrecoverable, which have no commit to recreate them at", tally.unrecoverableRefs)
	}
	if tally.unrecoverableNotes > 0 {
		scope += fmt.Sprintf("; %d GitHub Release(s) absent for versions recorded unrecoverable", tally.unrecoverableNotes)
	}
	return r.Passed(fmt.Sprintf("%s exists for %s", subjects, scope))
}

// versionRefProblems are the problems of one archived version's refs and
// GitHub Release.
func versionRefProblems(c *Context, rel declarations.Releasable, scheme workspace.TagScheme, dir string, v semver.Version, members []targets.RefMember, aliases map[string][]string, local, remote map[string]string, releases map[string]bool, tally *refTally) []string {
	archive, err := releaserecord.ReadArchive(c.Root(), dir, v)
	if err != nil {
		return []string{fmt.Sprintf("%s: its release archive could not be read (%v), so the refs it owns are unknown", v, err)}
	}
	switch archive.Fate {
	case releaserecord.FateNeverReleased:
		tally.neverReleased = append(tally.neverReleased, v.String())
		return nil
	case releaserecord.FateUnstated:
		return []string{fmt.Sprintf("%s: its release archive %s states no fate, so whether it owns refs is unknown; record it with `rlsbl release backfill`", v, archive.Path)}
	}
	releaseCommit := ""
	if archive.Fate == releaserecord.FateRecorded {
		releaseCommit = archive.ReleaseCommit.Commit
	}
	var recorded []string
	for p := range archive.ReleaseCommit.Trees {
		recorded = append(recorded, p)
	}
	sort.Strings(recorded)
	schemeTag := scheme.Render(v)
	primary := schemeTag
	if archive.ShippedAs != "" {
		primary = archive.ShippedAs
	}
	expected, err := targets.ExpectedRefsOf(v, targets.RefInputs{
		SchemeTag:     schemeTag,
		Members:       members,
		RecordedPaths: recorded,
		ShippedAs:     archive.ShippedAs,
		Aliases:       aliases[primary],
	})
	if err != nil {
		return []string{fmt.Sprintf("%s: the refs this version owns could not be derived (%v)", v, err)}
	}
	var problems []string
	for _, ref := range expected.Tags() {
		at, found := local[ref]
		if !found {
			if releaseCommit == "" {
				tally.unrecoverableRefs++
				continue
			}
			problems = append(problems, fmt.Sprintf("%s: the ref %s is recorded as released but does not exist locally. %s", v, ref, reconcileFix))
			continue
		}
		if releaseCommit != "" && !sameCommit(at, releaseCommit) {
			problems = append(problems, fmt.Sprintf("%s: the ref %s points at %s, but the release recorded %s; the archive is the record the release wrote, so the ref moved. %s", v, ref, at, releaseCommit, reconcileFix))
		}
		if remote == nil {
			continue
		}
		if _, onRemote := remote[ref]; !onRemote {
			problems = append(problems, fmt.Sprintf("%s: the ref %s exists locally but not on origin, so the release is not published where consumers resolve it. %s", v, ref, reconcileFix))
		}
	}
	if releases == nil || remote == nil {
		return problems
	}
	if _, onRemote := remote[expected.Primary]; !onRemote {
		return problems
	}
	if releases[expected.Primary] || (expected.SchemeSpelling != "" && releases[expected.SchemeSpelling]) {
		return problems
	}
	if releaseCommit == "" {
		tally.unrecoverableNotes++
		return problems
	}
	return append(problems, fmt.Sprintf("%s: the tag %s is on origin but carries no GitHub Release, so consumers see no notes for this version and the publish workflow finds no released-commit marker. %s", v, expected.Primary, reconcileFix))
}

func checkBranchSync(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	branch, err := c.Repo().CurrentBranch()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	tracking := "origin/" + branch
	if _, found, err := c.Repo().RemoteTrackingCommit("origin", branch); err != nil {
		panic(unanswered(err.Error()))
	} else if !found {
		return r.Skipped("no remote-tracking branch " + tracking)
	}
	ref := "refs/remotes/" + tracking
	behind, err := c.Repo().CountCommits([]string{ref}, []string{"HEAD"})
	if err != nil {
		panic(unanswered(err.Error()))
	}
	ahead, err := c.Repo().CountCommits([]string{"HEAD"}, []string{ref})
	if err != nil {
		panic(unanswered(err.Error()))
	}
	switch {
	case behind == 0 && ahead == 0:
		return r.Passed("up to date with " + tracking)
	case behind == 0:
		message := fmt.Sprintf("%d commit(s) ahead of %s", ahead, tracking)
		r.Warn(message)
		return r.Found(message)
	case ahead == 0:
		message := fmt.Sprintf("%d commit(s) behind %s: bring the branch up to date (`git pull --ff-only`) before releasing", behind, tracking)
		return reportErrors(r, []string{message}, message, "")
	}
	message := fmt.Sprintf("%d commit(s) behind and %d ahead of %s: the branch and its remote diverged; reconcile them before releasing", behind, ahead, tracking)
	return reportErrors(r, []string{message}, message, "")
}

// ciSecrets maps each Actions secret the releasable's CI publish pipelines
// authenticate with to the pipelines that need it.
func ciSecrets(c *Context, rel declarations.Releasable) map[string][]string {
	out := map[string][]string{}
	for _, m := range c.Workspace().MembersOf(rel.Name) {
		for _, p := range m.Pipelines {
			names, err := pipelines.CISecretNames(p)
			if err != nil {
				panic(unanswered(err.Error()))
			}
			for _, n := range names {
				out[n] = append(out[n], m.Name+"/"+p.Name)
			}
		}
	}
	return out
}

// ciPublishingReleasable is the releasable a CI credential check answers
// for, or a skip reason when nothing it declares publishes from CI.
func ciPublishingReleasable(c *Context) (declarations.Releasable, string) {
	rel, ok := c.Releasable()
	if !ok {
		return rel, noReleasable(c)
	}
	if rel.PublishMode != declarations.PublishCI {
		return rel, fmt.Sprintf("the publish_mode of %q is %q, so CI publishes nothing", rel.Name, rel.PublishMode)
	}
	return rel, ""
}

// secretSetCommand sets an Actions secret from the local credential: the
// npm token in ~/.npmrc for NPM_TOKEN, a value the operator supplies for
// any other.
func secretSetCommand(repo github.Repository, secret string) string {
	if secret == registry.NpmTokenSecret {
		return fmt.Sprintf("gh secret set %s --repo %s --body \"$(grep _authToken ~/.npmrc | cut -d= -f2)\"", secret, repo)
	}
	return fmt.Sprintf("gh secret set %s --repo %s", secret, repo)
}

func checkCIPublishSecrets(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, skip := ciPublishingReleasable(c)
	if skip != "" {
		return r.Skipped(skip)
	}
	required := ciSecrets(c, rel)
	if len(required) == 0 {
		return r.Skipped(fmt.Sprintf("no pipeline of %q authenticates its CI publish with a repository secret", rel.Name))
	}
	repo, found, err := c.GitHubRepository()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if !found {
		return r.Skipped(noGitHubRepository)
	}
	names := make([]string, 0, len(required))
	for n := range required {
		names = append(names, n)
	}
	sort.Strings(names)
	var problems, notes []string
	client := c.GitHub()
	for _, name := range names {
		users := strings.Join(required[name], ", ")
		secret, err := client.Secret(repo, name)
		switch {
		case err != nil:
			problems = append(problems, fmt.Sprintf("%s: could not determine whether the %s secret exists (%v); an unanswered probe is not an answer, so fix the credential or the connection and run the check again", repo, name, err))
		case !secret.Present:
			problems = append(problems, fmt.Sprintf("%s has no %s secret, but the pipeline(s) %s authenticate their CI publish with it, so the publish job would fail after the release tagged and pushed. Set it: %s", repo, name, users, secretSetCommand(repo, name)))
		default:
			notes = append(notes, fmt.Sprintf("%s carries %s for %s", repo, name, users))
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d CI secret problem(s)", len(problems)), strings.Join(notes, "; "))
}

func checkNpmTokenSynced(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, skip := ciPublishingReleasable(c)
	if skip != "" {
		return r.Skipped(skip)
	}
	if _, ok := ciSecrets(c, rel)[registry.NpmTokenSecret]; !ok {
		return r.Skipped(fmt.Sprintf("no CI publish pipeline of %q authenticates with %s", rel.Name, registry.NpmTokenSecret))
	}
	repo, found, err := c.GitHubRepository()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if !found {
		return r.Skipped(noGitHubRepository)
	}
	verdict := c.Registry().EvaluateNpmTokenSync(c.Effects(), c.GitHub(), registry.NpmrcPath(c.Home()), repo)
	return reportErrors(r, verdict.Problems, fmt.Sprintf("%d npm token problem(s)", len(verdict.Problems)), strings.Join(verdict.Notes, "; "))
}

// unknownVisibility is the visibility source of a repository that names no
// GitHub repository to ask.
type unknownVisibility struct{ reason string }

func (u unknownVisibility) Visibility() (lifecycle.Visibility, error) {
	return lifecycle.VisibilityUnknown, errors.New(u.reason)
}

// visibilitySource asks GitHub for the repository's visibility, once, when a
// rule's answer turns on it.
func visibilitySource(c *Context) publishrules.VisibilitySource {
	repo, found, err := c.GitHubRepository()
	if err != nil {
		return unknownVisibility{reason: err.Error()}
	}
	if !found {
		return unknownVisibility{reason: noGitHubRepository}
	}
	return publishrules.GitHubVisibility(c.GitHub(), repo)
}

func checkPrivateRepoPublishing(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	uses, err := publishrules.ReleasableUses(c.Workspace(), rel.Name)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	files, err := publishrules.WorkingTreeWorkflows(c.Root())
	if err != nil {
		panic(unanswered(err.Error()))
	}
	workflowUses, err := publishrules.WorkflowUses(files)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	uses = append(uses, workflowUses...)
	if len(uses) == 0 {
		return r.Skipped(fmt.Sprintf("neither %q nor a committed workflow publishes anything", rel.Name))
	}
	err = publishrules.CheckUses(c.Record(), uses, visibilitySource(c), c.Now())
	var refusals *publishrules.Refusals
	switch {
	case errors.As(err, &refusals):
		return reportErrors(r, refusals.Items, fmt.Sprintf("%d publishing output(s) refused", len(refusals.Items)), "")
	case err != nil:
		return reportErrors(r, []string{err.Error()}, firstLine(err.Error()), "")
	}
	return r.Passed(fmt.Sprintf("every publishing output of %q is allowed (%d checked)", rel.Name, len(uses)))
}

// reportFollowup finishes a check from a follow-up verdict.
func reportFollowup(r *strictcli.ErrorReporter, v releaserecord.FollowupVerdict, passed string) strictcli.CheckOutcome {
	if v.SkipReason != "" {
		return r.Skipped(v.SkipReason)
	}
	if len(v.Notes) > 0 {
		passed = strings.Join(v.Notes[:min(3, len(v.Notes))], "; ")
	}
	return reportErrors(r, v.Problems, fmt.Sprintf("%d finding(s)", len(v.Problems)), passed)
}

func checkOldRepoArchived(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	client := c.GitHub()
	verdict := releaserecord.EvaluateOldRepositoryArchived(c.Record().Identities(), func(repo github.Repository) (bool, error) {
		info, err := client.Info(repo)
		if err != nil {
			return false, err
		}
		return info.Archived, nil
	})
	return reportFollowup(r, verdict, "every earlier repository is archived")
}

func checkGoDeprecationPublished(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	client := c.Registry()
	verdict := releaserecord.EvaluateGoDeprecationPublished(c.Record().Identities(), func(module string) (releaserecord.DeprecationAnswer, error) {
		dep, found, err := client.GoModuleDeprecation(module)
		switch {
		case err != nil:
			return releaserecord.DeprecationAnswer{}, err
		case !found:
			return releaserecord.DeprecationAnswer{Status: releaserecord.NeverPublished}, nil
		case dep.Deprecated:
			return releaserecord.DeprecationAnswer{Status: releaserecord.Deprecated, Version: dep.Version}, nil
		}
		return releaserecord.DeprecationAnswer{Status: releaserecord.NotDeprecated, Version: dep.Version}, nil
	})
	return reportFollowup(r, verdict, "every superseded module path is deprecated")
}
