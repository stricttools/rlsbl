package checks

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"unicode/utf8"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/strictspec/go/lifecycle/index"

	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The lifecycle family: the lifecycle-and-license record itself, the
// confidential names a public repository must not carry, and GitHub's
// visibility against what the record makes the repository. Each answers
// for the whole repository.
func lifecycleChecks() []check {
	return []check{
		repositoryWide(errorCheck("lifecycle-record-valid", checkLifecycleRecordValid)),
		repositoryWide(errorCheck("confidential-names", checkConfidentialNames)),
		repositoryWide(errorCheck("repository-visibility", checkRepositoryVisibility)),
	}
}

// declaredSubjects are every releasable and member name the declarations
// hold: the subjects an open period of the record may name.
func declaredSubjects(c *Context) []string {
	var names []string
	for _, r := range c.Declarations().Releasables {
		names = append(names, r.Name)
	}
	for _, m := range c.Declarations().Members {
		names = append(names, m.Name)
	}
	return names
}

// validationProblems are the problems err names, one each.
func validationProblems(err error) []string {
	var ve *lifecycle.ValidationError
	if errors.As(err, &ve) {
		out := make([]string, len(ve.Problems))
		for i, p := range ve.Problems {
			out[i] = lifecycle.RecordFile + ": " + p
		}
		return out
	}
	return []string{err.Error()}
}

func checkLifecycleRecordValid(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	if c.recErr != nil {
		return reportErrors(r, validationProblems(c.recErr), lifecycle.RecordFile+" is refused", "")
	}
	record := c.record
	if !record.Present() {
		message := fmt.Sprintf("this repository has no lifecycle-and-license record (%s), so no releasable has a license, a lifecycle, or an identity on record: declare each releasable's license with `rlsbl transition license --subject <releasable> --license <SPDX identifier> --reason <why>`, which writes it", lifecycle.RecordFile)
		return reportErrors(r, []string{message}, "no lifecycle-and-license record", "")
	}
	var problems []string
	if err := record.Validate(c.Now(), declaredSubjects(c)); err != nil {
		problems = append(problems, validationProblems(err)...)
	}
	compared, previous := appendOnlyProblems(c, record)
	problems = append(problems, previous...)
	passed := "the record is valid"
	if compared > 0 {
		passed += fmt.Sprintf(", and keeps everything the record at its %d nearest release commit(s) held", compared)
	}
	return reportErrors(r, problems, fmt.Sprintf("%d record problem(s)", len(problems)), passed)
}

// appendOnlyProblems holds the record against the record at the nearest
// release commit of every releasable: a closed period and a held registry
// name are never changed or dropped. compared counts the records found.
func appendOnlyProblems(c *Context, record *lifecycle.Record) (compared int, problems []string) {
	head, err := c.Repo().Head()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	seen := map[string]bool{}
	for _, rel := range c.Workspace().Releasables() {
		scheme, err := workspace.SchemeOf(rel)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		nearest, err := releaserecord.New(c.Repo(), rel.Name, scheme, c.Upstream()).Nearest(head)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if nearest == nil || seen[nearest.ReleaseCommit] {
			continue
		}
		seen[nearest.ReleaseCommit] = true
		text, found, err := c.Repo().FileAt(nearest.ReleaseCommit, lifecycle.RecordFile)
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if !found {
			continue
		}
		previous, err := lifecycle.Parse([]byte(text))
		if err != nil {
			problems = append(problems, fmt.Sprintf("the record at %s, the release commit of %s %s, cannot be read, so the record cannot be held to it: %v", nearest.ReleaseCommit, rel.Name, nearest.Version, err))
			continue
		}
		compared++
		if err := record.CheckAppendOnly(previous); err != nil {
			problems = append(problems, fmt.Sprintf("against the record at %s, the release commit of %s %s: %v", nearest.ReleaseCommit, rel.Name, nearest.Version, err))
		}
	}
	return compared, problems
}

func checkConfidentialNames(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	idx, err := index.Load(c.IndexPath())
	if err != nil {
		panic(unanswered(fmt.Sprintf("the confidential-name index cannot be read: %v", err)))
	}
	scanner, err := publishrules.NewScanner(c.Record(), c.Now(), idx)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if !scanner.Active() {
		return r.Skipped("the repository is confidential, and its names are its own to carry")
	}
	files, err := c.Repo().TrackedFiles()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	var texts []publishrules.Text
	for _, rel := range files {
		data, err := os.ReadFile(filepath.Join(c.Root(), filepath.FromSlash(rel)))
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if bytes.IndexByte(data, 0) >= 0 || !utf8.Valid(data) {
			continue
		}
		texts = append(texts, publishrules.Text{Name: rel, Content: string(data)})
	}
	if err := scanner.ScanTexts(texts); err != nil {
		return reportErrors(r, []string{err.Error()}, "tracked files carry confidential names", "")
	}
	return r.Passed(fmt.Sprintf("no tracked text file carries a confidential name (%d scanned)", len(texts)))
}

func checkRepositoryVisibility(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	repo, found, err := c.GitHubRepository()
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if !found {
		return r.Skipped(noGitHubRepository)
	}
	record := c.Record()
	if err := publishrules.CheckVisibility(record, publishrules.GitHubVisibility(c.GitHub(), repo), c.Now()); err != nil {
		return reportErrors(r, []string{err.Error()}, "the repository's visibility disagrees with the record", "")
	}
	if record.Confidential(c.Now()) {
		return r.Passed(fmt.Sprintf("%s is private, as its proprietary releasable requires", repo))
	}
	return r.Passed(fmt.Sprintf("%s is public, and no releasable is proprietary", repo))
}
