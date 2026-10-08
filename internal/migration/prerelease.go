package migration

import (
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"

	tomledit "github.com/stricttools/go-toml-edit"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// The Python rlsbl had a pre-release channel the new layout dropped: its
// versions were MAJOR.MINOR.PATCH-<channel>.<counter>, the channels alpha,
// beta, and rc in that order, and each pre-release preceded the stable
// version it carries before the suffix.
var preReleaseVersion = regexp.MustCompile(`^(\d+\.\d+\.\d+)-(alpha|beta|rc)\.(\d+)$`)

// preReleaseChannelRank orders the channels as the Python ordered them.
var preReleaseChannelRank = map[string]int{"alpha": 0, "beta": 1, "rc": 2}

// preRelease is one version of the dropped pre-release channel.
type preRelease struct {
	// label is the version as the old file names spell it.
	label string
	// stable is the version the pre-release preceded.
	stable  semver.Version
	channel int
	counter int
}

// parsePreRelease reads a version of the dropped pre-release channel, and
// false for any other label.
func parsePreRelease(label string) (preRelease, bool) {
	m := preReleaseVersion.FindStringSubmatch(label)
	if m == nil {
		return preRelease{}, false
	}
	stable, err := semver.Parse(m[1])
	if err != nil {
		return preRelease{}, false
	}
	counter, err := strconv.Atoi(m[3])
	if err != nil {
		return preRelease{}, false
	}
	return preRelease{label: label, stable: stable, channel: preReleaseChannelRank[m[2]], counter: counter}, true
}

// newerPreReleaseFirst orders pre-releases newest first: by the stable
// version, then the channel, then the counter, compared as a number.
func newerPreReleaseFirst(prs []preRelease) {
	sort.Slice(prs, func(i, j int) bool {
		a, b := prs[i], prs[j]
		if c := semver.Compare(a.stable, b.stable); c != 0 {
			return c > 0
		}
		if a.channel != b.channel {
			return a.channel > b.channel
		}
		return a.counter > b.counter
	})
}

// preReleaseTag is a pre-release of a subject whose tag the
// lifecycle-and-license record keeps as an unversioned tag.
type preReleaseTag struct {
	subject string
	pr      preRelease
	// destination is the changelog file the pre-release's entries moved
	// into, as the reason names it.
	destination string
}

// preReleasesOf are the pre-releases an old state directory holds an
// archive or a changelog file of, newest first.
func (b *builder) preReleasesOf(stateDir string) []preRelease {
	seen := map[string]bool{}
	var out []preRelease
	add := func(label string) {
		if pr, ok := parsePreRelease(label); ok && !seen[label] {
			seen[label] = true
			out = append(out, pr)
		}
	}
	for _, f := range b.tree.under(stateDir + "/changes") {
		name := strings.TrimPrefix(f, stateDir+"/changes/")
		if !strings.Contains(name, "/") && strings.HasSuffix(name, ".jsonl") {
			add(strings.TrimSuffix(name, ".jsonl"))
		}
	}
	for _, f := range b.tree.under(stateDir + "/releases") {
		name := strings.TrimPrefix(f, stateDir+"/releases/")
		if !strings.Contains(name, "/") && strings.HasPrefix(name, "v") && strings.HasSuffix(name, ".toml") {
			add(strings.TrimSuffix(strings.TrimPrefix(name, "v"), ".toml"))
		}
	}
	newerPreReleaseFirst(out)
	return out
}

// stableReleased reports whether the stable version v was released, as the
// old state directory records it: its archive records a release (any fate
// but never_released), or, with no archive, its released changelog file
// exists, which the Python wrote only when it released the version.
func (b *builder) stableReleased(stateDir string, v semver.Version) bool {
	archive := stateDir + "/releases/v" + v.String() + ".toml"
	if b.tree.has(archive) {
		data, err := b.tree.read(archive)
		if err != nil {
			b.p.add("%v", err)
			return false
		}
		fields, err := tomledit.Unmarshal[map[string]any](data)
		if err != nil {
			b.p.add("%s is not valid TOML: %v", archive, err)
			return false
		}
		never, _ := (*fields)["never_released"].(bool)
		return !never
	}
	return b.tree.has(stateDir + "/changes/" + v.String() + ".jsonl")
}

// preReleaseDestination is the label of the changelog file a pre-release's
// entries move into: the stable version's when it was released, and the
// unreleased file's otherwise.
func (b *builder) preReleaseDestination(stateDir string, pr preRelease) string {
	if b.stableReleased(stateDir, pr.stable) {
		return pr.stable.String()
	}
	return "unreleased"
}

// foldEntries is a destination file's own entries followed by the folded
// pre-releases' entries, newest pre-release first, leaving out an entry
// naming the same set of commits as one already kept: the Python's
// changelog showed a stable version and its pre-releases as one section
// deduplicated by that rule, the first occurrence kept.
func foldEntries(own []changelog.Entry, folded [][]changelog.Entry) []changelog.Entry {
	seen := map[string]bool{}
	var out []changelog.Entry
	keep := func(e changelog.Entry) {
		commits := append([]string(nil), e.Commits...)
		sort.Strings(commits)
		key := strings.Join(commits, ",")
		if seen[key] {
			return
		}
		seen[key] = true
		out = append(out, e)
	}
	for _, e := range own {
		keep(e)
	}
	for _, entries := range folded {
		for _, e := range entries {
			keep(e)
		}
	}
	return out
}

// addPreReleaseTags records, in the composed lifecycle-and-license record,
// the tag of every pre-release whose tag this repository holds, as an
// unversioned tag: the release model holds stable versions only, so the
// tag releases no version it reads.
func (b *builder) addPreReleaseTags(add func(tag, reason string), d *declarations.Releasables) {
	for _, p := range b.preReleaseTags {
		r, ok := d.Releasable(p.subject)
		if !ok {
			b.p.add("%s holds pre-releases, and %s is no declared releasable, so the tags of its pre-releases cannot be spelled", p.subject, p.subject)
			continue
		}
		scheme, err := workspace.SchemeOf(r)
		if err != nil {
			b.p.add("the tag format of %s: %v", p.subject, err)
			continue
		}
		tag := strings.Replace(scheme.Pattern(), "{version}", p.pr.label, 1)
		_, found, err := b.repo.TagCommit(tag)
		if err != nil {
			b.p.add("reading the tag %s: %v", tag, err)
			continue
		}
		if !found {
			b.note("%s's pre-release %s has no tag %s here, so no unversioned tag is recorded for it", p.subject, p.pr.label, tag)
			continue
		}
		add(tag, fmt.Sprintf("a pre-release tag of the pre-release channel rlsbl dropped: %s's %s, whose changelog entries moved into the %s changelog file", p.subject, p.pr.label, p.destination))
	}
}
