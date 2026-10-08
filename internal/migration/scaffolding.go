package migration

import (
	"crypto/sha256"
	"encoding/hex"
	"path"
	"sort"
	"strings"
	"unicode"

	tomledit "github.com/stricttools/go-toml-edit"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/scaffold"
	"github.com/stricttools/rlsbl/internal/semver"
)

// stateOwner is one old directory holding scaffold records: its member's
// path, and where the member's merge bases are kept.
type stateOwner struct {
	member string
	dir    string
	// bases is the directory of the member's merge bases.
	bases string
}

// scaffoldOwners are every old directory that may hold a managed-files
// registry, merge bases, or a scaffold marker.
func (b *builder) scaffoldOwners() []stateOwner {
	var out []stateOwner
	for _, member := range sortedKeys(b.memberDirs) {
		dir := b.memberDirs[member]
		out = append(out, stateOwner{member: member, dir: dir, bases: dir + "/bases"})
	}
	if b.d.Layout == declarations.LayoutWorkspace {
		for _, r := range b.d.Releasables {
			dir := b.stateDirs[r.Name]
			for _, m := range b.d.MembersOf(r.Name) {
				out = append(out, stateOwner{member: m.Path, dir: dir, bases: joinRel(dir+"/bases", m.Path)})
			}
		}
	}
	return out
}

// droppedManagedPath reports a managed path the new scaffold state does not
// carry: anything in the old layout, which the migration removes, and the
// changelogs, which the migration regenerates and scaffold no longer
// manages.
func droppedManagedPath(rel string) bool {
	for _, part := range strings.Split(rel, "/") {
		if part == oldDir || part == oldWorkspaceDir {
			return true
		}
	}
	return path.Base(rel) == "CHANGELOG.md"
}

// convertScaffoldState converts every managed-files.json and scaffold
// marker into the one scaffold state, and the merge bases of the files it
// still manages into .strictmetadata/.scaffold-bases/.
func (b *builder) convertScaffoldState() {
	files := map[string]string{}
	var sources []string
	version := ""
	for _, owner := range b.scaffoldOwners() {
		registry := owner.dir + "/managed-files.json"
		if b.tree.has(registry) && !b.tree.claimed[registry] {
			b.tree.claim(registry)
			sources = append(sources, registry)
			b.readManagedFiles(registry, owner.member, files)
		}
		marker := owner.dir + "/version"
		isMarker := owner.dir == oldDir || strings.HasSuffix(owner.dir, "/"+oldDir)
		if isMarker && b.tree.has(marker) && !b.tree.claimed[marker] {
			b.tree.claim(marker)
			sources = append(sources, marker)
			data, err := b.tree.read(marker)
			if err != nil {
				b.p.add("%v", err)
				continue
			}
			v := strings.TrimSpace(string(data))
			parsed, err := semver.Parse(v)
			if err != nil {
				b.p.add("%s names the rlsbl version %q, which is no MAJOR.MINOR.PATCH version; repair it by hand", marker, v)
				continue
			}
			if version == "" {
				version = v
			} else if current, _ := semver.Parse(version); semver.Compare(parsed, current) > 0 {
				version = v
			}
		}
	}
	for _, owner := range b.scaffoldOwners() {
		for _, f := range b.tree.under(owner.bases) {
			if b.tree.claimed[f] {
				continue
			}
			managed := joinRel(owner.member, strings.TrimPrefix(f, owner.bases+"/"))
			b.tree.claim(f)
			if _, ok := files[managed]; !ok {
				continue
			}
			b.copyVerbatim(f, scaffold.BasePath(managed), "the merge base of a managed file")
		}
	}
	if len(files) == 0 && version == "" {
		return
	}
	if version == "" {
		version = b.req.RlsblVersion
		b.note("no old scaffold marker names the rlsbl version that last scaffolded; the scaffold state records %s, the version running the migration", version)
	}
	text := scaffold.RenderState(scaffold.State{RlsblVersion: version, Files: files})
	if _, err := scaffold.ParseState([]byte(text)); err != nil {
		b.p.add("the converted scaffold state is refused by its reader: %v", err)
		return
	}
	b.addWrite(write{path: declarations.ScaffoldStateFile, sources: sources, change: "the managed files and the rlsbl version that last scaffolded", data: []byte(text)})
}

func (b *builder) readManagedFiles(rel, member string, files map[string]string) {
	value, _, err := b.tree.readJSON(rel)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	o, ok := newObject(rel, value, b.p)
	if !ok {
		return
	}
	for _, k := range o.keys() {
		if k != "version" && k != "files" {
			b.p.add("%s: %s is not a managed-files key", rel, k)
		}
	}
	table, ok := o.table("files")
	if !ok {
		return
	}
	for _, p := range table.keys() {
		hash, _ := table.str(p)
		canonical, ok := declarations.CanonicalPath(p)
		if !ok || canonical == declarations.RootPath {
			b.p.add("%s names the managed file %q, which is no path inside the member", rel, p)
			continue
		}
		managed := joinRel(member, canonical)
		if droppedManagedPath(managed) {
			continue
		}
		if prior, ok := files[managed]; ok && prior != hash {
			b.p.add("%s and another registry record %s with different hashes; keep one by hand", rel, managed)
			continue
		}
		files[managed] = hash
	}
}

// The content hashes (SHA-256 of the text with trailing whitespace
// removed) of every release hook script a Python scaffold shipped. A script
// matching one carries nothing of its owner's and is deleted.
var shippedHookHashes = map[string]map[string]bool{
	"pre-checks.sh": {
		"920e3e67214ccfd46d5fd06dc26f6d06ab6cb7966f4e5a2ff60844e7598c5dc1": true,
	},
	"pre-release.sh": {
		"00dcee4e92eb5ba8b2e4061f670bbb888ca29a30f9c723f2ceb42e4b446d0470": true,
		"081a50bd2a9de34aca2c14292b7b8c169ae9484e3668d3a713066d71c2a620c0": true,
		"24ce620320b45fd1f6609a3ace06a2a83ba44345ba78d69a271c67ea0e082a01": true,
		"838f81546bfc178f1b31b6fb8d3ace95ad59e89fd9ecb5b48ea0559127f1d61b": true,
		"b0d0bbbdb51d55d4704f189a0753766e290f1411b3df7192573ec7c789e104c7": true,
		"b8e40d299698a9c0d40eb403fbbc2289cc86bfb72640354e84093122860474b0": true,
		"bfea383a77c752ef12e894155d17db2127956d95890ee7e5148eed2a226a3462": true,
		"f1fa0719da4e07f12d0233bc307c7c6ec827b6a9bc4f91e8cac3b61eef5eabf8": true,
	},
	"post-release.sh": {
		"cd790393122dfdc361cd6bdd393777b805f2462ae94b601840dcda8798b71efb": true,
		"f68ebec58899c9b1e5c4b20ad59012aa31ba1dfc08bcb2148133c693947514ed": true,
	},
}

// hookPoints maps each script to the declarations' hook point.
var hookPoints = map[string]string{"pre-checks.sh": "pre_checks", "pre-release.sh": "pre_release", "post-release.sh": "post_release"}

// hookContentHash is the hash shippedHookHashes holds for content.
func hookContentHash(content string) string {
	sum := sha256.Sum256([]byte(strings.TrimRightFunc(content, unicode.IsSpace)))
	return hex.EncodeToString(sum[:])
}

// hookScriptOwner is one old hooks/ directory and whose hooks it held.
type hookScriptOwner struct {
	dir string
	// name is the releasable or member the scripts belong to.
	name string
	// member is set when the owner is a member; its path, from which its
	// hooks run.
	member     string
	isMember   bool
	releasable bool
}

// convertHookScripts deletes every hook script a scaffold shipped
// unchanged, and moves each customized one to
// .strictmetadata/release-hooks/<owner>/, adding a declaration that runs it
// to the same owner's hooks.
func (b *builder) convertHookScripts() {
	var owners []hookScriptOwner
	if b.d.Layout == declarations.LayoutStandalone && len(b.d.Releasables) == 1 {
		owners = append(owners, hookScriptOwner{dir: oldDir + "/hooks", name: b.d.Releasables[0].Name, releasable: true})
	} else {
		for _, r := range b.d.Releasables {
			owners = append(owners, hookScriptOwner{dir: b.stateDirs[r.Name] + "/hooks", name: r.Name, releasable: true})
		}
		for _, m := range b.d.Members {
			if dir, ok := b.memberDirs[m.Path]; ok {
				owners = append(owners, hookScriptOwner{dir: dir + "/hooks", name: m.Name, member: m.Path, isMember: true})
			}
		}
	}
	for _, owner := range owners {
		for _, f := range b.tree.under(owner.dir) {
			script := strings.TrimPrefix(f, owner.dir+"/")
			point, ok := hookPoints[script]
			if !ok {
				continue
			}
			b.tree.claim(f)
			data, err := b.tree.read(f)
			if err != nil {
				b.p.add("%v", err)
				continue
			}
			if shippedHookHashes[script][hookContentHash(string(data))] {
				continue
			}
			if b.declaredHookSlots[owner.name][point] {
				b.p.add("%s is a customized hook script, and its owner's config declares hooks.%s, so the Python never ran the script. Hand edit: move its commands into hooks.%s of the config, or delete it through saferm, then migrate", f, point, point)
				continue
			}
			dst := declarations.ReleaseHooksDir(owner.name) + "/" + script
			b.addWrite(write{path: dst, sources: []string{f}, change: "a customized hook script, now run by a hooks." + point + " declaration", data: data, mode: 0o755})
			command := "bash " + relativeFrom(owner.member, dst)
			if !owner.isMember {
				command = b.releasableHookCommand(owner.name, dst)
			}
			hook := declarations.Hook{Command: command}
			if owner.isMember {
				for i := range b.d.Members {
					if b.d.Members[i].Path == owner.member {
						appendHook(&b.d.Members[i].Hooks, point, hook)
					}
				}
			} else {
				for i := range b.d.Releasables {
					if b.d.Releasables[i].Name == owner.name {
						appendHook(&b.d.Releasables[i].Hooks, point, hook)
					}
				}
			}
		}
	}
}

// releasableHookCommand runs the script at the repository-relative path
// dst from the directory a releasable's hooks run from: the representative
// member's, which is any member the release is started from. When every
// member of the releasable sits at one depth, the path is relative to that
// depth; otherwise no one relative path reaches the script from every
// member, and the command finds the repository root through git.
func (b *builder) releasableHookCommand(releasable, dst string) string {
	prefix, first := "", true
	for _, m := range b.d.Members {
		if m.Releasable != releasable {
			continue
		}
		p := relativeFrom(m.Path, "")
		if first {
			prefix, first = p, false
		} else if p != prefix {
			return `bash "$(git rev-parse --show-toplevel)/` + dst + `"`
		}
	}
	return "bash " + prefix + dst
}

func appendHook(h *declarations.Hooks, point string, hook declarations.Hook) {
	switch point {
	case "pre_checks":
		h.PreChecks = append(h.PreChecks, hook)
	case "pre_release":
		h.PreRelease = append(h.PreRelease, hook)
	case "post_release":
		h.PostRelease = append(h.PostRelease, hook)
	}
}

// relativeFrom is the repository-relative path rel as seen from the
// repository-relative directory dir.
func relativeFrom(dir, rel string) string {
	if dir == "" || dir == declarations.RootPath {
		return rel
	}
	return strings.Repeat("../", strings.Count(dir, "/")+1) + rel
}

// The forbidden imports each language's library lint shipped, which are
// also strictcode's defaults, so a list equal to them carries nothing.
var shippedForbiddenImports = map[string][]string{
	"python": {"argparse", "click", "typer", "flask", "fastapi", "django", "uvicorn", "granian", "starlette", "tornado", "bottle"},
	"npm":    {"express", "koa", "hono", "commander", "yargs"},
	"go":     {"net/http", "github.com/spf13/cobra", "github.com/urfave/cli"},
}

// strictcodeLanguages maps the library lint's languages to strictcode's.
var strictcodeLanguages = map[string]string{"python": "py", "npm": "ts", "go": "go"}

// libraryLint is what the library lint configs carry that strictcode.toml
// keeps, per strictcode language.
type libraryLint struct {
	forbidden   map[string]sourcedList
	allow       map[string]sourcedList
	stdoutAllow map[string]sourcedList
}

type sourcedList struct {
	values []string
	file   string
}

// set records list for language, refusing a different list from another
// file: strictcode.toml holds one list per language for the repository.
func (b *builder) setLintList(m map[string]sourcedList, key, language string, list sourcedList) {
	if prior, ok := m[language]; ok {
		if strings.Join(prior.values, "\x00") != strings.Join(list.values, "\x00") {
			b.p.add("%s and %s set different %s lists for %s, and strictcode.toml holds one for the repository; make them agree by hand", prior.file, list.file, key, language)
		}
		return
	}
	m[language] = list
}

// convertMemberLeftovers converts what the old directories held beside the
// records: the library lint configs, dead-modules.toml, and the private
// module stubs; and refuses per-member release state.
func (b *builder) convertMemberLeftovers() {
	b.lint = libraryLint{forbidden: map[string]sourcedList{}, allow: map[string]sourcedList{}, stdoutAllow: map[string]sourcedList{}}
	// A workspace's scaffold kept .rlsbl-monorepo/ out of the root's Go
	// module with a stub of its own.
	if f := oldWorkspaceDir + "/go.mod"; b.tree.has(f) {
		b.tree.claim(f)
		b.rootStub = f
	}
	type leftoverDir struct {
		dir    string
		member string
	}
	var dirs []leftoverDir
	for _, member := range sortedKeys(b.memberDirs) {
		dirs = append(dirs, leftoverDir{b.memberDirs[member], member})
	}
	for _, r := range b.d.Releasables {
		if dir := b.stateDirs[r.Name]; dir != oldDir {
			dirs = append(dirs, leftoverDir{dir, ""})
		}
	}
	for _, ld := range dirs {
		for _, f := range b.tree.under(ld.dir + "/lint") {
			b.tree.claim(f)
			b.convertLintConfig(f)
		}
		if f := ld.dir + "/lint.toml"; b.tree.has(f) {
			b.tree.claim(f)
			b.convertLintParser(f)
		}
		if f := ld.dir + "/dead-modules.toml"; b.tree.has(f) {
			b.tree.claim(f)
			if ld.member == "" {
				b.p.add("%s sits in a releasable's directory, and its paths name no member; move it into the .rlsbl/ of the member it concerns by hand", f)
				continue
			}
			b.convertDeadModules(f, ld.member)
		}
		if f := ld.dir + "/go.mod"; b.tree.has(f) {
			b.tree.claim(f)
			if ld.dir == oldDir {
				b.rootStub = f
			}
		}
		if ld.dir != oldDir && ld.member != "" && b.d.Layout == declarations.LayoutWorkspace {
			for _, sub := range []string{"changes", "releases"} {
				if files := b.tree.under(ld.dir + "/" + sub); len(files) > 0 {
					for _, f := range files {
						b.tree.claim(f)
					}
					b.p.add("%s/%s/ is per-member release state, which a workspace keeps only in its releasable's directory. Remove it by hand (the Python rlsbl 0.131.0's `rlsbl monorepo cleanup` removes it), then migrate", ld.dir, sub)
				}
			}
		}
	}
}

func (b *builder) convertLintConfig(f string) {
	language := strings.TrimSuffix(path.Base(f), ".toml")
	lang, ok := strictcodeLanguages[language]
	if !ok || !strings.HasSuffix(f, ".toml") {
		b.p.add("%s is not a library lint config (they are python.toml, npm.toml, and go.toml); delete it by hand", f)
		return
	}
	doc, _, err := b.tree.readTOML(f)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	o, _ := newObject(f, doc, b.p)
	for _, k := range o.keys() {
		switch k {
		case "forbidden-imports", "stdout", "entry-point", "files":
		default:
			b.p.add("%s: %s is not a library lint section", f, k)
		}
	}
	if fi, ok := o.table("forbidden-imports"); ok {
		if modules, ok := fi.strings("modules"); ok && strings.Join(modules, "\x00") != strings.Join(shippedForbiddenImports[language], "\x00") {
			b.setLintList(b.lint.forbidden, "forbidden-imports", lang, sourcedList{modules, f})
		}
		if allow, ok := fi.strings("allow"); ok && len(allow) > 0 {
			b.setLintList(b.lint.allow, "forbidden-imports allow", lang, sourcedList{allow, f})
		}
	}
	if so, ok := o.table("stdout"); ok {
		if enabled, ok := so.boolean("enabled"); ok && !enabled {
			b.p.add("%s: stdout.enabled = false has no counterpart in strictcode, whose library-stdout rule is switched by its strictcode:library-stdout option. Hand edit: delete the line (and set that option after the migration if the rule must not run)", f)
		}
		if ignore, ok := so.strings("ignore"); ok && len(ignore) > 0 {
			b.setLintList(b.lint.stdoutAllow, "stdout ignore", lang, sourcedList{ignore, f})
		}
	}
	if ep, ok := o.table("entry-point"); ok {
		if enabled, ok := ep.boolean("enabled"); ok && !enabled {
			b.p.add("%s: entry-point.enabled = false has no counterpart in strictcode. Hand edit: delete the line (and set the strictcode:library-entry-point option after the migration if the rule must not run)", f)
		}
		if ignore, ok := ep.strings("ignore"); ok && len(ignore) > 0 {
			b.p.add("%s: entry-point.ignore has no counterpart in strictcode. Hand edit: empty the list", f)
		}
	}
	if fs, ok := o.table("files"); ok {
		if exclude, ok := fs.strings("exclude"); ok && len(exclude) > 0 {
			b.p.add("%s: files.exclude has no counterpart in strictcode. Hand edit: empty the list", f)
		}
	}
}

func (b *builder) convertLintParser(f string) {
	doc, _, err := b.tree.readTOML(f)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	for k, v := range doc {
		if k != "parser" || (v != "ast" && v != "regex") {
			b.p.add("%s: %s is not a lint.toml setting", f, k)
			continue
		}
		if v == "regex" {
			b.p.add("%s selects the regex parser, which has no counterpart in strictcode. Hand edit: delete the file", f)
		}
	}
}

// convertDeadModules reads a dead-modules.toml: each entry becomes a
// dead-modules suppression with its reason, its path relative to the
// repository root.
func (b *builder) convertDeadModules(f, member string) {
	doc, _, err := b.tree.readTOML(f)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	o, _ := newObject(f, doc, b.p)
	for _, k := range o.keys() {
		if k != "known_non_entry" {
			b.p.add("%s: %s is not a dead-modules.toml key", f, k)
		}
	}
	entries, _ := o.list("known_non_entry")
	for i, item := range entries {
		eo, ok := o.child("known_non_entry", i, item)
		if !ok {
			continue
		}
		for _, k := range eo.keys() {
			if k != "path" && k != "reason" {
				b.p.add("%s: %s.%s is not an entry key", f, eo.where, k)
			}
		}
		raw, _ := eo.str("path")
		reason, _ := eo.str("reason")
		p, ok := declarations.CanonicalPath(raw)
		if !ok || raw == "" || strings.TrimSpace(reason) == "" {
			b.p.add("%s: %s needs a path inside the member and a reason", f, eo.where)
			continue
		}
		b.deadModules = append(b.deadModules, suppression{path: joinRel(member, p), reason: reason, file: f})
	}
}

// convertPrivateModule writes .strictmetadata/go.mod, the stub keeping
// .strictmetadata/ out of the Go module, where the root's .rlsbl/go.mod kept
// .rlsbl/ out of it, or the root holds a Go module.
func (b *builder) convertPrivateModule() {
	rootModule, err := exists(b.root, "go.mod")
	if err != nil {
		b.p.add("%v", err)
		return
	}
	if b.rootStub == "" && !rootModule {
		return
	}
	if ok, err := exists(b.root, declarations.PrivateModuleFile); err != nil || ok {
		return
	}
	stub, err := scaffold.PrivateModuleStub()
	if err != nil {
		b.p.add("%v", err)
		return
	}
	var sources []string
	if b.rootStub != "" {
		sources = append(sources, b.rootStub)
	}
	b.addWrite(write{path: declarations.PrivateModuleFile, sources: sources, change: "the stub that keeps .strictmetadata/ out of the Go module", data: []byte(stub)})
}

// convertTestRunner writes test-runner.toml from the root's test_sandbox.
func (b *builder) convertTestRunner() {
	var runner *object
	for i := range b.sandboxes {
		o := b.sandboxes[i]
		if !b.sandboxAtRoot(o.file) {
			b.p.add("%s: test_sandbox is declared in the config of a member below the root, and the sandboxed test runner serves the whole repository from its root. Hand edit: move the section into the config of the root's releasable (%s/config.json standalone), with its paths relative to the repository root", o.file, oldDir)
			continue
		}
		if runner != nil {
			b.p.add("%s and %s both declare test_sandbox, and the repository has one test runner; keep one by hand", runner.file, o.file)
			continue
		}
		runner = &b.sandboxes[i]
	}
	if runner == nil {
		return
	}
	o := *runner
	var lines []string
	lines = append(lines, "format_version = 1")
	quoteList := func(values []string) string {
		quoted := make([]string, len(values))
		for i, v := range values {
			quoted[i] = tomlString(v)
		}
		return "[" + strings.Join(quoted, ", ") + "]"
	}
	for _, k := range o.keys() {
		switch k {
		case "runner_path", "command", "default_args":
			v, _ := o.str(k)
			lines = append(lines, k+" = "+tomlString(v))
		case "caches", "prewarm", "ci_workflows", "carry_ignored":
			v, _ := o.strings(k)
			lines = append(lines, k+" = "+quoteList(v))
		case "extra_env":
			env, ok := o.table(k)
			if !ok {
				continue
			}
			var pairs []string
			for _, name := range env.keys() {
				v, _ := env.str(name)
				pairs = append(pairs, tomlKey(name)+" = "+tomlString(v))
			}
			lines = append(lines, "extra_env = { "+strings.Join(pairs, ", ")+" }")
		default:
			b.p.add("%s: %s is not a test_sandbox key", o.file, o.key(k))
		}
	}
	sort.Strings(lines[1:])
	data := []byte(strings.Join(lines, "\n") + "\n")
	if _, err := declarations.ParseTestRunner(data); err != nil {
		b.p.add("%s: test_sandbox converts to settings the new reader refuses (%v); repair it by hand", o.file, err)
		return
	}
	b.addWrite(write{path: declarations.TestRunnerFile, sources: []string{o.file}, change: "the sandboxed test runner's settings", data: data})
}

// suppression is one dead-modules suppression.
type suppression struct {
	path   string
	reason string
	file   string
}

func tomlString(s string) string { return tomledit.QuoteString(s) }

func tomlKey(s string) string { return tomledit.QuoteKey(s) }
