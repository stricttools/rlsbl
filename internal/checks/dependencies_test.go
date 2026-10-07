package checks

import (
	"fmt"
	"path/filepath"
	"testing"

	"github.com/stricttools/strictspec/go/strictspec"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/testsupport"
)

// pypiPortal is portal as a pypi project depending on strictcli, locked
// at 0.36.0; the dependency's declaration is filled in.
func pypiPortal(t *testing.T, declared string, floors bool) *testsupport.Repo {
	t.Helper()
	declarations := fmt.Sprintf(standalonePortal, "none")
	if floors {
		declarations += "internal_dep_floors = [\"strictcli\"]\n"
	}
	return newRepo(t, declarations, map[string]string{
		"pyproject.toml": fmt.Sprintf("[project]\nname = \"portal\"\nversion = \"1.0.0\"\ndependencies = [%q]\n", declared),
		"uv.lock":        "version = 1\n\n[[package]]\nname = \"portal\"\nversion = \"1.0.0\"\nsource = { editable = \".\" }\n\n[[package]]\nname = \"strictcli\"\nversion = \"0.36.0\"\nsource = { registry = \"https://pypi.org/simple\" }\n",
		recordFile:       publicRecord,
	})
}

func TestADependencyFloorBehindTheLockFailsUntilItIsRaised(t *testing.T) {
	hygiene.Isolate(t)
	r := pypiPortal(t, "strictcli", true)
	got := runCheck(t, inputs(t, r.Dir), "dep-floors")
	mustStatus(t, got, "fail")
	mustMention(t, got, `"strictcli>=0.36.0"`)
	// The fix the finding names: declare the floor at the locked version.
	r.Write("pyproject.toml", "[project]\nname = \"portal\"\nversion = \"1.0.0\"\ndependencies = [\"strictcli>=0.36.0\"]\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "dep-floors"), "pass")
}

func TestALockfileMissingARequirementFailsUntilItIsRegenerated(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", map[string]string{"go.mod": "module github.com/acme/portal\n\ngo 1.26\n\nrequire example.com/widget v1.0.0\n"})
	got := runCheck(t, inputs(t, r.Dir), "dep-locks")
	mustStatus(t, got, "fail")
	mustMention(t, got, "go.sum")
	// What `go mod tidy` writes.
	r.Write("go.sum", "example.com/widget v1.0.0 h1:x=\nexample.com/widget v1.0.0/go.mod h1:y=\n")
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "dep-locks"), "pass")
}

// installOverlay writes the dist-info of pkg into the environment env:
// editable at checkout when given, a registry wheel otherwise.
func installOverlay(t *testing.T, env, pkg, checkout string) {
	t.Helper()
	info := filepath.Join(env, "lib", "python3.14", "site-packages", pkg+"-0.36.0.dist-info")
	testsupport.WriteFile(t, filepath.Join(info, "METADATA"), "Metadata-Version: 2.4\nName: "+pkg+"\nVersion: 0.36.0\n")
	if checkout != "" {
		testsupport.WriteFile(t, filepath.Join(info, "direct_url.json"), `{"url": "file://`+checkout+`", "dir_info": {"editable": true}}`)
	}
}

func TestAWipedDevOverlayFailsUntilTheSyncRestoresIt(t *testing.T) {
	hygiene.Isolate(t)
	r := pypiPortal(t, "strictcli>=0.36.0", false)
	got := runCheck(t, inputs(t, r.Dir), "dev-overlay-drift")
	mustStatus(t, got, "skip")
	mustMention(t, got, "no dev overlay was ever synced")
	checkout, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	r.Write("dev-overlays-state.toml.local-only", "[[overlay]]\npackage = \"strictcli\"\npath = \""+checkout+"\"\n")
	got = runCheck(t, inputs(t, r.Dir), "dev-overlay-drift")
	mustStatus(t, got, "fail")
	mustMention(t, got, "not installed", "rlsbl dev sync")
	installOverlay(t, r.Path(".venv"), "strictcli", "")
	got = runCheck(t, inputs(t, r.Dir), "dev-overlay-drift")
	mustStatus(t, got, "fail")
	mustMention(t, got, "overlay wiped")
	// What `rlsbl dev sync` installs.
	installOverlay(t, r.Path(".venv"), "strictcli", checkout)
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "dev-overlay-drift"), "pass")
}

func TestTheOverlayIsLookedForWhereUVProjectEnvironmentPutsIt(t *testing.T) {
	hygiene.Isolate(t)
	r := pypiPortal(t, "strictcli>=0.36.0", false)
	checkout, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	r.Write("dev-overlays-state.toml.local-only", "[[overlay]]\npackage = \"strictcli\"\npath = \""+checkout+"\"\n")
	installOverlay(t, r.Path("envs/dev"), "strictcli", checkout)
	in := inputs(t, r.Dir)
	mustStatus(t, runCheck(t, in, "dev-overlay-drift"), "fail")
	in.UVProjectEnvironment = "envs/dev"
	mustStatus(t, runCheck(t, in, "dev-overlay-drift"), "pass")
}

func TestAnUnreadableOverlaySentinelIsAFinding(t *testing.T) {
	hygiene.Isolate(t)
	r := pypiPortal(t, "strictcli>=0.36.0", false)
	r.Write("dev-overlays-state.toml.local-only", "[[overlay]\n")
	got := runCheck(t, inputs(t, r.Dir), "dev-overlay-drift")
	mustStatus(t, got, "fail")
	mustMention(t, got, "rlsbl dev sync")
}

func TestAGeneratedValidatorOutsideTheRuntimesFormatsFailsUntilRegenerated(t *testing.T) {
	hygiene.Isolate(t)
	r := portalRepo(t, "none", nil)
	got := runCheck(t, inputs(t, r.Dir), "strictspec-generated-format")
	mustStatus(t, got, "skip")
	mustMention(t, got, "strictspec.toml")
	r.Write("strictspec.toml", "[[schemas]]\nschema = \"x.schema.toml\"\n\n[[schemas.targets]]\nlang = \"python\"\noutput = \"validator.py\"\n")
	r.Write("validator.py", fmt.Sprintf("GENERATED_CODE_FORMAT = %d\n", strictspec.MaxGeneratedCodeFormat+1))
	got = runCheck(t, inputs(t, r.Dir), "strictspec-generated-format")
	mustStatus(t, got, "fail")
	mustMention(t, got, "validator.py", "strictspec gen")
	// What `strictspec gen` writes.
	r.Write("validator.py", fmt.Sprintf("GENERATED_CODE_FORMAT = %d\n", strictspec.MaxGeneratedCodeFormat))
	mustStatus(t, runCheck(t, inputs(t, r.Dir), "strictspec-generated-format"), "pass")
}
