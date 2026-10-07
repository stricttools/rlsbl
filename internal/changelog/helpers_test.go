package changelog_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/releaserecord"
	"github.com/stricttools/rlsbl/internal/semver"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// standalone declares a standalone repository releasing portal.
const standalone = `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
releasable = "portal"
`

// twoReleasables declares a workspace with a dev-node root and two
// releasables, widget and gadget, one member each.
const twoReleasables = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "widget"
tag_format = "widget/v{version}"
publish_mode = "none"

[[releasables]]
name = "gadget"
tag_format = "gadget/v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "widget"
name = "widget"
releasable = "widget"

[[members]]
path = "gadget"
name = "gadget"
releasable = "gadget"
`

func parseDeclarations(t *testing.T, text string) *declarations.Releasables {
	t.Helper()
	d, err := declarations.Parse([]byte(text))
	if err != nil {
		t.Fatalf("the fixture declarations are refused: %v", err)
	}
	return d
}

func newWorkspace(t *testing.T, root, text string) *workspace.Workspace {
	t.Helper()
	w, err := workspace.New(root, parseDeclarations(t, text))
	if err != nil {
		t.Fatal(err)
	}
	return w
}

func version(t *testing.T, s string) semver.Version {
	t.Helper()
	v, err := semver.Parse(s)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

// id is a valid entry id ending in n, for fixtures that name entries.
func id(n string) string {
	return strings.Repeat("0", 48-len(n)) + n
}

// line is the serialized line of an entry.
func line(e changelog.Entry) string { return changelog.Serialize(e) }

// feature is a user-facing feature entry.
func feature(n, description string, commits ...string) changelog.Entry {
	return changelog.Entry{ID: id(n), Commits: commits, UserFacing: true, Description: description, Type: changelog.TypeFeature}
}

// internalEntry is an entry that is not user-facing.
func internalEntry(n string, commits ...string) changelog.Entry {
	return changelog.Entry{ID: id(n), Commits: commits}
}

// writeLines writes a changelog file of the repository at root, outside any
// effects handle: the world a test starts from.
func writeLines(t *testing.T, root, rel string, entries ...changelog.Entry) {
	t.Helper()
	var text strings.Builder
	for _, e := range entries {
		text.WriteString(line(e) + "\n")
	}
	testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(rel)), text.String())
}

// writeReleased writes a released version's file, read-only.
func writeReleased(t *testing.T, root, releasable, v string, entries ...changelog.Entry) string {
	t.Helper()
	rel := changelog.Dir(releasable) + "/" + v + ".jsonl"
	writeLines(t, root, rel, entries...)
	if err := os.Chmod(filepath.Join(root, filepath.FromSlash(rel)), 0o444); err != nil {
		t.Fatal(err)
	}
	return rel
}

// writeArchive writes a release archive of releasable directly. fate is
// "recorded" (with commit) or "never-released".
func writeArchive(t *testing.T, root, releasable, v, fate, commit, description string) {
	t.Helper()
	body := "format_version = 2\nbump = \"minor\"\ninclude = []\nexclude = []\ndescription = \"" + description + "\"\n"
	switch fate {
	case "recorded":
		body += "release_commit = \"" + commit + "\"\n\n[released_trees]\n\".\" = \"" + strings.Repeat("e", 40) + "\"\n"
	case "never-released":
		body += "never_released = true\n"
	default:
		t.Fatalf("unknown fate %q", fate)
	}
	rel := releaserecord.ArchivePath(releaserecord.ArchiveDir(releasable), version(t, v))
	testsupport.WriteFile(t, filepath.Join(root, filepath.FromSlash(rel)), body)
}

// reading runs fn in a read_only command with rlsbl's observe allowlist, so
// every git read is an allowlisted observe.
func reading(t *testing.T, dir string, fn func(repo git.Repo) error) error {
	t.Helper()
	var ferr error
	testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		repo, err := git.Open(ctx.Effects(), dir)
		if err != nil {
			return err
		}
		ferr = fn(repo)
		return nil
	})
	return ferr
}

// writing runs fn in a mutating command and returns the dispatch's result
// and fn's error.
func writing(t *testing.T, dir string, dryRun bool, fn func(e *strictcli.Effects, repo git.Repo) error) (strictcli.Result, error) {
	t.Helper()
	var ferr error
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		repo, err := git.Open(ctx.Effects(), dir)
		if err != nil {
			return err
		}
		ferr = fn(ctx.Effects(), repo)
		return nil
	})
	return res, ferr
}

// subject is the changelog of releasable in the repository at dir.
func subject(t *testing.T, repo git.Repo, w *workspace.Workspace, releasable string) changelog.Subject {
	t.Helper()
	s, err := changelog.NewSubject(repo, w, releasable, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func mustNotFail(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func readText(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func requireContains(t *testing.T, text string, wants ...string) {
	t.Helper()
	for _, want := range wants {
		if !strings.Contains(text, want) {
			t.Errorf("missing %q in:\n%s", want, text)
		}
	}
}

func mode(t *testing.T, path string) os.FileMode {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info.Mode().Perm()
}
