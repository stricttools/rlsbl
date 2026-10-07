package monorepo

import (
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"
	"github.com/stricttools/strictspec/go/lifecycle"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/declarations"
	"github.com/stricttools/rlsbl/internal/testsupport"
)

// requireCreatedRecord fails unless the record of repo gives the releasable
// name an open active lifecycle period, an open license period under
// license, and its open releasable-name identity owning glob.
func requireCreatedRecord(t *testing.T, repo *testsupport.Repo, name, license, glob string) *lifecycle.Record {
	t.Helper()
	rec, err := lifecycle.Load(repo.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if l, ok := rec.LifecycleOn(name, today); !ok || l.Status != lifecycle.StatusActive || !l.Open() {
		t.Errorf("%s has no open active lifecycle period: %+v", name, rec.Lifecycle())
	}
	if l, ok := rec.LicenseOn(name, today); !ok || l.License != license || !l.Open() {
		t.Errorf("%s is not licensed %s: %+v", name, license, rec.Licenses())
	}
	found := false
	for _, id := range rec.Identities() {
		if id.Subject == name && id.Facet == lifecycle.FacetReleasableName && id.Open() && !id.Pending() {
			found = id.Value == name && strings.Join(id.TagPatterns, ",") == glob
		}
	}
	if !found {
		t.Errorf("%s has no open releasable-name identity owning %s: %+v", name, glob, rec.Identities())
	}
	return rec
}

func TestAddRecordsTheReleasableItCreatesAndARenameOfItSucceeds(t *testing.T) {
	hygiene.Isolate(t)
	repo := addFixture(t)
	mustRun(t, strictcli.EffectMutating, false, adding(gadgetRequest(repo)))
	requireCreatedRecord(t, repo, "gadget", "MIT", "gadget@v*")
	files := repo.Git("show", "--name-only", "--format=", "HEAD")
	for _, want := range []string{lifecycle.RecordFile, lifecycle.ManifestFile, "apps/gadget/LICENSE"} {
		if !strings.Contains(files, want) {
			t.Errorf("the add's commit lacks %s:\n%s", want, files)
		}
	}
	repo.AddBareRemote("origin")
	mustRun(t, strictcli.EffectMutating, false, renaming(repo, "gadget", "gizmo", false, nil, nil))
	requireCreatedRecord(t, repo, "gizmo", "MIT", "gizmo@v*")
}

func TestAddRequiresALicenseOnlyForTheReleasableItCreates(t *testing.T) {
	hygiene.Isolate(t)
	repo := addFixture(t)
	req := gadgetRequest(repo)
	req.License = ""
	mustFail(t, strictcli.EffectMutating, false, adding(req), "pass --license <SPDX identifier or proprietary>")
	req.License = "two words"
	mustFail(t, strictcli.EffectMutating, false, adding(req), `--license "two words" is not a license`)

	joining := gadgetRequest(repo)
	joining.Releasable, joining.TagFormat, joining.PublishMode = "widget", "", ""
	mustFail(t, strictcli.EffectMutating, false, adding(joining), `it creates none (the member joins the declared releasable "widget")`, "drop --license")
	none := gadgetRequest(repo)
	none.Releasable, none.TagFormat, none.PublishMode = NoReleasable, "", ""
	mustFail(t, strictcli.EffectMutating, false, adding(none), "--releasable false versions the member under none", "drop --license")
	if exists(repo, lifecycle.RecordFile) {
		t.Fatal("a refused add wrote the record")
	}
	// The fix each refusal names.
	joining.License = ""
	mustRun(t, strictcli.EffectMutating, false, adding(joining))
	if exists(repo, lifecycle.RecordFile) {
		t.Error("an add creating no releasable wrote the record")
	}
}

// visibility is gh's answer that acme/portal is public or private.
func visibility(public bool) testsupport.GHAnswer {
	v := "private"
	if public {
		v = "public"
	}
	return testsupport.GHAnswer{Args: []string{"api", "--method", "GET", "repos/acme/portal"}, Stdout: `{"full_name":"acme/portal","visibility":"` + v + `","private":` + map[bool]string{true: "false", false: "true"}[public] + `,"archived":false,"permissions":{"push":true}}`}
}

func TestAProprietaryReleasableIsCreatedOnlyInAPrivateRepository(t *testing.T) {
	hygiene.Isolate(t)
	repo := addFixture(t)
	req := gadgetRequest(repo)
	req.License = lifecycle.ProprietaryLicense
	testsupport.FakeGH(t, topicsAnswer, visibility(true))
	mustFail(t, strictcli.EffectMutating, false, adding(req), "GitHub reports acme/portal public", "gh repo edit acme/portal --visibility private")
	if exists(repo, lifecycle.RecordFile) {
		t.Fatal("the refused add wrote the record")
	}
	// The owner makes the repository private.
	testsupport.FakeGH(t, topicsAnswer, visibility(false))
	mustRun(t, strictcli.EffectMutating, false, adding(req))
	requireCreatedRecord(t, repo, "gadget", lifecycle.ProprietaryLicense, "gadget@v*")
}

func TestInitRecordsTheRootReleasable(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := emptyRepository(t)
	r := &declarations.Releasable{Name: "portal", TagFormat: "v{version}", PublishMode: declarations.PublishNone}
	mustFail(t, strictcli.EffectMutating, false, initLicensed(repo, r, "", true), "pass --license")
	if exists(repo, declarations.ReleasablesFile) || exists(repo, lifecycle.RecordFile) {
		t.Fatal("the refused init wrote")
	}
	mustRun(t, strictcli.EffectMutating, false, initLicensed(repo, r, "Apache-2.0", true))
	requireCreatedRecord(t, repo, "portal", "Apache-2.0", "v*")
	if status := repo.Git("status", "--porcelain"); status != "" {
		t.Errorf("the init left changes uncommitted:\n%s", status)
	}
}

func TestInitOfADevNodeRefusesALicense(t *testing.T) {
	hygiene.Isolate(t)
	testsupport.FakeSafegit(t)
	repo := emptyRepository(t)
	mustFail(t, strictcli.EffectMutating, false, initLicensed(repo, nil, "MIT", true), "the root member is a dev node", "drop --license")
	mustRun(t, strictcli.EffectMutating, false, initLicensed(repo, nil, "", true))
	if exists(repo, lifecycle.RecordFile) {
		t.Error("a dev-node init wrote the record")
	}
}

// A repository holding a minimal record keeps it, with the root
// releasable's entries added, and a failed commit puts it back as it was.
func TestInitAddsToAMinimalRecordAndPutsItBackWhenTheCommitFails(t *testing.T) {
	hygiene.Isolate(t)
	repo := emptyRepository(t)
	minimal := "format_version = 1\n"
	repo.Write(lifecycle.RecordFile, minimal)
	repo.Write(lifecycle.ManifestFile, "owner = \"strictspec\"\n")
	repo.Commit("the minimal record", lifecycle.RecordFile, lifecycle.ManifestFile)
	r := &declarations.Releasable{Name: "portal", TagFormat: "v{version}", PublishMode: declarations.PublishNone}
	testsupport.PathOnly(t, "git")
	deletingSaferm(t)
	mustFail(t, strictcli.EffectMutating, false, initLicensed(repo, r, "MIT", true), "safegit is not on PATH", "the workspace is not initialized")
	if readText(t, repo, lifecycle.RecordFile) != minimal || exists(repo, declarations.ReleasablesFile) {
		t.Fatal("the failed init did not put the record back")
	}
	testsupport.FakeSafegit(t)
	mustRun(t, strictcli.EffectMutating, false, initLicensed(repo, r, "MIT", true))
	requireCreatedRecord(t, repo, "portal", "MIT", "v*")
}

func TestAbsorbRecordsTheReleasableItCreatesKeepingTheArrivingLicense(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", func(r *AbsorbRequest) { r.License = "Apache-2.0" }, `licenses "gizmo" MIT since 2026-01-01, and --license says Apache-2.0`, nil, nil)
	mustConvert(t, false, absorbing(repo, src, "packages/gizmo", nil))
	requireCreatedRecord(t, repo, "gizmo", "MIT", "gizmo@v*")
}

func TestAbsorbRequiresALicenseOnlyForTheReleasableItCreates(t *testing.T) {
	hygiene.Isolate(t)
	repo, src, _, _ := absorbFixture(t, absorbDecls, nil)
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", func(r *AbsorbRequest) { r.License = "" }, "pass --license", nil, nil)
	absorbRefusedThenCleared(t, repo, src, "packages/gizmo", func(r *AbsorbRequest) { r.Releasable, r.TagFormat = "widget", "" }, "drop --license", nil, func(r *AbsorbRequest) {
		r.Releasable, r.TagFormat, r.License = "widget", "", ""
	})
}
