package workflows

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"
)

func TestTheCheckPatternMatchesTheCIJobAndItsMatrixEntries(t *testing.T) {
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
	if !strings.Contains(resolver, "pattern='(^(kernel-ci) / |^(kernel-tools-ci) / )'") {
		t.Fatalf("one scheme's members do not share a branch:\n%s", resolver)
	}
	for _, c := range []struct{ tag, scheme, pattern string }{
		{"kernel/v0.2.0", "kernel/v{version}", "(^(kernel-ci) / |^(kernel-tools-ci) / )"},
		{"kernel/vulkan/v0.1.0", "kernel/vulkan/v{version}", "^(vulkan-ci) / "},
	} {
		scheme, pattern, ok := runResolver(t, resolver, c.tag)
		if !ok || scheme != c.scheme || pattern != c.pattern {
			t.Errorf("%s: scheme %q, pattern %q, judged %t", c.tag, scheme, pattern, ok)
		}
	}
}

// runResolver runs the router's resolver step in bash for tag, and returns
// the tag scheme it put in the step's outputs, the check pattern it put in
// the job's environment, and whether it accepted the tag.
func runResolver(t *testing.T, resolver, tag string) (scheme, pattern string, ok bool) {
	t.Helper()
	bash, err := exec.LookPath("bash")
	if err != nil {
		t.Fatalf("the router's steps run in bash: %v", err)
	}
	dir := t.TempDir()
	env, output := filepath.Join(dir, "env"), filepath.Join(dir, "output")
	cmd := exec.Command(bash, "-c", resolver)
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "TAG_INPUT=" + tag, "GITHUB_ENV=" + env, "GITHUB_OUTPUT=" + output}
	if out, err := cmd.CombinedOutput(); err != nil {
		if !strings.Contains(string(out), "is no tag of any project's tag scheme") {
			t.Fatalf("the resolver failed for %s other than by refusing the tag: %v\n%s", tag, err, out)
		}
		return "", "", false
	}
	read := func(path, key string) string {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		for _, line := range strings.Split(string(data), "\n") {
			if v, found := strings.CutPrefix(line, key+"="); found {
				return v
			}
		}
		t.Fatalf("%s holds no %s:\n%s", path, key, data)
		return ""
	}
	return read(output, RouterTagSchemeOutput), read(env, "CI_CHECK_PATTERN"), true
}

// The router judges a tag by each scheme's tag matcher, never by a prefix:
// v{version} claims no video-proc@v0.1.0, and kernel/v{version} no
// kernel/vulkan/v0.1.0.
func TestTheRouterJudgesATagByTheTagMatcher(t *testing.T) {
	hygiene.Isolate(t)
	text, err := RouterWaitForCIJob([]ReleasingProject{
		{Tag: TagParts{Prefix: "v"}, CheckPattern: "^(app) / "},
		{Tag: TagParts{Prefix: "kernel/v"}, CheckPattern: "^(kernel-ci) / "},
	})
	if err != nil {
		t.Fatal(err)
	}
	resolver := job(t, parseYAML(t, "jobs:\n"+text), WaitForCIJobKey)["steps"].([]any)[0].(map[string]any)["run"].(string)
	for tag, want := range map[string]string{
		"v0.1.0":               "v{version}",
		"v10.2.30":             "v{version}",
		"kernel/v1.2.3":        "kernel/v{version}",
		"video-proc@v0.1.0":    "",
		"vlatest":              "",
		"v01.2.3":              "",
		"v1.2.3-rc.1":          "",
		"kernel/vulkan/v0.1.0": "",
	} {
		scheme, _, ok := runResolver(t, resolver, tag)
		if got := map[bool]string{true: scheme, false: ""}[ok]; got != want {
			t.Errorf("%s: judged a tag of %q, want %q", tag, got, want)
		}
	}
}

// bannedWords match the words the house style keeps out of generated
// prose, each spelled with a split so this file does not carry it.
var bannedWords = regexp.MustCompile(`(?i)\b(` + strings.Join([]string{
	"an" + "chor\\w*", "ar" + "m", "blem" + "ish\\w*", "can" + "ar(y|ies)", "cere" + "mon\\w*", "codi" + "f\\w*",
	"cr" + "uft", "cu" + "te", "dan" + "c(e|es|ed|ing)", "disso" + "lv\\w*", "doss" + "ier\\w*", "envel" + "ope\\w*",
	"ess" + "ays?", "exa" + "ctly", "fle" + "ets?", "foot" + "guns?", "ga" + "t(e|es|ed|ing)", "ga" + "tekeep\\w*",
	"genu" + "inely", "gol" + "den", "ki" + "nd\\w*", "lan" + "ds?", "led" + "gers?", "le" + "gs?", "line" + "ages?",
	"ora" + "cles?", "preci" + "sely", "prove" + "nance", "quies" + "cent", "rati" + "f\\w*", "re" + "al(ly|s)?",
	"reme" + "d\\w*", "ri" + "d(e|es|ing)", "scratch" + "pads?", "sharp" + "en\\w*", "side" + "cars?", "so" + "ak\\w*",
	"surv" + "iv\\w*", "tee" + "th", "tomb" + "stone\\w*", "trench" + "coats?", "unusu" + "ally", "wrin" + "kles?",
	"par" + "k(s|ed|ing)?", "flo" + "ors?", "mat" + "ters",
}, "|") + `)\b`)

// prose is the natural-language text of a generated workflow: job keys,
// job and step names, and comments, with flags and code spans removed, since
// a tool's own spelling may be quoted.
func prose(text string) []string {
	flagsAndCode := regexp.MustCompile("--[A-Za-z0-9-]+|`[^`]*`")
	var out []string
	for _, line := range strings.Split(text, "\n") {
		trimmed := strings.TrimSpace(line)
		trimmed = strings.TrimPrefix(trimmed, "- ")
		switch {
		case strings.HasPrefix(trimmed, "#"), strings.HasPrefix(trimmed, "name:"):
		case strings.HasPrefix(line, "  ") && !strings.HasPrefix(line, "   ") && strings.HasSuffix(trimmed, ":"):
		default:
			continue
		}
		out = append(out, flagsAndCode.ReplaceAllString(trimmed, ""))
	}
	return out
}

func TestNoGeneratedWorkflowProseUsesABannedWord(t *testing.T) {
	hygiene.Isolate(t)
	standalone, err := WaitForCIJob("^(test)$")
	if err != nil {
		t.Fatal(err)
	}
	router, err := RouterWaitForCIJob([]ReleasingProject{{Tag: TagParts{Prefix: "v"}, CheckPattern: "^(a) / "}})
	if err != nil {
		t.Fatal(err)
	}
	npm, err := NPMPackagingJobs(NPMPackaging{GoBinaryRelease: portalBinary(), Package: "portal", Dir: "npm", License: "MIT", Provenance: true, RepositoryURL: "https://github.com/acme/portal", Actions: testActions})
	if err != nil {
		t.Fatal(err)
	}
	wheel, err := WheelJob(WheelPackaging{GoBinaryRelease: portalBinary(), Dir: ".", Attestations: true, Actions: testActions})
	if err != nil {
		t.Fatal(err)
	}
	ciRouter, _, err := CIRouter(fixtureWorkspace(t, threeMembers, routerWorkspace(t, nil)), RouterInputs{Actions: routerActions})
	if err != nil {
		t.Fatal(err)
	}
	publish, err := PublishRouter(fixtureWorkspace(t, withTools, publishFixture(map[string]string{"packages/core/.github/workflows/publish.yml": goBinaryPublish(t)})), PublishInputs{})
	if err != nil {
		t.Fatal(err)
	}
	for label, text := range map[string]string{
		"the wait-for-ci job": standalone, "the router's wait-for-ci job": router,
		"the npm jobs": "jobs:\n" + npm, "the wheel job": "jobs:\n" + wheel,
		"the CI router": ciRouter, "the publish router": publish.Text,
	} {
		lines := prose(text)
		if len(lines) == 0 {
			t.Fatalf("no prose was read from %s", label)
		}
		for _, line := range lines {
			if word := bannedWords.FindString(line); word != "" {
				t.Errorf("%s carries %q: %s", label, word, line)
			}
		}
	}
	if !strings.Contains(strings.Join(prose("jobs:\n"+npm), "\n"), "name: Publish the npm package portal-linux-x64") {
		t.Error("the npm platform jobs' names were not read")
	}
}
