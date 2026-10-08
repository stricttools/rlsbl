package targets

import (
	"path/filepath"
	"slices"
	"strings"
	"testing"

	tomledit "github.com/stricttools/go-toml-edit"
	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/previewapply"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// sdistExclusions reads [tool.hatch.build.targets.sdist] exclude back.
func sdistExclusions(t *testing.T, data []byte) []string {
	t.Helper()
	doc, err := tomledit.Parse(data)
	if err != nil {
		t.Fatalf("the merged file does not parse: %v\n%s", err, data)
	}
	entries, found, err := stringArray(doc, sdistExcludePath+".exclude")
	if err != nil || !found {
		t.Fatalf("no exclude array: %v\n%s", err, data)
	}
	return entries
}

func TestTheSdistExclusionsAreMergedKeepingTheProjectsOwn(t *testing.T) {
	hygiene.Isolate(t)
	fresh := "[project]\nname = \"portal\" # the name\n"
	out, changed, err := MergeHatchSdistExclusions("pyproject.toml", []byte(fresh))
	if err != nil || !changed || !strings.HasPrefix(string(out), fresh+"\n[tool.hatch.build.targets.sdist]\nexclude = [\n    \"/todo/\",\n") {
		t.Fatalf("a fresh table: %v\n%s", err, out)
	}
	if got := sdistExclusions(t, out); !slices.Equal(got, ExcludeEntries()) {
		t.Fatalf("entries = %q", got)
	}
	if again, changed, err := MergeHatchSdistExclusions("pyproject.toml", out); err != nil || changed || string(again) != string(out) {
		t.Fatalf("a second merge changed the file: %v", err)
	}
	own := "[project]\nname = \"portal\"\n\n[tool.hatch.build.targets.sdist]\nexclude = [\"/build/\", \"/todo/\"]\n"
	out, changed, err = MergeHatchSdistExclusions("pyproject.toml", []byte(own))
	if err != nil || !changed {
		t.Fatalf("existing entries: %v", err)
	}
	got := sdistExclusions(t, out)
	if got[0] != "/build/" || len(got) != len(ExcludeEntries())+1 {
		t.Fatalf("entries = %q", got)
	}
	table := "[project]\nname = \"portal\"\n\n[tool.hatch.build.targets.sdist]\ninclude = [\"src\"]\n"
	if out, _, err := MergeHatchSdistExclusions("pyproject.toml", []byte(table)); err != nil || len(sdistExclusions(t, out)) != len(ExcludeEntries()) || !strings.Contains(string(out), "include = [\"src\"]") {
		t.Fatalf("an existing table without exclude: %v\n%s", err, out)
	}
}

func TestNestedMembersAreIgnoredByTheParentsPytest(t *testing.T) {
	hygiene.Isolate(t)
	base := t.TempDir()
	nested := NestedPathsInside(base, []string{filepath.Join(base, "python"), filepath.Join(base, "a", "b"), filepath.Dir(base), base})
	if !slices.Equal(nested, []string{"a/b", "python"}) {
		t.Fatalf("NestedPathsInside = %q", nested)
	}
	dir := project(t, map[string]string{"pyproject.toml": "[project]\nname = \"sdk\"\n\n[tool.pytest.ini_options]\naddopts = \"-q --ignore='a/b'\"\n"})
	file, missing, owed, err := MissingNestedExclusions(dir, nested)
	if err != nil || !owed || file != filepath.Join(dir, "pyproject.toml") || !slices.Equal(missing, []string{"--ignore=python"}) {
		t.Fatalf("missing = %q in %s, %v, %v", missing, file, owed, err)
	}
	// Performing the merge the check names clears it.
	path := filepath.Join(dir, "pyproject.toml")
	out, changed, err := MergePytestIgnores(path, []byte(read(t, path)), missing)
	if err != nil || !changed || !strings.Contains(string(out), "addopts = \"-q --ignore='a/b' --ignore=python\"") {
		t.Fatalf("merge: %v\n%s", err, out)
	}
	testsupport.WriteFile(t, path, string(out))
	if _, _, owed, err := MissingNestedExclusions(dir, nested); err != nil || owed {
		t.Fatalf("after the merge: owed %v, %v", owed, err)
	}
	array := "[tool.pytest.ini_options]\naddopts = [\"-q\"]\n"
	if out, _, err := MergePytestIgnores("pyproject.toml", []byte(array), []string{"--ignore=python"}); err != nil || !strings.Contains(string(out), "\"--ignore=python\"") {
		t.Fatalf("an array: %v\n%s", err, out)
	}
	if out, _, err := MergePytestIgnores("pyproject.toml", []byte("[project]\nname = \"sdk\"\n"), []string{"--ignore=python"}); err != nil || !strings.Contains(string(out), "[tool.pytest.ini_options]\naddopts = [\n    \"--ignore=python\",\n]\n") {
		t.Fatalf("no table: %v\n%s", err, out)
	}
	ini := project(t, map[string]string{"pytest.ini": "[pytest]\n", "pyproject.toml": "[project]\nname = \"sdk\"\n"})
	if file, missing, owed, err := MissingNestedExclusions(ini, []string{"python"}); err != nil || !owed || filepath.Base(file) != "pytest.ini" || len(missing) != 1 {
		t.Fatalf("a pytest.ini owes every option: %s %q %v %v", file, missing, owed, err)
	}
}

func TestShellSplitReadsAddoptsTheWayAShellDoes(t *testing.T) {
	hygiene.Isolate(t)
	got, err := shellSplit(`-q  --ignore='a b' "-k x\"y" c\ d`)
	if err != nil || !slices.Equal(got, []string{"-q", "--ignore=a b", `-k x"y`, "c d"}) {
		t.Fatalf("shellSplit = %q, %v", got, err)
	}
	if _, err := shellSplit(`-k "open`); err == nil {
		t.Fatal("an unclosed quote was accepted")
	}
}

func TestGoStubsGoInTrackedPrivateDirectories(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("widget/go.mod", "module example.com/widget\n", "add widget")
	repo.CommitFile("widget/todo/plan.md", "plan\n", "add plan")
	repo.CommitFile("widget/.claude/settings.json", "{}\n", "add settings")
	repo.Write("widget/stricttools/untracked.txt", "x\n")
	var stubs []string
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		stubs, err = GoStubDirectories(r, repo.Path("widget"))
		return err
	})
	if !slices.Equal(stubs, []string{"todo", ".claude"}) {
		t.Fatalf("stubs = %q", stubs)
	}
	repo.CommitFile("widget/.strictmetadata/.cli-schema/schema.json", "{}\n", "add the schema dump")
	testsupport.RunEffects(t, testsupport.CommandOptions{Effect: strictcli.EffectReadOnly, Allowlist: previewapply.Prefixes()}, func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		stubs, err = GoStubDirectories(r, repo.Path("widget"))
		return err
	})
	if !slices.Equal(stubs, []string{".strictmetadata", "todo", ".claude"}) {
		t.Fatalf("a module tracking files in its own .strictmetadata/: stubs = %q", stubs)
	}
}
