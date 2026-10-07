package release_test

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/release"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func writeZip(t *testing.T, path string, files map[string]string) {
	t.Helper()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	for name, content := range files {
		f, err := w.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := f.Write([]byte(content)); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	testsupport.WriteFile(t, path, buf.String())
}

func writeTarGz(t *testing.T, path string, files map[string]string) {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for name, content := range files {
		if err := tw.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(content)), Typeflag: tar.TypeReg}); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write([]byte(content)); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	testsupport.WriteFile(t, path, buf.String())
}

// fakeGitleaks is a gitleaks that records each argv and finds a secret in
// any unpacked file holding SECRET, exiting as gitleaks does.
func fakeGitleaks(t *testing.T) string {
	t.Helper()
	calls := filepath.Join(t.TempDir(), "calls")
	fakeProgram(t, "gitleaks", `echo "$@" >> `+calls+`
if grep -rq SECRET "$2"; then echo "Finding: SECRET"; exit 1; fi
exit 0
`)
	return calls
}

func TestCleanArtifactsRemovesOnlyArtifactsOfEveryDistDirectory(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	for _, f := range []string{"dist/old.whl", "dist/old.tar.gz", "dist/notes.txt", "py/dist/old.tgz", "py/dist/old.zip", "elsewhere/dist/kept.whl"} {
		testsupport.WriteFile(t, filepath.Join(root, f), "x")
	}
	var removed []string
	_, err := mutating(t, false, func(e *strictcli.Effects) error {
		var err error
		removed, err = release.CleanArtifacts(e, root, []string{".", "py", "py"})
		return err
	})
	mustNotFail(t, err)
	if len(removed) != 4 {
		t.Errorf("removed: %v", removed)
	}
	for _, kept := range []string{"dist/notes.txt", "elsewhere/dist/kept.whl"} {
		if _, err := os.Stat(filepath.Join(root, kept)); err != nil {
			t.Errorf("%s was removed: %v", kept, err)
		}
	}
}

func scan(t *testing.T, root string) error {
	t.Helper()
	_, err := mutating(t, false, func(e *strictcli.Effects) error {
		_, err := release.ScanArtifacts(e, root, filepath.Join(t.TempDir(), "scratch"), []string{".", "py"})
		return err
	})
	return err
}

func TestAMissingGitleaksIsRefusedUntilInstalled(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	writeZip(t, filepath.Join(root, "dist", "portal-1.0.0-py3-none-any.whl"), map[string]string{"portal/__init__.py": "x = 1\n"})
	testsupport.PathOnly(t, "git", "sh", "grep")
	if err := scan(t, root); err == nil || !strings.Contains(err.Error(), "gitleaks is not on PATH") || !strings.Contains(err.Error(), "go install github.com/zricethezav/gitleaks") {
		t.Fatalf("a missing gitleaks was not refused naming the install: %v", err)
	}
	// The fix the refusal names: install gitleaks.
	fakeGitleaks(t)
	mustNotFail(t, scan(t, root))
}

func TestASecretInAnArtifactRefusesTheReleaseUntilRemoved(t *testing.T) {
	hygiene.Isolate(t)
	calls := fakeGitleaks(t)
	root := t.TempDir()
	wheel := filepath.Join(root, "dist", "portal-1.0.0-py3-none-any.whl")
	writeZip(t, wheel, map[string]string{"portal/__init__.py": "token = 'SECRET'\n"})
	writeTarGz(t, filepath.Join(root, "py", "dist", "portal-1.0.0.tar.gz"), map[string]string{"portal-1.0.0/README": "clean\n"})
	err := scan(t, root)
	var found *release.SecretScanError
	if !errors.As(err, &found) || !strings.Contains(err.Error(), "portal-1.0.0-py3-none-any.whl") || strings.Contains(err.Error(), "portal-1.0.0.tar.gz:") || !strings.Contains(err.Error(), ".gitleaks.toml") {
		t.Fatalf("the secret was not refused naming its artifact: %v", err)
	}
	// The fix the refusal names: the build no longer packs the secret.
	writeZip(t, wheel, map[string]string{"portal/__init__.py": "token = None\n"})
	mustNotFail(t, scan(t, root))
	if got := read(t, calls); strings.Count(got, "\n") != 4 || strings.Contains(got, "--config") {
		t.Errorf("gitleaks calls:\n%s", got)
	}
}

func TestTheProjectsGitleaksConfigIsHonored(t *testing.T) {
	hygiene.Isolate(t)
	calls := fakeGitleaks(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, ".gitleaks.toml"), "[allowlist]\n")
	writeZip(t, filepath.Join(root, "dist", "a.zip"), map[string]string{"a.txt": "a\n"})
	mustNotFail(t, scan(t, root))
	if got := read(t, calls); !strings.Contains(got, "--config "+filepath.Join(root, ".gitleaks.toml")) {
		t.Errorf("the config was not passed: %s", got)
	}
}

func TestAGitleaksThatFailsIsAnError(t *testing.T) {
	hygiene.Isolate(t)
	fakeProgram(t, "gitleaks", "echo broken >&2\nexit 2\n")
	root := t.TempDir()
	writeZip(t, filepath.Join(root, "dist", "a.zip"), map[string]string{"a.txt": "a\n"})
	if err := scan(t, root); err == nil || !strings.Contains(err.Error(), "exit 2") || !strings.Contains(err.Error(), "broken") {
		t.Fatalf("a failing gitleaks was not an error: %v", err)
	}
}

func TestAnArchiveEntryLeavingTheArchiveIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	fakeGitleaks(t)
	root := t.TempDir()
	writeZip(t, filepath.Join(root, "dist", "a.zip"), map[string]string{"../escape.txt": "x\n"})
	if err := scan(t, root); err == nil || !strings.Contains(err.Error(), "outside the archive") {
		t.Fatalf("an escaping entry was unpacked: %v", err)
	}
}
