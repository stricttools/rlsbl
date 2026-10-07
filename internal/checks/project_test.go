package checks

import (
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

const pyproject = "[project]\nname = \"%s\"\nversion = \"%s\"\nlicense = \"%s\"\ndescription = \"%s\"\n"

const packageJSON = "{\n  \"name\": \"%s\",\n  \"version\": \"%s\",\n  \"license\": \"%s\",\n  \"description\": \"%s\"\n}\n"

func TestLockPassesWithNoReleaseInProgress(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "lock"), "pass")
}

func TestLockWarnsOfAnInterruptedReleaseUntilItIsFinishedOrAbandoned(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	r.Write(".strictmetadata/.release-state/portal/in-progress.toml", "format_version = 1\n")
	got := runCheck(t, inputs(t, r.Dir), "lock")
	mustStatus(t, got, "warn")
	mustMention(t, got, `"portal"`, "rlsbl release resume", "rlsbl release abandon")
	// Abandoning the release deletes its in-progress state.
	if err := os.Remove(r.Path(".strictmetadata/.release-state/portal/in-progress.toml")); err != nil {
		t.Fatal(err)
	}
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "lock"), "pass")
}

func TestVersionConsistencyAcrossTargetsAndSelfdoc(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		"package.json": fmt.Sprintf(packageJSON, "portal", "1.0.0", "MIT", "A portal"),
	})
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "version-consistency"), "pass")
	r.Write("package.json", fmt.Sprintf(packageJSON, "portal", "1.1.0", "MIT", "A portal"))
	got := runCheck(t, inputs(t, r.Dir), "version-consistency")
	mustStatus(t, got, "fail")
	mustMention(t, got, "go=1.0.0", "npm=1.1.0")
	r.Write("package.json", fmt.Sprintf(packageJSON, "portal", "1.0.0", "MIT", "A portal"))
	r.Write("selfdoc.json", `{"version": "0.9.0"}`)
	got = runCheck(t, inputs(t, r.Dir), "version-consistency")
	mustStatus(t, got, "fail")
	mustMention(t, got, "selfdoc.json=0.9.0")
}

func TestVersionConsistencyReportsAnUnreadableVersion(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"VERSION": "one\n"})
	got := runCheck(t, inputs(t, r.Dir), "version-consistency")
	mustStatus(t, got, "fail")
	mustMention(t, got, "go:")
}

// widgetPublishing is the workspace with widget publishing from CI.
var widgetPublishing = strings.Replace(workspaceWidgetGadget, "tag_format = \"widget/v{version}\"\npublish_mode = \"none\"", "tag_format = \"widget/v{version}\"\npublish_mode = \"ci\"", 1)

func TestAPublishingMembersManifestsFollowTheReleasableVersionFile(t *testing.T) {
	hygiene.Isolate(t)
	r := newRepo(t, widgetPublishing, map[string]string{
		"widget/go.mod":  "module github.com/acme/repo/widget\n\ngo 1.26\n",
		"widget/VERSION": "1.0.0\n",
		"gadget/go.mod":  "module github.com/acme/repo/gadget\n\ngo 1.26\n",
		"gadget/VERSION": "0.1.0\n",
		".strictmetadata/releases/widget/version": "1.1.0\n",
		".strictmetadata/releases/gadget/version": "1.0.0\n",
	})
	got := runCheck(t, inputs(t, r.Path("widget")), "version-consistency")
	mustStatus(t, got, "fail")
	mustMention(t, got, ".strictmetadata/releases/widget/version", "go=1.0.0")
	// A member publishing nothing carries no version a release writes.
	mustStatus(t, runCheck(t, inputs(t, r.Path("gadget")), "version-consistency"), "pass")
	// The fix: the manifest follows the version file.
	r.Write("widget/VERSION", "1.1.0\n")
	mustStatus(t, runCheck(t, inputs(t, r.Path("widget")), "version-consistency"), "pass")
}

func TestNameConsistencyComparesNormalizedNames(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		"go.mod":         "",
		"VERSION":        "",
		"package.json":   fmt.Sprintf(packageJSON, "portal", "1.0.0", "MIT", "A portal"),
		"pyproject.toml": fmt.Sprintf(pyproject, "Portal", "1.0.0", "MIT", "A portal"),
	})
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "name-consistency"), "pass")
	r.Write("pyproject.toml", fmt.Sprintf(pyproject, "gizmo", "1.0.0", "MIT", "A portal"))
	got := runCheck(t, inputs(t, r.Dir), "name-consistency")
	mustStatus(t, got, "warn")
	mustMention(t, got, "npm=portal", "pypi=gizmo")
}

func TestLicenseConsistencyHoldsTheManifestsToEachOtherAndToTheRecord(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		"package.json":   fmt.Sprintf(packageJSON, "portal", "1.0.0", "MIT", "A portal"),
		"pyproject.toml": fmt.Sprintf(pyproject, "portal", "1.0.0", "mit", "A portal"),
	})
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "license-consistency"), "pass")
	r.Write("pyproject.toml", fmt.Sprintf(pyproject, "portal", "1.0.0", "Apache-2.0", "A portal"))
	r.Write("package.json", fmt.Sprintf(packageJSON, "portal", "1.0.0", "Apache-2.0", "A portal"))
	got := runCheck(t, inputs(t, r.Dir), "license-consistency")
	mustStatus(t, got, "warn")
	mustMention(t, got, "the lifecycle-and-license record licenses the releasable \"portal\" MIT")
}

func TestLicenseConsistencyNamesTheMissingLicensePeriodUntilItIsDeclared(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		"package.json": fmt.Sprintf(packageJSON, "portal", "1.0.0", "MIT", "A portal"),
		recordFile:     "format_version = 1\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "license-consistency")
	mustStatus(t, got, "warn")
	mustMention(t, got, "rlsbl transition license --subject portal")
	// What `rlsbl transition license` writes: a license period.
	r.Write(recordFile, publicRecord)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "license-consistency"), "pass")
}

func TestAProprietaryRecordAcceptsNpmsUnlicensed(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		"package.json": fmt.Sprintf(packageJSON, "portal", "1.0.0", "UNLICENSED", "A portal"),
		recordFile:     proprietaryRecord,
	})
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "license-consistency"), "pass")
}

func TestDescriptionConsistency(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		"package.json":   fmt.Sprintf(packageJSON, "portal", "1.0.0", "MIT", "A portal"),
		"pyproject.toml": fmt.Sprintf(pyproject, "portal", "1.0.0", "MIT", "Another portal"),
	})
	got := runCheck(t, inputs(t, r.Dir), "description-consistency")
	mustStatus(t, got, "warn")
	mustMention(t, got, "description mismatch")
}

func TestDeclarationsValidRefusesAnAdoptionDeclarationWhoseOptionIsOff(t *testing.T) {
	hygiene.Isolate(t)
	declared := fmt.Sprintf(standalonePortal, "none") + "internal_dep_floors = [\"widget\"]\n"
	r := newRepo(t, declared, map[string]string{"VERSION": "1.0.0\n", "go.mod": "module github.com/acme/portal\n\ngo 1.26\n", recordFile: publicRecord})
	got := runCheck(t, inputs(t, r.Dir), "declarations-valid")
	mustStatus(t, got, "fail")
	mustMention(t, got, "internal_dep_floors", "rlsbl options set rlsbl:dep-floors")
	// The first fix the finding names: delete the declaration.
	r.Write(".strictmetadata/releasables/releasables.toml", fmt.Sprintf(standalonePortal, "none"))
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "declarations-valid"), "pass")
}

func TestDeclarationsValidRefusesADeployCommandOfAReleasableThatIsNotProprietary(t *testing.T) {
	hygiene.Isolate(t)
	declared := strings.Replace(fmt.Sprintf(standalonePortal, "none"), "publish_mode = \"none\"\n", "publish_mode = \"none\"\ndeploy_command = [\"tool\", \"deploy\", \"portal-server\", \"--version\", \"{version}\"]\n", 1)
	r := newRepo(t, declared, map[string]string{"VERSION": "1.0.0\n", "go.mod": "module github.com/acme/portal\n\ngo 1.26\n", recordFile: publicRecord})
	got := runCheck(t, inputs(t, r.Dir), "declarations-valid")
	mustStatus(t, got, "fail")
	mustMention(t, got, "deploy_command", "rlsbl transition classify --subject portal")
	// What classifying it writes: a proprietary license period.
	r.Write(recordFile, proprietaryRecord)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "declarations-valid"), "pass")
}

func TestLicenseFile(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"LICENSE": ""})
	got := runCheck(t, inputs(t, r.Dir), "license-file")
	mustStatus(t, got, "fail")
	mustMention(t, got, "rlsbl scaffold")
	r.Write("LICENSE", "Copyright {{author}}\n")
	got = runCheck(t, inputs(t, r.Dir), "license-file")
	mustStatus(t, got, "fail")
	mustMention(t, got, "{{author}}")
	// What scaffold writes: the license text, filled in.
	r.Write("LICENSE", "MIT License\nCopyright Acme\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "license-file"), "pass")
}

func TestAPrivatePackageJSONContradictsPublishingFromCI(t *testing.T) {
	hygiene.Isolate(t)
	manifest := "{\n  \"name\": \"portal\",\n  \"version\": \"1.0.0\",\n  \"private\": true\n}\n"
	r := portalRepo(t, "ci", map[string]string{"package.json": manifest})
	got := runCheck(t, inputs(t, r.Dir), "npm-private-mismatch")
	mustStatus(t, got, "fail")
	mustMention(t, got, `"private": true`, `publish_mode = "none"`)
	// One fix the finding names: delete "private".
	r.Write("package.json", fmt.Sprintf(packageJSON, "portal", "1.0.0", "MIT", "A portal"))
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "npm-private-mismatch"), "pass")
	// The other: publish nothing.
	r.Write("package.json", manifest)
	r.Write(".strictmetadata/releasables/releasables.toml", fmt.Sprintf(standalonePortal, "none"))
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "npm-private-mismatch"), "pass")
}

func TestTargetVersionReadable(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"VERSION": "v1.0\n"})
	got := runCheck(t, inputs(t, r.Dir), "target-version-readable")
	mustStatus(t, got, "fail")
	mustMention(t, got, "go:")
	r.Write("VERSION", "1.0.0\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "target-version-readable"), "pass")
}

func TestAVersionConstantNotNamedDunderVersionIsRefusedUntilRenamed(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{
		"go.mod":             "",
		"VERSION":            "",
		"pyproject.toml":     fmt.Sprintf(pyproject, "portal", "1.0.0", "MIT", "A portal"),
		"portal/__init__.py": "\"\"\"The portal.\"\"\"\n\nVERSION = \"1.0.0\"\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "dunder-version-missing")
	mustStatus(t, got, "fail")
	mustMention(t, got, "VERSION = \"1.0.0\"", "rename it to __version__")
	r.Write("portal/__init__.py", "\"\"\"The portal.\"\"\"\n\n__version__ = \"1.0.0\"\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "dunder-version-missing"), "pass")
}

func TestDunderVersionMissingSkipsWithoutAPypiTarget(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "dunder-version-missing"), "skip")
}

func TestDunderFindingLeavesAComputedOrReExportedVersionAlone(t *testing.T) {
	hygiene.Isolate(t)
	for _, text := range []string{
		"from importlib.metadata import version\n__version__ = version(\"portal\")\n",
		"from ._version import __version__\n",
		"api_version = \"v2\"\n",
		"    VERSION = \"1.0.0\"\n",
	} {
		if finding := dunderFinding(text, "portal/__init__.py"); finding != "" {
			t.Errorf("%q gave %q", text, finding)
		}
	}
}

func TestSelfdocVersionDrift(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"selfdoc.json": `{"version": "0.9.0"}`})
	got := runCheck(t, inputs(t, r.Dir), "selfdoc-version-drift")
	mustStatus(t, got, "fail")
	mustMention(t, got, "0.9.0", "set it to 1.0.0")
	r.Write("selfdoc.json", `{"version": "1.0.0"}`)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "selfdoc-version-drift"), "pass")
}

func TestAStashFailsUntilItIsDropped(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "stash-free"), "pass")
	r.Write("VERSION", "1.0.1\n")
	r.Git("stash")
	got := runCheck(t, inputs(t, r.Dir), "stash-free")
	mustStatus(t, got, "fail")
	mustMention(t, got, "git stash drop")
	r.Git("stash", "drop")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "stash-free"), "pass")
}

func TestAPathSourceOutsideTheRepositoryIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	base := fmt.Sprintf(pyproject, "portal", "1.0.0", "MIT", "A portal")
	r := portalRepo(t, "none", map[string]string{
		"pyproject.toml":         base + "\n[tool.uv.sources]\nwidget = { path = \"../../widget\" }\ngadget = { path = \"libs/gadget\" }\n",
		"libs/gadget/README.md":  "gadget\n",
		"portal/__init__.py":     "__version__ = \"1.0.0\"\n",
		"libs/gadget/.gitignore": "\n",
	})
	got := runCheck(t, inputs(t, r.Dir), "cross-repo-path-sources")
	mustStatus(t, got, "fail")
	mustMention(t, got, "widget", "dev-sources.toml.local-only")
	if strings.Contains(got.texts(), "gadget:") {
		t.Errorf("a path inside the repository was reported: %s", got)
	}
	// The fix: depend on the registry release instead.
	r.Write("pyproject.toml", base+"\n[tool.uv.sources]\ngadget = { path = \"libs/gadget\" }\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "cross-repo-path-sources"), "pass")
}

func TestAGoModulePathOriginDoesNotServeIsRefusedNamingTheRewrite(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"go.mod": "module github.com/acme/oldportal\n\ngo 1.26\n"})
	r.Git("remote", "add", "origin", "https://github.com/acme/portal.git")
	got := runCheck(t, inputs(t, r.Dir), "go-module-identity")
	mustStatus(t, got, "fail")
	mustMention(t, got, "rlsbl rewrite go-module-path --from-module github.com/acme/oldportal --to-module github.com/acme/portal")
	// What the rewrite does to go.mod.
	r.Write("go.mod", "module github.com/acme/portal\n\ngo 1.26\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "go-module-identity"), "pass")
}

func TestGoModuleIdentitySkipsWithoutAnOrigin(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "go-module-identity"), "skip")
}
