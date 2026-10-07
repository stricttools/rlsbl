package release

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/git"
)

// A release scans every artifact it built for leaked secrets with gitleaks
// before anything is pushed or published. There is no way past it: a
// finding stops the release, and false positives are allowlisted in the
// project's own .gitleaks.toml.

// artifactPatterns are the archive types the scan unpacks; the clean before
// the build removes the same.
var artifactPatterns = []string{"*.whl", "*.tar.gz", "*.tgz", "*.zip"}

// SecretScanRelative is where the scan unpacks artifacts, relative to the
// repository's git common directory: outside the working tree and the
// release checkout, emptied by every scan.
const SecretScanRelative = "rlsbl/secret-scan"

// gitleaksInstall is what installs gitleaks.
const gitleaksInstall = "go install github.com/zricethezav/gitleaks/v8@latest (or brew install gitleaks, or sudo apt install gitleaks; see https://github.com/gitleaks/gitleaks#installing)"

// gitleaksConfig is the project's own gitleaks configuration, honored when
// present.
const gitleaksConfig = ".gitleaks.toml"

// distDirs are the dist directories of the repository-relative dirs, each
// once.
func distDirs(dirs []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, d := range dirs {
		dist := path.Join(d, "dist")
		if d == "." {
			dist = "dist"
		}
		if !seen[dist] {
			seen[dist] = true
			out = append(out, dist)
		}
	}
	return out
}

// findArtifacts are the artifacts in the dist directories of dirs under
// root, absolute and sorted.
func findArtifacts(root string, dirs []string) ([]string, error) {
	seen := map[string]bool{}
	var out []string
	for _, dist := range distDirs(dirs) {
		abs := filepath.Join(root, filepath.FromSlash(dist))
		for _, pattern := range artifactPatterns {
			matches, err := filepath.Glob(filepath.Join(abs, pattern))
			if err != nil {
				return nil, err
			}
			for _, m := range matches {
				if info, err := os.Stat(m); err == nil && info.Mode().IsRegular() && !seen[m] {
					seen[m] = true
					out = append(out, m)
				}
			}
		}
	}
	sort.Strings(out)
	return out, nil
}

// CleanArtifacts removes the artifacts an earlier build left in the dist
// directories of dirs under root, so the scan covers what this release
// builds and nothing else; every other file in dist stays. It returns the
// removed files, absolute.
func CleanArtifacts(e *strictcli.Effects, root string, dirs []string) ([]string, error) {
	found, err := findArtifacts(root, dirs)
	if err != nil {
		return nil, err
	}
	for _, f := range found {
		if _, err := e.Remove(f); err != nil {
			return nil, fmt.Errorf("removing the stale artifact %s: %w", f, err)
		}
	}
	return found, nil
}

// SecretScanError is the refusal of artifacts gitleaks found secrets in.
type SecretScanError struct{ Message string }

func (e *SecretScanError) Error() string { return e.Message }

// ScanArtifacts scans every artifact in the dist directories of dirs under
// root with `gitleaks dir`, each unpacked into its own directory under the
// scratch directory scratch (absolute, emptied first), honoring the
// project's .gitleaks.toml at root. A missing gitleaks, a gitleaks that
// fails, and any finding are errors. It returns the scanned artifacts.
func ScanArtifacts(e *strictcli.Effects, root, scratch string, dirs []string) ([]string, error) {
	if _, err := exec.LookPath("gitleaks"); err != nil {
		return nil, fmt.Errorf("gitleaks is not on PATH, and a release scans every artifact it built for secrets before publishing; install it: %s, then run the release again", gitleaksInstall)
	}
	artifacts, err := findArtifacts(root, dirs)
	if err != nil {
		return nil, err
	}
	if len(artifacts) == 0 {
		return nil, nil
	}
	if _, err := e.Remove(scratch); err != nil {
		return nil, err
	}
	var config []interface{}
	if found, err := exists(filepath.Join(root, gitleaksConfig)); err != nil {
		return nil, err
	} else if found {
		config = []interface{}{"--config", filepath.Join(root, gitleaksConfig)}
	}
	var findings []string
	for i, a := range artifacts {
		dir := filepath.Join(scratch, fmt.Sprintf("%d", i))
		if err := unpackArtifact(e, a, dir); err != nil {
			return nil, err
		}
		argv := append([]interface{}{"gitleaks", "dir", dir}, config...)
		c, err := e.Run(argv, strictcli.Check(false))
		if err != nil {
			return nil, fmt.Errorf("gitleaks could not run on %s: %w", filepath.Base(a), err)
		}
		switch c.ExitCode() {
		case 0:
		case 1:
			findings = append(findings, "  "+filepath.Base(a)+":\n"+indent(c.Stdout()+"\n"+c.Stderr()))
		default:
			return nil, fmt.Errorf("gitleaks failed on %s (exit %d): %s", filepath.Base(a), c.ExitCode(), strings.TrimSpace(c.Stderr()))
		}
	}
	if _, err := e.Remove(scratch); err != nil {
		return nil, err
	}
	if len(findings) > 0 {
		return nil, &SecretScanError{Message: fmt.Sprintf("gitleaks found secrets in %d of the built artifacts, so nothing was pushed or published:\n%s\nRemove each secret from what the build packs, or, for a false positive, allowlist it by rule id or regex in %s at the project root (paths inside an artifact differ from source paths); see https://github.com/gitleaks/gitleaks#configuration. Then run the release again", len(findings), strings.Join(findings, "\n"), gitleaksConfig)}
	}
	return artifacts, nil
}

// indent indents every non-empty line of text by four spaces.
func indent(text string) string {
	var lines []string
	for _, l := range strings.Split(strings.TrimSpace(text), "\n") {
		if strings.TrimSpace(l) != "" {
			lines = append(lines, "    "+l)
		}
	}
	return strings.Join(lines, "\n")
}

// unpackArtifact unpacks a wheel or zip (zip) or a .tar.gz or .tgz (gzip'd
// tar) into dir through the effects handle. An entry naming a path outside
// dir is refused.
func unpackArtifact(e *strictcli.Effects, artifact, dir string) error {
	if _, err := e.Mkdir(dir); err != nil {
		return err
	}
	switch {
	case strings.HasSuffix(artifact, ".whl"), strings.HasSuffix(artifact, ".zip"):
		zr, err := zip.OpenReader(artifact)
		if errors.Is(err, zip.ErrInsecurePath) {
			if zr != nil {
				zr.Close()
			}
			return fmt.Errorf("%s carries an entry naming a path outside the archive; the secret scan refuses to unpack it", artifact)
		}
		if err != nil {
			return fmt.Errorf("%s is not a readable zip archive: %w", artifact, err)
		}
		defer zr.Close()
		for _, f := range zr.File {
			if f.FileInfo().IsDir() {
				continue
			}
			rc, err := f.Open()
			if err != nil {
				return fmt.Errorf("reading %s in %s: %w", f.Name, artifact, err)
			}
			data, err := io.ReadAll(rc)
			rc.Close()
			if err != nil {
				return fmt.Errorf("reading %s in %s: %w", f.Name, artifact, err)
			}
			if err := writeEntry(e, dir, artifact, f.Name, data); err != nil {
				return err
			}
		}
		return nil
	case strings.HasSuffix(artifact, ".tar.gz"), strings.HasSuffix(artifact, ".tgz"):
		file, err := os.Open(artifact)
		if err != nil {
			return err
		}
		defer file.Close()
		gz, err := gzip.NewReader(file)
		if err != nil {
			return fmt.Errorf("%s is not a readable gzip archive: %w", artifact, err)
		}
		tr := tar.NewReader(gz)
		for {
			h, err := tr.Next()
			if errors.Is(err, io.EOF) {
				return nil
			}
			if err != nil {
				return fmt.Errorf("%s is not a readable tar archive: %w", artifact, err)
			}
			if h.Typeflag != tar.TypeReg {
				continue
			}
			data, err := io.ReadAll(tr)
			if err != nil {
				return fmt.Errorf("reading %s in %s: %w", h.Name, artifact, err)
			}
			if err := writeEntry(e, dir, artifact, h.Name, data); err != nil {
				return err
			}
		}
	}
	return fmt.Errorf("%s is no archive type the secret scan unpacks (%s)", artifact, strings.Join(artifactPatterns, ", "))
}

// writeEntry writes one archive entry under dir.
func writeEntry(e *strictcli.Effects, dir, artifact, name string, data []byte) error {
	clean := path.Clean(strings.TrimPrefix(name, "./"))
	if path.IsAbs(clean) || clean == ".." || strings.HasPrefix(clean, "../") {
		return fmt.Errorf("%s carries the entry %q, which names a path outside the archive; the secret scan refuses to unpack it", artifact, name)
	}
	target := filepath.Join(dir, filepath.FromSlash(clean))
	if _, err := e.Mkdir(filepath.Dir(target)); err != nil {
		return err
	}
	_, err := e.Write(target, data)
	return err
}

// secretScanDir is the scan's scratch directory for the repository.
func secretScanDir(repo git.Repo) (string, error) {
	common, err := repo.CommonDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(resolvePath(common), filepath.FromSlash(SecretScanRelative)), nil
}
