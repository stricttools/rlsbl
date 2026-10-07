package batchrelease_test

import (
	"reflect"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/batchrelease"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

func TestOrderPutsEachReleasableAfterTheOnesItsMembersDependOn(t *testing.T) {
	hygiene.Isolate(t)
	repo := workspaceRepo(t, nil)
	report, err := batchrelease.Order(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	want := batchrelease.OrderReport{Members: []string{"root", "widget", "gadget"}, Independent: false, Releasables: []string{"widget", "gadget"}}
	if !reflect.DeepEqual(report, want) {
		t.Fatalf("order: %+v", report)
	}
	requireContains(t, report.Render(), "Members, each after the members it depends on:\n  1. root\n  2. widget\n  3. gadget", "Releasables, in the order a batch release releases them:\n  1. widget\n  2. gadget")
}

func TestOrderOfIndependentMembersIsByName(t *testing.T) {
	hygiene.Isolate(t)
	repo := workspaceRepo(t, nil)
	repo.CommitFile(declarationsPath, strings.Replace(batchDeclarations, "depends_on = [\"widget\"]\n", "", 1), "gadget depends on nothing")
	report, err := batchrelease.Order(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	want := batchrelease.OrderReport{Members: []string{"gadget", "root", "widget"}, Independent: true, Releasables: []string{"gadget", "widget"}}
	if !reflect.DeepEqual(report, want) {
		t.Fatalf("order: %+v", report)
	}
	requireContains(t, report.Render(), "Every member is independent")
}

func TestOrderRefusesAManifestThatCannotBeReadUntilItIsFixed(t *testing.T) {
	hygiene.Isolate(t)
	repo := workspaceRepo(t, nil)
	repo.CommitFile("gadget/package.json", "{\n", "break the manifest")
	_, err := batchrelease.Order(repo.Dir)
	if err == nil || !strings.Contains(err.Error(), "gadget/package.json") || !strings.Contains(err.Error(), "fix each manifest") {
		t.Fatalf("an unreadable manifest was not refused: %v", err)
	}
	// The fix the refusal names: the manifest fixed.
	repo.CommitFile("gadget/package.json", packageJSON("gadget", "0.2.0"), "fix the manifest")
	if _, err := batchrelease.Order(repo.Dir); err != nil {
		t.Fatal(err)
	}
}

func TestOrderRefusesACycleUntilItIsBroken(t *testing.T) {
	hygiene.Isolate(t)
	repo := workspaceRepo(t, nil)
	cyclic := strings.Replace(batchDeclarations, "path = \"widget\"\nname = \"widget\"\nreleasable = \"widget\"\n", "path = \"widget\"\nname = \"widget\"\nreleasable = \"widget\"\ndepends_on = [\"gadget\"]\n", 1)
	repo.CommitFile(declarationsPath, cyclic, "widget depends on gadget too")
	_, err := batchrelease.Order(repo.Dir)
	if err == nil || !strings.Contains(err.Error(), "cycle through gadget, widget") {
		t.Fatalf("a cycle was not refused: %v", err)
	}
	// The fix: one of the two dependencies taken out.
	repo.CommitFile(declarationsPath, batchDeclarations, "break the cycle")
	if _, err := batchrelease.Order(repo.Dir); err != nil {
		t.Fatal(err)
	}
}

func TestOrderInAStandaloneProjectIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	repo := testsupport.NewRepo(t)
	repo.CommitFile(declarationsPath, "format_version = 1\nrepository_layout = \"standalone\"\nrelease_branches = [\"main\"]\n\n[[releasables]]\nname = \"portal\"\ntag_format = \"v{version}\"\npublish_mode = \"none\"\n\n[[members]]\npath = \".\"\nname = \"root\"\nreleasable = \"portal\"\n", "the project")
	if _, err := batchrelease.Order(repo.Dir); err == nil || !strings.Contains(err.Error(), "works on a workspace") {
		t.Fatalf("a standalone project's order was not refused: %v", err)
	}
}
