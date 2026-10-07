package publishrules

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/gomodule"
	"github.com/stricttools/rlsbl/internal/pipelines"
	"github.com/stricttools/rlsbl/internal/targets"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// goListTimeout bounds one `go list -deps -json`, which reads the local tree
// and the module cache.
const goListTimeout = 5 * time.Minute

// PackedFile is one file a published artifact carries.
type PackedFile struct {
	// Entry is the file as the artifact names it.
	Entry string
	// Source is the repository-relative file it was packed from; empty
	// when it comes from outside the repository's files (a wheel entry no
	// file of the repository matches).
	Source string
	// Matches are, for an entry attributed by its content (a wheel's), every
	// repository file holding that content, sorted; Source is one of them.
	// An entry whose content files of several members hold (an empty
	// __init__.py, a license text) is the releasable's own when one of them
	// lies in its members.
	Matches []string
	// content is the entry's bytes when they were read from the artifact
	// itself (a wheel); otherwise the bytes are Source's.
	content []byte
}

// Artifact is one published artifact and the files it carries.
type Artifact struct {
	// Label names the artifact in a refusal.
	Label string
	Files []PackedFile
}

// PackedArtifacts lists what the releasable's release would publish, after
// the build step: for each pipeline of its members (none under publish
// mode none), an npm package's tarball (`npm pack --dry-run`), a pypi
// package's wheels in its target's dist directory, a go library's module
// zip, and the sources of a go binary pipeline's binaries (each package
// directory's files and embedded files inside the repository, for every
// platform of the platform table), which npm and pypi go-binary pipelines
// carry too. repo is the repository w describes; scratch holds npm's cache.
func PackedArtifacts(h targets.Handle, repo git.Repo, w *workspace.Workspace, releasable string, scratch targets.Scratch) ([]Artifact, error) {
	r, ok := w.Declarations.Releasable(releasable)
	if !ok {
		return nil, fmt.Errorf("no releasable %q is declared in .strictmetadata/releasables/releasables.toml", releasable)
	}
	if r.PublishMode == declarations.PublishNone {
		return nil, nil
	}
	var repoFiles []string
	listRepoFiles := func() ([]string, error) {
		if repoFiles == nil {
			files, err := repo.TrackedAndUntrackedFiles()
			if err != nil {
				return nil, err
			}
			repoFiles = files
		}
		return repoFiles, nil
	}
	var out []Artifact
	binaries := map[string]bool{}
	for _, m := range w.MembersOf(releasable) {
		goBinary := func(name string) error {
			key := m.Name + "\x00" + name
			if binaries[key] {
				return nil
			}
			binaries[key] = true
			p, ok := m.Pipeline(name)
			if !ok {
				return fmt.Errorf("the member %q declares no pipeline %q", m.Name, name)
			}
			dir, err := targetDir(m, p)
			if err != nil {
				return err
			}
			a, err := ListGoBinary(h, w.Root, dir, p.InstallPaths)
			if err != nil {
				return err
			}
			a.Label = fmt.Sprintf("the binaries of the go pipeline %q of the member %q", p.Name, m.Name)
			out = append(out, a)
			return nil
		}
		for _, p := range m.Pipelines {
			dir, err := targetDir(m, p)
			if err != nil {
				return nil, err
			}
			label := fmt.Sprintf("the %s pipeline %q of the member %q", p.Type, p.Name, m.Name)
			switch {
			case p.Type == declarations.TargetGo && p.Artifact == declarations.ArtifactBinary:
				if err := goBinary(p.Name); err != nil {
					return nil, err
				}
			case p.Type == declarations.TargetGo:
				a, err := listUpload(h, repo, w.Root, dir, targets.Go, scratch)
				if err != nil {
					return nil, err
				}
				a.Label = "the module zip of " + label
				out = append(out, a)
			case p.Type == declarations.TargetNPM:
				a, err := listUpload(h, repo, w.Root, dir, targets.NPM, scratch)
				if err != nil {
					return nil, err
				}
				a.Label = "the `npm pack` tarball of " + label
				out = append(out, a)
				if p.Artifact == declarations.ArtifactGoBinary {
					if err := goBinary(p.BinaryPipeline); err != nil {
						return nil, err
					}
				}
			case p.Type == declarations.TargetPyPI && p.Artifact == declarations.ArtifactGoBinary:
				if err := goBinary(p.BinaryPipeline); err != nil {
					return nil, err
				}
			case p.Type == declarations.TargetPyPI:
				files, err := listRepoFiles()
				if err != nil {
					return nil, err
				}
				wheels, err := ListWheels(w.Root, dir, files)
				if err != nil {
					return nil, err
				}
				for i := range wheels {
					wheels[i].Label += " of " + label
				}
				out = append(out, wheels...)
			}
		}
	}
	return out, nil
}

// listUpload is targets.ListUpload of the target in dir (repository-relative),
// with each file named from the repository root.
func listUpload(h targets.Handle, repo git.Repo, root, dir, target string, scratch targets.Scratch) (Artifact, error) {
	t, err := targets.Get(target)
	if err != nil {
		return Artifact{}, err
	}
	l, found, err := targets.ListUpload(h, repo, t, filepath.Join(root, filepath.FromSlash(dir)), scratch)
	if err != nil {
		return Artifact{}, err
	}
	if !found {
		return Artifact{}, fmt.Errorf("the %s target in %s has no offline upload listing", target, dir)
	}
	a := Artifact{}
	for _, f := range l.Files {
		a.Files = append(a.Files, PackedFile{Entry: f, Source: declarations.Join(dir, f)})
	}
	return a, nil
}

// ListWheels lists every wheel in dir/dist (dir repository-relative). Each
// entry outside the wheel's *.dist-info/ directory is matched by content to
// one of files (repository-relative, as `git ls-files --cached --others
// --exclude-standard` lists them); an entry matching none has no Source. A
// dist directory holding no wheel is an error: the build step builds one.
func ListWheels(root, dir string, files []string) ([]Artifact, error) {
	dist := filepath.Join(root, filepath.FromSlash(dir), "dist")
	matches, err := filepath.Glob(filepath.Join(dist, "*.whl"))
	if err != nil {
		return nil, err
	}
	sort.Strings(matches)
	if len(matches) == 0 {
		return nil, fmt.Errorf("%s holds no wheel; the build step builds the wheel the contents check reads", filepath.ToSlash(filepath.Join(dir, "dist")))
	}
	byContent, err := contentIndex(root, files)
	if err != nil {
		return nil, err
	}
	var out []Artifact
	for _, wheel := range matches {
		a, err := listWheel(wheel, byContent)
		if err != nil {
			return nil, err
		}
		a.Label = "the wheel " + filepath.Base(wheel)
		out = append(out, a)
	}
	return out, nil
}

// contentIndex maps each file's SHA-256 to the files holding that content,
// sorted.
func contentIndex(root string, files []string) (map[[sha256.Size]byte][]string, error) {
	index := map[[sha256.Size]byte][]string{}
	for _, f := range files {
		data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(f)))
		if err != nil {
			if os.IsNotExist(err) {
				// A tracked file deleted from the working tree packs nothing.
				continue
			}
			return nil, err
		}
		sum := sha256.Sum256(data)
		index[sum] = append(index[sum], f)
	}
	for _, paths := range index {
		sort.Strings(paths)
	}
	return index, nil
}

func listWheel(wheel string, byContent map[[sha256.Size]byte][]string) (Artifact, error) {
	z, err := zip.OpenReader(wheel)
	if err != nil {
		return Artifact{}, fmt.Errorf("reading the wheel %s: %w", wheel, err)
	}
	defer z.Close()
	var a Artifact
	for _, f := range z.File {
		if strings.HasSuffix(f.Name, "/") {
			continue
		}
		top, _, _ := strings.Cut(f.Name, "/")
		if strings.HasSuffix(top, ".dist-info") {
			continue
		}
		rc, err := f.Open()
		if err != nil {
			return Artifact{}, fmt.Errorf("reading %s in the wheel %s: %w", f.Name, wheel, err)
		}
		data, err := io.ReadAll(rc)
		rc.Close()
		if err != nil {
			return Artifact{}, fmt.Errorf("reading %s in the wheel %s: %w", f.Name, wheel, err)
		}
		pf := PackedFile{Entry: f.Name, content: data}
		if paths := byContent[sha256.Sum256(data)]; len(paths) > 0 {
			pf.Source = preferredSource(f.Name, paths)
			pf.Matches = paths
		}
		a.Files = append(a.Files, pf)
	}
	return a, nil
}

// preferredSource picks, among the files whose content an entry matches,
// the one whose path ends with the entry's path, so a file packed from its
// own place is named rather than an identical copy elsewhere; otherwise the
// first.
func preferredSource(entry string, paths []string) string {
	for _, p := range paths {
		if p == entry || strings.HasSuffix(p, "/"+entry) {
			return p
		}
	}
	return paths[0]
}

// goPackage is the part of `go list -json` output the listing reads.
type goPackage struct {
	ImportPath string
	Dir        string
	Standard   bool
	GoFiles    []string
	CgoFiles   []string
	CFiles     []string
	CXXFiles   []string
	MFiles     []string
	HFiles     []string
	FFiles     []string
	SFiles     []string
	SysoFiles  []string
	EmbedFiles []string
}

func (p goPackage) files() []string {
	var out []string
	for _, list := range [][]string{p.GoFiles, p.CgoFiles, p.CFiles, p.CXXFiles, p.MFiles, p.HFiles, p.FFiles, p.SFiles, p.SysoFiles, p.EmbedFiles} {
		out = append(out, list...)
	}
	return out
}

// ListGoBinary lists the files inside the repository rooted at root that
// the binaries built from the go module in dir (repository-relative) are
// compiled from or embed: every non-standard package `go list -deps -json`
// reports for the main packages, for each platform of the platform table
// (CGO off, as the release builds). The main packages are installPaths, or
// the module's one main package when none are declared. A package outside
// the repository (a dependency from the module cache) is another project's
// published code and is not listed.
func ListGoBinary(h gomodule.Runner, root, dir string, installPaths []string) (Artifact, error) {
	moduleDir := filepath.Join(root, filepath.FromSlash(dir))
	var mains []string
	if installPaths != nil {
		paths, err := gomodule.ValidateInstallPaths(h, moduleDir, installPaths)
		if err != nil {
			return Artifact{}, err
		}
		mains = paths
	} else {
		main, err := gomodule.ResolveMainPackageDir(h, moduleDir, nil)
		if err != nil {
			return Artifact{}, err
		}
		mains = []string{main}
	}
	realRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return Artifact{}, err
	}
	seen := map[string]bool{}
	var a Artifact
	for _, p := range pipelines.Platforms() {
		argv := []interface{}{"go", "list", "-deps", "-json"}
		for _, m := range mains {
			argv = append(argv, m)
		}
		res, err := h.Run(argv, strictcli.Cwd(moduleDir), strictcli.Check(false), strictcli.Timeout(goListTimeout),
			strictcli.EffectEnv(map[string]string{"GOOS": p.GOOS, "GOARCH": p.GOARCH, "CGO_ENABLED": "0"}))
		if err != nil {
			return Artifact{}, err
		}
		if res.ExitCode() != 0 {
			return Artifact{}, fmt.Errorf("`go list -deps -json` for %s/%s failed in %s (exit %d):\n%s", p.GOOS, p.GOARCH, dir, res.ExitCode(), strings.TrimSpace(res.Stderr()))
		}
		dec := json.NewDecoder(strings.NewReader(res.Stdout()))
		for dec.More() {
			var pkg goPackage
			if err := dec.Decode(&pkg); err != nil {
				return Artifact{}, fmt.Errorf("`go list -deps -json` in %s printed something that is not package JSON: %w", dir, err)
			}
			if pkg.Standard || pkg.Dir == "" {
				continue
			}
			pkgDir, err := filepath.EvalSymlinks(pkg.Dir)
			if err != nil {
				return Artifact{}, fmt.Errorf("`go list` in %s named the package directory %s: %w", dir, pkg.Dir, err)
			}
			rel, err := filepath.Rel(realRoot, pkgDir)
			if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
				continue
			}
			for _, f := range pkg.files() {
				source := path.Join(filepath.ToSlash(rel), filepath.ToSlash(f))
				if seen[source] {
					continue
				}
				seen[source] = true
				a.Files = append(a.Files, PackedFile{Entry: source + " (package " + pkg.ImportPath + ")", Source: source})
			}
		}
	}
	sort.Slice(a.Files, func(i, j int) bool { return a.Files[i].Source < a.Files[j].Source })
	return a, nil
}

// ownMatch reports whether an entry attributed by its content matches a
// file the releasable's members own.
func ownMatch(d *declarations.Releasables, releasable string, f PackedFile) bool {
	for _, p := range f.Matches {
		if owner, ok := d.MemberForPath(p); ok && owner.Releasable == releasable {
			return true
		}
	}
	return false
}

// CheckPackedContents refuses every packed file that does not come from the
// releasable's member paths: a file another member owns (a nested member
// included), a file of the repository no member owns, and a wheel entry no
// file of the repository matches. The refusal names each file and the
// artifact carrying it.
func CheckPackedContents(d *declarations.Releasables, releasable string, artifacts []Artifact) error {
	var problems []string
	for _, a := range artifacts {
		for _, f := range a.Files {
			if f.Source == "" {
				problems = append(problems, fmt.Sprintf("%s carries %s, which matches no file of the repository", a.Label, f.Entry))
				continue
			}
			if ownMatch(d, releasable, f) {
				continue
			}
			owner, ok := d.MemberForPath(f.Source)
			switch {
			case !ok:
				problems = append(problems, fmt.Sprintf("%s carries %s, from %s, which no member owns", a.Label, f.Entry, f.Source))
			case owner.Releasable != releasable:
				problems = append(problems, fmt.Sprintf("%s carries %s, from %s, which the member %q owns", a.Label, f.Entry, f.Source, owner.Name))
			}
		}
	}
	if len(problems) == 0 {
		return nil
	}
	return fmt.Errorf("the release of %q would publish files from outside its members' paths; a published artifact carries only its releasable's own files, so stop packing each one (narrow package.json's \"files\", the wheel's build configuration, or the Go imports and embeds) or move it into a member of %q:\n  - %s",
		releasable, releasable, strings.Join(problems, "\n  - "))
}
