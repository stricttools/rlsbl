package migration

import (
	"encoding/json"
	"fmt"
	"io/fs"
	"path"
	"strings"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/semver"
)

// The modes of the changelog files: a released version's file is read-only.
const (
	unreleasedMode fs.FileMode = 0o644
	releasedMode   fs.FileMode = 0o444
)

// collectExclusions reads a config's batch_limits: the exclusions move onto
// the entries they exempt as batch_reason, and the limits are fixed in code.
func (b *builder) collectExclusions(releasable string, cfg object) {
	limits, ok := cfg.table("batch_limits")
	if !ok {
		return
	}
	for _, k := range limits.keys() {
		switch k {
		case "max_commits_per_entry", "max_entries_per_commit":
			b.note("%s: batch_limits.%s is dropped: the batch limits are fixed at %d commits per entry and %d entries per commit", cfg.file, k, changelog.MaxCommitsPerEntry, changelog.MaxEntriesPerCommit)
		case "exclusions":
		default:
			b.p.add("%s: batch_limits.%s is not a batch_limits key", cfg.file, k)
		}
	}
	items, _ := limits.list("exclusions")
	for i, item := range items {
		eo, ok := limits.child("exclusions", i, item)
		if !ok {
			continue
		}
		for _, k := range eo.keys() {
			if k != "reason" && k != "commits" && k != "entries" {
				b.p.add("%s: %s.%s is not an exclusion key", cfg.file, eo.where, k)
			}
		}
		if commits, ok := eo.list("commits"); ok && len(commits) > 0 {
			b.p.add("%s: %s exempts commits rather than changelog entries, and the exemption now sits on the entry it exempts (batch_reason). Hand edit: delete its commits list (and the exclusion when nothing else is left), then add the entries again with `rlsbl changelog add --batch-reason` after the migration", cfg.file, eo.where)
		}
		reason, _ := eo.str("reason")
		if strings.TrimSpace(reason) == "" {
			b.p.add("%s: %s has no reason, and the batch_reason it becomes must say why the entry is exempt; write one by hand", cfg.file, eo.where)
			continue
		}
		entries, _ := eo.list("entries")
		for j, ent := range entries {
			en, ok := eo.child("entries", j, ent)
			if !ok {
				continue
			}
			version, okV := en.str("version")
			line, okL := en.integer("line")
			if !okV || !okL {
				b.p.add("%s: %s needs a version and a line", cfg.file, en.where)
				continue
			}
			b.exclusions[releasable] = append(b.exclusions[releasable], exclusion{version: version, line: line, reason: strings.TrimSpace(reason), file: cfg.file})
		}
	}
}

// convertChangelog converts the changes/ directory of an old state
// directory into dest: every line restamped to format version 2, an id
// minted where a line has none, and batch_reason set from the exclusions.
// The Markdown copies and the validation cache are deleted. A pre-release's
// file is folded into the file destinations names for it (a stable
// version's, or "unreleased"), which is written when the old layout holds
// none.
func (b *builder) convertChangelog(stateDir, dest, releasable string, preReleases []preRelease, destinations map[string]string) {
	changesDir := stateDir + "/changes"
	used := map[int]bool{}
	excl := b.exclusions[releasable]
	// The files written, by label, each with its sources and own entries.
	type target struct {
		sources []string
		own     []changelog.Entry
	}
	targets := map[string]*target{}
	for _, f := range b.tree.under(changesDir) {
		name := strings.TrimPrefix(f, changesDir+"/")
		if strings.Contains(name, "/") {
			continue
		}
		switch {
		case name == ".validated":
			b.tree.claim(f)
		case strings.HasSuffix(name, ".md"):
			b.tree.claim(f)
		case name == changelog.UnreleasedName:
			b.tree.claim(f)
			if entries, ok := b.convertChangelogEntries(f, "unreleased", excl, used); ok {
				targets["unreleased"] = &target{sources: []string{f}, own: entries}
			}
		case strings.HasSuffix(name, ".jsonl"):
			label := strings.TrimSuffix(name, ".jsonl")
			b.tree.claim(f)
			if _, ok := parsePreRelease(label); ok {
				continue
			}
			if _, err := semver.Parse(label); err != nil {
				b.p.add("%s is a changelog file whose name is no MAJOR.MINOR.PATCH version and names no pre-release of the dropped pre-release channel (%v), and the new layout holds released versions only; rename or remove it by hand", f, err)
				continue
			}
			if entries, ok := b.convertChangelogEntries(f, label, excl, used); ok {
				targets[label] = &target{sources: []string{f}, own: entries}
			}
		}
	}
	folded := map[string][][]changelog.Entry{}
	for _, pr := range preReleases {
		src := changesDir + "/" + pr.label + ".jsonl"
		if !b.tree.has(src) {
			continue
		}
		entries, ok := b.convertChangelogEntries(src, pr.label, excl, used)
		if !ok {
			continue
		}
		to := destinations[pr.label]
		if targets[to] == nil {
			targets[to] = &target{}
		}
		targets[to].sources = append(targets[to].sources, src)
		folded[to] = append(folded[to], entries)
		b.note("%s's entries move into the %s changelog file: the new layout holds stable versions only, and %s was a pre-release of %s", src, to, pr.label, pr.stable)
	}
	for _, label := range sortedKeys(targets) {
		t := targets[label]
		name, mode, change := label+".jsonl", releasedMode, "changelog lines restamped to format_version 2"
		if label == "unreleased" {
			name, mode = changelog.UnreleasedName, unreleasedMode
		}
		if len(folded[label]) > 0 {
			change += ", with the entries of the pre-releases folded in"
		}
		b.writeChangelogFile(t.sources, dest+"/"+name, mode, change, foldEntries(t.own, folded[label]))
	}
	for i, x := range excl {
		if !used[i] {
			b.note("%s: the batch exclusion of line %d of %s.jsonl is dropped: that line does not exist or is within the limit of %d commits", x.file, x.line, x.version, changelog.MaxCommitsPerEntry)
		}
	}
}

// The keys a first-format changelog line may carry.
var oldEntryKeys = map[string]bool{"format_version": true, "id": true, "commits": true, "user_facing": true, "description": true, "type": true, "packages": true}

// convertChangelogEntries reads one first-format changelog file into the
// entries it converts to, and false when it cannot be read.
func (b *builder) convertChangelogEntries(src, label string, excl []exclusion, used map[int]bool) ([]changelog.Entry, bool) {
	data, err := b.tree.read(src)
	if err != nil {
		b.p.add("%v", err)
		return nil, false
	}
	var out []changelog.Entry
	entry := 0
	minted := 0
	for i, text := range strings.Split(string(data), "\n") {
		if strings.TrimSpace(text) == "" {
			continue
		}
		entry++
		where := fmt.Sprintf("line %d of %s", i+1, src)
		e, ok := b.convertEntry(where, text, &minted)
		if !ok {
			continue
		}
		for j, x := range excl {
			if x.version == label && x.line == int64(entry) && len(e.Commits) > changelog.MaxCommitsPerEntry {
				e.BatchReason = x.reason
				used[j] = true
			}
		}
		if err := changelog.Validate(e); err != nil {
			b.p.add("%s converts to a line the new changelog reader refuses (%v); repair the line by hand", where, err)
			continue
		}
		out = append(out, e)
	}
	if minted > 0 {
		b.note("%s: %d entries carried no id the new format accepts, so each got a newly minted one", src, minted)
	}
	return out, true
}

// writeChangelogFile plans one changelog file of the new layout holding
// entries.
func (b *builder) writeChangelogFile(sources []string, dst string, mode fs.FileMode, change string, entries []changelog.Entry) {
	var lines []string
	for _, e := range entries {
		lines = append(lines, changelog.Serialize(e))
	}
	content := ""
	if len(lines) > 0 {
		content = strings.Join(lines, "\n") + "\n"
	}
	b.addWrite(write{path: dst, sources: sources, change: change, data: []byte(content), mode: mode})
}

// convertEntry reads one first-format line.
func (b *builder) convertEntry(where, text string, minted *int) (changelog.Entry, bool) {
	dec := json.NewDecoder(strings.NewReader(text))
	dec.UseNumber()
	var value any
	if err := dec.Decode(&value); err != nil {
		b.p.add("%s is not JSON (%v); repair it by hand", where, err)
		return changelog.Entry{}, false
	}
	o, ok := newObject(where, value, b.p)
	if !ok {
		return changelog.Entry{}, false
	}
	for _, k := range o.keys() {
		if !oldEntryKeys[k] {
			b.p.add("%s carries %s, which no changelog line has; delete it by hand", where, k)
			return changelog.Entry{}, false
		}
	}
	if v, ok := o.integer("format_version"); ok && v != 1 && v != changelog.FormatVersion {
		b.p.add("%s carries format_version %d, which no rlsbl wrote", where, v)
		return changelog.Entry{}, false
	}
	var e changelog.Entry
	e.ID, _ = o.str("id")
	if !isEntryID(e.ID) {
		e.ID = changelog.NewID()
		*minted++
	}
	commits, ok := o.strings("commits")
	if !ok {
		b.p.add("%s names no commits list", where)
		return changelog.Entry{}, false
	}
	e.Commits = commits
	userFacing, ok := o.boolean("user_facing")
	if !ok {
		b.p.add("%s carries no user_facing; add true or false by hand", where)
		return changelog.Entry{}, false
	}
	e.UserFacing = userFacing
	e.Description, _ = o.str("description")
	e.Type, _ = o.str("type")
	if packages, ok := o.strings("packages"); ok {
		e.Packages = packages
	}
	return e, true
}

// isEntryID reports an id of the format the Python minted: 48 lowercase
// hexadecimal digits.
func isEntryID(id string) bool {
	if len(id) != 48 {
		return false
	}
	for _, c := range id {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}

// planChangelogs plans the changelogs regenerated from the converted
// records: each releasable's CHANGELOG.md and, in a workspace, the roll-up.
func (b *builder) planChangelogs(d *declarations.Releasables) {
	seen := map[string]bool{}
	for _, r := range d.Releasables {
		home := changelog.Home(d, r.Name)
		if !seen[home] {
			seen[home] = true
			b.plan.generated = append(b.plan.generated, home)
		}
	}
	if d.IsWorkspace() && !seen[changelog.RollUpPath] {
		b.plan.generated = append(b.plan.generated, changelog.RollUpPath)
	}
}

// claimGeneratedChangelog claims an old generated CHANGELOG.md in a state
// directory: the new one is regenerated from the converted records.
func (b *builder) claimGeneratedChangelog(stateDir string) {
	rel := path.Join(stateDir, changelog.MarkdownName)
	if b.tree.has(rel) {
		b.tree.claim(rel)
	}
}
