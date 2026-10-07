package release_test

import (
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/release"
)

func carried(list release.TargetList) []string {
	var names []string
	for _, row := range list.Targets {
		if row.Carried {
			names = append(names, row.Name)
		}
	}
	return names
}

func TestTargetsListsEverySupportedTargetAgainstTheMember(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "none")
	list, err := release.ListTargets(repo.Dir)
	mustNotFail(t, err)
	var names []string
	for _, row := range list.Targets {
		names = append(names, row.Name)
		if len(row.VersionFiles) == 0 {
			t.Errorf("%s names no version file", row.Name)
		}
	}
	if strings.Join(names, ",") != "go,npm,pypi" {
		t.Fatalf("targets %v", names)
	}
	if list.Member != "root" || list.Declared || !slices.Equal(carried(list), []string{"npm"}) {
		t.Fatalf("list %+v", list)
	}
}

func TestTargetsReportsTheMemberTheDirectorySelects(t *testing.T) {
	hygiene.Isolate(t)
	repo := workspaceRepo(t)
	repo.CommitFile("gadget/go.mod", "module github.com/acme/gadget\n\ngo 1.26\n", "gadget in go too")
	list, err := release.ListTargets(repo.Path("gadget"))
	mustNotFail(t, err)
	if list.Member != "gadget" || !slices.Equal(carried(list), []string{"go", "npm"}) {
		t.Fatalf("list %+v", list)
	}
	list, err = release.ListTargets(repo.Dir)
	mustNotFail(t, err)
	if list.Member != "root" || len(carried(list)) != 0 {
		t.Fatalf("the bare root: %+v", list)
	}
}

func TestTargetsReadsDeclaredTargets(t *testing.T) {
	hygiene.Isolate(t)
	repo := standalone(t, "none")
	decl := strings.Replace(standaloneDeclarations, "%s", "none", 1) + "targets = [{ name = \"pypi\", path = \"py\" }]\n"
	repo.Write(declarationsPath, decl)
	repo.Write("py/pyproject.toml", "[project]\nname = \"portal\"\nversion = \"0.4.0\"\n")
	repo.Commit("declare the python package", declarationsPath, "py/pyproject.toml")
	list, err := release.ListTargets(repo.Dir)
	mustNotFail(t, err)
	if !list.Declared || !slices.Equal(carried(list), []string{"pypi"}) {
		t.Fatalf("list %+v", list)
	}
}
