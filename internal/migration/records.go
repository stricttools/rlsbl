package migration

import (
	"bytes"
	"encoding/json"
	"fmt"
	"path"
	"strings"
	"time"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/semver"
)

// convertState converts a releasable's old state directory (or a retired
// subject's): its changelog, archives, release file, version file, undo
// audits, history-rewrite archives, and retry file.
func (b *builder) convertState(name, stateDir string, retired bool) {
	if stateDir == "" {
		return
	}
	changelogDest := declarations.ChangelogDir(name)
	releasesDest := releaserecord.ArchiveDir(name)
	if retired {
		changelogDest = declarations.RetiredHistoryDir(name) + "/changelog"
		releasesDest = releaserecord.RetiredArchiveDir(name)
	}
	b.convertChangelog(stateDir, changelogDest, name)
	b.claimGeneratedChangelog(stateDir)
	releasesDir := stateDir + "/releases"
	for _, f := range b.tree.under(releasesDir) {
		base := strings.TrimPrefix(f, releasesDir+"/")
		if strings.Contains(base, "/") {
			continue
		}
		switch {
		case base == releaserecord.ReleaseFileName:
			b.tree.claim(f)
			if retired {
				b.note("%s is dropped: %s's release history is closed, so it releases nothing", f, name)
				continue
			}
			b.convertReleaseFile(f, releaserecord.ReleaseFilePath(name))
		case base == "retry.toml":
			b.tree.claim(f)
			if retired {
				b.note("%s is dropped: %s's release history is closed", f, name)
				continue
			}
			b.convertRetry(f, name)
		case strings.HasPrefix(base, "v") && strings.HasSuffix(base, ".toml"):
			v, err := semver.Parse(strings.TrimSuffix(strings.TrimPrefix(base, "v"), ".toml"))
			b.tree.claim(f)
			if err != nil {
				b.p.add("%s is an archive whose name carries no MAJOR.MINOR.PATCH version (%v), and the new record holds release versions only; rename or remove it by hand", f, err)
				continue
			}
			b.convertArchive(f, releaserecord.ArchivePath(releasesDest, v), v)
		}
	}
	if stateDir != oldDir {
		// A standalone project's .rlsbl/version is the scaffold marker; a
		// workspace releasable's and a retired subject's is its version.
		versionFile := stateDir + "/version"
		if b.tree.has(versionFile) {
			b.tree.claim(versionFile)
			dest := declarations.VersionFile(name)
			if retired {
				dest = releasesDest + "/version"
			}
			b.copyVerbatim(versionFile, dest, "the version, unchanged")
		}
	}
	audit := stateDir + "/undo-audit.json"
	if b.tree.has(audit) {
		b.tree.claim(audit)
		dest := declarations.ReleasesDir(name) + "/undo-audits.jsonl"
		if retired {
			dest = releasesDest + "/undo-audits.jsonl"
		}
		b.convertUndoAudits(audit, dest)
	}
	scrubs := stateDir + "/scrubs"
	for _, f := range b.tree.under(scrubs) {
		b.tree.claim(f)
		b.convertScrubArchive(f)
	}
}

// copyVerbatim plans a file copied unchanged.
func (b *builder) copyVerbatim(src, dst, change string) {
	data, err := b.tree.read(src)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	b.addWrite(write{path: dst, sources: []string{src}, change: change, data: data})
}

// convertArchive converts one archive: format_version 2, candidate_sha
// renamed release_commit, and tree_hashes renamed released_trees, every
// other byte kept. The result must read as an archive of the new format.
func (b *builder) convertArchive(src, dst string, v semver.Version) {
	data, err := b.tree.read(src)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	doc, err := tomledit.Parse(data)
	if err != nil {
		b.p.add("%s is not valid TOML: %v", src, err)
		return
	}
	fields, _ := tomledit.Unmarshal[map[string]any](data)
	if fields != nil {
		for _, retired := range []string{"preid", "blog"} {
			if _, ok := (*fields)[retired]; ok {
				b.p.add("%s carries %s, a field of a channel rlsbl dropped. Hand edit: delete the %s line", src, retired, retired)
				return
			}
		}
	}
	if err := doc.SetCreate("format_version", int64(releaserecord.FormatVersion)); err != nil {
		b.p.add("%s: stamping format_version: %v", src, err)
		return
	}
	for _, rename := range []struct{ from, to string }{{"candidate_sha", "release_commit"}, {"tree_hashes", "released_trees"}} {
		if fields == nil {
			break
		}
		if _, ok := (*fields)[rename.from]; !ok {
			continue
		}
		if err := doc.RenameKey(rename.from, rename.to); err != nil {
			b.p.add("%s: renaming %s to %s: %v", src, rename.from, rename.to, err)
			return
		}
	}
	out := doc.Bytes()
	if _, err := releaserecord.ParseArchive(dst, v, out); err != nil {
		b.p.add("%s converts to an archive the new release record refuses (%v); repair %s by hand", src, err, src)
		return
	}
	b.addWrite(write{path: dst, sources: []string{src}, change: "archive restamped to format_version 2 (candidate_sha is release_commit, tree_hashes is released_trees)", data: out, mode: releasedMode})
}

// convertReleaseFile converts the editable release file. An unfilled one
// (no bump or no description) is dropped: `rlsbl release init` writes a new
// one.
func (b *builder) convertReleaseFile(src, dst string) {
	data, err := b.tree.read(src)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	fields, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		b.p.add("%s is not valid TOML: %v", src, err)
		return
	}
	for _, retired := range []string{"preid", "blog"} {
		if _, ok := (*fields)[retired]; ok {
			b.p.add("%s carries %s, a field of a channel rlsbl dropped, so the release it describes cannot be converted. Hand edit: delete the %s line, then migrate", src, retired, retired)
			return
		}
	}
	if bump, _ := (*fields)["bump"].(string); bump == "prerelease" {
		b.p.add("%s declares bump = \"prerelease\", and rlsbl has no pre-release channel. Hand edit: declare patch, minor, major, or infra", src)
		return
	}
	bump, _ := (*fields)["bump"].(string)
	description, _ := (*fields)["description"].(string)
	if strings.TrimSpace(bump) == "" || strings.TrimSpace(description) == "" {
		b.note("%s is dropped: it declares no bump or no description, so it is an unfilled release file; `rlsbl release init` writes a new one", src)
		return
	}
	doc, err := tomledit.Parse(data)
	if err != nil {
		b.p.add("%s is not valid TOML: %v", src, err)
		return
	}
	if err := doc.SetCreate("format_version", int64(releaserecord.FormatVersion)); err != nil {
		b.p.add("%s: stamping format_version: %v", src, err)
		return
	}
	out := doc.Bytes()
	if _, err := releaserecord.ParseReleaseFile(dst, out); err != nil {
		b.p.add("%s converts to a release file the new reader refuses (%v); repair it by hand", src, err)
		return
	}
	b.addWrite(write{path: dst, sources: []string{src}, change: "release file restamped to format_version 2", data: out})
}

// convertRetry converts a retry file: its ref (or tag) and dispatch list.
func (b *builder) convertRetry(src, name string) {
	doc, _, err := b.tree.readTOML(src)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	o, _ := newObject(src, doc, b.p)
	ref, _ := o.str("ref")
	if ref == "" {
		ref, _ = o.str("tag")
	}
	workflows, _ := o.strings("dispatch")
	retry := runstate.Retry{Ref: ref, Workflows: workflows}
	dst := runstate.RetryPath(name)
	data := runstate.RenderRetry(retry)
	if _, err := runstate.ParseRetry(dst, data); err != nil {
		b.p.add("%s converts to a retry file the new reader refuses (%v); finish the retry with the Python rlsbl, or delete the file through saferm, then migrate", src, err)
		return
	}
	b.addWrite(write{path: dst, sources: []string{src}, change: "run state: the workflows a retry dispatches", data: data, ignored: true})
	b.addReleaseStateGitignore()
}

// releaseStateGitignore keeps the run-state directory's content out of git.
const releaseStateGitignore = "*\n!.gitignore\n"

// addReleaseStateGitignore plans the run-state directory's .gitignore once.
func (b *builder) addReleaseStateGitignore() {
	rel := declarations.ReleaseStateDir + "/.gitignore"
	for _, w := range b.plan.writes {
		if w.path == rel {
			return
		}
	}
	if ok, _ := exists(b.root, rel); ok {
		return
	}
	b.addWrite(write{path: rel, change: "keeps the run state out of git", data: []byte(releaseStateGitignore)})
}

// convertUndoAudits converts undo-audit.json, a JSON array, into one JSON
// object per line, each carrying format_version 1 ahead of its fields.
func (b *builder) convertUndoAudits(src, dst string) {
	data, err := b.tree.read(src)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	var records []json.RawMessage
	if err := json.Unmarshal(data, &records); err != nil {
		b.p.add("%s is not a JSON array of undo audits (%v); repair it by hand", src, err)
		return
	}
	if len(records) == 0 {
		b.note("%s holds no undo audit and is deleted", src)
		return
	}
	var lines []string
	for i, r := range records {
		var compact bytes.Buffer
		if err := json.Compact(&compact, r); err != nil || !bytes.HasPrefix(compact.Bytes(), []byte("{")) {
			b.p.add("%s: audit %d is not a JSON object; repair it by hand", src, i)
			return
		}
		body := strings.TrimPrefix(compact.String(), "{")
		if body == "}" {
			lines = append(lines, `{"format_version":1}`)
			continue
		}
		lines = append(lines, `{"format_version":1,`+body)
	}
	b.addWrite(write{path: dst, sources: []string{src}, change: "one undo audit per line", data: []byte(strings.Join(lines, "\n") + "\n")})
}

// The modes a scrub archive names, old to new.
var scrubModes = map[string]string{"match": "pattern", "pattern": "pattern", "file": "file", "recipe": "recipe"}

// convertScrubArchive converts a scrub archive into a history-rewrite
// archive, named by its new head's committer date.
func (b *builder) convertScrubArchive(src string) {
	value, _, err := b.tree.readJSON(src)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	o, ok := newObject(src, value, b.p)
	if !ok {
		return
	}
	a := releaserecord.RewriteArchive{Operation: releaserecord.OperationScrub, Rewrites: map[string]string{}}
	mode, _ := o.str("mode")
	a.Mode = scrubModes[mode]
	if a.Mode == "" {
		b.p.add("%s names the scrub mode %q, which no scrub had", src, mode)
		return
	}
	a.Reason, _ = o.str("reason")
	a.OldHead, _ = o.str("old_head")
	a.NewHead, _ = o.str("new_head")
	if n, ok := o.integer("commits_rewritten"); ok {
		a.CommitsRewritten = int(n)
	}
	if rewrites, ok := o.table("rewrites"); ok {
		for _, old := range rewrites.keys() {
			a.Rewrites[old], _ = rewrites.str(old)
		}
	}
	if tags, ok := o.list("tags"); ok {
		for i, item := range tags {
			to, ok := o.child("tags", i, item)
			if !ok {
				continue
			}
			var t releaserecord.ArchivedTag
			t.Refname, _ = to.str("refname")
			t.OldSHA, _ = to.str("old_sha")
			t.NewSHA, _ = to.str("new_sha")
			t.Annotated, _ = to.boolean("annotated")
			a.Tags = append(a.Tags, t)
		}
	}
	if a.OldHead == "" || a.NewHead == "" || a.Reason == "" {
		b.p.add("%s lacks its old_head, new_head, or reason; repair it by hand", src)
		return
	}
	started, err := b.repo.CommitterDate(a.NewHead)
	if err != nil {
		b.p.add("%s: the new head %s, whose committer date names the history-rewrite archive, cannot be read (%v)", src, a.NewHead, err)
		return
	}
	dst := releaserecord.RewriteArchivePath(started)
	b.addWrite(write{path: dst, sources: []string{src}, change: "the history-rewrite archive of a scrub", writer: func(e *strictcli.Effects, root string) error {
		_, err := releaserecord.WriteRewriteArchive(e, root, started, a)
		return err
	}})
}

// convertBatchReleases converts a workspace's batch release file, its
// archives, and the plan of a batch in progress.
func (b *builder) convertBatchReleases() {
	if b.d.Layout != declarations.LayoutWorkspace {
		return
	}
	dir := oldWorkspaceDir + "/releases"
	for _, f := range b.tree.under(dir) {
		base := strings.TrimPrefix(f, dir+"/")
		if strings.Contains(base, "/") {
			continue
		}
		switch {
		case base == releaserecord.ReleaseFileName:
			b.tree.claim(f)
			b.convertBatchReleaseFile(f)
		case base == "unreleased.plan.json":
			b.tree.claim(f)
			b.convertBatchPlan(f)
		case strings.HasPrefix(base, "batch-") && strings.HasSuffix(base, ".plan.json"):
			b.tree.claim(f)
		case strings.HasPrefix(base, "batch-") && strings.HasSuffix(base, ".toml"):
			b.tree.claim(f)
			b.convertBatchArchive(f, strings.TrimSuffix(strings.TrimPrefix(base, "batch-"), ".toml"))
		}
	}
}

// renamePackages renames a batch document's [packages] tables to
// [releasables] and stamps format_version 2.
func renamePackages(data []byte) ([]byte, error) {
	doc, err := tomledit.Parse(data)
	if err != nil {
		return nil, err
	}
	if fields, err := tomledit.Unmarshal[map[string]any](data); err == nil {
		if _, ok := (*fields)["packages"]; ok {
			if _, clash := (*fields)["releasables"]; clash {
				return nil, fmt.Errorf("it holds both [packages] and [releasables] tables")
			}
			if err := doc.RenameKey("packages", "releasables"); err != nil {
				return nil, err
			}
		}
	}
	if err := doc.SetCreate("format_version", int64(releaserecord.FormatVersion)); err != nil {
		return nil, err
	}
	return doc.Bytes(), nil
}

func (b *builder) convertBatchReleaseFile(src string) {
	data, err := b.tree.read(src)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	fields, err := tomledit.Unmarshal[map[string]any](data)
	if err != nil {
		b.p.add("%s is not valid TOML: %v", src, err)
		return
	}
	if _, ok := (*fields)["packages"]; ok {
		b.p.add("%s holds [packages] tables, which the Python already refused: a batch release names releasables. Hand edit: rename each [packages.<name>] to [releasables.<name>], naming releasables", src)
		return
	}
	tables, _ := (*fields)["releasables"].(map[string]any)
	filled := false
	for _, t := range tables {
		if m, ok := t.(map[string]any); ok {
			bump, _ := m["bump"].(string)
			description, _ := m["description"].(string)
			if strings.TrimSpace(bump) != "" || strings.TrimSpace(description) != "" {
				filled = true
			}
		}
	}
	if !filled {
		b.note("%s is dropped: no releasable in it declares a bump or a description, so it is unfilled; `rlsbl monorepo release init` writes a new one", src)
		return
	}
	out, err := renamePackages(data)
	if err != nil {
		b.p.add("%s: %v", src, err)
		return
	}
	if _, err := releaserecord.ParseBatchReleaseFile(releaserecord.BatchReleaseFilePath, out); err != nil {
		b.p.add("%s converts to a batch release file the new reader refuses (%v); repair it by hand", src, err)
		return
	}
	b.addWrite(write{path: releaserecord.BatchReleaseFilePath, sources: []string{src}, change: "batch release file restamped to format_version 2", data: out})
}

// convertBatchArchive converts an archived batch release, named by the
// local time the Python wrote into its name, to its UTC name. Its tables
// are kept as written, [packages] renamed [releasables].
func (b *builder) convertBatchArchive(src, stamp string) {
	at, err := time.ParseInLocation("20060102-150405", stamp, time.Local)
	if err != nil {
		b.p.add("%s is a batch archive whose name carries no time (%v); rename or remove it by hand", src, err)
		return
	}
	data, err := b.tree.read(src)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	out, err := renamePackages(data)
	if err != nil {
		b.p.add("%s: %v", src, err)
		return
	}
	b.addWrite(write{path: releaserecord.BatchArchivePath(at), sources: []string{src}, change: "batch archive restamped to format_version 2", data: out})
}

// convertBatchPlan converts the plan of a batch release in progress into
// run state.
func (b *builder) convertBatchPlan(src string) {
	value, _, err := b.tree.readJSON(src)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	o, ok := newObject(src, value, b.p)
	if !ok {
		return
	}
	if section, ok := o.str("section_type"); ok && section != "releasables" {
		b.p.add("%s plans a batch of %s, which no rlsbl reads; finish or abandon that batch with the Python rlsbl, delete the plan through saferm, then migrate", src, section)
		return
	}
	items, _ := o.list("items")
	var plan runstate.BatchPlan
	for i, item := range items {
		io, ok := o.child("items", i, item)
		if !ok {
			continue
		}
		var it runstate.BatchPlanItem
		it.Name, _ = io.str("name")
		it.BaseVersion, _ = io.str("base_version")
		it.TargetVersion, _ = io.str("target_version")
		it.Tag, _ = io.str("tag")
		it.Registry, _ = io.str("registry")
		it.Bump, _ = io.str("bump")
		plan.Items = append(plan.Items, it)
	}
	data := runstate.RenderBatchPlan(plan)
	if _, err := runstate.ParseBatchPlan(runstate.BatchPlanPath, data); err != nil {
		b.p.add("%s converts to a batch plan the new reader refuses (%v); finish or abandon the batch with the Python rlsbl, or delete the plan through saferm, then migrate", src, err)
		return
	}
	b.addWrite(write{path: runstate.BatchPlanPath, sources: []string{src}, change: "run state: the plan of the batch release in progress", data: data, ignored: true})
	b.addReleaseStateGitignore()
}

// convertRetiredHistories moves the release state of every subject whose
// release history the transition record closes into
// .strictmetadata/retired-release-histories/<subject>/.
func (b *builder) convertRetiredHistories() {
	closed := oldDir + "/closed-histories"
	for _, name := range b.tree.children(closed) {
		dir := closed + "/" + name
		if len(b.tree.under(dir)) == 0 {
			continue
		}
		if b.retired[name] != "" {
			b.p.add("%s's release state is in two old places (%s and %s); keep one by hand", name, b.retired[name], dir)
			continue
		}
		b.retired[name] = dir
	}
	for _, subject := range sortedKeys(b.retired) {
		dir := b.retired[subject]
		if dir == "" {
			continue
		}
		if !b.history.closed[subject] {
			b.p.add("%s/ holds the release state of %q, which no releasable is declared as and whose release history the transition record does not close. Declare the releasable, or record the closed history with `rlsbl transition record --release-history-closed %s` (the Python rlsbl 0.131.0), then migrate", dir, subject, subject)
			continue
		}
		b.convertState(subject, dir, true)
		cfg := dir + "/" + oldConfigName
		if b.tree.has(cfg) {
			b.tree.claim(cfg)
			b.copyVerbatim(cfg, declarations.RetiredHistoryDir(subject)+"/"+oldConfigName, "the retired subject's last config, kept verbatim and read by no command")
		}
		for _, sub := range []string{"hooks", "bases", "lint"} {
			for _, f := range b.tree.under(dir + "/" + sub) {
				b.tree.claim(f)
			}
		}
	}
	for _, subject := range sortedKeys(b.history.closed) {
		if _, ok := b.d.Releasable(subject); ok {
			b.p.add("the transition record closes the release history of %q, yet it is a declared releasable; remove the releasable from the declarations by hand, or the event (it was declared in error)", subject)
		}
	}
}

// archiveDirectory is where a subject's converted archives go.
func archiveDirectory(subject string, retired bool) string {
	if retired {
		return releaserecord.RetiredArchiveDir(subject)
	}
	return releaserecord.ArchiveDir(subject)
}

// plannedArchive is the converted archive of v in dir, read from the plan.
func (b *builder) plannedArchive(dir string, v semver.Version) (releaserecord.Archive, bool) {
	rel := releaserecord.ArchivePath(dir, v)
	for _, w := range b.plan.writes {
		if w.path == rel {
			a, err := releaserecord.ParseArchive(rel, v, w.data)
			if err != nil {
				return releaserecord.Archive{}, false
			}
			return a, true
		}
	}
	return releaserecord.Archive{}, false
}

// plannedArchives are every converted archive in dir.
func (b *builder) plannedArchives(dir string) []releaserecord.Archive {
	var out []releaserecord.Archive
	for _, w := range b.plan.writes {
		if path.Dir(w.path) != dir || !strings.HasPrefix(path.Base(w.path), "v") || !strings.HasSuffix(w.path, ".toml") {
			continue
		}
		v, err := semver.Parse(strings.TrimSuffix(strings.TrimPrefix(path.Base(w.path), "v"), ".toml"))
		if err != nil {
			continue
		}
		if a, ok := b.plannedArchive(dir, v); ok {
			out = append(out, a)
		}
	}
	return out
}
