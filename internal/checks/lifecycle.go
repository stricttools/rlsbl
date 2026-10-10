package checks

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/confidential"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The lifecycle family: the lifecycle-and-license record itself, the
// confidential terms a public repository must not publish unresolved, and GitHub's
// visibility against what the record makes the repository. Each answers
// for the whole repository.
func lifecycleChecks() []check {
	return []check{
		repositoryWide(errorCheck("lifecycle-record-valid", checkLifecycleRecordValid)),
		repositoryWide(errorCheck("confidential-terms", checkConfidentialTerms)),
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

func checkConfidentialTerms(c *Context, r *strictcli.ErrorReporter) strictcli.CheckOutcome {
	scanner, hits, what, err := ConfidentialHits(c)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if !scanner.Active() {
		return r.Skipped("the repository is confidential, so its pushes stay private and are not scanned")
	}
	o := scanner.Resolve(hits)
	if resolved := o.ReportResolved(); resolved != "" {
		r.Note("hits the repository's resolutions resolve:\n" + strings.TrimRight(resolved, "\n"))
	}
	if o.Clear() {
		return r.Passed(fmt.Sprintf("%s carries no unresolved confidential-term hit (%d resolved); %s", what, len(o.Resolved), scanner.Status()))
	}
	r.Note(publishrules.ResolveFix)
	return reportErrors(r, o.UnresolvedReports(), fmt.Sprintf("%s carries %d unresolved confidential-term hit(s)", what, len(o.Unresolved)), "")
}

// ConfidentialHits are the confidential-term hits of the next push of the
// run's branch (see pushRange and publishrules.Scanner.HitsInRange), with the
// scanner that found them and the range's name: what a release would push
// before its own commits, and what the confidential commands resolve hits
// of.
func ConfidentialHits(c *Context) (*publishrules.Scanner, []confidential.Hit, string, error) {
	names, err := publishrules.RepositoryNames(c.Repo())
	if err != nil {
		return nil, nil, "", err
	}
	list, err := confidential.Load(c.TermList())
	if err != nil {
		return nil, nil, "", err
	}
	resolutions, err := confidential.LoadResolutions(c.Root())
	if err != nil {
		return nil, nil, "", err
	}
	scanner, err := publishrules.NewScanner(c.Record(), c.Now(), list, names, resolutions)
	if err != nil {
		return nil, nil, "", err
	}
	include, exclude, what := pushRange(c)
	hits, err := scanner.HitsInRange(c.Repo(), include, exclude)
	if err != nil {
		return nil, nil, "", err
	}
	return scanner, hits, what, nil
}

// ResolvableHits are the hits a resolution may name: those of the next push
// of the run's branch (ConfidentialHits), and those in the text of every
// file the repository tracks, which what a release packs and the
// documentation tool builds is made from. A file that is not UTF-8 text, or
// holds a NUL byte, is binary and not scanned. A question the scan cannot
// answer is an error.
func ResolvableHits(c *Context) (scanner *publishrules.Scanner, hits []confidential.Hit, err error) {
	defer func() {
		if p := recover(); p != nil {
			u, ok := p.(Unanswered)
			if !ok {
				panic(p)
			}
			scanner, hits, err = nil, nil, errors.New(u.Reason)
		}
	}()
	scanner, hits, _, err = ConfidentialHits(c)
	if err != nil || !scanner.Active() || scanner.Matcher().Len() == 0 {
		return scanner, hits, err
	}
	files, err := c.Repo().TrackedFiles()
	if err != nil {
		return nil, nil, err
	}
	var texts []publishrules.Text
	for _, rel := range files {
		data, err := os.ReadFile(filepath.Join(c.Root(), filepath.FromSlash(rel)))
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, nil, err
		}
		if bytes.IndexByte(data, 0) >= 0 || !utf8.Valid(data) {
			continue
		}
		texts = append(texts, publishrules.Text{Name: rel, Content: string(data)})
	}
	return scanner, append(hits, scanner.HitsInTexts(texts)...), nil
}

// pushRange is the commits the next push of the run's branch carries: those
// reachable from HEAD and not from origin's remote-tracking branch, nor from
// the history a fork inherited. With no remote-tracking branch, every commit
// of HEAD's history is in it. what names the range.
func pushRange(c *Context) (include, exclude []string, what string) {
	branch := c.in.Branch
	if branch == "" {
		head, attached, err := c.Repo().HeadBranch()
		if err != nil {
			panic(unanswered(err.Error()))
		}
		if !attached {
			panic(unanswered("HEAD is detached and the run names no branch, so which commits the next push carries is unknown; check out the release branch"))
		}
		branch = head
	}
	exclude = append(exclude, c.UpstreamExclude()...)
	tracking, found, err := c.Repo().RemoteTrackingCommit("origin", branch)
	if err != nil {
		panic(unanswered(err.Error()))
	}
	if !found {
		return []string{"HEAD"}, exclude, fmt.Sprintf("the history of HEAD (origin has no %s, so the next push carries all of it)", branch)
	}
	return []string{"HEAD"}, append(exclude, tracking), fmt.Sprintf("the range origin/%s..HEAD, the next push of %s", branch, branch)
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
