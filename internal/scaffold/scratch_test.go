package scaffold

import (
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

// A pyproject.toml without the setting gets pytest's default back with the
// scratch directories after it, since setting the option replaces the
// default.
func TestNorecursedirsIsAddedWithPytestsDefault(t *testing.T) {
	hygiene.Isolate(t)
	in := "[project]\nname = \"portal\"\n"
	out, changed, err := MergePytestNorecursedirs("pyproject.toml", []byte(in))
	if err != nil || !changed {
		t.Fatalf("%v, %v", changed, err)
	}
	want := in + "\n[tool.pytest.ini_options]\nnorecursedirs = [\"*.egg\", \".*\", \"_darcs\", \"build\", \"CVS\", \"dist\", \"node_modules\", \"venv\", \"{arch}\", \"experiments\", \"screenshots\"]\n"
	if string(out) != want {
		t.Fatalf("got:\n%s\nwant:\n%s", out, want)
	}
	again, changed, err := MergePytestNorecursedirs("pyproject.toml", out)
	if err != nil || changed || string(again) != string(out) {
		t.Fatalf("a second merge changed the file: %v, %v\n%s", changed, err, again)
	}
}

// A setting of the project's own keeps its patterns and gains the missing
// scratch directories, in either of pytest's spellings; every other byte is
// kept.
func TestNorecursedirsKeepsTheProjectsPatterns(t *testing.T) {
	hygiene.Isolate(t)
	array := "# tools\n[tool.pytest.ini_options]\nnorecursedirs = [\"vendor\", \"experiments\"]\ntestpaths = [\"tests\"]\n"
	out, changed, err := MergePytestNorecursedirs("pyproject.toml", []byte(array))
	if err != nil || !changed {
		t.Fatalf("%v, %v", changed, err)
	}
	text := string(out)
	if !strings.HasPrefix(text, "# tools\n") || !strings.Contains(text, `"vendor"`) || !strings.Contains(text, `"screenshots"`) || strings.Count(text, `"experiments"`) != 1 || !strings.Contains(text, "testpaths = [\"tests\"]") {
		t.Fatalf("an array setting:\n%s", text)
	}
	if strings.Contains(text, `"node_modules"`) {
		t.Fatalf("pytest's default was added to a setting of the project's own:\n%s", text)
	}
	spaced := "[tool.pytest.ini_options]\nnorecursedirs = \"vendor .git\"\n"
	out, changed, err = MergePytestNorecursedirs("pyproject.toml", []byte(spaced))
	if err != nil || !changed || !strings.Contains(string(out), `"vendor"`) || !strings.Contains(string(out), `".git"`) || !strings.Contains(string(out), `"experiments"`) {
		t.Fatalf("a string setting: %v, %v\n%s", changed, err, out)
	}
}

func TestNorecursedirsOfAnotherTypeIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	for _, text := range []string{
		"[tool.pytest.ini_options]\nnorecursedirs = 3\n",
		"[tool.pytest.ini_options]\nnorecursedirs = [\"vendor\", 3]\n",
	} {
		if _, _, err := MergePytestNorecursedirs("pyproject.toml", []byte(text)); err == nil || !strings.Contains(err.Error(), "norecursedirs") {
			t.Errorf("%q: %v", text, err)
		}
	}
}
