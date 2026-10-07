package dependencies_test

import (
	"fmt"
	"strings"
	"testing"

	"github.com/stricttools/strictspec/go/strictspec"
	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/rlsbl/internal/dependencies"
)

func generatedFormat(t *testing.T, dir string) dependencies.GeneratedFormatVerdict {
	t.Helper()
	v, err := dependencies.EvaluateGeneratedFormat(dir)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

// manifestDeclaring writes a strictspec.toml with one schema per output.
func manifestDeclaring(t *testing.T, dir string, outputs map[string]string) {
	t.Helper()
	text := ""
	for output, lang := range outputs {
		text += fmt.Sprintf("[[schemas]]\nschema = \"x.schema.toml\"\n\n[[schemas.targets]]\nlang = %q\noutput = %q\n\n", lang, output)
	}
	write(t, dir, "strictspec.toml", text)
}

func TestEachEmittersSpellingOfTheFormatIsRead(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	write(t, dir, "a.py", "# header\nGENERATED_CODE_FORMAT = 2\n")
	write(t, dir, "b.go", "// GENERATED_CODE_FORMAT is the shape\nconst GENERATED_CODE_FORMAT = 1\n")
	write(t, dir, "c.ts", "export const GENERATED_CODE_FORMAT = 2;\n")
	write(t, dir, "d.py", "# GENERATED_CODE_FORMAT = 9 is only a comment\n")
	for name, want := range map[string]int{"a.py": 2, "b.go": 1, "c.ts": 2} {
		got, found, err := dependencies.ReadGeneratedCodeFormat(dir + "/" + name)
		if err != nil || !found || got != want {
			t.Errorf("%s: %d, %v, %v", name, got, found, err)
		}
	}
	if _, found, _ := dependencies.ReadGeneratedCodeFormat(dir + "/d.py"); found {
		t.Error("a comment was read as the constant")
	}
}

func TestFormatsOutsideTheRuntimesRangeAreProblems(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	high := strictspec.MaxGeneratedCodeFormat + 1
	manifestDeclaring(t, dir, map[string]string{"ok.py": "python", "high.py": "python", "old.py": "python", "plain.ts": "typescript", "later.go": "go"})
	write(t, dir, "ok.py", fmt.Sprintf("GENERATED_CODE_FORMAT = %d\n", strictspec.MaxGeneratedCodeFormat))
	write(t, dir, "high.py", fmt.Sprintf("GENERATED_CODE_FORMAT = %d\n", high))
	write(t, dir, "old.py", "GENERATED_BY = \"0.1.0\"\n")
	write(t, dir, "plain.ts", "export const x = 1;\n")
	v := generatedFormat(t, dir)
	problems := joined(v.Problems)
	if len(v.Problems) != 2 || !strings.Contains(problems, fmt.Sprintf("high.py: declares generated-code format %d", high)) || !strings.Contains(problems, "old.py: no GENERATED_CODE_FORMAT") || !strings.Contains(problems, "regenerate with `strictspec gen`") {
		t.Fatalf("%+v", v)
	}
	notes := joined(v.Notes)
	for _, want := range []string{"1 generated validator(s) declare a format", "plain.ts: no GENERATED_CODE_FORMAT to read (lang typescript)", "later.go: declared but not generated yet"} {
		if !strings.Contains(notes, want) {
			t.Errorf("missing %q in\n%s", want, notes)
		}
	}
	// Regenerating clears the problems.
	write(t, dir, "high.py", "GENERATED_CODE_FORMAT = 1\n")
	write(t, dir, "old.py", "GENERATED_CODE_FORMAT = 1\n")
	if v := generatedFormat(t, dir); !v.OK() {
		t.Errorf("%+v", v)
	}
}

func TestNoGeneratedValidatorsIsASkip(t *testing.T) {
	hygiene.Isolate(t)
	dir := t.TempDir()
	if v := generatedFormat(t, dir); !strings.Contains(v.SkipReason, "generates no validators") {
		t.Errorf("%+v", v)
	}
	write(t, dir, "strictspec.toml", "")
	if v := generatedFormat(t, dir); !strings.Contains(v.SkipReason, "declares no generation target") {
		t.Errorf("%+v", v)
	}
	manifestDeclaring(t, dir, map[string]string{"later.py": "python"})
	if v := generatedFormat(t, dir); v.SkipReason != "" || !v.OK() {
		t.Errorf("only ungenerated outputs: %+v", v)
	}
}
