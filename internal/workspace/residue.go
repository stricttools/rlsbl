package workspace

import (
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/rlsbl/internal/declarations"
)

// Residue is release state sitting where nothing reads it: a directory of
// the old layout (.rlsbl/, .rlsbl-monorepo/), a member's own CHANGELOG.md
// in a workspace (a workspace releasable's changelog is generated under
// .strictmetadata/changelog/), or the release state of a subject the
// declarations do not hold. A retired subject's release state lives under
// .strictmetadata/retired-release-histories/ and is never residue.
type Residue struct {
	// Path is repository-relative.
	Path      string
	Directory bool
	// Reason says why nothing reads it.
	Reason string
	// CleanupRemoves says whether `monorepo cleanup` deletes it. Release
	// state of an undeclared subject is a record of what it released, which
	// cleanup never deletes.
	CleanupRemoves bool
}

// DetectResidue finds the residue on disk at the repository root and at
// every member's path, and, through tracked (the repository's tracked
// files), old-layout directories anywhere else.
func (w *Workspace) DetectResidue(tracked []string) ([]Residue, error) {
	found := map[string]Residue{}
	add := func(r Residue) {
		if _, ok := found[r.Path]; !ok {
			found[r.Path] = r
		}
	}
	oldLayout := func(p string, directory bool) Residue {
		return Residue{
			Path:           p,
			Directory:      directory,
			Reason:         "a directory of the old layout, which rlsbl no longer reads; its records live under " + declarations.MetadataDir + "/",
			CleanupRemoves: true,
		}
	}

	if info, err := w.stat(oldWorkspaceDir); err != nil {
		return nil, err
	} else if info != nil {
		add(oldLayout(oldWorkspaceDir, info.IsDir()))
	}
	for _, m := range w.Members() {
		p := declarations.Join(m.Path, oldStateDir)
		info, err := w.stat(p)
		if err != nil {
			return nil, err
		}
		if info != nil {
			add(oldLayout(p, info.IsDir()))
		}
		if !m.IsRoot() {
			records, err := w.memberRecords(m)
			if err != nil {
				return nil, err
			}
			for _, r := range records {
				add(r)
			}
		}
		if !w.IsWorkspace() || m.IsRoot() || !m.Versioned() {
			continue
		}
		changelog := declarations.Join(m.Path, "CHANGELOG.md")
		info, err = w.stat(changelog)
		if err != nil {
			return nil, err
		}
		if info != nil && !info.IsDir() {
			add(Residue{
				Path:           changelog,
				Reason:         fmt.Sprintf("a member's own changelog; the changelog of the releasable %q is generated at %s/CHANGELOG.md", m.Releasable, declarations.ChangelogDir(m.Releasable)),
				CleanupRemoves: true,
			})
		}
	}
	for _, f := range tracked {
		p := cleanPath(f)
		if strings.HasPrefix(p, oldWorkspaceDir+"/") {
			add(oldLayout(oldWorkspaceDir, true))
			continue
		}
		parts := strings.Split(p, "/")
		for i, part := range parts[:len(parts)-1] {
			if part == oldStateDir {
				add(oldLayout(strings.Join(parts[:i+1], "/"), true))
				break
			}
		}
	}

	undeclared, err := w.undeclaredState()
	if err != nil {
		return nil, err
	}
	for _, r := range undeclared {
		add(r)
	}

	out := make([]Residue, 0, len(found))
	for _, r := range found {
		out = append(out, r)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out, nil
}

// memberRecordDirs are the directories of a repository's own records under
// .strictmetadata/, as a repository absorbed into a workspace brings them
// inside its member, where nothing reads them: the workspace's records are
// at its root. keep marks the ones holding the record of what a subject
// released, which cleanup never deletes; the rest are declarations,
// generated state, or records `monorepo absorb` carried into the
// workspace's own.
var memberRecordDirs = []struct {
	dir  string
	keep bool
}{
	{declarations.ReleasablesDir, false},
	{declarations.TestRunnerDir, false},
	{declarations.ChangelogRoot, true},
	{declarations.ReleasesRoot, true},
	{declarations.BatchReleasesDir, false},
	{path.Dir(declarations.TransitionsFile), false},
	{declarations.HistoryRewritesDir, true},
	{declarations.RetiredHistoriesRoot, true},
	{declarations.ReleaseHooksRoot, false},
	{path.Dir(declarations.ScaffoldStateFile), false},
	{declarations.ScaffoldBasesDir, false},
	{declarations.ChangelogValidationDir, false},
	{declarations.ReleaseStateDir, false},
	{lifecycleRecordDir, false},
}

// lifecycleRecordDir is the lifecycle-and-license record's directory, which
// strictspec owns.
const lifecycleRecordDir = declarations.MetadataDir + "/lifecycle-and-license"

// memberRecords are the record directories present under the member's own
// .strictmetadata/.
func (w *Workspace) memberRecords(m declarations.Member) ([]Residue, error) {
	var out []Residue
	for _, d := range memberRecordDirs {
		p := declarations.Join(m.Path, d.dir)
		info, err := w.stat(p)
		if err != nil {
			return nil, err
		}
		if info == nil || !info.IsDir() {
			continue
		}
		reason := fmt.Sprintf("records of a repository of its own, kept inside the member %q, which rlsbl does not read: this repository's records are under %s/ at its root", m.Name, declarations.MetadataDir)
		if d.keep {
			reason += fmt.Sprintf("; they record what a subject released, so cleanup keeps them: move what this repository needs into %s/ at its root, and delete the rest through saferm", d.dir)
		}
		out = append(out, Residue{Path: p, Directory: true, Reason: reason, CleanupRemoves: !d.keep})
	}
	return out, nil
}

// stat is the repository-relative path's file information, nil when it does
// not exist.
func (w *Workspace) stat(rel string) (os.FileInfo, error) {
	info, err := os.Lstat(filepath.Join(w.Root, filepath.FromSlash(rel)))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", rel, err)
	}
	return info, nil
}

// entries are the names in a repository-relative directory; none when it
// does not exist.
func (w *Workspace) entries(rel string) ([]os.DirEntry, error) {
	list, err := os.ReadDir(filepath.Join(w.Root, filepath.FromSlash(rel)))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", rel, err)
	}
	return list, nil
}

// undeclaredState is the per-releasable state of names no releasable is
// declared with.
func (w *Workspace) undeclaredState() ([]Residue, error) {
	var out []Residue
	declared := func(name string) bool {
		_, ok := w.Declarations.Releasable(name)
		return ok
	}
	for _, root := range []string{declarations.ChangelogRoot, declarations.ReleasesRoot} {
		list, err := w.entries(root)
		if err != nil {
			return nil, err
		}
		for _, entry := range list {
			if !entry.IsDir() || declared(entry.Name()) {
				continue
			}
			out = append(out, Residue{
				Path:      root + "/" + entry.Name(),
				Directory: true,
				Reason:    fmt.Sprintf("release state of %q, which no releasable is declared as, so nothing releases from it; a subject that stopped releasing keeps it as %s/", entry.Name(), declarations.RetiredHistoryDir(entry.Name())+"/"+path.Base(root)),
			})
		}
	}
	list, err := w.entries(declarations.ChangelogValidationDir)
	if err != nil {
		return nil, err
	}
	for _, entry := range list {
		name, ok := strings.CutSuffix(entry.Name(), ".toml")
		if entry.IsDir() || !ok || declared(name) {
			continue
		}
		out = append(out, Residue{
			Path:           declarations.ChangelogValidationFile(name),
			Reason:         fmt.Sprintf("the changelog validation cache of %q, which no releasable is declared as", name),
			CleanupRemoves: true,
		})
	}
	return out, nil
}
