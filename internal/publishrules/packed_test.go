package publishrules_test

import (
	"os/exec"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/git"
	"github.com/stricttools/rlsbl/internal/publishrules"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// A wheel entry packed from another member's file, and one no file of the
// repository matches, are each named; rebuilding the wheel from the
// releasable's own files clears the refusal.
func TestAPackedFileFromOutsideTheMemberPathsIsNamed(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write("portal/pyproject.toml", "[project]\nname = \"portal\"\nversion = \"0.1.0\"\n")
	repo.Write("portal/src/portal/__init__.py", "VALUE = 1\n")
	repo.Write("widget/shared.py", "SHARED = 2\n")
	repo.Commit("files", "portal/pyproject.toml", "portal/src/portal/__init__.py", "widget/shared.py")
	w := newWorkspace(t, repo.Dir, portalDeclarations("ci", pypiPipeline))
	wheel := repo.Path("portal/dist/portal-0.1.0-py3-none-any.whl")
	writeWheel(t, wheel, map[string]string{
		"portal/__init__.py":              "VALUE = 1\n",
		"portal/shared.py":                "SHARED = 2\n",
		"portal/generated.py":             "GENERATED = 3\n",
		"portal-0.1.0.dist-info/METADATA": "Name: portal\n",
	})
	check := func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		artifacts, err := publishrules.PackedArtifacts(e, r, w, "portal")
		if err != nil {
			return err
		}
		return publishrules.CheckPackedContents(w.Declarations, "portal", artifacts)
	}
	run(t, strictcli.EffectMutating, func(e *strictcli.Effects) error {
		err := check(e)
		if err == nil {
			t.Fatal("a wheel carrying another member's file was not refused")
		}
		for _, want := range []string{
			"portal/shared.py, from widget/shared.py, which the member \"widget\" owns",
			"portal/generated.py, which matches no file of the repository",
			"the wheel portal-0.1.0-py3-none-any.whl",
		} {
			if !strings.Contains(err.Error(), want) {
				t.Errorf("the refusal does not say %q:\n%v", want, err)
			}
		}
		if strings.Contains(err.Error(), "__init__.py") || strings.Contains(err.Error(), "METADATA") {
			t.Errorf("the refusal names a file of the releasable's own:\n%v", err)
		}
		return nil
	})
	// The fix the refusal names: stop packing them.
	writeWheel(t, wheel, map[string]string{
		"portal/__init__.py":              "VALUE = 1\n",
		"portal-0.1.0.dist-info/METADATA": "Name: portal\n",
	})
	run(t, strictcli.EffectMutating, func(e *strictcli.Effects) error {
		if err := check(e); err != nil {
			t.Errorf("the rebuilt wheel is still refused: %v", err)
		}
		return nil
	})
}

// A releasable that publishes nothing packs nothing.
func TestNothingIsPackedUnderPublishModeNone(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("portal/pyproject.toml", "[project]\nname = \"portal\"\nversion = \"0.1.0\"\n", "files")
	w := newWorkspace(t, repo.Dir, portalDeclarations("none", pypiPipeline))
	run(t, strictcli.EffectMutating, func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		artifacts, err := publishrules.PackedArtifacts(e, r, w, "portal")
		if err != nil || len(artifacts) != 0 {
			t.Errorf("artifacts = %v, %v", artifacts, err)
		}
		return nil
	})
}

// A wheel the build step did not write is an error, not an empty listing.
func TestAMissingWheelIsAnError(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile("portal/pyproject.toml", "[project]\nname = \"portal\"\nversion = \"0.1.0\"\n", "files")
	if _, err := publishrules.ListWheels(repo.Dir, "portal", nil); err == nil || !strings.Contains(err.Error(), "portal/dist holds no wheel") {
		t.Errorf("err = %v", err)
	}
}

// A go binary compiled from another member's package names that package's
// files; dropping the import clears the refusal.
func TestAGoBinaryBuiltFromAnotherMembersPackageIsRefused(t *testing.T) {
	hygiene.Isolate(t, hygiene.Preserve(hygiene.GoCache))
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("no Go toolchain on PATH")
	}
	t.Setenv("GOPROXY", "off")
	repo := testsupport.NewRepo(t)
	repo.Write("portal/go.mod", "module example.com/portal\n\ngo 1.21\n\nrequire example.com/widget v0.0.0\n\nreplace example.com/widget => ../widget\n")
	repo.Write("portal/cmd/portal/main.go", "package main\n\nimport \"example.com/widget/lib\"\n\nfunc main() { lib.Run() }\n")
	repo.Write("portal/cmd/portal/banner.go", "package main\n\nimport _ \"embed\"\n\n//go:embed banner.txt\nvar banner string\n")
	repo.Write("portal/cmd/portal/banner.txt", "portal\n")
	repo.Write("widget/go.mod", "module example.com/widget\n\ngo 1.21\n")
	repo.Write("widget/lib/lib.go", "package lib\n\nfunc Run() {}\n")
	w := newWorkspace(t, repo.Dir, portalDeclarations("ci", goBinaryPipeline))
	check := func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		artifacts, err := publishrules.PackedArtifacts(e, r, w, "portal")
		if err != nil {
			return err
		}
		return publishrules.CheckPackedContents(w.Declarations, "portal", artifacts)
	}
	run(t, strictcli.EffectMutating, func(e *strictcli.Effects) error {
		err := check(e)
		if err == nil || !strings.Contains(err.Error(), "from widget/lib/lib.go, which the member \"widget\" owns") {
			t.Fatalf("err = %v", err)
		}
		for _, own := range []string{"portal/cmd/portal/main.go", "banner.txt"} {
			if strings.Contains(err.Error(), own) {
				t.Errorf("the refusal names %s, a file of the releasable's own:\n%v", own, err)
			}
		}
		return nil
	})
	// The fix the refusal names: stop importing it.
	testsupport.WriteFile(t, repo.Path("portal/cmd/portal/main.go"), "package main\n\nfunc main() {}\n")
	testsupport.WriteFile(t, repo.Path("portal/go.mod"), "module example.com/portal\n\ngo 1.21\n")
	run(t, strictcli.EffectMutating, func(e *strictcli.Effects) error {
		if err := check(e); err != nil {
			t.Errorf("the binary without the import is still refused: %v", err)
		}
		return nil
	})
}

// The embedded files of a go binary are listed with its sources.
func TestAGoBinaryListsItsEmbeddedFiles(t *testing.T) {
	hygiene.Isolate(t, hygiene.Preserve(hygiene.GoCache))
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("no Go toolchain on PATH")
	}
	t.Setenv("GOPROXY", "off")
	repo := testsupport.NewRepo(t)
	repo.Write("portal/go.mod", "module example.com/portal\n\ngo 1.21\n")
	repo.Write("portal/cmd/portal/main.go", "package main\n\nimport _ \"embed\"\n\n//go:embed banner.txt\nvar banner string\n\nfunc main() {}\n")
	repo.Write("portal/cmd/portal/banner.txt", "portal\n")
	run(t, strictcli.EffectMutating, func(e *strictcli.Effects) error {
		a, err := publishrules.ListGoBinary(e, repo.Dir, "portal", nil)
		if err != nil {
			return err
		}
		var sources []string
		for _, f := range a.Files {
			sources = append(sources, f.Source)
		}
		if strings.Join(sources, "|") != "portal/cmd/portal/banner.txt|portal/cmd/portal/main.go" {
			t.Errorf("sources = %q", sources)
		}
		return nil
	})
}

// An entry whose content several files hold (an empty __init__.py) is the
// releasable's own when one of them lies in its members, wherever the
// others sort: the wheel maps its package directory to another name, so no
// path matches the entry.
func TestAWheelEntryIdenticalToFilesOfSeveralMembersIsCreditedToTheReleasables(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.Write("portal/pyproject.toml", "[project]\nname = \"portal\"\nversion = \"0.1.0\"\n")
	repo.Write("portal/src/__init__.py", "")
	repo.Write("conftest.py", "")
	repo.Write("widget/tests/__init__.py", "")
	repo.Commit("files", "portal/pyproject.toml", "portal/src/__init__.py", "conftest.py", "widget/tests/__init__.py")
	w := newWorkspace(t, repo.Dir, portalDeclarations("ci", pypiPipeline))
	writeWheel(t, repo.Path("portal/dist/portal-0.1.0-py3-none-any.whl"), map[string]string{
		"renamed/__init__.py":             "",
		"portal-0.1.0.dist-info/METADATA": "Name: portal\n",
	})
	run(t, strictcli.EffectMutating, func(e *strictcli.Effects) error {
		r, err := git.Open(e, repo.Dir)
		if err != nil {
			return err
		}
		artifacts, err := publishrules.PackedArtifacts(e, r, w, "portal")
		if err != nil {
			return err
		}
		if err := publishrules.CheckPackedContents(w.Declarations, "portal", artifacts); err != nil {
			t.Errorf("an entry the releasable's own file holds was refused: %v", err)
		}
		return nil
	})
}
