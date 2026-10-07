package cli

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/runstate"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// releaseCommandLines are the release command lines a text may name; each
// takes a required --watch or --no-watch, so a text naming one without it
// prints a command that is refused as printed.
var releaseCommandLines = []string{"rlsbl release resume", "rlsbl release run", "rlsbl monorepo release run"}

// releaseLinesWithoutWatch names every string literal in a non-test file
// that names a release command line not followed by --watch or --no-watch.
func releaseLinesWithoutWatch(t *testing.T, root string, files []string) []string {
	t.Helper()
	fset := token.NewFileSet()
	var offenders []string
	for _, rel := range files {
		if strings.HasSuffix(rel, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(root, filepath.FromSlash(rel)), nil, parser.SkipObjectResolution)
		if err != nil {
			t.Fatalf("parsing %s: %v", rel, err)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			lit, ok := n.(*ast.BasicLit)
			if !ok || !isStringLiteral(lit) {
				return true
			}
			text, err := strconv.Unquote(lit.Value)
			if err != nil {
				return true
			}
			for _, line := range releaseCommandLines {
				for rest := text; ; {
					i := strings.Index(rest, line)
					if i < 0 {
						break
					}
					rest = rest[i+len(line):]
					if !strings.HasPrefix(rest, " --watch") && !strings.HasPrefix(rest, " --no-watch") {
						offenders = append(offenders, fmt.Sprintf("%s:%d: %q", rel, fset.Position(lit.Pos()).Line, line))
					}
				}
			}
			return true
		})
	}
	return offenders
}

func TestEveryTextNamingAReleaseCommandSpellsTheWatchChoice(t *testing.T) {
	hygiene.Isolate(t)
	root := testsupport.ModuleRoot(t)
	if offenders := releaseLinesWithoutWatch(t, root, testsupport.GoFiles(t, root)); len(offenders) > 0 {
		t.Fatalf("release run, release resume, and monorepo release run require --watch or --no-watch; name them through runstate.RunInvocation, runstate.ResumeInvocation, and runstate.BatchRunInvocation:\n  %s", strings.Join(offenders, "\n  "))
	}
}

func TestTheWatchChoiceGuardFindsALineWithoutIt(t *testing.T) {
	hygiene.Isolate(t)
	root := t.TempDir()
	testsupport.WriteFile(t, filepath.Join(root, "internal/release/messages.go"), "package release\n\n"+
		"const a = \"run `rlsbl release resume`\"\n"+
		"const b = \"run `rlsbl release resume --watch`\"\n"+
		"const c = \"run `rlsbl release run --no-watch` or `rlsbl release run`\"\n"+
		"const d = \"run `rlsbl monorepo release run` or `rlsbl monorepo release run --watch`\"\n")
	got := releaseLinesWithoutWatch(t, root, []string{"internal/release/messages.go"})
	want := `internal/release/messages.go:3: "rlsbl release resume"|internal/release/messages.go:5: "rlsbl release run"|internal/release/messages.go:6: "rlsbl monorepo release run"`
	if strings.Join(got, "|") != want {
		t.Fatalf("offenders = %q", got)
	}
}

// The command lines the texts print are accepted as printed: each run here
// reaches the release's own refusal (nothing to release or resume in this
// project), not a refusal of its arguments. Without the watch choice the
// same line is refused for its arguments.
func TestTheReleaseCommandLinesTheTextsPrintAreAccepted(t *testing.T) {
	hygiene.Isolate(t)
	releaseCommandsProject(t)
	app := appWith(t, testsupport.NewFakeHTTP(t))
	for line, refusal := range map[string]string{
		runstate.ResumeInvocation: "no release of portal is in progress",
		runstate.RunInvocation:    "unreleased.toml",
		// A standalone project has no batch release.
		runstate.BatchRunInvocation: "works on a workspace",
	} {
		args, ok := strings.CutPrefix(line, "rlsbl ")
		if !ok {
			t.Fatalf("%q does not start with rlsbl", line)
		}
		r := app.Test(append(strings.Fields(args), "--dry-run"))
		if r.ExitCode != 1 || !strings.Contains(r.Stderr, refusal) {
			t.Errorf("%s: exit %d, want the release's own refusal naming %q:\n%s%s", line, r.ExitCode, refusal, r.Stdout, r.Stderr)
		}
		bare := strings.Fields(strings.TrimSuffix(args, " --watch"))
		if r := app.Test(append(bare, "--dry-run")); r.ExitCode == 0 || !strings.Contains(r.Stderr, "watch") {
			t.Errorf("%v without the watch choice was not refused for it: exit %d\n%s", bare, r.ExitCode, r.Stderr)
		}
	}
}
