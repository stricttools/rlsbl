package devtools_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
	"github.com/stricttools/rlsbl/internal/workspace"
)

// fakeToolScript stands in for go, npm, and uv: it records each call as
// "<working directory>|<program and arguments>|<VIRTUAL_ENV>", answers `go
// list` with one main package at cmd/portal, and exits 3 when a file named
// <program>.fail sits beside it. `uv pip install -e <path>` also writes the
// dist-info of an editable install of the package named after the path's
// last element into $VIRTUAL_ENV, as uv would, so `rlsbl dev status` can be
// tested against what a sync left.
const fakeToolScript = `#!/bin/sh
dir="${0%/*}"
name="${0##*/}"
printf '%s|%s|%s\n' "$PWD" "$name $*" "$VIRTUAL_ENV" >> "$dir/calls.txt"
if [ -f "$dir/$name.fail" ]; then
	exit 3
fi
if [ "$name" = go ] && [ "$1" = list ]; then
	printf 'main\texample.com/portal/cmd/portal\t%s/cmd/portal\n' "$PWD"
	exit 0
fi
if [ "$name" = uv ] && [ "$1" = pip ] && [ "$2" = install ] && [ "$3" = -e ]; then
	pkg="${4##*/}"
	info="$VIRTUAL_ENV/lib/python3.12/site-packages/$pkg-1.0.0.dist-info"
	/bin/mkdir -p "$info"
	printf 'Metadata-Version: 2.1\nName: %s\nVersion: 1.0.0\n\n' "$pkg" > "$info/METADATA"
	printf '{"url": "file://%s", "dir_info": {"editable": true}}\n' "$4" > "$info/direct_url.json"
fi
exit 0
`

// tools are the fake programs of one test.
type tools struct {
	t   *testing.T
	dir string
}

// fakeTools makes PATH, for the rest of the test, one directory holding a
// fake of each named program and nothing else (the fakes reach the one
// program they need by its absolute path).
func fakeTools(t *testing.T, names ...string) *tools {
	t.Helper()
	dir := t.TempDir()
	for _, n := range names {
		if err := os.WriteFile(filepath.Join(dir, n), []byte(fakeToolScript), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", dir)
	return &tools{t: t, dir: dir}
}

// fail makes the named program exit 3 from now on.
func (f *tools) fail(name string) {
	f.t.Helper()
	if err := os.WriteFile(filepath.Join(f.dir, name+".fail"), nil, 0o644); err != nil {
		f.t.Fatal(err)
	}
}

// call is one recorded call.
type call struct {
	dir  string
	argv string
	venv string
}

// calls are the recorded calls, `go list` left out.
func (f *tools) calls() []call {
	f.t.Helper()
	data, err := os.ReadFile(filepath.Join(f.dir, "calls.txt"))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		f.t.Fatal(err)
	}
	var out []call
	for _, line := range strings.Split(strings.TrimRight(string(data), "\n"), "\n") {
		parts := strings.SplitN(line, "|", 3)
		if len(parts) != 3 {
			f.t.Fatalf("an unreadable call line %q", line)
		}
		if strings.HasPrefix(parts[1], "go list") {
			continue
		}
		out = append(out, call{dir: parts[0], argv: parts[1], venv: parts[2]})
	}
	return out
}

// argvs are the recorded calls' argv.
func (f *tools) argvs() []string {
	var out []string
	for _, c := range f.calls() {
		out = append(out, c.argv)
	}
	return out
}

// declare writes the repository's releasables.toml.
func declare(repo *testsupport.Repo, text string) {
	repo.Write(".strictmetadata/releasables/releasables.toml", text)
}

// standalone declares a standalone project, its one member's lines after
// the root member's (targets, pipelines).
func standalone(repo *testsupport.Repo, memberLines string) {
	declare(repo, "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\n\n"+
		"[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n"+
		"[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n"+memberLines)
}

// workspaceOf declares a workspace whose root member is a dev node and
// whose other members are widget (registry name widget-py) and gadget,
// each under a releasable of its own.
func workspaceOf(repo *testsupport.Repo) {
	declare(repo, `format_version = 1
repository_layout = "workspace"
release_branches = ["main"]

[[releasables]]
name = "widget"
tag_format = "widget@v{version}"
publish_mode = "none"

[[releasables]]
name = "gadget"
tag_format = "gadget@v{version}"
publish_mode = "none"

[[members]]
path = "."
name = "root"
dev_only = true
releasable = false

[[members]]
path = "packages/widget"
name = "widget"
releasable = "widget"
registry_name = "widget-py"

[[members]]
path = "packages/gadget"
name = "gadget"
releasable = "gadget"
`)
}

// run runs fn with the workspace model of the repository at root, as the
// handler of a mutating command.
func run(t *testing.T, root string, dryRun bool, fn func(ctx *strictcli.Context, w *workspace.Workspace) error) strictcli.Result {
	t.Helper()
	return testsupport.RunCommand(t, testsupport.CommandOptions{Effect: strictcli.EffectMutating, DryRun: dryRun, Allowlist: previewapply.Prefixes()}, func(ctx *strictcli.Context) error {
		w, err := workspace.Load(root)
		if err != nil {
			return err
		}
		return fn(ctx, w)
	})
}
