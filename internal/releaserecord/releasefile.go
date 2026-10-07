package releaserecord

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictspec/go/strictspec"

	"github.com/stricttools/rlsbl/internal/releaserecord/releasefilespec"
	"github.com/stricttools/rlsbl/internal/semver"
)

// FormatVersion is the format version of the release file, its archives,
// and the batch release file.
const FormatVersion = 2

// ReleaseFile is what a releasable's next release declares: how it moves the
// version, which targets it releases, and its prose.
type ReleaseFile struct {
	Bump        semver.Bump
	Include     []string
	Exclude     []string
	Description string
	Context     string
}

// Fate is what the record says about one version number.
type Fate string

// The fates. Recorded, unrecoverable, and never released are what an archive
// states; absent means there is no archive, and unstated means an archive
// stating none of the three (one the backfill has not reached).
const (
	FateAbsent        Fate = "absent"
	FateUnstated      Fate = "unstated"
	FateRecorded      Fate = "recorded"
	FateUnrecoverable Fate = "unrecoverable"
	FateNeverReleased Fate = "never-released"
)

// Released reports whether a release was published under the version:
// recorded and unrecoverable are releases, a never-released archive is not.
func (f Fate) Released() bool { return f == FateRecorded || f == FateUnrecoverable }

// ReleaseCommit is the commit a version shipped from and the tree each
// released path carried at it: "." for a standalone repository, one entry
// per member directory for a workspace releasable.
type ReleaseCommit struct {
	Commit string
	Trees  map[string]string
}

// Archive is one archived release, as its file states it.
type Archive struct {
	ReleaseFile
	Version semver.Version
	// Path is the archive's repository-relative path.
	Path          string
	Fate          Fate
	ReleaseCommit ReleaseCommit
	// ShippedAs is the tag the version shipped under when it differs from
	// the one the releasable's tag format gives it today, and empty
	// otherwise.
	ShippedAs string
	// ReleaseNotices are the deprecate and yank notices of the version's
	// GitHub Release, top to bottom.
	ReleaseNotices []string
}

// The flow-owned fields: written only by rlsbl's own commands (the release
// flow, the backfill, deprecate and yank, the history rewrites), refused in
// the editable release file, and stripped when an archive is restored as
// one.
const (
	fieldReleaseCommit  = "release_commit"
	fieldReleasedTrees  = "released_trees"
	fieldUnrecoverable  = "unrecoverable"
	fieldNeverReleased  = "never_released"
	fieldShippedAs      = "shipped_as"
	fieldReleaseNotices = "release_notices"
)

var flowOwnedFields = []string{fieldReleaseCommit, fieldReleasedTrees, fieldUnrecoverable, fieldNeverReleased, fieldShippedAs, fieldReleaseNotices}

// rawReleaseFields are the fields of one release, in the release file and in
// each table of the batch release file.
type rawReleaseFields struct {
	Bump           string             `toml:"bump,required"`
	Include        []string           `toml:"include,required"`
	Exclude        []string           `toml:"exclude,required"`
	Description    string             `toml:"description,required"`
	Context        *string            `toml:"context"`
	ReleaseCommit  *string            `toml:"release_commit"`
	ReleasedTrees  *map[string]string `toml:"released_trees"`
	Unrecoverable  *bool              `toml:"unrecoverable"`
	NeverReleased  *bool              `toml:"never_released"`
	ShippedAs      *string            `toml:"shipped_as"`
	ReleaseNotices *[]string          `toml:"release_notices"`
}

type rawReleaseDocument struct {
	FormatVersion int64 `toml:"format_version,required"`
	rawReleaseFields
}

// document is one parsed release document, release file or archive.
type document struct {
	file          ReleaseFile
	releaseCommit *string
	trees         *map[string]string
	unrecoverable *bool
	neverReleased *bool
	shippedAs     *string
	notices       *[]string
}

// flowOwnedPresent names the flow-owned fields the document carries.
func (d document) flowOwnedPresent() []string {
	var present []string
	for _, f := range []struct {
		name string
		set  bool
	}{
		{fieldReleaseCommit, d.releaseCommit != nil},
		{fieldReleasedTrees, d.trees != nil},
		{fieldUnrecoverable, d.unrecoverable != nil},
		{fieldNeverReleased, d.neverReleased != nil},
		{fieldShippedAs, d.shippedAs != nil},
		{fieldReleaseNotices, d.notices != nil},
	} {
		if f.set {
			present = append(present, f.name)
		}
	}
	return present
}

// fate is what an archive document states. A marker is read by presence, as
// the schema's exclusion judges it.
func (d document) fate() Fate {
	switch {
	case d.neverReleased != nil:
		return FateNeverReleased
	case d.unrecoverable != nil:
		return FateUnrecoverable
	case d.releaseCommit != nil:
		return FateRecorded
	}
	return FateUnstated
}

// FileError is a release document rlsbl refuses: the file and every problem
// found in it.
type FileError struct {
	File     string
	Problems []string
}

func (e *FileError) Error() string {
	if len(e.Problems) == 1 {
		return e.File + ": " + e.Problems[0]
	}
	return e.File + " is refused:\n  " + strings.Join(e.Problems, "\n  ")
}

func refuseFile(file string, problems ...string) error {
	return &FileError{File: file, Problems: problems}
}

// retiredProblems names the fields of the first format, and of the channels
// the rewrite dropped, with the reason each is refused: the generated
// validator would report them as an unknown key or an unsupported version,
// which does not say what to do.
func retiredProblems(fields map[string]any, where string) []string {
	var problems []string
	if _, ok := fields["preid"]; ok {
		problems = append(problems, where+"preid is refused: rlsbl has no pre-release channel; delete the line and release a MAJOR.MINOR.PATCH version")
	}
	if _, ok := fields["blog"]; ok {
		problems = append(problems, where+"blog is refused: a release no longer writes a blog post; delete the line")
	}
	if bump, ok := fields["bump"].(string); ok && bump == "prerelease" {
		problems = append(problems, where+"bump \"prerelease\" is refused: rlsbl has no pre-release channel; declare one of patch, minor, major, infra")
	}
	for _, old := range []struct{ name, replacement string }{{"candidate_sha", fieldReleaseCommit}, {"tree_hashes", fieldReleasedTrees}} {
		if _, ok := fields[old.name]; ok {
			problems = append(problems, fmt.Sprintf("%s%s is the first format's name for %s; this file was not migrated (rlsbl migrate records converts it)", where, old.name, old.replacement))
		}
	}
	return problems
}

// parseDocument parses a release document: the refusals that name a
// retired field, the generated validator, the strict decode, and the checks
// the schema cannot state. rel names the file in every problem.
func parseDocument(rel string, data []byte) (document, error) {
	if fields, err := tomledit.Unmarshal[map[string]any](data); err == nil {
		if v, ok := (*fields)["format_version"].(int64); ok && v == 1 {
			return document{}, refuseFile(rel, "format_version 1 is the release file format before the Go rewrite; rlsbl migrate records converts it")
		}
		if problems := retiredProblems(*fields, ""); len(problems) > 0 {
			return document{}, refuseFile(rel, problems...)
		}
	}
	if _, diags := releasefilespec.ValidateBytes(data, "toml"); len(diags) > 0 {
		return document{}, refuseFile(rel, diagnosticProblems(diags)...)
	}
	raw, err := tomledit.Unmarshal[rawReleaseDocument](data)
	if err != nil {
		return document{}, refuseFile(rel, err.Error())
	}
	d, problems := raw.rawReleaseFields.convert("")
	if len(problems) > 0 {
		return document{}, refuseFile(rel, problems...)
	}
	return d, nil
}

// convert checks one release's fields beyond the schema and builds them.
// where prefixes every problem (a batch table names itself).
func (r rawReleaseFields) convert(where string) (document, []string) {
	var problems []string
	bump, err := semver.ParseBump(r.Bump)
	if err != nil {
		problems = append(problems, where+err.Error())
	}
	description := strings.TrimSpace(r.Description)
	if description == "" {
		problems = append(problems, where+"description must be set: a short summary of this release")
	}
	context := ""
	if r.Context != nil {
		context = strings.TrimSpace(*r.Context)
	}
	if overlap := intersection(r.Include, r.Exclude); len(overlap) > 0 {
		problems = append(problems, fmt.Sprintf("%stargets appear in both include and exclude: %s", where, strings.Join(overlap, ", ")))
	}
	return document{
		file: ReleaseFile{
			Bump:        bump,
			Include:     append([]string{}, r.Include...),
			Exclude:     append([]string{}, r.Exclude...),
			Description: description,
			Context:     context,
		},
		releaseCommit: r.ReleaseCommit,
		trees:         r.ReleasedTrees,
		unrecoverable: r.Unrecoverable,
		neverReleased: r.NeverReleased,
		shippedAs:     r.ShippedAs,
		notices:       r.ReleaseNotices,
	}, problems
}

func intersection(a, b []string) []string {
	in := map[string]bool{}
	for _, x := range a {
		in[x] = true
	}
	seen := map[string]bool{}
	var out []string
	for _, x := range b {
		if in[x] && !seen[x] {
			seen[x] = true
			out = append(out, x)
		}
	}
	sort.Strings(out)
	return out
}

// diagnosticProblems renders strictspec's diagnostics, in emission order.
func diagnosticProblems(diags []strictspec.Diagnostic) []string {
	out := make([]string, 0, len(diags))
	for _, d := range diags {
		where := d.Path
		if where == "" {
			where = "the document"
		}
		out = append(out, fmt.Sprintf("%s: %s [%s]", where, d.Message, d.Code))
	}
	return out
}

// ParseReleaseFile parses an editable release file. rel names the file in
// every problem. A flow-owned field is refused: the release flow alone
// writes the release commit, a fate, shipped_as, and the notices, and only
// into an archive.
func ParseReleaseFile(rel string, data []byte) (ReleaseFile, error) {
	d, err := parseDocument(rel, data)
	if err != nil {
		return ReleaseFile{}, err
	}
	if present := d.flowOwnedPresent(); len(present) > 0 {
		return ReleaseFile{}, refuseFile(rel, fmt.Sprintf("%s carried by the editable release file: only rlsbl writes these, and only into an archive. Delete the lines; a release file restored by `rlsbl release undo` never carries them", strings.Join(present, ", ")))
	}
	return d.file, nil
}

// ReadReleaseFile reads a releasable's editable release file. A missing file
// is refused, naming the command that writes one.
func ReadReleaseFile(root, releasable string) (ReleaseFile, error) {
	rel := ReleaseFilePath(releasable)
	data, found, err := readFile(root, rel)
	if err != nil {
		return ReleaseFile{}, err
	}
	if !found {
		return ReleaseFile{}, fmt.Errorf("the releasable %q has no release file %s: write one with `rlsbl release init`, then fill in its bump and description", releasable, rel)
	}
	return ParseReleaseFile(rel, data)
}

// ReadArchive reads the archive of v in dir (repository-relative). An
// archive stating no fate is returned with FateUnstated: whether that is an
// error is the reader's question (the record's reads refuse it, the backfill
// repairs it). A missing archive is an error.
func ReadArchive(root, dir string, v semver.Version) (Archive, error) {
	rel := ArchivePath(dir, v)
	data, found, err := readFile(root, rel)
	if err != nil {
		return Archive{}, err
	}
	if !found {
		return Archive{}, fmt.Errorf("there is no archive %s", rel)
	}
	return parseArchive(rel, v, data)
}

func parseArchive(rel string, v semver.Version, data []byte) (Archive, error) {
	d, err := parseDocument(rel, data)
	if err != nil {
		return Archive{}, err
	}
	a := Archive{ReleaseFile: d.file, Version: v, Path: rel, Fate: d.fate()}
	if a.Fate == FateRecorded {
		a.ReleaseCommit.Commit = *d.releaseCommit
		if d.trees != nil {
			a.ReleaseCommit.Trees = copyTrees(*d.trees)
		}
	}
	if d.shippedAs != nil {
		a.ShippedAs = *d.shippedAs
	}
	if d.notices != nil {
		a.ReleaseNotices = append([]string{}, *d.notices...)
	}
	return a, nil
}

func copyTrees(trees map[string]string) map[string]string {
	out := make(map[string]string, len(trees))
	for k, v := range trees {
		out[k] = v
	}
	return out
}

// objectID is a git object id as the record holds one: full, or
// abbreviated to at least seven hexadecimal digits.
var objectID = regexp.MustCompile(`^[0-9a-f]{7,64}$`)

// releasedPath is a released tree's key, as the schema states it.
var releasedPath = regexp.MustCompile(`^(\.|[A-Za-z0-9._][A-Za-z0-9._/-]*)$`)

// check refuses a malformed release commit before it reaches a file, so a
// release never writes an archive its own reader refuses.
func (c ReleaseCommit) check() error {
	if !objectID.MatchString(c.Commit) {
		return fmt.Errorf("a release commit must be a git commit id (7 to 64 lowercase hexadecimal digits), not %q", c.Commit)
	}
	if len(c.Trees) == 0 {
		return fmt.Errorf("a release commit names at least one released tree (\".\" for a standalone repository, one entry per member directory for a workspace releasable)")
	}
	for p, tree := range c.Trees {
		if !releasedPath.MatchString(p) {
			return fmt.Errorf("the released path %q is not a repository-relative directory (\".\" for the root)", p)
		}
		if !objectID.MatchString(tree) {
			return fmt.Errorf("the released tree of %q must be a git tree id (7 to 64 lowercase hexadecimal digits), not %q", p, tree)
		}
	}
	return nil
}

// IsPristineReleaseFile reports whether content is a release file nobody has
// filled in yet: empty, or a TOML document whose bump and description are
// both blank. Anything else, a document that does not parse included, is
// someone's work, which `release init` refuses to overwrite.
func IsPristineReleaseFile(content []byte) bool {
	if strings.TrimSpace(string(content)) == "" {
		return true
	}
	fields, err := tomledit.Unmarshal[map[string]any](content)
	if err != nil {
		return false
	}
	return blank((*fields)["bump"]) && blank((*fields)["description"])
}

// IsPristineBatchReleaseFile reports whether content is a batch release file
// nobody has filled in yet: empty, or one whose every releasable table has a
// blank bump and description.
func IsPristineBatchReleaseFile(content []byte) bool {
	if strings.TrimSpace(string(content)) == "" {
		return true
	}
	fields, err := tomledit.Unmarshal[map[string]any](content)
	if err != nil {
		return false
	}
	section, ok := (*fields)["releasables"]
	if !ok {
		return true
	}
	tables, ok := section.(map[string]any)
	if !ok {
		return false
	}
	for _, entry := range tables {
		table, ok := entry.(map[string]any)
		if !ok || !blank(table["bump"]) || !blank(table["description"]) {
			return false
		}
	}
	return true
}

// blank reports whether a field is absent or a string of only whitespace;
// any other value is filled in.
func blank(v any) bool {
	if v == nil {
		return true
	}
	s, ok := v.(string)
	return ok && strings.TrimSpace(s) == ""
}
