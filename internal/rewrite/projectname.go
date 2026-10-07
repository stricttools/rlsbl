package rewrite

import (
	"fmt"
	"path"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/upstream"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// RenameCommitMessage is the rename commit's message, which a run after a
// crash finds the commit by.
func RenameCommitMessage(old, new string) string {
	return fmt.Sprintf("rewrite: rename project %s -> %s", old, new)
}

// recordCommitMessage is the message of the commit recording the pending
// identities.
func recordCommitMessage(old, new string) string {
	return fmt.Sprintf("rewrite: record the %s -> %s pending identities", old, new)
}

// declaredTarget is one target of the project with its directory.
type declaredTarget struct {
	name   string
	target targets.Target
	// dir is absolute; relDir is repository-relative.
	dir    string
	relDir string
}

// manifestStep is one manifest-field target's manifest, as observed.
type manifestStep struct {
	target declaredTarget
	rel    string
	rename targets.ManifestRename
}

// renameCommitStep and recordCommitStep are the data of the two commit
// items.
type (
	renameCommitStep struct{}
	recordCommitStep struct{}
)

// ProjectRename renames a standalone project's published identity: the
// package name in every manifest whose name is a manifest field, and the Go
// module path when the project has a Go target, recording each changed
// identity as a pending identity of the lifecycle-and-license record,
// effective from the version the next release ships.
type ProjectRename struct {
	// Root is the repository root, absolute.
	Root string
	Old  string
	New  string

	releasable declarations.Releasable
	ws         *workspace.Workspace
	declared   []declaredTarget
	effective  semver.Version
	latest     releaserecord.LatestFact
	record     *releaserecord.Record

	manifests      []manifestStep
	renamedAlready []string
	// goCurrent and goNew are the module path go.mod declares and the one
	// the rename moves it to; goRelDir is the Go target's directory. Empty
	// without a Go target.
	goCurrent, goNew, goRelDir string
	module                     *ModuleRename
	moduleFiles                []ModuleFile

	pending         []lifecycle.Identity
	recordedAlready []string
	notes           []string
	sourceDirs      []string
	installPaths    []string
	commandNames    []string

	applied      []string
	renameCommit string
}

func (p *ProjectRename) refuse(format string, args ...any) error {
	return fmt.Errorf(format, args...)
}

// prepare derives the whole rename and refuses, before anything is written,
// everything the caller must change first.
func (p *ProjectRename) prepare(r git.Runner) error {
	d, err := declarations.Load(p.Root)
	if err != nil {
		return err
	}
	if d.IsWorkspace() {
		return p.refuse("this repository is a workspace (%s declares repository_layout = %q), and `rlsbl rewrite project-name` renames a standalone project. In a workspace, rename the releasable with `rlsbl monorepo rename-releasable <old> <new>` instead.", declarations.ReleasablesFile, declarations.LayoutWorkspace)
	}
	if p.ws, err = workspace.New(p.Root, d); err != nil {
		return err
	}
	member := d.RootMember()
	releasable, ok := p.ws.ReleasableOf(member)
	if !ok {
		return p.refuse("the root member is versioned under no releasable, so this repository publishes no identity to rename")
	}
	p.releasable = releasable
	declared, err := targets.MemberTargets(p.Root, member)
	if err != nil {
		return err
	}
	for _, dt := range declared {
		t, err := targets.Get(dt.Name)
		if err != nil {
			return err
		}
		relDir := member.TargetDir(dt)
		p.declared = append(p.declared, declaredTarget{name: dt.Name, target: t, dir: filepath.Join(p.Root, filepath.FromSlash(relDir)), relDir: relDir})
	}
	if err := p.refuseInvalidNames(); err != nil {
		return err
	}
	repo, err := git.Open(r, p.Root)
	if err != nil {
		return err
	}
	if err := p.refuseDirtyTree(repo); err != nil {
		return err
	}
	if err := p.decideEffectiveVersion(repo); err != nil {
		return err
	}
	if err := p.observeNames(); err != nil {
		return err
	}
	if p.goCurrent != "" && p.goCurrent != p.goNew {
		p.module = &ModuleRename{Root: p.Root, From: p.goCurrent, To: p.goNew}
		if p.moduleFiles, _, err = p.module.ObserveModuleFiles(r); err != nil {
			return err
		}
	}
	if err := p.planIdentities(repo); err != nil {
		return err
	}
	return p.planRemainingSteps(member)
}

func (p *ProjectRename) refuseInvalidNames() error {
	if p.Old == p.New {
		return p.refuse("--from and --to are the same name ('%s'); there is nothing to rename. Pass the new name as --to.", p.Old)
	}
	var problems []string
	for _, t := range p.declared {
		for _, problem := range t.target.PackageNameProblems(p.New) {
			problems = append(problems, fmt.Sprintf("  %s: %s", t.name, problem))
		}
	}
	if len(problems) > 0 {
		return p.refuse("--to '%s' is not a valid package name for every target this project publishes to:\n%s\nRun again with a --to that every one of them accepts.", p.New, strings.Join(problems, "\n"))
	}
	return nil
}

func (p *ProjectRename) refuseDirtyTree(repo git.Repo) error {
	changed, err := repo.ChangedPaths(nil, git.UntrackedAll)
	if err != nil {
		return err
	}
	var dirty []string
	for _, c := range changed {
		if !declarations.IsInside(c, declarations.ReleaseStateDir) {
			dirty = append(dirty, "  "+c)
		}
	}
	if len(dirty) > 0 {
		return p.refuse("the working tree has uncommitted changes:\n%s\nCommit them (safegit commit) or remove them, then run again.", strings.Join(dirty, "\n"))
	}
	return nil
}

// decideEffectiveVersion asks the release record what the next release
// ships, as `release run` decides it, so a state the release refuses is
// refused here too. One state is answered differently: version files naming
// an unrecoverable version, which the release refuses until the version's
// tag is restored. That refusal is about the release's history, which a
// rename does not read; the release then ships the declared bump from it,
// and that is the version recorded.
func (p *ProjectRename) decideEffectiveVersion(repo git.Repo) error {
	rf, err := releaserecord.ReadReleaseFile(p.Root, p.releasable.Name)
	if err != nil {
		return p.refuse("the version the new names take effect from cannot be derived: %v", err)
	}
	if len(rf.Include) == 0 {
		return p.refuse("the release file %s includes no target, so the version the new names take effect from cannot be read. Name the targets in its include list, commit it, and run again.", releaserecord.ReleaseFilePath(p.releasable.Name))
	}
	var source *declaredTarget
	var names []string
	for i, t := range p.declared {
		names = append(names, t.name)
		if t.name == rf.Include[0] {
			source = &p.declared[i]
		}
	}
	if source == nil {
		return p.refuse("the release file's include list starts with %q, which is not one of this project's targets (%s). Fix include in %s and run again.", rf.Include[0], strings.Join(names, ", "), releaserecord.ReleaseFilePath(p.releasable.Name))
	}
	current, err := source.target.ReadVersion(source.dir)
	if err != nil {
		return err
	}
	scheme, err := workspace.SchemeOf(p.releasable)
	if err != nil {
		return err
	}
	forkOf, err := upstream.URLOf(p.Root)
	if err != nil {
		return err
	}
	p.record = releaserecord.New(repo, p.releasable.Name, scheme, forkOf)
	head, err := repo.Head()
	if err != nil {
		return err
	}
	if p.latest, err = p.record.Latest(head); err != nil {
		return err
	}
	fate, err := p.record.Fate(current)
	if err != nil {
		return err
	}
	var decision releaserecord.Decision
	if fate == releaserecord.FateUnrecoverable {
		decision, err = p.record.BumpedVersion(current, rf.Bump)
	} else {
		decision, err = p.record.DecideVersion(current, rf.Bump, p.latest)
	}
	if err != nil {
		return err
	}
	p.effective = decision.Version
	return nil
}

// observeNames classifies every target's declared name and refuses any that
// matches neither --from nor --to.
func (p *ProjectRename) observeNames() error {
	var mismatched []string
	declaredNames := map[string]bool{}
	goModule := ""
	for _, t := range p.declared {
		facts := t.target.Facts()
		switch facts.PackageRename {
		case targets.RenameManifestField:
			plan, err := targets.RenamePackage(t.target, t.dir, p.Old, p.New)
			if err != nil {
				return err
			}
			relPath := rel(p.Root, plan.Path)
			switch {
			case plan.Occurrences > 0:
				p.manifests = append(p.manifests, manifestStep{target: t, rel: relPath, rename: plan})
			case plan.CurrentName == p.New:
				p.renamedAlready = append(p.renamedAlready, fmt.Sprintf("%s already declares %q", relPath, p.New))
			default:
				mismatched = append(mismatched, fmt.Sprintf("  %s (%s) declares %q", relPath, facts.PackageNameField, plan.CurrentName))
				declaredNames[plan.CurrentName] = true
			}
		case targets.RenameGoModulePath:
			module, found, err := gomodule.ModulePath(t.dir)
			if err != nil {
				return err
			}
			if !found {
				return p.refuse("the Go target at %s has no go.mod, so it declares no module path. Fix the target and run again.", t.relDir)
			}
			goModule, p.goRelDir = module, t.relDir
		}
	}
	if len(mismatched) > 0 {
		hint := "Run again with --from set to the name these manifests declare, after making them agree."
		if len(declaredNames) == 1 {
			for n := range declaredNames {
				hint = "Run again with --from " + n + "."
			}
		}
		return p.refuse("--from '%s' does not match the package name the manifests declare:\n%s\n%s", p.Old, strings.Join(mismatched, "\n"), hint)
	}
	if goModule != "" {
		switch last := gomodule.LastElement(goModule); last {
		case p.Old:
			p.goCurrent, p.goNew = goModule, goModule[:len(goModule)-len(p.Old)]+p.New
		case p.New:
			p.goCurrent, p.goNew = goModule, goModule
		default:
			return p.refuse("go.mod declares module %s, whose last element '%s' is not --from '%s', so the new module path cannot be derived from it. If '%s' is the project's name, run again with --from %s. Otherwise move the module by hand with `rlsbl rewrite go-module-path --from-module %s --to-module <a path ending in /%s>`, commit that, and run again.", goModule, last, p.Old, last, last, goModule, p.New)
		}
	}
	if len(p.manifests) == 0 && len(p.renamedAlready) == 0 && p.goCurrent == "" {
		return p.refuse("no target of this project has a package name rlsbl renames (npm, PyPI, or a Go module). Rename the project by hand.")
	}
	return nil
}

// moduleDirective is the module path go.mod text declares, or "".
func moduleDirective(text string) string {
	for _, raw := range strings.Split(text, "\n") {
		line := strings.TrimSpace(strings.SplitN(raw, "//", 2)[0])
		if rest, ok := strings.CutPrefix(line, "module "); ok {
			return strings.Trim(strings.TrimSpace(rest), `"`)
		}
	}
	return ""
}

// publishedModulePath is the module path the latest release published, read
// from go.mod at its release commit; "" (with a note) when nothing published
// one. An unrecoverable latest release records no commit to read it at, and
// is refused: nothing can establish which module path it published.
func (p *ProjectRename) publishedModulePath(repo git.Repo) (string, error) {
	if !p.latest.Released {
		p.notes = append(p.notes, "no pending go-module-path identity: this project has never released, so no Go module path was ever published.")
		return "", nil
	}
	if p.latest.Unrecoverable {
		return "", p.refuse("the latest release, %s, is marked unrecoverable in its archive, so rlsbl's record cannot establish the Go module path that release published, and the pending go-module-path identity this rename must record cannot be written.", p.latest.Version)
	}
	entry, err := p.record.Entry(p.latest.Version)
	if err != nil {
		return "", err
	}
	goMod := path.Join(p.goRelDir, gomodule.FileName)
	text, found, err := repo.FileAt(entry.ReleaseCommit, goMod)
	if err != nil {
		return "", err
	}
	if !found {
		p.notes = append(p.notes, fmt.Sprintf("no pending go-module-path identity: the latest release, %s, had no %s, so it published no Go module.", p.latest.Version, goMod))
		return "", nil
	}
	module := moduleDirective(text)
	if module == "" {
		p.notes = append(p.notes, fmt.Sprintf("no pending go-module-path identity: %s at the latest release, %s, declares no module.", goMod, p.latest.Version))
	}
	return module, nil
}

// identityReason is the reason the pending identities carry.
func (p *ProjectRename) identityReason() string {
	return fmt.Sprintf("rlsbl rewrite project-name renamed %s to %s", p.Old, p.New)
}

// planIdentities decides the pending identities to record: package-name for
// a project with a manifest-field target, and go-module-path when the latest
// release published another module path than the new one.
func (p *ProjectRename) planIdentities(repo git.Repo) error {
	rec, err := lifecycle.Load(p.Root)
	if err != nil {
		return err
	}
	scheme, err := workspace.SchemeOf(p.releasable)
	if err != nil {
		return err
	}
	var manifestRegistries []string
	for _, t := range p.declared {
		if t.target.Facts().PackageRename == targets.RenameManifestField {
			manifestRegistries = append(manifestRegistries, t.name)
		}
	}
	type wanted struct {
		facet    lifecycle.Facet
		value    string
		registry string
	}
	var want []wanted
	if len(manifestRegistries) > 0 {
		registry := ""
		if len(manifestRegistries) == 1 {
			registry = manifestRegistries[0]
		}
		want = append(want, wanted{lifecycle.FacetPackageName, p.New, registry})
	}
	if p.goCurrent != "" {
		published, err := p.publishedModulePath(repo)
		if err != nil {
			return err
		}
		switch {
		case published == p.goNew:
			p.notes = append(p.notes, fmt.Sprintf("no pending go-module-path identity: the latest release already published %s.", published))
		case published != "":
			want = append(want, wanted{lifecycle.FacetGoModulePath, p.goNew, targets.Go})
		}
	}
	for _, w := range want {
		id := lifecycle.Identity{
			Subject:          p.releasable.Name,
			Facet:            w.facet,
			Value:            w.value,
			Registry:         w.registry,
			TagPatterns:      []string{scheme.ListGlob()},
			EffectiveVersion: p.effective.String(),
			Reason:           p.identityReason(),
		}
		recorded := false
		for _, existing := range rec.Identities() {
			if existing.Subject != id.Subject || existing.Facet != id.Facet {
				continue
			}
			if existing.Pending() {
				if existing.Value == id.Value && existing.EffectiveVersion == id.EffectiveVersion {
					recorded = true
					continue
				}
				return p.refuse("%s already holds a pending %s identity of %s, %q effective %s, and a subject has at most one pending identity per facet. Delete that [[identities]] entry from %s, commit it, and run again.", lifecycle.RecordFile, existing.Facet, existing.Subject, existing.Value, existing.EffectiveVersion, lifecycle.RecordFile)
			}
			if existing.Open() {
				// The pending identity replaces the open one, and keeps
				// the registry and tag namespace it records.
				id.Registry = existing.Registry
				id.TagPatterns = append([]string(nil), existing.TagPatterns...)
			}
		}
		if recorded {
			p.recordedAlready = append(p.recordedAlready, fmt.Sprintf("%s %s, effective %s", id.Facet, id.Value, id.EffectiveVersion))
			continue
		}
		p.pending = append(p.pending, id)
	}
	return nil
}

// planRemainingSteps lists what still carries the old name: source
// directories, install paths, and command names.
func (p *ProjectRename) planRemainingSteps(member declarations.Member) error {
	for _, pipeline := range member.Pipelines {
		for _, entry := range pipeline.InstallPaths {
			var parts []string
			for _, part := range strings.Split(entry, "/") {
				if part != "" && part != "." {
					parts = append(parts, part)
				}
			}
			if slices.Contains(parts, p.Old) {
				p.installPaths = append(p.installPaths, entry)
				p.sourceDirs = append(p.sourceDirs, strings.Join(parts, "/"))
			}
		}
	}
	for _, t := range p.declared {
		for _, dir := range targets.SourceDirs(t.target, t.dir, p.Old) {
			p.sourceDirs = append(p.sourceDirs, rel(p.Root, dir))
		}
		names, err := targets.CommandNames(t.target, t.dir, p.Old)
		if err != nil {
			return err
		}
		for _, n := range names {
			p.commandNames = append(p.commandNames, n+" still names the old command")
		}
	}
	return nil
}

func identitySummary(id lifecycle.Identity) string {
	return fmt.Sprintf("%s %s, effective %s", id.Facet, id.Value, id.EffectiveVersion)
}

// observe is the whole plan: per file, per identity, and the two commits.
func (p *ProjectRename) observe(o previewapply.Observer) (previewapply.Preview, error) {
	if err := p.prepare(o); err != nil {
		return previewapply.Preview{}, err
	}
	var items []previewapply.Item
	for _, m := range p.manifests {
		items = append(items, previewapply.Item{
			Key:     m.rel,
			State:   "rewrite",
			Summary: fmt.Sprintf("1 occurrence in this %s manifest", m.target.name),
			Facts:   []string{fmt.Sprintf("package name %q -> %q", p.Old, p.New)},
			Actions: []string{"apply would rewrite 1 occurrence here."},
			Data:    m,
		})
	}
	for _, line := range p.renamedAlready {
		key, _, _ := strings.Cut(line, " ")
		items = append(items, previewapply.Item{Key: key, State: "already_renamed", Summary: line})
	}
	for _, f := range p.moduleFiles {
		items = append(items, ModuleItem(f))
	}
	if p.goCurrent != "" && p.goCurrent == p.goNew {
		items = append(items, previewapply.Item{Key: "(go module)", State: "already_renamed", Summary: "go.mod already declares " + p.goNew})
	}
	rewrites := len(p.manifests) + len(p.moduleFiles)
	if rewrites > 0 {
		items = append(items, previewapply.Item{
			Key:     "(rename commit)",
			State:   "commit",
			Summary: fmt.Sprintf("commit the %s as one commit: '%s'", plural(rewrites, "rewritten file"), RenameCommitMessage(p.Old, p.New)),
			Data:    renameCommitStep{},
		})
	}
	for _, id := range p.pending {
		items = append(items, previewapply.Item{Key: lifecycle.RecordFile + " " + string(id.Facet), State: "record", Summary: "pending " + identitySummary(id)})
	}
	for _, line := range p.recordedAlready {
		facet, _, _ := strings.Cut(line, " ")
		items = append(items, previewapply.Item{Key: lifecycle.RecordFile + " " + facet, State: "already_recorded", Summary: line})
	}
	for i, note := range p.notes {
		items = append(items, previewapply.Item{Key: fmt.Sprintf("(note %d)", i+1), State: "note", Summary: note})
	}
	if len(p.pending) > 0 {
		items = append(items, previewapply.Item{
			Key:     "(record commit)",
			State:   "commit",
			Summary: fmt.Sprintf("add %s to %s and commit them", counted(len(p.pending), "pending identity", "pending identities"), lifecycle.RecordFile),
			Data:    recordCommitStep{},
		})
	}
	items = append(items, previewapply.Item{
		Key:     "(total)",
		State:   "summary",
		Summary: fmt.Sprintf("%s to rewrite and %s to record: %s -> %s, effective %s", plural(rewrites, "file"), counted(len(p.pending), "pending identity", "pending identities"), p.Old, p.New, p.effective),
	})
	return previewapply.NewPreview(items...)
}

// apply performs one item.
func (p *ProjectRename) apply(ctx *strictcli.Context, e *strictcli.Effects, it previewapply.Item) error {
	switch step := it.Data.(type) {
	case manifestStep:
		fresh, err := targets.RenamePackage(step.target.target, step.target.dir, p.Old, p.New)
		if err != nil {
			return err
		}
		if err := previewapply.CountMoved(step.rel, step.rename.Occurrences, fresh.Occurrences); err != nil {
			return err
		}
		if err := replaceFile(e, fresh.Path, fresh.Text); err != nil {
			return err
		}
		p.applied = append(p.applied, step.rel)
		ctx.Info(fmt.Sprintf("  %s: rewrote 1 occurrence", step.rel))
	case ModuleFile:
		if err := p.module.ApplyModuleFile(e, step); err != nil {
			return err
		}
		p.applied = append(p.applied, step.Rel)
		ctx.Info(fmt.Sprintf("  %s: rewrote %s", step.Rel, plural(step.Occurrences, "occurrence")))
	case renameCommitStep:
		repo, err := git.Open(e, p.Root)
		if err != nil {
			return err
		}
		// Not autogenerated: the rename changes the published identity, so
		// changelog coverage asks for the breaking entry the closing
		// message prints.
		if _, err := repo.Commit(git.CommitRequest{Message: RenameCommitMessage(p.Old, p.New), Paths: p.applied, RequireChange: true}); err != nil {
			return err
		}
		if p.renameCommit, err = repo.Head(); err != nil {
			return err
		}
	case recordCommitStep:
		rec, err := lifecycle.Load(p.Root)
		if err != nil {
			return err
		}
		for _, id := range p.pending {
			if err := rec.AddPendingIdentity(id); err != nil {
				return err
			}
		}
		if err := rec.Write(recordWriter{e: e}, p.Root); err != nil {
			return err
		}
		repo, err := git.Open(e, p.Root)
		if err != nil {
			return err
		}
		if _, err := repo.Commit(git.CommitRequest{Message: recordCommitMessage(p.Old, p.New), Paths: []string{lifecycle.ManifestFile, lifecycle.RecordFile}, Autogenerated: true, RequireChange: true}); err != nil {
			return err
		}
	}
	return nil
}

// findRenameCommit is the newest commit carrying the rename commit's
// message, for a run completing one a crash interrupted; "" when there is
// none.
func (p *ProjectRename) findRenameCommit(e *strictcli.Effects) (string, error) {
	c, err := e.Run([]interface{}{"git", "log", "-1", "--format=%H", "--fixed-strings", "--grep=" + RenameCommitMessage(p.Old, p.New)},
		strictcli.Cwd(p.Root), strictcli.Check(false), strictcli.Timeout(2*time.Minute))
	if err != nil {
		return "", err
	}
	if c.ExitCode() != 0 {
		return "", fmt.Errorf("git log in %s exited %d: %s", p.Root, c.ExitCode(), strings.TrimSpace(c.Stderr()))
	}
	return strings.TrimSpace(c.Stdout()), nil
}

// needsEntry reports whether changelog coverage asks an entry for sha.
func (p *ProjectRename) needsEntry(e *strictcli.Effects, sha string) (bool, error) {
	repo, err := git.Open(e, p.Root)
	if err != nil {
		return false, err
	}
	needing, err := changelog.CommitsNeedingEntries(repo, []string{sha}, p.ws.ScopeOfReleasable(p.releasable.Name), nil)
	if err != nil {
		return false, err
	}
	return len(needing.Commits) > 0, nil
}

// RemainingSteps is what the caller does next, in order. The changelog step
// is listed unless the rename commit exists and changelog coverage asks no
// entry for it.
func (p *ProjectRename) RemainingSteps(renameCommit string, needsEntry bool) []string {
	var steps []string
	if len(p.sourceDirs) > 0 {
		steps = append(steps,
			"Move the source directories whose paths carry the old name: "+strings.Join(p.sourceDirs, ", ")+".",
			"Update the code that imports them to the new paths.")
	} else {
		steps = append(steps, fmt.Sprintf("No source directory named '%s' was found; move any whose path carries the old name, and update the code that imports it.", p.Old))
	}
	if len(p.installPaths) > 0 {
		steps = append(steps, fmt.Sprintf("Update install_paths in %s: %s.", declarations.ReleasablesFile, strings.Join(p.installPaths, ", ")))
	}
	steps = append(steps, "Run `rlsbl scaffold` to regenerate the files that follow the source layout and the module path.")
	for _, line := range p.commandNames {
		steps = append(steps, "Decide the command name: "+line+".")
	}
	steps = append(steps, fmt.Sprintf("Rename the GitHub repository to match '%s' (rlsbl does not rename repositories).", p.New))
	if renameCommit != "" && !needsEntry {
		return steps
	}
	description := fmt.Sprintf("Renamed from %s to %s: releases from %s are published as %s", p.Old, p.New, p.effective, p.New)
	if p.goNew != "" {
		description += ", and the Go module path is " + p.goNew
	}
	commits := "<the rename commit>"
	if renameCommit != "" {
		commits = renameCommit[:12]
	}
	return append(steps, fmt.Sprintf("Add a breaking changelog entry for the rename: rlsbl changelog add --commits %s --type breaking --description %s", commits, shellQuote(description+".")))
}

// RunProjectName is `rlsbl rewrite project-name`: rename the standalone
// project in the repository rooted at root from old to new, previewed under
// --dry-run. The rename and the record are committed separately, so a crash
// between them is completed by running again: a manifest already declaring
// the new name counts as renamed, and a pending identity already recorded
// is not added again.
func RunProjectName(ctx *strictcli.Context, root, old, new string) error {
	p := &ProjectRename{Root: root, Old: old, New: new}
	if _, err := previewapply.Reconcile(ctx, previewapply.Reconciler{
		Observe: p.observe,
		Apply: func(e *strictcli.Effects, it previewapply.Item) error {
			return p.apply(ctx, e, it)
		},
		ShowKeys: true,
	}); err != nil {
		return err
	}
	renameCommit, needsEntry := "", true
	if !ctx.DryRun() {
		ctx.Info(fmt.Sprintf("Renamed %s -> %s in %s.", old, new, plural(len(p.applied), "file")))
		for _, id := range p.pending {
			ctx.Info("Recorded the pending " + identitySummary(id) + ".")
		}
		for _, line := range p.recordedAlready {
			ctx.Info("Already recorded: " + line + ".")
		}
		for _, note := range p.notes {
			ctx.Info("Note: " + note)
		}
		renameCommit = p.renameCommit
		if renameCommit == "" {
			found, err := p.findRenameCommit(ctx.Effects())
			if err != nil {
				return err
			}
			renameCommit = found
		}
		if renameCommit != "" {
			var err error
			if needsEntry, err = p.needsEntry(ctx.Effects(), renameCommit); err != nil {
				return err
			}
		}
	}
	lines := []string{"", "Remaining steps, in order:"}
	for i, step := range p.RemainingSteps(renameCommit, needsEntry) {
		lines = append(lines, fmt.Sprintf("  %d. %s", i+1, step))
	}
	ctx.Out(strings.Join(lines, "\n"))
	return nil
}
