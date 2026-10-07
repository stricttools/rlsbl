package cli

import (
	"fmt"
	"path/filepath"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/historyrewrite"
	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/workspace"
)

var releaseScrubHelp = fmt.Sprintf("Scrub content from the repository's git history through safegit, then repair every record the rewrite renamed and "+
	"publish the result. --pattern rewrites every match of a regular expression (each replaced by --replace, or by random text of the same "+
	"length under --mangle), --file rewrites one repository-relative file (every past version of it replaced by the copy on disk now, or removed "+
	"from every commit when there is no copy on disk), and --recipe runs a safegit scrub recipe; --from-commit or --entire-history says how much "+
	"of the history is rewritten. safegit remaps the changelog's commit ids at every rewritten commit; the scrub then requires every changelog "+
	"commit id to name a commit (repairing ids from safegit's rewrite journal where it can), moves each archive's release commit through the "+
	"rewrite (recording the rewritten trees, and a release-commit remap in the transition record), requires every generated changelog to be what "+
	"generating it gives, deletes the changelog validation caches, writes the rewrite's archive to .strictmetadata/history-rewrites/<UTC "+
	"time>.toml (commit ids, tags, the mode, and --reason only, never what was removed), and commits. It then force-pushes the branch and every "+
	"moved tag, each guarded by the value origin held before the rewrite, and rewrites each moved tag's GitHub Release in place from the record "+
	"(creating one a tag lacks); a Release is never deleted. A scrub that stops is finished by running the same command again, from the step "+
	"that stopped it, with its state kept in .strictmetadata/.release-state/scrub-result.json. Requires safegit %s or newer, whose --json "+
	"machine-mode document is read at interface_version %d, and a release branch. Refused while a release is stopped mid-flight. --dry-run records the safegit "+
	"invocation, which prints safegit's own counts, and writes nothing.", historyrewrite.SafegitMinimum, historyrewrite.SafegitInterfaceVersion)

const releaseReconcileHelp = "Reconcile one releasable's published release metadata with what its records say it released: push the refs origin " +
	"lacks, force-push the ones a recorded rewrite moved, and create the GitHub Releases that are absent. Every archived version's refs (its " +
	"primary tag, its companion tags, its recorded aliases) and its GitHub Release are judged, and so is every local tag origin holds that no " +
	"archive claims: materialize, already-correct, re-point-with-lease (origin holds a commit safegit's rewrite journal, a release-commit remap " +
	"in the transition record, or a committed history-rewrite archive maps to this one), refuse-foreign (nothing explains what origin holds), or " +
	"refuse-identity-mismatch (a Go tag of a version released under a module path or repository the lifecycle-and-license record has since " +
	"closed). Tags the lifecycle-and-license record keeps outside the version model, and tags a closed identity owned, are not judged. One " +
	"refusal aborts the whole reconcile, and nothing anywhere is written. Archives naming a commit the repository no longer has are first moved " +
	"through the records that explain the move, and committed; one nothing explains is refused. --mode plan writes the plan to " +
	".strictmetadata/.release-state/<releasable>/reconcile-plan.toml (an empty plan included); --mode apply observes again, refuses when origin " +
	"or the Release listing moved since the plan, or when the observation names work the plan does not, and performs the plan. --releasable " +
	"names the releasable where the working directory selects none, and is refused where it selects one."

const releaseBackfillHelp = "Bring every releasable's release archives into the fate model from the repository's own history: record each " +
	"version's release commit from its tag (or the spelling its archive names in shipped_as, or its version-bump commit), complete an archive " +
	"whose required fields are missing or blank, materialize an archive for a released version that has none, and adopt a version tag no record " +
	"names as the release it is evidence of. A version with no tag and no version-bump commit is recorded unrecoverable; a version never " +
	"released is declared by writing its archive with never_released = true first, which the backfill then leaves alone. A reconstructed " +
	"description comes from the first source that yields one: --overrides (one [versions.\"X.Y.Z\"] table per version, with description and an " +
	"optional context), the version's GitHub Release body, its CHANGELOG.md section, the commit subjects in its tag range, and otherwise a " +
	"placeholder naming the obligation; each written field names its source. Every tag nothing accounts for (no archive or changelog file, no " +
	"unversioned tag or closed identity in the lifecycle-and-license record) is listed first and refuses the whole apply, as does a stash. " +
	"--dry-run prints the plan and writes nothing, exiting 1 when an unexplained tag would refuse the apply."

// The scrub's selectors. Each choice is spelled as the flag that elects it,
// so the argv is --pattern <re> or --file <path> or --recipe <path>, and
// --from-commit <commit> or --entire-history.
var (
	scrubReplace = strictcli.MemberChoice(
		strictcli.StringFlag("replace", "The literal text each match is replaced with", strictcli.Required()),
		"Replace each match with a literal text")
	scrubMangle = strictcli.MemberChoice(
		strictcli.BoolFlag("mangle", "Replace each match with random printable text of the same length", strictcli.Required()),
		"Replace each match with random text of the same length")
	scrubPattern = strictcli.MemberChoice(
		strictcli.StringFlag("pattern", "The regular expression whose every match is rewritten, in file contents, commit messages, and tag annotations", strictcli.Required()),
		"Pattern mode: rewrite every match of a regular expression in the chosen range",
		strictcli.MemberChoiceFlag("substitution", "What replaces each match", strictcli.Required(), scrubReplace, scrubMangle),
	)
	scrubFile = strictcli.MemberChoice(
		strictcli.StringFlag("file", "The repository-relative file to rewrite: every past version of it is replaced by the copy on disk now, or the file is removed from every commit when there is no copy on disk", strictcli.Required()),
		"File mode: rewrite one file throughout the chosen range")
	scrubRecipe = strictcli.MemberChoice(
		strictcli.StringFlag("recipe", "The path of a safegit scrub recipe, whose operations hold their own patterns and replacements", strictcli.Required()),
		"Recipe mode: run a safegit scrub recipe in one pass")
	scrubFromCommit = strictcli.MemberChoice(
		strictcli.StringFlag("from-commit", "The earliest commit rewritten; every commit before it keeps its id", strictcli.Required()),
		"Rewrite the commits from one commit onward")
	scrubEntireHistory = strictcli.MemberChoice(
		strictcli.BoolFlag("entire-history", "Rewrite every commit from the repository's first", strictcli.Required()),
		"Rewrite the whole history")

	reconcilePlanChoice  = strictcli.Choice("plan", "Observe origin and write the plan, writing nothing to origin")
	reconcileApplyChoice = strictcli.Choice("apply", "Observe again and perform the plan the plan half wrote")
)

func registerHistoryRewrites(r *commandSet, version string) {
	r.add(command{
		path:   []string{"release", "scrub"},
		help:   releaseScrubHelp,
		effect: mutating,
		// An irreversible history rewrite, force-pushed: only a person decides
		// that what was published is not what the history will say.
		consequential: true,
		flags: []strictcli.Flag{
			strictcli.MemberChoiceFlag("mode", "Which scrub runs", strictcli.Required(), scrubPattern, scrubFile, scrubRecipe),
			strictcli.MemberChoiceFlag("commit-range", "Which commits the rewrite covers", strictcli.Required(), scrubFromCommit, scrubEntireHistory),
			strictcli.StringFlag("reason", "Why the content is scrubbed; recorded in the scrub commit and in the rewrite's archive", strictcli.Required()),
		},
		run: runReleaseScrub,
	})
	r.add(command{
		path:   []string{"release", "reconcile"},
		help:   releaseReconcileHelp,
		effect: mutating,
		// Force-pushes tags and creates GitHub Releases. Consent is for running
		// the command, so the plan half asks too.
		consequential: true,
		flags: []strictcli.Flag{
			strictcli.ChoiceFlag("mode", "Which half of the reconcile runs", strictcli.Required(), reconcilePlanChoice, reconcileApplyChoice),
			strictcli.IntFlag("push-timeout", timeoutHelp("Seconds each ref push may take", "push_seconds", release.ShippedPushTimeout), strictcli.Optional()),
			strictcli.StringFlag("releasable", "The releasable to reconcile, where the working directory selects none", strictcli.Optional()),
		},
		run: func(ctx *strictcli.Context, kw map[string]any) (any, error) {
			return nil, runReleaseReconcile(ctx, kw, version)
		},
	})
	r.add(command{
		path:   []string{"release", "backfill"},
		help:   releaseBackfillHelp,
		effect: mutating,
		// Only a person decides to reconstruct a repository's release history:
		// the backfill derives what each version shipped from and writes it
		// into the archives every later read treats as authoritative.
		consequential: true,
		flags: []strictcli.Flag{
			strictcli.StringFlag("overrides", "A TOML file of reviewed descriptions, one [versions.\"X.Y.Z\"] table per version with a description and an optional context", strictcli.Optional()),
			strictcli.BoolFlag("auto-commit", "Commit the written archives with the Autogenerated trailer (committed when neither --auto-commit nor --no-auto-commit is passed)", strictcli.Optional()),
		},
		run: runReleaseBackfill,
	})
}

func runReleaseScrub(ctx *strictcli.Context, kw map[string]any) (any, error) {
	dir, root, err := workingRepository()
	if err != nil {
		return nil, err
	}
	req := historyrewrite.ScrubRequest{Reason: strictcli.Get[string](kw, "reason")}
	mode := strictcli.GetElected(kw, "mode")
	switch {
	case mode.Is(scrubPattern):
		req.Mode = historyrewrite.ModePattern
		req.Pattern = strictcli.Get[string](mode.Fields, "value")
		substitution := strictcli.GetElected(mode.Fields, "substitution")
		if substitution.Is(scrubMangle) {
			req.Mangle = true
		} else {
			req.Replace = strictcli.Get[string](substitution.Fields, "value")
			if req.Replace == "" {
				return nil, emptyArgument("--replace")
			}
		}
	case mode.Is(scrubFile):
		req.Mode = historyrewrite.ModeFile
		req.File = filepath.ToSlash(filepath.Clean(strictcli.Get[string](mode.Fields, "value")))
	case mode.Is(scrubRecipe):
		req.Mode = historyrewrite.ModeRecipe
		recipe := strictcli.Get[string](mode.Fields, "value")
		if recipe == "" {
			return nil, emptyArgument("--recipe")
		}
		// safegit runs at the repository root, so the recipe is named
		// absolutely.
		if !filepath.IsAbs(recipe) {
			recipe = filepath.Join(dir, recipe)
		}
		req.Recipe = recipe
	}
	commits := strictcli.GetElected(kw, "commit_range")
	if commits.Is(scrubFromCommit) {
		req.FromCommit = strictcli.Get[string](commits.Fields, "value")
		if req.FromCommit == "" {
			return nil, emptyArgument("--from-commit")
		}
	} else {
		req.EntireHistory = true
	}
	return nil, historyrewrite.Scrub(ctx, root, req, time.Now)
}

func runReleaseReconcile(ctx *strictcli.Context, kw map[string]any, version string) error {
	dir, root, err := workingRepository()
	if err != nil {
		return err
	}
	ws, err := workspace.Load(root)
	if err != nil {
		return err
	}
	named, given := strictcli.GetOpt[string](kw, "releasable")
	if given && named == "" {
		return emptyArgument("--releasable")
	}
	releasable, err := historyrewrite.SelectReleasable(ws, dir, named)
	if err != nil {
		return err
	}
	seconds, timed := strictcli.GetOpt[int](kw, "push_timeout")
	timeout, err := historyrewrite.PushTimeout(ws.Declarations, seconds, timed)
	if err != nil {
		return err
	}
	mode := historyrewrite.ReconcilePlan
	if strictcli.GetElected(kw, "mode").Is(reconcileApplyChoice) {
		mode = historyrewrite.ReconcileApply
	}
	return historyrewrite.Reconcile(ctx, root, historyrewrite.ReconcileRequest{
		Mode:        mode,
		Releasable:  releasable,
		PushTimeout: timeout,
		Version:     version,
	}, time.Now)
}

func runReleaseBackfill(ctx *strictcli.Context, kw map[string]any) (any, error) {
	dir, root, err := workingRepository()
	if err != nil {
		return nil, err
	}
	overrides, given := strictcli.GetOpt[string](kw, "overrides")
	if given && overrides == "" {
		return nil, emptyArgument("--overrides")
	}
	if given && !filepath.IsAbs(overrides) {
		overrides = filepath.Join(dir, overrides)
	}
	return nil, historyrewrite.Backfill(ctx, root, historyrewrite.BackfillRequest{
		Overrides:  overrides,
		AutoCommit: optionalBool(kw, "auto_commit", true),
	})
}
