package scaffold

import (
	"regexp"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestThePinnedActionsResolve(t *testing.T) {
	hygiene.Isolate(t)
	if v, err := ActionVersion("actions/checkout"); err != nil || v != "v6" {
		t.Fatalf("actions/checkout = %q, %v", v, err)
	}
	if ref, err := Action("actions/checkout"); err != nil || ref != "actions/checkout@v6" {
		t.Fatalf("the full reference = %q, %v", ref, err)
	}
	if v, err := ActionVersion("dorny/paths-filter"); err != nil || v != "v4" {
		t.Fatalf("dorny/paths-filter = %q, %v", v, err)
	}
	v, err := ActionVersion("goreleaser/goreleaser")
	if err != nil || !regexp.MustCompile(`^v\d+\.\d+\.\d+$`).MatchString(v) {
		t.Fatalf("the goreleaser distribution is pinned exactly: %q, %v", v, err)
	}
	names, err := PinnedActions()
	if err != nil || len(names) == 0 {
		t.Fatalf("the table lists nothing: %v", err)
	}
}

func TestAnUnpinnedActionIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	_, err := ActionVersion("nonexistent/action")
	if err == nil || !strings.Contains(err.Error(), "nonexistent/action") || !strings.Contains(err.Error(), "actions.toml") {
		t.Fatalf("err = %v", err)
	}
	_, err = Render("ci.yml.tpl", `uses: {{action "nonexistent/action"}}`, Vars{})
	if err == nil || !strings.Contains(err.Error(), "ci.yml.tpl") {
		t.Fatalf("a template naming it: %v", err)
	}
}

func TestRenderSubstitutesActionsVariablesAndBlocks(t *testing.T) {
	hygiene.Isolate(t)
	text := "uses: {{action \"actions/checkout\"}}\n" +
		"version: {{actionVersion \"goreleaser/goreleaser\"}}\n" +
		"name: {{name}} {{npm.nodeMatrix}}\n" +
		"{{#if on}}kept {{name}}\n{{/if}}{{#if off}}dropped\n{{/if}}end\n"
	got, err := Render("t", text, Vars{"name": "portal", "npm.nodeMatrix": "[22, 24]", "on": "1", "off": ""})
	if err != nil {
		t.Fatal(err)
	}
	pinned, _ := ActionVersion("goreleaser/goreleaser")
	want := "uses: actions/checkout@v6\nversion: " + pinned + "\nname: portal [22, 24]\nkept portal\nend\n"
	if got != want {
		t.Fatalf("got:\n%s\nwant:\n%s", got, want)
	}
}

func TestADroppedBlockLeavesNoRunOfBlankLines(t *testing.T) {
	hygiene.Isolate(t)
	got, err := Render("t", "a\n\n{{#if x}}b\n{{/if}}\n\nc\n", Vars{"x": ""})
	if err != nil || got != "a\n\nc\n" {
		t.Fatalf("got %q, %v", got, err)
	}
}

func TestAnUnansweredPlaceholderIsRefused(t *testing.T) {
	hygiene.Isolate(t)
	_, err := Render("pypi/ci.yml.tpl", "{{importName}} {{other}}", Vars{"other": "x"})
	if err == nil || !strings.Contains(err.Error(), "importName") || !strings.Contains(err.Error(), "pypi/ci.yml.tpl") {
		t.Fatalf("err = %v", err)
	}
}

func TestGitHubExpressionsAndEscapesPassThrough(t *testing.T) {
	hygiene.Isolate(t)
	text := "ref: ${{ inputs.tag || github.ref_name }}\ntight: ${{github.sha}}\nliteral: \\{{version}}\ngoreleaser: {{ .Version }} {{.Os}}\n"
	got, err := Render("t", text, Vars{})
	if err != nil {
		t.Fatal(err)
	}
	want := "ref: ${{ inputs.tag || github.ref_name }}\ntight: ${{github.sha}}\nliteral: {{version}}\ngoreleaser: {{ .Version }} {{.Os}}\n"
	if got != want {
		t.Fatalf("got:\n%s\nwant:\n%s", got, want)
	}
}

func TestAValueIsNeverRescanned(t *testing.T) {
	hygiene.Isolate(t)
	got, err := Render("t", "{{job}}", Vars{"job": "token: ${{ secrets.GITHUB_TOKEN }} {{name}}"})
	if err != nil || got != "token: ${{ secrets.GITHUB_TOKEN }} {{name}}" {
		t.Fatalf("got %q, %v", got, err)
	}
}

func TestABlankRunAwayFromAnyBlockIsKept(t *testing.T) {
	hygiene.Isolate(t)
	text := "import sys\n\n\ndef main():\n    pass\n{{#if extra}}\nextra\n{{/if}}\n\n\n\nend\n"
	for value, want := range map[string]string{
		"":    "import sys\n\n\ndef main():\n    pass\n\nend\n",
		"yes": "import sys\n\n\ndef main():\n    pass\n\nextra\n\nend\n",
	} {
		got, err := Render("t", text, Vars{"extra": value})
		if err != nil {
			t.Fatal(err)
		}
		if got != want {
			t.Errorf("with extra %q:\n%q\nwant\n%q", value, got, want)
		}
	}
}
