package release

import (
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/strictspec/go/lifecycle/index"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/dependencies"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/scaffold"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/tagging"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// A release's plan is derived before anything is written and issued after:
// the builder reads (the manifests, the scaffold state, the lockfiles the
// validation found owed) and returns entries carrying plain operands; the
// executor issues each entry's effect and derives nothing. A preview
// therefore records the whole plan, and a resumed release builds the same
// plan from the same files.

// PlanEntry is one piece of work a step's plan issues. Which operands it
// carries depends on its type; the others are empty.
type PlanEntry struct {
	Type EntryType
	// Summary is the entry as the preview's plan table shows it.
	Summary string
	// Member and Target name the member and target a version write, a
	// keyword, or a build is for.
	Member string
	Target string
	// Dir is the repository-relative directory a version write, a keyword,
	// or a build works in.
	Dir string
	// Path is the repository-relative file a whole-file write replaces.
	Path    string
	Version semver.Version
	// Content is a whole-file write's finished bytes.
	Content []byte
	// Mode is the mode of the file Content replaces.
	Mode os.FileMode
	// Sync is a lockfile re-lock.
	Sync *LockfileSync
	// State is the scaffold state a write-scaffold-state entry writes.
	State *scaffold.State
	// Dirs are the repository-relative directories whose dist directories
	// the clean and the scan cover.
	Dirs []string
	// Expected are the repository-relative paths the guard allows to have
	// changed, besides what the writers report.
	Expected []string
}

// BumpPlan is the version-bumped step's plan.
type BumpPlan struct {
	Entries []PlanEntry
	// FilesToCommit are the repository-relative files the release commit
	// carries, as far as the plan can know them; the executor's report of
	// what the version writers touched widens it.
	FilesToCommit []string
	// AlreadyBumped is set when the version source already names the new
	// version (a release resumed after its version writes), so no version
	// write is planned.
	AlreadyBumped bool
}

// BumpPlanInputs are what the version-bumped plan is derived from.
type BumpPlanInputs struct {
	// Workspace is the declarations of the repository the release writes,
	// rooted at the release checkout.
	Workspace  *workspace.Workspace
	Releasable string
	// Representative is the member the release was started from.
	Representative string
	Current        semver.Version
	Next           semver.Version
	// Primary is the target the release file includes first, whose
	// manifest holds a standalone project's version.
	Primary string
	// Syncs are the lockfile re-locks the validation found owed.
	Syncs []LockfileSync
	// EcosystemTagging is the rlsbl:ecosystem-tagging option's value.
	EcosystemTagging bool
	// RlsblVersion is the running rlsbl's version, recorded in the scaffold
	// state.
	RlsblVersion string
	// Generated are the repository-relative files the pre-release pipeline
	// and the hooks wrote, which the release commit carries.
	Generated []string
}

// BuildBumpPlan derives the version-bumped step's plan. It reads files and
// writes nothing.
func BuildBumpPlan(in BumpPlanInputs) (BumpPlan, error) {
	w := in.Workspace
	root := w.Root
	r, ok := w.Declarations.Releasable(in.Releasable)
	if !ok {
		return BumpPlan{}, fmt.Errorf("no releasable %q is declared in %s", in.Releasable, declarations.ReleasablesFile)
	}
	var plan BumpPlan
	files := newPathSet()
	add := func(e PlanEntry) error {
		if step, err := stepOfEntry(e.Type); err != nil || step != StepVersionBumped {
			return fmt.Errorf("the plan entry %q does not belong to the %s step", e.Type, StepVersionBumped)
		}
		plan.Entries = append(plan.Entries, e)
		return nil
	}
	versioned, err := versionedMembers(w, in.Releasable)
	if err != nil {
		return BumpPlan{}, err
	}
	already, err := versionAlreadyWritten(w, in.Releasable, in.Primary, in.Next)
	if err != nil {
		return BumpPlan{}, err
	}
	plan.AlreadyBumped = already && semver.Compare(in.Current, in.Next) != 0
	if !already {
		if w.IsWorkspace() {
			rel := declarations.VersionFile(in.Releasable)
			files.add(rel)
			if err := add(PlanEntry{Type: EntryWriteReleasableVersion, Summary: fmt.Sprintf("write %s = %s", rel, in.Next), Path: rel, Version: in.Next}); err != nil {
				return BumpPlan{}, err
			}
		}
		// The representative's versions are written first and the other
		// members' synced after, the order the Python planned them in.
		bumped := append([]declarations.Member(nil), versioned...)
		sort.SliceStable(bumped, func(i, j int) bool {
			return bumped[i].Name == in.Representative && bumped[j].Name != in.Representative
		})
		for _, m := range bumped {
			ts, err := targets.MemberTargets(root, m)
			if err != nil {
				return BumpPlan{}, err
			}
			entryType, verb := EntryWriteMemberVersions, "sync"
			if m.Name == in.Representative {
				entryType, verb = EntryWriteTargetVersions, "write"
			}
			for _, t := range ts {
				target, err := targets.Get(t.Name)
				if err != nil {
					return BumpPlan{}, err
				}
				dir := m.TargetDir(t)
				for _, f := range target.Facts().VersionFiles {
					files.add(declarations.Join(dir, f))
				}
				if err := add(PlanEntry{Type: entryType, Summary: fmt.Sprintf("%s the %s version in %s -> %s", verb, t.Name, dir, in.Next), Member: m.Name, Target: t.Name, Dir: dir, Version: in.Next}); err != nil {
					return BumpPlan{}, err
				}
			}
		}
		for _, m := range w.MembersOf(in.Releasable) {
			rel := declarations.Join(m.Path, selfdocFile)
			abs := filepath.Join(root, filepath.FromSlash(rel))
			data, err := os.ReadFile(abs)
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			if err != nil {
				return BumpPlan{}, err
			}
			info, err := os.Stat(abs)
			if err != nil {
				return BumpPlan{}, err
			}
			content, changed, err := bumpSelfdocJSON(data, in.Next.String())
			if err != nil {
				return BumpPlan{}, fmt.Errorf("%s: %w", rel, err)
			}
			if !changed {
				continue
			}
			files.add(rel)
			if err := add(PlanEntry{Type: EntryBumpSelfdoc, Summary: fmt.Sprintf("write %s (version %s)", rel, in.Next), Member: m.Name, Path: rel, Content: content, Mode: info.Mode().Perm()}); err != nil {
				return BumpPlan{}, err
			}
		}
	}
	if in.EcosystemTagging && r.PublishMode != declarations.PublishNone {
		for _, m := range versioned {
			ts, err := targets.MemberTargets(root, m)
			if err != nil {
				return BumpPlan{}, err
			}
			for _, t := range ts {
				manifest := ""
				switch t.Name {
				case targets.NPM:
					manifest = "package.json"
				case targets.PyPI:
					manifest = targets.Pyproject
				default:
					continue
				}
				dir := m.TargetDir(t)
				rel := declarations.Join(dir, manifest)
				files.add(rel)
				if err := add(PlanEntry{Type: EntryEnsureKeyword, Summary: "add the rlsbl keyword to " + rel, Member: m.Name, Target: t.Name, Dir: dir}); err != nil {
					return BumpPlan{}, err
				}
			}
		}
	}
	for i := range in.Syncs {
		s := in.Syncs[i]
		files.add(s.Lockfile)
		if err := add(PlanEntry{Type: EntrySyncLockfiles, Summary: fmt.Sprintf("run %s in %s", strings.Join(s.Argv, " "), s.Dir), Sync: &s, Dir: s.Dir}); err != nil {
			return BumpPlan{}, err
		}
	}
	state, found, err := scaffold.ReadState(root)
	if err != nil {
		return BumpPlan{}, err
	}
	if found && in.RlsblVersion != "" && state.RlsblVersion != in.RlsblVersion {
		state.RlsblVersion = in.RlsblVersion
		files.add(declarations.ScaffoldStateFile)
		files.add(path.Dir(declarations.ScaffoldStateFile) + "/manifest.toml")
		if err := add(PlanEntry{Type: EntryWriteScaffoldState, Summary: fmt.Sprintf("write %s (rlsbl %s)", declarations.ScaffoldStateFile, in.RlsblVersion), Path: declarations.ScaffoldStateFile, State: &state}); err != nil {
			return BumpPlan{}, err
		}
	}
	home := changelog.Home(w.Declarations, in.Releasable)
	files.add(home)
	// Writing the changelog under the changelog root creates the root's
	// ownership manifest when it is missing, and the release commit
	// carries it.
	if strings.HasPrefix(home, declarations.ChangelogRoot+"/") {
		files.add(declarations.ChangelogRoot + "/manifest.toml")
	}
	if w.IsWorkspace() {
		files.add(changelog.RollUpPath)
	}
	for _, g := range in.Generated {
		files.add(g)
	}
	var built []string
	var buildEntries []PlanEntry
	if r.PublishMode != declarations.PublishNone {
		for _, m := range versioned {
			built = append(built, m.Path)
			ts, err := targets.MemberTargets(root, m)
			if err != nil {
				return BumpPlan{}, err
			}
			for _, t := range ts {
				target, err := targets.Get(t.Name)
				if err != nil {
					return BumpPlan{}, err
				}
				dir := m.TargetDir(t)
				built = append(built, dir)
				if target.Facts().BuildTimeoutSeconds == 0 {
					continue
				}
				buildEntries = append(buildEntries, PlanEntry{Type: EntryBuild, Summary: fmt.Sprintf("build %s in %s", t.Name, dir), Member: m.Name, Target: t.Name, Dir: dir, Version: in.Next})
			}
		}
	}
	built = uniqueSorted(built)
	if len(built) > 0 {
		if err := add(PlanEntry{Type: EntryCleanArtifacts, Summary: "remove the artifacts an earlier build left in dist/", Dirs: built}); err != nil {
			return BumpPlan{}, err
		}
	}
	for _, b := range buildEntries {
		if err := add(b); err != nil {
			return BumpPlan{}, err
		}
	}
	if len(built) > 0 {
		if err := add(PlanEntry{Type: EntrySecretScan, Summary: "scan the built artifacts for secrets (gitleaks)", Dirs: built}); err != nil {
			return BumpPlan{}, err
		}
		if err := add(PlanEntry{Type: EntryPackedContents, Summary: "refuse a packed artifact carrying a file from outside the releasable's members, or a confidential name"}); err != nil {
			return BumpPlan{}, err
		}
	}
	plan.FilesToCommit = files.list()
	if err := add(PlanEntry{Type: EntryGuardUnexpectedFiles, Summary: "refuse if files outside the release's own set changed", Expected: plan.FilesToCommit}); err != nil {
		return BumpPlan{}, err
	}
	return plan, nil
}

// versionAlreadyWritten reports whether the releasable's version source
// already names v: a workspace releasable's version file, or a standalone
// project's primary target manifest.
func versionAlreadyWritten(w *workspace.Workspace, releasable, primary string, v semver.Version) (bool, error) {
	if w.IsWorkspace() {
		current, err := w.ReadReleasableVersion(releasable)
		if err != nil {
			return false, err
		}
		return semver.Compare(current, v) == 0, nil
	}
	current, err := standaloneVersion(w, releasable, primary)
	if err != nil {
		return false, err
	}
	return semver.Compare(current, v) == 0, nil
}

// standaloneVersion reads a standalone project's version from its primary
// target's manifest.
func standaloneVersion(w *workspace.Workspace, releasable, primary string) (semver.Version, error) {
	m := w.RootMember()
	ts, err := targets.MemberTargets(w.Root, m)
	if err != nil {
		return semver.Version{}, err
	}
	for _, t := range ts {
		if t.Name != primary {
			continue
		}
		target, err := targets.Get(t.Name)
		if err != nil {
			return semver.Version{}, err
		}
		return target.ReadVersion(filepath.Join(w.Root, filepath.FromSlash(m.TargetDir(t))))
	}
	var names []string
	for _, t := range ts {
		names = append(names, t.Name)
	}
	return semver.Version{}, fmt.Errorf("the release file includes %s first, which is not a target of the project (its targets are %s), so its version cannot be read; fix include in %s", primary, strings.Join(names, ", "), releaserecord.ReleaseFilePath(releasable))
}

// pathSet is an ordered set of repository-relative paths.
type pathSet struct {
	seen  map[string]bool
	order []string
}

func newPathSet() *pathSet { return &pathSet{seen: map[string]bool{}} }

func (s *pathSet) add(p string) {
	if p != "" && !s.seen[p] {
		s.seen[p] = true
		s.order = append(s.order, p)
	}
}

func (s *pathSet) list() []string { return append([]string(nil), s.order...) }

func uniqueSorted(items []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, i := range items {
		if !seen[i] {
			seen[i] = true
			out = append(out, i)
		}
	}
	sort.Strings(out)
	return out
}

// RenderPlan is the plan as the preview's table shows it, one line per
// entry.
func RenderPlan(entries []PlanEntry) string {
	lines := make([]string, len(entries))
	for i, e := range entries {
		lines[i] = fmt.Sprintf("  %2d. %-26s %s", i+1, e.Type, e.Summary)
	}
	return strings.Join(lines, "\n")
}

// BumpExecution is what issuing the version-bumped plan needs besides the
// plan.
type BumpExecution struct {
	Workspace  *workspace.Workspace
	Releasable string
	// Repo is the repository the release writes: the release checkout.
	Repo git.Repo
	// LiveRoot is the working tree, which refusals name as where a fix is
	// made.
	LiveRoot string
	// Environment is what every program the release runs gets.
	Environment map[string]string
	// Rerun is how the release continues after a refusal is dealt with.
	Rerun string
	// Preview is set under --dry-run: every write is recorded, and the
	// steps that read what a recorded write would have produced (the scan,
	// the packed contents, the guard) say so instead of running.
	Preview bool
	Record  *lifecycle.Record
	Index   *index.Index
	// Scratch is the command's scratch directory, where npm writes its cache
	// when the packed contents list an npm upload.
	Scratch targets.Scratch
	Now     time.Time
	Log     func(string)
}

// BumpResult is what issuing the version-bumped plan did.
type BumpResult struct {
	// Written are the repository-relative files the version writers report
	// touching, beyond what the plan predicted (a package's __version__).
	Written []string
}

// ExecuteBumpPlan issues every entry of the plan, in order. It derives
// nothing: each entry carries its operands, and its reads are the
// guard's status read and the packed-artifact listing, which read what the
// entries before them produced.
func ExecuteBumpPlan(e *strictcli.Effects, plan BumpPlan, x BumpExecution) (BumpResult, error) {
	var result BumpResult
	root := x.Workspace.Root
	written := newPathSet()
	note := x.Log
	if note == nil {
		note = func(string) {}
	}
	log := note
	if x.Preview {
		log = func(string) {}
	}
	abs := func(rel string) string { return filepath.Join(root, filepath.FromSlash(rel)) }
	for _, entry := range plan.Entries {
		switch entry.Type {
		case EntryWriteReleasableVersion:
			if err := x.Workspace.WriteReleasableVersion(e, x.Releasable, entry.Version); err != nil {
				return result, err
			}
			log(fmt.Sprintf("Wrote %s = %s", entry.Path, entry.Version))
		case EntryWriteTargetVersions, EntryWriteMemberVersions:
			target, err := targets.Get(entry.Target)
			if err != nil {
				return result, err
			}
			files, err := target.WriteVersion(e, abs(entry.Dir), entry.Version)
			if err != nil {
				return result, err
			}
			for _, f := range files {
				written.add(declarations.Join(entry.Dir, filepath.ToSlash(f)))
			}
			log(fmt.Sprintf("Wrote version %s to %s", entry.Version, strings.Join(files, ", ")))
		case EntryBumpSelfdoc:
			tmp := abs(entry.Path) + ".rlsbl-writing"
			if _, err := e.Write(tmp, entry.Content, strictcli.Mode(entry.Mode)); err != nil {
				return result, err
			}
			if _, err := e.Rename(tmp, abs(entry.Path)); err != nil {
				return result, err
			}
			log("Wrote the version to " + entry.Path)
		case EntryEnsureKeyword:
			ensure := tagging.EnsureNpmKeyword
			if entry.Target == targets.PyPI {
				ensure = tagging.EnsurePypiKeyword
			}
			if _, _, err := ensure(e, abs(entry.Dir)); err != nil {
				return result, err
			}
		case EntrySyncLockfiles:
			where := filepath.Join(x.LiveRoot, filepath.FromSlash(entry.Sync.Dir))
			if err := SyncLockfile(e, root, *entry.Sync, x.Environment, where, x.Rerun, x.Preview); err != nil {
				return result, err
			}
		case EntryWriteScaffoldState:
			if err := scaffold.WriteState(e, root, *entry.State); err != nil {
				return result, err
			}
		case EntryCleanArtifacts:
			removed, err := CleanArtifacts(e, root, entry.Dirs)
			if err != nil {
				return result, err
			}
			for _, f := range removed {
				log("Removed the stale artifact " + f)
			}
		case EntryBuild:
			if err := build(e, x, entry); err != nil {
				return result, err
			}
		case EntrySecretScan:
			if x.Preview {
				note("Secret scan: not previewable (it scans the artifacts the recorded build did not produce)")
				continue
			}
			scratch, err := secretScanDir(x.Repo)
			if err != nil {
				return result, err
			}
			scanned, err := ScanArtifacts(e, root, scratch, entry.Dirs)
			if err != nil {
				return result, err
			}
			log(fmt.Sprintf("Secret scan: %d artifact(s) clean", len(scanned)))
		case EntryPackedContents:
			if x.Preview {
				note("Packed-artifact contents: not previewable (they are listed from the artifacts the recorded build did not produce)")
				continue
			}
			if err := checkPacked(e, x); err != nil {
				return result, err
			}
		case EntryGuardUnexpectedFiles:
			if x.Preview {
				continue
			}
			if err := guardUnexpected(x.Repo, entry.Expected, written.list()); err != nil {
				return result, err
			}
		default:
			return result, fmt.Errorf("the version-bumped plan holds the entry %q, which nothing issues", entry.Type)
		}
	}
	result.Written = written.list()
	return result, nil
}

// build builds one target, its pyproject.toml's direct references to
// workspace siblings rewritten to registry constraints at their versions.
func build(e *strictcli.Effects, x BumpExecution, entry PlanEntry) error {
	target, err := targets.Get(entry.Target)
	if err != nil {
		return err
	}
	dir := filepath.Join(x.Workspace.Root, filepath.FromSlash(entry.Dir))
	in := targets.BuildInputs{Dir: dir}
	if entry.Target == targets.PyPI && x.Workspace.IsWorkspace() {
		constraints, err := siblingConstraints(x.Workspace)
		if err != nil {
			return err
		}
		manifest := filepath.Join(dir, targets.Pyproject)
		data, err := os.ReadFile(manifest)
		if err != nil {
			return err
		}
		rewritten, n, err := dependencies.RewritePublishedDirectReferences(manifest, data, constraints)
		if err != nil {
			return err
		}
		if n > 0 {
			in.RewrittenPyproject = rewritten
		}
	}
	return targets.Build(e, target, in)
}

// siblingConstraints map every workspace member's PyPI package name to a
// registry constraint at its version (>=version), for the direct references
// a published manifest must not carry.
func siblingConstraints(w *workspace.Workspace) (map[string]string, error) {
	out := map[string]string{}
	pypi, err := targets.Get(targets.PyPI)
	if err != nil {
		return nil, err
	}
	for _, m := range w.Members() {
		ts, err := targets.MemberTargets(w.Root, m)
		if err != nil {
			return nil, err
		}
		for _, t := range ts {
			if t.Name != targets.PyPI {
				continue
			}
			dir := filepath.Join(w.Root, filepath.FromSlash(m.TargetDir(t)))
			name, found, err := pypi.ReadName(dir)
			if err != nil {
				return nil, err
			}
			if !found {
				continue
			}
			v, err := pypi.ReadVersion(dir)
			if err != nil {
				return nil, err
			}
			out[name] = ">=" + v.String()
		}
	}
	return out, nil
}

// checkPacked lists what the release would publish and refuses a file from
// outside the releasable's members and, in a public repository, a
// confidential name in any packed text.
func checkPacked(e *strictcli.Effects, x BumpExecution) error {
	artifacts, err := publishrules.PackedArtifacts(e, x.Repo, x.Workspace, x.Releasable, x.Scratch)
	if err != nil {
		return err
	}
	if err := publishrules.CheckPackedContents(x.Workspace.Declarations, x.Releasable, artifacts); err != nil {
		return err
	}
	scanner, err := publishrules.NewScanner(x.Record, x.Now, x.Index)
	if err != nil {
		return err
	}
	return scanner.ScanArtifacts(x.Workspace.Root, artifacts)
}

// guardUnexpected refuses a change in the release checkout outside the
// release's own set: something other than the release wrote into the tree
// it is about to commit.
func guardUnexpected(repo git.Repo, expected, written []string) error {
	changes, err := repo.Changes()
	if err != nil {
		return err
	}
	allowed := map[string]bool{}
	for _, p := range expected {
		allowed[p] = true
	}
	for _, p := range written {
		allowed[p] = true
	}
	var unexpected []string
	for _, c := range changes {
		if !allowed[c.Path] && !isRunState(c.Path) {
			unexpected = append(unexpected, c.Path)
		}
	}
	if len(unexpected) == 0 {
		return nil
	}
	sort.Strings(unexpected)
	return fmt.Errorf("the release checkout has changes the release did not make: %s. Something other than the release (a build writing into the tree, a hook running late) changed it, and the release commit must carry only the release's own files; nothing was committed. Make the build write only paths git ignores, then run the release again", strings.Join(unexpected, ", "))
}
