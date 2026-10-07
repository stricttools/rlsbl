package checks

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
)

// The changelog family: the releasable's JSONL changelog against the
// repository's history.
func changelogChecks() []check {
	return []check{
		errorCheck("changelog-format-version", checkChangelogFormatVersion),
		errorCheck("changelog-schema", checkChangelogSchema),
		errorCheck("changelog-entry", checkChangelogEntry),
		errorCheck("changelog-hashes", checkChangelogHashes),
		errorCheck("changelog-range", checkChangelogRange),
		errorCheck("changelog-coverage", checkChangelogCoverage),
		errorCheck("changelog-orphans", checkChangelogOrphans),
		warnCheck("changelog-user-facing", checkChangelogUserFacing),
		errorCheck("changelog-batch-commits", checkChangelogBatchCommits),
		errorCheck("changelog-batch-entries", checkChangelogBatchEntries),
	}
}

// noReleasable is the skip reason of a changelog check run for a member
// versioned under no releasable, which has no changelog.
func noReleasable(c *Context) string {
	return fmt.Sprintf("the member %q is versioned under no releasable, and only a releasable has a changelog", c.Member().Name)
}

// changelogFiles reads every file of the releasable's changelog, each on its
// own: the files that read, and the refused lines of the ones that do not.
func changelogFiles(c *Context, rel declarations.Releasable) ([]*changelog.File, []*changelog.LineError) {
	dir := changelog.Dir(rel.Name)
	versions, err := changelog.Versions(c.Root(), dir)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	var files []*changelog.File
	var refused []*changelog.LineError
	keep := func(f *changelog.File, err error) {
		var fe *changelog.FileError
		switch {
		case errors.As(err, &fe):
			refused = append(refused, fe.Lines...)
		case err != nil:
			panic(unanswered(err.Error()))
		default:
			files = append(files, f)
		}
	}
	keep(changelog.ReadUnreleased(c.Root(), dir))
	for _, v := range versions {
		keep(changelog.ReadVersion(c.Root(), dir, v))
	}
	return files, refused
}

// unreleasedFile reads the releasable's unreleased entries. A file with
// refused lines leaves the check unanswered: changelog-schema and
// changelog-format-version name them.
func unreleasedFile(c *Context, rel declarations.Releasable) *changelog.File {
	f, err := changelog.ReadUnreleased(c.Root(), changelog.Dir(rel.Name))
	if err != nil {
		panic(unanswered(fmt.Sprintf("the unreleased changelog cannot be read, so this check cannot be answered (changelog-schema and changelog-format-version name its refused lines): %v", err)))
	}
	return f
}

func checkChangelogFormatVersion(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	_, refused := changelogFiles(c, rel)
	var problems []string
	for _, l := range refused {
		if l.FormatVersion {
			problems = append(problems, l.Error())
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d changelog line(s) of another format version", len(problems)), fmt.Sprintf("every changelog line carries format_version %d", changelog.FormatVersion))
}

func checkChangelogSchema(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	_, refused := changelogFiles(c, rel)
	var problems []string
	for _, l := range refused {
		if !l.FormatVersion {
			problems = append(problems, l.Error())
		}
	}
	return reportErrors(r, problems, fmt.Sprintf("%d changelog line(s) refused by the schema", len(problems)), "every changelog entry passes the schema")
}

// currentVersion is the version the releasable stands at: its version file
// in a workspace, and its first target's version standalone.
func currentVersion(c *Context, rel declarations.Releasable) (semver.Version, error) {
	if c.Declarations().IsWorkspace() {
		return c.Workspace().ReadReleasableVersion(rel.Name)
	}
	ts := targetsOf(c, c.Member())
	if len(ts) == 0 {
		return semver.Version{}, fmt.Errorf("the member %q has no target to read the version from", c.Member().Name)
	}
	return ts[0].target.ReadVersion(ts[0].dir)
}

func checkChangelogEntry(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	version, err := currentVersion(c, rel)
	if err != nil {
		return r.Skipped(fmt.Sprintf("no version to look for: %v", err))
	}
	home := changelog.Home(c.Declarations(), rel.Name)
	data, err := os.ReadFile(filepath.Join(c.Root(), filepath.FromSlash(home)))
	if errors.Is(err, os.ErrNotExist) {
		return reportErrors(r, []string{fmt.Sprintf("%s does not exist; `rlsbl changelog generate` writes it from the changelog", home)}, home+" not found", "")
	}
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if _, found := changelog.ExtractSection(string(data), version.String()); !found {
		message := fmt.Sprintf("%s has no section for %s, the version %q stands at; `rlsbl changelog generate` writes it from the changelog", home, version, rel.Name)
		r.Warn(message)
		return r.Found(fmt.Sprintf("no section for %s", version))
	}
	return r.Passed(fmt.Sprintf("%s has a section for %s", home, version))
}

func checkChangelogHashes(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	s := subjectOf(c, rel)
	findings, err := s.HashFindings(unreleasedFile(c, rel))
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return reportErrors(r, findings, fmt.Sprintf("%d commit(s) do not resolve", len(findings)), "every commit of the unreleased entries resolves")
}

// subjectOf is the releasable's changelog subject.
func subjectOf(c *Context, rel declarations.Releasable) changelog.Subject {
	s, err := c.Subject(rel)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return s
}

// unreleasedRange is the releasable's unreleased range.
func unreleasedRange(s changelog.Subject) changelog.Range {
	rng, err := s.UnreleasedRange()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return rng
}

func checkChangelogRange(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	s := subjectOf(c, rel)
	findings, err := s.RangeFindings(unreleasedFile(c, rel), unreleasedRange(s))
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return reportErrors(r, findings, fmt.Sprintf("%d commit(s) outside the unreleased range or scope", len(findings)), "every commit of the unreleased entries is an unreleased commit of this releasable")
}

func checkChangelogCoverage(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	s := subjectOf(c, rel)
	coverage, err := s.CoverageOf(unreleasedFile(c, rel), unreleasedRange(s))
	if err != nil {
		panic(unanswered(err.Error()))
	}
	for _, note := range coverage.Notes() {
		r.Note(note)
	}
	findings := coverage.Findings()
	return reportErrors(r, findings, fmt.Sprintf("%d uncovered commit(s): describe each with `rlsbl changelog add --commits <commit> --description \"...\" --type <feature|fix|breaking>`", len(findings)), "every unreleased commit is covered")
}

func checkChangelogOrphans(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	s := subjectOf(c, rel)
	findings, err := s.OrphanFindings(unreleasedFile(c, rel), unreleasedRange(s))
	if err != nil {
		panic(unanswered(err.Error()))
	}
	return reportErrors(r, findings, fmt.Sprintf("%d orphaned entry(ies)", len(findings)), "no orphaned entries")
}

func checkChangelogUserFacing(c *Context, r *strictcli.WarnReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	if changelog.HasUserFacing(unreleasedFile(c, rel).Entries()) {
		return r.Passed("an unreleased entry is user-facing")
	}
	message := fmt.Sprintf("no unreleased entry of %q is user-facing, and every release but an infrastructure-only one needs one; a release with only infrastructure changes sets bump = \"infra\" in %s", rel.Name, releaserecord.ReleaseFilePath(rel.Name))
	return reportWarnings(r, []string{message}, "no user-facing entries", "")
}

func checkChangelogBatchCommits(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	findings := changelog.BatchCommitFindings(unreleasedFile(c, rel))
	return reportErrors(r, findings, fmt.Sprintf("%d entry(ies) over the limit of %d commits", len(findings), changelog.MaxCommitsPerEntry), "every entry is within the per-entry commit limit")
}

func checkChangelogBatchEntries(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	rel, ok := c.Releasable()
	if !ok {
		return r.Skipped(noReleasable(c))
	}
	files, err := changelog.ReadAll(c.Root(), changelog.Dir(rel.Name))
	if err != nil {
		panic(unanswered(fmt.Sprintf("the changelog cannot be read, so this check cannot be answered (changelog-schema and changelog-format-version name its refused lines): %v", err)))
	}
	findings := changelog.BatchEntryFindings(files)
	return reportErrors(r, findings, fmt.Sprintf("%d commit(s) in more than %d entries", len(findings), changelog.MaxEntriesPerCommit), "every commit is within the per-commit entry limit")
}
