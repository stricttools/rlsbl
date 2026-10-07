package cli

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/strictspec/go/lifecycle/index"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestReleaseInitWritesAndCommitsTheReleaseFile(t *testing.T) {
	hygiene.Isolate(t)
	repo := releaseCommandsProject(t)
	testsupport.FakeSafegit(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"release", "init"})
	if r.ExitCode != 0 || !strings.Contains(r.Stdout, "Wrote and committed .strictmetadata/releases/portal/unreleased.toml") {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if text := repo.Git("show", "HEAD:.strictmetadata/releases/portal/unreleased.toml"); !strings.Contains(text, `include = ["npm"]`) {
		t.Fatalf("the committed release file:\n%s", text)
	}
}

func TestAMutatingCommandUpsertsAConfidentialRepositorysIndexEntry(t *testing.T) {
	hygiene.Isolate(t)
	// Every license is proprietary, so the repository's directory name is
	// one of its names.
	requireIndexRefreshed(t, "proprietary", []string{"portal", "repo"})
}

func TestAMutatingCommandRemovesAPublicRepositorysIndexEntry(t *testing.T) {
	hygiene.Isolate(t)
	requireIndexRefreshed(t, "MIT", nil)
}

// A repository without a record holds no releasable-name identity, which
// the index keys entries by, so it has no entry and the one portal keys is
// another repository's.
func TestAMutatingCommandInARepositoryWithoutARecordLeavesTheIndexAlone(t *testing.T) {
	hygiene.Isolate(t)
	requireIndexRefreshed(t, "", []string{"oldname"})
}

// requireIndexRefreshed runs a mutating command in a repository whose
// license is license (no record when it is empty) and whose index entry
// holds a stale name, and fails the test unless the entry holds want
// afterwards (none: removed).
func requireIndexRefreshed(t *testing.T, license string, want []string) {
	t.Helper()
	repo := releaseCommandsProject(t)
	if license != "" {
		repo.Write(lifecycle.RecordFile, "format_version = 1\n\n[[licenses]]\nsubject = \"portal\"\nlicense = \""+license+"\"\nfrom = 2026-01-01\nreason = \"the license\"\n\n[[identities]]\nsubject = \"portal\"\nfacet = \"releasable-name\"\nvalue = \"portal\"\nregistry = \"\"\ntag_patterns = [\"v*\"]\nfrom = 2026-01-01\nreason = \"its name\"\n")
		repo.Commit("the license", lifecycle.RecordFile)
	}
	repo.Git("remote", "add", "origin", "https://github.com/acme/portal.git")
	path, err := index.DefaultPath()
	if err != nil {
		t.Fatal(err)
	}
	idx, err := index.Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := idx.Upsert(diskWriter{}, []string{"portal"}, []string{"oldname"}); err != nil {
		t.Fatal(err)
	}
	testsupport.FakeSafegit(t)
	r := appWith(t, testsupport.NewFakeHTTP(t)).Test([]string{"release", "init"})
	if r.ExitCode != 0 {
		t.Fatalf("exit %d:\n%s%s", r.ExitCode, r.Stdout, r.Stderr)
	}
	if idx, err = index.Load(path); err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, e := range idx.Held([]string{"portal"}) {
		got = append(got, e.Names...)
	}
	if !slices.Equal(got, want) {
		t.Fatalf("the index entry of portal holds %q, want %q", got, want)
	}
}

// diskWriter performs the index's writes, for a fixture.
type diskWriter struct{}

func (diskWriter) WriteFile(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}
func (diskWriter) MkdirAll(path string) error { return os.MkdirAll(path, 0o755) }
