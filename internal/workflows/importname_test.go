package workflows

import (
	"reflect"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestImportNamesAreDeclaredWhereThePackageDirectoryDiffers(t *testing.T) {
	hygiene.Isolate(t)
	decls := threeMembers + `
[[releasables]]
name = "cf"
tag_format = "{name}@v{version}"
publish_mode = "none"

[[members]]
path = "cloudflare"
name = "cloudflare"
releasable = "cf"

[[releasables]]
name = "known"
tag_format = "{name}@v{version}"
publish_mode = "none"

[[members]]
path = "known"
name = "known"
releasable = "known"
import_name = "kn"
`
	// web: a src layout whose package is named otherwise; core: the package
	// named as the member; cloudflare: uv's build backend names the module
	// root; known: a declared import_name is kept.
	w := fixtureWorkspace(t, decls, map[string]string{
		"apps/web/pyproject.toml":         "[project]\nname = \"webapp\"\n",
		"apps/web/src/webapp/__init__.py": "",
		"packages/core/pyproject.toml":    "[project]\nname = \"core\"\n",
		"packages/core/core/__init__.py":  "",
		"cloudflare/pyproject.toml":       "[project]\nname = \"cf\"\n\n[tool.uv.build-backend]\nmodule-root = \"lib\"\n",
		"known/pyproject.toml":            "[project]\nname = \"other\"\n",
		"known/other/__init__.py":         "",
	})
	got, err := ImportNames(w)
	if err != nil {
		t.Fatal(err)
	}
	want := []ImportName{{Member: "web", ImportName: "webapp"}, {Member: "cloudflare", ImportName: "cf"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}
