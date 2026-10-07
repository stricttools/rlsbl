package workflows

import (
	"regexp"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestTheCheckPatternMatchesTheCIJobAndItsMatrixLegs(t *testing.T) {
	hygiene.Isolate(t)
	pattern, err := CheckPatternForTargets([]string{"go", "npm"})
	if err != nil {
		t.Fatal(err)
	}
	re := regexp.MustCompile(pattern)
	for name, want := range map[string]bool{"test": true, "test (20)": true, "testing": false, "lint": false} {
		if re.MatchString(name) != want {
			t.Errorf("%q against %s: want %v", name, pattern, want)
		}
	}
	if _, err := CheckPatternForTargets([]string{"zig"}); err == nil {
		t.Fatal("a target rlsbl scaffolds no CI for was given a pattern")
	}
	if _, err := CheckPatternForTargets(nil); err == nil {
		t.Fatal("no targets were given a pattern")
	}
}

func TestTheStandaloneWaitForCIJobCarriesItsPattern(t *testing.T) {
	hygiene.Isolate(t)
	text, err := WaitForCIJob(`^(test)( \(.*\))?$`)
	if err != nil {
		t.Fatal(err)
	}
	doc := parseYAML(t, "jobs:\n"+text)
	j := job(t, doc, WaitForCIJobKey)
	env := j["env"].(map[string]any)
	if env["CI_CHECK_PATTERN"] != `^(test)( \(.*\))?$` || env["GH_REPO"] != "${{ github.repository }}" {
		t.Fatalf("env: %v", env)
	}
	perms := j["permissions"].(map[string]any)
	if perms["checks"] != "read" || perms["contents"] != "read" {
		t.Fatalf("permissions: %v", perms)
	}
	steps := j["steps"].([]any)
	if len(steps) != 1 || !strings.Contains(steps[0].(map[string]any)["run"].(string), "rlsbl-ci-sha") {
		t.Fatalf("steps: %v", steps)
	}
	if _, err := WaitForCIJob("("); err == nil {
		t.Fatal("a pattern that is not a regular expression was accepted")
	}
}

func TestTheRouterJobPicksTheProjectByTagLongestSchemeFirst(t *testing.T) {
	hygiene.Isolate(t)
	text, err := RouterWaitForCIJob([]ReleasingProject{
		{Tag: TagParts{Prefix: "kernel/v"}, CheckPattern: "^(kernel-ci) / "},
		{Tag: TagParts{Prefix: "kernel/vulkan/v"}, CheckPattern: "^(vulkan-ci) / "},
		{Tag: TagParts{Prefix: "kernel/v"}, CheckPattern: "^(kernel-tools-ci) / "},
	})
	if err != nil {
		t.Fatal(err)
	}
	doc := parseYAML(t, "jobs:\n"+text)
	j := job(t, doc, WaitForCIJobKey)
	if j["env"].(map[string]any)["TAG_INPUT"] != "${{ inputs.tag }}" {
		t.Fatalf("env: %v", j["env"])
	}
	steps := j["steps"].([]any)
	resolver := steps[0].(map[string]any)["run"].(string)
	vulkan := strings.Index(resolver, "'kernel/vulkan/v'*)")
	kernel := strings.Index(resolver, "'kernel/v'*)")
	if vulkan < 0 || kernel < 0 || vulkan > kernel {
		t.Fatalf("the longer scheme is not tried first:\n%s", resolver)
	}
	if !strings.Contains(resolver, "pattern='(^(kernel-ci) / |^(kernel-tools-ci) / )'") {
		t.Fatalf("one scheme's members do not share a branch:\n%s", resolver)
	}
}

// bannedJobWords are words the house style keeps out of generated text,
// spelled in parts so this file does not carry them.
var bannedJobWords = []string{"ga" + "te", "rem" + "edy", "can" + "ary", "ki" + "nd"}

func TestNoGeneratedJobNameUsesABannedWord(t *testing.T) {
	hygiene.Isolate(t)
	standalone, err := WaitForCIJob("^(test)$")
	if err != nil {
		t.Fatal(err)
	}
	router, err := RouterWaitForCIJob([]ReleasingProject{{Tag: TagParts{Prefix: "v"}, CheckPattern: "^(a) / "}})
	if err != nil {
		t.Fatal(err)
	}
	for _, text := range []string{standalone, router} {
		lower := strings.ToLower(text)
		for _, word := range bannedJobWords {
			if strings.Contains(lower, word) {
				t.Fatalf("the wait-for-ci job carries %q:\n%s", word, text)
			}
		}
	}
}
