package migration

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/targets"
)

// builder builds one plan: it reads the old layout, converts each record,
// and collects every refusal.
type builder struct {
	e    *strictcli.Effects
	req  Request
	root string
	repo git.Repo
	tree *oldTree
	p    *problems
	plan *Plan

	values      configValues
	tools       map[string][]toolDeclaration
	certificate *certificateDeclaration
	// sandboxes are the test_sandbox sections of every config.
	sandboxes   []object
	exclusions  map[string][]exclusion
	lint        libraryLint
	deadModules []suppression
	// rootStub reports the root's old private-module stub, .rlsbl/go.mod.
	rootStub bool
	// ruffLint is the repository's rlsbl:ruff-lint entry, which decides
	// whether the members ruff-lint covered get strictcode's lint rule.
	ruffLint *ruffLintEntry

	d declarations.Releasables
	// stateDirs maps each releasable to its old state directory, which
	// holds its changelog, archives, and version.
	stateDirs map[string]string
	// memberDirs maps each member path to its old .rlsbl/ directory, for
	// the members that have one.
	memberDirs map[string]string
	// retired maps each subject whose release history the transition
	// record closed to its old state directory, empty when none is left.
	retired map[string]string
	// declaredHookSlots are the hook points a config declared, per owner,
	// which an old hook script beside it cannot also fill.
	declaredHookSlots map[string]map[string]bool

	history *history
}

// Build reads the repository at req.Root and decides everything the
// migration does, or refuses with every problem found. It writes nothing.
func Build(e *strictcli.Effects, req Request) (*Plan, error) {
	root := req.Root
	if !filepath.IsAbs(root) {
		return nil, fmt.Errorf("the repository root %q is not absolute", root)
	}
	hasOld, err := exists(root, oldDir)
	if err != nil {
		return nil, err
	}
	hasWorkspace, err := exists(root, oldWorkspaceDir)
	if err != nil {
		return nil, err
	}
	hasNew, err := exists(root, declarations.ReleasablesFile)
	if err != nil {
		return nil, err
	}
	switch {
	case !hasOld && !hasWorkspace && hasNew:
		return &Plan{Converted: true}, nil
	case !hasOld && !hasWorkspace:
		return nil, &RefusedError{Problems: []string{fmt.Sprintf("%s holds no rlsbl records: neither %s/, %s/, nor %s exists, so there is nothing to migrate", root, oldDir, oldWorkspaceDir, declarations.ReleasablesFile)}}
	case hasNew:
		return nil, &RefusedError{Problems: []string{fmt.Sprintf("%s already exists beside the old layout (%s): the repository is half migrated, and the migration converts only an old layout with no new one. Remove the files under %s/ that the migration writes (they are listed by a dry run once they are gone), then run it again", declarations.ReleasablesFile, strings.Join(oldLayoutNames(hasOld, hasWorkspace), ", "), declarations.MetadataDir)}}
	}
	repo, err := git.Open(e, root)
	if err != nil {
		return nil, err
	}
	b := &builder{
		e: e, req: req, root: root, repo: repo, p: &problems{},
		plan:              &Plan{},
		values:            configValues{},
		tools:             map[string][]toolDeclaration{},
		exclusions:        map[string][]exclusion{},
		stateDirs:         map[string]string{},
		memberDirs:        map[string]string{},
		retired:           map[string]string{},
		declaredHookSlots: map[string]map[string]bool{},
	}
	if hasWorkspace {
		b.d.Layout = declarations.LayoutWorkspace
		if hasOld {
			b.p.add("%s/ sits beside %s/ at the repository root; a workspace keeps no root %s/ (its root member's config is its releasable's). Move what it holds into the releasable's directory or delete it by hand", oldDir, oldWorkspaceDir, oldDir)
		}
		if ok, err := exists(root, oldWorkspaceFile); err != nil {
			return nil, err
		} else if !ok {
			return nil, &RefusedError{Problems: []string{fmt.Sprintf("%s/ exists without %s, a layout the migration does not recognize; restore workspace.toml or remove the directory by hand", oldWorkspaceDir, oldWorkspaceFile)}}
		}
	} else {
		b.d.Layout = declarations.LayoutStandalone
	}
	b.plan.Layout = b.d.Layout
	if err := b.build(); err != nil {
		return nil, err
	}
	return b.plan, nil
}

func oldLayoutNames(hasOld, hasWorkspace bool) []string {
	var out []string
	if hasOld {
		out = append(out, oldDir+"/")
	}
	if hasWorkspace {
		out = append(out, oldWorkspaceDir+"/")
	}
	return out
}

func (b *builder) note(format string, args ...any) {
	b.plan.notes = append(b.plan.notes, fmt.Sprintf(format, args...))
}

// addWrite plans one file. Two conversions writing one path are refused.
func (b *builder) addWrite(w write) {
	for _, existing := range b.plan.writes {
		if existing.path == w.path {
			b.p.add("two old records convert to %s (%s and %s); remove one by hand", w.path, strings.Join(existing.sources, ", "), strings.Join(w.sources, ", "))
			return
		}
	}
	if w.mode == 0 {
		w.mode = 0o644
	}
	b.plan.writes = append(b.plan.writes, w)
}

func (b *builder) build() error {
	var dirs []string
	if b.d.Layout == declarations.LayoutWorkspace {
		dirs = append(dirs, oldWorkspaceDir)
		if ok, err := exists(b.root, oldDir); err != nil {
			return err
		} else if ok {
			dirs = append(dirs, oldDir)
		}
		members, err := b.workspaceMemberDirs()
		if err != nil {
			return err
		}
		dirs = append(dirs, members...)
	} else {
		dirs = append(dirs, oldDir)
	}
	tree, err := newOldTree(b.root, dirs)
	if err != nil {
		return err
	}
	b.tree = tree
	if err := b.refuseStrayOldDirectories(dirs); err != nil {
		return err
	}
	b.refuseRunsInProgress()
	if b.d.Layout == declarations.LayoutWorkspace {
		b.readWorkspace()
	} else {
		b.readStandalone()
	}
	b.applyRepositoryValues()
	b.history = b.readHistory()
	for _, r := range b.d.Releasables {
		b.convertState(r.Name, b.stateDirs[r.Name], false)
	}
	b.convertRetiredHistories()
	b.convertBatchReleases()
	b.convertHookScripts()
	b.convertMemberLeftovers()
	b.convertScaffoldState()
	b.convertPrivateModule()
	b.convertTestRunner()
	b.convertOptions()
	b.convertStrictcode()
	d := b.renderDeclarations()
	if d != nil {
		b.plan.d = d
		b.convertTransitions(d)
		b.convertLifecycle(d)
		b.planChangelogs(d)
	}
	b.claimDiscarded()
	// A file the conversions did not reach because an earlier problem
	// stopped them is not reported: the next run, once those are fixed,
	// judges it.
	if len(b.p.list) == 0 {
		for _, f := range b.tree.unclaimed() {
			b.p.add("%s is a file of the old layout the migration does not recognize, so it cannot say where its content belongs; move it out of the old layout or delete it by hand", f)
		}
	}
	b.planRemovals(dirs)
	b.addManifests()
	if err := b.p.err(); err != nil {
		return err
	}
	return b.checkUncommitted()
}

// workspaceMemberDirs are the .rlsbl/ directories of the members
// workspace.toml declares below the root, read before the workspace is
// converted so the tree can list them.
func (b *builder) workspaceMemberDirs() ([]string, error) {
	doc, _, err := (&oldTree{root: b.root}).readTOML(oldWorkspaceFile)
	if err != nil {
		return nil, &RefusedError{Problems: []string{err.Error()}}
	}
	projects, _ := asList(doc["projects"])
	var out []string
	for _, item := range projects {
		m, ok := item.(map[string]any)
		if !ok {
			continue
		}
		p, ok := m["path"].(string)
		if !ok || p == declarations.RootPath || declarations.PathProblem(p) != "" {
			continue
		}
		dir := p + "/" + oldDir
		if ok, err := exists(b.root, dir); err != nil {
			return nil, err
		} else if ok {
			b.memberDirs[p] = dir
			out = append(out, dir)
		}
	}
	sort.Strings(out)
	return out, nil
}

// refuseStrayOldDirectories refuses a tracked file of an old-layout
// directory the migration does not convert: a .rlsbl/ below no declared
// member.
func (b *builder) refuseStrayOldDirectories(dirs []string) error {
	tracked, err := b.repo.TrackedFiles()
	if err != nil {
		return err
	}
	known := map[string]bool{}
	for _, d := range dirs {
		known[d] = true
	}
	stray := map[string]bool{}
	for _, f := range tracked {
		parts := strings.Split(f, "/")
		for i, part := range parts[:len(parts)-1] {
			if part != oldDir && part != oldWorkspaceDir {
				continue
			}
			dir := strings.Join(parts[:i+1], "/")
			if !known[dir] {
				stray[dir] = true
			}
			break
		}
	}
	for _, dir := range sortedKeys(stray) {
		b.p.add("%s/ is an old-layout directory of no declared member, so the migration cannot say whose records it holds; declare its directory as a member in %s, or remove it by hand", dir, oldWorkspaceFile)
	}
	return nil
}

// refuseRunsInProgress refuses a release, a scrub, or a reconcile that is
// still in progress.
func (b *builder) refuseRunsInProgress() {
	for f := range b.tree.files {
		if strings.Contains(f, "/bases/") {
			continue
		}
		switch path.Base(f) {
		case "in-progress.json":
			b.p.add("%s records a release in progress; finish it with `rlsbl release resume` or abandon it with `rlsbl release abandon` (the Python rlsbl 0.131.0), then migrate", f)
		case "scrub-result.json":
			b.p.add("%s records a history rewrite in progress; finish it by running `rlsbl release scrub` again (the Python rlsbl 0.131.0), then migrate", f)
		case "reconcile-plan.toml":
			b.p.add("%s is a reconcile plan not yet applied; apply it with `rlsbl release reconcile --apply` or delete it through saferm (`rlsbl release reconcile --plan` writes it again), then migrate", f)
		case "lock":
			b.p.add("%s is the release lock: a release is running or one died holding it. Finish the release, or delete the lock through saferm once no rlsbl process runs, then migrate", f)
		}
	}
}

// readStandalone converts a standalone project's config and releasable.toml.
func (b *builder) readStandalone() {
	cfgRel := oldDir + "/" + oldConfigName
	cfg, ok := b.readConfig(cfgRel)
	if !ok {
		if !b.tree.has(cfgRel) {
			b.p.add("%s is missing: a standalone project's old layout declares its targets and publish mode there, and the migration cannot convert a project that declares nothing. Write it by hand, then migrate", cfgRel)
		}
		return
	}
	root := declarations.Member{Path: declarations.RootPath, Name: declarations.RootName}
	b.memberConfig(cfg, &root)
	name, tagFormat := b.standaloneIdentity(cfg)
	if name == "" {
		return
	}
	root.Releasable = name
	r := declarations.Releasable{Name: name, TagFormat: tagFormat}
	b.releasableKeys(cfg, &r)
	if r.PublishCICheckPattern != "" {
		b.note("%s: publish_gate_check_regex is dropped: a standalone project's CI is its own, so no pattern names it", cfgRel)
		r.PublishCICheckPattern = ""
	}
	if hooks, ok := cfg.table("hooks"); ok {
		r.Hooks = b.convertHooks(hooks)
		b.declareHookSlots(name, hooks)
	}
	b.d.Releasables = []declarations.Releasable{r}
	b.d.Members = []declarations.Member{root}
	b.stateDirs[name] = oldDir
	b.memberDirs[declarations.RootPath] = oldDir
	b.collectExclusions(name, cfg)
}

// standaloneIdentity is the standalone releasable's name and tag format:
// .rlsbl/releasable.toml's when it states them, otherwise the name the
// Python derived (the first configured target's package name, else the
// directory's name) and the standalone tag format.
func (b *builder) standaloneIdentity(cfg object) (name, tagFormat string) {
	rel := oldDir + "/releasable.toml"
	tagFormat = "v{version}"
	doc, found, err := b.tree.readTOML(rel)
	if found {
		b.tree.claim(rel)
	}
	if err != nil {
		b.p.add("%v", err)
		return "", ""
	}
	if found {
		o, _ := newObject(rel, doc, b.p)
		for _, k := range o.keys() {
			if k != "name" && k != "tag_format" {
				b.p.add("%s: %s is not a releasable.toml key; it carries name and tag_format", rel, k)
			}
		}
		if f, ok := o.str("tag_format"); ok {
			tagFormat = f
		}
		if n, ok := o.str("name"); ok && n != "" {
			name = n
		} else {
			b.p.add("%s carries no name; write the releasable's name into it by hand", rel)
			return "", ""
		}
	} else {
		name = b.derivedStandaloneName(cfg)
		if name == "" {
			return "", ""
		}
		b.note("the standalone releasable is named %q, as the Python derived it, and the name is recorded in %s", name, declarations.ReleasablesFile)
	}
	if problem := declarations.NameProblem(name); problem != "" {
		b.p.add("the standalone releasable's name %q cannot name the releasable's directories (%s). Hand edit: write %s holding name = \"<a name without a path separator>\", then migrate", name, problem, rel)
		return "", ""
	}
	return name, tagFormat
}

// derivedStandaloneName derives the name as the Python's
// create_standalone_releasable did: the package name of the first target the
// config lists, read from that target's manifest, else the name of the
// repository's directory.
func (b *builder) derivedStandaloneName(cfg object) string {
	if entries, ok := cfg.list("targets"); ok && len(entries) > 0 {
		var first declarations.Target
		switch v := entries[0].(type) {
		case string:
			first.Name = v
		case map[string]any:
			first.Name, _ = v["name"].(string)
			if p, ok := v["path"].(string); ok {
				first.Path, _ = declarations.CanonicalPath(p)
			}
		}
		if t, err := targets.Get(first.Name); err == nil {
			dir := filepath.Join(b.root, filepath.FromSlash(joinRel(first.Path)))
			name, found, err := t.ReadName(dir)
			if err != nil {
				b.p.add("the standalone releasable's name is the package name in the %s manifest under %s, which cannot be read: %v. Repair the manifest, or write %s/releasable.toml holding the name, then migrate", first.Name, joinRel(first.Path), err, oldDir)
				return ""
			}
			if found && name != "" {
				return name
			}
		}
	}
	real, err := filepath.EvalSymlinks(b.root)
	if err != nil {
		b.p.add("resolving %s: %v", b.root, err)
		return ""
	}
	if base := filepath.Base(real); base != "" && base != string(filepath.Separator) {
		return base
	}
	return "project"
}

// readWorkspace converts workspace.toml, the releasables' configs, and the
// members' configs.
func (b *builder) readWorkspace() {
	doc, _, err := b.tree.readTOML(oldWorkspaceFile)
	b.tree.claim(oldWorkspaceFile)
	if err != nil {
		b.p.add("%v", err)
		return
	}
	ws, _ := newObject(oldWorkspaceFile, doc, b.p)
	for _, k := range ws.keys() {
		switch k {
		case "projects", "releasables":
		case "layers":
			b.note("%s: the [layers] section is dropped with member layering", oldWorkspaceFile)
		default:
			b.p.add("%s: %s is not a workspace.toml key; it carries [[projects]] and [[releasables]]", oldWorkspaceFile, k)
		}
	}
	rawReleasables, ok := ws.list("releasables")
	if !ok {
		b.p.add("%s has no [[releasables]]: it is an implicit-mode workspace, which no rlsbl reads any more. Declare its releasables and each member's releasable by hand (rlsbl 0.117.2 is the last version that reads it), then migrate", oldWorkspaceFile)
		return
	}
	projects, _ := ws.list("projects")
	b.readMembers(ws, projects)
	root, hasRoot := b.d.MemberAt(declarations.RootPath)
	for i, item := range rawReleasables {
		ro, ok := ws.child("releasables", i, item)
		if !ok {
			continue
		}
		var r declarations.Releasable
		for _, k := range ro.keys() {
			switch k {
			case "name", "tag_format":
			case "subtree_remote":
				if v, _ := ro.str(k); v != "" {
					b.p.add("%s: %s.subtree_remote = %q declares a subtree mirror, which rlsbl dropped. Hand edit: delete the subtree_remote line", oldWorkspaceFile, ro.where, v)
				}
			default:
				b.p.add("%s: %s.%s is not a releasable key", oldWorkspaceFile, ro.where, k)
			}
		}
		r.Name, _ = ro.str("name")
		if r.Name == "" {
			b.p.add("%s: %s has no name", oldWorkspaceFile, ro.where)
			continue
		}
		format, declared := ro.str("tag_format")
		switch {
		case declared:
			r.TagFormat = format
		case hasRoot && root.Releasable == r.Name:
			b.p.add("%s: the releasable %q owns the root member but declares no tag_format, and its tags may be either v{version} or {name}@v{version}. Hand edit: add tag_format to its [[releasables]] entry, saying which its history uses", oldWorkspaceFile, r.Name)
			continue
		default:
			r.TagFormat = "{name}@v{version}"
		}
		b.readReleasableConfig(&r)
		b.d.Releasables = append(b.d.Releasables, r)
	}
	b.readMemberConfigs()
	b.checkReleasableDirectories()
}

// readMembers converts the [[projects]] tables into members.
func (b *builder) readMembers(ws object, projects []any) {
	for i, item := range projects {
		po, ok := ws.child("projects", i, item)
		if !ok {
			continue
		}
		var m declarations.Member
		for _, k := range po.keys() {
			switch k {
			case "path", "name", "library", "dev_only", "releasable", "depends_on", "import_name", "registry_name", "description", "test_only", "lint_allow":
			case "watch":
				b.p.add("%s: %s.watch is a retired key: a member owns what lies under its path, and the root member everything else. Hand edit: delete the watch line, and declare a directory it named as a member of its own if that member must own it", oldWorkspaceFile, po.where)
			case "subtree_remote":
				b.p.add("%s: %s.subtree_remote is a retired key, and the subtree mirror is dropped. Hand edit: delete the subtree_remote line", oldWorkspaceFile, po.where)
			case "dev_node":
				b.p.add("%s: %s.dev_node is a retired key. Hand edit: replace it with dev_only = true and releasable = false (or releasable = \"<name>\" for a member that is not dev-only)", oldWorkspaceFile, po.where)
			default:
				b.p.add("%s: %s.%s is not a member key", oldWorkspaceFile, po.where, k)
			}
		}
		raw, _ := po.str("path")
		if problem := declarations.PathProblem(raw); problem != "" {
			b.p.add("%s: %s: %s", oldWorkspaceFile, po.where, problem)
			continue
		}
		m.Path = raw
		m.Name, _ = po.str("name")
		if m.Path == declarations.RootPath {
			if m.Name == "" {
				m.Name = declarations.RootName
			}
			if m.Name != declarations.RootName {
				b.p.add("%s: the root member is named %q; the root member's one name is %q. Hand edit: set name = %q", oldWorkspaceFile, m.Name, declarations.RootName, declarations.RootName)
			}
		} else if m.Name == "" {
			b.p.add("%s: %s has no name", oldWorkspaceFile, po.where)
			continue
		}
		switch v := po.m["releasable"].(type) {
		case nil:
		case string:
			m.Releasable = v
		case bool:
			if v {
				b.p.add("%s: %s.releasable = true names no releasable; write the releasable's name, or false", oldWorkspaceFile, po.where)
			}
		default:
			po.wrong("releasable", "a releasable name or false")
		}
		m.Library, _ = po.boolean("library")
		m.DevOnly, _ = po.boolean("dev_only")
		m.TestOnly, _ = po.boolean("test_only")
		if deps, ok := po.strings("depends_on"); ok && len(deps) > 0 {
			m.DependsOn = deps
		}
		m.ImportName, _ = po.str("import_name")
		m.RegistryName, _ = po.str("registry_name")
		m.Description, _ = po.str("description")
		if allow, ok := po.strings("lint_allow"); ok && len(allow) > 0 {
			m.LintAllow = allow
		}
		b.d.Members = append(b.d.Members, m)
	}
	if _, ok := b.d.MemberAt(declarations.RootPath); !ok {
		b.p.add("%s declares no root member. Hand edit: add a [[projects]] table with path = \".\", name = \"root\", and either releasable = \"<a releasable>\" or dev_only = true with releasable = false", oldWorkspaceFile)
	}
	sortedMembers(b.d.Members)
}

// readReleasableConfig reads a releasable's own config: its releasable
// keys and its hooks. The member keys it carries reach each member through
// the merge.
func (b *builder) readReleasableConfig(r *declarations.Releasable) {
	dir := oldWorkspaceDir + "/releasables/" + r.Name
	b.stateDirs[r.Name] = dir
	cfg, ok := b.readConfig(dir + "/" + oldConfigName)
	if !ok {
		return
	}
	if hooks, ok := cfg.table("hooks"); ok {
		r.Hooks = b.convertHooks(hooks)
		b.declareHookSlots(r.Name, hooks)
	}
	b.collectExclusions(r.Name, cfg)
}

// readMemberConfigs converts each member's effective config: its own
// .rlsbl/config.json merged over its releasable's, as the Python merged
// them, a releasable's targets list overriding a member's.
func (b *builder) readMemberConfigs() {
	releasableConfigs := map[string]object{}
	for _, r := range b.d.Releasables {
		rel := b.stateDirs[r.Name] + "/" + oldConfigName
		value, found, err := b.tree.readJSON(rel)
		if found && err == nil {
			if o, ok := newObject(rel, value, &problems{}); ok {
				releasableConfigs[r.Name] = o
			}
		}
	}
	modes := map[string]configValues{}
	patterns := map[string]configValues{}
	for i := range b.d.Members {
		m := &b.d.Members[i]
		relCfg, versioned := releasableConfigs[m.Releasable]
		var own object
		hasOwn := false
		if !m.IsRoot() {
			own, hasOwn = b.readConfig(memberConfigPath(m.Path))
			if b.memberDirs[m.Path] != "" && !hasOwn && !b.tree.has(memberConfigPath(m.Path)) {
				b.note("%s/ holds no config.json; the member's targets are detected from its manifests", b.memberDirs[m.Path])
			}
			if hasOwn {
				if hooks, ok := own.table("hooks"); ok {
					m.Hooks = b.convertHooks(hooks)
					b.declareHookSlots(m.Name, hooks)
				}
				if own.has("batch_limits") {
					b.p.add("%s: batch_limits belongs in the releasable's config, never a member's; move its exclusions into %s by hand", own.file, b.stateDirs[m.Releasable]+"/"+oldConfigName)
				}
			}
		}
		eff := object{p: b.p}
		switch {
		case versioned && hasOwn:
			merged := mergeConfig(relCfg.m, own.m)
			if targets, ok := relCfg.m["targets"]; ok {
				merged["targets"] = targets
			}
			eff = object{file: own.file + " over " + relCfg.file, m: merged, p: b.p}
		case versioned:
			eff = object{file: relCfg.file, m: relCfg.m, p: b.p}
		case hasOwn:
			eff = own
		default:
			continue
		}
		b.memberConfig(eff, m)
		if m.Versioned() {
			if mode, ok := eff.str("publish_mode"); ok {
				if modes[m.Releasable] == nil {
					modes[m.Releasable] = configValues{}
				}
				modes[m.Releasable].add("publish_mode", mode, eff.file)
			}
			if pattern, ok := eff.str("publish_gate_check_regex"); ok {
				if patterns[m.Releasable] == nil {
					patterns[m.Releasable] = configValues{}
				}
				patterns[m.Releasable].add("publish_gate_check_regex", pattern, eff.file)
			}
		}
	}
	root, _ := b.d.MemberAt(declarations.RootPath)
	for i := range b.d.Releasables {
		r := &b.d.Releasables[i]
		if cfg, ok := releasableConfigs[r.Name]; ok {
			if mode, ok := cfg.str("publish_mode"); ok {
				if modes[r.Name] == nil {
					modes[r.Name] = configValues{}
				}
				modes[r.Name].add("publish_mode", mode, cfg.file)
			}
		}
		mode, ok := modes[r.Name].agreed("publish_mode", b.p)
		switch {
		case !ok && len(modes[r.Name]["publish_mode"]) == 0:
			b.p.add("the releasable %q declares no publish_mode in its config or its members'; add \"publish_mode\": \"ci\" or \"none\" to %s by hand", r.Name, b.stateDirs[r.Name]+"/"+oldConfigName)
		case ok && mode != string(declarations.PublishCI) && mode != string(declarations.PublishNone):
			b.p.add("the releasable %q has publish_mode %q; it is \"ci\" or \"none\"", r.Name, mode)
		case ok:
			r.PublishMode = declarations.PublishMode(mode)
		}
		pattern, hasPattern := patterns[r.Name].agreed("publish_gate_check_regex", b.p)
		ownsRoot := root.Releasable == r.Name
		switch {
		case ownsRoot && r.PublishMode == declarations.PublishCI && !hasPattern:
			b.p.add("the releasable %q owns the workspace's root member and publishes from CI, so the publish job must name the root's CI checks. Hand edit: add \"publish_gate_check_regex\" (a pattern matching the check runs of the root's own CI) to %s", r.Name, b.stateDirs[r.Name]+"/"+oldConfigName)
		case ownsRoot && r.PublishMode == declarations.PublishCI:
			r.PublishCICheckPattern = pattern
		case hasPattern:
			b.note("publish_gate_check_regex of the releasable %q is dropped: only a releasable owning the root member and publishing from CI names one", r.Name)
		}
	}
}

// checkReleasableDirectories refuses a releasable directory no releasable
// is declared for and whose history the transition record does not close.
func (b *builder) checkReleasableDirectories() {
	dir := oldWorkspaceDir + "/releasables"
	for _, name := range b.tree.children(dir) {
		if _, ok := b.d.Releasable(name); ok {
			continue
		}
		b.retired[name] = dir + "/" + name
	}
}

// applyRepositoryValues sets the repository-wide declarations from the
// values every config agrees on.
func (b *builder) applyRepositoryValues() {
	b.d.ReleaseBranches = []string{"main", "master"}
	if v, ok := b.values.agreed("release_branches", b.p); ok {
		b.d.ReleaseBranches = strings.Split(v, ",")
	}
	b.d.EnvironmentFile, _ = b.values.agreed("env_file", b.p)
	b.d.GitHubRepository, _ = b.values.agreed("github_repo", b.p)
	timeouts := []struct {
		key string
		dst *int64
	}{
		{"push_timeout", &b.d.Timeouts.PushSeconds},
		{"ci_timeout", &b.d.Timeouts.CISeconds},
		{"check_timeout", &b.d.Timeouts.CheckSeconds},
		{"hook_timeout", &b.d.Timeouts.HookSeconds},
	}
	for _, t := range timeouts {
		if v, ok := b.values.agreed(t.key, b.p); ok {
			fmt.Sscan(v, t.dst)
		}
	}
}

// sandboxAtRoot reports whether a test_sandbox section in file serves the
// repository root: a standalone project's config, or the config of the
// releasable owning a workspace's root member.
func (b *builder) sandboxAtRoot(file string) bool {
	if file == oldDir+"/"+oldConfigName {
		return true
	}
	root, ok := b.d.MemberAt(declarations.RootPath)
	return ok && root.Releasable != "" && file == b.stateDirs[root.Releasable]+"/"+oldConfigName
}

// declareHookSlots records which hook points a config declares for an
// owner.
func (b *builder) declareHookSlots(owner string, hooks object) {
	if b.declaredHookSlots[owner] == nil {
		b.declaredHookSlots[owner] = map[string]bool{}
	}
	for _, k := range hooks.keys() {
		b.declaredHookSlots[owner][k] = true
	}
}

// renderDeclarations renders releasables.toml, validates it with the
// declarations' own reader, and plans it.
func (b *builder) renderDeclarations() *declarations.Releasables {
	if len(b.p.list) > 0 {
		return nil
	}
	data := declarations.Render(&b.d)
	d, err := declarations.Parse(data)
	if err != nil {
		b.p.add("the converted declarations are refused by the new reader, so the old records need a hand edit first: %v", err)
		return nil
	}
	var sources []string
	if d.IsWorkspace() {
		sources = append(sources, oldWorkspaceFile)
	}
	for _, r := range d.Releasables {
		if b.tree.has(b.stateDirs[r.Name] + "/" + oldConfigName) {
			sources = append(sources, b.stateDirs[r.Name]+"/"+oldConfigName)
		}
	}
	for _, m := range d.Members {
		if !m.IsRoot() && b.tree.has(memberConfigPath(m.Path)) {
			sources = append(sources, memberConfigPath(m.Path))
		}
	}
	b.addWrite(write{path: declarations.ReleasablesFile, sources: sources, change: "the release declarations", data: data})
	return d
}

// claimDiscarded claims the files the migration deletes without converting
// them.
func (b *builder) claimDiscarded() {
	for f := range b.tree.files {
		switch {
		case isLocalOnly(f):
			b.tree.claim(f)
		case f == oldWorkspaceDir+"/snapshot.json":
			b.tree.claim(f)
			b.note("%s is deleted: the monorepo snapshot is dropped", f)
		case f == oldWorkspaceDir+"/publish-cache.json":
			b.tree.claim(f)
		case path.Base(f) == "hashes.json" && path.Base(path.Dir(f)) == oldDir:
			b.tree.claim(f)
		}
	}
}

// planRemovals removes every old-layout directory whole.
func (b *builder) planRemovals(dirs []string) {
	for _, dir := range dirs {
		b.plan.removals = append(b.plan.removals, removal{dir: dir, files: b.tree.under(dir)})
	}
}

// addManifests plans the ownership manifest of every directory under
// .strictmetadata/ the plan writes into, where it is missing, and refuses one
// naming another owner.
func (b *builder) addManifests() {
	dirs := map[string]bool{}
	for _, w := range b.plan.writes {
		if top := topDirectory(w.path); top != "" && top != declarations.ReleaseStateDir {
			dirs[top] = true
		}
	}
	for _, g := range b.plan.generated {
		if top := topDirectory(g); top != "" {
			dirs[top] = true
		}
	}
	for _, dir := range sortedKeys(dirs) {
		rel := dir + "/manifest.toml"
		planned := false
		for _, w := range b.plan.writes {
			if w.path == rel {
				planned = true
			}
		}
		if planned {
			continue
		}
		data, err := os.ReadFile(filepath.Join(b.root, filepath.FromSlash(rel)))
		if errors.Is(err, fs.ErrNotExist) {
			b.plan.writes = append(b.plan.writes, write{path: rel, change: "the directory's ownership manifest", data: []byte("owner = \"" + ownerOf(dir) + "\"\n"), mode: 0o644})
			continue
		}
		if err != nil {
			b.p.add("reading %s: %v", rel, err)
			continue
		}
		if !strings.Contains(string(data), "\""+ownerOf(dir)+"\"") {
			b.p.add("%s names another owner than %s; the migration writes only into directories %s owns", rel, ownerOf(dir), ownerOf(dir))
		}
	}
}

// ownerOf is the tool owning a directory under .strictmetadata/.
func ownerOf(dir string) string {
	switch dir {
	case declarations.MetadataDir + "/options", declarations.MetadataDir + "/lifecycle-and-license":
		return "strictspec"
	}
	return declarations.Owner
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
