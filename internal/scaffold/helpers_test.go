package scaffold

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/github"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}

// today is the date every scaffold in these tests runs on.
var today = time.Date(2026, 10, 7, 0, 0, 0, 0, time.UTC)

// The gh answers a scaffold of acme/portal asks for: the repository's
// topics, which already hold rlsbl (so the topic is never written), and its
// visibility, asked only where a publish workflow is rendered for a
// repository the record does not make confidential.
var (
	topicsAnswer = testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "repos/acme/portal/topics"}, Stdout: `{"names":["rlsbl"]}`}
	publicAnswer = testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "repos/acme/portal"}, Stdout: `{"full_name":"acme/portal","visibility":"public","private":false}`}
)

// The lifecycle-and-license records the fixtures use.
const (
	mitRecord = `format_version = 1

[[licenses]]
subject = "portal"
license = "MIT"
from = 2026-01-01
reason = "published client"
`
	proprietaryRecord = `format_version = 1

[[licenses]]
subject = "portal"
license = "proprietary"
from = 2026-01-01
reason = "the server's logic"
`
)

// standalone declares a standalone repository on GitHub as acme/portal: one
// releasable, portal, with the given publish mode, and the root member, to
// whose table member appends.
func standalone(publishMode, member string) string {
	return `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "` + publishMode + `"

[[members]]
path = "."
name = "root"
releasable = "portal"
` + member
}

// workspaceWithWidget declares a workspace on GitHub as acme/portal whose
// root member is a dev node and whose widget member, at widget/, is
// versioned under a releasable of its own that publishes nothing.
const workspaceWithWidget = `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]
github_repository = "acme/portal"

[[releasables]]
name = "widget"
tag_format = "widget/v{version}"
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
`

// npmPipeline is an npm package pipeline publishing from CI, for a member
// declaring the npm target.
const npmPipeline = `targets = [{ name = "npm" }]

[[members.pipelines]]
name = "npm"
type = "npm"
target = "npm"
local = false
artifact = "package"
`

// The files of the fixture projects.
var (
	goModule = map[string]string{
		"go.mod":  "module github.com/acme/portal\n\ngo 1.26\n",
		"main.go": "package main\n\nfunc main() {}\n",
	}
	npmPackage = map[string]string{
		"package.json":      "{\n  \"name\": \"portal\",\n  \"version\": \"0.1.0\",\n  \"engines\": {\"node\": \">=22\"}\n}\n",
		"package-lock.json": "{}\n",
	}
)

// newProject is a repository holding the declarations and files, with a
// fake gh answering answers besides the topics.
func newProject(t *testing.T, declarationsText string, files map[string]string, answers ...testsupport.GHAnswer) *testsupport.Repo {
	t.Helper()
	repo := testsupport.NewRepo(t)
	repo.Write(declarations.ReleasablesFile, declarationsText)
	for rel, content := range files {
		repo.Write(rel, content)
	}
	testsupport.FakeGH(t, append([]testsupport.GHAnswer{topicsAnswer}, answers...)...)
	return repo
}

// writeRecord writes the lifecycle-and-license record.
func writeRecord(repo *testsupport.Repo, text string) {
	repo.Write(lifecycle.RecordFile, text)
}

// scaffolded is what one scaffold run said and how it ended.
type scaffolded struct {
	said   []string
	result strictcli.Result
}

// text is everything the run printed, its report and its error alike.
func (s scaffolded) text() string {
	return strings.Join(s.said, "\n") + "\n" + s.result.Stdout + s.result.Stderr
}

// rowStatus is the status the report's file table gives path, or "" when
// the table has no row for it.
func (s scaffolded) rowStatus(path string) string {
	for _, line := range s.said {
		trimmed := strings.TrimSpace(line)
		if rest, ok := strings.CutPrefix(trimmed, path+" ."); ok {
			return strings.TrimSpace(strings.TrimLeft(rest, "."))
		}
	}
	return ""
}

// runScaffold scaffolds the member at dir on today, through a throwaway
// command; change adjusts the inputs (AutoCommit is off unless it sets it).
func runScaffold(t *testing.T, dir string, change func(*Inputs)) scaffolded {
	t.Helper()
	var said []string
	in := Inputs{Dir: dir, Version: "0.132.0", Now: today}
	if change != nil {
		change(&in)
	}
	r := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: in.DryRun, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		gh, err := github.New(ctx.Effects())
		if err != nil {
			return err
		}
		in.GitHub = gh
		in.Say = func(s string) { said = append(said, s) }
		return Run(ctx.Effects(), in)
	})
	return scaffolded{said: said, result: r}
}

// mustScaffold is runScaffold for a run that must succeed.
func mustScaffold(t *testing.T, dir string, change func(*Inputs)) scaffolded {
	t.Helper()
	s := runScaffold(t, dir, change)
	if s.result.ExitCode != 0 {
		t.Fatalf("scaffold exited %d:\n%s", s.result.ExitCode, s.text())
	}
	return s
}

// readFile is the content of rel in the repository, failing the test when
// it is missing.
func readFile(t *testing.T, repo *testsupport.Repo, rel string) string {
	t.Helper()
	data, err := os.ReadFile(repo.Path(rel))
	if err != nil {
		t.Fatalf("reading %s: %v", rel, err)
	}
	return string(data)
}

// exists reports whether rel exists in the repository.
func exists(t *testing.T, repo *testsupport.Repo, rel string) bool {
	t.Helper()
	_, err := os.Lstat(repo.Path(rel))
	return err == nil
}

// snapshot maps every file of the repository to its content, leaving out
// .git and the run state's directory, which holds the lock a run takes.
func snapshot(t *testing.T, root string) map[string]string {
	t.Helper()
	files := map[string]string{}
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if d.IsDir() {
			if rel == ".git" || rel == declarations.MetadataDir+"/.release-state" {
				return filepath.SkipDir
			}
			return nil
		}
		data, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		files[rel] = string(data)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return files
}

// sameFiles fails the test when two snapshots differ, naming the paths.
func sameFiles(t *testing.T, before, after map[string]string) {
	t.Helper()
	for p, content := range after {
		if old, ok := before[p]; !ok {
			t.Errorf("%s was created", p)
		} else if old != content {
			t.Errorf("%s was changed", p)
		}
	}
	for p := range before {
		if _, ok := after[p]; !ok {
			t.Errorf("%s was removed", p)
		}
	}
}
