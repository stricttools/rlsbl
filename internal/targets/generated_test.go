package targets

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestTheSupportMatrixIsFresh(t *testing.T) {
	hygiene.Isolate(t)
	fresh, err := RenderMatrix()
	if err != nil {
		t.Fatal(err)
	}
	if committed := read(t, "support-matrix.json"); committed != string(fresh) {
		t.Fatalf("%s does not match the targets' facts; run `%s` from the repository root and commit the result", MatrixPath, RegenerateCommand)
	}
}

func TestThePrivatePathsProgramIsFreshAndCarriesTheRulesText(t *testing.T) {
	hygiene.Isolate(t)
	fresh, err := RenderPrivatePathsProgram()
	if err != nil {
		t.Fatal(err)
	}
	committed := read(t, "privatepathscheck/main.go")
	if committed != string(fresh) {
		t.Fatalf("%s is stale; run `%s` from the repository root and commit the result", PrivatePathsProgramPath, RegenerateCommand)
	}
	_, _, rules, err := splitGoSource("privatepaths.go", read(t, "privatepaths.go"), "targets")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(committed, rules) {
		t.Fatal("the generated program does not carry the rules' text as privatepaths.go holds it")
	}
}

// Every fact of Facts after its name is an axis, in order, and every axis
// says something.
func TestEveryFactIsAnAxis(t *testing.T) {
	hygiene.Isolate(t)
	facts := reflect.TypeFor[Facts]()
	var keys []string
	for i := 1; i < facts.NumField(); i++ {
		keys = append(keys, facts.Field(i).Tag.Get("json"))
	}
	var names []string
	for _, a := range Axes {
		names = append(names, a.Name)
		if strings.TrimSpace(a.Doc) == "" {
			t.Errorf("the axis %s says nothing", a.Name)
		}
	}
	if strings.Join(keys, ",") != strings.Join(names, ",") {
		t.Fatalf("Facts fields %q and Axes %q differ", keys, names)
	}
}

// Every target answers every closed-vocabulary fact from its vocabulary.
func TestEveryTargetDeclaresItsFacts(t *testing.T) {
	hygiene.Isolate(t)
	for _, target := range All() {
		f := target.Facts()
		if f.Ecosystem == "" || len(f.DetectionFiles) == 0 || len(f.VersionFiles) == 0 || f.RegistryDisplayName == "" || f.BuiltinTestCommand == "" || f.TestSettings == nil {
			t.Errorf("%s leaves a fact undeclared: %+v", f.Name, f)
		}
		if f.ReleaseMaterializationPolicy != MaterializeAlways && f.ReleaseMaterializationPolicy != MaterializeUnlessIdentityChanged {
			t.Errorf("%s: release_materialization_policy %q", f.Name, f.ReleaseMaterializationPolicy)
		}
		if f.PackageRename != RenameManifestField && f.PackageRename != RenameGoModulePath {
			t.Errorf("%s: package_rename %q", f.Name, f.PackageRename)
		}
		switch f.ScratchTestExclusion {
		case ScratchGoNestedModule, ScratchPytestNorecursedirs, ScratchRunnerChosenByProject:
		default:
			t.Errorf("%s: scratch_test_exclusion %q", f.Name, f.ScratchTestExclusion)
		}
	}
	var m Matrix
	data, err := os.ReadFile("support-matrix.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &m); err != nil || len(m.Targets) != len(All()) {
		t.Fatalf("the committed matrix does not read back: %v", err)
	}
}
