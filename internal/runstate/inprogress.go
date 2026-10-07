package runstate

import (
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// InProgressFormatVersion is the format version of in-progress.toml.
const InProgressFormatVersion = 1

// ResumeInvocation, RunInvocation, and BatchRunInvocation are the command
// lines every text naming the resume of a stopped release, a release run,
// or a batch release run prints. Each command requires the watch choice, so
// a line without it is refused as printed; stating the lines once keeps the
// texts from drifting from them.
const (
	ResumeInvocation   = "rlsbl release resume --watch"
	RunInvocation      = "rlsbl release run --watch"
	BatchRunInvocation = "rlsbl monorepo release run --watch"
)

// InProgress is a release in progress: what it releases, where it stands,
// and which steps finished or failed. It is written when the release starts
// mutating, after every step, and removed once the release is provably
// complete; a release that stops leaves it for `release resume`.
//
// Two markers are kept per step. A completed step is skipped on resume. A
// failed step is attempted again on resume; the failure feeds the closing
// summary, and a fatal step's failure stops the release. Which steps exist
// and which are fatal is the release's table, passed to the questions that
// need it.
type InProgress struct {
	Releasable string
	// RepresentativeMember is the member resume resolves the releasable
	// through.
	RepresentativeMember string
	Version              string
	Tag                  string
	CompanionTags        []string
	Branch               string
	// Registry is the registry the release publishes to, empty when none.
	Registry string
	// Bump is empty for a first release, which ships the current version as
	// it is.
	Bump             string
	CommitMessage    string
	Description      string
	Context          string
	Include          []string
	Exclude          []string
	PreReleaseCommit string
	PinCommit        string
	// ReleaseCommit is the candidate, once it is recorded.
	ReleaseCommit string
	// CreatedCommits are the commits the release made, in order.
	CreatedCommits []string
	// RunAllDispatchFor is the candidate a run_all dispatch is still owed
	// for, empty when none is.
	RunAllDispatchFor string
	CompletedSteps    []string
	// FailedSteps maps a failed step to its failure message.
	FailedSteps      map[string]string
	PublishedTargets []string
	PublishedMembers []string
}

type rawInProgress struct {
	FormatVersion        int64              `toml:"format_version,required"`
	Releasable           string             `toml:"releasable,required"`
	RepresentativeMember string             `toml:"representative_member,required"`
	Version              string             `toml:"version,required"`
	Tag                  string             `toml:"tag,required"`
	CompanionTags        *[]string          `toml:"companion_tags"`
	Branch               string             `toml:"branch,required"`
	Registry             *string            `toml:"registry"`
	Bump                 *string            `toml:"bump"`
	CommitMessage        string             `toml:"commit_message,required"`
	Description          string             `toml:"description,required"`
	Context              *string            `toml:"context"`
	Include              []string           `toml:"include,required"`
	Exclude              []string           `toml:"exclude,required"`
	PreReleaseCommit     string             `toml:"pre_release_commit,required"`
	PinCommit            string             `toml:"pin_commit,required"`
	ReleaseCommit        *string            `toml:"release_commit"`
	CreatedCommits       *[]string          `toml:"created_commits"`
	RunAllDispatchFor    *string            `toml:"run_all_dispatch_for"`
	CompletedSteps       []string           `toml:"completed_steps,required"`
	PublishedTargets     *[]string          `toml:"published_targets"`
	PublishedMembers     *[]string          `toml:"published_members"`
	FailedSteps          *map[string]string `toml:"failed_steps"`
}

func deref[T any](p *T) T {
	var zero T
	if p == nil {
		return zero
	}
	return *p
}

// ParseInProgress parses in-progress.toml. rel names the file in every
// problem.
func ParseInProgress(rel string, data []byte) (InProgress, error) {
	raw, err := decode[rawInProgress](rel, data)
	if err != nil {
		return InProgress{}, err
	}
	if raw.FormatVersion != InProgressFormatVersion {
		return InProgress{}, &FileError{File: rel, Problems: []string{fmt.Sprintf("format_version %d is not the in-progress state format %d", raw.FormatVersion, InProgressFormatVersion)}}
	}
	s := InProgress{
		Releasable:           raw.Releasable,
		RepresentativeMember: raw.RepresentativeMember,
		Version:              raw.Version,
		Tag:                  raw.Tag,
		CompanionTags:        deref(raw.CompanionTags),
		Branch:               raw.Branch,
		Registry:             deref(raw.Registry),
		Bump:                 deref(raw.Bump),
		CommitMessage:        raw.CommitMessage,
		Description:          raw.Description,
		Context:              deref(raw.Context),
		Include:              raw.Include,
		Exclude:              raw.Exclude,
		PreReleaseCommit:     raw.PreReleaseCommit,
		PinCommit:            raw.PinCommit,
		ReleaseCommit:        deref(raw.ReleaseCommit),
		CreatedCommits:       deref(raw.CreatedCommits),
		RunAllDispatchFor:    deref(raw.RunAllDispatchFor),
		CompletedSteps:       raw.CompletedSteps,
		FailedSteps:          deref(raw.FailedSteps),
		PublishedTargets:     deref(raw.PublishedTargets),
		PublishedMembers:     deref(raw.PublishedMembers),
	}
	if problems := s.problems(); len(problems) > 0 {
		return InProgress{}, &FileError{File: rel, Problems: problems}
	}
	return s, nil
}

// problems are what the decode cannot see: required text left empty, and a
// step both completed and failed.
func (s InProgress) problems() []string {
	var problems []string
	for _, f := range []struct{ name, value string }{
		{"releasable", s.Releasable},
		{"representative_member", s.RepresentativeMember},
		{"version", s.Version},
		{"tag", s.Tag},
		{"branch", s.Branch},
		{"commit_message", s.CommitMessage},
		{"pre_release_commit", s.PreReleaseCommit},
		{"pin_commit", s.PinCommit},
	} {
		if f.value == "" {
			problems = append(problems, f.name+" is empty")
		}
	}
	for _, step := range s.CompletedSteps {
		if _, failed := s.FailedSteps[step]; failed {
			problems = append(problems, fmt.Sprintf("the step %q is both completed and failed", step))
		}
	}
	return problems
}

// Render writes the state as TOML, its optional fields only when set.
func (s InProgress) Render() []byte {
	var b strings.Builder
	line := func(key, value string) { fmt.Fprintf(&b, "%s = %s\n", key, value) }
	text := func(key, value string) { line(key, quote(value)) }
	optionalText := func(key, value string) {
		if value != "" {
			text(key, value)
		}
	}
	optionalList := func(key string, values []string) {
		if len(values) > 0 {
			line(key, stringArray(values))
		}
	}
	line("format_version", fmt.Sprint(InProgressFormatVersion))
	text("releasable", s.Releasable)
	text("representative_member", s.RepresentativeMember)
	text("version", s.Version)
	text("tag", s.Tag)
	optionalList("companion_tags", s.CompanionTags)
	text("branch", s.Branch)
	optionalText("registry", s.Registry)
	optionalText("bump", s.Bump)
	text("commit_message", s.CommitMessage)
	text("description", s.Description)
	optionalText("context", s.Context)
	line("include", stringArray(s.Include))
	line("exclude", stringArray(s.Exclude))
	text("pre_release_commit", s.PreReleaseCommit)
	text("pin_commit", s.PinCommit)
	optionalText("release_commit", s.ReleaseCommit)
	optionalList("created_commits", s.CreatedCommits)
	optionalText("run_all_dispatch_for", s.RunAllDispatchFor)
	line("completed_steps", stringArray(s.CompletedSteps))
	optionalList("published_targets", s.PublishedTargets)
	optionalList("published_members", s.PublishedMembers)
	if len(s.FailedSteps) > 0 {
		b.WriteString("\n[failed_steps]\n")
		steps := make([]string, 0, len(s.FailedSteps))
		for step := range s.FailedSteps {
			steps = append(steps, step)
		}
		sort.Strings(steps)
		for _, step := range steps {
			fmt.Fprintf(&b, "%s = %s\n", quoteKey(step), quote(s.FailedSteps[step]))
		}
	}
	return []byte(b.String())
}

// LoadInProgress reads a releasable's in-progress state. found is false when
// no release of it is in progress.
func LoadInProgress(root, releasable string) (s InProgress, found bool, err error) {
	rel := InProgressPath(releasable)
	data, found, err := readFile(root, rel)
	if err != nil || !found {
		return InProgress{}, false, err
	}
	s, err = ParseInProgress(rel, data)
	if err != nil {
		return InProgress{}, false, err
	}
	if s.Releasable != releasable {
		return InProgress{}, false, &FileError{File: rel, Problems: []string{fmt.Sprintf("it names the releasable %q, but it is the state file of %q", s.Releasable, releasable)}}
	}
	return s, true, nil
}

// SaveInProgress writes the state of its releasable, atomically, refusing a
// state its own reader would refuse.
func SaveInProgress(e *strictcli.Effects, root string, s InProgress) error {
	if s.Releasable == "" {
		return fmt.Errorf("refusing to save an in-progress state that names no releasable")
	}
	rel := InProgressPath(s.Releasable)
	data := s.Render()
	if _, err := ParseInProgress(rel, data); err != nil {
		return err
	}
	return writeFile(e, root, rel, data)
}

// ClearInProgress removes a releasable's in-progress state, and its
// directory when that is left empty. A missing state is nothing to remove.
func ClearInProgress(e *strictcli.Effects, root, releasable string) error {
	return removeFile(e, root, InProgressPath(releasable))
}

// Complete records step as completed, clearing a failure recorded for it
// (a resume that re-attempts a failed step and succeeds).
func (s *InProgress) Complete(step string) {
	delete(s.FailedSteps, step)
	for _, done := range s.CompletedSteps {
		if done == step {
			return
		}
	}
	s.CompletedSteps = append(s.CompletedSteps, step)
}

// Fail records step as failed with message, clearing a completion recorded
// for it.
func (s *InProgress) Fail(step, message string) {
	if s.FailedSteps == nil {
		s.FailedSteps = map[string]string{}
	}
	s.FailedSteps[step] = message
	kept := s.CompletedSteps[:0]
	for _, done := range s.CompletedSteps {
		if done != step {
			kept = append(kept, done)
		}
	}
	s.CompletedSteps = kept
}

// Completed reports whether step is recorded as completed.
func (s InProgress) Completed(step string) bool {
	for _, done := range s.CompletedSteps {
		if done == step {
			return true
		}
	}
	return false
}

// UnknownSteps lists the recorded steps steps does not name: a state written
// by another version's step table, which resume refuses rather than guess.
func (s InProgress) UnknownSteps(steps []string) []string {
	known := map[string]bool{}
	for _, step := range steps {
		known[step] = true
	}
	var unknown []string
	for _, step := range s.CompletedSteps {
		if !known[step] {
			unknown = append(unknown, step)
		}
	}
	for step := range s.FailedSteps {
		if !known[step] {
			unknown = append(unknown, step)
		}
	}
	sort.Strings(unknown)
	return unknown
}

// MissingSteps lists, in the order of steps, the steps with neither marker.
func (s InProgress) MissingSteps(steps []string) []string {
	var missing []string
	for _, step := range steps {
		if _, failed := s.FailedSteps[step]; !failed && !s.Completed(step) {
			missing = append(missing, step)
		}
	}
	return missing
}

// FatalFailure reports whether a step fatal names has a failure marker.
func (s InProgress) FatalFailure(fatal map[string]bool) bool {
	for step := range s.FailedSteps {
		if fatal[step] {
			return true
		}
	}
	return false
}

// IsComplete reports whether the state is provably complete: every step has
// a marker and no fatal step failed. Only then may a release remove it.
func (s InProgress) IsComplete(steps []string, fatal map[string]bool) bool {
	return len(s.MissingSteps(steps)) == 0 && !s.FatalFailure(fatal)
}

// InProgressReleasables lists the releasables of the repository at root with
// a release in progress, in name order: resume's question when it is not
// told which releasable to resume.
func InProgressReleasables(root string) ([]string, error) {
	entries, err := os.ReadDir(absolute(root, declarations.ReleaseStateDir))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("listing %s: %w", declarations.ReleaseStateDir, err)
	}
	var names []string
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		found, err := exists(root, InProgressPath(entry.Name()))
		if err != nil {
			return nil, err
		}
		if found {
			names = append(names, entry.Name())
		}
	}
	return names, nil
}
