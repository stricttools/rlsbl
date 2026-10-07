package checks

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/changelog"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestMain(m *testing.M) {
	if testsupport.IsFakeGH() {
		os.Exit(testsupport.FakeGHMain())
	}
	os.Exit(m.Run())
}

// today is the date every fixture judges the lifecycle-and-license record
// on.
var today = time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)

// standalonePortal declares a standalone repository releasing portal; the
// publish mode is filled in.
const standalonePortal = `format_version = 1
repository_layout = "standalone"
release_branches = ["main"]

[[releasables]]
name = "portal"
tag_format = "v{version}"
publish_mode = "%s"

[[members]]
path = "."
name = "root"
releasable = "portal"
`

// workspaceWidgetGadget declares a workspace with a dev-node root and two
// releasables, widget and gadget, one member each.
const workspaceWidgetGadget = `format_version = 1
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

// publicRecord is a lifecycle-and-license record licensing portal MIT.
const publicRecord = `format_version = 1

[[lifecycle]]
subject = "portal"
status = "active"
from = 2026-01-01
reason = "first release"

[[licenses]]
subject = "portal"
license = "MIT"
from = 2026-01-01
reason = "chosen by the owner"
`

// proprietaryRecord licenses portal proprietary, which makes the repository
// confidential.
const proprietaryRecord = `format_version = 1

[[lifecycle]]
subject = "portal"
status = "active"
from = 2026-01-01
reason = "first release"

[[licenses]]
subject = "portal"
license = "proprietary"
from = 2026-01-01
reason = "a server"
`

// recordFile is where a fixture writes the lifecycle-and-license record.
const recordFile = ".strictmetadata/lifecycle-and-license/lifecycle-and-license.toml"

// newRepo creates a repository on main holding the declarations and files,
// committed as one commit.
func newRepo(t *testing.T, declared string, files map[string]string) *testsupport.Repo {
	t.Helper()
	r := testsupport.NewRepo(t)
	r.Write(".strictmetadata/releasables/releasables.toml", declared)
	for rel, content := range files {
		r.Write(rel, content)
	}
	r.Git("add", "-A")
	r.Git("commit", "-q", "-m", "initial")
	return r
}

// portalRepo is a standalone go project, portal, at version 1.0.0, with the
// public record.
func portalRepo(t *testing.T, mode string, files map[string]string) *testsupport.Repo {
	t.Helper()
	all := map[string]string{
		"go.mod":   "module github.com/acme/portal\n\ngo 1.26\n",
		"VERSION":  "1.0.0\n",
		"LICENSE":  "MIT License\n",
		recordFile: publicRecord,
	}
	for k, v := range files {
		if v == "" {
			delete(all, k)
			continue
		}
		all[k] = v
	}
	return newRepo(t, fmt.Sprintf(standalonePortal, mode), all)
}

// inputs are the inputs of a check run standing in dir.
func inputs(t *testing.T, dir string) Inputs {
	t.Helper()
	return Inputs{
		Dir:       dir,
		Now:       today,
		Home:      t.TempDir(),
		IndexPath: filepath.Join(t.TempDir(), "confidential-names.toml"),
	}
}

// problem is one problem of an outcome.
type problem struct {
	Severity string `json:"severity"`
	Text     string `json:"text"`
}

// result is one check's outcome as strictcli renders it, or the reason it
// refused to answer.
type result struct {
	Status   string    `json:"status"`
	Message  string    `json:"message"`
	Problems []problem `json:"problems"`
	Notes    []string  `json:"notes"`
	// Unanswered is the reason the check refused to answer, empty when it
	// answered.
	Unanswered string
}

// texts are the problems' texts, joined, for assertions.
func (r result) texts() string {
	var parts []string
	for _, p := range r.Problems {
		parts = append(parts, p.Text)
	}
	return strings.Join(parts, "\n")
}

func (r result) String() string {
	if r.Unanswered != "" {
		return "unanswered: " + r.Unanswered
	}
	return fmt.Sprintf("%s: %s\n  problems: %s\n  notes: %v", r.Status, r.Message, r.texts(), r.Notes)
}

// runOptions are what a check run's dispatch is given besides its inputs.
type runOptions struct {
	http *http.Client
}

// runCheck runs the named check in a read-only dispatch with rlsbl's observe
// allowlist, as the check command would: its scope applied, its refusals
// caught.
func runCheck(t *testing.T, in Inputs, name string) result {
	t.Helper()
	return runCheckWith(t, in, name, runOptions{})
}

func runCheckWith(t *testing.T, in Inputs, name string, o runOptions) result {
	t.Helper()
	decls, err := declared()
	if err != nil {
		t.Fatal(err)
	}
	impl, ok := implementation(name)
	if !ok {
		t.Fatalf("no check named %s is implemented", name)
	}
	var out result
	res := testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes(), HTTPClient: o.http}, func(ctx *strictcli.Context) error {
		cc, err := NewContext(ctx.Effects(), in)
		if err != nil {
			return err
		}
		out = invokeCheck(t, impl, decls[name], cc)
		return nil
	})
	if res.ExitCode != 0 {
		t.Fatalf("the check run did not finish:\n%s%s", res.Stdout, res.Stderr)
	}
	return out
}

// implementation is the implemented check called name.
func implementation(name string) (check, bool) {
	for _, family := range families {
		for _, c := range family() {
			if c.name == name {
				return c, true
			}
		}
	}
	return check{}, false
}

// invokeCheck runs one check the way Register's wrapper does, with fresh
// reporters, and renders its outcome.
func invokeCheck(t *testing.T, c check, d declaration, cc strictcli.CheckContext) (out result) {
	t.Helper()
	defer func() {
		if p := recover(); p != nil {
			u, ok := p.(Unanswered)
			if !ok {
				panic(p)
			}
			out = result{Unanswered: u.Reason}
		}
	}()
	var outcome strictcli.CheckOutcome
	if c.errorRun != nil {
		r := &strictcli.ErrorReporter{}
		ctx, skip := prepare(cc, c, d)
		if skip != "" {
			outcome = r.Skipped(skip)
		} else {
			outcome = c.errorRun(ctx, r)
		}
	} else {
		r := &strictcli.WarnReporter{}
		ctx, skip := prepare(cc, c, d)
		if skip != "" {
			outcome = r.Skipped(skip)
		} else {
			outcome = c.warnRun(ctx, r)
		}
	}
	return render(t, c.name, outcome)
}

// render is an outcome as strictcli's JSON renders it.
func render(t *testing.T, name string, o strictcli.CheckOutcome) result {
	t.Helper()
	var rendered []result
	if err := json.Unmarshal([]byte(strictcli.FormatCheckResultsJSON([]strictcli.CheckRunResult{{Name: name, Outcome: o}})), &rendered); err != nil || len(rendered) != 1 {
		t.Fatalf("rendering the outcome of %s: %v", name, err)
	}
	return rendered[0]
}

// mustStatus fails the test unless the check ended with status.
func mustStatus(t *testing.T, got result, status string) {
	t.Helper()
	if got.Unanswered != "" || got.Status != status {
		t.Fatalf("want %s, got %s", status, got)
	}
}

// mustMention fails the test unless the outcome's text holds every part.
func mustMention(t *testing.T, got result, parts ...string) {
	t.Helper()
	text := got.Message + "\n" + got.texts() + "\n" + strings.Join(got.Notes, "\n") + "\n" + got.Unanswered
	for _, p := range parts {
		if !strings.Contains(text, p) {
			t.Errorf("the outcome does not mention %q:\n%s", p, got)
		}
	}
}

// entryID is a valid changelog entry id ending in n.
func entryID(n string) string { return strings.Repeat("0", 48-len(n)) + n }

// entryLine is the changelog line of a user-facing feature entry naming
// commits.
func entryLine(n string, commits ...string) string {
	return changelog.Serialize(changelog.Entry{ID: entryID(n), Commits: commits, UserFacing: true, Description: "change " + n, Type: changelog.TypeFeature}) + "\n"
}

// writeArchive writes a recorded release archive of releasable at commit.
func writeArchive(t *testing.T, r *testsupport.Repo, releasable, version, commit string, trees map[string]string) {
	t.Helper()
	body := "format_version = 2\nbump = \"minor\"\ninclude = []\nexclude = []\ndescription = \"a release\"\nrelease_commit = \"" + commit + "\"\n\n[released_trees]\n"
	for path, tree := range trees {
		body += fmt.Sprintf("%q = %q\n", path, tree)
	}
	r.Write(".strictmetadata/releases/"+releasable+"/v"+version+".toml", body)
}
