package changelog

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"regexp"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
)

// NeverReleasedNote is what a never-released version's section says first.
// Such a version can have released changelog files (entries written and
// locked before the release was abandoned), so its section is rendered, and
// the note keeps a reader from taking it for a release that happened.
const NeverReleasedNote = "**Never released.** This version number exists in the release record, but no release was ever published under it."

// typeGroups are the entry types and their headings, in the order a
// section lists them.
var typeGroups = []struct{ typ, heading string }{
	{TypeBreaking, "Breaking"},
	{TypeFeature, "Features"},
	{TypeFix, "Fixes"},
}

// SectionMeta is the release prose a section shows beside its entries.
type SectionMeta struct {
	Description   string
	Context       string
	Bump          semver.Bump
	NeverReleased bool
}

func heading(depth int, title string) string { return strings.Repeat("#", depth) + " " + title }

// Section renders one version's section with its heading at depth (2 in a
// releasable's CHANGELOG.md, 3 in a workspace's roll-up) and its type
// groups one level below. Only user-facing entries are listed, grouped by
// type; a section with none says so, or, for an infra release with a
// description, lists that description as its infrastructure change.
func Section(title string, entries []Entry, meta SectionMeta, depth int) string {
	var userFacing []Entry
	for _, e := range entries {
		if e.UserFacing {
			userFacing = append(userFacing, e)
		}
	}
	parts := []string{heading(depth, title)}
	if meta.NeverReleased {
		parts = append(parts, "", NeverReleasedNote)
	}
	if meta.Description != "" {
		parts = append(parts, "", meta.Description)
	}
	if meta.Context != "" {
		parts = append(parts, "", "<details>", "<summary>Context</summary>", "", meta.Context, "", "</details>")
	}
	if len(userFacing) == 0 {
		if meta.Bump == semver.Infra && meta.Description != "" {
			parts = append(parts, "", heading(depth+1, "Infrastructure"), "", "- "+meta.Description)
		} else {
			parts = append(parts, "", "- No user-facing changes.")
		}
		return strings.Join(parts, "\n") + "\n"
	}
	groups := map[string][]string{}
	for _, e := range userFacing {
		text := e.Description
		if len(e.Packages) > 0 {
			packages := append([]string(nil), e.Packages...)
			sort.Strings(packages)
			text = "[" + strings.Join(packages, ", ") + "] " + text
		}
		groups[e.Type] = append(groups[e.Type], text)
	}
	for _, g := range typeGroups {
		if len(groups[g.typ]) == 0 {
			continue
		}
		parts = append(parts, "", heading(depth+1, g.heading), "")
		for _, text := range groups[g.typ] {
			parts = append(parts, "- "+text)
		}
	}
	return strings.Join(parts, "\n") + "\n"
}

// Pending is the release being prepared: its section is headed by the
// version instead of "Unreleased" and shows its prose, and it is rendered
// even with no entries (an infra release may have none).
type Pending struct {
	Version     semver.Version
	Description string
	Context     string
	Bump        semver.Bump
}

// archiveMeta is the prose of version v's archive, and nothing when v has
// no archive (a version released before archives were written). An archive
// that exists and cannot be read is an error naming it: a regeneration must
// never replace a release's description with nothing.
func archiveMeta(root, releasable string, archived map[string]bool, v semver.Version) (SectionMeta, error) {
	if !archived[v.String()] {
		return SectionMeta{}, nil
	}
	a, err := releaserecord.ReadArchive(root, releaserecord.ArchiveDir(releasable), v)
	if err != nil {
		return SectionMeta{}, err
	}
	return SectionMeta{
		Description:   strings.TrimSpace(a.Description),
		Context:       strings.TrimSpace(a.Context),
		Bump:          a.Bump,
		NeverReleased: a.Fate == releaserecord.FateNeverReleased,
	}, nil
}

// Sections are a releasable's sections at depth, newest first: the
// unreleased entries (when there are any, or a release is pending), then
// each released version with its archive's prose.
func Sections(root, releasable string, depth int, pending *Pending) ([]string, error) {
	dir := Dir(releasable)
	files, err := ReadAll(root, dir)
	if err != nil {
		return nil, err
	}
	versions, err := releaserecord.ArchivedVersions(root, releaserecord.ArchiveDir(releasable))
	if err != nil {
		return nil, err
	}
	archived := map[string]bool{}
	for _, v := range versions {
		archived[v.String()] = true
	}
	var sections []string
	for _, f := range files {
		if !f.Released {
			switch {
			case pending != nil:
				sections = append(sections, Section(pending.Version.String(), f.Entries(), SectionMeta{Description: pending.Description, Context: pending.Context, Bump: pending.Bump}, depth))
			case len(f.Lines) > 0:
				sections = append(sections, Section("Unreleased", f.Entries(), SectionMeta{}, depth))
			}
			continue
		}
		meta, err := archiveMeta(root, releasable, archived, f.Version)
		if err != nil {
			return nil, err
		}
		sections = append(sections, Section(f.Version.String(), f.Entries(), meta, depth))
	}
	return sections, nil
}

// VersionSection renders released version v's section at depth as Sections
// renders it: the entries of v's changelog file with v's archive's prose. A
// version with no changelog file is an error naming the file.
func VersionSection(root, releasable string, v semver.Version, depth int) (string, error) {
	f, err := ReadVersion(root, Dir(releasable), v)
	if err != nil {
		return "", err
	}
	versions, err := releaserecord.ArchivedVersions(root, releaserecord.ArchiveDir(releasable))
	if err != nil {
		return "", err
	}
	archived := map[string]bool{}
	for _, a := range versions {
		archived[a.String()] = true
	}
	meta, err := archiveMeta(root, releasable, archived, v)
	if err != nil {
		return "", err
	}
	return Section(v.String(), f.Entries(), meta, depth), nil
}

func generatedComment(source string) string {
	return "<!-- Generated by rlsbl from " + source + " — do not edit -->"
}

// Document is a releasable's CHANGELOG.md: one "# Changelog" heading and
// its sections below it.
func Document(root, releasable string, pending *Pending) (string, error) {
	sections, err := Sections(root, releasable, 2, pending)
	if err != nil {
		return "", err
	}
	return generatedComment(Dir(releasable)+"/") + "\n\n# Changelog\n\n" + strings.Join(sections, "\n"), nil
}

// RollUp is a workspace's root CHANGELOG.md: one "# Changelog" heading,
// one "## <releasable>" section per releasable with any section, in
// declaration order, and each releasable's versions one level below that.
// pending is the release being prepared of the releasable pendingFor.
func RollUp(root string, d *declarations.Releasables, pendingFor string, pending *Pending) (string, error) {
	var blocks []string
	for _, r := range d.Releasables {
		var p *Pending
		if r.Name == pendingFor {
			p = pending
		}
		sections, err := Sections(root, r.Name, 3, p)
		if err != nil {
			return "", err
		}
		if len(sections) == 0 {
			continue
		}
		blocks = append(blocks, heading(2, r.Name)+"\n\n"+strings.Join(sections, "\n"))
	}
	return generatedComment(declarations.ChangelogRoot+"/") + "\n\n# Changelog\n\n" + strings.Join(blocks, "\n"), nil
}

// Regenerate writes releasable's CHANGELOG.md at its home (Home) and, in a
// workspace, the roll-up, each only when its content changed. It returns
// the repository-relative paths it wrote.
func Regenerate(e *strictcli.Effects, root string, d *declarations.Releasables, releasable string, pending *Pending) ([]string, error) {
	document, err := Document(root, releasable, pending)
	if err != nil {
		return nil, err
	}
	outputs := []struct{ rel, content string }{{Home(d, releasable), document}}
	if d.IsWorkspace() {
		rollUp, err := RollUp(root, d, releasable, pending)
		if err != nil {
			return nil, err
		}
		outputs = append(outputs, struct{ rel, content string }{RollUpPath, rollUp})
	}
	var written []string
	for _, o := range outputs {
		current, err := os.ReadFile(absolute(root, o.rel))
		if err == nil && string(current) == o.content {
			continue
		}
		if err != nil && !errors.Is(err, fs.ErrNotExist) {
			return nil, fmt.Errorf("reading %s: %w", o.rel, err)
		}
		mode, err := modeOf(root, o.rel, 0o644)
		if err != nil {
			return nil, err
		}
		if err := writeFile(e, root, o.rel, []byte(o.content), mode); err != nil {
			return nil, err
		}
		written = append(written, o.rel)
	}
	return written, nil
}

// ExtractSection is the body of version's section in a releasable's
// CHANGELOG.md content: everything after its "## <version>" heading up to
// the next "## " heading, trimmed. found is false when there is no such
// section or its body is empty.
func ExtractSection(content, version string) (body string, found bool) {
	h := regexp.MustCompile(`(?m)^## ` + regexp.QuoteMeta(version) + `\s*$`)
	loc := h.FindStringIndex(content)
	if loc == nil {
		return "", false
	}
	rest := content[loc[1]:]
	if next := strings.Index(rest, "\n## "); next >= 0 {
		rest = rest[:next]
	}
	body = strings.TrimSpace(rest)
	return body, body != ""
}
